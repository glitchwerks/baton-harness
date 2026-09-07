"""Secret redaction helpers for diagnostic text."""

from __future__ import annotations

import re
from collections.abc import Iterable

_REDACTION_MARKER = "«redacted»"
_TOKEN_PATTERN = re.compile(
    r"(?:github_pat_|ghs_|ghp_|gho_|ghu_|ghr_)[A-Za-z0-9_]+"
)
_URL_USERINFO_PATTERN = re.compile(r"(?<=://)[^\s/@:]+:[^\s/@]+@")
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
    result = text
    try:
        result = _PEM_PATTERN.sub(_REDACTION_MARKER, result)
        result = _URL_USERINFO_PATTERN.sub(_REDACTION_MARKER, result)
        result = _AUTHORIZATION_PATTERN.sub(r"\1" + _REDACTION_MARKER, result)
        result = _QUERY_PATTERN.sub(r"\1" + _REDACTION_MARKER, result)
        result = _FIELD_PATTERN.sub(r"\1" + _REDACTION_MARKER, result)
        result = _TOKEN_PATTERN.sub(_REDACTION_MARKER, result)
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
                    result = result.replace(value, _REDACTION_MARKER)
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
    return result
