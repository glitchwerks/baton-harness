"""State-label constants and single-state invariant checker for the chain.

Defines the three mutually-exclusive GitHub state labels that drive the
baton-harness issue-state machine, and provides a pure checker that
asserts exactly one of them is present on a given issue at any time.

Direction rule: chain modules may import from this module; hooks (e.g.
``after_run``) import FROM this module.  This module must never import
from hooks.
"""

from __future__ import annotations

from collections.abc import Iterable

# ---------------------------------------------------------------------------
# State-label constants
# ---------------------------------------------------------------------------

#: Label that marks an issue as ready for an agent run.
LABEL_AGENT_READY: str = "agent-ready"

#: Label applied when the agent has opened a PR (awaiting CI/merge).
LABEL_AGENT_DONE: str = "agent-done"

#: Label applied when the agent or daemon detects a blocking condition.
LABEL_BLOCKED: str = "blocked"

#: The complete set of mutually-exclusive state labels.
#:
#: Exactly one member of this set must be present on every tracked issue
#: at all times.  Membership here defines what ``assert_single_state``
#: counts.
STATE_LABELS: frozenset[str] = frozenset(
    {LABEL_AGENT_READY, LABEL_AGENT_DONE, LABEL_BLOCKED}
)


# ---------------------------------------------------------------------------
# Invariant checker
# ---------------------------------------------------------------------------


def assert_single_state(labels: Iterable[str]) -> str | None:
    """Check that exactly one state label is present in *labels*.

    This is a **pure checker** — it never raises and never mutates state.
    Callers are expected to act on the returned violation string (e.g. fire
    an alert, park the issue) rather than catch an exception.

    Non-state labels (anything not in ``STATE_LABELS``) are ignored for
    the purpose of the count.

    Args:
        labels: Any iterable of GitHub label name strings.  Lists, sets,
            and generators are all accepted; the iterable is materialised
            internally.

    Returns:
        ``None`` when exactly one state label is found — the invariant
        holds.  A non-empty human-readable violation string when zero or
        two-or-more state labels are found, naming the offending labels so
        callers can include it in alert messages and runlog events.
        A non-empty violation string is also returned when the *labels*
        argument is malformed (e.g. contains unhashable members); the
        function never raises regardless of input.
    """
    try:
        found: frozenset[str] = frozenset(labels) & STATE_LABELS
    except Exception as exc:  # noqa: BLE001 — pure checker must never raise
        return f"label invariant uncheckable: malformed labels input ({exc})"
    count = len(found)
    if count == 1:
        return None
    if count == 0:
        return (
            "label invariant violated: no state label found "
            f"(expected exactly one of {sorted(STATE_LABELS)})"
        )
    # count >= 2
    found_sorted = sorted(found)
    return (
        f"label invariant violated: {count} state labels found "
        f"({', '.join(found_sorted)}); expected exactly one"
    )


# ---------------------------------------------------------------------------
# Observed-fact reconciler (Issue #31 AC2)
# ---------------------------------------------------------------------------


def target_state_from_observed(blocked: bool, pr_open: bool) -> str:
    """Re-derive the correct single-state label from observable facts.

    This is a **pure function** — no I/O, no side-effects, never raises.
    It encodes the label-state-machine precedence rules from
    harness-design.md §5 so that any caller can re-derive the intended
    label state independent of which hook last ran (idempotent AC2
    reconciler).

    Precedence (highest to lowest):
    1. ``blocked=True``  → ``LABEL_BLOCKED`` (blocking condition wins
       regardless of PR state; the issue must not be marked done until
       the block is resolved).
    2. ``blocked=False, pr_open=True`` → ``LABEL_AGENT_DONE`` (agent
       opened a PR and no blocker is active).
    3. ``blocked=False, pr_open=False`` → ``LABEL_AGENT_READY`` (no PR,
       no block; the issue is ready for another agent run).

    The return value is always a member of ``STATE_LABELS``.

    Args:
        blocked: Whether the ``blocked`` label is currently present on
            the issue (i.e. a blocking condition is active).
        pr_open: Whether the agent has an open pull request associated
            with this issue.

    Returns:
        The single-state label string the issue should carry given the
        supplied observable facts.  Always one of ``LABEL_BLOCKED``,
        ``LABEL_AGENT_DONE``, or ``LABEL_AGENT_READY``.
    """
    if blocked:
        return LABEL_BLOCKED
    if pr_open:
        return LABEL_AGENT_DONE
    return LABEL_AGENT_READY
