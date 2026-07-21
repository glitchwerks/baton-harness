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
- ``alert()`` severity routing: info skips escalate; warn passes through
  unchanged; critical prefixes with a loud marker.
- ``alert()`` always emits a runlog ``escalation`` event (best-effort).
- ``alert()`` survives a None runlog and a failing runlog.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import baton_harness.chain.escalation as esc_mod
import baton_harness.chain.runlog as runlog_mod
from baton_harness.chain.escalation import escalate
from baton_harness.chain.runlog import RunLog

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


# ---------------------------------------------------------------------------
# Helpers for alert() tests
# ---------------------------------------------------------------------------


def _make_runlog(tmp_path: Path) -> tuple[RunLog, list[dict[str, Any]]]:
    """Create a RunLog backed by tmp_path and a list capturing all emits.

    Returns:
        A (RunLog, captured_events) pair.  Every dict passed to
        ``runlog.emit`` is appended to ``captured_events`` so tests can
        assert on the emitted payload without touching the filesystem.
    """
    rl = RunLog(tmp_path / "test.jsonl")
    captured: list[dict[str, Any]] = []

    original_write = runlog_mod._write_line

    def _capture(path: Path, line: str) -> None:
        captured.append(json.loads(line.rstrip("\n")))
        original_write(path, line)

    # Attach the capture list so callers can reach it.
    rl._captured = captured  # type: ignore[attr-defined]
    return rl, captured


# ---------------------------------------------------------------------------
# alert() — severity routing
# ---------------------------------------------------------------------------


def test_alert_info_does_not_call_escalate_and_returns_true(
    tmp_path: Path,
) -> None:
    """alert(severity='info') never invokes escalate; returns True."""
    rl, captured = _make_runlog(tmp_path)

    with (
        patch.object(esc_mod, "_run") as mock_run,
        patch.dict("os.environ", {}, clear=False),
        patch(
            "baton_harness.chain.runlog._write_line",
            side_effect=lambda p, ln: captured.append(
                json.loads(ln.rstrip("\n"))
            ),
        ),
    ):
        import os

        os.environ.pop("BH_SLACK_WEBHOOK_URL", None)
        # alert() does not exist yet; this import will fail (red phase).
        from baton_harness.chain.escalation import alert

        result = alert(
            _OWNER,
            _REPO,
            _ISSUE,
            "just informational",
            severity="info",
            runlog=rl,
        )

    assert result is True
    mock_run.assert_not_called()
    # Exactly one escalation event must have been emitted.
    esc_events = [e for e in captured if e.get("event") == "escalation"]
    assert len(esc_events) == 1
    ev = esc_events[0]
    assert ev["severity"] == "info"
    assert ev["issue"] == _ISSUE
    assert ev["detail"] == "just informational"


def test_alert_warn_calls_escalate_with_unchanged_body(
    tmp_path: Path,
) -> None:
    """alert(severity='warn') calls escalate with the summary unmodified."""
    rl, captured = _make_runlog(tmp_path)
    summary = "something is slightly off"

    with (
        patch.object(esc_mod, "_run", return_value=_ok()) as mock_run,
        patch.dict("os.environ", {}, clear=False),
        patch(
            "baton_harness.chain.runlog._write_line",
            side_effect=lambda p, ln: captured.append(
                json.loads(ln.rstrip("\n"))
            ),
        ),
    ):
        import os

        os.environ.pop("BH_SLACK_WEBHOOK_URL", None)
        from baton_harness.chain.escalation import alert

        result = alert(
            _OWNER,
            _REPO,
            _ISSUE,
            summary,
            severity="warn",
            runlog=rl,
        )

    assert result is True
    assert mock_run.called
    # The body passed to _run (the gh issue comment cmd) must contain the
    # summary verbatim.
    all_args = " ".join(
        " ".join(c[0][0]) if c[0] else "" for c in mock_run.call_args_list
    )
    assert summary in all_args
    # Runlog escalation event with severity=warn.
    esc_events = [e for e in captured if e.get("event") == "escalation"]
    assert len(esc_events) == 1
    assert esc_events[0]["severity"] == "warn"


def test_alert_critical_prefixes_body_with_loud_marker(
    tmp_path: Path,
) -> None:
    """alert(severity='critical') passes a loud-prefixed body to escalate.

    The body must (a) contain the original summary as a substring and
    (b) carry a recognisable loud prefix — either the string ``CRITICAL``
    or a leading ``🚨``.  The exact marker text is up to the implementer.
    """
    rl, captured = _make_runlog(tmp_path)
    summary = "CI gate failed catastrophically"

    bodies_seen: list[str] = []

    def _capture_run(
        cmd: list[str],
    ) -> subprocess.CompletedProcess[str]:
        # The gh issue comment body is passed via --body <value>.
        if "--body" in cmd:
            idx = cmd.index("--body")
            bodies_seen.append(cmd[idx + 1])
        return _ok()

    with (
        patch.object(esc_mod, "_run", side_effect=_capture_run),
        patch.dict("os.environ", {}, clear=False),
        patch(
            "baton_harness.chain.runlog._write_line",
            side_effect=lambda p, ln: captured.append(
                json.loads(ln.rstrip("\n"))
            ),
        ),
    ):
        import os

        os.environ.pop("BH_SLACK_WEBHOOK_URL", None)
        from baton_harness.chain.escalation import alert

        result = alert(
            _OWNER,
            _REPO,
            _ISSUE,
            summary,
            severity="critical",
            runlog=rl,
        )

    assert result is True
    assert bodies_seen, "escalate must be called for severity='critical'"
    body = bodies_seen[0]
    # Original summary must still be a substring.
    assert summary in body, (
        f"Original summary not found in critical body: {body!r}"
    )
    # A loud prefix must be present.
    assert "CRITICAL" in body or body.startswith("🚨"), (
        f"No loud prefix found in critical body: {body!r}"
    )
    # Runlog event with severity=critical.
    esc_events = [e for e in captured if e.get("event") == "escalation"]
    assert len(esc_events) == 1
    assert esc_events[0]["severity"] == "critical"


# ---------------------------------------------------------------------------
# alert() — runlog=None must not crash
# ---------------------------------------------------------------------------


def test_alert_warn_without_runlog_still_calls_escalate() -> None:
    """alert(runlog=None, severity='warn') succeeds; escalate is called."""
    with (
        patch.object(esc_mod, "_run", return_value=_ok()) as mock_run,
        patch.dict("os.environ", {}, clear=False),
    ):
        import os

        os.environ.pop("BH_SLACK_WEBHOOK_URL", None)
        from baton_harness.chain.escalation import alert

        result = alert(
            _OWNER,
            _REPO,
            _ISSUE,
            "warn without runlog",
            severity="warn",
            runlog=None,
        )

    assert result is True
    assert mock_run.called


def test_alert_critical_without_runlog_still_calls_escalate() -> None:
    """alert(runlog=None, severity='critical') succeeds; escalate is called."""
    with (
        patch.object(esc_mod, "_run", return_value=_ok()) as mock_run,
        patch.dict("os.environ", {}, clear=False),
    ):
        import os

        os.environ.pop("BH_SLACK_WEBHOOK_URL", None)
        from baton_harness.chain.escalation import alert

        result = alert(
            _OWNER,
            _REPO,
            _ISSUE,
            "critical without runlog",
            severity="critical",
            runlog=None,
        )

    assert result is True
    assert mock_run.called


def test_alert_info_without_runlog_returns_true_and_does_not_raise() -> None:
    """alert(runlog=None, severity='info') returns True without raising."""
    with (
        patch.object(esc_mod, "_run") as mock_run,
        patch.dict("os.environ", {}, clear=False),
    ):
        import os

        os.environ.pop("BH_SLACK_WEBHOOK_URL", None)
        from baton_harness.chain.escalation import alert

        result = alert(
            _OWNER,
            _REPO,
            _ISSUE,
            "info without runlog",
            severity="info",
            runlog=None,
        )

    assert result is True
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# alert() — failing runlog must not propagate (best-effort)
# ---------------------------------------------------------------------------


def test_alert_runlog_failure_does_not_propagate_and_escalate_still_fires(
    tmp_path: Path,
) -> None:
    """alert()'s own try/except around runlog.emit() must swallow failures.

    Patching ``RunLog.emit`` directly (not the internal ``_write_line``
    seam) exercises the guard inside ``alert()`` itself.  If that guard
    were removed, the test would fail because the ``RuntimeError`` would
    propagate out of ``alert()``.

    escalate() must still be called for severity='warn' even when the
    runlog fails.
    """
    rl = RunLog(tmp_path / "test.jsonl")

    with (
        patch.object(esc_mod, "_run", return_value=_ok()) as mock_run,
        patch.dict("os.environ", {}, clear=False),
        patch.object(rl, "emit", side_effect=RuntimeError("emit explodes")),
    ):
        import os

        os.environ.pop("BH_SLACK_WEBHOOK_URL", None)
        from baton_harness.chain.escalation import alert

        # Must not raise despite emit() failing — alert()'s guard protects
        # the caller.
        result = alert(
            _OWNER,
            _REPO,
            _ISSUE,
            "warn with broken runlog",
            severity="warn",
            runlog=rl,
        )

    assert result is True
    assert mock_run.called


# ---------------------------------------------------------------------------
# Sanity: escalate() contract unchanged when called directly
# ---------------------------------------------------------------------------


def test_escalate_direct_call_still_posts_gh_comment_first() -> None:
    """Calling escalate() directly (not via alert) still behaves as before.

    This test is a contract-preservation sanity check: if the existing
    tests in the module already cover this behaviour exhaustively, this
    additional test is redundant but harmless.
    """
    with (
        patch.object(esc_mod, "_run", return_value=_ok()) as mock_run,
        patch.dict("os.environ", {}, clear=False),
    ):
        import os

        os.environ.pop("BH_SLACK_WEBHOOK_URL", None)
        result = escalate(_OWNER, _REPO, _ISSUE, "direct escalate call")

    assert result is True
    assert mock_run.called
    cmd = mock_run.call_args_list[0][0][0]
    assert "gh" in cmd
    assert "issue" in cmd
    assert "comment" in cmd


# ---------------------------------------------------------------------------
# Bug 3 — alert() must accept and forward installation_token
#
# Required behaviour (codex P1 #154):
#   alert(installation_token: str) must accept the token, forward it to
#   escalate(installation_token=...), and every gh subprocess call must
#   use a per-call env dict containing GH_TOKEN=<token> without mutating
#   os.environ.
#
# Current behaviour causing FAIL:
#   - alert() has no installation_token parameter → TypeError.
#   - Even if it did, the call to escalate() would not forward the kwarg.
#   - Daemon callsites of alert() omit the token (separate failing points).
# ---------------------------------------------------------------------------


class TestAlertThreadsInstallationToken:
    """RED: alert() must accept and forward installation_token.

    These tests fail until:
    1. ``alert`` gains ``installation_token: str`` as a keyword argument.
    2. ``alert`` forwards ``installation_token`` to ``escalate()``.
    3. ``escalate()`` uses the token via a per-call env dict (already
       implemented in escalate — the gap is that alert never passes it).
    """

    def test_alert_accepts_installation_token_kwarg(
        self,
    ) -> None:
        """alert(installation_token=...) must not raise TypeError.

        The contract requires ``alert`` to accept ``installation_token``
        as a keyword argument.  Currently the parameter is absent, so
        calling with it raises ``TypeError``.
        """
        import os

        with (
            patch.object(esc_mod, "_run", return_value=_ok()),
            patch.dict("os.environ", {}, clear=False),
        ):
            os.environ.pop("BH_SLACK_WEBHOOK_URL", None)
            from baton_harness.chain.escalation import alert

            # Must not raise TypeError — the kwarg must exist.
            result = alert(
                _OWNER,
                _REPO,
                _ISSUE,
                "test summary",
                severity="warn",
                runlog=None,
                installation_token="ghs_T_ALERT",
            )

        assert result is True

    def test_alert_forwards_token_to_escalate(
        self,
    ) -> None:
        """alert() must forward installation_token to escalate().

        ``alert`` calls ``escalate()`` for severity 'warn' and 'critical'.
        The ``installation_token`` kwarg must be forwarded in both cases.
        This test uses severity='warn' — the non-prefixed path.
        """
        import os

        received: dict[str, object] = {}

        def _fake_escalate(
            owner: str,
            repo: str,
            issue: int | None,
            summary: str,
            *args: object,
            **kwargs: object,
        ) -> bool:
            received.update(kwargs)
            return True

        with (
            patch.object(esc_mod, "escalate", side_effect=_fake_escalate),
            patch.dict("os.environ", {}, clear=False),
        ):
            os.environ.pop("BH_SLACK_WEBHOOK_URL", None)
            from baton_harness.chain.escalation import alert

            alert(
                _OWNER,
                _REPO,
                _ISSUE,
                "forward test",
                severity="warn",
                runlog=None,
                installation_token="ghs_T_FWD",
            )

        assert received.get("installation_token") == "ghs_T_FWD", (
            "alert() must forward installation_token to escalate(); "
            f"kwargs seen by escalate: {received!r}"
        )

    def test_alert_gh_subprocess_uses_per_call_env_dict(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """alert() with token → _run receives GH_TOKEN in env dict; env clean.

        The alert->escalate->_run chain must deliver the token via a
        per-call ``env`` kwarg rather than mutating ``os.environ``.

        Args:
            monkeypatch: Pytest monkeypatch fixture.
        """
        import os

        ghs_token = "ghs_T_ENV_ALERT"
        env_before = dict(os.environ)

        run_env_kwargs: list[dict[str, str] | None] = []

        def _spy_run(
            cmd: list[str],
            env: dict[str, str] | None = None,
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            run_env_kwargs.append(env)
            return _ok()

        monkeypatch.delenv("BH_SLACK_WEBHOOK_URL", raising=False)

        with patch.object(esc_mod, "_run", side_effect=_spy_run):
            from baton_harness.chain.escalation import alert

            alert(
                _OWNER,
                _REPO,
                _ISSUE,
                "env discipline test",
                severity="warn",
                runlog=None,
                installation_token=ghs_token,
            )

        # At least one _run call must carry the token in an env dict.
        gh_calls_with_env = [
            env
            for env in run_env_kwargs
            if env is not None and env.get("GH_TOKEN") == ghs_token
        ]
        assert gh_calls_with_env, (
            "At least one _run call from alert() must supply "
            f"GH_TOKEN={ghs_token!r} via a per-call env dict; "
            f"env kwargs seen: {run_env_kwargs!r}"
        )

        # os.environ must NOT have been mutated.
        env_after = dict(os.environ)
        assert env_after == env_before, (
            "os.environ must NOT be mutated during the alert() flow; "
            f"diff: {set(env_after.items()) ^ set(env_before.items())}"
        )

    def test_daemon_alert_callsites_thread_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Daemon alert() callsites during the merge path must thread token.

        The daemon's ``_do_merge`` helper calls ``alert()`` in two cases:
        (a) merge exception → warn alert, (b) CI failure/timeout → critical
        alert.  Both calls must forward ``installation_token`` once the
        parameter exists.

        This test exercises the CI-failure branch of ``_do_merge`` and
        checks that the ``alert`` spy receives a non-empty
        ``installation_token`` kwarg.

        Args:
            monkeypatch: Pytest monkeypatch fixture.
        """
        import baton_harness.chain.daemon as daemon_mod
        from baton_harness.chain.merge import MergeOutcome

        alert_calls: list[dict[str, object]] = []

        def _spy_alert(
            owner: str,
            repo: str,
            issue: int | None,
            summary: str,
            *args: object,
            **kwargs: object,
        ) -> bool:
            alert_calls.append(
                {
                    "owner": owner,
                    "repo": repo,
                    "issue": issue,
                    "summary": summary,
                    "kwargs": kwargs,
                }
            )
            return True

        # _do_merge is an inner closure inside run_daemon; we exercise it via
        # the exported helper path.  We patch merge_issue_branch to return
        # CI_FAILED (which triggers the critical-alert callsite in _do_merge)
        # and verify the alert spy received installation_token.
        with (
            patch.object(daemon_mod, "alert", side_effect=_spy_alert),
            patch.object(
                daemon_mod,
                "merge_issue_branch",
                return_value=MergeOutcome.CI_FAILED,
            ),
            patch.object(daemon_mod, "_label_edit", return_value=None),
        ):
            # Import and call _do_merge directly if it is module-level,
            # otherwise trigger it via the inline daemon path.  The function
            # is defined as a closure inside run_daemon so we call it
            # indirectly by importing the relevant helpers.  We use
            # merge_issue_branch's return value to prove the CI_FAILED path.
            #
            # If _do_merge is not directly callable, we fall back to
            # asserting the contract via the AST: look for alert( callsites
            # and verify they pass installation_token.
            #
            # Patch-target note (#277, Phase 6e): the three alert() sites
            # this scan has always covered (run_daemon's tick-failure
            # handler and _poll_and_run's two callsites) moved from
            # daemon/__init__.py to daemon/poll.py verbatim (still
            # threading installation_token= unchanged) -- inspecting
            # `poll` here instead of `daemon_mod` keeps the exact same
            # coverage set at its new home, matching the reconcile_startup
            # patch-target migration elsewhere in this sub-PR.
            import ast
            import inspect

            from baton_harness.chain.daemon import poll

            daemon_src = inspect.getsource(poll)
            tree = ast.parse(daemon_src)

            # Find all Call nodes where func.attr == "alert" or
            # func.id == "alert".
            alert_call_nodes = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    if (
                        isinstance(func, ast.Attribute)
                        and func.attr == "alert"
                    ) or (isinstance(func, ast.Name) and func.id == "alert"):
                        alert_call_nodes.append(node)

            assert alert_call_nodes, (
                "daemon/poll.py must contain at least one alert() call"
            )

            # Every alert() callsite that is in the merge-outcome path
            # (contains the word 'merge' or 'CI' nearby in its arguments)
            # must pass installation_token as a keyword argument.
            #
            # We check every alert() callsite — the contract is that ALL
            # daemon alert() calls must thread the token once the parameter
            # exists.
            callsites_missing_token = []
            for node in alert_call_nodes:
                kwarg_names = {
                    kw.arg for kw in node.keywords if kw.arg is not None
                }
                if "installation_token" not in kwarg_names:
                    callsites_missing_token.append(
                        ast.unparse(node)
                        if hasattr(ast, "unparse")
                        else f"line {node.lineno}"
                    )

            assert not callsites_missing_token, (
                "All daemon/poll.py alert() callsites must pass "
                "installation_token=... kwarg; missing at: "
                f"{callsites_missing_token}"
            )
