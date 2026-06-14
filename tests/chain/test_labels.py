"""Tests for baton_harness.chain.labels — state constants and invariant check.

Coverage:
- Constant string values for the three state labels.
- STATE_LABELS contains exactly {agent-ready, agent-done, blocked}.
- assert_single_state returns None for each single-state input.
- assert_single_state returns a non-empty violation string for zero labels.
- assert_single_state returns a non-empty violation string for two labels.
- assert_single_state returns a non-empty violation string for all three.
- Non-state labels are ignored: one state + extras yields None.
- assert_single_state never raises on odd input types.
"""

from __future__ import annotations

from baton_harness.chain.labels import (
    LABEL_AGENT_DONE,
    LABEL_AGENT_READY,
    LABEL_BLOCKED,
    STATE_LABELS,
    assert_single_state,
)

# ---------------------------------------------------------------------------
# Constant values
# ---------------------------------------------------------------------------


class TestConstants:
    """Exported label constants have the expected string values."""

    def test_label_agent_ready_value(self) -> None:
        """LABEL_AGENT_READY equals 'agent-ready'."""
        assert LABEL_AGENT_READY == "agent-ready"

    def test_label_agent_done_value(self) -> None:
        """LABEL_AGENT_DONE equals 'agent-done'."""
        assert LABEL_AGENT_DONE == "agent-done"

    def test_label_blocked_value(self) -> None:
        """LABEL_BLOCKED equals 'blocked'."""
        assert LABEL_BLOCKED == "blocked"


# ---------------------------------------------------------------------------
# STATE_LABELS collection
# ---------------------------------------------------------------------------


class TestStateLabels:
    """STATE_LABELS is exactly the set of three state label strings."""

    def test_state_labels_contains_agent_ready(self) -> None:
        """STATE_LABELS contains 'agent-ready'."""
        assert "agent-ready" in STATE_LABELS

    def test_state_labels_contains_agent_done(self) -> None:
        """STATE_LABELS contains 'agent-done'."""
        assert "agent-done" in STATE_LABELS

    def test_state_labels_contains_blocked(self) -> None:
        """STATE_LABELS contains 'blocked'."""
        assert "blocked" in STATE_LABELS

    def test_state_labels_has_exactly_three_members(self) -> None:
        """STATE_LABELS contains exactly three members."""
        assert len(STATE_LABELS) == 3

    def test_state_labels_contains_no_extra_members(self) -> None:
        """STATE_LABELS has no members beyond the three state labels."""
        expected = {"agent-ready", "agent-done", "blocked"}
        assert set(STATE_LABELS) == expected

    def test_state_labels_constants_match_collection(self) -> None:
        """The three constant objects are members of STATE_LABELS."""
        assert LABEL_AGENT_READY in STATE_LABELS
        assert LABEL_AGENT_DONE in STATE_LABELS
        assert LABEL_BLOCKED in STATE_LABELS


# ---------------------------------------------------------------------------
# assert_single_state — single valid inputs (returns None)
# ---------------------------------------------------------------------------


class TestAssertSingleStateValidInputs:
    """assert_single_state returns None when exactly one state label is set."""

    def test_single_agent_ready_returns_none(self) -> None:
        """{'agent-ready'} → None (valid single state)."""
        assert assert_single_state({"agent-ready"}) is None

    def test_single_agent_done_returns_none(self) -> None:
        """{'agent-done'} → None (valid single state)."""
        assert assert_single_state({"agent-done"}) is None

    def test_single_blocked_returns_none(self) -> None:
        """{'blocked'} → None (valid single state)."""
        assert assert_single_state({"blocked"}) is None

    def test_single_state_with_non_state_extra_labels_returns_none(
        self,
    ) -> None:
        """One state label plus non-state labels → None.

        Non-state labels (e.g. 'agent-in-progress') must be ignored for
        the invariant count.
        """
        result = assert_single_state({"agent-ready", "agent-in-progress"})
        assert result is None

    def test_single_state_plus_many_extras_returns_none(self) -> None:
        """One state label plus multiple non-state extras → None."""
        result = assert_single_state(
            {"agent-done", "agent-in-progress", "priority-high", "bug"}
        )
        assert result is None


# ---------------------------------------------------------------------------
# assert_single_state — violation inputs (returns non-empty string)
# ---------------------------------------------------------------------------


class TestAssertSingleStateViolations:
    """assert_single_state returns a non-empty string for violations."""

    def test_empty_set_returns_violation_string(self) -> None:
        """Empty input → non-empty violation string (zero state labels)."""
        result = assert_single_state(set())
        assert result is not None
        assert len(result) > 0

    def test_empty_list_returns_violation_string(self) -> None:
        """Empty list → non-empty violation string."""
        result = assert_single_state([])
        assert result is not None
        assert len(result) > 0

    def test_only_non_state_labels_returns_violation_string(self) -> None:
        """Non-state labels only → violation (zero state labels found)."""
        result = assert_single_state({"agent-in-progress", "priority-high"})
        assert result is not None
        assert len(result) > 0

    def test_two_state_labels_agent_done_and_blocked_returns_violation(
        self,
    ) -> None:
        """{'agent-done', 'blocked'} → violation string mentioning both."""
        result = assert_single_state({"agent-done", "blocked"})
        assert result is not None
        assert len(result) > 0
        # Violation string should name the found state labels.
        assert "agent-done" in result or "blocked" in result

    def test_two_state_labels_agent_ready_and_agent_done_returns_violation(
        self,
    ) -> None:
        """{'agent-ready', 'agent-done'} → violation string."""
        result = assert_single_state({"agent-ready", "agent-done"})
        assert result is not None
        assert len(result) > 0

    def test_two_state_labels_agent_ready_and_blocked_returns_violation(
        self,
    ) -> None:
        """{'agent-ready', 'blocked'} → violation string."""
        result = assert_single_state({"agent-ready", "blocked"})
        assert result is not None
        assert len(result) > 0

    def test_all_three_state_labels_returns_violation(self) -> None:
        """All three state labels → violation string."""
        result = assert_single_state({"agent-ready", "agent-done", "blocked"})
        assert result is not None
        assert len(result) > 0

    def test_all_three_plus_extras_returns_violation(self) -> None:
        """All three state labels plus extras → violation."""
        result = assert_single_state(
            {
                "agent-ready",
                "agent-done",
                "blocked",
                "agent-in-progress",
            }
        )
        assert result is not None
        assert len(result) > 0

    def test_two_state_labels_violation_mentions_found_labels(
        self,
    ) -> None:
        """Violation string names at least one of the conflicting states."""
        result = assert_single_state({"agent-done", "blocked"})
        assert result is not None
        # At least one of the present state labels appears in the message.
        found_in_msg = "agent-done" in result or "blocked" in result
        assert found_in_msg, (
            f"Violation string should name found state labels; got: {result!r}"
        )


# ---------------------------------------------------------------------------
# assert_single_state — never raises
# ---------------------------------------------------------------------------


class TestAssertSingleStateNeverRaises:
    """assert_single_state is a pure checker and never raises."""

    def test_does_not_raise_on_empty_set(self) -> None:
        """assert_single_state({}) does not raise."""
        # Confirm returns a str or None, not an exception.
        result = assert_single_state(set())
        assert isinstance(result, (str, type(None)))

    def test_does_not_raise_on_empty_list(self) -> None:
        """assert_single_state([]) does not raise."""
        result = assert_single_state([])
        assert isinstance(result, (str, type(None)))

    def test_does_not_raise_on_only_non_state_labels(self) -> None:
        """assert_single_state with non-state labels does not raise."""
        result = assert_single_state({"agent-in-progress"})
        assert isinstance(result, (str, type(None)))

    def test_accepts_list_input(self) -> None:
        """assert_single_state accepts a list (iterable), not just set."""
        result = assert_single_state(["agent-ready"])
        assert result is None

    def test_accepts_generator_input(self) -> None:
        """assert_single_state accepts a generator (any iterable)."""
        result = assert_single_state(x for x in ["agent-done"])
        assert result is None

    def test_does_not_raise_on_unhashable_members(self) -> None:
        """assert_single_state returns a non-empty string on unhashable input.

        Passing a list of dicts (each label as a mapping, not a string)
        triggers a TypeError when the implementation tries to build a set
        of state labels.  The function must catch that and return a
        non-empty diagnostic string rather than propagating the exception.
        """
        result = assert_single_state([{"name": "blocked"}])
        assert isinstance(result, str) and result, (
            "Expected a non-empty string when input contains unhashable"
            f" members (list of dicts); got {result!r}"
        )
