"""Bounded Linux system-service observation, shutdown, and verification.

Filesystem publication and restart guards belong to the durable coordinator.
This adapter never treats a process scan or an idle main PID as shutdown proof.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Protocol

from codereeve.chain.doctor import Phase
from codereeve.provenance import Provenance, validate_provenance

from .health import evidence_document, validate_doctor, validate_heartbeat
from .model import (
    CutoverError,
    ServiceSnapshot,
    ServiceSpec,
    StartEvidence,
    UnitState,
    service_path,
)
from .render import render_unit

OLD_UNIT = "bh-daemon.service"
NEW_UNIT = "codereeve.service"
_PROPERTIES = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "UnitFileState",
    "MainPID",
    "InvocationID",
    "ControlGroup",
    "FragmentPath",
    "DropInPaths",
    "KillMode",
    "User",
    "Job",
    "ExecStart",
)
_ENABLED = {"enabled", "disabled", "masked", "masked-runtime"}


def _console_script_is_supported(source: str) -> bool:
    """Accept only AST-equivalent pip/uv stubs for the two legacy entry points.

    Args:
        source: Bounded UTF-8 console-script source, never executed.

    Returns:
        Whether the entire script matches a supported generated stub.
    """
    observed = ast.dump(ast.parse(source), include_attributes=False)
    for module, function in (
        ("baton_harness.chain.cli", "main"),
        ("codereeve.legacy_cli", "daemon_main"),
    ):
        import_line = f"from {module} import {function}\n"
        exit_line = f"    sys.exit({function}())\n"
        guard = "if __name__ == '__main__':\n"
        pip_cleanup = (
            "    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', "
            "'', sys.argv[0])\n"
        )
        uv_cleanup = (
            "    if sys.argv[0].endswith('-script.pyw'):\n"
            "        sys.argv[0] = sys.argv[0][:-11]\n"
            "    elif sys.argv[0].endswith('.exe'):\n"
            "        sys.argv[0] = sys.argv[0][:-4]\n"
        )
        for template in (
            "import re\nimport sys\n"
            + import_line
            + guard
            + pip_cleanup
            + exit_line,
            "import re\nimport sys\n"
            + guard
            + "    "
            + import_line
            + pip_cleanup
            + exit_line,
            "import sys\n" + import_line + guard + uv_cleanup + exit_line,
        ):
            if observed == ast.dump(
                ast.parse(template), include_attributes=False
            ):
                return True
    return False


def _read_regular_file(path: Path, limit: int) -> bytes:
    """Read bounded regular-file evidence without executing a launcher."""
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError
    return content


def _wall_clock() -> datetime:
    """Return timezone-aware UTC for heartbeat freshness comparisons."""
    return datetime.now(timezone.utc)


def _quote_transient_environment(value: str) -> str:
    """C-escape a D-Bus environment token without unit specifier expansion."""
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise CutoverError("transient environment is invalid")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _read_heartbeat(path: Path) -> str:
    """Read bounded regular-file evidence after rejecting unsafe ancestors."""
    from codereeve.paths import validate_safe_file_path

    if not validate_safe_file_path(path, label="process heartbeat"):
        raise CutoverError("process heartbeat is unavailable")
    with path.open(encoding="utf-8") as stream:
        return stream.read(65_536)


@dataclass(frozen=True)
class PreflightEvidence:
    """Verified prior service metadata and separate installed provenance."""

    old: UnitState
    new: UnitState
    provenance: Provenance
    service_uids: frozenset[int]


class Runner(Protocol):
    """Inject only the bounded argv subprocess boundary."""

    def __call__(
        self, argv: tuple[str, ...], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        """Return captured output without performing shell interpretation."""
        ...


def run_command(
    argv: tuple[str, ...], *, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run a bounded command with a minimal caller environment.

    Args:
        argv: Literal command vector.
        timeout: Positive deadline for the child process.

    Returns:
        Captured exit status and UTF-8 output.
    """
    return subprocess.run(
        argv,
        timeout=timeout,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "SYSTEMD_COLORS": "0",
            "SYSTEMD_PAGER": "cat",
        },
    )


def _uid_for_user(user: str) -> int:
    """Resolve the target account on Linux, refusing a root service."""
    if sys.platform == "win32":
        raise CutoverError("Linux service identity required")
    import pwd

    try:
        uid = int(pwd.getpwnam(user).pw_uid)
        if uid <= 0:
            raise ValueError
        return uid
    except (KeyError, ValueError):
        raise CutoverError(
            "dedicated non-root service account required"
        ) from None


def _platform_check() -> None:
    """Require root authority and full host proc/cgroup v2 mount visibility."""
    try:
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            raise ValueError
        mounts = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        records = [line.split() for line in mounts.splitlines()]
        for target, kind in (("/proc", "proc"), ("/sys/fs/cgroup", "cgroup2")):
            matches = [
                r
                for r in records
                if r[4] == target and r[r.index("-") + 1] == kind
            ]
            if len(matches) != 1 or matches[0][3] != "/":
                raise ValueError
            if any(
                x.startswith("hidepid=") and x != "hidepid=0"
                for field in matches[0]
                for x in field.split(",")
            ):
                raise ValueError
        if not Path("/sys/fs/cgroup/cgroup.controllers").is_file():
            raise ValueError
        if not Path("/run/systemd/system").is_dir():
            raise ValueError
    except (OSError, ValueError, IndexError):
        raise CutoverError(
            "complete root systemd visibility required"
        ) from None


def _trusted_unit(state: UnitState) -> None:
    """Require an administrator-owned fragment without foreign drop-ins."""
    if state.dropin_paths:
        raise CutoverError("unexpected service drop-ins")
    if state.load_state in {"not-found", "masked"}:
        return
    if state.fragment_path != f"/etc/systemd/system/{state.name}":
        raise CutoverError("unexpected service fragment")
    try:
        path = Path(state.fragment_path)
        for item in (path, *path.parents):
            info = item.lstat()
            if (
                info.st_uid != 0
                or info.st_mode & 0o022
                or stat.S_ISLNK(info.st_mode)
            ):
                raise ValueError
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError
    except (OSError, ValueError):
        raise CutoverError("service fragment ownership is unproven") from None


class SystemdBackend:
    """Observe and control explicit units through injected boundaries."""

    def __init__(
        self,
        *,
        runner: Runner = run_command,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        proc_root: Path = Path("/proc"),
        cgroup_root: Path = Path("/sys/fs/cgroup"),
        platform_check: Callable[[], None] = _platform_check,
        uid_for_user: Callable[[str], int] = _uid_for_user,
        trusted_unit: Callable[[UnitState], None] = _trusted_unit,
        timeout_s: float = 120.0,
        temporary_root: Path | None = None,
        wall_clock: Callable[[], datetime] = _wall_clock,
        read_heartbeat: Callable[[Path], str] = _read_heartbeat,
        filesystem_root: Path = Path("/"),
    ) -> None:
        """Bind bounded commands and explicit host-observation seams.

        Args:
            runner: Captured argv command executor.
            clock: Monotonic deadline clock.
            sleep: Bounded poll delay.
            proc_root: Complete procfs root.
            cgroup_root: Complete cgroup v2 mount root.
            platform_check: Root and supported-platform validator.
            uid_for_user: Dedicated non-root account resolver.
            trusted_unit: Fragment ownership validator.
            timeout_s: Per-operation command and observation deadline.
            temporary_root: Optional private verification staging parent.
            wall_clock: UTC heartbeat clock.
            read_heartbeat: Bounded regular-file heartbeat reader.
            filesystem_root: Target root mapping for installed file evidence.
        """
        self.runner = runner
        self.clock = clock
        self.sleep = sleep
        self.proc_root = proc_root
        self.cgroup_root = cgroup_root
        self.platform_check = platform_check
        self.uid_for_user = uid_for_user
        self.trusted_unit = trusted_unit
        self.timeout_s = timeout_s
        self.temporary_root = temporary_root
        self.wall_clock = wall_clock
        self.read_heartbeat = read_heartbeat
        self.filesystem_root = filesystem_root
        self._known_groups: dict[str, str] = {}
        self._starts: dict[tuple[int, str], datetime] = {}
        self._owned_jobs: set[str] = set()
        self._job_observer: Callable[[str, bool], None] | None = None

    def _command(
        self, argv: tuple[str, ...], *, timeout: float | None = None
    ) -> str:
        """Capture a bounded successful command without echoing its payload."""
        try:
            result = self.runner(
                argv, timeout=self.timeout_s if timeout is None else timeout
            )
            if result.returncode != 0:
                raise ValueError
            return result.stdout
        except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
            raise CutoverError("bounded service command failed") from None

    @property
    def pending_verification_units(self) -> tuple[str, ...]:
        """Return exact owned jobs whose shutdown has not yet been proven."""
        return tuple(sorted(self._owned_jobs))

    def inspect(self, name: str) -> UnitState:
        """Read complete manager properties, refusing omissions and ambiguity.

        Args:
            name: Known service name or an owned verification job name.

        Returns:
            Immutable exact state, including explicit absence.

        Raises:
            CutoverError: If command, schema, or state cannot be verified.
        """
        self.platform_check()
        if name not in {OLD_UNIT, NEW_UNIT} and name not in self._owned_jobs:
            raise CutoverError("unsupported service name")
        text = self._command(
            (
                "systemctl",
                "--system",
                "--no-pager",
                "show",
                "--all",
                "--property=" + ",".join(_PROPERTIES),
                "--",
                name,
            )
        )
        try:
            values: dict[str, str] = {}
            for line in text.splitlines():
                key, sep, value = line.partition("=")
                if not sep or key in values:
                    raise ValueError
                values[key] = value
            if set(values) != set(_PROPERTIES) or values["Id"] != name:
                raise ValueError
            load = values["LoadState"]
            active = values["ActiveState"]
            enabled = values["UnitFileState"]
            if load not in {"loaded", "not-found", "masked"} or active not in {
                "active",
                "inactive",
                "failed",
                "activating",
                "deactivating",
            }:
                raise ValueError
            if (
                enabled not in _ENABLED
                and not (load == "not-found" and enabled == "")
                and not (name in self._owned_jobs and enabled == "transient")
            ):
                raise ValueError
            if not re.fullmatch(r"[0-9]+", values["MainPID"]):
                raise ValueError
            pid = int(values["MainPID"])
            invocation = values["InvocationID"]
            if invocation and not re.fullmatch(r"[0-9a-f]{32}", invocation):
                raise ValueError
            group = values["ControlGroup"]
            if group and (
                not group.startswith("/")
                or group == "/"
                or ".." in PurePosixPath(group).parts
                or "\\" in group
            ):
                raise ValueError
            if active == "active" and (
                pid <= 0 or not invocation or not group
            ):
                raise ValueError
            if load == "not-found" and (
                active != "inactive" or pid or group or values["FragmentPath"]
            ):
                raise ValueError
            executable = ""
            executable_argv = ""
            if values["ExecStart"]:
                match = re.fullmatch(
                    r"\{ path=(/[^;\\\n]+) ; argv\[\]=(.*?)"
                    r" ; ignore_errors=(?:yes|no) ; [^\n]* \}",
                    values["ExecStart"],
                )
                if not match or " ; path=" in values["ExecStart"]:
                    raise ValueError
                executable = match[1]
                executable_argv = match[2]
            state = UnitState(
                name,
                load,
                active,
                enabled,
                pid,
                invocation,
                group,
                values["FragmentPath"],
                tuple(values["DropInPaths"].split()),
                values["KillMode"],
                values["User"],
                executable,
                values["SubState"],
                values["Job"],
                executable_argv,
            )
            if group:
                expected = f"/system.slice/{name}"
                if group != expected:
                    raise ValueError
                self._known_groups[name] = group
            return state
        except (ValueError, KeyError):
            raise CutoverError(
                "systemd state is incomplete or unsupported"
            ) from None

    def cgroup_pids(
        self, group: str, *, allow_absent: bool = False
    ) -> frozenset[int]:
        """Read every domain in a complete cgroup subtree.

        Args:
            group: Previously observed absolute cgroup path.
            allow_absent: Permit manager removal after proven stop.

        Returns:
            All processes in the subtree.

        Raises:
            CutoverError: If any directory or required evidence is unreadable.
        """
        try:
            if not self.cgroup_root.is_dir():
                raise ValueError
            relative = PurePosixPath(group)
            if (
                not relative.is_absolute()
                or group == "/"
                or ".." in relative.parts
            ):
                raise ValueError
            path = self.cgroup_root.joinpath(*relative.parts[1:])
            if allow_absent and not path.exists():
                return frozenset()
            pending = [path]
            pids: set[int] = set()
            populated = False
            while pending:
                current = pending.pop()
                if current.is_symlink():
                    raise ValueError
                if (current / "cgroup.type").read_text(
                    encoding="utf-8"
                ).strip() != "domain":
                    raise ValueError
                events = dict(
                    line.split()
                    for line in (current / "cgroup.events")
                    .read_text(encoding="utf-8")
                    .splitlines()
                )
                if events.get("populated") not in {"0", "1"}:
                    raise ValueError
                populated |= events["populated"] == "1"
                for raw in (
                    (current / "cgroup.procs")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ):
                    if not raw.isdecimal() or int(raw) <= 0:
                        raise ValueError
                    pids.add(int(raw))
                with os.scandir(current) as entries:
                    for entry in entries:
                        if entry.is_symlink():
                            raise ValueError
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
            if populated and not pids:
                raise ValueError
            return frozenset(pids)
        except (OSError, UnicodeError, ValueError):
            raise CutoverError(
                "complete cgroup visibility is unproven"
            ) from None

    def stop_and_verify(self, name: str) -> None:
        """Stop a service and verify inactive state and empty descendants.

        Args:
            name: Explicitly controlled service or verification unit.

        Raises:
            CutoverError: If kill mode or final quiescence is unknown.
        """
        before = self.inspect(name)
        if before.load_state in {"not-found", "masked"}:
            if (
                before.active_state != "inactive"
                or before.main_pid
                or before.job
            ):
                raise CutoverError("masked or absent service is not stopped")
            self._verify_group_empty(f"/system.slice/{name}")
            return
        if before.kill_mode != "control-group":
            raise CutoverError("whole-cgroup shutdown required")
        group = before.control_group or self._known_groups.get(
            name, f"/system.slice/{name}"
        )
        self.cgroup_pids(group, allow_absent=before.active_state == "inactive")
        deadline = self.clock() + self.timeout_s
        self._command(
            ("systemctl", "--system", "--no-pager", "stop", "--", name)
        )
        while self.clock() < deadline:
            state = self.inspect(name)
            if (
                state.active_state == "inactive"
                and state.main_pid == 0
                and not state.job
            ):
                self._verify_group_empty(group)
                return
            self.sleep(min(0.1, max(0.0, deadline - self.clock())))
        raise CutoverError("service shutdown deadline expired")

    def _verify_group_empty(self, group: str) -> None:
        """Cross-check cgroup emptiness against the complete proc view."""
        if self.cgroup_pids(group, allow_absent=True):
            raise CutoverError("service descendants remain active")
        try:
            for process in self.proc_root.iterdir():
                if not process.name.isdecimal():
                    continue
                text = (process / "cgroup").read_text(encoding="utf-8")
                lines = text.splitlines()
                if len(lines) != 1 or not lines[0].startswith("0::/"):
                    raise ValueError
                member = lines[0][3:]
                if member == group or member.startswith(group + "/"):
                    raise ValueError
        except (OSError, UnicodeError, ValueError):
            raise CutoverError(
                "service process quiescence is unproven"
            ) from None

    def verify_process_ownership(
        self,
        spec: ServiceSpec,
        service_uids: frozenset[int],
        *,
        allowed_groups: tuple[str, ...] = (
            "/system.slice/bh-daemon.service",
            "/system.slice/codereeve.service",
        ),
    ) -> None:
        """Reject unmanaged writers; never use this scan as shutdown authority.

        Args:
            spec: Explicit managed-repository selection.
            service_uids: Both old and candidate dedicated account IDs.
            allowed_groups: Exact owned subtrees, empty for quiescent checks.

        Raises:
            CutoverError: If a relevant process is uncontained or hidden.
        """
        self.platform_check()
        if not service_uids or any(uid <= 0 for uid in service_uids):
            raise CutoverError("dedicated non-root service account required")
        try:
            for process in self.proc_root.iterdir():
                if not process.name.isdecimal():
                    continue
                status = dict(
                    line.split(":", 1)
                    for line in (process / "status")
                    .read_text(encoding="utf-8")
                    .splitlines()
                    if ":" in line
                )
                uids = {int(uid) for uid in status["Uid"].split()}
                if len(status["Uid"].split()) != 4:
                    raise ValueError
                if int(process.name) == os.getpid() and uids == {0}:
                    continue
                fields = (
                    (process / "stat")
                    .read_text(encoding="utf-8")
                    .rsplit(") ", 1)[1]
                    .split()
                )
                if fields[0] == "Z" or int(fields[6]) & 0x00200000:
                    continue  # Zombies and kernel threads cannot write state.
                cgroups = (
                    (process / "cgroup")
                    .read_text(encoding="utf-8")
                    .splitlines()
                )
                if len(cgroups) != 1 or not cgroups[0].startswith("0::/"):
                    raise ValueError
                group = cgroups[0][3:]
                contained = any(
                    group == g or group.startswith(g + "/")
                    for g in allowed_groups
                )
                if uids & service_uids and not contained:
                    raise ValueError
                environment = (process / "environ").read_bytes()
                root = spec.project_root.as_posix().encode()
                relevant_env = any(
                    part
                    in {
                        b"CODEREEVE_PROJECT_ROOT=" + root,
                        b"BH_PROJECT_ROOT=" + root,
                    }
                    for part in environment.split(b"\0")
                )
                cwd = (process / "cwd").readlink().as_posix()
                relevant_cwd = cwd == root.decode() or cwd.startswith(
                    root.decode() + "/"
                )
                if (relevant_env or relevant_cwd) and not contained:
                    raise ValueError
        except (OSError, ValueError, KeyError, IndexError, UnicodeError):
            raise CutoverError(
                "process ownership or visibility is unproven"
            ) from None

    def _verification_job(
        self, spec: ServiceSpec, command: tuple[str, ...]
    ) -> str:
        """Run one bounded job and verify cleanup after client failure."""
        name = "codereeve-verify-" + uuid.uuid4().hex + ".service"
        if self._job_observer is not None:
            self._job_observer(name, False)
        self._owned_jobs.add(name)
        group = f"/system.slice/{name}"
        timeout = min(spec.timeout_s, self.timeout_s)
        argv = [
            "systemd-run",
            "--system",
            "--quiet",
            "--wait",
            "--pipe",
            "--collect",
            "--expand-environment=no",
            "--unit=" + name,
            "--description=CodeReeve cutover verification",
            "--property=Type=exec",
            "--property=KillMode=control-group",
            "--property=RuntimeMaxSec=" + str(timeout),
            "--property=TimeoutStopSec=" + str(timeout),
            "--property=User=" + spec.run_user,
            "--property=WorkingDirectory=" + spec.project_root.as_posix(),
            "--property=Environment="
            + " ".join(
                _quote_transient_environment(v)
                for v in (
                    "HOME=" + spec.home.as_posix(),
                    "CODEREEVE_PROJECT_ROOT=" + spec.project_root.as_posix(),
                    "PATH="
                    + service_path(
                        spec.environment.as_posix(), spec.home.as_posix()
                    ),
                )
            ),
        ]
        if spec.secrets is not None:
            argv.append(
                "--property=EnvironmentFile=" + spec.secrets.as_posix()
            )
        argv.extend(("--", *command))
        # Manager runtime limits bound the job even if this process is killed.
        try:
            output = self._command(tuple(argv), timeout=timeout)
        except CutoverError:
            try:
                self.stop_and_verify(name)
            except CutoverError:
                raise CutoverError(
                    "verification cleanup is unproven"
                ) from None
            self._owned_jobs.remove(name)
            if self._job_observer is not None:
                self._job_observer(name, True)
            raise
        self.stop_and_verify(name)
        self._verify_group_empty(group)
        self._owned_jobs.remove(name)
        if self._job_observer is not None:
            self._job_observer(name, True)
        return output

    def preflight(
        self,
        spec: ServiceSpec,
        *,
        owned_snapshot: ServiceSnapshot | None = None,
    ) -> PreflightEvidence:
        """Verify supported containment and candidate installation before stop.

        Args:
            owned_snapshot: Verified prior inactive installation authority.
            spec: Candidate context, selecting compatible source secrets.

        Returns:
            Immutable old/new state, artifact identity, and account IDs.

        Raises:
            CutoverError: If any required installation evidence is missing.
        """
        # These paths are ambiguous in the manager's ExecStart text format.
        if any(
            character in spec.environment.as_posix() for character in ";\\"
        ):
            raise CutoverError("candidate executable path is unsupported")
        old, new = self.inspect(OLD_UNIT), self.inspect(NEW_UNIT)
        if old.load_state == "masked":
            raise CutoverError(
                "masked old service identity requires recovery metadata"
            )
        if owned_snapshot is not None and new.load_state == "loaded":
            raw = self.effective_environment(NEW_UNIT)
            new = replace(
                new,
                environment_digest=hashlib.sha256(
                    json.dumps(
                        raw,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode()
                ).hexdigest(),
            )
        if new.load_state != "not-found" and (
            owned_snapshot is None
            or new.active_state != "inactive"
            or new != owned_snapshot.new
        ):
            raise CutoverError("canonical service already exists")
        uids = {self.uid_for_user(spec.run_user)}
        for state in (old, new):
            if state.dropin_paths:
                raise CutoverError("unexpected service drop-ins")
            self.trusted_unit(state)
            if state.active_state not in {"active", "inactive"} or state.job:
                raise CutoverError("service activation is unstable")
        if old.load_state == "loaded":
            if old.kill_mode != "control-group" or not old.exec_start:
                raise CutoverError("old service containment is unsupported")
            uids.add(self.uid_for_user(old.user))
            candidate = self._target_path(
                spec.environment.as_posix()
            ).resolve()
            old_environment = self._legacy_environment(old)
            if candidate == old_environment:
                raise CutoverError("separate candidate environment required")
            if old.control_group:
                if old.main_pid not in self.cgroup_pids(old.control_group):
                    raise CutoverError(
                        "old main process containment is unproven"
                    )
        self.verify_process_ownership(spec, frozenset(uids))
        with tempfile.TemporaryDirectory(
            prefix="codereeve-verify-", dir=self.temporary_root
        ) as temporary:
            unit = Path(temporary) / NEW_UNIT
            unit.write_text(render_unit(spec), encoding="utf-8", newline="\n")
            self._command(
                (
                    "systemd-analyze",
                    "verify",
                    "--man=no",
                    "--recursive-errors=no",
                    str(unit),
                )
            )
        executable = spec.environment.as_posix() + "/bin/codereeve"
        self._verification_job(spec, (executable, "verify", "--installed"))
        provenance_document = evidence_document(
            self._verification_job(spec, (executable, "provenance"))
        )
        try:
            version = provenance_document["package_version"]
            if not isinstance(version, str):
                raise ValueError
            provenance = validate_provenance(provenance_document, version)
            if provenance.development:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise CutoverError("candidate provenance is unproven") from None
        phases = (Phase.INSTALLATION, Phase.CONFIGURATION)
        report = self._verification_job(
            spec,
            (
                executable,
                "doctor",
                "--strict",
                "--phase",
                "installation",
                "--phase",
                "configuration",
                "--format",
                "json",
            ),
        )
        if validate_doctor(report, phases) != provenance:
            raise CutoverError("candidate artifact identity changed")
        self.service_context(spec, canonical=False)
        self.verify_process_ownership(spec, frozenset(uids))
        return PreflightEvidence(old, new, provenance, frozenset(uids))

    def _target_path(self, value: str) -> Path:
        """Map an absolute target path onto the supplied filesystem root."""
        path = PurePosixPath(value)
        if not path.is_absolute() or ".." in path.parts:
            raise CutoverError("installed executable path is unsupported")
        return self.filesystem_root.joinpath(*path.parts[1:])

    def _legacy_environment(self, state: UnitState) -> Path:
        """Prove the direct legacy entry point and interpreter environment.

        Args:
            state: Complete observed old executable and argument selection.

        Returns:
            Resolved virtualenv root selected by the script's own shebang.

        Raises:
            CutoverError: If the launcher or environment is unsupported.
        """
        try:
            selected = PurePosixPath(state.exec_start)
            argv = state.exec_start_argv.split()
            if (
                selected.name != "bh-daemon"
                or selected.parent.name != "bin"
                or not argv
                or argv[0] != state.exec_start
                or (
                    len(argv) != 1
                    and not (
                        len(argv) == 3
                        and argv[1] == "--workflow"
                        and PurePosixPath(argv[2]).is_absolute()
                    )
                )
            ):
                raise ValueError
            script = self._target_path(state.exec_start).resolve(strict=True)
            if script.name != "bh-daemon" or script.parent.name != "bin":
                raise ValueError
            source = _read_regular_file(script, 16_384).decode("utf-8")
            first_line = source.partition("\n")[0]
            match = re.fullmatch(
                r"#!(/[^\s;\\]+/bin/python(?:[0-9]+(?:\.[0-9]+)?)?)",
                first_line,
            )
            if not match or not _console_script_is_supported(source):
                raise ValueError
            interpreter = self._target_path(match[1])
            # Keep the invocation path: different venvs may symlink the same
            # Python binary; their own pyvenv.cfg selects sys.prefix.
            if (
                interpreter.parent.resolve(strict=True) != script.parent
                or not interpreter.is_file()
            ):
                raise ValueError
            # A nested script launcher would invalidate the shebang evidence.
            # This is a binary-format check, not interpreter attestation.
            binary = interpreter.resolve(strict=True)
            if not stat.S_ISREG(binary.stat().st_mode):
                raise ValueError
            with binary.open("rb") as stream:
                if stream.read(4) != b"\x7fELF":
                    raise ValueError
            marker = script.parent.parent / "pyvenv.cfg"
            if marker.is_symlink():
                raise ValueError
            config = _read_regular_file(marker, 16_384).decode("utf-8")
            if not any(
                line.strip().startswith("home = ")
                for line in config.splitlines()
            ):
                raise ValueError
            return script.parent.parent
        except (OSError, ValueError, SyntaxError, UnicodeError, RuntimeError):
            raise CutoverError(
                "old executable environment is unproven"
            ) from None

    def service_context(
        self, spec: ServiceSpec, *, canonical: bool
    ) -> dict[str, object]:
        """Check service-account access and resolve the heartbeat path.

        Args:
            spec: Exact service selection.
            canonical: Require canonical post-migration state/config paths.

        Returns:
            Schema-checked non-secret access evidence.

        Raises:
            CutoverError: If context or access cannot be verified.
        """
        command = (
            spec.environment.as_posix() + "/bin/python",
            "-I",
            "-c",
            "import json,sys; "
            "from codereeve.service_cutover.health import "
            "probe_service_context; "
            "print(json.dumps(probe_service_context(*sys.argv[1:])))",
            spec.project_root.as_posix(),
            spec.environment.as_posix(),
            spec.run_user,
            spec.home.as_posix(),
            "canonical" if canonical else "compatible",
            spec.workflow.as_posix() if spec.workflow else "",
        )
        raw = evidence_document(self._verification_job(spec, command))
        if (
            set(raw)
            != {
                "schema_version",
                "uid",
                "heartbeat_path",
                "state_path",
                "config_path",
                "runtime_paths",
            }
            or type(raw.get("schema_version")) is not int
            or raw.get("schema_version") != 1
            or type(raw.get("uid")) is not int
            or raw["uid"] != self.uid_for_user(spec.run_user)
        ):
            raise CutoverError("service context is unproven")
        for key in ("heartbeat_path", "state_path", "config_path"):
            value = raw.get(key)
            if (
                not isinstance(value, str)
                or not PurePosixPath(value).is_absolute()
            ):
                raise CutoverError("service context is unproven")
        paths = raw.get("runtime_paths")
        if (
            not isinstance(paths, list)
            or not paths
            or any(
                not isinstance(p, str)
                or not PurePosixPath(p).is_absolute()
                or ".." in PurePosixPath(p).parts
                for p in paths
            )
            or len(set(paths)) != len(paths)
            or raw["heartbeat_path"] not in paths
        ):
            raise CutoverError("runtime publication paths are unproven")
        return raw

    def start(self, spec: ServiceSpec) -> UnitState:
        """Start the published candidate only after old restart is guarded.

        Args:
            spec: Exact service context already published by the coordinator.

        Returns:
            New systemd process and invocation identity.

        Raises:
            CutoverError: If either old isolation or candidate identity fails.
        """
        old = self.inspect(OLD_UNIT)
        if (
            old.load_state not in {"masked", "not-found"}
            or old.active_state != "inactive"
            or old.main_pid
            or old.job
        ):
            raise CutoverError("effective old restart guard required")
        self._verify_group_empty(f"/system.slice/{OLD_UNIT}")
        candidate = self.inspect(NEW_UNIT)
        self.trusted_unit(candidate)
        expected_executable = spec.environment.as_posix() + "/bin/codereeve"
        if (
            candidate.load_state != "loaded"
            or candidate.active_state != "inactive"
            or candidate.kill_mode != "control-group"
            or candidate.dropin_paths
            or candidate.user != spec.run_user
            or candidate.exec_start != expected_executable
        ):
            raise CutoverError("published candidate identity is unproven")
        self._verify_group_empty(f"/system.slice/{NEW_UNIT}")
        started_at = self.wall_clock()
        self._command(
            ("systemctl", "--system", "--no-pager", "start", "--", NEW_UNIT),
            timeout=spec.timeout_s,
        )
        started = self.inspect(NEW_UNIT)
        if (
            started.active_state != "active"
            or started.sub_state != "running"
            or started.user != spec.run_user
            or started.exec_start != expected_executable
            or started.main_pid not in self.cgroup_pids(started.control_group)
        ):
            raise CutoverError(
                "candidate did not start with verified identity"
            )
        self._starts[(started.main_pid, started.invocation_id)] = started_at
        return started

    def verify_health(
        self, spec: ServiceSpec, started: UnitState
    ) -> Provenance:
        """Require strict live doctor and a fresh stable invocation heartbeat.

        Args:
            spec: Canonical service configuration after migration.
            started: Identity returned by this backend's start operation.

        Raises:
            CutoverError: If live checks, deadline, or process evidence fails.
        """
        started_at = self._starts.get(
            (started.main_pid, started.invocation_id)
        )
        if started_at is None or started.name != NEW_UNIT:
            raise CutoverError("candidate start evidence is unavailable")
        executable = spec.environment.as_posix() + "/bin/codereeve"
        report = self._verification_job(
            spec,
            (
                executable,
                "doctor",
                "--strict",
                "--phase",
                "live",
                "--format",
                "json",
            ),
        )
        provenance = validate_doctor(report, (Phase.LIVE,))
        context = self.service_context(spec, canonical=True)
        heartbeat_path = context["heartbeat_path"]
        if not isinstance(heartbeat_path, str):
            raise CutoverError("heartbeat path is unproven")
        deadline = self.clock() + spec.timeout_s
        while self.clock() < deadline:
            before = self.inspect(NEW_UNIT)
            if (
                before.main_pid != started.main_pid
                or before.invocation_id != started.invocation_id
                or before.active_state != "active"
            ):
                raise CutoverError(
                    "candidate invocation changed during health check"
                )
            try:
                text = self.read_heartbeat(Path(heartbeat_path))
                validate_heartbeat(
                    text,
                    pid=started.main_pid,
                    invocation_id=started.invocation_id,
                    started_at=started_at,
                    now=self.wall_clock(),
                )
            except (CutoverError, OSError, UnicodeError):
                self.sleep(min(0.1, max(0.0, deadline - self.clock())))
                continue
            after = self.inspect(NEW_UNIT)
            if (
                after.main_pid != started.main_pid
                or after.invocation_id != started.invocation_id
                or after.active_state != "active"
                or after.control_group != started.control_group
            ):
                raise CutoverError(
                    "candidate invocation changed during health check"
                )
            if self.clock() >= deadline:
                break
            return provenance
        raise CutoverError("candidate health deadline expired")

    @contextmanager
    def verification_lifecycle(
        self, observer: Callable[[str, bool], None]
    ) -> Iterator[None]:
        """Record exact job intent before launch and completion after cleanup.

        Args:
            observer: Durable callback; true means cleanup was proven.

        Yields:
            Control while all verification jobs have durable callbacks.
        """
        if self._job_observer is not None:
            raise CutoverError("verification lifecycle already bound")
        self._job_observer = observer
        try:
            yield
        finally:
            self._job_observer = None

    def cleanup_verification(self, name: str) -> None:
        """Adopt an exact journal-owned job solely for bounded cleanup."""
        if (
            re.fullmatch(r"codereeve-verify-[a-z0-9-]{1,80}\.service", name)
            is None
        ):
            raise CutoverError("invalid verification cleanup identity")
        self._owned_jobs.add(name)
        self.stop_and_verify(name)
        self._verify_group_empty(f"/system.slice/{name}")
        self._owned_jobs.remove(name)

    def target_path(self, logical: Path) -> Path:
        """Map a Linux selection onto the explicit filesystem root."""
        return self._target_path(logical.as_posix())

    def reload(self) -> None:
        """Reload the system manager with bounded root-validated control."""
        self.platform_check()
        self._command(("systemctl", "--system", "--no-pager", "daemon-reload"))

    def set_enabled(self, name: str, enabled: bool) -> UnitState:
        """Change only known-unit persistent enablement and verify it."""
        self.platform_check()
        if name not in {OLD_UNIT, NEW_UNIT} or type(enabled) is not bool:
            raise CutoverError("invalid service enablement selection")
        self._command(
            (
                "systemctl",
                "--system",
                "--no-pager",
                "enable" if enabled else "disable",
                "--",
                name,
            )
        )
        state = self.inspect(name)
        expected = "enabled" if enabled else "disabled"
        if not enabled:
            self.verify_disabled(name)
        if state.enabled_state != expected and not (
            not enabled and state.load_state in {"masked", "not-found"}
        ):
            raise CutoverError("service enablement is unproven")
        return state

    def start_evidence(self, state: UnitState) -> StartEvidence:
        """Export the actual pre-command UTC lower bound for persistence."""
        baseline = self._starts.get((state.main_pid, state.invocation_id))
        if baseline is None or baseline.tzinfo is None:
            raise CutoverError("candidate start evidence is unavailable")
        delta = baseline - datetime(1970, 1, 1, tzinfo=timezone.utc)
        nanoseconds = (
            (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
        ) * 1000
        return StartEvidence(state.main_pid, state.invocation_id, nanoseconds)

    def adopt_start(self, evidence: StartEvidence) -> None:
        """Restore a validated durable start baseline in a fresh backend."""
        try:
            baseline = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
                microseconds=evidence.started_ns // 1000
            )
        except OverflowError:
            raise CutoverError("invalid persisted start evidence") from None
        self._starts[(evidence.pid, evidence.invocation_id)] = baseline

    def effective_environment(self, name: str) -> dict[str, str]:
        """Read the complete loaded manager environment selection.

        Args:
            name: One of the two supported daemon units.

        Returns:
            Raw selected fields for ephemeral comparison, never logging.
        """
        self.platform_check()
        if name not in {OLD_UNIT, NEW_UNIT}:
            raise CutoverError("unsupported service selection")
        raw = self._command(
            (
                "systemctl",
                "--system",
                "--no-pager",
                "show",
                "--all",
                "--property=Environment,WorkingDirectory,EnvironmentFiles",
                "--",
                name,
            )
        )
        result: dict[str, str] = {}
        for line in raw.splitlines():
            key, separator, value = line.partition("=")
            if not separator or key in result or "\\" in value:
                raise CutoverError(
                    "effective service selection is unsupported"
                )
            result[key] = value
        if set(result) != {
            "Environment",
            "WorkingDirectory",
            "EnvironmentFiles",
        }:
            raise CutoverError("effective service selection is incomplete")
        return result

    def verify_selection(
        self, spec: ServiceSpec, *, gate: Path | None
    ) -> UnitState:
        """Compare every loaded daemon argument and environment selection.

        Args:
            spec: Exact published canonical service selection.
            gate: Expected private gate, absent for the permanent unit.

        Returns:
            Fresh state matching all intended loaded selections.
        """
        state = self.inspect(NEW_UNIT)
        self.trusted_unit(state)
        raw = self.effective_environment(NEW_UNIT)
        expected = {
            "HOME": spec.home.as_posix(),
            "CODEREEVE_PROJECT_ROOT": spec.project_root.as_posix(),
            "PATH": service_path(
                spec.environment.as_posix(), spec.home.as_posix()
            ),
        }
        if gate is not None:
            expected["CODEREEVE_CUTOVER_GATE"] = gate.as_posix()
        command = [spec.environment.as_posix() + "/bin/codereeve", "daemon"]
        if spec.workflow is not None:
            command.extend(("--workflow", spec.workflow.as_posix()))
        try:
            assignments = shlex.split(raw["Environment"])
            environment = {}
            for assignment in assignments:
                key, separator, value = assignment.partition("=")
                if not separator or key in environment:
                    raise ValueError
                environment[key] = value
            files = (
                spec.secrets.as_posix() + " (ignore_errors=no)"
                if spec.secrets
                else ""
            )
            if (
                state.load_state != "loaded"
                or state.dropin_paths
                or state.user != spec.run_user
                or state.kill_mode != "control-group"
                or state.exec_start != command[0]
                or "\\" in state.exec_start_argv
                or shlex.split(state.exec_start_argv) != command
                or environment != expected
                or raw["WorkingDirectory"] != spec.project_root.as_posix()
                or raw["EnvironmentFiles"] != files
            ):
                raise ValueError
        except ValueError:
            raise CutoverError("effective service selection differs") from None
        return state

    def restore_activation(self, snapshot: ServiceSnapshot) -> None:
        """Restore activation after verifying original selected files.

        Args:
            snapshot: Typed original manager state, with restored executable
                and argument evidence, never an inferred candidate identity.
        """
        states = (snapshot.old, snapshot.new)
        if sum(s.active_state == "active" for s in states) > 1:
            raise CutoverError("original service activation overlaps")
        for expected in states:
            actual = self.inspect(expected.name)
            self.trusted_unit(actual)
            if (
                actual.load_state != expected.load_state
                or actual.dropin_paths != expected.dropin_paths
                or actual.fragment_path != expected.fragment_path
                or actual.user != expected.user
                or actual.exec_start != expected.exec_start
                or actual.exec_start_argv != expected.exec_start_argv
                or actual.kill_mode != expected.kill_mode
                or actual.active_state
                not in {"inactive", expected.active_state}
            ):
                raise CutoverError("restored service selection is unproven")
        for expected in states:
            if expected.active_state != "active":
                continue
            present = self.inspect(expected.name)
            if present.active_state == "active":
                if present.main_pid not in self.cgroup_pids(
                    present.control_group
                ):
                    raise CutoverError(
                        "restored activation containment is unproven"
                    )
                continue
            self._command(
                (
                    "systemctl",
                    "--system",
                    "--no-pager",
                    "start",
                    "--",
                    expected.name,
                )
            )
            actual = self.inspect(expected.name)
            if (
                actual.active_state != "active"
                or actual.main_pid
                not in self.cgroup_pids(actual.control_group)
            ):
                raise CutoverError("original activation is unproven")

    def account(self, user: str) -> tuple[int, int, Path]:
        """Resolve the actual nonroot UID, primary GID, and account home."""
        if sys.platform == "win32":
            raise CutoverError("Linux service identity required")
        import pwd

        try:
            account = pwd.getpwnam(user)
            if (
                account.pw_uid <= 0
                or account.pw_gid < 0
                or not Path(account.pw_dir).is_absolute()
            ):
                raise ValueError
            return account.pw_uid, account.pw_gid, Path(account.pw_dir)
        except (KeyError, ValueError):
            raise CutoverError(
                "dedicated non-root service account required"
            ) from None

    def legacy_environment(self, state: UnitState) -> Path:
        """Return attested old virtualenv without executing or changing it."""
        return self._legacy_environment(state)

    def verify_shutdown(self, name: str) -> None:
        """Prove inactive state and empty complete cgroup without mutation."""
        state = self.inspect(name)
        if state.active_state != "inactive" or state.main_pid or state.job:
            raise CutoverError("service shutdown is unproven")
        self._verify_group_empty(f"/system.slice/{name}")

    def verify_disabled(self, name: str) -> None:
        """Prove no persistent or runtime target enablement links remain."""
        if name not in {OLD_UNIT, NEW_UNIT}:
            raise CutoverError("unsupported enablement identity")
        for location in ("/etc/systemd/system", "/run/systemd/system"):
            root = self._target_path(location)
            if not root.exists():
                continue
            entries = list(root.iterdir())
            if len(entries) > 10000:
                raise CutoverError("enablement visibility is unsupported")
            for directory in entries:
                if not directory.name.endswith((".wants", ".requires")):
                    continue
                if directory.is_symlink() or not directory.is_dir():
                    raise CutoverError("enablement visibility is unsupported")
                link = directory / name
                if link.exists() or link.is_symlink():
                    raise CutoverError("service enablement links remain")
