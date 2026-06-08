"""Topological work-unit scheduler with sub-tree parking.

Wraps ``graphlib.TopologicalSorter`` to provide a ready frontier, done
signalling, and sub-tree parking for failed or blocked issues.

The graph convention is ``{issue: [blocker_issues]}`` — identical to the
adjacency map produced by ``dag.build_dag``.  An issue is "ready" when all
of its blockers have been marked done.

``TopologicalSorter`` does not model failure.  ``IssueScheduler`` keeps a
separate ``parked`` set (failed or blocked issues plus their transitive
dependents) and filters ``get_ready()`` output against it.  Parked nodes
are never dispatched again within the current work unit.

Cycle detection is free: ``graphlib.TopologicalSorter.prepare()`` raises
``graphlib.CycleError`` when the graph contains a cycle.

Usage::

    sched = IssueScheduler(dag_result.graph)
    sched.prepare()                       # raises CycleError if cyclic
    while sched.is_active():
        for issue in sched.get_ready():
            result = run_worker(issue)
            if result == "done":
                sched.mark_done(issue)
            else:
                sched.mark_parked(issue)  # parks issue + dependents

Note:
    ``get_ready()`` must be called to consume the ready nodes from the
    underlying ``TopologicalSorter`` before ``mark_done`` can be called.
    This follows the standard ``graphlib`` usage contract.
"""

from __future__ import annotations

import graphlib
from collections import defaultdict


class IssueScheduler:
    """Topological scheduler with parked-sub-tree filtering.

    Wraps ``graphlib.TopologicalSorter`` so that ``get_ready()`` returns
    only issues whose blockers are all done AND that are not parked.

    Attributes:
        parked: The set of parked issue numbers (failed, blocked, or
            transitively affected by a parked blocker).
    """

    def __init__(self, graph: dict[int, list[int]]) -> None:
        """Initialise the scheduler from a ``{issue: [blockers]}`` graph.

        Does **not** call ``prepare()`` — the caller must do that explicitly
        so ``CycleError`` is raised in a controlled context.

        Args:
            graph: Adjacency map ``{issue: [blocker_issues]}`` as produced
                by ``dag.build_dag``.
        """
        self._sorter: graphlib.TopologicalSorter[int] = (
            graphlib.TopologicalSorter(graph)
        )
        self.parked: set[int] = set()
        # Build a reverse adjacency (dependents of each node) for transitive
        # parking.  ``dependents[n]`` = issues that list ``n`` as a blocker.
        self._dependents: dict[int, list[int]] = defaultdict(list)
        for issue, blockers in graph.items():
            for blocker in blockers:
                self._dependents[blocker].append(issue)
        # Track all nodes for is_active bookkeeping.
        self._all_nodes: set[int] = set(graph.keys())
        self._done: set[int] = set()
        # Track consumed nodes (graphlib requires get_ready → done pairing).
        self._consumed: set[int] = set()

    def prepare(self) -> None:
        """Prepare the topological sorter for iteration.

        Must be called before ``get_ready()``.  Raises
        ``graphlib.CycleError`` if the graph contains a cycle.

        Raises:
            graphlib.CycleError: If the graph is not a DAG.
        """
        self._sorter.prepare()

    def get_ready(self) -> set[int]:
        """Return the current ready frontier, excluding parked nodes.

        Returns the set of issues whose blockers are all marked done AND
        that are not in the ``parked`` set.  This consumes the ready nodes
        from the underlying sorter so they can be marked done later.

        Returns:
            A set of issue numbers ready for dispatch.  May be empty if all
            remaining nodes are blocked or parked.
        """
        ready_raw: set[int] = set(self._sorter.get_ready())
        self._consumed.update(ready_raw)
        return ready_raw - self.parked

    def mark_done(self, issue: int) -> None:
        """Mark an issue as completed, unblocking its dependents.

        Must only be called for issues that were returned by ``get_ready()``
        (the ``graphlib`` contract).

        Args:
            issue: The issue number to mark as done.
        """
        self._sorter.done(issue)
        self._done.add(issue)

    def mark_parked(self, issue: int) -> None:
        """Park an issue and its transitive dependents.

        A parked issue (and all issues that depend on it, directly or
        indirectly) will never appear in ``get_ready()`` output again.

        Args:
            issue: The issue number to park.  May or may not have been
                returned by ``get_ready()`` yet.
        """
        to_park: list[int] = [issue]
        while to_park:
            node = to_park.pop()
            if node in self.parked:
                continue
            self.parked.add(node)
            to_park.extend(self._dependents.get(node, []))

    def is_active(self) -> bool:
        """Return True while the sorter has undispatched or in-progress work.

        Returns False when every node is either done or parked — i.e. the
        per-DAG loop should terminate.

        Returns:
            ``True`` if there are nodes not yet done or parked; ``False``
            when all nodes have reached a terminal state.
        """
        terminal = self._done | self.parked
        return not self._all_nodes.issubset(terminal)
