"""Secret redaction helpers for diagnostic text."""

from __future__ import annotations

import re
from collections.abc import Iterable

_REDACTION_MARKER = "«redacted»"
_TOKEN_PATTERN = re.compile(
    r"(?:github_pat_|ghs_|ghp_|gho_|ghu_|ghr_)[A-Za-z0-9_]+"
)
_URL_USERINFO_PATTERN = re.compile(
    r"(\b[A-Za-z][A-Za-z0-9+.-]*:[/\\]+)[^\s/@?#\\]+@"
)
_PEM_PATTERN = re.compile(
    r"-----BEGIN ([A-Z0-9 ]+)-----.*?-----END \1-----", re.DOTALL
)
_AUTHORIZATION_PATTERN = re.compile(
    r"(\bauthorization\s*[:=]\s*)(?:Bearer|Basic)\s+[^\s,;\"']+",
    re.IGNORECASE,
)
_CREDENTIAL_NAMES = (
    r"access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"api[_-]?key|client[_-]?secret|password|passwd|"
    r"authorization|oauth[_-]?token"
)
_QUERY_PATTERN = re.compile(
    rf"([?&](?:{_CREDENTIAL_NAMES})=)[^\s&#\"'<>]+", re.IGNORECASE
)
_FIELD_PATTERN = re.compile(
    rf"(?<![\w-])([\"']?(?:{_CREDENTIAL_NAMES})[\"']?\s*[:=]\s*)"
    r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;}\]&<>]+)""",
    re.IGNORECASE,
)


class RedactionError(RuntimeError):
    """Raised when strict redaction cannot safely complete."""


def redact_secrets(
    text: str, *, extra_values: Iterable[str] = (), strict: bool = False
) -> str:
    """Redact known and caller-supplied secrets from text.

    Args:
        text: Diagnostic text that may contain credentials.
        extra_values: Exact secret values to redact when non-empty.
        strict: Raise a fixed error instead of suppressing failures.

    Returns:
        Text with secret spans replaced by a visible marker.

    Raises:
        RedactionError: If strict redaction encounters invalid input or
            cannot complete all substitutions.
    """
    spans: list[tuple[int, int]] = []
    try:
        for pattern, preserve_prefix in (
            (_PEM_PATTERN, False),
            (_URL_USERINFO_PATTERN, True),
            (_AUTHORIZATION_PATTERN, True),
            (_QUERY_PATTERN, True),
            (_FIELD_PATTERN, True),
            (_TOKEN_PATTERN, False),
        ):

            def record(
                match: re.Match[str], keep_prefix: bool = preserve_prefix
            ) -> str:
                """Record a structural span using original text offsets."""
                start = match.end(1) if keep_prefix else match.start()
                spans.append((start, match.end()))
                return match.group()

            # Discover structural spans first, without changing offsets.
            pattern.sub(record, text)
    except Exception:  # noqa: BLE001
        # Fail CLOSED: this is security-critical redaction, so an
        # exception mid-substitution must never hand back the raw
        # (possibly still secret-bearing) input text.
        if strict:
            raise RedactionError("credential redaction failed") from None
        return _REDACTION_MARKER
    try:
        for value in extra_values:
            try:
                if value:
                    start = text.find(value)
                    while start != -1:
                        spans.append((start, start + len(value)))
                        # Include self-overlapping occurrences as well.
                        start = text.find(value, start + 1)
            except Exception:  # noqa: BLE001
                # Preserve per-entry isolation for existing callers.
                if strict:
                    raise RedactionError(
                        "credential redaction failed"
                    ) from None
                continue
    except Exception:  # noqa: BLE001
        if strict:
            raise RedactionError("credential redaction failed") from None
        return _REDACTION_MARKER
    # Union every original span before replacing any text. Sequential
    # substitutions could otherwise hide a later match while leaking its
    # suffix, including overlaps between structural and exact secrets.
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    parts: list[str] = []
    previous_end = 0
    for start, end in merged:
        parts.extend((text[previous_end:start], _REDACTION_MARKER))
        previous_end = end
    parts.append(text[previous_end:])
    return "".join(parts)
