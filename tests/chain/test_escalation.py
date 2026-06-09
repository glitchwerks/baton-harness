"""Unit tests for baton_harness.chain.escalation.

Tests the dual-channel escalation module.  All subprocess calls are
intercepted by patching the module-local ``_run`` seam; Slack HTTP calls
are intercepted by patching ``urllib.request.urlopen`` (or the module-
level helper).  No live network or ``gh`` binary is required.

Coverage:
- GitHub comment attempted first and return value is the durable record.
- Slack skipped when ``BH_SLACK_WEBHOOK_URL`` is not set.
- Slack best-effort failure does NOT change the return value (GitHub
  comment is the record).
- GitHub-comment failure logs a WARNING and returns ``False``.
- ``kind`` kwarg accepted (default ``"block"``).
"""

from __future__ import annotations

import logging
import subprocess
from unittest.mock import patch

import pytest

import baton_harness.chain.escalation as esc_mod
from baton_harness.chain.escalation import escalate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_OWNER = "glitchwerks"
_REPO = "baton-harness"
_ISSUE = 42


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


# ---------------------------------------------------------------------------
# GitHub comment is the durable record
# ---------------------------------------------------------------------------


def test_escalate_github_comment_success_returns_true() -> None:
    """Escalate returns True when the GitHub comment succeeds."""
    with (
        patch.object(esc_mod, "_run", return_value=_ok()) as mock_run,
        patch.dict("os.environ", {}, clear=False),
    ):
        # Ensure Slack env is absent.
        import os

        os.environ.pop("BH_SLACK_WEBHOOK_URL", None)
        result = escalate(
            _OWNER, _REPO, _ISSUE, "stalled on issue #42", kind="block"
        )

    assert result is True
    # gh issue comment must be called
    assert mock_run.called
    gh_call = mock_run.call_args_list[0]
    cmd = gh_call[0][0]
    assert "gh" in cmd
    assert "issue" in cmd
    assert "comment" in cmd
    assert str(_ISSUE) in cmd


def test_escalate_gh_comment_uses_repo_flag() -> None:
    """Escalate passes --repo owner/repo to gh issue comment."""
    with (
        patch.object(esc_mod, "_run", return_value=_ok()) as mock_run,
        patch.dict("os.environ", {}, clear=False),
    ):
        import os

        os.environ.pop("BH_SLACK_WEBHOOK_URL", None)
        escalate(_OWNER, _REPO, _ISSUE, "summary")

    cmd = mock_run.call_args_list[0][0][0]
    assert "--repo" in cmd
    repo_idx = cmd.index("--repo")
    assert cmd[repo_idx + 1] == f"{_OWNER}/{_REPO}"


def test_escalate_github_failure_returns_false_and_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Escalate returns False and logs WARNING when gh comment fails."""
    with (
        patch.object(esc_mod, "_run", return_value=_fail("gh error")),
        patch.dict("os.environ", {}, clear=False),
        caplog.at_level(
            logging.WARNING,
            logger="baton_harness.chain.escalation",
        ),
    ):
        import os

        os.environ.pop("BH_SLACK_WEBHOOK_URL", None)
        result = escalate(_OWNER, _REPO, _ISSUE, "summary")

    assert result is False
    assert any(
        "WARNING" in r.levelname or r.levelno >= logging.WARNING
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Slack channel behaviour
# ---------------------------------------------------------------------------


def test_escalate_slack_skipped_when_env_unset() -> None:
    """No Slack POST when BH_SLACK_WEBHOOK_URL is not set."""
    with (
        patch.object(esc_mod, "_run", return_value=_ok()),
        patch("urllib.request.urlopen") as mock_urlopen,
        patch.dict("os.environ", {}, clear=False),
    ):
        import os

        os.environ.pop("BH_SLACK_WEBHOOK_URL", None)
        escalate(_OWNER, _REPO, _ISSUE, "summary")

    mock_urlopen.assert_not_called()


def test_escalate_slack_posted_when_env_set() -> None:
    """Slack POST is attempted when BH_SLACK_WEBHOOK_URL is set."""
    webhook = "https://hooks.slack.com/test"
    with (
        patch.object(esc_mod, "_run", return_value=_ok()),
        patch("urllib.request.urlopen") as mock_urlopen,
        patch.dict(
            "os.environ",
            {"BH_SLACK_WEBHOOK_URL": webhook},
            clear=False,
        ),
    ):
        result = escalate(_OWNER, _REPO, _ISSUE, "stalled!")

    # GitHub succeeded → True regardless of Slack.
    assert result is True
    mock_urlopen.assert_called_once()


def test_escalate_slack_failure_does_not_change_return() -> None:
    """Slack failure is best-effort; return reflects GitHub comment only."""
    webhook = "https://hooks.slack.com/test"
    with (
        patch.object(esc_mod, "_run", return_value=_ok()),
        patch(
            "urllib.request.urlopen",
            side_effect=OSError("network error"),
        ),
        patch.dict(
            "os.environ",
            {"BH_SLACK_WEBHOOK_URL": webhook},
            clear=False,
        ),
    ):
        result = escalate(_OWNER, _REPO, _ISSUE, "stalled!")

    # GitHub comment succeeded → True, Slack failure doesn't flip it.
    assert result is True


def test_escalate_slack_failure_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Slack failure is logged at WARNING level."""
    webhook = "https://hooks.slack.com/test"
    with (
        patch.object(esc_mod, "_run", return_value=_ok()),
        patch(
            "urllib.request.urlopen",
            side_effect=OSError("network error"),
        ),
        patch.dict(
            "os.environ",
            {"BH_SLACK_WEBHOOK_URL": webhook},
            clear=False,
        ),
        caplog.at_level(
            logging.WARNING,
            logger="baton_harness.chain.escalation",
        ),
    ):
        escalate(_OWNER, _REPO, _ISSUE, "stalled!")

    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_escalate_kind_parameter_accepted() -> None:
    """Escalate accepts kind='debug' without error."""
    with (
        patch.object(esc_mod, "_run", return_value=_ok()),
        patch.dict("os.environ", {}, clear=False),
    ):
        import os

        os.environ.pop("BH_SLACK_WEBHOOK_URL", None)
        result = escalate(_OWNER, _REPO, _ISSUE, "debug summary", kind="debug")

    assert result is True


# ---------------------------------------------------------------------------
# No-target escalation: issue=None and issue=0 (FIX B)
# ---------------------------------------------------------------------------


def test_escalate_none_issue_skips_gh_comment(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """issue=None skips the gh issue comment entirely and logs WARNING.

    GitHub issue numbers start at 1, so there is no valid issue to comment
    on for repo-level (tick-level) daemon failures.  The durable record
    cannot land, so the call is skipped rather than sending ``gh issue
    comment 0`` which always fails.
    """
    import os

    with (
        patch.object(esc_mod, "_run") as mock_run,
        patch("urllib.request.urlopen") as mock_urlopen,
        patch.dict("os.environ", {}, clear=False),
        caplog.at_level(
            logging.WARNING,
            logger="baton_harness.chain.escalation",
        ),
    ):
        os.environ.pop("BH_SLACK_WEBHOOK_URL", None)
        result = escalate(_OWNER, _REPO, None, "daemon tick error")

    # gh must NOT be called — no valid issue target.
    mock_run.assert_not_called()
    # No Slack env set → no Slack call.
    mock_urlopen.assert_not_called()
    # Return must signal that no durable record was written.
    assert result is False
    # A WARNING must be logged so operators can see the failure.
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "Expected a WARNING log when issue=None"
    )


def test_escalate_zero_issue_skips_gh_comment(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """issue=0 is treated as no-target (same as None) and skips gh comment.

    Callers that pass 0 (the previous broken behaviour in daemon.py) must
    not silently call ``gh issue comment 0``.
    """
    import os

    with (
        patch.object(esc_mod, "_run") as mock_run,
        patch.dict("os.environ", {}, clear=False),
        caplog.at_level(
            logging.WARNING,
            logger="baton_harness.chain.escalation",
        ),
    ):
        os.environ.pop("BH_SLACK_WEBHOOK_URL", None)
        result = escalate(_OWNER, _REPO, 0, "daemon tick error")

    mock_run.assert_not_called()
    assert result is False
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_escalate_none_issue_still_posts_slack_when_env_set(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When issue=None but BH_SLACK_WEBHOOK_URL is set, Slack is still tried.

    Slack is the best-effort fallback when the durable GitHub record cannot
    land.  Skipping the gh comment must not suppress the Slack notification.
    """
    webhook = "https://hooks.slack.com/test"
    with (
        patch.object(esc_mod, "_run") as mock_run,
        patch("urllib.request.urlopen") as mock_urlopen,
        patch.dict(
            "os.environ",
            {"BH_SLACK_WEBHOOK_URL": webhook},
            clear=False,
        ),
        caplog.at_level(
            logging.WARNING,
            logger="baton_harness.chain.escalation",
        ),
    ):
        result = escalate(_OWNER, _REPO, None, "daemon tick error")

    # gh still not called — no valid issue.
    mock_run.assert_not_called()
    # But Slack IS attempted.
    mock_urlopen.assert_called_once()
    # Return is still False — no durable record was written.
    assert result is False
