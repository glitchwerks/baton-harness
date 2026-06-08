"""Read GitHub issue-dependency edges and milestone membership via ``gh api``.

Wraps three GitHub REST endpoints using ``gh api`` subprocess calls:

- ``GET repos/{owner}/{repo}/issues/{n}/dependencies/blocked_by`` —
  issues that block issue ``n`` (its prerequisites).
- ``GET repos/{owner}/{repo}/issues/{n}/dependencies/blocking`` —
  issues that issue ``n`` blocks (its dependents).
- ``GET repos/{owner}/{repo}/issues?milestone={m}&state=all`` —
  all issues belonging to a milestone (membership set).

All responses are parsed with ``json.loads``; the output is **never**
grepped (``after_run.py:L180`` discipline).

Same-repo constraint:
    The GitHub issue-dependencies API only represents dependencies between
    issues within the **same repository**.  Cross-repo dependencies are not
    supported.  All issue numbers returned by this module are integers
    belonging to the same ``{owner}/{repo}`` repository.

Pagination:
    Each endpoint is paginated.  This module requests ``per_page=100``
    (the API maximum) and fetches subsequent pages until a page contains
    fewer than 100 items, at which point pagination is complete.

Subprocess style follows the ``before_run``/``after_run`` ``_run`` helper
pattern: a single module-local ``_run`` function is the only subprocess
seam, making it trivially patchable in tests (spike finding F8).
"""

from __future__ import annotations

import json
import subprocess
from typing import cast

# ---------------------------------------------------------------------------
# Subprocess helper (the sole I/O seam; patch this in tests)
# ---------------------------------------------------------------------------


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run an external command and return its completed process.

    Centralises subprocess invocation so tests can patch a single symbol
    (spike finding F8 — hooks must be independently testable).

    Args:
        cmd: Command and arguments to execute (no shell interpolation).

    Returns:
        A ``subprocess.CompletedProcess`` with captured stdout/stderr.
        Callers inspect ``returncode`` themselves.
    """
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Internal pagination helper
# ---------------------------------------------------------------------------

_PAGE_SIZE = 100


def _paginate(base_cmd: list[str]) -> list[dict[str, object]]:
    """Fetch all pages from a paginated ``gh api`` endpoint.

    Starts at page 1 and keeps fetching until a page with fewer than
    ``_PAGE_SIZE`` items is returned, then stops.

    Args:
        base_cmd: The ``gh api`` command **without** ``--paginate`` flags.
            This function appends ``-F per_page=100 -F page={n}`` for each
            request.

    Returns:
        The concatenated list of all items across all pages.

    Raises:
        RuntimeError: If any ``gh api`` call returns a non-zero exit code.
    """
    results: list[dict[str, object]] = []
    page = 1
    while True:
        cmd = base_cmd + [
            "-F",
            f"per_page={_PAGE_SIZE}",
            "-F",
            f"page={page}",
        ]
        proc = _run(cmd)
        if proc.returncode != 0:
            raise RuntimeError(
                f"gh api call failed (exit {proc.returncode}): {proc.stderr}"
            )
        items: list[dict[str, object]] = json.loads(proc.stdout)
        results.extend(items)
        if len(items) < _PAGE_SIZE:
            break
        page += 1
    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_blocked_by(owner: str, repo: str, issue: int) -> list[int]:
    """Fetch the issue numbers that block ``issue`` (its prerequisites).

    Calls the GitHub REST endpoint::

        GET repos/{owner}/{repo}/issues/{issue}/dependencies/blocked_by

    Each element of the response array is a full Issue object; this
    function extracts only the ``number`` field.

    Same-repo only: all returned numbers are issues in the same
    ``{owner}/{repo}`` repository.

    Args:
        owner: The GitHub repository owner (organisation or user).
        repo: The repository name.
        issue: The issue number whose blockers are fetched.

    Returns:
        A list of issue numbers that block ``issue``, in API-returned order.
        Returns an empty list when ``issue`` has no blockers.

    Raises:
        RuntimeError: If the ``gh api`` call returns a non-zero exit code.
    """
    endpoint = f"repos/{owner}/{repo}/issues/{issue}/dependencies/blocked_by"
    base_cmd = ["gh", "api", endpoint]
    items = _paginate(base_cmd)
    return [cast(int, item["number"]) for item in items]


def fetch_blocking(owner: str, repo: str, issue: int) -> list[int]:
    """Fetch the issue numbers that ``issue`` blocks (its dependents).

    Calls the GitHub REST endpoint::

        GET repos/{owner}/{repo}/issues/{issue}/dependencies/blocking

    Each element of the response array is a full Issue object; this
    function extracts only the ``number`` field.

    Same-repo only: all returned numbers are issues in the same
    ``{owner}/{repo}`` repository.

    Args:
        owner: The GitHub repository owner (organisation or user).
        repo: The repository name.
        issue: The issue number whose dependents are fetched.

    Returns:
        A list of issue numbers that ``issue`` blocks, in API-returned
        order.  Returns an empty list when ``issue`` blocks nobody.

    Raises:
        RuntimeError: If the ``gh api`` call returns a non-zero exit code.
    """
    endpoint = f"repos/{owner}/{repo}/issues/{issue}/dependencies/blocking"
    base_cmd = ["gh", "api", endpoint]
    items = _paginate(base_cmd)
    return [cast(int, item["number"]) for item in items]


def fetch_milestone_members(
    owner: str, repo: str, milestone: int
) -> frozenset[int]:
    """Fetch the set of issue numbers belonging to a milestone.

    Calls the GitHub REST issues endpoint filtered by milestone number::

        GET repos/{owner}/{repo}/issues?milestone={milestone}&state=all

    Same-repo only: all returned numbers are issues in the same
    ``{owner}/{repo}`` repository.

    Args:
        owner: The GitHub repository owner (organisation or user).
        repo: The repository name.
        milestone: The milestone number (integer ID, not the title string).

    Returns:
        A ``frozenset`` of all issue numbers in the milestone.  Returns an
        empty frozenset when the milestone has no issues.

    Raises:
        RuntimeError: If the ``gh api`` call returns a non-zero exit code.
    """
    endpoint = f"repos/{owner}/{repo}/issues"
    base_cmd = [
        "gh",
        "api",
        endpoint,
        "-F",
        f"milestone={milestone}",
        "-F",
        "state=all",
    ]
    items = _paginate(base_cmd)
    return frozenset(cast(int, item["number"]) for item in items)
