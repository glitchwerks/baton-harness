"""Resolve and securely load GitHub App private-key providers."""

from __future__ import annotations

import os
import re
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

_UUID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-"
    r"[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{12}$"
)
_MAX_PRIVATE_KEY_BYTES = 1024 * 1024
_OPTIONAL_BWS_SECRET_IDS = (
    "BWS_GH_TOKEN_SECRET_ID",
    "BWS_HEARTBEAT_PING_URL_SECRET_ID",
)


class AppPrivateKeyProvider(str, Enum):
    """Supported GitHub App private-key providers."""

    BWS = "bws"
    FILE = "file"


@dataclass(frozen=True)
class AppPrivateKeyConfig:
    """Validated configuration for one private-key provider.

    Attributes:
        provider: Selected private-key provider.
        bws_secret_id: Selected BWS secret UUID, if applicable.
        file_path: Selected absolute file path, if applicable.
    """

    provider: AppPrivateKeyProvider
    bws_secret_id: str | None = None
    file_path: Path | None = None


class AppPrivateKeyConfigError(RuntimeError):
    """Raised when private-key provider configuration is invalid."""


class AppPrivateKeyLoadError(RuntimeError):
    """Raised when selected private-key material cannot be loaded safely."""


class SecretFetcher(Protocol):
    """Callable contract for retrieving a BWS secret."""

    def __call__(self, secret_id: str, *, access_token: str) -> str:
        """Fetch one secret using an explicit BWS access token.

        Args:
            secret_id: UUID of the secret to retrieve.
            access_token: BWS machine-account access token.

        Returns:
            The fetched secret value.
        """
        raise NotImplementedError


def resolve_app_private_key_config(
    values: Mapping[str, str],
) -> AppPrivateKeyConfig:
    """Resolve and validate the GitHub App private-key provider matrix.

    Args:
        values: Configuration values to validate without side effects.

    Returns:
        A source-discriminated immutable provider configuration.

    Raises:
        AppPrivateKeyConfigError: If the provider is missing, unsupported,
            malformed, incomplete, or conflicts with the unselected source.
    """
    provider_value = values.get("BH_GITHUB_APP_KEY_PROVIDER", "")
    if not provider_value:
        raise AppPrivateKeyConfigError(
            "BH_GITHUB_APP_KEY_PROVIDER is required"
        )

    try:
        provider = AppPrivateKeyProvider(provider_value)
    except ValueError:
        raise AppPrivateKeyConfigError(
            "BH_GITHUB_APP_KEY_PROVIDER is unsupported"
        ) from None

    bws_secret_id = values.get("BWS_PEM_SECRET_ID", "")
    file_value = values.get("BH_GITHUB_APP_PRIVATE_KEY_FILE", "")

    if provider is AppPrivateKeyProvider.BWS:
        if file_value:
            raise AppPrivateKeyConfigError(
                "conflicting BH_GITHUB_APP_PRIVATE_KEY_FILE for bws provider"
            )
        if not bws_secret_id or _UUID_RE.fullmatch(bws_secret_id) is None:
            raise AppPrivateKeyConfigError(
                "BWS_PEM_SECRET_ID must be a valid UUID for bws provider"
            )
        return AppPrivateKeyConfig(
            provider=provider,
            bws_secret_id=bws_secret_id,
        )

    if bws_secret_id:
        raise AppPrivateKeyConfigError(
            "conflicting BWS_PEM_SECRET_ID for file provider"
        )
    if not file_value:
        raise AppPrivateKeyConfigError(
            "BH_GITHUB_APP_PRIVATE_KEY_FILE is required for file provider"
        )
    file_path = Path(file_value)
    if not file_path.is_absolute():
        raise AppPrivateKeyConfigError(
            "BH_GITHUB_APP_PRIVATE_KEY_FILE must be absolute"
        )
    return AppPrivateKeyConfig(
        provider=provider,
        file_path=file_path,
    )


def requires_bws(
    config: AppPrivateKeyConfig,
    values: Mapping[str, str],
) -> bool:
    """Return whether the selected configuration has any BWS consumer.

    Args:
        config: Resolved App private-key provider configuration.
        values: Values containing optional BWS secret locators.

    Returns:
        True for the BWS provider or either configured optional consumer.
    """
    return config.provider is AppPrivateKeyProvider.BWS or any(
        values.get(key, "") for key in _OPTIONAL_BWS_SECRET_IDS
    )


def _meaningful_identity(metadata: os.stat_result) -> tuple[int, int] | None:
    """Return a comparable device/inode pair when both values are nonzero.

    Args:
        metadata: File metadata whose identity should be inspected.

    Returns:
        A device/inode pair, or None if the platform lacks meaningful values.
    """
    identity = (metadata.st_dev, metadata.st_ino)
    return identity if all(identity) else None


def _read_file_private_key(path: Path) -> str:
    """Read an App private key through one securely validated descriptor.

    Args:
        path: Absolute path selected by the validated provider config.

    Returns:
        Exact UTF-8 text read from the file.

    Raises:
        AppPrivateKeyLoadError: If the file is unavailable, unsafe, changed,
            oversized, empty, or not valid UTF-8.
    """
    try:
        before = os.lstat(path)
    except OSError:
        raise AppPrivateKeyLoadError(
            "file provider private key is unavailable"
        ) from None

    if stat.S_ISLNK(before.st_mode):
        raise AppPrivateKeyLoadError(
            "file provider private key cannot be a symbolic link"
        )
    if not stat.S_ISREG(before.st_mode):
        raise AppPrivateKeyLoadError(
            "file provider private key must be a regular file"
        )

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    # A FIFO replacement must reach fstat without waiting for a writer.
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise AppPrivateKeyLoadError(
            "file provider private key is unavailable"
        ) from None

    try:
        try:
            opened = os.fstat(descriptor)
        except OSError:
            raise AppPrivateKeyLoadError(
                "file provider private key metadata is unavailable"
            ) from None

        if not stat.S_ISREG(opened.st_mode):
            raise AppPrivateKeyLoadError(
                "file provider private key must be a regular file"
            )
        if opened.st_mode & 0o077:
            raise AppPrivateKeyLoadError(
                "file provider private key has unsafe permissions"
            )

        before_identity = _meaningful_identity(before)
        opened_identity = _meaningful_identity(opened)
        if (
            before_identity is not None
            and opened_identity is not None
            and before_identity != opened_identity
        ):
            raise AppPrivateKeyLoadError(
                "file provider private key changed while opening"
            )

        content = bytearray()
        try:
            while len(content) <= _MAX_PRIVATE_KEY_BYTES:
                chunk = os.read(
                    descriptor,
                    _MAX_PRIVATE_KEY_BYTES + 1 - len(content),
                )
                if not chunk:
                    break
                content.extend(chunk)
        except OSError:
            raise AppPrivateKeyLoadError(
                "file provider private key could not be read"
            ) from None

        if len(content) > _MAX_PRIVATE_KEY_BYTES:
            raise AppPrivateKeyLoadError(
                "file provider private key exceeds maximum size"
            )
    finally:
        try:
            os.close(descriptor)
        except OSError:
            raise AppPrivateKeyLoadError(
                "file provider private key descriptor could not be closed"
            ) from None

    if not content:
        raise AppPrivateKeyLoadError("file provider private key is empty")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        raise AppPrivateKeyLoadError(
            "file provider private key is not valid UTF-8"
        ) from None


def load_app_private_key(
    config: AppPrivateKeyConfig,
    *,
    bws_access_token: str,
    fetch_secret: SecretFetcher,
) -> str:
    """Load private-key material from exactly one selected provider.

    Args:
        config: Validated provider configuration.
        bws_access_token: Explicit BWS access token for the BWS provider.
        fetch_secret: Injected BWS secret fetcher.

    Returns:
        Exact private-key text from the selected provider.

    Raises:
        AppPrivateKeyLoadError: If the selected source cannot safely return
            non-empty key material.
    """
    if config.provider is AppPrivateKeyProvider.FILE:
        if config.file_path is None:
            raise AppPrivateKeyLoadError(
                "file provider private key path is unavailable"
            )
        return _read_file_private_key(config.file_path)

    if not bws_access_token:
        raise AppPrivateKeyLoadError(
            "bws provider requires a non-empty access token"
        )
    if config.bws_secret_id is None:
        raise AppPrivateKeyLoadError(
            "bws provider private key configuration is unavailable"
        )
    try:
        private_key = fetch_secret(
            config.bws_secret_id,
            access_token=bws_access_token,
        )
    except Exception:
        raise AppPrivateKeyLoadError(
            "bws provider private key could not be fetched"
        ) from None
    if not private_key.strip():
        raise AppPrivateKeyLoadError("bws provider private key is empty")
    return private_key


def main(argv: list[str]) -> int:
    """Run the secret-safe provider policy shell query.

    Args:
        argv: Command arguments excluding the executable name.

    Returns:
        Zero for a successful query, or two for usage/configuration errors.
    """
    if argv != ["requires-bws"]:
        print("app_private_key: usage: requires-bws", file=sys.stderr)
        return 2

    try:
        config = resolve_app_private_key_config(os.environ)
    except AppPrivateKeyConfigError as exc:
        print(f"app_private_key: {exc}", file=sys.stderr)
        return 2

    print("true" if requires_bws(config, os.environ) else "false")
    return 0


if __name__ == "__main__":  # pragma: no cover - module execution seam
    raise SystemExit(main(sys.argv[1:]))
