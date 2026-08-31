"""Enforce repository pull-request metadata policy."""

from __future__ import annotations

import re
from collections.abc import Mapping

BRANCH_PATTERN = re.compile(
    r"^(?:feature|bug|docs|chore|refactor)/"
    r"(?P<issue>[1-9][0-9]*)-[a-z0-9]+(?:-[a-z0-9]+)*$"
)
CLOSING_PATTERN = re.compile(
    r"\b(?:closes|fixes|resolves)\s+"
    r"(?:(?P<repository>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?"
    r"#(?P<issue>[1-9][0-9]*)\b",
    re.IGNORECASE,
)


def parse_branch_issue(head_ref: str) -> int | None:
    """Extract the issue number from a valid source branch name.

    Args:
        head_ref: Pull-request source branch reference.

    Returns:
        The positive issue number, or ``None`` for an invalid branch name.
    """
    match = BRANCH_PATTERN.fullmatch(head_ref)
    return int(match.group("issue")) if match else None


def parse_closing_issues(body: str, repository: str) -> tuple[int, ...]:
    """Extract unique same-repository issues named by closing directives.

    Args:
        body: Pull-request body text.
        repository: Full owner/repository name for the current repository.

    Returns:
        Issue numbers in their first-appearance order.
    """
    numbers: list[int] = []
    for match in CLOSING_PATTERN.finditer(body):
        qualified_repo = match.group("repository")
        if (
            qualified_repo
            and qualified_repo.casefold() != repository.casefold()
        ):
            continue
        number = int(match.group("issue"))
        if number not in numbers:
            numbers.append(number)
    return tuple(numbers)


def evaluate_pr_policy(
    head_ref: str,
    body: str,
    repository: str,
    issue_has_milestone: Mapping[int, bool],
) -> list[str]:
    """Evaluate pull-request branch, closing, and milestone requirements.

    Args:
        head_ref: Pull-request source branch reference.
        body: Pull-request body text.
        repository: Full owner/repository name for the current repository.
        issue_has_milestone: Known milestone status keyed by issue number.

    Returns:
        Policy violations in deterministic evaluation order.
    """
    errors: list[str] = []
    branch_issue = parse_branch_issue(head_ref)
    closing_issues = parse_closing_issues(body, repository)
    if branch_issue is None:
        errors.append(f"invalid source branch: {head_ref}")
    if not closing_issues:
        errors.append("PR body has no closing directive for this repository")
    elif branch_issue is not None and branch_issue not in closing_issues:
        errors.append(
            f"branch issue #{branch_issue} is not closed by the PR body"
        )
    for issue_number in closing_issues:
        if issue_number not in issue_has_milestone:
            errors.append(
                f"unable to verify milestone for closing issue #{issue_number}"
            )
        elif not issue_has_milestone[issue_number]:
            errors.append(f"closing issue #{issue_number} has no milestone")
    return errors
