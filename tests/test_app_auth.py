"""Tests for baton_harness.chain.app_auth — GitHub App JWT and token minting.

Coverage:
- ``build_app_jwt`` produces an RS256 JWT with correct claims (iss, iat,
  exp) and a 60-second clock-skew backdate on ``iat``.
- ``mint_installation_token`` POSTs to the correct URL with the correct
  ``Authorization: Bearer <jwt>`` header and parses the response.
- ``mint_installation_token`` raises on non-200 HTTP responses
  (fail-closed).
- ``mint_installation_token`` raises when the injected ``http_post``
  callable raises an exception (fail-closed).
- ``InstallationTokenProvider`` caches the token and avoids a second
  HTTP call within the TTL window.
- ``InstallationTokenProvider`` re-mints when within the refresh margin
  of expiry.
- ``bootstrap_secrets`` removes ``BWS_ACCESS_TOKEN`` from
  ``os.environ`` after fetching the private key (env-discipline scrub).
- ``bootstrap_secrets`` ensures the installation token value is absent
  from ``os.environ`` after startup (env-discipline scrub).

CLI entrypoint coverage (issue #200 — ``main()`` in
``baton_harness.chain.app_auth``, invoked as
``python -m baton_harness.chain.app_auth {jwt|token}``):

- ``main(["jwt"])`` mints and prints an App JWT to stdout, exit 0.
- ``main(["token"])`` mints and prints an installation token to stdout,
  exit 0.
- Neither the fetched PEM content nor ``BWS_ACCESS_TOKEN`` ever appears
  in captured stdout/stderr, on both the success path and the error
  path (e.g. an unparseable PEM causing ``build_app_jwt`` to raise).
- Missing/malformed required env vars (``BH_GITHUB_APP_ID``,
  ``BH_GITHUB_APP_INSTALLATION_ID``, ``BWS_PEM_SECRET_ID``,
  ``BWS_ACCESS_TOKEN``) produce a non-zero exit and a clear stderr
  message naming the missing var, without ever calling
  ``bws_client.fetch_secret`` or the network transport.
- All Bitwarden and GitHub HTTP calls are intercepted via
  ``baton_harness.chain.bws_client.fetch_secret`` and
  ``baton_harness.chain.app_auth._github_http_post`` mocks — no live
  network call is made by this test module.

These CLI tests import ``main`` locally inside each test body (not at
module import time) because the symbol does not exist yet; a top-level
import would turn every test in this file into a collection error
instead of isolating the failure to the new CLI coverage.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_der_public_key

from baton_harness.chain.app_auth import (
    AppAuthError,
    InstallationTokenProvider,
    bootstrap_secrets,
    build_app_jwt,
    mint_installation_token,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_APP_ID = "12345"
_INSTALLATION_ID = 67890
_FAKE_TOKEN = "ghs_FAKEFAKEFAKEFAKEFAKEFAKEFAKE"
_FAKE_EXPIRES_AT = "2099-01-01T00:00:00Z"


def _generate_rsa_keypair() -> tuple[str, bytes]:
    """Generate a fresh RSA keypair for test use.

    Returns:
        A tuple of (private_key_pem_str, public_key_der_bytes) where
        the PEM is the private key in PKCS8 PEM format and the bytes
        are the DER-encoded public key.
    """
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    pub_der = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return pem, pub_der


def _http_post_ok(
    url: str,
    headers: dict[str, str],
) -> dict[str, Any]:
    """Fake http_post that returns a successful token response.

    Args:
        url: The URL that would be called.
        headers: The headers that would be sent.

    Returns:
        A dict matching the GitHub API ``access_tokens`` response shape.
    """
    return {
        "token": _FAKE_TOKEN,
        "expires_at": _FAKE_EXPIRES_AT,
    }


def _make_http_error(
    code: int,
    payload: dict[str, Any],
) -> urllib.error.HTTPError:
    """Create an HTTPError with a readable JSON body."""
    body = MagicMock()
    body.read.return_value = json.dumps(payload).encode("utf-8")
    return urllib.error.HTTPError(
        url="https://api.github.com/test",
        code=code,
        msg=f"HTTP {code}",
        hdrs=None,
        fp=body,
    )


# ---------------------------------------------------------------------------
# A1. JWT build
# ---------------------------------------------------------------------------


class TestBuildAppJwt:
    """``build_app_jwt`` produces a correctly-formed RS256 JWT."""

    def test_jwt_is_rs256(self) -> None:
        """JWT header must specify ``RS256`` algorithm."""
        pem, _pub = _generate_rsa_keypair()
        now = int(time.time())

        token = build_app_jwt(app_id=_APP_ID, private_key_pem=pem, now=now)

        header = jwt.get_unverified_header(token)
        assert header["alg"] == "RS256"

    def test_jwt_iss_equals_app_id(self) -> None:
        """JWT ``iss`` claim must equal the supplied ``app_id``."""
        pem, pub_der = _generate_rsa_keypair()
        now = int(time.time())

        token = build_app_jwt(app_id=_APP_ID, private_key_pem=pem, now=now)

        pub_key = load_der_public_key(pub_der)
        claims = jwt.decode(token, pub_key, algorithms=["RS256"])  # type: ignore[arg-type]
        assert claims["iss"] == _APP_ID

    def test_jwt_iat_backdated_60_seconds(self) -> None:
        """JWT ``iat`` must be approx ``now - 60`` (clock-skew backdate).

        Tolerance window: ±5 seconds around ``now - 60``.
        """
        pem, pub_der = _generate_rsa_keypair()
        now = int(time.time())

        token = build_app_jwt(app_id=_APP_ID, private_key_pem=pem, now=now)

        pub_key = load_der_public_key(pub_der)
        claims = jwt.decode(
            token,
            pub_key,  # type: ignore[arg-type]
            algorithms=["RS256"],
            options={"verify_exp": False},
        )
        expected_iat = now - 60
        assert abs(claims["iat"] - expected_iat) <= 5, (
            f"Expected iat≈{expected_iat}, got {claims['iat']}"
        )

    def test_jwt_exp_honours_default_ttl(self) -> None:
        """JWT ``exp`` must be approx ``now + 540`` with default TTL.

        540 seconds is the default TTL.  Tolerance: ±5 seconds.
        """
        pem, pub_der = _generate_rsa_keypair()
        now = int(time.time())

        token = build_app_jwt(app_id=_APP_ID, private_key_pem=pem, now=now)

        pub_key = load_der_public_key(pub_der)
        claims = jwt.decode(
            token,
            pub_key,  # type: ignore[arg-type]
            algorithms=["RS256"],
            options={"verify_exp": False},
        )
        expected_exp = now + 540
        assert abs(claims["exp"] - expected_exp) <= 5, (
            f"Expected exp≈{expected_exp}, got {claims['exp']}"
        )

    def test_jwt_exp_honours_custom_ttl(self) -> None:
        """JWT ``exp`` must reflect a custom ``ttl_seconds`` argument."""
        pem, pub_der = _generate_rsa_keypair()
        now = int(time.time())
        custom_ttl = 300

        token = build_app_jwt(
            app_id=_APP_ID,
            private_key_pem=pem,
            now=now,
            ttl_seconds=custom_ttl,
        )

        pub_key = load_der_public_key(pub_der)
        claims = jwt.decode(
            token,
            pub_key,  # type: ignore[arg-type]
            algorithms=["RS256"],
            options={"verify_exp": False},
        )
        expected_exp = now + custom_ttl
        assert abs(claims["exp"] - expected_exp) <= 5, (
            f"Expected exp≈{expected_exp}, got {claims['exp']}"
        )

    def test_jwt_is_verifiable_with_matching_public_key(self) -> None:
        """JWT decoded with the matching public key must not raise."""
        pem, pub_der = _generate_rsa_keypair()
        now = int(time.time())

        token = build_app_jwt(app_id=_APP_ID, private_key_pem=pem, now=now)

        pub_key = load_der_public_key(pub_der)
        # If signature is wrong this raises jwt.InvalidSignatureError.
        jwt.decode(
            token,
            pub_key,  # type: ignore[arg-type]
            algorithms=["RS256"],
            options={"verify_exp": False},
        )


# ---------------------------------------------------------------------------
# A2. Mint installation token
# ---------------------------------------------------------------------------


class TestMintInstallationToken:
    """``mint_installation_token`` calls the correct endpoint and parses."""

    def test_posts_to_correct_url(self) -> None:
        """Must POST to ``/app/installations/{id}/access_tokens``."""
        pem, _ = _generate_rsa_keypair()
        captured_urls: list[str] = []

        def recording_post(
            url: str,
            headers: dict[str, str],
        ) -> dict[str, Any]:
            """Capture the URL and return a valid response."""
            captured_urls.append(url)
            return {"token": _FAKE_TOKEN, "expires_at": _FAKE_EXPIRES_AT}

        mint_installation_token(
            app_id=_APP_ID,
            private_key_pem=pem,
            installation_id=_INSTALLATION_ID,
            http_post=recording_post,
            now=int(time.time()),
        )

        assert len(captured_urls) == 1
        expected_fragment = (
            f"app/installations/{_INSTALLATION_ID}/access_tokens"
        )
        assert expected_fragment in captured_urls[0], (
            f"Expected URL containing '{expected_fragment}', "
            f"got '{captured_urls[0]}'"
        )

    def test_sends_bearer_jwt_header(self) -> None:
        """``Authorization`` header must be ``Bearer <jwt>``."""
        pem, _ = _generate_rsa_keypair()
        captured_headers: list[dict[str, str]] = []

        def recording_post(
            url: str,
            headers: dict[str, str],
        ) -> dict[str, Any]:
            """Capture headers and return a valid response."""
            captured_headers.append(dict(headers))
            return {"token": _FAKE_TOKEN, "expires_at": _FAKE_EXPIRES_AT}

        mint_installation_token(
            app_id=_APP_ID,
            private_key_pem=pem,
            installation_id=_INSTALLATION_ID,
            http_post=recording_post,
            now=int(time.time()),
        )

        assert captured_headers, "http_post was never called"
        auth = captured_headers[0].get("Authorization", "")
        assert auth.startswith("Bearer "), (
            f"Expected 'Bearer <jwt>' header, got: {auth!r}"
        )
        # The bearer value must be a valid JWT (3 dot-separated parts).
        bearer_value = auth[len("Bearer ") :]
        assert bearer_value.count(".") == 2, (
            f"Bearer value does not look like a JWT: {bearer_value!r}"
        )

    def test_returns_token_and_expires_at(self) -> None:
        """Return value must be ``(token, expires_at)`` from the response."""
        pem, _ = _generate_rsa_keypair()

        token, expires_at = mint_installation_token(
            app_id=_APP_ID,
            private_key_pem=pem,
            installation_id=_INSTALLATION_ID,
            http_post=_http_post_ok,
            now=int(time.time()),
        )

        assert token == _FAKE_TOKEN
        assert expires_at == _FAKE_EXPIRES_AT

    def test_raises_on_http_error_response(self) -> None:
        """Error response from ``http_post`` raises ``AppAuthError``."""
        pem, _ = _generate_rsa_keypair()

        def failing_post(
            url: str,
            headers: dict[str, str],
        ) -> dict[str, Any]:
            """Simulate an HTTP-layer failure."""
            raise RuntimeError("HTTP 401 Unauthorized")

        with pytest.raises(AppAuthError):
            mint_installation_token(
                app_id=_APP_ID,
                private_key_pem=pem,
                installation_id=_INSTALLATION_ID,
                http_post=failing_post,
                now=int(time.time()),
            )

    def test_raises_when_http_post_raises(self) -> None:
        """Network exception from ``http_post`` raises ``AppAuthError``."""
        pem, _ = _generate_rsa_keypair()

        def exploding_post(
            url: str,
            headers: dict[str, str],
        ) -> dict[str, Any]:
            """Simulate a network-level failure."""
            raise OSError("network unreachable")

        with pytest.raises(AppAuthError):
            mint_installation_token(
                app_id=_APP_ID,
                private_key_pem=pem,
                installation_id=_INSTALLATION_ID,
                http_post=exploding_post,
                now=int(time.time()),
            )

    def test_raises_when_response_missing_token_field(self) -> None:
        """Response JSON lacking ``token`` raises ``AppAuthError``."""
        pem, _ = _generate_rsa_keypair()

        def bad_response_post(
            url: str,
            headers: dict[str, str],
        ) -> dict[str, Any]:
            """Return a response body that omits the 'token' field."""
            return {"expires_at": _FAKE_EXPIRES_AT}

        with pytest.raises(AppAuthError):
            mint_installation_token(
                app_id=_APP_ID,
                private_key_pem=pem,
                installation_id=_INSTALLATION_ID,
                http_post=bad_response_post,
                now=int(time.time()),
            )


# ---------------------------------------------------------------------------
# A3. Cache + refresh
# ---------------------------------------------------------------------------


class TestInstallationTokenProvider:
    """``InstallationTokenProvider`` caches and refreshes tokens."""

    def test_first_call_invokes_http_post(self) -> None:
        """The first call to ``get_token`` must invoke the HTTP mint."""
        pem, _ = _generate_rsa_keypair()
        call_count = 0

        def counting_post(
            url: str,
            headers: dict[str, str],
        ) -> dict[str, Any]:
            """Count calls and return a far-future token."""
            nonlocal call_count
            call_count += 1
            return {"token": _FAKE_TOKEN, "expires_at": _FAKE_EXPIRES_AT}

        provider = InstallationTokenProvider(
            app_id=_APP_ID,
            private_key_pem=pem,
            installation_id=_INSTALLATION_ID,
            http_post=counting_post,
        )
        provider.get_token()

        assert call_count == 1, (
            f"Expected 1 http_post call on first get_token, got {call_count}"
        )

    def test_second_call_within_ttl_does_not_remint(self) -> None:
        """A second call within TTL must NOT call ``http_post`` again."""
        pem, _ = _generate_rsa_keypair()
        call_count = 0

        def counting_post(
            url: str,
            headers: dict[str, str],
        ) -> dict[str, Any]:
            """Count calls and return a far-future token."""
            nonlocal call_count
            call_count += 1
            return {"token": _FAKE_TOKEN, "expires_at": _FAKE_EXPIRES_AT}

        provider = InstallationTokenProvider(
            app_id=_APP_ID,
            private_key_pem=pem,
            installation_id=_INSTALLATION_ID,
            http_post=counting_post,
        )
        provider.get_token()
        provider.get_token()  # second call, still within TTL

        assert call_count == 1, (
            f"Expected 1 http_post call (cached), got {call_count}"
        )

    def test_call_past_refresh_margin_remints(self) -> None:
        """A call past the refresh margin of expiry must re-mint."""
        import datetime

        pem, _ = _generate_rsa_keypair()
        call_count = 0

        def counting_post(
            url: str,
            headers: dict[str, str],
        ) -> dict[str, Any]:
            """Count calls; return token expiring 1 second from now."""
            nonlocal call_count
            call_count += 1
            exp = datetime.datetime.now(
                tz=datetime.timezone.utc
            ) + datetime.timedelta(seconds=1)
            return {
                "token": f"ghs_EXPIRED_{call_count}",
                "expires_at": exp.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }

        provider = InstallationTokenProvider(
            app_id=_APP_ID,
            private_key_pem=pem,
            installation_id=_INSTALLATION_ID,
            http_post=counting_post,
        )
        provider.get_token()  # primes cache with a near-expired token

        # Second call detects expiry within the margin and remints.
        provider.get_token()

        assert call_count == 2, (
            f"Expected 2 http_post calls (initial + remint), got {call_count}"
        )

    def test_get_token_returns_token_string(self) -> None:
        """``get_token`` must return the token string, not a tuple."""
        pem, _ = _generate_rsa_keypair()

        provider = InstallationTokenProvider(
            app_id=_APP_ID,
            private_key_pem=pem,
            installation_id=_INSTALLATION_ID,
            http_post=_http_post_ok,
        )
        token = provider.get_token()

        assert isinstance(token, str), (
            f"Expected get_token() to return str, got {type(token)}"
        )
        assert token == _FAKE_TOKEN


# ---------------------------------------------------------------------------
# D. Env-discipline: bootstrap_secrets scrubs privileged tokens
# ---------------------------------------------------------------------------


class TestBootstrapSecretsEnvDiscipline:
    """``bootstrap_secrets`` scrubs privileged tokens from ``os.environ``."""

    def test_real_mint_path_uses_http_transport(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Bootstrap with the real mint function must call GitHub over HTTP."""
        monkeypatch.setenv("BWS_ACCESS_TOKEN", "fake-bws-token-for-test")
        pem, _ = _generate_rsa_keypair()

        def fake_fetch_secret(
            secret_id: str,
            *,
            access_token: str,
            run: object = None,
        ) -> str:
            """Return a valid test PEM without touching Bitwarden."""
            return pem

        response = MagicMock()
        response.read.return_value = json.dumps(
            {
                "token": _FAKE_TOKEN,
                "expires_at": _FAKE_EXPIRES_AT,
            }
        ).encode("utf-8")
        response.__enter__.return_value = response
        response.__exit__.return_value = None

        with patch(
            "urllib.request.urlopen",
            return_value=response,
        ) as mock_open:
            token, expires_at = bootstrap_secrets(
                app_id=_APP_ID,
                app_private_key_bws_id="bws-secret-id-for-key",
                installation_id=_INSTALLATION_ID,
                fetch_secret=fake_fetch_secret,
                mint_token=mint_installation_token,
            )

        assert token == _FAKE_TOKEN
        assert expires_at == _FAKE_EXPIRES_AT
        mock_open.assert_called_once()


class TestGithubHttpPost:
    """Real GitHub HTTP transport for installation-token minting."""

    def test_retries_retryable_5xx_then_returns_json(self) -> None:
        """A transient 5xx from GitHub must be retried and then succeed."""
        import baton_harness.chain.app_auth as app_auth

        response = MagicMock()
        response.read.return_value = json.dumps(
            {
                "token": _FAKE_TOKEN,
                "expires_at": _FAKE_EXPIRES_AT,
            }
        ).encode("utf-8")
        response.__enter__.return_value = response
        response.__exit__.return_value = None

        with patch(
            "urllib.request.urlopen",
            side_effect=[
                _make_http_error(502, {"message": "bad gateway"}),
                response,
            ],
        ) as mock_open:
            result = app_auth._github_http_post(
                "https://api.github.com/test",
                {"Authorization": "Bearer fake"},
            )

        assert result["token"] == _FAKE_TOKEN
        assert mock_open.call_count == 2

    def test_http_401_raises_without_retry(self) -> None:
        """A non-retryable 4xx from GitHub must fail closed immediately."""
        import baton_harness.chain.app_auth as app_auth

        with patch(
            "urllib.request.urlopen",
            side_effect=_make_http_error(
                401,
                {"message": "Bad credentials"},
            ),
        ) as mock_open:
            with pytest.raises(AppAuthError, match="HTTP 401"):
                app_auth._github_http_post(
                    "https://api.github.com/test",
                    {"Authorization": "Bearer fake"},
                )

        assert mock_open.call_count == 1

    def test_retries_on_transient_oserror_then_succeeds(self) -> None:
        """A transient OSError must be retried and then return parsed JSON.

        ``socket.timeout`` is a subclass of ``OSError``.  The first
        urlopen call raises it; the second succeeds.  The caller must
        receive the parsed response dict and urlopen must be called
        exactly twice.
        """
        import baton_harness.chain.app_auth as app_auth

        response = MagicMock()
        response.read.return_value = json.dumps(
            {
                "token": _FAKE_TOKEN,
                "expires_at": _FAKE_EXPIRES_AT,
            }
        ).encode("utf-8")
        response.__enter__.return_value = response
        response.__exit__.return_value = None

        with patch(
            "urllib.request.urlopen",
            side_effect=[
                TimeoutError("timed out"),
                response,
            ],
        ) as mock_open:
            result = app_auth._github_http_post(
                "https://api.github.com/test",
                {"Authorization": "Bearer fake"},
            )

        assert result["token"] == _FAKE_TOKEN
        assert mock_open.call_count == 2

    def test_raises_app_auth_error_after_oserror_retries_exhausted(
        self,
    ) -> None:
        """Persistent OSError across all attempts must raise ``AppAuthError``.

        After ``_HTTP_POST_MAX_ATTEMPTS`` failed attempts, the function
        must raise ``AppAuthError`` (not the raw ``OSError``), urlopen
        must be called exactly ``_HTTP_POST_MAX_ATTEMPTS`` times, and
        the error message must reference the underlying timeout.
        """
        import baton_harness.chain.app_auth as app_auth

        with patch(
            "urllib.request.urlopen",
            side_effect=TimeoutError("timed out"),
        ) as mock_open:
            with pytest.raises(AppAuthError, match="timed out"):
                app_auth._github_http_post(
                    "https://api.github.com/test",
                    {"Authorization": "Bearer fake"},
                )

        assert mock_open.call_count == app_auth._HTTP_POST_MAX_ATTEMPTS


class TestBootstrapSecretsEnvDisciplineInvariants:
    """Bootstrap invariants beyond the real-mint transport path."""

    def test_bws_access_token_not_in_environ_after_bootstrap(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``BWS_ACCESS_TOKEN`` must be absent from ``os.environ`` post-boot.

        Rationale: the worker subprocess inherits ``os.environ``.  If
        ``BWS_ACCESS_TOKEN`` remains in the env, the worker can read the
        harness machine-account bootstrap secret -- a privilege escalation.
        """
        monkeypatch.setenv("BWS_ACCESS_TOKEN", "fake-bws-token-for-test")

        def fake_fetch_secret(
            secret_id: str,
            *,
            access_token: str,
            run: object = None,
        ) -> str:
            """Return a fake PEM without touching real Bitwarden."""
            return "FAKE_PEM_PRIVATE_KEY_MATERIAL"

        def fake_mint(
            app_id: str,
            private_key_pem: str,
            installation_id: int,
            *,
            http_post: object,
            now: int,
        ) -> tuple[str, str]:
            """Return a fake token without a real GitHub API call."""
            return (_FAKE_TOKEN, _FAKE_EXPIRES_AT)

        bootstrap_secrets(
            app_id=_APP_ID,
            app_private_key_bws_id="bws-secret-id-for-key",
            installation_id=_INSTALLATION_ID,
            fetch_secret=fake_fetch_secret,
            mint_token=fake_mint,
        )

        assert "BWS_ACCESS_TOKEN" not in os.environ, (
            "BWS_ACCESS_TOKEN must be scrubbed from os.environ after "
            "bootstrap_secrets; found in env"
        )

    def test_installation_token_not_in_environ_after_bootstrap(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Installation token value must not appear in ``os.environ``.

        The installation token is the harness's privileged merge
        credential.  If it leaks into the inherited env the worker gains
        merge authority.
        """
        monkeypatch.setenv("BWS_ACCESS_TOKEN", "fake-bws-token-for-test")
        unique_token = "ghs_UNIQUE_SENTINEL_TOKEN_12345678"

        def fake_fetch_secret(
            secret_id: str,
            *,
            access_token: str,
            run: object = None,
        ) -> str:
            """Return a fake PEM without touching real Bitwarden."""
            return "FAKE_PEM_PRIVATE_KEY_MATERIAL"

        def fake_mint(
            app_id: str,
            private_key_pem: str,
            installation_id: int,
            *,
            http_post: object,
            now: int,
        ) -> tuple[str, str]:
            """Return the unique sentinel token."""
            return (unique_token, _FAKE_EXPIRES_AT)

        bootstrap_secrets(
            app_id=_APP_ID,
            app_private_key_bws_id="bws-secret-id-for-key",
            installation_id=_INSTALLATION_ID,
            fetch_secret=fake_fetch_secret,
            mint_token=fake_mint,
        )

        env_values = list(os.environ.values())
        assert unique_token not in env_values, (
            "Installation token must not be set anywhere in os.environ; "
            f"found token {unique_token!r} in env values"
        )


# ---------------------------------------------------------------------------
# E. CLI entrypoint (issue #200): main() in baton_harness.chain.app_auth
#
# Contract asserted by these tests (authored here — no implementation
# exists yet):
#   - ``baton_harness.chain.app_auth.main(argv: list[str]) -> int``
#   - ``main(["jwt"])``   -> mints + prints an App JWT to stdout, exit 0.
#   - ``main(["token"])`` -> mints + prints an installation token to
#     stdout, exit 0.
#   - Required env vars: BH_GITHUB_APP_ID, BWS_PEM_SECRET_ID,
#     BWS_ACCESS_TOKEN (both modes); BH_GITHUB_APP_INSTALLATION_ID
#     (token mode only — jwt mode does not need an installation id).
#   - PEM fetch goes through ``baton_harness.chain.bws_client
#     .fetch_secret`` (patched at that module attribute, matching the
#     established pattern in test_cli_bootstrap_vault.py).
#   - The GitHub HTTP transport for token mode goes through
#     ``baton_harness.chain.app_auth._github_http_post`` (the existing
#     internal transport helper already used by
#     ``build_installation_token_provider``).
# ---------------------------------------------------------------------------

_CLI_APP_ID = "424242"
_CLI_INSTALLATION_ID = "989898"
_CLI_PEM_SECRET_ID = "pem-secret-cli-aaaa-bbbb-cccc-dddddddddddd"
_CLI_ACCESS_TOKEN_SENTINEL = "0.cli-sentinel-bws-access-token-fake-9f3c1a"


@pytest.fixture()
def cli_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the four env vars the CLI entrypoint requires (issue #200)."""
    monkeypatch.setenv("BH_GITHUB_APP_ID", _CLI_APP_ID)
    monkeypatch.setenv("BH_GITHUB_APP_INSTALLATION_ID", _CLI_INSTALLATION_ID)
    monkeypatch.setenv("BWS_PEM_SECRET_ID", _CLI_PEM_SECRET_ID)
    monkeypatch.setenv("BWS_ACCESS_TOKEN", _CLI_ACCESS_TOKEN_SENTINEL)


def _make_cli_fetch_secret_stub(pem: str) -> Callable[..., str]:
    """Return a fetch_secret stub bound to the CLI's PEM secret ID.

    Asserts it is called with the expected secret_id and the
    access_token sourced from BWS_ACCESS_TOKEN (set by ``cli_env``) —
    this doubles as proof that the CLI forwards the env-sourced access
    token rather than a hardcoded or empty value.

    Args:
        pem: The PEM string to return when called correctly.

    Returns:
        A callable matching the ``bws_client.fetch_secret`` signature.
    """

    def _stub(
        secret_id: str,
        *,
        access_token: str | None = None,
        run: object = None,
    ) -> str:
        assert secret_id == _CLI_PEM_SECRET_ID, (
            f"expected fetch_secret to be called with "
            f"{_CLI_PEM_SECRET_ID!r}, got {secret_id!r}"
        )
        assert access_token == _CLI_ACCESS_TOKEN_SENTINEL, (
            "expected fetch_secret to receive the BWS_ACCESS_TOKEN env "
            f"value, got {access_token!r}"
        )
        return pem

    return _stub


class TestCliJwtMode:
    """``main(["jwt"])`` mints and prints an App JWT.

    MUST FAIL today: ``main`` does not exist yet in
    ``baton_harness.chain.app_auth`` — expect ``ImportError``.
    """

    def test_prints_valid_app_jwt_and_exits_zero(
        self,
        cli_env: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Stdout carries a JWT whose iss claim equals BH_GITHUB_APP_ID."""
        pem, pub_der = _generate_rsa_keypair()
        stub = _make_cli_fetch_secret_stub(pem)

        with patch(
            "baton_harness.chain.bws_client.fetch_secret",
            side_effect=stub,
        ):
            from baton_harness.chain.app_auth import main

            exit_code = main(["jwt"])

        captured = capsys.readouterr()
        assert exit_code == 0, (
            f"expected exit 0, got {exit_code}; stderr={captured.err!r}"
        )
        token = captured.out.strip()
        pub_key = load_der_public_key(pub_der)
        claims = jwt.decode(
            token,
            pub_key,  # type: ignore[arg-type]
            algorithms=["RS256"],
            options={"verify_exp": False},
        )
        assert claims["iss"] == _CLI_APP_ID

    def test_does_not_leak_pem_or_access_token_in_output(
        self,
        cli_env: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """PEM content and BWS_ACCESS_TOKEN never reach stdout/stderr."""
        pem, _pub_der = _generate_rsa_keypair()
        stub = _make_cli_fetch_secret_stub(pem)

        with patch(
            "baton_harness.chain.bws_client.fetch_secret",
            side_effect=stub,
        ):
            from baton_harness.chain.app_auth import main

            main(["jwt"])

        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert pem not in combined, (
            "raw PEM content leaked into CLI stdout/stderr"
        )
        assert "BEGIN PRIVATE KEY" not in combined, (
            "PEM header leaked into CLI stdout/stderr"
        )
        assert _CLI_ACCESS_TOKEN_SENTINEL not in combined, (
            "BWS_ACCESS_TOKEN value leaked into CLI stdout/stderr"
        )


class TestCliTokenMode:
    """``main(["token"])`` mints and prints an installation token.

    MUST FAIL today: ``main`` does not exist yet — expect ``ImportError``.
    """

    def test_prints_installation_token_and_exits_zero(
        self,
        cli_env: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Stdout equals exactly the minted installation token string."""
        pem, _pub_der = _generate_rsa_keypair()
        stub = _make_cli_fetch_secret_stub(pem)

        captured_urls: list[str] = []

        def fake_http_post(
            url: str,
            headers: dict[str, str],
        ) -> dict[str, Any]:
            captured_urls.append(url)
            return {"token": _FAKE_TOKEN, "expires_at": _FAKE_EXPIRES_AT}

        with (
            patch(
                "baton_harness.chain.bws_client.fetch_secret",
                side_effect=stub,
            ),
            patch(
                "baton_harness.chain.app_auth._github_http_post",
                side_effect=fake_http_post,
            ),
        ):
            from baton_harness.chain.app_auth import main

            exit_code = main(["token"])

        captured = capsys.readouterr()
        assert exit_code == 0, (
            f"expected exit 0, got {exit_code}; stderr={captured.err!r}"
        )
        assert captured.out.strip() == _FAKE_TOKEN, (
            "expected stdout to be exactly the minted token, got "
            f"{captured.out!r}"
        )
        assert captured_urls, "the mocked GitHub transport was never called"
        assert (
            f"app/installations/{_CLI_INSTALLATION_ID}/access_tokens"
            in captured_urls[0]
        )

    def test_does_not_leak_pem_or_access_token_in_output(
        self,
        cli_env: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """PEM content and BWS_ACCESS_TOKEN never reach stdout/stderr."""
        pem, _pub_der = _generate_rsa_keypair()
        stub = _make_cli_fetch_secret_stub(pem)

        def fake_http_post(
            url: str,
            headers: dict[str, str],
        ) -> dict[str, Any]:
            return {"token": _FAKE_TOKEN, "expires_at": _FAKE_EXPIRES_AT}

        with (
            patch(
                "baton_harness.chain.bws_client.fetch_secret",
                side_effect=stub,
            ),
            patch(
                "baton_harness.chain.app_auth._github_http_post",
                side_effect=fake_http_post,
            ),
        ):
            from baton_harness.chain.app_auth import main

            main(["token"])

        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert pem not in combined, (
            "raw PEM content leaked into CLI stdout/stderr"
        )
        assert _CLI_ACCESS_TOKEN_SENTINEL not in combined, (
            "BWS_ACCESS_TOKEN value leaked into CLI stdout/stderr"
        )

    def test_no_live_network_call_when_transport_unmocked_would_error(
        self,
        cli_env: None,
    ) -> None:
        """The CLI must route through the injected transport, not urlopen.

        Patches ``urllib.request.urlopen`` to raise if invoked at all,
        as a defensive check that token mode never falls through to a
        raw socket call outside the mocked ``_github_http_post`` seam.
        """
        pem, _pub_der = _generate_rsa_keypair()
        stub = _make_cli_fetch_secret_stub(pem)

        def fake_http_post(
            url: str,
            headers: dict[str, str],
        ) -> dict[str, Any]:
            return {"token": _FAKE_TOKEN, "expires_at": _FAKE_EXPIRES_AT}

        def _explode(*args: object, **kwargs: object) -> None:
            raise AssertionError(
                "urllib.request.urlopen must not be called directly by "
                "the CLI — it must go through the mocked "
                "_github_http_post seam"
            )

        with (
            patch(
                "baton_harness.chain.bws_client.fetch_secret",
                side_effect=stub,
            ),
            patch(
                "baton_harness.chain.app_auth._github_http_post",
                side_effect=fake_http_post,
            ),
            patch("urllib.request.urlopen", side_effect=_explode),
        ):
            from baton_harness.chain.app_auth import main

            exit_code = main(["token"])

        assert exit_code == 0


class TestCliMissingEnvVars:
    """Missing/malformed required env vars fail clean with a non-zero exit.

    MUST FAIL today: ``main`` does not exist yet — expect ``ImportError``.
    """

    def test_missing_app_id_exits_nonzero_without_calling_vault(
        self,
        cli_env: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Absent BH_GITHUB_APP_ID fails before any vault fetch."""
        monkeypatch.delenv("BH_GITHUB_APP_ID", raising=False)
        fetch_mock = MagicMock(
            side_effect=AssertionError(
                "fetch_secret must not be called when BH_GITHUB_APP_ID "
                "is missing"
            )
        )

        with patch(
            "baton_harness.chain.bws_client.fetch_secret",
            fetch_mock,
        ):
            from baton_harness.chain.app_auth import main

            exit_code = main(["jwt"])

        captured = capsys.readouterr()
        assert exit_code != 0, "expected a non-zero exit code"
        assert "BH_GITHUB_APP_ID" in captured.err, (
            f"expected stderr to name the missing var, got {captured.err!r}"
        )
        fetch_mock.assert_not_called()

    def test_missing_pem_secret_id_exits_nonzero_without_calling_vault(
        self,
        cli_env: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Absent BWS_PEM_SECRET_ID fails before any vault fetch."""
        monkeypatch.delenv("BWS_PEM_SECRET_ID", raising=False)
        fetch_mock = MagicMock(
            side_effect=AssertionError(
                "fetch_secret must not be called when BWS_PEM_SECRET_ID "
                "is missing"
            )
        )

        with patch(
            "baton_harness.chain.bws_client.fetch_secret",
            fetch_mock,
        ):
            from baton_harness.chain.app_auth import main

            exit_code = main(["jwt"])

        captured = capsys.readouterr()
        assert exit_code != 0, "expected a non-zero exit code"
        assert "BWS_PEM_SECRET_ID" in captured.err, (
            f"expected stderr to name the missing var, got {captured.err!r}"
        )
        fetch_mock.assert_not_called()

    def test_missing_access_token_exits_nonzero_without_calling_vault(
        self,
        cli_env: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Absent BWS_ACCESS_TOKEN fails before any vault fetch."""
        monkeypatch.delenv("BWS_ACCESS_TOKEN", raising=False)
        fetch_mock = MagicMock(
            side_effect=AssertionError(
                "fetch_secret must not be called when BWS_ACCESS_TOKEN "
                "is missing"
            )
        )

        with patch(
            "baton_harness.chain.bws_client.fetch_secret",
            fetch_mock,
        ):
            from baton_harness.chain.app_auth import main

            exit_code = main(["jwt"])

        captured = capsys.readouterr()
        assert exit_code != 0, "expected a non-zero exit code"
        assert "BWS_ACCESS_TOKEN" in captured.err, (
            f"expected stderr to name the missing var, got {captured.err!r}"
        )
        fetch_mock.assert_not_called()

    def test_missing_installation_id_fails_token_mode_only(
        self,
        cli_env: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Token mode requires the installation id; jwt mode does not.

        Confirms the missing-var check is mode-aware rather than an
        unconditional gate on all four vars regardless of the selected
        output mode.
        """
        monkeypatch.delenv("BH_GITHUB_APP_INSTALLATION_ID", raising=False)
        pem, _pub_der = _generate_rsa_keypair()
        stub = _make_cli_fetch_secret_stub(pem)

        with patch(
            "baton_harness.chain.bws_client.fetch_secret",
            side_effect=stub,
        ):
            from baton_harness.chain.app_auth import main

            token_exit = main(["token"])
            token_captured = capsys.readouterr()

            jwt_exit = main(["jwt"])
            jwt_captured = capsys.readouterr()

        assert token_exit != 0, "expected token mode to fail: no exit code"
        assert "BH_GITHUB_APP_INSTALLATION_ID" in token_captured.err
        assert jwt_exit == 0, (
            "jwt mode does not require BH_GITHUB_APP_INSTALLATION_ID and "
            f"should have succeeded; stderr={jwt_captured.err!r}"
        )

    def test_malformed_installation_id_exits_nonzero(
        self,
        cli_env: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A non-numeric BH_GITHUB_APP_INSTALLATION_ID fails clean."""
        monkeypatch.setenv("BH_GITHUB_APP_INSTALLATION_ID", "not-a-number")
        pem, _pub_der = _generate_rsa_keypair()
        stub = _make_cli_fetch_secret_stub(pem)

        with patch(
            "baton_harness.chain.bws_client.fetch_secret",
            side_effect=stub,
        ):
            from baton_harness.chain.app_auth import main

            exit_code = main(["token"])

        captured = capsys.readouterr()
        assert exit_code != 0, "expected a non-zero exit for malformed id"
        assert "BH_GITHUB_APP_INSTALLATION_ID" in captured.err, (
            f"expected stderr to name the malformed var, got {captured.err!r}"
        )
        assert _CLI_ACCESS_TOKEN_SENTINEL not in captured.err, (
            "BWS_ACCESS_TOKEN leaked into the malformed-id error message"
        )


class TestCliErrorPathDoesNotLeakSecrets:
    """Secrets never appear in output even when minting itself fails.

    MUST FAIL today: ``main`` does not exist yet — expect ``ImportError``.
    """

    def test_unparseable_pem_error_does_not_leak_pem_or_token(
        self,
        cli_env: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An invalid PEM from the vault raises cleanly, without leaking it.

        ``fetch_secret`` is stubbed to return an obviously-fake,
        structurally invalid PEM string (not a real secret) so that
        ``build_app_jwt`` raises when asked to sign with it. The CLI
        must catch this, print a non-empty error to stderr, exit
        non-zero, and never echo the fake PEM string or the
        BWS_ACCESS_TOKEN sentinel back to the caller.
        """
        fake_invalid_pem = (
            "-----BEGIN FAKE PLACEHOLDER-----\n"
            "not-a-real-key-obviously-fake-marker-8f2c\n"
            "-----END FAKE PLACEHOLDER-----\n"
        )
        stub = _make_cli_fetch_secret_stub(fake_invalid_pem)

        with patch(
            "baton_harness.chain.bws_client.fetch_secret",
            side_effect=stub,
        ):
            from baton_harness.chain.app_auth import main

            exit_code = main(["jwt"])

        captured = capsys.readouterr()
        assert exit_code != 0, (
            "expected a non-zero exit when the vault PEM is invalid"
        )
        combined = captured.out + captured.err
        assert fake_invalid_pem not in combined, (
            "the invalid PEM value leaked into CLI output on the error path"
        )
        assert "8f2c" not in combined, (
            "the fake PEM marker leaked into CLI output on the error path"
        )
        assert _CLI_ACCESS_TOKEN_SENTINEL not in combined, (
            "BWS_ACCESS_TOKEN leaked into CLI output on the error path"
        )
        assert captured.err.strip(), (
            "expected a non-empty error message on stderr"
        )

    def test_vault_fetch_failure_does_not_leak_access_token(
        self,
        cli_env: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A vault-fetch failure is reported without leaking the token.

        Simulates a ``BwsClientError`` from ``fetch_secret`` itself
        (e.g. a vault outage) and asserts the CLI's error report never
        contains the BWS_ACCESS_TOKEN value that was used for the
        attempted fetch.
        """
        from baton_harness.chain.bws_client import BwsClientError

        def failing_fetch(
            secret_id: str,
            *,
            access_token: str | None = None,
            run: object = None,
        ) -> str:
            raise BwsClientError(
                f"bws exited 1 for secret {secret_id!r} (simulated outage)"
            )

        with patch(
            "baton_harness.chain.bws_client.fetch_secret",
            side_effect=failing_fetch,
        ):
            from baton_harness.chain.app_auth import main

            exit_code = main(["jwt"])

        captured = capsys.readouterr()
        assert exit_code != 0, (
            "expected a non-zero exit when the vault fetch fails"
        )
        combined = captured.out + captured.err
        assert _CLI_ACCESS_TOKEN_SENTINEL not in combined, (
            "BWS_ACCESS_TOKEN leaked into CLI output on the vault-fetch "
            "error path"
        )
        assert captured.err.strip(), (
            "expected a non-empty error message on stderr"
        )

    def test_vault_fetch_error_text_does_not_leak_access_token(
        self,
        cli_env: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Vault error text is reported without leaking the access token."""
        from baton_harness.chain.bws_client import BwsClientError

        def failing_fetch(
            secret_id: str,
            *,
            access_token: str | None = None,
            run: object = None,
        ) -> str:
            raise BwsClientError(
                f"bws exited 1: leaked {_CLI_ACCESS_TOKEN_SENTINEL} in stderr"
            )

        with patch(
            "baton_harness.chain.bws_client.fetch_secret",
            side_effect=failing_fetch,
        ):
            from baton_harness.chain.app_auth import main

            exit_code = main(["jwt"])

        captured = capsys.readouterr()
        assert exit_code != 0, (
            "expected a non-zero exit when the vault fetch fails"
        )
        combined = captured.out + captured.err
        assert _CLI_ACCESS_TOKEN_SENTINEL not in combined, (
            "BWS_ACCESS_TOKEN leaked through vault error text"
        )
        assert captured.err.strip(), (
            "expected a non-empty error message on stderr"
        )
