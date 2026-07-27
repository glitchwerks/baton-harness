"""Unit tests for baton_harness.chain.session_report (issue #243, Phase 1).

``session_report.py`` does not exist yet — this file is the frozen
contract for the greenfield ``SessionReport`` model described in
``docs/superpowers/plans/2026-07-09-daemon-report-scenario-harness-243.md``
§ Phase 1 / § 3 / § 3.1. Importing the module below is expected to raise
``ModuleNotFoundError`` until the implementation lands; that import-time
failure is the intended "red" for this phase, not a broken test harness.

Coverage:
- A freshly constructed report starts with empty issue/tick/startup
  collections and an unset exit state.
- ``to_dict()`` surfaces every schema field from § 3 (including the
  provenance-relevant fields called out in § 3.1: ``merge_gate.merged_sha``,
  ``escalations[].kind``, ``startup.findings[].pids``,
  ``session.poll_interval_s``, ``issue.park_kind``) and is fully
  JSON-serializable.
- ``totals`` rolls up ticks / pickups / PRs / merges / parks /
  escalations / CI-gate failures correctly.
- ``record_pickup`` upserts an ``IssueRecord`` by issue number — recording
  the same number twice updates the existing record in place rather than
  duplicating it.
- ``set_outcomes`` (the B2 aggregation entry point — deliberately NOT a
  per-site ``record_park``) folds ``merged_issues`` / ``parked_reasons``
  into the right per-issue records, including a ``park_kind`` derived
  from the park reason text (§ 2.4: "the park reason_text already
  distinguishes a block from a plain failure"), and upserts a record for
  an issue number with no prior ``record_pickup`` call.
- ``record_label_edit`` appends ordered ``label_transitions`` entries;
  order is preserved and is not treated as a set (§ 5.1 M12).
- ``begin_tick`` / ``end_tick`` bracket a poll cycle into an ordered,
  closed ``TickRecord``.
- ``set_exit_reason`` records the session exit reason and end timestamp.
- ``write(path)`` end-to-end: serializes to disk as exactly
  ``to_dict()``'s JSON.
- ``write(path)`` best-effort (M9): a missing parent directory does not
  raise.
- ``write(path)`` atomicity (M9): a crash at the ``os.replace`` boundary
  (after the temp file would have been written) leaves the prior report
  file wholly intact, and the temp-then-replace call shape
  (``<path>.tmp.<pid>``) is confirmed.

Assumptions made when concretizing signatures the plan only sketches
(see the router return for the full list): ``record_pickup``,
``record_label_edit``, ``record_pr``, ``record_escalation``, and
``record_merge_gate`` all take the issue ``number`` as their first
argument (the plan's abbreviated ``record_merge_gate(outcome,
merged_sha)`` etc. describes the daemon-side call, not the full model
signature); all timestamps are explicit string parameters (never
implicit ``now()``-only) so tests stay deterministic; ``park_kind`` is
derived by ``set_outcomes`` from a case-insensitive ``"block"`` substring
match on the park reason text.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import baton_harness.chain.session_report as session_report_mod
from baton_harness.chain.session_report import SessionReport

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_report() -> SessionReport:
    """Construct a bare SessionReport with fixed session metadata."""
    return SessionReport(
        mode="once",
        poll_interval_s=10.0,
        registry=[{"owner": "glitchwerks", "repo": "sandbox"}],
        started_at="2026-07-09T21:00:00.000000+00:00",
    )


# ---------------------------------------------------------------------------
# Construction defaults
# ---------------------------------------------------------------------------


def test_new_report_starts_with_empty_collections_and_unset_exit_state() -> (
    None
):
    """A fresh SessionReport has empty issue/tick/startup collections."""
    report = _new_report()

    d = report.to_dict()
    assert d["schema_version"] == 1
    assert d["issues"] == []
    assert d["ticks"] == []
    assert d["startup"]["findings"] == []
    assert d["session"]["exit_reason"] is None
    assert d["session"]["ended_at"] is None
    assert d["session"]["mode"] == "once"
    assert d["session"]["poll_interval_s"] == 10.0
    assert d["session"]["registry"] == [
        {"owner": "glitchwerks", "repo": "sandbox"}
    ]
    assert d["session"]["started_at"] == "2026-07-09T21:00:00.000000+00:00"


# ---------------------------------------------------------------------------
# Schema shape + provenance-relevant fields (§ 3 / § 3.1)
# ---------------------------------------------------------------------------


def test_to_dict_produces_full_schema_with_provenance_fields() -> None:
    """to_dict() surfaces every § 3 field and round-trips through JSON."""
    report = _new_report()
    report.record_startup_finding(
        gate="G2",
        detail="Prior daemon run ended ungracefully",
        pids=None,
    )
    report.record_startup_finding(
        gate="G1",
        detail="Orphan claude processes detected at startup",
        pids=[12345],
    )
    report.record_pickup(
        number=42,
        repo="glitchwerks/sandbox",
        title="add greet()",
        picked_up_at="2026-07-09T21:00:05.000000+00:00",
    )
    report.record_label_edit(
        number=42,
        added=["agent-in-progress"],
        removed=["agent-ready"],
        ts="2026-07-09T21:00:06.000000+00:00",
    )
    report.record_pr(
        number=42,
        url="https://github.com/glitchwerks/sandbox/pull/7",
        opened_at="2026-07-09T21:01:00.000000+00:00",
    )
    report.record_merge_gate(
        number=42,
        outcome="MERGED",
        merged_sha="deadbeef",
        ts="2026-07-09T21:02:00.000000+00:00",
    )
    report.record_escalation(
        number=42,
        kind="block",
        severity="warn",
        detail="Issue #42 parked: self-block.",
        ts="2026-07-09T21:02:30.000000+00:00",
    )
    report.set_exit_reason(
        "once_complete",
        ended_at="2026-07-09T21:03:12.000000+00:00",
    )

    d = report.to_dict()

    # Top-level schema keys (§ 3).
    assert set(d.keys()) == {
        "schema_version",
        "session",
        "startup",
        "totals",
        "issues",
        "ticks",
    }
    assert d["schema_version"] == 1

    session = d["session"]
    for key in (
        "started_at",
        "ended_at",
        "mode",
        "exit_reason",
        "poll_interval_s",
        "registry",
    ):
        assert key in session, f"session missing {key!r}"

    findings = d["startup"]["findings"]
    assert findings == [
        {
            "gate": "G2",
            "detail": "Prior daemon run ended ungracefully",
            "pids": None,
        },
        {
            "gate": "G1",
            "detail": "Orphan claude processes detected at startup",
            "pids": [12345],
        },
    ]

    [issue] = d["issues"]
    for key in (
        "number",
        "repo",
        "title",
        "picked_up_at",
        "label_transitions",
        "outcome",
        "park_reason",
        "park_kind",
        "pr",
        "merge_gate",
        "escalations",
    ):
        assert key in issue, f"issue record missing {key!r}"
    assert issue["number"] == 42
    assert issue["merge_gate"]["merged_sha"] == "deadbeef"
    assert issue["escalations"][0]["kind"] == "block"

    # Full round-trip through JSON — no non-serializable values (e.g. a
    # bare datetime or enum) leaked into the tree.
    json.dumps(d)


# ---------------------------------------------------------------------------
# Totals aggregation
# ---------------------------------------------------------------------------


def test_totals_aggregation_reflects_recorded_state() -> None:
    """Totals rolls up ticks/pickups/PRs/merges/parks/escalations/CI."""
    report = _new_report()
    report.begin_tick(started_at="T0")
    report.record_pickup(
        number=1,
        repo="glitchwerks/sandbox",
        title="clean",
        picked_up_at="T1",
    )
    report.record_pickup(
        number=2,
        repo="glitchwerks/sandbox",
        title="blocked",
        picked_up_at="T1",
    )
    report.record_pr(
        number=1,
        url="https://github.com/glitchwerks/sandbox/pull/1",
        opened_at="T2",
    )
    report.record_merge_gate(
        number=1, outcome="MERGED", merged_sha="abc", ts="T3"
    )
    report.record_merge_gate(
        number=2, outcome="CI_FAILED", merged_sha=None, ts="T3"
    )
    report.record_escalation(
        number=2,
        kind="block",
        severity="warn",
        detail="parked",
        ts="T3",
    )
    report.set_outcomes(
        merged_issues=[1],
        parked_reasons={2: "self-block: requirements ambiguous"},
    )
    report.end_tick(issues_processed=[1, 2], ended_at="T4")

    totals = report.to_dict()["totals"]
    assert totals["ticks"] == 1
    assert totals["issues_picked_up"] == 2
    assert totals["prs_opened"] == 1
    assert totals["issues_merged"] == 1
    assert totals["issues_parked"] == 1
    assert totals["escalations"] == 1
    assert totals["merge_gate_failures"] == 1


# ---------------------------------------------------------------------------
# Idempotent issue upsert by number
# ---------------------------------------------------------------------------


def test_record_pickup_upserts_by_number_without_duplicating() -> None:
    """Recording the same issue number twice updates in place, no dupes."""
    report = _new_report()

    report.record_pickup(
        number=42,
        repo="glitchwerks/sandbox",
        title="first title",
        picked_up_at="T1",
    )
    report.record_pickup(
        number=42,
        repo="glitchwerks/sandbox",
        title="second title",
        picked_up_at="T1",
    )

    issues = report.to_dict()["issues"]
    assert len(issues) == 1, (
        f"expected exactly one issue record for #42, got {len(issues)}"
    )
    assert issues[0]["number"] == 42
    assert issues[0]["title"] == "second title", (
        "second record_pickup call must update the existing record in place"
    )


# ---------------------------------------------------------------------------
# set_outcomes (B2 aggregation entry point)
# ---------------------------------------------------------------------------


def test_set_outcomes_folds_merged_and_parked_state_with_park_kind() -> None:
    """set_outcomes folds merged/parked state + derived park_kind (B2)."""
    report = _new_report()
    report.record_pickup(
        number=1,
        repo="glitchwerks/sandbox",
        title="clean",
        picked_up_at="T1",
    )
    report.record_pickup(
        number=2,
        repo="glitchwerks/sandbox",
        title="blocked",
        picked_up_at="T1",
    )
    report.record_pickup(
        number=3,
        repo="glitchwerks/sandbox",
        title="plain fail",
        picked_up_at="T1",
    )

    report.set_outcomes(
        merged_issues=[1],
        parked_reasons={
            2: "self-block: requirements ambiguous",
            3: "no PR created (agent may have failed)",
        },
    )

    by_number = {i["number"]: i for i in report.to_dict()["issues"]}
    assert by_number[1]["outcome"] == "merged"
    assert by_number[1]["park_reason"] is None
    assert by_number[1]["park_kind"] is None

    assert by_number[2]["outcome"] == "parked"
    assert by_number[2]["park_reason"] == "self-block: requirements ambiguous"
    assert by_number[2]["park_kind"] == "block"

    assert by_number[3]["outcome"] == "parked"
    assert (
        by_number[3]["park_reason"] == "no PR created (agent may have failed)"
    )
    assert by_number[3]["park_kind"] is None


def test_set_outcomes_upserts_issue_not_previously_recorded() -> None:
    """set_outcomes creates an IssueRecord for a number with no pickup."""
    report = _new_report()

    report.set_outcomes(merged_issues=[99], parked_reasons={})

    issues = report.to_dict()["issues"]
    assert len(issues) == 1
    assert issues[0]["number"] == 99
    assert issues[0]["outcome"] == "merged"


# ---------------------------------------------------------------------------
# Ordered label_transitions (§ 5.1 M12 — ordered, not set-based)
# ---------------------------------------------------------------------------


def test_label_transitions_preserve_insertion_order_per_issue() -> None:
    """label_transitions record daemon-attested edits in insertion order."""
    report = _new_report()
    report.record_pickup(
        number=7,
        repo="glitchwerks/sandbox",
        title="issue 7",
        picked_up_at="T0",
    )

    report.record_label_edit(
        number=7,
        added=["agent-in-progress"],
        removed=["agent-ready"],
        ts="T1",
    )
    report.record_label_edit(
        number=7,
        added=["agent-done"],
        removed=["agent-in-progress"],
        ts="T2",
    )
    report.record_label_edit(
        number=7,
        added=["agent-merged"],
        removed=["agent-done"],
        ts="T3",
    )

    [issue] = report.to_dict()["issues"]
    assert issue["label_transitions"] == [
        {
            "ts": "T1",
            "added": ["agent-in-progress"],
            "removed": ["agent-ready"],
        },
        {
            "ts": "T2",
            "added": ["agent-done"],
            "removed": ["agent-in-progress"],
        },
        {
            "ts": "T3",
            "added": ["agent-merged"],
            "removed": ["agent-done"],
        },
    ]


# ---------------------------------------------------------------------------
# begin_tick / end_tick
# ---------------------------------------------------------------------------


def test_begin_tick_end_tick_appends_ordered_tick_records() -> None:
    """begin_tick/end_tick bracket a poll cycle into an ordered TickRecord."""
    report = _new_report()

    report.begin_tick(started_at="T1")
    report.end_tick(issues_processed=[1, 2], ended_at="T2")
    report.begin_tick(started_at="T3")
    report.end_tick(issues_processed=[], error="boom", ended_at="T4")

    ticks = report.to_dict()["ticks"]
    assert ticks == [
        {
            "tick_index": 0,
            "started_at": "T1",
            "ended_at": "T2",
            "issues_processed": [1, 2],
            "error": None,
        },
        {
            "tick_index": 1,
            "started_at": "T3",
            "ended_at": "T4",
            "issues_processed": [],
            "error": "boom",
        },
    ]


# ---------------------------------------------------------------------------
# set_exit_reason
# ---------------------------------------------------------------------------


def test_set_exit_reason_records_reason_and_ended_at() -> None:
    """set_exit_reason stores the session exit reason and end timestamp."""
    report = _new_report()

    report.set_exit_reason("sigterm", ended_at="T9")

    session = report.to_dict()["session"]
    assert session["exit_reason"] == "sigterm"
    assert session["ended_at"] == "T9"


# ---------------------------------------------------------------------------
# write(path) — end-to-end
# ---------------------------------------------------------------------------


def test_write_end_to_end_creates_valid_json_matching_to_dict(
    tmp_path: Path,
) -> None:
    """write() serializes the report to disk as exactly to_dict()'s JSON."""
    report = _new_report()
    report.record_pickup(
        number=1,
        repo="glitchwerks/sandbox",
        title="clean",
        picked_up_at="T1",
    )
    report_path = tmp_path / "session-report.json"

    report.write(report_path)

    assert report_path.exists()
    on_disk = json.loads(report_path.read_text(encoding="utf-8"))
    assert on_disk == report.to_dict()


# ---------------------------------------------------------------------------
# write(path) — best-effort (M9): never raises
# ---------------------------------------------------------------------------


def test_write_to_missing_parent_directory_does_not_raise(
    tmp_path: Path,
) -> None:
    """write() to a path whose parent dir is absent swallows the failure."""
    report = _new_report()
    nested_path = tmp_path / "nested" / "deep" / "session-report.json"
    assert not nested_path.parent.exists()

    # Must not raise — mirrors runlog.py:117-142's never-raise contract.
    report.write(nested_path)


# ---------------------------------------------------------------------------
# write(path) — atomicity (M9): crash between temp-write and os.replace
# ---------------------------------------------------------------------------


def test_write_atomicity_prior_report_survives_a_failed_replace(
    tmp_path: Path,
) -> None:
    """A crash at the os.replace boundary leaves the prior file intact."""
    report_path = tmp_path / "session-report.json"

    first = _new_report()
    first.record_pickup(
        number=1,
        repo="glitchwerks/sandbox",
        title="first",
        picked_up_at="T1",
    )
    first.write(report_path)
    original_bytes = report_path.read_text(encoding="utf-8")
    assert json.loads(original_bytes)["issues"][0]["title"] == "first"

    second = _new_report()
    second.record_pickup(
        number=2,
        repo="glitchwerks/sandbox",
        title="second",
        picked_up_at="T2",
    )

    with patch.object(
        session_report_mod.os, "replace", side_effect=OSError("crash")
    ) as mock_replace:
        # Best-effort: must not raise even though the replace step fails.
        second.write(report_path)

    # The prior report's bytes are wholly unchanged — the crash never
    # reached the atomic swap, so the old file is still the live
    # artifact (M9: single-file oracle, never half-written).
    assert report_path.read_text(encoding="utf-8") == original_bytes
    assert "second" not in report_path.read_text(encoding="utf-8")

    # os.replace was attempted exactly once with the temp-then-target
    # shape ("<path>.tmp.<pid>" -> path), confirming a temp-write-then-
    # replace strategy rather than an in-place write.
    assert mock_replace.call_count == 1
    tmp_arg, dest_arg = mock_replace.call_args[0]
    assert str(dest_arg) == str(report_path)
    assert str(tmp_arg) == f"{report_path}.tmp.{os.getpid()}"
