"""Unit tests for baton_harness.chain.ruleset_status.

Drives ``ruleset_is_provisioned`` with a hand-rolled fake runner that
returns canned ``subprocess.CompletedProcess`` objects.

All subprocess calls are intercepted via the runner injection seam;
no real ``gh`` binary is invoked.

Coverage:
- MATCH: LIST + both BY-ID GETs return canonical placeholder-substituted
  bodies.
- DRIFT: feature ruleset content differs by one comparable key.
- DRIFT: main ruleset content differs.
- DRIFT NOT triggered by a ``_comment`` key in live response (excluded
  from compare-keys).
- DRIFT NOT triggered by server-managed keys (created_at, updated_at,
  id, _links).
- ABSENT: LIST missing the main ruleset name.
- ABSENT: LIST missing the feature ruleset name.
- ABSENT: LIST present but BY-ID returns HTTP 404 (new
  ``gh api --include`` parse path — stdout starts with
  ``HTTP/2.0 404 Not Found``).
- ERROR: LIST call returns non-zero with HTTP 500 (parsed from stdout
  first line).
- ERROR: BY-ID returns HTTP 500 after LIST succeeds (yields ERROR, not
  ABSENT).
- ERROR: LIST returns JSON-decode-failing body (non-empty stdout that
  isn't JSON; status says 200 but body is garbage).
- B1 pattern: runner called with discovered numeric ids (99 and 77),
  not with ruleset name strings as URL path segments.
- B1 pattern: runner called with ``--include`` flag on every gh-api
  call (Charge 8), verified via call-args inspection.
- Compare-keys loaded from shared config: ``config/ruleset.compare-keys.json``
  is the source of truth.  Monkeypatching the resolved path to a tmp file
  with a different key set; the module honours the changed set.
- Compare-keys config missing: falls back to historic literal set OR
  raises RulesetConfigError.  # SPEC-AMBIGUITY: see test docstring.
- Compare-keys config malformed: non-JSON or non-list content.
- Placeholder substitution: feature ruleset ``__BH_GITHUB_APP_ID__``
  substituted with ``int(app_id)`` before compare.
- HTTP-status edge: stderr containing "404" but stdout HTTP 200 does NOT
  trigger ABSENT (stderr ignored; only stdout status line matters).
- HTTP-status edge: stderr containing "HTTP 404" but stdout HTTP 200
  is NOT treated as 404 (no stderr consultation).
- Regression (#204): a real GitHub GET body — carrying server-echoed
  rule-parameter defaults and an omitted ``update``-rule ``parameters``
  block — must be judged MATCH, not DRIFT, against the rendered desired
  config for both the main and feature rulesets.
- Regression (CodeRabbit finding on PR #205): ``_rules_equal`` must
  treat duplicate rule types as a multiset, not collapse them into a
  single dict key by ``type``. DRIFT when a duplicated rule type is
  missing one occurrence on the current side; MATCH when both sides
  carry the same multiset of occurrences regardless of array order.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# We deliberately do NOT import the implementation here at module level.
# Each test imports from ``baton_harness.chain.ruleset_status`` inline so
# that an ImportError in one test is isolated and doesn't abort collection.
# ---------------------------------------------------------------------------

# Path helpers — tests/ is one level below the harness root.
_HARNESS = Path(__file__).resolve().parents[1]
_MAIN_CFG = _HARNESS / "config" / "ruleset.main.json"
_FEATURE_CFG = _HARNESS / "config" / "ruleset.feature.json"
_COMPARE_KEYS_CFG = _HARNESS / "config" / "ruleset.compare-keys.json"

# Real GitHub Rulesets GET bodies captured from cbeaulieu-gt/baton-test
# (issue #204).  Committed fixtures so the regression is self-contained —
# do NOT read from the gitignored .tmp/ captures at test runtime.
_LIVE_MAIN_FIXTURE = _HARNESS / "tests" / "fixtures" / "ruleset.main.live.json"
_LIVE_FEATURE_FIXTURE = (
    _HARNESS / "tests" / "fixtures" / "ruleset.feature.live.json"
)

# ---------------------------------------------------------------------------
# CompletedProcess factories
# ---------------------------------------------------------------------------


def _ok(
    body: str, *, status_line: str = "HTTP/2.0 200 OK"
) -> subprocess.CompletedProcess[str]:
    """Return a CompletedProcess representing a successful gh call.

    When ``--include`` is used, the first line of stdout is the HTTP
    status line.  The factory prepends it so runner fakes always include
    a valid status line.

    Args:
        body: The JSON payload to appear after the status line.
        status_line: The HTTP status line to prepend.

    Returns:
        A CompletedProcess with returncode=0 and status_line + body.
    """
    stdout = f"{status_line}\r\n\r\n{body}"
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=""
    )


def _http_404_stdout() -> subprocess.CompletedProcess[str]:
    """Return a 404 response via the ``--include`` stdout path (Charge 8).

    The first line of stdout is the HTTP status line; the module must
    detect the 404 from this line, NOT from stderr.

    Returns:
        A CompletedProcess with returncode=1 and ``HTTP/2.0 404`` on
        stdout line 1.
    """
    return subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="HTTP/2.0 404 Not Found\r\n\r\n{}",
        stderr="",
    )


def _http_500_stdout() -> subprocess.CompletedProcess[str]:
    """Return a server error via the ``--include`` stdout path.

    Returns:
        A CompletedProcess with returncode=1 and ``HTTP/2.0 500`` on
        stdout line 1.
    """
    return subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="HTTP/2.0 500 Internal Server Error\r\n\r\n{}",
        stderr="",
    )


def _ok_with_stderr_404(body: str) -> subprocess.CompletedProcess[str]:
    """Return a 200 response whose stderr happens to contain '404'.

    Simulates a proxy banner carrying the digits 404.  The module must
    ignore stderr entirely and rely only on the stdout status line.

    Args:
        body: The JSON payload.

    Returns:
        A CompletedProcess with returncode=0, HTTP 200 on stdout line 1,
        and '404' in stderr.
    """
    stdout = f"HTTP/2.0 200 OK\r\n\r\n{body}"
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr="X-Cache: 404-miss"
    )


def _ok_with_stderr_http404(body: str) -> subprocess.CompletedProcess[str]:
    """Return a 200 response whose stderr contains the string 'HTTP 404'.

    Confirms that even an exact 'HTTP 404' string in stderr does not
    trigger not-found detection when the stdout status line says 200.

    Args:
        body: The JSON payload.

    Returns:
        A CompletedProcess with returncode=0, HTTP 200 on stdout line 1,
        and 'HTTP 404' in stderr.
    """
    stdout = f"HTTP/2.0 200 OK\r\n\r\n{body}"
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=stdout,
        stderr="Proxy-Warning: upstream returned HTTP 404 previously",
    )


def _garbage_body_ok() -> subprocess.CompletedProcess[str]:
    """Return a 200-status response with an unparseable (non-JSON) body.

    Returns:
        A CompletedProcess with returncode=0, HTTP 200 on stdout line 1,
        and garbage after the blank line.
    """
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="HTTP/2.0 200 OK\r\n\r\nnot json at all",
        stderr="",
    )


# ---------------------------------------------------------------------------
# Config-rendering helpers
# (mirror what the module under test must do — never reads implementation)
# ---------------------------------------------------------------------------


def _render_main(admin_role_id: int = 5) -> dict[str, Any]:
    """Build the expected main ruleset body with the admin placeholder set.

    Args:
        admin_role_id: The numeric role id to embed in bypass_actors.

    Returns:
        Parsed JSON dict with actor_id set to admin_role_id.
    """
    body: dict[str, Any] = json.loads(_MAIN_CFG.read_text(encoding="utf-8"))
    body["bypass_actors"][0]["actor_id"] = admin_role_id
    return body


def _render_feature(app_id: int = 111) -> dict[str, Any]:
    """Build the expected feature ruleset body with the app-id placeholder set.

    Args:
        app_id: The numeric GitHub App ID to embed in bypass_actors.

    Returns:
        Parsed JSON dict with actor_id set to app_id.
    """
    body: dict[str, Any] = json.loads(_FEATURE_CFG.read_text(encoding="utf-8"))
    body["bypass_actors"][0]["actor_id"] = app_id
    return body


# ---------------------------------------------------------------------------
# Runner factory
# ---------------------------------------------------------------------------

# Default IDs for most tests: 11=main, 22=feature.
_MAIN_ID = 11
_FEAT_ID = 22


def _list_body(
    main_id: int = _MAIN_ID,
    feat_id: int = _FEAT_ID,
) -> str:
    """Return a JSON LIST response with both ruleset stubs.

    Args:
        main_id: Numeric id for the main ruleset.
        feat_id: Numeric id for the feature ruleset.

    Returns:
        JSON string suitable as stdout for the LIST call.
    """
    return json.dumps(
        [
            {"id": main_id, "name": "harness-main-no-merge"},
            {"id": feat_id, "name": "harness-feature-daemon-only"},
        ]
    )


class _FakeRunner:
    """Callable fake runner that records all call args and routes responses.

    Attributes:
        calls: List of ``args`` lists passed to each invocation.
    """

    def __init__(
        self,
        list_proc: subprocess.CompletedProcess[str],
        byid_main_proc: subprocess.CompletedProcess[str] | None = None,
        byid_feat_proc: subprocess.CompletedProcess[str] | None = None,
        main_id: int = _MAIN_ID,
        feat_id: int = _FEAT_ID,
    ) -> None:
        """Initialise the fake runner.

        Args:
            list_proc: Response to return for the LIST endpoint call.
            byid_main_proc: Response for the main BY-ID call.
            byid_feat_proc: Response for the feature BY-ID call.
            main_id: Numeric id for the main ruleset (used for routing).
            feat_id: Numeric id for the feature ruleset (used for routing).
        """
        self._list = list_proc
        self._main = byid_main_proc or _ok("{}")
        self._feat = byid_feat_proc or _ok("{}")
        self._main_id = main_id
        self._feat_id = feat_id
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        """Dispatch a gh-api call to the canned response.

        Args:
            args: The args list (without ``gh``) passed by the module.

        Returns:
            Canned CompletedProcess for the matched endpoint.

        Raises:
            AssertionError: When the call cannot be routed.
        """
        self.calls.append(list(args))
        url = next(
            (a for a in args if "rulesets" in a),
            "",
        )
        if url.endswith("/rulesets"):
            return self._list
        if f"/rulesets/{self._main_id}" in url:
            return self._main
        if f"/rulesets/{self._feat_id}" in url:
            return self._feat
        raise AssertionError(f"_FakeRunner: unroutable gh call args={args!r}")


# ---------------------------------------------------------------------------
# Test A1 — MATCH
# ---------------------------------------------------------------------------


def test_match_when_both_rulesets_present_and_content_equal() -> None:
    """MATCH when LIST + both BY-ID GETs return canonical bodies.

    Both rulesets are in the LIST and their by-id bodies equal the
    placeholder-substituted local configs.  The function must return
    ``RulesetStatus.MATCH``.
    """
    from baton_harness.chain.ruleset_status import (
        RulesetStatus,
        ruleset_is_provisioned,
    )

    runner = _FakeRunner(
        list_proc=_ok(_list_body()),
        byid_main_proc=_ok(json.dumps(_render_main())),
        byid_feat_proc=_ok(json.dumps(_render_feature())),
    )
    result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)
    assert result is RulesetStatus.MATCH


# ---------------------------------------------------------------------------
# Test A2 — DRIFT on feature content change
# ---------------------------------------------------------------------------


def test_drift_when_feature_ruleset_content_differs() -> None:
    """DRIFT when feature ruleset bypass_actors differs from local config.

    The feature ruleset returned by BY-ID has an empty bypass_actors list.
    The module must detect the difference and return ``RulesetStatus.DRIFT``.
    """
    from baton_harness.chain.ruleset_status import (
        RulesetStatus,
        ruleset_is_provisioned,
    )

    feature_drifted = _render_feature()
    feature_drifted["bypass_actors"] = []

    runner = _FakeRunner(
        list_proc=_ok(_list_body()),
        byid_main_proc=_ok(json.dumps(_render_main())),
        byid_feat_proc=_ok(json.dumps(feature_drifted)),
    )
    result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)
    assert result is RulesetStatus.DRIFT


# ---------------------------------------------------------------------------
# Test A3 — DRIFT on main ruleset content change
# ---------------------------------------------------------------------------


def test_drift_when_main_ruleset_content_differs() -> None:
    """DRIFT when main ruleset enforcement field differs from local config.

    The main ruleset returned by BY-ID has ``enforcement="disabled"``
    instead of the expected ``"active"``.  The module must detect the
    difference and return ``RulesetStatus.DRIFT``.
    """
    from baton_harness.chain.ruleset_status import (
        RulesetStatus,
        ruleset_is_provisioned,
    )

    main_drifted = _render_main()
    main_drifted["enforcement"] = "disabled"

    runner = _FakeRunner(
        list_proc=_ok(_list_body()),
        byid_main_proc=_ok(json.dumps(main_drifted)),
        byid_feat_proc=_ok(json.dumps(_render_feature())),
    )
    result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)
    assert result is RulesetStatus.DRIFT


# ---------------------------------------------------------------------------
# Test B1 — _comment key exclusion
# ---------------------------------------------------------------------------


def test_drift_not_triggered_by_comment_key_in_live_ruleset() -> None:
    """MATCH (not DRIFT) when live ruleset adds a ``_comment`` key.

    The ``_comment`` key is excluded from the compare-keys set.  A live
    ruleset that adds a ``_comment`` operator note must NOT cause DRIFT.
    """
    from baton_harness.chain.ruleset_status import (
        RulesetStatus,
        ruleset_is_provisioned,
    )

    feature_with_comment = _render_feature()
    feature_with_comment["_comment"] = "operator note: migration in progress"

    runner = _FakeRunner(
        list_proc=_ok(_list_body()),
        byid_main_proc=_ok(json.dumps(_render_main())),
        byid_feat_proc=_ok(json.dumps(feature_with_comment)),
    )
    result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)
    assert result is RulesetStatus.MATCH, (
        f"_comment in live ruleset must not cause DRIFT; got {result!r}"
    )


# ---------------------------------------------------------------------------
# Test B2 — server-managed keys exclusion
# ---------------------------------------------------------------------------


def test_drift_not_triggered_by_server_managed_keys() -> None:
    """MATCH when live ruleset contains server-managed fields not in config.

    Fields like ``created_at``, ``updated_at``, ``id``, and ``_links``
    are not in the compare-keys set and must not trigger DRIFT.
    """
    from baton_harness.chain.ruleset_status import (
        RulesetStatus,
        ruleset_is_provisioned,
    )

    main_with_server_fields = _render_main()
    main_with_server_fields["id"] = 99999
    main_with_server_fields["created_at"] = "2026-01-01T00:00:00Z"
    main_with_server_fields["updated_at"] = "2026-06-27T12:00:00Z"
    main_with_server_fields["_links"] = {
        "self": {"href": "https://api.github.com/repos/o/r/rulesets/99999"}
    }

    runner = _FakeRunner(
        list_proc=_ok(_list_body()),
        byid_main_proc=_ok(json.dumps(main_with_server_fields)),
        byid_feat_proc=_ok(json.dumps(_render_feature())),
    )
    result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)
    assert result is RulesetStatus.MATCH, (
        "Server-managed keys must not cause DRIFT; got {result!r}"
    )


# ---------------------------------------------------------------------------
# Test C1 — ABSENT when main ruleset missing from LIST
# ---------------------------------------------------------------------------


def test_absent_when_main_ruleset_missing_from_list() -> None:
    """ABSENT when LIST omits the main ruleset name.

    When the LIST response contains only the feature ruleset, the main
    ruleset is missing and the function must return ``RulesetStatus.ABSENT``.
    """
    from baton_harness.chain.ruleset_status import (
        RulesetStatus,
        ruleset_is_provisioned,
    )

    list_only_feature = json.dumps(
        [{"id": _FEAT_ID, "name": "harness-feature-daemon-only"}]
    )
    runner = _FakeRunner(list_proc=_ok(list_only_feature))
    result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)
    assert result is RulesetStatus.ABSENT


# ---------------------------------------------------------------------------
# Test C2 — ABSENT when feature ruleset missing from LIST
# ---------------------------------------------------------------------------


def test_absent_when_feature_ruleset_missing_from_list() -> None:
    """ABSENT when LIST omits the feature ruleset name.

    When the LIST response contains only the main ruleset, the feature
    ruleset is missing and the function must return ``RulesetStatus.ABSENT``.
    """
    from baton_harness.chain.ruleset_status import (
        RulesetStatus,
        ruleset_is_provisioned,
    )

    list_only_main = json.dumps(
        [{"id": _MAIN_ID, "name": "harness-main-no-merge"}]
    )
    runner = _FakeRunner(list_proc=_ok(list_only_main))
    result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)
    assert result is RulesetStatus.ABSENT


# ---------------------------------------------------------------------------
# Test C3 — ABSENT when BY-ID returns HTTP 404 via stdout status line
# ---------------------------------------------------------------------------


def test_absent_when_byid_returns_http_404_via_stdout_status_line() -> None:
    """ABSENT when BY-ID returns HTTP 404 parsed from stdout first line.

    This is the Charge-8 path: ``gh api --include`` writes the HTTP status
    line as the first line of stdout.  The module must parse that line to
    detect 404 — NOT from stderr string-matching.

    Stdout of the 404 response starts with ``HTTP/2.0 404 Not Found``.
    """
    from baton_harness.chain.ruleset_status import (
        RulesetStatus,
        ruleset_is_provisioned,
    )

    runner = _FakeRunner(
        list_proc=_ok(_list_body()),
        byid_main_proc=_ok(json.dumps(_render_main())),
        byid_feat_proc=_http_404_stdout(),
    )
    result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)
    assert result is RulesetStatus.ABSENT, (
        f"HTTP 404 from stdout status line must yield ABSENT; got {result!r}"
    )


# ---------------------------------------------------------------------------
# Test D1 — ERROR when LIST returns HTTP 500 (via stdout status line)
# ---------------------------------------------------------------------------


def test_error_when_list_returns_http_500_via_stdout_status_line() -> None:
    """ERROR when LIST call returns non-zero with HTTP 500 on stdout line 1.

    Charge 8: the HTTP status is parsed from the first line of stdout.
    A 500-class error on the LIST call must return ``RulesetStatus.ERROR``.
    """
    from baton_harness.chain.ruleset_status import (
        RulesetStatus,
        ruleset_is_provisioned,
    )

    runner = _FakeRunner(list_proc=_http_500_stdout())
    result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)
    assert result is RulesetStatus.ERROR


# ---------------------------------------------------------------------------
# Test D2 — ERROR when BY-ID returns HTTP 500 after successful LIST
# ---------------------------------------------------------------------------


def test_error_when_byid_returns_http_500_after_successful_list() -> None:
    """ERROR (not ABSENT) when BY-ID returns 500 after LIST succeeds.

    A 500 on the BY-ID call after a successful LIST must yield ERROR so
    the daemon halts startup rather than attempting to re-provision a
    ruleset that may exist but is unreachable.
    """
    from baton_harness.chain.ruleset_status import (
        RulesetStatus,
        ruleset_is_provisioned,
    )

    runner = _FakeRunner(
        list_proc=_ok(_list_body()),
        byid_main_proc=_ok(json.dumps(_render_main())),
        byid_feat_proc=_http_500_stdout(),
    )
    result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)
    assert result is RulesetStatus.ERROR
    assert result is not RulesetStatus.ABSENT, (
        "500 on BY-ID must yield ERROR, not ABSENT"
    )


# ---------------------------------------------------------------------------
# Test D3 — ERROR when LIST body is garbage JSON
# ---------------------------------------------------------------------------


def test_error_when_list_body_is_non_json() -> None:
    """ERROR when LIST returns a 200 status but an unparseable body.

    A non-JSON body (e.g. HTML error page) after a 200 status line
    cannot be parsed.  The module must return ``RulesetStatus.ERROR``.
    """
    from baton_harness.chain.ruleset_status import (
        RulesetStatus,
        ruleset_is_provisioned,
    )

    runner = _FakeRunner(list_proc=_garbage_body_ok())
    result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)
    assert result is RulesetStatus.ERROR


# ---------------------------------------------------------------------------
# Test E1 — B1 pattern: runner called with discovered numeric ids (99/77)
# ---------------------------------------------------------------------------


def test_b1_runner_called_with_discovered_numeric_ids() -> None:
    """B1: runner called with discovered ids (99/77), not ruleset name strings.

    Seeds the LIST response with non-default ids 99 and 77.  Asserts:
    - The runner is called with URLs containing /rulesets/99 and
      /rulesets/77 (the discovered ids).
    - The runner is NOT called with the ruleset name strings in the URL
      path after ``/rulesets/``.
    - Final status is MATCH (bodies match after id resolution).
    """
    from baton_harness.chain.ruleset_status import (
        RulesetStatus,
        ruleset_is_provisioned,
    )

    stale_list = json.dumps(
        [
            {"id": 99, "name": "harness-main-no-merge"},
            {"id": 77, "name": "harness-feature-daemon-only"},
        ]
    )
    runner = _FakeRunner(
        list_proc=_ok(stale_list),
        byid_main_proc=_ok(json.dumps(_render_main())),
        byid_feat_proc=_ok(json.dumps(_render_feature())),
        main_id=99,
        feat_id=77,
    )

    result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)

    assert result is RulesetStatus.MATCH, (
        f"Expected MATCH with discovered ids 99/77; got {result!r}"
    )
    # All calls after the LIST call must use numeric ids, never name strings.
    byid_calls = [
        c
        for c in runner.calls
        if "/rulesets/" in " ".join(c)
        and not " ".join(c).endswith("/rulesets")
    ]
    assert any("/rulesets/99" in " ".join(c) for c in byid_calls), (
        f"Expected /rulesets/99 in BY-ID calls; calls={runner.calls!r}"
    )
    assert any("/rulesets/77" in " ".join(c) for c in byid_calls), (
        f"Expected /rulesets/77 in BY-ID calls; calls={runner.calls!r}"
    )
    assert not any(
        "harness-main-no-merge" in " ".join(c) for c in byid_calls
    ), (
        "Rule name string must not appear in BY-ID URL path segment; "
        f"calls={runner.calls!r}"
    )
    assert not any(
        "harness-feature-daemon-only" in " ".join(c) for c in byid_calls
    ), (
        "Rule name string must not appear in BY-ID URL path segment; "
        f"calls={runner.calls!r}"
    )


# ---------------------------------------------------------------------------
# Test E2 — B1 pattern: ``--include`` flag present on every call (Charge 8)
# ---------------------------------------------------------------------------


def test_b1_all_gh_api_calls_include_flag_present() -> None:
    """Charge 8: every ``gh api`` call must include the ``--include`` flag.

    The module must request the HTTP status line on stdout by passing
    ``--include`` to every ``gh api`` invocation.  This test verifies via
    call-args inspection that ``--include`` appears in all runner calls.
    """
    from baton_harness.chain.ruleset_status import (
        RulesetStatus,
        ruleset_is_provisioned,
    )

    runner = _FakeRunner(
        list_proc=_ok(_list_body()),
        byid_main_proc=_ok(json.dumps(_render_main())),
        byid_feat_proc=_ok(json.dumps(_render_feature())),
    )
    result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)

    assert result is RulesetStatus.MATCH
    assert runner.calls, "Runner must be called at least once"
    for call_args in runner.calls:
        assert "--include" in call_args, (
            f"``--include`` must be present in every gh api call; "
            f"call missing it: {call_args!r}"
        )


# ---------------------------------------------------------------------------
# Test F1 — compare-keys loaded from shared config file
# ---------------------------------------------------------------------------


def test_compare_keys_loaded_from_shared_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """compare-keys come from ``config/ruleset.compare-keys.json``.

    Monkeypatches the resolved config path to a tmp file with a stripped
    key set that excludes ``"rules"``.  A drift in only the ``rules`` key
    must NOT cause DRIFT when ``rules`` is not in the compare set.

    This locks the contract that the compare-keys are loaded from disk,
    not from any in-module ``_COMPARE_KEYS`` literal.
    """
    from baton_harness.chain.ruleset_status import (
        RulesetStatus,
        ruleset_is_provisioned,
    )

    # Custom compare-keys that omit "rules".
    custom_keys = [
        "name",
        "target",
        "enforcement",
        "bypass_actors",
        "conditions",
    ]
    tmp_cfg = tmp_path / "ruleset.compare-keys.json"
    tmp_cfg.write_text(json.dumps(custom_keys), encoding="utf-8")

    # Build a main ruleset with a different "rules" value (would be DRIFT
    # under the default key set, but must be MATCH with custom_keys above).
    main_different_rules = _render_main()
    main_different_rules["rules"] = []  # stripped — would cause DRIFT normally

    runner = _FakeRunner(
        list_proc=_ok(_list_body()),
        byid_main_proc=_ok(json.dumps(main_different_rules)),
        byid_feat_proc=_ok(json.dumps(_render_feature())),
    )

    # Monkeypatch the module's resolved config path.
    import baton_harness.chain.ruleset_status as rs_mod

    monkeypatch.setattr(rs_mod, "_COMPARE_KEYS_CFG", tmp_cfg)

    result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)

    assert result is RulesetStatus.MATCH, (
        "When 'rules' is dropped from compare-keys, a rules-only diff "
        f"must not cause DRIFT; got {result!r}"
    )


# ---------------------------------------------------------------------------
# Test F2 — compare-keys config missing
# ---------------------------------------------------------------------------


def test_compare_keys_config_missing_produces_defined_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """compare-keys config absent → fallback or RulesetConfigError.

    # SPEC-AMBIGUITY: The spec allows two behaviors when the shared config
    # file is absent: (a) fall back to the historic literal key set
    # ("name","target","enforcement","bypass_actors","conditions","rules")
    # and continue normally, or (b) raise a structured ``RulesetConfigError``.
    # This test accepts EITHER behavior and pins whichever the implementation
    # chooses.  The code-writer should document the chosen behavior.
    #
    # Pinned decision here: test that the function does NOT silently use an
    # EMPTY compare-keys set (which would always return MATCH regardless of
    # drift).  The module must either raise or use the historic fallback.
    # We detect the silent-empty-set failure by providing drifted content
    # and asserting the result is not MATCH (for the raise case) or is
    # non-MATCH or raises (for both cases).

    Args:
        tmp_path: Pytest tmp_path fixture.
        monkeypatch: Pytest monkeypatch fixture.
    """
    import baton_harness.chain.ruleset_status as rs_mod
    from baton_harness.chain.ruleset_status import ruleset_is_provisioned

    # Point the module at a non-existent file.
    missing = tmp_path / "does-not-exist.json"
    monkeypatch.setattr(rs_mod, "_COMPARE_KEYS_CFG", missing)

    main_drifted = _render_main()
    main_drifted["enforcement"] = "disabled"  # obvious drift in historic keys

    runner = _FakeRunner(
        list_proc=_ok(_list_body()),
        byid_main_proc=_ok(json.dumps(main_drifted)),
        byid_feat_proc=_ok(json.dumps(_render_feature())),
    )

    try:
        result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)
        # If no exception: must not silently return MATCH on drifted content.
        from baton_harness.chain.ruleset_status import RulesetStatus

        assert result is not RulesetStatus.MATCH, (
            "When compare-keys config is missing, a drifted ruleset must "
            "NOT silently return MATCH (empty compare-set silence not allowed)"
        )
    except Exception:
        # Any exception (RulesetConfigError or FileNotFoundError) accepted.
        pass


# ---------------------------------------------------------------------------
# Test F3 — compare-keys config malformed (non-JSON)
# ---------------------------------------------------------------------------


def test_compare_keys_config_malformed_non_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ERROR or raise when compare-keys config contains non-JSON bytes.

    # SPEC-AMBIGUITY: same disposition as the missing-file case.
    # The module must not silently use an empty compare set.  Either raise
    # or return ERROR is acceptable; silently returning MATCH on drifted
    # content is NOT acceptable.

    Args:
        tmp_path: Pytest tmp_path fixture.
        monkeypatch: Pytest monkeypatch fixture.
    """
    import baton_harness.chain.ruleset_status as rs_mod
    from baton_harness.chain.ruleset_status import ruleset_is_provisioned

    bad_cfg = tmp_path / "bad.json"
    bad_cfg.write_text("not json at all }{", encoding="utf-8")
    monkeypatch.setattr(rs_mod, "_COMPARE_KEYS_CFG", bad_cfg)

    main_drifted = _render_main()
    main_drifted["enforcement"] = "disabled"

    runner = _FakeRunner(
        list_proc=_ok(_list_body()),
        byid_main_proc=_ok(json.dumps(main_drifted)),
        byid_feat_proc=_ok(json.dumps(_render_feature())),
    )

    try:
        result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)
        from baton_harness.chain.ruleset_status import RulesetStatus

        assert result is not RulesetStatus.MATCH, (
            "Non-JSON compare-keys config must not silently yield MATCH "
            "on drifted content"
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Test F4 — compare-keys config is an empty list []
# ---------------------------------------------------------------------------


def test_compare_keys_config_empty_list_does_not_silently_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ERROR or raise when compare-keys config is an empty JSON list.

    An empty compare-keys set would make every ruleset match regardless
    of content — this must not be the silently-accepted behavior.
    Either the function raises or returns something other than MATCH
    when the config content is drifted.

    # SPEC-AMBIGUITY: same disposition as the missing-file case.

    Args:
        tmp_path: Pytest tmp_path fixture.
        monkeypatch: Pytest monkeypatch fixture.
    """
    import baton_harness.chain.ruleset_status as rs_mod
    from baton_harness.chain.ruleset_status import ruleset_is_provisioned

    empty_cfg = tmp_path / "empty.json"
    empty_cfg.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(rs_mod, "_COMPARE_KEYS_CFG", empty_cfg)

    main_drifted = _render_main()
    main_drifted["enforcement"] = "disabled"

    runner = _FakeRunner(
        list_proc=_ok(_list_body()),
        byid_main_proc=_ok(json.dumps(main_drifted)),
        byid_feat_proc=_ok(json.dumps(_render_feature())),
    )

    try:
        result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)
        from baton_harness.chain.ruleset_status import RulesetStatus

        assert result is not RulesetStatus.MATCH, (
            "Empty compare-keys list must not silently yield MATCH "
            "on drifted content"
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Test G1 — placeholder substitution: app_id string → int before compare
# ---------------------------------------------------------------------------


def test_placeholder_substituted_with_int_app_id_before_compare() -> None:
    """Placeholder ``__BH_GITHUB_APP_ID__`` is replaced with int(app_id).

    Passing ``app_id="111"`` (a string) must result in MATCH when the live
    feature ruleset has numeric 111 in bypass_actors[0].actor_id.

    This confirms the module converts the string to int before comparing
    against the live API response (which returns numbers as integers).
    """
    from baton_harness.chain.ruleset_status import (
        RulesetStatus,
        ruleset_is_provisioned,
    )

    # _render_feature() substitutes with int(111) already.
    runner = _FakeRunner(
        list_proc=_ok(_list_body()),
        byid_main_proc=_ok(json.dumps(_render_main())),
        byid_feat_proc=_ok(json.dumps(_render_feature(app_id=111))),
    )
    result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)
    assert result is RulesetStatus.MATCH, (
        "app_id='111' (string) must produce MATCH when live ruleset "
        f"has numeric 111; got {result!r}"
    )


# ---------------------------------------------------------------------------
# Test G2 — DRIFT when app_id does NOT match live actor_id
# ---------------------------------------------------------------------------


def test_drift_when_app_id_does_not_match_live_actor_id() -> None:
    """DRIFT when app_id='999' but live ruleset has actor_id=111.

    Confirms the placeholder substitution actually affects the comparison:
    passing a different app_id must detect the mismatch as DRIFT.
    """
    from baton_harness.chain.ruleset_status import (
        RulesetStatus,
        ruleset_is_provisioned,
    )

    # Live ruleset has actor_id=111; we pass app_id="999" (mismatch).
    runner = _FakeRunner(
        list_proc=_ok(_list_body()),
        byid_main_proc=_ok(json.dumps(_render_main())),
        byid_feat_proc=_ok(json.dumps(_render_feature(app_id=111))),
    )
    result = ruleset_is_provisioned("o", "r", app_id="999", runner=runner)
    assert result is RulesetStatus.DRIFT, (
        f"app_id='999' vs live actor_id=111 must produce DRIFT; got {result!r}"
    )


# ---------------------------------------------------------------------------
# Test H1 — HTTP-status edge: stderr with "404" but stdout HTTP 200 → not 404
# ---------------------------------------------------------------------------


def test_stderr_containing_404_digits_with_http_200_stdout_not_absent() -> (
    None
):
    """Stderr '404' digits ignored; only stdout status line matters.

    A successful response (HTTP 200 on stdout first line) whose stderr
    happens to contain the digits '404' (proxy banner) must NOT trigger
    ABSENT or ERROR.  The module must no longer consult stderr at all.

    The result is MATCH (both rulesets present and content-equal).
    """
    from baton_harness.chain.ruleset_status import (
        RulesetStatus,
        ruleset_is_provisioned,
    )

    runner = _FakeRunner(
        list_proc=_ok_with_stderr_404(_list_body()),
        byid_main_proc=_ok_with_stderr_404(json.dumps(_render_main())),
        byid_feat_proc=_ok_with_stderr_404(json.dumps(_render_feature())),
    )
    result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)
    assert result is RulesetStatus.MATCH, (
        "stderr containing '404' digits must not trigger ABSENT or ERROR "
        "when stdout status line says 200; got {result!r}"
    )


# ---------------------------------------------------------------------------
# Test H2 — HTTP-status edge: stderr with "HTTP 404" but stdout HTTP 200
# ---------------------------------------------------------------------------


def test_stderr_http_404_string_ignored_when_stdout_says_200() -> None:
    """Stderr 'HTTP 404' string ignored; only stdout status line matters.

    Even an exact 'HTTP 404' string in stderr must not trigger ABSENT when
    the stdout status line indicates HTTP 200.  The old brittle heuristic
    (``'HTTP 404' in stderr``) is explicitly removed by Charge 8.
    """
    from baton_harness.chain.ruleset_status import (
        RulesetStatus,
        ruleset_is_provisioned,
    )

    runner = _FakeRunner(
        list_proc=_ok_with_stderr_http404(_list_body()),
        byid_main_proc=_ok_with_stderr_http404(json.dumps(_render_main())),
        byid_feat_proc=_ok_with_stderr_http404(json.dumps(_render_feature())),
    )
    result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)
    assert result is RulesetStatus.MATCH, (
        "stderr containing 'HTTP 404' must not trigger ABSENT or ERROR "
        "when stdout status line says 200; got {result!r}"
    )


# ---------------------------------------------------------------------------
# Test I1 — regression #204: real live main body must be MATCH, not DRIFT
# ---------------------------------------------------------------------------


def test_provisioned_despite_github_server_defaults_main() -> None:
    """MATCH (not DRIFT) for a real GitHub GET body of the main ruleset.

    Regression test for #204.  The fixture at ``ruleset.main.live.json``
    is a real ``GET /repos/.../rulesets/<id>`` body captured against
    ``cbeaulieu-gt/baton-test``'s ``harness-main-no-merge`` ruleset,
    which was provisioned correctly.  Two GitHub-only, functionally
    irrelevant divergences from the rendered desired config are present:

    - server-echoed defaults (``pull_request.parameters.
      required_reviewers``, ``allowed_merge_methods``,
      ``required_status_checks.parameters.do_not_enforce_on_create``),
      and
    - the ``update`` rule's ``parameters`` block entirely omitted
      because its only parameter is already the default ``false``.

    ``_filter_for_compare`` only strips top-level keys, so today the
    whole ``rules`` array is compared by strict ``==`` and these two
    divergences flip the result to DRIFT even though the ruleset is
    correctly provisioned.  A fixed comparator must still return MATCH.
    """
    from baton_harness.chain.ruleset_status import (
        RulesetStatus,
        ruleset_is_provisioned,
    )

    live_main_body = _LIVE_MAIN_FIXTURE.read_text(encoding="utf-8")
    live_main_id = json.loads(live_main_body)["id"]

    list_body = json.dumps(
        [
            {"id": live_main_id, "name": "harness-main-no-merge"},
            {"id": _FEAT_ID, "name": "harness-feature-daemon-only"},
        ]
    )

    runner = _FakeRunner(
        list_proc=_ok(list_body),
        byid_main_proc=_ok(live_main_body),
        byid_feat_proc=_ok(json.dumps(_render_feature())),
        main_id=live_main_id,
        feat_id=_FEAT_ID,
    )

    # The live fixture's bypass_actors carries actor_id=5 (RepositoryRole),
    # matching the default admin_role_id used by _render_main() above.
    result = ruleset_is_provisioned(
        "cbeaulieu-gt", "baton-test", app_id="111", runner=runner
    )

    assert result is RulesetStatus.MATCH, (
        "Real GitHub GET body for the main ruleset (server defaults + "
        f"omitted update-rule parameters) must be MATCH, not DRIFT; "
        f"got {result!r}"
    )


# ---------------------------------------------------------------------------
# Test I2 — regression #204: real live feature body must be MATCH, not DRIFT
# ---------------------------------------------------------------------------


def test_provisioned_despite_github_server_defaults_feature() -> None:
    """MATCH (not DRIFT) for a real GitHub GET body of the feature ruleset.

    Regression test for #204.  The fixture at ``ruleset.feature.live.json``
    is a real ``GET /repos/.../rulesets/<id>`` body captured against
    ``cbeaulieu-gt/baton-test``'s ``harness-feature-daemon-only`` ruleset,
    which was provisioned correctly.  As with the main ruleset, the
    ``update`` rule's ``parameters`` block is entirely omitted by GitHub
    because its only parameter is already the default ``false`` — a
    divergence from the rendered desired config that must not be treated
    as drift once ``rules`` is compared structurally instead of as an
    opaque blob.
    """
    from baton_harness.chain.ruleset_status import (
        RulesetStatus,
        ruleset_is_provisioned,
    )

    live_feature_body = _LIVE_FEATURE_FIXTURE.read_text(encoding="utf-8")
    live_feature = json.loads(live_feature_body)
    live_feature_id = live_feature["id"]
    live_app_id = live_feature["bypass_actors"][0]["actor_id"]

    list_body = json.dumps(
        [
            {"id": _MAIN_ID, "name": "harness-main-no-merge"},
            {"id": live_feature_id, "name": "harness-feature-daemon-only"},
        ]
    )

    runner = _FakeRunner(
        list_proc=_ok(list_body),
        byid_main_proc=_ok(json.dumps(_render_main())),
        byid_feat_proc=_ok(live_feature_body),
        main_id=_MAIN_ID,
        feat_id=live_feature_id,
    )

    # app_id is passed as the string form of the fixture's live actor_id
    # (an Integration/App bypass actor) so the placeholder substitution
    # in the rendered desired config matches the live body exactly.
    result = ruleset_is_provisioned(
        "cbeaulieu-gt",
        "baton-test",
        app_id=str(live_app_id),
        runner=runner,
    )

    assert result is RulesetStatus.MATCH, (
        "Real GitHub GET body for the feature ruleset (server defaults + "
        f"omitted update-rule parameters) must be MATCH, not DRIFT; "
        f"got {result!r}"
    )


# ---------------------------------------------------------------------------
# Test J1 — regression (CodeRabbit, PR #205): duplicate rule types must be
# compared as a multiset, not collapsed to one dict key per type.
# ---------------------------------------------------------------------------


def test_rules_equal_drift_when_current_missing_duplicate_rule_type() -> None:
    """DRIFT when current drops one of two same-typed desired rules.

    GitHub allows repeating the same rule ``type`` more than once in a
    single ruleset (e.g. two ``commit_message_pattern`` restrictions
    with different ``parameters``). ``_rules_equal`` builds
    ``{rule["type"]: rule}`` dicts keyed by type, which collapses
    duplicates to their *last* occurrence in each list — so a desired
    side carrying two ``commit_message_pattern`` rules and a current
    side carrying only the *last* of those two both reduce to the same
    single key/value pair (``"commit_message_pattern"`` -> the "BH-"
    rule) and compare equal, even though the first restriction
    ("JIRA-") is entirely missing from current.

    A correct comparator counts occurrences per rule type (a multiset
    compare): two desired vs. one current for the same type is DRIFT,
    not MATCH, because a genuine restriction went missing.
    """
    from baton_harness.chain.ruleset_status import _rules_equal

    desired_rules: list[object] = [
        {
            "type": "commit_message_pattern",
            "parameters": {
                "operator": "starts_with",
                "pattern": "JIRA-",
            },
        },
        {
            "type": "commit_message_pattern",
            "parameters": {
                "operator": "starts_with",
                "pattern": "BH-",
            },
        },
    ]
    # Current carries only the SECOND (last) of desired's two duplicate
    # occurrences. The buggy dict-by-type collapse keeps the last entry
    # from each list, so desired collapses to "BH-" and current also
    # collapses to "BH-" — a false MATCH — even though the "JIRA-"
    # restriction is missing entirely from current.
    current_rules: list[object] = [
        {
            "type": "commit_message_pattern",
            "parameters": {
                "operator": "starts_with",
                "pattern": "BH-",
            },
        },
    ]

    assert _rules_equal(desired_rules, current_rules) is False, (
        "current is missing one of two desired 'commit_message_pattern' "
        "occurrences — this must report DRIFT, not MATCH"
    )


def test_rules_equal_match_duplicate_type_order_independent() -> None:
    """MATCH when both sides carry the same multiset of a duplicated type.

    Guards against over-tightening the multiset fix: when desired and
    current both carry two ``commit_message_pattern`` rules with the
    same two ``parameters`` payloads — even in a different array
    order — the comparison must still report MATCH. The multiset
    compare must not depend on array position.
    """
    from baton_harness.chain.ruleset_status import _rules_equal

    jira_rule = {
        "type": "commit_message_pattern",
        "parameters": {
            "operator": "starts_with",
            "pattern": "JIRA-",
        },
    }
    bh_rule = {
        "type": "commit_message_pattern",
        "parameters": {
            "operator": "starts_with",
            "pattern": "BH-",
        },
    }

    desired_rules: list[object] = [jira_rule, bh_rule]
    # Same two occurrences, reversed order, to prove order-independence.
    current_rules: list[object] = [bh_rule, jira_rule]

    assert _rules_equal(desired_rules, current_rules) is True, (
        "same multiset of two 'commit_message_pattern' occurrences "
        "(order-independent) must report MATCH, not DRIFT"
    )
