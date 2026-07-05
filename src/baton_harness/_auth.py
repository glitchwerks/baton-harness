"""GitHub PAT defense-in-depth validation gate.

This module provides a lightweight pre-flight check that rejects obviously
wrong token types before any real GitHub API work begins.  It is called at
the top of ``before_run.main()`` so the harness fails fast rather than
burning an agent turn with a bad credential.

The capability self-test (Gate 3) distinguishes **transient** network/API
errors from **permanent** credential failures:

- **Transient**: rate-limit (429), gateway errors (502-504), DNS/TLS
  failures, timeouts.  The self-test is retried with bounded backoff
  (``_MAX_RETRIES`` attempts, ``_RETRY_SLEEP_SECONDS`` between each).
  If still failing, ``TokenValidationError`` is raised with a message
  indicating the transient/network nature so the operator knows to retry
  after GitHub recovers.
- **Permanent**: authentication failures (401, 403, "Unauthorized", "Bad
  credentials").  Raised immediately with no retries; the operator must
  fix the token.

.. important::

    **This check is a defense-in-depth layer, NOT a safety guarantee.**

    The real safeguard is operating the harness under a least-privilege
    GitHub bot/machine account whose *repository-level permissions are
    structurally limited* — e.g. ``contents: read``, ``issues: write``,
    no org-admin or team-write scopes.  A structurally least-privilege
    account cannot cause destructive repository changes even if the token
    is valid and passes this gate.

    **Known limitation — no scope introspection for fine-grained PATs:**

    GitHub's API exposes the granted scopes for *classic* PATs via the
    ``X-OAuth-Scopes`` response header.  Fine-grained PATs do NOT expose
    their permission grants through any public API endpoint as of mid-2026.
    Consequently, this module cannot verify *what* a fine-grained PAT can
    do — only that it (a) has the right token type prefix and (b) can
    successfully authenticate to the GitHub API.  Scope-level enforcement
    relies entirely on the bot account's repository-level permission
    configuration.

    **Known limitation — persistent transient GitHub API failures:**

    If the GitHub API is experiencing a sustained outage or rate-limit,
    the capability self-test will exhaust all retries and fail-closed
    (i.e. block the run).  The ``TokenValidationError`` message will
    indicate a transient/network condition.  Recovery: wait for GitHub
    to recover, then re-run the harness.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable


class TokenValidationError(Exception):
    """Raised when the GitHub token fails the pre-flight validation gate.

    The message is human-readable and actionable — it names the specific
    problem and tells the operator how to fix it.

    Attributes:
        message: The human-readable validation failure description.
    """

    def __init__(self, message: str) -> None:
        """Initialise with a human-readable failure description.

        Args:
            message: Describes what is wrong with the token and how to
                fix it (e.g. which prefix was rejected, where to mint a
                replacement).
        """
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

#: Token prefix for fine-grained PATs — the only type accepted.
_FINE_GRAINED_PREFIX = "github_pat_"

#: Token prefix for classic PATs — explicitly rejected with a targeted message.
_CLASSIC_PREFIX = "ghp_"

#: Maximum number of capability self-test retries on transient errors.
#: Inject / override in tests via monkeypatching ``_auth._MAX_RETRIES``.
_MAX_RETRIES: int = 2

#: Seconds to sleep between capability self-test retry attempts.
#: Override with a zero-sleep callable in tests to avoid real delays:
#: ``monkeypatch.setattr(auth_mod, "_RETRY_SLEEP_SECONDS", 0)``
_RETRY_SLEEP_SECONDS: float = 2.0

#: Substrings in ``gh`` stderr that indicate a *transient* (network/API)
#: failure.  Checked case-insensitively.  Everything else is permanent.
_TRANSIENT_MARKERS: tuple[str, ...] = (
    "429",
    "too many requests",
    "502",
    "503",
    "504",
    "timeout",
    "timed out",
    "connection refused",
    "connection reset",
    "could not resolve host",
    "temporarily unavailable",
    "tls handshake",
    "eof",
)


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run an external command and return its completed process.

    Centralises subprocess invocation so that tests can patch a single
    symbol.  Uses ``encoding="utf-8"`` explicitly to avoid Windows cp1252
    mangling of non-ASCII output (Python skill footgun note).

    Args:
        cmd: Command and arguments to execute (no shell interpolation).

    Returns:
        A ``subprocess.CompletedProcess`` with captured stdout/stderr and
        ``returncode`` set.  The process may exit with any code; callers
        inspect ``returncode`` themselves.
    """
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _read_token() -> str:
    """Read the GitHub token from the environment.

    Checks ``GH_TOKEN`` first, then falls back to ``GITHUB_TOKEN``.  Both
    names are recognised by the ``gh`` CLI and used interchangeably in most
    CI environments.

    Returns:
        The token string (may be empty — callers check for emptiness).
    """
    return os.environ.get("GH_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")


def _is_transient(stderr: str) -> bool:
    """Return True if the ``gh`` stderr looks like a transient error.

    Checks case-insensitively against a fixed set of substrings that
    indicate rate-limits, gateway errors, and network-level failures
    (as opposed to authentication/authorisation rejections).

    Args:
        stderr: The captured standard-error text from the failed ``gh``
            invocation.

    Returns:
        ``True`` when ``stderr`` contains at least one transient-error
        marker; ``False`` for permanent errors (401, 403, bad credentials,
        etc.) and for unrecognised stderr content.
    """
    lower = stderr.lower()
    return any(marker in lower for marker in _TRANSIENT_MARKERS)


def _run_self_test(
    sleep_fn: Callable[[float], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the ``gh api user`` capability self-test with transient retries.

    On transient failures (rate-limits, network errors, gateway errors)
    retries up to ``_MAX_RETRIES`` times with ``_RETRY_SLEEP_SECONDS``
    between each attempt.  Permanent failures (401/403, bad credentials)
    are returned immediately without retrying.

    The ``sleep_fn`` parameter exists solely for test injection; callers
    must not supply it in production.

    Args:
        sleep_fn: A callable that replaces ``time.sleep`` during retries.
            Defaults to ``time.sleep``.  Pass a no-op lambda in tests to
            avoid real delays (see ``_RETRY_SLEEP_SECONDS`` docstring).

    Returns:
        The ``CompletedProcess`` from the *last* ``gh api user`` call,
        whether successful or not.  Callers inspect ``returncode`` and
        the ``_is_transient`` classification.
    """
    if sleep_fn is None:
        sleep_fn = time.sleep

    result = _run(["gh", "api", "user", "--jq", ".login"])
    if result.returncode == 0:
        return result

    attempt = 0
    while attempt < _MAX_RETRIES and _is_transient(result.stderr):
        sleep_fn(_RETRY_SLEEP_SECONDS)
        result = _run(["gh", "api", "user", "--jq", ".login"])
        if result.returncode == 0:
            return result
        attempt += 1

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_github_token(
    sleep_fn: Callable[[float], None] | None = None,
) -> None:
    """Validate the GitHub token before any harness work begins.

    Performs three checks in order:

    1. **Token-type gate**: reads ``GH_TOKEN`` (falling back to
       ``GITHUB_TOKEN``) and rejects any token whose prefix is not
       ``github_pat_``.  Classic PATs (``ghp_``) get a specific message;
       all other non-fine-grained types get a generic rejection.  Missing
       or empty tokens are rejected immediately.

    2. **Capability self-test**: calls ``gh api user --jq .login`` to
       confirm the token is authenticated and can reach the GitHub API.
       Transient errors (rate-limits, network timeouts, gateway errors)
       are retried up to ``_MAX_RETRIES`` times before raising.  Permanent
       errors (401, 403, bad credentials) raise immediately.

    3. **Login parse**: validates the parsed login string is non-empty.

    .. note::

        This is a **defense-in-depth layer, not a safety guarantee**.  The
        real enforcement of least-privilege is the bot account's repository
        permission configuration.  Fine-grained PATs expose no scope-
        introspection API, so this gate checks token type + reachability
        only — not what the token is permitted to do.

    Args:
        sleep_fn: Injected sleep callable for tests (default
            ``time.sleep``).  Pass a no-op lambda to avoid real delay in
            unit tests.

    Raises:
        TokenValidationError: When the token is missing, has the wrong
            type, or fails the capability self-test.  Transient failures
            include a message indicating the network/API condition and
            that retrying after GitHub recovers is the remedy.  Permanent
            failures name the credential problem and direct the operator
            to mint a new fine-grained, repo-scoped PAT.

    Example::

        try:
            validate_github_token()
        except TokenValidationError as exc:
            sys.exit(f"auth gate: {exc.message}")
    """
    token = _read_token()

    # ------------------------------------------------------------------ #
    # Gate 1: token presence                                               #
    # ------------------------------------------------------------------ #
    if not token:
        raise TokenValidationError(
            "no token found in GH_TOKEN or GITHUB_TOKEN. "
            "Mint a fine-grained, repo-scoped PAT at "
            "https://github.com/settings/personal-access-tokens/new "
            "and export it as GH_TOKEN."
        )

    # ------------------------------------------------------------------ #
    # Gate 2: token type                                                   #
    # ------------------------------------------------------------------ #
    if token.startswith(_CLASSIC_PREFIX):
        raise TokenValidationError(
            f"classic PAT detected (prefix '{_CLASSIC_PREFIX}'). "
            "Classic PATs grant broad account-wide scopes and are not "
            "accepted by this harness. "
            "Mint a fine-grained, repo-scoped PAT at "
            "https://github.com/settings/personal-access-tokens/new "
            "and export it as GH_TOKEN."
        )

    if not token.startswith(_FINE_GRAINED_PREFIX):
        raise TokenValidationError(
            f"unrecognised token type (does not start with "
            f"'{_FINE_GRAINED_PREFIX}'). "
            "Only fine-grained PATs are accepted by this harness. "
            "Mint a fine-grained, repo-scoped PAT at "
            "https://github.com/settings/personal-access-tokens/new "
            "and export it as GH_TOKEN."
        )

    # ------------------------------------------------------------------ #
    # Gate 3: capability self-test                                         #
    # ------------------------------------------------------------------ #
    result = _run_self_test(sleep_fn=sleep_fn)

    if result.returncode != 0:
        if _is_transient(result.stderr):
            raise TokenValidationError(
                f"capability self-test failed after {_MAX_RETRIES + 1} "
                f"attempt(s): transient network or GitHub API condition "
                f"(see gh output above). "
                "Recovery: wait for GitHub to recover and re-run the "
                "harness. If the problem persists, check "
                "https://www.githubstatus.com/ for an active incident."
            )
        raise TokenValidationError(
            f"capability self-test failed: gh api user exited "
            f"{result.returncode} — token may be expired, revoked, or "
            f"lack the required repository permissions "
            f"(see gh output above). "
            "Mint a fine-grained, repo-scoped PAT at "
            "https://github.com/settings/personal-access-tokens/new "
            "and export it as GH_TOKEN."
        )

    try:
        # gh --jq returns plain text; fall back to json.loads for
        # robustness when --jq is not available or the CLI version
        # returns a JSON object instead.
        login = result.stdout.strip()
        if not login:
            raise ValueError("empty login response")
        # Attempt JSON parse only when the output looks like JSON
        if login.startswith("{"):
            data = json.loads(login)
            login = str(data.get("login", ""))
            if not login:
                raise ValueError("login field missing or empty")
    except (json.JSONDecodeError, ValueError) as exc:
        raise TokenValidationError(
            f"capability self-test failed: could not parse gh api user "
            f"response — {exc} "
            f"(see gh output above)."
        ) from exc


# ---------------------------------------------------------------------------
# Daemon-side token validator
# ---------------------------------------------------------------------------

#: Token prefix for GitHub App installation tokens — the only type
#: accepted by the daemon (harness) auth gate.
_INSTALLATION_PREFIX = "ghs_"


def validate_daemon_token(token: str) -> None:
    """Validate a GitHub App installation token for daemon-side use.

    This is a **type-gate only** — no live ``gh`` call is made.  The
    daemon uses a ``ghs_`` installation token minted by the GitHub App
    auth flow, not a fine-grained PAT.  All worker-token forms and
    unknown prefixes are rejected so the daemon cannot accidentally use
    a worker credential (and vice-versa).

    Args:
        token: The token string to validate.

    Raises:
        TokenValidationError: When the token is empty, has an unexpected
            prefix (e.g. ``github_pat_``, ``ghp_``, ``gho_``), or is
            otherwise not a ``ghs_`` installation token.

    Example::

        try:
            validate_daemon_token(installation_token)
        except TokenValidationError as exc:
            sys.exit(f"daemon auth gate: {exc.message}")
    """
    if not token:
        raise TokenValidationError(
            "validate_daemon_token: token is empty. "
            "A GitHub App installation token (ghs_ prefix) is required."
        )

    if not token.startswith(_INSTALLATION_PREFIX):
        raise TokenValidationError(
            f"validate_daemon_token: token has unexpected prefix — "
            f"expected '{_INSTALLATION_PREFIX}' (GitHub App installation "
            f"token), got a token starting with "
            f"'{token[:12]}...'. "
            "Only ghs_ installation tokens are accepted by the daemon."
        )


def validate_gh_token(token: str, *, secret_id_configured: bool) -> None:
    """Validate that a vault-configured ``GH_TOKEN`` resolved non-empty.

    Boot-time guard for issue #212: when a Bitwarden Secrets ID was
    configured for ``GH_TOKEN`` (``BWS_GH_TOKEN_SECRET_ID`` set), the
    vault fetch is expected to have populated ``os.environ["GH_TOKEN"]``.
    If it ended up empty or whitespace-only anyway, that misconfiguration
    should fail fast at startup rather than surface opaquely later inside
    a worker subprocess.

    This is a **no-op** when no secret ID was configured — an externally
    supplied, non-vault-backed ``GH_TOKEN`` (empty or otherwise) is out of
    scope for this specific guard and preserves pre-#212 behaviour.

    Args:
        token: The resolved ``GH_TOKEN`` value (may be empty).
        secret_id_configured: Whether a Bitwarden Secrets ID was
            configured for ``GH_TOKEN`` (i.e. a vault fetch was expected
            to populate it).

    Raises:
        TokenValidationError: When ``secret_id_configured`` is ``True``
            and ``token`` is empty or whitespace-only.

    Example::

        try:
            validate_gh_token(
                os.environ.get("GH_TOKEN", ""),
                secret_id_configured=bool(
                    os.environ.get("BWS_GH_TOKEN_SECRET_ID")
                ),
            )
        except TokenValidationError as exc:
            sys.exit(f"gh token gate: {exc.message}")
    """
    if not secret_id_configured:
        return

    if not token.strip():
        raise TokenValidationError(
            "validate_gh_token: GH_TOKEN is empty after vault bootstrap. "
            "BWS_GH_TOKEN_SECRET_ID was configured but the fetched "
            "secret resolved to an empty or whitespace-only value — "
            "check the Bitwarden Secrets entry."
        )
