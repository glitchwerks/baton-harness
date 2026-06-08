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
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass
class WorktreeResult:
    path: str
    created_now: bool


async def run_cmd(args: list[str], cwd: str | None = None) -> str:
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
            f"{' '.join(args)} failed (rc={proc.returncode}): {stderr.decode().strip()}",
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
    def __init__(self, project_root: str, symphony_dir: str | None = None):
        self.project_root = os.path.abspath(project_root)
        self.symphony_dir = symphony_dir or os.path.join(self.project_root, ".symphony")
        self.worktrees_dir = os.path.join(self.symphony_dir, "worktrees")

    def worktree_path(self, issue_number: int) -> str:
        sanitized = str(issue_number)
        path = os.path.join(self.worktrees_dir, sanitized)
        # Safety: must be under symphony_dir
        abs_path = os.path.abspath(path)
        if not abs_path.startswith(os.path.abspath(self.symphony_dir)):
            raise WorkspaceError("path_escape", f"Worktree path {abs_path} escapes symphony dir")
        return abs_path

    def branch_name(self, issue_number: int, title: str) -> str:
        slug = slugify(title)
        if slug:
            return f"baton/{slug}-{issue_number}"
        return f"baton/issue-{issue_number}"

    async def ensure_worktree(self, issue_number: int, title: str = "") -> WorktreeResult:
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
                raise WorkspaceError("worktree_create_failed", str(e))

        log.info(f"workspace: created worktree at {path}")
        return WorktreeResult(path=path, created_now=True)

    async def cleanup_worktree(self, issue_number: int) -> None:
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
