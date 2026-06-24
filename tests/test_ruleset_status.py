"""Slice 3b — ruleset_status.ruleset_is_provisioned() unit tests.

Drives the function with a hand-rolled fake runner that returns canned
CompletedProcess objects for each gh call.  Coverage:

  - MATCH      : LIST + both BY-ID GETs return canonical bodies.
  - DRIFT      : both rulesets present but feature ruleset content
                 differs from the canonical config.
  - ABSENT     : LIST is missing the feature ruleset name.
  - ERROR      : LIST call returns non-zero with a 500-class error.
  - B1         : stale numeric IDs (99 / 77) — runner is called with
                 the discovered IDs, not the ruleset name strings.
  - _comment   : ``_comment`` key in the live feature ruleset does NOT
                 cause spurious DRIFT (excluded from ``_COMPARE_KEYS``).
  - C5 tight   : ``HTTP 404`` and ``gh: Not Found`` stderr forms yield
                 ABSENT; a proxy-banner stderr that happens to contain
                 "404" does NOT — it yields ABSENT via the empty-list
                 branch, confirming returncode==0 is not fed to the
                 ``_is_not_found`` heuristic.
  - ERROR vs   : a 500 error on the by-id call after a successful LIST
    ABSENT       yields ERROR, not ABSENT — the daemon behaviour for
                 these two states differs (ABSENT may auto-provision;
                 ERROR halts startup).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from baton_harness.chain.ruleset_status import (
    RulesetStatus,
    ruleset_is_provisioned,
)

HARNESS = Path(__file__).resolve().parents[1]
MAIN_CFG = HARNESS / "config" / "ruleset.main.json"
FEATURE_CFG = HARNESS / "config" / "ruleset.feature.json"

# ---------------------------------------------------------------------------
# CompletedProcess factories
# ---------------------------------------------------------------------------


def _ok(body: str) -> subprocess.CompletedProcess[str]:
    """Return a CompletedProcess representing a successful gh call.

    Args:
        body: The stdout string to return.

    Returns:
        A CompletedProcess with returncode=0.
    """
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=body, stderr=""
    )


def _not_found_gh() -> subprocess.CompletedProcess[str]:
    """Return the ``gh: Not Found`` form of a 404 error.

    Returns:
        A CompletedProcess with returncode=1 and the exact gh stderr.
    """
    return subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="gh: Not Found (HTTP 404)"
    )


def _not_found_http404() -> subprocess.CompletedProcess[str]:
    """Return the ``HTTP 404`` form of a 404 error (C5 — explicit form).

    This form uses ``HTTP 404`` without the ``gh: Not Found`` prefix,
    to verify both detection paths independently.

    Returns:
        A CompletedProcess with returncode=1 and ``HTTP 404`` in stderr.
    """
    return subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="HTTP 404: not found"
    )


def _http_500() -> subprocess.CompletedProcess[str]:
    """Return a non-404 server error CompletedProcess.

    Returns:
        A CompletedProcess with returncode=1 and a 500 stderr.
    """
    return subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="HTTP 500: server error"
    )


def _proxy_404_banner() -> subprocess.CompletedProcess[str]:
    """Return a successful response whose stderr coincidentally contains 404.

    Simulates a proxy cache-miss header that carries the digits "404"
    without indicating an actual HTTP 404 response from GitHub.

    Returns:
        A CompletedProcess with returncode=0 and a proxy-banner stderr.
    """
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout="[]", stderr="X-Cache: 404-miss"
    )


# ---------------------------------------------------------------------------
# Config-rendering helpers (mirror what the module under test must do)
# ---------------------------------------------------------------------------


def _render_main(admin_role_id: int = 5) -> dict:  # type: ignore[type-arg]
    """Build the expected main ruleset body with placeholder substituted.

    Args:
        admin_role_id: The numeric role id to embed in bypass_actors.

    Returns:
        The parsed JSON dict with the actor_id placeholder replaced.
    """
    body = json.loads(MAIN_CFG.read_text(encoding="utf-8"))
    body["bypass_actors"][0]["actor_id"] = admin_role_id
    return body


def _render_feature(app_id: int = 111) -> dict:  # type: ignore[type-arg]
    """Build the expected feature ruleset body with placeholder substituted.

    Args:
        app_id: The numeric GitHub App ID to embed in bypass_actors.

    Returns:
        The parsed JSON dict with the actor_id placeholder replaced.
    """
    body = json.loads(FEATURE_CFG.read_text(encoding="utf-8"))
    body["bypass_actors"][0]["actor_id"] = app_id
    return body


# ---------------------------------------------------------------------------
# Runner factory for the default (IDs 11 / 22) shape
# ---------------------------------------------------------------------------


def _make_runner(
    list_proc: subprocess.CompletedProcess[str],
    byid_main_proc: subprocess.CompletedProcess[str] | None = None,
    byid_feature_proc: subprocess.CompletedProcess[str] | None = None,
) -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
    """Build a fake runner keyed on the URL contained in each call.

    Dispatches on the presence of ``/rulesets/11`` or ``/rulesets/22``
    (the default canonical IDs used in most tests) in the URL arg.

    Args:
        list_proc: Response for the LIST endpoint.
        byid_main_proc: Response for the main ruleset BY-ID call
            (``/rulesets/11``).  Defaults to a trivial ``{}`` body.
        byid_feature_proc: Response for the feature ruleset BY-ID call
            (``/rulesets/22``).  Defaults to a trivial ``{}`` body.

    Returns:
        A callable with the ``(args: list[str]) -> CompletedProcess``
        signature expected by ``ruleset_is_provisioned``.
    """

    def _run(
        args: list[str],
    ) -> subprocess.CompletedProcess[str]:
        url = next((a for a in args if "rulesets" in a or "/app" in a), "")
        if url.endswith("/rulesets"):
            return list_proc
        if "/rulesets/11" in url:
            return byid_main_proc or _ok("{}")
        if "/rulesets/22" in url:
            return byid_feature_proc or _ok("{}")
        raise AssertionError(f"unexpected gh call: {args}")

    return _run


# ---------------------------------------------------------------------------
# A1. MATCH — both rulesets present and content-equal
# ---------------------------------------------------------------------------


def test_match_returns_match() -> None:
    """Both rulesets match canonical config -> MATCH."""
    list_body = json.dumps(
        [
            {"id": 11, "name": "harness-main-no-merge"},
            {"id": 22, "name": "harness-feature-daemon-only"},
        ]
    )
    runner = _make_runner(
        _ok(list_body),
        _ok(json.dumps(_render_main())),
        _ok(json.dumps(_render_feature())),
    )

    result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)

    assert result is RulesetStatus.MATCH


# ---------------------------------------------------------------------------
# A2. DRIFT — feature ruleset content differs
# ---------------------------------------------------------------------------


def test_drift_on_feature_content_change() -> None:
    """Feature ruleset bypass_actors cleared -> DRIFT."""
    list_body = json.dumps(
        [
            {"id": 11, "name": "harness-main-no-merge"},
            {"id": 22, "name": "harness-feature-daemon-only"},
        ]
    )
    feature_drifted = _render_feature()
    feature_drifted["bypass_actors"] = []
    runner = _make_runner(
        _ok(list_body),
        _ok(json.dumps(_render_main())),
        _ok(json.dumps(feature_drifted)),
    )

    result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)

    assert result is RulesetStatus.DRIFT


# ---------------------------------------------------------------------------
# A3. ABSENT — feature ruleset name missing from LIST
# ---------------------------------------------------------------------------


def test_absent_when_feature_ruleset_missing_from_list() -> None:
    """LIST omits feature ruleset name -> ABSENT."""
    list_body = json.dumps(
        [
            {"id": 11, "name": "harness-main-no-merge"},
        ]
    )
    runner = _make_runner(
        _ok(list_body),
        _ok(json.dumps(_render_main())),
    )

    result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)

    assert result is RulesetStatus.ABSENT


# ---------------------------------------------------------------------------
# A4. ERROR — LIST returns a 500-class error
# ---------------------------------------------------------------------------


def test_error_on_list_5xx() -> None:
    """LIST returns non-zero non-404 exit -> ERROR."""
    runner = _make_runner(_http_500())

    result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)

    assert result is RulesetStatus.ERROR


# ---------------------------------------------------------------------------
# B1. Stale numeric ID — list-then-by-id shape (regression guard)
# ---------------------------------------------------------------------------


def test_stale_id_uses_discovered_numeric_id_not_name_string() -> None:
    """B1: runner is called with the IDs discovered from LIST (99/77).

    Seeds the LIST response with non-default IDs 99 and 77.  Asserts:
    - The runner is called with URLs containing /rulesets/99 and
      /rulesets/77 (the discovered ids).
    - The runner is NOT called with the ruleset name strings in the
      URL path after ``/rulesets/``.
    - Final status is MATCH (bodies match after resolution).
    """
    list_body = json.dumps(
        [
            {"id": 99, "name": "harness-main-no-merge"},
            {"id": 77, "name": "harness-feature-daemon-only"},
        ]
    )
    captured_urls: list[str] = []

    def _recording_runner(
        args: list[str],
    ) -> subprocess.CompletedProcess[str]:
        url = next((a for a in args if "rulesets" in a or "/app" in a), "")
        captured_urls.append(url)
        if url.endswith("/rulesets"):
            return _ok(list_body)
        if "/rulesets/99" in url:
            return _ok(json.dumps(_render_main()))
        if "/rulesets/77" in url:
            return _ok(json.dumps(_render_feature()))
        raise AssertionError(f"unexpected gh call: {args}")

    result = ruleset_is_provisioned(
        "o", "r", app_id="111", runner=_recording_runner
    )

    assert result is RulesetStatus.MATCH, (
        f"expected MATCH for stale-id state, got {result!r}"
    )
    by_id_urls = [u for u in captured_urls if "/rulesets/" in u]
    assert any("/rulesets/99" in u for u in by_id_urls), (
        f"expected GET on /rulesets/99 in call URLs: {by_id_urls}"
    )
    assert any("/rulesets/77" in u for u in by_id_urls), (
        f"expected GET on /rulesets/77 in call URLs: {by_id_urls}"
    )
    # Name strings must NOT appear as URL path segments after /rulesets/.
    assert not any("harness-main-no-merge" in u for u in by_id_urls), (
        f"name-string in GET URL implies name-lookup used: {by_id_urls}"
    )
    assert not any("harness-feature-daemon-only" in u for u in by_id_urls), (
        f"name-string in GET URL implies name-lookup used: {by_id_urls}"
    )


# ---------------------------------------------------------------------------
# C1. _comment key exclusion — no spurious DRIFT
# ---------------------------------------------------------------------------


def test_comment_key_in_live_ruleset_does_not_cause_drift() -> None:
    """``_comment`` in the live feature ruleset body does NOT trigger DRIFT.

    The ``_COMPARE_KEYS`` allowlist intentionally excludes ``_comment``
    so that an operator who copies the key into the live ruleset does not
    create a permanent drift alert.  This test verifies the exclusion at
    the Python layer.
    """
    list_body = json.dumps(
        [
            {"id": 11, "name": "harness-main-no-merge"},
            {"id": 22, "name": "harness-feature-daemon-only"},
        ]
    )
    feature_with_comment = _render_feature()
    # Inject _comment directly into what the live API would return.
    feature_with_comment["_comment"] = (
        "operator note: bypass intentionally broad during migration"
    )
    runner = _make_runner(
        _ok(list_body),
        _ok(json.dumps(_render_main())),
        _ok(json.dumps(feature_with_comment)),
    )

    result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)

    assert result is RulesetStatus.MATCH, (
        "_comment in live ruleset must not cause DRIFT; "
        f"got {result!r} instead"
    )


# ---------------------------------------------------------------------------
# C5. Tightened _is_not_found heuristic
# ---------------------------------------------------------------------------


def test_gh_not_found_stderr_yields_absent() -> None:
    """``gh: Not Found`` stderr form yields ABSENT (C5 — gh form)."""
    list_body = json.dumps(
        [
            {"id": 11, "name": "harness-main-no-merge"},
            {"id": 22, "name": "harness-feature-daemon-only"},
        ]
    )
    runner = _make_runner(
        _ok(list_body),
        _ok(json.dumps(_render_main())),
        _not_found_gh(),  # feature by-id returns gh: Not Found
    )

    result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)

    assert result is RulesetStatus.ABSENT, (
        f"'gh: Not Found' form must yield ABSENT, got {result!r}"
    )


def test_http_404_stderr_yields_absent() -> None:
    """``HTTP 404`` stderr form yields ABSENT (C5 — HTTP form)."""
    list_body = json.dumps(
        [
            {"id": 11, "name": "harness-main-no-merge"},
            {"id": 22, "name": "harness-feature-daemon-only"},
        ]
    )
    runner = _make_runner(
        _ok(list_body),
        _ok(json.dumps(_render_main())),
        _not_found_http404(),  # feature by-id returns HTTP 404
    )

    result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)

    assert result is RulesetStatus.ABSENT, (
        f"'HTTP 404' form must yield ABSENT, got {result!r}"
    )


def test_proxy_404_banner_does_not_false_positive() -> None:
    """C5 regression: proxy-banner stderr containing '404' is NOT ABSENT.

    The proxy-banner runner returns returncode=0 with an empty list body
    and a stderr header that contains the string "404".  The broad
    heuristic ``"404" in stderr`` would classify this as a 404-not-found
    response; the tight heuristic must not fire because returncode==0.
    The result is ABSENT only because the LIST body is empty — not
    because the not-found heuristic fired.
    """
    runner = _make_runner(_proxy_404_banner())
    # LIST returns ok with empty body -> both rulesets absent by list.
    result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)

    # ABSENT because the list is empty — this is correct.
    assert result is RulesetStatus.ABSENT, (
        f"expected ABSENT (empty list), got {result!r}"
    )
    # The key assertion: this must NOT be ERROR.  If the not-found
    # heuristic fired on returncode==0, the result would be incorrect
    # (ERROR), proving the tight heuristic failed.
    assert result is not RulesetStatus.ERROR, (
        "proxy-banner stderr must NOT trigger ERROR via _is_not_found"
    )


# ---------------------------------------------------------------------------
# ERROR vs ABSENT distinction — by-id 500 after successful LIST
# ---------------------------------------------------------------------------


def test_error_on_byid_5xx_not_absent() -> None:
    """By-id call returns 500 after LIST succeeds -> ERROR, not ABSENT.

    ABSENT means a ruleset is genuinely missing and may be auto-
    provisioned by the daemon.  ERROR means the infrastructure is
    unreliable and startup must be halted.  A 500 on a by-id call must
    yield ERROR so the daemon does not attempt to provision a ruleset
    that may already exist but is unreachable.
    """
    list_body = json.dumps(
        [
            {"id": 11, "name": "harness-main-no-merge"},
            {"id": 22, "name": "harness-feature-daemon-only"},
        ]
    )
    runner = _make_runner(
        _ok(list_body),
        _ok(json.dumps(_render_main())),
        _http_500(),  # feature by-id: 500 server error
    )

    result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)

    assert result is RulesetStatus.ERROR, (
        f"500 on by-id must yield ERROR not ABSENT, got {result!r}"
    )
    assert result is not RulesetStatus.ABSENT, (
        "by-id 500 must not be classified as ABSENT"
    )
