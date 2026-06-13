"""Unit tests for baton_harness.chain.runlog.

Tests the JSONL run-record substrate.  All filesystem I/O is
intercepted by patching the module-local ``_write_line`` seam (mirrors
the ``escalation._run`` seam pattern).  At least one end-to-end test
uses a real ``tmp_path`` to confirm the file is written correctly.

Coverage:
- Each ``emit`` call produces exactly one ``_write_line`` call whose
  payload is valid JSONL (single JSON object per line, trailing newline).
- Two successive ``emit`` calls produce two ordered ``_write_line``
  calls (append semantics).
- End-to-end: two ``emit`` calls on a real temp file yield two valid
  JSONL lines.
- Construction with a missing parent directory neither raises nor
  prevents a subsequent ``emit`` from succeeding.
- ``_write_line`` raising ``OSError`` is swallowed by ``emit`` (best-
  effort); a WARNING is logged.
- Full-schema dict is written verbatim — no injected or dropped keys.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import baton_harness.chain.runlog as runlog_mod
import pytest
from baton_harness.chain.runlog import RunLog

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FULL_EVENT: dict[str, object] = {
    "ts": "2026-06-13T12:00:00Z",
    "event": "daemon_start",
    "issue": None,
    "outcome": None,
    "severity": "info",
    "detail": "daemon starting up",
    "tick_id": "tick-001",
}


# ---------------------------------------------------------------------------
# emit produces exactly one valid JSONL line per call
# ---------------------------------------------------------------------------


def test_emit_produces_one_write_line_call(tmp_path: Path) -> None:
    """Emit calls _write_line exactly once with a valid JSONL payload."""
    log = RunLog(tmp_path / "runlog.jsonl")
    event = {"event": "test", "value": 42}

    with patch.object(runlog_mod, "_write_line") as mock_write:
        log.emit(event)

    assert mock_write.call_count == 1
    written_line: str = mock_write.call_args[0][1]
    # Must end with exactly one newline.
    assert written_line.endswith("\n"), (
        "Written line must end with a newline character"
    )
    # Must not contain embedded newlines (JSONL = one object per line).
    assert written_line.count("\n") == 1, (
        "Written line must contain exactly one newline (no embedded newlines)"
    )
    # Payload must round-trip back to the original dict.
    parsed = json.loads(written_line)
    assert parsed == event


def test_emit_line_is_json_dumps_plus_newline(tmp_path: Path) -> None:
    """Written line is json.dumps(event) + newline, no extra whitespace."""
    log = RunLog(tmp_path / "runlog.jsonl")
    event = {"event": "dispatch", "issue": 7}

    with patch.object(runlog_mod, "_write_line") as mock_write:
        log.emit(event)

    written_line: str = mock_write.call_args[0][1]
    expected = json.dumps(event) + "\n"
    assert written_line == expected


# ---------------------------------------------------------------------------
# Append semantics: two emits → two ordered _write_line calls
# ---------------------------------------------------------------------------


def test_emit_two_events_produces_two_write_line_calls(
    tmp_path: Path,
) -> None:
    """Two successive emit calls produce two _write_line calls in order."""
    log = RunLog(tmp_path / "runlog.jsonl")
    event_a = {"event": "first", "seq": 1}
    event_b = {"event": "second", "seq": 2}

    with patch.object(runlog_mod, "_write_line") as mock_write:
        log.emit(event_a)
        log.emit(event_b)

    assert mock_write.call_count == 2
    line_a: str = mock_write.call_args_list[0][0][1]
    line_b: str = mock_write.call_args_list[1][0][1]
    assert json.loads(line_a) == event_a
    assert json.loads(line_b) == event_b


def test_emit_two_events_end_to_end_writes_two_jsonl_lines(
    tmp_path: Path,
) -> None:
    """End-to-end: two emit calls write two valid JSONL lines to disk."""
    runlog_path = tmp_path / "runlog.jsonl"
    log = RunLog(runlog_path)
    event_a = {"event": "daemon_start", "tick_id": "t1"}
    event_b = {"event": "outcome", "issue": 99}

    log.emit(event_a)
    log.emit(event_b)

    raw = runlog_path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    assert len(lines) == 2, (
        f"Expected 2 JSONL lines, got {len(lines)}: {lines!r}"
    )
    assert json.loads(lines[0]) == event_a
    assert json.loads(lines[1]) == event_b


# ---------------------------------------------------------------------------
# mkdir / missing parent directory
# ---------------------------------------------------------------------------


def test_runlog_construction_with_missing_parents_does_not_raise(
    tmp_path: Path,
) -> None:
    """Constructing RunLog with non-existent parents does not raise."""
    nested = tmp_path / "nested" / "deep" / "runlog.jsonl"
    # Confirm parents do not exist yet.
    assert not nested.parent.exists()

    # Must not raise.
    log = RunLog(nested)

    # Either the parent was created, OR a subsequent emit does not raise.
    parent_created = nested.parent.exists()
    if not parent_created:
        # Emit must not raise even without the directory.
        with patch.object(runlog_mod, "_write_line"):
            log.emit({"event": "test"})
    else:
        # Parent was created — emit can proceed end-to-end.
        log.emit({"event": "test"})
        assert nested.exists()


def test_runlog_construction_creates_parent_directory(
    tmp_path: Path,
) -> None:
    """RunLog construction mkdir-parents the log file's parent directory."""
    nested = tmp_path / "a" / "b" / "c" / "runlog.jsonl"
    assert not nested.parent.exists()

    RunLog(nested)

    assert nested.parent.exists(), (
        "RunLog must create parent directories on construction"
    )


# ---------------------------------------------------------------------------
# Write failure is swallowed (best-effort)
# ---------------------------------------------------------------------------


def test_emit_swallows_oserror_from_write_line(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Emit returns None and does NOT raise when _write_line raises OSError."""
    log = RunLog(tmp_path / "runlog.jsonl")

    with (
        patch.object(
            runlog_mod, "_write_line", side_effect=OSError("disk full")
        ),
        caplog.at_level(
            logging.WARNING,
            logger="baton_harness.chain.runlog",
        ),
    ):
        result = log.emit({"event": "test"})

    assert result is None
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "A WARNING must be logged when _write_line raises"
    )


def test_emit_swallows_file_not_found_from_write_line(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Emit does NOT raise when _write_line raises FileNotFoundError."""
    log = RunLog(tmp_path / "runlog.jsonl")

    with (
        patch.object(
            runlog_mod,
            "_write_line",
            side_effect=FileNotFoundError("no such file"),
        ),
        caplog.at_level(
            logging.WARNING,
            logger="baton_harness.chain.runlog",
        ),
    ):
        result = log.emit({"event": "test"})

    assert result is None


# ---------------------------------------------------------------------------
# Full-schema dict written verbatim — no injected or dropped keys
# ---------------------------------------------------------------------------


def test_emit_full_schema_dict_written_verbatim(tmp_path: Path) -> None:
    """Full-schema event dict written verbatim — no injected/dropped keys."""
    log = RunLog(tmp_path / "runlog.jsonl")

    with patch.object(runlog_mod, "_write_line") as mock_write:
        log.emit(_FULL_EVENT)

    written_line: str = mock_write.call_args[0][1]
    parsed = json.loads(written_line)
    assert parsed == _FULL_EVENT, (
        f"emit must write the dict verbatim; got {parsed!r}"
    )
    # No extra keys injected.
    assert set(parsed.keys()) == set(_FULL_EVENT.keys()), (
        "emit must not inject extra keys into the written JSON"
    )


def test_emit_preserves_none_values_in_full_schema(tmp_path: Path) -> None:
    """None values in the event dict survive the JSON round-trip as null."""
    log = RunLog(tmp_path / "runlog.jsonl")
    event = {**_FULL_EVENT, "issue": None, "outcome": None, "tick_id": None}

    with patch.object(runlog_mod, "_write_line") as mock_write:
        log.emit(event)

    written_line: str = mock_write.call_args[0][1]
    parsed = json.loads(written_line)
    assert parsed["issue"] is None
    assert parsed["outcome"] is None
    assert parsed["tick_id"] is None


# ---------------------------------------------------------------------------
# _write_line seam: path is passed correctly
# ---------------------------------------------------------------------------


def test_emit_passes_correct_path_to_write_line(tmp_path: Path) -> None:
    """Emit passes the RunLog's path as the first arg to _write_line."""
    runlog_path = tmp_path / "run.jsonl"
    log = RunLog(runlog_path)

    with patch.object(runlog_mod, "_write_line") as mock_write:
        log.emit({"event": "test"})

    called_path: Path = mock_write.call_args[0][0]
    assert called_path == runlog_path
