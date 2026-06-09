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
