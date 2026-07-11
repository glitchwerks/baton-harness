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

#206 — App-token-safe per-launch preflight:
    ``ruleset_is_provisioned`` compares ``bypass_actors`` structurally,
    but a GitHub App installation token cannot read that field (only a
    caller with ruleset-write access can) — so calling it per-launch
    with the App's own token silently misreports drift as MATCH or
    ERROR.  ``check_ruleset_signals`` is the App-token-safe replacement:
    it compares only fields the App token CAN read (the app-subset
    structural keys in ``config/ruleset.compare-keys.app.json``, which
    exclude ``bypass_actors``), plus two admin-free signals that speak
    to bypass configuration without reading it directly:

    1. ``current_user_can_bypass`` — GitHub computes this per-requester,
       so the App token sees its OWN bypass verdict.  The expected
       verdict is derived from the checked-in config's ``bypass_actors``
       via ``_expected_bypass_verdict`` (never from ruleset name).
    2. ``updated_at`` — pinned in ``.bh/ruleset-baseline.json`` by
       ``bin/provision-ruleset.sh``'s baseline-capture step.  Any
       mutation invisible to the other two signals (e.g. a third
       bypass actor added by someone else) still bumps this timestamp.

    Fails closed to ``RulesetStatus.NOT_PROVISIONED`` (distinct from
    DRIFT) when no baseline is pinned for the repo, without making any
    ``gh`` call at all.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import cast

from baton_harness.chain.identity import Identity, env_for

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

#: App-token-safe compare-keys subset (#206) — excludes ``bypass_actors``,
#: which an App installation token cannot read.  Used only by
#: ``check_ruleset_signals``; ``ruleset_is_provisioned`` is unaffected.
_COMPARE_KEYS_APP_CFG: Path = (
    _HARNESS_ROOT / "config" / "ruleset.compare-keys.app.json"
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
    """The states the #144 (and #206) preflight gates consume.

    Attributes:
        MATCH: Both rulesets are present and content-equal to the
            checked-in JSON (placeholders substituted).
        DRIFT: Both rulesets are present but at least one differs.
        ABSENT: One or both rulesets are missing.
        ERROR: A gh call failed with a non-404 error (network, auth, 5xx).
        NOT_PROVISIONED: #206 addition.  ``check_ruleset_signals`` has no
            pinned ``.bh/ruleset-baseline.json`` entry for the repo, so it
            cannot safely assert "no drift" (fail-closed).  Distinct from
            DRIFT — this means "never provisioned/pinned", not "drifted
            from a known-good state".  Never returned by
            ``ruleset_is_provisioned``.
    """

    MATCH = auto()
    DRIFT = auto()
    ABSENT = auto()
    ERROR = auto()
    NOT_PROVISIONED = auto()


@dataclass(frozen=True)
class RulesetCheckResult:
    """Result of ``check_ruleset_signals`` — a status plus human detail.

    Attributes:
        status: The overall ``RulesetStatus`` verdict.
        detail: A human-readable explanation.  Populated for DRIFT (names
            the ruleset, the signal, and the expected-vs-live values) and
            for NOT_PROVISIONED/ABSENT/ERROR; ``None`` for MATCH.
    """

    status: RulesetStatus
    detail: str | None = None


# ---------------------------------------------------------------------------
# Compare-keys loader (Charge 2)
# ---------------------------------------------------------------------------


def _load_keys_from_path(cfg_path: Path) -> tuple[str, ...]:
    """Load and validate a compare-keys JSON list from an arbitrary path.

    Shared by ``_load_compare_keys`` (full admin-visible key set) and
    ``_load_app_compare_keys`` (#206 app-subset).  Validates: file
    exists, content is valid JSON, value is a non-empty list of strings.

    Args:
        cfg_path: Path to the compare-keys JSON config file.

    Returns:
        A tuple of key names to compare.

    Raises:
        RulesetConfigError: When the file is missing, non-JSON, not a
            list, or an empty list.
    """
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


def _load_compare_keys() -> tuple[str, ...]:
    """Load and validate the compare-keys list from the shared config.

    Reads ``_COMPARE_KEYS_CFG`` (monkeypatchable in tests) at call time.

    Returns:
        A tuple of key names to compare.

    Raises:
        RulesetConfigError: When the file is missing, non-JSON, not a
            list, or an empty list.
    """
    return _load_keys_from_path(_COMPARE_KEYS_CFG)


def _load_app_compare_keys() -> tuple[str, ...]:
    """Load the App-token-safe compare-keys subset (#206).

    Reads ``_COMPARE_KEYS_APP_CFG`` at call time — the app-subset config
    deliberately excludes ``bypass_actors``, which an App installation
    token cannot read.

    Returns:
        A tuple of key names to compare (structural signal only).

    Raises:
        RulesetConfigError: When the file is missing, non-JSON, not a
            list, or an empty list.
    """
    return _load_keys_from_path(_COMPARE_KEYS_APP_CFG)


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
        env=env_for(Identity.WORKER),
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


#: Documented GitHub defaults for rule ``parameters`` sub-fields, keyed by
#: rule ``type`` then parameter name.  GitHub's live GET echoes these back
#: even when a write omitted them, and omits an entire ``parameters`` block
#: when every parameter in it is already at its default (observed for the
#: ``update`` rule's ``update_allows_fetch_and_merge``).  Used only to
#: decide whether a *desired* value the live body doesn't mention is
#: already satisfied by default — never to silently ignore a live value
#: that conflicts with a desired one.
_RULE_PARAM_DEFAULTS: dict[str, dict[str, object]] = {
    "update": {
        "update_allows_fetch_and_merge": False,
    },
    "pull_request": {
        "required_approving_review_count": 0,
        "dismiss_stale_reviews_on_push": False,
        "require_code_owner_review": False,
        "require_last_push_approval": False,
        "required_review_thread_resolution": False,
        "required_reviewers": [],
        "allowed_merge_methods": ["merge", "squash", "rebase"],
    },
    "required_status_checks": {
        "strict_required_status_checks_policy": False,
        "do_not_enforce_on_create": False,
    },
}


def _strip_comments(value: object) -> object:
    """Recursively remove ``_comment`` keys from nested dicts/lists.

    GitHub's live GET body never carries ``_comment`` (stripped on the
    write side, #203), but the checked-in desired config still carries
    it as an operator annotation — including nested inside a rule's
    ``parameters`` block (see the ``pull_request`` rule in
    ``config/ruleset.main.json``).  Recursing here keeps both sides
    comparable regardless of nesting depth.

    Args:
        value: Any JSON-decoded value (dict, list, or scalar).

    Returns:
        A deep copy of ``value`` with every ``_comment`` key removed
        from every dict at any nesting depth.
    """
    if isinstance(value, dict):
        return {
            k: _strip_comments(v) for k, v in value.items() if k != "_comment"
        }
    if isinstance(value, list):
        return [_strip_comments(v) for v in value]
    return value


def _filter_for_compare(
    ruleset: dict[str, object],
    keys: tuple[str, ...],
) -> dict[str, object]:
    """Extract only the compare-key subset from a ruleset dict.

    Recursively strips ``_comment`` (at any nesting depth) and any other
    keys not in the allowlist so that server-managed fields (timestamps,
    ids) and operator annotations do not participate in drift detection.

    Args:
        ruleset: A parsed ruleset dict (live API or rendered config).
        keys: The tuple of keys to include (loaded from compare-keys config).

    Returns:
        A new dict containing only keys present in ``keys``, with
        ``_comment`` recursively stripped from the retained values.
    """
    stripped = cast("dict[str, object]", _strip_comments(ruleset))
    return {k: stripped[k] for k in keys if k in stripped}


def _param_matches_default(rule_type: str, param: str, value: object) -> bool:
    """Return True when ``value`` equals the documented GitHub default.

    Fails safe toward drift: an unknown ``(rule_type, param)`` pair (no
    recorded default) never counts as a match, so a genuinely unexpected
    desired value with no corresponding live field still reports DRIFT
    rather than being silently waved through.

    Args:
        rule_type: The rule's ``type`` field (e.g. ``"update"``).
        param: The parameter name within the rule's ``parameters`` block.
        value: The desired value to check against the known default.

    Returns:
        True when a documented default is known for ``(rule_type, param)``
        and it equals ``value``; False otherwise.
    """
    defaults = _RULE_PARAM_DEFAULTS.get(rule_type, {})
    return param in defaults and defaults[param] == value


def _rule_params_equal(
    rule_type: str,
    desired_params: dict[str, object],
    current_params: dict[str, object] | None,
) -> bool:
    """Compare one rule's ``parameters`` block, tolerating server defaults.

    Every key ``desired_params`` specifies must be satisfied by
    ``current_params``: either present with an equal value, or absent
    because it is already at its documented GitHub default (covers
    GitHub omitting a rule's entire ``parameters`` block, or omitting
    individual keys within it, when they are already default-valued).
    Keys ``current_params`` carries that ``desired_params`` does not
    mention are ignored outright — those are server-echoed defaults
    (e.g. ``required_reviewers``, ``allowed_merge_methods``) that the
    desired config never asserted an opinion on, so they cannot
    represent a divergence from what was requested.

    Args:
        rule_type: The rule's ``type`` field.
        desired_params: ``parameters`` from the rendered desired config
            (empty dict when the rule has no ``parameters`` key).
        current_params: ``parameters`` from the live ruleset, or
            ``None`` when the live rule omits the key entirely.

    Returns:
        True when every desired parameter is satisfied by ``current``
        (directly or via a known default); False otherwise.
    """
    live = current_params if current_params is not None else {}
    for key, desired_value in desired_params.items():
        if key in live:
            if live[key] != desired_value:
                return False
        elif not _param_matches_default(rule_type, key, desired_value):
            return False
    return True


def _rules_equal(
    desired_rules: list[object],
    current_rules: list[object],
) -> bool:
    """Compare two ``rules`` arrays as a multiset, tolerating defaults.

    GitHub permits repeating the same rule ``type`` more than once in a
    single ruleset (e.g. two ``commit_message_pattern`` restrictions
    with different ``parameters``), so rules are grouped by ``type``
    and compared as a **multiset**: both sides must carry the same
    *count* of each rule type — a type missing on one side, present
    only on the other, or occurring a different number of times, is
    genuine drift (a missing or an unexpectedly-added control), never
    tolerated. Within a type, each desired occurrence is matched
    against a distinct, as-yet-unmatched current occurrence whose
    ``parameters`` agree per ``_rule_params_equal`` (order-independent
    greedy matching); a desired occurrence that finds no unmatched
    current match is drift.

    Args:
        desired_rules: The rendered desired config's ``rules`` list.
        current_rules: The live ruleset's ``rules`` list.

    Returns:
        True when both sides carry the same multiset of rule types and
        every desired occurrence is matched by a distinct current
        occurrence (per ``_rule_params_equal``); False otherwise.
    """
    desired_by_type: dict[str, list[dict[str, object]]] = {}
    for rule in cast("list[dict[str, object]]", desired_rules):
        desired_by_type.setdefault(cast(str, rule["type"]), []).append(rule)

    current_by_type: dict[str, list[dict[str, object]]] = {}
    for rule in cast("list[dict[str, object]]", current_rules):
        current_by_type.setdefault(cast(str, rule["type"]), []).append(rule)

    if desired_by_type.keys() != current_by_type.keys():
        return False

    for rule_type, desired_occurrences in desired_by_type.items():
        current_occurrences = list(current_by_type[rule_type])
        if len(desired_occurrences) != len(current_occurrences):
            return False

        for desired_rule in desired_occurrences:
            desired_params = cast(
                "dict[str, object]", desired_rule.get("parameters", {})
            )
            match_index = None
            for index, current_rule in enumerate(current_occurrences):
                current_params = cast(
                    "dict[str, object] | None",
                    current_rule.get("parameters"),
                )
                if _rule_params_equal(
                    rule_type, desired_params, current_params
                ):
                    match_index = index
                    break
            if match_index is None:
                return False
            # Consume this current occurrence so it cannot be reused to
            # satisfy a different desired occurrence of the same type.
            current_occurrences.pop(match_index)
    return True


def _content_equal(
    desired: dict[str, object],
    current: dict[str, object],
    keys: tuple[str, ...],
) -> bool:
    """Return True when desired and current rulesets agree on all compare keys.

    Both dicts are filtered through ``keys`` before comparison so
    server-managed fields and (recursively) ``_comment`` annotations are
    ignored.  The ``rules`` key, when present in the compare set, is
    compared structurally via ``_rules_equal`` instead of opaque ``==``
    so GitHub's server-echoed rule-parameter defaults don't register as
    drift; every other key is compared by strict equality unchanged.

    Args:
        desired: The locally-rendered config dict (placeholders substituted).
        current: The live ruleset dict returned by the GitHub API.
        keys: The set of keys to compare (loaded from compare-keys config).

    Returns:
        True when all keys in ``keys`` are satisfied in both dicts.
    """
    desired_filtered = _filter_for_compare(desired, keys)
    current_filtered = _filter_for_compare(current, keys)

    if "rules" in desired_filtered or "rules" in current_filtered:
        desired_rules = cast("list[object]", desired_filtered.pop("rules", []))
        current_rules = cast("list[object]", current_filtered.pop("rules", []))
        if not _rules_equal(desired_rules, current_rules):
            return False

    return desired_filtered == current_filtered


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


# ---------------------------------------------------------------------------
# #206 — App-token-safe per-launch preflight
# ---------------------------------------------------------------------------


def _expected_bypass_verdict(
    rendered_config: dict[str, object], app_id: str
) -> str:
    """Derive the expected ``current_user_can_bypass`` verdict for the App.

    Keyed purely by ``bypass_actors`` membership — never by ruleset name.
    A ruleset's expected verdict is ``"always"`` when the App (matched by
    numeric id AND an ``"Integration"`` ``actor_type``) appears in
    ``rendered_config["bypass_actors"]``; ``"never"`` otherwise.

    Args:
        rendered_config: A rendered desired ruleset config (unfiltered —
            still carries ``bypass_actors``).
        app_id: Numeric GitHub App ID as a string.

    Returns:
        ``"always"`` or ``"never"``.
    """
    app_id_int = int(app_id)
    bypass_actors = cast(
        "list[dict[str, object]]", rendered_config.get("bypass_actors", [])
    )
    for actor in bypass_actors:
        if (
            actor.get("actor_type") == "Integration"
            and actor.get("actor_id") == app_id_int
        ):
            return "always"
    return "never"


def _first_structural_diff(
    desired: dict[str, object],
    current: dict[str, object],
    keys: tuple[str, ...],
) -> str | None:
    """Return the first app-subset compare key that differs, or ``None``.

    Mirrors ``_content_equal``'s field semantics (``rules`` compared via
    the multiset-aware ``_rules_equal``; every other key by strict
    equality) but additionally reports WHICH key diverged, for DRIFT
    messaging.

    Args:
        desired: The rendered desired config (unfiltered; may still carry
            ``bypass_actors`` — irrelevant since ``keys`` excludes it).
        current: The live ruleset body from the App-token GET.
        keys: The app-subset compare-keys tuple (excludes bypass_actors).

    Returns:
        The name of the first key (in ``keys`` order) whose value
        differs, or ``None`` when every key agrees.
    """
    desired_filtered = _filter_for_compare(desired, keys)
    current_filtered = _filter_for_compare(current, keys)
    for key in keys:
        if key == "rules":
            if not _rules_equal(
                cast("list[object]", desired_filtered.get("rules", [])),
                cast("list[object]", current_filtered.get("rules", [])),
            ):
                return key
            continue
        if desired_filtered.get(key) != current_filtered.get(key):
            return key
    return None


def _load_baseline_entries(
    baseline_path: Path, owner: str, repo: str
) -> dict[str, dict[str, object]] | None:
    """Load this repo's pinned baseline entries, or ``None`` if unavailable.

    Fail-closed: a missing file, invalid JSON, absent repo key, or a
    non-dict value all return ``None`` so the caller reports
    ``RulesetStatus.NOT_PROVISIONED`` rather than risk comparing against
    a partial or garbled pin.

    Args:
        baseline_path: Path to the ``ruleset-baseline.json`` file.
        owner: Repository owner (org or user login).
        repo: Repository name.

    Returns:
        The ``{ruleset_name: {"ruleset_id": int, "updated_at": str}}``
        mapping pinned for ``owner/repo``, or ``None``.
    """
    if not baseline_path.exists():
        return None
    try:
        raw = json.loads(baseline_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    entries = raw.get(f"{owner}/{repo}")
    if not isinstance(entries, dict):
        return None
    return cast("dict[str, dict[str, object]]", entries)


def _ruleset_signal_drift(
    name: str,
    live: dict[str, object],
    desired: dict[str, object],
    app_keys: tuple[str, ...],
    app_id: str,
    baseline_entry: dict[str, object],
) -> str | None:
    """Check one ruleset's three admin-free signals; return a DRIFT detail.

    Check order: structural (app-subset keys) -> ``current_user_can_bypass``
    crown-jewel -> ``updated_at`` version-pin.  Returns on the first
    signal that disagrees, so a single DRIFT reason is reported per call.

    Args:
        name: The ruleset's checked-in name (for the detail message).
        live: The live App-token GET body (``bypass_actors`` may be
            entirely absent — the App token cannot read it).
        desired: The rendered desired config for this ruleset (unfiltered
            — used for the bypass-verdict derivation, which needs the
            full ``bypass_actors`` list).
        app_keys: The app-subset structural compare-keys tuple.
        app_id: Numeric GitHub App ID as a string.
        baseline_entry: This ruleset's pinned
            ``{"ruleset_id": int, "updated_at": str}``.

    Returns:
        A human-readable DRIFT detail naming the ruleset, the signal, and
        the expected-vs-live values, or ``None`` when all three signals
        agree.
    """
    diff_key = _first_structural_diff(desired, live, app_keys)
    if diff_key is not None:
        desired_filtered = _filter_for_compare(desired, app_keys)
        live_filtered = _filter_for_compare(live, app_keys)
        return (
            f"DRIFT ({name}): structural key {diff_key!r} differs — "
            f"expected {desired_filtered.get(diff_key)!r}, "
            f"live {live_filtered.get(diff_key)!r}"
        )

    expected_verdict = _expected_bypass_verdict(desired, app_id)
    live_verdict = live.get("current_user_can_bypass")
    if live_verdict != expected_verdict:
        return (
            f"DRIFT ({name}): current_user_can_bypass differs — "
            f"expected {expected_verdict!r}, live {live_verdict!r}"
        )

    baseline_updated_at = baseline_entry.get("updated_at")
    live_updated_at = live.get("updated_at")
    if live_updated_at != baseline_updated_at:
        return (
            f"DRIFT ({name}): updated_at differs from the pinned baseline "
            f"— expected {baseline_updated_at!r}, live {live_updated_at!r}"
        )
    return None


def check_ruleset_signals(
    owner: str,
    repo: str,
    *,
    app_id: str,
    runner: (
        Callable[[list[str]], subprocess.CompletedProcess[str]] | None
    ) = None,
    baseline_path: Path | None = None,
    admin_role_id: int = 5,
) -> RulesetCheckResult:
    """App-token-safe per-launch ruleset preflight (#206).

    Replaces ``ruleset_is_provisioned``'s ``bypass_actors`` structural
    compare — unreadable by a GitHub App installation token, since only a
    ruleset-write-capable requester sees that field — with three signals
    the App token CAN read:

    1. Structural compare over ``config/ruleset.compare-keys.app.json``
       (name/target/enforcement/conditions/rules — never bypass_actors).
    2. ``current_user_can_bypass`` crown-jewel check: GitHub computes
       this per-requester, so the App token sees its OWN bypass verdict.
       The expected value comes from ``_expected_bypass_verdict``, keyed
       by the checked-in config's ``bypass_actors`` membership.
    3. ``updated_at`` version-pin: the live timestamp must match the
       value pinned in the baseline file exactly.  Catches mutations
       invisible to signals 1 and 2 — e.g. a third bypass actor added by
       an operator or attacker that the App token cannot see directly.

    Fails closed to ``RulesetStatus.NOT_PROVISIONED`` (distinct from
    DRIFT) when no baseline is pinned for this repo — WITHOUT calling
    ``runner`` at all, since there is nothing to compare against.

    Uses the baseline's pinned ``ruleset_id`` directly for each BY-ID GET
    (no LIST/name-discovery round-trip): the id was already resolved and
    pinned by ``bin/provision-ruleset.sh``'s baseline-capture step.

    Args:
        owner: Repository owner (org or user login).
        repo: Repository name.
        app_id: Numeric GitHub App ID, used to derive the expected
            bypass verdict and substitute the feature ruleset's
            placeholder.  NOT the installation id.
        runner: Optional callable that takes a list of ``gh`` args
            (without the leading ``gh``) and returns a CompletedProcess.
            Defaults to a thin ``subprocess.run(["gh", *args], …)``
            wrapper.
        baseline_path: Path to the pinned ruleset baseline JSON.
            Defaults to ``$BH_PROJECT_ROOT/.bh/ruleset-baseline.json``.
        admin_role_id: Numeric RepositoryRole id used only to render the
            main ruleset's desired config for comparison (mirrors
            ``ruleset_is_provisioned``'s default; irrelevant to the
            bypass-verdict outcome since the admin actor is never an
            ``"Integration"``).

    Returns:
        A ``RulesetCheckResult``: ``MATCH`` when all three signals agree
        for both rulesets; ``DRIFT`` with a per-field ``detail`` message
        on the first disagreement found; ``NOT_PROVISIONED`` when no
        baseline is pinned for this repo; ``ABSENT``/``ERROR`` on a
        ruleset-not-found or failed gh call for a pinned id.
    """
    if baseline_path is None:
        baseline_path = (
            Path(os.environ["BH_PROJECT_ROOT"])
            / ".bh"
            / "ruleset-baseline.json"
        )

    baseline_entries = _load_baseline_entries(baseline_path, owner, repo)
    if baseline_entries is None:
        return RulesetCheckResult(
            status=RulesetStatus.NOT_PROVISIONED,
            detail=(
                f"no ruleset baseline pinned for {owner}/{repo} at "
                f"{baseline_path}; run bin/provision-ruleset.sh first"
            ),
        )

    run = runner or _default_runner
    app_keys = _load_app_compare_keys()

    desired_by_name: dict[str, dict[str, object]] = {
        _MAIN_NAME: _render_main_config(admin_role_id),
        _FEATURE_NAME: _render_feature_config(app_id),
    }

    for name, desired in desired_by_name.items():
        entry = baseline_entries.get(name)
        if not isinstance(entry, dict) or "ruleset_id" not in entry:
            return RulesetCheckResult(
                status=RulesetStatus.NOT_PROVISIONED,
                detail=(
                    f"no baseline entry for ruleset {name!r} in {owner}/{repo}"
                ),
            )
        ruleset_id = entry["ruleset_id"]

        proc = run(
            [
                "api",
                "--include",
                f"repos/{owner}/{repo}/rulesets/{ruleset_id}",
            ]
        )
        status = _parse_http_status(proc.stdout)

        if status == 404:
            return RulesetCheckResult(
                status=RulesetStatus.ABSENT,
                detail=f"ruleset {name!r} (id={ruleset_id}) not found (404)",
            )
        if status is None or not (200 <= status < 300):
            return RulesetCheckResult(
                status=RulesetStatus.ERROR,
                detail=(
                    f"ruleset {name!r} (id={ruleset_id}) GET failed "
                    f"(http={status})"
                ),
            )
        try:
            live: dict[str, object] = json.loads(_extract_body(proc.stdout))
        except json.JSONDecodeError as exc:
            return RulesetCheckResult(
                status=RulesetStatus.ERROR,
                detail=(
                    f"ruleset {name!r} (id={ruleset_id}) returned "
                    f"non-JSON body: {exc}"
                ),
            )

        detail = _ruleset_signal_drift(
            name, live, desired, app_keys, app_id, entry
        )
        if detail is not None:
            return RulesetCheckResult(
                status=RulesetStatus.DRIFT, detail=detail
            )

    return RulesetCheckResult(status=RulesetStatus.MATCH, detail=None)
