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
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
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
