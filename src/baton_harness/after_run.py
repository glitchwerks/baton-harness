"""Hook: after_run — outcome classification and GitHub label reconciliation.

.. note::

    **CHAIN_BASE_BRANCH env-awareness (P0):**  The ``CHAIN_BASE_BRANCH``
    environment variable controls which ref is used as the cherry base for
    the F5 classifier (default ``origin/main``).  The daemon threads this
    variable to per-issue hooks so the correct feature-branch base is used
    rather than ``main``.  The ref is resolved to a concrete SHA at entry
    (``git rev-parse <ref>``) to freeze the cut-point for the duration of
    the run window (B-I1 in the chain spec §3.7).

    **Priority-3 carry-forward deleted (P0):**  On ``COMMITTED_NO_PR``,
    ``NO_COMMITS``, and ``UNCOMMITTED_CHANGES``, the hook now removes
    ``agent-ready`` and adds ``blocked`` instead of leaving ``agent-ready``
    for Baton retry.  The always-on daemon is the new retry authority and
    consults ``blocked`` issues itself.

    **Transient gh failure handling (#32):**  ``gh pr list`` returncode
    and ``json.loads`` parse errors are treated as transient, not terminal.
    The call is retried up to ``_GH_PR_LIST_MAX_ATTEMPTS`` times with
    linear backoff via ``time.sleep``.  When all attempts are exhausted
    the outcome is ``TRANSIENT_ERROR``, which leaves ``agent-ready``
    intact (the issue remains eligible for a future run) and causes
    ``main()`` to return non-zero.  A non-zero ``git cherry`` exit is
    likewise treated as ``TRANSIENT_ERROR`` (MAJOR 1).
    ``_current_labels`` returns ``None`` (not ``[]``) on parse or
    returncode errors so that ``_reconcile_labels`` aborts with zero
    label mutations, preserving the single-state invariant (MAJOR 2).

Invoked by Baton after each agent run turn completes.  Responsible for:

1. Classifying the run outcome into one of the states defined in
   ``harness-design.md §5``:

   - ``uncommitted-changes`` — agent left changes but did not commit.
   - ``no-commits`` — agent ran but produced no changes.
   - ``committed-no-pr`` — commits were made but no PR was opened.
   - ``pr-opened`` — a PR is open for the worktree branch (success path).
   - ``transient-error`` — a transient gh API failure prevented
     classification; ``agent-ready`` is left intact.

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
    Baton names worktrees ``<repo>/.symphony/worktrees/<issue>`` (a bare
    integer); the harness's own convention is ``<repo>/.worktrees/<branch>``
    (``<prefix>-<issue>[-<slug>]``).  Both forms are accepted.

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
import os
import subprocess
import sys
import time
from pathlib import Path

from baton_harness._cli import err, log, resolve_issue_number
from baton_harness.chain.escalation import alert
from baton_harness.chain.labels import (
    LABEL_AGENT_DONE,
    LABEL_AGENT_READY,
    LABEL_BLOCKED,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Short name used in log/err prefixes.
_HOOK = "after-run"

#: Environment variable controlling the cherry base ref.  Set by the daemon
#: to ``feature/<slug>`` for milestone work units; defaults to
#: ``origin/main`` for flat (N=1 DAG / un-milestoned) runs.
_ENV_CHAIN_BASE_BRANCH = "CHAIN_BASE_BRANCH"
_DEFAULT_BASE = "origin/main"

# Slice 3b — sentinel file written by the force-pr-not-merge PreToolUse hook
# (src/baton_harness/hooks/force_pr_not_merge.py) when a worker attempts to
# merge a PR directly.  _classify() checks this as its FIRST step so the
# outcome is terminal regardless of any other git or gh state.
#
# IMPORTANT: ``alert`` is imported as the bare name (no ``as`` alias) so that
# ``monkeypatch.setattr(after_run, "alert", ...)`` in tests patches the same
# module-namespace attribute that the _reconcile_labels call site resolves.
_SENTINEL_DIR = ".bh-state"
_SENTINEL_NAME = "worker-tried-merge"


# ---------------------------------------------------------------------------
# Outcome state machine
# ---------------------------------------------------------------------------


class RunOutcome(enum.Enum):
    """Classification of what an agent run produced.

    Represents the four terminal states identified in spike finding F5,
    plus a fifth non-terminal state for transient infrastructure errors.
    Using an enum (rather than raw strings) gives exhaustive-match checking
    and eliminates the grep-for-string fragility in the prior shell version.

    Members:
        UNCOMMITTED_CHANGES: Agent left modified files but did not commit.
        NO_COMMITS: Agent ran but produced no new commits ahead of main.
        COMMITTED_NO_PR: Agent committed changes but did not open a PR.
        PR_OPENED: Agent opened a PR; the success path (pilot: CI unverified).
        TRANSIENT_ERROR: A transient gh API failure prevented classification.
            ``agent-ready`` is left intact so the issue remains eligible for
            a future run.  ``main()`` returns non-zero (#32).
    """

    UNCOMMITTED_CHANGES = "uncommitted-changes"
    NO_COMMITS = "no-commits"
    COMMITTED_NO_PR = "committed-no-pr"
    PR_OPENED = "pr-opened"
    TRANSIENT_ERROR = "transient-error"
    WORKER_TRIED_MERGE = "worker-tried-merge"  # slice 3b


# ---------------------------------------------------------------------------
# Retry constants for transient gh failures (#32)
# ---------------------------------------------------------------------------

#: Minimum number of ``gh pr list`` attempts before giving up.
_GH_PR_LIST_MAX_ATTEMPTS: int = 3

#: Base sleep duration (seconds) between ``gh pr list`` retry attempts.
_GH_PR_LIST_BACKOFF_SECONDS: float = 2.0


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


def _resolve_base_sha() -> str:
    """Resolve CHAIN_BASE_BRANCH to a concrete SHA at call time.

    Reads the ``CHAIN_BASE_BRANCH`` environment variable (default
    ``origin/main``) and resolves it to a concrete commit SHA via
    ``git rev-parse``.  This freezes the cut-point for the duration of the
    after_run window so the classifier is not confused by a moving
    ``--no-ff`` feature-branch tip (B-I1, chain spec §3.7).

    Returns:
        The resolved SHA string, or the bare ref string if resolution fails
        (allowing the cherry command to fail gracefully with a clear error
        rather than a confusing Python traceback).
    """
    base_ref = os.environ.get(_ENV_CHAIN_BASE_BRANCH, _DEFAULT_BASE)
    result = _run(["git", "rev-parse", base_ref])
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    # Fallback: return the ref string (git cherry will surface the error).
    return base_ref


def _classify() -> RunOutcome:
    """Classify the outcome of the most recent agent run.

    Implements the five-state F5 classification (spike finding F5, #32):

    1. ``UNCOMMITTED_CHANGES`` — ``git status --porcelain`` is non-empty.
    2. ``NO_COMMITS`` — ``git cherry <base-sha> HEAD`` has no ``+`` lines.
       The base SHA is resolved from ``CHAIN_BASE_BRANCH`` (default
       ``origin/main``) at entry via ``_resolve_base_sha()``.
    3. ``COMMITTED_NO_PR`` — commits exist ahead of base but ``gh pr list``
       returns a valid empty array.
    4. ``PR_OPENED`` — ``gh pr list`` returns a valid non-empty array.
    5. ``TRANSIENT_ERROR`` — ``gh pr list`` returned a non-zero exit code or
       non-JSON stdout on every attempt (up to
       ``_GH_PR_LIST_MAX_ATTEMPTS``).  ``agent-ready`` is left intact.

    ``gh --json`` output is parsed with ``json.loads`` (not grepped), fixing
    the fragility identified in PR #9's shell implementation.

    Transient ``gh pr list`` failures (#32):
        Both a non-zero returncode and a ``json.JSONDecodeError`` from
        ``json.loads`` are treated as transient.  The call is retried up to
        ``_GH_PR_LIST_MAX_ATTEMPTS`` times with ``time.sleep`` backoff
        between each failed attempt.  Callers patch ``after_run.time.sleep``
        to control timing in tests.

    Returns:
        The ``RunOutcome`` value matching the current worktree state.
    """
    # Slice 3b: sentinel check is FIRST — the force-pr-not-merge hook writes
    # ${PWD}/.bh-state/worker-tried-merge on a worker merge attempt, and
    # that outcome is terminal regardless of any other git or gh state.
    # See src/baton_harness/hooks/force_pr_not_merge.py.
    if (Path.cwd() / _SENTINEL_DIR / _SENTINEL_NAME).exists():
        return RunOutcome.WORKER_TRIED_MERGE

    # Step 1: uncommitted changes?
    status = _run(["git", "status", "--porcelain", "--untracked-files=no"])
    if status.stdout.strip():
        return RunOutcome.UNCOMMITTED_CHANGES

    # Resolve base SHA once here so it is stable for the rest of classify.
    base_sha = _resolve_base_sha()

    # Step 2: any commits ahead of base?
    cherry = _run(["git", "cherry", base_sha, "HEAD"])

    # MAJOR 1 (#32): a non-zero returncode means git cherry itself failed
    # (e.g. bad base SHA, detached HEAD).  Do NOT derive commit presence from
    # empty stdout — that would misclassify as NO_COMMITS and trigger
    # agent-ready removal.  Treat any non-zero exit as transient.
    if cherry.returncode != 0:
        err(
            _HOOK,
            0,
            f"git cherry failed (returncode={cherry.returncode}); "
            f"stderr: {cherry.stderr.strip()!r} — treating as "
            "TRANSIENT_ERROR, agent-ready left intact.",
        )
        return RunOutcome.TRANSIENT_ERROR

    ahead_commits = [
        line for line in cherry.stdout.splitlines() if line.startswith("+")
    ]
    if not ahead_commits:
        return RunOutcome.NO_COMMITS

    # Step 3: open PR for this branch? — guarded with retry (#32).
    branch = _current_branch()
    pr_cmd = [
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

    for attempt in range(1, _GH_PR_LIST_MAX_ATTEMPTS + 1):
        pr_result = _run(pr_cmd)

        # Treat non-zero returncode as transient — do not parse stdout.
        if pr_result.returncode != 0:
            print(
                f"[{_HOOK}] gh pr list attempt "
                f"{attempt}/{_GH_PR_LIST_MAX_ATTEMPTS} "
                f"failed (returncode={pr_result.returncode}); "
                "treating as transient.",
                flush=True,
            )
            if attempt < _GH_PR_LIST_MAX_ATTEMPTS:
                time.sleep(_GH_PR_LIST_BACKOFF_SECONDS * attempt)
            continue

        # Guard json.loads — non-JSON output (rate-limit banners, auth
        # expiry HTML, etc.) is transient, not terminal.
        try:
            prs: list[dict[str, object]] = json.loads(pr_result.stdout)
        except json.JSONDecodeError:
            print(
                f"[{_HOOK}] gh pr list attempt "
                f"{attempt}/{_GH_PR_LIST_MAX_ATTEMPTS} "
                "returned non-JSON stdout; treating as transient.",
                flush=True,
            )
            if attempt < _GH_PR_LIST_MAX_ATTEMPTS:
                time.sleep(_GH_PR_LIST_BACKOFF_SECONDS * attempt)
            continue

        # Successful parse — classify normally.
        if prs:
            return RunOutcome.PR_OPENED
        return RunOutcome.COMMITTED_NO_PR

    # All attempts exhausted without a clean response.
    print(
        f"[{_HOOK}] gh pr list failed on all {_GH_PR_LIST_MAX_ATTEMPTS} "
        "attempts; returning TRANSIENT_ERROR — agent-ready left intact.",
        flush=True,
    )
    return RunOutcome.TRANSIENT_ERROR


# ---------------------------------------------------------------------------
# Label reconciliation
# ---------------------------------------------------------------------------


def _current_labels(issue: int) -> list[str] | None:
    """Fetch the current label names for a GitHub issue.

    Parses ``gh issue view --json labels`` output with ``json.loads`` (never
    grepped — addresses the H1 root-cause pattern).

    MAJOR 2 (#32): failure is signalled distinctly from "no labels".  A
    non-zero returncode or a ``json.JSONDecodeError`` returns ``None`` (not
    ``[]``) so that ``_reconcile_labels`` can detect the failure and abort
    with zero label mutations, preserving the single-state invariant.
    Returning ``[]`` would have been misread as "issue has no labels" and
    allowed mutations to proceed against an unknown label state.

    Args:
        issue: GitHub issue number whose labels are fetched.

    Returns:
        A list of label name strings currently on the issue, or ``None``
        if the ``gh`` call failed or returned non-JSON output (signals
        fetch failure, distinct from an empty label list).
    """
    result = _run(["gh", "issue", "view", str(issue), "--json", "labels"])

    if result.returncode != 0:
        err(
            _HOOK,
            issue,
            f"gh issue view failed (returncode={result.returncode}); "
            f"stderr: {result.stderr.strip()!r} — aborting label "
            "reconciliation to preserve single-state invariant.",
        )
        return None

    try:
        data: dict[str, list[dict[str, str]]] = json.loads(result.stdout)
    except json.JSONDecodeError:
        err(
            _HOOK,
            issue,
            "gh issue view returned non-JSON stdout; aborting label "
            "reconciliation to preserve single-state invariant.",
        )
        return None

    return [lbl["name"] for lbl in data.get("labels", [])]


def _reconcile_labels(issue: int, outcome: RunOutcome) -> int:
    """Reconcile GitHub labels to enforce the single-state invariant.

    Implements the label state machine from ``harness-design.md §5``.
    Exactly one of ``agent-ready``, ``agent-done``, or ``blocked`` must
    be present after this function returns.

    Priority:
        0. If outcome is ``TRANSIENT_ERROR`` (#32), perform no label
           mutations — ``agent-ready`` is left intact so the issue remains
           eligible for a future run.  Return ``0`` (the label state is
           unchanged, not broken); ``main()`` is responsible for returning
           non-zero to the caller.
        0b. If ``_current_labels`` returns ``None`` (labels-fetch failure),
           perform ZERO label mutations and return ``1`` so that ``main()``
           returns non-zero.  Operating on an unknown label state would risk
           violating the single-state invariant (MAJOR 2, #32).
        1. If ``blocked`` is already on the issue (applied mid-run by the
           agent), remove ``agent-ready`` and leave ``blocked``.  Do NOT add
           ``agent-done`` — the block overrides the F5 classification.
        2. If outcome is ``PR_OPENED``, add ``agent-done`` and remove
           ``agent-ready``.  Log the F10 caveat: CI status is NOT checked
           (human verifies at review — pilot scope).
        3. Otherwise (``NO_COMMITS``, ``UNCOMMITTED_CHANGES``,
           ``COMMITTED_NO_PR``): remove ``agent-ready`` and add ``blocked``.
           The always-on daemon (P3) is the new retry authority — it consults
           ``blocked`` issues rather than ``agent-ready``.  The old
           "leave agent-ready for Baton retry" behaviour is deleted (P0).

    Label-edit failures are surfaced via non-zero exit codes and ``_cli.err``
    logging — they are never swallowed (H1 root cause was ``|| true``
    silencing).

    Args:
        issue: GitHub issue number whose labels are reconciled.
        outcome: The F5 classification for the current run.

    Returns:
        ``0`` on success, ``1`` if any label mutation fails or if the
        labels-fetch itself failed (to signal hook failure without mutations).
    """
    # Priority 0: transient gh failure — leave all labels untouched (#32).
    # agent-ready stays on the issue so the daemon can retry later.
    if outcome == RunOutcome.TRANSIENT_ERROR:
        log(
            _HOOK,
            issue,
            "outcome=transient-error: skipping label mutations — "
            "agent-ready left intact for future run.",
        )
        return 0

    # Priority 0.5 (slice 3b): WORKER_TRIED_MERGE emits a critical escalation
    # alert BEFORE the label-fetch so the operator is paged even if the
    # subsequent label work fails (e.g. _current_labels returns None on a
    # transient gh error).  After the alert, execution falls through to the
    # existing Priority-3 label flow (remove agent-ready, add blocked).
    if outcome == RunOutcome.WORKER_TRIED_MERGE:
        log(
            _HOOK,
            issue,
            "outcome=worker-tried-merge: force-pr-not-merge hook fired — "
            "emitting critical escalation, then applying blocked label.",
        )
        try:
            alert(
                os.environ.get("BH_REPO_OWNER", ""),
                os.environ.get("BH_REPO_NAME", ""),
                issue,
                "worker attempted to merge a PR"
                " — see .bh-state/worker-tried-merge sentinel",
                severity="critical",
            )
        except Exception as exc:  # noqa: BLE001 — escalation must not crash hook.
            err(_HOOK, issue, f"escalation alert failed: {exc}")
        # Fall through to Priority-3 label flow below (remove agent-ready,
        # add blocked).

    # MAJOR 2 (#32): fetch labels BEFORE any mutation path.  None signals a
    # fetch failure (non-JSON or non-zero returncode) — distinct from [] which
    # means the issue genuinely has no labels.  Operating on an unknown label
    # state would risk violating the single-state invariant, so abort with
    # zero mutations and return non-zero.
    labels = _current_labels(issue)
    if labels is None:
        err(
            _HOOK,
            issue,
            "labels-fetch failed — aborting reconciliation with zero label "
            "mutations to preserve the single-state invariant.",
        )
        return 1

    # Priority 1: blocked label wins regardless of F5 outcome.
    if LABEL_BLOCKED in labels:
        # Block is not terminal at the Baton level — see #23 for the
        # upstream-dependent terminal-block fix; the harness only enforces
        # the single-state label invariant here.
        log(
            _HOOK,
            issue,
            f"blocked label present — removing {LABEL_AGENT_READY!r}; "
            "leaving 'blocked' in place.",
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
        #
        # Loop-resilience (Finding B / issue #21): remove 'agent-ready' FIRST
        # to exit the eligible set before adding 'agent-done'.  This ensures
        # a partial failure (add succeeds, remove fails — or vice versa) never
        # leaves the issue eligible for re-dispatch.  Even if add-agent-done
        # subsequently fails we return non-zero and log loudly, but the issue
        # is already ineligible.
        log(
            _HOOK,
            issue,
            "outcome=pr-opened: removing 'agent-ready' first (if present), "
            "then adding 'agent-done'. "
            "CAVEAT(F10): agent-done means a PR exists, NOT that CI is green"
            " — human verifies at review.",
        )
        # Idempotency guard (#31 AC1): only remove agent-ready when it is
        # actually present.  On a re-run after a kill-between-remove-and-add,
        # agent-ready is already gone; issuing an unconditional remove causes
        # gh to exit non-zero, which aborts the function before agent-done is
        # added and leaves torn state intact across re-runs.  Mirrors the
        # guard pattern already used in Priority 1 (:456) and Priority 3
        # (:549).  The remove-before-add ordering (Finding B / #21) is
        # preserved: when agent-ready IS present, remove still precedes add.
        if LABEL_AGENT_READY in labels:
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
        return 0

    # Priority 3 (P0 change): NOT retryable via Baton anymore — the always-on
    # daemon is the new retry authority.  Remove agent-ready and set blocked so
    # the daemon can decide when and whether to retry.
    #
    # Old behaviour (deleted): leave agent-ready in place for Baton retry.
    # New behaviour: remove agent-ready + add blocked.
    log(
        _HOOK,
        issue,
        f"outcome={outcome.value}: removing {LABEL_AGENT_READY!r} and "
        f"setting {LABEL_BLOCKED!r} — daemon controls retry (Priority-3 "
        "carry-forward deleted, P0).",
    )
    # Remove agent-ready if present.
    if LABEL_AGENT_READY in labels:
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
    # Add blocked.
    add_result = _run(
        [
            "gh",
            "issue",
            "edit",
            str(issue),
            "--add-label",
            LABEL_BLOCKED,
        ]
    )
    if add_result.returncode != 0:
        err(
            _HOOK,
            issue,
            f"failed to add {LABEL_BLOCKED!r}: {add_result.stderr.strip()}",
        )
        return 1
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``bh-after-run`` console script.

    Resolves the issue number from the current working directory, classifies
    the run outcome (F5), and reconciles GitHub labels to a single state
    (harness-design.md §5 / H1 fix).

    Transient failure handling (#32): if ``_classify`` returns
    ``TRANSIENT_ERROR`` (all ``gh pr list`` attempts exhausted), this
    function returns ``1`` after reconciling labels as a no-op.  The
    non-zero exit signals the daemon/Baton that the hook did not complete
    successfully, without altering the issue's label state.

    Args:
        argv: Unused; reserved for future CLI argument support.  Baton
            passes no env-var context to hooks (spike finding F2), so
            all context is derived from the worktree directory name.

    Returns:
        Exit code: ``0`` on success, ``1`` on any failure (unresolvable
        issue number, transient gh failure, or a label-edit error that
        must not be swallowed).
    """
    issue = resolve_issue_number()
    if issue is None:
        print(
            f"[{_HOOK}] error: could not derive issue number from cwd — "
            "expected a bare integer (Baton: .symphony/worktrees/<issue>) "
            "or <prefix>-<issue>[-<slug>] (harness: .worktrees/<branch>)",
            file=sys.stderr,
            flush=True,
        )
        return 1

    log(_HOOK, issue, "classifying run outcome (F5)...")
    outcome = _classify()
    log(_HOOK, issue, f"outcome={outcome.value}")

    reconcile_rc = _reconcile_labels(issue, outcome)
    if reconcile_rc != 0:
        return reconcile_rc

    # TRANSIENT_ERROR: _reconcile_labels is a no-op (agent-ready intact),
    # but main() must still signal failure to the caller (#32).
    if outcome == RunOutcome.TRANSIENT_ERROR:
        err(
            _HOOK,
            issue,
            "transient gh pr list failure — hook returning non-zero; "
            "agent-ready is intact for a future run.",
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
