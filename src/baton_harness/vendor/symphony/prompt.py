"""symphony/prompt.py — Jinja2 prompt template renderer."""

from __future__ import annotations

from dataclasses import asdict

from jinja2 import (
    Environment,
    StrictUndefined,
    TemplateSyntaxError,
    UndefinedError,
)

from .tracker import Issue  # VENDOR-PATCH: relative import for vendoring


class PromptError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


_env = Environment(undefined=StrictUndefined)


def render_prompt(
    template_str: str,
    issue: Issue,
    attempt: int | None = None,
) -> str:
    """Render a prompt template with issue context."""
    if not template_str:
        return ""

    try:
        template = _env.from_string(template_str)
    except TemplateSyntaxError as e:
        raise PromptError("template_parse_error", str(e))

    try:
        return template.render(
            issue=asdict(issue),
            attempt=attempt,
        )
    except UndefinedError as e:
        raise PromptError("template_render_error", str(e))
