"""Unit tests for baton_harness.chain.redispatch.

Tests the ``exceeds_threshold`` pure helper and the ``RedispatchTally``
durable persistence class that tracks per-issue re-dispatch counts across
daemon restarts.

Coverage:
- ``exceeds_threshold``: empty list, at/below/above max_count, boundary
  inclusivity (mark == current_tick - window_ticks is EXCLUDED,
  mark == current_tick is INCLUDED), mixed in/out-window marks.
- ``exceeds_threshold`` off-by-one semantics (Fix A): ``BH_REDISPATCH_MAX``
  means the MAXIMUM NUMBER OF REDISPATCHES ALLOWED. ``record_and_check``
  is called BEFORE each dispatch. Therefore:
    - count == max_count -> False  (the Nth redispatch is still permitted)
    - count >  max_count -> True   (park on the (N+1)th attempt)
- ``RedispatchTally`` construction: tolerates missing file, tolerates
  corrupt/unreadable file (no raise).
- ``advance_tick`` increments and persists the global tick.
- ``record_and_check``: appends current tick mark, prunes stale marks,
  persists, and returns True iff threshold is breached.
- **Restart-survival** (the headline AC): a fresh ``RedispatchTally``
  over the same path inherits prior tick + per-issue marks and can trip
  the threshold from accumulated prior history.
- Write failures are swallowed (best-effort); they do not raise.
- **Atomic persist** (Fix B): a failed persist must not truncate/corrupt
  the prior counts file; the original JSON must survive intact.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from baton_harness.chain.redispatch import (
    RedispatchTally,
    exceeds_threshold,
)

# ---------------------------------------------------------------------------
# exceeds_threshold — pure helper
# ---------------------------------------------------------------------------


def test_exceeds_threshold_empty_marks_returns_false() -> None:
    """No marks never breaches any threshold."""
    assert (
        exceeds_threshold([], current_tick=10, window_ticks=5, max_count=1)
        is False
    )


def test_exceeds_threshold_at_max_count_is_allowed_returns_false() -> None:
    """Exactly max_count marks inside the window returns False.

    BH_REDISPATCH_MAX is the MAXIMUM NUMBER ALLOWED.  When count equals
    max_count the Nth redispatch is still permitted — park only on
    the (N+1)th.  Correct semantics: count > max_count, NOT >=.
    """
    # window = (10 - 5, 10] = (5, 10]; marks 6, 7, 8 are 3 inside
    # count == max_count == 3 -> allowed -> False
    assert (
        exceeds_threshold(
            [6, 7, 8], current_tick=10, window_ticks=5, max_count=3
        )
        is False
    ), (
        "exceeds_threshold must return False when count == max_count; "
        "the Nth redispatch is still allowed"
    )


def test_exceeds_threshold_one_above_max_count_returns_true() -> None:
    """max_count + 1 marks in the window returns True (park on N+1th).

    With max_count=3, 4 marks in the window means the 4th attempt
    is the one that should be parked.
    """
    # window = (5, 10]; marks 6, 7, 8, 9 are 4 inside; max_count=3
    # count (4) > max_count (3) -> True
    assert (
        exceeds_threshold(
            [6, 7, 8, 9], current_tick=10, window_ticks=5, max_count=3
        )
        is True
    ), (
        "exceeds_threshold must return True when count == max_count + 1; "
        "the (N+1)th redispatch must be parked"
    )


def test_exceeds_threshold_below_max_count_returns_false() -> None:
    """Fewer than max_count marks inside the window returns False."""
    # 2 marks inside (5, 10], max_count=3
    assert (
        exceeds_threshold([6, 8], current_tick=10, window_ticks=5, max_count=3)
        is False
    )


def test_exceeds_threshold_old_marks_outside_window_excluded() -> None:
    """Marks at or below current_tick - window_ticks are excluded."""
    # window = (5, 10]; mark at 5 is EXCLUDED (not strictly greater)
    assert (
        exceeds_threshold(
            [5, 5, 5], current_tick=10, window_ticks=5, max_count=3
        )
        is False
    )


def test_exceeds_threshold_boundary_lower_excluded() -> None:
    """Mark exactly at current_tick - window_ticks is EXCLUDED."""
    # boundary = current_tick - window_ticks = 10 - 5 = 5
    # mark at 5 must NOT count
    assert (
        exceeds_threshold([5], current_tick=10, window_ticks=5, max_count=1)
        is False
    )


def test_exceeds_threshold_boundary_upper_included_at_max_allowed() -> None:
    """Mark exactly at current_tick is INCLUDED, count==max_count -> False.

    The upper-boundary mark IS counted, but with count == max_count
    the result is still False (Nth dispatch is allowed).
    """
    # mark at 10 == current_tick -> counted; count=1 == max_count=1 -> False
    assert (
        exceeds_threshold([10], current_tick=10, window_ticks=5, max_count=1)
        is False
    ), (
        "Upper-boundary mark must be counted, but count==max_count is "
        "still allowed (False) under the corrected > semantics"
    )


def test_exceeds_threshold_boundary_upper_included_above_max() -> None:
    """Two upper-boundary marks with max_count=1 -> True (breach).

    Confirms the upper boundary IS counted (window-membership math is
    unchanged) while the comparison is strictly >.
    """
    # marks 9, 10 both inside (5, 10]; count=2 > max_count=1 -> True
    assert (
        exceeds_threshold(
            [9, 10], current_tick=10, window_ticks=5, max_count=1
        )
        is True
    )


def test_exceeds_threshold_mixed_in_and_out_of_window() -> None:
    """Only marks strictly inside (current_tick-window, current_tick] count."""
    # window = (5, 10]; marks 3, 5 are OUT; marks 6, 10 are IN (count=2)
    # count (2) == max_count (2) -> still allowed -> False
    assert (
        exceeds_threshold(
            [3, 5, 6, 10], current_tick=10, window_ticks=5, max_count=2
        )
        is False
    ), (
        "count==max_count must return False (allow N, park on N+1); "
        "window-membership math (marks 3 and 5 are excluded) is unchanged"
    )
    # Three in-window marks -> count (3) > max_count (2) -> True
    assert (
        exceeds_threshold(
            [3, 5, 6, 9, 10], current_tick=10, window_ticks=5, max_count=2
        )
        is True
    )
    # One fewer in-window mark than max_count -> False
    assert (
        exceeds_threshold(
            [3, 5, 6], current_tick=10, window_ticks=5, max_count=2
        )
        is False
    )


def test_exceeds_threshold_is_pure_no_side_effects() -> None:
    """Calling exceeds_threshold does not mutate the input list."""
    marks = [6, 7, 8]
    original = list(marks)
    exceeds_threshold(marks, current_tick=10, window_ticks=5, max_count=3)
    assert marks == original


# ---------------------------------------------------------------------------
# RedispatchTally — construction / tolerance
# ---------------------------------------------------------------------------


def test_tally_tolerates_missing_file(tmp_path: Path) -> None:
    """RedispatchTally does not raise when the backing file is absent."""
    path = tmp_path / "counts.json"
    tally = RedispatchTally(path, window_ticks=10, max_count=3)
    assert tally is not None


def test_tally_tolerates_corrupt_file(tmp_path: Path) -> None:
    """RedispatchTally treats a corrupt JSON file as empty (no raise)."""
    path = tmp_path / "counts.json"
    path.write_text("not-valid-json{{{", encoding="utf-8")
    tally = RedispatchTally(path, window_ticks=10, max_count=3)
    # Should behave as if empty — advance_tick starts at 0
    tick = tally.advance_tick()
    assert isinstance(tick, int)


def test_tally_tolerates_unreadable_file(tmp_path: Path) -> None:
    """RedispatchTally does not raise when the file cannot be opened."""
    path = tmp_path / "counts.json"
    # Write a directory at the path so open() fails
    path.mkdir()
    # Construction must not raise
    tally = RedispatchTally(path, window_ticks=10, max_count=3)
    assert tally is not None


def test_tally_accepts_path_as_str(tmp_path: Path) -> None:
    """RedispatchTally accepts str path as well as Path."""
    path_str = str(tmp_path / "counts.json")
    tally = RedispatchTally(path_str, window_ticks=10, max_count=3)
    assert tally is not None


# ---------------------------------------------------------------------------
# RedispatchTally — advance_tick
# ---------------------------------------------------------------------------


def test_advance_tick_increments_from_zero(tmp_path: Path) -> None:
    """advance_tick returns 1 on first call (starts at 0)."""
    tally = RedispatchTally(
        tmp_path / "counts.json", window_ticks=10, max_count=3
    )
    assert tally.advance_tick() == 1


def test_advance_tick_increments_sequentially(tmp_path: Path) -> None:
    """Successive advance_tick calls increment by 1 each time."""
    tally = RedispatchTally(
        tmp_path / "counts.json", window_ticks=10, max_count=3
    )
    assert tally.advance_tick() == 1
    assert tally.advance_tick() == 2
    assert tally.advance_tick() == 3


def test_advance_tick_persists_to_file(tmp_path: Path) -> None:
    """advance_tick writes the new tick to the backing file."""
    path = tmp_path / "counts.json"
    tally = RedispatchTally(path, window_ticks=10, max_count=3)
    tally.advance_tick()
    tally.advance_tick()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["tick"] == 2


def test_advance_tick_creates_parent_directory(tmp_path: Path) -> None:
    """advance_tick creates the parent directory if it does not exist."""
    path = tmp_path / "subdir" / "counts.json"
    tally = RedispatchTally(path, window_ticks=10, max_count=3)
    tally.advance_tick()
    assert path.exists()


# ---------------------------------------------------------------------------
# RedispatchTally — record_and_check
# ---------------------------------------------------------------------------


def test_record_and_check_below_threshold_returns_false(
    tmp_path: Path,
) -> None:
    """record_and_check returns False when count < max_count."""
    tally = RedispatchTally(
        tmp_path / "counts.json", window_ticks=10, max_count=3
    )
    tally.advance_tick()  # tick = 1
    # One mark — well below max_count=3
    result = tally.record_and_check(issue=42)
    assert result is False


def test_record_and_check_at_max_count_still_allowed_returns_false(
    tmp_path: Path,
) -> None:
    """record_and_check returns False when the Nth (==max_count) mark lands.

    BH_REDISPATCH_MAX is the number of redispatches ALLOWED.  When the
    accumulated count equals max_count the issue is still allowed through;
    only the (max_count+1)th call returns True and parks the issue.
    """
    tally = RedispatchTally(
        tmp_path / "counts.json", window_ticks=10, max_count=3
    )
    tally.advance_tick()  # tick=1
    tally.record_and_check(issue=42)
    tally.advance_tick()  # tick=2
    tally.record_and_check(issue=42)
    tally.advance_tick()  # tick=3
    # Third mark: count == max_count == 3 -> still allowed -> False
    result = tally.record_and_check(issue=42)
    assert result is False, (
        "The 3rd redispatch (count==max_count==3) must still be allowed; "
        "park only on the 4th (count==max_count+1)"
    )


def test_record_and_check_at_max_count_plus_one_returns_true(
    tmp_path: Path,
) -> None:
    """record_and_check returns True on the (max_count+1)th mark.

    The (N+1)th call is the one that gets parked.
    """
    tally = RedispatchTally(
        tmp_path / "counts.json", window_ticks=10, max_count=3
    )
    for i in range(1, 5):
        tally.advance_tick()  # ticks 1..4
        result = tally.record_and_check(issue=42)
        if i < 4:
            assert result is False, (
                f"Expected False on call {i} (count={i} <= max_count=3)"
            )
    # Fourth mark: count == max_count + 1 == 4 -> breach -> True
    assert result is True, (
        "The 4th redispatch (count==max_count+1==4) must be parked (True)"
    )


def test_record_and_check_persists_marks(tmp_path: Path) -> None:
    """record_and_check writes per-issue marks to the backing file."""
    path = tmp_path / "counts.json"
    tally = RedispatchTally(path, window_ticks=10, max_count=3)
    tally.advance_tick()
    tally.record_and_check(issue=7)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert "7" in data.get("issues", {}) or 7 in data.get("issues", {})


def test_record_and_check_prunes_stale_marks(tmp_path: Path) -> None:
    """Marks outside the window are pruned on record_and_check."""
    # window_ticks=2, max_count=3 — so only marks within the last 2 ticks
    tally = RedispatchTally(
        tmp_path / "counts.json", window_ticks=2, max_count=3
    )
    # Advance 5 ticks, recording every tick (old marks expire)
    for _ in range(5):
        tally.advance_tick()
        tally.record_and_check(issue=99)

    # After all that, the marks in the file must only be within
    # the last 2 ticks (window).  With max_count=3 only reachable
    # if stale marks were NOT pruned.
    path = tmp_path / "counts.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    issues = data.get("issues", {})
    key = "99" if "99" in issues else 99
    marks_in_file = issues.get(key, [])
    current_tick = data["tick"]
    # Every mark remaining should be within the window
    for m in marks_in_file:
        assert m > current_tick - 2, (
            f"Stale mark {m} survived pruning "
            f"(current_tick={current_tick}, window=2)"
        )


def test_record_and_check_independent_issues(tmp_path: Path) -> None:
    """Marks for different issue numbers are tracked independently.

    With max_count=2, two marks for issue 1 is count==max_count which
    is now ALLOWED (False).  Issue 2 with one mark is also False.
    A third mark for issue 1 (count==max_count+1==3) triggers breach.
    """
    tally = RedispatchTally(
        tmp_path / "counts.json", window_ticks=10, max_count=2
    )
    tally.advance_tick()  # tick=1
    tally.record_and_check(issue=1)
    tally.advance_tick()  # tick=2
    # Issue 1 at count==max_count: still allowed (False)
    at_max_1 = tally.record_and_check(issue=1)
    # Issue 2 first mark: well below threshold (False)
    not_breach_2 = tally.record_and_check(issue=2)
    assert at_max_1 is False, (
        "Issue 1 at count==max_count==2 must still be allowed (False)"
    )
    assert not_breach_2 is False
    tally.advance_tick()  # tick=3
    # Issue 1 third mark: count==max_count+1==3 -> breach (True)
    breach_1 = tally.record_and_check(issue=1)
    assert breach_1 is True, (
        "Issue 1 at count==max_count+1==3 must be parked (True)"
    )


# ---------------------------------------------------------------------------
# RedispatchTally — restart-survival (THE HEADLINE AC)
# ---------------------------------------------------------------------------


def test_tally_restart_survival_new_instance_sees_prior_marks(
    tmp_path: Path,
) -> None:
    """A fresh RedispatchTally over the same path inherits prior state.

    Instance A advances ticks and records an issue three times (== max_count).
    Instance B is constructed anew over the same file.  B's fourth
    record_and_check (count==max_count+1) must trip the threshold —
    proving detection survives a daemon restart.

    This test is the primary acceptance criterion for #77.
    """
    path = tmp_path / "counts.json"

    # --- Instance A (simulates daemon before restart) ---
    tally_a = RedispatchTally(path, window_ticks=10, max_count=3)
    tally_a.advance_tick()  # tick=1
    result_a1 = tally_a.record_and_check(issue=55)
    tally_a.advance_tick()  # tick=2
    result_a2 = tally_a.record_and_check(issue=55)
    tally_a.advance_tick()  # tick=3
    result_a3 = tally_a.record_and_check(issue=55)
    # At and below max_count — all allowed (False)
    assert result_a1 is False
    assert result_a2 is False
    assert result_a3 is False, (
        "count==max_count==3 must still be allowed (False)"
    )

    # --- Simulate daemon restart: fresh instance over same path ---
    tally_b = RedispatchTally(path, window_ticks=10, max_count=3)
    tally_b.advance_tick()  # tick=4
    # Fourth mark: count==max_count+1==4 -> breach
    result_b = tally_b.record_and_check(issue=55)
    assert result_b is True, (
        "Expected RedispatchTally to breach after restart: "
        "prior marks from instance A must survive in the persisted file "
        "and the 4th mark (count==max_count+1) must park the issue"
    )


def test_tally_restart_tick_is_continued_not_reset(tmp_path: Path) -> None:
    """After restart, tick continues from the persisted value."""
    path = tmp_path / "counts.json"

    tally_a = RedispatchTally(path, window_ticks=10, max_count=3)
    tally_a.advance_tick()  # tick=1
    tally_a.advance_tick()  # tick=2

    # Fresh instance — should resume at 3, not reset to 1
    tally_b = RedispatchTally(path, window_ticks=10, max_count=3)
    resumed_tick = tally_b.advance_tick()
    assert resumed_tick == 3, (
        f"Expected tick to continue from 2 after restart; got {resumed_tick}"
    )


def test_tally_restart_stale_marks_from_prior_run_do_not_count(
    tmp_path: Path,
) -> None:
    """Marks recorded far in the past are outside the window after restart.

    If ticks advance far enough between A's records and B's check,
    A's marks fall outside the window and should NOT contribute to the
    threshold — proving pruning works correctly across restarts.
    """
    path = tmp_path / "counts.json"
    window = 3
    max_count = 2

    # Instance A records at tick 1 and 2
    tally_a = RedispatchTally(path, window_ticks=window, max_count=max_count)
    tally_a.advance_tick()  # tick=1
    tally_a.record_and_check(issue=77)
    tally_a.advance_tick()  # tick=2
    tally_a.record_and_check(issue=77)

    # Fresh instance B — advance tick well past the window
    tally_b = RedispatchTally(path, window_ticks=window, max_count=max_count)
    # Advance 10 more ticks -> current_tick=12; window=(9,12]
    # Marks at 1 and 2 are now outside the window
    for _ in range(10):
        tally_b.advance_tick()

    # This record lands at tick=12 (1 mark in window < max_count=2)
    result = tally_b.record_and_check(issue=77)
    assert result is False, (
        "Marks from the prior run should be outside the window after "
        "many tick advances — should not contribute to breach"
    )


# ---------------------------------------------------------------------------
# RedispatchTally — write failure tolerance
# ---------------------------------------------------------------------------


def test_tally_write_failure_does_not_raise(tmp_path: Path) -> None:
    """A write failure in persist is swallowed; no exception escapes."""
    tally = RedispatchTally(
        tmp_path / "counts.json", window_ticks=10, max_count=3
    )
    tally.advance_tick()

    # Make the path unwritable by patching open to raise on write
    with patch("builtins.open", side_effect=OSError("disk full")):
        # Must not raise
        try:
            tally.advance_tick()
            tally.record_and_check(issue=1)
        except OSError:
            pytest.fail(
                "RedispatchTally.advance_tick / record_and_check must "
                "swallow OSError — write is best-effort"
            )


# ---------------------------------------------------------------------------
# RedispatchTally — atomic persist (Fix B)
# ---------------------------------------------------------------------------


def test_persist_failure_leaves_prior_file_intact(tmp_path: Path) -> None:
    """A failed persist must not truncate or corrupt the prior counts file.

    The implementation must write to a temp sibling, fsync, then
    ``os.replace()`` atomically into the final path.  If the write step
    fails mid-way the original file must remain present and contain the
    last valid JSON snapshot.

    The failure mode under the non-atomic ``open("w")`` implementation:
    ``open(path, "w")`` truncates the file to zero bytes before any
    content is written.  If an exception is then raised (e.g. disk full,
    interrupted by SIGKILL) the file is left empty, silently zeroing the
    durable counter and defeating the crash-loop detector.

    This test injects a write failure by making the file object's
    ``write()`` method raise after the file has been opened.  The patch
    fires on any write-mode open whose filename starts with the counts
    filename stem (``counts.json``), so it intercepts both:

    - the non-atomic impl, which opens the real ``counts.json`` directly
      ("w" truncates before any bytes land), and
    - the atomic impl, which opens a temp sibling such as
      ``counts.json.tmp`` or ``counts.json.<rand>`` first.

    Against the non-atomic impl the raise fires after truncation, so the
    file is left empty → FAIL (regression guard RED).
    Against the atomic impl the raise fires before ``os.replace`` runs,
    so the real ``counts.json`` is never touched → PASS (atomic is GREEN).

    NOTE: This test is intentionally RED against the current non-atomic
    ``open("w")`` implementation.  That RED is the regression guard.
    The implementer must satisfy it by making ``_persist`` atomic
    (write-to-temp, fsync, ``os.replace``).
    """
    path = tmp_path / "counts.json"

    # Seed a valid counts file by recording one event.
    tally = RedispatchTally(path, window_ticks=10, max_count=3)
    tally.advance_tick()  # tick=1
    tally.record_and_check(issue=11)

    # Capture the last good state for comparison.
    prior_text = path.read_text(encoding="utf-8")
    prior_data = json.loads(prior_text)
    assert prior_data["tick"] == 1  # sanity: file has real content

    # Inject a mid-write failure.  We open the real file normally, but
    # return a wrapper whose write() raises before any bytes land, so:
    #   - non-atomic impl: the file is already truncated by open("w");
    #     the raised error is swallowed; the file is now empty -> FAIL.
    #   - atomic impl: the write goes to a temp file; raise corrupts the
    #     temp, os.replace never runs, original file is untouched -> PASS.
    _real_open = open  # noqa: WPS421 (need the builtin)

    class _WriteFails:
        """Proxy that raises on the first write() call."""

        def __init__(self, fh: object) -> None:
            self._fh = fh

        def write(self, data: str) -> int:
            raise OSError("simulated mid-write failure")

        def __enter__(self) -> _WriteFails:
            self._fh.__enter__()  # type: ignore[union-attr]
            return self

        def __exit__(self, *args: object) -> None:
            self._fh.__exit__(*args)  # type: ignore[union-attr]

    def _patched_open(file: object, mode: str = "r", **kw: object) -> object:
        fh = _real_open(file, mode, **kw)  # type: ignore[call-overload]
        fp = Path(str(file))
        if "w" in mode and fp.name.startswith(path.name):
            return _WriteFails(fh)
        return fh

    with patch("builtins.open", _patched_open):
        # advance_tick triggers _persist; the write will fail.
        # The failure must be swallowed (best-effort write).
        try:
            tally.advance_tick()
        except OSError:
            pytest.fail(
                "_persist must swallow OSError from a failed write — "
                "write failures must never propagate to the caller"
            )

    # The original file must still be present and contain the prior data.
    assert path.exists(), (
        "The counts file must still exist after a failed persist; "
        "a non-atomic write may have truncated or deleted it"
    )
    surviving_text = path.read_text(encoding="utf-8")
    try:
        surviving_data = json.loads(surviving_text)
    except json.JSONDecodeError:
        pytest.fail(
            f"Counts file is not valid JSON after a failed persist.\n"
            f"  content: {surviving_text!r}\n"
            "A non-atomic open('w') truncates the file before writing, "
            "leaving it empty on a mid-write failure."
        )
    assert surviving_data == prior_data, (
        f"Prior counts file must survive a failed persist intact.\n"
        f"  expected: {prior_data}\n"
        f"  got:      {surviving_data}\n"
        "A non-atomic open('w') truncates the file before writing, "
        "destroying the prior content on a mid-write crash."
    )
