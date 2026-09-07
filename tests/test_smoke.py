"""Smoke tests — verify the package installs and exposes its public API.

These tests intentionally contain no logic: if they pass, the package is
importable and the declared version is present.
"""

import pytest

import codereeve

pytestmark = pytest.mark.fast


def test_version_is_string() -> None:
    """__version__ must be a non-empty string."""
    assert isinstance(codereeve.__version__, str)
    assert codereeve.__version__


def test_version_matches_pyproject() -> None:
    """__version__ must match the version declared in pyproject.toml.

    Uses importlib.metadata so the test stays DRY (no hard-coding the
    version string in two places).
    """
    import importlib.metadata

    assert codereeve.__version__ == importlib.metadata.version(
        "codereeve"
    )
