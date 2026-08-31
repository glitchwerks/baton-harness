"""Structural contract for the pull request policy workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

HARNESS = Path(__file__).resolve().parents[1]


def _load_yaml(relative_path: str) -> dict[str, Any]:
    """Load a workflow YAML file without YAML 1.1 key coercion.

    Args:
        relative_path: Path to the YAML file relative to the repository root.

    Returns:
        Parsed workflow mapping with scalar values preserved as strings.
    """
    path = HARNESS / relative_path
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


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
