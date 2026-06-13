"""symphony/config.py — WORKFLOW.md parser and typed config."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml


class ConfigError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass
class WorkflowConfig:
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


def _get(d: dict, *keys: str, default: Any = None) -> Any:
    """Nested dict get."""
    current = d
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
        raise ConfigError("workflow_parse_error", str(e))

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
        raise ConfigError("missing_workflow_file", str(e))

    fm, prompt = _parse_front_matter(content)

    def _int(val: Any, default: int) -> int:
        if val is None:
            return default
        try:
            return int(val)
        except (ValueError, TypeError):
            return default

    return WorkflowConfig(
        tracker_kind=_get(fm, "tracker", "kind", default="github"),
        tracker_labels=_get(fm, "tracker", "labels", default=[]),
        tracker_exclude_labels=_get(fm, "tracker", "exclude_labels", default=[]),
        tracker_assignee=_get(fm, "tracker", "assignee"),
        poll_interval_ms=_int(_get(fm, "polling", "interval_ms"), 30000),
        max_concurrent=_int(_get(fm, "agent", "max_concurrent"), 3),
        max_turns=_int(_get(fm, "agent", "max_turns"), 5),
        max_retry_backoff_ms=_int(_get(fm, "agent", "max_retry_backoff_ms"), 300000),
        agent_command=_get(fm, "agent", "command", default="claude"),
        permission_mode=_get(fm, "agent", "permission_mode", default="acceptEdits"),
        skills=_get(fm, "agent", "skills", default=[]),
        mcp_servers=_get(fm, "agent", "mcp_servers", default=[]),
        hook_after_create=_get(fm, "hooks", "after_create"),
        hook_before_run=_get(fm, "hooks", "before_run"),
        hook_after_run=_get(fm, "hooks", "after_run"),
        hook_before_remove=_get(fm, "hooks", "before_remove"),
        hook_timeout_ms=_int(_get(fm, "hooks", "timeout_ms"), 60000),
        prompt_template=prompt,
    )
