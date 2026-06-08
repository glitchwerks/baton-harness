"""Chain package — DAG-ordered work-unit orchestration for the daemon.

This package provides the components needed to read a dependency DAG from
GitHub's native issue-dependencies REST API, build a topological schedule,
and manage execution state (ready, done, parked) across a milestone work
unit.

Modules:
    gh_deps:   Read ``blocked_by``/``blocking`` edges and milestone
               membership via ``gh api`` subprocess calls.
    dag:       Pure adjacency-map builder from edge data scoped to a
               membership set.  No I/O.
    scheduler: Wrap ``graphlib.TopologicalSorter`` with a ``parked`` set
               for failed/blocked sub-tree exclusion.  No I/O.
    registry:  Single-entry repo registry (owner, repo, project_root).
"""
