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
- Transient errors (429, 502–504, network failures) are retried up to
  ``_MAX_RETRIES`` times before raising with a transient-specific message.
- Permanent errors (401, bad credentials) raise immediately without retry.
- Exception messages never contain raw ``gh`` stderr payloads.

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


# ---------------------------------------------------------------------------
# Transient vs permanent distinction (review finding: Critical)
# ---------------------------------------------------------------------------


_TRANSIENT_STDERRS = [
    "error: HTTP 429: Too Many Requests",
    "error: HTTP 503 Service Unavailable",
    "connection timed out",
    "connection refused",
    "502 Bad Gateway",
    "504 Gateway Timeout",
    "could not resolve host: api.github.com",
    "TLS handshake timeout",
    "unexpected EOF",
]

_PERMANENT_STDERRS = [
    "HTTP 401: Unauthorized",
    "HTTP 401 Bad credentials",
    "error: HTTP 403 Forbidden",
]


class TestTransientRetry:
    """Transient errors are retried; permanent errors are not."""

    @pytest.mark.parametrize("transient_stderr", _TRANSIENT_STDERRS)
    def test_transient_stderr_raises_after_retries(
        self,
        transient_stderr: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Transient ``gh`` stderr causes retries then a transient message.

        After exhausting ``_MAX_RETRIES`` retries, raises
        ``TokenValidationError`` whose message indicates a transient /
        network condition — NOT the permanent "token expired" wording.
        """
        monkeypatch.setenv("GH_TOKEN", _VALID_TOKEN)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        sleep_calls: list[float] = []

        def no_sleep(secs: float) -> None:
            sleep_calls.append(secs)

        with patch.object(
            auth_mod,
            "_run",
            return_value=_completed(
                stdout="",
                returncode=1,
                stderr=transient_stderr,
            ),
        ):
            with pytest.raises(TokenValidationError) as exc_info:
                validate_github_token(sleep_fn=no_sleep)

        msg = exc_info.value.message
        # Message must indicate transient/network nature.
        assert any(
            word in msg.lower()
            for word in ("transient", "network", "github api condition")
        ), f"Expected transient-condition wording, got: {msg!r}"
        # Message must NOT use the permanent "token may be expired/revoked"
        # wording.
        assert "expired" not in msg.lower(), (
            f"Transient message must not say 'expired': {msg!r}"
        )
        assert "revoked" not in msg.lower(), (
            f"Transient message must not say 'revoked': {msg!r}"
        )

    @pytest.mark.parametrize("transient_stderr", _TRANSIENT_STDERRS)
    def test_transient_retried_max_retries_times(
        self,
        transient_stderr: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Self-test is retried exactly ``_MAX_RETRIES`` times on transient.

        Total ``_run`` calls = 1 (initial) + ``_MAX_RETRIES`` (retries).
        Sleep is called ``_MAX_RETRIES`` times — once between each pair.
        """
        monkeypatch.setenv("GH_TOKEN", _VALID_TOKEN)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        sleep_calls: list[float] = []

        def no_sleep(secs: float) -> None:
            sleep_calls.append(secs)

        run_calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            run_calls.append(cmd)
            return _completed(
                stdout="",
                returncode=1,
                stderr=transient_stderr,
            )

        with patch.object(auth_mod, "_run", fake_run):
            with pytest.raises(TokenValidationError):
                validate_github_token(sleep_fn=no_sleep)

        # Count only the gh api user calls (not token-type gate).
        api_calls = [c for c in run_calls if "gh" in c and "api" in c]
        expected_calls = 1 + auth_mod._MAX_RETRIES
        assert len(api_calls) == expected_calls, (
            f"Expected {expected_calls} gh api user calls "
            f"(1 initial + {auth_mod._MAX_RETRIES} retries), "
            f"got {len(api_calls)}"
        )
        assert len(sleep_calls) == auth_mod._MAX_RETRIES, (
            f"Expected {auth_mod._MAX_RETRIES} sleep calls, "
            f"got {len(sleep_calls)}"
        )

    @pytest.mark.parametrize("permanent_stderr", _PERMANENT_STDERRS)
    def test_permanent_stderr_raises_immediately(
        self,
        permanent_stderr: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Permanent errors raise immediately with no retries.

        Only one ``_run`` call (the initial self-test) should be made.
        Sleep must never be called.
        """
        monkeypatch.setenv("GH_TOKEN", _VALID_TOKEN)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        sleep_calls: list[float] = []

        def no_sleep(secs: float) -> None:
            sleep_calls.append(secs)

        run_calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            run_calls.append(cmd)
            return _completed(
                stdout="",
                returncode=1,
                stderr=permanent_stderr,
            )

        with patch.object(auth_mod, "_run", fake_run):
            with pytest.raises(TokenValidationError) as exc_info:
                validate_github_token(sleep_fn=no_sleep)

        api_calls = [c for c in run_calls if "gh" in c and "api" in c]
        assert len(api_calls) == 1, (
            f"Permanent error must not be retried — expected 1 gh api "
            f"call, got {len(api_calls)}"
        )
        assert sleep_calls == [], (
            f"Sleep must not be called for permanent errors, got {sleep_calls}"
        )
        # Message must mention capability failure or token problem,
        # not the transient wording.
        msg = exc_info.value.message
        assert "transient" not in msg.lower(), (
            f"Permanent error message must not say 'transient': {msg!r}"
        )

    @pytest.mark.parametrize("permanent_stderr", _PERMANENT_STDERRS)
    def test_permanent_message_is_permanent_wording(
        self,
        permanent_stderr: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Permanent errors produce the 'expired/revoked/permissions' message.

        The token-expired wording gives the operator an actionable hint
        that the token itself (not a transient GitHub outage) is the
        problem.
        """
        monkeypatch.setenv("GH_TOKEN", _VALID_TOKEN)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        with patch.object(
            auth_mod,
            "_run",
            return_value=_completed(
                stdout="",
                returncode=1,
                stderr=permanent_stderr,
            ),
        ):
            with pytest.raises(TokenValidationError) as exc_info:
                validate_github_token(sleep_fn=lambda _: None)

        msg = exc_info.value.message
        assert any(
            word in msg.lower()
            for word in ("expired", "revoked", "permissions")
        ), f"Permanent message must mention token state, got: {msg!r}"


# ---------------------------------------------------------------------------
# Credential hygiene (review finding: Warning)
# ---------------------------------------------------------------------------


class TestCredentialHygiene:
    """Raw gh stderr must never appear verbatim in the exception message."""

    def test_transient_message_does_not_contain_raw_stderr(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sentinel injected into transient stderr must not appear in message.

        If the exception message leaks the raw stderr string, any secret
        material in that output (auth headers, tokens in error responses)
        could end up in structured logs or tracebacks.
        """
        monkeypatch.setenv("GH_TOKEN", _VALID_TOKEN)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        sentinel = "SENTINEL_SECRET_DO_NOT_LEAK_xyzzy1234"
        stderr_with_sentinel = f"429 Too Many Requests — {sentinel}"

        with patch.object(
            auth_mod,
            "_run",
            return_value=_completed(
                stdout="",
                returncode=1,
                stderr=stderr_with_sentinel,
            ),
        ):
            with pytest.raises(TokenValidationError) as exc_info:
                validate_github_token(sleep_fn=lambda _: None)

        msg = str(exc_info.value)
        assert sentinel not in msg, (
            f"Raw stderr sentinel must not appear in the exception "
            f"message. Got: {msg!r}"
        )

    def test_permanent_message_does_not_contain_raw_stderr(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sentinel injected into permanent stderr must not appear in message.

        Permanent failures may include user-identifying information in the
        response body; leaking that into the exception could expose PII.
        """
        monkeypatch.setenv("GH_TOKEN", _VALID_TOKEN)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        sentinel = "SENTINEL_SECRET_DO_NOT_LEAK_xyzzy5678"
        stderr_with_sentinel = f"401 Unauthorized — {sentinel}"

        with patch.object(
            auth_mod,
            "_run",
            return_value=_completed(
                stdout="",
                returncode=1,
                stderr=stderr_with_sentinel,
            ),
        ):
            with pytest.raises(TokenValidationError) as exc_info:
                validate_github_token(sleep_fn=lambda _: None)

        msg = str(exc_info.value)
        assert sentinel not in msg, (
            f"Raw stderr sentinel must not appear in the exception "
            f"message. Got: {msg!r}"
        )
