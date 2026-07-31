"""Unit tests for baton_harness.scenario (issue #306, Phase 5a of #243).

This file is the frozen contract for the implemented scenario-assertion
matcher in ``src/baton_harness/scenario/``, originally described in
``docs/superpowers/plans/2026-07-09-daemon-report-scenario-harness-243.md``
§ 5.1 / § 6 Phase 5a.

Call-surface decisions made here (no implementation existed to consult):

- ``EXPECTATIONS: dict[str, dict]`` in ``expectations.py`` — pure data,
  reproduced verbatim from the plan's § 5.1 code block.
- ``match_report(expectation, report, *, scenario="") -> VerifyResult``
  in ``verify.py`` — the generic matcher, taking an already-resolved
  expectation dict rather than a scenario key. This is the layer these
  tests exercise directly for matcher semantics (including
  ``label_transitions``, which no default scenario currently uses).
- ``verify_report(scenario_key, report) -> VerifyResult`` — the
  scenario-keyed convenience wrapper: looks up
  ``EXPECTATIONS[scenario_key]`` and delegates to ``match_report``.
  Raises ``KeyError`` for an unknown scenario key.
- ``VerifyResult`` — has ``scenario: str``, ``passed: bool``, and
  ``assertions: list[AssertionResult]``.
- ``AssertionResult`` — has ``name: str``, ``passed: bool``, and
  ``message: str``. ``name`` is a stable, dotted identifier for the
  specific expectation sub-check so a failure can be attributed to the
  exact assertion that produced it, not just "something failed":
    - ``"issues_len"``
    - ``"issue.present"`` — emitted when an issue expectation is present
      but the report has no usable issue record.
    - ``"issue.outcome"``
    - ``"issue.outcome_not"``
    - ``"issue.park_kind"``
    - ``"issue.park_reason_present"``
    - ``"issue.pr_present"``
    - ``"issue.merge_gate.outcome"``
    - ``"issue.escalations_include[<i>]"`` — one per expectation-list
      entry, ``<i>`` is that entry's 0-based index.
    - ``"issue.label_transitions"`` — one assertion for the whole
      ordered sub-sequence check (a sub-sequence match is a single
      pass/fail fact about the list, not decomposable per expected
      entry independent of the others' positions).
    - ``"startup.findings_include_gates[<GATE>]"`` — one per gate
      string in the expectation list.
- **issues_len / "issue" precondition (§ 5.1):** when an expectation
  includes both ``issues_len`` and ``issue``, ``issues_len`` is checked
  first as its own assertion. If it fails, the matcher does NOT fall
  back to silently picking ``issues[0]`` as the target — the ``issue.*``
  sub-assertions are omitted entirely from the result rather than
  produced against a target no precondition ever confirmed. This is the
  "fail loud/distinctly" reading of § 5.1.
- **``findings_include_gates`` reading (§ 5.1):** the plan's inline
  ``# and/or G1`` comment on the ``recovery`` scenario's single-element
  ``["G2"]`` list is a *fixture-design* note (the recovery scenario
  could equally well be satisfied by seeding a G1 finding instead of
  G2), not part of the machine-checked contract. The matcher's actual
  semantics are literal: every gate string listed in
  ``findings_include_gates`` must have a matching ``startup.findings``
  entry (all-of over the list) — for the shipped single-element
  ``["G2"]`` spec this trivially means "must include a G2 finding".
- CLI: ``main(argv) -> int`` in ``verify.py``, argv shaped as
  ``["--scenario", "<key>", "--report", "<path>"]``. Prints one
  ``"[PASS] <name>: <message>"`` / ``"[FAIL] <name>: <message>"`` line
  per assertion to stdout, returns 0 if all assertions passed, 1
  otherwise. Tested by calling ``main`` directly (matching this
  project's established convention in
  ``tests/chain/test_cli_report.py``), not via subprocess.

Coverage:
- All 5 default scenarios (``block-ambiguity``, ``clean-implement``,
  ``ci-fail``, ``terminal-block``, ``recovery``): one PASS fixture and
  at least one FAIL fixture per scenario, each FAIL fixture mutating
  exactly one field and asserting the failure is attributed to the one
  assertion that should have flipped, with every other assertion for
  that scenario still passing.
- ``issues_len`` mismatch (``terminal-block`` with 2 issues instead of
  1) produces a distinct, attributable ``"issues_len"`` failure with no
  ``issue.*`` assertions present.
- ``verify_report`` raises ``KeyError`` for an unknown scenario key.
- Unknown top-level, issue-level, and startup-level expectation keys raise
  ``ValueError`` instead of producing a vacuous pass.
- Scalar and list forms of ``outcome_not`` reject forbidden outcomes and
  accept outcomes outside the forbidden set.
- Ordered-subsequence ``label_transitions`` matching (§ 5.1 M12, tested
  via ``match_report`` directly since no default scenario uses this
  key): correct order passes, reversed order fails, a valid
  subsequence with gaps still passes.
- CLI exit-code contract: a PASS report exits 0 with exactly one
  ``[PASS]`` line per assertion (and no ``[FAIL]`` lines); a FAIL
  report exits 1 with at least one ``[FAIL]`` line, and every assertion
  name from the underlying VerifyResult appears in stdout. Missing or
  invalid report files and unknown scenario keys exit 2 with an error on
  stderr.
- The empty-``issues[]`` trap (plan § 4, B1): a scenario whose
  expectation guards ``issues_len`` (``terminal-block``) treats 0
  issues the same as any other ``issues_len`` mismatch — a distinct,
  attributable ``issues_len`` failure, no ``issue.*`` assertions, no
  exception. A scenario whose expectation has no ``issues_len`` guard
  (``clean-implement``) must not raise (e.g. an ``IndexError`` from
  blindly indexing ``issues[0]``) when given an empty issues list —
  it must fail cleanly instead.
- The ``python -m baton_harness.scenario.verify`` entry point itself
  (not just the importable ``main`` function) is exercised once via
  subprocess, guarding against an implementer who defines ``main()``
  but omits the ``if __name__ == "__main__":`` dispatch — a module
  that imports cleanly and does nothing would otherwise exit 0 and
  silently pass this check with no ``[FAIL]`` output.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from baton_harness.scenario.expectations import EXPECTATIONS
from baton_harness.scenario.verify import (
    AssertionResult,
    VerifyResult,
    main,
    match_report,
    verify_report,
)

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _report(
    issues: list[dict[str, Any]] | None = None,
    startup_findings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a minimal report dict matching SessionReport.to_dict()'s shape.

    Args:
        issues: Issue dicts to place under ``report["issues"]``.
        startup_findings: Finding dicts to place under
            ``report["startup"]["findings"]``.

    Returns:
        A plain dict with every top-level key SessionReport.to_dict()
        produces.
    """
    issues = issues or []
    return {
        "schema_version": 1,
        "session": {
            "started_at": "2026-07-30T00:00:00.000000+00:00",
            "ended_at": "2026-07-30T00:10:00.000000+00:00",
            "mode": "once",
            "exit_reason": "once_complete",
            "poll_interval_s": 30.0,
            "registry": [{"owner": "glitchwerks", "repo": "sandbox"}],
        },
        "startup": {"findings": startup_findings or []},
        "totals": {
            "ticks": 1,
            "issues_picked_up": len(issues),
            "prs_opened": 0,
            "issues_merged": 0,
            "issues_parked": 0,
            "escalations": 0,
            "merge_gate_failures": 0,
        },
        "issues": issues,
        "ticks": [],
    }


def _issue(
    number: int = 1,
    *,
    outcome: str | None = None,
    park_reason: str | None = None,
    park_kind: str | None = None,
    pr: dict[str, Any] | None = None,
    merge_gate: dict[str, Any] | None = None,
    escalations: list[dict[str, Any]] | None = None,
    label_transitions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a minimal issue dict matching SessionReport's issue schema.

    Args:
        number: GitHub issue number.
        outcome: Final issue outcome.
        park_reason: Human-readable park reason.
        park_kind: Derived parking category.
        pr: Pull-request record dict.
        merge_gate: Merge-gate result dict.
        escalations: Ordered escalation dicts.
        label_transitions: Ordered label-transition dicts.

    Returns:
        A plain dict with every key SessionReport's issue schema uses.
    """
    return {
        "number": number,
        "repo": "glitchwerks/sandbox",
        "title": "scenario fixture issue",
        "picked_up_at": "2026-07-30T00:00:05.000000+00:00",
        "label_transitions": label_transitions or [],
        "outcome": outcome,
        "park_reason": park_reason,
        "park_kind": park_kind,
        "pr": pr,
        "merge_gate": merge_gate,
        "escalations": escalations or [],
    }


def _label_transition(
    ts: str, added: list[str], removed: list[str]
) -> dict[str, Any]:
    """Build a single label-transition dict.

    Args:
        ts: Caller-supplied timestamp for the edit.
        added: Labels added by the edit.
        removed: Labels removed by the edit.

    Returns:
        A plain dict matching SessionReport's label_transition schema.
    """
    return {"ts": ts, "added": added, "removed": removed}


def _escalation(
    kind: str, severity: str = "warn", detail: str = "test escalation"
) -> dict[str, Any]:
    """Build a single escalation dict.

    Args:
        kind: Escalation category.
        severity: Escalation severity.
        detail: Human-readable escalation detail.

    Returns:
        A plain dict matching SessionReport's escalation schema.
    """
    return {"ts": "T2", "severity": severity, "kind": kind, "detail": detail}


def _names(result: VerifyResult) -> set[str]:
    """Return the set of assertion names present in a VerifyResult.

    Args:
        result: A VerifyResult returned by match_report/verify_report.

    Returns:
        The set of every AssertionResult.name in result.assertions.
    """
    return {a.name for a in result.assertions}


def _failing_names(result: VerifyResult) -> set[str]:
    """Return the set of assertion names that failed in a VerifyResult.

    Args:
        result: A VerifyResult returned by match_report/verify_report.

    Returns:
        The set of AssertionResult.name for every failed assertion.
    """
    return {a.name for a in result.assertions if not a.passed}


def _find(result: VerifyResult, name: str) -> AssertionResult:
    """Return the AssertionResult with the given name, or fail loudly.

    Args:
        result: A VerifyResult returned by match_report/verify_report.
        name: The assertion name to look up.

    Returns:
        The matching AssertionResult.
    """
    for assertion in result.assertions:
        if assertion.name == name:
            return assertion
    raise AssertionError(
        f"no assertion named {name!r} in result; got "
        f"{[a.name for a in result.assertions]!r}"
    )


# ---------------------------------------------------------------------------
# EXPECTATIONS is pure data matching the plan's § 5.1 spec verbatim
# ---------------------------------------------------------------------------


def test_expectations_has_all_five_scenario_keys() -> None:
    """EXPECTATIONS declares exactly the 5 scenarios from § 5.1."""
    assert set(EXPECTATIONS.keys()) == {
        "block-ambiguity",
        "clean-implement",
        "ci-fail",
        "terminal-block",
        "recovery",
    }


# ---------------------------------------------------------------------------
# block-ambiguity
# ---------------------------------------------------------------------------


def test_block_ambiguity_pass() -> None:
    """A daemon-attested self-block satisfies block-ambiguity."""
    report = _report(
        issues=[
            _issue(
                number=42,
                outcome="parked",
                park_reason="self-block: requirements ambiguous",
                park_kind="block",
                escalations=[_escalation(kind="block")],
            )
        ]
    )

    result = verify_report("block-ambiguity", report)

    assert result.passed is True
    assert result.scenario == "block-ambiguity"
    assert all(a.passed for a in result.assertions)
    assert len(result.assertions) > 0


def test_block_ambiguity_fail_park_kind_mismatch() -> None:
    """A non-block park_kind fails only the park_kind assertion."""
    report = _report(
        issues=[
            _issue(
                number=42,
                outcome="parked",
                park_reason="self-block: requirements ambiguous",
                park_kind=None,  # mutated: should be "block"
                escalations=[_escalation(kind="block")],
            )
        ]
    )

    result = verify_report("block-ambiguity", report)

    assert result.passed is False
    assert _failing_names(result) == {"issue.park_kind"}
    assert _find(result, "issue.outcome").passed is True
    assert _find(result, "issue.park_reason_present").passed is True
    assert _find(result, "issue.escalations_include[0]").passed is True


def test_block_ambiguity_fail_missing_block_escalation() -> None:
    """Missing a kind=block escalation fails only that assertion."""
    report = _report(
        issues=[
            _issue(
                number=42,
                outcome="parked",
                park_reason="self-block: requirements ambiguous",
                park_kind="block",
                escalations=[_escalation(kind="info")],  # mutated
            )
        ]
    )

    result = verify_report("block-ambiguity", report)

    assert result.passed is False
    assert _failing_names(result) == {"issue.escalations_include[0]"}
    assert _find(result, "issue.outcome").passed is True
    assert _find(result, "issue.park_kind").passed is True
    assert _find(result, "issue.park_reason_present").passed is True


# ---------------------------------------------------------------------------
# clean-implement
# ---------------------------------------------------------------------------


def test_clean_implement_pass_merged() -> None:
    """outcome=merged with a PR satisfies clean-implement."""
    report = _report(
        issues=[
            _issue(
                number=7,
                outcome="merged",
                pr={
                    "url": "https://github.com/glitchwerks/sandbox/pull/7",
                    "opened_at": "T2",
                },
            )
        ]
    )

    result = verify_report("clean-implement", report)

    assert result.passed is True
    assert all(a.passed for a in result.assertions)


def test_clean_implement_pass_pr_open() -> None:
    """outcome=pr_open (the other accepted value) also satisfies it."""
    report = _report(
        issues=[
            _issue(
                number=7,
                outcome="pr_open",
                pr={
                    "url": "https://github.com/glitchwerks/sandbox/pull/7",
                    "opened_at": "T2",
                },
            )
        ]
    )

    result = verify_report("clean-implement", report)

    assert result.passed is True
    assert all(a.passed for a in result.assertions)


def test_clean_implement_fail_outcome_not_in_accepted_list() -> None:
    """An outcome outside {merged, pr_open} fails only that assertion."""
    report = _report(
        issues=[
            _issue(
                number=7,
                outcome="parked",  # mutated: not in ["merged", "pr_open"]
                pr={
                    "url": "https://github.com/glitchwerks/sandbox/pull/7",
                    "opened_at": "T2",
                },
            )
        ]
    )

    result = verify_report("clean-implement", report)

    assert result.passed is False
    assert _failing_names(result) == {"issue.outcome"}
    assert _find(result, "issue.pr_present").passed is True


def test_clean_implement_fail_pr_not_present() -> None:
    """A null pr field fails only the pr_present assertion."""
    report = _report(
        issues=[_issue(number=7, outcome="merged", pr=None)]  # mutated
    )

    result = verify_report("clean-implement", report)

    assert result.passed is False
    assert _failing_names(result) == {"issue.pr_present"}
    assert _find(result, "issue.outcome").passed is True


def test_clean_implement_empty_issues_fails_without_raising() -> None:
    """An empty issues list fails cleanly, not with an IndexError.

    ``clean-implement``'s expectation has no ``issues_len`` guard (only
    ``terminal-block`` uses one), so this pins the other half of the
    empty-``issues[]`` trap (plan § 4, B1): a matcher that blindly reads
    ``report["issues"][0]`` would raise ``IndexError`` here instead of
    producing a ``[FAIL]`` result the CLI can report and exit non-zero
    for.
    """
    report = _report(issues=[])

    result = verify_report("clean-implement", report)

    assert result.passed is False
    assert _failing_names(result) == {"issue.present"}


# ---------------------------------------------------------------------------
# ci-fail
# ---------------------------------------------------------------------------


def test_ci_fail_pass() -> None:
    """A CI_FAILED merge gate on a non-merged issue satisfies ci-fail."""
    report = _report(
        issues=[
            _issue(
                number=9,
                outcome="parked",
                merge_gate={
                    "outcome": "CI_FAILED",
                    "merged_sha": None,
                    "ts": "T3",
                },
            )
        ]
    )

    result = verify_report("ci-fail", report)

    assert result.passed is True
    assert all(a.passed for a in result.assertions)


def test_ci_fail_fail_outcome_not_violated() -> None:
    """outcome=merged violates outcome_not, failing only that assertion."""
    report = _report(
        issues=[
            _issue(
                number=9,
                outcome="merged",  # mutated: outcome_not forbids "merged"
                merge_gate={
                    "outcome": "CI_FAILED",
                    "merged_sha": None,
                    "ts": "T3",
                },
            )
        ]
    )

    result = verify_report("ci-fail", report)

    assert result.passed is False
    assert _failing_names(result) == {"issue.outcome_not"}
    assert _find(result, "issue.merge_gate.outcome").passed is True


def test_outcome_not_list_fails_for_forbidden_outcome() -> None:
    """A list-form outcome_not fails when the actual outcome is listed."""
    expectation = {"issue": {"outcome_not": ["merged", "pr_open"]}}
    report = _report(issues=[_issue(outcome="pr_open")])

    result = match_report(expectation, report)

    assert result.passed is False
    assert _failing_names(result) == {"issue.outcome_not"}


def test_outcome_not_list_passes_for_allowed_outcome() -> None:
    """A list-form outcome_not passes when the actual outcome is absent."""
    expectation = {"issue": {"outcome_not": ["merged", "pr_open"]}}
    report = _report(issues=[_issue(outcome="parked")])

    result = match_report(expectation, report)

    assert result.passed is True
    assert _find(result, "issue.outcome_not").passed is True


def test_ci_fail_fail_merge_gate_outcome_mismatch() -> None:
    """A merge_gate.outcome other than CI_FAILED fails only that check."""
    report = _report(
        issues=[
            _issue(
                number=9,
                outcome="parked",
                merge_gate={
                    "outcome": "MERGED",  # mutated
                    "merged_sha": "deadbeef",
                    "ts": "T3",
                },
            )
        ]
    )

    result = verify_report("ci-fail", report)

    assert result.passed is False
    assert _failing_names(result) == {"issue.merge_gate.outcome"}
    assert _find(result, "issue.outcome_not").passed is True


def test_ci_fail_fail_merge_gate_missing() -> None:
    """A null merge_gate fails the merge_gate.outcome assertion."""
    report = _report(
        issues=[_issue(number=9, outcome="parked", merge_gate=None)]
    )

    result = verify_report("ci-fail", report)

    assert result.passed is False
    assert "issue.merge_gate.outcome" in _failing_names(result)


# ---------------------------------------------------------------------------
# terminal-block
# ---------------------------------------------------------------------------


def test_terminal_block_pass() -> None:
    """Exactly one skipped_blocked issue satisfies terminal-block."""
    report = _report(issues=[_issue(number=5, outcome="skipped_blocked")])

    result = verify_report("terminal-block", report)

    assert result.passed is True
    assert _find(result, "issues_len").passed is True
    assert _find(result, "issue.outcome").passed is True


def test_terminal_block_fail_issues_len_mismatch() -> None:
    """A 2-issue report fails issues_len distinctly (§4 empty-issues trap).

    Per § 5.1, when issues_len fails the matcher must not silently fall
    back to issues[0] — no issue.* assertions are produced at all.
    """
    report = _report(
        issues=[
            _issue(number=5, outcome="skipped_blocked"),
            _issue(number=6, outcome="skipped_blocked"),
        ]
    )

    result = verify_report("terminal-block", report)

    assert result.passed is False
    assert _failing_names(result) == {"issues_len"}
    assert not any(name.startswith("issue.") for name in _names(result)), (
        "issue.* assertions must be omitted, not silently run against "
        "issues[0], once the issues_len precondition fails"
    )


def test_terminal_block_fail_outcome_mismatch() -> None:
    """A non-skipped_blocked outcome fails only that assertion."""
    report = _report(issues=[_issue(number=5, outcome="parked")])

    result = verify_report("terminal-block", report)

    assert result.passed is False
    assert _failing_names(result) == {"issue.outcome"}
    assert _find(result, "issues_len").passed is True


def test_terminal_block_fail_empty_issues_list() -> None:
    """Zero issues fails issues_len the same as any other count mismatch.

    Plan § 4 names the empty-``issues[]`` case (a daemon skip that
    produces no issue record at all) as the trap ``terminal-block``
    exists to catch — this must not be treated differently from the
    2-issue mismatch above.
    """
    report = _report(issues=[])

    result = verify_report("terminal-block", report)

    assert result.passed is False
    assert _failing_names(result) == {"issues_len"}
    assert not any(name.startswith("issue.") for name in _names(result))


# ---------------------------------------------------------------------------
# recovery
# ---------------------------------------------------------------------------


def test_recovery_pass_g2_finding_present() -> None:
    """A G2 startup finding satisfies recovery."""
    report = _report(
        startup_findings=[
            {
                "gate": "G2",
                "detail": "Prior daemon run ended ungracefully",
                "pids": None,
            }
        ]
    )

    result = verify_report("recovery", report)

    assert result.passed is True
    assert all(a.passed for a in result.assertions)


def test_recovery_fail_no_matching_gate() -> None:
    """A findings list without a G2 entry fails findings_include_gates.

    Only a G1 finding is present here (mutated away from G2) — per this
    module's documented reading of § 5.1's ``["G2"]`` spec, every gate
    string listed must have a matching finding, so a G1-only list does
    not satisfy a ``["G2"]`` expectation.
    """
    report = _report(
        startup_findings=[
            {
                "gate": "G1",
                "detail": "Orphan claude processes detected at startup",
                "pids": [123],
            }
        ]
    )

    result = verify_report("recovery", report)

    assert result.passed is False
    assert _failing_names(result) == {"startup.findings_include_gates[G2]"}


def test_recovery_fail_empty_findings() -> None:
    """An empty findings list fails findings_include_gates."""
    report = _report(startup_findings=[])

    result = verify_report("recovery", report)

    assert result.passed is False
    assert "startup.findings_include_gates[G2]" in _failing_names(result)


# ---------------------------------------------------------------------------
# verify_report: unknown scenario key
# ---------------------------------------------------------------------------


def test_verify_report_unknown_scenario_raises_key_error() -> None:
    """An unknown scenario key raises KeyError, not a silent no-op."""
    report = _report(issues=[_issue(number=1, outcome="merged")])

    with pytest.raises(KeyError):
        verify_report("not-a-real-scenario", report)


@pytest.mark.parametrize(
    ("expectation", "level"),
    [
        ({"issues_lens": 1}, "top-level"),
        ({"issue": {"outcom": "merged"}}, "issue"),
        (
            {"startup": {"finding_include_gates": ["G2"]}},
            "startup",
        ),
    ],
)
def test_match_report_rejects_unrecognized_expectation_keys(
    expectation: dict[str, Any],
    level: str,
) -> None:
    """A typo at any expectation level raises a descriptive ValueError.

    Args:
        expectation: Standalone expectation containing one typo'd key.
        level: Expectation mapping level named in the expected error.

    Raises:
        AssertionError: If match_report does not reject the unknown key.
    """
    with pytest.raises(ValueError, match=f"unrecognized {level} expectation"):
        match_report(expectation, _report())


# ---------------------------------------------------------------------------
# match_report: ordered-subsequence label_transitions (§ 5.1 M12)
#
# No default scenario in EXPECTATIONS uses label_transitions, so these
# tests build a standalone expectation dict and call match_report
# directly rather than going through verify_report/EXPECTATIONS.
# ---------------------------------------------------------------------------


def test_label_transitions_correct_order_passes() -> None:
    """An expected sub-sequence found in the same relative order passes."""
    expectation = {
        "issue": {
            "label_transitions": [
                {"removed": ["agent-ready"]},
                {"removed": ["agent-in-progress"]},
            ],
        },
    }
    report = _report(
        issues=[
            _issue(
                number=7,
                label_transitions=[
                    _label_transition(
                        "T1", ["agent-in-progress"], ["agent-ready"]
                    ),
                    _label_transition(
                        "T2", ["agent-done"], ["agent-in-progress"]
                    ),
                ],
            )
        ]
    )

    result = match_report(expectation, report, scenario="label-order-ok")

    assert result.passed is True
    assert _find(result, "issue.label_transitions").passed is True


def test_label_transitions_reversed_order_fails() -> None:
    """The same two entries in reversed relative order fails to match.

    Actual removals occur agent-done-then-agent-in-progress; the
    expectation asks for agent-in-progress-then-agent-done. A
    set/membership check would wrongly pass both removals being
    present; the ordered sub-sequence check must not.
    """
    expectation = {
        "issue": {
            "label_transitions": [
                {"removed": ["agent-in-progress"]},
                {"removed": ["agent-done"]},
            ],
        },
    }
    report = _report(
        issues=[
            _issue(
                number=7,
                label_transitions=[
                    _label_transition("T1", [], ["agent-done"]),
                    _label_transition("T2", [], ["agent-in-progress"]),
                ],
            )
        ]
    )

    result = match_report(expectation, report, scenario="label-order-bad")

    assert result.passed is False
    assert _find(result, "issue.label_transitions").passed is False


def test_label_transitions_subsequence_with_gap_passes() -> None:
    """A valid sub-sequence with a non-matching entry in between passes.

    The sub-sequence match is not required to be contiguous.
    """
    expectation = {
        "issue": {
            "label_transitions": [
                {"removed": ["a"]},
                {"removed": ["c"]},
            ],
        },
    }
    report = _report(
        issues=[
            _issue(
                number=7,
                label_transitions=[
                    _label_transition("T1", [], ["a"]),
                    _label_transition("T2", [], ["b"]),
                    _label_transition("T3", [], ["c"]),
                ],
            )
        ]
    )

    result = match_report(expectation, report, scenario="label-order-gap")

    assert result.passed is True
    assert _find(result, "issue.label_transitions").passed is True


def test_label_transitions_partial_match_ignores_unlisted_keys() -> None:
    """A partial-match entry only checks the keys it names.

    The expectation entry names only ``added``; the actual transition
    also carries a ``removed`` list the expectation is silent on, which
    must not block the match.
    """
    expectation = {
        "issue": {
            "label_transitions": [{"added": ["agent-merged"]}],
        },
    }
    report = _report(
        issues=[
            _issue(
                number=7,
                label_transitions=[
                    _label_transition("T1", ["agent-merged"], ["agent-done"]),
                ],
            )
        ]
    )

    result = match_report(expectation, report, scenario="label-partial")

    assert result.passed is True


# ---------------------------------------------------------------------------
# CLI: main(argv) exit-code contract
# ---------------------------------------------------------------------------


def _write_report(tmp_path: Path, report: dict[str, Any]) -> Path:
    """Write a report dict to a scratch JSON file and return its path.

    Args:
        tmp_path: Pytest's per-test temporary directory.
        report: The report dict to serialize.

    Returns:
        The path the report was written to.
    """
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def test_main_exits_zero_and_prints_one_pass_line_per_assertion(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A matching report exits 0 and prints exactly one [PASS] per check."""
    report = _report(
        issues=[
            _issue(
                number=7,
                outcome="merged",
                pr={
                    "url": "https://github.com/glitchwerks/sandbox/pull/7",
                    "opened_at": "T2",
                },
            )
        ]
    )
    report_path = _write_report(tmp_path, report)
    expected = verify_report("clean-implement", report)

    exit_code = main(
        ["--scenario", "clean-implement", "--report", str(report_path)]
    )

    captured = capsys.readouterr()
    pass_lines = [
        line for line in captured.out.splitlines() if "[PASS]" in line
    ]
    assert exit_code == 0
    assert "[FAIL]" not in captured.out
    assert len(pass_lines) == len(expected.assertions) > 0, (
        "expected one [PASS] line per assertion "
        f"({len(expected.assertions)}), got {len(pass_lines)}: "
        f"{captured.out!r}"
    )
    for assertion in expected.assertions:
        assert assertion.name in captured.out, (
            f"assertion {assertion.name!r} not named anywhere in stdout: "
            f"{captured.out!r}"
        )


def test_main_exits_nonzero_and_prints_fail_marker_for_failed_assertion(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A mismatching report exits non-zero and names the failed check."""
    report = _report(
        issues=[_issue(number=7, outcome="merged", pr=None)]  # no PR
    )
    report_path = _write_report(tmp_path, report)

    exit_code = main(
        ["--scenario", "clean-implement", "--report", str(report_path)]
    )

    captured = capsys.readouterr()
    assert exit_code != 0
    assert "[FAIL]" in captured.out
    assert "issue.pr_present" in captured.out


def test_main_missing_report_returns_input_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing report path exits 2 and reports the error on stderr."""
    report_path = tmp_path / "missing-report.json"

    exit_code = main(
        ["--scenario", "clean-implement", "--report", str(report_path)]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "error: report file not found:" in captured.err
    assert captured.out == ""


def test_main_invalid_json_returns_input_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Invalid report JSON exits 2 and reports the error on stderr."""
    report_path = tmp_path / "invalid-report.json"
    report_path.write_text("not JSON", encoding="utf-8")

    exit_code = main(
        ["--scenario", "clean-implement", "--report", str(report_path)]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "error: report file is not valid JSON:" in captured.err
    assert captured.out == ""


def test_main_unknown_scenario_returns_input_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unknown scenario exits 2 and reports the error on stderr."""
    report_path = _write_report(tmp_path, _report())

    exit_code = main(
        ["--scenario", "not-a-real-scenario", "--report", str(report_path)]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "error: unknown scenario key:" in captured.err
    assert captured.out == ""


# ---------------------------------------------------------------------------
# CLI: the actual "python -m baton_harness.scenario.verify" entry point
#
# A subprocess test, not just a main(argv) call, because an implementer
# who defines main() but forgets the `if __name__ == "__main__":`
# dispatch produces a module that imports cleanly and does nothing --
# `python -m` would exit 0 with no output, silently passing a
# main()-only test suite while bin/verify-scenarios.sh (Phase 5a-ci)
# stays green forever on a no-op check.
# ---------------------------------------------------------------------------


def test_python_dash_m_entry_point_reports_fail_and_exits_nonzero(
    tmp_path: Path,
) -> None:
    """``python -m baton_harness.scenario.verify`` runs and reports FAIL.

    Uses a FAIL report specifically: a missing/broken module also exits
    non-zero, so asserting on the exit code alone would pass vacuously
    even before the module exists. Asserting ``[FAIL]`` appears in
    stdout requires the entry point to have actually run the matcher
    and printed its result.
    """
    report = _report(
        issues=[_issue(number=7, outcome="merged", pr=None)]  # no PR
    )
    report_path = _write_report(tmp_path, report)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "baton_harness.scenario.verify",
            "--scenario",
            "clean-implement",
            "--report",
            str(report_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode != 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "[FAIL]" in result.stdout, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
