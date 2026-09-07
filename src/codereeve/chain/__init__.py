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
    branches:  Feature-branch lifecycle: naming, create off main, HEAD
               checkout before each ``_run_worker`` call, cut-point SHA
               recording.
    merge:     CI-gated ``--no-ff`` merge into ``feature/<slug>``; green
               predicate (§3.3.1); daemon-provenance trailer; persistence
               of the CI-green-at-merge fact.
"""
