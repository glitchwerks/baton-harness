"""Enforce repository pull-request metadata policy."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request as urllib_request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

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


@dataclass(frozen=True)
class PullRequestEvent:
    """The PR metadata required to evaluate repository policy.

    Attributes:
        head_ref: Source branch name of the pull request.
        body: Pull-request body, normalized to an empty string when absent.
        repository: Full owner/repository name for the event repository.
    """

    head_ref: str
    body: str
    repository: str


class PolicyRuntimeError(RuntimeError):
    """Raised when live PR policy input cannot be read safely."""


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


def _request_json(request: urllib_request.Request) -> dict[str, object]:
    """Request and validate GitHub issue metadata as a JSON object.

    Args:
        request: Fully configured GitHub API request.

    Returns:
        The decoded issue metadata with string keys.

    Raises:
        PolicyRuntimeError: If GitHub cannot return a JSON object safely.
    """
    try:
        with urllib_request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyRuntimeError(
            "GitHub issue metadata request failed"
        ) from exc
    if not isinstance(payload, dict):
        raise PolicyRuntimeError("GitHub issue metadata was not a JSON object")
    return {str(key): value for key, value in payload.items()}


def fetch_issue_has_milestone(
    repository: str, issue_number: int, token: str
) -> bool:
    """Return whether a GitHub issue is assigned to a milestone.

    Args:
        repository: Full owner/repository name containing the issue.
        issue_number: Positive GitHub issue number to inspect.
        token: GitHub token used only as an API authorization header.

    Returns:
        ``True`` when the issue metadata contains a milestone object.

    Raises:
        PolicyRuntimeError: If GitHub issue metadata cannot be read safely.
    """
    request = urllib_request.Request(
        f"https://api.github.com/repos/{repository}/issues/{issue_number}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    return _request_json(request).get("milestone") is not None


def load_pull_request_event(path: Path) -> PullRequestEvent:
    """Load validated pull-request metadata from a GitHub event file.

    Args:
        path: Path to the GitHub Actions event JSON file.

    Returns:
        Normalized pull-request metadata required by policy evaluation.

    Raises:
        PolicyRuntimeError: If required pull-request fields are invalid.
        json.JSONDecodeError: If the event file is not valid JSON.
        OSError: If the event file cannot be read.
    """
    payload: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise PolicyRuntimeError("GitHub event was not a JSON object")
    repository_payload = payload.get("repository")
    pull_request_payload = payload.get("pull_request")
    if not isinstance(repository_payload, dict) or not isinstance(
        pull_request_payload, dict
    ):
        raise PolicyRuntimeError("GitHub event is missing PR metadata")
    head_payload = pull_request_payload.get("head")
    repository = repository_payload.get("full_name")
    body = pull_request_payload.get("body")
    if not isinstance(head_payload, dict) or not isinstance(repository, str):
        raise PolicyRuntimeError("GitHub event has invalid PR metadata")
    head_ref = head_payload.get("ref")
    if not isinstance(head_ref, str) or not (
        body is None or isinstance(body, str)
    ):
        raise PolicyRuntimeError("GitHub event has invalid PR fields")
    return PullRequestEvent(head_ref, body or "", repository)


def main(argv: Sequence[str] | None = None) -> int:
    """Evaluate live pull-request policy from GitHub Actions environment.

    Args:
        argv: Reserved command-line arguments; no arguments are accepted.

    Returns:
        Zero when policy passes, otherwise one.
    """
    del argv
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    token = os.environ.get("GITHUB_TOKEN")
    if not event_path or not token:
        print(
            "PR policy: required GitHub environment is missing",
            file=sys.stderr,
        )
        return 1
    try:
        event = load_pull_request_event(Path(event_path))
        closing_issues = parse_closing_issues(event.body, event.repository)
        milestones = {
            number: fetch_issue_has_milestone(event.repository, number, token)
            for number in closing_issues
        }
        errors = evaluate_pr_policy(
            event.head_ref,
            event.body,
            event.repository,
            milestones,
        )
    except (OSError, json.JSONDecodeError, PolicyRuntimeError) as exc:
        print(f"PR policy: {exc}", file=sys.stderr)
        return 1
    for error in errors:
        print(f"PR policy: {error}", file=sys.stderr)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
