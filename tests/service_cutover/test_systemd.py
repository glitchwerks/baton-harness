"""System service observation and complete-cgroup shutdown contracts."""

from __future__ import annotations

import importlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codereeve.service_cutover.model import CutoverError, ServiceSpec

if TYPE_CHECKING:
    from conftest import FakeRunner

    from codereeve.service_cutover.systemd import SystemdBackend


def test_backend_capability_exists() -> None:
    """Catch the absent bounded service backend before implementation."""
    assert importlib.util.find_spec("codereeve.service_cutover.systemd")


def test_inspect_complete_unit_state(backend: SystemdBackend) -> None:
    """Catch lost invocation, cgroup, and prior activation metadata."""
    state = backend.inspect("bh-daemon.service")
    assert (state.main_pid, state.invocation_id, state.kill_mode) == (
        51,
        "a" * 32,
        "control-group",
    )
    assert state.enabled_state == "enabled"
    assert state.exec_start == "/opt/old/bin/bh-daemon"
    assert backend.inspect("codereeve.service").load_state == "not-found"


@pytest.mark.parametrize(
    "mutation",
    ["missing", "duplicate", "pid", "load", "invocation", "traversal", "name"],
)
def test_inspect_refuses_incomplete_properties(
    backend: SystemdBackend, runner: FakeRunner, mutation: str
) -> None:
    """Catch permissive default values authorizing unknown manager state."""
    values = runner.units["bh-daemon.service"]
    if mutation == "missing":
        values.pop("MainPID")
    elif mutation == "duplicate":
        runner.corrupt_show = (
            "\n".join(f"{k}={v}" for k, v in values.items()) + "\nMainPID=0\n"
        )
    else:
        key, value = {
            "pid": ("MainPID", "nan"),
            "load": ("LoadState", "error"),
            "invocation": ("InvocationID", ""),
            "traversal": ("ControlGroup", "/../elsewhere"),
            "name": ("Id", "different.service"),
        }[mutation]
        values[key] = value
    with pytest.raises(CutoverError):
        backend.inspect("bh-daemon.service")


@pytest.mark.parametrize(
    "failure",
    [subprocess.TimeoutExpired("secret-token", 2), OSError("secret-token")],
)
def test_command_failure_does_not_echo_output(
    backend: SystemdBackend, runner: FakeRunner, failure: Exception
) -> None:
    """Catch unbounded or secret-bearing subprocess errors."""
    runner.failure = failure
    with pytest.raises(CutoverError) as exc:
        backend.inspect("bh-daemon.service")
    assert "secret-token" not in str(exc.value)
    assert exc.value.__cause__ is None
    assert runner.timeouts == [2]


def group(root: Path, name: str, pids: str = "") -> Path:
    """Create a disposable cgroup v2 domain with explicit population."""
    path = root / "system.slice" / name
    path.mkdir(parents=True)
    (path / "cgroup.procs").write_text(pids, encoding="utf-8")
    (path / "cgroup.type").write_text("domain\n", encoding="utf-8")
    (path / "cgroup.events").write_text(
        "populated 0\nfrozen 0\n", encoding="utf-8"
    )
    return path


def test_stop_verifies_all_descendants(
    backend: SystemdBackend, runner: FakeRunner
) -> None:
    """Catch main-PID-only shutdown allowing an old worker to survive."""
    group(backend.cgroup_root, "bh-daemon.service")
    worker = group(backend.cgroup_root, "bh-daemon.service/child", "88\n")
    with pytest.raises(CutoverError):
        backend.stop_and_verify("bh-daemon.service")
    (worker / "cgroup.procs").write_text("", encoding="utf-8")
    backend.stop_and_verify("bh-daemon.service")
    assert any("stop" in call for call in runner.calls)


@pytest.mark.parametrize(
    "file", ["cgroup.procs", "cgroup.events", "cgroup.type"]
)
def test_partial_cgroup_visibility_refuses_stop(
    backend: SystemdBackend, runner: FakeRunner, file: str
) -> None:
    """Catch missing subtree evidence being interpreted as emptiness."""
    path = group(backend.cgroup_root, "bh-daemon.service")
    (path / file).unlink()
    with pytest.raises(CutoverError):
        backend.stop_and_verify("bh-daemon.service")
    assert not any("stop" in call for call in runner.calls)


def test_unsupported_killmode_refuses_before_stop(
    backend: SystemdBackend, runner: FakeRunner
) -> None:
    """Catch non-cgroup kill modes entering a mutation transaction."""
    runner.units["bh-daemon.service"]["KillMode"] = "process"
    with pytest.raises(CutoverError):
        backend.stop_and_verify("bh-daemon.service")
    assert not any("stop" in call for call in runner.calls)


def process(
    root: Path, pid: int, uid: int, cgroup: str, *, environment: bytes = b""
) -> None:
    """Create complete read-only proc evidence without a live OS process."""
    path = root / str(pid)
    path.mkdir()
    (path / "status").write_text(
        f"Name:\tworker\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n", encoding="utf-8"
    )
    (path / "stat").write_text(
        f"{pid} (worker) S 1 0 0 0 0 0", encoding="utf-8"
    )
    (path / "cgroup").write_text(f"0::{cgroup}\n", encoding="utf-8")
    (path / "environ").write_bytes(environment)


@pytest.mark.parametrize("uid", [1001, 2001])
def test_outside_worker_refuses_preflight(
    backend: SystemdBackend, spec: ServiceSpec, uid: int
) -> None:
    """Catch escaped workers even after names or environment are changed."""
    process(backend.proc_root, 88, uid, "/user.slice/session.scope")
    with pytest.raises(CutoverError):
        backend.verify_process_ownership(spec, frozenset({1001, 2001}))


def test_masked_service_can_be_observed_and_proven_stopped(
    backend: SystemdBackend, runner: FakeRunner
) -> None:
    """Catch empty regular-file guards being mistaken for malformed units."""
    values = runner.units["bh-daemon.service"]
    values.update(
        LoadState="masked",
        ActiveState="inactive",
        SubState="dead",
        UnitFileState="masked",
        MainPID="0",
        InvocationID="",
        ControlGroup="",
        ExecStart="",
        User="",
        KillMode="",
    )
    backend.stop_and_verify("bh-daemon.service")
    assert not any("stop" in call for call in runner.calls)


def verification_outputs() -> list[str]:
    """Supply candidate output from independent public report fixtures."""
    import json

    from test_health import report

    from codereeve.chain.doctor import Phase

    provenance = report((Phase.INSTALLATION,))["provenance"]
    assert isinstance(provenance, dict)
    return [
        "all invariants passed",
        json.dumps({"schema_version": 1, **provenance}),
        json.dumps(report((Phase.INSTALLATION, Phase.CONFIGURATION))),
        json.dumps(
            {
                "schema_version": 1,
                "uid": 1001,
                "heartbeat_path": (
                    "/srv/project/.baton-harness/heartbeat.identity.json"
                ),
                "state_path": "/srv/project/.baton-harness",
                "config_path": "/srv/project/.bh/config.env",
            }
        ),
    ]


def test_preflight_runs_strict_jobs_with_explicit_service_context(
    backend: SystemdBackend, runner: FakeRunner, spec: ServiceSpec
) -> None:
    """Catch interactive-identity probes and missing installed-wheel checks."""
    group(backend.cgroup_root, "bh-daemon.service", "51\n")
    runner.transient_outputs = verification_outputs()
    evidence = backend.preflight(spec)
    assert evidence.provenance.source_revision == "a" * 40
    jobs = [c for c in runner.calls if c[0] == "systemd-run"]
    assert len(jobs) == 4
    for job in jobs:
        assert "--property=User=runner" in job
        assert "--property=WorkingDirectory=/srv/project" in job
        assert "--property=EnvironmentFile=/etc/codereeve/secrets.env" in job
        assert "--property=RuntimeMaxSec=2" in job
        assert "--expand-environment=no" in job
    assert not any(
        "stop" in c and c[-1] == "bh-daemon.service" for c in runner.calls
    )
    assert any(
        c[0] == "systemd-analyze" and "--recursive-errors=no" in c
        for c in runner.calls
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "canonical",
        "failed",
        "dropin",
        "same-environment",
        "root",
        "missing-checks",
        "wheel",
        "parser",
    ],
)
def test_preflight_refuses_unsupported_installation(
    backend: SystemdBackend,
    runner: FakeRunner,
    spec: ServiceSpec,
    mutation: str,
) -> None:
    """Catch mutation starting before a fully verified deployment exists."""
    group(backend.cgroup_root, "bh-daemon.service", "51\n")
    runner.transient_outputs = verification_outputs()
    values = runner.units["bh-daemon.service"]
    if mutation == "canonical":
        runner.units["codereeve.service"] = {
            **values,
            "Id": "codereeve.service",
        }
    elif mutation == "failed":
        values["ActiveState"] = "failed"
    elif mutation == "dropin":
        values["DropInPaths"] = (
            "/run/systemd/system/bh-daemon.service.d/foreign.conf"
        )
    elif mutation == "same-environment":
        values["ExecStart"] = values["ExecStart"].replace(
            "/opt/old", "/opt/new"
        )
    elif mutation == "root":
        backend.uid_for_user = lambda _: 0
    elif mutation == "missing-checks":
        runner.transient_outputs[2] = '{"selected_phases":[],"checks":[]}'
    elif mutation == "wheel":
        runner.fail_token = "--installed"
    else:
        runner.fail_token = "systemd-analyze"
    with pytest.raises(CutoverError):
        backend.preflight(spec)
    assert not any(
        "stop" in c and c[-1] == "bh-daemon.service" for c in runner.calls
    )


def prepare_start(backend: SystemdBackend, runner: FakeRunner) -> None:
    """Set a guarded old unit and inactive canonical candidate."""
    from conftest import properties

    runner.units["bh-daemon.service"] = properties(
        "bh-daemon.service", absent=True
    )
    runner.units["codereeve.service"] = properties("codereeve.service")
    runner.units["codereeve.service"]["ExecStart"] = runner.units[
        "codereeve.service"
    ]["ExecStart"].replace("/opt/old/bin/bh-daemon", "/opt/new/bin/codereeve")
    path = group(backend.cgroup_root, "codereeve.service")

    def started() -> None:
        (path / "cgroup.procs").write_text("51\n", encoding="utf-8")

    runner.after_start = started
    backend.wall_clock = lambda: datetime(
        2026, 9, 8, 12, 0, 10, tzinfo=timezone.utc
    )
    current = [0.0]
    backend.clock = lambda: current[0]

    def advance(duration: float) -> None:
        current[0] += duration

    backend.sleep = advance


@pytest.mark.parametrize(
    "mutation", [None, "restart", "stale", "missing", "deadline"]
)
def test_health_requires_fresh_heartbeat_and_stable_invocation(
    backend: SystemdBackend,
    runner: FakeRunner,
    spec: ServiceSpec,
    mutation: str | None,
) -> None:
    """Reject doctor-only success, stale evidence, and concurrent restart."""
    from test_health import report

    from codereeve.chain.doctor import Phase

    prepare_start(backend, runner)
    started = backend.start(spec)
    runner.transient_outputs = [
        json.dumps(report((Phase.LIVE,))),
        json.dumps(
            {
                "schema_version": 1,
                "uid": 1001,
                "heartbeat_path": (
                    "/srv/project/.codereeve/heartbeat.identity.json"
                ),
                "state_path": "/srv/project/.codereeve",
                "config_path": "/srv/project/.codereeve/config.env",
            }
        ),
    ]

    def read(_path: Path) -> str:
        if mutation == "missing":
            raise FileNotFoundError
        if mutation == "restart":
            runner.units["codereeve.service"]["InvocationID"] = "b" * 32
        if mutation == "deadline":
            backend.sleep(3)
        return json.dumps(
            {
                "schema_version": 1,
                "pid": 51,
                "invocation_id": "a" * 32,
                "timestamp": "2026-09-08T12:00:00+00:00"
                if mutation == "stale"
                else "2026-09-08T12:00:10+00:00",
            }
        )

    backend.read_heartbeat = read
    if mutation:
        with pytest.raises(CutoverError):
            backend.verify_health(spec, started)
    else:
        backend.verify_health(spec, started)


def test_start_requires_effective_old_restart_guard(
    backend: SystemdBackend, spec: ServiceSpec
) -> None:
    """Catch a start racing an unguarded old service restart."""
    with pytest.raises(CutoverError):
        backend.start(spec)


def test_timed_out_verification_job_requires_proven_cleanup(
    backend: SystemdBackend, runner: FakeRunner, spec: ServiceSpec
) -> None:
    """Reject client timeout with an unobserved verifier still alive."""
    runner.failure = subprocess.TimeoutExpired("secret-token", 2)
    with pytest.raises(CutoverError, match="cleanup is unproven"):
        backend.service_context(spec, canonical=False)
    assert runner.calls[0][0] == "systemd-run"
    assert runner.calls[1][0] == "systemctl"
    assert len(backend.pending_verification_units) == 1


def test_preexisting_mask_does_not_infer_original_account(
    backend: SystemdBackend, runner: FakeRunner, spec: ServiceSpec
) -> None:
    """Catch unknown old UID or installation being inferred from candidate."""
    values = runner.units["bh-daemon.service"]
    values.update(
        LoadState="masked",
        ActiveState="inactive",
        MainPID="0",
        ControlGroup="",
        UnitFileState="masked",
        User="",
        ExecStart="",
    )
    with pytest.raises(CutoverError):
        backend.preflight(spec)
    assert not any(c[0] == "systemd-run" for c in runner.calls)


def test_transient_environment_keeps_literal_percent_and_quotes(
    backend: SystemdBackend,
    runner: FakeRunner,
    spec: ServiceSpec,
) -> None:
    """Catch unit-file specifier escaping being reused for D-Bus properties."""
    from dataclasses import replace
    from pathlib import PurePosixPath
    from typing import cast

    selected = replace(
        spec, home=cast(Path, PurePosixPath('/home/a "b" \\ 50% $cash'))
    )
    runner.transient_outputs = ["probe"]
    backend._verification_job(
        selected, ("/opt/new/bin/codereeve", "provenance")
    )
    job = runner.calls[0]
    env = next(v for v in job if v.startswith("--property=Environment="))
    assert '"HOME=/home/a \\"b\\" \\\\ 50% $cash"' in env
    assert "50%%" not in env


def test_transient_state_is_accepted_only_for_owned_jobs(
    backend: SystemdBackend,
    runner: FakeRunner,
    spec: ServiceSpec,
) -> None:
    """Permit owned transient unit observation during error cleanup."""
    from conftest import properties

    def register() -> None:
        job = next(c for c in runner.calls if c[0] == "systemd-run")
        name = next(
            v.removeprefix("--unit=") for v in job if v.startswith("--unit=")
        )
        runner.units[name] = properties(name)
        runner.units[name]["UnitFileState"] = "transient"

    runner.transient_outputs = ["probe"]
    original = runner.__call__

    def command(
        argv: tuple[str, ...], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        result = original(argv, timeout=timeout)
        if argv[0] == "systemd-run":
            register()
        return result

    backend.runner = command
    backend._verification_job(spec, ("/opt/new/bin/codereeve", "provenance"))
    assert any("stop" in c for c in runner.calls)


def test_absent_cgroup_root_is_not_empty_evidence(
    backend: SystemdBackend,
) -> None:
    """Catch an unavailable cgroup mount authorizing an absent service."""
    backend.cgroup_root.rmdir()
    with pytest.raises(CutoverError):
        backend.stop_and_verify("codereeve.service")


def test_active_main_pid_must_appear_in_cgroup(
    backend: SystemdBackend,
    runner: FakeRunner,
    spec: ServiceSpec,
) -> None:
    """Catch internally inconsistent manager/cgroup snapshots in preflight."""
    group(backend.cgroup_root, "bh-daemon.service")
    runner.transient_outputs = verification_outputs()
    with pytest.raises(CutoverError):
        backend.preflight(spec)


def test_same_account_process_in_owned_cgroup_is_supported(
    backend: SystemdBackend,
    spec: ServiceSpec,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch containment checks rejecting observable owned service workers."""
    process(backend.proc_root, 51, 1001, "/system.slice/bh-daemon.service")
    monkeypatch.setattr(Path, "readlink", lambda _: Path("/srv/project"))
    backend.verify_process_ownership(spec, frozenset({1001}))


def test_external_uid_project_writer_is_rejected(
    backend: SystemdBackend,
    spec: ServiceSpec,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch cwd-bound writers outside the two service identities."""
    process(backend.proc_root, 55, 2001, "/other.service")
    monkeypatch.setattr(Path, "readlink", lambda _: Path("/srv/project"))
    with pytest.raises(CutoverError):
        backend.verify_process_ownership(spec, frozenset({1001}))


def test_proc_membership_overrules_missing_stopped_cgroup(
    backend: SystemdBackend,
) -> None:
    """Catch missing cgroup directories hiding surviving descendants."""
    process(
        backend.proc_root, 55, 1001, "/system.slice/codereeve.service/child"
    )
    with pytest.raises(CutoverError):
        backend.stop_and_verify("codereeve.service")


def test_only_exact_root_coordinator_is_exempt_from_relevance(
    backend: SystemdBackend,
    spec: ServiceSpec,
) -> None:
    """Permit the root coordinator without exempting installer ancestors."""
    import os

    process(backend.proc_root, os.getpid(), 0, "/installer.scope")
    backend.verify_process_ownership(spec, frozenset({1001}))


def test_unknown_verification_name_is_not_control_authority(
    backend: SystemdBackend,
    runner: FakeRunner,
) -> None:
    """Catch accepting any name with the verification-job prefix as owned."""
    with pytest.raises(CutoverError):
        backend.stop_and_verify("codereeve-verify-" + "a" * 32 + ".service")
    assert runner.calls == []


@pytest.mark.parametrize("value", [True, 2, "1"])
def test_context_report_schema_is_strict(
    backend: SystemdBackend,
    runner: FakeRunner,
    spec: ServiceSpec,
    value: object,
) -> None:
    """Catch boolean schema versions accidentally comparing equal to one."""
    raw = json.loads(verification_outputs()[-1])
    raw["schema_version"] = value
    runner.transient_outputs = [json.dumps(raw)]
    with pytest.raises(CutoverError):
        backend.service_context(spec, canonical=False)
