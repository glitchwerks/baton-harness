"""Match daemon session reports against declarative scenario expectations."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from baton_harness.scenario.expectations import EXPECTATIONS


@dataclass(frozen=True)
class AssertionResult:
    """Describe the result of one expectation check.

    Attributes:
        name: Stable identifier for the expectation check.
        passed: Whether the check passed.
        message: Human-readable check detail.
    """

    name: str
    passed: bool
    message: str


@dataclass(frozen=True)
class VerifyResult:
    """Describe all expectation checks for one scenario.

    Attributes:
        scenario: Scenario identifier supplied to the matcher.
        passed: Whether every assertion passed.
        assertions: Individual assertion results in evaluation order.
    """

    scenario: str
    passed: bool
    assertions: list[AssertionResult]


def _assertion(
    name: str,
    passed: bool,
    *,
    expected: object,
    actual: object,
) -> AssertionResult:
    """Build an assertion result with a consistent diagnostic message.

    Args:
        name: Stable assertion identifier.
        passed: Whether the assertion passed.
        expected: Expected value or condition.
        actual: Observed value.

    Returns:
        The populated assertion result.
    """
    return AssertionResult(
        name=name,
        passed=passed,
        message=f"expected {expected!r}; got {actual!r}",
    )


def _partial_matches(
    expected: Mapping[str, Any],
    actual: object,
) -> bool:
    """Return whether an actual mapping contains every expected pair.

    Args:
        expected: Partial mapping specification.
        actual: Candidate value to inspect.

    Returns:
        True when actual is a mapping containing every expected key and
        matching value.
    """
    if not isinstance(actual, Mapping):
        return False
    return all(
        key in actual and actual[key] == value
        for key, value in expected.items()
    )


def _is_ordered_subsequence(
    expected: list[dict[str, Any]],
    actual: list[object],
) -> bool:
    """Return whether partial specs occur in actual order.

    Args:
        expected: Ordered partial-match specifications.
        actual: Ordered candidate entries.

    Returns:
        True when every expected entry matches a later actual entry.
    """
    actual_index = 0
    for expected_entry in expected:
        while actual_index < len(actual):
            candidate = actual[actual_index]
            actual_index += 1
            if _partial_matches(expected_entry, candidate):
                break
        else:
            return False
    return True


def _match_issue(
    expectation: Mapping[str, Any],
    issue: Mapping[str, Any],
) -> list[AssertionResult]:
    """Match one issue record against its expectation.

    Args:
        expectation: Declarative issue checks.
        issue: Report issue record.

    Returns:
        Assertion results in expectation evaluation order.

    Raises:
        ValueError: If expectation contains an unrecognized key.
    """
    recognized_keys = {
        "escalations_include",
        "label_transitions",
        "merge_gate",
        "outcome",
        "outcome_not",
        "park_kind",
        "park_reason_present",
        "pr_present",
    }
    unknown = set(expectation) - recognized_keys
    if unknown:
        raise ValueError(
            f"unrecognized issue expectation key(s): {sorted(unknown)}"
        )

    assertions: list[AssertionResult] = []

    if "outcome" in expectation:
        expected_outcome = expectation["outcome"]
        actual_outcome = issue.get("outcome")
        passed = (
            actual_outcome in expected_outcome
            if isinstance(expected_outcome, list)
            else actual_outcome == expected_outcome
        )
        assertions.append(
            _assertion(
                "issue.outcome",
                passed,
                expected=expected_outcome,
                actual=actual_outcome,
            )
        )

    if "outcome_not" in expectation:
        forbidden_outcome = expectation["outcome_not"]
        actual_outcome = issue.get("outcome")
        passed = (
            actual_outcome not in forbidden_outcome
            if isinstance(forbidden_outcome, list)
            else actual_outcome != forbidden_outcome
        )
        assertions.append(
            _assertion(
                "issue.outcome_not",
                passed,
                expected=f"not {forbidden_outcome!r}",
                actual=actual_outcome,
            )
        )

    if "park_kind" in expectation:
        expected_park_kind = expectation["park_kind"]
        actual_park_kind = issue.get("park_kind")
        assertions.append(
            _assertion(
                "issue.park_kind",
                actual_park_kind == expected_park_kind,
                expected=expected_park_kind,
                actual=actual_park_kind,
            )
        )

    if "park_reason_present" in expectation:
        expected_presence = bool(expectation["park_reason_present"])
        actual_presence = bool(issue.get("park_reason"))
        assertions.append(
            _assertion(
                "issue.park_reason_present",
                actual_presence == expected_presence,
                expected=expected_presence,
                actual=actual_presence,
            )
        )

    if "pr_present" in expectation:
        expected_presence = bool(expectation["pr_present"])
        actual_presence = bool(issue.get("pr"))
        assertions.append(
            _assertion(
                "issue.pr_present",
                actual_presence == expected_presence,
                expected=expected_presence,
                actual=actual_presence,
            )
        )

    merge_gate_expectation = expectation.get("merge_gate")
    if isinstance(merge_gate_expectation, Mapping):
        merge_gate = issue.get("merge_gate")
        for key, expected_value in merge_gate_expectation.items():
            actual_value = (
                merge_gate.get(key)
                if isinstance(merge_gate, Mapping)
                else None
            )
            assertions.append(
                _assertion(
                    f"issue.merge_gate.{key}",
                    isinstance(merge_gate, Mapping)
                    and key in merge_gate
                    and actual_value == expected_value,
                    expected=expected_value,
                    actual=actual_value,
                )
            )

    escalation_expectations = expectation.get("escalations_include")
    if isinstance(escalation_expectations, list):
        escalations_value = issue.get("escalations")
        escalations = (
            escalations_value if isinstance(escalations_value, list) else []
        )
        for index, expected_entry in enumerate(escalation_expectations):
            passed = isinstance(expected_entry, Mapping) and any(
                _partial_matches(expected_entry, entry)
                for entry in escalations
            )
            assertions.append(
                _assertion(
                    f"issue.escalations_include[{index}]",
                    passed,
                    expected=expected_entry,
                    actual=escalations,
                )
            )

    transition_expectations = expectation.get("label_transitions")
    if isinstance(transition_expectations, list):
        transitions_value = issue.get("label_transitions")
        transitions = (
            transitions_value if isinstance(transitions_value, list) else []
        )
        typed_expectations = [
            cast(dict[str, Any], entry)
            for entry in transition_expectations
            if isinstance(entry, dict)
        ]
        passed = len(typed_expectations) == len(transition_expectations)
        passed = passed and _is_ordered_subsequence(
            typed_expectations,
            transitions,
        )
        assertions.append(
            _assertion(
                "issue.label_transitions",
                passed,
                expected=transition_expectations,
                actual=transitions,
            )
        )

    return assertions


def _match_startup(
    expectation: Mapping[str, Any],
    report: Mapping[str, Any],
) -> list[AssertionResult]:
    """Match startup findings against their expectation.

    Args:
        expectation: Declarative startup checks.
        report: Complete session report.

    Returns:
        One assertion per expected startup gate.

    Raises:
        ValueError: If expectation contains an unrecognized key.
    """
    recognized_keys = {"findings_include_gates"}
    unknown = set(expectation) - recognized_keys
    if unknown:
        raise ValueError(
            f"unrecognized startup expectation key(s): {sorted(unknown)}"
        )

    startup_value = report.get("startup")
    startup = startup_value if isinstance(startup_value, Mapping) else {}
    findings_value = startup.get("findings")
    findings = findings_value if isinstance(findings_value, list) else []
    gates = expectation.get("findings_include_gates")
    if not isinstance(gates, list):
        return []

    assertions: list[AssertionResult] = []
    for gate in gates:
        passed = any(
            isinstance(finding, Mapping) and finding.get("gate") == gate
            for finding in findings
        )
        assertions.append(
            _assertion(
                f"startup.findings_include_gates[{gate}]",
                passed,
                expected=f"finding with gate {gate!r}",
                actual=findings,
            )
        )
    return assertions


def match_report(
    expectation: Mapping[str, Any],
    report: Mapping[str, Any],
    *,
    scenario: str = "",
) -> VerifyResult:
    """Match a session report against a resolved expectation.

    Args:
        expectation: Declarative checks to apply.
        report: Session report matching ``SessionReport.to_dict()``.
        scenario: Scenario identifier copied to the result.

    Returns:
        The complete verification result.

    Raises:
        ValueError: If an expectation mapping contains an unrecognized key.
    """
    recognized_keys = {"issue", "issues_len", "startup"}
    unknown = set(expectation) - recognized_keys
    if unknown:
        raise ValueError(
            f"unrecognized top-level expectation key(s): {sorted(unknown)}"
        )

    assertions: list[AssertionResult] = []
    issues_value = report.get("issues")
    issues = issues_value if isinstance(issues_value, list) else []

    issue_precondition_passed = True
    if "issues_len" in expectation:
        expected_length = expectation["issues_len"]
        issue_precondition_passed = len(issues) == expected_length
        assertions.append(
            _assertion(
                "issues_len",
                issue_precondition_passed,
                expected=expected_length,
                actual=len(issues),
            )
        )

    issue_expectation = expectation.get("issue")
    if isinstance(issue_expectation, Mapping):
        issue_present = bool(issues) and isinstance(issues[0], Mapping)
        issue = issues[0] if issue_present else {}
        issue_assertions = _match_issue(issue_expectation, issue)
        if issue_precondition_passed:
            if issue_present:
                assertions.extend(issue_assertions)
            else:
                assertions.append(
                    AssertionResult(
                        name="issue.present",
                        passed=False,
                        message="expected an issue record; got none",
                    )
                )

    startup_expectation = expectation.get("startup")
    if isinstance(startup_expectation, Mapping):
        assertions.extend(_match_startup(startup_expectation, report))

    return VerifyResult(
        scenario=scenario,
        passed=all(assertion.passed for assertion in assertions),
        assertions=assertions,
    )


def verify_report(
    scenario_key: str,
    report: Mapping[str, Any],
) -> VerifyResult:
    """Verify a report against a named scenario expectation.

    Args:
        scenario_key: Key in ``EXPECTATIONS``.
        report: Session report matching ``SessionReport.to_dict()``.

    Returns:
        The complete verification result.

    Raises:
        KeyError: If scenario_key is unknown.
    """
    return match_report(
        EXPECTATIONS[scenario_key],
        report,
        scenario=scenario_key,
    )


def main(argv: list[str] | None = None) -> int:
    """Run the scenario-report verification CLI.

    Args:
        argv: Command-line arguments. Defaults to ``sys.argv[1:]``.

    Returns:
        Zero when every assertion passes, one when an assertion fails, or
        two when the report path, report JSON, or scenario key is invalid.

    Raises:
        ValueError: If the report JSON is valid but does not contain an
            object.
    """
    parser = argparse.ArgumentParser(
        prog="python -m baton_harness.scenario.verify",
        description="Verify a daemon session report against a scenario.",
    )
    parser.add_argument(
        "--scenario",
        required=True,
        metavar="KEY",
        help="Scenario expectation key.",
    )
    parser.add_argument(
        "--report",
        required=True,
        metavar="PATH",
        help="Path to a daemon session report JSON file.",
    )
    args = parser.parse_args(argv)

    try:
        report_data = json.loads(Path(args.report).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        print(f"error: report file not found: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(
            f"error: report file is not valid JSON: {exc}",
            file=sys.stderr,
        )
        return 2
    if not isinstance(report_data, dict):
        raise ValueError("session report JSON must contain an object")

    try:
        result = verify_report(args.scenario, report_data)
    except KeyError as exc:
        print(f"error: unknown scenario key: {exc}", file=sys.stderr)
        return 2
    for assertion in result.assertions:
        marker = "PASS" if assertion.passed else "FAIL"
        print(f"[{marker}] {assertion.name}: {assertion.message}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
