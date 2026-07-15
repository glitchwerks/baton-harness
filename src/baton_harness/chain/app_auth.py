"""GitHub App authentication: JWT minting, installation token caching.

Provides three public building blocks for GitHub App auth:

1. ``build_app_jwt`` — produce an RS256 JWT suitable for the GitHub App
   authentication endpoint (with a 60-second clock-skew backdate on
   ``iat``).
2. ``mint_installation_token`` — exchange the App JWT for a short-lived
   installation access token via the GitHub REST API.  The HTTP call is
   injected so callers control the transport layer.
3. ``InstallationTokenProvider`` — thin caching wrapper that re-mints
   only when within a configurable refresh margin of the token's expiry.

Security invariant (env-discipline seam)
-----------------------------------------
``bootstrap_secrets`` is the harness startup entry point.  It accepts
injected ``fetch_secret`` and ``mint_token`` callables so tests can drive
the full flow without real Bitwarden or GitHub calls.  After reading
``BWS_ACCESS_TOKEN`` from ``os.environ`` it **pops it** immediately, and
the installation token value is **never written into** ``os.environ``.

These two rules ensure that worker subprocesses that inherit
``os.environ`` cannot read either the Bitwarden machine-account bootstrap
secret or the GitHub installation token — preventing two distinct
privilege-escalation paths.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

import jwt

import baton_harness.chain.bws_client as bws_client


class AppAuthError(RuntimeError):
    """Raised when GitHub App authentication fails.

    Attributes:
        message: Human-readable description of what went wrong.
    """

    def __init__(self, message: str) -> None:
        """Initialise with a human-readable failure description.

        Args:
            message: Describes the authentication failure (e.g. which
                API call failed, what the HTTP status was, or which
                required field was absent from the response).
        """
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default JWT lifetime in seconds (9 minutes — GitHub's documented max is
#: 10 min; leaving 1 minute headroom avoids edge-case rejections).
_DEFAULT_TTL_SECONDS: int = 540

#: Clock-skew backdate on ``iat`` in seconds (GitHub recommends 60 s).
_IAT_BACKDATE_SECONDS: int = 60

#: Seconds before token expiry at which the provider proactively re-mints.
#: 5 minutes (300 s) gives enough runway for a slow API call or retry.
_REFRESH_MARGIN_SECONDS: int = 300

#: GitHub API base URL.
_GITHUB_API_BASE: str = "https://api.github.com"

#: HTTP status codes that should be retried by the mint transport.
_RETRYABLE_HTTP_STATUS_CODES: frozenset[int] = frozenset({500, 502, 503, 504})

#: Max attempts for the GitHub installation-token POST.
_HTTP_POST_MAX_ATTEMPTS: int = 3

#: Socket timeout for the GitHub installation-token POST.
_HTTP_POST_TIMEOUT_S: float = 10.0

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

#: Callable type for the injected HTTP POST: (url, headers) -> response dict.
HttpPostFn = Callable[[str, dict[str, str]], dict[str, Any]]

#: Callable type for ``mint_token`` injected into ``bootstrap_secrets``.
MintTokenFn = Callable[
    [str, str, int],
    tuple[str, str],
]


@runtime_checkable
class InstallationTokenSourceProtocol(Protocol):
    """Protocol for objects that can resolve a current installation token."""

    def get_token(self) -> str:
        """Return the current installation token string."""


InstallationTokenSource = str | InstallationTokenSourceProtocol


# ---------------------------------------------------------------------------
# Public: JWT
# ---------------------------------------------------------------------------


def build_app_jwt(
    app_id: str,
    private_key_pem: str,
    *,
    now: int,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> str:
    """Build an RS256-signed GitHub App JWT.

    GitHub requires the ``iat`` claim to be backdated by 60 seconds to
    compensate for clock-skew between the caller and GitHub's servers.

    Args:
        app_id: The numeric GitHub App ID, as a string (used as ``iss``).
        private_key_pem: The App's RSA private key in PEM format.
        now: Current Unix timestamp (seconds since epoch).  Pass an
            explicit value so callers control the clock in tests.
        ttl_seconds: JWT lifetime in seconds.  Defaults to 540 (9 min).

    Returns:
        A signed RS256 JWT string ready for use in an
        ``Authorization: Bearer <jwt>`` header.
    """
    iat = now - _IAT_BACKDATE_SECONDS
    exp = now + ttl_seconds

    payload = {
        "iss": app_id,
        "iat": iat,
        "exp": exp,
    }
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


# ---------------------------------------------------------------------------
# Public: installation token mint
# ---------------------------------------------------------------------------


def mint_installation_token(
    app_id: str,
    private_key_pem: str,
    installation_id: int,
    *,
    http_post: HttpPostFn,
    now: int,
) -> tuple[str, str]:
    """Mint a short-lived GitHub App installation access token.

    Builds an App JWT and POSTs to the GitHub App installation
    ``access_tokens`` endpoint.  The HTTP layer is injected via
    ``http_post`` so callers control the transport in tests.

    Fail-closed semantics: any exception raised by ``http_post``, any
    non-200-equivalent response (signalled by the ``http_post``
    implementation raising), or any missing required field in the
    response body raises ``AppAuthError``.

    Args:
        app_id: GitHub App ID (used as JWT ``iss``).
        private_key_pem: RSA private key PEM for JWT signing.
        installation_id: The numeric ID of the GitHub App installation.
        http_post: Injected callable ``(url, headers) -> dict``.  Must
            raise on non-200 responses so this function stays
            fail-closed.
        now: Current Unix timestamp (passed to ``build_app_jwt``).

    Returns:
        A ``(token, expires_at)`` tuple where ``token`` is the
        installation access token string and ``expires_at`` is the ISO
        8601 expiry timestamp returned by GitHub.

    Raises:
        AppAuthError: On any transport failure, non-200 HTTP status
            (propagated as an exception from ``http_post``), or missing
            ``token`` / ``expires_at`` field in the response.
    """
    app_jwt = build_app_jwt(
        app_id=app_id,
        private_key_pem=private_key_pem,
        now=now,
    )

    url = (
        f"{_GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens"
    )
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        response = http_post(url, headers)
    except Exception as exc:
        raise AppAuthError(
            f"mint_installation_token: HTTP POST to {url} failed: {exc}"
        ) from exc

    try:
        token: str = response["token"]
        expires_at: str = response["expires_at"]
    except (KeyError, TypeError) as exc:
        keys_repr = (
            list(response.keys())
            if isinstance(response, dict)
            else repr(response)
        )
        raise AppAuthError(
            f"mint_installation_token: response missing required field: "
            f"{exc}. Got keys: {keys_repr}"
        ) from exc

    return token, expires_at


# ---------------------------------------------------------------------------
# Public: caching provider
# ---------------------------------------------------------------------------


class InstallationTokenProvider:
    """Caches a GitHub App installation token and re-mints near expiry.

    The provider lazily mints on the first ``get_token()`` call and
    re-uses the cached value until within ``_REFRESH_MARGIN_SECONDS`` of
    expiry, at which point it proactively re-mints.

    Attributes:
        app_id: GitHub App ID passed through to ``mint_installation_token``.
        private_key_pem: RSA private key PEM for JWT signing.
        installation_id: GitHub App installation numeric ID.
    """

    def __init__(
        self,
        app_id: str,
        private_key_pem: str,
        installation_id: int,
        http_post: HttpPostFn,
    ) -> None:
        """Initialise the provider without minting a token yet.

        Args:
            app_id: GitHub App ID (used as JWT ``iss``).
            private_key_pem: RSA private key PEM for JWT signing.
            installation_id: The numeric GitHub App installation ID.
            http_post: Injected HTTP POST callable forwarded to
                ``mint_installation_token``.  Tests pass a stub here.
        """
        self.app_id = app_id
        self.private_key_pem = private_key_pem
        self.installation_id = installation_id
        self._http_post = http_post

        self._token: str | None = None
        # Epoch seconds at which the cached token expires.
        self._expires_at_ts: float = 0.0

    def get_token(self, now: int | None = None) -> str:
        """Return a valid installation access token, minting if necessary.

        Args:
            now: Current Unix timestamp override (used in tests to
                control the clock).  Defaults to ``int(time.time())``.

        Returns:
            A valid GitHub App installation access token string.

        Raises:
            AppAuthError: If minting fails (propagated from
                ``mint_installation_token``).
        """
        if now is None:
            now = int(time.time())

        # Re-mint when no token cached or within the refresh margin.
        needs_refresh = (
            self._token is None
            or now >= self._expires_at_ts - _REFRESH_MARGIN_SECONDS
        )

        if needs_refresh:
            token, expires_at_iso = mint_installation_token(
                app_id=self.app_id,
                private_key_pem=self.private_key_pem,
                installation_id=self.installation_id,
                http_post=self._http_post,
                now=now,
            )
            self._token = token
            self._expires_at_ts = _parse_iso_to_epoch(expires_at_iso)

        # _token is guaranteed non-None here after needs_refresh branch.
        assert self._token is not None  # noqa: S101 — type narrowing
        return self._token


def _parse_iso_to_epoch(iso_str: str) -> float:
    """Parse a GitHub ISO 8601 UTC timestamp to a Unix epoch float.

    GitHub returns timestamps in the form ``YYYY-MM-DDTHH:MM:SSZ``.

    Args:
        iso_str: ISO 8601 timestamp string ending in ``Z`` (UTC).

    Returns:
        Unix epoch as a float (seconds since 1970-01-01T00:00:00Z).

    Raises:
        AppAuthError: If ``iso_str`` does not match the expected
            ``YYYY-MM-DDTHH:MM:SSZ`` format.
    """
    import datetime

    # Python 3.10 does not support ``%z`` parsing ``Z`` directly;
    # strip the trailing ``Z`` and treat as UTC explicitly.
    try:
        dt = datetime.datetime.strptime(
            iso_str.rstrip("Z"), "%Y-%m-%dT%H:%M:%S"
        ).replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except ValueError as exc:
        raise AppAuthError(
            f"mint_installation_token: invalid expires_at timestamp"
            f" format: {iso_str}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Public: env-discipline seam (bootstrap entry point)
# ---------------------------------------------------------------------------


def bootstrap_secrets(
    app_id: str,
    app_private_key_bws_id: str,
    installation_id: int,
    *,
    fetch_secret: Callable[..., str],
    mint_token: Callable[..., tuple[str, str]],
) -> tuple[str, str]:
    """Fetch the App private key from Bitwarden Secrets and mint a token.

    This is the harness startup entry point for the GitHub App auth
    flow.  It enforces the env-discipline invariants:

    1. ``BWS_ACCESS_TOKEN`` is **popped** from ``os.environ`` immediately
       after it is read, so worker subprocesses cannot access it.
    2. The installation token is **never** written into ``os.environ``,
       so workers cannot read the privileged merge credential.

    The ``fetch_secret`` and ``mint_token`` callables are injected so
    tests can drive the full flow without a real Bitwarden vault or
    GitHub API.

    Note: ``BWS_ACCESS_TOKEN`` is removed from ``os.environ`` immediately,
    before any other operation.  If this function raises an exception,
    the token will have already been scrubbed.

    Args:
        app_id: GitHub App numeric ID (forwarded to ``mint_token``).
        app_private_key_bws_id: Bitwarden Secrets secret ID containing
            the GitHub App RSA private key PEM.
        installation_id: GitHub App installation ID (forwarded to
            ``mint_token``).
        fetch_secret: Callable ``(secret_id, *, access_token, run) -> str``
            that retrieves a secret value from Bitwarden Secrets.
            Signature matches ``bws_client.fetch_secret``.
        mint_token: Callable
            ``(app_id, private_key_pem, installation_id, *, http_post,
            now) -> (token, expires_at)`` that mints an installation
            token.  Signature matches ``mint_installation_token``.

    Returns:
        A ``(token, expires_at)`` tuple.  The token is the short-lived
        GitHub App installation access token; ``expires_at`` is the ISO
        8601 expiry timestamp.  Neither value is written to
        ``os.environ``.

    Raises:
        AppAuthError: If the Bitwarden secret fetch or the GitHub token
            mint fails.
    """
    # Read and immediately pop BWS_ACCESS_TOKEN so workers cannot inherit it.
    bws_token = os.environ.pop("BWS_ACCESS_TOKEN", None) or ""

    private_key_pem = fetch_secret(
        app_private_key_bws_id,
        access_token=bws_token,
    )

    now = int(time.time())
    token, expires_at = mint_token(
        app_id,
        private_key_pem,
        installation_id,
        http_post=_github_http_post,
        now=now,
    )

    # Invariant: never write the installation token into os.environ.
    # The caller receives it as a return value and stores it outside env.
    return token, expires_at


def build_installation_token_provider(
    app_id: str,
    app_private_key_bws_id: str,
    installation_id: int,
    *,
    fetch_secret: Callable[..., str],
) -> InstallationTokenProvider:
    """Fetch the App private key and return a refreshable token provider.

    Mirrors the env-discipline behavior of ``bootstrap_secrets`` while
    returning an ``InstallationTokenProvider`` that can mint fresh tokens
    on demand for long-running daemon work.

    Args:
        app_id: GitHub App numeric ID.
        app_private_key_bws_id: Bitwarden secret ID for the RSA PEM key.
        installation_id: GitHub App installation ID.
        fetch_secret: Callable used to retrieve the private key PEM.

    Returns:
        A configured ``InstallationTokenProvider`` that mints via the real
        GitHub HTTP transport and keeps the private key outside ``os.environ``.
    """
    bws_token = os.environ.pop("BWS_ACCESS_TOKEN", None) or ""
    private_key_pem = fetch_secret(
        app_private_key_bws_id,
        access_token=bws_token,
    )
    return InstallationTokenProvider(
        app_id=app_id,
        private_key_pem=private_key_pem,
        installation_id=installation_id,
        http_post=_github_http_post,
    )


def resolve_installation_token(
    installation_token: InstallationTokenSource,
) -> str:
    """Resolve a token source to the current installation-token string.

    Args:
        installation_token: Either a literal token string or a refreshable
            provider object exposing ``get_token()``.

    Returns:
        The current GitHub App installation token string.
    """
    if isinstance(installation_token, str):
        return installation_token
    return installation_token.get_token()


def gh_env(installation_token: InstallationTokenSource) -> dict[str, str]:
    """Return ``os.environ`` overlaid with the resolved installation token.

    Builds a full shallow copy of the current process environment and
    injects the resolved GitHub App installation token into both canonical
    ``gh``-CLI credential keys (``GH_TOKEN`` and ``GITHUB_TOKEN``).

    Env-discipline invariants:
        * ``os.environ`` is **never mutated** — the overlay is a fresh
          ``dict`` per call.
        * The token is resolved at call time and does not persist in the
          returned dict beyond the immediate subprocess invocation; callers
          should discard the dict after use.
        * Both ``GH_TOKEN`` and ``GITHUB_TOKEN`` are always set so that
          both the ``gh`` CLI and git-credential helpers pick up the token
          regardless of which key they consult.

    This is the shared implementation for all chain modules.  It replaces
    the five previously identical per-module ``_gh_env`` helpers in
    ``daemon``, ``escalation``, ``gh_deps``, ``merge``, and ``recovery``
    (PR #163 reviewer W1).

    Args:
        installation_token: A literal token string (``ghs_…``) or a
            refreshable ``InstallationTokenSourceProtocol`` object whose
            ``get_token()`` method returns the current token.  Resolved
            via :func:`resolve_installation_token`.

    Returns:
        A new ``dict[str, str]`` containing all key-value pairs from
        ``os.environ`` at the time of the call, with ``GH_TOKEN`` and
        ``GITHUB_TOKEN`` overridden to the resolved installation token.
    """
    from baton_harness.chain.identity import Identity, env_for

    return env_for(Identity.APP, installation_token=installation_token)


def _github_http_post(
    url: str,
    headers: dict[str, str],
) -> dict[str, Any]:
    """POST to the GitHub API with retries on transient 5xx responses.

    Args:
        url: Fully qualified GitHub API URL to POST to.
        headers: Request headers to include, such as Authorization.

    Returns:
        Parsed JSON response body as a dict.

    Raises:
        AppAuthError: If GitHub returns a non-retryable 4xx, if all retry
            attempts for a retryable 5xx or transient OSError (e.g.
            ``socket.timeout``, connection reset) are exhausted, or if
            the response body is not valid JSON.
    """
    request_headers = {
        **headers,
        "Content-Type": "application/json",
    }
    payload = json.dumps({}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers=request_headers,
        method="POST",
    )

    for attempt in range(1, _HTTP_POST_MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(
                request,
                timeout=_HTTP_POST_TIMEOUT_S,
            ) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            status = exc.code
            response_body = exc.read().decode("utf-8", errors="replace")
            if (
                status in _RETRYABLE_HTTP_STATUS_CODES
                and attempt < _HTTP_POST_MAX_ATTEMPTS
            ):
                continue
            raise AppAuthError(
                "GitHub installation-token POST failed with "
                f"HTTP {status}: {response_body}"
            ) from exc
        except OSError as exc:
            if attempt < _HTTP_POST_MAX_ATTEMPTS:
                continue
            raise AppAuthError(
                f"GitHub installation-token POST failed after "
                f"{attempt} attempts: {exc}"
            ) from exc

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise AppAuthError(
                f"GitHub installation-token POST returned invalid JSON: {exc}"
            ) from exc

        if not isinstance(parsed, dict):
            raise AppAuthError(
                "GitHub installation-token POST returned a non-object JSON "
                f"payload: {parsed!r}"
            )
        return parsed

    raise AssertionError("_github_http_post exhausted without returning")


def main(argv: list[str]) -> int:
    """Mint a GitHub App JWT or installation token for shell callers.

    Args:
        argv: Command-line arguments excluding the module name. The sole
            argument must be ``jwt`` or ``token``.

    Returns:
        Zero on success, or a non-zero status after writing a safe error
        message to stderr.
    """
    if len(argv) != 1 or argv[0] not in {"jwt", "token"}:
        print("app_auth: usage: app_auth.py {jwt|token}", file=sys.stderr)
        return 2

    mode = argv[0]
    required_vars = [
        "BH_GITHUB_APP_ID",
        "BWS_PEM_SECRET_ID",
        "BWS_ACCESS_TOKEN",
    ]
    if mode == "token":
        required_vars.append("BH_GITHUB_APP_INSTALLATION_ID")

    missing_vars = [name for name in required_vars if not os.environ.get(name)]
    if missing_vars:
        print(
            "app_auth: missing required environment variable(s): "
            + ", ".join(missing_vars),
            file=sys.stderr,
        )
        return 2

    app_id = os.environ["BH_GITHUB_APP_ID"]
    secret_id = os.environ["BWS_PEM_SECRET_ID"]
    access_token = os.environ["BWS_ACCESS_TOKEN"]

    installation_id: int | None = None
    if mode == "token":
        try:
            installation_id = int(os.environ["BH_GITHUB_APP_INSTALLATION_ID"])
        except ValueError:
            print(
                "app_auth: BH_GITHUB_APP_INSTALLATION_ID must be an integer",
                file=sys.stderr,
            )
            return 2

    try:
        private_key_pem = bws_client.fetch_secret(
            secret_id,
            access_token=access_token,
        )
    except bws_client.BwsClientError:
        print("app_auth: failed to fetch private key", file=sys.stderr)
        return 1
    except Exception:
        print("app_auth: failed to fetch private key", file=sys.stderr)
        return 1

    if mode == "jwt":
        try:
            app_jwt = build_app_jwt(
                app_id,
                private_key_pem,
                now=int(time.time()),
            )
        except Exception:
            print(
                "app_auth: failed to sign App JWT "
                "(invalid private key material)",
                file=sys.stderr,
            )
            return 1
        print(app_jwt)
        return 0

    assert installation_id is not None  # noqa: S101 - narrowed by mode
    try:
        token, _expires_at = mint_installation_token(
            app_id,
            private_key_pem,
            installation_id,
            http_post=_github_http_post,
            now=int(time.time()),
        )
    except Exception:
        print("app_auth: failed to mint installation token", file=sys.stderr)
        return 1
    print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
