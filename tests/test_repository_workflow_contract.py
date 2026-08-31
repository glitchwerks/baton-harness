"""Structural contract for the pull request policy workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

HARNESS = Path(__file__).resolve().parents[1]
ISSUE_TEMPLATE_DIRECTORY = ".github/ISSUE_TEMPLATE"
FAST_MARKER_DESCRIPTION = (
    "fast: branch-level smoke, CLI, policy, and workflow contract tests"
)
SUPPORTED_BRANCHES = [
    "feature/**",
    "bug/**",
    "docs/**",
    "chore/**",
    "refactor/**",
]
SECURITY_ADVISORY_URL = (
    "https://github.com/glitchwerks/baton-harness/security/advisories/new"
)
EXPECTED_FORM_FIELDS: dict[str, list[tuple[str, str, str]]] = {
    "bug.yml": [
        ("problem", "textarea", "true"),
        ("reproduction", "textarea", "true"),
        ("expected", "textarea", "true"),
        ("context", "textarea", "false"),
        ("acceptance-criteria", "textarea", "true"),
    ],
    "feature.yml": [
        ("problem", "textarea", "true"),
        ("outcome", "textarea", "true"),
        ("alternatives", "textarea", "false"),
        ("acceptance-criteria", "textarea", "true"),
    ],
    "work-item.yml": [
        ("work", "textarea", "true"),
        ("outcome", "textarea", "true"),
        ("context", "textarea", "false"),
        ("acceptance-criteria", "textarea", "true"),
    ],
}
EXPECTED_FORM_LABELS: dict[str, list[str]] = {
    "bug.yml": ["bug"],
    "feature.yml": ["enhancement"],
    "work-item.yml": [],
}
pytestmark = pytest.mark.fast


def _load_yaml(relative_path: str) -> dict[str, Any]:
    """Load a workflow YAML file without YAML 1.1 key coercion.

    Args:
        relative_path: Path to the YAML file relative to the repository root.

    Returns:
        Parsed workflow mapping with scalar values preserved as strings.
    """
    path = HARNESS / relative_path
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _form_fields(form: dict[str, Any]) -> list[dict[str, Any]]:
    """Return form body entries that accept user input.

    Args:
        form: Parsed GitHub issue form.

    Returns:
        The input-bearing form fields.
    """
    return [field for field in form["body"] if "id" in field]


def _assert_issue_form_contract(
    filename: str,
    expected_labels: list[str],
) -> dict[str, Any]:
    """Assert the structural contract shared by all issue forms.

    Args:
        filename: Issue-form filename in the template directory.
        expected_labels: Labels GitHub should apply to created issues.
    """
    form = _load_yaml(f"{ISSUE_TEMPLATE_DIRECTORY}/{filename}")

    assert all(form[key] for key in ("name", "description", "title", "body"))
    assert form["labels"] == expected_labels

    fields = _form_fields(form)
    field_ids = [field["id"] for field in fields]
    assert len(field_ids) == len(set(field_ids))
    assert [
        (field["id"], field["type"], field["validations"]["required"])
        for field in fields
    ] == EXPECTED_FORM_FIELDS[filename]

    acceptance_criteria = next(
        field for field in fields if field["id"] == "acceptance-criteria"
    )
    assert acceptance_criteria["type"] == "textarea"
    assert acceptance_criteria["validations"]["required"] == "true"
    description = acceptance_criteria["attributes"]["description"].lower()
    assert "before merge" in description
    assert "do not include post-merge" in description
    return form


def test_issue_form_contract() -> None:
    """Issue forms expose the labels and acceptance-criteria boundary."""
    forms = [
        _assert_issue_form_contract(filename, labels)
        for filename, labels in EXPECTED_FORM_LABELS.items()
    ]
    for metadata_field in ("name", "description", "title"):
        values = [form[metadata_field] for form in forms]
        assert len(values) == len(set(values))


def test_issue_template_config_contract() -> None:
    """Issue-template configuration routes security reports without blanks."""
    config = _load_yaml(f"{ISSUE_TEMPLATE_DIRECTORY}/config.yml")
    assert config["blank_issues_enabled"] == "false"
    assert any(
        link.get("url") == SECURITY_ADVISORY_URL
        for link in config["contact_links"]
    )


def test_pull_request_template_contract() -> None:
    """Default PR template contains the required sections and fields."""
    template = (HARNESS / ".github/PULL_REQUEST_TEMPLATE.md").read_text(
        encoding="utf-8"
    )

    for required_value in (
        "Closes #",
        "## Tests",
        "## Documentation",
        "## Review decision",
        "needs-review",
        "post-merge",
    ):
        assert required_value in template


def test_coderabbit_is_label_opt_in_only() -> None:
    """CodeRabbit reviews are advisory and opt in through one PR label."""
    config = _load_yaml(".coderabbit.yaml")
    auto_review = config["reviews"]["auto_review"]
    assert auto_review["enabled"] == "false"
    assert auto_review["labels"] == ["needs-review"]


def test_fast_marker_registration(pytestconfig: pytest.Config) -> None:
    """The branch-fast marker has one exact registered description."""
    fast_markers = [
        marker
        for marker in pytestconfig.getini("markers")
        if marker.startswith("fast:")
    ]
    assert fast_markers == [FAST_MARKER_DESCRIPTION]


def test_fast_validation_workflow_contract() -> None:
    """Fast validation runs only the branch-level checks on work pushes."""
    workflow = _load_yaml(".github/workflows/fast-validation.yml")
    assert workflow["on"]["push"]["branches"] == SUPPORTED_BRANCHES
    assert set(workflow["on"]) == {"push"}
    job = workflow["jobs"]["fast"]
    assert job["name"] == "Fast validation"
    assert job["permissions"] == {"contents": "read"}
    commands = [step["run"] for step in job["steps"] if "run" in step]
    assert commands == [
        ".venv/bin/python -m ruff check .",
        ".venv/bin/python -m ruff format --check .",
        "shellcheck bin/*.sh bin/lib/*.sh",
        ".venv/bin/python -m pytest -m fast",
    ]


def test_full_ci_only_targets_main_pull_requests() -> None:
    """Full CI runs only for pull requests targeting main."""
    workflow = _load_yaml(".github/workflows/ci.yml")
    assert workflow["on"] == {"pull_request": {"branches": ["main"]}}


def test_pr_policy_workflow_contract() -> None:
    """PR policy workflow exposes the required trigger and check contract."""
    workflow = _load_yaml(".github/workflows/pr-policy.yml")
    assert workflow["on"]["pull_request"]["branches"] == ["main"]
    assert workflow["on"]["pull_request"]["types"] == [
        "opened",
        "synchronize",
        "reopened",
        "edited",
    ]
    assert workflow["concurrency"] == {
        "group": "pr-policy-${{ github.event.pull_request.number }}",
        "cancel-in-progress": "true",
    }
    job = workflow["jobs"]["policy"]
    assert job["permissions"] == {
        "contents": "read",
        "issues": "read",
        "pull-requests": "read",
    }
    assert job["name"] == "PR policy"
    assert job["steps"] == [
        {"uses": "actions/checkout@v4"},
        {"uses": "./.github/actions/setup"},
        {
            "name": "Validate pull request policy",
            "env": {"GITHUB_TOKEN": "${{ secrets.GITHUB_TOKEN }}"},
            "run": ".venv/bin/python -m baton_harness.pr_policy",
        },
    ]
