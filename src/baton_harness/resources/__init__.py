"""Canonical runtime resources shipped inside the harness package."""

from __future__ import annotations

from contextlib import AbstractContextManager
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any, Protocol, cast

RESOURCE_NAMES = (
    "WORKFLOW.md",
    "ruleset.main.json",
    "ruleset.feature.json",
    "ruleset.compare-keys.json",
    "ruleset.compare-keys.app.json",
)

_PACKAGE = "baton_harness.resources"


class PackageResource(Protocol):
    """Operations required from an import-system package resource."""

    def is_file(self) -> bool:
        """Return whether this resource is a file."""

    def read_bytes(self) -> bytes:
        """Return the complete binary contents."""

    def read_text(self, encoding: str | None = None) -> str:
        """Return the complete decoded contents."""


class PackagedResourceError(RuntimeError):
    """Raised when a required packaged-resource invariant is broken."""


def resource(name: str) -> PackageResource:
    """Return one required resource from the fixed package manifest.

    Args:
        name: Basename from :data:`RESOURCE_NAMES`.

    Returns:
        The import-system resource handle.

    Raises:
        PackagedResourceError: If the name is unknown or the resource is
            absent from the installed package.
    """
    if name not in RESOURCE_NAMES:
        raise PackagedResourceError(f"unknown packaged resource: {name}")
    candidate = files(_PACKAGE).joinpath(name)
    if not candidate.is_file():
        raise PackagedResourceError(f"packaged resource missing: {name}")
    return candidate


def read_bytes(name: str) -> bytes:
    """Read a required resource as bytes.

    Args:
        name: Basename from :data:`RESOURCE_NAMES`.

    Returns:
        Exact resource bytes.
    """
    return resource(name).read_bytes()


def read_text(name: str) -> str:
    """Read a required UTF-8 resource as text.

    Args:
        name: Basename from :data:`RESOURCE_NAMES`.

    Returns:
        Decoded resource text.
    """
    return resource(name).read_text(encoding="utf-8")


def as_path(name: str) -> AbstractContextManager[Path]:
    """Expose a required resource as a scoped filesystem path.

    Args:
        name: Basename from :data:`RESOURCE_NAMES`.

    Returns:
        Context manager yielding a path valid for the context lifetime.
    """
    traversable = cast(Any, resource(name))
    return as_file(traversable)


def assert_config_mirrors(root: Path) -> None:
    """Require top-level compatibility mirrors to match canonical bytes.

    Args:
        root: Harness repository root containing ``config/``.

    Raises:
        PackagedResourceError: If a mirror is absent or has different bytes.
    """
    for name in RESOURCE_NAMES:
        mirror = root / "config" / name
        if not mirror.is_file():
            raise PackagedResourceError(f"missing config mirror: {mirror}")
        if mirror.read_bytes() != read_bytes(name):
            raise PackagedResourceError(f"config mirror drift: {mirror}")
