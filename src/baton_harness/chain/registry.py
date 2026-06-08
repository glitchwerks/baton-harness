"""Single-entry repo registry for the always-on daemon.

Holds the configuration for the one repository the daemon manages in v1.
The daemon poll loop iterates this registry; adding a second repo is a
matter of appending a second ``RepoConfig`` entry (the multi-repo seam —
``harness-design.md §10``).

v1 YAGNI policy: only one repo is registered.  No multi-repo machinery
(no dynamic discovery, no concurrent work-unit dispatchers per repo).
The ``max_concurrent`` concurrency budget is documented in
``config/WORKFLOW.md``; in v1 it is effectively 1.

Usage::

    registry = load_registry()
    for repo_cfg in registry:
        # ... poll for ready work units
        pass
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RepoConfig:
    """Configuration for a single managed repository.

    Attributes:
        owner: The GitHub repository owner (organisation or user login).
        repo: The repository name (without the owner prefix).
        project_root: Absolute path to the local clone of the repository.
            The daemon uses this as the working directory for ``git`` and
            ``gh`` commands and as the root for symphony worktrees.
    """

    owner: str
    repo: str
    project_root: Path


def load_registry() -> list[RepoConfig]:
    """Load the v1 single-entry repo registry.

    In v1 the registry is hard-coded to one entry derived from environment
    variables and conventions.  The function signature accepts no arguments
    so the daemon can call it without knowing the registry source; future
    versions may read from a config file or environment.

    The registry is a plain list so the daemon can iterate it with a
    ``for repo_cfg in load_registry()`` loop — the multi-repo extension
    (v2) just appends entries here without touching the daemon loop.

    Returns:
        A list containing exactly one ``RepoConfig`` for the single managed
        repository.  Never returns an empty list in v1.

    Raises:
        ValueError: If the required environment variables are not set and
            no default can be determined.
    """
    import os

    owner = os.environ.get("BH_REPO_OWNER", "")
    repo = os.environ.get("BH_REPO_NAME", "")
    root_str = os.environ.get("BH_PROJECT_ROOT", "")

    if not owner or not repo or not root_str:
        raise ValueError(
            "Registry is not configured.  Set BH_REPO_OWNER, BH_REPO_NAME,"
            " and BH_PROJECT_ROOT environment variables."
        )

    return [
        RepoConfig(
            owner=owner,
            repo=repo,
            project_root=Path(root_str),
        )
    ]
