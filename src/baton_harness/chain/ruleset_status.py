"""Slice 3b — read-only ruleset status check for the #144 preflight gate.

Returns a four-state enum that a future daemon-startup gate will use to
decide whether the merge boundary is in place before processing any
issues. This module is INSPECTION-ONLY — it never mutates GitHub state.
The provisioning side lives in bin/provision-ruleset.sh.

API shape (same as bin/provision-ruleset.sh):
    1. GET /repos/<owner>/<repo>/rulesets  -> list of {id,name}
    2. Filter by name to discover numeric id for each expected ruleset.
    3. GET /repos/<owner>/<repo>/rulesets/<id>  -> single ruleset detail.

The Rulesets BY-ID endpoint takes a numeric id, NOT a name string
(verified against the live docs on 2026-06-23 — see plan task 3).
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

# Compared keys — the subset of a ruleset object whose drift signals a
# real config divergence (timestamps, server-managed ids, the synthetic
# _comment key, and links are excluded on purpose).
_COMPARE_KEYS = (
    "name",
    "target",
    "enforcement",
    "bypass_actors",
    "conditions",
    "rules",
)

# Path resolution: this module lives at src/baton_harness/chain/ —
# config/ is three parents up from the module file (chain -> baton_harness
# -> src -> harness root).
_HARNESS_ROOT = Path(__file__).resolve().parents[3]
_MAIN_CFG = _HARNESS_ROOT / "config" / "ruleset.main.json"
_FEATURE_CFG = _HARNESS_ROOT / "config" / "ruleset.feature.json"

_MAIN_NAME = "harness-main-no-merge"
_FEATURE_NAME = "harness-feature-daemon-only"


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


def _default_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Default gh-runner: thin wrapper around ``subprocess.run``.

    Args:
        args: Args to pass to ``gh`` (NOT including ``gh`` itself).

    Returns:
        CompletedProcess with captured stdout/stderr.
    """
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _render_main_config(admin_role_id: int) -> dict[str, object]:
    """Load the main ruleset config and substitute the admin role placeholder.

    Args:
        admin_role_id: Numeric RepositoryRole id for the admin bypass actor.

    Returns:
        Parsed dict with the ``__BH_ADMIN_ROLE_ID__`` placeholder replaced
        by ``admin_role_id``.
    """
    body: dict[str, object] = json.loads(_MAIN_CFG.read_text(encoding="utf-8"))
    body["bypass_actors"][0]["actor_id"] = admin_role_id  # type: ignore[index]
    return body


def _render_feature_config(app_id: str) -> dict[str, object]:
    """Load the feature ruleset config and substitute the app-id placeholder.

    The config file stores the placeholder as the JSON string
    ``"__BH_GITHUB_APP_ID__"``; this function replaces it with the
    numeric integer value so comparisons against the live API response
    work correctly.

    Args:
        app_id: Numeric GitHub App ID as a string (e.g. ``"111"``).

    Returns:
        Parsed dict with the ``__BH_GITHUB_APP_ID__`` placeholder replaced
        by ``int(app_id)``.
    """
    body: dict[str, object] = json.loads(
        _FEATURE_CFG.read_text(encoding="utf-8")
    )
    body["bypass_actors"][0]["actor_id"] = int(app_id)  # type: ignore[index]
    return body


def _is_not_found(proc: subprocess.CompletedProcess[str]) -> bool:
    """Tight 404 detector (C5).

    Only matches the exact stderr forms ``gh api`` itself emits — not the
    looser ``"404" in stderr`` heuristic, which false-positives on proxy
    headers and cache banners that happen to contain the digits 404.

    A ``returncode == 0`` response is NEVER a not-found, regardless of
    what appears in stderr.

    Args:
        proc: The CompletedProcess to inspect.

    Returns:
        True only when ``returncode != 0`` AND stderr contains
        ``"HTTP 404"`` or ``"gh: Not Found"``.
    """
    if proc.returncode == 0:
        return False
    return "HTTP 404" in proc.stderr or "gh: Not Found" in proc.stderr


def _is_error(proc: subprocess.CompletedProcess[str]) -> bool:
    """Return True when the call failed with a non-404 error.

    Args:
        proc: The CompletedProcess to inspect.

    Returns:
        True when ``returncode != 0`` and the failure is NOT a recognised
        404 not-found response.
    """
    return proc.returncode != 0 and not _is_not_found(proc)


def _filter_for_compare(ruleset: dict[str, object]) -> dict[str, object]:
    """Extract only the ``_COMPARE_KEYS`` subset from a ruleset dict.

    Strips ``_comment`` and any other keys not in the allowlist so that
    server-managed fields (timestamps, ids) and operator annotations do
    not participate in drift detection.

    Args:
        ruleset: A parsed ruleset dict (from the live API or rendered config).

    Returns:
        A new dict containing only the keys present in ``_COMPARE_KEYS``.
    """
    return {k: ruleset[k] for k in _COMPARE_KEYS if k in ruleset}


def _content_equal(
    desired: dict[str, object],
    current: dict[str, object],
) -> bool:
    """Return True when desired and current rulesets agree on all compare keys.

    Both dicts are filtered through ``_COMPARE_KEYS`` before comparison so
    server-managed fields and ``_comment`` annotations are ignored.

    Args:
        desired: The locally-rendered config dict (placeholders substituted).
        current: The live ruleset dict returned by the GitHub API.

    Returns:
        True when all keys in ``_COMPARE_KEYS`` have equal values in both
        dicts (missing keys on either side count as ``None``).
    """
    desired_filtered = _filter_for_compare(desired)
    current_filtered = _filter_for_compare(current)
    return desired_filtered == current_filtered


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
    list to discover numeric ids, then GETs each ruleset by its numeric id.
    The ruleset name is NEVER used as a URL path segment after ``/rulesets/``.

    Args:
        owner: Repository owner (org or user login).
        repo: Repository name (no owner prefix).
        app_id: Numeric GitHub App ID, used to substitute the placeholder
            in the feature ruleset before comparing.  NOT the installation
            id.
        runner: Optional callable that takes a list of ``gh`` args (without
            the leading ``gh``) and returns a CompletedProcess.  Defaults
            to a thin ``subprocess.run(["gh", *args], …)`` wrapper.  Tests
            inject a fake runner; #156 (when it lands) can swap in
            GhRunner.
        admin_role_id: Numeric RepositoryRole id for the admin bypass on
            main.  Default 5 (community-cited; #157 plan flags it as
            unverified against official docs).

    Returns:
        ``RulesetStatus.MATCH`` if both rulesets are present and
        content-equal to ``config/ruleset.*.json``;
        ``RulesetStatus.DRIFT`` if both present but content differs;
        ``RulesetStatus.ABSENT`` if at least one is missing from the list
        or returns a 404 on the by-id call;
        ``RulesetStatus.ERROR`` if any gh call returns a non-404 error.
    """
    run = runner or _default_runner

    # Step 1 — LIST all rulesets to discover numeric ids.
    list_proc = run(["api", f"repos/{owner}/{repo}/rulesets"])
    if _is_error(list_proc):
        _log.warning(
            "ruleset LIST failed (rc=%d): %s",
            list_proc.returncode,
            list_proc.stderr[:200],
        )
        return RulesetStatus.ERROR

    try:
        listed: list[dict[str, object]] = (
            json.loads(list_proc.stdout) if list_proc.stdout.strip() else []
        )
    except json.JSONDecodeError as exc:
        _log.warning("ruleset LIST returned non-JSON body: %s", exc)
        return RulesetStatus.ERROR

    # Step 2 — Find numeric ids by name.
    # item["id"] is a JSON number (int at runtime); cast to satisfy mypy.
    name_to_id: dict[str, int] = {
        str(item["name"]): cast(int, item["id"])
        for item in listed
        if "name" in item and "id" in item
    }

    if _MAIN_NAME not in name_to_id or _FEATURE_NAME not in name_to_id:
        return RulesetStatus.ABSENT

    main_id = name_to_id[_MAIN_NAME]
    feature_id = name_to_id[_FEATURE_NAME]

    # Step 3 — GET each ruleset by its numeric id (B1: never by name).
    main_proc = run(["api", f"repos/{owner}/{repo}/rulesets/{main_id}"])
    feature_proc = run(["api", f"repos/{owner}/{repo}/rulesets/{feature_id}"])

    if _is_not_found(main_proc) or _is_not_found(feature_proc):
        return RulesetStatus.ABSENT
    if _is_error(main_proc) or _is_error(feature_proc):
        _log.warning(
            "ruleset BY-ID failed — main(rc=%d) feature(rc=%d)",
            main_proc.returncode,
            feature_proc.returncode,
        )
        return RulesetStatus.ERROR

    try:
        current_main: dict[str, object] = json.loads(main_proc.stdout)
        current_feature: dict[str, object] = json.loads(feature_proc.stdout)
    except json.JSONDecodeError as exc:
        _log.warning("ruleset BY-ID returned non-JSON body: %s", exc)
        return RulesetStatus.ERROR

    # Step 4 — Compare with placeholder-substituted local configs.
    desired_main = _render_main_config(admin_role_id)
    desired_feature = _render_feature_config(app_id)

    if _content_equal(desired_main, current_main) and _content_equal(
        desired_feature, current_feature
    ):
        return RulesetStatus.MATCH
    return RulesetStatus.DRIFT
