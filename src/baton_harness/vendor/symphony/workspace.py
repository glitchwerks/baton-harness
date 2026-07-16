"""symphony/workspace.py — Git worktree lifecycle manager."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from dataclasses import dataclass

log = logging.getLogger("symphony")


class WorkspaceError(Exception):
    """Raised when a git worktree operation fails.

    Attributes:
        code: Short machine-readable error code (e.g. ``"path_escape"``).
    """

    def __init__(self, code: str, message: str) -> None:
        """Initialize the error with a machine-readable code and message.

        Args:
            code: Short machine-readable error code.
            message: Human-readable description of the failure.
        """
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass
class WorktreeResult:
    """The outcome of ensuring a worktree exists for an issue.

    Attributes:
        path: Absolute filesystem path to the worktree.
        created_now: True if this call created the worktree; False if an
            existing worktree was reused.
    """

    path: str
    created_now: bool


async def run_cmd(args: list[str], cwd: str | None = None) -> str:
    """Run a subprocess to completion and return its captured stdout.

    Args:
        args: The command and its arguments to execute.
        cwd: Working directory to run the command in, or None to use the
            current process's working directory.

    Returns:
        The subprocess's decoded stdout.

    Raises:
        WorkspaceError: If the subprocess exits with a non-zero return
            code.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise WorkspaceError(
            "command_failed",
            f"{' '.join(args)} failed (rc={proc.returncode}): "
            f"{stderr.decode().strip()}",
        )
    return stdout.decode()


def slugify(title: str, max_len: int = 50) -> str:
    """Convert an issue title to a git-safe branch slug."""
    slug = title.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:max_len].rstrip("-")


class WorkspaceManager:
    """Creates and tears down per-issue git worktrees under symphony_dir.

    Attributes:
        project_root: Absolute path to the main git checkout.
        symphony_dir: Absolute path to the directory holding symphony's
            working state (worktrees, etc.).
        worktrees_dir: Absolute path to the directory holding per-issue
            worktrees.
    """

    def __init__(
        self, project_root: str, symphony_dir: str | None = None
    ) -> None:
        """Initialize the manager for the given project.

        Args:
            project_root: Path to the main git checkout.
            symphony_dir: Directory to hold symphony's working state, or
                None to default to ``<project_root>/.symphony``.
        """
        self.project_root = os.path.abspath(project_root)
        # VENDOR-PATCH VP-9: normalize caller-provided symphony_dir to an
        # absolute path.
        self.symphony_dir = os.path.abspath(
            symphony_dir or os.path.join(self.project_root, ".symphony")
        )
        self.worktrees_dir = os.path.join(self.symphony_dir, "worktrees")

    def worktree_path(self, issue_number: int) -> str:
        """Compute the worktree path for *issue_number*.

        Args:
            issue_number: The GitHub issue number.

        Returns:
            The absolute worktree path.

        Raises:
            WorkspaceError: If the computed path would escape
                ``symphony_dir``.
        """
        sanitized = str(issue_number)
        path = os.path.join(self.worktrees_dir, sanitized)
        # Safety: must be under symphony_dir
        abs_path = os.path.abspath(path)
        if not abs_path.startswith(os.path.abspath(self.symphony_dir)):
            raise WorkspaceError(
                "path_escape", f"Worktree path {abs_path} escapes symphony dir"
            )
        return abs_path

    def branch_name(self, issue_number: int, title: str) -> str:
        """Compute the git branch name to use for *issue_number*.

        Args:
            issue_number: The GitHub issue number.
            title: The issue title, used to derive a readable slug.

        Returns:
            A branch name of the form ``baton/<slug>-<issue_number>``, or
            ``baton/issue-<issue_number>`` if the title yields no slug.
        """
        slug = slugify(title)
        if slug:
            return f"baton/{slug}-{issue_number}"
        return f"baton/issue-{issue_number}"

    async def ensure_worktree(
        self, issue_number: int, title: str = ""
    ) -> WorktreeResult:
        """Ensure a worktree exists for *issue_number*, creating it if needed.

        Args:
            issue_number: The GitHub issue number to create a worktree
                for.
            title: The issue title, used to derive the branch name for a
                newly created worktree.

        Returns:
            A ``WorktreeResult`` describing the worktree path and
            whether it was newly created.

        Raises:
            WorkspaceError: If worktree creation fails on both the
                new-branch and existing-branch code paths.
        """
        path = self.worktree_path(issue_number)

        if os.path.isdir(path):
            log.info(f"workspace: reusing worktree at {path}")
            return WorktreeResult(path=path, created_now=False)

        os.makedirs(self.worktrees_dir, exist_ok=True)
        branch = self.branch_name(issue_number, title)

        try:
            await run_cmd(
                ["git", "worktree", "add", "-b", branch, path, "HEAD"],
                cwd=self.project_root,
            )
        except WorkspaceError:
            # Branch may already exist from a previous run
            try:
                await run_cmd(
                    ["git", "worktree", "add", path, branch],
                    cwd=self.project_root,
                )
            except WorkspaceError as e:
                raise WorkspaceError("worktree_create_failed", str(e)) from e

        log.info(f"workspace: created worktree at {path}")
        return WorktreeResult(path=path, created_now=True)

    async def cleanup_worktree(self, issue_number: int) -> None:
        """Remove the worktree for *issue_number*, if one exists.

        Args:
            issue_number: The GitHub issue number whose worktree should
                be removed.
        """
        path = self.worktree_path(issue_number)
        if not os.path.exists(path):
            return

        try:
            await run_cmd(
                ["git", "worktree", "remove", "--force", path],
                cwd=self.project_root,
            )
        except WorkspaceError:
            pass

        # Ensure directory is gone even if git worktree remove didn't clean it
        if os.path.exists(path):
            shutil.rmtree(path, ignore_errors=True)

        log.info(f"workspace: cleaned up worktree at {path}")
