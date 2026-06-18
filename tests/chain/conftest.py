"""Shared pytest fixtures for the chain test package.

Autouse fixtures here apply to all tests in tests/chain/.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _auto_patch_reconcile_startup() -> None:  # type: ignore[return]
    """No-op reconcile_startup for daemon tests that don't test it directly.

    Pre-#40 daemon tests call run_daemon without patching reconcile_startup.
    Without this fixture those tests would fail with SystemExit(1) because
    validate_github_token raises TokenValidationError in environments without
    a valid GH_TOKEN.

    Tests that DO exercise reconcile_startup (test_reconcile.py and the two
    #40 daemon tests) override this fixture with their own explicit patches,
    which take precedence as the innermost patch in the mock stack.
    """
    with patch(
        "baton_harness.chain.daemon.reconcile_startup",
        new=AsyncMock(return_value=None),
    ):
        yield
