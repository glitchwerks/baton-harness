"""Tests for pull-request metadata policy evaluation."""

from __future__ import annotations

import pytest

from baton_harness.pr_policy import (
    evaluate_pr_policy,
    parse_branch_issue,
    parse_closing_issues,
)


@pytest.mark.parametrize(
    ("branch", "expected"),
    [
        ("feature/365-repository-workflow-standards", 365),
        ("bug/7-auth-check", 7),
        ("docs/9-readme", 9),
        ("feature/no-issue", None),
        ("feature/365_Bad", None),
        ("feat/365-short", None),
    ],
)
def test_parse_branch_issue(branch: str, expected: int | None) -> None:
    """Parse only valid repository workflow branch names."""
    assert parse_branch_issue(branch) == expected


def test_parse_closing_issues_accepts_local_and_qualified_references() -> None:
    """Parse local and same-repository qualified closing references."""
    body = "Closes #365\nFixes glitchwerks/baton-harness#366"
    assert parse_closing_issues(
        body, "glitchwerks/baton-harness"
    ) == (365, 366)


def test_parse_closing_issues_ignores_plain_and_foreign_references() -> None:
    """Ignore references without directives and foreign repositories."""
    body = "Related: #365\nCloses another/repository#99"
    assert parse_closing_issues(body, "glitchwerks/baton-harness") == ()


@pytest.mark.parametrize(
    ("head", "body", "milestones", "message"),
    [
        ("topic/365-bad", "Closes #365", {365: True}, "invalid source branch"),
        ("feature/365-good", "Related #365", {}, "closing directive"),
        ("feature/365-good", "Closes #366", {366: True}, "branch issue #365"),
        (
            "feature/365-good",
            "Closes #365",
            {365: False},
            "issue #365 has no milestone",
        ),
    ],
)
def test_evaluate_pr_policy_reports_each_violation(
    head: str,
    body: str,
    milestones: dict[int, bool],
    message: str,
) -> None:
    """Report each independent pull-request policy violation."""
    assert any(
        message in error
        for error in evaluate_pr_policy(
            head, body, "glitchwerks/baton-harness", milestones
        )
    )


def test_evaluate_pr_policy_reports_all_missing_milestones() -> None:
    """Report missing milestones for every closing issue in order."""
    errors = evaluate_pr_policy(
        "feature/365-good",
        "Closes #365\nResolves #366",
        "glitchwerks/baton-harness",
        {365: False, 366: False},
    )
    assert errors == [
        "closing issue #365 has no milestone",
        "closing issue #366 has no milestone",
    ]


def test_evaluate_pr_policy_reports_unverified_milestone_for_absent_issue(
) -> None:
    """Distinguish an absent milestone result from a confirmed absence."""
    errors = evaluate_pr_policy(
        "feature/365-good",
        "Closes #365",
        "glitchwerks/baton-harness",
        {},
    )
    assert errors == ["unable to verify milestone for closing issue #365"]


def test_evaluate_pr_policy_orders_invalid_branch_before_missing_directive(
) -> None:
    """Report invalid branch before a missing closing directive."""
    errors = evaluate_pr_policy(
        "topic/365-bad",
        "Related #365",
        "glitchwerks/baton-harness",
        {},
    )
    assert errors == [
        "invalid source branch: topic/365-bad",
        "PR body has no closing directive for this repository",
    ]


def test_evaluate_pr_policy_orders_mismatch_before_milestone_errors() -> None:
    """Report branch mismatch before missing milestones in issue order."""
    errors = evaluate_pr_policy(
        "feature/365-good",
        "Closes #366\nResolves #367",
        "glitchwerks/baton-harness",
        {366: False, 367: False},
    )
    assert errors == [
        "branch issue #365 is not closed by the PR body",
        "closing issue #366 has no milestone",
        "closing issue #367 has no milestone",
    ]
