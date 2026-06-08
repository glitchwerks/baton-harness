"""Unit tests for baton_harness.chain.dag.

``dag.py`` is a pure module (no I/O) that builds an adjacency map from
``blocked_by`` edge data scoped to a membership set, and exposes the
membership for downstream use.

Coverage:
- ``build_dag`` returns a correct ``{issue: [blockers]}`` adjacency map.
- Blockers outside the membership set are excluded (same-repo scoping).
- An issue in the membership with no blockers appears with an empty list.
- Issues with no blockers and no dependents appear in the graph.
- A cyclic adjacency IS buildable by ``build_dag``; cycle detection is
  the scheduler's responsibility (it raises ``CycleError`` at prepare).
- ``DagResult.membership`` reflects the original membership set.
- ``DagResult.graph`` is keyed by every member.
"""

from __future__ import annotations

import pytest

from baton_harness.chain.dag import DagResult, build_dag


class TestBuildDagGraph:
    """Tests for the graph adjacency produced by ``build_dag``."""

    def test_single_issue_no_blockers(self) -> None:
        """A single-issue membership with no edges returns one empty entry."""
        result = build_dag(
            membership=frozenset({10}),
            blocked_by={10: []},
        )
        assert result.graph == {10: []}

    def test_linear_chain(self) -> None:
        """A → B → C chain produces correct adjacency entries."""
        # 10 is blocked by 11; 11 is blocked by 12; 12 has no blockers
        result = build_dag(
            membership=frozenset({10, 11, 12}),
            blocked_by={10: [11], 11: [12], 12: []},
        )
        assert result.graph[10] == [11]
        assert result.graph[11] == [12]
        assert result.graph[12] == []

    def test_diamond_dag(self) -> None:
        """A diamond dependency (two paths converging) is represented."""
        # 10 blocked by 11 and 12; both 11, 12 blocked by 13
        result = build_dag(
            membership=frozenset({10, 11, 12, 13}),
            blocked_by={10: [11, 12], 11: [13], 12: [13], 13: []},
        )
        assert set(result.graph[10]) == {11, 12}
        assert result.graph[11] == [13]
        assert result.graph[12] == [13]
        assert result.graph[13] == []

    def test_blockers_outside_membership_are_excluded(self) -> None:
        """Blockers not in the membership set are silently dropped."""
        # Issue 10 is blocked by 99, but 99 is not a member
        result = build_dag(
            membership=frozenset({10}),
            blocked_by={10: [99]},
        )
        assert result.graph[10] == []

    def test_partial_blockers_outside_membership_filtered(self) -> None:
        """Only in-membership blockers survive; out-of-membership dropped."""
        result = build_dag(
            membership=frozenset({10, 11}),
            blocked_by={10: [11, 999], 11: []},
        )
        assert result.graph[10] == [11]
        assert result.graph[11] == []

    def test_all_members_appear_as_keys(self) -> None:
        """Every member appears as a key even if absent from blocked_by."""
        result = build_dag(
            membership=frozenset({10, 11, 12}),
            blocked_by={10: [11]},
        )
        assert set(result.graph.keys()) == {10, 11, 12}

    def test_empty_membership_returns_empty_graph(self) -> None:
        """An empty membership produces an empty graph."""
        result = build_dag(membership=frozenset(), blocked_by={})
        assert result.graph == {}

    def test_cyclic_adjacency_is_buildable(self) -> None:
        """Build_dag accepts cycles; the scheduler raises CycleError."""
        # 10 → 11 → 10 is cyclic; build_dag should not raise
        result = build_dag(
            membership=frozenset({10, 11}),
            blocked_by={10: [11], 11: [10]},
        )
        assert result.graph[10] == [11]
        assert result.graph[11] == [10]


class TestBuildDagMembership:
    """Tests for the membership set exposed on ``DagResult``."""

    def test_membership_equals_input(self) -> None:
        """DagResult.membership is the original membership frozenset."""
        members = frozenset({1, 2, 3})
        result = build_dag(membership=members, blocked_by={})
        assert result.membership == members

    def test_membership_is_frozenset(self) -> None:
        """DagResult.membership is a frozenset (immutable)."""
        result = build_dag(membership=frozenset({5}), blocked_by={})
        assert isinstance(result.membership, frozenset)


class TestDagResult:
    """Tests for the ``DagResult`` dataclass."""

    def test_dag_result_has_graph_attribute(self) -> None:
        """DagResult exposes a .graph attribute."""
        result = build_dag(membership=frozenset({1}), blocked_by={})
        assert hasattr(result, "graph")

    def test_dag_result_has_membership_attribute(self) -> None:
        """DagResult exposes a .membership attribute."""
        result = build_dag(membership=frozenset({1}), blocked_by={})
        assert hasattr(result, "membership")

    def test_dag_result_is_not_none(self) -> None:
        """build_dag always returns a DagResult, never None."""
        result = build_dag(membership=frozenset(), blocked_by={})
        assert result is not None
        assert isinstance(result, DagResult)


class TestCyclicGraphPassthrough:
    """Verify the cyclic-graph contract: build_dag builds, scheduler rejects.

    This test documents the interface contract: build_dag itself does NOT
    raise CycleError.  The scheduler's prepare() call does.
    """

    def test_cycle_does_not_raise_in_build_dag(self) -> None:
        """A cyclic blocked_by map does not raise during build_dag."""
        try:
            build_dag(
                membership=frozenset({1, 2}),
                blocked_by={1: [2], 2: [1]},
            )
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"build_dag raised unexpectedly: {exc}")
