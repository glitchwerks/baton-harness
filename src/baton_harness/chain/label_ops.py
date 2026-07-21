"""Label-fetch I/O for the chain (daemon, after_run, recovery).

Houses three separately-named GitHub label-fetch functions, relocated
verbatim from their original call sites (issue #272, phase 4 of the
module-refactor proposal #268).  They are **not** unified into a single
helper — each site's signature, case-normalization, and failure-sentinel
contract were confirmed concretely incompatible during the Codex
escalation review that rewrote this phase's scope:

- :func:`fetch_daemon_labels` — lowercase; takes ``repo``/``token``
  params; failure returns ``None``.
- :func:`fetch_after_run_labels` — case-preserving; uses the ambient
  repository (no ``repo``/``token`` params); emits hook errors on
  failure rather than returning a sentinel.
- :func:`fetch_recovery_labels` — lowercase; takes ``repo``/``token``
  params; failure returns an **empty set**, not ``None``.

Each function still calls back into its origin module's own private
subprocess seam (``daemon._run_gh``, ``after_run._run``,
``recovery._run``) via a deferred (function-body) import, rather than
invoking ``subprocess.run`` directly.  This is required, not
incidental: the existing test suites patch those module-local seams
(and, for ``daemon``/``recovery``, patch the label-fetch function
itself by its original dotted path) to exercise the callers
end-to-end, so relocating the *logic* without preserving the *seam*
would silently stop those tests from intercepting the ``gh`` calls
they're pinning. The deferred import (rather than a top-level one)
also avoids a circular import: each origin module imports its
relocated function back under its original private name (e.g.
``daemon.py`` does
``from baton_harness.chain.label_ops import fetch_daemon_labels as
_fetch_issue_labels``) so that existing ``mock.patch("...daemon.
_fetch_issue_labels", ...)`` call sites keep working unmodified.
"""

from __future__ import annotations

import json

from baton_harness._cli import err
from baton_harness.chain.app_auth import InstallationTokenSource, gh_env

# ---------------------------------------------------------------------------
# fetch_daemon_labels — relocated from daemon._fetch_issue_labels
# ---------------------------------------------------------------------------


def fetch_daemon_labels(
    owner: str,
    repo: str,
    issue: int,
    *,
    installation_token: InstallationTokenSource = "",
) -> set[str] | None:
    """Fetch current labels for an issue (lowercase).

    Returns ``None`` on any fetch failure so callers can distinguish
    an unreadable state from a genuinely empty label set.  This mirrors
    the sentinel pattern used by ``fetch_after_run_labels`` (#32).

    On a ``gh`` call failure (``returncode != 0``) the issue may still
    carry ``blocked`` or other state labels that we cannot see — returning
    ``None`` forces the caller to handle the unknown state conservatively
    rather than treating it as zero-state and triggering convergence.

    Args:
        owner: The GitHub repository owner.
        repo: The repository name.
        issue: The issue number.
        installation_token: Optional GitHub App installation access token.
            When non-empty, overrides ``GH_TOKEN`` in the subprocess env
            via a per-call copy — ``os.environ`` is never mutated.

    Returns:
        A ``set[str]`` of lowercase label name strings when the fetch
        succeeds (possibly empty — a genuine empty set is distinct from
        failure).  ``None`` when the ``gh`` call returns a non-zero exit
        code or when the response cannot be parsed (``JSONDecodeError``,
        ``KeyError``, or ``TypeError``).  Callers must guard on ``None``
        and must NOT attempt single-state convergence on an unknown state
        (Codex P1 #3, PR #95).
    """
    # Deferred import — see module docstring: avoids a circular import
    # with daemon.py (which imports this function back under the
    # ``_fetch_issue_labels`` name) and preserves daemon's patchable
    # ``_run`` seam via ``_run_gh``.
    from baton_harness.chain import daemon as _daemon_mod

    _gh_call_env = gh_env(installation_token) if installation_token else None
    proc = _daemon_mod._run_gh(
        [
            "gh",
            "issue",
            "view",
            str(issue),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "labels",
        ],
        _gh_call_env,
    )
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
        return {lbl["name"].lower() for lbl in data.get("labels", [])}
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


# ---------------------------------------------------------------------------
# fetch_after_run_labels — relocated from after_run._current_labels
# ---------------------------------------------------------------------------


def fetch_after_run_labels(issue: int) -> list[str] | None:
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
    # Deferred import — see module docstring: avoids a circular import
    # with after_run.py (which imports this function back under the
    # ``_current_labels`` name) and preserves after_run's patchable
    # ``_run`` seam.  ``err`` is imported directly from ``_cli`` (its
    # origin) rather than via ``after_run.err`` — after_run.py imports
    # ``err`` without re-exporting it, so accessing it as a module
    # attribute from outside trips mypy's ``no_implicit_reexport``; no
    # test patches ``after_run.err`` while exercising this fetch path
    # (it's only patched alongside a wholesale ``_current_labels``
    # replacement, which bypasses this function entirely), so sourcing
    # it directly from ``_cli`` is behavior-preserving.
    from baton_harness import after_run as _after_run_mod

    result = _after_run_mod._run(
        ["gh", "issue", "view", str(issue), "--json", "labels"]
    )

    if result.returncode != 0:
        err(
            _after_run_mod._HOOK,
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
            _after_run_mod._HOOK,
            issue,
            "gh issue view returned non-JSON stdout; aborting label "
            "reconciliation to preserve single-state invariant.",
        )
        return None

    return [lbl["name"] for lbl in data.get("labels", [])]


# ---------------------------------------------------------------------------
# fetch_recovery_labels — relocated from recovery._fetch_labels
# ---------------------------------------------------------------------------


def fetch_recovery_labels(
    owner: str,
    repo: str,
    issue: int,
    *,
    installation_token: InstallationTokenSource = "",
) -> set[str]:
    """Fetch the current labels for an issue (lowercase).

    Calls ``gh issue view <N> --json labels`` and returns a set of
    lowercase label names.

    Args:
        owner: The GitHub repository owner.
        repo: The repository name.
        issue: The issue number.
        installation_token: Optional GitHub App installation access token
            (``ghs_`` prefix).  When non-empty, the ``gh`` subprocess uses
            a per-call env copy with ``GH_TOKEN`` overridden —
            ``os.environ`` is never mutated.

    Returns:
        A set of lowercase label name strings.  Returns an empty set on
        error (best-effort; a failed label fetch is not fatal for recovery).
    """
    # Deferred import — see module docstring: avoids a circular import
    # with recovery.py (which imports this function back under the
    # ``_fetch_labels`` name) and preserves recovery's patchable ``_run``
    # seam.
    from baton_harness.chain import recovery as _recovery_mod

    env = gh_env(installation_token) if installation_token else None
    proc = _recovery_mod._run(
        [
            "gh",
            "issue",
            "view",
            str(issue),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "labels",
        ],
        env=env,
    )
    if proc.returncode != 0:
        _recovery_mod._log.debug(
            "recovery: gh issue view failed for #%d (exit %d): %s",
            issue,
            proc.returncode,
            proc.stderr,
        )
        return set()
    try:
        data = json.loads(proc.stdout)
        return {lbl["name"].lower() for lbl in data.get("labels", [])}
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        _recovery_mod._log.debug(
            "recovery: label parse error for #%d: %s", issue, exc
        )
        return set()
