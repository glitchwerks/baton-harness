"""Deterministic external-command fixtures for service cutover tests."""

from __future__ import annotations

import importlib
import subprocess
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import cast

import pytest

from codereeve.service_cutover.model import (
    ServiceSnapshot,
    ServiceSpec,
    UnitState,
)
from codereeve.service_cutover.storage import Storage
from codereeve.service_cutover.systemd import SystemdBackend

CutoverContext = tuple[ServiceSpec, SystemdBackend, Storage]


@pytest.fixture
def spec() -> ServiceSpec:
    """Return target POSIX paths without consulting Windows host services."""

    def path(value: str) -> Path:
        return cast(Path, PurePosixPath(value))

    return ServiceSpec(
        path("/srv/project"),
        path("/opt/new"),
        "runner",
        None,
        path("/etc/codereeve/secrets.env"),
        path("/home/runner"),
        timeout_s=2,
    )


def properties(
    name: str, *, active: bool = False, absent: bool = False
) -> dict[str, str]:
    """Supply complete systemctl properties, including empty values."""
    return {
        "Id": name,
        "LoadState": "not-found" if absent else "loaded",
        "ActiveState": "active" if active else "inactive",
        "SubState": "running" if active else "dead",
        "UnitFileState": "" if absent else "enabled",
        "MainPID": "51" if active else "0",
        "InvocationID": "a" * 32 if active else "",
        "ControlGroup": "/system.slice/" + name if active else "",
        "FragmentPath": "" if absent else "/etc/systemd/system/" + name,
        "DropInPaths": "",
        "KillMode": "" if absent else "control-group",
        "User": "" if absent else "runner",
        "Job": "",
        "ExecStart": ""
        if absent
        else (
            "{ path=/opt/old/bin/bh-daemon ; argv[]=/opt/old/bin/bh-daemon ; "
            "ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; "
            "pid=0 ; code=(null) ; status=0/0 }"
        ),
    }


class FakeRunner:
    """Respond only to supported commands while recording bounded argv."""

    def __init__(self) -> None:
        """Initialize two known units and explicit command result seams."""
        self.units = {
            "bh-daemon.service": properties("bh-daemon.service", active=True),
            "codereeve.service": properties("codereeve.service", absent=True),
        }
        self.calls: list[tuple[str, ...]] = []
        self.timeouts: list[float] = []
        self.failure: Exception | None = None
        self.transient_outputs: list[str] = []
        self.after_stop: Callable[[], None] = lambda: None
        self.after_show: Callable[[], None] = lambda: None
        self.after_start: Callable[[], None] = lambda: None
        self.corrupt_show: str | None = None
        self.returncode = 0
        self.fail_token: str | None = None

    def __call__(
        self, argv: tuple[str, ...], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        """Simulate systemd effects only inside the fixture."""
        self.calls.append(argv)
        self.timeouts.append(timeout)
        if self.failure:
            raise self.failure
        output = ""
        if argv[0] == "systemctl":
            if "show" in argv:
                name = argv[-1]
                values = self.units.get(name, properties(name, absent=True))
                output = (
                    self.corrupt_show
                    if self.corrupt_show is not None
                    else (
                        "\n".join(f"{k}={v}" for k, v in values.items()) + "\n"
                    )
                )
                self.after_show()
            elif "stop" in argv:
                name = argv[-1]
                if name in self.units:
                    self.units[name].update(
                        ActiveState="inactive",
                        SubState="dead",
                        MainPID="0",
                        ControlGroup="",
                    )
                self.after_stop()
            elif "start" in argv:
                self.units[argv[-1]].update(
                    ActiveState="active",
                    SubState="running",
                    MainPID="51",
                    InvocationID="a" * 32,
                    ControlGroup="/system.slice/" + argv[-1],
                )
                self.after_start()
        elif argv[0] == "systemd-run":
            output = self.transient_outputs.pop(0)
        elif argv[0] != "systemd-analyze":
            raise AssertionError("unexpected command")
        return subprocess.CompletedProcess(
            argv,
            1 if self.fail_token in argv else self.returncode,
            output,
            "secret-token",
        )


@pytest.fixture
def runner() -> FakeRunner:
    """Supply a fresh strict command double."""
    return FakeRunner()


@pytest.fixture
def backend(tmp_path: Path, runner: FakeRunner) -> object:
    """Create real filesystem inspection with only host authority injected."""
    assert importlib.util.find_spec("codereeve.service_cutover.systemd")
    module = importlib.import_module("codereeve.service_cutover.systemd")
    (tmp_path / "cgroup").mkdir()
    (tmp_path / "proc").mkdir()
    legacy_installation(tmp_path / "host")
    return module.SystemdBackend(
        runner=runner,
        cgroup_root=tmp_path / "cgroup",
        proc_root=tmp_path / "proc",
        platform_check=lambda: None,
        uid_for_user=lambda _: 1001,
        trusted_unit=lambda _: None,
        timeout_s=2,
        temporary_root=tmp_path,
        filesystem_root=tmp_path / "host",
    )


def legacy_installation(
    root: Path, *, shebang: str = "/opt/old/bin/python"
) -> Path:
    """Write a disposable legacy console-script and virtualenv marker."""
    environment = root / "opt/old"
    (environment / "bin").mkdir(parents=True, exist_ok=True)
    script = environment / "bin/bh-daemon"
    script.write_text(
        f"#!{shebang}\nimport sys\n"
        "from baton_harness.chain.cli import main\n"
        "if __name__ == '__main__':\n"
        "    if sys.argv[0].endswith('-script.pyw'):\n"
        "        sys.argv[0] = sys.argv[0][:-11]\n"
        "    elif sys.argv[0].endswith('.exe'):\n"
        "        sys.argv[0] = sys.argv[0][:-4]\n"
        "    sys.exit(main())\n",
        encoding="utf-8",
        newline="\n",
    )
    (environment / "bin/python").write_bytes(b"\x7fELFdisposable interpreter")
    (environment / "pyvenv.cfg").write_text(
        "home = /usr/bin\ninclude-system-site-packages = false\n",
        encoding="utf-8",
    )
    return script


class Interrupted(BaseException):
    """Simulate abrupt coordinator process loss after a durable boundary."""


@pytest.fixture
def cutover_context(
    tmp_path: Path, spec: ServiceSpec, monkeypatch: pytest.MonkeyPatch
) -> CutoverContext:
    """Run real policy/storage with only POSIX service authority injected."""
    from dataclasses import replace
    from datetime import datetime, timezone

    from codereeve.provenance import Provenance

    def provenance() -> Provenance:
        return Provenance(1, "0.3.0", "a" * 40, "sha256:" + "b" * 64, False)

    from test_storage import portable_storage

    from codereeve.service_cutover import readiness
    from codereeve.service_cutover.model import UnitState
    from codereeve.service_cutover.systemd import (
        PreflightEvidence,
        SystemdBackend,
    )

    root = tmp_path / "host"
    root.mkdir()
    for directory in (
        "srv/project/.codereeve",
        "etc/systemd/system",
        "home/runner/.config",
        "opt/new/bin",
        "run",
    ):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / "srv/project/.codereeve/config.env").write_bytes(
        b"CODEREEVE_REPO_OWNER=owner\n"
    )
    store = portable_storage(root)
    store.owner = lambda: (0, 0)
    metadata_by_inode = {}
    store.metadata = lambda p: metadata_by_inode.get(
        p.stat().st_ino, (0o755 if p.is_dir() else 0o600, 0, 0)
    )
    store.set_metadata = lambda p, mode, uid, gid: metadata_by_inode.update(
        {p.stat().st_ino: (mode, uid, gid)}
    )
    monkeypatch.setattr(
        readiness, "_GATE_ROOT", root / "run/codereeve/cutover"
    )

    class Backend(SystemdBackend):
        def __init__(self) -> None:
            super().__init__(
                filesystem_root=root,
                platform_check=lambda: None,
                uid_for_user=lambda _: 1001,
                trusted_unit=lambda _: None,
            )
            self.states = {
                name: UnitState(
                    name, "not-found", "inactive", "", 0, "", "", "", ()
                )
                for name in ("bh-daemon.service", "codereeve.service")
            }
            self.events = []
            self.active_counts = []
            self.fail_at = None
            self.wall_clock = lambda: datetime(
                2026, 9, 8, 12, 0, 10, tzinfo=timezone.utc
            )

        def event(self, name: str) -> None:
            self.events.append(name)
            self.active_counts.append(
                sum(s.active_state == "active" for s in self.states.values())
            )
            if name == self.fail_at:
                self.fail_at = None
                raise model.CutoverError("injected service failure")

        def inspect(self, name: str) -> UnitState:
            return self.states[name]

        def account(self, user: str) -> tuple[int, int, Path]:
            return (1001, 1001, spec.home)

        def reload(self) -> None:
            self.event("reload")
            for name in self.states:
                path = root / "etc/systemd/system" / name
                state = self.states[name]
                if not path.exists():
                    self.states[name] = UnitState(
                        name, "not-found", "inactive", "", 0, "", "", "", ()
                    )
                elif not path.read_bytes():
                    self.states[name] = replace(
                        state, load_state="masked", enabled_state="masked"
                    )
                else:
                    self.states[name] = replace(
                        state,
                        load_state="loaded",
                        fragment_path="/etc/systemd/system/" + name,
                        user=next(
                            line.removeprefix("User=")
                            for line in path.read_text(
                                encoding="utf-8"
                            ).splitlines()
                            if line.startswith("User=")
                        ),
                        kill_mode="control-group",
                        enabled_state=state.enabled_state
                        if state.enabled_state in {"enabled", "disabled"}
                        else "disabled",
                    )

        def verify_shutdown(self, name: str) -> None:
            assert self.states[name].active_state == "inactive"
            self.event("shutdown_verified:" + name)

        def effective_environment(self, name: str) -> dict[str, str]:
            return {
                "Environment": (
                    "HOME=/home/runner CODEREEVE_PROJECT_ROOT=/srv/"
                    "project PATH=/opt/old/bin:/home/runner/.local/"
                    "bin:/usr/local/bin:/usr/bin:/bin"
                ),
                "WorkingDirectory": "/srv/project",
                "EnvironmentFiles": "",
            }

        def preflight(
            self, selected: ServiceSpec, **kwargs: object
        ) -> PreflightEvidence:
            self.event("preflight")
            return PreflightEvidence(
                self.states["bh-daemon.service"],
                self.states["codereeve.service"],
                provenance(),
                frozenset({1001}),
            )

        def stop_and_verify(self, name: str) -> None:
            self.event("stop:" + name)
            self.states[name] = replace(
                self.states[name],
                active_state="inactive",
                main_pid=0,
                control_group="",
                sub_state="dead",
                job="",
            )
            self.event("stopped:" + name)

        def verify_process_ownership(
            self,
            selected: ServiceSpec,
            uids: frozenset[int],
            *,
            allowed_groups: tuple[str, ...] | None = None,
        ) -> None:
            self.event("writers_checked")

        def service_context(
            self, selected: ServiceSpec, *, canonical: bool
        ) -> dict[str, object]:
            self.event("context")
            return {
                "schema_version": 1,
                "uid": 1001,
                "state_path": "/srv/project/.codereeve",
                "config_path": "/srv/project/.codereeve/config.env",
                "heartbeat_path": (
                    "/srv/project/.codereeve/heartbeat.identity.json"
                ),
                "runtime_paths": [
                    "/srv/project/.codereeve/heartbeat",
                    "/srv/project/.codereeve/heartbeat.identity.json",
                    "/srv/project/.codereeve/session-report.json",
                ],
            }

        def verify_selection(
            self, selected: ServiceSpec, *, gate: Path | None
        ) -> UnitState:
            from codereeve.service_cutover.render import render_unit

            assert (root / "etc/systemd/system/codereeve.service").read_text(
                encoding="utf-8"
            ) == render_unit(selected, gate=gate)
            self.event("selection")
            return self.states["codereeve.service"]

        def start(self, selected: ServiceSpec) -> UnitState:
            self.event("start_new")
            assert self.states["bh-daemon.service"].active_state == "inactive"
            assert self.states["bh-daemon.service"].load_state in {
                "masked",
                "not-found",
            }
            state = replace(
                self.states["codereeve.service"],
                active_state="active",
                main_pid=51,
                invocation_id="a" * 32,
                control_group="/system.slice/codereeve.service",
                sub_state="running",
            )
            self.states[state.name] = state
            self._starts[(51, "a" * 32)] = self.wall_clock()
            self.event("new_started")
            return state

        def verify_health(
            self, selected: ServiceSpec, started: UnitState
        ) -> Provenance:
            self.event("health")
            return provenance()

        def set_enabled(self, name: str, enabled: bool) -> UnitState:
            self.event("enable:" + name + ":" + str(enabled))
            state = self.states[name]
            if state.load_state == "loaded":
                state = replace(
                    state, enabled_state="enabled" if enabled else "disabled"
                )
                self.states[name] = state
            return state

        def restore_activation(self, snapshot: ServiceSnapshot) -> None:
            self.event("restore_activation")
            for state in (snapshot.old, snapshot.new):
                was_active = self.states[state.name].active_state == "active"
                self.states[state.name] = state
                if state.active_state == "active" and not was_active:
                    self.event("old_started")

    from codereeve.service_cutover import model

    backend = Backend()
    return spec, backend, store


@pytest.fixture
def upgrade_context(cutover_context: CutoverContext) -> CutoverContext:
    """Add a real legacy tree and direct environment to the disposable host."""
    from codereeve.service_cutover.model import UnitState

    spec, backend, storage = cutover_context
    root = backend.filesystem_root
    canonical = root / "srv/project/.codereeve"
    (canonical / "config.env").unlink()
    canonical.rmdir()
    legacy = root / "srv/project/.baton-harness"
    legacy.mkdir()
    (legacy / "run.log").write_bytes(b"original runtime")
    config = root / "srv/project/.bh"
    config.mkdir()
    (config / "config.env").write_bytes(
        b"BH_REPO_OWNER=owner\nBH_REPO_NAME=repo\n"
    )
    legacy_installation(root)
    unit = root / "etc/systemd/system/bh-daemon.service"
    unit.write_bytes(
        b"[Service]\nUser=runner\nExecStart=/opt/old/bin/bh-daemon\n"
    )
    backend.states["bh-daemon.service"] = UnitState(
        "bh-daemon.service",
        "loaded",
        "active",
        "enabled",
        41,
        "b" * 32,
        "/system.slice/bh-daemon.service",
        "/etc/systemd/system/bh-daemon.service",
        (),
        "control-group",
        "runner",
        "/opt/old/bin/bh-daemon",
        "running",
        "",
        "/opt/old/bin/bh-daemon",
    )
    return spec, backend, storage
