"""Failing tests for bin/init-sandbox.sh branch guard + .gitignore (#349).

Bug A: the stub-CI-workflow section (~line 676-787) resolves
``DEFAULT_BRANCH`` via ``gh repo view`` but never checks out that
branch before committing and pushing. It only guards against a
detached HEAD, not "wrong branch checked out" -- which the always-on
daemon's own normal operation (``chain/branches.py``'s
``checkout_feature_branch``) routinely leaves the clone parked on
(``feature/<slug>``) after a prior dispatch. The fix must explicitly
check out (or verify it is already on) the default branch, fast-
forward it to ``origin/<default>``, and fail loudly if that isn't
possible cleanly -- never silently commit onto whatever branch happens
to be checked out.

Bug B: the ``.gitignore``-seeding block (~line 796-826) seeds
``.symphony/`` and ``.baton-harness/`` but not ``.bh/``, even though
the same script unconditionally writes ``.bh/config.env`` a few lines
later.

These tests drive the real ``bin/init-sandbox.sh`` via subprocess
(mirroring the ``tests/test_run_daemon_repo_owner_env.py`` and
``tests/test_provision_ruleset_idempotent.py`` convention: a fake
``gh`` on PATH, isolated HOME/XDG_CONFIG_HOME, real Git Bash on
Windows) against REAL local git repositories -- a bare "origin" plus a
working-tree clone -- so the branch-guard behavior under test is
exercised against real git fast-forward/divergence semantics with no
GitHub network access required. The ``gh`` stub only implements the
handful of calls the script issues before the section under test:
``gh auth status``, ``gh auth setup-git``, the per-label existence
probe (always answered "exists" so label creation is never exercised),
and ``gh repo view --json defaultBranchRef``. The ``recovery`` scenario
is used throughout because it seeds no issues/milestones, so it makes
zero further ``gh`` calls between the label preflight and the section
under test -- the least-invasive way to reach that section without
factoring the script (it is not decomposed into isolable functions;
see the return-summary note on this constraint).

Every full run of the script eventually reaches the interactive
``.bh/config.env`` prompt step and fails there (this subprocess has no
tty), so a "happy path" run's overall exit code is never 0 -- success
for the section under test is verified via git state (what actually
landed on the "origin" bare repo) and stdout markers, not the final
process exit code.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[1]
INIT_SANDBOX = HARNESS / "bin" / "init-sandbox.sh"

# On Windows, the system bash (C:\Windows\System32\bash.exe) launches WSL
# and fails when no WSL distro is configured. Prefer Git Bash when
# available (same resolution as test_run_daemon_repo_owner_env.py).
_GIT_BASH = Path("C:/Program Files/Git/usr/bin/bash.exe")
if sys.platform == "win32" and _GIT_BASH.exists():
    _BASH = str(_GIT_BASH)
else:
    _BASH = "bash"
_BASH_BIN_DIR = str(Path(_BASH).parent) if Path(_BASH).exists() else ""
_VENV_BIN_DIR = str(
    HARNESS / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
)

_GH_STUB_SCRIPT = """#!/usr/bin/env bash
# Minimal gh stub for init-sandbox.sh branch-guard tests (issue #349).
#
# Handles exactly the gh invocations init-sandbox.sh issues before the
# default-branch/workflow section under test:
#   - gh auth status            -> success
#   - gh auth setup-git         -> success
#   - gh api repos/*/labels/*   -> HTTP/1.1 200 (label already exists,
#                                  so the create branch is never hit)
#   - gh repo view ... --json defaultBranchRef --jq ...
#                               -> prints FAKE_GH_DEFAULT_BRANCH
# Anything else is an unexpected call for these tests (the "recovery"
# scenario makes no further gh calls) and fails loudly so a broken
# fixture cannot masquerade as a passing test.
set -euo pipefail

if [[ "$1" == "auth" && "$2" == "status" ]]; then
    exit 0
fi
if [[ "$1" == "auth" && "$2" == "setup-git" ]]; then
    exit 0
fi
if [[ "$1" == "api" && "$2" == repos/*/labels/* ]]; then
    echo "HTTP/1.1 200 OK"
    exit 0
fi
if [[ "$1" == "repo" && "$2" == "view" ]]; then
    echo "${FAKE_GH_DEFAULT_BRANCH:-main}"
    exit 0
fi

echo "fake-gh: unexpected invocation: $*" >&2
exit 1
"""


# ---------------------------------------------------------------------------
# git fixture helpers
# ---------------------------------------------------------------------------


def _git_env() -> dict[str, str]:
    """Build a git-invocation environment with a stable commit identity.

    Returns:
        A copy of the current environment plus deterministic
        ``GIT_AUTHOR_*`` / ``GIT_COMMITTER_*`` values and
        ``GIT_CONFIG_NOSYSTEM=1``, so these throwaway fixture repos
        never depend on (or are perturbed by) the host machine's git
        identity or system-wide git config (e.g. ``core.autocrlf``).
    """
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": "test-author@example.invalid",
            "GIT_COMMITTER_NAME": "Test Author",
            "GIT_COMMITTER_EMAIL": "test-author@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    return env


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command against ``cwd`` and require it to succeed.

    Args:
        cwd: The repository directory (working tree or bare) to run
            the command against.
        *args: Arguments passed to ``git`` after the executable name.

    Returns:
        The completed process.
    """
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=_git_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, (
        f"git {' '.join(args)} failed in {cwd}:\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return proc


def _rev(cwd: Path, ref: str) -> str:
    """Resolve ``ref`` to a commit SHA inside the ``cwd`` repository.

    Args:
        cwd: The repository directory (working tree or bare).
        ref: The ref to resolve (branch name, HEAD, etc.).

    Returns:
        The resolved commit SHA.
    """
    return _git(cwd, "rev-parse", ref).stdout.strip()


def _ref_contains_path(repo: Path, ref: str, path: str) -> bool:
    """Return whether ``path`` exists in the tree at ``ref``.

    Args:
        repo: The repository directory (working tree or bare).
        ref: The ref whose tree to inspect.
        path: The repo-relative path to look up.

    Returns:
        True if ``path`` exists in ``ref``'s tree, False otherwise.
    """
    proc = subprocess.run(
        ["git", "cat-file", "-e", f"{ref}:{path}"],
        cwd=str(repo),
        env=_git_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc.returncode == 0


def _write_commit(
    repo: Path, filename: str, content: str, message: str
) -> str:
    """Write ``filename`` with ``content``, commit it, and return its SHA.

    Args:
        repo: Working-tree repository to commit into.
        filename: Relative path of the file to create/overwrite.
        content: File content to write.
        message: Commit message.

    Returns:
        The new commit's SHA.
    """
    (repo / filename).write_text(content, encoding="utf-8", newline="\n")
    _git(repo, "add", filename)
    _git(repo, "commit", "-m", message)
    return _rev(repo, "HEAD")


def _make_origin_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """Build a bare 'origin' repo with one commit on main, plus a clone.

    Args:
        tmp_path: Pytest-provided temp directory for the test.

    Returns:
        A tuple of ``(origin_bare_path, project_root_clone_path)``. The
        clone's current branch is ``main`` and matches ``origin/main``.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(origin)],
        env=_git_env(),
        capture_output=True,
        text=True,
        check=True,
    )
    project_root = tmp_path / "project"
    subprocess.run(
        ["git", "clone", str(origin), str(project_root)],
        env=_git_env(),
        capture_output=True,
        text=True,
        check=True,
    )
    _write_commit(project_root, "base.txt", "v0\n", "initial commit")
    _git(project_root, "branch", "-M", "main")
    _git(project_root, "push", "-u", "origin", "main")
    # A plain `git init --bare` defaults HEAD to the local
    # init.defaultBranch (often "master"), which never gets created
    # above. Point origin's HEAD at "main" explicitly so a later
    # `git clone origin ...` (used by _advance_origin_main) checks out
    # "main" by default instead of landing on an unborn/invalid HEAD.
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    return origin, project_root


def _advance_origin_main(
    origin: Path,
    tmp_path: Path,
    filename: str,
    content: str,
    message: str,
) -> None:
    """Push a new commit directly to origin's main via a throwaway clone.

    Simulates independent activity on the remote's default branch (e.g.
    another PR merging elsewhere) without touching ``project_root``'s
    own refs, so ``project_root``'s local knowledge of ``origin/main``
    stays exactly as stale as it would be in a long-running daemon
    clone that never fetched.

    Args:
        origin: The bare origin repository to push into.
        tmp_path: Pytest temp dir to hold the throwaway side clone.
        filename: Relative path of the file the new commit adds.
        content: Content for the new file.
        message: Commit message for the new commit.
    """
    side = tmp_path / f"side-{filename.replace('/', '_').replace('.', '_')}"
    subprocess.run(
        ["git", "clone", str(origin), str(side)],
        env=_git_env(),
        capture_output=True,
        text=True,
        check=True,
    )
    _write_commit(side, filename, content, message)
    _git(side, "push", "origin", "main")


# ---------------------------------------------------------------------------
# init-sandbox.sh invocation helper
# ---------------------------------------------------------------------------


def _write_gh_stub(bin_dir: Path) -> None:
    """Write the fake ``gh`` executable described by ``_GH_STUB_SCRIPT``.

    Args:
        bin_dir: Directory to create and place the ``gh`` stub in.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    gh_stub = bin_dir / "gh"
    gh_stub.write_text(_GH_STUB_SCRIPT, encoding="utf-8", newline="\n")
    gh_stub.chmod(0o755)


def _run_init_sandbox(
    tmp_path: Path,
    project_root: Path,
    *,
    default_branch: str,
    scenario: str = "recovery",
) -> subprocess.CompletedProcess[str]:
    """Invoke the real ``init-sandbox.sh`` against an isolated environment.

    Uses a fake ``gh`` on PATH (auth + label preflight + default-branch
    resolution only) and a real local git remote (``project_root``'s
    ``origin``), so the branch-guard and ``.gitignore`` behavior under
    test runs against real git semantics with no network access.

    Args:
        tmp_path: Pytest-provided temp directory for the test.
        project_root: The local clone to point ``BH_PROJECT_ROOT`` at.
        default_branch: The branch name the fake ``gh repo view`` call
            reports as the sandbox repo's default branch.
        scenario: The ``--scenario``/``BH_SCENARIO`` value. Defaults to
            ``"recovery"``, which seeds no issues/milestones and so
            makes no further ``gh`` calls the stub would need to
            handle between the label preflight and the section under
            test.

    Returns:
        The completed ``init-sandbox.sh`` process, including captured
        output. The overall exit code is expected to be non-zero even
        on the "happy path" for the section under test, because the
        script's later ``.bh/config.env`` step requires an interactive
        terminal this subprocess does not have.
    """
    gh_bin_dir = tmp_path / "gh_bin"
    _write_gh_stub(gh_bin_dir)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    xdg_config_home = tmp_path / "xdg_config"
    xdg_config_home.mkdir(exist_ok=True)

    env = _git_env()
    env.pop("BH_REPO_OWNER", None)
    env.pop("BH_REPO_NAME", None)
    env.pop("BH_PROJECT_ROOT", None)
    env.pop("BH_SCENARIO", None)
    env["PATH"] = os.pathsep.join(
        part
        for part in [
            gh_bin_dir.as_posix(),
            _BASH_BIN_DIR,
            _VENV_BIN_DIR,
            env.get("PATH", ""),
        ]
        if part
    )
    env["HOME"] = home.as_posix()
    env["XDG_CONFIG_HOME"] = xdg_config_home.as_posix()
    env["BH_PROJECT_ROOT"] = project_root.as_posix()
    env["BH_REPO_OWNER"] = "fake-owner"
    env["BH_REPO_NAME"] = "fake-repo"
    env["BH_SCENARIO"] = scenario
    env["FAKE_GH_DEFAULT_BRANCH"] = default_branch

    return subprocess.run(
        [_BASH, str(INIT_SANDBOX)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Bug A -- wrong-branch guard
# ---------------------------------------------------------------------------


def test_wrong_branch_behind_default_lands_commit_only_on_synced_default(
    tmp_path: Path,
) -> None:
    """A leftover feature-branch checkout must never receive the commit.

    Reproduces the primary trigger from the issue: the daemon's normal
    operation leaves ``BH_PROJECT_ROOT`` parked on ``feature/<slug>``
    after a prior dispatch, and the sandbox repo's default branch has
    since advanced (e.g. another PR merged) while that feature branch
    was being worked. The fix must switch to the default branch, fast-
    forward it to ``origin/<default>``, and commit+push the stub CI
    workflow there -- never onto the checked-out feature branch, and
    without discarding the branch's own prior advance.
    """
    origin, project_root = _make_origin_and_clone(tmp_path)

    _git(project_root, "checkout", "-b", "feature/stale-123")
    _write_commit(
        project_root, "feature-only.txt", "feature work\n", "feature commit"
    )
    feature_head_before = _rev(project_root, "feature/stale-123")

    _advance_origin_main(
        origin,
        tmp_path,
        "remote-advance.txt",
        "advanced main\n",
        "remote-only commit",
    )

    proc = _run_init_sandbox(tmp_path, project_root, default_branch="main")

    assert _rev(project_root, "feature/stale-123") == feature_head_before, (
        "init-sandbox.sh must never commit the stub CI workflow onto "
        "the currently checked-out feature branch -- it must switch to "
        "the default branch first.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert not _ref_contains_path(origin, "main", "feature-only.txt"), (
        "feature branch content must never land on the default branch\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert _ref_contains_path(origin, "main", "remote-advance.txt"), (
        "the pre-existing remote advance on the default branch must be "
        "preserved -- the script must fast-forward, not overwrite, it\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert _ref_contains_path(origin, "main", ".github/workflows/ci.yml"), (
        "the stub CI workflow must still be committed onto the synced "
        "default branch\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def test_wrong_branch_ff_ancestor_never_lands_on_default_branch(
    tmp_path: Path,
) -> None:
    """A feature branch that is a fast-forward ancestor must not land.

    The dangerous variant of Bug A: the checked-out feature branch's
    history is a pure linear descendant of the default branch's current
    tip (the default branch never independently advanced), so a naive
    ``git push HEAD:<default>`` from the feature branch is technically
    a fast-forward and would succeed silently -- landing the feature
    branch's commits on the default branch. The fix must explicitly
    check out the default branch first, regardless of this ancestor
    relationship, so the feature commits are never even considered.
    """
    origin, project_root = _make_origin_and_clone(tmp_path)

    _git(project_root, "checkout", "-b", "feature/stale-123")
    _write_commit(
        project_root, "feature-marker-1.txt", "f1\n", "feature commit 1"
    )
    _write_commit(
        project_root, "feature-marker-2.txt", "f2\n", "feature commit 2"
    )

    proc = _run_init_sandbox(tmp_path, project_root, default_branch="main")

    assert not _ref_contains_path(origin, "main", "feature-marker-1.txt"), (
        "feature branch commits must never land on the default branch, "
        "even when they happen to be a fast-forward ancestor of it\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert not _ref_contains_path(origin, "main", "feature-marker-2.txt"), (
        "feature branch commits must never land on the default branch, "
        "even when they happen to be a fast-forward ancestor of it\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert _ref_contains_path(origin, "main", ".github/workflows/ci.yml"), (
        "the stub CI workflow must still be committed onto the "
        "(unchanged) default branch\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def test_cannot_cleanly_reach_default_branch_fails_without_committing(
    tmp_path: Path,
) -> None:
    """An unrecoverable divergence must fail loudly, touching nothing.

    Simulates the local default branch itself having drifted from
    ``origin``'s tip (e.g. a stray local-only commit from a prior
    operator action or failed run) while the working tree is parked on
    a feature branch and the remote default branch has also
    independently advanced. No clean fast-forward is possible, so the
    fix must exit non-zero with a clear error before ever committing or
    pushing anything -- not attempt the commit/push and let it fail via
    a raw git rejection.

    This does NOT assert that the local ``main`` ref itself is left
    untouched: for a throwaway sandbox clone, resolving the drift via
    ``git checkout main && git reset --hard origin/main`` (discarding
    the local-only drift entirely) is an equally acceptable "fail
    loudly and refuse to proceed" style response, provided it never
    fabricates a commit and never pushes. Only the safety-critical
    facts are pinned: the feature branch and the remote are untouched,
    and the overall run is treated as fatal.
    """
    origin, project_root = _make_origin_and_clone(tmp_path)

    _git(project_root, "checkout", "main")
    _write_commit(
        project_root,
        "local-main-drift.txt",
        "drift\n",
        "local-only main drift",
    )

    _git(project_root, "checkout", "-b", "feature/stale-123")
    _write_commit(
        project_root, "feature-only.txt", "feature work\n", "feature commit"
    )
    feature_head_before = _rev(project_root, "feature/stale-123")

    _advance_origin_main(
        origin,
        tmp_path,
        "remote-advance.txt",
        "advanced\n",
        "remote-only commit",
    )
    origin_main_before = _rev(origin, "main")

    proc = _run_init_sandbox(tmp_path, project_root, default_branch="main")

    assert proc.returncode != 0, (
        "init-sandbox.sh must fail loudly (non-zero exit) when it "
        "cannot cleanly reach the default branch, instead of "
        "committing anywhere\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert _rev(project_root, "feature/stale-123") == feature_head_before, (
        "no commit may be made on the checked-out feature branch when "
        "the default branch cannot be cleanly reached\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert _rev(origin, "main") == origin_main_before, (
        "nothing may be pushed to the default branch when it cannot be "
        "cleanly reached\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def test_already_synced_default_branch_still_commits_and_pushes_workflow(
    tmp_path: Path,
) -> None:
    """The ordinary already-on-default-branch path keeps working.

    Baseline/regression guard for item 1(c): when the working tree is
    already on the default branch and matches ``origin`` exactly, the
    fix must not have introduced any new friction -- the stub CI
    workflow must still be committed and pushed normally.
    """
    origin, project_root = _make_origin_and_clone(tmp_path)

    proc = _run_init_sandbox(tmp_path, project_root, default_branch="main")

    assert (
        "baton-harness:   ci.yml committed and pushed to sandbox"
        in proc.stdout
    ), (
        "the CI workflow must still be committed and pushed on the "
        "ordinary already-synced-default-branch path\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert _ref_contains_path(origin, "main", ".github/workflows/ci.yml"), (
        "the stub CI workflow must actually reach the default branch\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    # The overall process still exits non-zero because writing
    # .bh/config.env requires an interactive terminal this subprocess
    # doesn't have -- expected and unrelated to the branch-guard fix.
    assert proc.returncode == 1
    assert "interactive prompts required" in proc.stderr


# ---------------------------------------------------------------------------
# Bug B -- .gitignore completeness
# ---------------------------------------------------------------------------


def test_gitignore_seeds_bh_directory_alongside_existing_entries(
    tmp_path: Path,
) -> None:
    """The .gitignore seed step must also cover ``.bh/``.

    The script unconditionally writes ``.bh/config.env`` later in the
    same run, but the gitignore-seeding block currently only seeds
    ``.symphony/`` and ``.baton-harness/``. All three must be present.
    """
    _origin, project_root = _make_origin_and_clone(tmp_path)

    proc = _run_init_sandbox(tmp_path, project_root, default_branch="main")

    gitignore_content = (project_root / ".gitignore").read_text(
        encoding="utf-8"
    )
    # Line-exact, matching the script's own idempotency check for the
    # existing two entries (`grep -qxF '.symphony/' ...`) -- a
    # substring match would also accept e.g. ".bh/config.env" or a
    # comment mentioning ".bh/", neither of which actually gitignores
    # the ".bh/" directory itself.
    gitignore_lines = gitignore_content.splitlines()
    assert ".symphony/" in gitignore_lines, (
        f"expected pre-existing .symphony/ entry; got:\n"
        f"{gitignore_content}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert ".baton-harness/" in gitignore_lines, (
        f"expected pre-existing .baton-harness/ entry; got:\n"
        f"{gitignore_content}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert ".bh/" in gitignore_lines, (
        "the script unconditionally writes .bh/config.env later in the "
        "same run but never seeds .bh/ into .gitignore alongside "
        ".symphony/ and .baton-harness/ (issue #349 bug B)\n"
        f"got .gitignore:\n{gitignore_content}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
