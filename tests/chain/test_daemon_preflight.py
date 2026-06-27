"""Tests for the daemon per-launch preflight wiring.

This test file pins the contract for a small, testable seam that the
code-writer must implement at the worker-launch site in ``daemon.py``.

Proposed seam (code-writer adopts this name in Phase 2):

    _should_launch_worker(
        issue_number: int,
        owner: str,
        repo: str,
        *,
        app_id: str,
        runner: Callable[[list[str]], subprocess.CompletedProcess[str]],
        obs: ObsConfig,
    ) -> bool

``_should_launch_worker`` wraps ``ruleset_is_provisioned`` and:
  1. Returns ``True`` only when the status is ``MATCH``.
  2. On any non-MATCH result (``DRIFT``, ``ABSENT``, or ``ERROR``):
     - Returns ``False`` (refuse to launch).
     - Calls ``post_slack_alert(obs.heartbeat_ping_url, <message>)`` when
       ``obs.heartbeat_ping_url`` is not None.
     - Does NOT crash if ``post_slack_alert`` raises (fail-closed, not
       fail-open).
  3. When ``obs.heartbeat_ping_url`` is None, skips the POST entirely
     but still refuses to launch (returns False for non-MATCH).

The alert message body for Charge 5 must satisfy:
  ``"baton-harness refusing to launch worker"`` is a substring, AND
  the failed-checks description is present (e.g. ``"Failed checks:"``).

All external calls (``ruleset_is_provisioned``, ``post_slack_alert``) are
mocked.  Tests work with a minimal hand-constructed ObsConfig rather than
importing the full daemon start-up machinery.

Coverage:
- MATCH → _should_launch_worker returns True; no alert sent; no parking.
- DRIFT → returns False; alert POSTed with 'Failed checks:' in message.
- ABSENT → returns False; alert body mentions missing ruleset(s).
- ERROR → returns False (fail-closed); alert mentions error path.
- Alert POST failure does NOT crash launch decision loop.
- No ``BH_HEARTBEAT_PING_URL`` configured (obs.heartbeat_ping_url is None)
  → returns False on DRIFT but no POST attempted; a warning is logged.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from baton_harness.chain.obs_config import ObsConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_OWNER = "glitchwerks"
_REPO = "baton-harness"
_ISSUE = 42
_APP_ID = "111"
_WEBHOOK = "https://hooks.slack.com/services/T00/B00/secret"


def _make_obs(
    tmp_path: Path,
    *,
    ping_url: str | None = _WEBHOOK,
) -> ObsConfig:
    """Build a minimal ObsConfig for preflight tests.

    Args:
        tmp_path: Pytest tmp_path fixture; used to generate required paths.
        ping_url: Value for ``heartbeat_ping_url``; use ``None`` to
            test the no-URL path.

    Returns:
        A populated ObsConfig with the heartbeat_ping_url set accordingly.
    """
    return ObsConfig(
        runlog_path=tmp_path / "runlog.jsonl",
        heartbeat_file=tmp_path / "heartbeat",
        redispatch_window_ticks=10,
        redispatch_max=3,
        heartbeat_stall_s=7200.0,
        heartbeat_ping_url=ping_url,
        redispatch_counts_path=tmp_path / "dispatch-counts.json",
    )


def _fake_runner(args: list[str]) -> Any:  # noqa: ANN401
    """Stub runner (never called in these integration seam tests)."""
    raise AssertionError(
        "_fake_runner must not be called in preflight unit tests "
        "(ruleset_is_provisioned is mocked)"
    )


# ---------------------------------------------------------------------------
# Test 1 — MATCH → proceed (returns True; no alert)
# ---------------------------------------------------------------------------


def test_should_launch_worker_returns_true_on_match(
    tmp_path: Path,
) -> None:
    """_should_launch_worker returns True when ruleset_is_provisioned → MATCH.

    No Slack alert must be sent; the launch is not refused.

    Args:
        tmp_path: Pytest tmp_path fixture.
    """
    from baton_harness.chain.ruleset_status import RulesetStatus

    import baton_harness.chain.daemon as daemon_mod

    obs = _make_obs(tmp_path)
    post_calls: list[tuple[str, str]] = []

    def _fake_post(url: str, message: str, **kwargs: Any) -> bool:  # noqa: ANN401
        post_calls.append((url, message))
        return True

    with (
        patch.object(
            daemon_mod,
            "ruleset_is_provisioned",
            return_value=RulesetStatus.MATCH,
        ),
        patch(
            "baton_harness.chain.daemon.post_slack_alert",
            side_effect=_fake_post,
        ),
    ):
        result = daemon_mod._should_launch_worker(  # type: ignore[attr-defined]
            _ISSUE,
            _OWNER,
            _REPO,
            app_id=_APP_ID,
            runner=_fake_runner,
            obs=obs,
        )

    assert result is True, "_should_launch_worker must return True on MATCH"
    assert not post_calls, (
        "No Slack alert must be sent when preflight passes (MATCH)"
    )


# ---------------------------------------------------------------------------
# Test 2 — DRIFT → refuse + alert with 'Failed checks:'
# ---------------------------------------------------------------------------


def test_should_launch_worker_refuses_and_alerts_on_drift(
    tmp_path: Path,
) -> None:
    """_should_launch_worker returns False on DRIFT and POSTs a Slack alert.

    The alert body must contain:
    - ``"baton-harness refusing to launch worker"``
    - ``"Failed checks:"``

    Args:
        tmp_path: Pytest tmp_path fixture.
    """
    from baton_harness.chain.ruleset_status import RulesetStatus

    import baton_harness.chain.daemon as daemon_mod

    obs = _make_obs(tmp_path)
    post_calls: list[tuple[str, str]] = []

    def _fake_post(url: str, message: str, **kwargs: Any) -> bool:  # noqa: ANN401
        post_calls.append((url, message))
        return True

    with (
        patch.object(
            daemon_mod,
            "ruleset_is_provisioned",
            return_value=RulesetStatus.DRIFT,
        ),
        patch(
            "baton_harness.chain.daemon.post_slack_alert",
            side_effect=_fake_post,
        ),
    ):
        result = daemon_mod._should_launch_worker(  # type: ignore[attr-defined]
            _ISSUE,
            _OWNER,
            _REPO,
            app_id=_APP_ID,
            runner=_fake_runner,
            obs=obs,
        )

    assert result is False, "_should_launch_worker must return False on DRIFT"
    assert post_calls, "A Slack alert must be posted on DRIFT"
    url_posted, message_posted = post_calls[0]
    assert url_posted == _WEBHOOK, (
        f"Alert must POST to obs.heartbeat_ping_url {_WEBHOOK!r}; "
        f"got {url_posted!r}"
    )
    assert "baton-harness refusing to launch worker" in message_posted, (
        f"Alert body must contain the refusal phrase; got {message_posted!r}"
    )
    assert "Failed checks:" in message_posted, (
        f"Alert body must contain 'Failed checks:'; got {message_posted!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — ABSENT → refuse + alert mentioning missing ruleset(s)
# ---------------------------------------------------------------------------


def test_should_launch_worker_refuses_and_alerts_on_absent(
    tmp_path: Path,
) -> None:
    """_should_launch_worker returns False on ABSENT and POSTs a Slack alert.

    The alert body must contain the refusal phrase and 'Failed checks:'.

    Args:
        tmp_path: Pytest tmp_path fixture.
    """
    from baton_harness.chain.ruleset_status import RulesetStatus

    import baton_harness.chain.daemon as daemon_mod

    obs = _make_obs(tmp_path)
    post_calls: list[tuple[str, str]] = []

    def _fake_post(url: str, message: str, **kwargs: Any) -> bool:  # noqa: ANN401
        post_calls.append((url, message))
        return True

    with (
        patch.object(
            daemon_mod,
            "ruleset_is_provisioned",
            return_value=RulesetStatus.ABSENT,
        ),
        patch(
            "baton_harness.chain.daemon.post_slack_alert",
            side_effect=_fake_post,
        ),
    ):
        result = daemon_mod._should_launch_worker(  # type: ignore[attr-defined]
            _ISSUE,
            _OWNER,
            _REPO,
            app_id=_APP_ID,
            runner=_fake_runner,
            obs=obs,
        )

    assert result is False, "_should_launch_worker must return False on ABSENT"
    assert post_calls, "A Slack alert must be posted on ABSENT"
    _, message_posted = post_calls[0]
    assert "baton-harness refusing to launch worker" in message_posted, (
        f"Alert body must contain the refusal phrase; got {message_posted!r}"
    )
    assert "Failed checks:" in message_posted, (
        f"Alert body must mention 'Failed checks:'; got {message_posted!r}"
    )


# ---------------------------------------------------------------------------
# Test 4 — ERROR → refuse + alert (fail-closed)
# ---------------------------------------------------------------------------


def test_should_launch_worker_refuses_and_alerts_on_error_fail_closed(
    tmp_path: Path,
) -> None:
    """_should_launch_worker returns False on ERROR (fail-closed contract).

    Even when the ruleset check itself fails with an error (e.g. network
    outage), the daemon must refuse to launch rather than proceeding
    without branch-protection.  The Slack alert must still be attempted.

    Args:
        tmp_path: Pytest tmp_path fixture.
    """
    from baton_harness.chain.ruleset_status import RulesetStatus

    import baton_harness.chain.daemon as daemon_mod

    obs = _make_obs(tmp_path)
    post_calls: list[tuple[str, str]] = []

    def _fake_post(url: str, message: str, **kwargs: Any) -> bool:  # noqa: ANN401
        post_calls.append((url, message))
        return True

    with (
        patch.object(
            daemon_mod,
            "ruleset_is_provisioned",
            return_value=RulesetStatus.ERROR,
        ),
        patch(
            "baton_harness.chain.daemon.post_slack_alert",
            side_effect=_fake_post,
        ),
    ):
        result = daemon_mod._should_launch_worker(  # type: ignore[attr-defined]
            _ISSUE,
            _OWNER,
            _REPO,
            app_id=_APP_ID,
            runner=_fake_runner,
            obs=obs,
        )

    assert result is False, (
        "_should_launch_worker must return False on ERROR (fail-closed)"
    )
    assert post_calls, "A Slack alert must be attempted on ERROR"
    _, message_posted = post_calls[0]
    assert "baton-harness refusing to launch worker" in message_posted, (
        f"Alert body must contain the refusal phrase; got {message_posted!r}"
    )


# ---------------------------------------------------------------------------
# Test 5 — Alert POST failure does NOT crash the launch decision
# ---------------------------------------------------------------------------


def test_alert_post_failure_does_not_crash_launch_decision(
    tmp_path: Path,
) -> None:
    """Launch refusal is clean even when post_slack_alert raises.

    post_slack_alert's own fire-and-forget contract guarantees it does not
    raise, but the daemon seam must also be resilient in case the helper
    contract is violated.  The launch refusal must complete without raising.

    Args:
        tmp_path: Pytest tmp_path fixture.
    """
    from baton_harness.chain.ruleset_status import RulesetStatus

    import baton_harness.chain.daemon as daemon_mod

    obs = _make_obs(tmp_path)

    with (
        patch.object(
            daemon_mod,
            "ruleset_is_provisioned",
            return_value=RulesetStatus.DRIFT,
        ),
        patch(
            "baton_harness.chain.daemon.post_slack_alert",
            side_effect=RuntimeError("post helper itself raised"),
        ),
    ):
        # Must NOT raise — the daemon must continue cleanly.
        result = daemon_mod._should_launch_worker(  # type: ignore[attr-defined]
            _ISSUE,
            _OWNER,
            _REPO,
            app_id=_APP_ID,
            runner=_fake_runner,
            obs=obs,
        )

    assert result is False, (
        "_should_launch_worker must return False on DRIFT even if "
        "post_slack_alert raises"
    )


# ---------------------------------------------------------------------------
# Test 6 — No heartbeat_ping_url → no POST; warn logged; still refuses
# ---------------------------------------------------------------------------


def test_no_ping_url_configured_skips_post_but_still_refuses(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When obs.heartbeat_ping_url is None, no POST is attempted.

    The launch refusal must still fire (returns False for DRIFT).  A
    warning must be logged so operators know no Slack alert was sent.

    Args:
        tmp_path: Pytest tmp_path fixture.
        caplog: Pytest log-capture fixture.
    """
    from baton_harness.chain.ruleset_status import RulesetStatus

    import baton_harness.chain.daemon as daemon_mod

    obs = _make_obs(tmp_path, ping_url=None)  # no webhook configured
    post_calls: list[Any] = []

    def _fake_post(url: str, message: str, **kwargs: Any) -> bool:  # noqa: ANN401
        post_calls.append((url, message))
        return True

    with (
        patch.object(
            daemon_mod,
            "ruleset_is_provisioned",
            return_value=RulesetStatus.DRIFT,
        ),
        patch(
            "baton_harness.chain.daemon.post_slack_alert",
            side_effect=_fake_post,
        ),
        caplog.at_level(logging.WARNING),
    ):
        result = daemon_mod._should_launch_worker(  # type: ignore[attr-defined]
            _ISSUE,
            _OWNER,
            _REPO,
            app_id=_APP_ID,
            runner=_fake_runner,
            obs=obs,
        )

    assert result is False, (
        "Launch must still be refused when no ping URL is configured"
    )
    assert not post_calls, (
        "post_slack_alert must NOT be called when "
        "obs.heartbeat_ping_url is None"
    )
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "A WARNING must be logged when a preflight alert cannot be sent "
        "because no heartbeat_ping_url is configured; "
        f"records: {[r.message for r in caplog.records]!r}"
    )
