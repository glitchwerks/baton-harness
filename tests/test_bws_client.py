"""Tests for baton_harness.chain.bws_client — Bitwarden Secrets client.

Coverage:
- ``fetch_secret`` extracts and returns the secret ``value`` from a
  well-formed ``bws secret get`` JSON response.
- Non-zero exit from the ``bws`` CLI raises ``BwsClientError``
  (fail-closed).
- Empty or missing ``access_token`` raises before shelling out.
- Malformed JSON from ``bws`` raises ``BwsClientError`` (fail-closed).
- Response JSON missing the ``value`` field raises ``BwsClientError``
  (fail-closed).
- The ``bws`` CLI is called with the correct arguments.

All subprocess calls are intercepted through an injectable ``run``
callable — no real ``bws`` binary is invoked.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable

import pytest

from baton_harness.chain.bws_client import BwsClientError, fetch_secret

# ---------------------------------------------------------------------------
# Type alias for the injected run callable
# ---------------------------------------------------------------------------

RunFn = Callable[..., subprocess.CompletedProcess[str]]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_ACCESS_TOKEN = "0.fake-bws-machine-account-token"
_SECRET_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_SECRET_VALUE = "super-secret-private-key-material"

_BWS_SECRET_JSON = json.dumps(
    {
        "id": _SECRET_ID,
        "key": "APP_PRIVATE_KEY",
        "value": _SECRET_VALUE,
        "organizationId": "org-111",
        "projectId": "proj-222",
    }
)


def _ok_run(
    args: list[str],
    **_: object,
) -> subprocess.CompletedProcess[str]:
    """Fake ``run`` that returns a successful bws response.

    Args:
        args: The command-line arguments that would be passed.
        **_: Any additional keyword arguments (captured, ignored).

    Returns:
        A ``subprocess.CompletedProcess`` simulating a successful
        ``bws`` invocation.
    """
    return subprocess.CompletedProcess(
        args=args,
        returncode=0,
        stdout=_BWS_SECRET_JSON,
        stderr="",
    )


def _fail_run(
    args: list[str],
    **_: object,
) -> subprocess.CompletedProcess[str]:
    """Fake ``run`` that simulates a non-zero bws exit.

    Args:
        args: The command-line arguments that would be passed.
        **_: Any additional keyword arguments (captured, ignored).

    Returns:
        A ``subprocess.CompletedProcess`` with returncode=1.
    """
    return subprocess.CompletedProcess(
        args=args,
        returncode=1,
        stdout="",
        stderr="error: unauthorized",
    )


# ---------------------------------------------------------------------------
# B1. Happy path — value extraction
# ---------------------------------------------------------------------------


class TestFetchSecretHappyPath:
    """``fetch_secret`` extracts the secret value from a valid bws response."""

    def test_returns_secret_value(self) -> None:
        """Returns the ``value`` field from a successful bws JSON response."""
        result = fetch_secret(
            _SECRET_ID,
            access_token=_VALID_ACCESS_TOKEN,
            run=_ok_run,
        )

        assert result == _SECRET_VALUE, (
            f"Expected secret value {_SECRET_VALUE!r}, got {result!r}"
        )

    def test_calls_bws_with_secret_id(self) -> None:
        """The ``bws`` CLI must be invoked with the correct secret ID."""
        captured_args: list[list[str]] = []

        def recording_run(
            args: list[str],
            **_: object,
        ) -> subprocess.CompletedProcess[str]:
            """Capture args and return a valid response."""
            captured_args.append(list(args))
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=_BWS_SECRET_JSON,
                stderr="",
            )

        fetch_secret(
            _SECRET_ID,
            access_token=_VALID_ACCESS_TOKEN,
            run=recording_run,
        )

        assert captured_args, "run was never called"
        flat_args = " ".join(captured_args[0])
        assert _SECRET_ID in flat_args, (
            f"Expected secret ID {_SECRET_ID!r} in bws args: "
            f"{captured_args[0]!r}"
        )

    def test_calls_bws_binary(self) -> None:
        """The first argument to ``run`` must be the ``bws`` command."""
        captured_args: list[list[str]] = []

        def recording_run(
            args: list[str],
            **_: object,
        ) -> subprocess.CompletedProcess[str]:
            """Capture args and return a valid response."""
            captured_args.append(list(args))
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=_BWS_SECRET_JSON,
                stderr="",
            )

        fetch_secret(
            _SECRET_ID,
            access_token=_VALID_ACCESS_TOKEN,
            run=recording_run,
        )

        assert captured_args, "run was never called"
        assert captured_args[0][0] == "bws", (
            f"Expected first arg 'bws', got {captured_args[0][0]!r}"
        )


# ---------------------------------------------------------------------------
# B2. Fail-closed: error paths
# ---------------------------------------------------------------------------


class TestFetchSecretFailClosed:
    """``fetch_secret`` raises ``BwsClientError`` on any failure path."""

    def test_nonzero_exit_raises(self) -> None:
        """Non-zero exit from the bws CLI must raise ``BwsClientError``."""
        with pytest.raises(BwsClientError):
            fetch_secret(
                _SECRET_ID,
                access_token=_VALID_ACCESS_TOKEN,
                run=_fail_run,
            )

    def test_empty_access_token_raises_before_run(self) -> None:
        """Empty ``access_token`` must raise without calling ``run``."""
        run_called = False

        def sentinel_run(
            args: list[str],
            **_: object,
        ) -> subprocess.CompletedProcess[str]:
            """Record that run was called; must not be reached."""
            nonlocal run_called
            run_called = True
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=_BWS_SECRET_JSON,
                stderr="",
            )

        with pytest.raises(BwsClientError):
            fetch_secret(
                _SECRET_ID,
                access_token="",
                run=sentinel_run,
            )

        assert not run_called, (
            "run must not be called when access_token is empty"
        )

    def test_none_access_token_raises_before_run(self) -> None:
        """``None`` ``access_token`` must raise without calling ``run``.

        Even if the caller passes ``None`` instead of an empty string,
        the guard must fire before any subprocess is spawned.
        """
        run_called = False

        def sentinel_run(
            args: list[str],
            **_: object,
        ) -> subprocess.CompletedProcess[str]:
            """Record that run was called; must not be reached."""
            nonlocal run_called
            run_called = True
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=_BWS_SECRET_JSON,
                stderr="",
            )

        with pytest.raises(BwsClientError):
            fetch_secret(
                _SECRET_ID,
                access_token=None,  # type: ignore[arg-type]
                run=sentinel_run,
            )

        assert not run_called, (
            "run must not be called when access_token is None"
        )

    def test_malformed_json_raises(self) -> None:
        """Malformed JSON from bws stdout raises ``BwsClientError``."""

        def bad_json_run(
            args: list[str],
            **_: object,
        ) -> subprocess.CompletedProcess[str]:
            """Return garbled non-JSON stdout."""
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="this is not json {{{",
                stderr="",
            )

        with pytest.raises(BwsClientError):
            fetch_secret(
                _SECRET_ID,
                access_token=_VALID_ACCESS_TOKEN,
                run=bad_json_run,
            )

    def test_response_missing_value_field_raises(self) -> None:
        """Response JSON lacking ``value`` raises ``BwsClientError``."""
        no_value_json = json.dumps({"id": _SECRET_ID, "key": "APP_KEY"})

        def no_value_run(
            args: list[str],
            **_: object,
        ) -> subprocess.CompletedProcess[str]:
            """Return a response body that omits the 'value' field."""
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=no_value_json,
                stderr="",
            )

        with pytest.raises(BwsClientError):
            fetch_secret(
                _SECRET_ID,
                access_token=_VALID_ACCESS_TOKEN,
                run=no_value_run,
            )

    def test_value_field_is_not_string_raises(self) -> None:
        """Non-string ``value`` field raises ``BwsClientError``.

        F2 polish (#133): ``bws_client.py`` must validate that the
        ``value`` field in the response JSON is a ``str``.  A non-string
        value (e.g. an integer) must be rejected with ``BwsClientError``
        rather than returning the wrong type silently.

        This test pins existing behaviour at bws_client.py line 174
        and is expected to be GREEN today.
        """
        not_string_json = json.dumps({"id": _SECRET_ID, "value": 123})

        def int_value_run(
            args: list[str],
            **_: object,
        ) -> subprocess.CompletedProcess[str]:
            """Return a response body where 'value' is an integer."""
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=not_string_json,
                stderr="",
            )

        with pytest.raises(BwsClientError):
            fetch_secret(
                _SECRET_ID,
                access_token=_VALID_ACCESS_TOKEN,
                run=int_value_run,
            )
