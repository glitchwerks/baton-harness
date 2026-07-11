"""Tests for vault-fetch extensions to bootstrap_secrets() in cli.py.

Coverage (issue #171 / #222):
- GH_TOKEN is fetched from Bitwarden vault when BWS_GH_TOKEN_SECRET_ID is
  set and GH_TOKEN is absent from the environment, but is NOT written
  into ambient ``os.environ``.
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
  build_installation_token_provider() is called (which pops
  BWS_ACCESS_TOKEN), enforcing the ordering constraint.
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
    """GH_TOKEN is fetched by value when absent from env."""

    def test_bootstrap_fetches_gh_token_from_vault_when_env_absent(
        self,
        base_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fetched GH_TOKEN stays out of ambient ``os.environ``.

        BWS_GH_TOKEN_SECRET_ID is set; GH_TOKEN is absent.  After
        bootstrap_secrets() returns, the fetched token must be available
        through the startup-only by-value seam, not written into
        ``os.environ``.
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
            import baton_harness.chain.cli as cli_mod

            cli_mod.bootstrap_secrets()

        assert "GH_TOKEN" not in os.environ, (
            "bootstrap_secrets must not write the fetched GH_TOKEN into "
            "ambient os.environ"
        )
        assert cli_mod._BOOTSTRAPPED_GH_TOKEN == _FAKE_GH_TOKEN, (
            "bootstrap_secrets must retain the fetched GH_TOKEN for "
            "immediate startup validation without storing it in "
            f"os.environ; got {cli_mod._BOOTSTRAPPED_GH_TOKEN!r}"
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

_BUILD_PROVIDER_SENTINEL = "__BUILD_PROVIDER__"


class TestVaultFetchOrdering:
    """New vault fetches must occur before BWS_ACCESS_TOKEN is consumed."""

    def test_vault_fetches_happen_before_build_installation_token_provider(
        self,
        base_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GH_TOKEN and heartbeat fetch_secret calls precede provider build.

        Records the order of all fetch_secret secret_id arguments, then
        appends a sentinel when build_installation_token_provider() is
        called.  The GH_TOKEN and heartbeat secret IDs must appear in
        the call sequence BEFORE the sentinel.

        Rationale: build_installation_token_provider() pops
        BWS_ACCESS_TOKEN as its very first operation.  Any vault call
        that happens after it would receive an empty access_token and
        fail with BwsClientError("access_token is empty") in production.
        Asserting relative to the provider-build call — not relative to
        the PEM fetch that happens *inside* the provider — measures the
        actual protected boundary directly, without requiring
        bootstrap_secrets() to call fetch_secret for the PEM a second
        time just to make the invariant observable.
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
            # The PEM secret ID is tolerated here so that this stub does not
            # break if bootstrap_secrets() currently fetches the PEM before
            # delegating to build_installation_token_provider().  The
            # ordering assertion below measures relative to the provider-
            # build sentinel, not the PEM position, so the PEM fetch is
            # irrelevant to the invariant under test.
            if secret_id == _PEM_SECRET_ID:
                return _FAKE_PEM
            raise BwsClientError(f"unexpected secret_id: {secret_id!r}")

        provider = _make_provider_patch()

        def sentinel_provider_builder(
            *args: object, **kwargs: object
        ) -> MagicMock:
            call_order.append(_BUILD_PROVIDER_SENTINEL)
            return provider

        # Patch the name as bound in cli's namespace, not in app_auth.
        # cli.py does `from baton_harness.chain.app_auth import
        # build_installation_token_provider`, so patching the app_auth
        # module's attribute only intercepts the call when cli is first
        # imported (Python caches the binding at import time).  Patching
        # cli.build_installation_token_provider is the stable target that
        # works whether cli is already imported or not.
        with (
            patch(
                "baton_harness.chain.bws_client.fetch_secret",
                side_effect=recording_stub,
            ),
            patch(
                "baton_harness.chain.cli.build_installation_token_provider",
                side_effect=sentinel_provider_builder,
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
        assert _BUILD_PROVIDER_SENTINEL in call_order, (
            "build_installation_token_provider was never called"
        )

        provider_index = call_order.index(_BUILD_PROVIDER_SENTINEL)
        gh_index = call_order.index(_GH_TOKEN_SECRET_ID)
        hb_index = call_order.index(_HEARTBEAT_SECRET_ID)

        assert gh_index < provider_index, (
            f"GH_TOKEN fetch (position {gh_index}) must precede "
            f"build_installation_token_provider call "
            f"(position {provider_index}); got order {call_order!r}"
        )
        assert hb_index < provider_index, (
            f"Heartbeat fetch (position {hb_index}) must precede "
            f"build_installation_token_provider call "
            f"(position {provider_index}); got order {call_order!r}"
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


# ---------------------------------------------------------------------------
# V8. GH_TOKEN empty-but-present guard must be truthiness-based (#211)
# ---------------------------------------------------------------------------


class TestGhTokenEmptyButPresentGuard:
    """The GH_TOKEN vault-fetch guard must check truthiness, not presence.

    Regression test for #211: ``cli.py``'s guard
    (``if _gh_token_secret_id and "GH_TOKEN" not in os.environ:``) only
    checks whether the key exists in ``os.environ``. When ``GH_TOKEN``
    is present but set to ``""`` (e.g. a systemd unit or shell that
    exports an empty string rather than leaving the var unset), the key
    IS in ``os.environ``, so the vault fetch is skipped and the empty
    value silently wins over the configured vault secret.
    """

    def test_bootstrap_fetches_gh_token_when_env_value_is_empty_string(
        self,
        base_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An empty-but-present GH_TOKEN must still trigger the vault fetch.

        Sets ``GH_TOKEN=""`` and ``BWS_GH_TOKEN_SECRET_ID`` to a fake
        secret ID, stubs ``fetch_secret`` to return a sentinel value for
        that ID, and no-ops ``build_installation_token_provider`` so
        ``bootstrap_secrets()`` can run without touching a real vault or
        PEM. After ``bootstrap_secrets()`` returns, ambient
        ``os.environ["GH_TOKEN"]`` must remain the pre-existing empty
        string, while the fetched sentinel is retained only in the
        startup-only by-value seam — i.e. the fetch WAS attempted
        despite the pre-existing empty value.

        MUST FAIL today: the current presence-only guard sees
        ``"GH_TOKEN" in os.environ`` (even though the value is ``""``)
        and skips the fetch entirely, leaving
        ``os.environ["GH_TOKEN"] == ""``.
        """
        monkeypatch.setenv("GH_TOKEN", "")
        monkeypatch.setenv("BWS_GH_TOKEN_SECRET_ID", _GH_TOKEN_SECRET_ID)

        stub = _make_fetch_secret_stub({_GH_TOKEN_SECRET_ID: "tok-sentinel"})
        provider = _make_provider_patch()

        with (
            patch(
                "baton_harness.chain.bws_client.fetch_secret",
                side_effect=stub,
            ),
            # Patched on cli's own bound name (not app_auth's) so this
            # test is robust regardless of whether an earlier test in
            # this module already imported baton_harness.chain.cli and
            # cached the `from ... import build_installation_token_
            # provider` binding — see the V6 ordering test's comment for
            # why patching app_auth alone is import-order-fragile.
            patch(
                "baton_harness.chain.cli.build_installation_token_provider",
                return_value=provider,
            ),
        ):
            import baton_harness.chain.cli as cli_mod

            cli_mod.bootstrap_secrets()

        assert os.environ.get("GH_TOKEN") == "", (
            "bootstrap_secrets must not overwrite ambient GH_TOKEN even "
            "when it is empty; the fetched value must stay out of "
            f"os.environ, got {os.environ.get('GH_TOKEN')!r}"
        )
        assert cli_mod._BOOTSTRAPPED_GH_TOKEN == "tok-sentinel", (
            "Expected the vault fetch to retain the fetched value in the "
            "startup-only by-value seam; got "
            f"{cli_mod._BOOTSTRAPPED_GH_TOKEN!r}"
        )


# ---------------------------------------------------------------------------
# V9. Boot-time validation that GH_TOKEN resolved non-empty (#212)
# ---------------------------------------------------------------------------


class TestValidateGhTokenBootTimeGuard:
    """Boot-time validation that GH_TOKEN resolved to a non-empty value.

    Regression tests for #212: after ``bootstrap_secrets()`` runs,
    nothing currently asserts that ``GH_TOKEN`` ended up non-empty when
    a vault secret ID was configured for it — the failure only surfaces
    later, opaquely, inside a worker subprocess.

    SEAM CHOICE (documented ambiguity for the router/implementer to
    reconcile): the exact call site for this validation is the
    implementer's choice. Per the briefing, this test targets the most
    stable *public* contract: a standalone helper,
    ``validate_gh_token(token: str, secret_id_configured: bool) -> None``,
    mirroring the shape/naming of the sibling
    ``baton_harness._auth.validate_daemon_token(token: str) -> None``
    pattern already established in ``cli.py``'s startup path (called
    right after ``bootstrap_secrets()`` at ~cli.py:365). Raises when a
    vault secret ID WAS configured but the resolved token is
    empty/whitespace; is a no-op otherwise (preserving the pre-#212
    backward-compat path where an externally-supplied, non-vault-backed
    empty ``GH_TOKEN`` is out of scope for this specific guard).

    This test imports ``validate_gh_token`` from
    ``baton_harness.chain.cli`` — the module that owns GH_TOKEN
    resolution today (``bootstrap_secrets``) and the daemon startup
    sequence that already calls the sibling ``validate_daemon_token``.
    If the implementer instead places the helper in
    ``baton_harness._auth`` (alongside ``validate_daemon_token`` and
    ``TokenValidationError``), only the import path here needs to move;
    the asserted contract (signature + raise/no-raise behavior) is
    unchanged. Flagged for router reconciliation if the implementer
    picks a different name or shape entirely.
    """

    def test_raises_when_secret_id_configured_but_token_empty(
        self,
    ) -> None:
        """Raises a clear, GH_TOKEN-naming error when configured but empty.

        MUST FAIL today: ``validate_gh_token`` does not exist yet — no
        boot-time check of this kind is performed anywhere in the
        codebase (issue #212). Expect an ``ImportError``/
        ``AttributeError`` collection-time failure until the helper is
        added, which is itself valid evidence of the missing behavior.
        """
        from baton_harness.chain.cli import validate_gh_token

        with pytest.raises(Exception, match="GH_TOKEN"):
            validate_gh_token("", secret_id_configured=True)

    def test_raises_when_secret_id_configured_but_token_whitespace(
        self,
    ) -> None:
        """Whitespace-only resolved token is treated as empty.

        MUST FAIL today: ``validate_gh_token`` does not exist yet.
        """
        from baton_harness.chain.cli import validate_gh_token

        with pytest.raises(Exception, match="GH_TOKEN"):
            validate_gh_token("   ", secret_id_configured=True)

    def test_does_not_raise_when_secret_id_not_configured(self) -> None:
        """No-op when no vault secret ID was configured for GH_TOKEN.

        Preserves the pre-#212 backward-compat path: an empty
        ``GH_TOKEN`` is only this helper's concern when a vault fetch
        was expected to populate it.

        MUST FAIL today: ``validate_gh_token`` does not exist yet, so
        the import itself raises.
        """
        from baton_harness.chain.cli import validate_gh_token

        validate_gh_token("", secret_id_configured=False)

    def test_does_not_raise_when_token_non_empty(self) -> None:
        """No-op when the resolved token is non-empty.

        MUST FAIL today: ``validate_gh_token`` does not exist yet, so
        the import itself raises.
        """
        from baton_harness.chain.cli import validate_gh_token

        validate_gh_token(
            "github_pat_TESTVAL_ABCDEFGHIJKLMNOP",
            secret_id_configured=True,
        )
