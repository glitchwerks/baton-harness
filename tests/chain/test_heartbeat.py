"""Tests for baton_harness.chain.heartbeat (thread-based re-arch, issue #78).

Covers acceptance criteria for the P1 thread-based heartbeat re-architecture:
the monitor is a daemon OS thread so it beats even while the asyncio event
loop is blocked inside the synchronous CI gate (time.sleep in merge.py).

AC coverage:
- _heartbeat_tick writes heartbeat file each call (seam: _write_heartbeat).
- _heartbeat_tick emits runlog event='heartbeat' when runlog provided.
- Stall fires once at severity='critical' with correct owner/repo/issue and
  runlog event='stall'; second call does NOT re-alert (debounce); after
  state.clear()+mark_in_progress a new breach CAN alert again.
- STRICT boundary: elapsed == stall_s exactly does NOT alert (> not >=).
- No false alarm: elapsed ~1800s with stall_s=7200 -- no alert, heartbeat
  still written.
- Guarded: _write_heartbeat raising is swallowed by _heartbeat_tick.
- _write_heartbeat real tmp_path: temp-then-os.replace mechanism.
- LivenessState unit: mark/clear set/null fields + reset debounce.
- run_heartbeat_loop: calls _heartbeat_tick repeatedly, stops promptly on
  stop_event.set(); thread-interruptible sleep (threading.Event.wait).
- P1 regression: monitor beats independently while main thread blocks
  (simulating CI-gate time.sleep).

No asyncio / pytest-asyncio dependency -- all tests are synchronous.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from baton_harness.chain.heartbeat import (
    LivenessState,
    _heartbeat_tick,
    _write_heartbeat,
    run_heartbeat_loop,
)
from baton_harness.chain.obs_config import ObsConfig
from baton_harness.chain.runlog import RunLog

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_obs(
    heartbeat_file: Path,
    *,
    heartbeat_stall_s: float = 7200.0,
    runlog_path: Path | None = None,
    redispatch_counts_path: Path | None = None,
) -> ObsConfig:
    """Construct a minimal ObsConfig for tests.

    Args:
        heartbeat_file: Path to the heartbeat file under test.
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
        heartbeat_ping_url=None,
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
# Test 1 -- _heartbeat_tick writes heartbeat file each call
# ---------------------------------------------------------------------------


def test_tick_writes_heartbeat_file_with_now_isoformat(
    tmp_path: Path,
) -> None:
    """_heartbeat_tick calls _write_heartbeat with now().isoformat().

    Each call to _heartbeat_tick must invoke the _write_heartbeat seam
    once, passing obs.heartbeat_file and the ISO-format timestamp from
    the injected now() callable.
    """
    obs = _make_obs(tmp_path / "heartbeat")
    state = LivenessState()
    fixed_ts = _utc(1000.0)

    write_calls: list[tuple[Path, str]] = []

    def fake_write(path: Path, timestamp: str) -> None:
        write_calls.append((path, timestamp))

    with patch(
        "baton_harness.chain.heartbeat._write_heartbeat",
        side_effect=fake_write,
    ):
        _heartbeat_tick(obs, state, now=lambda: fixed_ts)
        _heartbeat_tick(obs, state, now=lambda: fixed_ts)

    assert len(write_calls) == 2, (
        f"_write_heartbeat must be called once per tick; "
        f"got {len(write_calls)} calls"
    )
    for path, ts_str in write_calls:
        assert path == obs.heartbeat_file
        assert ts_str == fixed_ts.isoformat()


# ---------------------------------------------------------------------------
# Test 2 -- _heartbeat_tick emits runlog event='heartbeat' when runlog given
# ---------------------------------------------------------------------------


def test_tick_emits_heartbeat_runlog_event_when_runlog_provided(
    tmp_path: Path,
) -> None:
    """_heartbeat_tick emits event='heartbeat' to runlog when provided.

    When a RunLog is passed, each _heartbeat_tick call must append a
    JSON object with event='heartbeat' to the runlog file.
    """
    obs = _make_obs(tmp_path / "heartbeat")
    runlog_path = tmp_path / "runlog.jsonl"
    runlog = RunLog(runlog_path)
    state = LivenessState()

    with patch("baton_harness.chain.heartbeat._write_heartbeat"):
        _heartbeat_tick(obs, state, runlog=runlog, now=lambda: _utc(0.0))
        _heartbeat_tick(obs, state, runlog=runlog, now=lambda: _utc(1.0))

    lines = runlog_path.read_text(encoding="utf-8").splitlines()
    events = [json.loads(ln) for ln in lines if ln.strip()]
    hb_events = [e for e in events if e.get("event") == "heartbeat"]
    assert len(hb_events) == 2, (
        f"Expected 2 heartbeat runlog events (one per tick); "
        f"got {len(hb_events)}: {hb_events}"
    )


# ---------------------------------------------------------------------------
# Test 3 -- Stall fires once, debounced, resets after clear+mark_in_progress
# ---------------------------------------------------------------------------


def test_stall_fires_once_at_critical_with_correct_identity(
    tmp_path: Path,
) -> None:
    """Stall fires exactly once at critical severity with correct identity.

    First call past stall_s: alert fires once with severity='critical',
    correct owner/repo/issue, and a runlog event='stall'.
    Second call still past stall_s: NO second alert (debounce holds).
    """
    owner, repo, issue_num = "glitchwerks", "baton-harness", 42
    stall_s = 100.0
    obs = _make_obs(tmp_path / "heartbeat", heartbeat_stall_s=stall_s)
    runlog_path = tmp_path / "runlog.jsonl"
    runlog = RunLog(runlog_path)
    state = LivenessState()

    t0 = _utc(0.0)
    state.mark_in_progress(owner, repo, issue_num, t0)

    # Clock is past stall threshold for both calls.
    t_breach = _utc(stall_s + 1.0)

    alert_calls: list[dict[str, Any]] = []

    def fake_alert(
        a_owner: str,
        a_repo: str,
        a_issue: int | None,
        msg: str,
        *,
        severity: str,
        kind: str = "block",
        runlog: RunLog | None = None,
    ) -> bool:
        alert_calls.append(
            {
                "owner": a_owner,
                "repo": a_repo,
                "issue": a_issue,
                "severity": severity,
                "kind": kind,
            }
        )
        return True

    with (
        patch("baton_harness.chain.heartbeat._write_heartbeat"),
        patch(
            "baton_harness.chain.heartbeat.alert",
            side_effect=fake_alert,
        ),
    ):
        _heartbeat_tick(obs, state, runlog=runlog, now=lambda: t_breach)
        _heartbeat_tick(obs, state, runlog=runlog, now=lambda: t_breach)

    critical_calls = [a for a in alert_calls if a["severity"] == "critical"]
    assert len(critical_calls) == 1, (
        f"Expected exactly 1 critical stall alert; "
        f"got {len(critical_calls)}: {critical_calls}"
    )
    a = critical_calls[0]
    assert a["owner"] == owner
    assert a["repo"] == repo
    assert a["issue"] == issue_num

    lines = runlog_path.read_text(encoding="utf-8").splitlines()
    events = [json.loads(ln) for ln in lines if ln.strip()]
    stall_events = [e for e in events if e.get("event") == "stall"]
    assert stall_events, (
        "Expected at least one runlog event with event='stall'; "
        f"got events: {[e.get('event') for e in events]}"
    )


def test_stall_alert_fires_again_after_clear_and_mark_in_progress(
    tmp_path: Path,
) -> None:
    """A new stall CAN alert after clear()+mark_in_progress resets debounce.

    Episode 1: breach -> one alert.
    state.clear() + state.mark_in_progress(new t0) -> debounce reset.
    Episode 2: breach again -> one more alert.
    Total expected: 2 alerts.
    """
    owner, repo, issue_num = "acme", "widget", 99
    stall_s = 50.0
    obs = _make_obs(tmp_path / "heartbeat", heartbeat_stall_s=stall_s)
    state = LivenessState()
    state.mark_in_progress(owner, repo, issue_num, _utc(0.0))

    alert_calls: list[dict[str, Any]] = []

    def fake_alert(*args: Any, **kwargs: Any) -> bool:  # noqa: ANN401
        alert_calls.append({"args": args, "kwargs": kwargs})
        return True

    with (
        patch("baton_harness.chain.heartbeat._write_heartbeat"),
        patch(
            "baton_harness.chain.heartbeat.alert",
            side_effect=fake_alert,
        ),
    ):
        # Episode 1 -- breach
        _heartbeat_tick(obs, state, now=lambda: _utc(stall_s + 1.0))

        # Reset debounce via clear + re-mark
        state.clear()
        state.mark_in_progress(owner, repo, issue_num, _utc(stall_s + 2.0))

        # Episode 2 -- breach again from new t0
        _heartbeat_tick(obs, state, now=lambda: _utc(stall_s * 2 + 3.0))

    assert len(alert_calls) == 2, (
        "Expected 2 stall alerts (one per episode after reset); "
        f"got {len(alert_calls)}"
    )


# ---------------------------------------------------------------------------
# Test 4 -- STRICT boundary: elapsed == stall_s does NOT alert
# ---------------------------------------------------------------------------


def test_stall_exact_boundary_does_not_alert(tmp_path: Path) -> None:
    """At elapsed == heartbeat_stall_s exactly, no stall alert must fire.

    The contract is strictly > (not >=): elapsed must EXCEED the threshold
    before a stall fires.  This test locks the > boundary.
    """
    owner, repo, issue_num = "acme", "widget", 11
    stall_s = 300.0
    obs = _make_obs(tmp_path / "heartbeat", heartbeat_stall_s=stall_s)
    state = LivenessState()
    state.mark_in_progress(owner, repo, issue_num, _utc(0.0))

    # Elapsed is exactly stall_s -- boundary, not past it.
    t_exact = _utc(stall_s)

    alert_calls: list[dict[str, Any]] = []

    def fake_alert(*args: Any, **kwargs: Any) -> bool:  # noqa: ANN401
        alert_calls.append({"args": args, "kwargs": kwargs})
        return True

    with (
        patch("baton_harness.chain.heartbeat._write_heartbeat"),
        patch(
            "baton_harness.chain.heartbeat.alert",
            side_effect=fake_alert,
        ),
    ):
        _heartbeat_tick(obs, state, now=lambda: t_exact)
        _heartbeat_tick(obs, state, now=lambda: t_exact)

    assert not alert_calls, (
        "elapsed == heartbeat_stall_s must NOT fire a stall alert "
        "(contract is strictly >, not >=); "
        f"got {len(alert_calls)} alert(s)"
    )


# ---------------------------------------------------------------------------
# Test 5 -- No false alarm when elapsed < stall_s but heartbeat still written
# ---------------------------------------------------------------------------


def test_no_stall_alert_when_elapsed_below_threshold(
    tmp_path: Path,
) -> None:
    """No stall alert when elapsed (~1800s) < stall_s (7200s); tick writes.

    A 1800s-old in-progress issue with stall_s=7200 must NOT trigger a
    stall alert, but _write_heartbeat must still be called each tick.
    """
    owner, repo, issue_num = "acme", "widget", 5
    stall_s = 7200.0
    obs = _make_obs(tmp_path / "heartbeat", heartbeat_stall_s=stall_s)
    state = LivenessState()
    state.mark_in_progress(owner, repo, issue_num, _utc(0.0))

    t_safe = _utc(1800.0)

    alert_calls: list[dict[str, Any]] = []
    write_calls: list[str] = []

    def fake_alert(*args: Any, **kwargs: Any) -> bool:  # noqa: ANN401
        alert_calls.append({"args": args, "kwargs": kwargs})
        return True

    with (
        patch(
            "baton_harness.chain.heartbeat._write_heartbeat",
            side_effect=lambda p, ts: write_calls.append(ts),
        ),
        patch(
            "baton_harness.chain.heartbeat.alert",
            side_effect=fake_alert,
        ),
    ):
        _heartbeat_tick(obs, state, now=lambda: t_safe)
        _heartbeat_tick(obs, state, now=lambda: t_safe)
        _heartbeat_tick(obs, state, now=lambda: t_safe)

    assert not alert_calls, (
        "Must not alert for a 1800s-old issue with stall_s=7200; "
        f"got alerts: {alert_calls}"
    )
    assert len(write_calls) == 3, (
        "Heartbeat writes must still occur below stall threshold; "
        f"got {len(write_calls)} writes"
    )


# ---------------------------------------------------------------------------
# Test 6 -- Guarded: _write_heartbeat raising is swallowed
# ---------------------------------------------------------------------------


def test_tick_swallows_write_heartbeat_exception(tmp_path: Path) -> None:
    """_heartbeat_tick swallows exceptions from _write_heartbeat.

    If _write_heartbeat raises (e.g. disk full), _heartbeat_tick must
    catch and suppress the error without propagating.
    """
    obs = _make_obs(tmp_path / "heartbeat")
    state = LivenessState()

    def always_raises(path: Path, ts: str) -> None:
        raise OSError("simulated disk full")

    # Must not raise.
    with patch(
        "baton_harness.chain.heartbeat._write_heartbeat",
        side_effect=always_raises,
    ):
        _heartbeat_tick(obs, state, now=lambda: _utc(0.0))


# ---------------------------------------------------------------------------
# Test 7 -- _write_heartbeat real tmp_path: temp-then-os.replace mechanism
# ---------------------------------------------------------------------------


def test_write_heartbeat_uses_temp_then_os_replace(tmp_path: Path) -> None:
    """_write_heartbeat atomically writes via temp file then os.replace.

    Patches os.replace with a spy that delegates to the real impl.
    Asserts os.replace is called with target path as dst and the target
    file contains the timestamp afterward.
    """
    target = tmp_path / "heartbeat"
    ts = "2026-06-14T12:00:00.000000+00:00"

    replace_calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy_replace(src: str, dst: str) -> None:
        replace_calls.append((str(src), str(dst)))
        real_replace(src, dst)

    with patch("os.replace", side_effect=spy_replace):
        _write_heartbeat(target, ts)

    assert target.exists(), "_write_heartbeat must create the target file"
    content = target.read_text(encoding="utf-8")
    assert ts in content, (
        f"Target file must contain the timestamp; got: {content!r}"
    )
    assert replace_calls, "os.replace must be used (temp-then-replace)"
    _, dst = replace_calls[-1]
    assert dst == str(target), (
        f"os.replace destination must be target path {target}; got {dst!r}"
    )


# ---------------------------------------------------------------------------
# Test 8 -- LivenessState unit: mark/clear set/null fields + debounce
# ---------------------------------------------------------------------------


def test_liveness_state_mark_in_progress_sets_all_fields() -> None:
    """mark_in_progress populates all four public fields of LivenessState."""
    state = LivenessState()
    ts = _utc(1000.0)
    state.mark_in_progress("owner1", "repo1", 7, ts)

    assert state.in_progress_owner == "owner1"
    assert state.in_progress_repo == "repo1"
    assert state.in_progress_issue == 7
    assert state.in_progress_since == ts


def test_liveness_state_clear_nulls_all_fields() -> None:
    """clear() sets all four public fields back to None."""
    state = LivenessState()
    state.mark_in_progress("owner1", "repo1", 7, _utc(1000.0))
    state.clear()

    assert state.in_progress_owner is None
    assert state.in_progress_repo is None
    assert state.in_progress_issue is None
    assert state.in_progress_since is None


def test_liveness_state_mark_in_progress_resets_stall_debounce(
    tmp_path: Path,
) -> None:
    """mark_in_progress resets the stall-debounce flag.

    After an alert fires (debounce engaged), mark_in_progress must allow
    the next breach to alert again -- without requiring state.clear() first.
    """
    owner, repo, issue_num = "x", "y", 3
    stall_s = 10.0
    obs = _make_obs(tmp_path / "heartbeat", heartbeat_stall_s=stall_s)
    state = LivenessState()
    state.mark_in_progress(owner, repo, issue_num, _utc(0.0))

    alert_calls: list[int] = []

    def fake_alert(*args: Any, **kwargs: Any) -> bool:  # noqa: ANN401
        alert_calls.append(1)
        return True

    with (
        patch("baton_harness.chain.heartbeat._write_heartbeat"),
        patch(
            "baton_harness.chain.heartbeat.alert",
            side_effect=fake_alert,
        ),
    ):
        # Episode 1: fire first alert
        _heartbeat_tick(obs, state, now=lambda: _utc(stall_s + 1.0))
        assert len(alert_calls) == 1, "First episode must fire an alert"

        # Reset via mark_in_progress (NOT clear()) -- must also reset debounce
        state.mark_in_progress(owner, repo, issue_num, _utc(stall_s + 2.0))

        # Episode 2: advance past new t0 + stall_s
        _heartbeat_tick(obs, state, now=lambda: _utc(stall_s * 2 + 3.0))

    assert len(alert_calls) == 2, (
        "mark_in_progress must reset debounce; expected 2nd alert to fire, "
        f"got {len(alert_calls)} total alert(s)"
    )


def test_liveness_state_clear_resets_debounce_and_re_mark_works(
    tmp_path: Path,
) -> None:
    """clear() plus re-mark_in_progress allows a fresh stall episode.

    Verifies post-clear state is truly blank and a re-mark starts a
    fresh episode that can alert again.
    """
    owner, repo, issue_num = "a", "b", 1
    stall_s = 10.0
    obs = _make_obs(tmp_path / "heartbeat", heartbeat_stall_s=stall_s)
    state = LivenessState()
    state.mark_in_progress(owner, repo, issue_num, _utc(0.0))

    alert_calls: list[int] = []

    def fake_alert(*args: Any, **kwargs: Any) -> bool:  # noqa: ANN401
        alert_calls.append(1)
        return True

    with (
        patch("baton_harness.chain.heartbeat._write_heartbeat"),
        patch(
            "baton_harness.chain.heartbeat.alert",
            side_effect=fake_alert,
        ),
    ):
        # Episode 1
        _heartbeat_tick(obs, state, now=lambda: _utc(stall_s + 1.0))
        assert len(alert_calls) == 1

        # Full clear + re-mark
        state.clear()
        assert state.in_progress_since is None

        state.mark_in_progress(owner, repo, issue_num, _utc(stall_s + 2.0))
        assert state.in_progress_since == _utc(stall_s + 2.0)

        # Episode 2
        _heartbeat_tick(obs, state, now=lambda: _utc(stall_s * 2 + 3.0))

    assert len(alert_calls) == 2, (
        "clear()+mark_in_progress must enable a second stall alert; "
        f"got {len(alert_calls)}"
    )


# ---------------------------------------------------------------------------
# Test 9 -- run_heartbeat_loop calls tick repeatedly and stops on stop_event
# ---------------------------------------------------------------------------


def test_run_heartbeat_loop_ticks_repeatedly_and_stops_on_event(
    tmp_path: Path,
) -> None:
    """run_heartbeat_loop calls _heartbeat_tick repeatedly; stops promptly.

    Uses a real threading.Event with a small interval (0.01s).
    Asserts >= 2 ticks occurred before stop_event fired.
    Verifies thread exits within a short join timeout.
    """
    obs = _make_obs(tmp_path / "heartbeat")
    state = LivenessState()
    stop_event = threading.Event()
    tick_count: list[int] = [0]

    def counting_write(path: Path, ts: str) -> None:
        tick_count[0] += 1

    thread = threading.Thread(
        target=run_heartbeat_loop,
        kwargs={
            "obs": obs,
            "state": state,
            "stop_event": stop_event,
            "interval_s": 0.01,
            "now": lambda: _utc(0.0),
        },
        daemon=True,
    )

    with patch(
        "baton_harness.chain.heartbeat._write_heartbeat",
        side_effect=counting_write,
    ):
        thread.start()
        # Give it time for >= 2 ticks at 0.01s interval
        time.sleep(0.08)
        stop_event.set()
        thread.join(timeout=2.0)

    assert not thread.is_alive(), (
        "run_heartbeat_loop thread must exit within 2s of stop_event.set()"
    )
    assert tick_count[0] >= 2, (
        f"Expected >= 2 ticks before stopping; got {tick_count[0]}"
    )


# ---------------------------------------------------------------------------
# Test 10 -- P1 REGRESSION: monitor beats while main thread blocks
# ---------------------------------------------------------------------------


def test_heartbeat_loop_beats_while_main_thread_blocks(
    tmp_path: Path,
) -> None:
    """Heartbeat loop beats independently while main thread blocks.

    This is the P1 regression test: the thread-based monitor must
    continue writing heartbeats even while the caller is blocked in a
    synchronous time.sleep (simulating the CI-gate block in merge.py).

    Uses a tiny interval (0.01s) and a main-thread block of ~0.1s
    (several cadences).  Asserts >= 2 writes occurred DURING the block,
    proving the monitor is independent of the caller's thread.
    """
    obs = _make_obs(tmp_path / "heartbeat")
    state = LivenessState()
    stop_event = threading.Event()
    write_count: list[int] = [0]

    def counting_write(path: Path, ts: str) -> None:
        write_count[0] += 1

    thread = threading.Thread(
        target=run_heartbeat_loop,
        kwargs={
            "obs": obs,
            "state": state,
            "stop_event": stop_event,
            "interval_s": 0.01,
            "now": lambda: _utc(0.0),
        },
        daemon=True,
    )

    try:
        with patch(
            "baton_harness.chain.heartbeat._write_heartbeat",
            side_effect=counting_write,
        ):
            thread.start()

            # Simulate the CI-gate blocking the main thread.
            time.sleep(0.1)

            count_during_block = write_count[0]
    finally:
        stop_event.set()
        thread.join(timeout=2.0)

    assert not thread.is_alive(), (
        "Heartbeat thread must exit cleanly after stop_event.set()"
    )
    assert count_during_block >= 2, (
        "Monitor must have written >= 2 heartbeats during the main-thread "
        f"block (proving thread independence); got {count_during_block}"
    )
