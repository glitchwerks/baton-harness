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
