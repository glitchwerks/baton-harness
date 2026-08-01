"""Slice 3b — bin/provision-ruleset.sh idempotency.

Drives the bash script with a fake gh AND a fake curl on PATH
(``tests/fixtures/fake_gh/gh`` and ``tests/fixtures/fake_curl/curl``)
that both record every call to a shared log file and return canned
responses. gh serves every endpoint except the GET /app preflight:

  - LIST:  GET /repos/<owner>/<repo>/rulesets  (returns array of
           {id, name})
  - BY-ID: GET /repos/<owner>/<repo>/rulesets/<id>  (returns single
           object)

curl serves the GET /app preflight only (issue #326 — see that section
near the end of this file); the old `gh api app`-based preflight cases
are the "REMOVED" / "superseded" items in the numbered list below.

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
5. REMOVED (issue #326) — the old gh-based B3 preflight-mismatch test
   is superseded by the curl-based "success, ID mismatch" test in the
   "Issue #326" section below.
6. Admin-bypass actor_id logging: script logs the resolved actor_id
   before any writes so an operator can verify before enforcement.

History of the GET /app preflight mechanism (issues #199 through #201,
superseded by #326 — kept for context; none of cases 7/7b/8/9/10/11
below exist as test functions any more):

  GET /app is App-JWT-only. Issue #199 first tried softening a
  PAT-authenticated gh's 401 into skip+proceed. Issue #200 then always
  minted a real App JWT before this call, so a confirmed 401 became a
  hard-fail instead (case 7) alongside softening only a genuine no-.id
  success body (case 7b), with mismatched/matching regression guards
  (cases 8/9). Adversarial review of PR #201 found that ANY non-zero
  `gh api app` exit — a genuine 401 but also a transient 5xx, a
  rate-limit, a network blip — was being collapsed into the same
  soften path; case 10 narrowed the soften to a stderr match on
  literal "HTTP 401". CodeRabbit review of PR #201 (commit 6e9287a)
  then found that a bare "401" substring anywhere in stderr (e.g. a
  correlation id) could false-trigger that same match; case 11
  narrowed it again to require the "HTTP 401" substring specifically.

  Issue #326 replaces the entire mechanism instead of patching it
  further: `gh api app` cannot work here at all, because `gh api`
  unconditionally sends `Authorization: token <value>` while GitHub's
  API requires `Authorization: Bearer <value>` for App JWTs
  (https://github.com/cli/cli/issues/12828 — confirmed via a live A/B
  test: the identical JWT gets 200 via `curl -H "Authorization: Bearer
  $JWT"` and 401 "could not be decoded" via `gh api app`). The
  preflight now calls curl directly and inspects the REAL HTTP status
  code curl reports, which eliminates the whole stray-substring class
  of bug cases 10/11 existed to guard against — there is no more
  stderr text to stray-match against. See the "Issue #326" section
  near the end of this file for the six replacement test cases and
  ``tests/fixtures/fake_curl/curl`` for the new shim.

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
FAKE_CURL_DIR = HARNESS / "tests" / "fixtures" / "fake_curl"

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
    app_get_outcome: str = "success",
    app_http_status: str = "503",
    return_stderr: bool = False,
) -> tuple[int, str, Path] | tuple[int, str, str, Path]:
    """Run the provisioning script with the fake gh AND fake curl on PATH.

    Issue #326: the GET /app preflight now calls curl (not `gh api
    app`), so the "app_*" scenario knobs from before that issue have
    been replaced by app_get_outcome / app_http_status, which drive
    ``tests/fixtures/fake_curl/curl``'s marker files instead of the old
    ``tests/fixtures/fake_gh/gh`` "get_app" marker files.

    Args:
        tmp_path: Pytest-provided temp directory for this test.
        canned_state_dir: Directory containing canned response files
            read by both the fake gh and fake curl shims.
        app_id: Value of BH_GITHUB_APP_ID passed to the script.
        preflight_app_id: The id the fake curl GET /app endpoint
            returns in the "success" outcome (may differ from app_id
            to trigger a B3 mismatch). Ignored for every other
            app_get_outcome value.
        admin_role_id: Value of BH_ADMIN_ROLE_ID passed to the
            script; defaults to the spec default of "5".
        admin_collaborators_body: Optional canned JSON body for
            GET /collaborators?permission=admin. Defaults to one
            collaborator with role_name="admin".
        custom_roles_body: Optional canned JSON body for
            GET /orgs/.../custom-repository-roles. When absent, the
            fake gh returns a 404-style "feature not available" error.
        app_get_outcome: Selects which fake-curl GET /app scenario to
            can. One of:
              "success" (default) — HTTP 200, body {"id": <the
                  preflight_app_id value>}. Compared against app_id
                  exactly as before — a match proceeds, a mismatch
                  hard-fails with exit 2.
              "no_id" — HTTP 200, body has no "id" field (e.g. {}).
                  Soften-and-skip: warn, then proceed to writes.
              "http_401" — a genuine HTTP 401 response (curl itself
                  still exits 0 — the transfer succeeded, the server
                  said no). Soften-and-skip: warn, then proceed.
               "http_other" — any HTTP status that is neither 200 nor
                   401 (value set via app_http_status, default "503").
                   Hard-fails with exit 1, zero writes.
              "malformed_body" — HTTP 200 with a body that is not
                  valid JSON. Hard-fails with exit 1, zero writes.
               "transport_failure" — curl itself fails before
                  completing the transfer (DNS/network/TLS). Hard
                  -fails with exit 1, zero writes.
        app_http_status: The status code fake curl reports when
            app_get_outcome is "http_other". Ignored otherwise.
        return_stderr: When True, return a four-tuple that also
            includes the combined stderr, for tests that assert on
            preflight warning text.

    Returns:
        A three-tuple of (returncode, combined_stdout, gh_call_log_path)
        by default, or a four-tuple that inserts stderr before the log
        path when return_stderr is True. The log path is shared by
        both shims — see ``_curl_calls`` to filter to fake-curl
        records only.
    """
    log_path = tmp_path / "gh_calls.jsonl"
    # Reused by fake_curl's "success" outcome (and, harmlessly, still
    # read by fake_gh's now-unused "get_app" case — see that shim's
    # header comment).
    (canned_state_dir / "app_id.txt").write_text(
        preflight_app_id, encoding="utf-8"
    )
    if app_get_outcome == "no_id":
        (canned_state_dir / "curl_no_id").write_text("1", encoding="utf-8")
    elif app_get_outcome == "http_401":
        (canned_state_dir / "curl_http_401").write_text("1", encoding="utf-8")
    elif app_get_outcome == "http_other":
        (canned_state_dir / "curl_http_status.txt").write_text(
            app_http_status, encoding="utf-8"
        )
    elif app_get_outcome == "malformed_body":
        (canned_state_dir / "curl_malformed_body").write_text(
            "1", encoding="utf-8"
        )
    elif app_get_outcome == "transport_failure":
        (canned_state_dir / "curl_transport_failure").write_text(
            "1", encoding="utf-8"
        )
    elif app_get_outcome != "success":
        raise ValueError(f"unknown app_get_outcome: {app_get_outcome!r}")
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
                str(FAKE_CURL_DIR),
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
        # this suite's pre-existing cases keep exercising the ruleset
        # write/idempotency behavior without needing real BWS_* secrets.
        # #326: the App JWT this produces is also what the curl-based
        # preflight must send as "Authorization: Bearer <this value>" —
        # see the "Issue #326" test section, which pins the exact header.
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
    """Parse the combined gh + curl call log as a list of dicts.

    Args:
        log_path: Path to the JSONL file written by both the fake gh
            and fake curl shims (see ``_invoke``'s BH_FAKE_GH_LOG).

    Returns:
        List of call-record dicts, one per gh or curl invocation.
        Empty list if the log file does not exist (no calls were made).
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


def _curl_calls(calls: list[dict]) -> list[dict]:  # type: ignore[type-arg]
    """Filter a call list to only fake-curl records (issue #326).

    fake_curl's JSONL records carry an additive ``"tool":"curl"`` field
    that fake_gh's records never carry, so this is a simple filter over
    the same combined log ``_calls`` returns.

    Args:
        calls: Call records from ``_calls``.

    Returns:
        Subset of records written by ``tests/fixtures/fake_curl/curl``.
    """
    return [c for c in calls if c.get("tool") == "curl"]


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
# Issue #326: GET /app preflight replaced with curl + real HTTP status
# codes.
#
# `gh api app` cannot authenticate this call: `gh api` unconditionally
# sends `Authorization: token <value>`, but GitHub's API requires
# `Authorization: Bearer <value>` for App JWTs
# (https://github.com/cli/cli/issues/12828 — confirmed via a live A/B
# test: the identical JWT gets 200 via `curl -H "Authorization: Bearer
# $JWT"` and 401 "could not be decoded" via `gh api app`). The preflight
# now shells out to curl directly, capturing the real HTTP status code
# via `-w '%{http_code}'` instead of sniffing gh's stderr text — see
# ``tests/fixtures/fake_curl/curl`` for the exact invocation shape this
# suite expects the implementation to use.
#
# Because real status codes replace stderr text-matching, the whole
# stray-substring class of bug the old cases 10/11 existed to guard
# against (a bare "401" appearing incidentally in stderr) no longer has
# an attack surface — there is no more stderr to stray-match. The six
# cases below are the complete replacement contract:
#
#   1. Success, ID matches -> preflight OK, proceeds to write rulesets.
#   2. Success, ID mismatch -> PREFLIGHT FAILURE, exit 2, zero writes.
#   3. HTTP 200, no .id in body -> warn + skip + proceed.
#   4. HTTP 401 -> warn + skip + proceed (curl itself still exits 0;
#      the *server* said no, which is not a transport failure).
#   5. HTTP 503 (any non-200/non-401 status) -> hard fail, exit 1, zero
#      writes.
#   6. Genuine curl transport failure (non-zero curl exit, no HTTP
#      response at all) -> hard fail, exit 1, zero writes.
#
# Contract gap, deliberately resolved (not silently dropped): case 4b
# covers "HTTP 200 with a body that is not valid JSON" (for example, a
# truncated response or an HTML error page served with a 200 status).
# This is distinct from case 3, where the JSON body is valid but has no
# .id field. An unparseable body fails closed, matching the analogous
# malformed-body treatment on the LIST endpoint (see ``list_malformed``
# / ``test_lookup_id_fails_closed_on_malformed_list_body`` below). The
# rejected alternative would have folded a parse failure into case 3's
# soften-and-skip path merely because neither response yields an .id;
# keeping them separate prevents a malformed response from being
# mistaken for an affirmative no-.id result. The fake-curl router wires
# this outcome through ``curl_malformed_body``, and
# ``test_preflight_curl_http_200_malformed_body_hard_fails`` enforces the
# fail-closed contract below. Together with the other four marker-driven
# outcomes and the default success path, all six router sub-scenarios
# are covered.
#
# Every case below additionally asserts a curl call was actually
# recorded (via ``_curl_calls``), carrying the Bearer-scheme
# Authorization header the App JWT requires. This is deliberate, not
# redundant with the rc/writes assertions: for cases 1 and 2, the
# *outcome* (exit 0 + 2 writes / exit 2 + zero writes) is
# indistinguishable from what the OLD gh-based preflight already
# produces on a match/mismatch — since _invoke no longer writes any of
# the old fake_gh "get_app" markers, the pre-#326 implementation falls
# through to fake_gh's default (matching) GET /app response regardless
# of which app_get_outcome this suite requests. Without the
# curl-was-actually-called assertion, cases 1 and 2 would misreport
# green against an implementation that still calls `gh api app` and
# never touches curl at all — the exact "red for the wrong reason" trap
# a byte-for-byte outcome match can hide.
# ---------------------------------------------------------------------------


def test_preflight_curl_success_id_match_proceeds(tmp_path: Path) -> None:
    """Case 1: curl GET /app returns 200 with a matching .id -> proceeds.

    Mirrors the pre-#326 "App-authed with matching .id" regression
    guard, but via the curl-based mechanism: the script must send the
    App JWT as a Bearer credential and read the REAL 200 status curl
    reports, not sniff any stderr text.
    """
    canned = tmp_path / "canned"
    canned.mkdir()
    (canned / "list.body").write_text("[]", encoding="utf-8")

    rc, stdout, log = _invoke(
        tmp_path,
        canned,
        app_id="111",
        preflight_app_id="111",
        app_get_outcome="success",
    )

    curl_calls = _curl_calls(_calls(log))
    assert curl_calls, (
        "expected the script to call curl for the GET /app preflight; "
        "got zero fake-curl call-log records — the implementation is "
        "still using the old gh-based preflight (or never made the "
        f"call at all). Full call log: {_calls(log)}"
    )
    assert curl_calls[0]["auth_header"] == (
        "Authorization: Bearer fake-jwt-for-idempotency-tests"
    ), (
        "expected the App JWT to be sent as a Bearer credential (the "
        "actual bug under test — gh api app always sends "
        "'Authorization: token ...' instead); got auth_header="
        f"{curl_calls[0]['auth_header']!r}"
    )

    assert rc == 0, f"script exited {rc}"
    assert "preflight ok" in stdout.lower(), (
        f"expected a preflight-OK message in stdout; stdout was:\n{stdout}"
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


def test_preflight_curl_success_id_mismatch_hard_fails(
    tmp_path: Path,
) -> None:
    """Case 2: curl GET /app returns 200 with a mismatched .id -> exit 2.

    Mirrors the pre-#326 B3 preflight-mismatch case, but via curl: the
    real 200 status plus a parsed .id that disagrees with
    BH_GITHUB_APP_ID must still abort before any ruleset write.
    """
    canned = tmp_path / "canned"
    canned.mkdir()
    (canned / "list.body").write_text("[]", encoding="utf-8")

    rc, stdout, stderr, log = _invoke(
        tmp_path,
        canned,
        app_id="222",
        preflight_app_id="111",
        app_get_outcome="success",
        return_stderr=True,
    )

    curl_calls = _curl_calls(_calls(log))
    assert curl_calls, (
        "expected the script to call curl for the GET /app preflight; "
        "got zero fake-curl call-log records. Full call log: "
        f"{_calls(log)}"
    )
    assert curl_calls[0]["auth_header"] == (
        "Authorization: Bearer fake-jwt-for-idempotency-tests"
    ), (
        f"expected a Bearer-scheme Authorization header; got "
        f"auth_header={curl_calls[0]['auth_header']!r}"
    )

    assert rc == 2, (
        f"expected exit 2 for a curl-confirmed App-ID mismatch, got "
        f"{rc}\nstdout:\n{stdout}\nstderr:\n{stderr}"
    )
    assert "PREFLIGHT FAILURE" in stderr, (
        f"expected the existing hard-fail banner to be preserved; "
        f"stderr was:\n{stderr}"
    )
    assert "BH_GITHUB_APP_ID=222" in stderr, (
        f"expected the configured (wrong) app id in the failure "
        f"message; stderr was:\n{stderr}"
    )
    assert "111" in stderr, (
        f"expected the live .id GET /app actually returned in the "
        f"failure message; stderr was:\n{stderr}"
    )
    assert _writes(_calls(log)) == [], (
        "script must write zero ruleset mutations on a confirmed "
        "App-ID mismatch"
    )


def test_preflight_curl_http_200_no_id_skips_and_proceeds(
    tmp_path: Path,
) -> None:
    """Case 3: curl GET /app returns 200 with no .id -> warn+skip+proceed.

    A successful response whose body has no "id" field is not evidence
    of a *wrong* App ID — it must soften into skip+proceed exactly like
    a confirmed non-App-auth response, not hard-fail as a mismatch.
    """
    canned = tmp_path / "canned"
    canned.mkdir()
    (canned / "list.body").write_text("[]", encoding="utf-8")

    rc, stdout, stderr, log = _invoke(
        tmp_path, canned, app_get_outcome="no_id", return_stderr=True
    )

    curl_calls = _curl_calls(_calls(log))
    assert curl_calls, (
        "expected the script to call curl for the GET /app preflight; "
        f"got zero fake-curl call-log records. Full call log: "
        f"{_calls(log)}"
    )
    assert curl_calls[0]["auth_header"] == (
        "Authorization: Bearer fake-jwt-for-idempotency-tests"
    ), (
        f"expected a Bearer-scheme Authorization header; got "
        f"auth_header={curl_calls[0]['auth_header']!r}"
    )
    assert curl_calls[0]["http_code"] == "200", (
        f"expected the fake curl shim to report HTTP 200 for the "
        f"no-id scenario; got http_code={curl_calls[0]['http_code']!r}"
    )

    assert rc == 0, (
        f"a 200 response with no .id must not hard-fail the preflight; "
        f"got rc={rc}\nstdout:\n{stdout}\nstderr:\n{stderr}"
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
        f"a no-.id 200 response must not be reported as a preflight "
        f"failure; stderr was:\n{stderr}"
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


def test_preflight_curl_http_401_skips_and_proceeds(tmp_path: Path) -> None:
    """Case 4: curl GET /app returns a real HTTP 401 -> warn+skip+proceed.

    curl itself exits 0 here (the transfer succeeded; the server
    responded with 401) — this is the "confirmed 401" scenario the
    pre-#326 suite covered via gh's stderr text; under the curl-based
    contract it is confirmed via the actual status code instead, so it
    still softens into skip+proceed rather than hard-failing.
    """
    canned = tmp_path / "canned"
    canned.mkdir()
    (canned / "list.body").write_text("[]", encoding="utf-8")

    rc, stdout, stderr, log = _invoke(
        tmp_path, canned, app_get_outcome="http_401", return_stderr=True
    )

    curl_calls = _curl_calls(_calls(log))
    assert curl_calls, (
        "expected the script to call curl for the GET /app preflight; "
        f"got zero fake-curl call-log records. Full call log: "
        f"{_calls(log)}"
    )
    assert curl_calls[0]["auth_header"] == (
        "Authorization: Bearer fake-jwt-for-idempotency-tests"
    ), (
        f"expected a Bearer-scheme Authorization header; got "
        f"auth_header={curl_calls[0]['auth_header']!r}"
    )
    assert curl_calls[0]["http_code"] == "401", (
        f"expected the fake curl shim to report HTTP 401; got "
        f"http_code={curl_calls[0]['http_code']!r}"
    )

    assert rc == 0, (
        f"a confirmed HTTP 401 from GET /app must soften into "
        f"skip+proceed, not hard-fail; got rc={rc}\nstdout:\n{stdout}\n"
        f"stderr:\n{stderr}"
    )
    stderr_lower = stderr.lower()
    assert "skip" in stderr_lower, (
        f"expected a 'skip' warning on stderr for a confirmed 401; "
        f"stderr was:\n{stderr}"
    )
    assert "preflight failure" not in stderr_lower, (
        f"a confirmed 401 must not be reported as a preflight failure; "
        f"stderr was:\n{stderr}"
    )
    writes = _writes(_calls(log))
    assert len(writes) == 2, (
        f"expected the script to proceed to create both rulesets after "
        f"skipping the App-ID check on a confirmed 401, got "
        f"writes={writes}"
    )


def test_preflight_curl_non_200_non_401_status_hard_fails(
    tmp_path: Path,
) -> None:
    """Case 5: curl GET /app returns 503 -> hard fail, exit 1, zero writes.

    Any HTTP status that is neither 200 nor 401 (403, 404, 500, 503,
    ...) must hard-fail exactly like a genuine transport failure — with
    real status codes there is no more stderr text to stray-match, so
    this single case replaces the old cases 10 AND 11 (which existed
    only to guard against stderr-substring false positives).

    Asserting rc == 1 specifically (not merely rc != 0) matters: a bare
    `set -e` abort on curl's own non-zero-status handling would not
    reliably produce this exact code or print the failure banner below
    — the implementation must explicitly branch on the status code and
    emit both.
    """
    canned = tmp_path / "canned"
    canned.mkdir()
    (canned / "list.body").write_text("[]", encoding="utf-8")

    rc, stdout, stderr, log = _invoke(
        tmp_path,
        canned,
        app_get_outcome="http_other",
        app_http_status="503",
        return_stderr=True,
    )

    curl_calls = _curl_calls(_calls(log))
    assert curl_calls, (
        "expected the script to call curl for the GET /app preflight; "
        f"got zero fake-curl call-log records. Full call log: "
        f"{_calls(log)}"
    )
    assert curl_calls[0]["http_code"] == "503", (
        f"expected the fake curl shim to report HTTP 503; got "
        f"http_code={curl_calls[0]['http_code']!r}"
    )

    assert rc == 1, (
        f"expected exit 1 for a non-200/non-401 GET /app status "
        f"(distinct from the exit-2 mismatch path); got rc={rc}\n"
        f"stdout:\n{stdout}\nstderr:\n{stderr}"
    )
    assert "PREFLIGHT FAILURE" in stderr, (
        f"expected the existing hard-fail banner to be preserved; "
        f"stderr was:\n{stderr}"
    )
    assert "GET /app" in stderr, (
        f"expected stderr to reference the failed GET /app call; "
        f"stderr was:\n{stderr}"
    )
    assert "503" in stderr, (
        f"expected stderr to surface the underlying HTTP status; "
        f"stderr was:\n{stderr}"
    )
    stderr_lower = stderr.lower()
    assert "skip" not in stderr_lower, (
        f"a non-200/non-401 status must not be reported as a skip; "
        f"stderr was:\n{stderr}"
    )
    assert _writes(_calls(log)) == [], (
        "a non-200/non-401 GET /app status must write zero ruleset "
        "mutations — an unconfirmed App ID must never reach the write "
        "phase"
    )


def test_preflight_curl_transport_failure_hard_fails(
    tmp_path: Path,
) -> None:
    """Case 6: a genuine curl transport failure -> hard fail, zero writes.

    Simulates curl itself failing before completing the transfer (DNS
    failure, network unreachable, TLS error, ...) — there is no HTTP
    response at all, so there is no status code to inspect. This must
    fail exactly like case 5 (exit 1, zero writes), and the underlying
    curl failure must be surfaced on stderr.
    """
    canned = tmp_path / "canned"
    canned.mkdir()
    (canned / "list.body").write_text("[]", encoding="utf-8")

    rc, stdout, stderr, log = _invoke(
        tmp_path,
        canned,
        app_get_outcome="transport_failure",
        return_stderr=True,
    )

    curl_calls = _curl_calls(_calls(log))
    assert curl_calls, (
        "expected the script to attempt curl for the GET /app "
        f"preflight; got zero fake-curl call-log records. Full call "
        f"log: {_calls(log)}"
    )
    assert curl_calls[0]["http_code"] == "", (
        "a genuine transport failure means no HTTP response was ever "
        "received, so the fake curl shim reports an empty http_code; "
        f"got http_code={curl_calls[0]['http_code']!r} — the "
        "implementation may be substituting a fabricated status "
        "instead of detecting curl's own non-zero exit"
    )

    assert rc == 1, (
        f"expected exit 1 for a genuine curl transport failure; got "
        f"rc={rc}\nstdout:\n{stdout}\nstderr:\n{stderr}"
    )
    assert "PREFLIGHT FAILURE" in stderr, (
        f"expected the existing hard-fail banner to be preserved; "
        f"stderr was:\n{stderr}"
    )
    assert "GET /app" in stderr, (
        f"expected stderr to reference the failed GET /app call; "
        f"stderr was:\n{stderr}"
    )
    assert "curl: (6) Could not resolve host: api.github.com" in stderr, (
        f"expected stderr to surface curl's transport error detail; "
        f"stderr was:\n{stderr}"
    )
    stderr_lower = stderr.lower()
    assert "skip" not in stderr_lower, (
        f"a genuine transport failure must not be reported as a skip; "
        f"stderr was:\n{stderr}"
    )
    assert _writes(_calls(log)) == [], (
        "a genuine curl transport failure must write zero ruleset mutations"
    )


def test_preflight_curl_http_200_malformed_body_hard_fails(
    tmp_path: Path,
) -> None:
    """Case 4b: HTTP 200 with an unparseable body must fail closed."""
    canned = tmp_path / "canned"
    canned.mkdir()
    (canned / "list.body").write_text("[]", encoding="utf-8")

    rc, _stdout, stderr, log = _invoke(
        tmp_path,
        canned,
        app_get_outcome="malformed_body",
        return_stderr=True,
    )

    calls = _calls(log)
    curl_calls = _curl_calls(calls)
    assert curl_calls, "expected curl GET /app preflight call"
    assert curl_calls[0]["http_code"] == "200", (
        f"expected malformed-body fixture to report HTTP 200; got "
        f"http_code={curl_calls[0]['http_code']!r}"
    )
    assert rc == 1, f"expected exit 1, got {rc}; stderr:\n{stderr}"
    assert "PREFLIGHT FAILURE" in stderr
    assert "GET /app" in stderr
    assert "unparseable JSON body" in stderr, (
        f"expected the malformed-body-specific failure detail; "
        f"stderr was:\n{stderr}"
    )
    assert "skip" not in stderr.lower(), (
        f"a malformed HTTP 200 body must fail closed, not skip; "
        f"stderr was:\n{stderr}"
    )
    assert _writes(calls) == []


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
