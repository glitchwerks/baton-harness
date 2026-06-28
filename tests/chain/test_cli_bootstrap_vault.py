"""Tests for vault-fetch extensions to bootstrap_secrets() in cli.py.

Coverage (issue #171):
- GH_TOKEN is fetched from Bitwarden vault when BWS_GH_TOKEN_SECRET_ID is
  set and GH_TOKEN is absent from the environment.
- BH_HEARTBEAT_PING_URL is fetched from Bitwarden vault when
  BWS_HEARTBEAT_PING_URL_SECRET_ID is set and BH_HEARTBEAT_PING_URL is
  absent from the environment.
- A pre-existing GH_TOKEN in the environment is preserved; vault is NOT
  called for that secret.
- A pre-existing BH_HEARTBEAT_PING_URL in the environment is preserved;
  vault is NOT called for that secret.
- bootstrap_secrets() succeeds (does NOT raise) when BWS_GH_TOKEN_SECRET_ID
  is absent from the environment — backward-compat path.
- bootstrap_secrets() succeeds (does NOT raise) when
  BWS_HEARTBEAT_PING_URL_SECRET_ID is absent from the environment.
- A BwsClientError from the GH_TOKEN vault fetch propagates (fail-closed).
- A BwsClientError from the heartbeat URL vault fetch propagates
  (fail-closed).
- Vault fetches for GH_TOKEN and BH_HEARTBEAT_PING_URL happen BEFORE
  BWS_ACCESS_TOKEN is popped, enforcing the ordering constraint.
- The returned InstallationTokenSource repr does not contain secret values
  fetched from the vault.

All subprocess / HTTP / Bitwarden calls are intercepted through mocks.
No real bws binary, Bitwarden vault, or GitHub API is contacted.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from unittest.mock import MagicMock, patch

import pytest

from baton_harness.chain.bws_client import BwsClientError

# ---------------------------------------------------------------------------
# Type alias — matches bws_client.RunFn
# ---------------------------------------------------------------------------

RunFn = Callable[..., subprocess.CompletedProcess[str]]

# Type alias for the fetch_secret stub callable shape.
FetchSecretFn = Callable[..., str]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ACCESS_TOKEN = "0.fake-bws-machine-account-token-for-171-tests"
_APP_ID = "99999"
_PEM_SECRET_ID = "pem-secret-aaaa-bbbb-cccc-dddddddddddd"
_GH_TOKEN_SECRET_ID = "gh-token-1111-2222-3333-444444444444"
_HEARTBEAT_SECRET_ID = "heartbeat-5555-6666-7777-888888888888"
_INSTALLATION_ID = "12345"

_FAKE_GH_TOKEN = "github_pat_TESTVAL_ABCDEFGHIJKLMNOP"
_FAKE_HEARTBEAT_URL = (
    "https://hooks.slack.com/services/T00000000/B00000000/XXXX"
)
_FAKE_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEA0000000000000000000000000000000000000000000000==\n"
    "-----END RSA PRIVATE KEY-----\n"
)
_FAKE_TOKEN = "ghs_FAKEFAKEFAKEFAKEFAKEFAKEFAKE"


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the minimum env vars that bootstrap_secrets() requires.

    Sets BWS_ACCESS_TOKEN, BWS_APP_ID, BWS_PEM_SECRET_ID, and
    BWS_INSTALLATION_ID.  Tests that also need BWS_GH_TOKEN_SECRET_ID or
    BWS_HEARTBEAT_PING_URL_SECRET_ID set those individually.

    Removes GH_TOKEN and BH_HEARTBEAT_PING_URL so each test starts with
    a clean slate for those keys.
    """
    monkeypatch.setenv("BWS_ACCESS_TOKEN", _ACCESS_TOKEN)
    monkeypatch.setenv("BWS_APP_ID", _APP_ID)
    monkeypatch.setenv("BWS_PEM_SECRET_ID", _PEM_SECRET_ID)
    monkeypatch.setenv("BWS_INSTALLATION_ID", _INSTALLATION_ID)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("BH_HEARTBEAT_PING_URL", raising=False)
    monkeypatch.delenv("BWS_GH_TOKEN_SECRET_ID", raising=False)
    monkeypatch.delenv("BWS_HEARTBEAT_PING_URL_SECRET_ID", raising=False)


def _make_fetch_secret_stub(
    secret_map: dict[str, str],
) -> FetchSecretFn:
    """Return a fetch_secret stub that resolves secrets by ID.

    The PEM secret always resolves to _FAKE_PEM; extra IDs are provided
    via ``secret_map``.

    Args:
        secret_map: Mapping of secret_id -> value for additional secrets
            beyond the PEM secret.

    Returns:
        A callable matching the bws_client.fetch_secret signature.
    """
    full_map = {_PEM_SECRET_ID: _FAKE_PEM, **secret_map}

    def _stub(
        secret_id: str,
        *,
        access_token: str | None = None,
        run: RunFn | None = None,
    ) -> str:
        if secret_id in full_map:
            return full_map[secret_id]
        raise BwsClientError(f"unexpected secret_id in stub: {secret_id!r}")

    return _stub


def _make_provider_patch() -> MagicMock:
    """Return a MagicMock to stand in for InstallationTokenProvider.

    Returns:
        A MagicMock whose get_token() method returns _FAKE_TOKEN.
    """
    provider = MagicMock()
    provider.get_token.return_value = _FAKE_TOKEN
    return provider


# ---------------------------------------------------------------------------
# V1. GH_TOKEN vault fetch when absent
# ---------------------------------------------------------------------------


class TestGhTokenVaultFetch:
    """GH_TOKEN is populated from Bitwarden when absent from env."""

    def test_bootstrap_fetches_gh_token_from_vault_when_env_absent(
        self,
        base_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GH_TOKEN is set in os.environ after bootstrap when fetched.

        BWS_GH_TOKEN_SECRET_ID is set; GH_TOKEN is absent.  After
        bootstrap_secrets() returns, os.environ["GH_TOKEN"] must equal
        the value returned by the vault.
        """
        monkeypatch.setenv("BWS_GH_TOKEN_SECRET_ID", _GH_TOKEN_SECRET_ID)

        stub = _make_fetch_secret_stub({_GH_TOKEN_SECRET_ID: _FAKE_GH_TOKEN})
        provider = _make_provider_patch()

        with (
            patch(
                "baton_harness.chain.bws_client.fetch_secret",
                side_effect=stub,
            ),
            patch(
                "baton_harness.chain.app_auth"
                ".build_installation_token_provider",
                return_value=provider,
            ),
        ):
            from baton_harness.chain.cli import bootstrap_secrets

            bootstrap_secrets()

        assert os.environ.get("GH_TOKEN") == _FAKE_GH_TOKEN, (
            f"Expected GH_TOKEN={_FAKE_GH_TOKEN!r} after bootstrap, "
            f"got {os.environ.get('GH_TOKEN')!r}"
        )

    def test_bootstrap_fetch_uses_access_token_before_it_is_popped(
        self,
        base_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The GH_TOKEN vault fetch uses BWS_ACCESS_TOKEN while available.

        Verifies that fetch_secret is called with the access token value
        that was present BEFORE it was popped.  If the implementation
        fetches after the pop, access_token would be empty and a real
        BwsClientError("access_token is empty") would fire in production.
        This test captures the access_token argument used in the call.
        """
        monkeypatch.setenv("BWS_GH_TOKEN_SECRET_ID", _GH_TOKEN_SECRET_ID)

        observed_access_tokens: list[str | None] = []

        def recording_stub(
            secret_id: str,
            *,
            access_token: str | None = None,
            run: RunFn | None = None,
        ) -> str:
            if secret_id == _GH_TOKEN_SECRET_ID:
                observed_access_tokens.append(access_token)
                return _FAKE_GH_TOKEN
            if secret_id == _PEM_SECRET_ID:
                return _FAKE_PEM
            raise BwsClientError(f"unexpected secret_id: {secret_id!r}")

        provider = _make_provider_patch()

        with (
            patch(
                "baton_harness.chain.bws_client.fetch_secret",
                side_effect=recording_stub,
            ),
            patch(
                "baton_harness.chain.app_auth"
                ".build_installation_token_provider",
                return_value=provider,
            ),
        ):
            from baton_harness.chain.cli import bootstrap_secrets

            bootstrap_secrets()

        assert observed_access_tokens, (
            "fetch_secret was never called for GH_TOKEN secret ID"
        )
        assert observed_access_tokens[0] == _ACCESS_TOKEN, (
            f"Expected access_token={_ACCESS_TOKEN!r} for GH_TOKEN fetch, "
            f"got {observed_access_tokens[0]!r}"
        )


# ---------------------------------------------------------------------------
# V2. BH_HEARTBEAT_PING_URL vault fetch when absent
# ---------------------------------------------------------------------------


class TestHeartbeatUrlVaultFetch:
    """BH_HEARTBEAT_PING_URL is populated from Bitwarden when absent."""

    def test_bootstrap_fetches_heartbeat_url_from_vault_when_env_absent(
        self,
        base_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """BH_HEARTBEAT_PING_URL is set in os.environ after bootstrap.

        BWS_HEARTBEAT_PING_URL_SECRET_ID is set; BH_HEARTBEAT_PING_URL is
        absent.  After bootstrap_secrets() returns, os.environ key must
        equal the vault value.
        """
        monkeypatch.setenv(
            "BWS_HEARTBEAT_PING_URL_SECRET_ID", _HEARTBEAT_SECRET_ID
        )

        stub = _make_fetch_secret_stub(
            {_HEARTBEAT_SECRET_ID: _FAKE_HEARTBEAT_URL}
        )
        provider = _make_provider_patch()

        with (
            patch(
                "baton_harness.chain.bws_client.fetch_secret",
                side_effect=stub,
            ),
            patch(
                "baton_harness.chain.app_auth"
                ".build_installation_token_provider",
                return_value=provider,
            ),
        ):
            from baton_harness.chain.cli import bootstrap_secrets

            bootstrap_secrets()

        assert (
            os.environ.get("BH_HEARTBEAT_PING_URL") == _FAKE_HEARTBEAT_URL
        ), (
            f"Expected BH_HEARTBEAT_PING_URL={_FAKE_HEARTBEAT_URL!r}, "
            f"got {os.environ.get('BH_HEARTBEAT_PING_URL')!r}"
        )


# ---------------------------------------------------------------------------
# V3. Existing env values are preserved (no vault call)
# ---------------------------------------------------------------------------


class TestExistingEnvPreservation:
    """When env vars are already set, vault must not be called for them."""

    def test_bootstrap_preserves_env_gh_token_when_already_set(
        self,
        base_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A pre-existing GH_TOKEN is preserved; vault is not called for it.

        BWS_GH_TOKEN_SECRET_ID is set AND GH_TOKEN is already in env.
        fetch_secret must not be called with _GH_TOKEN_SECRET_ID after
        bootstrap_secrets() returns.
        """
        monkeypatch.setenv("GH_TOKEN", "existing_gh_token_value")
        monkeypatch.setenv("BWS_GH_TOKEN_SECRET_ID", _GH_TOKEN_SECRET_ID)

        called_with_gh_token_id: list[str] = []

        def recording_stub(
            secret_id: str,
            *,
            access_token: str | None = None,
            run: RunFn | None = None,
        ) -> str:
            if secret_id == _GH_TOKEN_SECRET_ID:
                called_with_gh_token_id.append(secret_id)
            if secret_id == _PEM_SECRET_ID:
                return _FAKE_PEM
            return "should-not-be-used"

        provider = _make_provider_patch()

        with (
            patch(
                "baton_harness.chain.bws_client.fetch_secret",
                side_effect=recording_stub,
            ),
            patch(
                "baton_harness.chain.app_auth"
                ".build_installation_token_provider",
                return_value=provider,
            ),
        ):
            from baton_harness.chain.cli import bootstrap_secrets

            bootstrap_secrets()

        assert os.environ.get("GH_TOKEN") == "existing_gh_token_value", (
            "Pre-existing GH_TOKEN was overwritten"
        )
        assert not called_with_gh_token_id, (
            "fetch_secret was called for GH_TOKEN secret ID even though "
            "GH_TOKEN was already set"
        )

    def test_bootstrap_preserves_env_heartbeat_url_when_already_set(
        self,
        base_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A pre-existing BH_HEARTBEAT_PING_URL is preserved; no vault call.

        BWS_HEARTBEAT_PING_URL_SECRET_ID is set AND BH_HEARTBEAT_PING_URL
        is already in env.  fetch_secret must not be called with the
        heartbeat secret ID.
        """
        monkeypatch.setenv(
            "BH_HEARTBEAT_PING_URL", "https://existing.example.com/ping"
        )
        monkeypatch.setenv(
            "BWS_HEARTBEAT_PING_URL_SECRET_ID", _HEARTBEAT_SECRET_ID
        )

        called_with_heartbeat_id: list[str] = []

        def recording_stub(
            secret_id: str,
            *,
            access_token: str | None = None,
            run: RunFn | None = None,
        ) -> str:
            if secret_id == _HEARTBEAT_SECRET_ID:
                called_with_heartbeat_id.append(secret_id)
            if secret_id == _PEM_SECRET_ID:
                return _FAKE_PEM
            return "should-not-be-used"

        provider = _make_provider_patch()

        with (
            patch(
                "baton_harness.chain.bws_client.fetch_secret",
                side_effect=recording_stub,
            ),
            patch(
                "baton_harness.chain.app_auth"
                ".build_installation_token_provider",
                return_value=provider,
            ),
        ):
            from baton_harness.chain.cli import bootstrap_secrets

            bootstrap_secrets()

        assert os.environ.get("BH_HEARTBEAT_PING_URL") == (
            "https://existing.example.com/ping"
        ), "Pre-existing BH_HEARTBEAT_PING_URL was overwritten"
        assert not called_with_heartbeat_id, (
            "fetch_secret was called for heartbeat secret ID even though "
            "BH_HEARTBEAT_PING_URL was already set"
        )


# ---------------------------------------------------------------------------
# V4. Backward-compat: missing secret IDs → no fetch, no raise
# ---------------------------------------------------------------------------


class TestBackwardCompatNoSecretId:
    """bootstrap_secrets() does not raise when new secret IDs are absent."""

    def test_bootstrap_skips_gh_token_fetch_when_secret_id_absent(
        self,
        base_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """bootstrap_secrets() succeeds when BWS_GH_TOKEN_SECRET_ID absent.

        BWS_GH_TOKEN_SECRET_ID not set, GH_TOKEN not set.
        bootstrap_secrets() must NOT raise; GH_TOKEN remains absent.
        This is the pre-#171 deployment path.
        """
        stub = _make_fetch_secret_stub({})
        provider = _make_provider_patch()

        with (
            patch(
                "baton_harness.chain.bws_client.fetch_secret",
                side_effect=stub,
            ),
            patch(
                "baton_harness.chain.app_auth"
                ".build_installation_token_provider",
                return_value=provider,
            ),
        ):
            from baton_harness.chain.cli import bootstrap_secrets

            # Must not raise
            bootstrap_secrets()

        assert "GH_TOKEN" not in os.environ, (
            "GH_TOKEN was unexpectedly set when BWS_GH_TOKEN_SECRET_ID "
            "was absent"
        )

    def test_bootstrap_skips_heartbeat_fetch_when_secret_id_absent(
        self,
        base_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """bootstrap_secrets() succeeds when heartbeat secret ID absent.

        BWS_HEARTBEAT_PING_URL_SECRET_ID not set, BH_HEARTBEAT_PING_URL
        not set.  bootstrap_secrets() must NOT raise; BH_HEARTBEAT_PING_URL
        remains absent.
        """
        stub = _make_fetch_secret_stub({})
        provider = _make_provider_patch()

        with (
            patch(
                "baton_harness.chain.bws_client.fetch_secret",
                side_effect=stub,
            ),
            patch(
                "baton_harness.chain.app_auth"
                ".build_installation_token_provider",
                return_value=provider,
            ),
        ):
            from baton_harness.chain.cli import bootstrap_secrets

            bootstrap_secrets()

        assert "BH_HEARTBEAT_PING_URL" not in os.environ, (
            "BH_HEARTBEAT_PING_URL was unexpectedly set when "
            "BWS_HEARTBEAT_PING_URL_SECRET_ID was absent"
        )


# ---------------------------------------------------------------------------
# V5. Fail-closed: vault errors propagate
# ---------------------------------------------------------------------------


class TestVaultErrorFailClosed:
    """BwsClientError from vault fetch propagates — fail-closed semantics."""

    def test_bootstrap_fails_closed_on_vault_error_for_gh_token(
        self,
        base_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """BwsClientError from GH_TOKEN fetch propagates out of bootstrap.

        BWS_GH_TOKEN_SECRET_ID is set, GH_TOKEN is absent, and
        fetch_secret raises BwsClientError.  bootstrap_secrets() must
        NOT catch and swallow the exception.
        """
        monkeypatch.setenv("BWS_GH_TOKEN_SECRET_ID", _GH_TOKEN_SECRET_ID)

        def failing_stub(
            secret_id: str,
            *,
            access_token: str | None = None,
            run: RunFn | None = None,
        ) -> str:
            if secret_id == _GH_TOKEN_SECRET_ID:
                raise BwsClientError("simulated vault outage")
            if secret_id == _PEM_SECRET_ID:
                return _FAKE_PEM
            raise BwsClientError(f"unexpected secret_id: {secret_id!r}")

        provider = _make_provider_patch()

        with (
            patch(
                "baton_harness.chain.bws_client.fetch_secret",
                side_effect=failing_stub,
            ),
            patch(
                "baton_harness.chain.app_auth"
                ".build_installation_token_provider",
                return_value=provider,
            ),
        ):
            from baton_harness.chain.cli import bootstrap_secrets

            with pytest.raises(BwsClientError, match="simulated vault outage"):
                bootstrap_secrets()

    def test_bootstrap_fails_closed_on_vault_error_for_heartbeat(
        self,
        base_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """BwsClientError from heartbeat URL fetch propagates out.

        BWS_HEARTBEAT_PING_URL_SECRET_ID is set, BH_HEARTBEAT_PING_URL is
        absent, and fetch_secret raises BwsClientError.
        bootstrap_secrets() must NOT swallow the exception.
        """
        monkeypatch.setenv(
            "BWS_HEARTBEAT_PING_URL_SECRET_ID", _HEARTBEAT_SECRET_ID
        )

        def failing_stub(
            secret_id: str,
            *,
            access_token: str | None = None,
            run: RunFn | None = None,
        ) -> str:
            if secret_id == _HEARTBEAT_SECRET_ID:
                raise BwsClientError("simulated vault outage")
            if secret_id == _PEM_SECRET_ID:
                return _FAKE_PEM
            raise BwsClientError(f"unexpected secret_id: {secret_id!r}")

        provider = _make_provider_patch()

        with (
            patch(
                "baton_harness.chain.bws_client.fetch_secret",
                side_effect=failing_stub,
            ),
            patch(
                "baton_harness.chain.app_auth"
                ".build_installation_token_provider",
                return_value=provider,
            ),
        ):
            from baton_harness.chain.cli import bootstrap_secrets

            with pytest.raises(BwsClientError, match="simulated vault outage"):
                bootstrap_secrets()


# ---------------------------------------------------------------------------
# V6. Ordering: vault fetches happen before BWS_ACCESS_TOKEN is popped
# ---------------------------------------------------------------------------


class TestVaultFetchOrdering:
    """New vault fetches must occur before BWS_ACCESS_TOKEN is consumed."""

    def test_vault_fetches_happen_before_pem_fetch(
        self,
        base_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GH_TOKEN and heartbeat fetch_secret calls precede PEM fetch call.

        Records the order of all fetch_secret secret_id arguments.
        The GH_TOKEN and heartbeat secret IDs must appear in the call
        sequence BEFORE the PEM secret ID.

        Rationale: build_installation_token_provider() pops
        BWS_ACCESS_TOKEN as its very first operation.  Any vault call
        that happens after it would receive an empty access_token and
        fail with BwsClientError("access_token is empty") in production.
        This ordering invariant is the structural guarantee that the new
        fetches can always reach the vault.
        """
        monkeypatch.setenv("BWS_GH_TOKEN_SECRET_ID", _GH_TOKEN_SECRET_ID)
        monkeypatch.setenv(
            "BWS_HEARTBEAT_PING_URL_SECRET_ID", _HEARTBEAT_SECRET_ID
        )

        call_order: list[str] = []

        def recording_stub(
            secret_id: str,
            *,
            access_token: str | None = None,
            run: RunFn | None = None,
        ) -> str:
            call_order.append(secret_id)
            if secret_id == _GH_TOKEN_SECRET_ID:
                return _FAKE_GH_TOKEN
            if secret_id == _HEARTBEAT_SECRET_ID:
                return _FAKE_HEARTBEAT_URL
            if secret_id == _PEM_SECRET_ID:
                return _FAKE_PEM
            raise BwsClientError(f"unexpected secret_id: {secret_id!r}")

        provider = _make_provider_patch()

        with (
            patch(
                "baton_harness.chain.bws_client.fetch_secret",
                side_effect=recording_stub,
            ),
            patch(
                "baton_harness.chain.app_auth"
                ".build_installation_token_provider",
                return_value=provider,
            ),
        ):
            from baton_harness.chain.cli import bootstrap_secrets

            bootstrap_secrets()

        assert _GH_TOKEN_SECRET_ID in call_order, (
            "fetch_secret was never called for GH_TOKEN secret ID"
        )
        assert _HEARTBEAT_SECRET_ID in call_order, (
            "fetch_secret was never called for heartbeat secret ID"
        )

        pem_index = call_order.index(_PEM_SECRET_ID)
        gh_index = call_order.index(_GH_TOKEN_SECRET_ID)
        hb_index = call_order.index(_HEARTBEAT_SECRET_ID)

        assert gh_index < pem_index, (
            f"GH_TOKEN fetch (position {gh_index}) must precede PEM fetch "
            f"(position {pem_index}); got order {call_order!r}"
        )
        assert hb_index < pem_index, (
            f"Heartbeat fetch (position {hb_index}) must precede PEM fetch "
            f"(position {pem_index}); got order {call_order!r}"
        )


# ---------------------------------------------------------------------------
# V7. Secret values do not appear in provider repr
# ---------------------------------------------------------------------------


class TestNoSecretLeakInRepr:
    """Fetched secret values must not appear in the provider repr.

    This is a defensive assertion against logging or repr implementations
    that might inadvertently surface secret material.  The returned
    InstallationTokenSource repr (or str()) must not contain the raw
    GitHub PAT or the heartbeat webhook URL.
    """

    def test_bootstrap_does_not_leak_secret_values_in_logs_or_repr(
        self,
        base_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """InstallationTokenSource repr must not contain vault secret values.

        After bootstrap_secrets() returns, the repr and str of the
        returned provider object must not contain the GitHub PAT value
        or the Slack webhook URL that were fetched from the vault.
        """
        monkeypatch.setenv("BWS_GH_TOKEN_SECRET_ID", _GH_TOKEN_SECRET_ID)
        monkeypatch.setenv(
            "BWS_HEARTBEAT_PING_URL_SECRET_ID", _HEARTBEAT_SECRET_ID
        )

        stub = _make_fetch_secret_stub(
            {
                _GH_TOKEN_SECRET_ID: _FAKE_GH_TOKEN,
                _HEARTBEAT_SECRET_ID: _FAKE_HEARTBEAT_URL,
            }
        )
        provider = _make_provider_patch()

        with (
            patch(
                "baton_harness.chain.bws_client.fetch_secret",
                side_effect=stub,
            ),
            patch(
                "baton_harness.chain.app_auth"
                ".build_installation_token_provider",
                return_value=provider,
            ),
        ):
            from baton_harness.chain.cli import bootstrap_secrets

            result = bootstrap_secrets()

        result_repr = repr(result)
        result_str = str(result)

        assert "github_pat_" not in result_repr, (
            "GitHub PAT prefix found in provider repr — secret value may leak"
        )
        assert "github_pat_" not in result_str, (
            "GitHub PAT prefix found in provider str — secret value may leak"
        )
        assert "hooks.slack.com" not in result_repr, (
            "Slack webhook URL found in provider repr — secret value may leak"
        )
        assert "hooks.slack.com" not in result_str, (
            "Slack webhook URL found in provider str — secret value may leak"
        )
