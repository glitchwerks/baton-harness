"""Unit tests for the Phase 2 ``--doctor``/``--strict`` CLI wiring (#193).

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
from baton_harness.chain.doctor import CheckResult, CheckStatus, Severity

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
