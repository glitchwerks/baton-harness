"""Unit tests for baton_harness.chain.recovery.

Tests provenance-hardened crash / unblock recovery reconstruction.
All subprocess calls are intercepted by patching the module-local
``_run`` seam; no live network, ``gh`` binary, or ``git`` binary is
required.

Coverage:
- ``done`` ONLY from provenance-trailer merge commit + ``agent-merged``
  label.
- A human merge commit naming issue N (no trailer) is NOT ``done``
  (B-I2 invariant).
- ``parked_seed`` from ``blocked`` label.
- Rule 3a: ``agent-done`` + open PR + no daemon-provenance merge commit
  → ``ci_gate_reentry`` (NOT ``done``).
- Rule 3a: provenance merge commit present BUT missing ``agent-merged``
  label → ``ci_gate_reentry`` (NOT ``done``).
- Rule 3b: ``agent-in-progress`` orphan → ``redispatch``.
- Membership issue with no special label → neither set (fresh frontier).
- ``RecoveryResult`` exposes the four expected sets.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import baton_harness.chain.recovery as recovery_mod
from baton_harness.chain.recovery import RecoveryResult, reconstruct

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO = Path("/fake/repo")
_OWNER = "glitchwerks"
_REPO_NAME = "baton-harness"
_FEATURE = "feature/my-milestone"


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    """Return a successful CompletedProcess."""
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=""
    )


def _fail(stderr: str = "error") -> subprocess.CompletedProcess[str]:
    """Return a failed CompletedProcess."""
    return subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr=stderr
    )


def _labels_json(*labels: str) -> str:
    """Build a ``gh issue view --json labels`` response."""
    return json.dumps({"labels": [{"name": lbl} for lbl in labels]})


def _pr_list_json(*branches: str) -> str:
    """Build a ``gh pr list --json ...`` response."""
    return json.dumps(
        [
            {"number": i + 1, "headRefName": branch}
            for i, branch in enumerate(branches)
        ]
    )


def _git_log_with_trailer(issue: int) -> str:
    """Return git log output with a daemon-provenance trailer for issue N."""
    sha = "aabbccdd" * 5
    body = (
        f"Merge branch 'baton/my-milestone-{issue}'"
        f" into feature/my-milestone\n\n"
        f"Baton-Harness-Merge: issue-{issue} ci=green"
    )
    # Format: sha + unit-separator + body + record-separator
    return f"{sha}\x1f{body}\x1e"


def _git_log_human_merge(issue: int) -> str:
    """Return git log output WITHOUT daemon-provenance trailer."""
    sha = "deadbeef" * 5
    body = (
        f"Merge branch 'baton/my-milestone-{issue}' into feature/my-milestone"
    )
    return f"{sha}\x1f{body}\x1e"


def _git_log_empty() -> str:
    """Return empty git log output (no merge commits)."""
    return ""


# ---------------------------------------------------------------------------
# RecoveryResult dataclass
# ---------------------------------------------------------------------------


def test_recovery_result_is_frozen_dataclass() -> None:
    """RecoveryResult is a frozen dataclass with the four expected sets."""
    rr = RecoveryResult(
        done={1},
        parked_seed={2},
        ci_gate_reentry={3},
        redispatch={4},
    )
    assert rr.done == {1}
    assert rr.parked_seed == {2}
    assert rr.ci_gate_reentry == {3}
    assert rr.redispatch == {4}
    # frozen — mutation must raise
    with pytest.raises((AttributeError, TypeError)):
        rr.done = {99}  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Rule 1: done = provenance merge + agent-merged label
# ---------------------------------------------------------------------------


def test_done_requires_provenance_trailer_and_agent_merged_label() -> None:
    """Issue is done only when merge trailer + agent-merged label present."""
    membership = frozenset({10})

    def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        cmd_str = " ".join(cmd)
        if "log" in cmd_str and "--merges" in cmd_str:
            return _ok(_git_log_with_trailer(10))
        if "issue" in cmd_str and "view" in cmd_str and "10" in cmd_str:
            return _ok(_labels_json("agent-merged"))
        if "pr" in cmd_str and "list" in cmd_str:
            return _ok(_pr_list_json())
        return _ok("{}")

    with patch.object(recovery_mod, "_run", side_effect=fake_run):
        result = reconstruct(_REPO, _OWNER, _REPO_NAME, _FEATURE, membership)

    assert 10 in result.done
    assert 10 not in result.parked_seed
    assert 10 not in result.ci_gate_reentry
    assert 10 not in result.redispatch


# ---------------------------------------------------------------------------
# B-I2: human merge (no trailer) is NOT done
# ---------------------------------------------------------------------------


def test_human_merge_without_trailer_is_not_done() -> None:
    """A human merge commit without daemon-provenance trailer is NOT done."""
    membership = frozenset({20})

    def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        cmd_str = " ".join(cmd)
        if "log" in cmd_str and "--merges" in cmd_str:
            # Human merge: no Baton-Harness-Merge trailer
            return _ok(_git_log_human_merge(20))
        if "issue" in cmd_str and "view" in cmd_str and "20" in cmd_str:
            # Labels: NOT agent-merged (human merge doesn't set it)
            return _ok(_labels_json("agent-done"))
        if "pr" in cmd_str and "list" in cmd_str:
            return _ok(_pr_list_json("baton/my-milestone-20"))
        return _ok("{}")

    with patch.object(recovery_mod, "_run", side_effect=fake_run):
        result = reconstruct(_REPO, _OWNER, _REPO_NAME, _FEATURE, membership)

    # The human merge must NOT be treated as done.
    assert 20 not in result.done


# ---------------------------------------------------------------------------
# Rule 2: parked_seed from blocked label
# ---------------------------------------------------------------------------


def test_blocked_label_yields_parked_seed() -> None:
    """Issue carrying ``blocked`` label goes into parked_seed."""
    membership = frozenset({30})

    def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        cmd_str = " ".join(cmd)
        if "log" in cmd_str and "--merges" in cmd_str:
            return _ok(_git_log_empty())
        if "issue" in cmd_str and "view" in cmd_str and "30" in cmd_str:
            return _ok(_labels_json("blocked"))
        if "pr" in cmd_str and "list" in cmd_str:
            return _ok(_pr_list_json())
        return _ok("{}")

    with patch.object(recovery_mod, "_run", side_effect=fake_run):
        result = reconstruct(_REPO, _OWNER, _REPO_NAME, _FEATURE, membership)

    assert 30 in result.parked_seed
    assert 30 not in result.done
    assert 30 not in result.ci_gate_reentry
    assert 30 not in result.redispatch


# ---------------------------------------------------------------------------
# Rule 3a: agent-done + open PR + no provenance merge → ci_gate_reentry
# ---------------------------------------------------------------------------


def test_agent_done_with_open_pr_no_merge_is_ci_gate_reentry() -> None:
    """agent-done + open PR but no daemon-merge → ci_gate_reentry (rule 3a)."""
    membership = frozenset({40})

    def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        cmd_str = " ".join(cmd)
        if "log" in cmd_str and "--merges" in cmd_str:
            return _ok(_git_log_empty())  # No merge at all
        if "issue" in cmd_str and "view" in cmd_str and "40" in cmd_str:
            return _ok(_labels_json("agent-done"))
        if "pr" in cmd_str and "list" in cmd_str:
            return _ok(_pr_list_json("baton/my-milestone-40"))
        return _ok("{}")

    with patch.object(recovery_mod, "_run", side_effect=fake_run):
        result = reconstruct(_REPO, _OWNER, _REPO_NAME, _FEATURE, membership)

    assert 40 in result.ci_gate_reentry
    assert 40 not in result.done
    assert 40 not in result.parked_seed
    assert 40 not in result.redispatch


def test_provenance_merge_no_label_is_ci_gate_reentry() -> None:
    """Daemon merge present but missing agent-merged → ci_gate_reentry."""
    membership = frozenset({50})

    def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        cmd_str = " ".join(cmd)
        if "log" in cmd_str and "--merges" in cmd_str:
            # Merge commit with trailer present
            return _ok(_git_log_with_trailer(50))
        if "issue" in cmd_str and "view" in cmd_str and "50" in cmd_str:
            # No agent-merged label — daemon died before writing the marker.
            return _ok(_labels_json("agent-done"))
        if "pr" in cmd_str and "list" in cmd_str:
            return _ok(_pr_list_json("baton/my-milestone-50"))
        return _ok("{}")

    with patch.object(recovery_mod, "_run", side_effect=fake_run):
        result = reconstruct(_REPO, _OWNER, _REPO_NAME, _FEATURE, membership)

    assert 50 in result.ci_gate_reentry
    assert 50 not in result.done


# ---------------------------------------------------------------------------
# Rule 3b: agent-in-progress orphan → redispatch
# ---------------------------------------------------------------------------


def test_agent_in_progress_orphan_is_redispatch() -> None:
    """agent-in-progress with no other signals → redispatch (rule 3b)."""
    membership = frozenset({60})

    def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        cmd_str = " ".join(cmd)
        if "log" in cmd_str and "--merges" in cmd_str:
            return _ok(_git_log_empty())
        if "issue" in cmd_str and "view" in cmd_str and "60" in cmd_str:
            return _ok(_labels_json("agent-in-progress"))
        if "pr" in cmd_str and "list" in cmd_str:
            return _ok(_pr_list_json())
        return _ok("{}")

    with patch.object(recovery_mod, "_run", side_effect=fake_run):
        result = reconstruct(_REPO, _OWNER, _REPO_NAME, _FEATURE, membership)

    assert 60 in result.redispatch
    assert 60 not in result.done
    assert 60 not in result.parked_seed
    assert 60 not in result.ci_gate_reentry


# ---------------------------------------------------------------------------
# No special label → issue appears in no set (fresh frontier)
# ---------------------------------------------------------------------------


def test_fresh_issue_in_no_set() -> None:
    """Issue with no special label belongs to none of the four sets."""
    membership = frozenset({70})

    def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        cmd_str = " ".join(cmd)
        if "log" in cmd_str and "--merges" in cmd_str:
            return _ok(_git_log_empty())
        if "issue" in cmd_str and "view" in cmd_str and "70" in cmd_str:
            return _ok(_labels_json("agent-ready"))
        if "pr" in cmd_str and "list" in cmd_str:
            return _ok(_pr_list_json())
        return _ok("{}")

    with patch.object(recovery_mod, "_run", side_effect=fake_run):
        result = reconstruct(_REPO, _OWNER, _REPO_NAME, _FEATURE, membership)

    assert 70 not in result.done
    assert 70 not in result.parked_seed
    assert 70 not in result.ci_gate_reentry
    assert 70 not in result.redispatch


# ---------------------------------------------------------------------------
# Multiple members with mixed states
# ---------------------------------------------------------------------------


def test_mixed_membership_correct_classification() -> None:
    """Multiple issues are classified independently and correctly."""
    # 81: done, 82: blocked (parked), 83: ci_gate_reentry, 84: redispatch
    membership = frozenset({81, 82, 83, 84})

    def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        cmd_str = " ".join(cmd)
        if "log" in cmd_str and "--merges" in cmd_str:
            # Only issue 81 has a provenance merge.
            return _ok(_git_log_with_trailer(81))
        if "issue" in cmd_str and "view" in cmd_str:
            # Dispatch per issue number.
            if "81" in cmd_str:
                return _ok(_labels_json("agent-merged"))
            if "82" in cmd_str:
                return _ok(_labels_json("blocked"))
            if "83" in cmd_str:
                return _ok(_labels_json("agent-done"))
            if "84" in cmd_str:
                return _ok(_labels_json("agent-in-progress"))
        if "pr" in cmd_str and "list" in cmd_str:
            # Issue 83 has an open PR.
            return _ok(_pr_list_json("baton/my-milestone-83"))
        return _ok("{}")

    with patch.object(recovery_mod, "_run", side_effect=fake_run):
        result = reconstruct(_REPO, _OWNER, _REPO_NAME, _FEATURE, membership)

    assert 81 in result.done
    assert 82 in result.parked_seed
    assert 83 in result.ci_gate_reentry
    assert 84 in result.redispatch
