"""Tests for pull-request metadata policy evaluation."""

from __future__ import annotations

import json
import urllib.request as urllib_request
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError

import pytest

import baton_harness.pr_policy as pr_policy
from baton_harness.pr_policy import (
    PullRequestEvent,
    evaluate_pr_policy,
    fetch_issue_has_milestone,
    load_pull_request_event,
    main,
    parse_branch_issue,
    parse_closing_issues,
)

pytestmark = pytest.mark.fast


def _write_event(tmp_path: Path, *, body: str | None) -> Path:
    """Write a minimal GitHub pull-request event fixture.

    Args:
        tmp_path: Pytest-provided temporary directory.
        body: Pull-request body included in the event.

    Returns:
        Path to the JSON event fixture.
    """
    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps(
            {
                "repository": {"full_name": "glitchwerks/baton-harness"},
                "pull_request": {
                    "body": body,
                    "head": {"ref": "feature/365-workflow"},
                },
            }
        ),
        encoding="utf-8",
    )
    return event_path


def test_load_pull_request_event(tmp_path: Path) -> None:
    """Decode the repository, source branch, and body from an event."""
    event_path = _write_event(tmp_path, body="Closes #365")

    assert load_pull_request_event(event_path) == PullRequestEvent(
        head_ref="feature/365-workflow",
        body="Closes #365",
        repository="glitchwerks/baton-harness",
    )


def test_load_pull_request_event_normalizes_null_body(tmp_path: Path) -> None:
    """Normalize an absent GitHub pull-request body to empty text."""
    event_path = _write_event(tmp_path, body=None)

    assert load_pull_request_event(event_path).body == ""


def test_load_pull_request_event_rejects_malformed_json(
    tmp_path: Path,
) -> None:
    """Reject an event file whose JSON cannot be decoded."""
    event_path = tmp_path / "event.json"
    event_path.write_text("{", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        load_pull_request_event(event_path)


def test_main_rejects_invalid_utf8_event_without_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Translate undecodable event bytes to a token-safe runtime error."""
    token = "private-token"
    event_path = tmp_path / "event.json"
    event_path.write_bytes(b"\x80")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", token)

    assert main([]) == 1
    output = capsys.readouterr().err
    assert output == "PR policy: GitHub event read failed\n"
    assert token not in output


def test_fetch_issue_has_milestone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Create the required GET request and accept an object milestone."""
    observed: dict[str, object] = {}
    token = "test-token"

    def request_json(request: urllib_request.Request) -> dict[str, object]:
        """Capture token-safe request properties and return issue metadata."""
        observed.update(
            {
                "method": request.get_method(),
                "url": request.full_url,
                "accept": request.get_header("Accept"),
                "authorization_is_expected": request.get_header(
                    "Authorization"
                )
                == f"Bearer {token}",
                "api_version": request.get_header("X-github-api-version"),
            }
        )
        return {"milestone": {"number": 10}}

    monkeypatch.setattr(
        pr_policy,
        "_request_json",
        request_json,
    )

    assert fetch_issue_has_milestone(
        "glitchwerks/baton-harness", 365, token
    )
    assert observed == {
        "method": "GET",
        "url": "https://api.github.com/repos/glitchwerks/baton-harness/issues/365",
        "accept": "application/vnd.github+json",
        "authorization_is_expected": True,
        "api_version": "2022-11-28",
    }


def test_fetch_issue_has_no_milestone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat a null milestone in issue metadata as no milestone assignment."""
    monkeypatch.setattr(
        pr_policy,
        "_request_json",
        lambda request: {"milestone": None},
    )

    assert not fetch_issue_has_milestone(
        "glitchwerks/baton-harness", 365, "token"
    )


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        ({}, "GitHub issue metadata is missing milestone"),
        (
            {"milestone": "assigned"},
            "GitHub issue metadata has invalid milestone",
        ),
        ({"milestone": []}, "GitHub issue metadata has invalid milestone"),
        ({"milestone": True}, "GitHub issue metadata has invalid milestone"),
    ],
)
def test_fetch_issue_has_milestone_rejects_invalid_metadata(
    metadata: dict[str, object],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject missing and malformed milestone fields from GitHub metadata."""
    monkeypatch.setattr(pr_policy, "_request_json", lambda request: metadata)

    with pytest.raises(pr_policy.PolicyRuntimeError, match=message):
        fetch_issue_has_milestone(
            "glitchwerks/baton-harness", 365, "test-token"
        )


def test_main_rejects_invalid_utf8_api_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Translate undecodable API bytes to a token-safe runtime error."""
    class Response:
        """Minimal HTTP response that supplies undecodable bytes."""

        def __enter__(self) -> Response:
            """Enter the response context."""
            return self

        def __exit__(
            self,
            exc_type: object,
            exc_value: object,
            traceback: object,
        ) -> None:
            """Exit the response context."""

        def read(self) -> bytes:
            """Return bytes that cannot be decoded as UTF-8."""
            return b"\x80"

    token = "private-token"
    event_path = _write_event(tmp_path, body="Closes #365")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", token)
    monkeypatch.setattr(
        urllib_request,
        "urlopen",
        lambda *args, **kwargs: Response(),
    )

    assert main([]) == 1
    output = capsys.readouterr().err
    assert output == "PR policy: GitHub issue metadata request failed\n"
    assert token not in output


def test_main_rejects_non_object_api_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Report a concise error for issue metadata that is not an object."""

    class Response:
        """Minimal context-managed HTTP response fixture."""

        def __enter__(self) -> Response:
            """Enter the response context."""
            return self

        def __exit__(
            self,
            exc_type: object,
            exc_value: object,
            traceback: object,
        ) -> None:
            """Exit the response context."""

    event_path = _write_event(tmp_path, body="Closes #365")
    token = "private-token"
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", token)
    monkeypatch.setattr(
        urllib_request,
        "urlopen",
        lambda *args, **kwargs: Response(),
    )
    monkeypatch.setattr(json, "load", lambda response: [])

    assert main([]) == 1
    output = capsys.readouterr().err
    assert output == "PR policy: GitHub issue metadata was not a JSON object\n"
    assert token not in output


def test_main_hides_http_error_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Replace an HTTP failure with a token-safe runtime error."""
    token = "private-token"

    def raise_http_error(*args: object, **kwargs: object) -> object:
        """Raise the HTTP failure used by this boundary test."""
        del args, kwargs
        raise HTTPError(
            "https://example.test",
            401,
            "Bearer private-token",
            Message(),
            None,
        )

    monkeypatch.setattr(urllib_request, "urlopen", raise_http_error)
    event_path = _write_event(tmp_path, body="Closes #365")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", token)

    assert main([]) == 1
    output = capsys.readouterr().err
    assert output == "PR policy: GitHub issue metadata request failed\n"
    assert token not in output


def test_main_rejects_missing_event_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Report a concise error when the event path is absent."""
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    assert main([]) == 1
    assert capsys.readouterr().err == (
        "PR policy: required GitHub environment is missing\n"
    )


def test_main_rejects_missing_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Report a concise error when the GitHub token is absent."""
    monkeypatch.setenv(
        "GITHUB_EVENT_PATH",
        str(_write_event(tmp_path, body="")),
    )
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    assert main([]) == 1
    assert capsys.readouterr().err == (
        "PR policy: required GitHub environment is missing\n"
    )


def test_main_reports_malformed_event_without_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Report event decoding failures without disclosing the access token."""
    token = "private-token"
    event_path = tmp_path / "event.json"
    event_path.write_text("{", encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", token)

    assert main([]) == 1
    output = capsys.readouterr().err
    assert output.startswith("PR policy: ")
    assert token not in output


def test_main_prints_all_policy_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Print every policy violation produced from one pull-request event."""
    event_path = _write_event(tmp_path, body="Closes #366\nResolves #367")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(
        pr_policy,
        "fetch_issue_has_milestone",
        lambda repository, issue_number, token: False,
    )

    assert main([]) == 1
    assert capsys.readouterr().err == (
        "PR policy: branch issue #365 is not closed by the PR body\n"
        "PR policy: closing issue #366 has no milestone\n"
        "PR policy: closing issue #367 has no milestone\n"
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
