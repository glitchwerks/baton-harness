"""Unit tests for baton_harness.chain.merge.

Tests the CI-gated ``--no-ff`` merge logic.  All subprocess calls are
intercepted by patching the module-local ``_run`` seam; no live network or
``gh`` binary is required.

Design note — required-check set sourced from configuration (C-I2):
    The repo has no classic branch-protection required-check set (the API
    returns 404).  ``merge.py`` therefore takes the required-check set from
    a module constant (``REQUIRED_CHECKS``) that defaults to the three actual
    CI check names: ``Lint (ruff)``, ``Test (pytest)``, ``Type check (mypy)``.
    A future wiring to config (WORKFLOW.md or similar) is noted as TODO in
    the module.

    CRITICAL: "zero matching checks found" is NEVER green (no vacuous pass).
    If a configured required check is absent from the check-runs response,
    that is treated as NOT-YET → RED on timeout, never as passing.

Coverage:
- ``check_runs`` API shape: ``{check_runs: [{name, status, conclusion}]}``.
- All-required-success → GREEN (merge proceeds).
- ``neutral`` or ``skipped`` conclusion on a required check → PASS (green).
- A non-required failing check → still GREEN (non-required checks ignored).
- ``failure`` / ``cancelled`` / ``timed_out`` / ``action_required`` on a
  required check → RED (no merge).
- ``in_progress`` required check → poll-then-timeout → RED (``ci-timeout``).
- A configured required check ABSENT from the response → NOT-YET → RED on
  timeout (NOT vacuous green).
- ``--no-ff`` merge (not squash) into the feature branch.
- Daemon-provenance trailer present in the merge commit message.
- ``agent-merged`` label added after green merge.
- Marker comment posted on the issue after green merge.
- Dependency-order merge: a list of issues is merged in the given order.
- NEVER merges ``feature/<slug>`` → ``main`` (hard constraint guard).
- The merge target is always ``feature/<slug>``, not ``main``.
"""

from __future__ import annotations

import logging
import logging.handlers
import subprocess
from pathlib import Path
from unittest.mock import patch

import baton_harness.chain.merge as merge_mod
from baton_harness.chain.merge import (
    REQUIRED_CHECKS,
    CiResult,
    MergeOutcome,
    evaluate_ci,
    merge_issue_branch,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO = Path("/fake/repo")
_OWNER = "glitchwerks"
_REPO_NAME = "baton-harness"
_SHA = "abc123def456abc123def456abc123def456abc1"
_FEATURE = "feature/v2-daemon"


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


def _check_runs_response(
    checks: list[dict[str, str | None]],
) -> subprocess.CompletedProcess[str]:
    """Wrap a list of check dicts as a ``gh api`` check-runs response.

    Args:
        checks: List of ``{name, status, conclusion}`` dicts.  ``conclusion``
            may be ``None`` for in-progress checks.

    Returns:
        A successful ``CompletedProcess`` with JSON stdout.
    """
    import json

    payload = {"check_runs": checks}
    return _ok(json.dumps(payload))


def _all_required_success() -> list[dict[str, str | None]]:
    """Build a check-runs list where all required checks succeed.

    Returns:
        Check-run dicts for all three required checks, all completed/success.
    """
    return [
        {
            "name": name,
            "status": "completed",
            "conclusion": "success",
        }
        for name in REQUIRED_CHECKS
    ]


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
# evaluate_ci — green predicate (§3.3.1)
# ---------------------------------------------------------------------------


class TestEvaluateCiGreen:
    """Tests for the GREEN path of ``evaluate_ci``."""

    def test_all_required_success_is_green(self) -> None:
        """All required checks completed/success → GREEN."""
        checks = _all_required_success()

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            return _check_runs_response(checks)

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            result = evaluate_ci(
                _OWNER, _REPO_NAME, _SHA, poll_interval=0, timeout=1
            )

        assert result == CiResult.GREEN

    def test_neutral_conclusion_counts_as_pass(self) -> None:
        """A required check with conclusion='neutral' → GREEN (§3.3.1)."""
        checks = [
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

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            return _check_runs_response(checks)

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            result = evaluate_ci(
                _OWNER, _REPO_NAME, _SHA, poll_interval=0, timeout=1
            )

        assert result == CiResult.GREEN

    def test_skipped_conclusion_counts_as_pass(self) -> None:
        """A required check with conclusion='skipped' → GREEN (§3.3.1)."""
        checks = [
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

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            return _check_runs_response(checks)

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            result = evaluate_ci(
                _OWNER, _REPO_NAME, _SHA, poll_interval=0, timeout=1
            )

        assert result == CiResult.GREEN

    def test_non_required_failure_does_not_block_green(self) -> None:
        """A non-required failing check is ignored → GREEN (§3.3.1)."""
        checks = _all_required_success() + [
            {
                "name": "Some optional check",
                "status": "completed",
                "conclusion": "failure",
            }
        ]

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            return _check_runs_response(checks)

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            result = evaluate_ci(
                _OWNER, _REPO_NAME, _SHA, poll_interval=0, timeout=1
            )

        assert result == CiResult.GREEN

    def test_non_required_check_in_progress_ignored(self) -> None:
        """An in-progress non-required check is ignored → GREEN."""
        checks = _all_required_success() + [
            {
                "name": "Optional flaky check",
                "status": "in_progress",
                "conclusion": None,
            }
        ]

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            return _check_runs_response(checks)

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            result = evaluate_ci(
                _OWNER, _REPO_NAME, _SHA, poll_interval=0, timeout=1
            )

        assert result == CiResult.GREEN


class TestEvaluateCiRed:
    """Tests for the RED path of ``evaluate_ci``."""

    def test_failure_on_required_check_is_red(self) -> None:
        """A required check with conclusion='failure' → RED."""
        checks = [
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
            return _check_runs_response(checks)

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            result = evaluate_ci(
                _OWNER, _REPO_NAME, _SHA, poll_interval=0, timeout=1
            )

        assert result == CiResult.RED

    def test_cancelled_on_required_check_is_red(self) -> None:
        """A required check with conclusion='cancelled' → RED."""
        checks = [
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

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            return _check_runs_response(checks)

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            result = evaluate_ci(
                _OWNER, _REPO_NAME, _SHA, poll_interval=0, timeout=1
            )

        assert result == CiResult.RED

    def test_timed_out_on_required_check_is_red(self) -> None:
        """A required check with conclusion='timed_out' → RED."""
        checks = [
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

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            return _check_runs_response(checks)

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            result = evaluate_ci(
                _OWNER, _REPO_NAME, _SHA, poll_interval=0, timeout=1
            )

        assert result == CiResult.RED

    def test_action_required_on_required_check_is_red(self) -> None:
        """A required check with conclusion='action_required' → RED."""
        checks = [
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

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            return _check_runs_response(checks)

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            result = evaluate_ci(
                _OWNER, _REPO_NAME, _SHA, poll_interval=0, timeout=1
            )

        assert result == CiResult.RED


class TestEvaluateCiPollingAndTimeout:
    """Tests for polling and timeout behaviour of ``evaluate_ci``."""

    def test_in_progress_required_check_polls_then_times_out_as_red(
        self,
    ) -> None:
        """An in-progress required check that never completes → not green.

        The function must poll up to the timeout and then return either
        ``CiResult.RED`` or ``CiResult.TIMEOUT`` (both are non-green /
        ci-timeout semantics), never blocking forever.
        """

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            # Always return in_progress for required checks → never green
            checks = [
                {
                    "name": name,
                    "status": "in_progress",
                    "conclusion": None,
                }
                for name in REQUIRED_CHECKS
            ]
            return _check_runs_response(checks)

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            result = evaluate_ci(
                _OWNER,
                _REPO_NAME,
                _SHA,
                poll_interval=0,
                timeout=0,  # immediate timeout
            )

        # Must NOT be green (ci-timeout semantics)
        assert result != CiResult.GREEN, (
            "An in-progress check that never completes must not be GREEN"
        )

    def test_absent_required_check_is_not_vacuous_green(self) -> None:
        """A configured required check absent from response → NOT green.

        CRITICAL: Zero matching checks found is NEVER vacuously green.
        An absent required check → NOT-YET → non-green on timeout.
        """

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            # Return only irrelevant checks — none are required
            checks = [
                {
                    "name": "Some unrelated check",
                    "status": "completed",
                    "conclusion": "success",
                }
            ]
            return _check_runs_response(checks)

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            result = evaluate_ci(
                _OWNER,
                _REPO_NAME,
                _SHA,
                poll_interval=0,
                timeout=0,  # immediate timeout
            )

        # MUST NOT be green — absent required check = timeout, not vacuous pass
        assert result != CiResult.GREEN, (
            "An absent required check must NEVER produce a GREEN result"
        )

    def test_empty_check_runs_is_not_vacuous_green(self) -> None:
        """Empty check-runs response → NOT green (no vacuous pass).

        No checks at all = every required check is absent = NOT-YET → not
        green on timeout.
        """

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            return _check_runs_response([])

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            result = evaluate_ci(
                _OWNER,
                _REPO_NAME,
                _SHA,
                poll_interval=0,
                timeout=0,
            )

        assert result != CiResult.GREEN, (
            "An empty check-runs response must NEVER produce GREEN"
        )

    def test_eventually_green_after_polling(self) -> None:
        """Checks that are in_progress then complete → GREEN after polling."""
        call_count = 0

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: all in_progress
                checks = [
                    {
                        "name": name,
                        "status": "in_progress",
                        "conclusion": None,
                    }
                    for name in REQUIRED_CHECKS
                ]
            else:
                # Subsequent calls: all success
                checks = [
                    {
                        "name": name,
                        "status": "completed",
                        "conclusion": "success",
                    }
                    for name in REQUIRED_CHECKS
                ]
            return _check_runs_response(checks)

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            result = evaluate_ci(
                _OWNER,
                _REPO_NAME,
                _SHA,
                poll_interval=0,
                timeout=60,  # allow polling
            )

        assert result == CiResult.GREEN
        assert call_count >= 2, "Must have polled at least twice"

    def test_queued_required_check_polls_not_immediately_red(self) -> None:
        """A queued required check is NOT-YET, not immediately RED."""
        call_count = 0

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                checks = [
                    {
                        "name": name,
                        "status": "queued",
                        "conclusion": None,
                    }
                    for name in REQUIRED_CHECKS
                ]
            else:
                checks = [
                    {
                        "name": name,
                        "status": "completed",
                        "conclusion": "success",
                    }
                    for name in REQUIRED_CHECKS
                ]
            return _check_runs_response(checks)

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            result = evaluate_ci(
                _OWNER,
                _REPO_NAME,
                _SHA,
                poll_interval=0,
                timeout=60,
            )

        # Must have polled (not immediately RED on queued)
        assert result == CiResult.GREEN
        assert call_count >= 2

    def test_checks_correct_api_endpoint(self) -> None:
        """Queries the check-runs endpoint for the correct SHA."""
        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _check_runs_response(_all_required_success())

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            evaluate_ci(_OWNER, _REPO_NAME, _SHA, poll_interval=0, timeout=1)

        assert any(_SHA in " ".join(cmd) for cmd in calls), (
            "The SHA must appear in the check-runs API call"
        )
        assert any("check-runs" in " ".join(cmd) for cmd in calls), (
            "The check-runs endpoint must be called"
        )


# ---------------------------------------------------------------------------
# merge_issue_branch — the --no-ff merge
# ---------------------------------------------------------------------------


class TestMergeIssueBranch:
    """Tests for ``merge_issue_branch`` performing the gated --no-ff merge."""

    def test_uses_no_ff_flag(self) -> None:
        """Merge command uses ``--no-ff``, not squash."""
        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            if "check-runs" in " ".join(cmd):
                return _check_runs_response(_all_required_success())
            return _ok()

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
        """Merges the per-issue branch into ``feature/<slug>`` (not main).

        The merge is done via ``git checkout <feature_branch>`` followed by
        ``git merge --no-ff <issue_branch>``.  The feature branch is thus the
        merge target via HEAD checkout, not an explicit argument to merge.
        """
        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            if "check-runs" in " ".join(cmd):
                return _check_runs_response(_all_required_success())
            return _ok()

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

        # The feature branch appears in the git checkout command (pre-merge).
        checkout_cmds = [c for c in calls if "checkout" in c]
        assert any(_FEATURE in c for c in checkout_cmds), (
            f"Must checkout INTO the feature branch before merging."
            f" Calls: {calls}"
        )
        # The git merge command must include the issue branch, not main.
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
            if "check-runs" in " ".join(cmd):
                return _check_runs_response(_all_required_success())
            return _ok()

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

    def test_provenance_trailer_in_merge_message(self) -> None:
        """Merge commit message carries the daemon-provenance trailer."""
        captured_messages: list[str] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            if "check-runs" in " ".join(cmd):
                return _check_runs_response(_all_required_success())
            # Capture -m flag content
            if "merge" in cmd and "-m" in cmd:
                m_idx = cmd.index("-m")
                if m_idx + 1 < len(cmd):
                    captured_messages.append(cmd[m_idx + 1])
            return _ok()

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
        # Must contain the structured trailer (§11.5 / B-I2)
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

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            if "check-runs" in " ".join(cmd):
                return _check_runs_response(_all_required_success())
            return _ok()

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

        assert outcome == MergeOutcome.MERGED

    def test_returns_red_outcome_when_ci_fails(self) -> None:
        """Returns ``MergeOutcome.CI_FAILED`` when CI is RED."""
        checks = [
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
            if "check-runs" in " ".join(cmd):
                return _check_runs_response(checks)
            return _ok()

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

        assert outcome == MergeOutcome.CI_FAILED

    def test_returns_timeout_outcome_when_ci_never_completes(self) -> None:
        """Returns ``MergeOutcome.CI_TIMEOUT`` when CI never completes."""

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            if "check-runs" in " ".join(cmd):
                checks = [
                    {
                        "name": name,
                        "status": "in_progress",
                        "conclusion": None,
                    }
                    for name in REQUIRED_CHECKS
                ]
                return _check_runs_response(checks)
            return _ok()

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
                timeout=0,  # immediate timeout
            )

        assert outcome == MergeOutcome.CI_TIMEOUT

    def test_no_merge_command_issued_when_ci_fails(self) -> None:
        """No ``git merge`` is called when CI is RED (do not merge on red)."""
        calls: list[list[str]] = []
        checks = [
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
            if "check-runs" in " ".join(cmd):
                return _check_runs_response(checks)
            return _ok()

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


class TestProvenancePersistence:
    """Tests for persisting the CI-green-at-merge fact (B-I2 / §11.5)."""

    def test_agent_merged_label_added_after_green_merge(self) -> None:
        """``agent-merged`` label is added to the issue after green merge."""
        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            if "check-runs" in " ".join(cmd):
                return _check_runs_response(_all_required_success())
            return _ok()

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

        # Should have called gh issue edit to add agent-merged
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
            if "check-runs" in " ".join(cmd):
                return _check_runs_response(_all_required_success())
            return _ok()

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

        # Should have called gh issue comment or similar
        comment_cmds = [
            c for c in calls if "gh" in c and "comment" in " ".join(c)
        ]
        assert comment_cmds, (
            "Expected a gh comment command for the CI-green marker"
        )

    def test_no_label_or_comment_when_ci_fails(self) -> None:
        """No ``agent-merged`` label or marker comment when CI is RED."""
        calls: list[list[str]] = []
        checks = [
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
            if "check-runs" in " ".join(cmd):
                return _check_runs_response(checks)
            return _ok()

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
            if "check-runs" in " ".join(cmd):
                return _check_runs_response(_all_required_success())
            if "merge" in cmd and "git" in cmd:
                # Find the issue branch token (baton/... pattern)
                for tok in cmd:
                    if tok.startswith("baton/"):
                        # branch is like "baton/v2-daemon-42" — last segment
                        # after the final "-" is the issue number.
                        last_part = tok.rsplit("-", 1)[-1]
                        try:
                            merge_order.append(int(last_part))
                        except ValueError:
                            pass
            return _ok()

        branches = [f"baton/v2-daemon-{n}" for n in issues]
        shas = [f"sha{n}" for n in issues]

        from baton_harness.chain.merge import merge_issue_branches

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

        merge_count = 0

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            nonlocal merge_count
            cmd_str = " ".join(cmd)
            if "check-runs" in cmd_str:
                # Return failure for sha43
                if "sha43" in cmd_str:
                    checks = [
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
                    return _check_runs_response(checks)
                return _check_runs_response(_all_required_success())
            if "merge" in cmd and "git" in cmd:
                merge_count += 1
            return _ok()

        with patch.object(merge_mod, "_run", side_effect=fake_run):
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

        # Issue 42 should merge (green), issue 43 should fail (red)
        assert outcomes[42] == MergeOutcome.MERGED
        assert outcomes[43] == MergeOutcome.CI_FAILED


# ---------------------------------------------------------------------------
# FIX 1 + FIX 2: unrecognized conclusions must not yield GREEN
# ---------------------------------------------------------------------------


class TestUnrecognizedConclusionNotGreen:
    """Guard FIX 1/FIX 2: completed check with unrecognised conclusion.

    A required check that is ``status: completed`` but whose ``conclusion``
    is outside both the pass set {success, neutral, skipped} and the known
    failing set must NEVER yield GREEN.  It must be treated as NOT-YET
    (polling continues until deadline → non-green on timeout).
    """

    def test_startup_failure_conclusion_is_not_green(self) -> None:
        """A required check completed with 'startup_failure' → NOT green.

        'startup_failure' is outside the failing set but also outside the
        pass set.  The poller must treat it as NOT-YET (no vacuous pass).
        """
        checks = [
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

        def fake_run(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            return _check_runs_response(checks)

        with patch.object(merge_mod, "_run", side_effect=fake_run):
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

    def test_null_conclusion_on_completed_check_is_not_green(self) -> None:
        """A required check completed with null conclusion → NOT green.

        A null conclusion on a completed check is unrecognised and must
        not pass vacuously.
        """
        checks = [
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

        def fake_run(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            return _check_runs_response(checks)

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            result = evaluate_ci(
                _OWNER,
                _REPO_NAME,
                _SHA,
                poll_interval=0,
                timeout=0,
            )

        assert result != CiResult.GREEN, (
            "conclusion=null on a completed check must NEVER yield GREEN"
        )

    def test_stale_conclusion_is_not_green(self) -> None:
        """A required check completed with 'stale' → NOT green."""
        checks = [
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

        def fake_run(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            return _check_runs_response(checks)

        with patch.object(merge_mod, "_run", side_effect=fake_run):
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
        checks = [
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

        def fake_run(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            if "check-runs" in " ".join(cmd):
                return _check_runs_response(checks)
            return _ok()

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
            if "check-runs" in " ".join(cmd):
                return _check_runs_response(_all_required_success())
            if "checkout" in cmd:
                return _ok()
            if "merge" in cmd and "--no-ff" in cmd:
                # Simulate a conflict / non-zero merge
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=1,
                    stdout="",
                    stderr="CONFLICT (content): Merge conflict",
                )
            # merge --abort or other
            return _ok()

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
            if "check-runs" in " ".join(cmd):
                return _check_runs_response(_all_required_success())
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

        with patch.object(merge_mod, "_run", side_effect=fake_run):
            # Must not raise — must return a structured non-merged outcome
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
            if "check-runs" in " ".join(cmd):
                return _check_runs_response(_all_required_success())
            if "edit" in cmd and "agent-merged" in " ".join(cmd):
                # Label write fails
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

        # The result must indicate provenance was not persisted.
        # Depending on the implementation, this is either a named tuple /
        # dataclass with provenance_persisted=False, or the outcome itself
        # has an attribute. We check for the attribute if present.
        if hasattr(result, "provenance_persisted"):
            assert result.provenance_persisted is False, (
                "provenance_persisted must be False when label write fails"
            )
        else:
            # If a plain MergeOutcome enum is returned, it should still be
            # MERGED (the merge happened). The important check is the warning
            # path — tested separately via logging below.
            assert result == MergeOutcome.MERGED, (
                "Merge committed; outcome must be MERGED"
            )

    def test_warning_logged_when_provenance_write_fails(self) -> None:
        """A loud warning is logged when the provenance write fails."""
        import logging

        def fake_run(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            if "check-runs" in " ".join(cmd):
                return _check_runs_response(_all_required_success())
            if "edit" in cmd and "agent-merged" in " ".join(cmd):
                return subprocess.CompletedProcess(
                    args=cmd, returncode=1, stdout="", stderr="label fail"
                )
            if "comment" in cmd:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=1, stdout="", stderr="comment fail"
                )
            return _ok()

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

        # At least one WARNING (or higher) must have been emitted from
        # the baton_harness.chain.merge logger.
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
