"""Unit tests for baton_harness.chain.failure_tally.

Tests the ``FailureTally`` durable persistence class that tracks the
per-issue *consecutive charged-failure* count across daemon restarts
(#351, D1). Unlike ``RedispatchTally`` (a sliding-tick-window loop
detector), ``FailureTally`` is a plain count-and-reset counter — no
window, no decay, no tick concept.

Coverage:
- Construction tolerates a missing/corrupt/unreadable backing file (no
  raise; behaves as empty state).
- ``record_and_check`` returns ``(new_count, exhausted)`` and increments
  sequentially across repeated calls for the same issue.
- ``exhausted`` semantics: ``count >= max_count``, exercised at
  ``max_count - 1`` (not yet exhausted), ``max_count`` (exhausted), and
  ``max_count + 1`` (still exhausted) — the D2 budget boundary.
- ``reset`` deletes the per-issue key entirely (not merely zeroes it).
- A write/persist failure is swallowed — never raised to the caller.
- Restart-survival: a fresh ``FailureTally`` constructed over the same
  path picks up the prior count and continues the sequence rather than
  resetting to zero.

Mirrors ``tests/chain/test_redispatch.py``'s structure and conventions
(``tmp_path`` fixtures, ``unittest.mock.patch`` for write-failure
injection) per #351 plan D1's explicit "sibling of redispatch.py"
directive.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from baton_harness.chain.failure_tally import FailureTally

# ---------------------------------------------------------------------------
# Construction / tolerance
# ---------------------------------------------------------------------------


def test_tally_tolerates_missing_file(tmp_path: Path) -> None:
    """FailureTally does not raise when the backing file is absent."""
    path = tmp_path / "failure-counts.json"
    tally = FailureTally(path, max_count=2)
    assert tally is not None


def test_tally_tolerates_corrupt_file(tmp_path: Path) -> None:
    """FailureTally treats a corrupt JSON file as empty (no raise)."""
    path = tmp_path / "failure-counts.json"
    path.write_text("not-valid-json{{{", encoding="utf-8")
    tally = FailureTally(path, max_count=2)
    # Corrupt file -> treated as empty -> peek returns 0.
    assert tally.peek(10) == 0


def test_tally_tolerates_unreadable_file(tmp_path: Path) -> None:
    """FailureTally does not raise when the file cannot be opened."""
    path = tmp_path / "failure-counts.json"
    # Write a directory at the path so open() fails.
    path.mkdir()
    tally = FailureTally(path, max_count=2)
    assert tally is not None


def test_tally_accepts_path_as_str(tmp_path: Path) -> None:
    """FailureTally accepts a str path as well as a Path."""
    path_str = str(tmp_path / "failure-counts.json")
    tally = FailureTally(path_str, max_count=2)
    assert tally is not None


# ---------------------------------------------------------------------------
# record_and_check — increment sequence and (count, exhausted) shape
# ---------------------------------------------------------------------------


def test_record_and_check_returns_a_tuple_of_count_and_exhausted(
    tmp_path: Path,
) -> None:
    """record_and_check returns a (new_count, exhausted) 2-tuple."""
    tally = FailureTally(tmp_path / "failure-counts.json", max_count=2)
    result = tally.record_and_check(issue=10)
    assert isinstance(result, tuple)
    assert len(result) == 2
    count, exhausted = result
    assert isinstance(count, int)
    assert isinstance(exhausted, bool)


def test_record_and_check_increments_sequentially_per_issue(
    tmp_path: Path,
) -> None:
    """Successive record_and_check calls for the same issue increment by 1."""
    tally = FailureTally(tmp_path / "failure-counts.json", max_count=5)
    count1, _ = tally.record_and_check(issue=10)
    count2, _ = tally.record_and_check(issue=10)
    count3, _ = tally.record_and_check(issue=10)
    assert (count1, count2, count3) == (1, 2, 3)


def test_record_and_check_tracks_issues_independently(
    tmp_path: Path,
) -> None:
    """Counts for different issue numbers do not interfere."""
    tally = FailureTally(tmp_path / "failure-counts.json", max_count=5)
    count_a1, _ = tally.record_and_check(issue=1)
    count_b1, _ = tally.record_and_check(issue=2)
    count_a2, _ = tally.record_and_check(issue=1)
    assert count_a1 == 1
    assert count_b1 == 1
    assert count_a2 == 2, "issue #1's count must not be affected by issue #2"


# ---------------------------------------------------------------------------
# exhausted semantics: count >= max_count, exercised at N-1, N, N+1
# ---------------------------------------------------------------------------


def test_exhausted_is_false_below_max_count(tmp_path: Path) -> None:
    """count == max_count - 1 -> not yet exhausted (auto-retry allowed).

    D2: N=2 means one auto-retry, terminal on the second failure. The
    first charged failure (count=1, N-1) must retry, not terminalise.
    """
    tally = FailureTally(tmp_path / "failure-counts.json", max_count=2)
    count, exhausted = tally.record_and_check(issue=10)
    assert count == 1
    assert exhausted is False, (
        "count == max_count - 1 (1 < 2) must NOT be exhausted; the first "
        "charged failure must still restore agent-ready for retry"
    )


def test_exhausted_is_true_at_max_count(tmp_path: Path) -> None:
    """count == max_count -> exhausted (terminal, agent-failed).

    D2: the second charged failure (count=2, N) is the terminal one.
    """
    tally = FailureTally(tmp_path / "failure-counts.json", max_count=2)
    tally.record_and_check(issue=10)  # count=1
    count, exhausted = tally.record_and_check(issue=10)  # count=2
    assert count == 2
    assert exhausted is True, (
        "count == max_count (2 == 2) must be exhausted; the second "
        "charged failure must terminalise to agent-failed"
    )


def test_exhausted_stays_true_above_max_count(tmp_path: Path) -> None:
    """count == max_count + 1 -> still exhausted (no un-terminalising).

    In normal operation ``reset`` fires as soon as the terminal state is
    written (D5), so a count should never actually reach N+1 in
    production. This test pins the boundary comparison itself
    (``count >= max_count``, not ``count == max_count``) so a
    hypothetical stray extra call does not silently flip back to
    "still retrying".
    """
    tally = FailureTally(tmp_path / "failure-counts.json", max_count=2)
    tally.record_and_check(issue=10)  # count=1
    tally.record_and_check(issue=10)  # count=2, exhausted
    count, exhausted = tally.record_and_check(issue=10)  # count=3
    assert count == 3
    assert exhausted is True, (
        "count == max_count + 1 (3 > 2) must remain exhausted "
        "(comparison is >=, not ==)"
    )


# ---------------------------------------------------------------------------
# reset — deletes the key entirely
# ---------------------------------------------------------------------------


def test_reset_deletes_the_key(tmp_path: Path) -> None:
    """reset removes the issue's count entirely (peek returns 0 after)."""
    path = tmp_path / "failure-counts.json"
    tally = FailureTally(path, max_count=2)
    tally.record_and_check(issue=10)
    tally.record_and_check(issue=10)
    assert tally.peek(10) == 2

    tally.reset(issue=10)

    assert tally.peek(10) == 0, (
        "reset must delete the per-issue key; peek must report 0 "
        "afterwards"
    )


def test_reset_persists_the_deletion(tmp_path: Path) -> None:
    """reset's deletion survives being read back from the backing file."""
    path = tmp_path / "failure-counts.json"
    tally = FailureTally(path, max_count=2)
    tally.record_and_check(issue=10)
    tally.reset(issue=10)

    data = json.loads(path.read_text(encoding="utf-8"))
    issues = data.get("issues", {})
    assert "10" not in issues and 10 not in issues, (
        f"reset must remove issue 10's key from the persisted file; "
        f"got issues={issues!r}"
    )


def test_reset_after_a_fresh_record_starts_the_next_count_at_one(
    tmp_path: Path,
) -> None:
    """After reset, the next charged failure for that issue starts at 1."""
    tally = FailureTally(tmp_path / "failure-counts.json", max_count=2)
    tally.record_and_check(issue=10)
    tally.record_and_check(issue=10)
    tally.reset(issue=10)

    count, exhausted = tally.record_and_check(issue=10)
    assert count == 1
    assert exhausted is False


def test_reset_on_an_issue_with_no_prior_record_does_not_raise(
    tmp_path: Path,
) -> None:
    """Resetting an issue that was never recorded is a safe no-op."""
    tally = FailureTally(tmp_path / "failure-counts.json", max_count=2)
    tally.reset(issue=999)  # must not raise
    assert tally.peek(999) == 0


# ---------------------------------------------------------------------------
# peek — read without mutating
# ---------------------------------------------------------------------------


def test_peek_returns_zero_for_an_unknown_issue(tmp_path: Path) -> None:
    """peek on an issue never recorded returns 0."""
    tally = FailureTally(tmp_path / "failure-counts.json", max_count=2)
    assert tally.peek(42) == 0


def test_peek_does_not_mutate_state(tmp_path: Path) -> None:
    """Calling peek repeatedly does not itself increment the count."""
    tally = FailureTally(tmp_path / "failure-counts.json", max_count=5)
    tally.record_and_check(issue=10)
    tally.peek(10)
    tally.peek(10)
    tally.peek(10)
    assert tally.peek(10) == 1, (
        "peek must be a pure read — it must not increment the count "
        "as a side effect"
    )


# ---------------------------------------------------------------------------
# Persist failure is swallowed (best-effort, matches redispatch.py)
# ---------------------------------------------------------------------------


def test_persist_failure_does_not_raise(tmp_path: Path) -> None:
    """A write failure during record_and_check is swallowed, not raised."""
    tally = FailureTally(tmp_path / "failure-counts.json", max_count=2)

    with patch("builtins.open", side_effect=OSError("disk full")):
        try:
            tally.record_and_check(issue=10)
        except OSError:
            pytest.fail(
                "FailureTally.record_and_check must swallow OSError from "
                "a failed persist — write is best-effort"
            )


def test_reset_persist_failure_does_not_raise(tmp_path: Path) -> None:
    """A write failure during reset is swallowed, not raised."""
    path = tmp_path / "failure-counts.json"
    tally = FailureTally(path, max_count=2)
    tally.record_and_check(issue=10)

    with patch("builtins.open", side_effect=OSError("disk full")):
        try:
            tally.reset(issue=10)
        except OSError:
            pytest.fail(
                "FailureTally.reset must swallow OSError from a failed "
                "persist — write is best-effort"
            )


# ---------------------------------------------------------------------------
# Restart-survival — state persists across re-instantiation
# ---------------------------------------------------------------------------


def test_state_survives_reinstantiation_from_the_same_path(
    tmp_path: Path,
) -> None:
    """A fresh FailureTally over the same path inherits the prior count.

    Simulates a daemon restart (``Restart=on-failure``, F1): instance A
    records one charged failure for issue #55; instance B is constructed
    anew over the same backing file and must see the inherited count,
    continuing the sequence rather than resetting to zero.
    """
    path = tmp_path / "failure-counts.json"

    tally_a = FailureTally(path, max_count=2)
    count_a, exhausted_a = tally_a.record_and_check(issue=55)
    assert count_a == 1
    assert exhausted_a is False

    # --- Simulate daemon restart: fresh instance over the same path ---
    tally_b = FailureTally(path, max_count=2)
    assert tally_b.peek(55) == 1, (
        "a fresh FailureTally over the same path must see instance A's "
        "prior count of 1"
    )

    count_b, exhausted_b = tally_b.record_and_check(issue=55)
    assert count_b == 2, (
        "instance B's record_and_check must continue the sequence from "
        "1, not restart at 1"
    )
    assert exhausted_b is True, (
        "the second charged failure (count==max_count==2), split across "
        "a restart, must still terminalise"
    )


def test_state_survives_reinstantiation_after_reset(tmp_path: Path) -> None:
    """A reset performed by instance A is visible to instance B."""
    path = tmp_path / "failure-counts.json"

    tally_a = FailureTally(path, max_count=2)
    tally_a.record_and_check(issue=7)
    tally_a.reset(issue=7)

    tally_b = FailureTally(path, max_count=2)
    assert tally_b.peek(7) == 0, (
        "instance B must see instance A's reset — the key must not "
        "reappear after a restart"
    )
