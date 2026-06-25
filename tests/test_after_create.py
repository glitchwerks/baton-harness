"""Unit tests for baton_harness.after_create — per-worktree dependency setup.

All subprocess calls are intercepted by monkeypatching the module-local
``_run`` helper so no real ``npm``/``pip``/``uv`` processes are spawned.
The filesystem is faked via ``tmp_path`` so real ``package.json`` etc. are
never required on the test host.

Coverage:
- npm ci path (package.json + package-lock.json present)
- npm install path (package.json without package-lock.json)
- requirements.txt path with uv available
- requirements.txt path without uv (plain pip fallback)
- pyproject.toml with [dev] extra succeeds on first try
- pyproject.toml without [dev] extra falls back to ``-e .``
- no recognised project files → informative no-op, exit 0
- install command failure → non-zero exit
- unresolvable issue number → exit 1
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import baton_harness.after_create as after_create_mod
from baton_harness.after_create import main

# ---------------------------------------------------------------------------
# Module-level fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_claude_settings_for_legacy_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub out the C4 BH_VENV gate so pre-3b tests stay focused.

    The new ``_write_claude_settings_if_configured`` call added in slice 3b
    fatally exits when ``BH_VENV`` is unset.  These tests were written before
    that contract existed and are not about settings-write behaviour; mocking
    the function out keeps them exercising only the dep-install paths they were
    designed for, and avoids false failures on clean CI environments where
    ``BH_VENV`` is not exported.
    """
    monkeypatch.setattr(
        after_create_mod,
        "_write_claude_settings_if_configured",
        lambda **_: 0,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok() -> subprocess.CompletedProcess[str]:
    """Return a CompletedProcess that signals success."""
    return subprocess.CompletedProcess(args=[], returncode=0)


def _fail() -> subprocess.CompletedProcess[str]:
    """Return a CompletedProcess that signals failure."""
    return subprocess.CompletedProcess(args=[], returncode=1)


# ---------------------------------------------------------------------------
# npm paths
# ---------------------------------------------------------------------------


class TestAfterCreateNpm:
    """Tests for npm dependency installation."""

    def test_npm_ci_when_lockfile_present(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``npm ci`` is used when package-lock.json is present."""
        worktree = tmp_path / "feat-2-test"
        worktree.mkdir()
        (worktree / "package.json").write_text("{}", encoding="utf-8")
        (worktree / "package-lock.json").write_text("{}", encoding="utf-8")
        monkeypatch.chdir(worktree)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _ok()

        monkeypatch.setattr(after_create_mod, "_run", fake_run)

        result = main()

        assert result == 0
        assert calls == [["npm", "ci"]]

    def test_npm_install_without_lockfile(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``npm install`` is used when there is no package-lock.json."""
        worktree = tmp_path / "feat-2-test"
        worktree.mkdir()
        (worktree / "package.json").write_text("{}", encoding="utf-8")
        monkeypatch.chdir(worktree)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _ok()

        monkeypatch.setattr(after_create_mod, "_run", fake_run)

        result = main()

        assert result == 0
        assert calls == [["npm", "install"]]

    def test_npm_failure_returns_nonzero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failing npm command causes main to return non-zero."""
        worktree = tmp_path / "feat-2-test"
        worktree.mkdir()
        (worktree / "package.json").write_text("{}", encoding="utf-8")
        monkeypatch.chdir(worktree)

        monkeypatch.setattr(after_create_mod, "_run", lambda _cmd: _fail())

        result = main()

        assert result != 0


# ---------------------------------------------------------------------------
# requirements.txt paths
# ---------------------------------------------------------------------------


class TestAfterCreateRequirements:
    """Tests for requirements.txt dependency installation."""

    def test_uv_pip_install_when_uv_available(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``uv pip install`` is used when uv is on PATH."""
        worktree = tmp_path / "fix-3-test"
        worktree.mkdir()
        (worktree / "requirements.txt").write_text(
            "pytest\n", encoding="utf-8"
        )
        monkeypatch.chdir(worktree)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _ok()

        monkeypatch.setattr(after_create_mod, "_run", fake_run)

        with patch("shutil.which", return_value="/usr/bin/uv"):
            result = main()

        assert result == 0
        assert calls == [["uv", "pip", "install", "-r", "requirements.txt"]]

    def test_pip_fallback_when_uv_not_available(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``pip install`` is used when uv is not on PATH."""
        worktree = tmp_path / "fix-3-test"
        worktree.mkdir()
        (worktree / "requirements.txt").write_text(
            "pytest\n", encoding="utf-8"
        )
        monkeypatch.chdir(worktree)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _ok()

        monkeypatch.setattr(after_create_mod, "_run", fake_run)

        with patch("shutil.which", return_value=None):
            result = main()

        assert result == 0
        assert calls == [["pip", "install", "-r", "requirements.txt"]]

    def test_requirements_failure_returns_nonzero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failing pip/uv command causes main to return non-zero."""
        worktree = tmp_path / "fix-3-test"
        worktree.mkdir()
        (worktree / "requirements.txt").write_text(
            "pytest\n", encoding="utf-8"
        )
        monkeypatch.chdir(worktree)

        monkeypatch.setattr(after_create_mod, "_run", lambda _cmd: _fail())

        with patch("shutil.which", return_value=None):
            result = main()

        assert result != 0


# ---------------------------------------------------------------------------
# pyproject.toml paths
# ---------------------------------------------------------------------------


class TestAfterCreatePyproject:
    """Tests for pyproject.toml editable install."""

    def test_editable_install_with_dev_extra(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``pip install -e '.[dev]'`` is tried first for pyproject.toml."""
        worktree = tmp_path / "feat-5-test"
        worktree.mkdir()
        (worktree / "pyproject.toml").write_text(
            "[project]\nname='x'\n", encoding="utf-8"
        )
        monkeypatch.chdir(worktree)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return _ok()

        monkeypatch.setattr(after_create_mod, "_run", fake_run)

        result = main()

        assert result == 0
        assert calls == [["pip", "install", "-e", ".[dev]"]]

    def test_editable_install_falls_back_without_dev_extra(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Falls back to ``-e .`` when the [dev] extra is absent."""
        worktree = tmp_path / "feat-5-test"
        worktree.mkdir()
        (worktree / "pyproject.toml").write_text(
            "[project]\nname='x'\n", encoding="utf-8"
        )
        monkeypatch.chdir(worktree)

        calls: list[list[str]] = []
        attempt = 0

        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            nonlocal attempt
            calls.append(cmd)
            # First call (with [dev]) fails; second (bare) succeeds.
            attempt += 1
            return _fail() if attempt == 1 else _ok()

        monkeypatch.setattr(after_create_mod, "_run", fake_run)

        result = main()

        assert result == 0
        assert calls == [
            ["pip", "install", "-e", ".[dev]"],
            ["pip", "install", "-e", "."],
        ]

    def test_both_editable_installs_fail_returns_nonzero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Returns non-zero when both editable install attempts fail."""
        worktree = tmp_path / "feat-5-test"
        worktree.mkdir()
        (worktree / "pyproject.toml").write_text(
            "[project]\nname='x'\n", encoding="utf-8"
        )
        monkeypatch.chdir(worktree)

        monkeypatch.setattr(after_create_mod, "_run", lambda _cmd: _fail())

        result = main()

        assert result != 0


# ---------------------------------------------------------------------------
# No project files
# ---------------------------------------------------------------------------


class TestAfterCreateNoProjectFiles:
    """Tests for the no-op path when no project files are detected."""

    def test_no_project_files_exits_zero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """No project files → log informative message and return 0."""
        worktree = tmp_path / "feat-7-test"
        worktree.mkdir()
        monkeypatch.chdir(worktree)

        calls: list[Any] = []
        monkeypatch.setattr(
            after_create_mod,
            "_run",
            lambda cmd: (calls.append(cmd), _ok())[1],
        )

        result = main()

        assert result == 0
        assert calls == [], "no subprocess should run for empty project"
        # A log line should be emitted so the no-op is visible in Baton.
        captured = capsys.readouterr()
        assert captured.out != ""


# ---------------------------------------------------------------------------
# Bad worktree name (unresolvable issue number)
# ---------------------------------------------------------------------------


class TestAfterCreateBadWorktreeName:
    """Tests for the error path when issue number cannot be resolved."""

    def test_unresolvable_issue_exits_one(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A worktree name without a number returns exit 1."""
        bad_dir = tmp_path / "nodashes"
        bad_dir.mkdir()
        monkeypatch.chdir(bad_dir)

        result = main()

        assert result == 1


# ---------------------------------------------------------------------------
# _write_claude_settings — git-exclude and backup behaviour
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path) -> None:
    """Initialise a minimal git repo at *path* for testing.

    Args:
        path: Directory to initialise.  Must already exist.
    """
    subprocess.run(
        ["git", "init", "-q", str(path)],
        check=True,
        encoding="utf-8",
        capture_output=True,
    )


class TestWriteClaudeSettingsExclude:
    """Tests for the .git/info/exclude injection in _write_claude_settings."""

    def test_exclude_line_written_on_fresh_repo(
        self,
        tmp_path: Path,
    ) -> None:
        """Fresh git repo: settings file written and exclude line added.

        After calling ``_write_claude_settings``:
        - ``.claude/settings.json`` must exist.
        - ``.git/info/exclude`` must contain a line equal to
          ``.claude/settings.json``.
        """
        worktree = tmp_path / "feat-99-test"
        worktree.mkdir()
        _init_git_repo(worktree)

        rc = after_create_mod._write_claude_settings(
            issue=99,
            cwd=worktree,
            venv_root=tmp_path / "fakevenv",
        )

        assert rc == 0
        assert (worktree / ".claude" / "settings.json").exists()
        exclude_path = worktree / ".git" / "info" / "exclude"
        assert exclude_path.exists(), ".git/info/exclude was not created"
        lines = [
            ln.rstrip()
            for ln in exclude_path.read_text(encoding="utf-8").splitlines()
        ]
        assert ".claude/settings.json" in lines, (
            f".claude/settings.json not found in exclude; lines={lines}"
        )

    def test_exclude_line_not_duplicated_on_second_call(
        self,
        tmp_path: Path,
    ) -> None:
        """Running _write_claude_settings twice does not duplicate the line.

        The idempotency guard must prevent appending ``.claude/settings.json``
        a second time to ``.git/info/exclude``.
        """
        worktree = tmp_path / "feat-99-test"
        worktree.mkdir()
        _init_git_repo(worktree)

        rc1 = after_create_mod._write_claude_settings(
            issue=99,
            cwd=worktree,
            venv_root=tmp_path / "fakevenv",
        )
        rc2 = after_create_mod._write_claude_settings(
            issue=99,
            cwd=worktree,
            venv_root=tmp_path / "fakevenv",
        )

        assert rc1 == 0
        assert rc2 == 0
        exclude_path = worktree / ".git" / "info" / "exclude"
        lines = [
            ln.rstrip()
            for ln in exclude_path.read_text(encoding="utf-8").splitlines()
        ]
        count = lines.count(".claude/settings.json")
        assert count == 1, (
            f"'.claude/settings.json' appears {count} times in exclude "
            f"after two calls; expected exactly 1"
        )

    def test_existing_settings_backed_up(
        self,
        tmp_path: Path,
    ) -> None:
        """Pre-existing .claude/settings.json is backed up before overwrite.

        When ``.claude/settings.json`` already exists (tracked or untracked),
        ``_write_claude_settings`` must copy it to
        ``.claude/settings.json.bh-backup`` before writing the harness version.
        """
        worktree = tmp_path / "feat-99-test"
        worktree.mkdir()
        _init_git_repo(worktree)

        # Plant a pre-existing settings file representing the target repo's
        # own Claude configuration.
        claude_dir = worktree / ".claude"
        claude_dir.mkdir()
        original_content = '{"original": true}\n'
        (claude_dir / "settings.json").write_text(
            original_content, encoding="utf-8"
        )

        rc = after_create_mod._write_claude_settings(
            issue=99,
            cwd=worktree,
            venv_root=tmp_path / "fakevenv",
        )

        assert rc == 0
        backup_path = claude_dir / "settings.json.bh-backup"
        assert backup_path.exists(), (
            ".claude/settings.json.bh-backup was not created"
        )
        backed_up = backup_path.read_text(encoding="utf-8")
        assert backed_up == original_content, (
            "backup content does not match original; "
            f"got {backed_up!r}, want {original_content!r}"
        )
        # The active settings.json must now be the harness version.
        active = (claude_dir / "settings.json").read_text(encoding="utf-8")
        assert active != original_content, (
            "settings.json was not overwritten with harness content"
        )


# ---------------------------------------------------------------------------
# NEW-P1: _register_git_exclude on a linked worktree
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> None:
    """Run a git command in *cwd*, raising on failure.

    Args:
        *args: git sub-command and arguments.
        cwd: Working directory for the git invocation.
    """
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        encoding="utf-8",
    )


class TestRegisterGitExcludeOnLinkedWorktree:
    """_register_git_exclude works on git-worktree-add linked worktrees.

    WorkspaceManager.ensure_worktree (vendor/symphony/workspace.py:84)
    creates worktrees via ``git worktree add``, which leaves ``.git`` as
    a pointer FILE, not a directory.  The old code assumed ``.git`` was
    a directory and raised ``NotADirectoryError`` on ``mkdir``.

    These tests verify the fix: the exclude path is resolved via
    ``git rev-parse --git-path info/exclude``, so the real path under
    ``<main>/.git/worktrees/<name>/info/exclude`` is used.
    """

    def test_linked_worktree_exclude_written_to_resolved_path(
        self,
        tmp_path: Path,
    ) -> None:
        """Exclude line is written to the resolved path, not the pointer.

        In a linked worktree ``.git`` is a file (pointer), not a
        directory.  After calling ``_register_git_exclude`` the
        ``.claude/settings.json`` line must appear in the resolved
        ``info/exclude`` path under the main repo's git dir, NOT inside
        the worktree's ``.git`` pointer file.
        """
        main_repo = tmp_path / "main-repo"
        main_repo.mkdir()
        _init_git_repo(main_repo)
        # git worktree add requires at least one commit on the main repo.
        (main_repo / "README.md").write_text("init\n", encoding="utf-8")
        _git("add", "README.md", cwd=main_repo)
        _git(
            "-c",
            "user.email=test@test.com",
            "-c",
            "user.name=Test",
            "commit",
            "-m",
            "init",
            cwd=main_repo,
        )

        linked_wt = tmp_path / "linked-worktree"
        _git(
            "worktree",
            "add",
            "--detach",
            str(linked_wt),
            cwd=main_repo,
        )

        # Verify the linked worktree really has .git as a FILE.
        dot_git = linked_wt / ".git"
        assert dot_git.is_file(), (
            f".git should be a pointer file in a linked worktree; "
            f"got: {dot_git}"
        )

        rc = after_create_mod._register_git_exclude(issue=1, cwd=linked_wt)

        assert rc == 0, (
            "_register_git_exclude should return 0 on a linked worktree"
        )

        # The exclude line must be in the RESOLVED path (under main repo's
        # git dir), not in the worktree's .git pointer file.
        result = subprocess.run(
            ["git", "rev-parse", "--git-path", "info/exclude"],
            cwd=str(linked_wt),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        raw = result.stdout.strip()
        if Path(raw).is_absolute():
            resolved_exclude = Path(raw)
        else:
            resolved_exclude = (linked_wt / raw).resolve()

        assert resolved_exclude.exists(), (
            f"Expected exclude file at {resolved_exclude} to exist"
        )
        lines = [
            ln.rstrip()
            for ln in resolved_exclude.read_text(encoding="utf-8").splitlines()
        ]
        assert ".claude/settings.json" in lines, (
            f".claude/settings.json not found in exclude lines: {lines}"
        )

    def test_linked_worktree_exclude_idempotent(
        self,
        tmp_path: Path,
    ) -> None:
        """Calling _register_git_exclude twice on a linked worktree is safe.

        The idempotency guard must also work when the git dir is resolved
        via ``git rev-parse`` (linked worktree case).
        """
        main_repo = tmp_path / "main-repo"
        main_repo.mkdir()
        _init_git_repo(main_repo)
        (main_repo / "README.md").write_text("init\n", encoding="utf-8")
        _git("add", "README.md", cwd=main_repo)
        _git(
            "-c",
            "user.email=test@test.com",
            "-c",
            "user.name=Test",
            "commit",
            "-m",
            "init",
            cwd=main_repo,
        )

        linked_wt = tmp_path / "linked-worktree"
        _git(
            "worktree",
            "add",
            "--detach",
            str(linked_wt),
            cwd=main_repo,
        )

        rc1 = after_create_mod._register_git_exclude(issue=1, cwd=linked_wt)
        rc2 = after_create_mod._register_git_exclude(issue=1, cwd=linked_wt)

        assert rc1 == 0
        assert rc2 == 0

        result = subprocess.run(
            ["git", "rev-parse", "--git-path", "info/exclude"],
            cwd=str(linked_wt),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        raw = result.stdout.strip()
        if Path(raw).is_absolute():
            resolved_exclude = Path(raw)
        else:
            resolved_exclude = (linked_wt / raw).resolve()

        lines = [
            ln.rstrip()
            for ln in resolved_exclude.read_text(encoding="utf-8").splitlines()
        ]
        count = lines.count(".claude/settings.json")
        assert count == 1, (
            f"'.claude/settings.json' appears {count} times; expected 1"
        )

    def test_non_git_directory_returns_rc_no_git(
        self,
        tmp_path: Path,
    ) -> None:
        """A plain directory (no git) returns _RC_NO_GIT (2).

        This verifies the degraded-mode path preserved from the original
        implementation still works when triggered via subprocess failure.
        """
        plain_dir = tmp_path / "not-a-repo"
        plain_dir.mkdir()

        rc = after_create_mod._register_git_exclude(issue=1, cwd=plain_dir)

        assert rc == after_create_mod._RC_NO_GIT, (
            f"Expected _RC_NO_GIT ({after_create_mod._RC_NO_GIT}), got {rc}"
        )


# ---------------------------------------------------------------------------
# NEW-P2: _write_claude_settings refuses tracked .claude/settings.json
# ---------------------------------------------------------------------------


def _git_config_identity(repo: Path) -> None:
    """Set a minimal git identity for commits in *repo*.

    Args:
        repo: Repository root where identity config is applied.
    """
    _git(
        "-c",
        "user.email=test@test.com",
        "-c",
        "user.name=Test",
        "commit",
        "--allow-empty",
        "-m",
        "placeholder",
        cwd=repo,
    )


class TestWriteClaudeSettingsTrackedFileFatal:
    """_write_claude_settings is FATAL when .claude/settings.json is tracked.

    If the target repo already has a committed .claude/settings.json,
    the git exclude entry cannot protect it from being staged by
    ``git add -A``.  The hook must refuse to overwrite the file and
    return 1 (FATAL) so the worker never starts without the tripwire.
    """

    def _make_tracked_settings(self, repo: Path) -> str:
        """Commit a .claude/settings.json in *repo* and return its content.

        Args:
            repo: Initialised git repository root.

        Returns:
            The content string written and committed.
        """
        claude_dir = repo / ".claude"
        claude_dir.mkdir(exist_ok=True)
        content = '{"original": true, "tracked": true}\n'
        (claude_dir / "settings.json").write_text(content, encoding="utf-8")
        _git("add", ".claude/settings.json", cwd=repo)
        _git(
            "-c",
            "user.email=test@test.com",
            "-c",
            "user.name=Test",
            "commit",
            "-m",
            "add tracked settings",
            cwd=repo,
        )
        return content

    def test_tracked_settings_returns_fatal(
        self,
        tmp_path: Path,
    ) -> None:
        """Returns 1 (FATAL) when .claude/settings.json is tracked.

        A tracked .claude/settings.json means git add -A would stage
        any overwrite as a modification — bypassing the exclude guard
        and risking loss of the repo's real Claude config in the PR.
        """
        worktree = tmp_path / "feat-99-test"
        worktree.mkdir()
        _init_git_repo(worktree)
        self._make_tracked_settings(worktree)

        rc = after_create_mod._write_claude_settings(
            issue=99,
            cwd=worktree,
            venv_root=tmp_path / "fakevenv",
        )

        assert rc == 1, (
            f"Expected FATAL (1) for tracked .claude/settings.json, got {rc}"
        )

    def test_tracked_settings_file_unmodified(
        self,
        tmp_path: Path,
    ) -> None:
        """The tracked file content is NOT modified on FATAL return.

        Even in the refusal path the hook must not alter the tracked
        file — any modification would dirty the git index and confuse
        subsequent operations.
        """
        worktree = tmp_path / "feat-99-test"
        worktree.mkdir()
        _init_git_repo(worktree)
        original_content = self._make_tracked_settings(worktree)

        after_create_mod._write_claude_settings(
            issue=99,
            cwd=worktree,
            venv_root=tmp_path / "fakevenv",
        )

        current = (worktree / ".claude" / "settings.json").read_text(
            encoding="utf-8"
        )
        assert current == original_content, (
            "tracked .claude/settings.json was modified despite FATAL "
            f"refusal; got {current!r}, want {original_content!r}"
        )

    def test_tracked_settings_no_backup_created(
        self,
        tmp_path: Path,
    ) -> None:
        """No backup file is created when returning FATAL for tracked case.

        Backup is only meaningful for untracked pre-existing files.
        For the tracked case the hook refuses entirely — creating a
        backup would be misleading since no overwrite occurs.
        """
        worktree = tmp_path / "feat-99-test"
        worktree.mkdir()
        _init_git_repo(worktree)
        self._make_tracked_settings(worktree)

        after_create_mod._write_claude_settings(
            issue=99,
            cwd=worktree,
            venv_root=tmp_path / "fakevenv",
        )

        backup_path = worktree / ".claude" / "settings.json.bh-backup"
        assert not backup_path.exists(), (
            "backup file should NOT be created in the tracked-file FATAL path"
        )
