"""Tests for OrchestratorState.load() and atomic persist() — issue #106.

Covers:
- Round-trip persist() -> load(): running, retry_queue, claimed all restored
  with exact field values.
- load() on a missing file: graceful no-op, state stays empty, no exception.
- load() on malformed JSON: no exception raised, state is empty (fresh start),
  a warning/error is logged.
- persist() atomicity sentinel: a crash mid-write leaves the original file
  content intact (no partial write).
- persist() uses os.replace (temp-file + rename pattern).
- load() ignores completed_count: integer count is not restored as set members.
- load() restores correctly after multiple persist cycles.
- Edge case: empty state round-trip.
- Edge case: last_event_at field omission from persist() output.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from baton_harness.vendor.symphony.state import (
    IssueState,
    OrchestratorState,
    RetryEntry,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SENTINEL_STARTED_AT = 1_700_000_000.0
_SENTINEL_DUE_AT = 1_700_001_000.0


def _make_issue_state(
    issue_number: int = 42,
    identifier: str = "owner/repo#42",
    title: str = "Fix the thing",
    state: str = "open",
    turn: int = 2,
    max_turns: int = 8,
    started_at: float = _SENTINEL_STARTED_AT,
    last_event: str | None = "turn_complete",
    last_event_at: float | None = 1_700_000_500.0,
    error: str | None = None,
) -> IssueState:
    """Return a fully-populated IssueState for use in tests.

    Args:
        issue_number: GitHub issue number.
        identifier: Repo-scoped identifier string.
        title: Issue title.
        state: Issue state string (e.g. "open").
        turn: Current turn index.
        max_turns: Maximum allowed turns.
        started_at: Unix timestamp when processing started.
        last_event: Description of the last event.
        last_event_at: Unix timestamp of the last event.
        error: Error string if the last turn failed.

    Returns:
        A populated IssueState dataclass instance.
    """
    return IssueState(
        issue_number=issue_number,
        identifier=identifier,
        title=title,
        state=state,
        turn=turn,
        max_turns=max_turns,
        started_at=started_at,
        last_event=last_event,
        last_event_at=last_event_at,
        error=error,
    )


def _make_retry_entry(
    issue_number: int = 7,
    identifier: str = "owner/repo#7",
    attempt: int = 3,
    due_at: float = _SENTINEL_DUE_AT,
    error: str | None = "transient failure",
) -> RetryEntry:
    """Return a fully-populated RetryEntry for use in tests.

    Args:
        issue_number: GitHub issue number.
        identifier: Repo-scoped identifier string.
        attempt: Retry attempt count.
        due_at: Unix timestamp when this retry becomes eligible.
        error: Error string from the previous attempt.

    Returns:
        A populated RetryEntry dataclass instance.
    """
    return RetryEntry(
        issue_number=issue_number,
        identifier=identifier,
        attempt=attempt,
        due_at=due_at,
        error=error,
    )


def _populated_state() -> OrchestratorState:
    """Return an OrchestratorState with running, retry_queue, and claimed set.

    Returns:
        An OrchestratorState with known, verifiable contents.
    """
    orch = OrchestratorState(max_concurrent=3)
    issue_st = _make_issue_state()
    orch.add_running(issue_st.issue_number, issue_st)
    retry = _make_retry_entry()
    orch.retry_queue[retry.issue_number] = retry
    orch.claimed.add(retry.issue_number)
    return orch


# ---------------------------------------------------------------------------
# 1. Round-trip persist -> load: all fields restored with exact values
# ---------------------------------------------------------------------------


class TestPersistLoadRoundTrip:
    """persist() then load() restores running, retry_queue, and claimed."""

    def test_running_issue_number_restored(self, tmp_path: Path) -> None:
        """issue_number in running is restored exactly after round-trip."""
        state_path = str(tmp_path / "state.json")
        original = _populated_state()
        original.persist(state_path)

        fresh = OrchestratorState(max_concurrent=3)
        fresh.load(state_path)

        assert 42 in fresh.running

    def test_running_identifier_restored(self, tmp_path: Path) -> None:
        """Identifier field in running IssueState is restored exactly."""
        state_path = str(tmp_path / "state.json")
        original = _populated_state()
        original.persist(state_path)

        fresh = OrchestratorState(max_concurrent=3)
        fresh.load(state_path)

        assert fresh.running[42].identifier == "owner/repo#42"

    def test_running_title_restored(self, tmp_path: Path) -> None:
        """Title field in running IssueState is restored exactly."""
        state_path = str(tmp_path / "state.json")
        original = _populated_state()
        original.persist(state_path)

        fresh = OrchestratorState(max_concurrent=3)
        fresh.load(state_path)

        assert fresh.running[42].title == "Fix the thing"

    def test_running_state_field_restored(self, tmp_path: Path) -> None:
        """State field in running IssueState is restored exactly."""
        state_path = str(tmp_path / "state.json")
        original = _populated_state()
        original.persist(state_path)

        fresh = OrchestratorState(max_concurrent=3)
        fresh.load(state_path)

        assert fresh.running[42].state == "open"

    def test_running_turn_restored(self, tmp_path: Path) -> None:
        """Turn field in running IssueState is restored exactly."""
        state_path = str(tmp_path / "state.json")
        original = _populated_state()
        original.persist(state_path)

        fresh = OrchestratorState(max_concurrent=3)
        fresh.load(state_path)

        assert fresh.running[42].turn == 2

    def test_running_max_turns_restored(self, tmp_path: Path) -> None:
        """max_turns field in running IssueState is restored exactly."""
        state_path = str(tmp_path / "state.json")
        original = _populated_state()
        original.persist(state_path)

        fresh = OrchestratorState(max_concurrent=3)
        fresh.load(state_path)

        assert fresh.running[42].max_turns == 8

    def test_running_started_at_restored(self, tmp_path: Path) -> None:
        """started_at field in running IssueState is restored exactly."""
        state_path = str(tmp_path / "state.json")
        original = _populated_state()
        original.persist(state_path)

        fresh = OrchestratorState(max_concurrent=3)
        fresh.load(state_path)

        assert fresh.running[42].started_at == _SENTINEL_STARTED_AT

    def test_running_last_event_restored(self, tmp_path: Path) -> None:
        """last_event field in running IssueState is restored exactly."""
        state_path = str(tmp_path / "state.json")
        original = _populated_state()
        original.persist(state_path)

        fresh = OrchestratorState(max_concurrent=3)
        fresh.load(state_path)

        assert fresh.running[42].last_event == "turn_complete"

    def test_running_error_field_restored(self, tmp_path: Path) -> None:
        """error=None in running IssueState is preserved after round-trip."""
        state_path = str(tmp_path / "state.json")
        original = _populated_state()
        original.persist(state_path)

        fresh = OrchestratorState(max_concurrent=3)
        fresh.load(state_path)

        assert fresh.running[42].error is None

    def test_running_error_string_restored(self, tmp_path: Path) -> None:
        """Non-None error string in running IssueState is restored exactly."""
        state_path = str(tmp_path / "state.json")
        orch = OrchestratorState(max_concurrent=3)
        issue_st = _make_issue_state(error="something blew up")
        orch.add_running(issue_st.issue_number, issue_st)
        orch.persist(state_path)

        fresh = OrchestratorState(max_concurrent=3)
        fresh.load(state_path)

        assert fresh.running[42].error == "something blew up"

    def test_retry_queue_issue_number_restored(self, tmp_path: Path) -> None:
        """issue_number in retry_queue RetryEntry is restored exactly."""
        state_path = str(tmp_path / "state.json")
        original = _populated_state()
        original.persist(state_path)

        fresh = OrchestratorState(max_concurrent=3)
        fresh.load(state_path)

        assert 7 in fresh.retry_queue

    def test_retry_queue_identifier_restored(self, tmp_path: Path) -> None:
        """Identifier field in retry_queue RetryEntry is restored exactly."""
        state_path = str(tmp_path / "state.json")
        original = _populated_state()
        original.persist(state_path)

        fresh = OrchestratorState(max_concurrent=3)
        fresh.load(state_path)

        assert fresh.retry_queue[7].identifier == "owner/repo#7"

    def test_retry_queue_attempt_restored(self, tmp_path: Path) -> None:
        """Attempt field in retry_queue RetryEntry is restored exactly."""
        state_path = str(tmp_path / "state.json")
        original = _populated_state()
        original.persist(state_path)

        fresh = OrchestratorState(max_concurrent=3)
        fresh.load(state_path)

        assert fresh.retry_queue[7].attempt == 3

    def test_retry_queue_due_at_restored(self, tmp_path: Path) -> None:
        """due_at field in retry_queue RetryEntry is restored exactly."""
        state_path = str(tmp_path / "state.json")
        original = _populated_state()
        original.persist(state_path)

        fresh = OrchestratorState(max_concurrent=3)
        fresh.load(state_path)

        assert fresh.retry_queue[7].due_at == _SENTINEL_DUE_AT

    def test_retry_queue_error_restored(self, tmp_path: Path) -> None:
        """Error field in retry_queue RetryEntry is restored exactly."""
        state_path = str(tmp_path / "state.json")
        original = _populated_state()
        original.persist(state_path)

        fresh = OrchestratorState(max_concurrent=3)
        fresh.load(state_path)

        assert fresh.retry_queue[7].error == "transient failure"

    def test_claimed_set_restored(self, tmp_path: Path) -> None:
        """Claimed set members are restored after round-trip."""
        state_path = str(tmp_path / "state.json")
        original = _populated_state()
        original.persist(state_path)

        fresh = OrchestratorState(max_concurrent=3)
        fresh.load(state_path)

        # Both issue 42 (running) and 7 (retry) were in claimed.
        assert 42 in fresh.claimed
        assert 7 in fresh.claimed


# ---------------------------------------------------------------------------
# 2. load() on a missing file: graceful no-op
# ---------------------------------------------------------------------------


class TestLoadMissingFile:
    """load() on a non-existent path does not raise and leaves state empty."""

    def test_load_missing_file_does_not_raise(self, tmp_path: Path) -> None:
        """load() on a non-existent path does not raise any exception."""
        state = OrchestratorState(max_concurrent=3)
        missing = str(tmp_path / "no_such_file.json")

        # Must not raise.
        state.load(missing)

    def test_load_missing_file_running_stays_empty(
        self, tmp_path: Path
    ) -> None:
        """Running dict is empty after load() on a missing file."""
        state = OrchestratorState(max_concurrent=3)
        state.load(str(tmp_path / "no_such_file.json"))

        assert state.running == {}

    def test_load_missing_file_retry_queue_stays_empty(
        self, tmp_path: Path
    ) -> None:
        """retry_queue dict is empty after load() on a missing file."""
        state = OrchestratorState(max_concurrent=3)
        state.load(str(tmp_path / "no_such_file.json"))

        assert state.retry_queue == {}

    def test_load_missing_file_claimed_stays_empty(
        self, tmp_path: Path
    ) -> None:
        """Claimed set is empty after load() on a missing file."""
        state = OrchestratorState(max_concurrent=3)
        state.load(str(tmp_path / "no_such_file.json"))

        assert state.claimed == set()


# ---------------------------------------------------------------------------
# 3. load() on malformed JSON: no exception, fresh state, warning logged
# ---------------------------------------------------------------------------


class TestLoadMalformedJson:
    """load() on corrupt JSON does not raise and logs a warning/error."""

    def test_load_malformed_does_not_raise(self, tmp_path: Path) -> None:
        """load() on malformed JSON must not raise any exception."""
        corrupt = tmp_path / "state.json"
        corrupt.write_bytes(b"not valid json {")

        state = OrchestratorState(max_concurrent=3)
        # Must not raise.
        state.load(str(corrupt))

    def test_load_malformed_running_stays_empty(self, tmp_path: Path) -> None:
        """Running dict is empty after load() on malformed JSON."""
        corrupt = tmp_path / "state.json"
        corrupt.write_bytes(b"not valid json {")

        state = OrchestratorState(max_concurrent=3)
        state.load(str(corrupt))

        assert state.running == {}

    def test_load_malformed_retry_queue_stays_empty(
        self, tmp_path: Path
    ) -> None:
        """retry_queue dict is empty after load() on malformed JSON."""
        corrupt = tmp_path / "state.json"
        corrupt.write_bytes(b"not valid json {")

        state = OrchestratorState(max_concurrent=3)
        state.load(str(corrupt))

        assert state.retry_queue == {}

    def test_load_malformed_claimed_stays_empty(self, tmp_path: Path) -> None:
        """Claimed set is empty after load() on malformed JSON."""
        corrupt = tmp_path / "state.json"
        corrupt.write_bytes(b"not valid json {")

        state = OrchestratorState(max_concurrent=3)
        state.load(str(corrupt))

        assert state.claimed == set()

    def test_load_malformed_logs_warning_or_error(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """load() on malformed JSON emits at least one WARNING or ERROR log.

        The exact message is implementation-defined; we only require that
        something is logged at WARNING level or above so operators can diagnose
        the corruption.
        """
        corrupt = tmp_path / "state.json"
        corrupt.write_bytes(b"not valid json {")

        state = OrchestratorState(max_concurrent=3)
        with caplog.at_level(logging.WARNING):
            state.load(str(corrupt))

        logged_at_warning_or_above = [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert logged_at_warning_or_above, (
            "Expected at least one WARNING/ERROR log entry when loading "
            "malformed JSON, but none were emitted. "
            "Operators need a signal to diagnose corruption."
        )


# ---------------------------------------------------------------------------
# 4. persist() atomicity sentinel: original file intact after mid-write crash
# ---------------------------------------------------------------------------


class TestPersistAtomicity:
    """persist() leaves the original file intact when a crash occurs mid-write.

    The implementation must use a tempfile + os.replace (or equivalent) so
    that a failure during the write never leaves a partial file at ``path``.
    """

    def test_original_file_intact_after_mid_write_exception(
        self, tmp_path: Path
    ) -> None:
        """Original state.json is intact after a simulated mid-write crash.

        Strategy: pre-populate a valid sentinel state.json, then mock json.dump
        to raise an exception, call persist(), and assert the sentinel bytes
        are unchanged.
        """
        state_path = tmp_path / "state.json"
        sentinel_content = json.dumps({"sentinel": "original"})
        state_path.write_text(sentinel_content, encoding="utf-8")

        orch = OrchestratorState(max_concurrent=3)
        issue_st = _make_issue_state()
        orch.add_running(issue_st.issue_number, issue_st)

        # Simulate a crash mid-write by raising from json.dump.
        with patch(
            "baton_harness.vendor.symphony.state.json.dump",
            side_effect=OSError("disk full"),
        ):
            try:
                orch.persist(str(state_path))
            except Exception:
                # persist() may propagate or swallow — either is acceptable as
                # long as the original file is intact (atomicity guarantee).
                pass

        actual = state_path.read_text(encoding="utf-8")
        assert actual == sentinel_content, (
            "persist() corrupted state.json on a mid-write crash. "
            "The original sentinel content must be intact "
            "(atomic write via tempfile + os.replace required)."
        )


# ---------------------------------------------------------------------------
# 5. persist() uses os.replace (atomic rename pattern)
# ---------------------------------------------------------------------------


class TestPersistUsesOsReplace:
    """persist() calls os.replace to atomically promote the temp file."""

    def test_os_replace_called_with_target_path(self, tmp_path: Path) -> None:
        """os.replace is called with the target state.json path as dest."""
        state_path = str(tmp_path / "state.json")
        orch = OrchestratorState(max_concurrent=3)
        orch.add_running(_make_issue_state().issue_number, _make_issue_state())

        replace_calls: list[tuple[Any, Any]] = []

        real_replace = os.replace

        def capturing_replace(src: str, dst: str) -> None:
            replace_calls.append((src, dst))
            real_replace(src, dst)

        with patch(
            "baton_harness.vendor.symphony.state.os.replace",
            side_effect=capturing_replace,
        ):
            orch.persist(state_path)

        assert replace_calls, (
            "os.replace was never called during persist(). "
            "The implementation must use tempfile + os.replace for atomicity."
        )
        # The destination of every replace call must be the target path.
        destinations = [dst for _, dst in replace_calls]
        assert state_path in destinations, (
            f"os.replace was not called with {state_path!r} as destination. "
            f"Actual destinations: {destinations}"
        )

    def test_write_goes_to_temp_then_renamed_to_final_path(
        self, tmp_path: Path
    ) -> None:
        """open() writes to a temp path, then os.replace renames to final path.

        Verify that the file opened for writing is NOT the final state.json
        path (it must be a temp path), and that os.replace promotes it.
        This test uses a capturing wrapper around builtins.open and os.replace
        to observe the sequence.
        """
        state_path = tmp_path / "state.json"
        orch = OrchestratorState(max_concurrent=3)

        opened_paths: list[str] = []
        replace_src_paths: list[str] = []
        real_open = open
        real_replace = os.replace

        def capturing_open(  # type: ignore[override]
            file: object, mode: str = "r", **kwargs: object
        ) -> object:
            if "w" in str(mode):
                opened_paths.append(str(file))
            return real_open(file, mode, **kwargs)  # type: ignore[call-overload]

        def capturing_replace(src: str, dst: str) -> None:
            replace_src_paths.append(src)
            real_replace(src, dst)

        with (
            patch("builtins.open", side_effect=capturing_open),
            patch(
                "baton_harness.vendor.symphony.state.os.replace",
                side_effect=capturing_replace,
            ),
        ):
            orch.persist(str(state_path))

        final = str(state_path)
        # The file opened for writing must NOT be the final path.
        write_to_final = [p for p in opened_paths if p == final]
        assert write_to_final == [], (
            "persist() wrote directly to the final state.json path. "
            "It must write to a temp file first, then rename via os.replace. "
            f"Paths opened for write: {opened_paths}"
        )
        # os.replace must have been called (moving temp → final).
        assert replace_src_paths, (
            "os.replace was never called. persist() must rename a temp file "
            "to the final path for atomicity."
        )


# ---------------------------------------------------------------------------
# 6. load() ignores completed_count (not restored as set members)
# ---------------------------------------------------------------------------


class TestLoadIgnoresCompletedCount:
    """completed_count in JSON is an integer; load() must not recreate members.

    The spec explicitly states only running, retry_queue, and claimed are
    restorable. completed is not tracked across restarts (by design).
    """

    def test_completed_set_empty_after_load(self, tmp_path: Path) -> None:
        """Completed set is empty after load() with completed_count > 0."""
        state_path = tmp_path / "state.json"
        # Write a state.json that claims a large completed_count.
        payload: dict[str, Any] = {
            "running": [],
            "retrying": [],
            "claimed": [],
            "completed_count": 99,
        }
        state_path.write_text(json.dumps(payload), encoding="utf-8")

        state = OrchestratorState(max_concurrent=3)
        state.load(str(state_path))

        assert state.completed == set(), (
            "completed set must remain empty after load(); "
            "completed_count is not recoverable per spec."
        )

    def test_completed_count_not_inflated_as_integers(
        self, tmp_path: Path
    ) -> None:
        """Completed does not contain synthetic integers from completed_count.

        A naive implementation might do ``self.completed = set(range(count))``
        — this test guards against that specific footgun.
        """
        state_path = tmp_path / "state.json"
        payload: dict[str, Any] = {
            "running": [],
            "retrying": [],
            "claimed": [],
            "completed_count": 5,
        }
        state_path.write_text(json.dumps(payload), encoding="utf-8")

        state = OrchestratorState(max_concurrent=3)
        state.load(str(state_path))

        assert len(state.completed) == 0, (
            f"Expected completed to be empty, got {state.completed!r}. "
            "completed_count must not be converted to fake set members."
        )


# ---------------------------------------------------------------------------
# 7. load() restores correctly after multiple persist cycles
# ---------------------------------------------------------------------------


class TestMultiplePersistCycles:
    """Second persist() overwrites the first; load() sees the final state."""

    def test_second_persist_overwrites_first(self, tmp_path: Path) -> None:
        """load() after two persist() calls reflects only the second state."""
        state_path = str(tmp_path / "state.json")

        # First persist: issue 1 running.
        orch1 = OrchestratorState(max_concurrent=3)
        issue1 = _make_issue_state(issue_number=1, title="First issue")
        orch1.add_running(1, issue1)
        orch1.persist(state_path)

        # Second persist: issue 2 running (issue 1 removed).
        orch2 = OrchestratorState(max_concurrent=3)
        issue2 = _make_issue_state(issue_number=2, title="Second issue")
        orch2.add_running(2, issue2)
        orch2.persist(state_path)

        fresh = OrchestratorState(max_concurrent=3)
        fresh.load(state_path)

        assert 2 in fresh.running, "Issue 2 from second persist not found."
        assert 1 not in fresh.running, (
            "Issue 1 from first persist should not appear after "
            "second persist."
        )

    def test_second_persist_title_matches_second_state(
        self, tmp_path: Path
    ) -> None:
        """Title after load() matches the second persist, not the first."""
        state_path = str(tmp_path / "state.json")

        orch1 = OrchestratorState(max_concurrent=3)
        orch1.add_running(
            1, _make_issue_state(issue_number=1, title="First issue")
        )
        orch1.persist(state_path)

        orch2 = OrchestratorState(max_concurrent=3)
        orch2.add_running(
            2, _make_issue_state(issue_number=2, title="Second issue")
        )
        orch2.persist(state_path)

        fresh = OrchestratorState(max_concurrent=3)
        fresh.load(state_path)

        assert fresh.running[2].title == "Second issue"


# ---------------------------------------------------------------------------
# 8. Edge case: empty state round-trip
# ---------------------------------------------------------------------------


class TestEmptyStateRoundTrip:
    """persist() then load() on a fully-empty state produces an empty state."""

    def test_empty_state_running_still_empty(self, tmp_path: Path) -> None:
        """Running is empty after empty-state round-trip."""
        state_path = str(tmp_path / "state.json")
        OrchestratorState(max_concurrent=3).persist(state_path)

        fresh = OrchestratorState(max_concurrent=3)
        fresh.load(state_path)

        assert fresh.running == {}

    def test_empty_state_retry_queue_still_empty(self, tmp_path: Path) -> None:
        """retry_queue is empty after empty-state round-trip."""
        state_path = str(tmp_path / "state.json")
        OrchestratorState(max_concurrent=3).persist(state_path)

        fresh = OrchestratorState(max_concurrent=3)
        fresh.load(state_path)

        assert fresh.retry_queue == {}

    def test_empty_state_claimed_still_empty(self, tmp_path: Path) -> None:
        """Claimed is empty after empty-state round-trip."""
        state_path = str(tmp_path / "state.json")
        OrchestratorState(max_concurrent=3).persist(state_path)

        fresh = OrchestratorState(max_concurrent=3)
        fresh.load(state_path)

        assert fresh.claimed == set()


# ---------------------------------------------------------------------------
# 9. Edge case: last_event_at field omission / preservation
# ---------------------------------------------------------------------------


class TestLastEventAtHandling:
    """last_event_at is absent from current persist() output.

    # SPEC-AMBIGUITY: persist() does not currently serialize last_event_at
    # (the field exists on IssueState but is omitted from the running-list
    # dict in persist()). The spec says "round-trip with exact field values"
    # but does not state whether last_event_at should be extended into the
    # serialized format or remain omitted.
    #
    # Decision taken here (pending code-writer adjudication): load() sets
    # last_event_at to None for every restored IssueState, because the field
    # is absent from the persisted JSON. If the implementation extends
    # persist() to include last_event_at, these tests need updating to assert
    # preservation of the exact value instead.
    #
    # Code-writer: if you extend persist() to serialize last_event_at, change
    # the assertion below from ``is None`` to ``== 1_700_000_500.0`` and
    # confirm the round-trip test passes.
    """

    def test_last_event_at_is_none_after_load(self, tmp_path: Path) -> None:
        """last_event_at is None on a restored IssueState (omitted from JSON).

        This test encodes the current contract: last_event_at is NOT persisted,
        so load() must set it to None. If the implementation extends
        serialization to include last_event_at, update this assertion.
        """
        state_path = str(tmp_path / "state.json")
        orch = OrchestratorState(max_concurrent=3)
        # last_event_at=1_700_000_500.0 in the original.
        issue_st = _make_issue_state(last_event_at=1_700_000_500.0)
        orch.add_running(issue_st.issue_number, issue_st)
        orch.persist(state_path)

        fresh = OrchestratorState(max_concurrent=3)
        fresh.load(state_path)

        # SPEC-AMBIGUITY: see class docstring.
        assert fresh.running[42].last_event_at is None, (
            "last_event_at should be None after load() because persist() "
            "does not serialize it. If the implementation adds last_event_at "
            "to persist(), change this assertion to check the exact value."
        )
