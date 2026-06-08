"""Unit tests for baton_harness.chain.gh_deps.

All ``gh api`` subprocess calls are intercepted by monkeypatching the
module-local ``_run`` helper so no real ``gh`` binary is required.

The fixture JSON shapes are based on confirmed live API responses for the
GitHub issue-dependencies REST endpoints:

    GET repos/{owner}/{repo}/issues/{n}/dependencies/blocked_by
    GET repos/{owner}/{repo}/issues/{n}/dependencies/blocking

Each endpoint returns a JSON **array** of full Issue objects.  The fields
used by gh_deps are: ``number`` (int), ``state`` (str), ``title`` (str),
``id`` (int), and ``milestone`` (object or null).

Coverage:
- ``fetch_blocked_by`` parses a one-page array of blocker objects.
- ``fetch_blocking`` parses a one-page array of dependent objects.
- Pagination: a second page is fetched when the first page is full
  (``per_page=100`` items).
- Empty arrays return empty lists.
- ``fetch_milestone_members`` parses issue-list output for a milestone.
- Milestone pagination: two pages are fetched when the first is full.
- Same-repo constraint: cross-repo issue numbers are not representable
  (all numbers are ints; the limitation is documented, not enforced
  at the API level, but the function docstring makes it explicit).
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

import baton_harness.chain.gh_deps as gh_deps_mod
from baton_harness.chain.gh_deps import (
    fetch_blocked_by,
    fetch_blocking,
    fetch_milestone_members,
)

# ---------------------------------------------------------------------------
# Shared fixture data (captured live-API shapes)
# ---------------------------------------------------------------------------

#: A single Issue object as returned by the dependency endpoints.
#: Fields match the confirmed live response (N-I2 entry gate).
_ISSUE_42: dict[str, object] = {
    "id": 4609294978,
    "number": 42,
    "state": "closed",
    "title": "P0 — vendored symphony + env-threading",
    "milestone": {
        "title": "Always-on daemon / #27",
        "number": 3,
    },
}

_ISSUE_43: dict[str, object] = {
    "id": 4609294979,
    "number": 43,
    "state": "open",
    "title": "P1 — DAG read + scheduler",
    "milestone": {
        "title": "Always-on daemon / #27",
        "number": 3,
    },
}

_ISSUE_44: dict[str, object] = {
    "id": 4609294980,
    "number": 44,
    "state": "open",
    "title": "P2 — branches + merge",
    "milestone": None,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(data: object) -> subprocess.CompletedProcess[str]:
    """Return a successful CompletedProcess with JSON-serialised data.

    Args:
        data: Python object to serialise as the subprocess stdout.

    Returns:
        A ``CompletedProcess`` with ``returncode=0`` and JSON stdout.
    """
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps(data),
        stderr="",
    )


def _fail(stderr: str = "error") -> subprocess.CompletedProcess[str]:
    """Return a failed CompletedProcess.

    Args:
        stderr: Simulated error output.

    Returns:
        A ``CompletedProcess`` with ``returncode=1``.
    """
    return subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr=stderr,
    )


# ---------------------------------------------------------------------------
# fetch_blocked_by
# ---------------------------------------------------------------------------


class TestFetchBlockedBy:
    """Tests for ``fetch_blocked_by``."""

    def test_returns_blocker_numbers_from_single_page(self) -> None:
        """Parses blocker issue numbers from a single-page array response."""
        payload = [_ISSUE_42]

        with patch.object(gh_deps_mod, "_run", return_value=_ok(payload)):
            result = fetch_blocked_by("glitchwerks", "baton-harness", 43)

        assert result == [42]

    def test_returns_multiple_blockers(self) -> None:
        """Returns all blocker numbers when multiple blockers exist."""
        payload = [_ISSUE_42, _ISSUE_44]

        with patch.object(gh_deps_mod, "_run", return_value=_ok(payload)):
            result = fetch_blocked_by("glitchwerks", "baton-harness", 43)

        assert result == [42, 44]

    def test_empty_array_returns_empty_list(self) -> None:
        """An empty response array returns an empty list."""
        with patch.object(gh_deps_mod, "_run", return_value=_ok([])):
            result = fetch_blocked_by("glitchwerks", "baton-harness", 43)

        assert result == []

    def test_pagination_fetches_second_page(self) -> None:
        """Second page is fetched when first page is full (100 items)."""
        page1 = [
            {
                "id": i,
                "number": i,
                "state": "open",
                "title": f"Issue {i}",
                "milestone": None,
            }
            for i in range(1, 101)
        ]
        page2 = [_ISSUE_44]

        pages = [_ok(page1), _ok(page2)]
        call_count = 0

        def fake_run(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            nonlocal call_count
            result = pages[call_count]
            call_count += 1
            return result

        with patch.object(gh_deps_mod, "_run", side_effect=fake_run):
            result = fetch_blocked_by("glitchwerks", "baton-harness", 200)

        assert 44 in result
        assert call_count == 2

    def test_first_page_not_full_stops_after_one_call(self) -> None:
        """Does not fetch a second page when first page has fewer than 100."""
        payload = [_ISSUE_42, _ISSUE_43]

        call_count = 0

        def fake_run(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            nonlocal call_count
            call_count += 1
            return _ok(payload)

        with patch.object(gh_deps_mod, "_run", side_effect=fake_run):
            fetch_blocked_by("glitchwerks", "baton-harness", 43)

        assert call_count == 1

    def test_raises_on_gh_failure(self) -> None:
        """Raises RuntimeError when gh api returns a non-zero exit code."""
        with patch.object(gh_deps_mod, "_run", return_value=_fail()):
            with pytest.raises(RuntimeError, match="gh api"):
                fetch_blocked_by("glitchwerks", "baton-harness", 43)

    def test_uses_correct_endpoint(self) -> None:
        """Calls the blocked_by REST endpoint with correct path components."""
        captured: list[list[str]] = []

        def fake_run(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            captured.append(cmd)
            return _ok([])

        with patch.object(gh_deps_mod, "_run", side_effect=fake_run):
            fetch_blocked_by("glitchwerks", "baton-harness", 99)

        assert len(captured) == 1
        joined = " ".join(captured[0])
        assert "blocked_by" in joined
        assert "99" in joined


# ---------------------------------------------------------------------------
# fetch_blocking
# ---------------------------------------------------------------------------


class TestFetchBlocking:
    """Tests for ``fetch_blocking``."""

    def test_returns_dependent_numbers_from_single_page(self) -> None:
        """Parses dependent issue numbers from a single-page array response."""
        payload = [_ISSUE_43, _ISSUE_44]

        with patch.object(gh_deps_mod, "_run", return_value=_ok(payload)):
            result = fetch_blocking("glitchwerks", "baton-harness", 42)

        assert result == [43, 44]

    def test_empty_array_returns_empty_list(self) -> None:
        """An empty blocking response returns an empty list."""
        with patch.object(gh_deps_mod, "_run", return_value=_ok([])):
            result = fetch_blocking("glitchwerks", "baton-harness", 99)

        assert result == []

    def test_pagination_fetches_second_page(self) -> None:
        """Second page is fetched when first page is full (100 items)."""
        page1 = [
            {
                "id": i,
                "number": i,
                "state": "open",
                "title": f"Issue {i}",
                "milestone": None,
            }
            for i in range(1, 101)
        ]
        page2 = [_ISSUE_44]

        pages = [_ok(page1), _ok(page2)]
        call_count = 0

        def fake_run(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            nonlocal call_count
            result = pages[call_count]
            call_count += 1
            return result

        with patch.object(gh_deps_mod, "_run", side_effect=fake_run):
            result = fetch_blocking("glitchwerks", "baton-harness", 1)

        assert 44 in result
        assert call_count == 2

    def test_raises_on_gh_failure(self) -> None:
        """Raises RuntimeError when gh api returns a non-zero exit code."""
        with patch.object(gh_deps_mod, "_run", return_value=_fail()):
            with pytest.raises(RuntimeError, match="gh api"):
                fetch_blocking("glitchwerks", "baton-harness", 42)

    def test_uses_correct_endpoint(self) -> None:
        """Calls the blocking REST endpoint with correct path components."""
        captured: list[list[str]] = []

        def fake_run(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            captured.append(cmd)
            return _ok([])

        with patch.object(gh_deps_mod, "_run", side_effect=fake_run):
            fetch_blocking("glitchwerks", "baton-harness", 42)

        assert len(captured) == 1
        joined = " ".join(captured[0])
        assert "blocking" in joined
        assert "42" in joined


# ---------------------------------------------------------------------------
# fetch_milestone_members
# ---------------------------------------------------------------------------


class TestFetchMilestoneMembers:
    """Tests for ``fetch_milestone_members``."""

    def test_returns_set_of_issue_numbers(self) -> None:
        """Returns a frozenset of issue numbers belonging to the milestone."""
        payload = [
            {"number": 42, "state": "closed"},
            {"number": 43, "state": "open"},
            {"number": 44, "state": "open"},
        ]

        with patch.object(gh_deps_mod, "_run", return_value=_ok(payload)):
            result = fetch_milestone_members("glitchwerks", "baton-harness", 3)

        assert result == frozenset({42, 43, 44})

    def test_empty_milestone_returns_empty_frozenset(self) -> None:
        """An empty milestone returns an empty frozenset."""
        with patch.object(gh_deps_mod, "_run", return_value=_ok([])):
            result = fetch_milestone_members(
                "glitchwerks", "baton-harness", 99
            )

        assert result == frozenset()

    def test_pagination_fetches_all_pages(self) -> None:
        """Fetches subsequent pages until a page with fewer than 100 items."""
        page1 = [{"number": i, "state": "open"} for i in range(1, 101)]
        page2 = [{"number": 101, "state": "open"}]

        pages = [_ok(page1), _ok(page2)]
        call_count = 0

        def fake_run(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            nonlocal call_count
            result = pages[call_count]
            call_count += 1
            return result

        with patch.object(gh_deps_mod, "_run", side_effect=fake_run):
            result = fetch_milestone_members("glitchwerks", "baton-harness", 3)

        assert 101 in result
        assert call_count == 2

    def test_raises_on_gh_failure(self) -> None:
        """Raises RuntimeError when gh api returns a non-zero exit code."""
        with patch.object(gh_deps_mod, "_run", return_value=_fail()):
            with pytest.raises(RuntimeError, match="gh api"):
                fetch_milestone_members("glitchwerks", "baton-harness", 3)

    def test_uses_milestone_number_in_query(self) -> None:
        """Passes the milestone number to the gh api issues endpoint."""
        captured: list[list[str]] = []

        def fake_run(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            captured.append(cmd)
            return _ok([])

        with patch.object(gh_deps_mod, "_run", side_effect=fake_run):
            fetch_milestone_members("glitchwerks", "baton-harness", 7)

        assert len(captured) == 1
        joined = " ".join(captured[0])
        assert "7" in joined

    def test_excludes_pull_requests_from_membership(self) -> None:
        """Items with a 'pull_request' field are excluded from membership."""
        payload = [
            {"number": 10, "state": "open"},
            {"number": 11, "state": "open", "pull_request": {"url": "..."}},
            {"number": 12, "state": "closed"},
        ]

        with patch.object(gh_deps_mod, "_run", return_value=_ok(payload)):
            result = fetch_milestone_members("glitchwerks", "baton-harness", 3)

        assert result == frozenset({10, 12})
        assert 11 not in result


# ---------------------------------------------------------------------------
# GET-method regression (FIX 1)
# ---------------------------------------------------------------------------


class TestGetMethodRegression:
    """Assert pagination params are passed via URL query string, not -F flags.

    ``gh api -F`` switches HTTP method from GET to POST.  All fetch_*
    functions must embed pagination params in the URL string so the
    request stays a GET.
    """

    def test_blocked_by_no_dash_f_pagination(self) -> None:
        """fetch_blocked_by does not pass -F flags for pagination params."""
        captured: list[list[str]] = []

        def fake_run(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            captured.append(cmd)
            return _ok([])

        with patch.object(gh_deps_mod, "_run", side_effect=fake_run):
            fetch_blocked_by("glitchwerks", "baton-harness", 5)

        for cmd in captured:
            assert "-F" not in cmd, f"Found -F in command: {cmd}"
            assert "-X" not in cmd or "POST" not in " ".join(cmd), (
                f"Found -X POST in command: {cmd}"
            )
            # Per_page and page must be embedded in the URL itself
            url_arg = cmd[2]  # third element is the endpoint URL
            assert "per_page" in url_arg, f"per_page not in URL: {url_arg}"
            assert "page=" in url_arg, f"page= not in URL: {url_arg}"

    def test_blocking_no_dash_f_pagination(self) -> None:
        """fetch_blocking does not pass -F flags for pagination params."""
        captured: list[list[str]] = []

        def fake_run(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            captured.append(cmd)
            return _ok([])

        with patch.object(gh_deps_mod, "_run", side_effect=fake_run):
            fetch_blocking("glitchwerks", "baton-harness", 5)

        for cmd in captured:
            assert "-F" not in cmd, f"Found -F in command: {cmd}"
            url_arg = cmd[2]
            assert "per_page" in url_arg, f"per_page not in URL: {url_arg}"
            assert "page=" in url_arg, f"page= not in URL: {url_arg}"

    def test_milestone_members_no_dash_f_pagination(self) -> None:
        """fetch_milestone_members does not pass -F flags for any params."""
        captured: list[list[str]] = []

        def fake_run(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            captured.append(cmd)
            return _ok([])

        with patch.object(gh_deps_mod, "_run", side_effect=fake_run):
            fetch_milestone_members("glitchwerks", "baton-harness", 7)

        for cmd in captured:
            assert "-F" not in cmd, f"Found -F in command: {cmd}"
            url_arg = cmd[2]
            assert "per_page" in url_arg, f"per_page not in URL: {url_arg}"
            assert "page=" in url_arg, f"page= not in URL: {url_arg}"
            assert "milestone=" in url_arg, f"milestone= not in URL: {url_arg}"
            assert "state=" in url_arg, f"state= not in URL: {url_arg}"


# ---------------------------------------------------------------------------
# Three-full-pages pagination (code-reviewer W4)
# ---------------------------------------------------------------------------


class TestThreePagePagination:
    """Verify three-page (100, 100, N<100) pagination collects all items."""

    def _make_full_page(self, start: int) -> list[dict[str, object]]:
        """Build a full page of 100 issue-shaped dicts starting at start.

        Args:
            start: The first issue number in the page.

        Returns:
            A list of 100 issue dicts.
        """
        return [
            {
                "id": i,
                "number": i,
                "state": "open",
                "title": f"Issue {i}",
                "milestone": None,
            }
            for i in range(start, start + 100)
        ]

    def test_three_pages_collected_and_loop_stops(self) -> None:
        """All items from pages of sizes 100, 100, 7 are returned; stops."""
        page1 = self._make_full_page(1)
        page2 = self._make_full_page(101)
        page3 = [
            {
                "id": i,
                "number": i,
                "state": "open",
                "title": f"I{i}",
                "milestone": None,
            }
            for i in range(201, 208)
        ]

        pages = [_ok(page1), _ok(page2), _ok(page3)]
        call_count = 0

        def fake_run(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            nonlocal call_count
            result = pages[call_count]
            call_count += 1
            return result

        with patch.object(gh_deps_mod, "_run", side_effect=fake_run):
            result = fetch_blocked_by("glitchwerks", "baton-harness", 999)

        assert call_count == 3
        assert len(result) == 207
        assert 1 in result
        assert 100 in result
        assert 101 in result
        assert 200 in result
        assert 201 in result
        assert 207 in result
