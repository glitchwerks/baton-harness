"""Structural contract for the pull request policy workflow."""

from __future__ import annotations

import re
import subprocess
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
    expected_forms = set(EXPECTED_FORM_LABELS)
    assert set(EXPECTED_FORM_FIELDS) == expected_forms

    template_directory = HARNESS / ISSUE_TEMPLATE_DIRECTORY
    actual_forms = {
        path.name
        for path in template_directory.glob("*.yml")
        if path.name != "config.yml"
    }
    assert actual_forms == expected_forms

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


def test_agent_policy_files_are_byte_identical() -> None:
    """AGENTS.md and CLAUDE.md provide the same harness policy bytes."""
    assert (HARNESS / "AGENTS.md").read_bytes() == (
        HARNESS / "CLAUDE.md"
    ).read_bytes()


def _assert_no_legacy_cli_invocations(text: str) -> None:
    """Reject legacy CLI invocations outside their compatibility table."""
    compatibility_prefix, compatibility_and_after = text.split(
        "The six legacy scripts remain temporary compatibility shims", 1
    )
    _, compatibility_suffix = compatibility_and_after.split(
        "The hooks derive the issue number", 1
    )
    outside_compatibility = (
        compatibility_prefix
        + "The hooks derive the issue number"
        + compatibility_suffix
    )
    replacements = {
        "bh-daemon": "codereeve daemon",
        "bh-after-create": "codereeve hook after-create",
        "bh-before-run": "codereeve hook before-run",
        "bh-after-run": "codereeve hook after-run",
        "bh-force-pr-not-merge": "codereeve hook force-pr-not-merge",
        "bh-verify-foundation": "codereeve verify",
    }
    fenced_examples = re.findall(
        r"```[^\n]*\n(.*?)```", outside_compatibility, re.DOTALL
    )
    legacy_pattern = "|".join(re.escape(name) for name in replacements)
    invocation = re.compile(
        rf"(?m)^[^#\n]*(?:^|[\s:`/])(?:{legacy_pattern})(?:\.exe)?(?=\s|$)"
    )
    assert not [
        line
        for example in fenced_examples
        for line in example.splitlines()
        if invocation.search(line)
    ], "legacy CLI invocation outside compatibility table"
    prose_without_fences = re.sub(
        r"```[^\n]*\n.*?```", "", outside_compatibility, flags=re.DOTALL
    )
    for allowed_context in (
        "temporary compatibility `bh-daemon` binary location",
        "compatibility-named `bh-daemon` systemd unit",
    ):
        prose_without_fences = prose_without_fences.replace(
            allowed_context,
            allowed_context.replace("`bh-daemon`", "bh-daemon"),
        )
    inline_invocation = re.compile(
        rf"(?:^|[\s/\\\"'])(?:{legacy_pattern})(?:\.exe)?"
        r"(?=$|[\s\"';&|<>])"
    )
    assert not [
        span
        for span in re.findall(r"(?<!`)`([^`\n]+)`(?!`)", prose_without_fences)
        if inline_invocation.search(span)
    ], "legacy CLI invocation outside compatibility table"
    standalone = re.compile(
        rf"^\s*`?(?:\S+/)?(?:{legacy_pattern})(?:\.exe)?(?=\s|`|$)"
    )
    assert not [
        line
        for line in prose_without_fences.splitlines()
        if standalone.search(line)
    ], "legacy CLI invocation outside compatibility table"


def test_readme_presents_codereeve_as_canonical_cli() -> None:
    """The README presents the unified CodeReeve command as canonical."""
    text = Path("README.md").read_text(encoding="utf-8")
    current_state = text.split("## Why vendor instead of depend upstream?", 1)[
        0
    ]
    assert "src/codereeve/vendor/symphony/" in current_state
    assert "src/codereeve/chain/" in current_state
    assert "src/baton_harness/vendor/symphony/" not in current_state
    assert "src/baton_harness/chain/" not in current_state
    for command in (
        "codereeve daemon",
        "codereeve doctor",
        "codereeve provenance",
        "codereeve hook after-create",
        "codereeve hook before-run",
        "codereeve hook after-run",
        "codereeve hook force-pr-not-merge",
        "codereeve verify",
    ):
        assert command in text
    for variable in (
        "CODEREEVE_BUILD_VERSION",
        "CODEREEVE_BUILD_SOURCE_REVISION",
        "CODEREEVE_BUILD_DEVELOPMENT",
    ):
        assert variable in text

    variable_compatibility = text.split(
        "The old build-variable names are accepted only as temporary", 1
    )[1].split("Do not set both names for one value", 1)[0]
    variable_replacements = {
        "BH_BUILD_VERSION": "CODEREEVE_BUILD_VERSION",
        "BH_BUILD_SOURCE_REVISION": ("CODEREEVE_BUILD_SOURCE_REVISION"),
        "BH_BUILD_DEVELOPMENT": "CODEREEVE_BUILD_DEVELOPMENT",
    }
    assert variable_compatibility.count("| `BH_BUILD_") == len(
        variable_replacements
    )
    for legacy, canonical in variable_replacements.items():
        assert f"| `{canonical}` | `{legacy}` |" in variable_compatibility

    _, compatibility_and_after = text.split(
        "The six legacy scripts remain temporary compatibility shims", 1
    )
    compatibility, _ = compatibility_and_after.split(
        "The hooks derive the issue number", 1
    )
    replacements = {
        "bh-daemon": "codereeve daemon",
        "bh-after-create": "codereeve hook after-create",
        "bh-before-run": "codereeve hook before-run",
        "bh-after-run": "codereeve hook after-run",
        "bh-force-pr-not-merge": "codereeve hook force-pr-not-merge",
        "bh-verify-foundation": "codereeve verify",
    }
    assert compatibility.count("| `bh-") == len(replacements)
    for legacy, canonical in replacements.items():
        assert f"| `{legacy}` | `{canonical}` |" in compatibility
    assert "removed in 0.4.0" in compatibility

    _assert_no_legacy_cli_invocations(text)

    assert (
        "Derived from the temporary compatibility `bh-daemon` binary "
        "location" in text
    )
    development = text.split("## Python development", 1)[1].split(
        "## Prerequisites (runtime)", 1
    )[0]
    assert "src/codereeve/vendor/symphony/" in development
    assert "src/baton_harness/vendor/symphony/" not in development

    upgrade = text.split("### Supported 0.2 upgrade and rollback", 1)[1].split(
        "### Provenance and preflight", 1
    )[0]
    normalized_upgrade = " ".join(upgrade.split())
    assert "codereeve verify --installed" in upgrade
    assert ".venv-codereeve" in upgrade
    assert (
        "Keep the original `.venv` completely unchanged" in normalized_upgrade
    )
    assert 'SOURCE_REVISION="$(git rev-parse HEAD)"' in upgrade
    assert 'CODEREEVE_BUILD_SOURCE_REVISION="$SOURCE_REVISION"' in upgrade


@pytest.mark.parametrize(
    "document",
    [
        Path("README.md"),
        Path("docs/superpowers/specs/2026-09-07-codereeve-rename-design.md"),
    ],
)
def test_package_docs_reference_persisted_repo_paths(document: Path) -> None:
    """Durable package docs reference committed repository paths."""
    tracked = set(
        subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    )
    generated_artifacts = {"src/codereeve/build_provenance.json"}
    references = re.findall(
        r"(?<!`)`((?:src|docs|bin|config|tests)/[A-Za-z0-9_./-]+)"
        r"(?::L\d+(?:-L\d+)?)?`(?!`)",
        document.read_text(encoding="utf-8"),
    )

    missing = sorted(
        reference
        for reference in references
        if reference not in generated_artifacts
        and reference not in tracked
        and not any(
            path.startswith(f"{reference.rstrip('/')}/") for path in tracked
        )
    )
    assert not missing, f"{document} references uncommitted paths: {missing}"


@pytest.mark.parametrize(
    "command",
    [
        "bh-daemon --doctor --strict",
        ".venv/bin/bh-daemon --once",
        r".venv\Scripts\bh-daemon.exe --once",
        "sudo bh-daemon --once",
    ],
    ids=["bare", "posix-path", "windows-path", "sudo"],
)
def test_readme_contract_rejects_inline_legacy_cli_invocation(
    command: str,
) -> None:
    """Inline legacy commands cannot evade the README CLI contract."""
    text = Path("README.md").read_text(encoding="utf-8")
    mutated = text.replace(
        "`codereeve doctor --strict`",
        f"`{command}`",
        1,
    )
    assert mutated != text
    with pytest.raises(
        AssertionError,
        match="legacy CLI invocation outside compatibility table",
    ):
        _assert_no_legacy_cli_invocations(mutated)


@pytest.mark.parametrize(
    "context",
    [
        "`sudo systemctl restart bh-daemon.service`",
        "`docs/operator.md#bh-daemon`",
        "`BH_DAEMON_LOG_LEVEL=debug`",
        "`echo $bh-daemon`",
        "`/etc/bh-daemon/secrets.env`",
        "temporary compatibility `bh-daemon` binary location",
        "compatibility-named `bh-daemon` systemd unit",
    ],
    ids=[
        "service",
        "anchor",
        "env-name",
        "env-ref",
        "config-path",
        "binary-prose",
        "service-prose",
    ],
)
def test_readme_contract_allows_legacy_non_invocation_contexts(
    context: str,
) -> None:
    """Retained identifiers and exact compatibility prose are permitted."""
    text = Path("README.md").read_text(encoding="utf-8")
    _assert_no_legacy_cli_invocations(f"{text}\nDetails: {context}.\n")


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
    """Full CI runs only for main PRs with read-only token access."""
    workflow = _load_yaml(".github/workflows/ci.yml")
    assert workflow["on"] == {"pull_request": {"branches": ["main"]}}
    assert workflow["permissions"] == {"contents": "read"}


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
            "run": ".venv/bin/python -m codereeve.pr_policy",
        },
    ]
