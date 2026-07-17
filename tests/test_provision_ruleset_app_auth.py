"""Issue #200 — bin/provision-ruleset.sh must obtain App-auth credentials.

via ``baton_harness.chain.app_auth`` instead of relying on ambient ``gh``
auth, and must never fall back to ambient auth if that fails.

Two new optional overrides the script must honour (agreed with the
router as the seam for this suite, replacing a direct
``python -m baton_harness.chain.app_auth {jwt|token}`` subprocess call
so tests do not need real BWS_*/PEM credentials):

  BH_APP_AUTH_JWT_CMD    — if non-empty, run this value as a shell
                            command to obtain the App JWT (captured
                            stdout), INSTEAD OF invoking
                            "$_PYTHON" -m baton_harness.chain.app_auth jwt.
  BH_APP_AUTH_TOKEN_CMD  — same, for the installation token, replacing
                            "$_PYTHON" -m baton_harness.chain.app_auth token.

When either override is unset, the script must fall back to the real
module invocation (default/production behavior) — see
``test_unset_overrides_fall_back_to_real_app_auth_module`` below.

Fail-fast (requirement #4) applies identically to both paths: a
non-zero exit from whichever command produced the credential — override
or real module — must abort the script before any ``gh api`` call that
needed it, with NO fallback to ambient ``gh`` auth.

Coverage:

1. Happy path
   (``test_jwt_used_for_app_preflight_and_token_used_for_repo_calls``):
   the JWT override's value is the bearer credential observed on the
   ``GET /app`` preflight call; the installation-token override's value
   is the bearer credential observed on EVERY ``repos/<slug>/...`` call
   (collaborators, rulesets LIST, rulesets POST-create) AND on the
   ``orgs/<owner>/custom-repository-roles`` call (exercised via a
   non-default admin role id). Neither call ever carries the other
   credential.

2. Fail-fast — JWT (``test_jwt_cmd_failure_aborts_before_any_gh_call``):
   ``BH_APP_AUTH_JWT_CMD`` exits non-zero -> script aborts (non-zero
   exit) before invoking ``gh`` at all — zero calls in the fake-gh log,
   proving no fallback to ambient auth for the preflight call.

3. Fail-fast — installation token
   (``test_token_cmd_failure_aborts_before_any_repo_scoped_gh_call``):
   ``BH_APP_AUTH_TOKEN_CMD`` exits non-zero (JWT succeeds) -> script
   aborts, zero writes, and zero calls to any ``repos/...``-scoped
   endpoint — regardless of whether the (JWT-only) preflight call had
   already run, proving no fallback to ambient auth for the
   installation-token-gated calls.

4. Non-leakage (``test_jwt_and_token_values_never_appear_in_script_output``):
   neither credential value appears anywhere in the script's own
   stdout/stderr on a successful run — the fake-gh call log is a
   separate test-instrumentation channel, not something a real log
   consumer would see.

5. Unset-override default path
   (``test_unset_overrides_fall_back_to_real_app_auth_module``): with
   both overrides unset, the script must attempt the real
   ``"$_PYTHON" -m baton_harness.chain.app_auth {jwt|token}``
   invocation. BWS_PEM_SECRET_ID / BWS_ACCESS_TOKEN are deliberately
   excluded from the subprocess environment (never inherited from the
   ambient environment, regardless of what may be set on the host) so
   this exercises the real module's fast, network-free "missing env"
   failure path (documented exit code 2) rather than risking a slow or
   flaky live network/auth attempt. Asserts the script fails closed
   (non-zero exit) with zero ``gh`` calls and zero writes.
"""

from __future__ import annotations

import hashlib
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

# Distinctive canned credential values — unlikely to collide with any
# incidental substring in script output, so a leakage assertion can use
# plain substring containment without false positives.
_FAKE_JWT = "fake-jwt-abc123-issue200"
_FAKE_TOKEN = "fake-install-token-xyz789-issue200"


def _fingerprint(value: str) -> str:
    """Mirror tests/fixtures/fake_gh/gh's `_fingerprint()` helper.

    First 12 hex chars of sha256(value), or "" for an empty value.
    Lets tests assert which credential (JWT vs installation token)
    was used as an env_gh_token/env_github_token bearer without the
    raw secret ever appearing in the JSONL call log.
    """
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Invocation helper
# ---------------------------------------------------------------------------


def _invoke(
    tmp_path: Path,
    canned_state_dir: Path,
    *,
    jwt_cmd: str | None = f"printf %s {_FAKE_JWT}",
    token_cmd: str | None = f"printf %s {_FAKE_TOKEN}",
    admin_role_id: str = "5",
    custom_roles_body: str | None = None,
    exclude_bws_env: bool = False,
    timeout: float = 60.0,
) -> tuple[int, str, str, Path]:
    """Run provision-ruleset.sh with the fake gh on PATH.

    Args:
        tmp_path: Pytest-provided temp directory for this test.
        canned_state_dir: Directory containing canned fake-gh responses.
        jwt_cmd: Value for BH_APP_AUTH_JWT_CMD, or None to leave unset
            (exercising the real-module fallback path).
        token_cmd: Value for BH_APP_AUTH_TOKEN_CMD, or None to leave
            unset.
        admin_role_id: Value of BH_ADMIN_ROLE_ID; a non-default value
            exercises the custom-repository-roles call.
        custom_roles_body: Optional canned body for
            GET /orgs/.../custom-repository-roles.
        exclude_bws_env: When True, BWS_PEM_SECRET_ID and
            BWS_ACCESS_TOKEN are stripped from the subprocess
            environment regardless of what the host has ambiently set,
            so the real app_auth module's env-validation path is
            exercised deterministically.
        timeout: Subprocess timeout in seconds — bounds the case where
            an unexpected fallback path attempts a live network call.

    Returns:
        (returncode, stdout, stderr, fake_gh_call_log_path).
    """
    log_path = tmp_path / "gh_calls.jsonl"
    (canned_state_dir / "app_id.txt").write_text("111", encoding="utf-8")
    (canned_state_dir / "collaborators_admin.body").write_text(
        json.dumps(
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
    if not (canned_state_dir / "list.body").exists():
        (canned_state_dir / "list.body").write_text("[]", encoding="utf-8")

    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("GH_TOKEN", "GITHUB_TOKEN")
        and not (
            exclude_bws_env and k in ("BWS_PEM_SECRET_ID", "BWS_ACCESS_TOKEN")
        )
    }
    env["PATH"] = os.pathsep.join(
        part
        for part in [
            str(FAKE_GH_DIR),
            _BASH_BIN_DIR,
            os.environ.get("PATH", ""),
        ]
        if part
    )
    env["BH_REPO_OWNER"] = "fake-owner"
    env["BH_REPO_NAME"] = "fake-repo"
    env["BH_GITHUB_APP_ID"] = "111"
    env["BH_GITHUB_APP_INSTALLATION_ID"] = "999999"
    env["BH_ADMIN_ROLE_ID"] = admin_role_id
    env["BH_FAKE_GH_LOG"] = str(log_path)
    env["BH_FAKE_GH_CANNED_DIR"] = str(canned_state_dir)
    # BH_PROJECT_ROOT deliberately left unset so the optional baseline
    # capture step (unrelated to this suite) is skipped, keeping the
    # expected call set small and deterministic.
    env.pop("BH_PROJECT_ROOT", None)

    if jwt_cmd is not None:
        env["BH_APP_AUTH_JWT_CMD"] = jwt_cmd
    else:
        env.pop("BH_APP_AUTH_JWT_CMD", None)
    if token_cmd is not None:
        env["BH_APP_AUTH_TOKEN_CMD"] = token_cmd
    else:
        env.pop("BH_APP_AUTH_TOKEN_CMD", None)

    try:
        proc = subprocess.run(
            [_BASH, str(SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            "provision-ruleset.sh did not exit within "
            f"{timeout}s — this suggests an unexpected fallback path "
            "(e.g. a live network call) rather than a fast, "
            "deterministic failure. "
            f"stdout so far: {exc.stdout!r}\nstderr so far: {exc.stderr!r}"
        ) from exc

    if proc.returncode != 0:
        print(
            f"\n--- provision-ruleset.sh stdout (rc={proc.returncode}) ---\n"
            f"{proc.stdout}"
            f"--- provision-ruleset.sh stderr ---\n"
            f"{proc.stderr}"
            f"---"
        )
    return proc.returncode, proc.stdout, proc.stderr, log_path


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


def _repo_scoped_calls(calls: list[dict]) -> list[dict]:  # type: ignore[type-arg]
    """Filter a call list to calls against a repos/<slug>/... URL.

    Args:
        calls: Call records from ``_calls``.

    Returns:
        Subset of records whose url starts with "repos/".
    """
    return [c for c in calls if c["url"].startswith("repos/")]


def _credential_blob(record: dict) -> str:  # type: ignore[type-arg]
    """Concatenate every auth-observability field on a call record.

    The script may pass a bearer credential to ``gh api`` via an
    ``-H "Authorization: ..."`` header, via ``GH_TOKEN``, or via
    ``GITHUB_TOKEN`` — this helper is agnostic to which mechanism the
    implementation chooses. The env_gh_token/env_github_token fields
    contain fingerprints rather than raw values, so tests can assert
    on credential identity without pinning a specific passing mechanism.

    Args:
        record: A single call record from ``_calls``.

    Returns:
        Space-joined string of the auth_header / env_gh_token /
        env_github_token fields (each defaulting to "" if absent).
    """
    return " ".join(
        [
            record.get("auth_header", ""),
            record.get("env_gh_token", ""),
            record.get("env_github_token", ""),
        ]
    )


# ---------------------------------------------------------------------------
# Case 1: happy path — correct credential per call group
# ---------------------------------------------------------------------------


def test_jwt_used_for_app_preflight_and_token_used_for_repo_calls(
    tmp_path: Path,
) -> None:
    """JWT bearer on GET /app; installation token bearer on every repo call.

    Uses a non-default admin role id (with a matching canned custom-roles
    body) so the ``orgs/<owner>/custom-repository-roles`` call — which
    the spec groups with the installation-token-gated calls despite its
    ``orgs/`` URL prefix — is also exercised and checked.
    """
    canned = tmp_path / "canned"
    canned.mkdir()
    (canned / "list.body").write_text("[]", encoding="utf-8")

    rc, stdout, stderr, log = _invoke(
        tmp_path,
        canned,
        admin_role_id="42",
        custom_roles_body=json.dumps(
            [{"id": 42, "name": "Platform Admin", "base_role": "admin"}]
        ),
    )

    assert rc == 0, (
        f"expected a successful run; rc={rc}\n"
        f"stdout:\n{stdout}\nstderr:\n{stderr}"
    )
    calls = _calls(log)
    assert calls, "expected the script to make at least one gh call"

    app_calls = [c for c in calls if c["endpoint"] == "get_app"]
    assert len(app_calls) == 1, f"expected exactly 1 GET /app call; {calls}"
    app_cred = _credential_blob(app_calls[0])
    assert _fingerprint(_FAKE_JWT) in app_cred, (
        f"GET /app call must carry the App JWT as its bearer credential; "
        f"observed credential fields: {app_calls[0]!r}"
    )
    assert _fingerprint(_FAKE_TOKEN) not in app_cred, (
        f"GET /app call must NOT carry the installation token; "
        f"observed credential fields: {app_calls[0]!r}"
    )

    other_calls = [c for c in calls if c["endpoint"] != "get_app"]
    assert other_calls, (
        "expected at least one non-preflight call (collaborators/roles/"
        f"rulesets); calls={calls!r}"
    )
    for call in other_calls:
        cred = _credential_blob(call)
        assert _fingerprint(_FAKE_TOKEN) in cred, (
            f"call to endpoint {call['endpoint']!r} (url={call['url']!r}) "
            f"must carry the installation token as its bearer credential; "
            f"observed credential fields: {call!r}"
        )
        assert _fingerprint(_FAKE_JWT) not in cred, (
            f"call to endpoint {call['endpoint']!r} (url={call['url']!r}) "
            f"must NOT carry the App JWT; observed credential fields: "
            f"{call!r}"
        )

    # Sanity: both the admin-collaborators and custom-repository-roles
    # endpoints were actually exercised, so the assertions above cover
    # both call shapes named in requirement 2, not just the rulesets
    # endpoints.
    endpoints_seen = {c["endpoint"] for c in other_calls}
    assert "get_admin_collaborators" in endpoints_seen, calls
    assert "get_custom_roles" in endpoints_seen, calls
    assert "post_create" in endpoints_seen, calls


# ---------------------------------------------------------------------------
# Case 2: JWT command failure — fail fast, zero gh calls
# ---------------------------------------------------------------------------


def test_jwt_cmd_failure_aborts_before_any_gh_call(tmp_path: Path) -> None:
    """BH_APP_AUTH_JWT_CMD failing must abort before any gh call is made.

    Proves there is no fallback to ambient gh auth for the preflight
    App-ID check: if the script tolerated the JWT failure and proceeded
    anyway, the fake gh shim would still record a GET /app call (using
    no/garbage credential) — this test asserts the call log is empty.
    """
    canned = tmp_path / "canned"
    canned.mkdir()
    (canned / "list.body").write_text("[]", encoding="utf-8")

    rc, stdout, stderr, log = _invoke(
        tmp_path,
        canned,
        jwt_cmd="printf 'simulated JWT fetch failure' >&2; exit 1",
    )

    assert rc != 0, (
        f"a failing BH_APP_AUTH_JWT_CMD must abort the script (non-zero "
        f"exit), not proceed; rc={rc}\nstdout:\n{stdout}\nstderr:\n{stderr}"
    )
    calls = _calls(log)
    assert calls == [], (
        "a failing JWT credential command must abort BEFORE any gh call "
        f"is made (no fallback to ambient auth); observed calls={calls!r}"
    )


# ---------------------------------------------------------------------------
# Case 3: installation-token command failure — fail fast before repo calls
# ---------------------------------------------------------------------------


def test_token_cmd_failure_aborts_before_any_repo_scoped_gh_call(
    tmp_path: Path,
) -> None:
    """BH_APP_AUTH_TOKEN_CMD failing must abort before any repos/... call.

    The (JWT-only) GET /app preflight call may or may not have already
    run depending on the implementation's internal ordering of the two
    credential acquisitions — this test does not assume either order.
    What must hold regardless: zero writes, and zero calls to any
    repos/<slug>/... endpoint (collaborators, rulesets), proving no
    fallback to ambient gh auth for the installation-token-gated calls.
    """
    canned = tmp_path / "canned"
    canned.mkdir()
    (canned / "list.body").write_text("[]", encoding="utf-8")

    rc, stdout, stderr, log = _invoke(
        tmp_path,
        canned,
        token_cmd="printf 'simulated token fetch failure' >&2; exit 1",
    )

    assert rc != 0, (
        f"a failing BH_APP_AUTH_TOKEN_CMD must abort the script (non-zero "
        f"exit), not proceed; rc={rc}\nstdout:\n{stdout}\nstderr:\n{stderr}"
    )
    calls = _calls(log)
    assert _writes(calls) == [], (
        "a failing installation-token credential command must write zero "
        f"ruleset mutations; observed writes={_writes(calls)!r}"
    )
    assert _repo_scoped_calls(calls) == [], (
        "a failing installation-token credential command must abort "
        "BEFORE any repos/<slug>/... call is made (no fallback to "
        f"ambient auth); observed calls={calls!r}"
    )


# ---------------------------------------------------------------------------
# Case 4: secret hygiene — neither credential leaks into script output
# ---------------------------------------------------------------------------


def test_jwt_and_token_values_never_appear_in_script_output(
    tmp_path: Path,
) -> None:
    """Neither the JWT nor the installation token appear in stdout/stderr.

    Drives a successful run (both credentials resolve, writes happen)
    and inspects only the script's own stdout/stderr — the fake-gh call
    log is test instrumentation on a side channel, not something a real
    log consumer of the script would ever see, so it is deliberately
    excluded from this assertion.

    Also asserts the credential mechanism was actually exercised (the
    fake-gh log shows the JWT/token were used as bearer credentials) —
    without this, a script that never wires up BH_APP_AUTH_*_CMD at all
    would trivially "pass" the non-leakage checks below for the wrong
    reason (nothing to leak because the feature doesn't run), rather
    than because the script correctly withholds a credential it did
    use.
    """
    canned = tmp_path / "canned"
    canned.mkdir()
    (canned / "list.body").write_text("[]", encoding="utf-8")

    rc, stdout, stderr, log = _invoke(tmp_path, canned)

    assert rc == 0, f"expected a successful run; rc={rc}\nstderr:\n{stderr}"

    calls = _calls(log)
    all_credentials = " ".join(_credential_blob(c) for c in calls)
    assert _fingerprint(_FAKE_JWT) in all_credentials, (
        "expected the JWT override to actually be used as a bearer "
        f"credential somewhere in the run; calls={calls!r}"
    )
    assert _fingerprint(_FAKE_TOKEN) in all_credentials, (
        "expected the installation-token override to actually be used "
        f"as a bearer credential somewhere in the run; calls={calls!r}"
    )

    combined = stdout + stderr
    assert _FAKE_JWT not in combined, (
        "the App JWT value must never be echoed/logged by the script; "
        f"found it in output:\n{combined}"
    )
    assert _FAKE_TOKEN not in combined, (
        "the installation token value must never be echoed/logged by the "
        f"script; found it in output:\n{combined}"
    )
    # Standing regression guard against accidentally echoing a
    # credential in a shape real GitHub tokens/JWTs take.
    for leaked_marker in (
        "Bearer fake-jwt",
        "Bearer fake-install",
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


# ---------------------------------------------------------------------------
# Case 5: unset overrides fall back to the real app_auth module
# ---------------------------------------------------------------------------


def test_unset_overrides_fall_back_to_real_app_auth_module(
    tmp_path: Path,
) -> None:
    """Absent BH_APP_AUTH_*_CMD -> script attempts the real app_auth module.

    BWS_PEM_SECRET_ID / BWS_ACCESS_TOKEN are deliberately excluded from
    the subprocess environment (never inherited from whatever the host
    ambiently has set) so this deterministically exercises the real
    module's fast, network-free "missing env" failure path (documented
    exit code 2) instead of risking a slow or flaky live BWS/GitHub
    network attempt if those variables happened to be ambiently present.

    Asserts the script fails closed: non-zero exit, zero gh calls
    (the real module must fail before the script ever reaches a gh
    invocation), zero writes.
    """
    canned = tmp_path / "canned"
    canned.mkdir()
    (canned / "list.body").write_text("[]", encoding="utf-8")

    rc, stdout, stderr, log = _invoke(
        tmp_path,
        canned,
        jwt_cmd=None,
        token_cmd=None,
        exclude_bws_env=True,
        timeout=30.0,
    )

    assert rc != 0, (
        f"with BH_APP_AUTH_JWT_CMD/BH_APP_AUTH_TOKEN_CMD unset and no "
        f"BWS_* credentials available, the script must fail closed "
        f"(via the real app_auth module's missing-env path), not "
        f"succeed; rc={rc}\nstdout:\n{stdout}\nstderr:\n{stderr}"
    )
    calls = _calls(log)
    assert calls == [], (
        "the real app_auth module must fail on missing BWS_* env before "
        f"the script ever reaches a gh call; observed calls={calls!r}"
    )
