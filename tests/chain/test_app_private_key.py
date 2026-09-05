"""Tests for GitHub App private-key provider policy and loading."""

from __future__ import annotations

import os
import stat
import traceback
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from baton_harness.chain.app_private_key import (
    AppPrivateKeyConfig,
    AppPrivateKeyConfigError,
    AppPrivateKeyLoadError,
    AppPrivateKeyProvider,
    load_app_private_key,
    main,
    requires_bws,
    resolve_app_private_key_config,
)

_PEM_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_OPTIONAL_ID = "11111111-2222-3333-4444-555555555555"


@pytest.mark.parametrize(
    ("values", "match"),
    [
        ({}, "BH_GITHUB_APP_KEY_PROVIDER"),
        ({"BH_GITHUB_APP_KEY_PROVIDER": "vault"}, "unsupported"),
        ({"BH_GITHUB_APP_KEY_PROVIDER": "bws"}, "BWS_PEM_SECRET_ID"),
        (
            {
                "BH_GITHUB_APP_KEY_PROVIDER": "bws",
                "BWS_PEM_SECRET_ID": "not-a-uuid",
            },
            "BWS_PEM_SECRET_ID",
        ),
        (
            {
                "BH_GITHUB_APP_KEY_PROVIDER": "bws",
                "BWS_PEM_SECRET_ID": _PEM_ID,
                "BH_GITHUB_APP_PRIVATE_KEY_FILE": "/run/key.pem",
            },
            "conflicting",
        ),
        ({"BH_GITHUB_APP_KEY_PROVIDER": "file"}, "PRIVATE_KEY_FILE"),
        (
            {
                "BH_GITHUB_APP_KEY_PROVIDER": "file",
                "BH_GITHUB_APP_PRIVATE_KEY_FILE": "relative.pem",
            },
            "absolute",
        ),
        (
            {
                "BH_GITHUB_APP_KEY_PROVIDER": "file",
                "BH_GITHUB_APP_PRIVATE_KEY_FILE": "/run/key.pem",
                "BWS_PEM_SECRET_ID": _PEM_ID,
            },
            "conflicting",
        ),
    ],
)
def test_invalid_provider_matrix_rejected(
    values: dict[str, str], match: str
) -> None:
    """Reject every missing, unsupported, malformed, or conflicting config."""
    with pytest.raises(AppPrivateKeyConfigError, match=match):
        resolve_app_private_key_config(values)


def test_bws_and_file_configs_are_discriminated() -> None:
    """Resolve source-specific settings into a discriminated config."""
    bws = resolve_app_private_key_config(
        {
            "BH_GITHUB_APP_KEY_PROVIDER": "bws",
            "BWS_PEM_SECRET_ID": _PEM_ID,
        }
    )
    file = resolve_app_private_key_config(
        {
            "BH_GITHUB_APP_KEY_PROVIDER": "file",
            "BH_GITHUB_APP_PRIVATE_KEY_FILE": "/run/key.pem",
        }
    )
    assert bws.provider is AppPrivateKeyProvider.BWS
    assert bws.bws_secret_id == _PEM_ID
    assert bws.file_path is None
    assert file.provider is AppPrivateKeyProvider.FILE
    assert file.file_path == Path("/run/key.pem")
    assert file.bws_secret_id is None


@pytest.mark.parametrize(
    ("provider_values", "optional_values", "expected"),
    [
        (
            {
                "BH_GITHUB_APP_KEY_PROVIDER": "bws",
                "BWS_PEM_SECRET_ID": _PEM_ID,
            },
            {},
            True,
        ),
        (
            {
                "BH_GITHUB_APP_KEY_PROVIDER": "file",
                "BH_GITHUB_APP_PRIVATE_KEY_FILE": "/run/key.pem",
            },
            {},
            False,
        ),
        (
            {
                "BH_GITHUB_APP_KEY_PROVIDER": "file",
                "BH_GITHUB_APP_PRIVATE_KEY_FILE": "/run/key.pem",
            },
            {"BWS_GH_TOKEN_SECRET_ID": _OPTIONAL_ID},
            True,
        ),
        (
            {
                "BH_GITHUB_APP_KEY_PROVIDER": "file",
                "BH_GITHUB_APP_PRIVATE_KEY_FILE": "/run/key.pem",
            },
            {"BWS_HEARTBEAT_PING_URL_SECRET_ID": _OPTIONAL_ID},
            True,
        ),
    ],
)
def test_requires_bws_composes_provider_and_optional_consumers(
    provider_values: dict[str, str],
    optional_values: dict[str, str],
    expected: bool,
) -> None:
    """Require BWS for the provider or either optional BWS consumer."""
    config = resolve_app_private_key_config(provider_values)
    assert requires_bws(config, optional_values) is expected


def _file_config(path: Path) -> AppPrivateKeyConfig:
    """Resolve a file-provider configuration for a test path."""
    return resolve_app_private_key_config(
        {
            "BH_GITHUB_APP_KEY_PROVIDER": "file",
            "BH_GITHUB_APP_PRIVATE_KEY_FILE": str(path.resolve()),
        }
    )


def _file_stat(path: Path, mode: int) -> os.stat_result:
    """Return real file identity with an explicit POSIX permission mode."""
    values = list(os.stat(path))
    values[stat.ST_MODE] = stat.S_IFREG | mode
    return os.stat_result(values)


def test_bws_loader_fetches_once_with_explicit_access_token() -> None:
    """Fetch the selected BWS secret once with the explicit token."""
    fetch = Mock(return_value="private-key")
    config = resolve_app_private_key_config(
        {"BH_GITHUB_APP_KEY_PROVIDER": "bws", "BWS_PEM_SECRET_ID": _PEM_ID}
    )
    assert (
        load_app_private_key(
            config, bws_access_token="machine-token", fetch_secret=fetch
        )
        == "private-key"
    )
    fetch.assert_called_once_with(_PEM_ID, access_token="machine-token")


def test_bws_loader_rejects_empty_access_token_without_fetch() -> None:
    """Reject an empty access token before touching BWS."""
    fetch = Mock()
    config = resolve_app_private_key_config(
        {"BH_GITHUB_APP_KEY_PROVIDER": "bws", "BWS_PEM_SECRET_ID": _PEM_ID}
    )
    with pytest.raises(AppPrivateKeyLoadError, match="access token"):
        load_app_private_key(config, bws_access_token="", fetch_secret=fetch)
    fetch.assert_not_called()


def test_bws_loader_rejects_empty_secret() -> None:
    """Reject empty key material returned by BWS."""
    fetch = Mock(return_value="")
    config = resolve_app_private_key_config(
        {"BH_GITHUB_APP_KEY_PROVIDER": "bws", "BWS_PEM_SECRET_ID": _PEM_ID}
    )
    with pytest.raises(AppPrivateKeyLoadError, match="empty"):
        load_app_private_key(
            config, bws_access_token="machine-token", fetch_secret=fetch
        )


def test_bws_loader_hides_fetch_error_text_in_traceback() -> None:
    """Exclude raw BWS failure text from formatted load errors."""
    sentinel = "sentinel-bws-output"
    fetch = Mock(side_effect=RuntimeError(sentinel))
    config = resolve_app_private_key_config(
        {"BH_GITHUB_APP_KEY_PROVIDER": "bws", "BWS_PEM_SECRET_ID": _PEM_ID}
    )
    with pytest.raises(AppPrivateKeyLoadError) as exc_info:
        load_app_private_key(
            config, bws_access_token="machine-token", fetch_secret=fetch
        )
    rendered = "".join(traceback.format_exception(exc_info.value))
    assert sentinel not in rendered


def test_file_loader_reads_owner_only_regular_file_once(
    tmp_path: Path,
) -> None:
    """Read exact UTF-8 contents from an owner-only regular file."""
    key = tmp_path / "app.pem"
    key.write_text("private-key", encoding="utf-8")
    key.chmod(0o600)
    fetch = Mock()
    with patch(
        "baton_harness.chain.app_private_key.os.fstat",
        return_value=_file_stat(key, 0o600),
    ):
        assert (
            load_app_private_key(
                _file_config(key), bws_access_token="", fetch_secret=fetch
            )
            == "private-key"
        )
    fetch.assert_not_called()


def test_file_loader_never_calls_bws(tmp_path: Path) -> None:
    """Never invoke BWS while loading the file provider."""
    key = tmp_path / "app.pem"
    key.write_text("private-key", encoding="utf-8")
    key.chmod(0o600)
    fetch = Mock()
    with patch(
        "baton_harness.chain.app_private_key.os.fstat",
        return_value=_file_stat(key, 0o600),
    ):
        assert (
            load_app_private_key(
                _file_config(key),
                bws_access_token="unused",
                fetch_secret=fetch,
            )
            == "private-key"
        )
    fetch.assert_not_called()


def test_file_loader_rejects_missing_path(tmp_path: Path) -> None:
    """Normalize a missing file to an unavailable-provider error."""
    key = tmp_path / "missing.pem"
    with pytest.raises(AppPrivateKeyLoadError, match="unavailable"):
        load_app_private_key(
            _file_config(key), bws_access_token="", fetch_secret=Mock()
        )


def test_file_loader_rejects_symlink(tmp_path: Path) -> None:
    """Reject a symbolic link before opening its target."""
    target = tmp_path / "target.pem"
    target.write_text("private-key", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "app.pem"
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    config = resolve_app_private_key_config(
        {
            "BH_GITHUB_APP_KEY_PROVIDER": "file",
            "BH_GITHUB_APP_PRIVATE_KEY_FILE": str(link.absolute()),
        }
    )
    with pytest.raises(AppPrivateKeyLoadError, match="symbolic link"):
        load_app_private_key(config, bws_access_token="", fetch_secret=Mock())


def test_file_loader_rejects_directory(tmp_path: Path) -> None:
    """Reject an owner-only directory as a non-regular source."""
    directory = tmp_path / "key-dir"
    directory.mkdir()
    directory.chmod(0o700)
    with pytest.raises(AppPrivateKeyLoadError, match="regular file"):
        load_app_private_key(
            _file_config(directory), bws_access_token="", fetch_secret=Mock()
        )


@pytest.mark.parametrize("mode", [0o004, 0o020, 0o040, 0o077])
def test_file_loader_rejects_group_or_world_bits(
    tmp_path: Path, mode: int
) -> None:
    """Reject every tested group or world permission bit."""
    key = tmp_path / "app.pem"
    key.write_text("private-key", encoding="utf-8")
    key.chmod(0o600 | mode)
    with (
        patch(
            "baton_harness.chain.app_private_key.os.fstat",
            return_value=_file_stat(key, 0o600 | mode),
        ),
        pytest.raises(AppPrivateKeyLoadError, match="permissions"),
    ):
        load_app_private_key(
            _file_config(key), bws_access_token="", fetch_secret=Mock()
        )


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_file_loader_accepts_0400_and_0600(tmp_path: Path, mode: int) -> None:
    """Accept the two documented owner-only file modes."""
    key = tmp_path / "app.pem"
    key.write_text("private-key", encoding="utf-8")
    key.chmod(mode)
    with patch(
        "baton_harness.chain.app_private_key.os.fstat",
        return_value=_file_stat(key, mode),
    ):
        assert (
            load_app_private_key(
                _file_config(key), bws_access_token="", fetch_secret=Mock()
            )
            == "private-key"
        )


def test_file_loader_rejects_permission_error(tmp_path: Path) -> None:
    """Normalize permission failures without revealing path or OS text."""
    key = tmp_path / "secret-file-name.pem"
    key.write_text("private-key", encoding="utf-8")
    key.chmod(0o600)
    sentinel = "operating-system-secret-detail"
    with (
        patch("baton_harness.chain.app_private_key.os.open") as open_file,
        pytest.raises(AppPrivateKeyLoadError) as exc_info,
    ):
        open_file.side_effect = PermissionError(sentinel)
        load_app_private_key(
            _file_config(key), bws_access_token="", fetch_secret=Mock()
        )
    message = str(exc_info.value)
    assert str(key) not in message
    assert sentinel not in message
    rendered = "".join(traceback.format_exception(exc_info.value))
    assert str(key) not in rendered
    assert sentinel not in rendered


def test_file_loader_rejects_close_error(tmp_path: Path) -> None:
    """Normalize a descriptor close failure without leaking OS text."""
    key = tmp_path / "app.pem"
    key.write_text("private-key", encoding="utf-8")
    key.chmod(0o600)
    sentinel = "sentinel-close-error"
    real_close = os.close

    def close_then_fail(descriptor: int) -> None:
        """Close the descriptor and simulate a subsequent OS failure."""
        real_close(descriptor)
        raise OSError(sentinel)

    with (
        patch(
            "baton_harness.chain.app_private_key.os.fstat",
            return_value=_file_stat(key, 0o600),
        ),
        patch(
            "baton_harness.chain.app_private_key.os.close",
            side_effect=close_then_fail,
        ),
        pytest.raises(AppPrivateKeyLoadError) as exc_info,
    ):
        load_app_private_key(
            _file_config(key), bws_access_token="", fetch_secret=Mock()
        )
    rendered = "".join(traceback.format_exception(exc_info.value))
    assert sentinel not in rendered


def test_file_loader_rejects_replacement_race(tmp_path: Path) -> None:
    """Reject a pre-open/post-open identity change and close the descriptor."""
    key = tmp_path / "app.pem"
    key.write_text("private-key", encoding="utf-8")
    key.chmod(0o600)
    before = os.lstat(key)
    replaced_values = list(before)
    replaced_values[stat.ST_MODE] = stat.S_IFREG | 0o600
    replaced_values[stat.ST_INO] += 1
    replaced = os.stat_result(replaced_values)
    real_close = os.close
    close = Mock(wraps=real_close)
    with (
        patch(
            "baton_harness.chain.app_private_key.os.fstat",
            return_value=replaced,
        ),
        patch("baton_harness.chain.app_private_key.os.close", close),
        pytest.raises(AppPrivateKeyLoadError, match="changed"),
    ):
        load_app_private_key(
            _file_config(key), bws_access_token="", fetch_secret=Mock()
        )
    close.assert_called_once()
    assert isinstance(close.call_args.args[0], int)


def test_file_loader_rejects_more_than_one_mib(tmp_path: Path) -> None:
    """Reject content larger than the one-MiB input bound."""
    key = tmp_path / "app.pem"
    key.write_bytes(b"x" * (1024 * 1024 + 1))
    key.chmod(0o600)
    with (
        patch(
            "baton_harness.chain.app_private_key.os.fstat",
            return_value=_file_stat(key, 0o600),
        ),
        pytest.raises(AppPrivateKeyLoadError, match="maximum size"),
    ):
        load_app_private_key(
            _file_config(key), bws_access_token="", fetch_secret=Mock()
        )


def test_file_loader_rejects_empty_file(tmp_path: Path) -> None:
    """Reject an empty file."""
    key = tmp_path / "app.pem"
    key.write_bytes(b"")
    key.chmod(0o600)
    with (
        patch(
            "baton_harness.chain.app_private_key.os.fstat",
            return_value=_file_stat(key, 0o600),
        ),
        pytest.raises(AppPrivateKeyLoadError, match="empty"),
    ):
        load_app_private_key(
            _file_config(key), bws_access_token="", fetch_secret=Mock()
        )


def test_file_loader_rejects_non_utf8(tmp_path: Path) -> None:
    """Reject file content that is not UTF-8."""
    key = tmp_path / "app.pem"
    key.write_bytes(b"\xff\xfe")
    key.chmod(0o600)
    with (
        patch(
            "baton_harness.chain.app_private_key.os.fstat",
            return_value=_file_stat(key, 0o600),
        ),
        pytest.raises(AppPrivateKeyLoadError, match="UTF-8"),
    ):
        load_app_private_key(
            _file_config(key), bws_access_token="", fetch_secret=Mock()
        )


def test_requires_bws_cli_prints_only_boolean(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Print only the lowercase boolean for a valid shell query."""
    monkeypatch.setenv("BH_GITHUB_APP_KEY_PROVIDER", "file")
    monkeypatch.setenv(
        "BH_GITHUB_APP_PRIVATE_KEY_FILE", str((tmp_path / "key.pem").resolve())
    )
    monkeypatch.delenv("BWS_PEM_SECRET_ID", raising=False)
    monkeypatch.delenv("BWS_GH_TOKEN_SECRET_ID", raising=False)
    monkeypatch.delenv("BWS_HEARTBEAT_PING_URL_SECRET_ID", raising=False)
    assert main(["requires-bws"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "false\n"
    assert captured.err == ""


def test_requires_bws_cli_reports_safe_config_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Report invalid configuration without leaking sentinel values."""
    key_sentinel = "sentinel-private-key-path"
    secret_sentinel = "sentinel-secret-id"
    monkeypatch.setenv("BH_GITHUB_APP_KEY_PROVIDER", "unknown")
    monkeypatch.setenv("BH_GITHUB_APP_PRIVATE_KEY_FILE", key_sentinel)
    monkeypatch.setenv("BWS_PEM_SECRET_ID", secret_sentinel)
    assert main(["requires-bws"]) == 2
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert key_sentinel not in output
    assert secret_sentinel not in output
