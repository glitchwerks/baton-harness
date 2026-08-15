"""Unit tests for baton_harness.chain.merge.

Tests the CI-gated ``--no-ff`` merge logic.  All subprocess calls are
intercepted by patching the module-local ``_run`` seam; no live network or
``gh`` binary is required.

Design note — required-check set sourced from configuration (C-I2):
    The repo has no classic branch-protection required-check set (the API
    returns 404).  ``merge.py`` therefore takes the required-check set from
    a module constant (``REQUIRED_CHECKS``) that defaults to the four actual
    CI check names: ``Lint (ruff)``, ``Lint (shellcheck)``, ``Test (pytest)``,
    ``Type check (mypy)``.  A future wiring to config (WORKFLOW.md or
    similar) is noted as TODO in the module.

    CRITICAL: "zero matching checks found" is NEVER green (no vacuous pass).
    If a configured required check is absent from the check-runs response,
    that is treated as NOT-YET → RED on timeout, never as passing.

Data source change (#121):
    The CI query is switching from the Checks API
    (``_query_check_runs``) to the Actions API
    (``_query_action_jobs``).  The new function issues two ``gh api``
    calls per poll:

    1. ``repos/{owner}/{repo}/actions/runs?head_sha={sha}`` — returns a
       ``workflow_runs`` array (each with an ``id`` field).
    2. ``repos/{owner}/{repo}/actions/runs/{id}/jobs`` — returns a ``jobs``
       array (each with ``name``, ``status``, ``conclusion``).

    ``_query_action_jobs`` flattens the jobs into the same
    ``[{name, status, conclusion}]`` shape that ``_classify_check_runs``
    already consumes, so the green predicate, polling loop, and merge
    orchestration are unchanged.

    Re-run dedup: if the same job ``name`` appears in multiple workflow
    runs for the SHA (a re-triggered run), the job from the *latest* run
    wins.  "Latest" is determined by workflow run creation order as
    returned by the API — the implementation must choose the run with the
    higher ``id`` (or the run later in the ``workflow_runs`` list when
    ordered chronologically) for each duplicate job name.

    403 fail-fast (#119 hardening): a permission error from ``gh api``
    (non-zero exit with stderr containing ``Resource not accessible`` or
    an HTTP 403) must raise ``CiAuthError(RuntimeError)`` immediately.
    ``evaluate_ci`` must NOT catch ``CiAuthError`` — it propagates to the
    caller rather than looping to a 30-minute timeout.

Coverage:
- ``REQUIRED_CHECKS`` constant shape.
- ``_query_action_jobs`` — correct two-step call shape.
- ``_query_action_jobs`` — flattens jobs from multiple runs.
- ``_query_action_jobs`` — empty ``workflow_runs`` → returns ``[]``.
- ``_query_action_jobs`` — re-run dedup: newer run's job wins.
- ``_query_action_jobs`` — re-run dedup: older run's job does NOT win.
- ``_query_action_jobs`` — 403 → raises ``CiAuthError``.
- ``_query_action_jobs`` — "Resource not accessible" in stderr → raises
  ``CiAuthError``.
- ``evaluate_ci`` — all three jobs ``completed``/``success`` → GREEN.
- ``evaluate_ci`` — one job ``failure`` → RED.
- ``evaluate_ci`` — one job ``in_progress`` → NOT-YET → TIMEOUT (non-green).
- ``evaluate_ci`` — a required job absent from jobs → NOT-YET → TIMEOUT.
- ``evaluate_ci`` — non-required jobs ignored (still GREEN).
- ``evaluate_ci`` — ``CiAuthError`` propagates (does NOT loop to timeout).
- ``merge_issue_branch`` — ``feature/`` guard unchanged.
- ``merge_issue_branch`` — ``--no-ff`` merge command unchanged.
- ``merge_issue_branch`` — provenance trailer unchanged.
- ``merge_issue_branch`` — RED CI → ``CI_FAILED`` unchanged.
- ``merge_issue_branch`` — TIMEOUT → ``CI_TIMEOUT`` unchanged.
- ``merge_issue_branch`` — provenance persistence (label + comment).
- Dependency-order merge list.
- NEVER merges to main (hard constraint guard).
- (#353) ``CiDiagnostic`` defaults — unpopulated, ``classifier()``/
  ``describe()`` both ``""``.
- (#353) ``_build_ci_diagnostic`` — the four D3 classification cases (no
  jobs observed at all, only non-required jobs observed, required checks
  present-but-pending, a partial mix of absent/pending required checks),
  the "observed earlier, absent from final poll" per-check state, and the
  ``_DIAG_NAME_CAP`` name-list truncation in ``describe()``.
- (#353) ``evaluate_ci(diagnostic=...)`` — populates the supplied
  ``CiDiagnostic`` on RED/TIMEOUT exits (including the literal PR #6
  regression shape: empty job list, immediate timeout), leaves it
  unpopulated on GREEN, and records ``polls``/``elapsed_s``.
- (#353) ``merge_issue_branch(diagnostic=...)`` — threads the diagnostic
  through to ``evaluate_ci``; omitting it leaves behaviour unchanged.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import baton_harness.chain.merge as merge_mod
from baton_harness.chain.merge import (
    REQUIRED_CHECKS,
    CiResult,
    MergeOutcome,
    evaluate_ci,
    merge_issue_branch,
)

# ---------------------------------------------------------------------------
# Deferred imports for symbols that do not exist yet in the implementation.
#
# We use a try/except at module level so that collection succeeds and
# individual tests fail (rather than the whole module failing to import).
# The implementation must provide:
#   - CiAuthError(RuntimeError)  in baton_harness.chain.merge
#   - _query_action_jobs(owner, repo, sha) -> list[dict]  in the same module
#
# Until the implementation lands, CiAuthError is a placeholder that raises
# AssertionError on instantiation or isinstance-check (making every test
# that uses it fail with a meaningful message), and _query_action_jobs is
# None (causing AttributeError when called inside test bodies).
# ---------------------------------------------------------------------------

try:
    from baton_harness.chain.merge import (
        CiAuthError,  # type: ignore[attr-defined]
    )
except ImportError:
    # Implementation not yet written.  Placeholder causes tests to fail.
    class CiAuthError(Exception):  # type: ignore[no-redef]
        """Placeholder — implementation has not added CiAuthError yet."""

        def __init__(self, *args: object) -> None:
            """Always raises — placeholder until implementation exists."""
            raise AssertionError(
                "CiAuthError is not yet implemented in"
                " baton_harness.chain.merge — this test must FAIL until"
                " the implementation adds it."
            )


try:
    from baton_harness.chain.merge import (
        _query_action_jobs,  # type: ignore[attr-defined]
    )
except ImportError:
    _query_action_jobs = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Deferred imports for #353's CiDiagnostic surface.
#
# None of these exist in baton_harness.chain.merge yet (they are what T1
# adds). Following the same collection-survives-import-failure convention
# used above for CiAuthError / _query_action_jobs: a placeholder keeps the
# whole module importable so unrelated tests in this file are unaffected,
# while any test that actually uses CiDiagnostic / _build_ci_diagnostic /
# _DIAG_NAME_CAP fails with a clear TypeError (calling None) rather than a
# collection-time ImportError.
# ---------------------------------------------------------------------------

try:
    from baton_harness.chain.merge import (
        _DIAG_NAME_CAP,  # type: ignore[attr-defined]
    )
except ImportError:
    _DIAG_NAME_CAP = None  # type: ignore[assignment]

try:
    from baton_harness.chain.merge import (
        CiDiagnostic,  # type: ignore[attr-defined]
    )
except ImportError:
    CiDiagnostic = None  # type: ignore[assignment,misc]

try:
    from baton_harness.chain.merge import (
        _build_ci_diagnostic,  # type: ignore[attr-defined]
    )
except ImportError:
    _build_ci_diagnostic = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO = Path("/fake/repo")
_OWNER = "glitchwerks"
_REPO_NAME = "baton-harness"
_SHA = "abc123def456abc123def456abc123def456abc1"
_FEATURE = "feature/v2-daemon"

# Fake workflow run IDs used to anchor ordering in dedup tests.
_RUN_ID_OLD = 1001
_RUN_ID_NEW = 1002


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    """Return a successful CompletedProcess.

    Args:
        stdout: Simulated process output.

    Returns:
        A ``CompletedProcess`` with ``returncode=0``.
    """
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=stdout,
        stderr="",
    )


def _fail(stderr: str = "error") -> subprocess.CompletedProcess[str]:
    """Return a failed CompletedProcess.

    Args:
        stderr: Simulated error output.

    Returns:
        A ``CompletedProcess`` with ``returncode=1``.
    """
    return subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr=stderr,
    )


def _action_jobs_responses(
    runs: list[tuple[int, list[dict[str, str | None]]]],
) -> list[subprocess.CompletedProcess[str]]:
    """Build a sequence of ``_run`` responses for the two-step Actions API.

    The Actions API requires two calls per poll:
    1. ``actions/runs?head_sha=...`` → workflow_runs list (with ``id``).
    2. ``actions/runs/{id}/jobs`` → jobs list for each run.

    Args:
        runs: A list of ``(run_id, jobs)`` tuples.  Each ``jobs`` element
            is a ``{name, status, conclusion}`` dict.

    Returns:
        A list of ``CompletedProcess`` values to be consumed in order
        by a ``side_effect`` mock: first the runs-list response, then
        one jobs response per run.
    """
    run_list = [{"id": run_id} for run_id, _ in runs]
    runs_response = _ok(json.dumps({"workflow_runs": run_list}))
    jobs_responses = [_ok(json.dumps({"jobs": jobs})) for _, jobs in runs]
    return [runs_response] + jobs_responses


def _all_required_success() -> list[dict[str, str | None]]:
    """Build a jobs list where all required checks succeed.

    Returns:
        Job dicts for all three required checks, all completed/success.
    """
    return [
        {
            "name": name,
            "status": "completed",
            "conclusion": "success",
        }
        for name in REQUIRED_CHECKS
    ]


def _actions_all_success_run(
    run_id: int = 999,
) -> tuple[int, list[dict[str, str | None]]]:
    """Return a single (run_id, jobs) tuple with all required checks green.

    Args:
        run_id: The workflow run ID to use.

    Returns:
        A ``(run_id, jobs)`` tuple for use with ``_action_jobs_responses``.
    """
    return (run_id, _all_required_success())


def _check_runs_response(
    checks: list[dict[str, str | None]],
) -> subprocess.CompletedProcess[str]:
    """Wrap a list of check dicts as a ``gh api`` check-runs response.

    This helper is kept for backward-compatibility with existing tests that
    were written against ``_query_check_runs``.  New tests that target
    ``_query_action_jobs`` should use ``_action_jobs_responses`` instead.

    Args:
        checks: List of ``{name, status, conclusion}`` dicts.  ``conclusion``
            may be ``None`` for in-progress checks.

    Returns:
        A successful ``CompletedProcess`` with JSON stdout.
    """
    payload = {"check_runs": checks}
    return _ok(json.dumps(payload))


# ---------------------------------------------------------------------------
# REQUIRED_CHECKS constant
# ---------------------------------------------------------------------------


class TestRequiredChecksConstant:
    """Tests for the ``REQUIRED_CHECKS`` module constant."""

    def test_contains_lint_ruff(self) -> None:
        """REQUIRED_CHECKS contains the Lint (ruff) check name."""
        assert "Lint (ruff)" in REQUIRED_CHECKS

    def test_contains_test_pytest(self) -> None:
        """REQUIRED_CHECKS contains the Test (pytest) check name."""
        assert "Test (pytest)" in REQUIRED_CHECKS

    def test_contains_type_check_mypy(self) -> None:
        """REQUIRED_CHECKS contains the Type check (mypy) check name."""
        assert "Type check (mypy)" in REQUIRED_CHECKS

    def test_is_a_sequence_of_strings(self) -> None:
        """REQUIRED_CHECKS is a non-empty sequence of strings."""
        assert len(REQUIRED_CHECKS) >= 1
        for name in REQUIRED_CHECKS:
            assert isinstance(name, str)


# ---------------------------------------------------------------------------
# CiAuthError — typed exception for 403 / permission failures
# ---------------------------------------------------------------------------


class TestCiAuthErrorType:
    """Tests that ``CiAuthError`` is a properly typed exception."""

    def test_ci_auth_error_is_runtime_error_subclass(self) -> None:
        """``CiAuthError`` must subclass ``RuntimeError``."""
        assert issubclass(CiAuthError, RuntimeError), (
            "CiAuthError must be a subclass of RuntimeError"
        )

    def test_ci_auth_error_can_be_raised_and_caught(self) -> None:
        """``CiAuthError`` can be raised and caught independently."""
        with pytest.raises(CiAuthError):
            raise CiAuthError("permission denied")

    def test_ci_auth_error_is_not_caught_as_plain_exception(self) -> None:
        """``CiAuthError`` is distinct from a bare ``RuntimeError``."""
        # Catching RuntimeError DOES catch CiAuthError (it is a subclass),
        # but CiAuthError must be distinguishable as a specific type.
        err = CiAuthError("Resource not accessible by integration")
        assert isinstance(err, CiAuthError)
        assert isinstance(err, RuntimeError)


# ---------------------------------------------------------------------------
# _query_action_jobs — two-step Actions API calls
# ---------------------------------------------------------------------------


class TestQueryActionJobsCallShape:
    """Tests that ``_query_action_jobs`` issues the correct two-step calls."""

    def test_first_call_queries_actions_runs_with_head_sha(self) -> None:
        """First call must query ``actions/runs?head_sha={sha}``."""
        calls: list[list[str]] = []
        responses = _action_jobs_responses([_actions_all_success_run()])

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(list(cmd))
            return responses.pop(0)

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            _query_action_jobs(_OWNER, _REPO_NAME, _SHA)

        assert calls, "Expected at least one _run call"
        first_call_str = " ".join(calls[0])
        assert "actions/runs" in first_call_str, (
            "First call must query the actions/runs endpoint"
        )
        assert _SHA in first_call_str, "First call must include the head SHA"
        assert "head_sha" in first_call_str, (
            "First call must use the head_sha query parameter"
        )

    def test_second_call_queries_jobs_for_each_run_id(self) -> None:
        """Second call(s) must query ``actions/runs/{id}/jobs``."""
        calls: list[list[str]] = []
        run_id = 9876
        responses = _action_jobs_responses([(run_id, _all_required_success())])

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(list(cmd))
            return responses.pop(0)

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            _query_action_jobs(_OWNER, _REPO_NAME, _SHA)

        assert len(calls) >= 2, (
            "Must make at least two calls: one for runs, one for jobs"
        )
        jobs_calls = [c for c in calls[1:] if "jobs" in " ".join(c)]
        assert jobs_calls, "Must call the /jobs endpoint for each run"
        assert any(str(run_id) in " ".join(c) for c in jobs_calls), (
            f"Jobs call must include run_id {run_id}"
        )

    def test_two_runs_produce_two_jobs_calls(self) -> None:
        """Two workflow runs → two separate jobs API calls."""
        calls: list[list[str]] = []
        responses = _action_jobs_responses(
            [
                (
                    _RUN_ID_OLD,
                    [
                        {
                            "name": REQUIRED_CHECKS[0],
                            "status": "completed",
                            "conclusion": "success",
                        },
                    ],
                ),
                (
                    _RUN_ID_NEW,
                    [
                        {
                            "name": REQUIRED_CHECKS[1],
                            "status": "completed",
                            "conclusion": "success",
                        },
                    ],
                ),
            ]
        )

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(list(cmd))
            return responses.pop(0)

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            _query_action_jobs(_OWNER, _REPO_NAME, _SHA)

        # 1 runs call + 2 jobs calls = 3 total
        assert len(calls) == 3, (
            f"Expected 3 _run calls for 2 workflow runs, got {len(calls)}"
        )


class TestQueryActionJobsFlattening:
    """Tests that ``_query_action_jobs`` flattens jobs correctly."""

    def test_returns_jobs_with_name_status_conclusion(self) -> None:
        """Returned dicts have ``name``, ``status``, and ``conclusion``."""
        responses = _action_jobs_responses([_actions_all_success_run()])

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            return responses.pop(0)

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            jobs = _query_action_jobs(_OWNER, _REPO_NAME, _SHA)

        assert jobs, "Expected a non-empty jobs list"
        for job in jobs:
            assert "name" in job, "Job must have a 'name' field"
            assert "status" in job, "Job must have a 'status' field"
            assert "conclusion" in job, "Job must have a 'conclusion' field"

    def test_jobs_from_multiple_runs_are_flattened_into_single_list(
        self,
    ) -> None:
        """Jobs from two runs are combined into one flat list, then deduped."""
        run_a_jobs = [
            {"name": "Job A", "status": "completed", "conclusion": "success"},
        ]
        run_b_jobs = [
            {"name": "Job B", "status": "completed", "conclusion": "success"},
        ]
        responses = _action_jobs_responses(
            [
                (_RUN_ID_OLD, run_a_jobs),
                (_RUN_ID_NEW, run_b_jobs),
            ]
        )

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            return responses.pop(0)

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            jobs = _query_action_jobs(_OWNER, _REPO_NAME, _SHA)

        names = {j["name"] for j in jobs}
        assert "Job A" in names, "Jobs from the first run must be present"
        assert "Job B" in names, "Jobs from the second run must be present"

    def test_empty_workflow_runs_returns_empty_list(self) -> None:
        """An empty ``workflow_runs`` list → returns ``[]``."""
        responses = [_ok(json.dumps({"workflow_runs": []}))]

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            return responses.pop(0)

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            jobs = _query_action_jobs(_OWNER, _REPO_NAME, _SHA)

        assert jobs == [], (
            "Empty workflow_runs must produce an empty jobs list"
        )


class TestQueryActionJobsRerunDedup:
    """Tests that re-run dedup keeps the *latest* run's job for each name.

    When the same job ``name`` appears in multiple workflow runs for the
    same SHA (a re-triggered run), the job from the newest run must win.
    The test exercises both orderings to force the implementation to be
    explicit about "latest" semantics rather than relying on list order
    coincidence.
    """

    def test_newer_run_success_wins_over_older_run_failure(self) -> None:
        """Newer run's Test (pytest)=success beats older run's=failure.

        Scenario:
        - Run _RUN_ID_OLD: Test (pytest) = failure
        - Run _RUN_ID_NEW: Test (pytest) = success

        After dedup: Test (pytest) must be success (newest wins → GREEN).
        """
        old_jobs = [
            {
                "name": "Test (pytest)",
                "status": "completed",
                "conclusion": "failure",
            },
        ]
        new_jobs = [
            {
                "name": "Test (pytest)",
                "status": "completed",
                "conclusion": "success",
            },
            # Other required checks also succeed in the new run.
            {
                "name": "Lint (ruff)",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "name": "Type check (mypy)",
                "status": "completed",
                "conclusion": "success",
            },
        ]
        # API returns OLD run first, NEW run second → dedup must pick NEW.
        responses = _action_jobs_responses(
            [
                (_RUN_ID_OLD, old_jobs),
                (_RUN_ID_NEW, new_jobs),
            ]
        )

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            return responses.pop(0)

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            jobs = _query_action_jobs(_OWNER, _REPO_NAME, _SHA)

        test_jobs = [j for j in jobs if j["name"] == "Test (pytest)"]
        assert len(test_jobs) == 1, (
            "Dedup must produce exactly one entry for 'Test (pytest)'"
        )
        assert test_jobs[0]["conclusion"] == "success", (
            "Newer run's conclusion (success) must win over older (failure)"
        )

    def test_older_run_failure_does_not_win_when_newer_run_succeeds(
        self,
    ) -> None:
        """Gate is GREEN when latest run succeeds, regardless of older failure.

        This test validates the dedup end-to-end via ``evaluate_ci``:
        older=failure + newer=success → GREEN (newest wins).
        """
        old_jobs = [
            {
                "name": "Test (pytest)",
                "status": "completed",
                "conclusion": "failure",
            },
        ]
        new_jobs = _all_required_success()
        responses = _action_jobs_responses(
            [
                (_RUN_ID_OLD, old_jobs),
                (_RUN_ID_NEW, new_jobs),
            ]
        )

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            return responses.pop(0)

        with patch.object(
            merge_mod,
            "_query_action_jobs",
            side_effect=[
                # Single poll call: all required checks green after dedup.
                _all_required_success(),
            ],
        ):
            result = evaluate_ci(
                _OWNER, _REPO_NAME, _SHA, poll_interval=0, timeout=1
            )

        assert result == CiResult.GREEN, (
            "Newest run success must produce GREEN — older failure ignored"
        )

    def test_older_run_failure_wins_when_newer_run_also_fails(self) -> None:
        """When newest run also fails, gate is RED.

        Scenario:
        - Run _RUN_ID_OLD: Test (pytest) = failure
        - Run _RUN_ID_NEW: Test (pytest) = failure

        After dedup: Test (pytest) from newest run = failure → RED.
        """
        old_jobs = [
            {
                "name": "Test (pytest)",
                "status": "completed",
                "conclusion": "failure",
            },
        ]
        new_jobs = [
            {
                "name": "Test (pytest)",
                "status": "completed",
                "conclusion": "failure",
            },
            {
                "name": "Lint (ruff)",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "name": "Type check (mypy)",
                "status": "completed",
                "conclusion": "success",
            },
        ]
        responses = _action_jobs_responses(
            [
                (_RUN_ID_OLD, old_jobs),
                (_RUN_ID_NEW, new_jobs),
            ]
        )

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            return responses.pop(0)

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            jobs = _query_action_jobs(_OWNER, _REPO_NAME, _SHA)

        test_jobs = [j for j in jobs if j["name"] == "Test (pytest)"]
        assert len(test_jobs) == 1, (
            "Dedup must produce exactly one 'Test (pytest)' entry"
        )
        assert test_jobs[0]["conclusion"] == "failure", (
            "When both runs fail, the deduped job must still be failure"
        )

    def test_newer_run_failure_beats_older_run_success(self) -> None:
        """Newer run failure → gate is RED even if older run succeeded.

        Scenario:
        - Run _RUN_ID_OLD: Test (pytest) = success
        - Run _RUN_ID_NEW: Test (pytest) = failure

        After dedup: Test (pytest) from newest run = failure → RED.
        This is the reverse of the first dedup test; both orderings must
        be deterministic.
        """
        old_jobs = [
            {
                "name": "Test (pytest)",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "name": "Lint (ruff)",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "name": "Type check (mypy)",
                "status": "completed",
                "conclusion": "success",
            },
        ]
        new_jobs = [
            {
                "name": "Test (pytest)",
                "status": "completed",
                "conclusion": "failure",
            },
        ]
        # API still returns OLD first, NEW second; new run has higher ID.
        responses = _action_jobs_responses(
            [
                (_RUN_ID_OLD, old_jobs),
                (_RUN_ID_NEW, new_jobs),
            ]
        )

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            return responses.pop(0)

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            jobs = _query_action_jobs(_OWNER, _REPO_NAME, _SHA)

        test_jobs = [j for j in jobs if j["name"] == "Test (pytest)"]
        assert len(test_jobs) == 1, (
            "Dedup must produce exactly one 'Test (pytest)' entry"
        )
        assert test_jobs[0]["conclusion"] == "failure", (
            "Newer run's failure must win over older run's success"
        )


# ---------------------------------------------------------------------------
# _query_action_jobs — 403 / permission fail-fast
# ---------------------------------------------------------------------------


class TestQueryActionJobsAuthError:
    """Tests that ``_query_action_jobs`` raises ``CiAuthError`` on 403."""

    def test_403_exit_code_raises_ci_auth_error(self) -> None:
        """Non-zero exit with HTTP 403 indicator → raises ``CiAuthError``."""

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout="",
                stderr="HTTP 403: Forbidden",
            )

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            with pytest.raises(CiAuthError):
                _query_action_jobs(_OWNER, _REPO_NAME, _SHA)

    def test_resource_not_accessible_stderr_raises_ci_auth_error(
        self,
    ) -> None:
        """``Resource not accessible`` in stderr → raises ``CiAuthError``."""

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout="",
                stderr="Resource not accessible by integration",
            )

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            with pytest.raises(CiAuthError):
                _query_action_jobs(_OWNER, _REPO_NAME, _SHA)

    def test_ci_auth_error_is_not_plain_runtime_error(self) -> None:
        """The raised exception is specifically ``CiAuthError``, not just any.

        A plain ``RuntimeError`` is insufficient — callers must be able to
        catch ``CiAuthError`` specifically for the fail-fast path.
        """

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout="",
                stderr="Resource not accessible by integration",
            )

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            with pytest.raises(CiAuthError) as exc_info:
                _query_action_jobs(_OWNER, _REPO_NAME, _SHA)

        # Must be exactly CiAuthError, not just any RuntimeError subtype.
        assert type(exc_info.value) is CiAuthError, (
            "Raised exception must be exactly CiAuthError, not a subclass"
        )

    def test_non_403_failure_does_not_raise_ci_auth_error(self) -> None:
        """A generic non-zero exit (not 403) raises something else, not GREEN.

        A transient network error is NOT a permission error and should NOT
        raise ``CiAuthError``.  It may raise any other exception or be
        propagated, but must not be silently swallowed as a vacuous green.
        """
        # Require _query_action_jobs to exist; if not, this test must fail
        # (not pass vacuously because None is not callable and the
        # pytest.raises(Exception) block would swallow the TypeError).
        if _query_action_jobs is None:
            pytest.fail(
                "_query_action_jobs is not implemented yet — this test"
                " cannot meaningfully assert the non-403 discrimination"
                " until the function exists."
            )

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout="",
                stderr="connection refused",
            )

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            with pytest.raises(Exception) as exc_info:
                _query_action_jobs(_OWNER, _REPO_NAME, _SHA)

        assert not isinstance(exc_info.value, CiAuthError), (
            "A non-403 failure must NOT raise CiAuthError"
        )


# ---------------------------------------------------------------------------
# evaluate_ci — green predicate (§3.3.1) via Actions API
# ---------------------------------------------------------------------------


class TestEvaluateCiGreen:
    """Tests for the GREEN path of ``evaluate_ci`` (Actions API)."""

    def test_all_required_success_is_green(self) -> None:
        """All required jobs completed/success → GREEN."""
        with patch.object(
            merge_mod,
            "_query_action_jobs",
            return_value=_all_required_success(),
        ):
            result = evaluate_ci(
                _OWNER, _REPO_NAME, _SHA, poll_interval=0, timeout=1
            )

        assert result == CiResult.GREEN

    def test_neutral_conclusion_counts_as_pass(self) -> None:
        """A required job with conclusion='neutral' → GREEN (§3.3.1)."""
        jobs = [
            {
                "name": REQUIRED_CHECKS[0],
                "status": "completed",
                "conclusion": "neutral",
            },
        ] + [
            {
                "name": name,
                "status": "completed",
                "conclusion": "success",
            }
            for name in REQUIRED_CHECKS[1:]
        ]

        with patch.object(merge_mod, "_query_action_jobs", return_value=jobs):
            result = evaluate_ci(
                _OWNER, _REPO_NAME, _SHA, poll_interval=0, timeout=1
            )

        assert result == CiResult.GREEN

    def test_skipped_conclusion_counts_as_pass(self) -> None:
        """A required job with conclusion='skipped' → GREEN (§3.3.1)."""
        jobs = [
            {
                "name": REQUIRED_CHECKS[0],
                "status": "completed",
                "conclusion": "skipped",
            },
        ] + [
            {
                "name": name,
                "status": "completed",
                "conclusion": "success",
            }
            for name in REQUIRED_CHECKS[1:]
        ]

        with patch.object(merge_mod, "_query_action_jobs", return_value=jobs):
            result = evaluate_ci(
                _OWNER, _REPO_NAME, _SHA, poll_interval=0, timeout=1
            )

        assert result == CiResult.GREEN

    def test_non_required_failure_does_not_block_green(self) -> None:
        """A non-required failing job is ignored → GREEN (§3.3.1)."""
        jobs = _all_required_success() + [
            {
                "name": "Some optional check",
                "status": "completed",
                "conclusion": "failure",
            }
        ]

        with patch.object(merge_mod, "_query_action_jobs", return_value=jobs):
            result = evaluate_ci(
                _OWNER, _REPO_NAME, _SHA, poll_interval=0, timeout=1
            )

        assert result == CiResult.GREEN

    def test_non_required_check_in_progress_ignored(self) -> None:
        """An in-progress non-required job is ignored → GREEN."""
        jobs = _all_required_success() + [
            {
                "name": "Optional flaky check",
                "status": "in_progress",
                "conclusion": None,
            }
        ]

        with patch.object(merge_mod, "_query_action_jobs", return_value=jobs):
            result = evaluate_ci(
                _OWNER, _REPO_NAME, _SHA, poll_interval=0, timeout=1
            )

        assert result == CiResult.GREEN


class TestEvaluateCiRed:
    """Tests for the RED path of ``evaluate_ci``."""

    def test_failure_on_required_job_is_red(self) -> None:
        """A required job with conclusion='failure' → RED."""
        jobs = [
            {
                "name": REQUIRED_CHECKS[0],
                "status": "completed",
                "conclusion": "failure",
            },
        ] + [
            {
                "name": name,
                "status": "completed",
                "conclusion": "success",
            }
            for name in REQUIRED_CHECKS[1:]
        ]

        with patch.object(merge_mod, "_query_action_jobs", return_value=jobs):
            result = evaluate_ci(
                _OWNER, _REPO_NAME, _SHA, poll_interval=0, timeout=1
            )

        assert result == CiResult.RED

    def test_cancelled_on_required_job_is_red(self) -> None:
        """A required job with conclusion='cancelled' → RED."""
        jobs = [
            {
                "name": REQUIRED_CHECKS[0],
                "status": "completed",
                "conclusion": "cancelled",
            },
        ] + [
            {
                "name": name,
                "status": "completed",
                "conclusion": "success",
            }
            for name in REQUIRED_CHECKS[1:]
        ]

        with patch.object(merge_mod, "_query_action_jobs", return_value=jobs):
            result = evaluate_ci(
                _OWNER, _REPO_NAME, _SHA, poll_interval=0, timeout=1
            )

        assert result == CiResult.RED

    def test_timed_out_on_required_job_is_red(self) -> None:
        """A required job with conclusion='timed_out' → RED."""
        jobs = [
            {
                "name": REQUIRED_CHECKS[0],
                "status": "completed",
                "conclusion": "timed_out",
            },
        ] + [
            {
                "name": name,
                "status": "completed",
                "conclusion": "success",
            }
            for name in REQUIRED_CHECKS[1:]
        ]

        with patch.object(merge_mod, "_query_action_jobs", return_value=jobs):
            result = evaluate_ci(
                _OWNER, _REPO_NAME, _SHA, poll_interval=0, timeout=1
            )

        assert result == CiResult.RED

    def test_action_required_on_required_job_is_red(self) -> None:
        """A required job with conclusion='action_required' → RED."""
        jobs = [
            {
                "name": REQUIRED_CHECKS[0],
                "status": "completed",
                "conclusion": "action_required",
            },
        ] + [
            {
                "name": name,
                "status": "completed",
                "conclusion": "success",
            }
            for name in REQUIRED_CHECKS[1:]
        ]

        with patch.object(merge_mod, "_query_action_jobs", return_value=jobs):
            result = evaluate_ci(
                _OWNER, _REPO_NAME, _SHA, poll_interval=0, timeout=1
            )

        assert result == CiResult.RED


class TestEvaluateCiPollingAndTimeout:
    """Tests for polling and timeout behaviour of ``evaluate_ci``."""

    def test_in_progress_required_job_polls_then_times_out(self) -> None:
        """An in-progress required job that never completes → not green.

        The function must poll up to the timeout and then return either
        ``CiResult.RED`` or ``CiResult.TIMEOUT`` (both are non-green /
        ci-timeout semantics), never blocking forever.
        """
        in_progress_jobs = [
            {
                "name": name,
                "status": "in_progress",
                "conclusion": None,
            }
            for name in REQUIRED_CHECKS
        ]

        with patch.object(
            merge_mod,
            "_query_action_jobs",
            return_value=in_progress_jobs,
        ):
            result = evaluate_ci(
                _OWNER,
                _REPO_NAME,
                _SHA,
                poll_interval=0,
                timeout=0,  # immediate timeout
            )

        assert result != CiResult.GREEN, (
            "An in-progress job that never completes must not be GREEN"
        )

    def test_absent_required_job_is_not_vacuous_green(self) -> None:
        """A configured required job absent from response → NOT green.

        CRITICAL: Zero matching jobs found is NEVER vacuously green.
        An absent required job → NOT-YET → non-green on timeout.

        (#353) Also drives a supplied ``CiDiagnostic`` and asserts it
        records the required checks as genuinely *absent* (never
        observed), classified as a required-check name mismatch — not
        conflated with "observed but still pending" (see the sibling
        in-progress case in ``TestEvaluateCiPollingAndTimeout``, which
        is the issue's core "indistinguishable" claim being falsified).
        """
        # Only return irrelevant jobs — none are required.
        irrelevant_jobs = [
            {
                "name": "Some unrelated check",
                "status": "completed",
                "conclusion": "success",
            }
        ]
        diag = CiDiagnostic()

        with patch.object(
            merge_mod,
            "_query_action_jobs",
            return_value=irrelevant_jobs,
        ):
            result = evaluate_ci(
                _OWNER,
                _REPO_NAME,
                _SHA,
                poll_interval=0,
                timeout=0,  # immediate timeout
                diagnostic=diag,
            )

        assert result != CiResult.GREEN, (
            "An absent required job must NEVER produce a GREEN result"
        )
        assert diag.populated is True
        assert diag.never_observed == tuple(REQUIRED_CHECKS), (
            "Every required check must be recorded as never observed, "
            f"not merely pending; got {diag.never_observed!r}"
        )
        assert "Some unrelated check" in diag.other_observed
        assert diag.classifier() == "required checks never appeared"

    def test_empty_jobs_list_is_not_vacuous_green(self) -> None:
        """Empty jobs list → NOT green (no vacuous pass).

        No jobs at all = every required job is absent = NOT-YET → not
        green on timeout.

        (#353) This is the literal PR #6 regression shape (F8): zero
        Actions jobs ever observed for the SHA. The supplied
        ``CiDiagnostic`` must record every required check as absent and
        classify this as "no jobs observed" — distinct from the
        name-mismatch case above, where *some* (non-required) jobs were
        seen.
        """
        diag = CiDiagnostic()

        with patch.object(merge_mod, "_query_action_jobs", return_value=[]):
            result = evaluate_ci(
                _OWNER,
                _REPO_NAME,
                _SHA,
                poll_interval=0,
                timeout=0,
                diagnostic=diag,
            )

        assert result != CiResult.GREEN, (
            "An empty jobs list must NEVER produce GREEN"
        )
        assert diag.populated is True
        assert diag.never_observed == tuple(REQUIRED_CHECKS), (
            "Every required check must be recorded as never observed "
            f"when zero jobs were ever seen; got {diag.never_observed!r}"
        )
        assert diag.other_observed == ()
        assert diag.classifier() == "no jobs observed"

    def test_eventually_green_after_polling(self) -> None:
        """Jobs that are in_progress then complete → GREEN after polling."""
        call_count = 0

        def fake_query(
            owner: str, repo: str, sha: str, **kwargs: object
        ) -> list[dict[str, str | None]]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [
                    {
                        "name": name,
                        "status": "in_progress",
                        "conclusion": None,
                    }
                    for name in REQUIRED_CHECKS
                ]
            return _all_required_success()

        with patch.object(
            merge_mod, "_query_action_jobs", side_effect=fake_query
        ):
            result = evaluate_ci(
                _OWNER,
                _REPO_NAME,
                _SHA,
                poll_interval=0,
                timeout=60,  # allow polling
            )

        assert result == CiResult.GREEN
        assert call_count >= 2, "Must have polled at least twice"

    def test_queued_required_job_polls_not_immediately_red(self) -> None:
        """A queued required job is NOT-YET, not immediately RED."""
        call_count = 0

        def fake_query(
            owner: str, repo: str, sha: str, **kwargs: object
        ) -> list[dict[str, str | None]]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [
                    {
                        "name": name,
                        "status": "queued",
                        "conclusion": None,
                    }
                    for name in REQUIRED_CHECKS
                ]
            return _all_required_success()

        with patch.object(
            merge_mod, "_query_action_jobs", side_effect=fake_query
        ):
            result = evaluate_ci(
                _OWNER,
                _REPO_NAME,
                _SHA,
                poll_interval=0,
                timeout=60,
            )

        assert result == CiResult.GREEN
        assert call_count >= 2

    def test_checks_correct_api_owner_repo_sha(self) -> None:
        """``_query_action_jobs`` is called with the correct owner/repo/sha."""
        calls: list[tuple[str, str, str]] = []

        def fake_query(
            owner: str, repo: str, sha: str, **kwargs: object
        ) -> list[dict[str, str | None]]:
            calls.append((owner, repo, sha))
            return _all_required_success()

        with patch.object(
            merge_mod, "_query_action_jobs", side_effect=fake_query
        ):
            evaluate_ci(_OWNER, _REPO_NAME, _SHA, poll_interval=0, timeout=1)

        assert calls, "Expected _query_action_jobs to be called"
        owner, repo, sha = calls[0]
        assert owner == _OWNER
        assert repo == _REPO_NAME
        assert sha == _SHA


class TestEvaluateCiAuthErrorPropagation:
    """Tests that ``CiAuthError`` propagates out of ``evaluate_ci``."""

    def test_ci_auth_error_propagates_immediately_not_looped(self) -> None:
        """``CiAuthError`` from ``_query_action_jobs`` propagates out.

        ``evaluate_ci`` must NOT catch ``CiAuthError``.  It must propagate
        to the caller immediately — not be swallowed into a 30-min timeout.
        """
        call_count = 0

        def fake_query(
            owner: str, repo: str, sha: str, **kwargs: object
        ) -> list[dict[str, str | None]]:
            nonlocal call_count
            call_count += 1
            raise CiAuthError("Resource not accessible by integration")

        with patch.object(
            merge_mod, "_query_action_jobs", side_effect=fake_query
        ):
            with pytest.raises(CiAuthError):
                evaluate_ci(
                    _OWNER,
                    _REPO_NAME,
                    _SHA,
                    poll_interval=0,
                    timeout=60,  # long timeout — must NOT loop
                )

        assert call_count == 1, (
            "evaluate_ci must stop immediately on CiAuthError, not loop"
        )

    def test_ci_auth_error_is_not_converted_to_timeout(self) -> None:
        """``CiAuthError`` must NOT be swallowed and returned as TIMEOUT."""

        def fake_query(
            owner: str, repo: str, sha: str, **kwargs: object
        ) -> list[dict[str, str | None]]:
            raise CiAuthError("Resource not accessible by integration")

        with patch.object(
            merge_mod, "_query_action_jobs", side_effect=fake_query
        ):
            # The call must raise, never return normally.
            raised = False
            try:
                evaluate_ci(
                    _OWNER,
                    _REPO_NAME,
                    _SHA,
                    poll_interval=0,
                    timeout=0,
                )
            except CiAuthError:
                raised = True
            except Exception:
                # Any other exception is also not a silent TIMEOUT return.
                raised = True

        # The critical contract: evaluate_ci must not return a CiResult
        # when _query_action_jobs raises CiAuthError.
        assert raised, (
            "evaluate_ci must propagate CiAuthError, not return CiResult"
        )


# ---------------------------------------------------------------------------
# #353 — CiDiagnostic dataclass defaults
# ---------------------------------------------------------------------------


class TestCiDiagnosticDefaults:
    """Tests for ``CiDiagnostic``'s unpopulated (default-constructed) state.

    Per D4, an unpopulated diagnostic must degrade to nothing — no
    trailing whitespace, no empty parenthetical — so every consumer that
    forgets to pass ``diagnostic=`` (the common case per F3/R-A) gets
    exactly today's behaviour.
    """

    def test_default_construction_is_unpopulated(self) -> None:
        """A bare ``CiDiagnostic()`` starts with ``populated is False``."""
        diag = CiDiagnostic()
        assert diag.populated is False

    def test_classifier_is_empty_string_when_unpopulated(self) -> None:
        """``classifier()`` on an unpopulated diagnostic returns ``""``."""
        assert CiDiagnostic().classifier() == ""

    def test_describe_is_empty_string_when_unpopulated(self) -> None:
        """``describe()`` on an unpopulated diagnostic returns ``""``."""
        assert CiDiagnostic().describe() == ""


# ---------------------------------------------------------------------------
# #353 — _build_ci_diagnostic: the four-way D3 classification (pure)
# ---------------------------------------------------------------------------


class TestBuildCiDiagnosticClassification:
    """Pure unit tests for ``_build_ci_diagnostic``'s D3 classification.

    ``_build_ci_diagnostic`` is a pure reduction (no I/O, no time calls —
    F1) over the facts ``evaluate_ci`` already accumulates: the required
    set, every job name ever observed across all polls, the final
    snapshot, poll count, and elapsed time. These tests exercise it
    directly, independent of the poll loop or any subprocess patching.
    """

    def test_zero_jobs_ever_observed_classifies_as_no_jobs_observed(
        self,
    ) -> None:
        """D3 case 1 — nothing was ever observed for this SHA.

        This is the literal PR #6 shape (F8): the branch's tree has no
        workflow file, so no job can ever appear.
        """
        diag = _build_ci_diagnostic(
            required=tuple(REQUIRED_CHECKS),
            ever_observed=(),
            last_runs=[],
            polls=181,
            elapsed_s=1801.0,
            red=False,
        )

        assert diag.populated is True
        assert diag.never_observed == tuple(REQUIRED_CHECKS)
        assert diag.other_observed == ()
        assert diag.classifier() == "no jobs observed"
        assert diag.polls == 181
        assert diag.elapsed_s == 1801.0

    def test_only_non_required_jobs_observed_flags_name_mismatch(
        self,
    ) -> None:
        """D3 case 2 — required checks never appeared, but others did.

        This is the signature of a required-check name mismatch (one of
        the two hypotheses F8 explicitly ruled out for PR #6, and the
        second failure mode the issue names).
        """
        last_runs = [
            {
                "name": "Some unrelated check",
                "status": "completed",
                "conclusion": "success",
            }
        ]
        diag = _build_ci_diagnostic(
            required=tuple(REQUIRED_CHECKS),
            ever_observed=("Some unrelated check",),
            last_runs=last_runs,
            polls=5,
            elapsed_s=12.0,
            red=False,
        )

        assert diag.never_observed == tuple(REQUIRED_CHECKS)
        assert "Some unrelated check" in diag.other_observed
        assert diag.classifier() == "required checks never appeared"

    def test_all_required_present_but_pending_classifies_never_completed(
        self,
    ) -> None:
        """D3 case 4 — every required check appeared but never completed.

        (#353) This is the direct falsification of the issue's core
        claim that absent and pending checks are indistinguishable: the
        ``never_observed`` set is empty here (contrast the
        ``no_jobs_observed`` / ``name_mismatch`` cases above, where it is
        exactly ``REQUIRED_CHECKS``), and every per-check state is
        ``"in_progress/--"`` rather than ``"never observed"``.
        """
        last_runs = [
            {"name": name, "status": "in_progress", "conclusion": None}
            for name in REQUIRED_CHECKS
        ]
        diag = _build_ci_diagnostic(
            required=tuple(REQUIRED_CHECKS),
            ever_observed=tuple(REQUIRED_CHECKS),
            last_runs=last_runs,
            polls=180,
            elapsed_s=1800.0,
            red=False,
        )

        assert diag.never_observed == ()
        assert diag.states, "expected one state entry per required check"
        assert len(diag.states) == len(REQUIRED_CHECKS)
        for _name, state in diag.states:
            assert state == "in_progress/--", (
                f"a present-but-pending required check must render as "
                f"'in_progress/--', not {state!r}"
            )
        assert diag.classifier() == "required checks never completed"

    def test_partial_missing_and_pending_mix_classifies_as_partial(
        self,
    ) -> None:
        """D3 case 3 — some required checks absent, others pending.

        This mixed state is exactly what a single ``CiResult`` enum
        member cannot express (D2) — it is a partition of the required
        set, not a single fact.
        """
        present = tuple(REQUIRED_CHECKS[:2])
        absent = tuple(REQUIRED_CHECKS[2:])
        assert present and absent, (
            "REQUIRED_CHECKS must have at least 2 entries on each side "
            "for this test to exercise a genuine partial mix"
        )
        last_runs = [
            {"name": name, "status": "in_progress", "conclusion": None}
            for name in present
        ]
        diag = _build_ci_diagnostic(
            required=tuple(REQUIRED_CHECKS),
            ever_observed=present,
            last_runs=last_runs,
            polls=90,
            elapsed_s=900.0,
            red=False,
        )

        assert set(diag.never_observed) == set(absent)
        assert diag.never_observed, "the absent half must be non-empty"
        assert diag.states, "the pending half must be non-empty"
        assert diag.classifier() == "required checks partially missing"

    def test_observed_earlier_absent_from_final_poll_state(self) -> None:
        """A name in ``ever_observed`` but missing from ``last_runs``.

        Not hypothetical padding (per D3): this is the only honest state
        for a workflow run deleted or re-created mid-poll, and without it
        a "never completed" message could assert something false.
        """
        required = (REQUIRED_CHECKS[0],)
        diag = _build_ci_diagnostic(
            required=required,
            ever_observed=(REQUIRED_CHECKS[0],),
            last_runs=[],  # absent from the FINAL snapshot only
            polls=10,
            elapsed_s=100.0,
            red=False,
        )

        assert diag.never_observed == (), (
            "a name that was observed at least once must not be treated "
            "as never observed, even if absent from the final snapshot"
        )
        [(name, state)] = diag.states
        assert name == required[0]
        assert state == "observed earlier, absent from final poll"

    def test_diag_name_cap_bounds_other_job_names_in_rendered_message(
        self,
    ) -> None:
        """``_DIAG_NAME_CAP`` truncates long non-required name lists.

        A monorepo with dozens of matrix jobs must not blow up the
        rendered park comment — capped at ``_DIAG_NAME_CAP`` entries plus
        a ``"+K more"`` marker (D3).
        """
        # Zero-padded so no name is a substring of another (e.g. "job-1"
        # would otherwise be a substring of "job-10") — a correct
        # implementation could render either the first or the last
        # _DIAG_NAME_CAP names, and an un-padded set would make the
        # ``listed_count`` check below unreliable in either direction.
        many_names = tuple(f"job-{i:03d}" for i in range(_DIAG_NAME_CAP + 5))
        last_runs = [
            {"name": n, "status": "completed", "conclusion": "success"}
            for n in many_names
        ]
        diag = _build_ci_diagnostic(
            required=tuple(REQUIRED_CHECKS),
            ever_observed=many_names,
            last_runs=last_runs,
            polls=3,
            elapsed_s=9.0,
            red=False,
        )

        rendered = diag.describe()
        assert "+5 more" in rendered, (
            f"expected a '+5 more' truncation marker (K={len(many_names)}"
            f" - _DIAG_NAME_CAP={_DIAG_NAME_CAP}); got: {rendered!r}"
        )
        listed_count = sum(1 for n in many_names if n in rendered)
        assert listed_count <= _DIAG_NAME_CAP, (
            f"expected at most {_DIAG_NAME_CAP} names interpolated into "
            f"the rendered message; found {listed_count} in {rendered!r}"
        )


# ---------------------------------------------------------------------------
# #356 — _build_ci_diagnostic: genuine RED (terminal failure) classification
#
# CodeRabbit review on PR #356: ``_build_ci_diagnostic`` accepts ``red`` but
# discards it (``del red``), so ``failed`` is always empty and a required
# check with status="completed"/conclusion="failure" is misclassified with
# the "required checks never completed" headline — indistinguishable from a
# check that is merely still pending. These tests pin the correct, distinct
# behaviour: ``failed`` is populated for terminal-failure required checks
# and ``classifier()`` returns a new, dedicated "required check failed"
# headline instead of the misleading pending-style one.
# ---------------------------------------------------------------------------


class TestBuildCiDiagnosticRedPathFailureClassification:
    """Pure unit tests for the RED (terminal failure) branch of D3."""

    def test_completed_failure_required_check_populates_failed_tuple(
        self,
    ) -> None:
        """A required check with a failing conclusion lands in ``diag.failed``.

        (#356) Before the fix, ``_build_ci_diagnostic`` does ``del red``
        and never inspects individual conclusions for failure, so
        ``failed`` stays ``()`` no matter what the snapshot contains.
        """
        failed_check = REQUIRED_CHECKS[0]
        last_runs = [
            {
                "name": failed_check,
                "status": "completed",
                "conclusion": "failure",
            }
        ] + [
            {"name": name, "status": "completed", "conclusion": "success"}
            for name in REQUIRED_CHECKS[1:]
        ]
        diag = _build_ci_diagnostic(
            required=tuple(REQUIRED_CHECKS),
            ever_observed=tuple(REQUIRED_CHECKS),
            last_runs=last_runs,
            polls=3,
            elapsed_s=30.0,
            red=True,
        )

        assert diag.failed, (
            "expected the terminal-failure required check to appear in "
            f"diag.failed; got {diag.failed!r}"
        )
        failed_names = [name for name, _state in diag.failed]
        assert failed_check in failed_names, (
            f"expected {failed_check!r} in diag.failed names; got "
            f"{failed_names!r}"
        )
        # The recorded state must reflect the actual failing conclusion,
        # matching the "{status}/{conclusion}" convention already used by
        # diag.states elsewhere in this module.
        failed_state = dict(diag.failed)[failed_check]
        assert failed_state == "completed/failure", (
            f"expected the failed-check state to read 'completed/failure'; "
            f"got {failed_state!r}"
        )

    def test_completed_failure_required_check_gets_dedicated_classifier(
        self,
    ) -> None:
        """A failed required check must not reuse the pending classifier.

        (#356) This is the false-classifier CodeRabbit flagged: a hard
        failure is not the same fact as "appeared but still pending", and
        conflating them misleads whoever reads ``parked_reasons``.
        """
        failed_check = REQUIRED_CHECKS[0]
        last_runs = [
            {
                "name": failed_check,
                "status": "completed",
                "conclusion": "failure",
            }
        ] + [
            {"name": name, "status": "completed", "conclusion": "success"}
            for name in REQUIRED_CHECKS[1:]
        ]
        diag = _build_ci_diagnostic(
            required=tuple(REQUIRED_CHECKS),
            ever_observed=tuple(REQUIRED_CHECKS),
            last_runs=last_runs,
            polls=1,
            elapsed_s=1.0,
            red=True,
        )

        assert diag.classifier() == "required check failed", (
            f"expected the dedicated RED classifier; got {diag.classifier()!r}"
        )
        assert diag.classifier() != "required checks never completed", (
            "a genuine terminal failure must not be misclassified as "
            "still-pending"
        )

    def test_multiple_failed_required_checks_all_appear_in_failed_tuple(
        self,
    ) -> None:
        """More than one required check can fail in the same snapshot."""
        first, second, *rest = REQUIRED_CHECKS
        last_runs = [
            {"name": first, "status": "completed", "conclusion": "failure"},
            {"name": second, "status": "completed", "conclusion": "cancelled"},
        ] + [
            {"name": name, "status": "completed", "conclusion": "success"}
            for name in rest
        ]
        diag = _build_ci_diagnostic(
            required=tuple(REQUIRED_CHECKS),
            ever_observed=tuple(REQUIRED_CHECKS),
            last_runs=last_runs,
            polls=1,
            elapsed_s=1.0,
            red=True,
        )

        failed_names = {name for name, _state in diag.failed}
        assert failed_names == {first, second}, (
            f"expected both failing required checks in diag.failed; got "
            f"{failed_names!r}"
        )
        assert diag.classifier() == "required check failed"

    def test_red_failure_headline_names_the_failed_check_in_describe(
        self,
    ) -> None:
        """``describe()`` for the RED classifier must name the failed check."""
        failed_check = REQUIRED_CHECKS[0]
        last_runs = [
            {
                "name": failed_check,
                "status": "completed",
                "conclusion": "failure",
            }
        ] + [
            {"name": name, "status": "completed", "conclusion": "success"}
            for name in REQUIRED_CHECKS[1:]
        ]
        diag = _build_ci_diagnostic(
            required=tuple(REQUIRED_CHECKS),
            ever_observed=tuple(REQUIRED_CHECKS),
            last_runs=last_runs,
            polls=1,
            elapsed_s=1.0,
            red=True,
        )

        rendered = diag.describe()
        assert failed_check in rendered, (
            f"expected the failed check's name in the rendered describe() "
            f"block; got {rendered!r}"
        )
        assert "did not all reach a completed passing state" not in rendered, (
            "the RED/failure headline must not reuse the generic "
            "still-pending wording; got " + repr(rendered)
        )


class TestCiDiagnosticDescribePollsElapsedTrailer:
    """(#356) ``describe()`` must report polls/elapsed for every classifier.

    Before the fix, only the "no jobs observed" branch embeds
    ``polls=``/``elapsed=`` in its headline; every other classifier
    (RED failure, partial-missing, never-completed) renders no polling
    metrics at all even though ``CiDiagnostic.polls`` /
    ``.elapsed_s`` are always populated. The fix contract: a single
    common trailer line, ``f"Polls: {self.polls}, elapsed: "
    f"{self.elapsed_s:.0f}s"``, appended once after the "Other job
    names observed: ..." line, regardless of classifier.
    """

    def test_red_failure_describe_includes_polls_elapsed_trailer(
        self,
    ) -> None:
        """The "required check failed" classifier must report polls/elapsed."""
        failed_check = REQUIRED_CHECKS[0]
        last_runs = [
            {
                "name": failed_check,
                "status": "completed",
                "conclusion": "failure",
            }
        ] + [
            {"name": name, "status": "completed", "conclusion": "success"}
            for name in REQUIRED_CHECKS[1:]
        ]
        diag = _build_ci_diagnostic(
            required=tuple(REQUIRED_CHECKS),
            ever_observed=tuple(REQUIRED_CHECKS),
            last_runs=last_runs,
            polls=7,
            elapsed_s=123.4,
            red=True,
        )
        assert diag.classifier() == "required check failed"

        rendered = diag.describe()
        expected_trailer = "Polls: 7, elapsed: 123s"
        assert expected_trailer in rendered, (
            "expected the common polls/elapsed trailer in describe() for "
            f"the RED classifier; got {rendered!r}"
        )
        assert rendered.count("Polls:") == 1, (
            "the polls/elapsed trailer must appear exactly once, not be "
            f"duplicated per branch; got {rendered!r}"
        )

    def test_never_completed_describe_includes_polls_elapsed_trailer(
        self,
    ) -> None:
        """The default "never completed" classifier reports polls/elapsed."""
        last_runs = [
            {"name": name, "status": "in_progress", "conclusion": None}
            for name in REQUIRED_CHECKS
        ]
        diag = _build_ci_diagnostic(
            required=tuple(REQUIRED_CHECKS),
            ever_observed=tuple(REQUIRED_CHECKS),
            last_runs=last_runs,
            polls=180,
            elapsed_s=1800.0,
            red=False,
        )
        assert diag.classifier() == "required checks never completed"

        rendered = diag.describe()
        expected_trailer = "Polls: 180, elapsed: 1800s"
        assert expected_trailer in rendered, (
            "expected the common polls/elapsed trailer in describe() for "
            f"the never-completed classifier; got {rendered!r}"
        )
        assert rendered.count("Polls:") == 1, (
            "the polls/elapsed trailer must appear exactly once, not be "
            f"duplicated per branch; got {rendered!r}"
        )

    def test_partially_missing_describe_includes_polls_elapsed_trailer(
        self,
    ) -> None:
        """The "partially missing" classifier reports polls/elapsed too."""
        present = tuple(REQUIRED_CHECKS[:2])
        assert len(REQUIRED_CHECKS) > 2, (
            "REQUIRED_CHECKS must have at least 3 entries for this test "
            "to exercise a genuine partial mix"
        )
        last_runs = [
            {"name": name, "status": "in_progress", "conclusion": None}
            for name in present
        ]
        diag = _build_ci_diagnostic(
            required=tuple(REQUIRED_CHECKS),
            ever_observed=present,
            last_runs=last_runs,
            polls=90,
            elapsed_s=900.0,
            red=False,
        )
        assert diag.classifier() == "required checks partially missing"

        rendered = diag.describe()
        expected_trailer = "Polls: 90, elapsed: 900s"
        assert expected_trailer in rendered, (
            "expected the common polls/elapsed trailer in describe() for "
            f"the partially-missing classifier; got {rendered!r}"
        )
        assert rendered.count("Polls:") == 1, (
            "the polls/elapsed trailer must appear exactly once, not be "
            f"duplicated per branch; got {rendered!r}"
        )

    def test_trailer_appears_after_other_job_names_line(self) -> None:
        """The trailer is appended after "Other job names observed:".

        Not interleaved per classifier headline — a single common
        block appended once at the end of ``describe()``'s output.
        """
        failed_check = REQUIRED_CHECKS[0]
        last_runs = [
            {
                "name": failed_check,
                "status": "completed",
                "conclusion": "failure",
            }
        ] + [
            {"name": name, "status": "completed", "conclusion": "success"}
            for name in REQUIRED_CHECKS[1:]
        ]
        diag = _build_ci_diagnostic(
            required=tuple(REQUIRED_CHECKS),
            ever_observed=tuple(REQUIRED_CHECKS),
            last_runs=last_runs,
            polls=2,
            elapsed_s=20.0,
            red=True,
        )

        rendered = diag.describe()
        other_jobs_idx = rendered.find("Other job names observed:")
        polls_idx = rendered.find("Polls:")
        assert other_jobs_idx != -1, (
            f"expected 'Other job names observed:' line in describe(); "
            f"got {rendered!r}"
        )
        assert polls_idx != -1, (
            f"expected a 'Polls:' trailer line in describe(); got {rendered!r}"
        )
        assert polls_idx > other_jobs_idx, (
            "expected the polls/elapsed trailer to be appended after the "
            f"'Other job names observed:' line; got {rendered!r}"
        )


# ---------------------------------------------------------------------------
# #353 — evaluate_ci(diagnostic=...): accumulation across the poll loop
# ---------------------------------------------------------------------------


class TestEvaluateCiDiagnosticAccumulation:
    """Tests for ``evaluate_ci``'s new ``diagnostic`` out-parameter (D1).

    ``_build_ci_diagnostic`` is pure and tested in isolation above; these
    tests exercise the accumulation *inside* ``evaluate_ci``'s real poll
    loop (``ever_observed``, ``polls``, ``elapsed_s``) and the RED/TIMEOUT
    -only population rule.
    """

    def test_populates_diagnostic_on_empty_jobs_timeout_pr6_regression(
        self,
    ) -> None:
        """The literal PR #6 regression (F8): empty job list, timeout=0."""
        diag = CiDiagnostic()

        with patch.object(merge_mod, "_query_action_jobs", return_value=[]):
            result = evaluate_ci(
                _OWNER,
                _REPO_NAME,
                _SHA,
                poll_interval=0,
                timeout=0,
                diagnostic=diag,
            )

        assert result != CiResult.GREEN
        assert diag.populated is True
        assert diag.never_observed == tuple(REQUIRED_CHECKS)
        assert diag.other_observed == ()
        assert diag.classifier() == "no jobs observed"

    def test_populates_diagnostic_with_name_mismatch_for_irrelevant_jobs(
        self,
    ) -> None:
        """Only non-required jobs observed → name-mismatch classifier."""
        diag = CiDiagnostic()
        irrelevant_jobs = [
            {
                "name": "Some unrelated check",
                "status": "completed",
                "conclusion": "success",
            }
        ]

        with patch.object(
            merge_mod, "_query_action_jobs", return_value=irrelevant_jobs
        ):
            evaluate_ci(
                _OWNER,
                _REPO_NAME,
                _SHA,
                poll_interval=0,
                timeout=0,
                diagnostic=diag,
            )

        assert "Some unrelated check" in diag.other_observed
        assert diag.classifier() == "required checks never appeared"

    def test_distinguishes_pending_required_checks_from_absent_ones(
        self,
    ) -> None:
        """All required checks in_progress → distinct from "never observed".

        (#353 core claim, falsified directly.) Contrast with the
        empty-jobs and irrelevant-jobs tests above: there,
        ``never_observed`` equals the full required set. Here it is
        empty, and every state is ``"in_progress/--"`` — the exact
        distinction the issue claims ``_classify_check_runs`` cannot
        make.
        """
        diag = CiDiagnostic()
        in_progress_jobs = [
            {"name": name, "status": "in_progress", "conclusion": None}
            for name in REQUIRED_CHECKS
        ]

        with patch.object(
            merge_mod, "_query_action_jobs", return_value=in_progress_jobs
        ):
            evaluate_ci(
                _OWNER,
                _REPO_NAME,
                _SHA,
                poll_interval=0,
                timeout=0,
                diagnostic=diag,
            )

        assert diag.never_observed == ()
        assert diag.states
        for _name, state in diag.states:
            assert state == "in_progress/--"
        assert diag.classifier() == "required checks never completed"

    def test_partial_mix_of_absent_and_pending_required_checks(
        self,
    ) -> None:
        """Two required checks pending, two absent → partial classifier."""
        diag = CiDiagnostic()
        present = REQUIRED_CHECKS[:2]
        absent = REQUIRED_CHECKS[2:]
        assert present and absent
        jobs = [
            {"name": name, "status": "in_progress", "conclusion": None}
            for name in present
        ]

        with patch.object(merge_mod, "_query_action_jobs", return_value=jobs):
            evaluate_ci(
                _OWNER,
                _REPO_NAME,
                _SHA,
                poll_interval=0,
                timeout=0,
                diagnostic=diag,
            )

        assert set(diag.never_observed) == set(absent)
        assert diag.classifier() == "required checks partially missing"

    def test_green_exit_leaves_diagnostic_unpopulated(self) -> None:
        """A GREEN result must not populate the diagnostic (D1/T1).

        No consumer needs diagnostic detail on the happy path — only the
        RED and TIMEOUT exits populate it.
        """
        diag = CiDiagnostic()

        with patch.object(
            merge_mod,
            "_query_action_jobs",
            return_value=_all_required_success(),
        ):
            result = evaluate_ci(
                _OWNER,
                _REPO_NAME,
                _SHA,
                poll_interval=0,
                timeout=1,
                diagnostic=diag,
            )

        assert result == CiResult.GREEN
        assert diag.populated is False

    def test_records_poll_count_and_elapsed_time(self) -> None:
        """``polls`` and ``elapsed_s`` are recorded on the diagnostic."""
        diag = CiDiagnostic()

        with patch.object(merge_mod, "_query_action_jobs", return_value=[]):
            evaluate_ci(
                _OWNER,
                _REPO_NAME,
                _SHA,
                poll_interval=0,
                timeout=0,
                diagnostic=diag,
            )

        assert diag.polls >= 1
        assert diag.elapsed_s >= 0.0

    def test_omitting_diagnostic_kwarg_leaves_behaviour_unchanged(
        self,
    ) -> None:
        """``diagnostic=None`` (the default) must not change behaviour.

        Every pre-existing ``evaluate_ci`` call site (and every test
        above this one in the file) omits ``diagnostic=`` — this is the
        explicit guard that omitting it raises nothing and returns the
        same result as always.
        """
        with patch.object(merge_mod, "_query_action_jobs", return_value=[]):
            result = evaluate_ci(
                _OWNER,
                _REPO_NAME,
                _SHA,
                poll_interval=0,
                timeout=0,
            )

        assert result != CiResult.GREEN


# ---------------------------------------------------------------------------
# merge_issue_branch — the --no-ff merge (Actions-API rewired)
# ---------------------------------------------------------------------------


class TestMergeIssueBranch:
    """Tests for ``merge_issue_branch`` performing the gated --no-ff merge."""

    def test_uses_no_ff_flag(self) -> None:
        """Merge command uses ``--no-ff``, not squash."""
        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _ok()

        with patch.object(
            merge_mod,
            "_query_action_jobs",
            return_value=_all_required_success(),
        ):
            with patch.object(merge_mod, "_run", side_effect=fake_run):
                merge_issue_branch(
                    _REPO,
                    _OWNER,
                    _REPO_NAME,
                    issue=44,
                    pr_head_sha=_SHA,
                    issue_branch="baton/v2-daemon-44",
                    feature_branch=_FEATURE,
                    poll_interval=0,
                    timeout=1,
                )

        merge_cmds = [c for c in calls if "merge" in c]
        assert any("--no-ff" in c for c in merge_cmds), (
            "Merge must use --no-ff"
        )
        for cmd in merge_cmds:
            assert "--squash" not in cmd, "Must NOT use --squash"

    def test_merges_issue_branch_into_feature_branch(self) -> None:
        """Merges the per-issue branch into ``feature/<slug>`` (not main)."""
        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _ok()

        with patch.object(
            merge_mod,
            "_query_action_jobs",
            return_value=_all_required_success(),
        ):
            with patch.object(merge_mod, "_run", side_effect=fake_run):
                merge_issue_branch(
                    _REPO,
                    _OWNER,
                    _REPO_NAME,
                    issue=44,
                    pr_head_sha=_SHA,
                    issue_branch="baton/v2-daemon-44",
                    feature_branch=_FEATURE,
                    poll_interval=0,
                    timeout=1,
                )

        checkout_cmds = [c for c in calls if "checkout" in c]
        assert any(_FEATURE in c for c in checkout_cmds), (
            f"Must checkout INTO the feature branch before merging."
            f" Calls: {calls}"
        )
        merge_cmds = [c for c in calls if "merge" in c and "git" in c]
        assert merge_cmds, "Expected a git merge command"
        for cmd in merge_cmds:
            assert "baton/v2-daemon-44" in cmd, (
                "Merge must include the issue branch"
            )
            assert "main" not in cmd, "Must NEVER explicitly merge to main"

    def test_never_merges_to_main(self) -> None:
        """Hard constraint: merge target is NEVER main (regression guard)."""
        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _ok()

        with patch.object(
            merge_mod,
            "_query_action_jobs",
            return_value=_all_required_success(),
        ):
            with patch.object(merge_mod, "_run", side_effect=fake_run):
                merge_issue_branch(
                    _REPO,
                    _OWNER,
                    _REPO_NAME,
                    issue=44,
                    pr_head_sha=_SHA,
                    issue_branch="baton/v2-daemon-44",
                    feature_branch=_FEATURE,
                    poll_interval=0,
                    timeout=1,
                )

        for cmd in calls:
            if cmd and "merge" in cmd:
                joined = " ".join(cmd)
                assert "main" not in joined, (
                    f"HARD CONSTRAINT: merge must never target main: {joined}"
                )

    def test_feature_branch_guard_raises_on_non_feature_branch(
        self,
    ) -> None:
        """``ValueError`` raised if ``feature_branch`` lacks ``feature/``."""
        with pytest.raises(ValueError, match="feature/"):
            merge_issue_branch(
                _REPO,
                _OWNER,
                _REPO_NAME,
                issue=44,
                pr_head_sha=_SHA,
                issue_branch="baton/v2-daemon-44",
                feature_branch="main",  # must be rejected
                poll_interval=0,
                timeout=1,
            )

    def test_provenance_trailer_in_merge_message(self) -> None:
        """Merge commit message carries the daemon-provenance trailer."""
        captured_messages: list[str] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            if "merge" in cmd and "-m" in cmd:
                m_idx = cmd.index("-m")
                if m_idx + 1 < len(cmd):
                    captured_messages.append(cmd[m_idx + 1])
            return _ok()

        with patch.object(
            merge_mod,
            "_query_action_jobs",
            return_value=_all_required_success(),
        ):
            with patch.object(merge_mod, "_run", side_effect=fake_run):
                merge_issue_branch(
                    _REPO,
                    _OWNER,
                    _REPO_NAME,
                    issue=44,
                    pr_head_sha=_SHA,
                    issue_branch="baton/v2-daemon-44",
                    feature_branch=_FEATURE,
                    poll_interval=0,
                    timeout=1,
                )

        assert captured_messages, "Expected a -m message on the merge command"
        full_msg = " ".join(captured_messages)
        assert "Baton-Harness-Merge" in full_msg, (
            "Merge message must carry Baton-Harness-Merge trailer"
        )
        assert "issue-44" in full_msg or "44" in full_msg, (
            "Merge message must reference the issue number"
        )
        assert "ci=green" in full_msg, (
            "Merge message must record ci=green fact"
        )

    def test_returns_success_outcome_on_green_merge(self) -> None:
        """Returns ``MergeOutcome.MERGED`` on a successful green merge."""
        with patch.object(
            merge_mod,
            "_query_action_jobs",
            return_value=_all_required_success(),
        ):
            with patch.object(merge_mod, "_run", return_value=_ok()):
                outcome = merge_issue_branch(
                    _REPO,
                    _OWNER,
                    _REPO_NAME,
                    issue=44,
                    pr_head_sha=_SHA,
                    issue_branch="baton/v2-daemon-44",
                    feature_branch=_FEATURE,
                    poll_interval=0,
                    timeout=1,
                )

        assert outcome == MergeOutcome.MERGED

    def test_returns_red_outcome_when_ci_fails(self) -> None:
        """Returns ``MergeOutcome.CI_FAILED`` when CI is RED."""
        red_jobs = [
            {
                "name": REQUIRED_CHECKS[0],
                "status": "completed",
                "conclusion": "failure",
            },
        ] + [
            {
                "name": name,
                "status": "completed",
                "conclusion": "success",
            }
            for name in REQUIRED_CHECKS[1:]
        ]

        with patch.object(
            merge_mod, "_query_action_jobs", return_value=red_jobs
        ):
            outcome = merge_issue_branch(
                _REPO,
                _OWNER,
                _REPO_NAME,
                issue=44,
                pr_head_sha=_SHA,
                issue_branch="baton/v2-daemon-44",
                feature_branch=_FEATURE,
                poll_interval=0,
                timeout=1,
            )

        assert outcome == MergeOutcome.CI_FAILED

    def test_returns_timeout_outcome_when_ci_never_completes(self) -> None:
        """Returns ``MergeOutcome.CI_TIMEOUT`` when CI never completes."""
        in_progress_jobs = [
            {
                "name": name,
                "status": "in_progress",
                "conclusion": None,
            }
            for name in REQUIRED_CHECKS
        ]

        with patch.object(
            merge_mod,
            "_query_action_jobs",
            return_value=in_progress_jobs,
        ):
            outcome = merge_issue_branch(
                _REPO,
                _OWNER,
                _REPO_NAME,
                issue=44,
                pr_head_sha=_SHA,
                issue_branch="baton/v2-daemon-44",
                feature_branch=_FEATURE,
                poll_interval=0,
                timeout=0,  # immediate timeout
            )

        assert outcome == MergeOutcome.CI_TIMEOUT

    def test_no_merge_command_issued_when_ci_fails(self) -> None:
        """No ``git merge`` is called when CI is RED (do not merge on red)."""
        calls: list[list[str]] = []
        red_jobs = [
            {
                "name": REQUIRED_CHECKS[0],
                "status": "completed",
                "conclusion": "failure",
            },
        ] + [
            {
                "name": name,
                "status": "completed",
                "conclusion": "success",
            }
            for name in REQUIRED_CHECKS[1:]
        ]

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _ok()

        with patch.object(
            merge_mod, "_query_action_jobs", return_value=red_jobs
        ):
            with patch.object(merge_mod, "_run", side_effect=fake_run):
                merge_issue_branch(
                    _REPO,
                    _OWNER,
                    _REPO_NAME,
                    issue=44,
                    pr_head_sha=_SHA,
                    issue_branch="baton/v2-daemon-44",
                    feature_branch=_FEATURE,
                    poll_interval=0,
                    timeout=1,
                )

        merge_cmds = [c for c in calls if "merge" in c and "git" in c]
        assert not merge_cmds, "Must NOT issue git merge when CI is RED"


# ---------------------------------------------------------------------------
# Provenance persistence (agent-merged label + marker comment)
# ---------------------------------------------------------------------------


class TestMergeIssueBranchDiagnosticThreading:
    """T2: ``merge_issue_branch`` threads ``diagnostic=`` to ``evaluate_ci``.

    ``merge_issue_branch`` gains ``diagnostic: CiDiagnostic | None = None``
    and forwards it unchanged to ``evaluate_ci`` (D1). Both TIMEOUT and
    "no kwarg at all" behaviour are pinned.
    """

    def test_merge_issue_branch_populates_supplied_diagnostic_on_timeout(
        self,
    ) -> None:
        """A supplied diagnostic is populated when the gate times out."""
        diag = CiDiagnostic()

        with patch.object(merge_mod, "_query_action_jobs", return_value=[]):
            outcome = merge_issue_branch(
                _REPO,
                _OWNER,
                _REPO_NAME,
                issue=44,
                pr_head_sha=_SHA,
                issue_branch="baton/v2-daemon-44",
                feature_branch=_FEATURE,
                poll_interval=0,
                timeout=0,
                diagnostic=diag,
            )

        assert outcome == MergeOutcome.CI_TIMEOUT
        assert diag.populated is True
        assert diag.classifier() == "no jobs observed"

    def test_merge_issue_branch_without_diagnostic_kwarg_is_unchanged(
        self,
    ) -> None:
        """Omitting ``diagnostic=`` (the default) changes nothing."""
        with patch.object(merge_mod, "_query_action_jobs", return_value=[]):
            outcome = merge_issue_branch(
                _REPO,
                _OWNER,
                _REPO_NAME,
                issue=44,
                pr_head_sha=_SHA,
                issue_branch="baton/v2-daemon-44",
                feature_branch=_FEATURE,
                poll_interval=0,
                timeout=0,
            )

        assert outcome == MergeOutcome.CI_TIMEOUT


class TestProvenancePersistence:
    """Tests for persisting the CI-green-at-merge fact (B-I2 / §11.5)."""

    def test_agent_merged_label_added_after_green_merge(self) -> None:
        """``agent-merged`` label is added to the issue after green merge."""
        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _ok()

        with patch.object(
            merge_mod,
            "_query_action_jobs",
            return_value=_all_required_success(),
        ):
            with patch.object(merge_mod, "_run", side_effect=fake_run):
                merge_issue_branch(
                    _REPO,
                    _OWNER,
                    _REPO_NAME,
                    issue=44,
                    pr_head_sha=_SHA,
                    issue_branch="baton/v2-daemon-44",
                    feature_branch=_FEATURE,
                    poll_interval=0,
                    timeout=1,
                )

        label_cmds = [
            c
            for c in calls
            if "gh" in c
            and "issue" in c
            and (
                "agent-merged" in " ".join(c)
                or any("label" in tok for tok in c)
            )
        ]
        assert label_cmds, "Expected a gh command to add agent-merged label"

    def test_marker_comment_posted_after_green_merge(self) -> None:
        """A marker comment is posted on the issue/PR after green merge."""
        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _ok()

        with patch.object(
            merge_mod,
            "_query_action_jobs",
            return_value=_all_required_success(),
        ):
            with patch.object(merge_mod, "_run", side_effect=fake_run):
                merge_issue_branch(
                    _REPO,
                    _OWNER,
                    _REPO_NAME,
                    issue=44,
                    pr_head_sha=_SHA,
                    issue_branch="baton/v2-daemon-44",
                    feature_branch=_FEATURE,
                    poll_interval=0,
                    timeout=1,
                )

        comment_cmds = [
            c for c in calls if "gh" in c and "comment" in " ".join(c)
        ]
        assert comment_cmds, (
            "Expected a gh comment command for the CI-green marker"
        )

    def test_no_label_or_comment_when_ci_fails(self) -> None:
        """No ``agent-merged`` label or marker comment when CI is RED."""
        calls: list[list[str]] = []
        red_jobs = [
            {
                "name": REQUIRED_CHECKS[0],
                "status": "completed",
                "conclusion": "failure",
            },
        ] + [
            {
                "name": name,
                "status": "completed",
                "conclusion": "success",
            }
            for name in REQUIRED_CHECKS[1:]
        ]

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _ok()

        with patch.object(
            merge_mod, "_query_action_jobs", return_value=red_jobs
        ):
            with patch.object(merge_mod, "_run", side_effect=fake_run):
                merge_issue_branch(
                    _REPO,
                    _OWNER,
                    _REPO_NAME,
                    issue=44,
                    pr_head_sha=_SHA,
                    issue_branch="baton/v2-daemon-44",
                    feature_branch=_FEATURE,
                    poll_interval=0,
                    timeout=1,
                )

        label_cmds = [c for c in calls if "agent-merged" in " ".join(c)]
        assert not label_cmds, "Must NOT add agent-merged label when CI is RED"


# ---------------------------------------------------------------------------
# Dependency-order merge
# ---------------------------------------------------------------------------


class TestDependencyOrderMerge:
    """Tests for merging a list of issues in dependency order."""

    def test_merge_list_processes_issues_in_given_order(self) -> None:
        """A list of issues is merged in the exact order provided."""
        merge_order: list[int] = []
        issues = [42, 43, 44]

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            if "merge" in cmd and "git" in cmd:
                for tok in cmd:
                    if tok.startswith("baton/"):
                        last_part = tok.rsplit("-", 1)[-1]
                        try:
                            merge_order.append(int(last_part))
                        except ValueError:
                            pass
            return _ok()

        branches = [f"baton/v2-daemon-{n}" for n in issues]
        shas = [f"sha{n}" for n in issues]

        from baton_harness.chain.merge import merge_issue_branches

        with patch.object(
            merge_mod,
            "_query_action_jobs",
            return_value=_all_required_success(),
        ):
            with patch.object(merge_mod, "_run", side_effect=fake_run):
                merge_issue_branches(
                    _REPO,
                    _OWNER,
                    _REPO_NAME,
                    issues=issues,
                    pr_head_shas=shas,
                    issue_branches=branches,
                    feature_branch=_FEATURE,
                    poll_interval=0,
                    timeout=1,
                )

        assert merge_order == issues, (
            f"Expected merge order {issues}, got {merge_order}"
        )

    def test_stops_on_red_ci_for_one_issue_in_list(self) -> None:
        """Stops processing the list if one issue has RED CI."""
        from baton_harness.chain.merge import merge_issue_branches

        call_num = 0

        def fake_query(
            owner: str, repo: str, sha: str, **kwargs: object
        ) -> list[dict[str, str | None]]:
            nonlocal call_num
            call_num += 1
            # sha43 gets a failure on the first required check.
            if sha == "sha43":
                return [
                    {
                        "name": REQUIRED_CHECKS[0],
                        "status": "completed",
                        "conclusion": "failure",
                    },
                ] + [
                    {
                        "name": name,
                        "status": "completed",
                        "conclusion": "success",
                    }
                    for name in REQUIRED_CHECKS[1:]
                ]
            return _all_required_success()

        with patch.object(
            merge_mod, "_query_action_jobs", side_effect=fake_query
        ):
            with patch.object(merge_mod, "_run", return_value=_ok()):
                outcomes = merge_issue_branches(
                    _REPO,
                    _OWNER,
                    _REPO_NAME,
                    issues=[42, 43, 44],
                    pr_head_shas=["sha42", "sha43", "sha44"],
                    issue_branches=[
                        "baton/v2-daemon-42",
                        "baton/v2-daemon-43",
                        "baton/v2-daemon-44",
                    ],
                    feature_branch=_FEATURE,
                    poll_interval=0,
                    timeout=1,
                )

        assert outcomes[42] == MergeOutcome.MERGED
        assert outcomes[43] == MergeOutcome.CI_FAILED
        # Issue 44 should not appear (processing stopped after 43's failure).
        assert 44 not in outcomes


# ---------------------------------------------------------------------------
# FIX 1 + FIX 2: unrecognized conclusions must not yield GREEN
# ---------------------------------------------------------------------------


class TestUnrecognizedConclusionNotGreen:
    """Guard FIX 1/FIX 2: completed job with unrecognised conclusion.

    A required job that is ``status: completed`` but whose ``conclusion``
    is outside both the pass set {success, neutral, skipped} and the known
    failing set must NEVER yield GREEN.  It must be treated as NOT-YET
    (polling continues until deadline → non-green on timeout).
    """

    def test_startup_failure_conclusion_is_not_green(self) -> None:
        """A required job completed with 'startup_failure' → NOT green.

        'startup_failure' is outside the failing set but also outside the
        pass set.  The poller must treat it as NOT-YET (no vacuous pass).
        """
        jobs = [
            {
                "name": REQUIRED_CHECKS[0],
                "status": "completed",
                "conclusion": "startup_failure",
            },
        ] + [
            {
                "name": name,
                "status": "completed",
                "conclusion": "success",
            }
            for name in REQUIRED_CHECKS[1:]
        ]

        with patch.object(merge_mod, "_query_action_jobs", return_value=jobs):
            result = evaluate_ci(
                _OWNER,
                _REPO_NAME,
                _SHA,
                poll_interval=0,
                timeout=0,  # immediate timeout → non-green
            )

        assert result != CiResult.GREEN, (
            "conclusion='startup_failure' must NEVER yield GREEN"
        )

    def test_null_conclusion_on_completed_job_is_not_green(self) -> None:
        """A required job completed with null conclusion → NOT green.

        A null conclusion on a completed job is unrecognised and must
        not pass vacuously.
        """
        jobs = [
            {
                "name": REQUIRED_CHECKS[0],
                "status": "completed",
                "conclusion": None,
            },
        ] + [
            {
                "name": name,
                "status": "completed",
                "conclusion": "success",
            }
            for name in REQUIRED_CHECKS[1:]
        ]

        with patch.object(merge_mod, "_query_action_jobs", return_value=jobs):
            result = evaluate_ci(
                _OWNER,
                _REPO_NAME,
                _SHA,
                poll_interval=0,
                timeout=0,
            )

        assert result != CiResult.GREEN, (
            "conclusion=null on a completed job must NEVER yield GREEN"
        )

    def test_stale_conclusion_is_not_green(self) -> None:
        """A required job completed with 'stale' → NOT green."""
        jobs = [
            {
                "name": REQUIRED_CHECKS[0],
                "status": "completed",
                "conclusion": "stale",
            },
        ] + [
            {
                "name": name,
                "status": "completed",
                "conclusion": "success",
            }
            for name in REQUIRED_CHECKS[1:]
        ]

        with patch.object(merge_mod, "_query_action_jobs", return_value=jobs):
            result = evaluate_ci(
                _OWNER,
                _REPO_NAME,
                _SHA,
                poll_interval=0,
                timeout=0,
            )

        assert result != CiResult.GREEN, (
            "conclusion='stale' must NEVER yield GREEN"
        )

    def test_unrecognized_conclusion_does_not_merge(self) -> None:
        """merge_issue_branch does NOT merge when conclusion unrecognised."""
        calls: list[list[str]] = []
        jobs = [
            {
                "name": REQUIRED_CHECKS[0],
                "status": "completed",
                "conclusion": "startup_failure",
            },
        ] + [
            {
                "name": name,
                "status": "completed",
                "conclusion": "success",
            }
            for name in REQUIRED_CHECKS[1:]
        ]

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _ok()

        with patch.object(merge_mod, "_query_action_jobs", return_value=jobs):
            with patch.object(merge_mod, "_run", side_effect=fake_run):
                outcome = merge_issue_branch(
                    _REPO,
                    _OWNER,
                    _REPO_NAME,
                    issue=44,
                    pr_head_sha=_SHA,
                    issue_branch="baton/v2-daemon-44",
                    feature_branch=_FEATURE,
                    poll_interval=0,
                    timeout=0,
                )

        merge_cmds = [c for c in calls if "merge" in c and "git" in c]
        assert not merge_cmds, "Must NOT merge when conclusion is unrecognised"
        assert outcome != MergeOutcome.MERGED, (
            "MergeOutcome must not be MERGED on unrecognised conclusion"
        )


# ---------------------------------------------------------------------------
# FIX 4: merge conflict aborts cleanly
# ---------------------------------------------------------------------------


class TestMergeConflictAbortsCleanly:
    """Guard FIX 4: a conflicted merge must abort, not leave repo dirty."""

    def test_merge_abort_issued_on_merge_failure(self) -> None:
        """On ``git merge --no-ff`` conflict, ``git merge --abort`` is called.

        The repo must NOT be left mid-merge (MERGE_HEAD / conflicted index)
        after a merge failure.
        """
        calls: list[list[str]] = []

        def fake_run(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            if "checkout" in cmd:
                return _ok()
            if "merge" in cmd and "--no-ff" in cmd:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=1,
                    stdout="",
                    stderr="CONFLICT (content): Merge conflict",
                )
            return _ok()

        with patch.object(
            merge_mod,
            "_query_action_jobs",
            return_value=_all_required_success(),
        ):
            with patch.object(merge_mod, "_run", side_effect=fake_run):
                outcome = merge_issue_branch(
                    _REPO,
                    _OWNER,
                    _REPO_NAME,
                    issue=44,
                    pr_head_sha=_SHA,
                    issue_branch="baton/v2-daemon-44",
                    feature_branch=_FEATURE,
                    poll_interval=0,
                    timeout=1,
                )

        abort_cmds = [
            c for c in calls if "--abort" in c or "abort" in " ".join(c)
        ]
        assert abort_cmds, (
            "git merge --abort must be issued after a merge conflict"
        )
        assert outcome != MergeOutcome.MERGED, (
            "A conflicted merge must NOT return MERGED"
        )

    def test_non_merged_status_returned_on_conflict(self) -> None:
        """A non-merged result is returned; no uncaught exception escapes."""

        def fake_run(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            if "checkout" in cmd:
                return _ok()
            if "merge" in cmd and "--no-ff" in cmd:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=1,
                    stdout="",
                    stderr="CONFLICT (content): Merge conflict",
                )
            return _ok()

        with patch.object(
            merge_mod,
            "_query_action_jobs",
            return_value=_all_required_success(),
        ):
            with patch.object(merge_mod, "_run", side_effect=fake_run):
                outcome = merge_issue_branch(
                    _REPO,
                    _OWNER,
                    _REPO_NAME,
                    issue=44,
                    pr_head_sha=_SHA,
                    issue_branch="baton/v2-daemon-44",
                    feature_branch=_FEATURE,
                    poll_interval=0,
                    timeout=1,
                )

        assert outcome != MergeOutcome.MERGED


# ---------------------------------------------------------------------------
# FIX 5: provenance persistence failure must be surfaced
# ---------------------------------------------------------------------------


class TestProvenancePersistenceFailureSurfaced:
    """Guard FIX 5: label/comment failure must be surfaced, not swallowed."""

    def test_merge_still_reported_merged_when_label_fails(self) -> None:
        """The merge commit stands even when the label write fails.

        The merge has already happened — reverting it would be worse.
        The outcome must still signal MERGED, but provenance_persisted
        must be False (or a warning must be recorded).
        """

        def fake_run(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            if "edit" in cmd and "agent-merged" in " ".join(cmd):
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=1,
                    stdout="",
                    stderr="gh: label write failed",
                )
            if "comment" in cmd:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=1,
                    stdout="",
                    stderr="gh: comment write failed",
                )
            return _ok()

        with patch.object(
            merge_mod,
            "_query_action_jobs",
            return_value=_all_required_success(),
        ):
            with patch.object(merge_mod, "_run", side_effect=fake_run):
                result = merge_issue_branch(
                    _REPO,
                    _OWNER,
                    _REPO_NAME,
                    issue=44,
                    pr_head_sha=_SHA,
                    issue_branch="baton/v2-daemon-44",
                    feature_branch=_FEATURE,
                    poll_interval=0,
                    timeout=1,
                )

        if hasattr(result, "provenance_persisted"):
            assert result.provenance_persisted is False, (
                "provenance_persisted must be False when label write fails"
            )
        else:
            assert result == MergeOutcome.MERGED, (
                "Merge committed; outcome must be MERGED"
            )

    def test_warning_logged_when_provenance_write_fails(self) -> None:
        """A loud warning is logged when the provenance write fails."""

        def fake_run(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            if "edit" in cmd and "agent-merged" in " ".join(cmd):
                return subprocess.CompletedProcess(
                    args=cmd, returncode=1, stdout="", stderr="label fail"
                )
            if "comment" in cmd:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=1, stdout="", stderr="comment fail"
                )
            return _ok()

        with patch.object(
            merge_mod,
            "_query_action_jobs",
            return_value=_all_required_success(),
        ):
            with patch.object(merge_mod, "_run", side_effect=fake_run):
                with self._capture_warnings() as records:
                    merge_issue_branch(
                        _REPO,
                        _OWNER,
                        _REPO_NAME,
                        issue=44,
                        pr_head_sha=_SHA,
                        issue_branch="baton/v2-daemon-44",
                        feature_branch=_FEATURE,
                        poll_interval=0,
                        timeout=1,
                    )

        warning_records = [
            r
            for r in records
            if r.levelno >= logging.WARNING and "baton_harness" in r.name
        ]
        assert warning_records, (
            "A WARNING must be logged when provenance write fails"
        )

    @staticmethod
    def _capture_warnings() -> object:
        """Context manager that captures log records from the module.

        Returns:
            A context manager yielding a list of ``LogRecord`` instances
            emitted by the ``baton_harness`` logger during the block.
        """
        from collections.abc import Generator
        from contextlib import contextmanager

        @contextmanager  # type: ignore[misc]
        def _ctx() -> Generator[list[logging.LogRecord], None, None]:
            records: list[logging.LogRecord] = []

            class _Collector(logging.Handler):
                def emit(self, record: logging.LogRecord) -> None:
                    records.append(record)

            collector = _Collector()
            root = logging.getLogger("baton_harness")
            root.addHandler(collector)
            old_level = root.level
            root.setLevel(logging.DEBUG)
            try:
                yield records
            finally:
                root.removeHandler(collector)
                root.setLevel(old_level)

        return _ctx()


# ---------------------------------------------------------------------------
# Bug 2 — merge_issue_branch / evaluate_ci token-threading callgraph
#
# Required behaviour (codex P1 #154):
#   merge_issue_branch(..., installation_token: str) must accept the token
#   and forward it to evaluate_ci; evaluate_ci must forward to
#   _query_action_jobs; every subprocess.run / _run call must receive the
#   token via a per-call env dict — os.environ must NOT be mutated.
#
# Current behaviour causing FAIL:
#   - merge_issue_branch has no installation_token parameter → TypeError.
#   - evaluate_ci has no installation_token parameter → TypeError.
#   - evaluate_ci calls _query_action_jobs without forwarding any token.
#   - Subprocess env for provenance writes (gh issue) inherits ambient env.
# ---------------------------------------------------------------------------


class TestMergeIssueBranchThreadsInstallationToken:
    """RED: token-threading callgraph for merge_issue_branch.

    These tests fail until:
    1. ``merge_issue_branch`` gains an ``installation_token`` parameter.
    2. ``merge_issue_branch`` forwards the token to ``evaluate_ci``.
    3. ``evaluate_ci`` gains an ``installation_token`` parameter.
    4. ``evaluate_ci`` forwards the token to ``_query_action_jobs``.
    5. All ``_run`` / subprocess calls inside the merge-and-provenance path
       use a per-call env dict containing ``GH_TOKEN=<token>`` rather than
       mutating ``os.environ``.
    """

    def test_merge_issue_branch_accepts_installation_token_kwarg(
        self,
    ) -> None:
        """merge_issue_branch(installation_token=...) must not raise TypeError.

        The contract requires the function signature to accept
        ``installation_token`` as a keyword argument.  Currently the
        parameter is absent, so calling with it raises ``TypeError``.
        """
        with (
            patch.object(
                merge_mod,
                "_query_action_jobs",
                return_value=_all_required_success(),
            ),
            patch.object(merge_mod, "_run", return_value=_ok()),
        ):
            # Must not raise TypeError — the kwarg must exist.
            merge_issue_branch(
                _REPO,
                _OWNER,
                _REPO_NAME,
                issue=44,
                pr_head_sha=_SHA,
                issue_branch="baton/v2-daemon-44",
                feature_branch=_FEATURE,
                poll_interval=0,
                timeout=1,
                installation_token="ghs_T_MERGE",
            )

    def test_merge_issue_branch_forwards_token_to_evaluate_ci(
        self,
    ) -> None:
        """merge_issue_branch forwards installation_token to evaluate_ci.

        After ``merge_issue_branch`` accepts the token, it must pass
        ``installation_token=<token>`` to ``evaluate_ci``.  Currently
        ``evaluate_ci`` is called without the kwarg.
        """
        received: dict[str, object] = {}

        def _fake_evaluate_ci(
            owner: str,
            repo: str,
            sha: str,
            *args: object,
            **kwargs: object,
        ) -> CiResult:
            received.update(kwargs)
            return CiResult.GREEN

        with (
            patch.object(
                merge_mod, "evaluate_ci", side_effect=_fake_evaluate_ci
            ),
            patch.object(merge_mod, "_run", return_value=_ok()),
        ):
            merge_issue_branch(
                _REPO,
                _OWNER,
                _REPO_NAME,
                issue=44,
                pr_head_sha=_SHA,
                issue_branch="baton/v2-daemon-44",
                feature_branch=_FEATURE,
                poll_interval=0,
                timeout=1,
                installation_token="ghs_T_FORWARD",
            )

        assert received.get("installation_token") == "ghs_T_FORWARD", (
            "merge_issue_branch must forward installation_token to "
            f"evaluate_ci; kwargs seen: {received!r}"
        )

    def test_evaluate_ci_forwards_token_to_query_action_jobs(
        self,
    ) -> None:
        """evaluate_ci forwards installation_token to _query_action_jobs.

        When ``evaluate_ci`` gains ``installation_token``, it must pass it
        to ``_query_action_jobs``.  Currently the call is made without
        the kwarg, so the token is never forwarded to the gh subprocess.
        """
        received: dict[str, object] = {}

        def _fake_query(
            owner: str,
            repo: str,
            sha: str,
            *args: object,
            **kwargs: object,
        ) -> list[dict[str, object]]:
            received.update(kwargs)
            return _all_required_success()

        with patch.object(
            merge_mod, "_query_action_jobs", side_effect=_fake_query
        ):
            from baton_harness.chain.merge import evaluate_ci

            evaluate_ci(
                _OWNER,
                _REPO_NAME,
                _SHA,
                poll_interval=0,
                timeout=1,
                installation_token="ghs_T_QUERY",
            )

        assert received.get("installation_token") == "ghs_T_QUERY", (
            "evaluate_ci must forward installation_token to "
            f"_query_action_jobs; kwargs seen: {received!r}"
        )

    def test_merge_path_gh_calls_use_per_call_env_dict(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All _run calls during a green merge receive GH_TOKEN in env dict.

        The token must be delivered via a per-call ``env`` kwarg on each
        ``subprocess.run`` / ``_run`` call.  ``os.environ`` must NOT be
        mutated during the merge flow.

        Args:
            monkeypatch: Pytest monkeypatch fixture.
        """
        import os

        ghs_token = "ghs_T_ENV_DISCIPLINE"

        # Snapshot env BEFORE the call so we can compare after.
        env_before = dict(os.environ)

        run_env_kwargs: list[dict[str, str] | None] = []

        def _spy_run(
            cmd: list[str],
            env: dict[str, str] | None = None,
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            run_env_kwargs.append(env)
            return _ok()

        with (
            patch.object(
                merge_mod,
                "_query_action_jobs",
                return_value=_all_required_success(),
            ),
            patch.object(merge_mod, "_run", side_effect=_spy_run),
        ):
            merge_issue_branch(
                _REPO,
                _OWNER,
                _REPO_NAME,
                issue=44,
                pr_head_sha=_SHA,
                issue_branch="baton/v2-daemon-44",
                feature_branch=_FEATURE,
                poll_interval=0,
                timeout=1,
                installation_token=ghs_token,
            )

        # Every _run call that uses the token must have a non-None env dict
        # containing GH_TOKEN=ghs_token.
        # (Git commands that don't touch gh may not need the env override,
        # but at least one call — provenance writes — must carry it.)
        gh_calls_with_env = [
            env
            for env in run_env_kwargs
            if env is not None and env.get("GH_TOKEN") == ghs_token
        ]
        assert gh_calls_with_env, (
            "At least one _run call during the merge-and-provenance path "
            f"must supply GH_TOKEN={ghs_token!r} via a per-call env dict; "
            f"env kwargs seen: {run_env_kwargs!r}"
        )

        # os.environ must NOT have been mutated.
        env_after = dict(os.environ)
        assert env_after == env_before, (
            "os.environ must NOT be mutated during the merge flow; "
            f"diff: {set(env_after.items()) ^ set(env_before.items())}"
        )
