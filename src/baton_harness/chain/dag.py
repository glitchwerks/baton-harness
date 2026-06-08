"""Pure DAG builder from ``blocked_by`` edge data scoped to a membership set.

This module is **pure** — it performs no I/O.  It accepts pre-fetched edge
data (from ``gh_deps``) and a membership set (the issues belonging to the
current work unit), and produces a ``DagResult`` holding:

- ``graph``: ``{issue_number: [blocker_numbers]}`` adjacency map, keyed by
  every member.  Blockers outside the membership set are silently excluded
  (same-repo scoping: an issue in a different milestone or un-milestoned is
  not a dependency of the current work unit, even if linked).
- ``membership``: the original membership frozenset, exposed for downstream
  use (e.g. scheduler initialisation, registry lookups).

Cycle detection is **not** performed here.  ``build_dag`` will happily build
a cyclic adjacency map.  The caller is responsible for detecting cycles via
``graphlib.TopologicalSorter.prepare()`` (``scheduler.IssueScheduler``), which
raises ``graphlib.CycleError``.  This separation keeps ``dag.py`` pure and
``scheduler.py`` the sole cycle-detection authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DagResult:
    """The result of building a dependency DAG for a work unit.

    Attributes:
        graph: Adjacency map ``{issue: [blocker_issues]}``.  Every member
            appears as a key; blockers outside the membership are excluded.
        membership: The original membership frozenset passed to
            ``build_dag``.  Preserved for downstream consumers.
    """

    graph: dict[int, list[int]] = field(default_factory=dict)
    membership: frozenset[int] = field(default_factory=frozenset)


def build_dag(
    membership: frozenset[int],
    blocked_by: dict[int, list[int]],
) -> DagResult:
    """Build a dependency adjacency map scoped to the given membership set.

    For each issue in ``membership``, collects the blockers listed in
    ``blocked_by`` that are themselves in ``membership``.  Blockers outside
    ``membership`` are silently dropped (cross-milestone or external links
    are not actionable within this work unit).

    Issues that appear in ``membership`` but not in ``blocked_by`` are
    included in the graph with an empty blocker list (they have no
    dependencies within the work unit).

    This function does **not** validate for cycles; call
    ``IssueScheduler.prepare()`` on the resulting graph to detect them.

    Args:
        membership: The set of issue numbers belonging to the current work
            unit (a milestone or a single un-milestoned issue).
        blocked_by: Pre-fetched mapping of ``{issue: [blocker_numbers]}``.
            Values may include numbers outside ``membership``; those are
            filtered out.

    Returns:
        A ``DagResult`` whose ``graph`` is keyed by every member and whose
        ``membership`` equals the input ``membership`` frozenset.
    """
    graph: dict[int, list[int]] = {}
    for issue in membership:
        raw_blockers = blocked_by.get(issue, [])
        # Keep only blockers that are themselves members of this work unit.
        in_scope = [b for b in raw_blockers if b in membership]
        graph[issue] = in_scope
    return DagResult(graph=graph, membership=membership)
