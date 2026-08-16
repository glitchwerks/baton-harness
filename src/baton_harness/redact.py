"""Secret redaction helpers for diagnostic text."""

from __future__ import annotations

import re
from collections.abc import Iterable

_REDACTION_MARKER = "«redacted»"
_TOKEN_PATTERN = re.compile(
    r"(?:github_pat_|ghs_|ghp_|gho_|ghu_|ghr_)[A-Za-z0-9_]+"
)
_URL_USERINFO_PATTERN = re.compile(r"(?<=://)[^\s/@:]+:[^\s/@]+@")


def redact_secrets(text: str, *, extra_values: Iterable[str] = ()) -> str:
    """Redact known and caller-supplied secrets from text.

    Args:
        text: Diagnostic text that may contain credentials.
        extra_values: Exact secret values to redact when non-empty.

    Returns:
        Text with secret spans replaced by a visible marker.
    """
    result = text
    try:
        result = _URL_USERINFO_PATTERN.sub(_REDACTION_MARKER, result)
        result = _TOKEN_PATTERN.sub(_REDACTION_MARKER, result)
        for value in extra_values:
            if value:
                result = result.replace(value, _REDACTION_MARKER)
    except Exception:  # noqa: BLE001
        return result
    return result
