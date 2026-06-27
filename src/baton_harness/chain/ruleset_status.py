"""Slice 3b — read-only ruleset status check for the #144 preflight gate.

Returns a four-state enum that the daemon per-launch preflight gate uses
to decide whether the merge boundary is in place before processing any
issues.  This module is INSPECTION-ONLY — it never mutates GitHub state.
The provisioning side lives in bin/provision-ruleset.sh.

API shape (same as bin/provision-ruleset.sh):
    1. GET /repos/<owner>/<repo>/rulesets  -> list of {id,name}
    2. Filter by name to discover numeric id for each expected ruleset.
    3. GET /repos/<owner>/<repo>/rulesets/<id>  -> single ruleset detail.

Charge 2 — Compare-keys single source of truth:
    The set of keys compared between desired and live state is loaded
    from ``config/ruleset.compare-keys.json`` at call time.  No literal
    ``_COMPARE_KEYS`` tuple exists in this module body.  The resolved
    ``Path`` is exposed as ``_COMPARE_KEYS_CFG`` so tests can monkeypatch.

Charge 8 — HTTP status from stdout, not stderr:
    Every ``gh api`` call passes ``--include`` so that the HTTP status
    line is the first line of stdout (``HTTP/2.0 <STATUS> <text>``).
    Status is parsed from that line exclusively.  stderr is NEVER
    consulted for HTTP status detection.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from enum import Enum, auto
from pathlib import Path
from typing import cast

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path resolution — config/ lives 3 parents up from this module file:
#   src/baton_harness/chain/  (this module)
#   -> src/baton_harness/
#   -> src/
#   -> <harness root>  (parents[3])
# ---------------------------------------------------------------------------

_HARNESS_ROOT = Path(__file__).resolve().parents[3]
_MAIN_CFG = _HARNESS_ROOT / "config" / "ruleset.main.json"
_FEATURE_CFG = _HARNESS_ROOT / "config" / "ruleset.feature.json"

#: Module-level attribute for the compare-keys config path.
#: Exposed so tests can monkeypatch to a tmp file.
_COMPARE_KEYS_CFG: Path = (
    _HARNESS_ROOT / "config" / "ruleset.compare-keys.json"
)

_MAIN_NAME = "harness-main-no-merge"
_FEATURE_NAME = "harness-feature-daemon-only"


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------


class RulesetConfigError(Exception):
    """Raised when the compare-keys config is missing, malformed, or empty.

    Fail-fast posture: an absent or invalid compare-keys config means the
    harness cannot safely determine ruleset drift, so the preflight gate
    refuses to proceed rather than silently over-approving.
    """


# ---------------------------------------------------------------------------
# Enum
# ---------------------------------------------------------------------------


class RulesetStatus(Enum):
    """The four states the #144 preflight gate consumes.

    Attributes:
        MATCH: Both rulesets are present and content-equal to the
            checked-in JSON (placeholders substituted).
        DRIFT: Both rulesets are present but at least one differs.
        ABSENT: One or both rulesets are missing.
        ERROR: A gh call failed with a non-404 error (network, auth, 5xx).
    """

    MATCH = auto()
    DRIFT = auto()
    ABSENT = auto()
    ERROR = auto()


# ---------------------------------------------------------------------------
# Compare-keys loader (Charge 2)
# ---------------------------------------------------------------------------


def _load_compare_keys() -> tuple[str, ...]:
    """Load and validate the compare-keys list from the shared config.

    Reads ``_COMPARE_KEYS_CFG`` (monkeypatchable in tests) at call time.
    Validates: file exists, content is valid JSON, value is a non-empty
    list of strings.

    Returns:
        A tuple of key names to compare.

    Raises:
        RulesetConfigError: When the file is missing, non-JSON, not a
            list, or an empty list.
    """
    cfg_path: Path = _COMPARE_KEYS_CFG
    if not cfg_path.exists():
        raise RulesetConfigError(f"compare-keys config not found: {cfg_path}")
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RulesetConfigError(
            f"compare-keys config is not valid JSON: {cfg_path}: {exc}"
        ) from exc
    if not isinstance(raw, list) or len(raw) == 0:
        raise RulesetConfigError(
            f"compare-keys config must be a non-empty JSON list: {cfg_path}"
        )
    return tuple(str(k) for k in raw)


# ---------------------------------------------------------------------------
# HTTP-status parsing (Charge 8)
# ---------------------------------------------------------------------------


def _parse_http_status(stdout: str) -> int | None:
    r"""Parse the numeric HTTP status code from the first line of stdout.

    When ``gh api --include`` is used, stdout starts with the HTTP status
    line, e.g. ``HTTP/2.0 200 OK\r\n\r\n<body>``.  This function splits
    on the first newline and extracts the numeric code from the second
    whitespace-delimited token.

    Args:
        stdout: Full stdout string from ``gh api --include``.

    Returns:
        Integer status code (e.g. 200, 404, 500) or ``None`` if the
        first line cannot be parsed.
    """
    first_line = stdout.split("\n", 1)[0].strip()
    parts = first_line.split()
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _extract_body(stdout: str) -> str:
    r"""Extract the response body from ``--include`` stdout.

    The body follows the blank line after the HTTP headers.  Splits on
    the first ``\r\n\r\n`` or ``\n\n`` boundary.

    Args:
        stdout: Full stdout string from ``gh api --include``.

    Returns:
        The body portion of stdout (may be empty string).
    """
    # Try CRLF double-blank first (canonical HTTP), then LF-only.
    for sep in ("\r\n\r\n", "\n\n"):
        if sep in stdout:
            return stdout.split(sep, 1)[1]
    return ""


# ---------------------------------------------------------------------------
# Default runner
# ---------------------------------------------------------------------------


def _default_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Default gh-runner: thin wrapper around ``subprocess.run``.

    Args:
        args: Args to pass to ``gh`` (NOT including ``gh`` itself).

    Returns:
        CompletedProcess with captured stdout/stderr, UTF-8 decoded.
    """
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Config-rendering helpers
# ---------------------------------------------------------------------------


def _render_main_config(admin_role_id: int) -> dict[str, object]:
    """Load the main ruleset config and substitute the admin role placeholder.

    Args:
        admin_role_id: Numeric RepositoryRole id for the admin bypass actor.

    Returns:
        Parsed dict with ``__BH_ADMIN_ROLE_ID__`` placeholder replaced.
    """
    body: dict[str, object] = json.loads(_MAIN_CFG.read_text(encoding="utf-8"))
    body["bypass_actors"][0]["actor_id"] = admin_role_id  # type: ignore[index]
    return body


def _render_feature_config(app_id: str) -> dict[str, object]:
    """Load the feature ruleset config and substitute the app-id placeholder.

    The config stores the placeholder as the JSON string
    ``"__BH_GITHUB_APP_ID__"``; this function replaces it with the
    numeric integer value so comparisons against the live API response
    work correctly.

    Args:
        app_id: Numeric GitHub App ID as a string (e.g. ``"111"``).

    Returns:
        Parsed dict with ``__BH_GITHUB_APP_ID__`` replaced by ``int(app_id)``.
    """
    body: dict[str, object] = json.loads(
        _FEATURE_CFG.read_text(encoding="utf-8")
    )
    body["bypass_actors"][0]["actor_id"] = int(app_id)  # type: ignore[index]
    return body


# ---------------------------------------------------------------------------
# Compare helpers
# ---------------------------------------------------------------------------


def _filter_for_compare(
    ruleset: dict[str, object],
    keys: tuple[str, ...],
) -> dict[str, object]:
    """Extract only the compare-key subset from a ruleset dict.

    Strips ``_comment`` and any other keys not in the allowlist so that
    server-managed fields (timestamps, ids) and operator annotations do
    not participate in drift detection.

    Args:
        ruleset: A parsed ruleset dict (live API or rendered config).
        keys: The tuple of keys to include (loaded from compare-keys config).

    Returns:
        A new dict containing only keys present in ``keys``.
    """
    return {k: ruleset[k] for k in keys if k in ruleset}


def _content_equal(
    desired: dict[str, object],
    current: dict[str, object],
    keys: tuple[str, ...],
) -> bool:
    """Return True when desired and current rulesets agree on all compare keys.

    Both dicts are filtered through ``keys`` before comparison so
    server-managed fields and ``_comment`` annotations are ignored.

    Args:
        desired: The locally-rendered config dict (placeholders substituted).
        current: The live ruleset dict returned by the GitHub API.
        keys: The set of keys to compare (loaded from compare-keys config).

    Returns:
        True when all keys in ``keys`` have equal values in both dicts.
    """
    return _filter_for_compare(desired, keys) == _filter_for_compare(
        current, keys
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ruleset_is_provisioned(
    owner: str,
    repo: str,
    *,
    app_id: str,
    runner: (
        Callable[[list[str]], subprocess.CompletedProcess[str]] | None
    ) = None,
    admin_role_id: int = 5,
) -> RulesetStatus:
    """Inspect both rulesets in the target repo and classify the state.

    Uses the list-then-by-id pattern (B1): first GETs the full ruleset
    list to discover numeric ids, then GETs each ruleset by its numeric
    id.  The ruleset name is NEVER used as a URL path segment after
    ``/rulesets/``.

    Every ``gh api`` call includes ``--include`` (Charge 8) so the HTTP
    status line is the first line of stdout.  Status is read exclusively
    from stdout; stderr is never consulted for HTTP status detection.

    Compare-keys are loaded from ``config/ruleset.compare-keys.json``
    (Charge 2).  The config path is ``_COMPARE_KEYS_CFG`` — monkeypatchable
    in tests.  Missing, malformed, or empty config raises
    ``RulesetConfigError`` (fail-fast posture).

    Args:
        owner: Repository owner (org or user login).
        repo: Repository name (no owner prefix).
        app_id: Numeric GitHub App ID, used to substitute the placeholder
            in the feature ruleset before comparing.  NOT the installation
            id.
        runner: Optional callable that takes a list of ``gh`` args (without
            the leading ``gh``) and returns a CompletedProcess.  Defaults
            to a thin ``subprocess.run(["gh", *args], …)`` wrapper.
        admin_role_id: Numeric RepositoryRole id for the admin bypass on
            main.  Default 5 (community-cited; not officially documented).

    Returns:
        ``RulesetStatus.MATCH`` if both rulesets are present and
        content-equal to ``config/ruleset.*.json`` (placeholders
        substituted); ``RulesetStatus.DRIFT`` if present but differs;
        ``RulesetStatus.ABSENT`` if at least one is missing or returns
        HTTP 404; ``RulesetStatus.ERROR`` if any gh call returns a
        non-404 error or produces an unparseable body.

    Raises:
        RulesetConfigError: When the compare-keys config is missing,
            malformed, or empty.
    """
    # Load compare-keys first (fail-fast on config error).
    compare_keys = _load_compare_keys()

    run = runner or _default_runner

    # Step 1 — LIST all rulesets (--include for HTTP status on stdout).
    list_proc = run(["api", "--include", f"repos/{owner}/{repo}/rulesets"])

    list_status = _parse_http_status(list_proc.stdout)
    list_body_str = _extract_body(list_proc.stdout)

    if list_status is None or not (200 <= list_status < 300):
        _log.warning(
            "ruleset LIST failed (http=%s rc=%d)",
            list_status,
            list_proc.returncode,
        )
        return RulesetStatus.ERROR

    try:
        listed: list[dict[str, object]] = (
            json.loads(list_body_str) if list_body_str.strip() else []
        )
    except json.JSONDecodeError as exc:
        _log.warning("ruleset LIST returned non-JSON body: %s", exc)
        return RulesetStatus.ERROR

    if not isinstance(listed, list):
        _log.warning("ruleset LIST body is not a JSON array")
        return RulesetStatus.ERROR

    # Step 2 — Find numeric ids by name.
    name_to_id: dict[str, int] = {
        str(item["name"]): cast(int, item["id"])
        for item in listed
        if "name" in item and "id" in item
    }

    if _MAIN_NAME not in name_to_id or _FEATURE_NAME not in name_to_id:
        return RulesetStatus.ABSENT

    main_id = name_to_id[_MAIN_NAME]
    feature_id = name_to_id[_FEATURE_NAME]

    # Step 3 — GET each ruleset by its numeric id (B1; --include Charge 8).
    main_proc = run(
        ["api", "--include", f"repos/{owner}/{repo}/rulesets/{main_id}"]
    )
    feature_proc = run(
        [
            "api",
            "--include",
            f"repos/{owner}/{repo}/rulesets/{feature_id}",
        ]
    )

    main_status = _parse_http_status(main_proc.stdout)
    feature_status = _parse_http_status(feature_proc.stdout)

    # 404 → ABSENT (detected from stdout status line, not stderr).
    if main_status == 404 or feature_status == 404:
        return RulesetStatus.ABSENT

    # Non-2xx (other than 404) → ERROR.
    main_ok = main_status is not None and 200 <= main_status < 300
    feature_ok = feature_status is not None and 200 <= feature_status < 300
    if not main_ok or not feature_ok:
        _log.warning(
            "ruleset BY-ID failed — main(http=%s) feature(http=%s)",
            main_status,
            feature_status,
        )
        return RulesetStatus.ERROR

    try:
        current_main: dict[str, object] = json.loads(
            _extract_body(main_proc.stdout)
        )
        current_feature: dict[str, object] = json.loads(
            _extract_body(feature_proc.stdout)
        )
    except json.JSONDecodeError as exc:
        _log.warning("ruleset BY-ID returned non-JSON body: %s", exc)
        return RulesetStatus.ERROR

    # Step 4 — Compare with placeholder-substituted local configs.
    desired_main = _render_main_config(admin_role_id)
    desired_feature = _render_feature_config(app_id)

    if _content_equal(
        desired_main, current_main, compare_keys
    ) and _content_equal(desired_feature, current_feature, compare_keys):
        return RulesetStatus.MATCH
    return RulesetStatus.DRIFT
