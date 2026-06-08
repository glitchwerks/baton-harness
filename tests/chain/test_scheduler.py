"""Unit tests for baton_harness.chain.scheduler.

``scheduler.py`` wraps ``graphlib.TopologicalSorter`` with a ``parked``
set so failed or blocked issues and their transitive dependents are
excluded from the ready frontier.

Coverage:
- ``get_ready()`` returns the initial ready frontier (issues with no
  un-done blockers).
- ``mark_done(n)`` unblocks dependents — they appear in the next
  ``get_ready()`` call.
- ``mark_parked(n)`` removes ``n`` AND its transitive dependents from
  the ready frontier, forever.
- ``is_active()`` returns True while work remains; False when all nodes
  are either done or parked.
- ``get_ready()`` filters out parked issues.
- ``prepare()`` raises ``graphlib.CycleError`` on a cyclic graph.
- ``parked`` set is accessible for inspection.
"""

from __future__ import annotations

import graphlib

import pytest

from baton_harness.chain.scheduler import IssueScheduler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scheduler(graph: dict[int, list[int]]) -> IssueScheduler:
    """Build and prepare an IssueScheduler from a graph dict.

    Args:
        graph: Adjacency map ``{issue: [blocker_issues]}``.

    Returns:
        A prepared ``IssueScheduler`` ready for use.
    """
    sched = IssueScheduler(graph)
    sched.prepare()
    return sched


# ---------------------------------------------------------------------------
# Initial ready frontier
# ---------------------------------------------------------------------------


class TestGetReady:
    """Tests for the initial ready frontier from ``get_ready()``."""

    def test_single_node_no_blockers_is_immediately_ready(self) -> None:
        """A single node with no blockers is in the initial ready set."""
        sched = _make_scheduler({10: []})
        assert 10 in sched.get_ready()

    def test_leaf_node_is_ready_before_dependent(self) -> None:
        """The leaf (no-blocker) node is ready; its dependent is not."""
        # 10 blocked by 11; 11 has no blockers → 11 is immediately ready
        sched = _make_scheduler({10: [11], 11: []})
        ready = sched.get_ready()
        assert 11 in ready
        assert 10 not in ready

    def test_multiple_independent_nodes_all_ready(self) -> None:
        """Multiple independent nodes are all in the initial ready set."""
        sched = _make_scheduler({10: [], 11: [], 12: []})
        ready = sched.get_ready()
        assert {10, 11, 12}.issubset(ready)

    def test_empty_graph_is_not_active(self) -> None:
        """An empty graph is immediately inactive."""
        sched = _make_scheduler({})
        assert not sched.is_active()


# ---------------------------------------------------------------------------
# mark_done
# ---------------------------------------------------------------------------


class TestMarkDone:
    """Tests for ``mark_done`` unblocking dependents."""

    def test_mark_done_unblocks_direct_dependent(self) -> None:
        """Marking a leaf done makes its direct dependent ready."""
        sched = _make_scheduler({10: [11], 11: []})
        _ = sched.get_ready()  # consume 11 from the sorter
        sched.mark_done(11)
        assert 10 in sched.get_ready()

    def test_mark_done_chain_unblocks_transitively(self) -> None:
        """Marking done in order unblocks a three-level chain."""
        # 10 blocked by 11; 11 blocked by 12; 12 has no blockers
        sched = _make_scheduler({10: [11], 11: [12], 12: []})
        _ = sched.get_ready()  # consumes 12
        sched.mark_done(12)
        _ = sched.get_ready()  # consumes 11
        sched.mark_done(11)
        assert 10 in sched.get_ready()

    def test_is_active_false_after_all_done(self) -> None:
        """is_active() returns False after all nodes are marked done."""
        sched = _make_scheduler({10: [], 11: []})
        _ = sched.get_ready()
        sched.mark_done(10)
        sched.mark_done(11)
        assert not sched.is_active()

    def test_is_active_true_while_work_remains(self) -> None:
        """is_active() returns True while any node is not done/parked."""
        sched = _make_scheduler({10: [11], 11: []})
        assert sched.is_active()


# ---------------------------------------------------------------------------
# mark_parked
# ---------------------------------------------------------------------------


class TestMarkParked:
    """Tests for ``mark_parked`` halting the affected sub-tree."""

    def test_parked_node_not_in_get_ready(self) -> None:
        """A parked node does not appear in get_ready()."""
        sched = _make_scheduler({10: [], 11: []})
        sched.mark_parked(10)
        ready = sched.get_ready()
        assert 10 not in ready

    def test_transitive_dependents_are_parked(self) -> None:
        """Parking a leaf also parks its transitive dependents."""
        # 10 blocked by 11; 11 blocked by 12
        # If 12 is parked, 11 and 10 should also be parked
        sched = _make_scheduler({10: [11], 11: [12], 12: []})
        sched.mark_parked(12)
        # After parking 12, get_ready is empty (11 and 10 are blocked by 12)
        # But they should also be in the parked set
        assert 12 in sched.parked
        assert 11 in sched.parked
        assert 10 in sched.parked

    def test_parking_middle_node_parks_dependents_only(self) -> None:
        """Parking a mid-chain node parks it and dependents, not blockers."""
        # Chain: 10 → 11 → 12 (10 blocked by 11, 11 blocked by 12)
        # Parking 11 should park 11 and 10 but NOT 12
        sched = _make_scheduler({10: [11], 11: [12], 12: []})
        sched.mark_parked(11)
        assert 11 in sched.parked
        assert 10 in sched.parked
        assert 12 not in sched.parked

    def test_independent_branch_not_parked(self) -> None:
        """Parking one branch does not affect an independent branch."""
        # Two independent chains: 10→11 and 20→21
        sched = _make_scheduler({10: [11], 11: [], 20: [21], 21: []})
        sched.mark_parked(11)
        assert 10 in sched.parked
        assert 11 in sched.parked
        assert 20 not in sched.parked
        assert 21 not in sched.parked

    def test_is_active_false_when_all_parked(self) -> None:
        """is_active() returns False when all nodes are parked (none done)."""
        sched = _make_scheduler({10: [], 11: []})
        sched.mark_parked(10)
        sched.mark_parked(11)
        assert not sched.is_active()

    def test_parked_set_is_accessible(self) -> None:
        """The parked set is accessible via the .parked attribute."""
        sched = _make_scheduler({10: []})
        assert hasattr(sched, "parked")
        sched.mark_parked(10)
        assert isinstance(sched.parked, set)


# ---------------------------------------------------------------------------
# Parked filter on get_ready
# ---------------------------------------------------------------------------


class TestGetReadyFilteredByParked:
    """Tests that get_ready() excludes parked nodes."""

    def test_get_ready_excludes_parked(self) -> None:
        """get_ready() does not return a parked node."""
        sched = _make_scheduler({10: [], 11: []})
        sched.mark_parked(11)
        # 10 is not parked; 11 is parked → only 10 in ready
        ready = sched.get_ready()
        assert 11 not in ready

    def test_get_ready_after_all_ready_parked_returns_empty(self) -> None:
        """get_ready() returns empty when all ready nodes are parked."""
        sched = _make_scheduler({10: [], 11: []})
        sched.mark_parked(10)
        sched.mark_parked(11)
        assert sched.get_ready() == set()

    def test_get_ready_returns_unparked_from_mixed_ready(self) -> None:
        """get_ready() returns only unparked nodes from a mixed ready set."""
        sched = _make_scheduler({10: [], 11: [], 12: []})
        sched.mark_parked(11)
        ready = sched.get_ready()
        assert 10 in ready
        assert 12 in ready
        assert 11 not in ready


# ---------------------------------------------------------------------------
# is_active termination
# ---------------------------------------------------------------------------


class TestIsActive:
    """Tests for the ``is_active`` loop-termination signal."""

    def test_is_active_true_at_start(self) -> None:
        """is_active() returns True before any work is done."""
        sched = _make_scheduler({10: [11], 11: []})
        assert sched.is_active()

    def test_is_active_false_after_all_done(self) -> None:
        """is_active() returns False after all nodes are marked done."""
        sched = _make_scheduler({10: []})
        _ = sched.get_ready()
        sched.mark_done(10)
        assert not sched.is_active()

    def test_is_active_false_when_mix_of_done_and_parked(self) -> None:
        """is_active() is False when remaining nodes are done or parked."""
        sched = _make_scheduler({10: [], 11: [], 12: []})
        _ = sched.get_ready()
        sched.mark_done(10)
        sched.mark_parked(11)
        sched.mark_parked(12)
        assert not sched.is_active()


# ---------------------------------------------------------------------------
# CycleError detection
# ---------------------------------------------------------------------------


class TestCycleError:
    """Tests for cycle detection via ``prepare()``."""

    def test_prepare_raises_cycle_error_on_direct_cycle(self) -> None:
        """prepare() raises CycleError when two nodes block each other."""
        sched = IssueScheduler({10: [11], 11: [10]})
        with pytest.raises(graphlib.CycleError):
            sched.prepare()

    def test_prepare_raises_cycle_error_on_longer_cycle(self) -> None:
        """prepare() raises CycleError for a three-node cycle."""
        sched = IssueScheduler({10: [11], 11: [12], 12: [10]})
        with pytest.raises(graphlib.CycleError):
            sched.prepare()

    def test_prepare_succeeds_on_acyclic_graph(self) -> None:
        """prepare() does not raise on a valid DAG."""
        sched = IssueScheduler({10: [11], 11: [12], 12: []})
        sched.prepare()  # should not raise
