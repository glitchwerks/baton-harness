"""Bitwarden Secrets (BWS) CLI client for secret retrieval.

Provides a single public function, ``fetch_secret``, that shells out to
the ``bws`` CLI, parses its JSON output, and returns the secret's
``value`` field.

The subprocess call is injected via the ``run`` parameter so callers
control the transport layer in tests — no real ``bws`` binary is
invoked during unit tests.

Fail-closed semantics
---------------------
Every failure path raises ``BwsClientError`` rather than returning a
partial or empty result:

- Empty / ``None`` ``access_token`` raises *before* spawning a subprocess
  (avoids unauthenticated ``bws`` calls that produce confusing errors).
- Non-zero ``bws`` exit raises with the captured stderr.
- Malformed JSON or missing ``value`` field raises with a description.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable

# ---------------------------------------------------------------------------
# Type alias for the injectable run callable
# ---------------------------------------------------------------------------

#: Type of the injected subprocess runner.  Signature: ``(args, **kwargs)``
#: where ``kwargs`` may include ``env``.  Matches the test helper shape.
RunFn = Callable[..., subprocess.CompletedProcess[str]]


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class BwsClientError(RuntimeError):
    """Raised when a Bitwarden Secrets CLI call fails.

    Attributes:
        message: Human-readable description of the failure.
    """

    def __init__(self, message: str) -> None:
        """Initialise with a human-readable failure description.

        Args:
            message: Describes what went wrong (e.g. non-zero exit,
                malformed JSON, missing ``value`` field, or empty
                ``access_token``).
        """
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------------------
# Default run implementation
# ---------------------------------------------------------------------------


def _default_run(
    args: list[str],
    **_kwargs: object,
) -> subprocess.CompletedProcess[str]:
    """Invoke a subprocess and return the completed process.

    Wraps ``subprocess.run`` with ``capture_output=True`` and
    ``text=True`` (UTF-8) so stdout/stderr are available as strings.
    The ``env`` from ``_kwargs`` is extracted and passed explicitly so
    mypy can resolve the correct ``subprocess.run`` overload.

    Args:
        args: Command and arguments list.
        **_kwargs: Additional keyword arguments; currently only ``env``
            is consumed (a ``dict[str, str]`` mapping).  Unknown keys
            are silently ignored so that test stubs with extra kwargs
            still work against this default.

    Returns:
        A ``subprocess.CompletedProcess[str]`` with captured
        ``stdout``, ``stderr``, and ``returncode``.
    """
    env: dict[str, str] | None = None
    raw_env = _kwargs.get("env")
    if isinstance(raw_env, dict):
        # Narrow to dict[str, str]; subprocess.run expects Mapping[str, str].
        env = {str(k): str(v) for k, v in raw_env.items()}

    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_secret(
    secret_id: str,
    *,
    access_token: str | None,
    run: RunFn = _default_run,
) -> str:
    """Retrieve a secret value from Bitwarden Secrets via the ``bws`` CLI.

    Shells out to ``bws secret get <secret_id>``, parses the JSON output,
    and returns the ``value`` field.

    Args:
        secret_id: The UUID of the Bitwarden Secrets secret to retrieve.
        access_token: Bitwarden machine-account access token.  Must be
            non-empty; raises ``BwsClientError`` before calling ``bws``
            if the token is ``None`` or an empty string.
        run: Injected subprocess runner.  Defaults to a wrapper around
            ``subprocess.run``.  Tests pass a stub here to avoid
            invoking the real ``bws`` binary.

    Returns:
        The ``value`` field from the Bitwarden Secrets JSON response as
        a plain string.

    Raises:
        BwsClientError: When ``access_token`` is empty/``None``, when
            the ``bws`` CLI exits non-zero, when its output is not valid
            JSON, or when the ``value`` field is absent from the
            response.
    """
    # Guard: fail before spawning any subprocess on a missing token.
    if not access_token:
        raise BwsClientError(
            "fetch_secret: access_token is empty or None; "
            "cannot call bws without a machine-account token."
        )

    args = ["bws", "secret", "get", secret_id]
    # Pass the access token via env so it is not visible on the
    # process command line (avoids token exposure in ps/audit logs).
    bws_env = {**os.environ, "BWS_ACCESS_TOKEN": access_token}
    result = run(args, env=bws_env)

    if result.returncode != 0:
        raise BwsClientError(
            f"fetch_secret: bws exited {result.returncode} for secret "
            f"{secret_id!r}. stderr: {result.stderr.strip()!r}"
        )

    try:
        data: dict[str, object] = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BwsClientError(
            f"fetch_secret: bws output is not valid JSON for secret "
            f"{secret_id!r}: {exc}"
        ) from exc

    if "value" not in data:
        raise BwsClientError(
            f"fetch_secret: bws response for secret {secret_id!r} is "
            f"missing the 'value' field. "
            f"Got keys: {list(data.keys())!r}"
        )

    value = data["value"]
    if not isinstance(value, str):
        raise BwsClientError(
            f"fetch_secret: 'value' field for secret {secret_id!r} is "
            f"not a string: {type(value).__name__}"
        )

    if not value.strip():
        raise BwsClientError(
            f"fetch_secret: 'value' field for secret {secret_id!r} is "
            f"empty or whitespace-only."
        )

    return value
