"""System service observation and complete-cgroup shutdown contracts."""

from __future__ import annotations

import importlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from conftest import legacy_installation

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
    assert state.exec_start_argv == "/opt/old/bin/bh-daemon"
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
                "runtime_paths": [
                    "/srv/project/.baton-harness/heartbeat.identity.json"
                ],
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
                "runtime_paths": [
                    "/srv/project/.codereeve/heartbeat.identity.json"
                ],
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


@pytest.mark.parametrize(
    "mutation",
    [
        "env-wrapper",
        "python-wrapper",
        "wrong-argv0",
        "shebang-candidate",
        "shebang-other",
        "shebang-env",
        "missing-script",
        "missing-pyvenv",
        "disguised-wrapper",
    ],
)
def test_old_environment_requires_direct_entrypoint_evidence(
    backend: SystemdBackend,
    runner: FakeRunner,
    spec: ServiceSpec,
    tmp_path: Path,
    mutation: str,
) -> None:
    """Reject wrappers and scripts selecting unverified environments."""
    root = tmp_path / "host"
    script = legacy_installation(root)
    backend.filesystem_root = root
    group(backend.cgroup_root, "bh-daemon.service", "51\n")
    runner.transient_outputs = verification_outputs()
    values = runner.units["bh-daemon.service"]
    if mutation in {"env-wrapper", "python-wrapper"}:
        path = (
            "/usr/bin/env"
            if mutation == "env-wrapper"
            else "/opt/new/bin/python"
        )
        args = (
            "/usr/bin/env /opt/new/bin/python -m codereeve daemon"
            if mutation == "env-wrapper"
            else "/opt/new/bin/python -m codereeve daemon"
        )
        values["ExecStart"] = (
            f"{{ path={path} ; argv[]={args} ; ignore_errors=no ; "
            "start_time=[n/a] ; stop_time=[n/a] ; pid=51 ; "
            "code=(null) ; status=0/0 }"
        )
    elif mutation == "wrong-argv0":
        values["ExecStart"] = values["ExecStart"].replace(
            "argv[]=/opt/old/bin/bh-daemon", "argv[]=/opt/new/bin/codereeve"
        )
    elif mutation.startswith("shebang-"):
        interpreter = {
            "shebang-candidate": "/opt/new/bin/python",
            "shebang-other": "/opt/third/bin/python",
            "shebang-env": "/usr/bin/env python",
        }[mutation]
        script.write_text(
            script.read_text(encoding="utf-8").replace(
                "#!/opt/old/bin/python", f"#!{interpreter}"
            ),
            encoding="utf-8",
            newline="\n",
        )
    elif mutation == "disguised-wrapper":
        script.write_text(
            "#!/opt/old/bin/python\nimport os\n"
            "os.execv('/opt/new/bin/python', ['python', '-m', 'codereeve'])\n",
            encoding="utf-8",
            newline="\n",
        )
    elif mutation == "missing-script":
        script.unlink()
    else:
        (script.parent.parent / "pyvenv.cfg").unlink()
    with pytest.raises(CutoverError):
        backend.preflight(spec)
    assert not any(
        call[0] == "systemd-run" or "stop" in call for call in runner.calls
    )


@pytest.mark.parametrize(
    "value", ["/opt/new;candidate", "/opt/new\\candidate"]
)
def test_unobservable_rendered_candidate_refuses_before_mutation(
    backend: SystemdBackend,
    runner: FakeRunner,
    spec: ServiceSpec,
    value: str,
) -> None:
    """Reject renderer-legal paths that observation cannot preserve."""
    from dataclasses import replace
    from pathlib import PurePosixPath
    from typing import cast

    from codereeve.service_cutover.render import render_unit

    candidate = replace(spec, environment=cast(Path, PurePosixPath(value)))
    assert "ExecStart=" in render_unit(candidate)
    group(backend.cgroup_root, "bh-daemon.service", "51\n")
    runner.transient_outputs = verification_outputs()
    with pytest.raises(CutoverError):
        backend.preflight(candidate)
    assert not any(
        call[0] in {"systemd-run", "systemd-analyze"} or "stop" in call
        for call in runner.calls
    )


def test_entrypoint_symlink_to_candidate_does_not_hide_environment(
    backend: SystemdBackend,
    runner: FakeRunner,
    spec: ServiceSpec,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Follow the executable alias before interpreting its environment."""
    root = tmp_path / "host"
    original = legacy_installation(root)
    target = root / "opt/new/bin/bh-daemon"
    target.parent.mkdir(parents=True)
    target.write_text(
        original.read_text(encoding="utf-8").replace(
            "/opt/old/bin/python", "/opt/new/bin/python"
        ),
        encoding="utf-8",
        newline="\n",
    )
    (target.parent / "python").write_bytes(b"\x7fELFdisposable interpreter")
    (target.parent.parent / "pyvenv.cfg").write_text(
        "home = /usr/bin\n", encoding="utf-8"
    )
    real_resolve = Path.resolve

    def resolve(path: Path, strict: bool = False) -> Path:
        return real_resolve(
            target if path == original else path, strict=strict
        )

    monkeypatch.setattr(Path, "resolve", resolve)
    backend.filesystem_root = root
    group(backend.cgroup_root, "bh-daemon.service", "51\n")
    runner.transient_outputs = verification_outputs()
    with pytest.raises(
        CutoverError, match="separate candidate environment required"
    ):
        backend.preflight(spec)
    assert not any(call[0] == "systemd-run" for call in runner.calls)


@pytest.mark.parametrize("template", ["pip", "pip-current", "uv"])
@pytest.mark.parametrize("entrypoint", ["legacy", "compatibility"])
def test_standard_installed_console_scripts_are_supported(
    backend: SystemdBackend,
    runner: FakeRunner,
    spec: ServiceSpec,
    template: str,
    entrypoint: str,
) -> None:
    """Retain source-verified distlib and uv entry points without wrappers."""
    script = backend.filesystem_root / "opt/old/bin/bh-daemon"
    source = script.read_text(encoding="utf-8")
    if template.startswith("pip"):
        source = (
            "#!/opt/old/bin/python\nimport re\nimport sys\n"
            "from baton_harness.chain.cli import main\n"
            "if __name__ == '__main__':\n"
            "    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', "
            "'', sys.argv[0])\n"
            "    sys.exit(main())\n"
        )
        if template == "pip-current":
            source = source.replace(
                "from baton_harness.chain.cli import main\n"
                "if __name__ == '__main__':\n",
                "if __name__ == '__main__':\n"
                "    from baton_harness.chain.cli import main\n",
            )
    if entrypoint == "compatibility":
        source = source.replace(
            "baton_harness.chain.cli import main",
            "codereeve.legacy_cli import daemon_main",
        ).replace("sys.exit(main())", "sys.exit(daemon_main())")
    script.write_text(source, encoding="utf-8", newline="\n")
    group(backend.cgroup_root, "bh-daemon.service", "51\n")
    runner.transient_outputs = verification_outputs()
    assert backend.preflight(spec).old.exec_start == "/opt/old/bin/bh-daemon"


def test_legacy_workflow_argument_is_preserved(
    backend: SystemdBackend,
    runner: FakeRunner,
    spec: ServiceSpec,
) -> None:
    """Retain the normal legacy installer workflow selection in evidence."""
    values = runner.units["bh-daemon.service"]
    values["ExecStart"] = values["ExecStart"].replace(
        "argv[]=/opt/old/bin/bh-daemon ;",
        "argv[]=/opt/old/bin/bh-daemon "
        "--workflow /srv/project/config/WORKFLOW.md ;",
    )
    group(backend.cgroup_root, "bh-daemon.service", "51\n")
    runner.transient_outputs = verification_outputs()
    evidence = backend.preflight(spec)
    assert evidence.old.exec_start_argv.endswith(
        "--workflow /srv/project/config/WORKFLOW.md"
    )


def test_shared_base_python_does_not_merge_virtualenv_identity(
    backend: SystemdBackend,
    runner: FakeRunner,
    spec: ServiceSpec,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolve venv roots without equating their shared base interpreter."""
    root = backend.filesystem_root
    global_python = root / "usr/bin/python3"
    global_python.parent.mkdir(parents=True)
    global_python.write_bytes(b"\x7fELFdisposable base interpreter")
    interpreters = {root / "opt/old/bin/python", root / "opt/new/bin/python"}
    real_resolve = Path.resolve

    def resolve(path: Path, strict: bool = False) -> Path:
        return real_resolve(
            global_python if path in interpreters else path, strict=strict
        )

    monkeypatch.setattr(Path, "resolve", resolve)
    group(backend.cgroup_root, "bh-daemon.service", "51\n")
    runner.transient_outputs = verification_outputs()
    assert backend.preflight(spec).old.exec_start == "/opt/old/bin/bh-daemon"


def test_nested_interpreter_launcher_is_rejected(
    backend: SystemdBackend,
    runner: FakeRunner,
    spec: ServiceSpec,
) -> None:
    """Reject a script named Python that forwards execution to candidate."""
    interpreter = backend.filesystem_root / "opt/old/bin/python"
    interpreter.write_bytes(b'#!/bin/sh\nexec /opt/new/bin/python "$@"\n')
    group(backend.cgroup_root, "bh-daemon.service", "51\n")
    runner.transient_outputs = verification_outputs()
    with pytest.raises(
        CutoverError, match="old executable environment is unproven"
    ):
        backend.preflight(spec)
    assert not any(call[0] == "systemd-run" for call in runner.calls)


@pytest.mark.parametrize(
    "mutation", ["oversized", "syntax", "wrong-import", "extra-code", "crlf"]
)
def test_unsupported_console_script_evidence_fails_closed(
    backend: SystemdBackend,
    runner: FakeRunner,
    spec: ServiceSpec,
    mutation: str,
) -> None:
    """Reject unsupported source without executing it."""
    script = backend.filesystem_root / "opt/old/bin/bh-daemon"
    source = script.read_text(encoding="utf-8")
    if mutation == "oversized":
        source += "#" * 16_384
    elif mutation == "syntax":
        source += "("
    elif mutation == "wrong-import":
        source = source.replace("baton_harness.chain.cli", "untrusted.wrapper")
    elif mutation == "extra-code":
        source += "import subprocess\n"
    script.write_text(
        source,
        encoding="utf-8",
        newline="\r\n" if mutation == "crlf" else "\n",
    )
    group(backend.cgroup_root, "bh-daemon.service", "51\n")
    runner.transient_outputs = verification_outputs()
    with pytest.raises(
        CutoverError, match="old executable environment is unproven"
    ):
        backend.preflight(spec)
    assert not any(call[0] == "systemd-run" for call in runner.calls)


def test_verification_lifecycle_precedes_launch_and_follows_cleanup(
    backend: SystemdBackend,
    runner: FakeRunner,
    spec: ServiceSpec,
) -> None:
    """Require durable intent before manager work, completion after stop."""
    assert hasattr(backend, "verification_lifecycle")
    events = []

    def record(name: str, completed: bool) -> None:
        events.append((name, completed, tuple(runner.calls)))

    runner.transient_outputs = ["ok"]
    with backend.verification_lifecycle(record):
        backend._verification_job(
            spec, ("/opt/new/bin/codereeve", "provenance")
        )
    assert len(events) == 2
    assert not events[0][1] and not events[0][2]
    assert events[1][1] and any("show" in c for c in events[1][2])
    assert events[0][0] == events[1][0]
    assert backend.pending_verification_units == ()


def test_recovery_adopts_only_exact_durable_verification_identity(
    backend: SystemdBackend,
) -> None:
    """A new backend must clean a recorded job without daemon-name power."""
    assert hasattr(backend, "cleanup_verification")
    name = "codereeve-verify-" + "a" * 32 + ".service"
    backend.cleanup_verification(name)
    assert backend.pending_verification_units == ()
    with pytest.raises(CutoverError):
        backend.cleanup_verification("bh-daemon.service")


def test_reload_and_enablement_are_bounded_and_verified(
    backend: SystemdBackend,
    runner: FakeRunner,
) -> None:
    """Enablement verifies manager state without starting services."""
    assert hasattr(backend, "reload") and hasattr(backend, "set_enabled")
    backend.reload()
    assert "daemon-reload" in runner.calls[-1]
    with pytest.raises(CutoverError):
        backend.set_enabled("bh-daemon.service", False)
    runner.units["bh-daemon.service"]["UnitFileState"] = "disabled"
    assert (
        backend.set_enabled("bh-daemon.service", False).enabled_state
        == "disabled"
    )
    assert not any("--now" in c or "start" in c for c in runner.calls)


def test_start_identity_survives_new_backend_instance(
    backend: SystemdBackend,
    runner: FakeRunner,
    spec: ServiceSpec,
) -> None:
    """Persist the actual pre-command UTC baseline without guessing time."""
    assert hasattr(backend, "start_evidence")
    prepare_start(backend, runner)
    started = backend.start(spec)
    evidence = backend.start_evidence(started)
    backend._starts.clear()
    backend.adopt_start(evidence)
    assert backend.start_evidence(started) == evidence
    assert evidence.started_ns == 1788868810000000000


@pytest.mark.parametrize(
    "field",
    [
        None,
        "argv",
        "HOME",
        "PATH",
        "WorkingDirectory",
        "EnvironmentFiles",
        "gate",
    ],
)
def test_effective_selection_checks_complete_loaded_daemon(
    backend: SystemdBackend,
    runner: FakeRunner,
    spec: ServiceSpec,
    field: str | None,
) -> None:
    """Reject correct executable paired with a different loaded selection."""
    assert hasattr(backend, "verify_selection")
    prepare_start(backend, runner)
    unit = runner.units["codereeve.service"]
    unit["ExecStart"] = unit["ExecStart"].replace(
        "argv[]=/opt/new/bin/codereeve ;",
        "argv[]=/opt/new/bin/codereeve daemon ;",
    )
    extra = {
        "Environment": (
            "HOME=/home/runner CODEREEVE_PROJECT_ROOT=/srv/"
            "project PATH=/opt/new/bin:/home/runner/.local/"
            "bin:/usr/local/bin:/usr/bin:/bin"
        ),
        "WorkingDirectory": "/srv/project",
        "EnvironmentFiles": "/etc/codereeve/secrets.env (ignore_errors=no)",
    }
    if field in {"HOME", "PATH"}:
        extra["Environment"] = extra["Environment"].replace(
            field + "=", field + "=wrong"
        )
    elif field == "gate":
        extra["Environment"] += " CODEREEVE_CUTOVER_GATE=/wrong"
    elif field == "argv":
        unit["ExecStart"] = unit["ExecStart"].replace(
            " daemon ;", " daemon --workflow /wrong ;"
        )
    elif field:
        extra[field] = "/wrong"
    original = backend.runner

    def selected(
        argv: tuple[str, ...], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        if "--property=Environment,WorkingDirectory,EnvironmentFiles" in argv:
            return subprocess.CompletedProcess(
                argv, 0, "\n".join(f"{k}={v}" for k, v in extra.items()), ""
            )
        return original(argv, timeout=timeout)

    backend.runner = selected
    if field:
        with pytest.raises(CutoverError):
            backend.verify_selection(spec, gate=None)
    else:
        backend.verify_selection(spec, gate=None)


def test_restore_activation_uses_typed_original_snapshot(
    backend: SystemdBackend,
    runner: FakeRunner,
) -> None:
    """Restoration starts only the prior active selection."""
    from codereeve.service_cutover.model import ServiceSnapshot

    assert hasattr(backend, "restore_activation")
    original = backend.inspect("bh-daemon.service")
    absent = backend.inspect("codereeve.service")
    path = group(backend.cgroup_root, "bh-daemon.service")
    runner.units["bh-daemon.service"].update(
        ActiveState="inactive", SubState="dead", MainPID="0", ControlGroup=""
    )
    runner.after_start = lambda: (path / "cgroup.procs").write_text(
        "51\n", encoding="utf-8"
    )
    backend.restore_activation(ServiceSnapshot(original, absent))
    assert backend.inspect("bh-daemon.service").active_state == "active"
    assert not any(
        "start" in c and c[-1] == "codereeve.service" for c in runner.calls
    )
    backend.restore_activation(ServiceSnapshot(original, absent))
    assert sum("start" in c for c in runner.calls) == 1


def test_account_and_legacy_environment_have_public_verified_apis(
    backend: SystemdBackend,
) -> None:
    """Expose actual old environment and account evidence to policy."""
    assert hasattr(backend, "legacy_environment") and hasattr(
        backend, "account"
    )
    assert (
        backend.legacy_environment(backend.inspect("bh-daemon.service"))
        == backend.filesystem_root / "opt/old"
    )
    with pytest.raises(CutoverError):
        backend.account("root")


def test_readonly_shutdown_proof_does_not_issue_stop(
    backend: SystemdBackend, runner: FakeRunner
) -> None:
    """Inventory callbacks observe services without stopping them."""
    assert hasattr(backend, "verify_shutdown")
    backend.verify_shutdown("codereeve.service")
    assert not any("stop" in c for c in runner.calls)
    with pytest.raises(CutoverError):
        backend.verify_shutdown("bh-daemon.service")


def test_masked_old_disable_requires_removed_enablement_links(
    backend: SystemdBackend, runner: FakeRunner
) -> None:
    """Masked state cannot prove enablement links were removed."""
    values = runner.units["bh-daemon.service"]
    values.update(
        LoadState="masked",
        ActiveState="inactive",
        SubState="dead",
        MainPID="0",
        ControlGroup="",
        UnitFileState="masked",
        ExecStart="",
        User="",
    )
    wants = (
        backend.filesystem_root / "etc/systemd/system/multi-user.target.wants"
    )
    wants.mkdir(parents=True)
    link = wants / "bh-daemon.service"
    link.write_bytes(b"enabled link fixture")
    with pytest.raises(CutoverError):
        backend.set_enabled("bh-daemon.service", False)
    link.unlink()
    assert (
        backend.set_enabled("bh-daemon.service", False).enabled_state
        == "masked"
    )
    assert not any("unmask" in c or "--now" in c for c in runner.calls)


def test_owned_installation_preflight_compares_effective_snapshot(
    backend: SystemdBackend,
    runner: FakeRunner,
    spec: ServiceSpec,
) -> None:
    """An attested inactive canonical unit can continue preflight."""
    from dataclasses import replace

    from conftest import properties

    from codereeve.service_cutover.model import ServiceSnapshot

    group(backend.cgroup_root, "bh-daemon.service", "51\n")
    runner.units["codereeve.service"] = properties("codereeve.service")
    from codereeve.service_cutover.selection import digest

    raw = {
        "Environment": "HOME=/home/runner",
        "WorkingDirectory": "/srv/project",
        "EnvironmentFiles": "",
    }
    backend.effective_environment = lambda _: raw
    new = replace(
        backend.inspect("codereeve.service"), environment_digest=digest(raw)
    )
    snapshot = ServiceSnapshot(backend.inspect("bh-daemon.service"), new)
    runner.transient_outputs = verification_outputs()
    assert (
        backend.preflight(spec, owned_snapshot=snapshot).new.load_state
        == "loaded"
    )
