"""Slice 3b — three sources of truth for required-check names MUST agree.

If they drift, the merge boundary breaks silently:
- merge.py:REQUIRED_CHECKS — what the daemon waits for before merging.
- ci.yml top-level job names — what GitHub Actions actually reports.
- ruleset.main.json required_status_checks — what GitHub enforces on
  PRs to main.

This test reads all three and asserts the sets are exactly equal.  The
ci.yml parse uses yaml.safe_load and walks data["jobs"].values() to
avoid the step-level ``name:`` false-positives a flat regex would catch
(C2 fix from the task-8 plan).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from codereeve.chain.merge import REQUIRED_CHECKS

HARNESS = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.fast


# ---------------------------------------------------------------------------
# Source extractors
# ---------------------------------------------------------------------------


def _ci_yml_top_level_job_names() -> set[str]:
    """Extract top-level job names from .github/workflows/ci.yml.

    Walks ``data["jobs"].values()`` and yields the ``name`` field of each
    job — pinned to top-level jobs only so a step-level ``name:`` cannot
    sneak into the set.

    Returns:
        Set of top-level job display names as strings.

    Raises:
        FileNotFoundError: If ci.yml does not exist at the expected path.
        yaml.YAMLError: If the file cannot be parsed as valid YAML.
        KeyError: If the parsed YAML has no ``jobs`` key.
    """
    ci_path = HARNESS / ".github" / "workflows" / "ci.yml"
    text = ci_path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    return {
        job["name"]
        for job in data.get("jobs", {}).values()
        if isinstance(job, dict) and "name" in job
    }


def _ruleset_required_checks() -> set[str]:
    """Extract required-status-check context values from ruleset.main.json.

    Locates the ``required_status_checks`` rule by type and returns the
    ``context`` field of each entry under its ``parameters``.

    Returns:
        Set of check context strings, or an empty set if the rule type
        is absent (which will trigger an equality failure in the test).

    Raises:
        FileNotFoundError: If ruleset.main.json does not exist.
        json.JSONDecodeError: If the file cannot be parsed as JSON.
    """
    ruleset_path = HARNESS / "config" / "ruleset.main.json"
    payload = json.loads(ruleset_path.read_text(encoding="utf-8"))
    for rule in payload["rules"]:
        if rule["type"] == "required_status_checks":
            return {
                c["context"]
                for c in rule["parameters"]["required_status_checks"]
            }
    return set()


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_required_checks_agree_across_sources() -> None:
    """All three check-name sources carry exactly the same set.

    Compares:
    - merge.py ``REQUIRED_CHECKS`` (the daemon's wait list)
    - ``.github/workflows/ci.yml`` top-level job ``name`` values
      (parsed with yaml.safe_load; step-level names are excluded)
    - ``config/ruleset.main.json`` ``required_status_checks[].context``
      values (the GitHub-enforced branch-protection gate)

    A mismatch here means the daemon waits for a check that CI never
    produces, or GitHub blocks a PR for a check the daemon ignores —
    either case is a silent merge-boundary break.
    """
    merge_set = set(REQUIRED_CHECKS)
    ci_set = _ci_yml_top_level_job_names()
    ruleset_set = _ruleset_required_checks()

    assert merge_set == ci_set, (
        f"merge.py REQUIRED_CHECKS {sorted(merge_set)!r} differs from "
        f"ci.yml top-level job names {sorted(ci_set)!r}"
    )
    assert merge_set == ruleset_set, (
        f"merge.py REQUIRED_CHECKS {sorted(merge_set)!r} differs from "
        f"ruleset.main.json required_status_checks {sorted(ruleset_set)!r}"
    )


def test_pytest_job_runs_foundation_verifier() -> None:
    """Removing the frozen-install step from merge-blocking CI is detected."""
    ci_path = HARNESS / ".github" / "workflows" / "ci.yml"
    workflow = yaml.safe_load(ci_path.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["test"]["steps"]
    verifier_steps = [
        step
        for step in steps
        if step.get("name") == "Verify frozen wheel foundation"
    ]

    assert verifier_steps == [
        {
            "name": "Verify frozen wheel foundation",
            "run": ".venv/bin/bh-verify-foundation",
        }
    ]


def test_composite_setup_selects_python_floor_explicitly() -> None:
    """The locked CI environment cannot bind to an ambient interpreter."""
    action_path = HARNESS / ".github" / "actions" / "setup" / "action.yml"
    action = yaml.safe_load(action_path.read_text(encoding="utf-8"))
    steps = action["runs"]["steps"]

    sync_step = next(
        step
        for step in steps
        if step.get("name") == "Install package and dev dependencies"
    )
    assert sync_step["run"] == "uv sync --python 3.10 --locked --extra dev"
    assert sync_step.get("env") == {"BH_BUILD_DEVELOPMENT": "1"}
    expected_version_check = (
        ".venv/bin/python -c 'import sys; assert "
        "sys.version_info[:2] == (3, 10), sys.version'"
    )
    assert any(step.get("run") == expected_version_check for step in steps)


def test_host_setup_opts_into_editable_development_identity() -> None:
    """Host setup must opt into the build hook's editable identity."""
    script = (HARNESS / "bin/setup-env.sh").read_text(encoding="utf-8")
    sync_lines = [line for line in script.splitlines() if "uv sync --" in line]
    assert sync_lines == [
        'CODEREEVE_BUILD_DEVELOPMENT=1 uv sync --project "${CODEREEVE_ROOT}" '
        "--locked --extra dev"
    ]
