"""Canonical CodeReeve package identity tests."""

from importlib import metadata

import codereeve
from codereeve.resources import RESOURCE_NAMES, read_bytes
from codereeve.vendor.symphony.orchestrator import Orchestrator


def test_canonical_distribution_and_package_versions_match() -> None:
    """Package version matches the canonical distribution metadata."""
    assert codereeve.__version__ == metadata.version("codereeve")


def test_canonical_resources_are_packaged() -> None:
    """Every declared canonical package resource has content."""
    assert RESOURCE_NAMES
    assert all(read_bytes(name) for name in RESOURCE_NAMES)


def test_symphony_remains_vendored_under_canonical_package() -> None:
    """Vendored Symphony modules live below the canonical namespace."""
    assert Orchestrator.__module__.startswith("codereeve.vendor.symphony")
