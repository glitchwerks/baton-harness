"""symphony/config.py — WORKFLOW.md parser and typed config."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml


class ConfigError(Exception):
    """Raised for WORKFLOW.md parsing/validation failures."""

    def __init__(self, code: str, message: str) -> None:
        """Initialize the error with a machine-readable code and message.

        Args:
            code: Short machine-readable error code.
            message: Human-readable error message.
        """
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass
class WorkflowConfig:
    """Typed config parsed from a WORKFLOW.md file's YAML front matter.

    Attributes:
        tracker_kind: Which issue tracker backend to use.
        tracker_labels: Only fetch issues carrying all of these labels.
        tracker_exclude_labels: Skip issues carrying any of these
            labels.
        tracker_assignee: Only fetch issues assigned to this login.
        poll_interval_ms: Delay between tracker polls, in milliseconds.
        max_concurrent: Maximum number of concurrent agent workers.
        max_turns: Maximum turns per agent run.
        max_retry_backoff_ms: Maximum retry backoff, in milliseconds.
        agent_command: The CLI command used to invoke the agent.
        permission_mode: The agent's permission mode.
        skills: Skill names to request for every agent run.
        mcp_servers: MCP server configs to pass to the agent.
        hook_after_create: Command to run after workspace creation.
        hook_before_run: Command to run before the agent run.
        hook_after_run: Command to run after the agent run.
        hook_before_remove: Command to run before workspace removal.
        hook_timeout_ms: Per-hook timeout, in milliseconds.
        prompt_template: The prompt body parsed from WORKFLOW.md.
        required_checks: Operator-facing override for the merge gate's
            required-check set. Empty is the "unset" sentinel.
    """

    # Tracker
    tracker_kind: str = "github"
    tracker_labels: list[str] = field(default_factory=list)
    tracker_exclude_labels: list[str] = field(default_factory=list)
    tracker_assignee: str | None = None

    # Polling
    poll_interval_ms: int = 30000

    # Agent
    max_concurrent: int = 3
    max_turns: int = 5
    max_retry_backoff_ms: int = 300000
    agent_command: str = "claude"
    permission_mode: str = "acceptEdits"
    skills: list[str] = field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)

    # Hooks
    hook_after_create: str | None = None
    hook_before_run: str | None = None
    hook_after_run: str | None = None
    hook_before_remove: str | None = None
    hook_timeout_ms: int = 60000

    # Prompt
    prompt_template: str = ""

    # Required checks (issue #225)
    # VENDOR-PATCH VP-8: operator-facing override for the merge gate's
    # required-check set (baton_harness.chain.merge.REQUIRED_CHECKS).
    # Empty list is the "unset" sentinel — the merge gate falls back to
    # the hardcoded default and warns when this is empty.
    required_checks: list[str] = field(default_factory=list)


def _get(
    d: dict[str, Any],
    *keys: str,
    default: Any = None,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    """Walk a nested dict by a sequence of keys.

    Args:
        d: The mapping to walk.
        *keys: Nested keys to look up in turn.
        default: Value returned if any key is missing or a value along
            the path is not itself a mapping.

    Returns:
        The value found at the nested key path, or `default`.
    """
    current: Any = d
    for k in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(k)
        if current is None:
            return default
    return current


def _parse_front_matter(content: str) -> tuple[dict[str, Any], str]:
    """Split WORKFLOW.md into YAML front matter dict and prompt body."""
    if not content.startswith("---"):
        return {}, content.strip()

    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content.strip()

    yaml_str = parts[1]
    body = parts[2].strip()

    try:
        parsed = yaml.safe_load(yaml_str)
    except yaml.YAMLError as e:
        raise ConfigError("workflow_parse_error", str(e)) from e

    if parsed is None:
        return {}, body

    if not isinstance(parsed, dict):
        raise ConfigError(
            "workflow_front_matter_not_a_map",
            f"Expected a mapping, got {type(parsed).__name__}",
        )

    return parsed, body


def load_workflow(path: str) -> WorkflowConfig:
    """Load and parse a WORKFLOW.md file into a typed config."""
    if not os.path.isfile(path):
        raise ConfigError("missing_workflow_file", f"File not found: {path}")

    try:
        with open(path) as f:
            content = f.read()
    except OSError as e:
        raise ConfigError("missing_workflow_file", str(e)) from e

    fm, prompt = _parse_front_matter(content)

    def _int(val: Any, default: int) -> int:  # noqa: ANN401
        if val is None:
            return default
        try:
            return int(val)
        except (ValueError, TypeError):
            return default

    # VENDOR-PATCH VP-8: top-level required_checks: front-matter key
    # (sibling of tracker/polling/agent/hooks), absent -> [] default.
    # A scalar value (e.g. a typo'd string instead of a YAML list) is
    # truthy and would otherwise pass straight through and be iterated
    # char-by-char downstream -- guard to a list, else fall back to [].
    # A list value may still contain non-string elements (e.g. a typo'd
    # int like `- 123` alongside real check names) -- those can never
    # match a real GitHub check name and would silently reproduce the
    # fail-closed "no matching jobs" symptom (#225) for that element, so
    # each element is filtered to `str` as well (#229).
    _raw_required_checks = _get(fm, "required_checks", default=[])

    return WorkflowConfig(
        tracker_kind=_get(fm, "tracker", "kind", default="github"),
        tracker_labels=_get(fm, "tracker", "labels", default=[]),
        tracker_exclude_labels=_get(
            fm, "tracker", "exclude_labels", default=[]
        ),
        tracker_assignee=_get(fm, "tracker", "assignee"),
        poll_interval_ms=_int(_get(fm, "polling", "interval_ms"), 30000),
        max_concurrent=_int(_get(fm, "agent", "max_concurrent"), 3),
        max_turns=_int(_get(fm, "agent", "max_turns"), 5),
        max_retry_backoff_ms=_int(
            _get(fm, "agent", "max_retry_backoff_ms"), 300000
        ),
        agent_command=_get(fm, "agent", "command", default="claude"),
        permission_mode=_get(
            fm, "agent", "permission_mode", default="acceptEdits"
        ),
        skills=_get(fm, "agent", "skills", default=[]),
        mcp_servers=_get(fm, "agent", "mcp_servers", default=[]),
        hook_after_create=_get(fm, "hooks", "after_create"),
        hook_before_run=_get(fm, "hooks", "before_run"),
        hook_after_run=_get(fm, "hooks", "after_run"),
        hook_before_remove=_get(fm, "hooks", "before_remove"),
        hook_timeout_ms=_int(_get(fm, "hooks", "timeout_ms"), 60000),
        prompt_template=prompt,
        required_checks=(
            [c for c in _raw_required_checks if isinstance(c, str)]
            if isinstance(_raw_required_checks, list)
            else []
        ),
    )
