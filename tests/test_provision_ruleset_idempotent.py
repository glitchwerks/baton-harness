"""Slice 3b — bin/provision-ruleset.sh idempotency.

Drives the bash script with a fake gh on PATH that records every API
call to a log file and returns canned JSON for BOTH endpoints the
script hits:

  - LIST:  GET /repos/<owner>/<repo>/rulesets  (returns array of
           {id, name})
  - BY-ID: GET /repos/<owner>/<repo>/rulesets/<id>  (returns single
           object)

Six cases:

1. Empty state (LIST returns []) -> exactly 2 POSTs (one per ruleset
   name).
2. Identical state (LIST returns both names with ids 11/22; BY-ID
   GETs return the canonical bodies) -> zero writes (idempotent
   re-run).
3. Drift in feature ruleset (BY-ID GET returns mutated body) ->
   exactly 1 PUT, targeting /rulesets/22.
4. Pre-existing ruleset with STALE numeric ID (LIST returns name with
   id=99; BY-ID GET on id=99 returns matching body) -> zero writes
   AND the call log shows the script GET-d /rulesets/99, not
   /rulesets/22.  Proves the list-then-by-id path is used (NOT a
   name-string lookup).
5. Preflight App-ID mismatch (gh api /app returns id=111 but
   BH_GITHUB_APP_ID=222) -> exit 2, no writes.
6. Admin-bypass actor_id logging: script logs the resolved actor_id
   before any writes so an operator can verify before enforcement.

Issue #199 — soften the App-ID preflight for PAT auth:

  GET /app is App-JWT-only. A PAT-authenticated gh gets a 401/non-zero
  exit rather than a body to compare .id against. Three more cases
  (below the six above) cover the softened contract:

7. Non-App auth (gh api app exits non-zero) -> preflight must NOT
   exit 2; it must warn on stderr that the App-ID/Installation-ID
   cross-check is being skipped (naming the "administration: write"
   PAT permission the skip relies on), then proceed to the write
   phase.
7b. (CodeRabbit follow-up) gh api app exits 0 but the body has no
    .id -> must be treated identically to case 7 (warn + skip +
    proceed), not as a mismatch.
8. App-authed but mismatched .id -> still hard-fails (regression
   guard; the soften must not weaken this).
9. App-authed with matching .id -> still proceeds (regression guard).

Adversarial review of PR #201 flagged the case-7 soften as too broad:
ANY non-zero exit of `gh api app` (a genuine 401, but also a transient
network error, a 5xx, a rate-limit) was being collapsed into the same
warn+skip+proceed path. A transient error on a genuinely App-authed
run would then silently proceed to write rulesets with an unconfirmed
App ID. Case 10 (below) is the new red test for the narrower contract:
only a *confirmed* 401 auth failure may soften; any other non-zero
exit from `gh api app` must hard-fail before any ruleset write.

10. Non-auth gh api app failure (e.g. HTTP 503) -> must hard-fail
    (non-zero exit, distinct from the exit-2 mismatch code, zero
    writes), NOT skip-and-proceed like case 7.

CodeRabbit review of PR #201 (commit 6e9287a) flagged the case-10 fix
as still too broad: it discriminates "confirmed 401" from other
failures by testing whether gh's stderr contains the bare substring
`401` — so a stray `401` anywhere in stderr (a request id, a
correlation token, a byte count) that has nothing to do with an auth
failure would false-trigger the soften-and-proceed path and silently
bypass App-ID validation. Case 11 (below) is the new red test for the
narrower contract: only a stderr containing `HTTP 401` specifically
may soften; a bare `401` substring elsewhere in stderr must hard-fail.

11. Stray "401" substring in non-auth gh api app stderr (e.g. inside a
    correlation id, no "HTTP 401" present) -> must hard-fail exactly
    like case 10 (exit 1, zero writes), NOT skip-and-proceed like
    case 7.

Issue #202 — the fake gh shim diverged from real gh in two ways that let
this suite pass against a script that fails in production:

  Bug 1: ``_lookup_id`` calls ``gh api --paginate --slurp``. ``--slurp``
  is a jq flag, not a ``gh api`` flag — real gh rejects it with
  "unknown flag: --slurp" and exits non-zero. The shim used to emulate
  --slurp by wrapping pages in an outer array; it now rejects --slurp
  exactly like real gh, and models real ``gh api --paginate`` as a FLAT
  concatenation of pages (see the pagination tests below).

  Bug 2: the canonical ``config/ruleset.*.json`` files carry "_comment"
  pseudo-comment keys (top-level and nested). Real GitHub GET responses
  never contain "_comment" — so canned by-id bodies built from the raw
  config must have "_comment" stripped to accurately simulate a live
  ruleset, and the write payload must never contain "_comment" (the
  GitHub Rulesets API rejects it with HTTP 422 for at least one nesting
  position). See ``_strip_comments`` / ``_contains_comment_key`` below
  and the tests in the "Issue #202" section at the end of this file.

Issue #202 follow-up (PR #203): PR #203 reworked ``_lookup_id`` to fail
closed on a LIST-call *failure* (non-zero gh exit), but the trailing
Python parse step still ends with ``2>/dev/null || true``. If the LIST
endpoint returns HTTP 200 with a malformed (non-JSON) body,
``json.loads`` raises, the swallowed non-zero exit makes ``_lookup_id``
return an empty string indistinguishable from "ruleset genuinely
absent", and the caller issues a spurious POST-create. See
``test_lookup_id_fails_closed_on_malformed_list_body`` and the
``list_malformed`` fake-gh marker.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[1]
SCRIPT = HARNESS / "bin" / "provision-ruleset.sh"
FAKE_GH_DIR = HARNESS / "tests" / "fixtures" / "fake_gh"

# On Windows, the system bash (C:\Windows\System32\bash.exe) launches WSL and
# fails when no WSL distro is configured.  Prefer Git Bash when available.
_GIT_BASH = Path("C:/Program Files/Git/usr/bin/bash.exe")
if sys.platform == "win32" and _GIT_BASH.exists():
    _BASH = str(_GIT_BASH)
else:
    _BASH = "bash"
_BASH_BIN_DIR = str(Path(_BASH).parent) if Path(_BASH).exists() else ""


# ---------------------------------------------------------------------------
# Invocation helper
# ---------------------------------------------------------------------------


def _invoke(
    tmp_path: Path,
    canned_state_dir: Path,
    *,
    app_id: str = "111",
    preflight_app_id: str = "111",
    admin_role_id: str = "5",
    admin_collaborators_body: str | None = None,
    custom_roles_body: str | None = None,
    app_authenticated: bool = True,
    app_response_has_id: bool = True,
    app_transient_error: bool = False,
    app_stray_401: bool = False,
    return_stderr: bool = False,
) -> tuple[int, str, Path] | tuple[int, str, str, Path]:
    """Run the provisioning script with the fake gh on PATH.

    Args:
        tmp_path: Pytest-provided temp directory for this test.
        canned_state_dir: Directory containing canned response files
            read by the fake gh shim.
        app_id: Value of BH_GITHUB_APP_ID passed to the script.
        preflight_app_id: The id the fake /app endpoint returns
            (may differ from app_id to trigger B3 mismatch). Ignored
            when app_authenticated is False or app_response_has_id
            is False.
        admin_role_id: Value of BH_ADMIN_ROLE_ID passed to the
            script; defaults to the spec default of "5".
        admin_collaborators_body: Optional canned JSON body for
            GET /collaborators?permission=admin. Defaults to one
            collaborator with role_name="admin".
        custom_roles_body: Optional canned JSON body for
            GET /orgs/.../custom-repository-roles. When absent, the
            fake gh returns a 404-style "feature not available" error.
        app_authenticated: When False, the fake gh simulates a PAT
            caller hitting the App-JWT-only GET /app endpoint: gh
            exits non-zero with a 401-style stderr message instead of
            returning an id (issue #199).
        app_response_has_id: When False, the fake gh returns a
            successful (exit 0) GET /app response whose body has no
            "id" field — e.g. {} — rather than omitting the call
            entirely (issue #199 no-.id gap). Ignored when
            app_authenticated is False (that case already omits any
            id).
        app_transient_error: When True, the fake gh simulates a
            non-auth failure on GET /app (e.g. HTTP 503) — gh exits
            non-zero with a stderr that does NOT indicate 401. Used
            to test the adversarial-review follow-up to #199: only a
            confirmed 401 may soften into skip+proceed; any other
            failure must hard-fail. Mutually exclusive with
            app_authenticated=False and app_response_has_id=False;
            when True, this marker takes precedence in the shim.
        app_stray_401: When True, the fake gh simulates a non-auth
            GET /app failure whose stderr contains the bare substring
            "401" (e.g. inside a correlation id) but NOT "HTTP 401".
            Used to test a CodeRabbit follow-up on PR #201: a bare
            `*"401"*` stderr match must not be treated as a confirmed
            auth failure. Mutually exclusive with app_transient_error,
            app_authenticated=False, and app_response_has_id=False;
            when True and app_transient_error is False, this marker
            takes precedence in the shim.
        return_stderr: When True, return a four-tuple that also
            includes the combined stderr, for tests that assert on
            preflight warning text.

    Returns:
        A three-tuple of (returncode, combined_stdout, gh_call_log_path)
        by default, or a four-tuple that inserts stderr before the log
        path when return_stderr is True.
    """
    log_path = tmp_path / "gh_calls.jsonl"
    # The shim reads app_id.txt to build its /app response.
    (canned_state_dir / "app_id.txt").write_text(
        preflight_app_id, encoding="utf-8"
    )
    if app_transient_error:
        # Presence of this marker tells the shim to simulate a non-401
        # failure (e.g. HTTP 503) on GET /app — a genuine transient/infra
        # error, distinct from a confirmed PAT-auth 401.
        (canned_state_dir / "app_transient_error").write_text(
            "1", encoding="utf-8"
        )
    elif app_stray_401:
        # Presence of this marker tells the shim to simulate a non-auth
        # failure on GET /app whose stderr merely contains the substring
        # "401" incidentally (not "HTTP 401") — must not soften.
        (canned_state_dir / "app_stray_401").write_text("1", encoding="utf-8")
    elif not app_authenticated:
        # Presence of this marker tells the shim to simulate a 401 on
        # GET /app, as a real PAT-authenticated gh would get.
        (canned_state_dir / "app_unauthenticated").write_text(
            "1", encoding="utf-8"
        )
    elif not app_response_has_id:
        # Presence of this marker tells the shim to return a successful
        # GET /app whose body has no "id" field.
        (canned_state_dir / "app_no_id").write_text("1", encoding="utf-8")
    (canned_state_dir / "collaborators_admin.body").write_text(
        admin_collaborators_body
        if admin_collaborators_body is not None
        else json.dumps(
            [
                {
                    "login": "repo-admin",
                    "permissions": {"admin": True},
                    "role_name": "admin",
                }
            ]
        ),
        encoding="utf-8",
    )
    if custom_roles_body is not None:
        (canned_state_dir / "custom_roles.body").write_text(
            custom_roles_body, encoding="utf-8"
        )
    env = {
        **os.environ,
        "PATH": os.pathsep.join(
            part
            for part in [
                str(FAKE_GH_DIR),
                _BASH_BIN_DIR,
                os.environ.get("PATH", ""),
            ]
            if part
        ),
        "BH_REPO_OWNER": "fake-owner",
        "BH_REPO_NAME": "fake-repo",
        "BH_GITHUB_APP_ID": app_id,
        "BH_GITHUB_APP_INSTALLATION_ID": "999999",
        "BH_ADMIN_ROLE_ID": admin_role_id,
        "BH_FAKE_GH_LOG": str(log_path),
        "BH_FAKE_GH_CANNED_DIR": str(canned_state_dir),
        # #200: the script now unconditionally obtains App-auth credentials
        # before any gh call. These two overrides stand in for the real
        # `python -m baton_harness.chain.app_auth {jwt|token}` invocation so
        # this suite's 21 pre-existing cases keep exercising the ruleset
        # write/idempotency behavior without needing real BWS_* secrets.
        "BH_APP_AUTH_JWT_CMD": ("printf %s fake-jwt-for-idempotency-tests"),
        "BH_APP_AUTH_TOKEN_CMD": (
            "printf %s fake-install-token-for-idempotency-tests"
        ),
    }
    proc = subprocess.run(
        [_BASH, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        # Surface script stderr + stdout on any non-zero exit so CI logs are
        # self-diagnosing without a separate debug-instrumentation commit.
        print(
            f"\n--- provision-ruleset.sh stdout (rc={proc.returncode}) ---\n"
            f"{proc.stdout}"
            f"--- provision-ruleset.sh stderr ---\n"
            f"{proc.stderr}"
            f"---"
        )
    if return_stderr:
        return proc.returncode, proc.stdout, proc.stderr, log_path
    return proc.returncode, proc.stdout, log_path


# ---------------------------------------------------------------------------
# Log-parsing helpers
# ---------------------------------------------------------------------------


def _calls(log_path: Path) -> list[dict]:  # type: ignore[type-arg]
    """Parse the gh call log as a list of dicts.

    Args:
        log_path: Path to the JSONL file written by the fake gh shim.

    Returns:
        List of call-record dicts, one per gh invocation.  Empty list
        if the log file does not exist (no calls were made).
    """
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _writes(calls: list[dict]) -> list[dict]:  # type: ignore[type-arg]
    """Filter a call list to only mutating (POST / PUT) requests.

    Args:
        calls: Call records from ``_calls``.

    Returns:
        Subset of records whose method is POST or PUT.
    """
    return [c for c in calls if c["method"] in ("POST", "PUT")]


def _strip_comments(obj: object) -> object:
    """Recursively remove "_comment" keys from a JSON-decoded structure.

    The real GitHub Rulesets API never emits a "_comment" pseudo-comment
    field in its responses, but ``config/ruleset.*.json`` carries one for
    human documentation (top-level and, in ``ruleset.main.json``, nested
    inside a rule's ``parameters``). Canned by-id bodies built from those
    config files must have every "_comment" key stripped so they
    accurately simulate a live GET response.

    Args:
        obj: A JSON-decoded value — dict, list, or scalar.

    Returns:
        A new structure with any "_comment" key removed at every depth.
        Scalars are returned unchanged.
    """
    if isinstance(obj, dict):
        return {
            key: _strip_comments(value)
            for key, value in obj.items()
            if key != "_comment"
        }
    if isinstance(obj, list):
        return [_strip_comments(item) for item in obj]
    return obj


def _contains_comment_key(obj: object) -> bool:
    """Check whether a JSON-decoded structure has a "_comment" key.

    Args:
        obj: A JSON-decoded value — dict, list, or scalar.

    Returns:
        True if a "_comment" key is present in ``obj`` at any depth
        (top-level or nested in a dict/list), False otherwise.
    """
    if isinstance(obj, dict):
        if "_comment" in obj:
            return True
        return any(_contains_comment_key(value) for value in obj.values())
    if isinstance(obj, list):
        return any(_contains_comment_key(item) for item in obj)
    return False


# ---------------------------------------------------------------------------
# Case 1: empty state
# ---------------------------------------------------------------------------


def test_empty_state_creates_both_rulesets(tmp_path: Path) -> None:
    """LIST returns [] -> script must POST-create both named rulesets."""
    canned = tmp_path / "canned"
    canned.mkdir()
    # LIST endpoint returns an empty array — no rulesets exist yet.
    (canned / "list.body").write_text("[]", encoding="utf-8")

    rc, _stdout, log = _invoke(tmp_path, canned)

    assert rc == 0, f"script exited {rc}"
    writes = _writes(_calls(log))
    assert len(writes) == 2, f"expected 2 POSTs, got {writes}"
    assert all(c["method"] == "POST" for c in writes)
    assert {c["ruleset_name"] for c in writes} == {
        "harness-main-no-merge",
        "harness-feature-daemon-only",
    }


# ---------------------------------------------------------------------------
# Case 2: identical state (no-op)
# ---------------------------------------------------------------------------


def test_identical_state_is_noop(tmp_path: Path) -> None:
    """Both rulesets match canonical config -> zero writes."""
    canned = tmp_path / "canned"
    canned.mkdir()
    # LIST returns both rulesets with canonical numeric IDs.
    (canned / "list.body").write_text(
        json.dumps(
            [
                {"id": 11, "name": "harness-main-no-merge"},
                {"id": 22, "name": "harness-feature-daemon-only"},
            ]
        ),
        encoding="utf-8",
    )
    # BY-ID bodies are the canonical configs with placeholders resolved.
    # "_comment" is stripped because a real GET response never carries it
    # (issue #202) — leaving it in would make this canned "live" body
    # differ from the comment-free desired config and spuriously drift.
    main_body = json.loads(
        (HARNESS / "config" / "ruleset.main.json").read_text(encoding="utf-8")
    )
    main_body["bypass_actors"][0]["actor_id"] = 5  # BH_ADMIN_ROLE_ID
    (canned / "byid_11.body").write_text(
        json.dumps(_strip_comments(main_body)), encoding="utf-8"
    )

    feature_body = json.loads(
        (HARNESS / "config" / "ruleset.feature.json").read_text(
            encoding="utf-8"
        )
    )
    feature_body["bypass_actors"][0]["actor_id"] = 111  # BH_GITHUB_APP_ID
    (canned / "byid_22.body").write_text(
        json.dumps(_strip_comments(feature_body)), encoding="utf-8"
    )

    rc, _stdout, log = _invoke(tmp_path, canned)

    assert rc == 0, f"script exited {rc}"
    assert _writes(_calls(log)) == [], (
        "expected zero writes on identical state"
    )


# ---------------------------------------------------------------------------
# Case 3: drift in one ruleset triggers a single PUT
# ---------------------------------------------------------------------------


def test_drift_in_feature_triggers_single_put(tmp_path: Path) -> None:
    """Drift detected in feature ruleset -> exactly 1 PUT to /rulesets/22."""
    canned = tmp_path / "canned"
    canned.mkdir()
    (canned / "list.body").write_text(
        json.dumps(
            [
                {"id": 11, "name": "harness-main-no-merge"},
                {"id": 22, "name": "harness-feature-daemon-only"},
            ]
        ),
        encoding="utf-8",
    )
    # Main ruleset is canonical — no drift. "_comment" stripped: a real GET
    # response never carries it (issue #202).
    main_body = json.loads(
        (HARNESS / "config" / "ruleset.main.json").read_text(encoding="utf-8")
    )
    main_body["bypass_actors"][0]["actor_id"] = 5
    (canned / "byid_11.body").write_text(
        json.dumps(_strip_comments(main_body)), encoding="utf-8"
    )
    # Feature ruleset has been mutated (bypass cleared — workers could merge).
    feature_drifted = json.loads(
        (HARNESS / "config" / "ruleset.feature.json").read_text(
            encoding="utf-8"
        )
    )
    feature_drifted["bypass_actors"] = []
    (canned / "byid_22.body").write_text(
        json.dumps(_strip_comments(feature_drifted)), encoding="utf-8"
    )

    rc, _stdout, log = _invoke(tmp_path, canned)

    assert rc == 0, f"script exited {rc}"
    writes = _writes(_calls(log))
    assert len(writes) == 1, f"expected exactly 1 write, got {writes}"
    assert writes[0]["method"] == "PUT"
    assert writes[0]["url"].endswith("/rulesets/22"), writes[0]["url"]


# ---------------------------------------------------------------------------
# Case 4: stale numeric ID — B1 regression guard
# ---------------------------------------------------------------------------


def test_preexisting_with_stale_id_uses_list_filter_path(
    tmp_path: Path,
) -> None:
    """B1 regression: script discovers ids via LIST, not name-string lookup.

    Seeds the LIST response with non-default IDs (99 / 77).  Asserts:
    - GET calls reference /rulesets/99 and /rulesets/77 (discovered ids).
    - No GET URL contains the ruleset name string after "/rulesets/".
    - Zero writes (bodies match after resolution).
    """
    canned = tmp_path / "canned"
    canned.mkdir()
    # Arbitrary IDs that the script must discover from the LIST response.
    (canned / "list.body").write_text(
        json.dumps(
            [
                {"id": 99, "name": "harness-main-no-merge"},
                {"id": 77, "name": "harness-feature-daemon-only"},
            ]
        ),
        encoding="utf-8",
    )
    # "_comment" stripped: a real GET response never carries it (#202).
    main_body = json.loads(
        (HARNESS / "config" / "ruleset.main.json").read_text(encoding="utf-8")
    )
    main_body["bypass_actors"][0]["actor_id"] = 5
    (canned / "byid_99.body").write_text(
        json.dumps(_strip_comments(main_body)), encoding="utf-8"
    )
    feature_body = json.loads(
        (HARNESS / "config" / "ruleset.feature.json").read_text(
            encoding="utf-8"
        )
    )
    feature_body["bypass_actors"][0]["actor_id"] = 111
    (canned / "byid_77.body").write_text(
        json.dumps(_strip_comments(feature_body)), encoding="utf-8"
    )

    rc, _stdout, log = _invoke(tmp_path, canned)

    assert rc == 0, f"script exited {rc}"
    calls = _calls(log)
    # Zero writes — bodies matched after discovery.
    assert _writes(calls) == [], (
        "expected no writes for matching stale-id state"
    )
    # GET URLs must use discovered numeric IDs.
    get_urls = [c["url"] for c in calls if c["method"] == "GET"]
    assert any(u.endswith("/rulesets/99") for u in get_urls), (
        f"expected GET on /rulesets/99 in {get_urls}"
    )
    assert any(u.endswith("/rulesets/77") for u in get_urls), (
        f"expected GET on /rulesets/77 in {get_urls}"
    )
    # Name strings must NOT appear as URL path segments after "/rulesets/".
    assert not any(
        "harness-main-no-merge" in u for u in get_urls if "/rulesets/" in u
    ), f"name-string appeared in GET URL (name-lookup used): {get_urls}"
    assert not any(
        "harness-feature-daemon-only" in u
        for u in get_urls
        if "/rulesets/" in u
    ), f"name-string appeared in GET URL (name-lookup used): {get_urls}"


# ---------------------------------------------------------------------------
# Case 5: B3 preflight mismatch aborts before any write
# ---------------------------------------------------------------------------


def test_preflight_app_id_mismatch_aborts(tmp_path: Path) -> None:
    """B3: BH_GITHUB_APP_ID != GET /app .id -> exit 2 with no writes.

    The /app endpoint returns id=111 but the env carries 222, so the
    script must abort (exit 2) before performing any ruleset mutation.
    """
    canned = tmp_path / "canned"
    canned.mkdir()
    (canned / "list.body").write_text("[]", encoding="utf-8")

    rc, _stdout, log = _invoke(
        tmp_path, canned, app_id="222", preflight_app_id="111"
    )

    assert rc == 2, f"expected exit 2 for App-ID mismatch, got {rc}"
    assert _writes(_calls(log)) == [], (
        "script must write zero ruleset mutations on preflight failure"
    )


# ---------------------------------------------------------------------------
# Case 6: C3 admin-bypass actor_id logged before first write
# ---------------------------------------------------------------------------


def test_admin_bypass_actor_id_logged_before_writes(tmp_path: Path) -> None:
    """C3: resolved actor_id appears in stdout before any ruleset write.

    An operator reading the log must be able to confirm the actor_id
    that will be embedded in the admin-bypass rule before enforcement
    is active.  This test uses a non-default admin_role_id to verify
    the resolved value (not a hard-coded default) is what gets logged.
    """
    canned = tmp_path / "canned"
    canned.mkdir()
    # LIST returns empty so the script will POST (write path exercised).
    (canned / "list.body").write_text("[]", encoding="utf-8")

    custom_role_id = "42"
    rc, stdout, log = _invoke(
        tmp_path,
        canned,
        admin_role_id=custom_role_id,
        custom_roles_body=json.dumps(
            [
                {
                    "id": 42,
                    "name": "Platform Admin",
                    "base_role": "admin",
                }
            ]
        ),
    )

    assert rc == 0, f"script exited {rc}"
    writes = _writes(_calls(log))
    # Sanity: writes did happen (so there was an opportunity to log first).
    assert writes, "expected at least one write for empty-state test"
    # The resolved actor_id must appear in stdout.
    assert custom_role_id in stdout, (
        f"actor_id '{custom_role_id}' not found in script stdout; "
        f"stdout was:\n{stdout}"
    )


def test_preflight_admin_role_requires_admin_collaborator(
    tmp_path: Path,
) -> None:
    """Admin-role preflight fails closed when the repo has no admins."""
    canned = tmp_path / "canned"
    canned.mkdir()
    (canned / "list.body").write_text("[]", encoding="utf-8")

    rc, _stdout, log = _invoke(
        tmp_path,
        canned,
        admin_collaborators_body="[]",
    )

    assert rc == 2, f"expected exit 2 when no admin collaborators, got {rc}"
    assert _writes(_calls(log)) == [], (
        "script must write zero ruleset mutations when admin-role "
        "preflight fails"
    )


def test_nondefault_admin_role_requires_custom_role_validation(
    tmp_path: Path,
) -> None:
    """Non-default role id fails if GitHub cannot validate the override."""
    canned = tmp_path / "canned"
    canned.mkdir()
    (canned / "list.body").write_text("[]", encoding="utf-8")

    rc, _stdout, log = _invoke(
        tmp_path,
        canned,
        admin_role_id="42",
    )

    assert rc == 2, (
        "expected exit 2 when custom-role validation is unavailable for a "
        "non-default BH_ADMIN_ROLE_ID override"
    )
    assert _writes(_calls(log)) == [], (
        "script must write zero ruleset mutations when overridden admin "
        "role id cannot be validated"
    )


def test_nondefault_admin_role_passes_when_custom_role_matches(
    tmp_path: Path,
) -> None:
    """Custom admin-role override succeeds when validation matches."""
    canned = tmp_path / "canned"
    canned.mkdir()
    (canned / "list.body").write_text("[]", encoding="utf-8")

    rc, _stdout, log = _invoke(
        tmp_path,
        canned,
        admin_role_id="42",
        custom_roles_body=json.dumps(
            [
                {
                    "id": 42,
                    "name": "Platform Admin",
                    "base_role": "admin",
                }
            ]
        ),
    )

    assert rc == 0, (
        f"expected exit 0 for validated custom admin role, got {rc}"
    )
    writes = _writes(_calls(log))
    assert len(writes) == 2, (
        f"expected provisioning writes after preflight, got {writes}"
    )


# ---------------------------------------------------------------------------
# P2-A: pagination — codex review PR #158
# ---------------------------------------------------------------------------


def test_pagination_ruleset_on_page1_found_and_noop(
    tmp_path: Path,
) -> None:
    """P2-A page 1: both rulesets on page 1 are found; zero writes.

    Regression-protection: verifies the flat, no-slurp --paginate path
    (issue #202) does NOT break existing single-page behaviour (the
    common case).
    """
    canned = tmp_path / "canned"
    canned.mkdir()
    # Page 1 contains both rulesets — no page 2 file.
    (canned / "list.body").write_text(
        json.dumps(
            [
                {"id": 11, "name": "harness-main-no-merge"},
                {"id": 22, "name": "harness-feature-daemon-only"},
            ]
        ),
        encoding="utf-8",
    )
    # "_comment" stripped: a real GET response never carries it (#202).
    main_body = json.loads(
        (HARNESS / "config" / "ruleset.main.json").read_text(encoding="utf-8")
    )
    main_body["bypass_actors"][0]["actor_id"] = 5
    (canned / "byid_11.body").write_text(
        json.dumps(_strip_comments(main_body)), encoding="utf-8"
    )
    feature_body = json.loads(
        (HARNESS / "config" / "ruleset.feature.json").read_text(
            encoding="utf-8"
        )
    )
    feature_body["bypass_actors"][0]["actor_id"] = 111
    (canned / "byid_22.body").write_text(
        json.dumps(_strip_comments(feature_body)), encoding="utf-8"
    )

    rc, _stdout, log = _invoke(tmp_path, canned)

    assert rc == 0, f"script exited {rc}"
    assert _writes(_calls(log)) == [], (
        "expected zero writes when rulesets match on page 1"
    )


def test_pagination_ruleset_on_page2_found_and_noop(
    tmp_path: Path,
) -> None:
    """P2-A page 2: rulesets discovered only on the second page; zero writes.

    Before the P2-A fix, _lookup_id fetched only one page so a ruleset
    that happened to land on page 2+ was treated as absent — causing a
    duplicate POST instead of the correct no-op or PUT.  After the fix
    (issue #202: plain ``--paginate``, no ``--slurp``), the shim emits a
    flat two-page response and the script must discover both rulesets
    and write nothing.
    """
    canned = tmp_path / "canned"
    canned.mkdir()
    # Page 1 is empty (e.g. 30+ other rulesets exist before ours).
    (canned / "list.body").write_text("[]", encoding="utf-8")
    # Page 2 contains our two rulesets.
    (canned / "list_page2.body").write_text(
        json.dumps(
            [
                {"id": 55, "name": "harness-main-no-merge"},
                {"id": 66, "name": "harness-feature-daemon-only"},
            ]
        ),
        encoding="utf-8",
    )
    # "_comment" stripped: a real GET response never carries it (#202).
    main_body = json.loads(
        (HARNESS / "config" / "ruleset.main.json").read_text(encoding="utf-8")
    )
    main_body["bypass_actors"][0]["actor_id"] = 5
    (canned / "byid_55.body").write_text(
        json.dumps(_strip_comments(main_body)), encoding="utf-8"
    )
    feature_body = json.loads(
        (HARNESS / "config" / "ruleset.feature.json").read_text(
            encoding="utf-8"
        )
    )
    feature_body["bypass_actors"][0]["actor_id"] = 111
    (canned / "byid_66.body").write_text(
        json.dumps(_strip_comments(feature_body)), encoding="utf-8"
    )

    rc, _stdout, log = _invoke(tmp_path, canned)

    assert rc == 0, f"script exited {rc}"
    calls = _calls(log)
    writes = _writes(calls)
    assert writes == [], (
        f"expected zero writes when page-2 rulesets match; got {writes}"
    )
    # GET calls must reference the page-2 discovered IDs.
    get_urls = [c["url"] for c in calls if c["method"] == "GET"]
    assert any(u.endswith("/rulesets/55") for u in get_urls), (
        f"expected GET on /rulesets/55 (page-2 id); got {get_urls}"
    )
    assert any(u.endswith("/rulesets/66") for u in get_urls), (
        f"expected GET on /rulesets/66 (page-2 id); got {get_urls}"
    )


def test_pagination_absent_on_all_pages_triggers_post(
    tmp_path: Path,
) -> None:
    """P2-A absent: rulesets missing on both pages -> two POSTs.

    Both pages return unrelated entries.  The script must conclude the
    target rulesets are absent and POST-create them — not raise an error.
    """
    canned = tmp_path / "canned"
    canned.mkdir()
    # Page 1: unrelated rulesets.
    (canned / "list.body").write_text(
        json.dumps([{"id": 1, "name": "some-other-ruleset"}]),
        encoding="utf-8",
    )
    # Page 2: also unrelated.
    (canned / "list_page2.body").write_text(
        json.dumps([{"id": 2, "name": "yet-another-ruleset"}]),
        encoding="utf-8",
    )

    rc, _stdout, log = _invoke(tmp_path, canned)

    assert rc == 0, f"script exited {rc}"
    writes = _writes(_calls(log))
    assert len(writes) == 2, (
        f"expected 2 POSTs when absent on all pages; got {writes}"
    )
    assert all(c["method"] == "POST" for c in writes)
    assert {c["ruleset_name"] for c in writes} == {
        "harness-main-no-merge",
        "harness-feature-daemon-only",
    }


# ---------------------------------------------------------------------------
# Issue #199: soften the App-ID preflight under PAT auth
# ---------------------------------------------------------------------------
#
# GET /app is App-JWT-only. When gh is authenticated with a PAT (the
# documented "administration:write" path), that endpoint 401s and gh
# exits non-zero — it never returns a body to compare .id against.
# Case 7 asserts the preflight must soften to a skip-with-warning in
# that situation rather than hard-failing. Cases 8-9 are regression
# guards proving the soften does not weaken the App-authed paths.


def test_non_app_auth_skips_appid_check_and_proceeds_to_writes(
    tmp_path: Path,
) -> None:
    """Case 7: gh api app exits non-zero (PAT auth) -> warn+skip, proceed.

    Simulates a PAT-authenticated gh hitting the App-JWT-only GET /app
    endpoint: the fake gh exits non-zero with a 401-style stderr message
    instead of returning an id. The script must NOT treat this as a
    preflight failure (must not exit 2, must not abort before writing).
    Instead it must:
      (a) print a warning to stderr explaining the App-ID vs
          Installation-ID cross-check is being skipped because gh is
          not App-authenticated, naming the `administration: write`
          PAT permission the documented path relies on instead
          (CodeRabbit follow-up: an operator reading the warning must
          be told which permission makes the skip safe), and
      (b) proceed past the preflight into the ruleset-write phase, as
          proven by the empty-state LIST triggering the normal 2-POST
          create path (same observable write shape as case 1).
    """
    canned = tmp_path / "canned"
    canned.mkdir()
    # Empty LIST -> if the script reaches the write phase, it creates
    # both rulesets. Zero writes would mean the script aborted early.
    (canned / "list.body").write_text("[]", encoding="utf-8")

    rc, stdout, stderr, log = _invoke(
        tmp_path, canned, app_authenticated=False, return_stderr=True
    )

    assert rc == 0, (
        f"non-App auth must not hard-fail the preflight (soften per "
        f"#199); got rc={rc}\nstdout:\n{stdout}\nstderr:\n{stderr}"
    )

    # (a) A warning explaining the cross-check was skipped must be on
    # stderr. Wording is the implementation's choice; the warning must
    # at minimum name the check being skipped (App ID) and say it was
    # skipped, without claiming success ("preflight OK") or emitting
    # the hard-fail banner used for a genuine mismatch (case 8).
    stderr_lower = stderr.lower()
    assert "skip" in stderr_lower, (
        f"expected a 'skip' warning on stderr when gh is not "
        f"App-authenticated; stderr was:\n{stderr}"
    )
    assert "app" in stderr_lower and "id" in stderr_lower, (
        f"expected the skip warning to reference the App-ID check; "
        f"stderr was:\n{stderr}"
    )
    assert "preflight failure" not in stderr_lower, (
        f"non-App auth must not be reported as a preflight failure; "
        f"stderr was:\n{stderr}"
    )
    # CodeRabbit follow-up (#199): the skip warning must hint at the
    # PAT permission that makes skipping the cross-check safe, so an
    # operator reading it knows what to grant instead of guessing.
    assert "administration: write" in stderr_lower or (
        "administration" in stderr_lower and "write" in stderr_lower
    ), (
        f"expected the skip warning to mention the 'administration: "
        f"write' PAT permission; stderr was:\n{stderr}"
    )

    # (b) Execution proceeded past the preflight to the write phase.
    writes = _writes(_calls(log))
    assert len(writes) == 2, (
        f"expected the script to proceed to create both rulesets after "
        f"skipping the App-ID check, got writes={writes}"
    )
    assert {c["ruleset_name"] for c in writes} == {
        "harness-main-no-merge",
        "harness-feature-daemon-only",
    }

    # (d) Secret hygiene: no token/JWT-shaped material in any observed
    # output. Nothing in this test setup carries a real credential, so
    # this is a standing regression guard against accidentally echoing
    # auth material when reporting the skip.
    combined = stdout + stderr
    for leaked_marker in (
        "Bearer ",
        "ghp_",
        "gho_",
        "github_pat_",
        "ghs_",
        "ghu_",
        "ghr_",
    ):
        assert leaked_marker not in combined, (
            f"possible credential material leaked in output: "
            f"{leaked_marker!r} found"
        )


def test_app_authed_empty_or_missing_id_skips_and_proceeds(
    tmp_path: Path,
) -> None:
    """Case 7b (CodeRabbit follow-up): GET /app succeeds with no .id.

    gh api app can exit 0 (success) yet return a body with no "id"
    field. Today the script extracts an empty _live_app_id from such a
    body and compares it against BH_GITHUB_APP_ID; since "" never
    equals a numeric app id, this is indistinguishable from a genuine
    mismatch and hard-fails with PREFLIGHT FAILURE (exit 2).

    A missing .id is not evidence of a *wrong* app id — it is the same
    "we cannot confirm the App identity" situation as non-App auth
    (case 7), and must be handled identically: warn + skip the
    cross-check + proceed to the write phase. Treating it as a hard
    mismatch would false-positive-block any caller whose /app response
    shape omits the field, which is exactly the class of gap #199 set
    out to soften.
    """
    canned = tmp_path / "canned"
    canned.mkdir()
    (canned / "list.body").write_text("[]", encoding="utf-8")

    rc, stdout, stderr, log = _invoke(
        tmp_path,
        canned,
        app_response_has_id=False,
        return_stderr=True,
    )

    assert rc == 0, (
        f"a successful GET /app with no .id must not hard-fail the "
        f"preflight (treat like non-App auth, #199); got rc={rc}\n"
        f"stdout:\n{stdout}\nstderr:\n{stderr}"
    )

    stderr_lower = stderr.lower()
    assert "skip" in stderr_lower, (
        f"expected a 'skip' warning on stderr when GET /app returns no "
        f".id; stderr was:\n{stderr}"
    )
    assert "app" in stderr_lower and "id" in stderr_lower, (
        f"expected the skip warning to reference the App-ID check; "
        f"stderr was:\n{stderr}"
    )
    assert "preflight failure" not in stderr_lower, (
        f"a no-.id success response must not be reported as a "
        f"preflight failure; stderr was:\n{stderr}"
    )

    writes = _writes(_calls(log))
    assert len(writes) == 2, (
        f"expected the script to proceed to create both rulesets after "
        f"skipping the App-ID check on a no-.id response, got "
        f"writes={writes}"
    )
    assert {c["ruleset_name"] for c in writes} == {
        "harness-main-no-merge",
        "harness-feature-daemon-only",
    }


def test_app_authed_mismatched_id_still_hard_fails(tmp_path: Path) -> None:
    """Case 8 (regression guard): App-authed + mismatched .id -> exit 2.

    When gh IS App-authenticated (GET /app succeeds) but the returned
    .id does not match BH_GITHUB_APP_ID, the preflight must still hard
    -fail exactly as before #199 — the soften only applies to the
    non-App-authenticated case (case 7), never to a confirmed mismatch.
    """
    canned = tmp_path / "canned"
    canned.mkdir()
    (canned / "list.body").write_text("[]", encoding="utf-8")

    rc, stdout, stderr, log = _invoke(
        tmp_path,
        canned,
        app_id="222",
        preflight_app_id="111",
        app_authenticated=True,
        return_stderr=True,
    )

    assert rc == 2, (
        f"App-authed mismatch must still hard-fail with exit 2 "
        f"(preserved by #199); got rc={rc}\nstderr:\n{stderr}"
    )
    assert "PREFLIGHT FAILURE" in stderr, (
        f"expected the existing hard-fail banner to be preserved; "
        f"stderr was:\n{stderr}"
    )
    assert _writes(_calls(log)) == [], (
        "script must write zero ruleset mutations on a confirmed "
        "App-ID mismatch"
    )


def test_app_authed_matching_id_still_proceeds(tmp_path: Path) -> None:
    """Case 9 (regression guard): App-authed + matching .id -> proceeds.

    When gh IS App-authenticated and the returned .id matches
    BH_GITHUB_APP_ID, the preflight must pass exactly as before #199,
    proceeding into the normal write phase.
    """
    canned = tmp_path / "canned"
    canned.mkdir()
    (canned / "list.body").write_text("[]", encoding="utf-8")

    rc, stdout, stderr, log = _invoke(
        tmp_path,
        canned,
        app_id="111",
        preflight_app_id="111",
        app_authenticated=True,
        return_stderr=True,
    )

    assert rc == 0, (
        f"App-authed matching id must proceed as before #199; "
        f"got rc={rc}\nstderr:\n{stderr}"
    )
    writes = _writes(_calls(log))
    assert len(writes) == 2, (
        f"expected the normal 2-POST create path after a matching "
        f"App-ID preflight, got writes={writes}"
    )
    assert {c["ruleset_name"] for c in writes} == {
        "harness-main-no-merge",
        "harness-feature-daemon-only",
    }


# ---------------------------------------------------------------------------
# Adversarial-review follow-up to #199 (PR #201): only a confirmed 401
# may soften into skip+proceed; any other GET /app failure must hard-fail.
# ---------------------------------------------------------------------------


def test_non_auth_gh_api_app_failure_hard_fails(tmp_path: Path) -> None:
    """Case 10: gh api app fails non-401 -> hard-fail, zero writes.

    The pre-fix behaviour (issue #199) collapsed ANY non-zero exit of
    `gh api app` — a genuine 401, but also a transient network error,
    a 5xx, or a rate-limit — into the same warn+skip+proceed path. A
    transient error on a genuinely App-authenticated run would then
    silently proceed to write rulesets with an *unconfirmed* App ID,
    defeating the preflight's protective purpose.

    This test simulates a non-auth GET /app failure (HTTP 503) via the
    `app_transient_error` shim marker. Unlike case 7 (confirmed 401,
    which softens), this must:
      - exit non-zero,
      - use a distinct code from the exit-2 App-ID-mismatch path (this
        suite asserts exit 1 — the implementer must match this code),
      - write ZERO ruleset mutations (no POST/PUT in the call log),
      - report on stderr that GET /app failed for a non-auth reason
        (i.e. must NOT emit the case-7/7b "skip" warning, and must
        surface the underlying failure, e.g. "503").
    """
    canned = tmp_path / "canned"
    canned.mkdir()
    # Empty LIST -> if the script incorrectly soften-and-proceeds (the
    # pre-fix bug), it would create both rulesets here.
    (canned / "list.body").write_text("[]", encoding="utf-8")

    rc, stdout, stderr, log = _invoke(
        tmp_path, canned, app_transient_error=True, return_stderr=True
    )

    assert rc != 0, (
        f"a non-auth GET /app failure must hard-fail, not exit 0; "
        f"stdout:\n{stdout}\nstderr:\n{stderr}"
    )
    assert rc != 2, (
        f"a non-auth GET /app failure must use a code distinct from "
        f"the exit-2 App-ID-mismatch path (confusing the two failure "
        f"modes was the adversarial-review finding); got rc={rc}"
    )
    assert rc == 1, (
        f"expected exit 1 for a non-auth GET /app failure (the code "
        f"this test suite standardizes on); got rc={rc}\n"
        f"stdout:\n{stdout}\nstderr:\n{stderr}"
    )

    assert _writes(_calls(log)) == [], (
        "a non-auth GET /app failure must write zero ruleset "
        "mutations — an unconfirmed App ID must never reach the "
        "write phase"
    )

    stderr_lower = stderr.lower()
    assert "app" in stderr_lower, (
        f"expected stderr to reference the failed GET /app call; "
        f"stderr was:\n{stderr}"
    )
    assert "503" in stderr or "service unavailable" in stderr_lower, (
        f"expected stderr to surface the underlying non-auth failure "
        f"(HTTP 503); stderr was:\n{stderr}"
    )
    # Must NOT be reported via the case-7/7b skip-and-proceed wording —
    # this is the exact confusion the adversarial review flagged.
    assert "skip" not in stderr_lower, (
        f"a non-auth failure must not be reported as a skip (that "
        f"wording is reserved for a confirmed 401 per case 7); "
        f"stderr was:\n{stderr}"
    )


# ---------------------------------------------------------------------------
# CodeRabbit follow-up on PR #201 (commit 6e9287a): the case-10 fix must
# require "HTTP 401" specifically, not a bare "401" substring anywhere in
# stderr.
# ---------------------------------------------------------------------------


def test_stray_401_in_stderr_still_hard_fails(tmp_path: Path) -> None:
    """Case 11: a bare "401" substring in non-auth stderr must hard-fail.

    The case-10 fix (commit 6e9287a) discriminates "confirmed 401 auth
    failure" from any other `gh api app` failure by testing whether
    gh's stderr contains the substring `401`. That match is too broad:
    a stderr that merely happens to contain the digits "401" for an
    unrelated reason (here, inside a correlation id:
    "req-401aa") is NOT a confirmed 401 auth failure, and must not be
    softened into the case-7 skip+proceed path. Only a stderr
    containing the specific substring "HTTP 401" (as the real GitHub
    CLI emits for an actual 401 response, and as the existing
    `app_unauthenticated` marker's "gh: HTTP 401: Bad credentials"
    reproduces) may soften.

    This must hard-fail exactly like case 10: non-zero exit (this
    suite standardizes on exit 1, matching
    ``test_non_auth_gh_api_app_failure_hard_fails``), zero ruleset
    writes, and stderr must NOT carry the case-7 "skip" wording.
    """
    canned = tmp_path / "canned"
    canned.mkdir()
    # Empty LIST -> if the script incorrectly soften-and-proceeds (the
    # bare-"401"-match bug), it would create both rulesets here.
    (canned / "list.body").write_text("[]", encoding="utf-8")

    rc, stdout, stderr, log = _invoke(
        tmp_path, canned, app_stray_401=True, return_stderr=True
    )

    assert rc != 0, (
        f"a stray '401' substring in non-auth stderr must hard-fail, "
        f"not exit 0; stdout:\n{stdout}\nstderr:\n{stderr}"
    )
    assert rc != 2, (
        f"a stray '401' substring in non-auth stderr must use a code "
        f"distinct from the exit-2 App-ID-mismatch path; got rc={rc}"
    )
    assert rc == 1, (
        f"expected exit 1 for a non-auth GET /app failure whose stderr "
        f"merely contains a stray '401' substring (matching the case-10 "
        f"convention); got rc={rc}\nstdout:\n{stdout}\nstderr:\n{stderr}"
    )

    assert _writes(_calls(log)) == [], (
        "a stray '401' substring in non-auth stderr must write zero "
        "ruleset mutations — it is not a confirmed auth failure and "
        "must not soften into skip+proceed"
    )

    stderr_lower = stderr.lower()
    # Must NOT be reported via the case-7/7b skip-and-proceed wording —
    # this is exactly the false-trigger CodeRabbit flagged.
    assert "skip" not in stderr_lower, (
        f"a stray '401' substring must not be reported as a skip (that "
        f"wording is reserved for a confirmed 'HTTP 401' failure per "
        f"case 7); stderr was:\n{stderr}"
    )


# ---------------------------------------------------------------------------
# Issue #202: the write payload must never carry the "_comment"
# pseudo-comment key the real GitHub Rulesets API rejects (HTTP 422).
# ---------------------------------------------------------------------------


def test_empty_state_post_payload_has_no_comment_key(tmp_path: Path) -> None:
    """No "_comment" key at any depth in a POST-create payload.

    ``config/ruleset.main.json`` carries a "_comment" key both at the
    top level and nested inside ``rules[].parameters`` for the
    ``pull_request`` rule; ``config/ruleset.feature.json`` carries one
    at the top level. The real GitHub Rulesets API has never accepted
    either — GET responses never contain it, and a POST/PUT that
    includes the nested one 422s with "Invalid rule 'pull_request':
    Unexpected parameter '_comment'".

    This test drives the empty-state POST-create path (LIST returns
    []) and inspects the *actual bytes sent on the wire* — the fake
    gh shim's new "body" call-log field — rather than the source
    config file, so it fails for the right reason if the script ever
    forwards the config verbatim instead of stripping the comment
    key(s) before serializing the request body.
    """
    canned = tmp_path / "canned"
    canned.mkdir()
    (canned / "list.body").write_text("[]", encoding="utf-8")

    rc, _stdout, log = _invoke(tmp_path, canned)

    assert rc == 0, f"script exited {rc}"
    writes = _writes(_calls(log))
    assert len(writes) == 2, f"expected 2 POSTs, got {writes}"
    for write in writes:
        payload = json.loads(write["body"])
        assert not _contains_comment_key(payload), (
            f"POST body for ruleset {write['ruleset_name']!r} contains a "
            f'"_comment" key at some depth — the real API 422s on this: '
            f"{write['body']}"
        )


# ---------------------------------------------------------------------------
# Issue #202: the "_comment" strip must be applied symmetrically to the
# comparison (desired vs live) and to the write payload, or a live
# ruleset that is genuinely identical to the desired config would be
# misdetected as drifted on every run (perpetual-drift trap).
# ---------------------------------------------------------------------------


def test_no_perpetual_drift_when_live_body_is_comment_free(
    tmp_path: Path,
) -> None:
    """A comment-free live body matching the comment-free desired -> no-op.

    Simulates the ONLY realistic post-#202-fix live state: GET responses
    from the real API never carry "_comment" (Bug 2's premise). If the
    script strips "_comment" from ``desired`` before comparing to the
    (already comment-free) live body, this is a genuine no-op — zero
    writes. If the strip is only applied on one side of the comparison
    (e.g. only when building the write payload, not the diff), the
    comment-free live body would never match the still-commented
    ``desired`` and the script would PUT on every single run even
    though nothing has actually changed — the perpetual-drift trap.
    """
    canned = tmp_path / "canned"
    canned.mkdir()
    (canned / "list.body").write_text(
        json.dumps(
            [
                {"id": 11, "name": "harness-main-no-merge"},
                {"id": 22, "name": "harness-feature-daemon-only"},
            ]
        ),
        encoding="utf-8",
    )
    main_body = _strip_comments(
        json.loads(
            (HARNESS / "config" / "ruleset.main.json").read_text(
                encoding="utf-8"
            )
        )
    )
    main_body["bypass_actors"][0]["actor_id"] = 5
    (canned / "byid_11.body").write_text(
        json.dumps(main_body), encoding="utf-8"
    )
    feature_body = _strip_comments(
        json.loads(
            (HARNESS / "config" / "ruleset.feature.json").read_text(
                encoding="utf-8"
            )
        )
    )
    feature_body["bypass_actors"][0]["actor_id"] = 111
    (canned / "byid_22.body").write_text(
        json.dumps(feature_body), encoding="utf-8"
    )

    rc, _stdout, log = _invoke(tmp_path, canned)

    assert rc == 0, f"script exited {rc}"
    assert _writes(_calls(log)) == [], (
        "a live ruleset that is comment-free and otherwise identical to "
        "the (comment-stripped) desired config must be a no-op — a "
        "non-empty write list here means the strip is not applied "
        "symmetrically to the comparison and the write payload "
        "(perpetual-drift trap)"
    )


# ---------------------------------------------------------------------------
# Issue #202 follow-up: _lookup_id must fail closed on a malformed LIST body,
# not swallow the Python json.loads parse error via `|| true`.
# ---------------------------------------------------------------------------


def test_lookup_id_fails_closed_on_malformed_list_body(
    tmp_path: Path,
) -> None:
    """LIST returns HTTP 200 with a non-JSON body -> script must fail closed.

    ``_lookup_id`` pipes the LIST response through a Python one-liner
    that calls ``json.loads`` and prints a matching ruleset id. The
    invocation ends with ``2>/dev/null || true``, so when the LIST
    endpoint returns HTTP 200 (gh exits 0, no stderr) but a body that
    is not valid JSON, ``json.loads`` raises, the resulting non-zero
    Python exit is swallowed by ``|| true``, and ``_lookup_id`` returns
    an empty string exactly as it would for a genuinely absent
    ruleset. The caller (``_apply_ruleset``) then can't distinguish
    "ruleset absent" from "LIST response unparseable" and issues a
    spurious POST-create against a repo whose actual ruleset state is
    unknown.

    A malformed LIST body must instead be treated as a hard failure:
    the script must exit non-zero and must not perform any POST/PUT
    ruleset write.
    """
    canned = tmp_path / "canned"
    canned.mkdir()
    # No usable list.body — the LIST endpoint serves a non-JSON body via
    # the "list_malformed" marker instead (see fake_gh doc comment).
    (canned / "list_malformed").write_text("1", encoding="utf-8")

    rc, stdout, log = _invoke(tmp_path, canned)

    assert rc != 0, (
        f"a malformed (non-JSON) LIST body must fail the script closed, "
        f"not exit 0; stdout:\n{stdout}"
    )
    assert _writes(_calls(log)) == [], (
        "a malformed LIST body must never reach the write phase — the "
        "script cannot know whether the target rulesets already exist, "
        "so issuing a POST-create here would be a spurious write against "
        "unknown repo state"
    )
