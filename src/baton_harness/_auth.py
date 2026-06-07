"""GitHub PAT defense-in-depth validation gate.

This module provides a lightweight pre-flight check that rejects obviously
wrong token types before any real GitHub API work begins.  It is called at
the top of ``before_run.main()`` so the harness fails fast rather than
burning an agent turn with a bad credential.

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
"""

from __future__ import annotations

import json
import os
import subprocess


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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_github_token() -> None:
    """Validate the GitHub token before any harness work begins.

    Performs two checks in order:

    1. **Token-type gate**: reads ``GH_TOKEN`` (falling back to
       ``GITHUB_TOKEN``) and rejects any token whose prefix is not
       ``github_pat_``.  Classic PATs (``ghp_``) get a specific message;
       all other non-fine-grained types get a generic rejection.  Missing
       or empty tokens are rejected immediately.

    2. **Capability self-test**: calls ``gh api user --jq .login`` to
       confirm the token is authenticated and can reach the GitHub API.
       A non-zero exit code or unparseable JSON response is treated as an
       under-scoped or invalid token.

    .. note::

        This is a **defense-in-depth layer, not a safety guarantee**.  The
        real enforcement of least-privilege is the bot account's repository
        permission configuration.  Fine-grained PATs expose no scope-
        introspection API, so this gate checks token type + reachability
        only — not what the token is permitted to do.

    Raises:
        TokenValidationError: When the token is missing, has the wrong
            type, or fails the capability self-test.  The exception
            message is human-readable and actionable, naming the specific
            problem and directing the operator to mint a fine-grained,
            repo-scoped PAT.

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
    result = _run(["gh", "api", "user", "--jq", ".login"])

    if result.returncode != 0:
        raise TokenValidationError(
            f"capability self-test failed: gh api user exited "
            f"{result.returncode} — token may be expired, revoked, or "
            f"lack the required repository permissions. "
            f"gh stderr: {result.stderr.strip()!r}"
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
            f"response — {exc}. "
            f"Raw output: {result.stdout.strip()!r}"
        ) from exc
