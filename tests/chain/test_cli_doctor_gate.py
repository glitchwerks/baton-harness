"""Unit tests for the Phase 2/3 CLI doctor wiring (#193).

Phase 2 (``--doctor``/``--strict`` standalone report mode) and Phase 3
(the ``run_gate(ctx, PRE_BOOTSTRAP)`` hard gate wired into the normal
daemon-startup path) both live in this file per the plan's section 15
task breakdown, which assigns ``test_cli_doctor_gate.py`` to both phases.

Covers the plan's Mode 1 (standalone doctor) contract
(``docs/superpowers/plans/2026-07-01-preflight-doctor-193.md``, section 7,
the Phase 2 bullet under section 15, and decisions D1/D7 in section 13):

- ``--doctor`` runs ``doctor.run_report`` over the full catalog, prints the
  report, and returns BEFORE ``bootstrap_secrets``/``run_daemon`` are
  reached -- regardless of check outcomes (D1).
- ``--strict`` flips the doctor's exit code (D7): 0 by default even when a
  CRITICAL check FAILs; 1 under ``--strict`` when any CRITICAL-severity
  check has ``status=FAIL``; still 0 under ``--strict`` when only
  WARNING-severity checks fail/warn.
- Without ``--doctor``, the existing daemon-launch path is unchanged and
  ``doctor.run_report`` is never invoked.

Deliberately NOT covered here (per the briefing): the individual 14
Phase-1 ``Check`` behaviors (already exhaustively covered by
``test_doctor.py``), and ``--strict`` used without ``--doctor`` (undefined
by the plan).

Patch-target note: ``doctor.run_report`` is patched at its defining module
(``baton_harness.chain.doctor.run_report``), mirroring the existing
``from baton_harness.chain import sandbox_config as _sandbox_cfg`` /
``_sandbox_cfg.read_and_validate(...)`` dotted-module-call idiom already
used in ``cli.py`` (cli.py:286-293) and the briefing's own phrasing
("``cli.main`` runs ``doctor.run_report(ctx)``"). If the implementation
instead does ``from baton_harness.chain.doctor import run_report`` and
calls the bare name, this patch target will need to move to
``baton_harness.chain.cli.run_report`` -- flagged in the return summary.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from baton_harness.chain.cli import main
from baton_harness.chain.doctor import (
    CheckResult,
    CheckStatus,
    Phase,
    Severity,
)

# ---------------------------------------------------------------------------
# Autouse fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _auto_patch_pre_bootstrap_gate() -> Iterator[None]:
    """No-op ``doctor.run_gate`` for tests that don't exercise it directly.

    Once Phase 3 wires ``doctor.run_gate(ctx, Phase.PRE_BOOTSTRAP)`` into
    the normal daemon-startup path, any ``--once``/daemon-path test in
    this file that doesn't stub the gate would hit the real
    implementation and fail on the CRITICAL PRE_BOOTSTRAP checks (no
    ``bws`` on PATH, no ``.bh/config.env``, etc. in the test
    environment) -- mirroring the rationale behind
    ``chain/conftest.py``'s ``_auto_patch_reconcile_startup`` fixture,
    but scoped to this file only: ``test_doctor.py`` calls
    ``doctor.run_gate`` directly and needs the real implementation, so
    this must NOT move into the shared ``chain/conftest.py`` autouse set.

    Tests that DO exercise the gate (``TestPreBootstrapDoctorGate``)
    override this fixture with their own explicit ``patch(...)`` inside
    a ``with`` block, which takes precedence as the innermost patch.
    """
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
        title=title or f"{check_id} sentinel title",
        severity=severity,
        status=status,
        detail=detail or f"{check_id} sentinel detail",
        fix=fix or f"{check_id} sentinel fix",
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

    assert "[PASS]" in output
    assert "GitHub CLI available" in output

    assert "[FAIL]" in output
    assert "Sandbox config file present" in output
    assert "Sentinel-detail: .bh/config.env is missing." in output
    assert "Sentinel-fix: create .bh/config.env in BH_PROJECT_ROOT." in output

    assert "[WARN]" in output
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
# Phase 3 (#193): run_gate(ctx, PRE_BOOTSTRAP) wired into the normal
# (non-``--doctor``) daemon-startup path
# ---------------------------------------------------------------------------


def _run_main_allow_system_exit(*args: str) -> int:
    """Run ``main`` and normalize either a return or a ``SystemExit`` to int.

    ``doctor.run_gate`` raises ``SystemExit(1)`` directly on its own
    documented contract (``doctor.py``: "Raises: SystemExit: With code 1
    on the first critical failed check."). The plan's Phase 3 bullet says
    ``cli.main`` "exits 1" on a CRITICAL doctor failure without
    prescribing whether that means letting the ``SystemExit`` propagate
    out of ``main`` unmodified (the most direct wiring: just call
    ``run_gate`` inline) or catching it and returning ``1`` (mirroring
    the ``except Exception as exc: return 1`` shape used by the
    neighboring tripwire/bootstrap blocks). Both are valid readings of
    "exits 1"; this helper normalizes so the test pins the *outcome*
    (process would exit non-zero) rather than the implementation's
    control-flow shape.

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
    """Assert ``run_gate`` was invoked once, with ``Phase.PRE_BOOTSTRAP``.

    Tolerates either a positional (``run_gate(ctx, Phase.PRE_BOOTSTRAP)``)
    or keyword (``run_gate(ctx, phase=Phase.PRE_BOOTSTRAP)``) call shape,
    so the test pins the observable phase argument, not the call
    convention the implementer chooses.

    Args:
        gate_mock: The mock standing in for ``doctor.run_gate``.
    """
    gate_mock.assert_called_once()
    call = gate_mock.call_args
    phase_arg = call.kwargs.get("phase")
    if phase_arg is None and len(call.args) >= 2:
        phase_arg = call.args[1]
    assert phase_arg is Phase.PRE_BOOTSTRAP, (
        "run_gate must be called with phase=Phase.PRE_BOOTSTRAP in the"
        f" normal daemon-startup path, got {phase_arg!r} (call={call!r})"
    )


class TestPreBootstrapDoctorGate:
    """Phase 3 (#193): the Phase-A hard gate in the normal startup path.

    Covers the plan's Phase 3 bullet (section 15) and section 8's Phase A
    integration point: ``cli.main`` must call
    ``doctor.run_gate(ctx, Phase.PRE_BOOTSTRAP)`` after the
    force-pr-not-merge tripwire self-test and before ``bootstrap_secrets``
    -- mirroring the call-order/short-circuit test style already
    established for the tripwire in
    ``TestForcePrNotMergeStartupSelfTest`` (test_cli.py).

    Patch-target note: mirrors this file's existing ``--doctor`` tests --
    ``doctor.run_gate`` is patched at its defining module
    (``baton_harness.chain.doctor.run_gate``), on the assumption
    ``cli.py`` calls it as ``doctor.run_gate(...)`` after a
    ``from baton_harness.chain import doctor`` import (the same
    dotted-module-call idiom already used for the ``--doctor`` branch and
    for ``sandbox_config``, cli.py:277,339). If the implementation
    instead imports the bare name (``from baton_harness.chain.doctor
    import run_gate``), this patch target will need to move to
    ``baton_harness.chain.cli.run_gate`` -- flagged in the return
    summary.
    """

    def test_gate_runs_after_tripwire_and_before_bootstrap_on_pass(
        self,
    ) -> None:
        """A passing gate lets startup reach run_daemon unimpeded.

        A passing gate runs between the tripwire and bootstrap, then
        execution continues through to ``run_daemon`` unimpeded.
        Combines the call-order assertion with a happy-path continuation
        check (mirrors ``test_main_runs_tripwire_self_test_before_
        bootstrap`` in test_cli.py) so a bug that wires the gate in but
        accidentally always short-circuits afterward is also caught.
        """
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

        assert call_order.index("self-test") < call_order.index("gate"), (
            "the PRE_BOOTSTRAP doctor gate must run after the"
            f" force-pr-not-merge self-test; got {call_order!r}"
        )
        assert call_order.index("gate") < call_order.index("bootstrap"), (
            "the PRE_BOOTSTRAP doctor gate must run before"
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
                side_effect=SystemExit(1),
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
            "a CRITICAL PRE_BOOTSTRAP doctor failure must produce a"
            f" non-zero (1) exit, got {result}"
        )
        _assert_run_gate_called_with_pre_bootstrap(gate_mock)
        assert not bootstrap_called, (
            "bootstrap_secrets must not run after a CRITICAL PRE_BOOTSTRAP"
            " doctor gate failure"
        )
        assert not run_daemon_called, (
            "run_daemon must not run after a CRITICAL PRE_BOOTSTRAP doctor"
            " gate failure"
        )
