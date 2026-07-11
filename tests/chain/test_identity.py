"""Tests for baton_harness.chain.identity — subprocess auth-identity broker.

Coverage:
- ``Identity`` is a typed enum exposing ``APP`` and ``WORKER`` members.
- ``env_for(Identity.APP, installation_token=...)`` returns a dict with
  ``GH_TOKEN`` and ``GITHUB_TOKEN`` set to the resolved installation
  token value.
- ``env_for(Identity.APP, ...)`` also carries ``GH_INSTALLATION_TOKEN``
  (the key ``daemon._authed_git_push`` reads for its git-credential
  helper) — the broker's env is a single superset dict usable by every
  APP-identity spawn class (daemon-side push, label edit, CI read), not
  a separate "push mode".
- ``env_for(Identity.APP, ...)`` resolves both a literal token string
  and a refreshable ``get_token()`` provider object.
- ``env_for(Identity.APP, ...)`` overrides a stale ambient ``GH_TOKEN``
  already present in ``os.environ`` with the freshly resolved token.
- ``env_for(Identity.APP, ...)`` passes through ambient non-credential
  env vars (e.g. ``PATH``-like entries) unchanged.
- ``env_for(Identity.APP, ...)`` never mutates the real ``os.environ``
  and the token value never appears in it afterwards.
- ``env_for(Identity.APP)`` without an ``installation_token`` raises
  ``ValueError`` — the APP identity must never silently spawn with an
  empty/missing credential.
- ``env_for(Identity.WORKER, ...)`` never carries ``GH_TOKEN``,
  ``GITHUB_TOKEN``, or ``GH_INSTALLATION_TOKEN`` — even if an
  ``installation_token`` argument is (mis)supplied, even if the ambient
  ``os.environ`` already has a real ``GH_TOKEN`` set, and even if the
  token value appears under some other key — the worker identity is
  deliberately denied privileged GitHub creds (daemon-side push/label
  edit/CI-read vs. worker-side push/PR-create is exactly this
  boundary).
- ``env_for(Identity.WORKER, ...)`` still passes through ambient
  non-credential env vars so the worker subprocess can actually run.
- ``env_for(Identity.WORKER, ...)`` never mutates the real
  ``os.environ``.
- ``env_for(Identity.WORKER, installation_token="")`` must NOT strip
  an unrelated ambient env var whose value is also the empty string —
  an empty ``installation_token`` must be treated as "no token
  supplied", never as a real (blank) credential value that then
  triggers value-based filtering of every empty-valued env var.
- ``env_for(Identity.APP, installation_token="")`` must raise
  ``ValueError`` — an empty string is not a valid App installation
  credential, exactly like ``None``.

Contract decisions pinned by these tests (see PR/return notes):
- ``env_for`` returns one superset dict per identity; there is no
  separate "push" vs. "label-edit" vs. "CI-read" env shape for APP —
  callers needing only ``GH_TOKEN``/``GITHUB_TOKEN`` simply ignore the
  extra ``GH_INSTALLATION_TOKEN`` key.
- Missing ``installation_token`` for ``Identity.APP`` is a hard
  ``ValueError``, not a silent empty-string credential.
- ``Identity.WORKER`` denial is unconditional: it does not matter
  whether the caller passed a token or whether one leaked into ambient
  ``os.environ`` — the returned dict never carries the privileged keys.
- An empty-string ``installation_token`` is equivalent to ``None`` for
  purposes of "was a token supplied": it must never be treated as a
  real (blank) credential value that collateral-damages unrelated
  empty-valued env vars, and it must still trip the ``Identity.APP``
  missing-credential ``ValueError``.
"""

from __future__ import annotations

import enum
import os

import pytest

from baton_harness.chain.identity import Identity, env_for

_APP_TOKEN = "ghs_FAKEFAKEFAKEFAKEFAKEFAKEFAKE"
_STALE_TOKEN = "ghs_STALE_AMBIENT_TOKEN_00000000"
_AMBIENT_VAR = "BH_IDENTITY_TEST_AMBIENT_VAR"
_AMBIENT_VALUE = "ambient-value-untouched"

_PRIVILEGED_KEYS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_INSTALLATION_TOKEN")


class _FakeTokenSource:
    """Test double for a refreshable installation-token source."""

    def __init__(self, token: str) -> None:
        """Store the token this fake will always resolve to.

        Args:
            token: The token string ``get_token`` will return.
        """
        self._token = token

    def get_token(self) -> str:
        """Return the configured token string."""
        return self._token


# ---------------------------------------------------------------------------
# Identity enum
# ---------------------------------------------------------------------------


class TestIdentityEnum:
    """``Identity`` is a typed enum with exactly APP and WORKER members."""

    def test_identity_is_an_enum(self) -> None:
        """``Identity`` must subclass ``enum.Enum``."""
        assert issubclass(Identity, enum.Enum)

    def test_app_and_worker_members_exist(self) -> None:
        """Both ``Identity.APP`` and ``Identity.WORKER`` must resolve."""
        assert isinstance(Identity.APP, Identity)
        assert isinstance(Identity.WORKER, Identity)

    def test_app_and_worker_are_distinct(self) -> None:
        """``APP`` and ``WORKER`` must be distinct enum members."""
        assert Identity.APP is not Identity.WORKER


# ---------------------------------------------------------------------------
# env_for(Identity.APP, ...)
# ---------------------------------------------------------------------------


class TestEnvForApp:
    """``env_for(Identity.APP, ...)`` builds the privileged spawn env."""

    def test_sets_gh_token_to_resolved_value(self) -> None:
        """``GH_TOKEN`` must equal the resolved installation token."""
        env = env_for(Identity.APP, installation_token=_APP_TOKEN)

        assert env["GH_TOKEN"] == _APP_TOKEN

    def test_sets_github_token_to_resolved_value(self) -> None:
        """``GITHUB_TOKEN`` must equal the resolved installation token."""
        env = env_for(Identity.APP, installation_token=_APP_TOKEN)

        assert env["GITHUB_TOKEN"] == _APP_TOKEN

    def test_sets_gh_installation_token_for_push_credential_helper(
        self,
    ) -> None:
        """``GH_INSTALLATION_TOKEN`` must carry the token too.

        This is the key ``daemon._authed_git_push``'s inline
        credential-helper reads (``echo "password=$GH_INSTALLATION_TOKEN"``)
        — the broker's APP env must satisfy the push spawn class without
        a second, push-specific call.
        """
        env = env_for(Identity.APP, installation_token=_APP_TOKEN)

        assert env["GH_INSTALLATION_TOKEN"] == _APP_TOKEN

    def test_resolves_token_from_get_token_callable(self) -> None:
        """Resolve a refreshable ``get_token()`` source.

        Its return value, not the object itself, must appear in env.
        """
        source = _FakeTokenSource(_APP_TOKEN)

        env = env_for(Identity.APP, installation_token=source)

        assert env["GH_TOKEN"] == _APP_TOKEN
        assert env["GITHUB_TOKEN"] == _APP_TOKEN
        assert env["GH_INSTALLATION_TOKEN"] == _APP_TOKEN

    def test_overrides_stale_ambient_gh_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A stale ambient ``GH_TOKEN`` must be overridden, not kept."""
        monkeypatch.setenv("GH_TOKEN", _STALE_TOKEN)

        env = env_for(Identity.APP, installation_token=_APP_TOKEN)

        assert env["GH_TOKEN"] == _APP_TOKEN
        assert env["GH_TOKEN"] != _STALE_TOKEN

    def test_passes_through_ambient_non_credential_env_vars(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Ambient non-credential vars (e.g. ``PATH``) must survive."""
        monkeypatch.setenv(_AMBIENT_VAR, _AMBIENT_VALUE)

        env = env_for(Identity.APP, installation_token=_APP_TOKEN)

        assert env.get(_AMBIENT_VAR) == _AMBIENT_VALUE

    def test_does_not_mutate_os_environ(self) -> None:
        """The real ``os.environ`` must be unchanged after the call.

        ``env_for`` must build a fresh overlay dict, never write
        through to the real process environment.
        """
        before = dict(os.environ)

        env_for(Identity.APP, installation_token=_APP_TOKEN)

        assert dict(os.environ) == before

    def test_token_value_absent_from_os_environ_after_call(self) -> None:
        """The resolved token must never land in real ``os.environ``."""
        unique_token = "ghs_UNIQUE_APP_SENTINEL_00000001"

        env_for(Identity.APP, installation_token=unique_token)

        assert unique_token not in os.environ.values()

    def test_raises_when_installation_token_missing(self) -> None:
        """A missing ``installation_token`` for APP must raise.

        The APP identity must never silently spawn with an
        empty/missing credential.
        """
        with pytest.raises(ValueError):
            env_for(Identity.APP)

    def test_raises_when_installation_token_is_empty_string(self) -> None:
        """An empty-string ``installation_token`` for APP must raise.

        Regression for a production bug: an empty string passes an
        ``is not None`` check and is silently accepted as a "real"
        (blank) credential. It must be treated the same as a missing
        token, not as a valid-but-empty one.
        """
        with pytest.raises(ValueError):
            env_for(Identity.APP, installation_token="")


# ---------------------------------------------------------------------------
# env_for(Identity.WORKER, ...)
# ---------------------------------------------------------------------------


class TestEnvForWorker:
    """``env_for(Identity.WORKER, ...)`` denies privileged GitHub creds."""

    def test_no_gh_token_key_present(self) -> None:
        """``GH_TOKEN`` must be absent from the worker env."""
        env = env_for(Identity.WORKER)

        assert "GH_TOKEN" not in env

    def test_no_github_token_key_present(self) -> None:
        """``GITHUB_TOKEN`` must be absent from the worker env."""
        env = env_for(Identity.WORKER)

        assert "GITHUB_TOKEN" not in env

    def test_no_gh_installation_token_key_present(self) -> None:
        """``GH_INSTALLATION_TOKEN`` must be absent from worker env.

        Confirms the push-credential key is APP-only.
        """
        env = env_for(Identity.WORKER)

        assert "GH_INSTALLATION_TOKEN" not in env

    def test_denies_even_when_installation_token_argument_supplied(
        self,
    ) -> None:
        """Passing a token to the WORKER identity must not leak it.

        The denial is unconditional: a caller mistakenly forwarding an
        installation token to a WORKER-identity spawn must not result
        in the worker subprocess receiving privileged creds.
        """
        env = env_for(Identity.WORKER, installation_token=_APP_TOKEN)

        for key in _PRIVILEGED_KEYS:
            assert key not in env, (
                f"WORKER env must not carry {key!r} even when an "
                f"installation_token argument is supplied; env={env}"
            )
        assert _APP_TOKEN not in env.values()

    def test_ambient_gh_token_not_inherited(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A real ambient ``GH_TOKEN`` must not reach the worker env."""
        monkeypatch.setenv("GH_TOKEN", _STALE_TOKEN)
        monkeypatch.setenv("GITHUB_TOKEN", _STALE_TOKEN)

        env = env_for(Identity.WORKER)

        assert "GH_TOKEN" not in env
        assert "GITHUB_TOKEN" not in env

    def test_token_value_not_present_anywhere_in_env_values(self) -> None:
        """The token value must not appear under any key.

        Not just the canonical ``GH_TOKEN``/``GITHUB_TOKEN``/
        ``GH_INSTALLATION_TOKEN`` keys — anywhere in the worker env.
        """
        unique_token = "ghs_UNIQUE_WORKER_SENTINEL_0000002"

        env = env_for(Identity.WORKER, installation_token=unique_token)

        assert unique_token not in env.values(), (
            "Installation token value must not appear anywhere in the "
            f"WORKER env; found it in env={env}"
        )

    def test_passes_through_ambient_non_credential_env_vars(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Ambient non-credential vars must still reach the worker.

        The worker subprocess needs ``PATH``/``HOME``/etc. to run at
        all — denial only targets the privileged GitHub-auth keys.
        """
        monkeypatch.setenv(_AMBIENT_VAR, _AMBIENT_VALUE)

        env = env_for(Identity.WORKER)

        assert env.get(_AMBIENT_VAR) == _AMBIENT_VALUE

    def test_does_not_mutate_os_environ(self) -> None:
        """The real ``os.environ`` must be unchanged after the call.

        ``env_for`` must build a fresh overlay dict, never write
        through to the real process environment.
        """
        before = dict(os.environ)

        env_for(Identity.WORKER, installation_token=_APP_TOKEN)

        assert dict(os.environ) == before

    def test_empty_installation_token_does_not_strip_empty_ambient_vars(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An empty ``installation_token`` must not collateral-damage.

        Regression for a production bug: ``installation_token=""``
        passes an ``is not None`` check, becomes the "resolved" token,
        and a naive ``value != ""`` env-value filter then strips
        *every* ambient env var whose value happens to also be the
        empty string — unrelated to credential leakage. An empty
        token must be treated as "no token supplied", so unrelated
        empty-valued env vars must survive untouched.
        """
        monkeypatch.setenv(_AMBIENT_VAR, "")

        env = env_for(Identity.WORKER, installation_token="")

        assert _AMBIENT_VAR in env
        assert env[_AMBIENT_VAR] == ""
