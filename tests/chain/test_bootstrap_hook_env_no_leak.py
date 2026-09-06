"""Invariant guard for issue #347: no PAT leak into ambient os.environ.

``bootstrap_secrets()`` must never write the vault-fetched GitHub PAT
into the daemon's ambient ``os.environ``.

Background: issue #222 stopped writing the BWS-vault-fetched executor
PAT into ``os.environ["GH_TOKEN"]`` inside ``bootstrap_secrets()``
(``src/baton_harness/chain/cli.py``), holding it only in the
module-global ``_BOOTSTRAPPED_GH_TOKEN``. This broke
``before_run.py``'s ``validate_github_token()`` gate, which reads
``GH_TOKEN``/``GITHUB_TOKEN`` straight from ``os.environ`` with no
vault fallback (issue #347).

The chosen fix (option (b), confirmed via architectural review) threads
the vault-fetched PAT to the ONE hook subprocess that needs it
(``before_run``) via an explicit ``env=`` override on
``Orchestrator.hook_env`` — see
``tests/vendor/test_orchestrator_hook_env.py`` for that half of the
fix. It deliberately does NOT write the PAT back into ``os.environ``:
doing so would revert #222's env-discipline invariant, which
`docs/superpowers/plans/2026-08-05-phase-5a-ci-scenario-smoke-307.md`
acceptance criterion 8 requires ("GH_TOKEN demonstrably absent from the
daemon's environment").

This test is an AUTHORED-GREEN invariant guard, not a red for new
behaviour: ``bootstrap_secrets()`` already satisfies it today (#222
already removed the write). It is included per the #347 investigation's
explicit ask for a test that "fails loudly if someone reintroduces
[the os.environ-write] pattern" — i.e. it would catch a wrong "option
(a)" fix (writing the PAT back into ``os.environ``) that the #347
implementation must not choose. See
``tests/chain/test_cli_bootstrap_vault.py`` for the sibling suite this
mirrors the mocking conventions of.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from unittest.mock import MagicMock, patch

import pytest

from baton_harness.chain.identity import Identity, env_for

# ---------------------------------------------------------------------------
# Type aliases — matches bws_client.RunFn (see test_cli_bootstrap_vault.py)
# ---------------------------------------------------------------------------

RunFn = Callable[..., subprocess.CompletedProcess[str]]
FetchSecretFn = Callable[..., str]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ACCESS_TOKEN = "0.fake-bws-machine-account-token-for-347-tests"
_APP_ID = "99999"
_PEM_SECRET_ID = "11111111-2222-3333-4444-555555555555"
_GH_TOKEN_SECRET_ID = "gh-token-1111-2222-3333-444444444444"
_INSTALLATION_ID = "12345"

_FAKE_GH_TOKEN = "github_pat_TESTVAL347_ABCDEFGHIJKLMNOP"
_FAKE_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEA0000000000000000000000000000000000000000000000==\n"
    "-----END RSA PRIVATE KEY-----\n"
)
_FAKE_TOKEN = "ghs_FAKEFAKEFAKEFAKEFAKEFAKEFAKE"

_DAEMON_ONLY_KEYS = {
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_INSTALLATION_TOKEN",
    "BH_GITHUB_APP_KEY_PROVIDER",
    "BH_GITHUB_APP_PRIVATE_KEY_FILE",
    "BWS_ACCESS_TOKEN",
    "BWS_PEM_SECRET_ID",
    "BWS_APP_ID",
    "BWS_INSTALLATION_ID",
    "BWS_GH_TOKEN_SECRET_ID",
    "BWS_HEARTBEAT_PING_URL_SECRET_ID",
}


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def bws_only_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure a BWS-only auth env with no literal GH_TOKEN present.

    Mirrors ``test_cli_bootstrap_vault.py``'s ``base_env`` fixture, but
    additionally deletes ``GITHUB_TOKEN`` — ``base_env`` only deletes
    ``GH_TOKEN`` — so an ambient CI-runner ``GITHUB_TOKEN`` cannot make
    this assertion fail for an environment reason rather than a code
    reason (see MEMORY.md ``feedback_ambient_environ_leak``).
    """
    monkeypatch.setenv("BWS_ACCESS_TOKEN", _ACCESS_TOKEN)
    monkeypatch.setenv("BH_GITHUB_APP_KEY_PROVIDER", "bws")
    monkeypatch.delenv("BH_GITHUB_APP_PRIVATE_KEY_FILE", raising=False)
    monkeypatch.setenv("BWS_APP_ID", _APP_ID)
    monkeypatch.setenv("BWS_PEM_SECRET_ID", _PEM_SECRET_ID)
    monkeypatch.setenv("BWS_INSTALLATION_ID", _INSTALLATION_ID)
    monkeypatch.setenv("BWS_GH_TOKEN_SECRET_ID", _GH_TOKEN_SECRET_ID)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


def _fetch_secret_stub(
    secret_id: str,
    *,
    access_token: str | None = None,
    run: RunFn | None = None,
) -> str:
    """Resolve the PEM and GH_TOKEN secret IDs used by this test module.

    Args:
        secret_id: The Bitwarden Secrets ID being fetched.
        access_token: Unused; accepted for signature compatibility with
            ``bws_client.fetch_secret``.
        run: Unused; accepted for signature compatibility.

    Returns:
        The fake secret value for a known secret ID.

    Raises:
        AssertionError: If an unexpected secret ID is requested.
    """
    if secret_id == _PEM_SECRET_ID:
        return _FAKE_PEM
    if secret_id == _GH_TOKEN_SECRET_ID:
        return _FAKE_GH_TOKEN
    raise AssertionError(f"unexpected secret_id in stub: {secret_id!r}")


def _provider_mock() -> MagicMock:
    """Return a MagicMock standing in for InstallationTokenProvider."""
    provider = MagicMock()
    provider.get_token.return_value = _FAKE_TOKEN
    return provider


# ---------------------------------------------------------------------------
# Invariant guard
# ---------------------------------------------------------------------------


class TestBootstrapNeverWritesTokenToAmbientEnviron:
    """bootstrap_secrets() must not revert #222's env-discipline invariant."""

    def test_gh_token_and_github_token_absent_from_os_environ_after_bootstrap(
        self,
        bws_only_env: None,
    ) -> None:
        """Neither GH_TOKEN nor GITHUB_TOKEN land in os.environ.

        Authored-green today (#222 already removed the write). Guards
        against a #347 fix that "solves" the before_run auth gap by
        reintroducing the ambient os.environ write instead of threading
        the PAT by value to the Orchestrator's ``hook_env`` (see
        ``tests/vendor/test_orchestrator_hook_env.py``).
        """
        provider = _provider_mock()

        import baton_harness.chain.cli as cli_mod

        with (
            patch(
                "baton_harness.chain.bws_client.fetch_secret",
                side_effect=_fetch_secret_stub,
            ),
            patch(
                "baton_harness.chain.cli.build_installation_token_provider",
                return_value=provider,
            ),
            # bootstrap_secrets() sets the module-global
            # _BOOTSTRAPPED_GH_TOKEN to the fetched PAT as a side
            # effect. Scope that mutation to this test with
            # patch.object so the original module state (empty string)
            # is restored on exit — otherwise a later test that reads
            # _BOOTSTRAPPED_GH_TOKEN could observe this test's fake PAT
            # and become order-dependent.
            patch.object(cli_mod, "_BOOTSTRAPPED_GH_TOKEN", ""),
        ):
            cli_mod.bootstrap_secrets()

        assert "BWS_ACCESS_TOKEN" not in os.environ
        assert _FAKE_TOKEN not in os.environ.values()
        assert "GH_TOKEN" not in os.environ, (
            "GH_TOKEN was written into ambient os.environ by "
            "bootstrap_secrets() — this reverts issue #222's "
            "env-discipline invariant. The #347 fix must thread the "
            "vault-fetched PAT to the before_run hook by value "
            "(Orchestrator.hook_env), never via os.environ."
        )
        assert "GITHUB_TOKEN" not in os.environ, (
            "GITHUB_TOKEN was written into ambient os.environ by "
            "bootstrap_secrets() — this reverts issue #222's "
            "env-discipline invariant."
        )

    def test_daemon_only_auth_values_absent_from_worker_environment(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No daemon-only authentication value reaches a worker spawn."""
        seeded_values = {key: f"sentinel-{key}" for key in _DAEMON_ONLY_KEYS}
        for key, value in seeded_values.items():
            monkeypatch.setenv(key, value)

        worker_env = env_for(Identity.WORKER)

        assert set(seeded_values.values()).isdisjoint(worker_env.values())
