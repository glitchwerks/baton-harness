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
- (C1) ``validate_github_token`` still REJECTS a ``ghs_`` installation
  token — the worker path must remain ``github_pat_``-only.
- (C2) ``validate_daemon_token`` ACCEPTS a ``ghs_`` installation token
  and REJECTS ``github_pat_``, ``ghp_``, and empty strings.
- (slice 3a) ``reconcile.py`` calls ``validate_daemon_token`` (not
  ``validate_github_token``) at startup — auth gate swap regression.
- (slice 3a) ``before_run.py`` still calls ``validate_github_token`` —
  worker path regression guard.

All ``gh`` subprocess calls are intercepted by patching
``baton_harness._auth._run`` so no real network calls are made.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import baton_harness._auth as auth_mod
from baton_harness._auth import (
    TokenValidationError,
    validate_daemon_token,
    validate_github_token,
)

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


# ---------------------------------------------------------------------------
# C1. Regression guard: worker validator still rejects ghs_ tokens
# ---------------------------------------------------------------------------


class TestWorkerValidatorRejectsInstallationToken:
    """``validate_github_token`` (worker path) must NOT accept ``ghs_`` tokens.

    Design decision #133: the worker path is ``github_pat_``-only.  A
    ``ghs_`` installation token reaching this validator would indicate the
    harness accidentally passed its privileged credential into the worker
    context -- a security regression.
    """

    def test_ghs_token_raises_token_validation_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``ghs_`` raises ``TokenValidationError`` in the worker gate."""
        monkeypatch.setenv("GH_TOKEN", "ghs_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        with pytest.raises(TokenValidationError):
            validate_github_token()

    def test_ghs_token_rejection_without_capability_call(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``ghs_`` must be rejected at the type gate, before any gh call.

        If the type gate fires first, no ``_run`` call should be made.
        """
        monkeypatch.setenv("GH_TOKEN", "ghs_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        run_called = False

        def sentinel_run(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            nonlocal run_called
            run_called = True
            return _completed(_GH_API_USER_OK)

        with patch.object(auth_mod, "_run", sentinel_run):
            with pytest.raises(TokenValidationError):
                validate_github_token()

        assert not run_called, (
            "validate_github_token must reject ghs_ at the type gate "
            "without making any gh subprocess call"
        )


# ---------------------------------------------------------------------------
# C2. New daemon validator: accept ghs_, reject all worker-token prefixes
# ---------------------------------------------------------------------------


class TestValidateDaemonToken:
    """``validate_daemon_token`` enforces the daemon-side token type gate.

    Accepts ``ghs_`` installation tokens only.  All worker-token forms
    (``github_pat_``, ``ghp_``) and empty strings must be rejected.  This
    is a type-gate only -- no live ``gh`` call is required or made.
    """

    def test_accepts_ghs_installation_token(self) -> None:
        """``ghs_`` installation token must be accepted without raising."""
        # Must not raise.
        validate_daemon_token("ghs_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")

    def test_rejects_fine_grained_pat(self) -> None:
        """Fine-grained worker PAT (``github_pat_``) must be rejected."""
        with pytest.raises(TokenValidationError):
            validate_daemon_token(
                "github_pat_AAAAAAAAAAAAAAAAAAAAAAAAAAAAA_BBBBBBB"
            )

    def test_rejects_classic_pat(self) -> None:
        """Classic PAT (``ghp_`` prefix) must be rejected."""
        with pytest.raises(TokenValidationError):
            validate_daemon_token("ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")

    def test_rejects_empty_string(self) -> None:
        """Empty string must be rejected."""
        with pytest.raises(TokenValidationError):
            validate_daemon_token("")

    def test_rejects_oauth_token(self) -> None:
        """OAuth app token (``gho_`` prefix) must be rejected."""
        with pytest.raises(TokenValidationError):
            validate_daemon_token("gho_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")

    def test_rejects_unknown_prefix(self) -> None:
        """An unrecognised token prefix must be rejected."""
        with pytest.raises(TokenValidationError):
            validate_daemon_token("v1_someoldstyletoken")


# ---------------------------------------------------------------------------
# Slice 3a regression: reconcile.py auth-gate swap
# ---------------------------------------------------------------------------


class TestReconcileUsesDaemonValidator:
    """reconcile.py calls validate_daemon_token, not the worker validator.

    These tests are RED until reconcile.py line 113 is swapped from
    ``validate_github_token`` to ``validate_daemon_token`` (slice 3a).
    """

    def test_reconcile_startup_calls_validate_daemon_token(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reconcile.reconcile_startup() calls validate_daemon_token.

        Patch both validators in the reconcile module scope and assert that
        validate_daemon_token is called while validate_github_token is NOT.
        Today this FAILS because reconcile.py still calls
        validate_github_token.
        """
        import asyncio  # noqa: PLC0415
        import importlib  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        reconcile = importlib.import_module("baton_harness.chain.reconcile")
        from baton_harness.chain.obs_config import ObsConfig  # noqa: PLC0415
        from baton_harness.chain.registry import RepoConfig  # noqa: PLC0415

        harness_dir = tmp_path / ".baton-harness"
        harness_dir.mkdir(parents=True, exist_ok=True)
        obs = ObsConfig(
            runlog_path=harness_dir / "runlog.jsonl",
            heartbeat_file=harness_dir / "heartbeat",
            redispatch_window_ticks=10,
            redispatch_max=3,
            heartbeat_stall_s=7200.0,
            heartbeat_ping_url=None,
            redispatch_counts_path=harness_dir / "dispatch-counts.json",
        )
        repo_cfgs = [
            RepoConfig(
                owner="glitchwerks",
                repo="baton-harness",
                project_root=tmp_path,
            )
        ]
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        cred_file = tmp_path / "fake_credentials.json"
        cred_file.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            "baton_harness.chain.reconcile._OAUTH_CRED_PATH",
            cred_file,
        )

        daemon_called: list[bool] = []
        worker_called: list[bool] = []

        with (
            patch(
                "baton_harness.chain.reconcile.validate_daemon_token",
                side_effect=lambda token: daemon_called.append(True),
            ),
            patch(
                "baton_harness.chain.reconcile.validate_github_token",
                side_effect=lambda **kw: worker_called.append(True),
            ),
            patch(
                "baton_harness.chain.reconcile.alert",
                return_value=True,
            ),
            patch(
                "baton_harness.chain.reconcile._list_claude_procs",
                return_value=[],
            ),
        ):
            # Provide a GH_TOKEN so the daemon validator can inspect it.
            monkeypatch.setenv("GH_TOKEN", "ghs_TESTTOKEN_for_reconcile")
            asyncio.run(
                reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
            )

        assert daemon_called, (
            "validate_daemon_token must be called by reconcile_startup "
            "after slice 3a (currently FAILS — reconcile still calls "
            "validate_github_token)"
        )
        assert not worker_called, (
            "validate_github_token must NOT be called in the daemon path "
            "after slice 3a"
        )

    def test_reconcile_startup_accepts_ghs_token(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reconcile_startup succeeds when GH_TOKEN is a ghs_ token.

        Today FAILS because validate_github_token rejects ghs_ prefix,
        causing reconcile_startup to sys.exit(1).  After slice 3a, the
        daemon validator accepts ghs_ and the function returns normally.
        """
        import asyncio  # noqa: PLC0415
        import importlib  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        reconcile = importlib.import_module("baton_harness.chain.reconcile")
        from baton_harness.chain.obs_config import ObsConfig  # noqa: PLC0415
        from baton_harness.chain.registry import RepoConfig  # noqa: PLC0415

        harness_dir = tmp_path / ".baton-harness"
        harness_dir.mkdir(parents=True, exist_ok=True)
        obs = ObsConfig(
            runlog_path=harness_dir / "runlog.jsonl",
            heartbeat_file=harness_dir / "heartbeat",
            redispatch_window_ticks=10,
            redispatch_max=3,
            heartbeat_stall_s=7200.0,
            heartbeat_ping_url=None,
            redispatch_counts_path=harness_dir / "dispatch-counts.json",
        )
        repo_cfgs = [
            RepoConfig(
                owner="glitchwerks",
                repo="baton-harness",
                project_root=tmp_path,
            )
        ]
        # ghs_ is a valid installation token — must be accepted post-swap.
        monkeypatch.setenv("GH_TOKEN", "ghs_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        cred_file = tmp_path / "fake_credentials.json"
        cred_file.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            "baton_harness.chain.reconcile._OAUTH_CRED_PATH",
            cred_file,
        )

        # Must NOT raise SystemExit after slice 3a.
        # Today raises SystemExit(1) because the worker validator rejects
        # ghs_ tokens — that is the RED signal for this test.
        with (
            patch(
                "baton_harness.chain.reconcile.alert",
                return_value=True,
            ),
            patch(
                "baton_harness.chain.reconcile._list_claude_procs",
                return_value=[],
            ),
        ):
            asyncio.run(
                reconcile.reconcile_startup(repo_cfgs, obs, runlog=None)
            )
        # If we reach here without SystemExit, the test passes.


# ---------------------------------------------------------------------------
# Slice 3a regression guard: worker path is unchanged
# ---------------------------------------------------------------------------


class TestWorkerPathAuthUnchanged:
    """before_run.main() must continue to call validate_github_token.

    This is the regression guard that slice 3a must NOT violate: the
    daemon-side swap to validate_daemon_token must not bleed into the
    worker (before_run) path.

    This test MUST be GREEN today AND after slice 3a.
    """

    def test_before_run_still_calls_validate_github_token(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """before_run.main() calls validate_github_token, not daemon variant.

        Patch both validators in the before_run module scope; confirm only
        the worker validator fires.  This must remain GREEN after slice 3a —
        it is the contract that the swap is daemon-only.
        """
        from unittest.mock import patch  # noqa: PLC0415

        import baton_harness.before_run as before_run_mod  # noqa: PLC0415

        worktree = tmp_path / "feat-2-sync"
        worktree.mkdir()
        monkeypatch.chdir(worktree)
        monkeypatch.delenv("CHAIN_BASE_BRANCH", raising=False)

        worker_called: list[bool] = []
        daemon_called: list[bool] = []

        def fake_worker_validate() -> None:
            worker_called.append(True)

        def fake_daemon_validate(token: str) -> None:
            daemon_called.append(True)

        import subprocess  # noqa: PLC0415

        def fake_run(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )

        def fake_run_capture(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="abc1234abc1234abc1234abc1234abc1234abc1234\n",
                stderr="",
            )

        monkeypatch.setattr(before_run_mod, "_run", fake_run)
        monkeypatch.setattr(before_run_mod, "_run_capture", fake_run_capture)
        monkeypatch.setattr(
            before_run_mod,
            "validate_github_token",
            fake_worker_validate,
        )

        # Also patch validate_daemon_token if it somehow ends up imported
        # in before_run after slice 3a changes.
        with patch(
            "baton_harness._auth.validate_daemon_token",
            side_effect=fake_daemon_validate,
        ):
            from baton_harness.before_run import main  # noqa: PLC0415

            result = main()

        assert result == 0, f"Expected exit 0, got {result}"
        assert worker_called, (
            "validate_github_token must be called by before_run.main() "
            "— worker path must remain github_pat_-only"
        )
        assert not daemon_called, (
            "validate_daemon_token must NOT be called in the worker path"
        )
