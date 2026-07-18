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


def _valid_running_dict(
    issue_number: int = 1,
    identifier: str | None = None,
    title: str = "Some issue",
    state: str = "open",
    turn: int = 0,
    max_turns: int = 8,
    started_at: float = _SENTINEL_STARTED_AT,
    last_event: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Return a well-formed raw JSON dict for a `running` list entry.

    Mirrors the exact key set persist() writes for an IssueState, so it can
    be embedded directly into a hand-built state.json payload for load()
    schema tests.

    Args:
        issue_number: GitHub issue number.
        identifier: Repo-scoped identifier string; defaults to a value
            derived from issue_number when None.
        title: Issue title.
        state: Issue state string (e.g. "open").
        turn: Current turn index.
        max_turns: Maximum allowed turns.
        started_at: Unix timestamp when processing started.
        last_event: Description of the last event.
        error: Error string if the last turn failed.

    Returns:
        A dict matching the JSON shape persist() writes for one running
        entry.
    """
    if identifier is None:
        identifier = f"owner/repo#{issue_number}"
    return {
        "issue_number": issue_number,
        "identifier": identifier,
        "title": title,
        "state": state,
        "turn": turn,
        "max_turns": max_turns,
        "started_at": started_at,
        "last_event": last_event,
        "error": error,
    }


def _valid_retrying_dict(
    issue_number: int = 7,
    identifier: str | None = None,
    attempt: int = 1,
    due_at: float = _SENTINEL_DUE_AT,
    error: str | None = None,
) -> dict[str, Any]:
    """Return a well-formed raw JSON dict for a `retrying` list entry.

    Mirrors the exact key set persist() writes for a RetryEntry.

    Args:
        issue_number: GitHub issue number.
        identifier: Repo-scoped identifier string; defaults to a value
            derived from issue_number when None.
        attempt: Retry attempt count.
        due_at: Unix timestamp when this retry becomes eligible.
        error: Error string from the previous attempt.

    Returns:
        A dict matching the JSON shape persist() writes for one retrying
        entry.
    """
    if identifier is None:
        identifier = f"owner/repo#{issue_number}"
    return {
        "issue_number": issue_number,
        "identifier": identifier,
        "attempt": attempt,
        "due_at": due_at,
        "error": error,
    }


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
# 3a. load() on a non-object JSON root: no exception, fresh state
# ---------------------------------------------------------------------------


class TestLoadNonObjectJsonRoot:
    """load() on a non-object JSON root does not raise and resets state."""

    def test_load_non_object_root_does_not_raise(self, tmp_path: Path) -> None:
        """load() on a JSON array root must not raise any exception."""
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps([]), encoding="utf-8")

        state = OrchestratorState(max_concurrent=3)
        state.load(str(state_path))

    def test_load_non_object_root_running_stays_empty(
        self, tmp_path: Path
    ) -> None:
        """Running is empty after load() on a JSON array root."""
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps([]), encoding="utf-8")

        state = OrchestratorState(max_concurrent=3)
        state.load(str(state_path))

        assert state.running == {}

    def test_load_non_object_root_retry_queue_stays_empty(
        self, tmp_path: Path
    ) -> None:
        """retry_queue is empty after load() on a JSON array root."""
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps([]), encoding="utf-8")

        state = OrchestratorState(max_concurrent=3)
        state.load(str(state_path))

        assert state.retry_queue == {}

    def test_load_non_object_root_claimed_stays_empty(
        self, tmp_path: Path
    ) -> None:
        """Claimed is empty after load() on a JSON array root."""
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps([]), encoding="utf-8")

        state = OrchestratorState(max_concurrent=3)
        state.load(str(state_path))

        assert state.claimed == set()


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


# ---------------------------------------------------------------------------
# 10. load() on malformed schema in `running`: no uncaught exception (#262)
# ---------------------------------------------------------------------------


class TestLoadMalformedRunningEntrySchema:
    """A `running` entry missing a required key must not crash load().

    Today, ``entry["issue_number"]`` (and similar direct subscripts) inside
    the running-reconstruction loop raise an uncaught KeyError that is not
    covered by the ``except (json.JSONDecodeError, OSError,
    UnicodeDecodeError)`` clause, so it propagates out of load() entirely.
    Per #262, a malformed schema must be handled the same way as a JSON
    parse failure: no exception, clean empty state.
    """

    def test_does_not_raise(self, tmp_path: Path) -> None:
        """load() must not raise on a running entry missing issue_number."""
        malformed = _valid_running_dict(issue_number=1)
        del malformed["issue_number"]
        payload: dict[str, Any] = {
            "running": [malformed],
            "retrying": [],
            "claimed": [],
            "completed_count": 0,
        }
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps(payload), encoding="utf-8")

        state = OrchestratorState(max_concurrent=3)
        # Must not raise -- currently raises an uncaught KeyError.
        state.load(str(state_path))

    def test_running_stays_empty(self, tmp_path: Path) -> None:
        """Running dict is empty after load() on a malformed running entry."""
        malformed = _valid_running_dict(issue_number=1)
        del malformed["issue_number"]
        payload: dict[str, Any] = {
            "running": [malformed],
            "retrying": [],
            "claimed": [],
            "completed_count": 0,
        }
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps(payload), encoding="utf-8")

        state = OrchestratorState(max_concurrent=3)
        state.load(str(state_path))

        assert state.running == {}

    def test_retry_queue_stays_empty(self, tmp_path: Path) -> None:
        """retry_queue is empty after load() on a malformed running entry."""
        malformed = _valid_running_dict(issue_number=1)
        del malformed["issue_number"]
        payload: dict[str, Any] = {
            "running": [malformed],
            "retrying": [],
            "claimed": [],
            "completed_count": 0,
        }
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps(payload), encoding="utf-8")

        state = OrchestratorState(max_concurrent=3)
        state.load(str(state_path))

        assert state.retry_queue == {}

    def test_claimed_stays_empty(self, tmp_path: Path) -> None:
        """Claimed set is empty after load() on a malformed running entry."""
        malformed = _valid_running_dict(issue_number=1)
        del malformed["issue_number"]
        payload: dict[str, Any] = {
            "running": [malformed],
            "retrying": [],
            "claimed": [],
            "completed_count": 0,
        }
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps(payload), encoding="utf-8")

        state = OrchestratorState(max_concurrent=3)
        state.load(str(state_path))

        assert state.claimed == set()

    def test_preexisting_state_survives_failure(self, tmp_path: Path) -> None:
        """Malformed running data preserves all pre-existing state."""
        malformed = _valid_running_dict(issue_number=1)
        del malformed["issue_number"]
        payload: dict[str, Any] = {
            "running": [malformed],
            "retrying": [],
            "claimed": [],
            "completed_count": 0,
        }
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps(payload), encoding="utf-8")

        state = OrchestratorState(max_concurrent=3)
        expected_running = _make_issue_state()
        expected_retry = _make_retry_entry()
        state.add_running(expected_running.issue_number, expected_running)
        state.retry_queue[expected_retry.issue_number] = expected_retry
        state.claimed.add(expected_retry.issue_number)

        state.load(str(state_path))

        assert state.running == {42: expected_running}
        assert state.retry_queue == {7: expected_retry}
        assert state.claimed == {42, 7}


# ---------------------------------------------------------------------------
# 11. load() is transactional: no partial mutation on running failure (#262)
# ---------------------------------------------------------------------------


class TestLoadNoPartialMutationOnRunningSchemaFailure:
    """The first valid `running` entry must not survive a later failure.

    Today the running loop mutates ``self.running`` in place, one entry at
    a time, as it iterates -- so a failure on the second entry leaves the
    first entry's mutation already applied. Per #262, load() must be
    transactional: either fully loaded or cleanly empty, never
    half-populated.
    """

    def test_does_not_raise(self, tmp_path: Path) -> None:
        """load() must not raise when the second running entry is malformed."""
        valid_entry = _valid_running_dict(issue_number=1)
        malformed_entry = _valid_running_dict(issue_number=2)
        del malformed_entry["issue_number"]
        payload: dict[str, Any] = {
            "running": [valid_entry, malformed_entry],
            "retrying": [],
            "claimed": [],
            "completed_count": 0,
        }
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps(payload), encoding="utf-8")

        state = OrchestratorState(max_concurrent=3)
        # Must not raise -- currently raises an uncaught KeyError.
        state.load(str(state_path))

    def test_first_valid_entry_does_not_survive(self, tmp_path: Path) -> None:
        """The first, well-formed running entry must not survive.

        A later entry in the same list fails to parse. This is the
        transactional guarantee: partial success is not success. Today
        this fails because issue 1 is inserted into self.running before
        the second entry raises.
        """
        valid_entry = _valid_running_dict(issue_number=1)
        malformed_entry = _valid_running_dict(issue_number=2)
        del malformed_entry["issue_number"]
        payload: dict[str, Any] = {
            "running": [valid_entry, malformed_entry],
            "retrying": [],
            "claimed": [],
            "completed_count": 0,
        }
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps(payload), encoding="utf-8")

        state = OrchestratorState(max_concurrent=3)
        state.load(str(state_path))

        assert state.running == {}, (
            "self.running must be empty when any entry in the running "
            "list fails to parse -- the first, valid entry must not "
            f"survive as a partial mutation. Got: {state.running!r}"
        )


# ---------------------------------------------------------------------------
# 12. load() malformed `retrying` entry spans transactionality across
#     collections (#262)
# ---------------------------------------------------------------------------


class TestLoadMalformedRetryingEntrySchema:
    """A malformed `retrying` entry must not crash load().

    Even though the running loop runs (and would succeed) before the
    retrying loop hits the malformed entry, a failure anywhere in load()
    must roll back to a fully empty state across ALL collections -- not
    just the collection where the failure occurred.
    """

    def _payload(self) -> dict[str, Any]:
        """Build a payload with a valid running entry, malformed retrying.

        The retrying entry is missing issue_number.

        Returns:
            A JSON-serializable dict for state.json.
        """
        valid_running = _valid_running_dict(issue_number=1)
        malformed_retrying = _valid_retrying_dict(issue_number=7)
        del malformed_retrying["issue_number"]
        return {
            "running": [valid_running],
            "retrying": [malformed_retrying],
            "claimed": [],
            "completed_count": 0,
        }

    def test_does_not_raise(self, tmp_path: Path) -> None:
        """load() must not raise when a retrying entry is malformed."""
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps(self._payload()), encoding="utf-8")

        state = OrchestratorState(max_concurrent=3)
        # Must not raise -- currently raises an uncaught KeyError.
        state.load(str(state_path))

    def test_running_stays_empty(self, tmp_path: Path) -> None:
        """The valid running entry must not survive a retrying failure."""
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps(self._payload()), encoding="utf-8")

        state = OrchestratorState(max_concurrent=3)
        state.load(str(state_path))

        assert state.running == {}, (
            "self.running must be empty when the retrying list fails to "
            "parse, even though the running entry itself was well-formed. "
            f"Got: {state.running!r}"
        )

    def test_retry_queue_stays_empty(self, tmp_path: Path) -> None:
        """retry_queue is empty after load() on a malformed retrying entry."""
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps(self._payload()), encoding="utf-8")

        state = OrchestratorState(max_concurrent=3)
        state.load(str(state_path))

        assert state.retry_queue == {}

    def test_claimed_stays_empty(self, tmp_path: Path) -> None:
        """Claimed set is empty after load() on a malformed retrying entry."""
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps(self._payload()), encoding="utf-8")

        state = OrchestratorState(max_concurrent=3)
        state.load(str(state_path))

        assert state.claimed == set()

    def test_preexisting_state_survives_failure(self, tmp_path: Path) -> None:
        """Malformed retrying data preserves all pre-existing state."""
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps(self._payload()), encoding="utf-8")

        state = OrchestratorState(max_concurrent=3)
        expected_running = _make_issue_state(
            issue_number=99,
            identifier="owner/repo#99",
        )
        expected_retry = _make_retry_entry(
            issue_number=88,
            identifier="owner/repo#88",
        )
        state.add_running(expected_running.issue_number, expected_running)
        state.retry_queue[expected_retry.issue_number] = expected_retry
        state.claimed.add(expected_retry.issue_number)

        state.load(str(state_path))

        assert state.running == {99: expected_running}
        assert state.retry_queue == {88: expected_retry}
        assert state.claimed == {99, 88}


# ---------------------------------------------------------------------------
# 13. load() malformed `claimed` entry (wrong type) doesn't crash (#262)
# ---------------------------------------------------------------------------


class TestLoadMalformedClaimedEntryType:
    """A `claimed` entry that fails int() conversion must not crash load().

    Today ``int(num)`` on a non-numeric claimed value raises an uncaught
    ValueError/TypeError. Per #262 this must degrade gracefully to a clean
    empty state, and transactionality spans to the other collections too.
    """

    def _payload(self) -> dict[str, Any]:
        """Build a payload with a valid running entry, unparseable claimed.

        The claimed list contains one value that cannot be coerced to int.

        Returns:
            A JSON-serializable dict for state.json.
        """
        valid_running = _valid_running_dict(issue_number=1)
        return {
            "running": [valid_running],
            "retrying": [],
            "claimed": [{"not": "a number"}],
            "completed_count": 0,
        }

    def test_does_not_raise(self, tmp_path: Path) -> None:
        """load() must not raise when a claimed entry is the wrong type."""
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps(self._payload()), encoding="utf-8")

        state = OrchestratorState(max_concurrent=3)
        # Must not raise -- currently raises an uncaught TypeError.
        state.load(str(state_path))

    def test_claimed_stays_empty(self, tmp_path: Path) -> None:
        """Claimed set is empty after load() on a malformed claimed entry."""
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps(self._payload()), encoding="utf-8")

        state = OrchestratorState(max_concurrent=3)
        state.load(str(state_path))

        assert state.claimed == set()

    def test_running_stays_empty(self, tmp_path: Path) -> None:
        """The valid running entry must not survive a claimed-side failure."""
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps(self._payload()), encoding="utf-8")

        state = OrchestratorState(max_concurrent=3)
        state.load(str(state_path))

        assert state.running == {}, (
            "self.running must be empty when the claimed list fails to "
            "parse, even though the running entry itself was well-formed. "
            f"Got: {state.running!r}"
        )

    def test_preexisting_state_survives_failure(self, tmp_path: Path) -> None:
        """Malformed claimed data preserves all pre-existing state."""
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps(self._payload()), encoding="utf-8")

        state = OrchestratorState(max_concurrent=3)
        expected_running = _make_issue_state(
            issue_number=99,
            identifier="owner/repo#99",
        )
        expected_retry = _make_retry_entry(
            issue_number=88,
            identifier="owner/repo#88",
        )
        state.add_running(expected_running.issue_number, expected_running)
        state.retry_queue[expected_retry.issue_number] = expected_retry
        state.claimed.add(expected_retry.issue_number)

        state.load(str(state_path))

        assert state.running == {99: expected_running}
        assert state.retry_queue == {88: expected_retry}
        assert state.claimed == {99, 88}

    def test_claimed_numeric_overflow_preserves_state(
        self, tmp_path: Path
    ) -> None:
        """An overflowing claimed number preserves pre-existing state."""
        payload: dict[str, Any] = {
            "running": [],
            "retrying": [],
            "claimed": [1e309],
            "completed_count": 0,
        }
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps(payload), encoding="utf-8")

        state = _populated_state()
        expected_running = dict(state.running)
        expected_retry_queue = dict(state.retry_queue)
        expected_claimed = set(state.claimed)

        state.load(str(state_path))

        assert state.running == expected_running
        assert state.retry_queue == expected_retry_queue
        assert state.claimed == expected_claimed
