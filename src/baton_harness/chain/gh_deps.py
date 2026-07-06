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

Issue-body dependency fallback (#126):
    Some repositories cannot use the native issue-dependencies API (a
    fine-grained PAT lacking the permission, a repo tier without the
    feature, or an org policy disabling it) — the endpoint then returns
    an empty array even when the issue really is blocked. When
    ``fetch_blocked_by`` sees an empty native result, it falls back to
    scanning the issue body text for a ``blocked_by #N`` / ``depends on
    #N`` marker and returns those issue numbers instead, in the order
    they appear in the body. The fallback never runs when the native API
    already returned a non-empty result — that result wins outright.

Subprocess style follows the ``before_run``/``after_run`` ``_run`` helper
pattern: a single module-local ``_run`` function is the only subprocess
seam, making it trivially patchable in tests (spike finding F8).
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import cast

from baton_harness.chain.app_auth import (
    InstallationTokenSource,
    gh_env,
)

# ---------------------------------------------------------------------------
# Subprocess helper (the sole I/O seam; patch this in tests)
# ---------------------------------------------------------------------------


def _run(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an external command and return its completed process.

    Centralises subprocess invocation so tests can patch a single symbol
    (spike finding F8 — hooks must be independently testable).

    Args:
        cmd: Command and arguments to execute (no shell interpolation).
        env: Optional per-call environment dict.  When supplied, passed
            directly to ``subprocess.run`` as ``env=``.  When ``None``,
            the subprocess inherits the current process environment.
            Callers must supply a FULL environment copy (e.g.
            ``gh_env(token)``); passing a partial dict causes missing
            vars in the subprocess.

    Returns:
        A ``subprocess.CompletedProcess`` with captured stdout/stderr.
        Callers inspect ``returncode`` themselves.
    """
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


# ---------------------------------------------------------------------------
# Internal pagination helper
# ---------------------------------------------------------------------------

_PAGE_SIZE = 100


def _paginate(
    base_url: str,
    *,
    installation_token: InstallationTokenSource = "",
) -> list[dict[str, object]]:
    """Fetch all pages from a paginated ``gh api`` endpoint.

    Starts at page 1 and keeps fetching until a page with fewer than
    ``_PAGE_SIZE`` items is returned, then stops.

    Query parameters (``per_page`` and ``page``) are embedded in the URL
    query string so the request remains an HTTP GET.  Using ``-F`` flags
    would switch ``gh api`` to POST.

    Args:
        base_url: The endpoint URL, which may already contain query
            parameters.  This function appends ``per_page=100`` and
            ``page={n}`` as additional query string parameters.
        installation_token: Optional GitHub App installation access token
            (``ghs_`` prefix).  When non-empty, each ``gh api`` call uses
            a per-call env copy with ``GH_TOKEN`` overridden —
            ``os.environ`` is never mutated.

    Returns:
        The concatenated list of all items across all pages.

    Raises:
        RuntimeError: If any ``gh api`` call returns a non-zero exit code.
        ValueError: If any item in the response is missing the ``number``
            field.
    """
    env = gh_env(installation_token) if installation_token else None
    results: list[dict[str, object]] = []
    page = 1
    separator = "&" if "?" in base_url else "?"
    while True:
        url = f"{base_url}{separator}per_page={_PAGE_SIZE}&page={page}"
        proc = _run(["gh", "api", url], env=env)
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


#: Matches ``blocked_by #N`` or ``depends on #N`` (case-insensitive),
#: capturing the referenced issue number. Used by the issue-body
#: dependency fallback (#126). The leading ``\b`` requires a word
#: boundary before ``blocked_by``/``depends on`` so a substring like
#: ``unblocked_by #12`` does not falsely match.
_BODY_MARKER_RE = re.compile(
    r"\b(?:blocked_by|depends on)\s*#(\d+)", re.IGNORECASE
)


def _parse_blocked_by_from_body(body: str) -> list[int]:
    """Scan issue body text for ``blocked_by #N`` / ``depends on #N`` markers.

    Args:
        body: The raw issue body text. May be empty.

    Returns:
        A list of blocker issue numbers, in the order the markers appear
        in ``body``. Returns an empty list when no marker is present.
    """
    return [int(match) for match in _BODY_MARKER_RE.findall(body)]


def _fetch_issue_body(
    owner: str,
    repo: str,
    issue: int,
    *,
    installation_token: InstallationTokenSource = "",
) -> str:
    """Fetch the raw body text of a single issue.

    Calls ``gh issue view <issue> --repo {owner}/{repo} --json body``.

    Args:
        owner: The GitHub repository owner (organisation or user).
        repo: The repository name.
        issue: The issue number to look up.
        installation_token: Optional GitHub App installation access
            token (``ghs_`` prefix). When non-empty, the call uses a
            per-call env copy with ``GH_TOKEN`` overridden —
            ``os.environ`` is never mutated.

    Returns:
        The issue body text, or ``""`` if the field is absent or the
        response is not a JSON object shaped like an issue.

    Raises:
        RuntimeError: If the ``gh`` call returns a non-zero exit code.
    """
    env = gh_env(installation_token) if installation_token else None
    proc = _run(
        [
            "gh",
            "issue",
            "view",
            str(issue),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "body",
        ],
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh api call failed (exit {proc.returncode}): {proc.stderr}"
        )
    data = json.loads(proc.stdout)
    if not isinstance(data, dict):
        return ""
    body = data.get("body")
    return body if isinstance(body, str) else ""


def _extract_number(item: dict[str, object]) -> int:
    """Extract the ``number`` field from a GitHub API issue object.

    Args:
        item: A dict representing a GitHub issue as returned by the API.

    Returns:
        The integer issue number.

    Raises:
        ValueError: If ``item`` does not contain a ``number`` key.
    """
    if "number" not in item:
        raise ValueError(f"Unexpected gh api item without 'number': {item!r}")
    return cast(int, item["number"])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_blocked_by(
    owner: str,
    repo: str,
    issue: int,
    *,
    installation_token: InstallationTokenSource = "",
) -> list[int]:
    """Fetch the issue numbers that block ``issue`` (its prerequisites).

    Calls the GitHub REST endpoint::

        GET repos/{owner}/{repo}/issues/{issue}/dependencies/blocked_by

    Each element of the response array is a full Issue object; this
    function extracts only the ``number`` field.

    Same-repo only: all returned numbers are issues in the same
    ``{owner}/{repo}`` repository.

    Issue-body fallback (#126): when the native API returns an empty
    result, the issue body is scanned for a ``blocked_by #N`` /
    ``depends on #N`` marker and those numbers are returned instead.
    The fallback never runs when the native API already returned a
    non-empty result.

    Args:
        owner: The GitHub repository owner (organisation or user).
        repo: The repository name.
        issue: The issue number whose blockers are fetched.
        installation_token: Optional GitHub App installation access token
            (``ghs_`` prefix).  When non-empty, the ``gh api`` subprocess
            call uses a per-call env copy with ``GH_TOKEN`` overridden —
            ``os.environ`` is never mutated (env-discipline invariant).

    Returns:
        A list of issue numbers that block ``issue``, in API-returned order.
        Falls back to issue-body markers (see above) when the API result
        is empty. Returns an empty list when ``issue`` has no blockers by
        either means.

    Raises:
        RuntimeError: If the ``gh api`` call returns a non-zero exit code.
        ValueError: If any API response item lacks a ``number`` field.
    """
    url = f"repos/{owner}/{repo}/issues/{issue}/dependencies/blocked_by"
    items = _paginate(url, installation_token=installation_token)
    if items:
        return [_extract_number(item) for item in items]
    body = _fetch_issue_body(
        owner, repo, issue, installation_token=installation_token
    )
    return _parse_blocked_by_from_body(body)


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
        ValueError: If any API response item lacks a ``number`` field.
    """
    url = f"repos/{owner}/{repo}/issues/{issue}/dependencies/blocking"
    items = _paginate(url)
    return [_extract_number(item) for item in items]


def fetch_milestone_members(
    owner: str, repo: str, milestone: int
) -> frozenset[int]:
    """Fetch the set of issue numbers belonging to a milestone.

    Calls the GitHub REST issues endpoint filtered by milestone number::

        GET repos/{owner}/{repo}/issues?milestone={milestone}&state=all

    Pull requests are excluded: GitHub models PRs as issues but items
    with a ``pull_request`` field are skipped so only real issues enter
    the membership set.

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
        ValueError: If any API response item lacks a ``number`` field.
    """
    url = f"repos/{owner}/{repo}/issues?milestone={milestone}&state=all"
    items = _paginate(url)
    return frozenset(
        _extract_number(item) for item in items if "pull_request" not in item
    )
