"""Tests for _heartbeat_tick ping-URL behavior (issues #79 / #85).

Covers acceptance criteria for the BH_HEARTBEAT_PING_URL best-effort ping
that _heartbeat_tick must perform after the liveness write on each tick.

AC coverage:
- Ping fired exactly once with configured URL when heartbeat_ping_url is set.
- No ping at all when heartbeat_ping_url is None.
- Ping failure is swallowed: _heartbeat_tick completes (does not raise),
  and _write_heartbeat is still called (liveness write is not aborted).

The _ping_url seam does NOT exist in the current codebase — tests reference
``baton_harness.chain.heartbeat._ping_url`` via string patch so the failure
reason is unambiguous (AttributeError naming the missing symbol).

No asyncio / pytest-asyncio dependency -- all tests are synchronous.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from baton_harness.chain.heartbeat import (
    LivenessState,
    _heartbeat_tick,
)
from baton_harness.chain.obs_config import ObsConfig

# ---------------------------------------------------------------------------
# Helpers (mirror test_heartbeat.py conventions exactly)
# ---------------------------------------------------------------------------

_PING_SEAM = "baton_harness.chain.heartbeat._ping_url"
_WRITE_SEAM = "baton_harness.chain.heartbeat._write_heartbeat"


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
