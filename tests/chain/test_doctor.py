"""Tests for the phase-selectable preflight doctor domain.

The suite covers stable machine enums, the public result/context model,
selection and aggregated gate behavior, credential-free installation
integrity, local configuration checks, and live authority checks. External
operations remain injected except for the real offline installation smoke.
"""

from __future__ import annotations

import dataclasses
import json
import os
import stat
import subprocess
import textwrap
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from baton_harness.chain import app_auth, doctor
from baton_harness.chain.cli import main
from baton_harness.chain.doctor import (
    CheckFn,
    CheckResult,
    CheckStatus,
    DoctorContext,
    Phase,
    Severity,
    run_gate,
    run_report,
)
from baton_harness.chain.ruleset_status import RulesetStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unused_which(name: str) -> str | None:
    """Stub ``which`` seam that fails the test if invoked unexpectedly."""
    raise AssertionError(
        f"which() must not be called in this test (requested {name!r})"
    )


def _unused_runner(
    args: list[str],
) -> subprocess.CompletedProcess[str]:
    """Stub ``runner`` seam that fails the test if invoked unexpectedly."""
    raise AssertionError(
        f"runner() must not be called in this test (args={args!r})"
    )


def _unused_run(
    args: list[str],
    **_kwargs: object,
) -> subprocess.CompletedProcess[str]:
    """Stub ``run`` seam that fails the test if invoked unexpectedly."""
    raise AssertionError(
        f"run() must not be called in this test (args={args!r})"
    )


def _unused_fetch_secret(secret_id: str, **_kwargs: object) -> str:
    """Stub ``fetch_secret`` seam; fails the test if invoked."""
    raise AssertionError(
        f"fetch_secret() must not be called in this test "
        f"(secret_id={secret_id!r})"
    )


def _make_ctx(**overrides: object) -> DoctorContext:
    """Build a ``DoctorContext`` with every seam explicitly supplied.

    Every field is set explicitly (rather than relying on
    ``DoctorContext``'s own defaults) so these tests do not silently
    depend on implementer-chosen default values -- except
    ``installation_token``, whose Rev-3 default of ``""`` is itself
    pinned by a dedicated test below.

    Args:
        **overrides: Field values to override the defaults with.

    Returns:
        A fully-populated ``DoctorContext``.
    """
    defaults: dict[str, object] = {
        "project_root": "",
        "home_dir": "",
        "env": {},
        "which": _unused_which,
        "runner": _unused_runner,
        "run": _unused_run,
        "fetch_secret": _unused_fetch_secret,
    }
    defaults.update(overrides)
    return DoctorContext(**defaults)  # type: ignore[arg-type]


def _get_check(check_id: str) -> Any:  # noqa: ANN401
    """Return the single ``CATALOG`` entry with the given ``check_id``.

    Args:
        check_id: Catalog ID to look up (e.g. ``"CLI_GH"``).

    Returns:
        The matching ``Check`` callable.
    """
    matches = [c for c in doctor.CATALOG if c.check_id == check_id]
    assert len(matches) == 1, (
        f"expected exactly one CATALOG entry with check_id={check_id!r}; "
        f"found {len(matches)}"
    )
    return matches[0]


def _make_check(
    check_id: str,
    *,
    title: str = "synthetic check",
    severity: Severity = Severity.CRITICAL,
    phase: Phase = Phase.CONFIGURATION,
    daemon_native: bool = False,
    fix: str = "synthetic fix",
    fn: Any = None,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    """Build a synthetic ``Check`` callable for runner-semantics tests.

    Args:
        check_id: Catalog ID for the synthetic check.
        title: Human-readable title.
        severity: Static severity attribute.
        phase: Static phase attribute.
        daemon_native: Static daemon_native attribute.
        fix: Static fix-string attribute (read by the exception
            contract when the check raises).
        fn: The callable body; defaults to one that always returns a
            PASS ``CheckResult``.

    Returns:
        A callable carrying ``check_id``/``title``/``severity``/
        ``phase``/``daemon_native``/``fix`` attributes, matching the
        ``Check`` contract.
    """
    if fn is None:

        def fn(ctx: DoctorContext) -> CheckResult:  # noqa: ANN001
            return CheckResult(
                check_id=check_id,
                phase=phase,
                title=title,
                severity=severity,
                status=CheckStatus.PASS,
                detail="synthetic pass",
                remediation=fix,
            )

    fn.check_id = check_id
    fn.title = title
    fn.severity = severity
    fn.phase = phase
    fn.daemon_native = daemon_native
    fn.fix = fix
    return fn


def _assert_no_secret_leak(result: CheckResult, secret_value: str) -> None:
    """Assert ``secret_value`` never appears in any ``CheckResult`` field.

    Args:
        result: The result to inspect.
        secret_value: The (fake) secret value that must never leak.
    """
    for field_name in ("check_id", "title", "detail", "remediation"):
        value = getattr(result, field_name)
        assert secret_value not in str(value), (
            f"CheckResult.{field_name} must never contain the secret "
            f"value; got {value!r}"
        )


def _write_config_env(project_root: Path, content: str) -> None:
    """Write ``content`` to ``{project_root}/.bh/config.env``.

    Args:
        project_root: Directory to write under.
        content: File content.
    """
    bh_dir = project_root / ".bh"
    bh_dir.mkdir(parents=True, exist_ok=True)
    (bh_dir / "config.env").write_text(content, encoding="utf-8")


_VALID_CONFIG_ENV = textwrap.dedent(
    """\
    BH_REPO_OWNER=my-org
    BH_REPO_NAME=my-sandbox
    BH_GITHUB_APP_ID=12345
    BH_GITHUB_APP_INSTALLATION_ID=67890
    BH_GITHUB_APP_KEY_PROVIDER=bws
    BWS_PEM_SECRET_ID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee
    """
)

_FAKE_BWS_TOKEN = "not-a-real-secret-VALUE-9f8e7d6c5b4a"  # placeholder
_FAKE_ANTHROPIC_KEY = "sk-fake-not-a-real-key-12345"  # placeholder


@pytest.fixture(scope="module")
def app_private_key_pem() -> str:
    """Generate a real signing key so probes exercise PEM validation."""
    return (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode("utf-8")
    )


def _file_provider_config(path: Path) -> str:
    """Build file-provider settings with no BWS consumer."""
    return _VALID_CONFIG_ENV.replace(
        "BH_GITHUB_APP_KEY_PROVIDER=bws", "BH_GITHUB_APP_KEY_PROVIDER=file"
    ).replace(
        "BWS_PEM_SECRET_ID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        f"BH_GITHUB_APP_PRIVATE_KEY_FILE={path}",
    )


@pytest.mark.parametrize(
    ("provider", "optional_key", "expected"),
    [
        ("bws", "", CheckStatus.FAIL),
        ("file", "", CheckStatus.PASS),
        ("file", "BWS_GH_TOKEN_SECRET_ID", CheckStatus.FAIL),
        ("file", "BWS_HEARTBEAT_PING_URL_SECRET_ID", CheckStatus.FAIL),
    ],
)
@pytest.mark.parametrize("optional_in_env", [False, True])
def test_cli_bws_and_access_token_follow_composed_requirement(
    tmp_path: Path,
    provider: str,
    optional_key: str,
    expected: CheckStatus,
    optional_in_env: bool,
) -> None:
    """Both prerequisites follow provider and optional BWS consumers."""
    content = (
        _VALID_CONFIG_ENV
        if provider == "bws"
        else _file_provider_config(tmp_path / "app.pem")
    )
    env = {}
    if optional_key:
        if optional_in_env:
            env[optional_key] = "11111111-2222-3333-4444-555555555555"
        else:
            content += f"{optional_key}=11111111-2222-3333-4444-555555555555\n"
    _write_config_env(tmp_path, content)
    ctx = _make_ctx(
        project_root=str(tmp_path), env=env, which=lambda name: None
    )
    for check_id in ("CLI_BWS", "ENV_BWS_ACCESS_TOKEN"):
        result = _get_check(check_id)(ctx)
        assert result.status is expected
        assert result.severity is Severity.CRITICAL


def test_file_only_missing_bws_binary_and_token_are_pass_not_required(
    tmp_path: Path,
) -> None:
    """File-only prerequisites pass explicitly without a binary or token."""
    _write_config_env(tmp_path, _file_provider_config(tmp_path / "app.pem"))
    ctx = _make_ctx(project_root=str(tmp_path), which=lambda name: None)
    for check_id in ("CLI_BWS", "ENV_BWS_ACCESS_TOKEN"):
        result = _get_check(check_id)(ctx)
        assert result.status is CheckStatus.PASS
        assert "not required" in result.detail
    assert _get_check("CFG_REQUIRED_KEYS")(ctx).status is CheckStatus.PASS


def test_invalid_provider_matrix_fails_cfg_required_keys(
    tmp_path: Path,
) -> None:
    """Conflicting sources fail validation before any secret retrieval."""
    _write_config_env(
        tmp_path,
        _VALID_CONFIG_ENV
        + f"BH_GITHUB_APP_PRIVATE_KEY_FILE={tmp_path / 'app.pem'}\n",
    )
    fetch = Mock()
    result = _get_check("CFG_REQUIRED_KEYS")(
        _make_ctx(project_root=str(tmp_path), fetch_secret=fetch)
    )
    assert result.status is CheckStatus.FAIL
    fetch.assert_not_called()


def test_bws_key_probe_fetches_without_exposing_secret(
    tmp_path: Path,
    app_private_key_pem: str,
) -> None:
    """A BWS probe loads exactly once and reports no secret or byte count."""
    fetch = Mock(return_value=app_private_key_pem)
    ctx = _make_vault_ctx(tmp_path, fetch)
    resolved_id = "11111111-2222-3333-4444-555555555555"
    ctx.env.update(
        {
            "BH_GITHUB_APP_KEY_PROVIDER": "",
            "BWS_PEM_SECRET_ID": resolved_id,
            "BH_GITHUB_APP_ID": "54321",
        }
    )
    with patch.object(
        app_auth, "build_app_jwt", wraps=app_auth.build_app_jwt
    ) as sign:
        result = doctor.VAULT_PEM_DRYRUN_CHECK(ctx)
    assert result.status is CheckStatus.PASS
    fetch.assert_called_once_with(resolved_id, access_token=_FAKE_BWS_TOKEN)
    assert sign.call_count == 1
    assert sign.call_args.args == ("54321", app_private_key_pem)
    for secret in (app_private_key_pem, _FAKE_BWS_TOKEN, _VAULT_SECRET_ID):
        _assert_no_secret_leak(result, secret)
    assert "characters" not in result.detail
    assert str(len(app_private_key_pem)) not in result.detail


@pytest.mark.parametrize("mode", [0o600, 0o644])
@pytest.mark.parametrize("malformed", [False, True])
def test_file_key_probe_reads_secure_file_without_bws(
    tmp_path: Path,
    app_private_key_pem: str,
    mode: int,
    malformed: bool,
) -> None:
    """Load real file bytes and enforce descriptor permission metadata."""
    path = tmp_path / "app.pem"
    contents = "MALFORMED_PEM_SENTINEL" if malformed else app_private_key_pem
    path.write_text(contents, encoding="utf-8")
    path.chmod(mode)
    _write_config_env(tmp_path, _file_provider_config(path))
    metadata = list(path.stat())
    metadata[stat.ST_MODE] = stat.S_IFREG | mode
    fetch = Mock(return_value=app_private_key_pem)
    # Windows chmod cannot express POSIX bits; preserve real identity and
    # file I/O while supplying the descriptor mode enforced in production.
    with patch(
        "baton_harness.chain.app_private_key.os.fstat",
        return_value=os.stat_result(metadata),
    ):
        result = doctor.VAULT_PEM_DRYRUN_CHECK(
            _make_ctx(project_root=str(tmp_path), fetch_secret=fetch)
        )
    assert result.status is (
        CheckStatus.PASS
        if mode == 0o600 and not malformed
        else CheckStatus.FAIL
    )
    _assert_no_secret_leak(result, contents)
    fetch.assert_not_called()


def test_file_key_probe_rejects_insecure_mode_safely(tmp_path: Path) -> None:
    """An insecure native file fails without leaking its content or path."""
    path = tmp_path / "private-sentinel.pem"
    path.write_text("SECRET_PEM_SENTINEL", encoding="utf-8")
    path.chmod(0o644)
    _write_config_env(tmp_path, _file_provider_config(path))
    result = doctor._run_check(
        doctor.VAULT_PEM_DRYRUN_CHECK,
        _make_ctx(project_root=str(tmp_path)),
    )
    assert result.status is CheckStatus.FAIL
    assert "file provider" in result.detail
    _assert_no_secret_leak(result, "SECRET_PEM_SENTINEL")
    _assert_no_secret_leak(result, str(path))


@pytest.mark.parametrize("raises", [False, True])
def test_key_probe_output_excludes_pem_token_and_credential_url(
    tmp_path: Path,
    raises: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Malformed PEM and transport failures stay secret-safe in reports."""
    sentinels = (
        "SECRET_PEM_SENTINEL",
        _FAKE_BWS_TOKEN,
        "https://credential-user:credential-pass@example.invalid/key",
    )
    payload = " ".join(sentinels)
    fetch = Mock(
        side_effect=RuntimeError(payload) if raises else None,
        return_value=payload,
    )
    ctx = _make_vault_ctx(tmp_path, fetch)
    result = doctor._run_check(doctor.VAULT_PEM_DRYRUN_CHECK, ctx)
    assert result.status is CheckStatus.FAIL
    with patch.object(doctor, "DoctorContext", return_value=ctx):
        assert main(["--doctor", "--check-vault"]) == 1
    captured = capsys.readouterr()
    for output in (
        captured.out + captured.err,
        json.dumps(dataclasses.asdict(result), default=lambda item: item.name),
    ):
        for sentinel in sentinels:
            assert sentinel not in output


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


def test_severity_enum_has_stable_machine_values() -> None:
    """Severity exposes stable values for report serialization."""
    assert {severity.value for severity in Severity} == {
        "critical",
        "warning",
    }


def test_check_status_enum_has_stable_machine_values() -> None:
    """CheckStatus exposes stable values for report serialization."""
    assert {status.value for status in CheckStatus} == {
        "pass",
        "fail",
        "warn",
        "skip",
    }


def test_phase_enum_has_stable_machine_values() -> None:
    """Phase exposes the three stable public machine values."""
    assert {phase.value for phase in Phase} == {
        "installation",
        "configuration",
        "live",
    }


# ---------------------------------------------------------------------------
# CheckResult dataclass
# ---------------------------------------------------------------------------


def test_check_result_carries_all_public_fields() -> None:
    """CheckResult stores the complete public doctor result contract."""
    result = CheckResult(
        check_id="X",
        phase=Phase.INSTALLATION,
        title="a title",
        severity=Severity.CRITICAL,
        status=CheckStatus.PASS,
        detail="a detail",
        remediation="a remediation",
    )
    assert result.check_id == "X"
    assert result.phase is Phase.INSTALLATION
    assert result.title == "a title"
    assert result.severity == Severity.CRITICAL
    assert result.status == CheckStatus.PASS
    assert result.detail == "a detail"
    assert result.remediation == "a remediation"


# ---------------------------------------------------------------------------
# DoctorContext shape
# ---------------------------------------------------------------------------


def test_doctor_context_installation_token_defaults_to_empty_string() -> None:
    """installation_token defaults to "" when omitted (Rev 3 invariant).

    The minted App token is threaded by value, never read from
    os.environ -- see plan section 3/4.
    """
    ctx = DoctorContext(
        project_root="",
        home_dir="",
        env={},
        which=_unused_which,
        runner=_unused_runner,
        run=_unused_run,
        fetch_secret=_unused_fetch_secret,
    )
    assert ctx.installation_token == ""


def test_doctor_context_declares_run_and_fetch_secret_fields() -> None:
    """DoctorContext declares the Phase 4/6 seams even if unused here.

    Only field *existence* is asserted -- their call signature and
    semantics belong to Phase 4 (``run``, reused by
    ``sandbox_config.read_and_validate``) and Phase 6 (``fetch_secret``,
    the bws seam), not Phase 1.
    """
    field_names = {f.name for f in dataclasses.fields(DoctorContext)}
    for expected in ("run", "fetch_secret", "installation_token"):
        assert expected in field_names, (
            f"DoctorContext must declare a {expected!r} field"
        )


# ---------------------------------------------------------------------------
# Exception contract (BLOCKING #1, section 3/14) -- load-bearing
# ---------------------------------------------------------------------------


def _phase_result(check_id: str, phase: Phase) -> CheckResult:
    """Build a passing result for phase-selection tests."""
    return CheckResult(
        check_id=check_id,
        phase=phase,
        title=check_id,
        severity=Severity.CRITICAL,
        status=CheckStatus.PASS,
        detail="passed",
        remediation="none",
    )


def test_run_report_selects_one_phase_without_calling_unselected_checks(
) -> None:
    """Selecting one phase executes only checks owned by that phase."""
    calls: list[str] = []

    def selected(ctx: DoctorContext) -> CheckResult:
        del ctx
        calls.append("selected")
        return _phase_result("selected", Phase.CONFIGURATION)

    def unselected(ctx: DoctorContext) -> CheckResult:
        del ctx
        raise AssertionError("unselected check was called")

    checks = (
        _make_check(
            "selected", phase=Phase.CONFIGURATION, fn=selected
        ),
        _make_check("unselected", phase=Phase.LIVE, fn=unselected),
    )

    results = run_report(
        _make_ctx(), (Phase.CONFIGURATION,), checks=checks
    )

    assert calls == ["selected"]
    assert [result.check_id for result in results] == ["selected"]


def test_run_report_repeated_phase_is_executed_once() -> None:
    """Repeated phase selectors do not duplicate catalog execution."""
    calls: list[str] = []

    def selected(ctx: DoctorContext) -> CheckResult:
        del ctx
        calls.append("selected")
        return _phase_result("selected", Phase.INSTALLATION)

    checks = (
        _make_check(
            "selected", phase=Phase.INSTALLATION, fn=selected
        ),
    )

    results = run_report(
        _make_ctx(),
        (Phase.INSTALLATION, Phase.INSTALLATION),
        checks=checks,
    )

    assert calls == ["selected"]
    assert [result.check_id for result in results] == ["selected"]


def test_run_report_defaults_to_all_phases_in_catalog_order() -> None:
    """Omitted phases execute every phase while preserving catalog order."""
    calls: list[str] = []

    def result_for(check_id: str, phase: Phase) -> CheckFn:
        def run(ctx: DoctorContext) -> CheckResult:
            del ctx
            calls.append(check_id)
            return _phase_result(check_id, phase)

        return run

    checks = (
        _make_check(
            "live",
            phase=Phase.LIVE,
            fn=result_for("live", Phase.LIVE),
        ),
        _make_check(
            "installation",
            phase=Phase.INSTALLATION,
            fn=result_for("installation", Phase.INSTALLATION),
        ),
        _make_check(
            "configuration",
            phase=Phase.CONFIGURATION,
            fn=result_for("configuration", Phase.CONFIGURATION),
        ),
    )

    results = run_report(_make_ctx(), checks=checks)

    assert calls == ["live", "installation", "configuration"]
    assert [result.check_id for result in results] == calls


def test_run_report_uses_catalog_metadata_for_emitted_results() -> None:
    """Catalog ownership overrides inconsistent function metadata."""

    def inconsistent(ctx: DoctorContext) -> CheckResult:
        del ctx
        return CheckResult(
            check_id="wrong-id",
            phase=Phase.INSTALLATION,
            title="wrong title",
            severity=Severity.WARNING,
            status=CheckStatus.PASS,
            detail="the check-specific outcome",
            remediation="wrong remediation",
        )

    check = _make_check(
        "CATALOG_OWNER",
        title="Catalog title",
        severity=Severity.CRITICAL,
        phase=Phase.LIVE,
        fix="Catalog remediation",
        fn=inconsistent,
    )

    result = run_report(
        _make_ctx(), (Phase.LIVE,), checks=(check,)
    )[0]

    assert result == CheckResult(
        check_id="CATALOG_OWNER",
        phase=Phase.LIVE,
        title="Catalog title",
        severity=Severity.CRITICAL,
        status=CheckStatus.PASS,
        detail="the check-specific outcome",
        remediation="Catalog remediation",
    )


def test_run_gate_collects_every_critical_failure() -> None:
    """The gate reports every selected critical failure together."""

    def critical_failure(check_id: str) -> CheckFn:
        def run(ctx: DoctorContext) -> CheckResult:
            del ctx
            return CheckResult(
                check_id=check_id,
                phase=Phase.INSTALLATION,
                title="failure",
                severity=Severity.CRITICAL,
                status=CheckStatus.FAIL,
                detail="failed",
                remediation="repair it",
            )

        return run

    checks = (
        _make_check(
            "A", phase=Phase.INSTALLATION, fn=critical_failure("A")
        ),
        _make_check(
            "B", phase=Phase.INSTALLATION, fn=critical_failure("B")
        ),
    )
    with pytest.raises(doctor.DoctorGateError) as captured:
        run_gate(_make_ctx(), (Phase.INSTALLATION,), checks=checks)
    assert [item.check_id for item in captured.value.results] == ["A", "B"]


def test_run_report_catches_raising_check_and_synthesizes_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising Check becomes a FAIL result; run_report never aborts.

    Pins the exact shape from section 3: ``CheckResult(status=FAIL,
    severity=check.severity, detail=repr(exc), fix=check.fix)``. Only
    those four fields are asserted -- the plan is silent on
    check_id/title for the synthesized result, so a correct
    implementation is free to choose there.
    """
    exc = ValueError("kaboom")

    def _boom(ctx: DoctorContext) -> CheckResult:
        raise exc

    raising = _make_check(
        "SYNTH_RAISE",
        severity=Severity.CRITICAL,
        fix="fix the boom",
        fn=_boom,
    )

    calls: list[str] = []

    def _second(ctx: DoctorContext) -> CheckResult:
        calls.append("second")
        return CheckResult(
            check_id="SYNTH_SECOND",
            phase=Phase.CONFIGURATION,
            title="t",
            severity=Severity.WARNING,
            status=CheckStatus.PASS,
            detail="ok",
            remediation="",
        )

    second = _make_check("SYNTH_SECOND", fn=_second)

    monkeypatch.setattr(doctor, "CATALOG", [raising, second])

    results = doctor.run_report(_make_ctx())

    assert calls == ["second"], (
        "run_report must continue running checks after one raises"
    )
    assert len(results) == 2, (
        "run_report must still return a result for the raising check "
        f"(not skip it); got {len(results)} results"
    )
    failed = results[0]
    assert failed.status == CheckStatus.FAIL
    assert failed.severity == Severity.CRITICAL
    assert failed.detail == repr(exc)
    assert failed.remediation == "fix the boom"


def test_run_gate_raising_critical_check_is_aggregated_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising critical check becomes an aggregated gate failure.

    This is the load-bearing half of BLOCKING #1: without the runner's
    catch, the raw exception would propagate out of run_gate and bypass
    SystemExit(1) entirely -- exactly the cryptic-crash mode #193 exists
    to eliminate. Proven here because pytest.raises(SystemExit) would
    fail (propagating the RuntimeError instead) if the exception were
    not caught internally.
    """

    def _boom(ctx: DoctorContext) -> CheckResult:
        raise RuntimeError("gate boom")

    raising = _make_check(
        "SYNTH_GATE_RAISE",
        severity=Severity.CRITICAL,
        phase=Phase.CONFIGURATION,
        daemon_native=False,
        fix="fix gate boom",
        fn=_boom,
    )
    monkeypatch.setattr(doctor, "CATALOG", [raising])

    with pytest.raises(doctor.DoctorGateError) as exc_info:
        doctor.run_gate(_make_ctx(), (Phase.CONFIGURATION,))

    assert [result.check_id for result in exc_info.value.results] == [
        "SYNTH_GATE_RAISE"
    ]


def test_run_gate_raising_warning_check_does_not_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising WARNING-severity check must not trigger SystemExit.

    The exception contract always synthesizes status=FAIL, but the
    gate's exit trigger is specifically a CRITICAL-severity FAIL; a
    WARNING check that raises must warn-and-proceed like any other
    WARNING failure, not exit.
    """

    def _boom(ctx: DoctorContext) -> CheckResult:
        raise RuntimeError("warn boom")

    raising = _make_check(
        "SYNTH_WARN_RAISE",
        severity=Severity.WARNING,
        phase=Phase.CONFIGURATION,
        daemon_native=False,
        fix="fix warn boom",
        fn=_boom,
    )
    monkeypatch.setattr(doctor, "CATALOG", [raising])

    # Must not raise.
    result = doctor.run_gate(_make_ctx(), (Phase.CONFIGURATION,))
    assert [item.check_id for item in result] == ["SYNTH_WARN_RAISE"]


# ---------------------------------------------------------------------------
# run_gate runner semantics
# ---------------------------------------------------------------------------


def test_run_gate_collects_after_first_critical_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_gate executes the complete selection before raising."""
    calls: list[str] = []

    def _pass(ctx: DoctorContext) -> CheckResult:
        calls.append("pass")
        return CheckResult(
            check_id="P",
            phase=Phase.CONFIGURATION,
            title="p",
            severity=Severity.WARNING,
            status=CheckStatus.PASS,
            detail="ok",
            remediation="",
        )

    def _warn_fail(ctx: DoctorContext) -> CheckResult:
        calls.append("warn_fail")
        return CheckResult(
            check_id="W",
            phase=Phase.CONFIGURATION,
            title="w",
            severity=Severity.WARNING,
            status=CheckStatus.WARN,
            detail="meh",
            remediation="fix w",
        )

    def _critical_fail(ctx: DoctorContext) -> CheckResult:
        calls.append("critical_fail")
        return CheckResult(
            check_id="C",
            phase=Phase.CONFIGURATION,
            title="c",
            severity=Severity.CRITICAL,
            status=CheckStatus.FAIL,
            detail="bad",
            remediation="fix c",
        )

    def _never(ctx: DoctorContext) -> CheckResult:
        calls.append("never")
        return CheckResult(
            check_id="N",
            phase=Phase.CONFIGURATION,
            title="n",
            severity=Severity.CRITICAL,
            status=CheckStatus.PASS,
            detail="ok",
            remediation="",
        )

    catalog = [
        _make_check("P", severity=Severity.WARNING, fn=_pass),
        _make_check("W", severity=Severity.WARNING, fn=_warn_fail),
        _make_check("C", severity=Severity.CRITICAL, fn=_critical_fail),
        _make_check("N", severity=Severity.CRITICAL, fn=_never),
    ]
    monkeypatch.setattr(doctor, "CATALOG", catalog)

    with pytest.raises(doctor.DoctorGateError) as exc_info:
        doctor.run_gate(_make_ctx(), (Phase.CONFIGURATION,))

    assert calls == ["pass", "warn_fail", "critical_fail", "never"]
    assert [item.check_id for item in exc_info.value.results] == [
        "P",
        "W",
        "C",
        "N",
    ]


def test_run_gate_only_runs_checks_for_the_requested_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_gate(phase=X) ignores checks tagged with a different phase."""
    calls: list[str] = []

    def _phase_a(ctx: DoctorContext) -> CheckResult:
        calls.append("A")
        return CheckResult(
            check_id="A",
            phase=Phase.CONFIGURATION,
            title="a",
            severity=Severity.WARNING,
            status=CheckStatus.PASS,
            detail="ok",
            remediation="",
        )

    def _phase_b(ctx: DoctorContext) -> CheckResult:
        calls.append("B")
        return CheckResult(
            check_id="B",
            phase=Phase.LIVE,
            title="b",
            severity=Severity.WARNING,
            status=CheckStatus.PASS,
            detail="ok",
            remediation="",
        )

    catalog = [
        _make_check("A", phase=Phase.CONFIGURATION, fn=_phase_a),
        _make_check("B", phase=Phase.LIVE, fn=_phase_b),
    ]
    monkeypatch.setattr(doctor, "CATALOG", catalog)

    doctor.run_gate(_make_ctx(), (Phase.CONFIGURATION,))

    assert calls == ["A"], (
        "run_gate(configuration) must only run configuration-phase "
        f"checks; got {calls!r}"
    )


@pytest.mark.parametrize("phase", [Phase.CONFIGURATION, Phase.LIVE])
def test_run_gate_includes_checks_regardless_of_legacy_native_flag(
    monkeypatch: pytest.MonkeyPatch,
    phase: Phase,
) -> None:
    """The phase catalog, not a legacy native flag, controls selection."""
    calls: list[str] = []

    def _native_pass(ctx: DoctorContext) -> CheckResult:
        calls.append("native")
        return _phase_result("NATIVE", phase)

    def _normal_pass(ctx: DoctorContext) -> CheckResult:
        return CheckResult(
            check_id="NORMAL",
            phase=phase,
            title="n",
            severity=Severity.CRITICAL,
            status=CheckStatus.PASS,
            detail="ok",
            remediation="",
        )

    catalog = [
        _make_check(
            "NATIVE",
            phase=phase,
            daemon_native=True,
            severity=Severity.CRITICAL,
            fn=_native_pass,
        ),
        _make_check(
            "NORMAL",
            phase=phase,
            daemon_native=False,
            fn=_normal_pass,
        ),
    ]
    monkeypatch.setattr(doctor, "CATALOG", catalog)

    doctor.run_gate(_make_ctx(), (phase,))
    assert calls == ["native"]


def test_run_report_includes_daemon_native_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_report (standalone) includes daemon_native checks always."""

    def _native(ctx: DoctorContext) -> CheckResult:
        return CheckResult(
            check_id="NATIVE",
            phase=Phase.CONFIGURATION,
            title="n",
            severity=Severity.CRITICAL,
            status=CheckStatus.FAIL,
            detail="native fail",
            remediation="native fix",
        )

    catalog = [_make_check("NATIVE", daemon_native=True, fn=_native)]
    monkeypatch.setattr(doctor, "CATALOG", catalog)

    results = doctor.run_report(_make_ctx())

    assert len(results) == 1
    assert results[0].check_id == "NATIVE"
    assert results[0].status == CheckStatus.FAIL


# ---------------------------------------------------------------------------
# Installation phase -- credential-free package integrity
# ---------------------------------------------------------------------------


def test_create_context_accepts_explicit_config_without_project_root(
    tmp_path: Path,
) -> None:
    """An explicit config path is sufficient and never mutates the input."""
    config_path = tmp_path / ".bh" / "config.env"
    _write_config_env(tmp_path, _VALID_CONFIG_ENV)
    environment: dict[str, str] = {}

    ctx = doctor.create_context(
        env=environment,
        config_path=config_path,
        home_dir=str(tmp_path),
        which=_unused_which,
        runner=_unused_runner,
        run=_unused_run,
        fetch_secret=_unused_fetch_secret,
    )

    assert environment == {}
    assert ctx.project_root == str(tmp_path)
    assert ctx.home_dir == str(tmp_path)


def test_configuration_checks_use_an_arbitrary_explicit_config_path(
    tmp_path: Path,
) -> None:
    """Config checks use the selected path rather than reconstructing it."""
    config_path = tmp_path / "operator-selected.env"
    config_path.write_text(_VALID_CONFIG_ENV, encoding="utf-8")
    ctx = doctor.create_context(
        env={},
        config_path=config_path,
        which=_unused_which,
        runner=_unused_runner,
        run=_unused_run,
        fetch_secret=_unused_fetch_secret,
    )
    checks = (
        _get_check("CFG_CONFIG_ENV"),
        _get_check("CFG_REQUIRED_KEYS"),
    )

    results = run_report(ctx, (Phase.CONFIGURATION,), checks=checks)

    assert [result.status for result in results] == [
        CheckStatus.PASS,
        CheckStatus.PASS,
    ]


def test_malformed_config_is_the_authoritative_configuration_failure(
    tmp_path: Path,
) -> None:
    """Strict resolver errors prevent contradictory required-key PASSes."""
    config_path = tmp_path / "config.env"
    config_path.write_text(
        _VALID_CONFIG_ENV + "not a config assignment\n",
        encoding="utf-8",
    )
    ctx = doctor.create_context(
        env={},
        config_path=config_path,
        which=_unused_which,
        runner=_unused_runner,
        run=_unused_run,
        fetch_secret=_unused_fetch_secret,
    )

    result = _get_check("CFG_REQUIRED_KEYS")(ctx)

    assert ctx.config is None
    assert "invalid sandbox config line" in ctx.config_error
    assert result.status is CheckStatus.FAIL
    assert result.severity is Severity.CRITICAL
    assert "invalid sandbox config line" in result.detail


def test_export_prefixed_config_uses_the_shared_resolver(
    tmp_path: Path,
) -> None:
    """Supported export assignments produce a valid configuration result."""
    config_path = tmp_path / "config.env"
    exported = "\n".join(
        f"export {line}" for line in _VALID_CONFIG_ENV.splitlines()
    )
    config_path.write_text(exported + "\n", encoding="utf-8")
    ctx = doctor.create_context(
        env={},
        config_path=config_path,
        which=_unused_which,
        runner=_unused_runner,
        run=_unused_run,
        fetch_secret=_unused_fetch_secret,
    )

    result = _get_check("CFG_REQUIRED_KEYS")(ctx)

    assert ctx.config is not None
    assert ctx.config_error == ""
    assert result.status is CheckStatus.PASS


@pytest.mark.parametrize(
    "failure_kind",
    ["permission", "directory", "invalid-utf8"],
)
def test_expected_config_read_errors_do_not_block_installation(
    tmp_path: Path,
    failure_kind: str,
) -> None:
    """Expected local read failures remain configuration result state."""
    config_path = tmp_path / "config.env"
    resolver_patch = nullcontext()
    if failure_kind == "permission":
        config_path.write_text(_VALID_CONFIG_ENV, encoding="utf-8")
        resolver_patch = patch.object(
            doctor.sandbox_config,
            "resolve_config",
            side_effect=PermissionError("config access denied"),
        )
    elif failure_kind == "directory":
        config_path.mkdir()
    else:
        config_path.write_bytes(b"\xff\xfe\xfa")

    with resolver_patch:
        ctx = doctor.create_context(
            env={},
            config_path=config_path,
            which=lambda name: None,
            runner=_unused_runner,
            run=_unused_run,
            fetch_secret=_unused_fetch_secret,
        )

    installation = run_report(ctx, (Phase.INSTALLATION,))
    required = _get_check("CFG_REQUIRED_KEYS")(ctx)

    assert ctx.config is None
    assert ctx.config_error
    assert all(result.status is CheckStatus.PASS for result in installation)
    assert required.status is CheckStatus.FAIL
    assert required.severity is Severity.CRITICAL


def test_live_repository_checks_use_explicit_resolved_config_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use explicit config for live probes without environment writes."""
    config_path = tmp_path / "selected.env"
    config_path.write_text(_VALID_CONFIG_ENV, encoding="utf-8")
    config_keys = (
        "BH_REPO_OWNER",
        "BH_REPO_NAME",
        "BH_GITHUB_APP_ID",
        "BH_GITHUB_APP_INSTALLATION_ID",
        "BH_GITHUB_APP_KEY_PROVIDER",
        "BWS_PEM_SECRET_ID",
    )
    for key in config_keys:
        monkeypatch.delenv(key, raising=False)
    caller_env: dict[str, str] = {}
    ruleset_args: list[tuple[str, str, str]] = []
    runner_commands: list[list[str]] = []

    def ruleset_probe(
        owner: str,
        repo: str,
        *,
        app_id: str,
        runner: Callable[[list[str]], subprocess.CompletedProcess[str]],
    ) -> RulesetStatus:
        del runner
        ruleset_args.append((owner, repo, app_id))
        return RulesetStatus.MATCH

    def live_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
        runner_commands.append(args)
        if args[:3] == ["gh", "label", "list"]:
            stdout = "\n".join(sorted(_REQUIRED_LABELS)) + "\n"
        else:
            stdout = json.dumps(
                [{"login": "operator", "role_name": "admin"}]
            )
        return subprocess.CompletedProcess(args, 0, stdout, "")

    ctx = doctor.create_context(
        env=caller_env,
        config_path=config_path,
        which=_unused_which,
        runner=live_runner,
        run=_unused_run,
        fetch_secret=_unused_fetch_secret,
    )
    checks = tuple(
        _get_check(check_id)
        for check_id in (
            "RULESET_MAIN",
            "RULESET_FEATURE",
            "LABELS_PRESENT",
            "GH_REPO_ADMIN",
        )
    )

    with patch.object(
        doctor.ruleset_status,
        "ruleset_is_provisioned",
        side_effect=ruleset_probe,
    ):
        results = run_report(ctx, (Phase.LIVE,), checks=checks)

    assert all(result.status is CheckStatus.PASS for result in results)
    assert ruleset_args == [
        ("my-org", "my-sandbox", "12345"),
        ("my-org", "my-sandbox", "12345"),
    ]
    assert any("my-org/my-sandbox" in command for command in runner_commands)
    assert any(
        "repos/my-org/my-sandbox/collaborators?permission=admin" in command
        for command in runner_commands
    )
    assert caller_env == {}
    assert ctx.env == {}
    assert all(key not in os.environ for key in config_keys)


def test_pkg_provenance_check_validates_the_packaged_record() -> None:
    """PKG_PROVENANCE passes when the runtime provenance loader succeeds."""
    check = _get_check("PKG_PROVENANCE")
    with patch.object(
        doctor, "load_provenance", return_value=object()
    ) as load:
        result = check(_make_ctx())
    assert result.status is CheckStatus.PASS
    load.assert_called_once_with()


def test_pkg_imports_check_imports_every_required_module() -> None:
    """PKG_IMPORTS imports each supported runtime package boundary."""
    check = _get_check("PKG_IMPORTS")
    with patch.object(doctor.importlib, "import_module") as import_module:
        result = check(_make_ctx())
    assert result.status is CheckStatus.PASS
    assert [call.args[0] for call in import_module.call_args_list] == [
        "baton_harness",
        "baton_harness.chain.cli",
        "baton_harness.vendor.symphony.config",
    ]


def test_pkg_entry_points_check_requires_all_console_scripts() -> None:
    """PKG_ENTRY_POINTS fails when an installed console script is absent."""
    check = _get_check("PKG_ENTRY_POINTS")
    distribution = Mock()
    distribution.entry_points = ()
    with patch.object(
        doctor.metadata, "distribution", return_value=distribution
    ):
        result = check(_make_ctx())
    assert result.status is CheckStatus.FAIL
    assert "bh-daemon" in result.detail


def test_pkg_resources_check_reads_every_packaged_resource() -> None:
    """PKG_RESOURCES reads every member of the canonical resource manifest."""
    check = _get_check("PKG_RESOURCES")
    with patch.object(
        doctor.resources, "read_bytes", return_value=b""
    ) as read:
        result = check(_make_ctx())
    assert result.status is CheckStatus.PASS
    assert [call.args[0] for call in read.call_args_list] == list(
        doctor.resources.RESOURCE_NAMES
    )


def test_pkg_workflow_check_parses_the_packaged_default() -> None:
    """PKG_WORKFLOW validates the packaged workflow through the CLI loader."""
    check = _get_check("PKG_WORKFLOW")
    packaged_path = Path("packaged-WORKFLOW.md")
    with (
        patch(
            "baton_harness.chain.cli._workflow_path",
            return_value=nullcontext(packaged_path),
        ),
        patch.object(doctor, "load_workflow", return_value=object()) as load,
    ):
        result = check(_make_ctx())
    assert result.status is CheckStatus.PASS
    load.assert_called_once_with(str(packaged_path))


def test_force_pr_tripwire_belongs_to_installation_phase() -> None:
    """FORCE_PR_TRIPWIRE is part of offline installation integrity."""
    assert _get_check("FORCE_PR_TRIPWIRE").phase is Phase.INSTALLATION


def test_installation_phase_never_uses_live_tools() -> None:
    """All real installation checks pass without credentials or live tools."""

    def forbidden_runner(
        command: list[str],
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"live command used: {command}")

    ctx = doctor.create_context(
        env={},
        which=lambda name: None,
        runner=forbidden_runner,
        run=forbidden_runner,
        fetch_secret=_unused_fetch_secret,
    )
    results = run_report(ctx, (Phase.INSTALLATION,))
    assert all(result.status is CheckStatus.PASS for result in results)
    assert [result.check_id for result in results] == [
        "PKG_PROVENANCE",
        "PKG_IMPORTS",
        "PKG_ENTRY_POINTS",
        "PKG_RESOURCES",
        "PKG_WORKFLOW",
        "FORCE_PR_TRIPWIRE",
    ]


# ---------------------------------------------------------------------------
# Catalog shape -- every Phase-1 check present with the right metadata
# ---------------------------------------------------------------------------

_EXPECTED_PHASE_1_CHECK_IDS = {
    "CLI_GH",
    "CLI_BWS",
    "CLI_CLAUDE",
    "CLI_UV",
    "ENV_PROJECT_ROOT",
    "ENV_HOST_ENV",
    "CFG_CONFIG_ENV",
    "CFG_REQUIRED_KEYS",
    "CFG_OPTIONAL_SECRET_IDS",
    "ENV_BWS_ACCESS_TOKEN",
    "GITIGNORE_SYMPHONY",
    "CRED_ANTHROPIC_UNSET",
    "FORCE_PR_TRIPWIRE",
    "GIT_CRED_HELPER",
}

_EXPECTED_METADATA: dict[str, tuple[Severity, bool]] = {
    "CLI_GH": (Severity.CRITICAL, False),
    "CLI_BWS": (Severity.CRITICAL, False),
    "CLI_CLAUDE": (Severity.CRITICAL, False),
    "CLI_UV": (Severity.WARNING, False),
    "ENV_PROJECT_ROOT": (Severity.CRITICAL, False),
    "ENV_HOST_ENV": (Severity.WARNING, False),
    "CFG_CONFIG_ENV": (Severity.CRITICAL, False),
    "CFG_REQUIRED_KEYS": (Severity.CRITICAL, False),
    "CFG_OPTIONAL_SECRET_IDS": (Severity.WARNING, False),
    "ENV_BWS_ACCESS_TOKEN": (Severity.CRITICAL, False),
    "GITIGNORE_SYMPHONY": (Severity.CRITICAL, False),
    "CRED_ANTHROPIC_UNSET": (Severity.CRITICAL, True),
    "FORCE_PR_TRIPWIRE": (Severity.CRITICAL, False),
    "GIT_CRED_HELPER": (Severity.CRITICAL, True),
}


def test_catalog_contains_all_phase_1_checks() -> None:
    """CATALOG contains (at least) every Phase-1 check_id."""
    catalog_ids = {c.check_id for c in doctor.CATALOG}
    missing = _EXPECTED_PHASE_1_CHECK_IDS - catalog_ids
    assert not missing, f"CATALOG is missing Phase-1 check ids: {missing!r}"


def test_catalog_has_no_duplicate_check_ids() -> None:
    """CATALOG must not contain two checks sharing a check_id."""
    catalog_ids = [c.check_id for c in doctor.CATALOG]
    assert len(catalog_ids) == len(set(catalog_ids)), (
        f"CATALOG must not contain duplicate check_id values: {catalog_ids!r}"
    )


@pytest.mark.parametrize("check_id", sorted(_EXPECTED_PHASE_1_CHECK_IDS))
def test_catalog_check_exposes_required_static_metadata(
    check_id: str,
) -> None:
    """Every catalog Check exposes required static metadata.

    check_id/title/severity/phase/daemon_native/fix are required by
    run_report/run_gate and by the exception contract, which reads
    check.severity and check.fix off the Check itself when
    synthesizing a FAIL result.
    """
    check = _get_check(check_id)
    assert check.check_id == check_id
    assert isinstance(check.title, str) and check.title
    assert isinstance(check.severity, Severity)
    assert isinstance(check.phase, Phase)
    assert isinstance(check.daemon_native, bool)
    assert isinstance(check.fix, str)


@pytest.mark.parametrize(
    "check_id, expected", sorted(_EXPECTED_METADATA.items())
)
def test_catalog_check_metadata_matches_the_plan_catalog(
    check_id: str,
    expected: tuple[Severity, bool],
) -> None:
    """Each check's severity/daemon_native/phase matches section 6's table."""
    check = _get_check(check_id)
    expected_severity, expected_daemon_native = expected
    assert check.severity == expected_severity
    assert check.daemon_native is expected_daemon_native
    expected_phase = {
        "FORCE_PR_TRIPWIRE": Phase.INSTALLATION,
        "GIT_CRED_HELPER": Phase.LIVE,
    }.get(check_id, Phase.CONFIGURATION)
    assert check.phase is expected_phase


# ---------------------------------------------------------------------------
# CLI_GH / CLI_BWS / CLI_CLAUDE / CLI_UV -- on-PATH checks
# ---------------------------------------------------------------------------

_CLI_CHECKS = [
    ("CLI_GH", "gh", Severity.CRITICAL, CheckStatus.FAIL),
    ("CLI_BWS", "bws", Severity.CRITICAL, CheckStatus.FAIL),
    ("CLI_CLAUDE", "claude", Severity.CRITICAL, CheckStatus.FAIL),
    ("CLI_UV", "uv", Severity.WARNING, CheckStatus.WARN),
]


@pytest.mark.parametrize(
    "check_id, binary, severity, fail_status", _CLI_CHECKS
)
def test_cli_on_path_check_passes_when_which_finds_binary(
    tmp_path: Path,
    check_id: str,
    binary: str,
    severity: Severity,
    fail_status: CheckStatus,
) -> None:
    """Each CLI on-PATH check PASSes when ``which`` resolves the binary."""
    check = _get_check(check_id)

    def _which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name == binary else None

    _write_config_env(tmp_path, _VALID_CONFIG_ENV)
    result = check(_make_ctx(project_root=str(tmp_path), which=_which))

    assert result.check_id == check_id
    assert result.status == CheckStatus.PASS
    assert result.severity == severity
    assert result.detail
    assert result.remediation is not None


@pytest.mark.parametrize(
    "check_id, binary, severity, fail_status", _CLI_CHECKS
)
def test_cli_on_path_check_reports_failure_when_binary_missing(
    tmp_path: Path,
    check_id: str,
    binary: str,
    severity: Severity,
    fail_status: CheckStatus,
) -> None:
    """A missing binary FAILs (CRITICAL checks) or WARNs (CLI_UV)."""
    check = _get_check(check_id)

    _write_config_env(tmp_path, _VALID_CONFIG_ENV)
    result = check(
        _make_ctx(project_root=str(tmp_path), which=lambda name: None)
    )

    assert result.status == fail_status, (
        f"{check_id} on a missing binary must report status "
        f"{fail_status!r}; got {result.status!r}"
    )
    assert result.severity == severity
    assert binary in result.detail or binary in result.remediation, (
        f"{check_id} must name the missing binary {binary!r} in its "
        f"detail or fix text; got detail={result.detail!r} "
        f"remediation={result.remediation!r}"
    )


# ---------------------------------------------------------------------------
# ENV_PROJECT_ROOT
# ---------------------------------------------------------------------------


class TestEnvProjectRoot:
    """BH_PROJECT_ROOT set and is a directory."""

    def test_fails_when_unset(self) -> None:
        """Empty project_root (unset BH_PROJECT_ROOT) FAILs."""
        check = _get_check("ENV_PROJECT_ROOT")
        result = check(_make_ctx(project_root=""))
        assert result.status == CheckStatus.FAIL
        assert result.severity == Severity.CRITICAL

    def test_fails_when_not_a_directory(self, tmp_path: Path) -> None:
        """A project_root that doesn't exist as a directory FAILs."""
        check = _get_check("ENV_PROJECT_ROOT")
        missing = str(tmp_path / "does-not-exist")
        result = check(_make_ctx(project_root=missing))
        assert result.status == CheckStatus.FAIL

    def test_passes_when_set_and_is_a_directory(self, tmp_path: Path) -> None:
        """A project_root pointing at a real directory PASSes."""
        check = _get_check("ENV_PROJECT_ROOT")
        result = check(_make_ctx(project_root=str(tmp_path)))
        assert result.status == CheckStatus.PASS


# ---------------------------------------------------------------------------
# ENV_HOST_ENV
# ---------------------------------------------------------------------------


class TestEnvHostEnv:
    """~/.config/baton-harness/host.env presence (WARNING)."""

    def test_warns_when_host_env_absent(self, tmp_path: Path) -> None:
        """No host.env file under home_dir WARNs."""
        check = _get_check("ENV_HOST_ENV")
        result = check(_make_ctx(home_dir=str(tmp_path)))
        assert result.status == CheckStatus.WARN
        assert result.severity == Severity.WARNING

    def test_passes_when_host_env_present(self, tmp_path: Path) -> None:
        """host.env present under home_dir PASSes."""
        check = _get_check("ENV_HOST_ENV")
        host_env_dir = tmp_path / ".config" / "baton-harness"
        host_env_dir.mkdir(parents=True)
        (host_env_dir / "host.env").write_text(
            "BH_PROJECT_ROOT=/x\n", encoding="utf-8"
        )
        result = check(_make_ctx(home_dir=str(tmp_path)))
        assert result.status == CheckStatus.PASS


# ---------------------------------------------------------------------------
# CFG_CONFIG_ENV
# ---------------------------------------------------------------------------


class TestCfgConfigEnv:
    """.bh/config.env exists under project_root (CRITICAL)."""

    def test_fails_when_config_env_missing(self, tmp_path: Path) -> None:
        """No .bh/config.env FAILs."""
        check = _get_check("CFG_CONFIG_ENV")
        result = check(_make_ctx(project_root=str(tmp_path)))
        assert result.status == CheckStatus.FAIL
        assert result.severity == Severity.CRITICAL

    def test_passes_when_config_env_present(self, tmp_path: Path) -> None:
        """A present .bh/config.env PASSes regardless of content."""
        check = _get_check("CFG_CONFIG_ENV")
        _write_config_env(tmp_path, "BH_REPO_OWNER=x\n")
        result = check(_make_ctx(project_root=str(tmp_path)))
        assert result.status == CheckStatus.PASS


# ---------------------------------------------------------------------------
# CFG_REQUIRED_KEYS
# ---------------------------------------------------------------------------


class TestCfgRequiredKeys:
    """Required config.env keys present + shape-valid (CRITICAL).

    Reuses sandbox_config's validation *rules* (per the briefing) but
    must not perform sandbox_config.read_and_validate's network ``gh
    api`` call -- Phase A is explicitly no-network (plan section 4).
    """

    def test_fails_when_config_env_missing(self, tmp_path: Path) -> None:
        """No config file at all FAILs (nothing to validate)."""
        check = _get_check("CFG_REQUIRED_KEYS")
        result = check(_make_ctx(project_root=str(tmp_path)))
        assert result.status == CheckStatus.FAIL

    def test_passes_with_all_required_keys_valid(self, tmp_path: Path) -> None:
        """All 5 required keys present and shape-valid PASSes."""
        check = _get_check("CFG_REQUIRED_KEYS")
        _write_config_env(tmp_path, _VALID_CONFIG_ENV)
        result = check(_make_ctx(project_root=str(tmp_path)))
        assert result.status == CheckStatus.PASS
        assert result.severity == Severity.CRITICAL

    def test_fails_when_a_required_key_is_missing(
        self, tmp_path: Path
    ) -> None:
        """A missing required key FAILs and names the key."""
        check = _get_check("CFG_REQUIRED_KEYS")
        content = _VALID_CONFIG_ENV.replace(
            "BWS_PEM_SECRET_ID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n",
            "",
        )
        _write_config_env(tmp_path, content)
        result = check(_make_ctx(project_root=str(tmp_path)))
        assert result.status == CheckStatus.FAIL
        assert "BWS_PEM_SECRET_ID" in result.detail

    def test_passes_when_required_key_is_sourced_only_from_env(
        self, tmp_path: Path
    ) -> None:
        """A valid required key supplied only by ctx.env PASSes."""
        check = _get_check("CFG_REQUIRED_KEYS")
        content = _VALID_CONFIG_ENV.replace(
            "BWS_PEM_SECRET_ID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n",
            "",
        )
        _write_config_env(tmp_path, content)
        result = check(
            _make_ctx(
                project_root=str(tmp_path),
                env={
                    "BWS_PEM_SECRET_ID": (
                        "11111111-2222-3333-4444-555555555555"
                    )
                },
            )
        )
        assert result.status == CheckStatus.PASS

    def test_fails_when_required_key_is_absent_from_file_and_env(
        self, tmp_path: Path
    ) -> None:
        """A required key absent from the file and ctx.env still FAILs."""
        check = _get_check("CFG_REQUIRED_KEYS")
        content = _VALID_CONFIG_ENV.replace(
            "BWS_PEM_SECRET_ID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n",
            "",
        )
        _write_config_env(tmp_path, content)
        result = check(_make_ctx(project_root=str(tmp_path), env={}))
        assert result.status == CheckStatus.FAIL
        assert "BWS_PEM_SECRET_ID" in result.detail

    def test_fails_when_a_required_value_is_malformed(
        self, tmp_path: Path
    ) -> None:
        """A malformed required value (non-numeric app id) FAILs."""
        check = _get_check("CFG_REQUIRED_KEYS")
        content = _VALID_CONFIG_ENV.replace(
            "BH_GITHUB_APP_ID=12345", "BH_GITHUB_APP_ID=not-a-number"
        )
        _write_config_env(tmp_path, content)
        result = check(_make_ctx(project_root=str(tmp_path)))
        assert result.status == CheckStatus.FAIL
        assert "BH_GITHUB_APP_ID" in result.detail

    def test_never_calls_run_or_runner_seams(self, tmp_path: Path) -> None:
        """Phase A / no-network: must not touch run() or runner()."""
        check = _get_check("CFG_REQUIRED_KEYS")
        _write_config_env(tmp_path, _VALID_CONFIG_ENV)
        # _unused_run / _unused_runner (the ctx defaults) raise
        # AssertionError if invoked -- their mere presence as defaults
        # in _make_ctx() is the assertion here.
        result = check(_make_ctx(project_root=str(tmp_path)))
        assert result.status == CheckStatus.PASS


# ---------------------------------------------------------------------------
# CFG_OPTIONAL_SECRET_IDS
# ---------------------------------------------------------------------------


class TestCfgOptionalSecretIds:
    """Optional BWS_*_SECRET_ID shape-valid if set (WARNING)."""

    def test_skips_when_config_env_missing(self, tmp_path: Path) -> None:
        """No config file -> not applicable -> SKIP."""
        check = _get_check("CFG_OPTIONAL_SECRET_IDS")
        result = check(_make_ctx(project_root=str(tmp_path)))
        assert result.status == CheckStatus.SKIP

    def test_passes_when_optional_keys_absent(self, tmp_path: Path) -> None:
        """Optional keys simply not configured PASSes (not required)."""
        check = _get_check("CFG_OPTIONAL_SECRET_IDS")
        _write_config_env(tmp_path, _VALID_CONFIG_ENV)
        result = check(_make_ctx(project_root=str(tmp_path)))
        assert result.status == CheckStatus.PASS
        assert result.severity == Severity.WARNING

    def test_passes_when_optional_keys_are_valid_uuids(
        self, tmp_path: Path
    ) -> None:
        """A well-formed optional secret ID PASSes."""
        check = _get_check("CFG_OPTIONAL_SECRET_IDS")
        content = (
            _VALID_CONFIG_ENV
            + "BWS_GH_TOKEN_SECRET_ID="
            + "11111111-2222-3333-4444-555555555555\n"
        )
        _write_config_env(tmp_path, content)
        result = check(_make_ctx(project_root=str(tmp_path)))
        assert result.status == CheckStatus.PASS

    def test_warns_when_optional_key_is_malformed(
        self, tmp_path: Path
    ) -> None:
        """A malformed optional secret ID WARNs (not FAIL -- WARNING sev)."""
        check = _get_check("CFG_OPTIONAL_SECRET_IDS")
        content = _VALID_CONFIG_ENV + "BWS_GH_TOKEN_SECRET_ID=not-a-uuid\n"
        _write_config_env(tmp_path, content)
        result = check(_make_ctx(project_root=str(tmp_path)))
        assert result.status == CheckStatus.WARN
        assert result.severity == Severity.WARNING

    def test_passes_when_optional_key_is_sourced_only_from_env(
        self, tmp_path: Path
    ) -> None:
        """A valid optional secret ID supplied only by ctx.env PASSes."""
        check = _get_check("CFG_OPTIONAL_SECRET_IDS")
        _write_config_env(tmp_path, _VALID_CONFIG_ENV)
        result = check(
            _make_ctx(
                project_root=str(tmp_path),
                env={
                    "BWS_GH_TOKEN_SECRET_ID": (
                        "11111111-2222-3333-4444-555555555555"
                    )
                },
            )
        )
        assert result.status == CheckStatus.PASS

    def test_warns_when_env_sourced_optional_key_is_malformed(
        self, tmp_path: Path
    ) -> None:
        """An invalid optional secret ID supplied only by ctx.env WARNs."""
        check = _get_check("CFG_OPTIONAL_SECRET_IDS")
        _write_config_env(tmp_path, _VALID_CONFIG_ENV)
        result = check(
            _make_ctx(
                project_root=str(tmp_path),
                env={"BWS_GH_TOKEN_SECRET_ID": "not-a-uuid"},
            )
        )
        assert result.status == CheckStatus.WARN


# ---------------------------------------------------------------------------
# ENV_BWS_ACCESS_TOKEN
# ---------------------------------------------------------------------------


class TestEnvBwsAccessToken:
    """BWS_ACCESS_TOKEN presence/shape only -- never the value (CRITICAL)."""

    def test_fails_when_unset(self, tmp_path: Path) -> None:
        """No BWS_ACCESS_TOKEN in env FAILs."""
        check = _get_check("ENV_BWS_ACCESS_TOKEN")
        _write_config_env(tmp_path, _VALID_CONFIG_ENV)
        result = check(_make_ctx(project_root=str(tmp_path), env={}))
        assert result.status == CheckStatus.FAIL
        assert result.severity == Severity.CRITICAL

    def test_fails_when_set_but_empty(self, tmp_path: Path) -> None:
        """An empty-string value is treated as unset -- FAILs."""
        check = _get_check("ENV_BWS_ACCESS_TOKEN")
        _write_config_env(tmp_path, _VALID_CONFIG_ENV)
        result = check(
            _make_ctx(project_root=str(tmp_path), env={"BWS_ACCESS_TOKEN": ""})
        )
        assert result.status == CheckStatus.FAIL

    def test_passes_when_set_and_never_leaks_the_value(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A token PASSes without exposing its value or length in reports."""
        check = _get_check("ENV_BWS_ACCESS_TOKEN")
        _write_config_env(tmp_path, _VALID_CONFIG_ENV)
        ctx = _make_ctx(
            project_root=str(tmp_path),
            env={"BWS_ACCESS_TOKEN": _FAKE_BWS_TOKEN},
        )
        result = check(ctx)
        assert result.status == CheckStatus.PASS
        assert result.check_id == "ENV_BWS_ACCESS_TOKEN"
        assert result.severity is Severity.CRITICAL
        _assert_no_secret_leak(result, _FAKE_BWS_TOKEN)
        with (
            patch.object(doctor, "CATALOG", [check]),
            patch.object(doctor, "DoctorContext", return_value=ctx),
        ):
            assert main(["--doctor"]) == 0
        captured = capsys.readouterr()
        serialized = json.dumps(
            dataclasses.asdict(result), default=lambda item: item.name
        )
        for output in (serialized, captured.out + captured.err):
            assert _FAKE_BWS_TOKEN not in output
            assert str(len(_FAKE_BWS_TOKEN)) not in output
            assert "characters" not in output


# ---------------------------------------------------------------------------
# GITIGNORE_SYMPHONY
# ---------------------------------------------------------------------------


class TestGitignoreSymphony:
    """.symphony/ is gitignored in the target repo (CRITICAL)."""

    def test_fails_when_gitignore_missing(self, tmp_path: Path) -> None:
        """No .gitignore file at all FAILs."""
        check = _get_check("GITIGNORE_SYMPHONY")
        result = check(_make_ctx(project_root=str(tmp_path)))
        assert result.status == CheckStatus.FAIL

    def test_fails_when_symphony_line_absent(self, tmp_path: Path) -> None:
        """.gitignore present but missing the .symphony/ line FAILs."""
        check = _get_check("GITIGNORE_SYMPHONY")
        (tmp_path / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
        result = check(_make_ctx(project_root=str(tmp_path)))
        assert result.status == CheckStatus.FAIL

    def test_passes_when_symphony_line_present(self, tmp_path: Path) -> None:
        """.gitignore containing the exact .symphony/ line PASSes."""
        check = _get_check("GITIGNORE_SYMPHONY")
        (tmp_path / ".gitignore").write_text(
            ".symphony/\n*.pyc\n", encoding="utf-8"
        )
        result = check(_make_ctx(project_root=str(tmp_path)))
        assert result.status == CheckStatus.PASS
        assert result.severity == Severity.CRITICAL


# ---------------------------------------------------------------------------
# CRED_ANTHROPIC_UNSET (daemon_native=True)
# ---------------------------------------------------------------------------


class TestCredAnthropicUnset:
    """ANTHROPIC_API_KEY NOT set.

    CRITICAL, daemon_native=True, mirrors existing G3b.
    """

    def test_passes_when_unset(self) -> None:
        """No ANTHROPIC_API_KEY in env PASSes."""
        check = _get_check("CRED_ANTHROPIC_UNSET")
        result = check(_make_ctx(env={}))
        assert result.status == CheckStatus.PASS

    def test_fails_when_set_and_never_leaks_the_value(self) -> None:
        """A set ANTHROPIC_API_KEY FAILs; the value is never leaked."""
        check = _get_check("CRED_ANTHROPIC_UNSET")
        result = check(
            _make_ctx(env={"ANTHROPIC_API_KEY": _FAKE_ANTHROPIC_KEY})
        )
        assert result.status == CheckStatus.FAIL
        _assert_no_secret_leak(result, _FAKE_ANTHROPIC_KEY)


# ---------------------------------------------------------------------------
# FORCE_PR_TRIPWIRE (daemon_native=True)
# ---------------------------------------------------------------------------


class TestForcePrTripwire:
    """force-pr-not-merge hook self-test passes (CRITICAL, daemon_native).

    Wraps the existing ``cli.py`` self-test
    (``cli._assert_force_pr_not_merge_tripwire()``, a no-arg callable
    that raises on failure -- see ``tests/chain/test_cli.py::
    TestForcePrNotMergeStartupSelfTest``). Routed through run_report
    (with CATALOG patched to isolate this one check) rather than called
    directly, since it is implementation-defined whether the Check
    catches the tripwire's exception itself or relies on the runner's
    catch-all (both satisfy the observable contract).
    """

    def test_passes_when_cli_self_test_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A clean tripwire self-test PASSes."""
        check = _get_check("FORCE_PR_TRIPWIRE")
        monkeypatch.setattr(doctor, "CATALOG", [check])
        with patch(
            "baton_harness.chain.cli._assert_force_pr_not_merge_tripwire",
            return_value=None,
        ):
            results = doctor.run_report(_make_ctx())
        assert len(results) == 1
        assert results[0].check_id == "FORCE_PR_TRIPWIRE"
        assert results[0].status == CheckStatus.PASS

    def test_fails_when_cli_self_test_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A raising tripwire self-test surfaces as a FAIL, not a crash."""
        check = _get_check("FORCE_PR_TRIPWIRE")
        monkeypatch.setattr(doctor, "CATALOG", [check])
        with patch(
            "baton_harness.chain.cli._assert_force_pr_not_merge_tripwire",
            side_effect=RuntimeError("hook parser drifted"),
        ):
            results = doctor.run_report(_make_ctx())
        assert len(results) == 1
        assert results[0].status == CheckStatus.FAIL
        assert results[0].severity == Severity.CRITICAL


# ---------------------------------------------------------------------------
# GIT_CRED_HELPER (G3d) -- see module docstring "Design notes" for why
# this is included despite being absent from the router's enumerated
# Phase-1 check list. Kept in its own clearly-labeled block so it can be
# dropped cheaply if that omission turns out to have been deliberate.
# ---------------------------------------------------------------------------


class TestGitCredHelper:
    """git credential helper configured for github.com push (G3d, #219).

    CRITICAL, phase A, daemon_native=True (native reconcile.py G3d stays
    the sole executor in the daemon path; this Check exists for the
    standalone run_report). Mirrors reconcile.py's
    ``_get_git_credential_helpers`` probe: a scoped
    (``credential.https://github.com.helper``) git config lookup with a
    global (``credential.helper``) fallback when the scoped key is
    absent -- but via the injected ``ctx.runner`` seam rather than a
    hardcoded ``subprocess.run`` call, since doctor.py's checks must be
    unit-testable through DoctorContext.
    """

    def test_passes_when_scoped_helper_configured(self) -> None:
        """A configured scoped helper PASSes without a fallback call."""
        check = _get_check("GIT_CRED_HELPER")
        calls: list[list[str]] = []

        def _runner(
            args: list[str],
        ) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="!'/usr/bin/gh' auth git-credential\n",
                stderr="",
            )

        result = check(_make_ctx(runner=_runner))

        assert result.status == CheckStatus.PASS
        assert len(calls) == 1, (
            "must not make a fallback (global) probe call when the "
            f"scoped helper is already present; calls={calls!r}"
        )

    def test_falls_back_to_global_helper_when_scoped_absent(self) -> None:
        """Scoped key absent -> falls back to the global helper key."""
        check = _get_check("GIT_CRED_HELPER")

        def _runner(
            args: list[str],
        ) -> subprocess.CompletedProcess[str]:
            if "credential.https://github.com.helper" in args:
                return subprocess.CompletedProcess(
                    args=args, returncode=1, stdout="", stderr=""
                )
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="manager\n", stderr=""
            )

        result = check(_make_ctx(runner=_runner))

        assert result.status == CheckStatus.PASS

    def test_fails_when_no_helper_configured_and_names_the_fix(
        self,
    ) -> None:
        """Neither scoped nor global helper configured FAILs.

        The fix text must name the remediation (``gh auth setup-git``),
        mirroring reconcile.py's G3d alert text.
        """
        check = _get_check("GIT_CRED_HELPER")

        def _runner(
            args: list[str],
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr=""
            )

        result = check(_make_ctx(runner=_runner))

        assert result.status == CheckStatus.FAIL
        assert result.severity == Severity.CRITICAL
        assert "gh auth setup-git" in result.remediation, (
            "remediation text must name the command; got "
            f"{result.remediation!r}"
        )

    def test_does_not_crash_when_git_binary_is_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing git binary (runner raises) degrades to FAIL, no crash."""
        check = _get_check("GIT_CRED_HELPER")
        monkeypatch.setattr(doctor, "CATALOG", [check])

        def _raising_runner(
            args: list[str],
        ) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("git not found")

        results = doctor.run_report(_make_ctx(runner=_raising_runner))

        assert len(results) == 1
        assert results[0].status == CheckStatus.FAIL
        assert results[0].check_id == "GIT_CRED_HELPER"

    def test_never_asserts_on_credential_value(self) -> None:
        """The probe checks presence/shape only -- never the helper value.

        Any non-empty configured helper name satisfies the check; the
        value itself must never appear negated or validated against a
        known-good list (secret/config discipline).
        """
        check = _get_check("GIT_CRED_HELPER")

        def _runner(
            args: list[str],
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="some-totally-unrecognized-helper-program\n",
                stderr="",
            )

        result = check(_make_ctx(runner=_runner))

        assert result.status == CheckStatus.PASS, (
            "any non-empty configured helper name must satisfy the "
            "check -- it must not validate against a known-program list"
        )


# ---------------------------------------------------------------------------
# Live phase: credential-bearing checks and the live gate.
#
# check_id set added this phase: RULESET_MAIN, RULESET_FEATURE,
# LABELS_PRESENT, GH_REPO_ADMIN, GH_AUTH, CRED_OAUTH_VOLUME.
#
# Design notes / ambiguities resolved by this file (flagged prominently
# per-test rather than folded into a shared matrix, so a wrong guess is a
# one-line router fix, not a parametrized-block break):
#
# - RULESET_MAIN / RULESET_FEATURE both read their verdict from the SAME
#   ``ruleset_status.ruleset_is_provisioned(...)`` call (plan section 6:
#   "same call evaluates both rulesets together"), patched at its own
#   defining module (mirrors the FORCE_PR_TRIPWIRE precedent above).
#   ERROR's mapping to FAIL is the one soft/judgment call in that trio
#   (fail-closed default; isolated in its own test).
# - LABELS_PRESENT / GH_REPO_ADMIN model the shell references at
#   bin/run-daemon.sh:177-213 and bin/provision-ruleset.sh:186-202
#   respectively, via ``ctx.runner``. Exact argv is deliberately NOT
#   pinned -- the fake runners branch on the args actually passed so a
#   correct implementation choosing either ``--json name`` or
#   ``--jq '.[].name'`` output shape is satisfiable.
# - CRED_OAUTH_VOLUME's static ``.severity`` is pinned to WARNING here,
#   isolated in its own test: the plan table cell reads "CRITICAL
#   (daemon) / WARN (standalone dev-box)", but daemon_native=True means
#   run_gate filters this Check out unconditionally in BOTH phases (per
#   the Rev-3 daemon_native note already pinned earlier in this file) --
#   so the Check's own severity attribute is only ever consulted by the
#   standalone run_report/--strict path, where only the dev-box ("WARN")
#   reading can apply. Flagged in the return summary.
# ---------------------------------------------------------------------------

_PHASE_4_ENV = {
    "BH_REPO_OWNER": "my-org",
    "BH_REPO_NAME": "my-sandbox",
}

_EXPECTED_PHASE_4_CHECK_IDS = {
    "RULESET_MAIN",
    "RULESET_FEATURE",
    "LABELS_PRESENT",
    "GH_REPO_ADMIN",
    "GH_AUTH",
    "CRED_OAUTH_VOLUME",
}


def test_catalog_contains_all_phase_4_checks() -> None:
    """CATALOG contains (at least) every Phase-4 check_id."""
    catalog_ids = {c.check_id for c in doctor.CATALOG}
    missing = _EXPECTED_PHASE_4_CHECK_IDS - catalog_ids
    assert not missing, f"CATALOG is missing Phase-4 check ids: {missing!r}"


@pytest.mark.parametrize(
    "check_id, severity, daemon_native",
    [
        ("RULESET_MAIN", Severity.CRITICAL, False),
        ("RULESET_FEATURE", Severity.CRITICAL, False),
        ("LABELS_PRESENT", Severity.CRITICAL, False),
        ("GH_REPO_ADMIN", Severity.WARNING, False),
        ("GH_AUTH", Severity.CRITICAL, True),
    ],
)
def test_phase_4_check_metadata_matches_the_plan_catalog(
    check_id: str,
    severity: Severity,
    daemon_native: bool,
) -> None:
    """Each Phase-4 check's severity/phase/daemon_native matches section 6.

    CRED_OAUTH_VOLUME is deliberately excluded from this shared matrix --
    see its own isolated tests in ``TestCredOauthVolume`` for why its
    severity is a judgment call rather than a certain reading.
    """
    check = _get_check(check_id)
    assert check.severity == severity
    assert check.phase is Phase.LIVE
    assert check.daemon_native is daemon_native


def test_daemon_native_set_after_phase_4() -> None:
    """The full daemon_native=True set matches section 3's Rev-3 note.

    daemon_native = {GH_AUTH, CRED_ANTHROPIC_UNSET, CRED_OAUTH_VOLUME,
    GIT_CRED_HELPER, FORCE_PR_TRIPWIRE} -- exactly these five, no more,
    no fewer, once Phase 4 lands.
    """
    expected = {
        "GH_AUTH",
        "CRED_ANTHROPIC_UNSET",
        "CRED_OAUTH_VOLUME",
        "GIT_CRED_HELPER",
    }
    actual = {c.check_id for c in doctor.CATALOG if c.daemon_native}
    assert actual == expected, (
        "daemon_native=True check set drifted from the plan; expected "
        f"{expected!r}, got {actual!r}"
    )


# ---------------------------------------------------------------------------
# RULESET_MAIN / RULESET_FEATURE
# ---------------------------------------------------------------------------


class TestRulesetChecks:
    """RULESET_MAIN / RULESET_FEATURE -- both call ruleset_is_provisioned.

    Per plan section 6, "same call evaluates both rulesets together":
    RULESET_MAIN and RULESET_FEATURE both read their PASS/FAIL from the
    SAME ``ruleset_is_provisioned()`` verdict (it classifies both
    rulesets in one round-trip), so both checks are exercised
    identically here via shared parametrization.

    Patch-target note: patched at ``ruleset_status``'s own defining
    module (``baton_harness.chain.ruleset_status.ruleset_is_provisioned``),
    mirroring the ``FORCE_PR_TRIPWIRE`` precedent above (a Check calling
    a dotted-imported function is patched at that function's *source*
    module, not a re-exported name on ``doctor.py``). If the
    implementation instead does ``from baton_harness.chain.ruleset_status
    import ruleset_is_provisioned`` (a bare name) inside ``doctor.py``,
    this patch target will need to move to
    ``baton_harness.chain.doctor.ruleset_is_provisioned`` -- flagged in
    the return summary.

    Every test supplies BOTH ``ctx.env`` (``BH_REPO_OWNER``/
    ``BH_REPO_NAME``/``BH_GITHUB_APP_ID``) and an equivalent
    ``.bh/config.env`` file so the test is satisfiable regardless of
    which source the implementation reads owner/repo/app_id from -- the
    plan does not specify this internal detail.
    """

    @pytest.mark.parametrize("check_id", ["RULESET_MAIN", "RULESET_FEATURE"])
    def test_passes_on_match(self, check_id: str, tmp_path: Path) -> None:
        """MATCH -> PASS for both checks."""
        check = _get_check(check_id)
        _write_config_env(tmp_path, _VALID_CONFIG_ENV)

        with patch(
            "baton_harness.chain.ruleset_status.ruleset_is_provisioned",
            return_value=RulesetStatus.MATCH,
        ):
            result = check(
                _make_ctx(
                    project_root=str(tmp_path),
                    env={**_PHASE_4_ENV, "BH_GITHUB_APP_ID": "12345"},
                )
            )

        assert result.status == CheckStatus.PASS
        assert result.severity == Severity.CRITICAL

    @pytest.mark.parametrize(
        "check_id, status",
        [
            ("RULESET_MAIN", RulesetStatus.DRIFT),
            ("RULESET_MAIN", RulesetStatus.ABSENT),
            ("RULESET_FEATURE", RulesetStatus.DRIFT),
            ("RULESET_FEATURE", RulesetStatus.ABSENT),
        ],
    )
    def test_fails_on_drift_or_absent(
        self,
        check_id: str,
        status: RulesetStatus,
        tmp_path: Path,
    ) -> None:
        """DRIFT and ABSENT both FAIL (CRITICAL, blocks daemon startup)."""
        check = _get_check(check_id)
        _write_config_env(tmp_path, _VALID_CONFIG_ENV)

        with patch(
            "baton_harness.chain.ruleset_status.ruleset_is_provisioned",
            return_value=status,
        ):
            result = check(
                _make_ctx(
                    project_root=str(tmp_path),
                    env={**_PHASE_4_ENV, "BH_GITHUB_APP_ID": "12345"},
                )
            )

        assert result.status == CheckStatus.FAIL
        assert result.severity == Severity.CRITICAL
        assert result.detail

    @pytest.mark.parametrize("check_id", ["RULESET_MAIN", "RULESET_FEATURE"])
    def test_fails_closed_on_error(
        self, check_id: str, tmp_path: Path
    ) -> None:
        """ERROR (gh call failed) fails closed -> FAIL, never silently PASS.

        This is the soft/judgment-call mapping in this class: ERROR means
        "could not verify" rather than a confirmed drift/absence, but a
        CRITICAL readiness gate that cannot confirm the ruleset is safe
        must not treat "unknown" as "PASS" -- fail-closed is the only
        defensible default absent an explicit SKIP/inconclusive contract
        in the plan. Flagged in the return summary.
        """
        check = _get_check(check_id)
        _write_config_env(tmp_path, _VALID_CONFIG_ENV)

        with patch(
            "baton_harness.chain.ruleset_status.ruleset_is_provisioned",
            return_value=RulesetStatus.ERROR,
        ):
            result = check(
                _make_ctx(
                    project_root=str(tmp_path),
                    env={**_PHASE_4_ENV, "BH_GITHUB_APP_ID": "12345"},
                )
            )

        assert result.status == CheckStatus.FAIL


# ---------------------------------------------------------------------------
# LABELS_PRESENT
# ---------------------------------------------------------------------------

_REQUIRED_LABELS = {
    "agent-ready",
    "agent-done",
    "agent-failed",
    "blocked",
    "agent-in-progress",
    "agent-merged",
}


def _fake_gh_label_runner(
    present: set[str],
) -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
    """Build a fake ``ctx.runner`` standing in for ``gh label list``.

    Branches on whether ``--jq`` appears in argv (mirrors
    ``bin/run-daemon.sh:192``'s ``--jq '.[].name'`` newline-of-bare-names
    output) versus a plain ``--json name`` JSON-array shape, so the test
    is satisfiable regardless of which output format the implementation
    chooses to parse.

    Args:
        present: The set of label names the fake repo currently has.

    Returns:
        A callable matching the ``ctx.runner`` seam contract.
    """

    def _runner(args: list[str]) -> subprocess.CompletedProcess[str]:
        uses_jq = any("jq" in a for a in args)
        if uses_jq:
            stdout = "".join(f"{name}\n" for name in sorted(present))
        else:
            stdout = json.dumps([{"name": n} for n in sorted(present)])
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=stdout, stderr=""
        )

    return _runner


class TestLabelsPresent:
    """Six required harness labels exist in the target repo (CRITICAL).

    Modeled on ``bin/run-daemon.sh:177-213``'s
    ``gh label list -R <slug> --json name --jq '.[].name'`` preflight.
    """

    def test_passes_when_all_six_labels_present(
        self, tmp_path: Path
    ) -> None:
        """All six required labels present in the target repo PASSes."""
        check = _get_check("LABELS_PRESENT")
        runner = _fake_gh_label_runner(_REQUIRED_LABELS)
        _write_config_env(tmp_path, _VALID_CONFIG_ENV)

        result = check(
            _make_ctx(project_root=str(tmp_path), runner=runner)
        )

        assert result.status == CheckStatus.PASS
        assert result.severity == Severity.CRITICAL

    def test_fails_and_names_each_missing_label(
        self, tmp_path: Path
    ) -> None:
        """Missing labels FAIL and are named individually in the detail."""
        check = _get_check("LABELS_PRESENT")
        present = _REQUIRED_LABELS - {
            "blocked",
            "agent-failed",
            "agent-merged",
        }
        runner = _fake_gh_label_runner(present)
        _write_config_env(tmp_path, _VALID_CONFIG_ENV)

        result = check(
            _make_ctx(project_root=str(tmp_path), runner=runner)
        )

        assert result.status == CheckStatus.FAIL
        assert result.severity == Severity.CRITICAL
        assert "blocked" in result.detail
        assert "agent-failed" in result.detail
        assert "agent-merged" in result.detail

    def test_init_sandbox_provisions_agent_failed_in_deep_red(self) -> None:
        """Sandbox initialization creates the terminal failure label."""
        script = (
            Path(__file__).resolve().parents[2] / "bin/init-sandbox.sh"
        ).read_text(encoding="utf-8")
        assert '_create_label "agent-failed"      "b60205"' in script

    def test_fails_when_gh_cli_call_errors(self, tmp_path: Path) -> None:
        """A ``gh`` CLI failure (non-zero exit) FAILs, never crashes."""
        check = _get_check("LABELS_PRESENT")

        def _erroring_runner(
            args: list[str],
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="not found"
            )

        _write_config_env(tmp_path, _VALID_CONFIG_ENV)
        result = check(
            _make_ctx(
                project_root=str(tmp_path), runner=_erroring_runner
            )
        )

        assert result.status == CheckStatus.FAIL


# ---------------------------------------------------------------------------
# GH_REPO_ADMIN
# ---------------------------------------------------------------------------


class TestGhRepoAdmin:
    """actor/repo has an admin collaborator (WARNING, informational).

    Modeled on ``bin/provision-ruleset.sh:186-202``'s
    ``gh api repos/<slug>/collaborators?permission=admin`` preflight
    (counts entries with ``role_name=="admin"`` or ``permissions.admin``).
    """

    def test_passes_when_an_admin_collaborator_exists(
        self, tmp_path: Path
    ) -> None:
        """At least one admin collaborator PASSes."""
        check = _get_check("GH_REPO_ADMIN")

        def _runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            body = json.dumps([{"login": "someone", "role_name": "admin"}])
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=body, stderr=""
            )

        _write_config_env(tmp_path, _VALID_CONFIG_ENV)
        result = check(
            _make_ctx(project_root=str(tmp_path), runner=_runner)
        )

        assert result.status == CheckStatus.PASS
        assert result.severity == Severity.WARNING

    def test_warns_when_no_admin_collaborator_found(
        self, tmp_path: Path
    ) -> None:
        """No admin collaborator found WARNs (informational, non-fatal)."""
        check = _get_check("GH_REPO_ADMIN")

        def _runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="[]", stderr=""
            )

        _write_config_env(tmp_path, _VALID_CONFIG_ENV)
        result = check(
            _make_ctx(project_root=str(tmp_path), runner=_runner)
        )

        assert result.status == CheckStatus.WARN
        assert result.severity == Severity.WARNING

    def test_never_fails_when_gh_api_call_errors(
        self, tmp_path: Path
    ) -> None:
        """A ``gh api`` failure degrades to WARN, never CRITICAL FAIL.

        GH_REPO_ADMIN is WARNING-severity and purely informational (D6)
        -- an inability to check admin status must not block startup.
        """
        check = _get_check("GH_REPO_ADMIN")

        def _erroring_runner(
            args: list[str],
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="error"
            )

        _write_config_env(tmp_path, _VALID_CONFIG_ENV)
        result = check(
            _make_ctx(
                project_root=str(tmp_path), runner=_erroring_runner
            )
        )

        assert result.status != CheckStatus.FAIL


# ---------------------------------------------------------------------------
# GH_AUTH (daemon_native=True)
# ---------------------------------------------------------------------------


class TestGhAuth:
    """gh token valid (CRITICAL, daemon_native=True).

    Standalone reports and daemon live gates execute ``gh auth status``
    through ``ctx.runner``. Native installation-token validation remains
    a separate startup check.
    """

    def test_passes_when_gh_auth_status_succeeds(self) -> None:
        """A successful gh-auth-status probe (returncode 0) PASSes."""
        check = _get_check("GH_AUTH")

        def _runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="",
                stderr="Logged in to github.com account someone",
            )

        result = check(_make_ctx(runner=_runner))

        assert result.status == CheckStatus.PASS
        assert result.severity == Severity.CRITICAL

    def test_fails_when_gh_auth_status_fails(self) -> None:
        """A failing gh-auth-status probe (non-zero returncode) FAILs."""
        check = _get_check("GH_AUTH")

        def _runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=1,
                stdout="",
                stderr="You are not logged into any GitHub hosts",
            )

        result = check(_make_ctx(runner=_runner))

        assert result.status == CheckStatus.FAIL
        assert result.severity == Severity.CRITICAL

    def test_is_daemon_native(self) -> None:
        """GH_AUTH retains native metadata and belongs to live."""
        check = _get_check("GH_AUTH")
        assert check.daemon_native is True
        assert check.phase is Phase.LIVE


# ---------------------------------------------------------------------------
# CRED_OAUTH_VOLUME (daemon_native=True)
# ---------------------------------------------------------------------------


class TestCredOauthVolume:
    """~/.claude/.credentials.json present + readable (daemon_native=True).

    Mirrors native G3c (reconcile.py:230-255): presence + readability via
    ``open()`` only -- contents are never read, decoded, or logged.
    """

    def test_passes_when_credentials_file_present_and_readable(
        self, tmp_path: Path
    ) -> None:
        """Present + readable credentials file PASSes."""
        check = _get_check("CRED_OAUTH_VOLUME")
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / ".credentials.json").write_text("{}", encoding="utf-8")

        result = check(_make_ctx(home_dir=str(tmp_path)))

        assert result.status == CheckStatus.PASS

    def test_warns_when_credentials_file_absent(self, tmp_path: Path) -> None:
        """Absent credential file WARNs (WARNING severity -> WARN status).

        Mirrors this file's established severity/status convention
        (e.g. ``CLI_UV``): a WARNING-severity check reports ``WARN`` on
        failure, not ``FAIL``.
        """
        check = _get_check("CRED_OAUTH_VOLUME")

        result = check(_make_ctx(home_dir=str(tmp_path)))

        assert result.status == CheckStatus.WARN
        assert result.severity == Severity.WARNING

    def test_never_reads_or_leaks_credential_contents(
        self, tmp_path: Path
    ) -> None:
        """Structural-only: presence + readability, never content."""
        check = _get_check("CRED_OAUTH_VOLUME")
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        secret_marker = "sk-ant-totally-fake-oauth-marker-9f8e7d"
        (claude_dir / ".credentials.json").write_text(
            secret_marker, encoding="utf-8"
        )

        result = check(_make_ctx(home_dir=str(tmp_path)))

        assert result.status == CheckStatus.PASS
        _assert_no_secret_leak(result, secret_marker)

    def test_is_daemon_native(self) -> None:
        """CRED_OAUTH_VOLUME retains native metadata and belongs to live."""
        check = _get_check("CRED_OAUTH_VOLUME")
        assert check.daemon_native is True
        assert check.phase is Phase.LIVE

    def test_static_severity_is_warning_for_standalone_dev_box_reporting(
        self,
    ) -> None:
        """OAuth availability is warning-severity in the shared catalog."""
        check = _get_check("CRED_OAUTH_VOLUME")
        assert check.severity == Severity.WARNING


# ---------------------------------------------------------------------------
# Opt-in App key probe (#193, updated by #359).
# VAULT_PEM_DRYRUN belongs to the live catalog and retains its stable ID.
# The selected provider loads key material and local JWT signing proves
# usability. Transport and signing failures must be sanitized before the
# generic check wrapper can render them. No GitHub call is permitted.
# ---------------------------------------------------------------------------

_FAKE_PEM_VALUE = "fake-vault-secret-value-9f8e7d6c5b4a"

_VAULT_SECRET_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _make_vault_ctx(
    project_root: Path,
    fetch_secret: Callable[..., str],
) -> DoctorContext:
    """Build a ``DoctorContext`` wired for the VAULT_PEM_DRYRUN check.

    Writes ``BWS_PEM_SECRET_ID`` into ``.bh/config.env`` and sets only
    ``BWS_ACCESS_TOKEN`` on ``ctx.env``, pinning the config file as the
    sole source of the secret ID.

    Args:
        project_root: Directory to write ``.bh/config.env`` under.
        fetch_secret: Fake ``fetch_secret`` seam to install.

    Returns:
        A fully-populated ``DoctorContext``.
    """
    _write_config_env(project_root, _VALID_CONFIG_ENV)
    return _make_ctx(
        project_root=str(project_root),
        env={
            "BWS_ACCESS_TOKEN": _FAKE_BWS_TOKEN,
        },
        fetch_secret=fetch_secret,
    )


class TestVaultPemDryrun:
    """Opt-in ``--check-vault`` live bws PEM dry-run (VAULT_PEM_DRYRUN)."""

    def test_is_included_in_the_live_catalog(self) -> None:
        """VAULT_PEM_DRYRUN is selected through the unified live catalog."""
        catalog_ids = {c.check_id for c in doctor.CATALOG}
        assert "VAULT_PEM_DRYRUN" in catalog_ids

    def test_check_exposes_required_static_metadata(self) -> None:
        """The standalone Check has the same metadata shape as CATALOG rows."""
        check = doctor.VAULT_PEM_DRYRUN_CHECK
        assert check.check_id == "VAULT_PEM_DRYRUN"
        assert isinstance(check.title, str) and check.title
        assert isinstance(check.severity, Severity)
        assert check.phase is Phase.LIVE
        assert check.daemon_native is False
        assert isinstance(check.fix, str) and check.fix

    def test_passes_when_secret_fetch_succeeds_and_can_sign(
        self, tmp_path: Path, app_private_key_pem: str
    ) -> None:
        """A fetched signing key PASSes after local JWT signing (#359)."""

        def _fake_fetch_secret(*args: object, **kwargs: object) -> str:
            assert args == (_VAULT_SECRET_ID,)
            assert kwargs == {"access_token": _FAKE_BWS_TOKEN}
            return app_private_key_pem

        ctx = _make_vault_ctx(tmp_path, _fake_fetch_secret)

        result = doctor.VAULT_PEM_DRYRUN_CHECK(ctx)

        assert result.check_id == "VAULT_PEM_DRYRUN"
        assert result.status == CheckStatus.PASS

    def test_never_leaks_the_fetched_secret_value(
        self, tmp_path: Path
    ) -> None:
        """The fetched PEM value never appears in any CheckResult field."""

        def _fake_fetch_secret(*args: object, **kwargs: object) -> str:
            assert args == (_VAULT_SECRET_ID,)
            assert kwargs == {"access_token": _FAKE_BWS_TOKEN}
            return _FAKE_PEM_VALUE

        ctx = _make_vault_ctx(tmp_path, _fake_fetch_secret)

        result = doctor.VAULT_PEM_DRYRUN_CHECK(ctx)

        _assert_no_secret_leak(result, _FAKE_PEM_VALUE)

    def test_fails_when_fetched_secret_is_empty(self, tmp_path: Path) -> None:
        """An empty fetched value FAILs (section 11: non-empty check)."""

        def _fake_fetch_secret(*args: object, **kwargs: object) -> str:
            assert args == (_VAULT_SECRET_ID,)
            assert kwargs == {"access_token": _FAKE_BWS_TOKEN}
            return ""

        ctx = _make_vault_ctx(tmp_path, _fake_fetch_secret)

        result = doctor.VAULT_PEM_DRYRUN_CHECK(ctx)

        assert result.status == CheckStatus.FAIL

    def test_fetch_failure_is_reported_as_secret_safe_provider_failure(
        self, tmp_path: Path
    ) -> None:
        """A raising fetch becomes a provider failure without raw text."""

        def _raising_fetch_secret(*args: object, **kwargs: object) -> str:
            raise RuntimeError("bws exited non-zero")

        ctx = _make_vault_ctx(tmp_path, _raising_fetch_secret)
        check = doctor.VAULT_PEM_DRYRUN_CHECK

        result = doctor._run_check(check, ctx)

        assert result.status == CheckStatus.FAIL
        assert result.severity == check.severity
        assert "bws provider" in result.detail
        assert "bws exited non-zero" not in result.detail
        assert result.remediation == check.fix
