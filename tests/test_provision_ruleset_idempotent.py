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
   cross-check is being skipped, then proceed to the write phase.
8. App-authed but mismatched .id -> still hard-fails (regression
   guard; the soften must not weaken this).
9. App-authed with matching .id -> still proceeds (regression guard).
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
            when app_authenticated is False.
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
    if not app_authenticated:
        # Presence of this marker tells the shim to simulate a 401 on
        # GET /app, as a real PAT-authenticated gh would get.
        (canned_state_dir / "app_unauthenticated").write_text(
            "1", encoding="utf-8"
        )
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
    main_body = json.loads(
        (HARNESS / "config" / "ruleset.main.json").read_text(encoding="utf-8")
    )
    main_body["bypass_actors"][0]["actor_id"] = 5  # BH_ADMIN_ROLE_ID
    (canned / "byid_11.body").write_text(
        json.dumps(main_body), encoding="utf-8"
    )

    feature_body = json.loads(
        (HARNESS / "config" / "ruleset.feature.json").read_text(
            encoding="utf-8"
        )
    )
    feature_body["bypass_actors"][0]["actor_id"] = 111  # BH_GITHUB_APP_ID
    (canned / "byid_22.body").write_text(
        json.dumps(feature_body), encoding="utf-8"
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
    # Main ruleset is canonical — no drift.
    main_body = json.loads(
        (HARNESS / "config" / "ruleset.main.json").read_text(encoding="utf-8")
    )
    main_body["bypass_actors"][0]["actor_id"] = 5
    (canned / "byid_11.body").write_text(
        json.dumps(main_body), encoding="utf-8"
    )
    # Feature ruleset has been mutated (bypass cleared — workers could merge).
    feature_drifted = json.loads(
        (HARNESS / "config" / "ruleset.feature.json").read_text(
            encoding="utf-8"
        )
    )
    feature_drifted["bypass_actors"] = []
    (canned / "byid_22.body").write_text(
        json.dumps(feature_drifted), encoding="utf-8"
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
    main_body = json.loads(
        (HARNESS / "config" / "ruleset.main.json").read_text(encoding="utf-8")
    )
    main_body["bypass_actors"][0]["actor_id"] = 5
    (canned / "byid_99.body").write_text(
        json.dumps(main_body), encoding="utf-8"
    )
    feature_body = json.loads(
        (HARNESS / "config" / "ruleset.feature.json").read_text(
            encoding="utf-8"
        )
    )
    feature_body["bypass_actors"][0]["actor_id"] = 111
    (canned / "byid_77.body").write_text(
        json.dumps(feature_body), encoding="utf-8"
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

    Regression-protection: verifies the --paginate --slurp path does NOT
    break existing single-page behaviour (the common case).
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
    main_body = json.loads(
        (HARNESS / "config" / "ruleset.main.json").read_text(encoding="utf-8")
    )
    main_body["bypass_actors"][0]["actor_id"] = 5
    (canned / "byid_11.body").write_text(
        json.dumps(main_body), encoding="utf-8"
    )
    feature_body = json.loads(
        (HARNESS / "config" / "ruleset.feature.json").read_text(
            encoding="utf-8"
        )
    )
    feature_body["bypass_actors"][0]["actor_id"] = 111
    (canned / "byid_22.body").write_text(
        json.dumps(feature_body), encoding="utf-8"
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
    (--paginate --slurp), the shim emits a two-page slurped response and
    the script must discover both rulesets and write nothing.
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
    main_body = json.loads(
        (HARNESS / "config" / "ruleset.main.json").read_text(encoding="utf-8")
    )
    main_body["bypass_actors"][0]["actor_id"] = 5
    (canned / "byid_55.body").write_text(
        json.dumps(main_body), encoding="utf-8"
    )
    feature_body = json.loads(
        (HARNESS / "config" / "ruleset.feature.json").read_text(
            encoding="utf-8"
        )
    )
    feature_body["bypass_actors"][0]["actor_id"] = 111
    (canned / "byid_66.body").write_text(
        json.dumps(feature_body), encoding="utf-8"
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
          not App-authenticated, and
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
    for leaked_marker in ("Bearer ", "ghp_", "gho_", "github_pat_"):
        assert leaked_marker not in combined, (
            f"possible credential material leaked in output: "
            f"{leaked_marker!r} found"
        )


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
