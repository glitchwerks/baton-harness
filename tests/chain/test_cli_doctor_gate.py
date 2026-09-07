"""CLI preflight selection, reporting, and daemon startup gates (#358)."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator, Mapping, MutableMapping
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from baton_harness.chain import doctor, sandbox_config
from baton_harness.chain.cli import main
from baton_harness.chain.doctor import (
    CheckResult,
    CheckStatus,
    Phase,
    Severity,
)

_REAL_RUN_GATE = doctor.run_gate


def test_doctor_probe_filters_retained_bootstrap_authority() -> None:
    """Live worker probes retain the established credential isolation."""
    from baton_harness.chain import cli

    ctx = cli._doctor_context(None)
    ctx.env = {
        "GH_TOKEN": "worker-pat",
        "BWS_ACCESS_TOKEN": "vault-token",
        "GH_INSTALLATION_TOKEN": "app-token",
    }
    ctx.installation_token = "app-token"
    with patch("baton_harness.chain.cli.subprocess.run") as run:
        ctx.runner(["gh", "auth", "status"])
    probe_env = run.call_args.kwargs["env"]
    assert "GH_TOKEN" not in probe_env
    assert "BWS_ACCESS_TOKEN" not in probe_env
    assert "GH_INSTALLATION_TOKEN" not in probe_env


@pytest.mark.parametrize("status", list(CheckStatus))
def test_check_vault_selects_only_vault_and_requires_pass(
    status: CheckStatus, capsys: pytest.CaptureFixture[str]
) -> None:
    """Compatibility mode selects only the vault and treats skip as failure."""
    result = CheckResult(
        "VAULT_PEM_DRYRUN",
        Phase.LIVE,
        "vault",
        Severity.CRITICAL,
        status,
        "detail",
        "fix",
    )
    with patch.object(doctor, "run_report", return_value=[result]) as report:
        assert main(["--check-vault", "--format", "json"]) == (
            0 if status is CheckStatus.PASS else 1
        )
    assert report.call_args.args[1] == (Phase.LIVE,)
    assert report.call_args.kwargs["checks"] == (
        doctor.VAULT_PEM_DRYRUN_CHECK,
    )
    assert (
        json.loads(capsys.readouterr().out)["checks"][0]["id"]
        == "VAULT_PEM_DRYRUN"
    )


@pytest.mark.parametrize(
    "failed_phase", [None, Phase.INSTALLATION, Phase.LIVE]
)
def test_daemon_config_and_gate_order(
    tmp_path: Path,
    failed_phase: Phase | None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Resolution precedes effects; either gate failure stops startup."""
    import os

    from baton_harness.chain import cli

    path = tmp_path / "selected.env"
    path.write_text(
        "BH_REPO_OWNER=my-org\nBH_REPO_NAME=my-sandbox\nBH_GITHUB_APP_ID=12345\nBH_GITHUB_APP_INSTALLATION_ID=67890\nBH_GITHUB_APP_KEY_PROVIDER=bws\nBWS_PEM_SECRET_ID=11111111-2222-3333-4444-555555555555\n",
        encoding="utf-8",
    )
    events = []
    resolve = sandbox_config.resolve_config
    apply = sandbox_config.apply_config

    def resolve_spy(
        selected: Path, env: Mapping[str, str]
    ) -> sandbox_config.SandboxConfig:
        """Record config resolution without replacing its validation."""
        assert selected == path
        events.append("resolve_config")
        return resolve(selected, env)

    def apply_spy(
        config: sandbox_config.SandboxConfig, env: MutableMapping[str, str]
    ) -> None:
        """Record explicit environment application."""
        events.append("apply_config")
        apply(config, env)

    def gate(ctx: doctor.DoctorContext, phases: tuple[Phase, ...]) -> None:
        """Expose each selected gate's position and terminal behavior."""
        assert ctx.config_path == path
        events.append(
            "live_gate"
            if phases == (Phase.LIVE,)
            else "installation_configuration_gate"
        )
        if failed_phase in phases:
            raise doctor.DoctorGateError(
                [
                    CheckResult(
                        "SENTINEL",
                        failed_phase,
                        "unsafe ghp_abcdefghijklmnopqrstuvwxyz123456",
                        Severity.CRITICAL,
                        CheckStatus.FAIL,
                        "retained-secret",
                        "fix",
                    )
                ]
            )

    def bootstrap() -> str:
        """Record secret bootstrap and return installation authority."""
        events.append("bootstrap_secrets")
        return "ghs_TESTTOKEN_sentinel"

    async def daemon(*args: object, **kwargs: object) -> None:
        """Record entry to the daemon event loop."""
        events.append("run_daemon")

    with (
        patch.dict(
            os.environ,
            {
                "BH_PROJECT_ROOT": str(tmp_path),
                "BWS_ACCESS_TOKEN": "retained-secret",
            },
            clear=True,
        ),
        patch.object(
            sandbox_config, "resolve_config", side_effect=resolve_spy
        ),
        patch.object(sandbox_config, "apply_config", side_effect=apply_spy),
        patch.object(doctor, "run_gate", side_effect=gate),
        patch.object(cli, "load_workflow", return_value=MagicMock()),
        patch.object(
            cli,
            "load_registry",
            return_value=[MagicMock(project_root=str(tmp_path))],
        ),
        patch("baton_harness.chain.cli.os.chdir"),
        patch.object(cli, "_assert_force_pr_not_merge_tripwire"),
        patch.object(cli, "bootstrap_secrets", side_effect=bootstrap),
        patch.object(cli, "validate_daemon_token"),
        patch.object(cli, "run_daemon", side_effect=daemon),
    ):
        assert main(["--once", "--config", str(path)]) == int(
            failed_phase is not None
        )
    expected = [
        "resolve_config",
        "installation_configuration_gate",
        "apply_config",
        "bootstrap_secrets",
        "live_gate",
        "run_daemon",
    ]
    assert (
        events == expected[:2]
        if failed_phase is Phase.INSTALLATION
        else events == expected[:5]
        if failed_phase is Phase.LIVE
        else events == expected
    )
    captured = capsys.readouterr()
    assert "retained-secret" not in captured.err
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in captured.err


@pytest.mark.parametrize(
    "phases", [[], ["installation"], ["configuration", "live"]]
)
def test_selected_phases_emit_one_json_document(
    phases: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Selection is ordered and explicit config reaches the shared factory."""
    path = tmp_path / "operator.env"
    args = ["--doctor", "--format", "json", "--config", str(path)]
    for phase in phases:
        args.extend(["--phase", phase])
    with (
        patch.object(
            doctor, "create_context", wraps=doctor.create_context
        ) as factory,
        patch.object(doctor, "run_report", return_value=[]) as report,
    ):
        assert main(args) == 0
    captured = capsys.readouterr()
    expected = phases or ["installation", "configuration", "live"]
    assert json.loads(captured.out)["selected_phases"] == expected
    assert captured.err == ""
    assert factory.call_args.kwargs["config_path"] == path
    assert list(report.call_args.args[1]) == [Phase(p) for p in expected]


@pytest.mark.parametrize("format", ["text", "json"])
def test_report_render_failure_is_fixed_and_atomic(
    format: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unsafe renderer failures cannot leak payloads or partial output."""
    with (
        patch.object(doctor, "run_report", return_value=[]),
        patch(
            f"baton_harness.chain.doctor_report.render_{format}",
            side_effect=RuntimeError("secret-sentinel"),
        ),
    ):
        assert main(["--doctor", "--format", format]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        captured.err
        == "bh-daemon: doctor report could not be safely rendered\n"
    )


def test_live_vault_retains_prebootstrap_authority(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Real vault check sees retained BWS authority after environment scrub."""
    import os

    from baton_harness.chain import cli

    path = tmp_path / "operator.env"
    path.write_text(
        "BH_REPO_OWNER=my-org\nBH_REPO_NAME=my-sandbox\nBH_GITHUB_APP_ID=12345\nBH_GITHUB_APP_INSTALLATION_ID=67890\nBH_GITHUB_APP_KEY_PROVIDER=bws\nBWS_PEM_SECRET_ID=11111111-2222-3333-4444-555555555555\n",
        encoding="utf-8",
    )
    events = []

    def bootstrap() -> str:
        """Reproduce bootstrap's mandatory environment scrub."""
        events.append("bootstrap")
        os.environ.pop("BWS_ACCESS_TOKEN")
        return "ghs_TESTTOKEN_sentinel"

    def fetch(secret_id: str, *, access_token: str) -> str:
        """Require captured BWS authority after the ambient scrub."""
        assert "BWS_ACCESS_TOKEN" not in os.environ
        assert access_token == "retained-vault-secret"
        events.append("vault")
        return (
            "-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----"
        )

    async def daemon(*args: object, **kwargs: object) -> None:
        """Verify the scrub remains intact when the event loop starts."""
        assert "BWS_ACCESS_TOKEN" not in os.environ
        events.append("daemon")

    with (
        patch.dict(
            os.environ,
            {
                "BH_PROJECT_ROOT": str(tmp_path),
                "BWS_ACCESS_TOKEN": "retained-vault-secret",
            },
            clear=True,
        ),
        patch.object(doctor, "CATALOG", [doctor.VAULT_PEM_DRYRUN_CHECK]),
        patch.object(doctor, "run_gate", new=_REAL_RUN_GATE),
        patch.object(cli, "load_workflow", return_value=MagicMock()),
        patch.object(
            cli,
            "load_registry",
            return_value=[MagicMock(project_root=str(tmp_path))],
        ),
        patch("baton_harness.chain.cli.os.chdir"),
        patch.object(cli, "_assert_force_pr_not_merge_tripwire"),
        patch.object(cli, "bootstrap_secrets", side_effect=bootstrap),
        patch.object(cli, "validate_daemon_token"),
        patch(
            "baton_harness.chain.bws_client.fetch_secret", side_effect=fetch
        ),
        patch(
            "baton_harness.chain.app_auth.build_app_jwt",
            return_value="signed-jwt",
        ),
        patch.object(cli, "run_daemon", side_effect=daemon),
    ):
        assert main(["--once", "--config", str(path)]) == 0
    assert events == ["bootstrap", "vault", "daemon"]
    assert "retained-vault-secret" not in capsys.readouterr().err


def _run_file_provider_gate(
    tmp_path: Path, *, optional_bws: bool
) -> tuple[int, int]:
    """Run the real prerequisite gate through CLI startup in isolation."""
    bh_dir = tmp_path / ".bh"
    bh_dir.mkdir()
    content = (
        "BH_REPO_OWNER=my-org\nBH_REPO_NAME=my-sandbox\n"
        "BH_GITHUB_APP_ID=12345\nBH_GITHUB_APP_INSTALLATION_ID=67890\n"
        "BH_GITHUB_APP_KEY_PROVIDER=file\n"
        f"BH_GITHUB_APP_PRIVATE_KEY_FILE={tmp_path / 'app.pem'}\n"
    )
    if optional_bws:
        content += (
            "BWS_GH_TOKEN_SECRET_ID=11111111-2222-3333-4444-555555555555\n"
        )
    (bh_dir / "config.env").write_text(content, encoding="utf-8")
    checks = [
        check
        for check in doctor.CATALOG
        if check.check_id
        in {"CLI_BWS", "CFG_REQUIRED_KEYS", "ENV_BWS_ACCESS_TOKEN"}
    ]
    with (
        patch.dict(
            "os.environ", {"BH_PROJECT_ROOT": str(tmp_path)}, clear=True
        ),
        patch.object(doctor, "CATALOG", checks),
        patch.object(doctor, "run_gate", new=_REAL_RUN_GATE),
        patch(
            "baton_harness.chain.cli.load_workflow", return_value=MagicMock()
        ),
        patch(
            "baton_harness.chain.cli.load_registry",
            return_value=[MagicMock(project_root=str(tmp_path))],
        ),
        patch("baton_harness.chain.sandbox_config.read_and_validate"),
        patch("baton_harness.chain.cli.os.chdir"),
        patch("baton_harness.chain.cli._assert_force_pr_not_merge_tripwire"),
        patch(
            "baton_harness.chain.cli.shutil.which",
            return_value="/usr/bin/bws" if optional_bws else None,
        ),
        patch(
            "baton_harness.chain.cli.bootstrap_secrets",
            return_value="ghs_TESTTOKEN_sentinel",
        ) as bootstrap,
        patch("baton_harness.chain.cli.validate_daemon_token"),
        patch("baton_harness.chain.cli.run_daemon", new_callable=AsyncMock),
    ):
        result = _run_main_allow_system_exit("--once")
    return result, bootstrap.call_count


def test_cli_doctor_gate_allows_file_only_host_without_bws(
    tmp_path: Path,
) -> None:
    """File-only hosts reach bootstrap without BWS binary or token."""
    assert _run_file_provider_gate(tmp_path, optional_bws=False) == (0, 1)


def test_cli_doctor_gate_blocks_file_provider_optional_bws_without_token(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An optional PAT consumer makes a missing BWS token fatal."""
    assert _run_file_provider_gate(tmp_path, optional_bws=True) == (1, 0)
    assert "ENV_BWS_ACCESS_TOKEN" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Autouse fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _auto_patch_pre_bootstrap_gate() -> Iterator[None]:
    """No-op both doctor gates for tests exercising other CLI behavior."""
    with patch("baton_harness.chain.doctor.run_gate", return_value=None):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_main(*args: str) -> int:
    """Run ``main`` with the given argv and return the exit code.

    Args:
        *args: Command-line arguments to pass to ``main``.

    Returns:
        The integer exit code returned by ``main``.
    """
    return main(list(args))


def _result(
    check_id: str,
    severity: Severity,
    status: CheckStatus,
    *,
    title: str | None = None,
    detail: str | None = None,
    fix: str | None = None,
) -> CheckResult:
    """Build a sentinel ``CheckResult`` for a canned ``run_report`` return.

    Args:
        check_id: Stable identifier for the check.
        severity: Operational severity of a failure.
        status: Outcome of the check.
        title: Human-readable check title. Defaults to a sentinel derived
            from ``check_id``.
        detail: Secret-safe explanation of the outcome. Defaults to a
            sentinel derived from ``check_id``.
        fix: Secret-safe remediation guidance. Defaults to a sentinel
            derived from ``check_id``.

    Returns:
        A populated, sentinel-valued check result.
    """
    return CheckResult(
        check_id=check_id,
        phase=Phase.CONFIGURATION,
        title=title or f"{check_id} sentinel title",
        severity=severity,
        status=status,
        detail=detail or f"{check_id} sentinel detail",
        remediation=fix or f"{check_id} sentinel fix",
    )


@contextlib.contextmanager
def _patched_pre_doctor_seams(
    *,
    run_report_return: list[CheckResult] | None = None,
) -> Iterator[tuple[MagicMock, AsyncMock, MagicMock]]:
    """Patch the seams ``cli.main`` crosses on its way to a doctor decision.

    Patches config/registry loading (so ``main`` reaches the ``--doctor``
    branch), ``bootstrap_secrets`` and ``run_daemon`` (so tests can assert
    they were never reached), and ``doctor.run_report`` (the seam under
    test, returning the canned ``run_report_return``).

    Args:
        run_report_return: The canned list of results ``doctor.run_report``
            should return.

    Yields:
        A tuple of ``(bootstrap_secrets_mock, run_daemon_mock,
        run_report_mock)`` for post-call assertions.
    """
    with (
        patch(
            "baton_harness.chain.cli.load_workflow",
            return_value=MagicMock(),
        ),
        patch(
            "baton_harness.chain.cli.load_registry",
            return_value=[MagicMock()],
        ),
        patch("baton_harness.chain.cli.os.chdir"),
        patch("baton_harness.chain.cli.os.path.isdir", return_value=True),
        # Stub the real subprocess-based tripwire self-test so this suite
        # never depends on it actually succeeding in the test environment
        # (the --doctor path reports FORCE_PR_TRIPWIRE via the catalog
        # instead; it must not additionally hard-block on the native
        # self-test before the report is even produced).
        patch("baton_harness.chain.cli._assert_force_pr_not_merge_tripwire"),
        patch("baton_harness.chain.cli.bootstrap_secrets") as bootstrap_mock,
        patch(
            "baton_harness.chain.cli.run_daemon",
            new_callable=AsyncMock,
        ) as run_daemon_mock,
        patch(
            "baton_harness.chain.doctor.run_report",
            return_value=run_report_return,
        ) as run_report_mock,
    ):
        yield bootstrap_mock, run_daemon_mock, run_report_mock


# ---------------------------------------------------------------------------
# --doctor alone: report-only, exit 0 regardless of outcomes, no daemon start
# ---------------------------------------------------------------------------


def test_doctor_flag_prints_report_and_exits_0_even_with_critical_fail(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--doctor is a pure diagnostic: exit 0 even when a CRITICAL check FAILs.

    Also asserts the report is printed with a recognizable [STATUS] tag per
    result and that FAIL/WARN results surface their detail/fix text (plan
    section 9's per-item "why" and "how to fix" content), and that neither
    bootstrap_secrets nor run_daemon is reached.
    """
    results = [
        _result(
            "CLI_GH",
            Severity.CRITICAL,
            CheckStatus.PASS,
            title="GitHub CLI available",
        ),
        _result(
            "CFG_CONFIG_ENV",
            Severity.CRITICAL,
            CheckStatus.FAIL,
            title="Sandbox config file present",
            detail="Sentinel-detail: .bh/config.env is missing.",
            fix="Sentinel-fix: create .bh/config.env in BH_PROJECT_ROOT.",
        ),
        _result(
            "CLI_UV",
            Severity.WARNING,
            CheckStatus.WARN,
            title="uv package manager available",
            detail="Sentinel-detail: uv is not available on PATH.",
            fix="Sentinel-fix: install uv and ensure it is on PATH.",
        ),
    ]

    with _patched_pre_doctor_seams(run_report_return=results) as (
        bootstrap_mock,
        run_daemon_mock,
        run_report_mock,
    ):
        result = _run_main("--doctor")

    captured = capsys.readouterr()
    # The report may legitimately land on stdout or stderr -- cli.py's own
    # convention (e.g. the registry/config error paths) prints operator
    # messages to stderr, so pinning one stream would be an unwarranted
    # guess. Assert against the union of both.
    output = captured.out + captured.err

    assert result == 0, (
        "--doctor without --strict must exit 0 even with a CRITICAL FAIL"
        f" present in the report, got {result}"
    )
    run_report_mock.assert_called_once()
    bootstrap_mock.assert_not_called()
    run_daemon_mock.assert_not_called()

    assert "[pass]" in output
    assert "GitHub CLI available" in output

    assert "[fail]" in output
    assert "Sandbox config file present" in output
    assert "Sentinel-detail: .bh/config.env is missing." in output
    assert "Sentinel-fix: create .bh/config.env in BH_PROJECT_ROOT." in output

    assert "[warn]" in output
    assert "uv package manager available" in output
    assert "Sentinel-detail: uv is not available on PATH." in output
    assert "Sentinel-fix: install uv and ensure it is on PATH." in output


def test_doctor_flag_returns_before_reaching_bootstrap_or_daemon() -> None:
    """--doctor returns before bootstrap_secrets/run_daemon are reached.

    Uses an all-PASS report so a bug that only skips the daemon on failure
    (rather than unconditionally) would still be caught.
    """
    results = [
        _result("CLI_GH", Severity.CRITICAL, CheckStatus.PASS),
        _result("CLI_BWS", Severity.CRITICAL, CheckStatus.PASS),
    ]

    with _patched_pre_doctor_seams(run_report_return=results) as (
        bootstrap_mock,
        run_daemon_mock,
        run_report_mock,
    ):
        result = _run_main("--doctor")

    assert result == 0
    run_report_mock.assert_called_once()
    bootstrap_mock.assert_not_called()
    run_daemon_mock.assert_not_called()


# ---------------------------------------------------------------------------
# --doctor --strict: exit-code gating on CRITICAL FAIL (D7)
# ---------------------------------------------------------------------------


def test_doctor_strict_all_pass_exits_0() -> None:
    """--doctor --strict exits 0 when every check PASSes."""
    results = [
        _result("CLI_GH", Severity.CRITICAL, CheckStatus.PASS),
        _result("CLI_UV", Severity.WARNING, CheckStatus.PASS),
    ]

    with _patched_pre_doctor_seams(run_report_return=results):
        result = _run_main("--doctor", "--strict")

    assert result == 0, (
        f"--doctor --strict with no failures must exit 0, got {result}"
    )


def test_doctor_strict_critical_fail_exits_1() -> None:
    """--doctor --strict exits 1 when a CRITICAL check FAILs."""
    results = [
        _result("CLI_GH", Severity.CRITICAL, CheckStatus.PASS),
        _result(
            "CFG_CONFIG_ENV",
            Severity.CRITICAL,
            CheckStatus.FAIL,
        ),
        _result("CLI_UV", Severity.WARNING, CheckStatus.WARN),
    ]

    with _patched_pre_doctor_seams(run_report_return=results):
        result = _run_main("--doctor", "--strict")

    assert result == 1, (
        "--doctor --strict must exit 1 when any CRITICAL-severity check"
        f" has status=FAIL, got {result}"
    )


def test_doctor_strict_only_warning_failures_exits_0() -> None:
    """--doctor --strict exits 0 when only WARNING-severity checks fail/warn.

    Includes both a WARNING-severity FAIL and a WARNING-severity WARN, to
    pin the exact D7 condition ("any CRITICAL-severity check has
    status=FAIL") rather than a looser "any FAIL status" reading -- a
    WARNING-severity check that happens to report FAIL must NOT trip
    --strict.
    """
    results = [
        _result("CLI_GH", Severity.CRITICAL, CheckStatus.PASS),
        _result(
            "CFG_OPTIONAL_SECRET_IDS",
            Severity.WARNING,
            CheckStatus.FAIL,
        ),
        _result("CLI_UV", Severity.WARNING, CheckStatus.WARN),
    ]

    with _patched_pre_doctor_seams(run_report_return=results):
        result = _run_main("--doctor", "--strict")

    assert result == 0, (
        "--doctor --strict must exit 0 when only WARNING-severity checks"
        f" fail or warn (no CRITICAL FAIL present), got {result}"
    )


# ---------------------------------------------------------------------------
# No --doctor flag: existing daemon-launch behavior is unchanged
# ---------------------------------------------------------------------------


def test_no_doctor_flag_runs_daemon_path_and_never_calls_run_report() -> None:
    """Without --doctor, main() still launches the daemon as before.

    Regression guard: adding the --doctor branch must not divert the
    default (--once) path, and doctor.run_report must never be invoked
    when --doctor was not passed.
    """
    called_kwargs: dict[str, object] = {}

    async def fake_run_daemon(*args: object, **kwargs: object) -> None:
        called_kwargs.update(kwargs)

    with (
        patch(
            "baton_harness.chain.cli.bootstrap_secrets",
            return_value="ghs_TESTTOKEN_xxxxxxx",
        ),
        patch("baton_harness.chain.cli.validate_daemon_token"),
        patch(
            "baton_harness.chain.cli.load_workflow",
            return_value=MagicMock(),
        ),
        patch(
            "baton_harness.chain.cli.load_registry",
            return_value=[MagicMock()],
        ),
        patch(
            "baton_harness.chain.cli.run_daemon",
            side_effect=fake_run_daemon,
        ),
        patch("baton_harness.chain.cli.os.chdir"),
        patch("baton_harness.chain.cli.os.path.isdir", return_value=True),
        patch("baton_harness.chain.doctor.run_report") as run_report_mock,
    ):
        result = _run_main("--once")

    assert result == 0, f"Expected exit 0, got {result}"
    assert called_kwargs.get("once") is True
    run_report_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Installation and configuration run_gate wired into the normal
# (non-``--doctor``) daemon-startup path
# ---------------------------------------------------------------------------


def _run_main_allow_system_exit(*args: str) -> int:
    """Run ``main`` and normalize either a return or a ``SystemExit`` to int.

    Startup renders the gate's aggregated critical failures and returns
    one. Argument parsing may still raise ``SystemExit``; normalize both
    paths so callers assert the process exit outcome.

    Args:
        *args: Command-line arguments to pass to ``main``.

    Returns:
        The integer exit code, whether returned directly or raised via
        ``SystemExit``.
    """
    try:
        return main(list(args))
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        return 1


def _assert_run_gate_called_with_pre_bootstrap(gate_mock: MagicMock) -> None:
    """Require installation and configuration in the first startup gate."""
    assert gate_mock.call_count in (1, 2)
    call = gate_mock.call_args_list[0]
    phase_arg = call.kwargs.get("phase")
    if phase_arg is None and len(call.args) >= 2:
        phase_arg = call.args[1]
    assert phase_arg == (Phase.INSTALLATION, Phase.CONFIGURATION), (
        "run_gate must select installation and configuration in the"
        f" normal daemon-startup path, got {phase_arg!r} (call={call!r})"
    )


class TestPreBootstrapDoctorGate:
    """Installation and configuration gate precedes startup side effects."""

    def test_gate_runs_before_tripwire_and_bootstrap_on_pass(
        self,
    ) -> None:
        """Both gates pass before the daemon loop can start."""
        call_order: list[str] = []

        fake_repo_cfg = MagicMock()

        def fake_self_test() -> None:
            call_order.append("self-test")

        def fake_run_gate(*args: object, **kwargs: object) -> None:
            call_order.append("gate")

        def fake_bootstrap(**kwargs: object) -> str:
            call_order.append("bootstrap")
            return "ghs_TESTTOKEN_sentinel"

        async def fake_run_daemon(*args: object, **kwargs: object) -> None:
            call_order.append("run-daemon")

        with (
            patch(
                "baton_harness.chain.cli.load_workflow",
                return_value=MagicMock(),
            ),
            patch(
                "baton_harness.chain.cli.load_registry",
                return_value=[fake_repo_cfg],
            ),
            patch("baton_harness.chain.cli.os.chdir"),
            patch(
                "baton_harness.chain.cli.os.path.isdir",
                return_value=True,
            ),
            patch(
                "baton_harness.chain.cli._assert_force_pr_not_merge_tripwire",
                side_effect=fake_self_test,
            ),
            patch(
                "baton_harness.chain.doctor.run_gate",
                side_effect=fake_run_gate,
            ) as gate_mock,
            patch(
                "baton_harness.chain.cli.bootstrap_secrets",
                side_effect=fake_bootstrap,
            ),
            patch("baton_harness.chain.cli.validate_daemon_token"),
            patch(
                "baton_harness.chain.cli.run_daemon",
                side_effect=fake_run_daemon,
            ),
        ):
            result = _run_main_allow_system_exit("--once")

        assert result == 0, (
            f"Expected exit 0 on an all-pass gate, got {result}"
        )

        _assert_run_gate_called_with_pre_bootstrap(gate_mock)

        assert call_order.index("gate") < call_order.index("self-test"), (
            "the installation/configuration gate must run before the"
            f" force-pr-not-merge self-test; got {call_order!r}"
        )
        assert call_order.index("gate") < call_order.index("bootstrap"), (
            "the initial doctor gate must run before"
            f" bootstrap_secrets; got {call_order!r}"
        )
        assert "run-daemon" in call_order, (
            "a passing gate must not prevent startup from reaching"
            f" run_daemon; got {call_order!r}"
        )

    def test_critical_gate_failure_stops_before_bootstrap_and_run_daemon(
        self,
    ) -> None:
        """A CRITICAL gate failure stops startup before bootstrap/run_daemon.

        ``run_gate`` raising ``SystemExit(1)`` stops startup before
        ``bootstrap_secrets``/``run_daemon`` and the overall exit is
        non-zero. Simulates the CRITICAL fail via ``run_gate``'s own documented
        contract (raises ``SystemExit(1)``) rather than constructing a
        real failing ``DoctorContext`` -- ``run_gate``'s check-selection
        and short-circuit behavior are already exhaustively covered by
        ``test_doctor.py``; this suite only needs to prove ``cli.main``
        is wired to react correctly to that contract.
        """
        bootstrap_called = False
        run_daemon_called = False

        fake_repo_cfg = MagicMock()

        def fake_bootstrap(**kwargs: object) -> str:
            nonlocal bootstrap_called
            bootstrap_called = True
            return "ghs_TESTTOKEN_sentinel"

        async def fake_run_daemon(*args: object, **kwargs: object) -> None:
            nonlocal run_daemon_called
            run_daemon_called = True

        with (
            patch(
                "baton_harness.chain.cli.load_workflow",
                return_value=MagicMock(),
            ),
            patch(
                "baton_harness.chain.cli.load_registry",
                return_value=[fake_repo_cfg],
            ),
            patch("baton_harness.chain.cli.os.chdir"),
            patch(
                "baton_harness.chain.cli.os.path.isdir",
                return_value=True,
            ),
            patch(
                "baton_harness.chain.cli._assert_force_pr_not_merge_tripwire",
            ),
            patch(
                "baton_harness.chain.doctor.run_gate",
                side_effect=doctor.DoctorGateError(()),
            ) as gate_mock,
            patch(
                "baton_harness.chain.cli.bootstrap_secrets",
                side_effect=fake_bootstrap,
            ),
            patch("baton_harness.chain.cli.validate_daemon_token"),
            patch(
                "baton_harness.chain.cli.run_daemon",
                side_effect=fake_run_daemon,
            ),
        ):
            result = _run_main_allow_system_exit("--once")

        assert result == 1, (
            "a CRITICAL initial doctor failure must produce a"
            f" non-zero (1) exit, got {result}"
        )
        _assert_run_gate_called_with_pre_bootstrap(gate_mock)
        assert not bootstrap_called, (
            "bootstrap_secrets must not run after a CRITICAL initial"
            " doctor gate failure"
        )
        assert not run_daemon_called, (
            "run_daemon must not run after a CRITICAL initial doctor"
            " gate failure"
        )
