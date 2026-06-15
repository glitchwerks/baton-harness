"""Tests for _heartbeat_tick ping-URL behavior (issues #79 / #85).

Covers acceptance criteria for the BH_HEARTBEAT_PING_URL best-effort ping
that _heartbeat_tick must perform on each tick.

AC coverage:
- Ping fired exactly once with configured URL when heartbeat_ping_url is set.
- No ping at all when heartbeat_ping_url is None.
- Ping failure is swallowed: _heartbeat_tick completes (does not raise),
  _write_heartbeat is still called (liveness write is not aborted),
  runlog.emit still fires, and a stall alert still fires when the stall
  threshold is exceeded — ping failure never skips downstream steps.
- _ping_url calls urllib.request.urlopen with timeout=_DEFAULT_PING_TIMEOUT_S
  and enters/exits the response as a context manager (or calls
  .read()/.close()).
- _ping_url rejects non-http/https schemes without calling urlopen
  (forward-spec — RED until implementation pass).

No asyncio / pytest-asyncio dependency -- all tests are synchronous.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from baton_harness.chain.heartbeat import (
    _DEFAULT_PING_TIMEOUT_S,
    LivenessState,
    _heartbeat_tick,
    _ping_url,
)
from baton_harness.chain.obs_config import ObsConfig

# ---------------------------------------------------------------------------
# Helpers (mirror test_heartbeat.py conventions exactly)
# ---------------------------------------------------------------------------

_PING_SEAM = "baton_harness.chain.heartbeat._ping_url"
_WRITE_SEAM = "baton_harness.chain.heartbeat._write_heartbeat"
_ALERT_SEAM = "baton_harness.chain.heartbeat.alert"
_URLOPEN_SEAM = "urllib.request.urlopen"


def _make_obs(
    heartbeat_file: Path,
    *,
    heartbeat_ping_url: str | None = None,
    heartbeat_stall_s: float = 7200.0,
    runlog_path: Path | None = None,
    redispatch_counts_path: Path | None = None,
) -> ObsConfig:
    """Construct a minimal ObsConfig for ping tests.

    Args:
        heartbeat_file: Path to the heartbeat file under test.
        heartbeat_ping_url: URL to configure for ping; ``None`` = disabled.
        heartbeat_stall_s: Stall threshold in seconds.
        runlog_path: Path for the runlog; defaults to heartbeat_file
            parent / 'runlog.jsonl'.
        redispatch_counts_path: Path for dispatch counts; defaults to
            heartbeat_file parent / 'dispatch-counts.json'.

    Returns:
        A populated ObsConfig frozen dataclass.
    """
    base = heartbeat_file.parent
    if runlog_path is None:
        runlog_path = base / "runlog.jsonl"
    if redispatch_counts_path is None:
        redispatch_counts_path = base / "dispatch-counts.json"
    return ObsConfig(
        runlog_path=runlog_path,
        heartbeat_file=heartbeat_file,
        redispatch_window_ticks=10,
        redispatch_max=3,
        heartbeat_stall_s=heartbeat_stall_s,
        heartbeat_ping_url=heartbeat_ping_url,
        redispatch_counts_path=redispatch_counts_path,
    )


def _utc(ts: float = 0.0) -> datetime:
    """Return a tz-aware UTC datetime from a POSIX timestamp.

    Args:
        ts: POSIX timestamp in seconds.

    Returns:
        A tz-aware UTC datetime.
    """
    return datetime.fromtimestamp(ts, tz=timezone.utc)


# ---------------------------------------------------------------------------
# Test 1 -- Ping fired exactly once with the configured URL
# ---------------------------------------------------------------------------


def test_ping_fired_once_with_configured_url(tmp_path: Path) -> None:
    """_heartbeat_tick calls _ping_url once with obs.heartbeat_ping_url.

    When heartbeat_ping_url is set to a non-None URL, one tick must invoke
    the _ping_url seam exactly once.  The URL value passed must equal
    obs.heartbeat_ping_url.
    """
    ping_url = "https://hc-ping.com/abc-123"
    obs = _make_obs(tmp_path / "heartbeat", heartbeat_ping_url=ping_url)
    state = LivenessState()

    ping_mock = MagicMock()

    with (
        patch(_WRITE_SEAM),
        patch(_PING_SEAM, ping_mock),
    ):
        _heartbeat_tick(obs, state, now=lambda: _utc(0.0))

    ping_mock.assert_called_once()
    # The URL must appear somewhere in the call — positional or keyword.
    # Inspect both args and kwargs to remain shape-agnostic.
    all_args = ping_mock.call_args.args + tuple(
        ping_mock.call_args.kwargs.values()
    )
    assert ping_url in all_args, (
        f"_ping_url must be called with the configured URL {ping_url!r}; "
        f"actual call: {ping_mock.call_args!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 -- No ping at all when heartbeat_ping_url is None
# ---------------------------------------------------------------------------


def test_no_ping_when_heartbeat_ping_url_is_none(tmp_path: Path) -> None:
    """_heartbeat_tick does NOT call _ping_url when ping URL is None.

    When obs.heartbeat_ping_url is None (not configured), _ping_url must
    never be called — not even with a None argument.
    """
    obs = _make_obs(tmp_path / "heartbeat", heartbeat_ping_url=None)
    state = LivenessState()

    ping_mock = MagicMock()

    with (
        patch(_WRITE_SEAM),
        patch(_PING_SEAM, ping_mock),
    ):
        _heartbeat_tick(obs, state, now=lambda: _utc(0.0))

    ping_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3 -- Ping failure swallowed; liveness write still happens
# ---------------------------------------------------------------------------


def test_ping_failure_swallowed_and_write_still_occurs(
    tmp_path: Path,
) -> None:
    """Ping exception is swallowed; _heartbeat_tick still completes.

    When _ping_url raises, _heartbeat_tick must:
    (a) not propagate the exception (the monitor thread must survive), and
    (b) still invoke _write_heartbeat (liveness write is not aborted).

    This test does not depend on the relative order of write vs. ping —
    it only asserts that a ping failure cannot prevent either (a) or (b).
    """
    ping_url = "https://hc-ping.com/xyz-boom"
    obs = _make_obs(tmp_path / "heartbeat", heartbeat_ping_url=ping_url)
    state = LivenessState()

    write_mock = MagicMock()

    # Should NOT raise even though _ping_url raises.
    with (
        patch(_WRITE_SEAM, write_mock),
        patch(_PING_SEAM, side_effect=Exception("boom")),
    ):
        # (a) must not raise
        _heartbeat_tick(obs, state, now=lambda: _utc(0.0))

    # (b) liveness write must still have been called
    write_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Test 4 -- Ping failure does not skip downstream steps (runlog + stall)
# ---------------------------------------------------------------------------


def test_ping_failure_does_not_skip_runlog_emit_or_stall_alert(
    tmp_path: Path,
) -> None:
    """Ping exception is swallowed; downstream steps still execute.

    When _ping_url raises AND the state is past the stall threshold,
    _heartbeat_tick must:
    (a) not propagate the exception,
    (b) still invoke _write_heartbeat,
    (c) still call runlog.emit for the heartbeat event, and
    (d) still fire the stall alert via alert().

    This proves a ping failure cannot skip any downstream step regardless
    of where ping appears in the tick's execution order.
    """
    ping_url = "https://hc-ping.com/xyz-boom"
    stall_s = 10.0
    obs = _make_obs(
        tmp_path / "heartbeat",
        heartbeat_ping_url=ping_url,
        heartbeat_stall_s=stall_s,
    )

    # Configure state to be past the stall threshold.
    state = LivenessState()
    state.mark_in_progress("owner", "repo", 7, _utc(0.0))

    # Clock well past the threshold so stall check fires.
    t_breach = _utc(stall_s + 1.0)

    write_mock = MagicMock()
    runlog_mock = MagicMock()
    alert_calls: list[dict[str, Any]] = []

    def fake_alert(*args: Any, **kwargs: Any) -> bool:  # noqa: ANN401
        alert_calls.append({"args": args, "kwargs": kwargs})
        return True

    with (
        patch(_WRITE_SEAM, write_mock),
        patch(_PING_SEAM, side_effect=Exception("network error")),
        patch(_ALERT_SEAM, side_effect=fake_alert),
    ):
        # (a) must not raise
        _heartbeat_tick(obs, state, runlog=runlog_mock, now=lambda: t_breach)

    # (b) liveness write must still have been called
    write_mock.assert_called_once()

    # (c) runlog.emit must have been called for the heartbeat event
    assert runlog_mock.emit.called, (
        "runlog.emit must be called even when _ping_url raises; "
        f"emit call count: {runlog_mock.emit.call_count}"
    )
    heartbeat_emit_calls = [
        c
        for c in runlog_mock.emit.call_args_list
        if c.args
        and isinstance(c.args[0], dict)
        and c.args[0].get("event") == "heartbeat"
    ]
    assert heartbeat_emit_calls, (
        "runlog.emit must be called with event='heartbeat' even when "
        "_ping_url raises; actual emit calls: "
        f"{runlog_mock.emit.call_args_list}"
    )

    # (d) stall alert must still have fired
    assert alert_calls, (
        "alert() must still fire for a stall condition even when "
        "_ping_url raises; got no alert calls"
    )


# ---------------------------------------------------------------------------
# Test 5 -- _ping_url forwards timeout to urlopen
# ---------------------------------------------------------------------------


def test_ping_url_calls_urlopen_with_default_timeout() -> None:
    """_ping_url forwards _DEFAULT_PING_TIMEOUT_S to urlopen.

    The timeout value passed to urlopen must equal the module-level
    _DEFAULT_PING_TIMEOUT_S constant so a hung ping cannot stall the
    heartbeat beat.  The response must also be consumed: the context
    manager must be entered and exited (or .read() + .close() called).
    """
    target_url = "https://hc-ping.com/test-timeout"

    # Build a mock response that supports both the context-manager protocol
    # and the explicit .read()/.close() protocol so the assertion below
    # holds regardless of which the implementation uses.
    mock_resp = MagicMock()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch(_URLOPEN_SEAM, return_value=mock_resp) as mock_urlopen:
        _ping_url(target_url)

    # urlopen must have been called exactly once.
    mock_urlopen.assert_called_once()
    call_kwargs = mock_urlopen.call_args.kwargs
    call_args = mock_urlopen.call_args.args

    # The URL must be the first positional arg or a 'url' keyword.
    url_passed = call_args[0] if call_args else call_kwargs.get("url")
    assert url_passed == target_url, (
        f"urlopen must be called with the target URL {target_url!r}; "
        f"got: {url_passed!r}"
    )

    # timeout must equal _DEFAULT_PING_TIMEOUT_S (keyword or positional).
    timeout_passed = call_kwargs.get("timeout")
    if timeout_passed is None and len(call_args) >= 2:
        timeout_passed = call_args[1]
    assert timeout_passed == _DEFAULT_PING_TIMEOUT_S, (
        f"urlopen must be called with timeout={_DEFAULT_PING_TIMEOUT_S}; "
        f"got timeout={timeout_passed!r}"
    )

    # The response must be consumed via context manager OR explicit close.
    # Prefer context-manager check; fall back to .read()/.close() check so
    # the test remains valid both before and after the impl switches to
    # `with urlopen(...) as resp:`.
    cm_entered = mock_resp.__enter__.called
    cm_exited = mock_resp.__exit__.called
    explicitly_closed = mock_resp.close.called or mock_resp.read.called

    assert cm_entered or explicitly_closed, (
        "The response from urlopen must be consumed: either the context "
        "manager must be entered (__enter__ called) or .read()/.close() "
        "must be called explicitly; got neither."
    )
    if cm_entered:
        assert cm_exited, (
            "If the response context manager is entered (__enter__ called), "
            "__exit__ must also be called to release the connection."
        )


# ---------------------------------------------------------------------------
# Test 6 -- _ping_url rejects non-http/https schemes (forward-spec, RED)
# ---------------------------------------------------------------------------


def test_ping_url_rejects_non_http_schemes_without_calling_urlopen() -> None:
    """_ping_url must reject non-http/https schemes before calling urlopen.

    A ``file://``, ``ftp://``, or other non-http/https URL must be
    refused before urlopen is invoked.  This prevents SSRF-style
    accidental reads of local files or intranet resources.

    FORWARD-SPEC: this test is expected to be RED until the implementation
    pass adds scheme validation to _ping_url.  The expected failure is
    that urlopen IS called (because the scheme check does not yet exist),
    not a setup or import error.
    """
    dangerous_url = "file:///etc/passwd"

    with patch(_URLOPEN_SEAM) as mock_urlopen:
        try:
            _ping_url(dangerous_url)
        except Exception:  # noqa: BLE001
            # Any exception from _ping_url is acceptable as long as
            # urlopen was not called first.
            pass

    assert not mock_urlopen.called, (
        "urlopen must NOT be called for non-http/https schemes; "
        f"_ping_url({dangerous_url!r}) caused urlopen to be called: "
        f"{mock_urlopen.call_args_list}"
    )
