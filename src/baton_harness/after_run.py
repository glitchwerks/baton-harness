"""Hook: after_run — outcome classification and GitHub label reconciliation.

Invoked by Baton after each agent run turn completes.  Responsible for:

1. Classifying the run outcome into one of the states defined in
   ``harness-design.md §5``:

   - ``uncommitted-changes`` — agent left changes but did not commit.
   - ``no-commits`` — agent ran but produced no changes.
   - ``committed-no-pr`` — commits were made but no PR was opened.
   - ``pr-opened`` — a PR is open for the worktree branch (success path).

2. Reconciling GitHub labels on the issue to a single state label
   (``agent-ready``, ``agent-done``, or ``blocked``), enforcing the
   invariant that exactly one state label is present at any time.

Entry point: ``bh-after-run`` (defined in ``pyproject.toml``).

WORKFLOW.md hook line (issue #5)::

    after_run: bh-after-run

Context:
    The hook runs with ``$PWD`` set to the worktree directory.  The issue
    number is inferred from ``basename($PWD)`` via
    ``baton_harness._cli.resolve_issue_number`` (spike finding F2).

    GitHub API calls use ``gh --json`` output parsed via ``json.loads``,
    never shell-grepped (addresses the pattern flagged in PR #9; see the
    language-decision rationale in ``harness-design.md``).

    All subprocess calls use ``encoding="utf-8"`` explicitly (Windows
    cp1252 footgun — see Python skill notes).

    This hook must finish under the 60 s timeout enforced by Baton
    (spike finding F11).
"""

from __future__ import annotations

import enum
import json
import subprocess
import sys

from baton_harness._cli import err, log, resolve_issue_number

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Short name used in log/err prefixes.
_HOOK = "after-run"

#: Label applied when the issue is ready for an agent run.
LABEL_AGENT_READY = "agent-ready"

#: Label applied when the agent has opened a PR (pilot: human verifies CI).
LABEL_AGENT_DONE = "agent-done"

#: Label applied by the agent mid-run when it needs human input.
LABEL_BLOCKED = "blocked"


# ---------------------------------------------------------------------------
# Outcome state machine
# ---------------------------------------------------------------------------


class RunOutcome(enum.Enum):
    """Classification of what an agent run produced.

    Represents the four terminal states identified in spike finding F5.
    Using an enum (rather than raw strings) gives exhaustive-match checking
    and eliminates the grep-for-string fragility in the prior shell version.

    Members:
        UNCOMMITTED_CHANGES: Agent left modified files but did not commit.
        NO_COMMITS: Agent ran but produced no new commits ahead of main.
        COMMITTED_NO_PR: Agent committed changes but did not open a PR.
        PR_OPENED: Agent opened a PR; the success path (pilot: CI unverified).
    """

    UNCOMMITTED_CHANGES = "uncommitted-changes"
    NO_COMMITS = "no-commits"
    COMMITTED_NO_PR = "committed-no-pr"
    PR_OPENED = "pr-opened"


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run an external command and return its completed process.

    Centralises subprocess invocation so that tests can patch a single
    symbol (spike finding F8 — hooks must be independently testable).

    Args:
        cmd: Command and arguments to execute (no shell interpolation).

    Returns:
        A ``subprocess.CompletedProcess`` with captured stdout/stderr.
        The process is allowed to exit with any code; callers inspect
        ``returncode`` themselves.
    """
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# F5 classification
# ---------------------------------------------------------------------------


def _current_branch() -> str:
    """Return the name of the current git branch.

    Returns:
        The branch name as reported by ``git rev-parse --abbrev-ref HEAD``.
    """
    result = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    return result.stdout.strip()


def _classify() -> RunOutcome:
    """Classify the outcome of the most recent agent run.

    Implements the four-state F5 classification (spike finding F5):

    1. ``UNCOMMITTED_CHANGES`` — ``git status --porcelain`` is non-empty.
    2. ``NO_COMMITS`` — ``git cherry origin/main HEAD`` has no ``+`` lines.
    3. ``COMMITTED_NO_PR`` — commits exist ahead of main but ``gh pr list``
       returns an empty array.
    4. ``PR_OPENED`` — ``gh pr list`` returns a non-empty array.

    ``gh --json`` output is parsed with ``json.loads`` (not grepped), fixing
    the fragility identified in PR #9's shell implementation.

    Returns:
        The ``RunOutcome`` value matching the current worktree state.
    """
    # Step 1: uncommitted changes?
    status = _run(["git", "status", "--porcelain", "--untracked-files=no"])
    if status.stdout.strip():
        return RunOutcome.UNCOMMITTED_CHANGES

    # Step 2: any commits ahead of origin/main?
    cherry = _run(["git", "cherry", "origin/main", "HEAD"])
    ahead_commits = [
        line for line in cherry.stdout.splitlines() if line.startswith("+")
    ]
    if not ahead_commits:
        return RunOutcome.NO_COMMITS

    # Step 3: open PR for this branch?
    branch = _current_branch()
    pr_result = _run(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "number",
        ]
    )
    prs: list[dict[str, object]] = json.loads(pr_result.stdout)
    if prs:
        return RunOutcome.PR_OPENED

    return RunOutcome.COMMITTED_NO_PR


# ---------------------------------------------------------------------------
# Label reconciliation
# ---------------------------------------------------------------------------


def _current_labels(issue: int) -> list[str]:
    """Fetch the current label names for a GitHub issue.

    Parses ``gh issue view --json labels`` output with ``json.loads`` (never
    grepped — addresses the H1 root-cause pattern).

    Args:
        issue: GitHub issue number whose labels are fetched.

    Returns:
        A list of label name strings currently on the issue.
    """
    result = _run(["gh", "issue", "view", str(issue), "--json", "labels"])
    data: dict[str, list[dict[str, str]]] = json.loads(result.stdout)
    return [lbl["name"] for lbl in data.get("labels", [])]


def _reconcile_labels(issue: int, outcome: RunOutcome) -> int:
    """Reconcile GitHub labels to enforce the single-state invariant.

    Implements the label state machine from ``harness-design.md §5``.
    Exactly one of ``agent-ready``, ``agent-done``, or ``blocked`` must
    be present after this function returns.

    Priority:
        1. If ``blocked`` is already on the issue (applied mid-run by the
           agent), remove ``agent-ready`` and leave ``blocked``.  Do NOT add
           ``agent-done`` — the block overrides the F5 classification.
           Note: TODO(#4) — making a block terminal (stopping Baton's own
           continuation retry) depends on the block-cost test result.
        2. If outcome is ``PR_OPENED``, add ``agent-done`` and remove
           ``agent-ready``.  Log the F10 caveat: CI status is NOT checked
           (human verifies at review — pilot scope).
        3. Otherwise (``NO_COMMITS``, ``UNCOMMITTED_CHANGES``,
           ``COMMITTED_NO_PR``): retryable.  Leave ``agent-ready`` in place
           for Baton's own retry mechanism; log the classification.

    Label-edit failures are surfaced via non-zero exit codes and ``_cli.err``
    logging — they are never swallowed (H1 root cause was ``|| true``
    silencing).

    Args:
        issue: GitHub issue number whose labels are reconciled.
        outcome: The F5 classification for the current run.

    Returns:
        ``0`` on success, ``1`` if any label mutation fails.
    """
    labels = _current_labels(issue)

    # Priority 1: blocked label wins regardless of F5 outcome.
    if LABEL_BLOCKED in labels:
        log(
            _HOOK,
            issue,
            f"blocked label present — removing {LABEL_AGENT_READY!r}; "
            "leaving 'blocked' in place.",
            # TODO(#4): make block terminal — stop Baton's continuation retry.
            # Implement once block-cost test (harness-design.md §8) confirms
            # whether `exclude_labels: ['blocked']` halts the retry loop.
        )
        if LABEL_AGENT_READY in labels:
            result = _run(
                [
                    "gh",
                    "issue",
                    "edit",
                    str(issue),
                    "--remove-label",
                    LABEL_AGENT_READY,
                ]
            )
            if result.returncode != 0:
                err(
                    _HOOK,
                    issue,
                    f"failed to remove {LABEL_AGENT_READY!r}: "
                    f"{result.stderr.strip()}",
                )
                return 1
        return 0

    # Priority 2: PR opened — success path.
    if outcome == RunOutcome.PR_OPENED:
        # F10 caveat: 'agent-done' means a PR exists, NOT that CI is green.
        # The human is the CI gate at review (pilot scope). Do NOT query or
        # gate on CI status here.
        log(
            _HOOK,
            issue,
            "outcome=pr-opened: adding 'agent-done', removing 'agent-ready'. "
            "CAVEAT(F10): agent-done means a PR exists, NOT that CI is green"
            " — human verifies at review.",
        )
        add_result = _run(
            [
                "gh",
                "issue",
                "edit",
                str(issue),
                "--add-label",
                LABEL_AGENT_DONE,
            ]
        )
        if add_result.returncode != 0:
            err(
                _HOOK,
                issue,
                f"failed to add {LABEL_AGENT_DONE!r}: "
                f"{add_result.stderr.strip()}",
            )
            return 1
        remove_result = _run(
            [
                "gh",
                "issue",
                "edit",
                str(issue),
                "--remove-label",
                LABEL_AGENT_READY,
            ]
        )
        if remove_result.returncode != 0:
            err(
                _HOOK,
                issue,
                f"failed to remove {LABEL_AGENT_READY!r}: "
                f"{remove_result.stderr.strip()}",
            )
            return 1
        return 0

    # Priority 3: retryable — leave agent-ready for Baton's own retry.
    log(
        _HOOK,
        issue,
        f"outcome={outcome.value}: retryable — leaving {LABEL_AGENT_READY!r} "
        "in place for Baton retry.",
    )
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``bh-after-run`` console script.

    Resolves the issue number from the current working directory, classifies
    the run outcome (F5), and reconciles GitHub labels to a single state
    (harness-design.md §5 / H1 fix).

    Args:
        argv: Unused; reserved for future CLI argument support.  Baton
            passes no env-var context to hooks (spike finding F2), so
            all context is derived from the worktree directory name.

    Returns:
        Exit code: ``0`` on success, ``1`` on any failure (unresolvable
        issue number, or a label-edit error that must not be swallowed).
    """
    issue = resolve_issue_number()
    if issue is None:
        print(
            f"[{_HOOK}] error: could not derive issue number from cwd — "
            "worktree name must match <prefix>-<issue>[-<slug>]",
            file=sys.stderr,
            flush=True,
        )
        return 1

    log(_HOOK, issue, "classifying run outcome (F5)...")
    outcome = _classify()
    log(_HOOK, issue, f"outcome={outcome.value}")

    return _reconcile_labels(issue, outcome)


if __name__ == "__main__":
    sys.exit(main())
