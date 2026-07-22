"""Tests for the unified preflight "doctor" catalog (issue #193, Phase 1).

Covers the module surface described in the ratified plan
(``docs/superpowers/plans/2026-07-01-preflight-doctor-193.md``, section 3,
6, and the Phase 1 bullet under section 15):

- ``Severity`` (``CRITICAL``/``WARNING``), ``CheckStatus``
  (``PASS``/``FAIL``/``WARN``/``SKIP``), ``Phase``
  (``PRE_BOOTSTRAP``/``POST_BOOTSTRAP``) enums.
- ``CheckResult`` dataclass shape.
- ``DoctorContext`` -- the injected-seam bundle (mirrors the
  ``runner=``/``run=``/``fetch_secret=`` style already used by
  ``sandbox_config.py`` and ``daemon.py``).
- The exception contract (BLOCKING #1, section 3/14): a raising ``Check``
  is caught by both runners and synthesized into
  ``CheckResult(status=FAIL, severity=check.severity, detail=repr(exc),
  fix=check.fix)`` -- never a bare traceback out of ``run_gate``.
- The ``daemon_native`` filter (BLOCKING #2 / section 3 / section 6):
  ``run_gate`` excludes every ``daemon_native=True`` check in **both**
  phases; ``run_report`` includes them unconditionally.
- Every Phase-1 ``Check`` in ``CATALOG``: ``CLI_GH``, ``CLI_BWS``,
  ``CLI_CLAUDE``, ``CLI_UV``, ``ENV_PROJECT_ROOT``, ``ENV_HOST_ENV``,
  ``CFG_CONFIG_ENV``, ``CFG_REQUIRED_KEYS``, ``CFG_OPTIONAL_SECRET_IDS``,
  ``ENV_BWS_ACCESS_TOKEN``, ``GITIGNORE_SYMPHONY``,
  ``CRED_ANTHROPIC_UNSET``, ``FORCE_PR_TRIPWIRE``, ``GIT_CRED_HELPER``.

Design notes / contract choices made by this test file (no implementation
existed to consult, so these are this file's own decisions -- see the
return summary's "Gaps / assumptions" section for the full list):

- ``Phase`` is introduced as an ``Enum`` with members ``PRE_BOOTSTRAP``
  and ``POST_BOOTSTRAP`` -- the plan names the two phases but never
  names an enum class for them.
- Each ``Check`` callable must expose ``check_id``, ``title``,
  ``severity``, ``phase``, ``daemon_native``, and ``fix`` as plain
  attributes (not just be callable) -- required so the exception
  contract's ``severity=check.severity, fix=check.fix`` synthesis is
  possible, and so tests can introspect/filter ``CATALOG`` without
  invoking every check.
- ``DoctorContext`` carries ``project_root`` and ``home_dir`` as plain
  resolved string fields (not callables) -- filesystem-backed checks
  operate against real temporary directories/files (mirrors the
  ``tmp_path``-based style already used in ``test_sandbox_config.py``),
  rather than an injected path-existence callable.
- ``CFG_REQUIRED_KEYS``/``CFG_OPTIONAL_SECRET_IDS`` read
  ``{project_root}/.bh/config.env`` directly and validate shape only
  (reusing ``sandbox_config``'s validation *rules*, per the briefing) --
  they must NOT make the ``gh api`` network call that
  ``sandbox_config.read_and_validate`` makes, since Phase A
  (PRE_BOOTSTRAP) is explicitly "no network/auth needed" (plan section
  4).
- ``GIT_CRED_HELPER`` (G3d) IS included in this Phase-1 file even though
  the router's enumerated Phase-1 check list omitted it: the plan's own
  Phase-1 bullet under section 15 (which the task explicitly pointed at
  as authoritative) lists it explicitly, alongside the section 6 catalog
  row. Flagged prominently in the return summary and kept in its own
  clearly-labeled block (``TestGitCredHelper``) so the router can drop
  it cheaply if the omission was in fact a deliberate re-scope.

All seams are injected (no real subprocess, filesystem-outside-tmp_path,
or network I/O). No pytest-asyncio (doctor.py is synchronous).
"""

from __future__ import annotations

import dataclasses
import subprocess
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from baton_harness.chain import doctor
from baton_harness.chain.doctor import (
    CheckResult,
    CheckStatus,
    DoctorContext,
    Phase,
    Severity,
)

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
    phase: Phase = Phase.PRE_BOOTSTRAP,
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
                title=title,
                severity=severity,
                status=CheckStatus.PASS,
                detail="synthetic pass",
                fix=fix,
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
    for field_name in ("check_id", "title", "detail", "fix"):
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
    BWS_PEM_SECRET_ID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee
    """
)

_FAKE_BWS_TOKEN = "not-a-real-secret-VALUE-9f8e7d6c5b4a"  # placeholder
_FAKE_ANTHROPIC_KEY = "sk-fake-not-a-real-key-12345"  # placeholder


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


def test_severity_enum_members() -> None:
    """Severity has exactly CRITICAL and WARNING members."""
    assert {m.name for m in Severity} == {"CRITICAL", "WARNING"}


def test_check_status_enum_members() -> None:
    """CheckStatus has exactly PASS/FAIL/WARN/SKIP members."""
    assert {m.name for m in CheckStatus} == {"PASS", "FAIL", "WARN", "SKIP"}


def test_phase_enum_members() -> None:
    """Phase has exactly PRE_BOOTSTRAP and POST_BOOTSTRAP members."""
    assert {m.name for m in Phase} == {"PRE_BOOTSTRAP", "POST_BOOTSTRAP"}


# ---------------------------------------------------------------------------
# CheckResult dataclass
# ---------------------------------------------------------------------------


def test_check_result_carries_all_six_fields() -> None:
    """CheckResult stores check_id/title/severity/status/detail/fix."""
    result = CheckResult(
        check_id="X",
        title="a title",
        severity=Severity.CRITICAL,
        status=CheckStatus.PASS,
        detail="a detail",
        fix="a fix",
    )
    assert result.check_id == "X"
    assert result.title == "a title"
    assert result.severity == Severity.CRITICAL
    assert result.status == CheckStatus.PASS
    assert result.detail == "a detail"
    assert result.fix == "a fix"


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
            title="t",
            severity=Severity.WARNING,
            status=CheckStatus.PASS,
            detail="ok",
            fix="",
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
    assert failed.fix == "fix the boom"


def test_run_gate_raising_critical_check_exits_cleanly_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising CRITICAL check triggers a clean SystemExit(1) via run_gate.

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
        phase=Phase.PRE_BOOTSTRAP,
        daemon_native=False,
        fix="fix gate boom",
        fn=_boom,
    )
    monkeypatch.setattr(doctor, "CATALOG", [raising])

    with pytest.raises(SystemExit) as exc_info:
        doctor.run_gate(_make_ctx(), Phase.PRE_BOOTSTRAP)

    assert exc_info.value.code == 1


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
        phase=Phase.PRE_BOOTSTRAP,
        daemon_native=False,
        fix="fix warn boom",
        fn=_boom,
    )
    monkeypatch.setattr(doctor, "CATALOG", [raising])

    # Must not raise.
    result = doctor.run_gate(_make_ctx(), Phase.PRE_BOOTSTRAP)
    assert result is None


# ---------------------------------------------------------------------------
# run_gate runner semantics
# ---------------------------------------------------------------------------


def test_run_gate_short_circuits_on_first_critical_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_gate stops at the first CRITICAL FAIL; later checks don't run."""
    calls: list[str] = []

    def _pass(ctx: DoctorContext) -> CheckResult:
        calls.append("pass")
        return CheckResult(
            check_id="P",
            title="p",
            severity=Severity.WARNING,
            status=CheckStatus.PASS,
            detail="ok",
            fix="",
        )

    def _warn_fail(ctx: DoctorContext) -> CheckResult:
        calls.append("warn_fail")
        return CheckResult(
            check_id="W",
            title="w",
            severity=Severity.WARNING,
            status=CheckStatus.WARN,
            detail="meh",
            fix="fix w",
        )

    def _critical_fail(ctx: DoctorContext) -> CheckResult:
        calls.append("critical_fail")
        return CheckResult(
            check_id="C",
            title="c",
            severity=Severity.CRITICAL,
            status=CheckStatus.FAIL,
            detail="bad",
            fix="fix c",
        )

    def _never(ctx: DoctorContext) -> CheckResult:
        calls.append("never")
        return CheckResult(
            check_id="N",
            title="n",
            severity=Severity.CRITICAL,
            status=CheckStatus.PASS,
            detail="ok",
            fix="",
        )

    catalog = [
        _make_check("P", severity=Severity.WARNING, fn=_pass),
        _make_check("W", severity=Severity.WARNING, fn=_warn_fail),
        _make_check("C", severity=Severity.CRITICAL, fn=_critical_fail),
        _make_check("N", severity=Severity.CRITICAL, fn=_never),
    ]
    monkeypatch.setattr(doctor, "CATALOG", catalog)

    with pytest.raises(SystemExit) as exc_info:
        doctor.run_gate(_make_ctx(), Phase.PRE_BOOTSTRAP)

    assert exc_info.value.code == 1
    assert calls == ["pass", "warn_fail", "critical_fail"], (
        "run_gate must exit on the first CRITICAL FAIL and must not run "
        f"checks positioned after it; call order was {calls!r}"
    )


def test_run_gate_only_runs_checks_for_the_requested_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_gate(phase=X) ignores checks tagged with a different phase."""
    calls: list[str] = []

    def _phase_a(ctx: DoctorContext) -> CheckResult:
        calls.append("A")
        return CheckResult(
            check_id="A",
            title="a",
            severity=Severity.WARNING,
            status=CheckStatus.PASS,
            detail="ok",
            fix="",
        )

    def _phase_b(ctx: DoctorContext) -> CheckResult:
        calls.append("B")
        return CheckResult(
            check_id="B",
            title="b",
            severity=Severity.WARNING,
            status=CheckStatus.PASS,
            detail="ok",
            fix="",
        )

    catalog = [
        _make_check("A", phase=Phase.PRE_BOOTSTRAP, fn=_phase_a),
        _make_check("B", phase=Phase.POST_BOOTSTRAP, fn=_phase_b),
    ]
    monkeypatch.setattr(doctor, "CATALOG", catalog)

    doctor.run_gate(_make_ctx(), Phase.PRE_BOOTSTRAP)

    assert calls == ["A"], (
        "run_gate(PRE_BOOTSTRAP) must only run PRE_BOOTSTRAP-phase "
        f"checks; got {calls!r}"
    )


@pytest.mark.parametrize("phase", [Phase.PRE_BOOTSTRAP, Phase.POST_BOOTSTRAP])
def test_run_gate_skips_daemon_native_checks_in_every_phase(
    monkeypatch: pytest.MonkeyPatch,
    phase: Phase,
) -> None:
    """run_gate never executes a daemon_native=True check, in any phase.

    Rev 3 (BLOCKING #2 / section 3 / section 6 daemon_native note): the
    daemon path's native code (a reconcile.py G3 gate or the cli.py
    tripwire) is the SOLE executor for daemon_native checks in BOTH
    PRE_BOOTSTRAP and POST_BOOTSTRAP -- run_gate must filter them out
    regardless of which phase is requested, even though every Phase-1
    daemon_native check happens to be tagged phase=PRE_BOOTSTRAP.

    NOTE: this directly contradicts a parenthetical in this task's own
    briefing ("run_gate filters OUT daemon_native=True when
    phase=POST_BOOTSTRAP" -- implying PRE_BOOTSTRAP is NOT filtered).
    The plan's section 3 ("run_gate excludes every daemon_native=True
    check"), section 6 Rev-3 note ("run_gate skips these rows in BOTH
    phases"), and section 15 ("NOT in the Phase-3
    run_gate(PRE_BOOTSTRAP) execution set") are unanimous and are
    treated as authoritative here. Flagged in the return summary.
    """

    def _native_fail(ctx: DoctorContext) -> CheckResult:
        raise AssertionError(
            "a daemon_native check must never be invoked by run_gate"
        )

    def _normal_pass(ctx: DoctorContext) -> CheckResult:
        return CheckResult(
            check_id="NORMAL",
            title="n",
            severity=Severity.CRITICAL,
            status=CheckStatus.PASS,
            detail="ok",
            fix="",
        )

    catalog = [
        _make_check(
            "NATIVE",
            phase=phase,
            daemon_native=True,
            severity=Severity.CRITICAL,
            fn=_native_fail,
        ),
        _make_check(
            "NORMAL",
            phase=phase,
            daemon_native=False,
            fn=_normal_pass,
        ),
    ]
    monkeypatch.setattr(doctor, "CATALOG", catalog)

    # Must not raise (the daemon_native check's AssertionError proves it
    # was never invoked) and must not SystemExit (the only check
    # run_gate is allowed to execute here passes).
    doctor.run_gate(_make_ctx(), phase)


def test_run_report_includes_daemon_native_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_report (standalone) includes daemon_native checks always."""

    def _native(ctx: DoctorContext) -> CheckResult:
        return CheckResult(
            check_id="NATIVE",
            title="n",
            severity=Severity.CRITICAL,
            status=CheckStatus.FAIL,
            detail="native fail",
            fix="native fix",
        )

    catalog = [_make_check("NATIVE", daemon_native=True, fn=_native)]
    monkeypatch.setattr(doctor, "CATALOG", catalog)

    results = doctor.run_report(_make_ctx())

    assert len(results) == 1
    assert results[0].check_id == "NATIVE"
    assert results[0].status == CheckStatus.FAIL


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
    "FORCE_PR_TRIPWIRE": (Severity.CRITICAL, True),
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
    assert check.phase == Phase.PRE_BOOTSTRAP, (
        "every Phase-1 check is authored under phase A (PRE_BOOTSTRAP), "
        "per plan section 6/15"
    )


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
    check_id: str,
    binary: str,
    severity: Severity,
    fail_status: CheckStatus,
) -> None:
    """Each CLI on-PATH check PASSes when ``which`` resolves the binary."""
    check = _get_check(check_id)

    def _which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name == binary else None

    result = check(_make_ctx(which=_which))

    assert result.check_id == check_id
    assert result.status == CheckStatus.PASS
    assert result.severity == severity
    assert result.detail
    assert result.fix is not None


@pytest.mark.parametrize(
    "check_id, binary, severity, fail_status", _CLI_CHECKS
)
def test_cli_on_path_check_reports_failure_when_binary_missing(
    check_id: str,
    binary: str,
    severity: Severity,
    fail_status: CheckStatus,
) -> None:
    """A missing binary FAILs (CRITICAL checks) or WARNs (CLI_UV)."""
    check = _get_check(check_id)

    result = check(_make_ctx(which=lambda name: None))

    assert result.status == fail_status, (
        f"{check_id} on a missing binary must report status "
        f"{fail_status!r}; got {result.status!r}"
    )
    assert result.severity == severity
    assert binary in result.detail or binary in result.fix, (
        f"{check_id} must name the missing binary {binary!r} in its "
        f"detail or fix text; got detail={result.detail!r} "
        f"fix={result.fix!r}"
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


# ---------------------------------------------------------------------------
# ENV_BWS_ACCESS_TOKEN
# ---------------------------------------------------------------------------


class TestEnvBwsAccessToken:
    """BWS_ACCESS_TOKEN presence/shape only -- never the value (CRITICAL)."""

    def test_fails_when_unset(self) -> None:
        """No BWS_ACCESS_TOKEN in env FAILs."""
        check = _get_check("ENV_BWS_ACCESS_TOKEN")
        result = check(_make_ctx(env={}))
        assert result.status == CheckStatus.FAIL
        assert result.severity == Severity.CRITICAL

    def test_fails_when_set_but_empty(self) -> None:
        """An empty-string value is treated as unset -- FAILs."""
        check = _get_check("ENV_BWS_ACCESS_TOKEN")
        result = check(_make_ctx(env={"BWS_ACCESS_TOKEN": ""}))
        assert result.status == CheckStatus.FAIL

    def test_passes_when_set_and_never_leaks_the_value(self) -> None:
        """A non-empty token PASSes; the value never appears anywhere."""
        check = _get_check("ENV_BWS_ACCESS_TOKEN")
        result = check(_make_ctx(env={"BWS_ACCESS_TOKEN": _FAKE_BWS_TOKEN}))
        assert result.status == CheckStatus.PASS
        _assert_no_secret_leak(result, _FAKE_BWS_TOKEN)


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
        assert "gh auth setup-git" in result.fix, (
            f"fix text must name the remediation command; got {result.fix!r}"
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
