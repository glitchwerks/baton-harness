"""Unit tests for the fire-and-forget Slack-alert POST helper.

The implementation is expected to live at
``baton_harness.chain.alert_post`` and expose:

    post_slack_alert(url: str, message: str, *, timeout: float = 5.0) -> bool

Contract being tested:
- POSTs to the configured URL with a JSON body matching the Slack
  incoming-webhook contract ``{"text": "<message>"}``.
- HTTP method is POST.
- Content-type header is ``application/json``.
- Fire-and-forget — never raises: ``urllib.error.URLError``,
  ``socket.timeout``, or any other exception is caught; the helper logs
  a warning and returns ``False``.
- Returns ``True`` on success (HTTP 2xx response from the mock).
- Returns ``False`` on non-2xx without raising.
- Default timeout is ≤ 5 s so a hung Slack webhook cannot hold up the
  daemon indefinitely.

All HTTP calls are intercepted by patching ``urllib.request.urlopen``
(or the module-level binding the implementation uses).  No live network
is touched.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WEBHOOK = "https://hooks.slack.com/services/T00/B00/secret"
_MESSAGE = (
    "baton-harness refusing to launch worker — main branch protection missing."
)


def _mock_response(status: int = 200) -> MagicMock:
    """Build a mock HTTP response with the given status.

    Args:
        status: The HTTP status code to surface from the mock.

    Returns:
        A MagicMock whose ``.status`` attribute equals ``status`` and
        whose context-manager protocol is wired up.
    """
    resp = MagicMock()
    resp.status = status
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ---------------------------------------------------------------------------
# Test 1 — POSTs to the correct URL with a Slack-contract JSON body
# ---------------------------------------------------------------------------


def test_post_slack_alert_posts_to_correct_url_with_slack_body() -> None:
    """post_slack_alert sends a POST to the webhook URL.

    Asserts that ``urllib.request.urlopen`` is called with a ``Request``
    whose url matches the webhook and whose body is a JSON object with
    a ``"text"`` key equal to the supplied message.
    """
    from baton_harness.chain.alert_post import post_slack_alert

    captured_requests: list[urllib.request.Request] = []

    def _fake_urlopen(
        req: urllib.request.Request, timeout: float = 5.0
    ) -> MagicMock:
        captured_requests.append(req)
        return _mock_response(200)

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        result = post_slack_alert(_WEBHOOK, _MESSAGE)

    assert result is True
    assert len(captured_requests) == 1, "Expected exactly one urlopen call"
    req = captured_requests[0]
    assert req.full_url == _WEBHOOK, (
        f"POST must target the webhook URL {_WEBHOOK!r}; got {req.full_url!r}"
    )
    body = json.loads(req.data.decode("utf-8"))
    assert body == {"text": _MESSAGE}, (
        f"Body must match Slack webhook contract {{text: <message>}}; "
        f"got {body!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — Method is POST and Content-Type is application/json
# ---------------------------------------------------------------------------


def test_post_slack_alert_uses_post_method_and_json_content_type() -> None:
    """POST method and application/json content-type are required.

    Asserts that the ``Request`` passed to urlopen uses the ``POST``
    method and includes ``Content-Type: application/json``.
    """
    from baton_harness.chain.alert_post import post_slack_alert

    captured: list[urllib.request.Request] = []

    def _fake_urlopen(
        req: urllib.request.Request, timeout: float = 5.0
    ) -> MagicMock:
        captured.append(req)
        return _mock_response(200)

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        post_slack_alert(_WEBHOOK, _MESSAGE)

    assert captured, "urlopen must be called"
    req = captured[0]
    assert req.get_method() == "POST", (
        f"HTTP method must be POST; got {req.get_method()!r}"
    )
    ct = req.get_header("Content-type")
    assert ct is not None and "application/json" in ct, (
        f"Content-Type must be application/json; got {ct!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — Fire-and-forget: URLError does not propagate; returns False
# ---------------------------------------------------------------------------


def test_post_slack_alert_returns_false_on_url_error_without_raising() -> None:
    """URLError is swallowed; helper logs a warning and returns False.

    The daemon must never crash because a Slack webhook is unreachable.
    Raising any exception from ``post_slack_alert`` is a contract
    violation.
    """
    from baton_harness.chain.alert_post import post_slack_alert

    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("connection refused"),
    ):
        result = post_slack_alert(_WEBHOOK, _MESSAGE)

    assert result is False, (
        "URLError must be caught; post_slack_alert must return False"
    )


def test_post_slack_alert_swallows_socket_timeout() -> None:
    """socket.timeout is swallowed; helper returns False without raising.

    Ensures a slow/hung Slack endpoint cannot hold up the daemon via an
    unhandled exception.
    """
    from baton_harness.chain.alert_post import post_slack_alert

    with patch(
        "urllib.request.urlopen",
        side_effect=TimeoutError("timed out"),
    ):
        result = post_slack_alert(_WEBHOOK, _MESSAGE)

    assert result is False, (
        "socket.timeout must be caught; post_slack_alert must return False"
    )


def test_post_slack_alert_returns_false_on_arbitrary_exception() -> None:
    """Any exception from urlopen is swallowed; helper returns False.

    Confirms the fire-and-forget contract covers the general case, not
    only URLError and socket.timeout.
    """
    from baton_harness.chain.alert_post import post_slack_alert

    with patch(
        "urllib.request.urlopen",
        side_effect=RuntimeError("unexpected"),
    ):
        result = post_slack_alert(_WEBHOOK, _MESSAGE)

    assert result is False, (
        "Any exception must be caught; post_slack_alert must return False"
    )


# ---------------------------------------------------------------------------
# Test 4 — Returns True on HTTP 2xx
# ---------------------------------------------------------------------------


def test_post_slack_alert_returns_true_on_2xx_response() -> None:
    """Returns True when urlopen succeeds (HTTP 2xx response)."""
    from baton_harness.chain.alert_post import post_slack_alert

    with patch(
        "urllib.request.urlopen",
        return_value=_mock_response(200),
    ):
        result = post_slack_alert(_WEBHOOK, _MESSAGE)

    assert result is True


# ---------------------------------------------------------------------------
# Test 5 — Returns False on non-2xx response without raising
# ---------------------------------------------------------------------------


def test_post_slack_alert_returns_false_on_non_2xx_response() -> None:
    """Returns False on a non-2xx HTTP response without raising.

    A 400 or 500 from the Slack webhook is treated as a delivery failure;
    the helper must return False without propagating an exception.
    """
    from baton_harness.chain.alert_post import post_slack_alert

    for status in (400, 500, 503):
        with patch(
            "urllib.request.urlopen",
            return_value=_mock_response(status),
        ):
            result = post_slack_alert(_WEBHOOK, _MESSAGE)
        assert result is False, (
            f"HTTP {status} must yield False; got {result!r}"
        )


# ---------------------------------------------------------------------------
# Test 6 — Default timeout ≤ 5 s
# ---------------------------------------------------------------------------


def test_post_slack_alert_uses_short_default_timeout() -> None:
    """Default timeout is ≤ 5 s so a hung webhook cannot stall the daemon.

    Inspects the timeout kwarg passed to ``urlopen`` when ``post_slack_alert``
    is called without an explicit timeout.
    """
    from baton_harness.chain.alert_post import post_slack_alert

    captured_timeout: list[float] = []

    def _recording_urlopen(
        req: urllib.request.Request,
        timeout: float = 30.0,
    ) -> MagicMock:
        captured_timeout.append(timeout)
        return _mock_response(200)

    with patch("urllib.request.urlopen", side_effect=_recording_urlopen):
        post_slack_alert(_WEBHOOK, _MESSAGE)

    assert captured_timeout, "urlopen must be called (timeout not captured)"
    assert captured_timeout[0] <= 5.0, (
        f"Default timeout must be ≤ 5 s to avoid stalling the daemon; "
        f"got {captured_timeout[0]!r}"
    )


# ---------------------------------------------------------------------------
# Test 7 — Failure logs a warning (caplog)
# ---------------------------------------------------------------------------


def test_post_slack_alert_logs_warning_on_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A delivery failure must emit at least one WARNING-level log entry.

    Operators need to see when a Slack alert could not be delivered.

    Args:
        caplog: Pytest log-capture fixture.
    """
    from baton_harness.chain.alert_post import post_slack_alert

    with (
        patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("refused"),
        ),
        caplog.at_level(logging.WARNING),
    ):
        post_slack_alert(_WEBHOOK, _MESSAGE)

    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "A delivery failure must emit a WARNING-level log; "
        f"records seen: {[r.message for r in caplog.records]!r}"
    )
