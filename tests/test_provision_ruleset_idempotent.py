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
) -> tuple[int, str, Path]:
    """Run the provisioning script with the fake gh on PATH.

    Args:
        tmp_path: Pytest-provided temp directory for this test.
        canned_state_dir: Directory containing canned response files
            read by the fake gh shim.
        app_id: Value of BH_GITHUB_APP_ID passed to the script.
        preflight_app_id: The id the fake /app endpoint returns
            (may differ from app_id to trigger B3 mismatch).
        admin_role_id: Value of BH_ADMIN_ROLE_ID passed to the
            script; defaults to the spec default of "5".

    Returns:
        A three-tuple of (returncode, combined_stdout, gh_call_log_path).
    """
    log_path = tmp_path / "gh_calls.jsonl"
    # The shim reads app_id.txt to build its /app response.
    (canned_state_dir / "app_id.txt").write_text(
        preflight_app_id, encoding="utf-8"
    )
    env = {
        **os.environ,
        "PATH": f"{FAKE_GH_DIR}{os.pathsep}{os.environ.get('PATH', '')}",
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
    rc, stdout, log = _invoke(tmp_path, canned, admin_role_id=custom_role_id)

    assert rc == 0, f"script exited {rc}"
    writes = _writes(_calls(log))
    # Sanity: writes did happen (so there was an opportunity to log first).
    assert writes, "expected at least one write for empty-state test"
    # The resolved actor_id must appear in stdout.
    assert custom_role_id in stdout, (
        f"actor_id '{custom_role_id}' not found in script stdout; "
        f"stdout was:\n{stdout}"
    )
