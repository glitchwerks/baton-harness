"""Tests for baton_harness.chain.heartbeat.

Covers all acceptance criteria for GitHub issue #78
(P4a: decoupled heartbeat coroutine + in-daemon stall detection):

- AC1: Heartbeat cadence is independent of work duration.
- AC2: Liveness (_write_heartbeat) and progress (runlog) are distinct
       signals produced by distinct components.
- AC3: In-daemon stall detection fires once per episode at critical
       severity, is debounced, and resets on clear()/mark_in_progress().
- Additional unit: _write_heartbeat uses temp-then-os.replace; the loop
  is guarded against write failures; LivenessState field/debounce
  behaviour.

Async coroutines are driven with asyncio.run() — no pytest-asyncio
dependency.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from baton_harness.chain.heartbeat import (
    LivenessState,
    _write_heartbeat,
    heartbeat_monitor,
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


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    """Return a successful CompletedProcess stub."""
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=""
    )


# ---------------------------------------------------------------------------
# AC1 — Cadence independent of work duration
# ---------------------------------------------------------------------------


def test_heartbeat_fires_k_times_on_own_cadence(
    tmp_path: Path,
) -> None:
    """Heartbeat writes K times driven by its own cadence, no work unit.

    No _poll_and_run / work-unit involvement.  The injected sleep advances
    a fake clock and sets stop_event after K ticks.  Asserts
    _write_heartbeat was called exactly K times on obs.heartbeat_file.
    """
    iterations = 5
    obs = _make_obs(tmp_path / "heartbeat")
    state = LivenessState()
    stop = asyncio.Event()
    clock: list[float] = [0.0]
    tick_count: list[int] = [0]

    async def fake_sleep(seconds: float) -> None:
        """Advance clock; stop after *iterations* ticks."""
        clock[0] += seconds
        tick_count[0] += 1
        if tick_count[0] >= iterations:
            stop.set()

    def fake_now() -> datetime:
        return _utc(clock[0])

    write_calls: list[tuple[Path, str]] = []

    def fake_write(path: Path, timestamp: str) -> None:
        write_calls.append((path, timestamp))

    with patch(
        "baton_harness.chain.heartbeat._write_heartbeat",
        side_effect=fake_write,
    ):
        asyncio.run(
            heartbeat_monitor(
                obs,
                state,
                interval_s=1.0,
                sleep=fake_sleep,
                now=fake_now,
                stop_event=stop,
            )
        )

    assert len(write_calls) == iterations, (
        f"Expected {iterations} heartbeat writes (cadence-only); "
        f"got {len(write_calls)}"
    )
    for path, _ in write_calls:
        assert path == obs.heartbeat_file


# ---------------------------------------------------------------------------
# AC2 — Liveness and progress are distinct signals
# ---------------------------------------------------------------------------


def test_heartbeat_monitor_emits_liveness_write_each_iteration(
    tmp_path: Path,
) -> None:
    """heartbeat_monitor calls _write_heartbeat each iteration.

    The liveness signal is exclusively the heartbeat file, written by
    heartbeat_monitor.  Verifies the seam is touched at least twice.
    """
    obs = _make_obs(tmp_path / "heartbeat")
    state = LivenessState()
    stop = asyncio.Event()
    clock: list[float] = [0.0]
    ticked: list[int] = [0]

    async def fake_sleep(seconds: float) -> None:
        clock[0] += seconds
        ticked[0] += 1
        if ticked[0] >= 2:
            stop.set()

    def fake_now() -> datetime:
        return _utc(clock[0])

    written: list[str] = []

    def fake_write(path: Path, ts: str) -> None:
        written.append(ts)

    with patch(
        "baton_harness.chain.heartbeat._write_heartbeat",
        side_effect=fake_write,
    ):
        asyncio.run(
            heartbeat_monitor(
                obs,
                state,
                interval_s=1.0,
                sleep=fake_sleep,
                now=fake_now,
                stop_event=stop,
            )
        )

    assert len(written) >= 2, "Liveness file must be written on each iteration"


def test_poll_and_run_does_not_touch_heartbeat_write_seam(
    tmp_path: Path,
) -> None:
    """A daemon tick (once=True) never calls heartbeat._write_heartbeat.

    Progress = runlog tick event (emitted by daemon).
    Liveness = heartbeat file (emitted exclusively by heartbeat_monitor).
    Verifies the liveness seam is NOT wired into the daemon poll path.
    """
    import baton_harness.chain.daemon as daemon_mod
    from baton_harness.chain.daemon import run_daemon
    from baton_harness.chain.merge import MergeOutcome
    from baton_harness.chain.recovery import RecoveryResult
    from baton_harness.chain.registry import RepoConfig
    from baton_harness.vendor.symphony.config import WorkflowConfig

    ready_issues = [
        {
            "number": 10,
            "title": "Issue 10",
            "state": "open",
            "body": "",
            "url": "https://github.com/o/r/issues/10",
            "labels": [{"name": "agent-ready"}],
            "milestone": None,
            "assignees": [],
        }
    ]

    def run_side(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        import json as _j

        cmd_str = " ".join(cmd)
        if "issue" in cmd_str and "list" in cmd_str:
            return _ok(_j.dumps(ready_issues))
        if "issue" in cmd_str and "view" in cmd_str and "edit" not in cmd_str:
            nums = [p for p in cmd if p.isdigit()]
            n = int(nums[0]) if nums else 10
            return _ok(
                _j.dumps(
                    {
                        "number": n,
                        "title": f"Issue {n}",
                        "state": "open",
                        "body": "",
                        "url": f"https://github.com/o/r/issues/{n}",
                        "labels": [{"name": "agent-done"}],
                        "assignees": [],
                    }
                )
            )
        if "issue" in cmd_str and "edit" in cmd_str:
            return _ok()
        if "pr" in cmd_str and "list" in cmd_str:
            prs = [
                {
                    "number": 1,
                    "headRefName": "baton/issue-10-10",
                    "headRefOid": "abc123",
                }
            ]
            return _ok(_j.dumps(prs))
        if "pr" in cmd_str and "create" in cmd_str:
            return _ok("https://github.com/o/r/pull/99")
        if "git" in cmd_str and "push" in cmd_str:
            return _ok()
        if "ls-remote" in cmd_str:
            return _ok("")
        if "rev-parse" in cmd_str:
            return _ok("abc123deadbeef\n")
        return _ok()

    wf_cfg = WorkflowConfig(
        prompt_template="Work on #{{ issue.number }}",
        tracker_labels=["agent-ready"],
        tracker_exclude_labels=["blocked"],
        tracker_assignee=None,
        max_concurrent=1,
        max_turns=8,
        hook_after_create=None,
        hook_before_run=None,
        hook_after_run=None,
        hook_timeout_ms=5000,
        poll_interval_ms=1000,
        max_retry_backoff_ms=10000,
    )
    repo_cfg = RepoConfig(
        owner="glitchwerks",
        repo="baton-harness",
        project_root=Path("/fake/repo"),
    )

    write_heartbeat_calls: list[tuple[Any, Any]] = []

    def spy_write(path: Path, ts: str) -> None:
        write_heartbeat_calls.append((path, ts))

    with (
        patch.object(daemon_mod, "_run", side_effect=run_side),
        patch(
            "baton_harness.chain.daemon.fetch_blocked_by",
            return_value=[],
        ),
        patch("baton_harness.chain.branches.create_feature_branch"),
        patch("baton_harness.chain.branches.checkout_feature_branch"),
        patch(
            "baton_harness.chain.branches.record_cut_point",
            return_value="deadbeef" * 5,
        ),
        patch(
            "baton_harness.chain.recovery.reconstruct",
            return_value=RecoveryResult(
                done=set(),
                parked_seed=set(),
                ci_gate_reentry=set(),
                redispatch=set(),
            ),
        ),
        patch(
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon.alert", return_value=True),
        patch(
            "baton_harness.vendor.symphony.orchestrator.Orchestrator"
            "._run_worker",
            new_callable=AsyncMock,
            return_value="pr_created",
        ),
        patch(
            "baton_harness.chain.heartbeat._write_heartbeat",
            side_effect=spy_write,
        ),
    ):
        asyncio.run(
            run_daemon(
                wf_cfg,
                [repo_cfg],
                once=True,
                poll_interval_s=0,
            )
        )

    assert write_heartbeat_calls == [], (
        "_poll_and_run (daemon tick) must never call "
        "heartbeat._write_heartbeat; liveness is heartbeat_monitor's job. "
        f"Got calls: {write_heartbeat_calls}"
    )


# ---------------------------------------------------------------------------
# AC3 — Stall detection: fires once, critical, debounced, resettable
# ---------------------------------------------------------------------------


def test_stall_alert_fires_once_at_critical_severity(
    tmp_path: Path,
) -> None:
    """Stall fires exactly once (critical) when in_progress_since breached.

    Drive two iterations both past the stall threshold; assert alert is
    called exactly once with severity='critical' and correct
    owner/repo/issue.  Assert a runlog event with event='stall' is
    emitted.  No second alert fires (debounce holds).
    """
    owner, repo, issue_num = "glitchwerks", "baton-harness", 42
    stall_s = 100.0
    obs = _make_obs(tmp_path / "heartbeat", heartbeat_stall_s=stall_s)
    runlog_path = tmp_path / "runlog.jsonl"
    runlog = RunLog(runlog_path)
    state = LivenessState()

    t0 = _utc(0.0)
    state.mark_in_progress(owner, repo, issue_num, t0)

    stop = asyncio.Event()
    clock: list[float] = [stall_s + 1.0]
    iteration: list[int] = [0]

    async def fake_sleep(seconds: float) -> None:
        clock[0] += seconds
        iteration[0] += 1
        if iteration[0] >= 2:
            stop.set()

    def fake_now() -> datetime:
        return _utc(clock[0])

    alert_calls: list[dict[str, Any]] = []

    def fake_alert(  # noqa: ANN401
        a_owner: str,
        a_repo: str,
        a_issue: int | None,
        msg: str,
        *,
        severity: str,
        kind: str = "block",
        runlog: Any = None,  # noqa: ANN401
    ) -> bool:
        alert_calls.append(
            {
                "owner": a_owner,
                "repo": a_repo,
                "issue": a_issue,
                "msg": msg,
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
        asyncio.run(
            heartbeat_monitor(
                obs,
                state,
                runlog=runlog,
                interval_s=1.0,
                sleep=fake_sleep,
                now=fake_now,
                stop_event=stop,
            )
        )

    stall_alerts = [a for a in alert_calls if a["severity"] == "critical"]
    assert len(stall_alerts) == 1, (
        f"Expected exactly 1 critical stall alert; "
        f"got {len(stall_alerts)}: {stall_alerts}"
    )
    a = stall_alerts[0]
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


def test_stall_debounce_no_second_alert_on_consecutive_breach(
    tmp_path: Path,
) -> None:
    """Second iteration past stall threshold must NOT re-trigger alert.

    After the first stall fires, subsequent qualifying iterations are
    silently skipped (debounced) until clear()/mark_in_progress() resets.
    """
    owner, repo, issue_num = "acme", "widget", 7
    stall_s = 50.0
    obs = _make_obs(tmp_path / "heartbeat", heartbeat_stall_s=stall_s)
    state = LivenessState()
    state.mark_in_progress(owner, repo, issue_num, _utc(0.0))

    stop = asyncio.Event()
    clock: list[float] = [stall_s + 1.0]
    iteration: list[int] = [0]

    async def fake_sleep(seconds: float) -> None:
        clock[0] += seconds
        iteration[0] += 1
        if iteration[0] >= 4:
            stop.set()

    def fake_now() -> datetime:
        return _utc(clock[0])

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
        asyncio.run(
            heartbeat_monitor(
                obs,
                state,
                interval_s=1.0,
                sleep=fake_sleep,
                now=fake_now,
                stop_event=stop,
            )
        )

    assert len(alert_calls) == 1, (
        "Debounce must suppress repeated stall alerts within one episode; "
        f"alert was called {len(alert_calls)} times"
    )


def test_stall_resets_after_clear_and_mark_in_progress(
    tmp_path: Path,
) -> None:
    """A new stall CAN alert again after state.clear()+mark_in_progress.

    Episode 1: breach → one alert fires.
    state.clear() then state.mark_in_progress(new t0) → debounce reset.
    Episode 2: breach again → one more alert fires.
    Total expected: 2 alerts.
    """
    owner, repo, issue_num = "acme", "widget", 99
    stall_s = 50.0
    obs = _make_obs(tmp_path / "heartbeat", heartbeat_stall_s=stall_s)
    state = LivenessState()
    state.mark_in_progress(owner, repo, issue_num, _utc(0.0))

    stop = asyncio.Event()
    clock: list[float] = [stall_s + 1.0]
    iteration: list[int] = [0]

    async def fake_sleep(seconds: float) -> None:
        """Advance clock; reset state between episode 1 and 2."""
        clock[0] += seconds
        iteration[0] += 1
        if iteration[0] == 1:
            # Reset: debounce must clear so episode 2 can fire.
            state.clear()
            state.mark_in_progress(owner, repo, issue_num, _utc(clock[0]))
        if iteration[0] >= 4:
            stop.set()

    def fake_now() -> datetime:
        return _utc(clock[0])

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
        asyncio.run(
            heartbeat_monitor(
                obs,
                state,
                interval_s=stall_s,
                sleep=fake_sleep,
                now=fake_now,
                stop_event=stop,
            )
        )

    assert len(alert_calls) == 2, (
        "Expected exactly 2 stall alerts (one per episode); "
        f"got {len(alert_calls)}"
    )


# ---------------------------------------------------------------------------
# AC3 threshold — no false alarm for a normal long block
# ---------------------------------------------------------------------------


def test_no_stall_alert_for_normal_long_running_issue(
    tmp_path: Path,
) -> None:
    """No stall alert fires when elapsed < heartbeat_stall_s (7200s).

    A 1800s-old in-progress issue (normal 30-min CI block) with
    heartbeat_stall_s=7200 must NOT trigger a stall alert, but
    heartbeats must still be written.
    """
    owner, repo, issue_num = "acme", "widget", 5
    stall_s = 7200.0
    obs = _make_obs(tmp_path / "heartbeat", heartbeat_stall_s=stall_s)
    state = LivenessState()
    state.mark_in_progress(owner, repo, issue_num, _utc(0.0))

    stop = asyncio.Event()
    # Clock starts at 1800s — well inside the 7200s stall window.
    clock: list[float] = [1800.0]
    iteration: list[int] = [0]

    async def fake_sleep(seconds: float) -> None:
        clock[0] += seconds
        iteration[0] += 1
        if iteration[0] >= 3:
            stop.set()

    def fake_now() -> datetime:
        return _utc(clock[0])

    alert_calls: list[dict[str, Any]] = []

    def fake_alert(*args: Any, **kwargs: Any) -> bool:  # noqa: ANN401
        alert_calls.append({"args": args, "kwargs": kwargs})
        return True

    write_calls: list[str] = []

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
        asyncio.run(
            heartbeat_monitor(
                obs,
                state,
                interval_s=30.0,
                sleep=fake_sleep,
                now=fake_now,
                stop_event=stop,
            )
        )

    assert not alert_calls, (
        "Must not alert for a 1800s-old issue with stall_s=7200.0; "
        f"got alerts: {alert_calls}"
    )
    assert write_calls, (
        "Heartbeat writes must still occur below stall threshold"
    )


def test_stall_boundary_exact_threshold_does_not_alert(
    tmp_path: Path,
) -> None:
    """At elapsed == heartbeat_stall_s exactly, no stall alert must fire.

    The contract is strictly greater-than (>): a stall fires only when the
    issue has been in progress *longer than* the threshold.  At the exact
    boundary (elapsed == stall_s) the alert must remain silent.

    This test is the regression lock for the > vs >= boundary: it will
    FAIL if the implementation uses >= instead of >.
    """
    owner, repo, issue_num = "acme", "widget", 11
    stall_s = 300.0
    obs = _make_obs(tmp_path / "heartbeat", heartbeat_stall_s=stall_s)
    state = LivenessState()
    # in_progress_since = t=0; clock starts at exactly stall_s so that
    # elapsed = stall_s - 0 = stall_s (boundary, not past).
    state.mark_in_progress(owner, repo, issue_num, _utc(0.0))

    stop = asyncio.Event()
    clock: list[float] = [stall_s]  # elapsed == stall_s exactly
    iteration: list[int] = [0]

    async def fake_sleep(seconds: float) -> None:
        """Advance clock by 0 each tick so elapsed stays at stall_s."""
        clock[0] += 0.0  # no advance — hold at exact boundary
        iteration[0] += 1
        if iteration[0] >= 2:
            stop.set()

    def fake_now() -> datetime:
        return _utc(clock[0])

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
        asyncio.run(
            heartbeat_monitor(
                obs,
                state,
                interval_s=0.0,
                sleep=fake_sleep,
                now=fake_now,
                stop_event=stop,
            )
        )

    assert not alert_calls, (
        "elapsed == heartbeat_stall_s must NOT fire a stall alert "
        "(contract is strictly >, not >=); "
        f"got {len(alert_calls)} alert(s): {alert_calls}"
    )


# ---------------------------------------------------------------------------
# _write_heartbeat uses temp-then-os.replace
# ---------------------------------------------------------------------------


def test_write_heartbeat_uses_temp_then_os_replace(tmp_path: Path) -> None:
    """_write_heartbeat atomically writes via temp file then os.replace.

    Patches os.replace with a spy that delegates to the real
    implementation.  Asserts os.replace is called with the target path as
    dst, and the target file contains the timestamp afterward.
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
        f"os.replace destination must be the target path {target}; got {dst!r}"
    )


# ---------------------------------------------------------------------------
# Guarded loop — write failure does not crash the coroutine
# ---------------------------------------------------------------------------


def test_write_heartbeat_error_does_not_crash_the_loop(
    tmp_path: Path,
) -> None:
    """OSError on iteration 1 is caught; the loop continues to iter 2+.

    The coroutine must swallow the exception, not propagate it, and
    attempt subsequent iterations normally.  The loop exits via
    stop_event after 3 iterations.
    """
    obs = _make_obs(tmp_path / "heartbeat")
    state = LivenessState()
    stop = asyncio.Event()
    clock: list[float] = [0.0]
    iteration: list[int] = [0]

    async def fake_sleep(seconds: float) -> None:
        clock[0] += seconds
        iteration[0] += 1
        if iteration[0] >= 3:
            stop.set()

    def fake_now() -> datetime:
        return _utc(clock[0])

    attempt_count: list[int] = [0]

    def failing_then_ok(path: Path, ts: str) -> None:
        attempt_count[0] += 1
        if attempt_count[0] == 1:
            raise OSError("simulated disk full on first write")

    # Must NOT raise.
    with patch(
        "baton_harness.chain.heartbeat._write_heartbeat",
        side_effect=failing_then_ok,
    ):
        asyncio.run(
            heartbeat_monitor(
                obs,
                state,
                interval_s=1.0,
                sleep=fake_sleep,
                now=fake_now,
                stop_event=stop,
            )
        )

    assert attempt_count[0] >= 2, (
        "After a write failure on iteration 1, the coroutine must attempt "
        f"subsequent writes; got {attempt_count[0]} attempts total"
    )


# ---------------------------------------------------------------------------
# LivenessState unit behaviour
# ---------------------------------------------------------------------------


def test_liveness_state_mark_in_progress_sets_all_fields() -> None:
    """mark_in_progress populates all four fields of LivenessState."""
    state = LivenessState()
    ts = _utc(1000.0)
    state.mark_in_progress("owner1", "repo1", 7, ts)

    assert state.in_progress_owner == "owner1"
    assert state.in_progress_repo == "repo1"
    assert state.in_progress_issue == 7
    assert state.in_progress_since == ts


def test_liveness_state_clear_nulls_all_fields() -> None:
    """clear() sets all four fields back to None."""
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

    After episode 1 fires an alert (debounce engaged), calling
    mark_in_progress must allow episode 2 to alert again.
    """
    owner, repo, issue_num = "x", "y", 3
    stall_s = 10.0
    obs = _make_obs(tmp_path / "heartbeat", heartbeat_stall_s=stall_s)
    state = LivenessState()
    state.mark_in_progress(owner, repo, issue_num, _utc(0.0))

    # --- Episode 1: fire the first stall alert ---------------------------
    stop1 = asyncio.Event()
    clock: list[float] = [stall_s + 1.0]
    iter1: list[int] = [0]

    async def fake_sleep1(seconds: float) -> None:
        clock[0] += seconds
        iter1[0] += 1
        if iter1[0] >= 1:
            stop1.set()

    def fake_now() -> datetime:
        return _utc(clock[0])

    first_alert: list[bool] = [False]

    def fake_alert1(*args: Any, **kwargs: Any) -> bool:  # noqa: ANN401
        first_alert[0] = True
        return True

    with (
        patch("baton_harness.chain.heartbeat._write_heartbeat"),
        patch(
            "baton_harness.chain.heartbeat.alert",
            side_effect=fake_alert1,
        ),
    ):
        asyncio.run(
            heartbeat_monitor(
                obs,
                state,
                interval_s=1.0,
                sleep=fake_sleep1,
                now=fake_now,
                stop_event=stop1,
            )
        )

    assert first_alert[0], "First stall episode must fire an alert"

    # --- Reset via mark_in_progress (not clear — mark itself resets) ----
    state.mark_in_progress(owner, repo, issue_num, _utc(clock[0]))

    # --- Episode 2: advance clock past new t0 ---------------------------
    stop2 = asyncio.Event()
    new_t0 = clock[0]
    clock[0] = new_t0 + stall_s + 1.0
    iter2: list[int] = [0]

    async def fake_sleep2(seconds: float) -> None:
        clock[0] += seconds
        iter2[0] += 1
        if iter2[0] >= 1:
            stop2.set()

    second_alert: list[bool] = [False]

    def fake_alert2(*args: Any, **kwargs: Any) -> bool:  # noqa: ANN401
        second_alert[0] = True
        return True

    with (
        patch("baton_harness.chain.heartbeat._write_heartbeat"),
        patch(
            "baton_harness.chain.heartbeat.alert",
            side_effect=fake_alert2,
        ),
    ):
        asyncio.run(
            heartbeat_monitor(
                obs,
                state,
                interval_s=1.0,
                sleep=fake_sleep2,
                now=fake_now,
                stop_event=stop2,
            )
        )

    assert second_alert[0], (
        "mark_in_progress must reset the debounce so a new stall episode "
        "can fire an alert"
    )


def test_liveness_state_clear_resets_debounce_fields() -> None:
    """clear() nulls all fields; a subsequent mark+stall can alert again.

    Verifies the post-clear state is truly blank: in_progress_since is
    None after clear(), and a re-mark starts a fresh episode.
    """
    state = LivenessState()
    state.mark_in_progress("a", "b", 1, _utc(0.0))
    state.clear()

    assert state.in_progress_since is None, (
        "clear() must null in_progress_since"
    )
    assert state.in_progress_owner is None
    assert state.in_progress_repo is None
    assert state.in_progress_issue is None

    # Re-marking must be a fresh episode.
    state.mark_in_progress("a", "b", 1, _utc(0.0))
    assert state.in_progress_since == _utc(0.0)


# ---------------------------------------------------------------------------
# Heartbeat runlog event emitted each iteration
# ---------------------------------------------------------------------------


def test_heartbeat_runlog_event_emitted_each_iteration(
    tmp_path: Path,
) -> None:
    """heartbeat_monitor emits event='heartbeat' to the runlog each tick.

    When a RunLog is provided, each iteration must append a JSON object
    with event='heartbeat' to the runlog file.
    """
    iterations = 3
    obs = _make_obs(tmp_path / "heartbeat")
    runlog_path = tmp_path / "runlog.jsonl"
    runlog = RunLog(runlog_path)
    state = LivenessState()
    stop = asyncio.Event()
    clock: list[float] = [0.0]
    tick: list[int] = [0]

    async def fake_sleep(seconds: float) -> None:
        clock[0] += seconds
        tick[0] += 1
        if tick[0] >= iterations:
            stop.set()

    def fake_now() -> datetime:
        return _utc(clock[0])

    with patch("baton_harness.chain.heartbeat._write_heartbeat"):
        asyncio.run(
            heartbeat_monitor(
                obs,
                state,
                runlog=runlog,
                interval_s=1.0,
                sleep=fake_sleep,
                now=fake_now,
                stop_event=stop,
            )
        )

    lines = runlog_path.read_text(encoding="utf-8").splitlines()
    events = [json.loads(ln) for ln in lines if ln.strip()]
    hb_events = [e for e in events if e.get("event") == "heartbeat"]
    assert len(hb_events) == iterations, (
        f"Expected {iterations} runlog heartbeat events; "
        f"got {len(hb_events)}: {hb_events}"
    )


# ---------------------------------------------------------------------------
# Stop-event and cancellation exit cleanly
# ---------------------------------------------------------------------------


def test_stop_event_exits_loop_cleanly(tmp_path: Path) -> None:
    """Setting stop_event causes the coroutine to return without error."""
    obs = _make_obs(tmp_path / "heartbeat")
    state = LivenessState()
    stop = asyncio.Event()
    clock: list[float] = [0.0]
    iteration: list[int] = [0]

    async def fake_sleep(seconds: float) -> None:
        clock[0] += seconds
        iteration[0] += 1
        stop.set()

    def fake_now() -> datetime:
        return _utc(clock[0])

    with patch("baton_harness.chain.heartbeat._write_heartbeat"):
        asyncio.run(
            heartbeat_monitor(
                obs,
                state,
                interval_s=1.0,
                sleep=fake_sleep,
                now=fake_now,
                stop_event=stop,
            )
        )
    # Returning normally is the assertion.


def test_cancellation_does_not_propagate(tmp_path: Path) -> None:
    """asyncio.CancelledError during sleep is suppressed; coroutine returns.

    The contract: heartbeat_monitor never propagates an exception out,
    including CancelledError.
    """
    obs = _make_obs(tmp_path / "heartbeat")
    state = LivenessState()

    async def fake_sleep(seconds: float) -> None:
        raise asyncio.CancelledError

    def fake_now() -> datetime:
        return _utc(0.0)

    # Must not raise CancelledError out of the coroutine.
    with patch("baton_harness.chain.heartbeat._write_heartbeat"):
        asyncio.run(
            heartbeat_monitor(
                obs,
                state,
                interval_s=1.0,
                sleep=fake_sleep,
                now=fake_now,
            )
        )
