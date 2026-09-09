"""Parse and resolve literal CodeReeve environment configuration safely."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_UNSAFE_VALUE_CHARACTERS = frozenset("$`;&|<>")
_UNSUPPORTED_LINE_SEPARATORS = frozenset("\v\f\x1c\x1d\x1e\x85\u2028\u2029")


class ConfigSyntaxError(ValueError):
    """Raised when an environment line is not a literal assignment."""

    def __init__(self, source: str, line: int) -> None:
        """Initialize a redacted syntax error with its source location.

        Args:
            source: Name of the parsed configuration source.
            line: One-based physical line number of the invalid input.
        """
        super().__init__(f"invalid environment assignment at {source}:{line}")
        self.source = source
        self.line = line


class ReservedControlError(ConfigSyntaxError):
    """External configuration attempted to supply a unit-owned control."""

    def __init__(self, source: str, line: int, key: str) -> None:
        """Identify only the reserved key and source, never its value."""
        super().__init__(source, line)
        self.args = (f"reserved environment control {key} at {source}:{line}",)


class AliasConflictError(ValueError):
    """Raised when canonical and legacy environment spellings disagree."""


@dataclass(frozen=True)
class Assignment:
    """One parsed literal assignment with enough source form for rewriting.

    Attributes:
        key: Assignment name.
        value: Decoded literal value.
        raw: Original physical input line, excluding its newline.
        line: One-based physical line number.
        comment: Preserved suffix beginning with whitespace or ``#``.
        leading_trivia: Blank or comment lines immediately before the
            assignment.
        trailing_trivia: Blank or comment lines immediately after the final
            assignment in a parsed file.
    """

    key: str
    value: str
    raw: str
    line: int
    comment: str = ""
    leading_trivia: tuple[str, ...] = ()
    trailing_trivia: tuple[str, ...] = ()


class _TriviaOnlyAssignments(tuple[Assignment, ...]):
    """Tuple-compatible parsed result that retains a trivia-only file."""

    file_trivia: tuple[str, ...]

    def __new__(cls, file_trivia: Sequence[str]) -> _TriviaOnlyAssignments:
        """Create an empty assignment tuple with its source trivia.

        Args:
            file_trivia: Physical comment and blank lines in source order.

        Returns:
            An empty tuple-compatible parsed result.
        """
        result = super().__new__(cls)
        result.file_trivia = tuple(file_trivia)
        return result


@dataclass(frozen=True)
class EnvLayer:
    """A named environment source in descending priority order.

    Attributes:
        source: Safe source label used in diagnostics.
        values: Values supplied by this layer; empty strings are set values.
    """

    source: str
    values: Mapping[str, str]


@dataclass(frozen=True)
class AliasSpec:
    """The canonical and temporary legacy names for one product variable.

    Attributes:
        canonical: Canonical CodeReeve environment variable name.
        legacy: Temporary Baton Harness compatibility variable name.
    """

    canonical: str
    legacy: str


@dataclass(frozen=True)
class LegacyUse:
    """One selected compatibility spelling and the layer that supplied it.

    Attributes:
        canonical: Canonical name selected for consumers.
        legacy: Legacy spelling observed in configuration.
        source: Layer that supplied the selected legacy spelling.
    """

    canonical: str
    legacy: str
    source: str


@dataclass(frozen=True)
class ResolvedEnvironment:
    """Canonical environment values and any temporary compatibility uses.

    Attributes:
        values: Resolved values, with canonical product spellings present.
        legacy_uses: Legacy spellings encountered during resolution.
    """

    values: Mapping[str, str]
    legacy_uses: tuple[LegacyUse, ...]


# Unit-owned controls have no historical spelling and are never file inputs.
PRIVATE_PRODUCT_CONTROLS = frozenset({"CODEREEVE_CUTOVER_GATE"})


PRODUCT_ALIASES = (
    AliasSpec("CODEREEVE_ADMIN_ROLE_ID", "BH_ADMIN_ROLE_ID"),
    AliasSpec("CODEREEVE_APP_AUTH_JWT_CMD", "BH_APP_AUTH_JWT_CMD"),
    AliasSpec("CODEREEVE_APP_AUTH_TOKEN_CMD", "BH_APP_AUTH_TOKEN_CMD"),
    AliasSpec("CODEREEVE_BUILD_DEVELOPMENT", "BH_BUILD_DEVELOPMENT"),
    AliasSpec("CODEREEVE_BUILD_SOURCE_REVISION", "BH_BUILD_SOURCE_REVISION"),
    AliasSpec("CODEREEVE_BUILD_VERSION", "BH_BUILD_VERSION"),
    AliasSpec("CODEREEVE_DAEMON_SECRETS_PATH", "BH_DAEMON_SECRETS_PATH"),
    AliasSpec("CODEREEVE_DEBUG_CONFIG", "BH_DEBUG_CONFIG"),
    AliasSpec("CODEREEVE_FAILURE_COUNTS_PATH", "BH_FAILURE_COUNTS_PATH"),
    AliasSpec("CODEREEVE_FEATURE_BRANCH", "BH_FEATURE_BRANCH"),
    AliasSpec("CODEREEVE_GITHUB_APP_ID", "BH_GITHUB_APP_ID"),
    AliasSpec(
        "CODEREEVE_GITHUB_APP_INSTALLATION_ID",
        "BH_GITHUB_APP_INSTALLATION_ID",
    ),
    AliasSpec(
        "CODEREEVE_GITHUB_APP_KEY_PROVIDER",
        "BH_GITHUB_APP_KEY_PROVIDER",
    ),
    AliasSpec(
        "CODEREEVE_GITHUB_APP_PRIVATE_KEY_FILE",
        "BH_GITHUB_APP_PRIVATE_KEY_FILE",
    ),
    AliasSpec("CODEREEVE_HEARTBEAT_FILE", "BH_HEARTBEAT_FILE"),
    AliasSpec("CODEREEVE_HEARTBEAT_PING_URL", "BH_HEARTBEAT_PING_URL"),
    AliasSpec("CODEREEVE_HEARTBEAT_STALL_S", "BH_HEARTBEAT_STALL_S"),
    AliasSpec("CODEREEVE_MAX_ISSUE_FAILURES", "BH_MAX_ISSUE_FAILURES"),
    AliasSpec("CODEREEVE_PROBE_DRY_RUN", "BH_PROBE_DRY_RUN"),
    AliasSpec("CODEREEVE_PROBE_HOOK_SCRIPT", "BH_PROBE_HOOK_SCRIPT"),
    AliasSpec("CODEREEVE_PROBE_PR_NUMBER", "BH_PROBE_PR_NUMBER"),
    AliasSpec("CODEREEVE_PROBE_SANDBOX_REPO", "BH_PROBE_SANDBOX_REPO"),
    AliasSpec(
        "CODEREEVE_PROBE_WORKER_TOKEN_PATH",
        "BH_PROBE_WORKER_TOKEN_PATH",
    ),
    AliasSpec("CODEREEVE_PROJECT_ROOT", "BH_PROJECT_ROOT"),
    AliasSpec("CODEREEVE_REDISPATCH_COUNTS_PATH", "BH_REDISPATCH_COUNTS_PATH"),
    AliasSpec("CODEREEVE_REDISPATCH_MAX", "BH_REDISPATCH_MAX"),
    AliasSpec(
        "CODEREEVE_REDISPATCH_WINDOW_TICKS",
        "BH_REDISPATCH_WINDOW_TICKS",
    ),
    AliasSpec("CODEREEVE_REPO_NAME", "BH_REPO_NAME"),
    AliasSpec("CODEREEVE_REPO_OWNER", "BH_REPO_OWNER"),
    AliasSpec("CODEREEVE_RUNLOG_PATH", "BH_RUNLOG_PATH"),
    AliasSpec("CODEREEVE_SCENARIO", "BH_SCENARIO"),
    AliasSpec("CODEREEVE_SETUP_NO_PROMPT", "BH_SETUP_NO_PROMPT"),
    AliasSpec("CODEREEVE_SLACK_WEBHOOK_URL", "BH_SLACK_WEBHOOK_URL"),
    AliasSpec("CODEREEVE_VENV", "BH_VENV"),
    AliasSpec(
        "CODEREEVE_VERIFY_BLOCK_TIMEOUT_SECS",
        "BH_VERIFY_BLOCK_TIMEOUT_SECS",
    ),
    AliasSpec(
        "CODEREEVE_WORKER_PROGRESS_STALL_S",
        "BH_WORKER_PROGRESS_STALL_S",
    ),
    AliasSpec("CODEREEVE_WORKTREE_GC", "BH_WORKTREE_GC"),
    AliasSpec("CODEREEVE_ROOT", "BATON_HARNESS_DIR"),
)


def parse_env_text(text: str, *, source: str) -> tuple[Assignment, ...]:
    """Parse a literal, non-executing environment configuration text.

    Args:
        text: Configuration content composed of physical assignment lines.
        source: Safe source label reported with syntax locations.

    Returns:
        Parsed assignments in physical order, including repeated spellings.

    Raises:
        ConfigSyntaxError: If input contains unsafe or unsupported syntax.
    """
    null_offset = text.find("\x00")
    if null_offset != -1:
        raise ConfigSyntaxError(source, text[:null_offset].count("\n") + 1)

    assignments: list[Assignment] = []
    pending_trivia: list[str] = []
    physical_lines = _physical_lines(text, source)
    for line_number, raw in enumerate(physical_lines, start=1):
        parsed = _parse_line(raw, source, line_number)
        if parsed is None:
            pending_trivia.append(raw)
            continue
        if parsed.key in PRIVATE_PRODUCT_CONTROLS:
            raise ReservedControlError(source, line_number, parsed.key)
        if raw.endswith("\\") and line_number < len(physical_lines):
            raise ConfigSyntaxError(source, line_number)
        assignments.append(
            replace(parsed, leading_trivia=tuple(pending_trivia))
        )
        pending_trivia.clear()
    if assignments and pending_trivia:
        assignments[-1] = replace(
            assignments[-1], trailing_trivia=tuple(pending_trivia)
        )
    if not assignments:
        return _TriviaOnlyAssignments(pending_trivia)
    return tuple(assignments)


def parse_env_file(path: Path) -> tuple[Assignment, ...]:
    """Read and parse the explicit environment configuration file.

    Args:
        path: Configuration file to read exactly once.

    Returns:
        Parsed literal assignments from ``path``.

    Raises:
        OSError: If ``path`` cannot be read.
        ConfigSyntaxError: If the file has unsupported syntax.
    """
    return parse_env_text(path.read_text(encoding="utf-8"), source=str(path))


def resolve_alias_pair(
    values: Mapping[str, str], canonical: str, legacy: str
) -> str | None:
    """Resolve one same-source canonical/legacy pair without normalization.

    Args:
        values: Values from one environment source.
        canonical: Canonical CodeReeve spelling.
        legacy: Temporary compatibility spelling.

    Returns:
        The canonical value when set, otherwise the legacy value, or ``None``.

    Raises:
        AliasConflictError: If both spellings are set to different values.
    """
    canonical_value = values.get(canonical)
    legacy_value = values.get(legacy)
    if (
        canonical_value is not None
        and legacy_value is not None
        and canonical_value != legacy_value
    ):
        raise AliasConflictError(
            f"conflicting environment variables: {canonical} and {legacy}"
        )
    return canonical_value if canonical_value is not None else legacy_value


def resolve_environment(
    layers: Sequence[EnvLayer],
    *,
    aliases: Sequence[AliasSpec] = PRODUCT_ALIASES,
    export_legacy: bool = True,
) -> ResolvedEnvironment:
    """Resolve layered configuration into canonical product environment keys.

    Args:
        layers: Environment sources ordered from highest to lowest priority.
        aliases: Canonical/legacy product key pairs to resolve.
        export_legacy: Whether to materialize selected legacy aliases too.

    Returns:
        Canonical values, third-party pass-through values, and legacy usage.

    Raises:
        AliasConflictError: If selected canonical and legacy values disagree.
    """
    resolved = _resolve_layer_values(layers)
    legacy_uses: list[LegacyUse] = []
    for alias in aliases:
        canonical_value = _first_layer_value(layers, alias.canonical)
        legacy_value = _first_layer_value(layers, alias.legacy)
        selected = _resolve_selected_pair(alias, canonical_value, legacy_value)
        resolved.pop(alias.legacy, None)
        if selected is None:
            resolved.pop(alias.canonical, None)
            continue

        value, _source = selected
        resolved[alias.canonical] = value
        if export_legacy:
            resolved[alias.legacy] = value
        if legacy_value is not None:
            legacy_uses.append(
                LegacyUse(alias.canonical, alias.legacy, legacy_value[1])
            )
    return ResolvedEnvironment(resolved, tuple(legacy_uses))


def runtime_environment(
    values: Mapping[str, str] | None = None,
) -> ResolvedEnvironment:
    """Capture a conflict-free canonical runtime environment.

    Args:
        values: Explicit environment, or the current process environment.

    Returns:
        A canonical snapshot without mutating the source.

    Raises:
        AliasConflictError: If any product alias pair disagrees.
    """
    return resolve_environment(
        (
            EnvLayer(
                "environment", dict(os.environ if values is None else values)
            ),
        ),
        export_legacy=False,
    )


def apply_resolved_environment(
    resolved: ResolvedEnvironment, target: MutableMapping[str, str]
) -> None:
    """Materialize a verified snapshot and temporary aliases at one boundary.

    Args:
        resolved: Complete conflict-free snapshot produced by the resolver.
        target: Environment receiving canonical and compatibility values.

    Product aliases absent from the snapshot are removed from the target.
    Unrelated target keys are retained.
    """
    materialized = dict(resolved.values)
    for alias in PRODUCT_ALIASES:
        if alias.canonical in materialized:
            materialized[alias.legacy] = materialized[alias.canonical]
        else:
            target.pop(alias.canonical, None)
            target.pop(alias.legacy, None)
    target.update(materialized)


def rewrite_assignments(
    assignments: Sequence[Assignment],
    *,
    path_values: Mapping[str, tuple[str, str]],
) -> str:
    """Canonicalize recognized assignments while retaining literal other lines.

    Args:
        assignments: Parsed assignment records in their original order.
        path_values: Canonical key to ``(legacy_default, new_default)`` pairs.

    Returns:
        A canonicalized environment file ending with one newline when nonempty.

    Raises:
        AliasConflictError: If an alias pair has conflicting selected values.
    """
    aliases_by_key = {
        key: alias
        for alias in PRODUCT_ALIASES
        for key in (alias.canonical, alias.legacy)
    }
    if not assignments:
        if isinstance(assignments, _TriviaOnlyAssignments):
            trivia = assignments.file_trivia
            return "" if not trivia else "\n".join(trivia) + "\n"
        return ""
    selected = _rewrite_selection(assignments, PRODUCT_ALIASES)
    emitted: set[str] = set()
    lines: list[str] = []
    for assignment in assignments:
        alias = aliases_by_key.get(assignment.key)
        if alias is None:
            lines.extend(assignment.leading_trivia)
            lines.append(assignment.raw)
            continue
        if alias.canonical in emitted:
            lines.extend(assignment.leading_trivia)
            comment = _comment_as_trivia(assignment.comment)
            if comment is not None:
                lines.append(comment)
            continue
        emitted.add(alias.canonical)
        chosen = selected.get(alias.canonical)
        if chosen is None:
            continue
        lines.extend(assignment.leading_trivia)
        value = _rewrite_path_value(alias.canonical, chosen.value, path_values)
        lines.append(
            f"{alias.canonical}={_format_value(value)}{assignment.comment}"
        )
    if assignments:
        lines.extend(assignments[-1].trailing_trivia)
    return "" if not lines else "\n".join(lines) + "\n"


def _parse_line(raw: str, source: str, line_number: int) -> Assignment | None:
    """Parse one physical line as a blank, comment, or literal assignment."""
    index = _skip_space(raw, 0)
    if index == len(raw) or raw[index] == "#":
        return None
    export_end = index + len("export")
    if raw.startswith("export", index) and (
        export_end == len(raw) or raw[export_end].isspace()
    ):
        if export_end == len(raw):
            raise ConfigSyntaxError(source, line_number)
        index = _skip_space(raw, export_end)

    match = _NAME_PATTERN.match(raw, index)
    if match is None:
        raise ConfigSyntaxError(source, line_number)
    key = match.group()
    index = _skip_space(raw, match.end())
    if index == len(raw) or raw[index] != "=":
        raise ConfigSyntaxError(source, line_number)
    value_start = index + 1
    index = _skip_space(raw, value_start)
    if index == len(raw):
        return Assignment(key, "", raw, line_number)
    if index > value_start and raw[index] == "#":
        return Assignment(key, "", raw, line_number, raw[value_start:])
    if raw[index] in "\"'":
        return _parse_quoted_assignment(raw, source, line_number, key, index)
    return _parse_unquoted_assignment(raw, source, line_number, key, index)


def _parse_quoted_assignment(
    raw: str, source: str, line_number: int, key: str, index: int
) -> Assignment:
    """Parse a complete quoted assignment and its optional comment."""
    quote = raw[index]
    value: list[str] = []
    index += 1
    while index < len(raw):
        character = raw[index]
        if character == quote:
            trailing = raw[index + 1 :]
            if trailing and not (
                trailing.isspace() or trailing.lstrip().startswith("#")
            ):
                raise ConfigSyntaxError(source, line_number)
            _reject_unsafe("".join(value), source, line_number)
            return Assignment(
                key,
                "".join(value),
                raw,
                line_number,
                trailing,
            )
        if character == "\\":
            if index + 1 == len(raw) or raw[index + 1] not in (quote, "\\"):
                raise ConfigSyntaxError(source, line_number)
            value.append(raw[index + 1])
            index += 2
            continue
        value.append(character)
        index += 1
    raise ConfigSyntaxError(source, line_number)


def _parse_unquoted_assignment(
    raw: str, source: str, line_number: int, key: str, index: int
) -> Assignment:
    """Parse an unquoted assignment, separating only whitespace comments."""
    value_end = len(raw)
    for offset in range(index, len(raw)):
        if raw[offset] == "#" and offset > index and raw[offset - 1].isspace():
            value_end = offset
            break
    untrimmed_value = raw[index:value_end]
    value = untrimmed_value.rstrip()
    comment = raw[index + len(value) :]
    if "'" in value or '"' in value:
        raise ConfigSyntaxError(source, line_number)
    _reject_unsafe(value, source, line_number)
    if _contains_following_assignment(value):
        raise ConfigSyntaxError(source, line_number)
    return Assignment(key, value, raw, line_number, comment)


def _skip_space(text: str, index: int) -> int:
    """Return the first non-horizontal-whitespace character offset."""
    while index < len(text) and text[index] in " \t":
        index += 1
    return index


def _physical_lines(text: str, source: str) -> list[str]:
    """Split only supported LF and CRLF physical lines.

    Args:
        text: Complete environment configuration text.
        source: Safe source label for syntax diagnostics.

    Returns:
        Input physical lines without their supported line endings.

    Raises:
        ConfigSyntaxError: If input contains a control line separator.
    """
    for index, character in enumerate(text):
        if character in _UNSUPPORTED_LINE_SEPARATORS:
            raise ConfigSyntaxError(source, text[:index].count("\n") + 1)
        if character == "\r" and (
            index + 1 == len(text) or text[index + 1] != "\n"
        ):
            raise ConfigSyntaxError(source, text[:index].count("\n") + 1)
    normalized = text.replace("\r\n", "\n")
    if not normalized:
        return []
    lines = normalized.split("\n")
    if normalized.endswith("\n"):
        lines.pop()
    return lines


def _reject_unsafe(value: str, source: str, line_number: int) -> None:
    """Reject shell control characters without exposing their value."""
    if any(character in _UNSAFE_VALUE_CHARACTERS for character in value):
        raise ConfigSyntaxError(source, line_number)


def _contains_following_assignment(value: str) -> bool:
    """Return whether an unquoted value embeds a second shell assignment."""
    return (
        re.search(r"\s+(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=", value)
        is not None
    )


def _resolve_layer_values(layers: Sequence[EnvLayer]) -> dict[str, str]:
    """Select the first occurrence of each spelling by layer priority."""
    values: dict[str, str] = {}
    for layer in layers:
        for key, value in layer.values.items():
            values.setdefault(key, value)
    return values


def _first_layer_value(
    layers: Sequence[EnvLayer], key: str
) -> tuple[str, str] | None:
    """Return the first set value and source, including empty values."""
    for layer in layers:
        if key in layer.values:
            return layer.values[key], layer.source
    return None


def _resolve_selected_pair(
    alias: AliasSpec,
    canonical_value: tuple[str, str] | None,
    legacy_value: tuple[str, str] | None,
) -> tuple[str, str] | None:
    """Choose a pair value or raise a source-labelled, redacted conflict."""
    if (
        canonical_value is not None
        and legacy_value is not None
        and canonical_value[0] != legacy_value[0]
    ):
        raise AliasConflictError(
            "conflicting environment variables: "
            f"{alias.canonical} ({canonical_value[1]}) and "
            f"{alias.legacy} ({legacy_value[1]})"
        )
    return canonical_value if canonical_value is not None else legacy_value


def _rewrite_selection(
    assignments: Sequence[Assignment], aliases: Sequence[AliasSpec]
) -> dict[str, Assignment]:
    """Select final values and reject cross-spelling conflicts."""
    selected: dict[str, Assignment] = {}
    for alias in aliases:
        canonical = _last_assignment(assignments, alias.canonical)
        legacy = _last_assignment(assignments, alias.legacy)
        if (
            canonical is not None
            and legacy is not None
            and canonical.value != legacy.value
        ):
            raise AliasConflictError(
                "conflicting environment variables: "
                f"{alias.canonical} and {alias.legacy}"
            )
        chosen = canonical if canonical is not None else legacy
        if chosen is not None:
            selected[alias.canonical] = chosen
    return selected


def _last_assignment(
    assignments: Sequence[Assignment], key: str
) -> Assignment | None:
    """Return the last physical assignment for one spelling."""
    for assignment in reversed(assignments):
        if assignment.key == key:
            return assignment
    return None


def _rewrite_path_value(
    key: str,
    value: str,
    path_values: Mapping[str, tuple[str, str]],
) -> str:
    """Rewrite only an exact documented legacy default path value."""
    defaults = path_values.get(key)
    if defaults is None:
        return value
    legacy_default, canonical_default = defaults
    return canonical_default if value == legacy_default else value


def _format_value(value: str) -> str:
    """Render one decoded value in a literal unquoted or quoted form."""
    if value and not any(
        character.isspace() or character in "#\"'\\" for character in value
    ):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _comment_as_trivia(comment: str) -> str | None:
    """Convert a skipped inline comment into a retained standalone comment."""
    normalized = comment.lstrip()
    return normalized if normalized.startswith("#") else None
