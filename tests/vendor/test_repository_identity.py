"""Repository identity contracts for the vendored Symphony runtime."""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, patch

import pytest

from codereeve.vendor.symphony.orchestrator import Orchestrator
from codereeve.vendor.symphony.tracker import GitHubTracker
from codereeve.vendor.symphony.workspace import WorkspaceManager


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Canonical Worker Branch", "codereeve/canonical-worker-branch-42"),
        ("!!!", "codereeve/issue-42"),
    ],
)
def test_new_worker_branches_use_codereeve_prefix(
    title: str, expected: str
) -> None:
    """New worktrees use canonical branch names, including empty slugs."""
    manager = WorkspaceManager("/repo")

    assert manager.branch_name(42, title) == expected


@pytest.mark.parametrize(
    "head",
    [
        "codereeve/canonical-worker-42",
        pytest.param(
            "baton/legacy-worker-42",
            id="legacy-baton-worker-branch",
        ),
    ],
)
def test_tracker_recognizes_canonical_and_legacy_worker_prs(head: str) -> None:
    """PR lookup accepts both supported worker-branch identities."""
    response = json.dumps(
        [{"number": 1, "title": "", "body": "", "headRefName": head}]
    )

    with patch(
        "codereeve.vendor.symphony.tracker.run_gh",
        new=AsyncMock(return_value=response),
    ):
        found = asyncio.run(GitHubTracker().check_pr_exists(42))

    assert found is True


@pytest.mark.parametrize(
    "head",
    [
        "feature/canonical-worker-42",
        "codereeve/canonical-worker-420",
        "codereeve/canonical-worker-41",
    ],
)
def test_tracker_rejects_unrelated_worker_prs(head: str) -> None:
    """PR lookup keeps prefix and exact issue-suffix boundaries."""
    response = json.dumps(
        [{"number": 1, "title": "", "body": "", "headRefName": head}]
    )

    with patch(
        "codereeve.vendor.symphony.tracker.run_gh",
        new=AsyncMock(return_value=response),
    ):
        found = asyncio.run(GitHubTracker().check_pr_exists(42))

    assert found is False


def test_orchestrator_uses_codereeve_product_name_in_lifecycle_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Active lifecycle logs identify CodeReeve and retain Symphony."""
    orchestrator = object.__new__(Orchestrator)
    orchestrator.config = type(
        "Config", (), {"poll_interval_ms": 1, "max_concurrent": 1}
    )()
    orchestrator._stop_event = asyncio.Event()
    orchestrator._stop_event.set()
    orchestrator._running_tasks = {}

    with caplog.at_level(logging.INFO, logger="symphony"):
        asyncio.run(orchestrator.run())

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "CodeReeve Symphony starting" in message for message in messages
    )
    assert any("CodeReeve Symphony stopped" in message for message in messages)
