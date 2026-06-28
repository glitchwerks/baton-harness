"""Tests for sandbox_config — parse, validate, and env-populate .bh/config.env.

Coverage (issue #175):
- Happy path: valid file → SandboxConfig with all 7 fields + 2 derived
  twins; os.environ populated; mocked gh api succeeds.
- Quote stripping: single and double quotes around values are stripped.
- Comment lines and blank lines are ignored.
- Each required key missing → SandboxConfigError naming the key.
- Malformed values → SandboxConfigError including the line number and
  the offending value:
  - Non-numeric BH_GITHUB_APP_ID
  - BH_GITHUB_APP_ID = 0 (not > 0)
  - Invalid UUID for BWS_PEM_SECRET_ID
  - Empty BH_REPO_OWNER
  - BH_REPO_OWNER with illegal characters
- Optional secret IDs absent → OK (default "").
- Optional secret IDs present but malformed UUID → SandboxConfigError.
- Missing file → SandboxConfigError.
- Network validation: gh api non-zero → SandboxConfigError naming
  owner/name; gh api success → proceeds; runner called with expected argv.
- Derived-twin keys appearing in file (BWS_APP_ID, BWS_INSTALLATION_ID)
  → silently ignored (no error).

All subprocess / network calls are intercepted via an injectable ``run``
kwarg.  No real ``gh`` binary or GitHub API is contacted.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

from baton_harness.chain.sandbox_config import (
    SandboxConfig,
    SandboxConfigError,
    read_and_validate,
)

# ---------------------------------------------------------------------------
# Type alias — matches bws_client.RunFn shape
# ---------------------------------------------------------------------------

RunFn = Callable[..., subprocess.CompletedProcess[str]]

# ---------------------------------------------------------------------------
# Constants — canonical valid values
# ---------------------------------------------------------------------------

_OWNER = "my-org"
_REPO = "my-sandbox"
_APP_ID = "12345"
_INSTALL_ID = "67890"
_PEM_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_GH_TOKEN_UUID = "11111111-2222-3333-4444-555555555555"
_HEARTBEAT_UUID = "66666666-7777-8888-9999-aaaaaaaaaaaa"

_VALID_ENV_CONTENT = textwrap.dedent(
    f"""\
    BH_REPO_OWNER={_OWNER}
    BH_REPO_NAME={_REPO}
    BH_GITHUB_APP_ID={_APP_ID}
    BH_GITHUB_APP_INSTALLATION_ID={_INSTALL_ID}
    BWS_PEM_SECRET_ID={_PEM_UUID}
    BWS_GH_TOKEN_SECRET_ID={_GH_TOKEN_UUID}
    BWS_HEARTBEAT_PING_URL_SECRET_ID={_HEARTBEAT_UUID}
    """
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run_stub(
    returncode: int = 0,
    stdout: str = '{"id": 999}',
    stderr: str = "",
) -> RunFn:
    """Return a run-stub that records calls and returns a fixed result.

    Args:
        returncode: Exit code the stub should return.
        stdout: stdout string for the CompletedProcess.
        stderr: stderr string for the CompletedProcess.

    Returns:
        A callable matching the ``run`` kwarg signature used by
        ``read_and_validate``.
    """
    calls: list[list[str]] = []

    def _stub(
        args: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return subprocess.CompletedProcess(
            args=args,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    _stub.calls = calls  # type: ignore[attr-defined]
    return _stub


def _write_env(tmp_path: Path, content: str) -> Path:
    """Write content to a temp config.env file and return its path.

    Args:
        tmp_path: pytest-provided temporary directory.
        content: File content to write.

    Returns:
        Absolute path to the written file.
    """
    p = tmp_path / "config.env"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# H1. Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    """Valid file → SandboxConfig populated; os.environ set; gh api called."""

    def test_returns_sandbox_config_with_all_fields(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """SandboxConfig fields match exactly what was in the file.

        A valid .bh/config.env with all 7 keys produces a SandboxConfig
        whose fields match the parsed values.
        """
        env_file = _write_env(tmp_path, _VALID_ENV_CONTENT)
        run = _make_run_stub()

        result = read_and_validate(env_file, run=run)

        assert isinstance(result, SandboxConfig)
        assert result.repo_owner == _OWNER
        assert result.repo_name == _REPO
        assert result.github_app_id == _APP_ID
        assert result.github_app_installation_id == _INSTALL_ID
        assert result.bws_pem_secret_id == _PEM_UUID
        assert result.bws_gh_token_secret_id == _GH_TOKEN_UUID
        assert result.bws_heartbeat_ping_url_secret_id == _HEARTBEAT_UUID

    def test_populates_os_environ_with_all_7_keys(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All 7 file keys are written into os.environ on success.

        Uses monkeypatch so the test environment is clean before and after.
        """
        for key in (
            "BH_REPO_OWNER",
            "BH_REPO_NAME",
            "BH_GITHUB_APP_ID",
            "BH_GITHUB_APP_INSTALLATION_ID",
            "BWS_PEM_SECRET_ID",
            "BWS_GH_TOKEN_SECRET_ID",
            "BWS_HEARTBEAT_PING_URL_SECRET_ID",
        ):
            monkeypatch.delenv(key, raising=False)

        env_file = _write_env(tmp_path, _VALID_ENV_CONTENT)
        run = _make_run_stub()

        read_and_validate(env_file, run=run)

        assert os.environ["BH_REPO_OWNER"] == _OWNER
        assert os.environ["BH_REPO_NAME"] == _REPO
        assert os.environ["BH_GITHUB_APP_ID"] == _APP_ID
        assert os.environ["BH_GITHUB_APP_INSTALLATION_ID"] == _INSTALL_ID
        assert os.environ["BWS_PEM_SECRET_ID"] == _PEM_UUID
        assert os.environ["BWS_GH_TOKEN_SECRET_ID"] == _GH_TOKEN_UUID
        assert (
            os.environ["BWS_HEARTBEAT_PING_URL_SECRET_ID"] == _HEARTBEAT_UUID
        )

    def test_populates_os_environ_with_derived_twins(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """BWS_APP_ID and BWS_INSTALLATION_ID derived twins are written to env.

        BWS_APP_ID must equal BH_GITHUB_APP_ID;
        BWS_INSTALLATION_ID must equal BH_GITHUB_APP_INSTALLATION_ID.
        """
        for key in ("BWS_APP_ID", "BWS_INSTALLATION_ID"):
            monkeypatch.delenv(key, raising=False)

        env_file = _write_env(tmp_path, _VALID_ENV_CONTENT)
        run = _make_run_stub()

        read_and_validate(env_file, run=run)

        assert os.environ["BWS_APP_ID"] == _APP_ID
        assert os.environ["BWS_INSTALLATION_ID"] == _INSTALL_ID

    def test_gh_api_runner_called_with_expected_argv(
        self,
        tmp_path: Path,
    ) -> None:
        """The injectable runner is called with gh api repos/OWNER/NAME.

        The exact argv must include the repo path so GitHub validates the
        repo exists and the app installation has access to it.
        """
        env_file = _write_env(tmp_path, _VALID_ENV_CONTENT)
        run = _make_run_stub()

        read_and_validate(env_file, run=run)

        assert run.calls, "run was never called — network validation skipped"  # type: ignore[attr-defined]
        first_call = run.calls[0]  # type: ignore[attr-defined]
        assert "gh" in first_call
        assert f"repos/{_OWNER}/{_REPO}" in " ".join(first_call)


# ---------------------------------------------------------------------------
# H2. Quote stripping, comments, blank lines
# ---------------------------------------------------------------------------


class TestFileParsingEdgeCases:
    """Quote stripping, comments, and blank lines are handled correctly."""

    def test_single_quotes_stripped_from_values(
        self,
        tmp_path: Path,
    ) -> None:
        """Single-quoted values are unquoted when stored in SandboxConfig."""
        content = textwrap.dedent(
            f"""\
            BH_REPO_OWNER='{_OWNER}'
            BH_REPO_NAME='{_REPO}'
            BH_GITHUB_APP_ID='{_APP_ID}'
            BH_GITHUB_APP_INSTALLATION_ID='{_INSTALL_ID}'
            BWS_PEM_SECRET_ID='{_PEM_UUID}'
            BWS_GH_TOKEN_SECRET_ID='{_GH_TOKEN_UUID}'
            BWS_HEARTBEAT_PING_URL_SECRET_ID='{_HEARTBEAT_UUID}'
            """
        )
        env_file = _write_env(tmp_path, content)
        run = _make_run_stub()

        result = read_and_validate(env_file, run=run)

        assert result.repo_owner == _OWNER
        assert result.bws_pem_secret_id == _PEM_UUID

    def test_double_quotes_stripped_from_values(
        self,
        tmp_path: Path,
    ) -> None:
        """Double-quoted values are unquoted when stored in SandboxConfig."""
        content = textwrap.dedent(
            f"""\
            BH_REPO_OWNER="{_OWNER}"
            BH_REPO_NAME="{_REPO}"
            BH_GITHUB_APP_ID="{_APP_ID}"
            BH_GITHUB_APP_INSTALLATION_ID="{_INSTALL_ID}"
            BWS_PEM_SECRET_ID="{_PEM_UUID}"
            BWS_GH_TOKEN_SECRET_ID="{_GH_TOKEN_UUID}"
            BWS_HEARTBEAT_PING_URL_SECRET_ID="{_HEARTBEAT_UUID}"
            """
        )
        env_file = _write_env(tmp_path, content)
        run = _make_run_stub()

        result = read_and_validate(env_file, run=run)

        assert result.repo_owner == _OWNER
        assert result.bws_pem_secret_id == _PEM_UUID

    def test_comment_lines_ignored(
        self,
        tmp_path: Path,
    ) -> None:
        """Lines whose first non-space char is '#' are silently ignored."""
        content = textwrap.dedent(
            f"""\
            # This is a comment
            BH_REPO_OWNER={_OWNER}
            # another comment
            BH_REPO_NAME={_REPO}
            BH_GITHUB_APP_ID={_APP_ID}
            BH_GITHUB_APP_INSTALLATION_ID={_INSTALL_ID}
            BWS_PEM_SECRET_ID={_PEM_UUID}
            BWS_GH_TOKEN_SECRET_ID={_GH_TOKEN_UUID}
            BWS_HEARTBEAT_PING_URL_SECRET_ID={_HEARTBEAT_UUID}
            """
        )
        env_file = _write_env(tmp_path, content)
        run = _make_run_stub()

        result = read_and_validate(env_file, run=run)

        assert result.repo_owner == _OWNER

    def test_blank_lines_ignored(
        self,
        tmp_path: Path,
    ) -> None:
        """Blank lines in the file are silently ignored."""
        content = textwrap.dedent(
            f"""\
            BH_REPO_OWNER={_OWNER}

            BH_REPO_NAME={_REPO}

            BH_GITHUB_APP_ID={_APP_ID}
            BH_GITHUB_APP_INSTALLATION_ID={_INSTALL_ID}
            BWS_PEM_SECRET_ID={_PEM_UUID}
            BWS_GH_TOKEN_SECRET_ID={_GH_TOKEN_UUID}
            BWS_HEARTBEAT_PING_URL_SECRET_ID={_HEARTBEAT_UUID}
            """
        )
        env_file = _write_env(tmp_path, content)
        run = _make_run_stub()

        result = read_and_validate(env_file, run=run)

        assert result.repo_name == _REPO


# ---------------------------------------------------------------------------
# H3. Missing file
# ---------------------------------------------------------------------------


class TestMissingFile:
    """A path that does not exist raises SandboxConfigError."""

    def test_missing_file_raises_sandbox_config_error(
        self,
        tmp_path: Path,
    ) -> None:
        """SandboxConfigError is raised when the config file does not exist."""
        missing = tmp_path / "does_not_exist.env"
        run = _make_run_stub()

        with pytest.raises(SandboxConfigError):
            read_and_validate(missing, run=run)


# ---------------------------------------------------------------------------
# H4. Required keys missing
# ---------------------------------------------------------------------------


class TestRequiredKeysMissing:
    """Each required key absent → SandboxConfigError naming that key."""

    @pytest.mark.parametrize(
        "missing_key",
        [
            "BH_REPO_OWNER",
            "BH_REPO_NAME",
            "BH_GITHUB_APP_ID",
            "BH_GITHUB_APP_INSTALLATION_ID",
            "BWS_PEM_SECRET_ID",
        ],
    )
    def test_missing_required_key_raises_error_naming_the_key(
        self,
        tmp_path: Path,
        missing_key: str,
    ) -> None:
        """SandboxConfigError is raised and names the missing required key.

        The error message must name the key so an operator knows exactly
        which line to add to their config.env.
        """
        lines = {
            "BH_REPO_OWNER": f"BH_REPO_OWNER={_OWNER}",
            "BH_REPO_NAME": f"BH_REPO_NAME={_REPO}",
            "BH_GITHUB_APP_ID": f"BH_GITHUB_APP_ID={_APP_ID}",
            "BH_GITHUB_APP_INSTALLATION_ID": (
                f"BH_GITHUB_APP_INSTALLATION_ID={_INSTALL_ID}"
            ),
            "BWS_PEM_SECRET_ID": f"BWS_PEM_SECRET_ID={_PEM_UUID}",
        }
        content = "\n".join(v for k, v in lines.items() if k != missing_key)
        env_file = _write_env(tmp_path, content)
        run = _make_run_stub()

        with pytest.raises(SandboxConfigError, match=missing_key):
            read_and_validate(env_file, run=run)


# ---------------------------------------------------------------------------
# H5. Malformed values — line numbers in error messages
# ---------------------------------------------------------------------------


class TestMalformedValues:
    """Malformed values raise SandboxConfigError with line number."""

    def test_non_numeric_app_id_raises_with_line_number(
        self,
        tmp_path: Path,
    ) -> None:
        """BH_GITHUB_APP_ID='not-a-number' → error includes line number."""
        content = textwrap.dedent(
            """\
            BH_REPO_OWNER=my-org
            BH_REPO_NAME=my-sandbox
            BH_GITHUB_APP_ID=not-a-number
            BH_GITHUB_APP_INSTALLATION_ID=67890
            BWS_PEM_SECRET_ID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee
            """
        )
        env_file = _write_env(tmp_path, content)
        run = _make_run_stub()

        with pytest.raises(SandboxConfigError) as exc_info:
            read_and_validate(env_file, run=run)

        msg = str(exc_info.value)
        assert "3" in msg, f"Expected line number '3' in error, got: {msg!r}"
        assert "not-a-number" in msg

    def test_app_id_zero_raises_error(
        self,
        tmp_path: Path,
    ) -> None:
        """BH_GITHUB_APP_ID=0 (not > 0) → SandboxConfigError."""
        content = textwrap.dedent(
            """\
            BH_REPO_OWNER=my-org
            BH_REPO_NAME=my-sandbox
            BH_GITHUB_APP_ID=0
            BH_GITHUB_APP_INSTALLATION_ID=67890
            BWS_PEM_SECRET_ID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee
            """
        )
        env_file = _write_env(tmp_path, content)
        run = _make_run_stub()

        with pytest.raises(SandboxConfigError):
            read_and_validate(env_file, run=run)

    def test_invalid_uuid_pem_secret_raises_with_line_number(
        self,
        tmp_path: Path,
    ) -> None:
        """BWS_PEM_SECRET_ID with bad UUID → error includes line number."""
        content = textwrap.dedent(
            """\
            BH_REPO_OWNER=my-org
            BH_REPO_NAME=my-sandbox
            BH_GITHUB_APP_ID=12345
            BH_GITHUB_APP_INSTALLATION_ID=67890
            BWS_PEM_SECRET_ID=not-a-uuid
            """
        )
        env_file = _write_env(tmp_path, content)
        run = _make_run_stub()

        with pytest.raises(SandboxConfigError) as exc_info:
            read_and_validate(env_file, run=run)

        msg = str(exc_info.value)
        assert "5" in msg, f"Expected line number '5' in error, got: {msg!r}"
        assert "not-a-uuid" in msg

    def test_empty_repo_owner_raises_error(
        self,
        tmp_path: Path,
    ) -> None:
        """BH_REPO_OWNER= (empty string) → SandboxConfigError."""
        content = textwrap.dedent(
            """\
            BH_REPO_OWNER=
            BH_REPO_NAME=my-sandbox
            BH_GITHUB_APP_ID=12345
            BH_GITHUB_APP_INSTALLATION_ID=67890
            BWS_PEM_SECRET_ID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee
            """
        )
        env_file = _write_env(tmp_path, content)
        run = _make_run_stub()

        with pytest.raises(SandboxConfigError):
            read_and_validate(env_file, run=run)

    def test_illegal_chars_in_repo_owner_raises_with_line_number(
        self,
        tmp_path: Path,
    ) -> None:
        """BH_REPO_OWNER with illegal chars (e.g. '!') → error + line num."""
        content = textwrap.dedent(
            """\
            BH_REPO_OWNER=bad!owner
            BH_REPO_NAME=my-sandbox
            BH_GITHUB_APP_ID=12345
            BH_GITHUB_APP_INSTALLATION_ID=67890
            BWS_PEM_SECRET_ID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee
            """
        )
        env_file = _write_env(tmp_path, content)
        run = _make_run_stub()

        with pytest.raises(SandboxConfigError) as exc_info:
            read_and_validate(env_file, run=run)

        msg = str(exc_info.value)
        assert "1" in msg, f"Expected line number '1' in error, got: {msg!r}"
        assert "bad!owner" in msg

    def test_negative_installation_id_raises_error(
        self,
        tmp_path: Path,
    ) -> None:
        """BH_GITHUB_APP_INSTALLATION_ID=-1 (not > 0) → SandboxConfigError."""
        content = textwrap.dedent(
            """\
            BH_REPO_OWNER=my-org
            BH_REPO_NAME=my-sandbox
            BH_GITHUB_APP_ID=12345
            BH_GITHUB_APP_INSTALLATION_ID=-1
            BWS_PEM_SECRET_ID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee
            """
        )
        env_file = _write_env(tmp_path, content)
        run = _make_run_stub()

        with pytest.raises(SandboxConfigError):
            read_and_validate(env_file, run=run)


# ---------------------------------------------------------------------------
# H6. Optional secret IDs
# ---------------------------------------------------------------------------


class TestOptionalSecretIds:
    """Optional BWS_*_SECRET_ID keys behave correctly when absent/malformed."""

    def test_optional_ids_absent_defaults_to_empty_string(
        self,
        tmp_path: Path,
    ) -> None:
        """SandboxConfig optional fields default to '' when keys are absent."""
        content = textwrap.dedent(
            f"""\
            BH_REPO_OWNER={_OWNER}
            BH_REPO_NAME={_REPO}
            BH_GITHUB_APP_ID={_APP_ID}
            BH_GITHUB_APP_INSTALLATION_ID={_INSTALL_ID}
            BWS_PEM_SECRET_ID={_PEM_UUID}
            """
        )
        env_file = _write_env(tmp_path, content)
        run = _make_run_stub()

        result = read_and_validate(env_file, run=run)

        assert result.bws_gh_token_secret_id == ""
        assert result.bws_heartbeat_ping_url_secret_id == ""

    def test_optional_id_malformed_uuid_raises_error(
        self,
        tmp_path: Path,
    ) -> None:
        """BWS_GH_TOKEN_SECRET_ID with bad UUID → SandboxConfigError."""
        content = textwrap.dedent(
            f"""\
            BH_REPO_OWNER={_OWNER}
            BH_REPO_NAME={_REPO}
            BH_GITHUB_APP_ID={_APP_ID}
            BH_GITHUB_APP_INSTALLATION_ID={_INSTALL_ID}
            BWS_PEM_SECRET_ID={_PEM_UUID}
            BWS_GH_TOKEN_SECRET_ID=not-a-valid-uuid
            """
        )
        env_file = _write_env(tmp_path, content)
        run = _make_run_stub()

        with pytest.raises(SandboxConfigError) as exc_info:
            read_and_validate(env_file, run=run)

        assert "not-a-valid-uuid" in str(exc_info.value)

    def test_optional_heartbeat_malformed_uuid_raises_error(
        self,
        tmp_path: Path,
    ) -> None:
        """BWS_HEARTBEAT_PING_URL_SECRET_ID bad UUID → SandboxConfigError."""
        content = textwrap.dedent(
            f"""\
            BH_REPO_OWNER={_OWNER}
            BH_REPO_NAME={_REPO}
            BH_GITHUB_APP_ID={_APP_ID}
            BH_GITHUB_APP_INSTALLATION_ID={_INSTALL_ID}
            BWS_PEM_SECRET_ID={_PEM_UUID}
            BWS_HEARTBEAT_PING_URL_SECRET_ID=definitely-not-a-uuid
            """
        )
        env_file = _write_env(tmp_path, content)
        run = _make_run_stub()

        with pytest.raises(SandboxConfigError) as exc_info:
            read_and_validate(env_file, run=run)

        assert "definitely-not-a-uuid" in str(exc_info.value)


# ---------------------------------------------------------------------------
# H7. Network validation
# ---------------------------------------------------------------------------


class TestNetworkValidation:
    """gh api call is made and failures raise SandboxConfigError."""

    def test_gh_api_nonzero_exit_raises_sandbox_config_error(
        self,
        tmp_path: Path,
    ) -> None:
        """Gh api returning non-zero → SandboxConfigError naming owner/name."""
        env_file = _write_env(tmp_path, _VALID_ENV_CONTENT)
        run = _make_run_stub(
            returncode=1,
            stdout="",
            stderr="HTTP 404: Not Found",
        )

        with pytest.raises(SandboxConfigError) as exc_info:
            read_and_validate(env_file, run=run)

        msg = str(exc_info.value)
        assert _OWNER in msg, (
            f"Expected owner {_OWNER!r} in error message, got: {msg!r}"
        )
        assert _REPO in msg, (
            f"Expected repo {_REPO!r} in error message, got: {msg!r}"
        )

    def test_gh_api_success_allows_parse_to_complete(
        self,
        tmp_path: Path,
    ) -> None:
        """Gh api returning 0 → read_and_validate returns SandboxConfig."""
        env_file = _write_env(tmp_path, _VALID_ENV_CONTENT)
        run = _make_run_stub(returncode=0, stdout='{"id": 12345678}')

        result = read_and_validate(env_file, run=run)

        assert isinstance(result, SandboxConfig)

    def test_runner_called_with_gh_api_repos_owner_name(
        self,
        tmp_path: Path,
    ) -> None:
        """Run is called with an argv containing 'gh api repos/OWNER/NAME'.

        Verifies the injection seam is used and the correct GitHub path is
        requested — the test suite never touches a real network.
        """
        env_file = _write_env(tmp_path, _VALID_ENV_CONTENT)
        run = _make_run_stub(returncode=0)

        read_and_validate(env_file, run=run)

        assert run.calls, "run stub was never called"  # type: ignore[attr-defined]
        # At least one call must reference repos/OWNER/NAME
        combined = " ".join(" ".join(c) for c in run.calls)  # type: ignore[attr-defined]
        assert f"repos/{_OWNER}/{_REPO}" in combined, (
            f"Expected 'repos/{_OWNER}/{_REPO}' in runner argv, "
            f"got calls: {run.calls!r}"  # type: ignore[attr-defined]
        )


# ---------------------------------------------------------------------------
# H8. Derived-twin keys in file → silently ignored
# ---------------------------------------------------------------------------


class TestDerivedTwinKeysInFile:
    """BWS_APP_ID / BWS_INSTALLATION_ID in file are silently ignored.

    Contract decision: if an operator accidentally commits the derived
    twin keys (BWS_APP_ID, BWS_INSTALLATION_ID) to their .bh/config.env,
    the parser ignores them without raising an error.  The values in env
    are still derived from BH_GITHUB_APP_ID / BH_GITHUB_APP_INSTALLATION_ID
    respectively.  This is the lenient / backwards-compatible behavior.
    """

    def test_derived_twin_keys_in_file_do_not_raise(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Presence of BWS_APP_ID / BWS_INSTALLATION_ID in file → no error."""
        content = textwrap.dedent(
            f"""\
            BH_REPO_OWNER={_OWNER}
            BH_REPO_NAME={_REPO}
            BH_GITHUB_APP_ID={_APP_ID}
            BH_GITHUB_APP_INSTALLATION_ID={_INSTALL_ID}
            BWS_PEM_SECRET_ID={_PEM_UUID}
            BWS_GH_TOKEN_SECRET_ID={_GH_TOKEN_UUID}
            BWS_HEARTBEAT_PING_URL_SECRET_ID={_HEARTBEAT_UUID}
            BWS_APP_ID=99999
            BWS_INSTALLATION_ID=88888
            """
        )
        env_file = _write_env(tmp_path, content)
        run = _make_run_stub()

        # Must not raise
        result = read_and_validate(env_file, run=run)

        # Derived values are from BH_* keys, not from the file's BWS_* lines
        assert result.github_app_id == _APP_ID
        assert result.github_app_installation_id == _INSTALL_ID

    def test_derived_twins_in_env_reflect_bh_keys_not_file_values(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """BWS_APP_ID in env derives from BH_GITHUB_APP_ID, not file override.

        Even when file has BWS_APP_ID=99999, os.environ['BWS_APP_ID'] must
        equal BH_GITHUB_APP_ID (_APP_ID = '12345'), not '99999'.
        """
        for key in ("BWS_APP_ID", "BWS_INSTALLATION_ID"):
            monkeypatch.delenv(key, raising=False)

        content = textwrap.dedent(
            f"""\
            BH_REPO_OWNER={_OWNER}
            BH_REPO_NAME={_REPO}
            BH_GITHUB_APP_ID={_APP_ID}
            BH_GITHUB_APP_INSTALLATION_ID={_INSTALL_ID}
            BWS_PEM_SECRET_ID={_PEM_UUID}
            BWS_APP_ID=99999
            BWS_INSTALLATION_ID=88888
            """
        )
        env_file = _write_env(tmp_path, content)
        run = _make_run_stub()

        read_and_validate(env_file, run=run)

        assert os.environ.get("BWS_APP_ID") == _APP_ID, (
            f"Expected BWS_APP_ID={_APP_ID!r} (derived), "
            f"got {os.environ.get('BWS_APP_ID')!r}"
        )
        assert os.environ.get("BWS_INSTALLATION_ID") == _INSTALL_ID, (
            f"Expected BWS_INSTALLATION_ID={_INSTALL_ID!r} (derived), "
            f"got {os.environ.get('BWS_INSTALLATION_ID')!r}"
        )
