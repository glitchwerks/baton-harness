"""Canonical CodeReeve paths and fail-closed compatibility selection."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class PathConflictError(RuntimeError):
    """Raised when compatibility paths are ambiguous or unsafe."""


@dataclass(frozen=True)
class SelectedPath:
    """A selected canonical or temporary legacy filesystem location.

    Attributes:
        path: The selected location. The selector never creates it.
        uses_legacy: Whether ``path`` is the temporary legacy location.
    """

    path: Path
    uses_legacy: bool


@dataclass(frozen=True)
class PathLayout:
    """All canonical and compatibility paths for one CodeReeve environment.

    Attributes:
        canonical_state: CodeReeve-owned managed-repository state directory.
        canonical_config: Canonical managed-repository configuration file.
        canonical_host: Canonical per-host configuration file.
        canonical_secrets: Canonical system secrets file.
        canonical_unit: Canonical systemd service unit path.
        legacy_state: Temporary Baton Harness state directory.
        legacy_config: Temporary Baton Harness configuration file.
        legacy_host: Temporary Baton Harness per-host configuration file.
        legacy_secrets: Temporary Baton Harness system secrets file.
        legacy_unit: Temporary Baton Harness systemd service unit path.
        symphony_state: Symphony-owned state, intentionally not migrated.
    """

    canonical_state: Path
    canonical_config: Path
    canonical_host: Path
    canonical_secrets: Path
    canonical_unit: Path
    legacy_state: Path
    legacy_config: Path
    legacy_host: Path
    legacy_secrets: Path
    legacy_unit: Path
    symphony_state: Path

    @classmethod
    def for_environment(
        cls,
        project_root: Path,
        env: Mapping[str, str],
        *,
        home: Path | None = None,
        etc_root: Path = Path("/etc"),
    ) -> PathLayout:
        """Build a layout from project, environment, and platform roots.

        Args:
            project_root: Root of the managed repository.
            env: Environment snapshot containing an optional XDG config root.
            home: Effective home directory, defaulting to ``Path.home()``.
            etc_root: System configuration root, injectable for tests.

        Returns:
            Immutable canonical and compatibility filesystem locations.
        """
        effective_home = (
            home if home is not None else Path(os.path.expanduser("~"))
        )
        xdg_config_home = env.get("XDG_CONFIG_HOME")
        config_home = (
            Path(xdg_config_home)
            if xdg_config_home
            else effective_home / ".config"
        )
        canonical_state = project_root / ".codereeve"
        return cls(
            canonical_state=canonical_state,
            canonical_config=canonical_state / "config.env",
            canonical_host=config_home / "codereeve" / "host.env",
            canonical_secrets=etc_root / "codereeve" / "secrets.env",
            canonical_unit=(
                etc_root / "systemd" / "system" / "codereeve.service"
            ),
            legacy_state=project_root / ".baton-harness",
            legacy_config=project_root / ".bh" / "config.env",
            legacy_host=(config_home / "baton-harness" / "host.env"),
            legacy_secrets=etc_root / "bh-daemon" / "secrets.env",
            legacy_unit=(
                etc_root / "systemd" / "system" / "bh-daemon.service"
            ),
            symphony_state=project_root / ".symphony",
        )


def select_compatible_file(
    canonical: Path, legacy: Path, *, label: str
) -> SelectedPath:
    """Select one regular compatibility file without following symlinks.

    Args:
        canonical: Canonical file location.
        legacy: Temporary compatibility file location.
        label: Safe human-readable resource type for diagnostics.

    Returns:
        The canonical path if neither exists, otherwise the sole safe path.

    Raises:
        PathConflictError: If both paths exist or either is unsafe.
    """
    return _select_compatible_path(
        canonical, legacy, label=label, expected_mode=stat.S_IFREG
    )


def validate_safe_file_path(path: Path, *, label: str) -> bool:
    """Validate a file path and its existing ancestors without following links.

    Args:
        path: File path to inspect without opening it.
        label: Safe human-readable resource type for diagnostics.

    Returns:
        Whether the final path exists as a regular file.

    Raises:
        PathConflictError: If the final path or an existing ancestor is unsafe.
    """
    return _is_safe_path(path, label, stat.S_IFREG)


def select_compatible_directory(
    canonical: Path, legacy: Path, *, label: str
) -> SelectedPath:
    """Select one directory compatibility path without following symlinks.

    Args:
        canonical: Canonical directory location.
        legacy: Temporary compatibility directory location.
        label: Safe human-readable resource type for diagnostics.

    Returns:
        The canonical path if neither exists, otherwise the sole safe path.

    Raises:
        PathConflictError: If both paths exist or either is unsafe.
    """
    return _select_compatible_path(
        canonical, legacy, label=label, expected_mode=stat.S_IFDIR
    )


def _select_compatible_path(
    canonical: Path,
    legacy: Path,
    *,
    label: str,
    expected_mode: int,
) -> SelectedPath:
    """Select one safe path of the requested type without following links."""
    canonical_exists = _is_safe_path(canonical, label, expected_mode)
    legacy_exists = _is_safe_path(legacy, label, expected_mode)
    if canonical_exists and legacy_exists:
        raise PathConflictError(
            f"ambiguous {label}: both {canonical} and {legacy} exist"
        )
    if legacy_exists:
        return SelectedPath(legacy, uses_legacy=True)
    return SelectedPath(canonical, uses_legacy=False)


def _is_safe_path(path: Path, label: str, expected_mode: int) -> bool:
    """Return whether a path exists and is a safe expected filesystem type."""
    _validate_existing_ancestors(path, label)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PathConflictError(
            f"unable to inspect {label} path {path}: {type(exc).__name__}"
        ) from exc
    if stat.S_IFMT(mode) != expected_mode:
        raise PathConflictError(f"unsafe {label} path: {path}")
    return True


def _validate_existing_ancestors(path: Path, label: str) -> None:
    """Reject linked or non-directory ancestors without resolving ``path``."""
    for ancestor in path.parents:
        try:
            mode = ancestor.lstat().st_mode
        except FileNotFoundError:
            break
        except OSError as exc:
            raise PathConflictError(
                "unable to inspect "
                f"{label} ancestor {ancestor}: {type(exc).__name__}"
            ) from exc
        if not stat.S_ISDIR(mode):
            raise PathConflictError(f"unsafe {label} ancestor: {ancestor}")
