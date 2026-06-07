"""Unit tests for baton_harness._auth — GitHub PAT validation gate.

Coverage:
- Missing/empty token is rejected with a clear error message.
- Classic PAT (``ghp_`` prefix) is rejected.
- Other non-fine-grained token types (``gho_``, ``ghs_``, ``ghu_``,
  unknown prefix) are rejected.
- Valid fine-grained PAT (``github_pat_`` prefix) with a passing
  capability self-test is accepted.
- Capability self-test failure (non-zero gh exit, bad JSON) is rejected.
- ``GH_TOKEN`` takes precedence over ``GITHUB_TOKEN``.
- ``GITHUB_TOKEN`` is used when ``GH_TOKEN`` is absent.

All ``gh`` subprocess calls are intercepted by patching
``baton_harness._auth._run`` so no real network calls are made.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

import baton_harness._auth as auth_mod
from baton_harness._auth import TokenValidationError, validate_github_token

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_TOKEN = "github_pat_AAAAAAAAAAAAAAAAAAAAAAA_BBBBBBB"
_GH_API_USER_OK = '{"login": "bot-user", "id": 12345}'


def _completed(
    stdout: str = "",
    returncode: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    """Build a fake CompletedProcess for use in mock return values.

    Args:
        stdout: Simulated standard output.
        returncode: Simulated process return code.
        stderr: Simulated standard error.

    Returns:
        A ``subprocess.CompletedProcess`` with the given fields populated.
    """
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


# ---------------------------------------------------------------------------
# Token-type gate: missing / empty
# ---------------------------------------------------------------------------


class TestMissingToken:
    """Token is absent or empty — must be rejected before any gh call."""

    def test_no_env_vars_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Raises ``TokenValidationError`` when neither env var is set."""
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        with pytest.raises(TokenValidationError, match="no token"):
            validate_github_token()

    def test_empty_gh_token_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Raises ``TokenValidationError`` when ``GH_TOKEN`` is empty."""
        monkeypatch.setenv("GH_TOKEN", "")
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        with pytest.raises(TokenValidationError, match="no token"):
            validate_github_token()

    def test_empty_github_token_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Raises when ``GITHUB_TOKEN`` is empty and ``GH_TOKEN`` absent."""
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "")

        with pytest.raises(TokenValidationError, match="no token"):
            validate_github_token()


# ---------------------------------------------------------------------------
# Token-type gate: classic PAT rejected
# ---------------------------------------------------------------------------


class TestClassicPatRejected:
    """Classic PATs (``ghp_`` prefix) must be rejected with a clear message."""

    def test_classic_pat_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Classic PAT is rejected with a message naming the problem."""
        monkeypatch.setenv("GH_TOKEN", "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        with pytest.raises(TokenValidationError, match="classic PAT"):
            validate_github_token()

    def test_classic_pat_error_mentions_fine_grained(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Classic PAT error message mentions fine-grained PAT."""
        monkeypatch.setenv("GH_TOKEN", "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        with pytest.raises(TokenValidationError, match="fine-grained"):
            validate_github_token()


# ---------------------------------------------------------------------------
# Token-type gate: other non-fine-grained types rejected
# ---------------------------------------------------------------------------


class TestOtherTokenTypesRejected:
    """OAuth, server-to-server, and unknown prefixes must all be rejected."""

    @pytest.mark.parametrize(
        "token",
        [
            "gho_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",  # OAuth app token
            "ghs_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",  # GitHub App s2s
            "ghu_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",  # GitHub App u2s
            "v1_someoldstyletoken",  # unknown prefix
            "mysecrettoken",  # no recognised prefix
        ],
    )
    def test_non_fine_grained_token_rejected(
        self,
        token: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-fine-grained token raises ``TokenValidationError``."""
        monkeypatch.setenv("GH_TOKEN", token)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        with pytest.raises(TokenValidationError):
            validate_github_token()


# ---------------------------------------------------------------------------
# Env-var precedence
# ---------------------------------------------------------------------------


class TestEnvVarPrecedence:
    """GH_TOKEN takes precedence over GITHUB_TOKEN."""

    def test_gh_token_preferred_over_github_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``GH_TOKEN`` wins over ``GITHUB_TOKEN`` when both are set.

        A classic token in ``GH_TOKEN`` fails even if ``GITHUB_TOKEN``
        holds a valid fine-grained PAT.
        """
        monkeypatch.setenv("GH_TOKEN", "ghp_classicoverthere")
        monkeypatch.setenv("GITHUB_TOKEN", _VALID_TOKEN)

        # GH_TOKEN is classic → should fail even though GITHUB_TOKEN is valid
        with pytest.raises(TokenValidationError, match="classic PAT"):
            validate_github_token()

    def test_github_token_fallback_when_gh_token_absent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``GITHUB_TOKEN`` is used when ``GH_TOKEN`` is not set."""
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", _VALID_TOKEN)

        with patch.object(
            auth_mod, "_run", return_value=_completed(_GH_API_USER_OK)
        ):
            # Should not raise — fine-grained token + passing self-test
            validate_github_token()


# ---------------------------------------------------------------------------
# Capability self-test: passing
# ---------------------------------------------------------------------------


class TestCapabilitySelfTestPass:
    """Fine-grained PAT + passing self-test is accepted."""

    def test_valid_fine_grained_token_accepted(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fine-grained PAT + successful gh api call → no exception raised."""
        monkeypatch.setenv("GH_TOKEN", _VALID_TOKEN)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        with patch.object(
            auth_mod, "_run", return_value=_completed(_GH_API_USER_OK)
        ):
            validate_github_token()  # must not raise

    def test_gh_api_user_called(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The capability self-test issues a ``gh api user`` call."""
        monkeypatch.setenv("GH_TOKEN", _VALID_TOKEN)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        captured: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            captured.append(cmd)
            return _completed(_GH_API_USER_OK)

        with patch.object(auth_mod, "_run", fake_run):
            validate_github_token()

        assert any("gh" in cmd and "api" in cmd for cmd in captured), (
            "Expected a gh api call in: " + str(captured)
        )


# ---------------------------------------------------------------------------
# Capability self-test: failing
# ---------------------------------------------------------------------------


class TestCapabilitySelfTestFail:
    """Self-test failures raise ``TokenValidationError``."""

    def test_gh_nonzero_exit_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A non-zero exit from the gh capability probe raises the error."""
        monkeypatch.setenv("GH_TOKEN", _VALID_TOKEN)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        with patch.object(
            auth_mod,
            "_run",
            return_value=_completed(
                stdout="",
                returncode=1,
                stderr="HTTP 401: Bad credentials",
            ),
        ):
            with pytest.raises(TokenValidationError, match="capability"):
                validate_github_token()

    def test_gh_empty_output_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Empty stdout from the gh probe raises ``TokenValidationError``.

        ``gh api user --jq .login`` returns a bare login string on
        success.  An empty response (e.g. null login) is treated as a
        capability failure.
        """
        monkeypatch.setenv("GH_TOKEN", _VALID_TOKEN)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        with patch.object(
            auth_mod,
            "_run",
            return_value=_completed(stdout="", returncode=0),
        ):
            with pytest.raises(TokenValidationError, match="capability"):
                validate_github_token()

    def test_gh_json_object_missing_login_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """JSON response missing ``login`` key raises the error."""
        monkeypatch.setenv("GH_TOKEN", _VALID_TOKEN)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        with patch.object(
            auth_mod,
            "_run",
            return_value=_completed(stdout='{"id": 1}', returncode=0),
        ):
            with pytest.raises(TokenValidationError, match="capability"):
                validate_github_token()
