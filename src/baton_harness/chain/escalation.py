"""Dual-channel escalation: GitHub issue comment + optional Slack.

The GitHub issue comment is the **durable record** and is always attempted
first.  Slack is a best-effort notification channel only — a failure there
never prevents the durable record from landing.

Slack is only attempted when the ``BH_SLACK_WEBHOOK_URL`` environment
variable is set.  When unset, Slack is silently skipped.  Any Slack
failure is logged at WARNING and does NOT affect the return value.

Single ``_run`` seam (module-local) makes the ``gh`` call patchable in
tests (spike finding F8).  Slack calls use ``urllib.request`` (stdlib
only — no new dependencies).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import urllib.request
from datetime import datetime, timezone
from typing import Literal

from baton_harness.chain.runlog import RunLog

_log = logging.getLogger(__name__)

# Loud prefix prepended to the escalation body for critical-severity alerts.
_CRITICAL_PREFIX = "🚨 CRITICAL: "

# ---------------------------------------------------------------------------
# Subprocess helper (the sole gh I/O seam; patch this in tests)
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
# Public API
# ---------------------------------------------------------------------------


def escalate(
    owner: str,
    repo: str,
    issue: int | None,
    summary: str,
    *,
    kind: str = "block",
) -> bool:
    """Post a stall summary as a GitHub comment and optionally to Slack.

    The GitHub issue comment is the **durable record** and MUST be
    attempted first.  Slack is a best-effort secondary channel.

    GitHub comment semantics:
        Calls ``gh issue comment <N> --repo <owner>/<repo> --body <summary>``
        when ``issue`` is a valid positive integer.  On failure, logs a loud
        WARNING.  Returns ``False`` in that case.  The caller must NOT treat
        a failed GitHub comment as a silent no-op — the durable record has
        not landed.

        When ``issue`` is ``None`` or ``<= 0`` (no valid GitHub issue target),
        the ``gh issue comment`` call is **skipped entirely** — sending
        ``gh issue comment 0`` always fails because GitHub issue numbers start
        at 1.  A WARNING is logged so operators can see the failure in the
        daemon log, and the return value is ``False`` to honestly reflect that
        no durable record was written.

    Slack semantics:
        If ``BH_SLACK_WEBHOOK_URL`` is set in the environment, POSTs a
        small JSON body ``{"text": summary}`` via ``urllib.request`` (stdlib
        only — no new dependencies).  Any failure (HTTP error, network
        error, etc.) is logged at WARNING and does NOT affect the return
        value.  If the env var is absent, Slack is silently skipped.  Slack
        is still attempted even when ``issue`` is ``None`` / ``<= 0`` — it
        is the best-effort fallback channel when the durable record cannot
        land.

    Args:
        owner: The GitHub repository owner (organisation or user login).
        repo: The repository name (without the owner prefix).
        issue: The issue number to comment on.  Pass ``None`` (or a value
            ``<= 0``) for repo-level / tick-level escalations where no
            valid issue target exists.  In that case the GitHub comment is
            skipped and ``False`` is returned.
        summary: The human-readable escalation summary (the stall card
            body).  Rendered as plain text in both GitHub and Slack.
        kind: Escalation kind hint — ``"block"`` (default, agent applied
            the ``blocked`` label) or ``"debug"`` (agent failed without
            blocking).  Not currently used to alter the message body; kept
            for caller-side logging and future filtering.

    Returns:
        ``True`` if the GitHub comment was posted successfully (the durable
        record landed).  ``False`` if the GitHub comment failed or was
        skipped (no valid issue target).  Slack success or failure has no
        bearing on the return value.
    """
    # ------------------------------------------------------------------
    # 1. GitHub comment — durable record; MUST be attempted first.
    # ------------------------------------------------------------------
    # GitHub issue numbers start at 1.  Passing issue=None or issue<=0
    # means the caller has no valid issue target (e.g. a repo-level daemon
    # tick failure).  Skip the gh call entirely to avoid the guaranteed
    # failure of ``gh issue comment 0``; log a loud WARNING so the failure
    # is visible in the daemon log.
    if issue is None or issue <= 0:
        _log.warning(
            "escalate: no valid GitHub issue target (issue=%r);"
            " durable record NOT written.  kind=%s owner=%s repo=%s"
            " summary=%r",
            issue,
            kind,
            owner,
            repo,
            summary,
        )
        durable_landed = False
    else:
        gh_proc = _run(
            [
                "gh",
                "issue",
                "comment",
                str(issue),
                "--repo",
                f"{owner}/{repo}",
                "--body",
                summary,
            ]
        )
        if gh_proc.returncode != 0:
            _log.warning(
                "escalate: failed to post GitHub comment on issue #%d"
                " (exit %d): %s.  Durable record NOT written."
                " kind=%s owner=%s repo=%s",
                issue,
                gh_proc.returncode,
                gh_proc.stderr,
                kind,
                owner,
                repo,
            )
            durable_landed = False
        else:
            _log.info(
                "escalate: GitHub comment posted on issue #%d (kind=%s)",
                issue,
                kind,
            )
            durable_landed = True

    # ------------------------------------------------------------------
    # 2. Slack — best-effort channel only.
    # ------------------------------------------------------------------
    # Attempted even when there is no valid issue target — Slack is the
    # fallback notification when the durable GitHub record cannot land.
    webhook_url = os.environ.get("BH_SLACK_WEBHOOK_URL", "")
    if webhook_url:
        _post_slack(
            webhook_url,
            summary,
            issue=issue if issue is not None else 0,
            kind=kind,
        )

    return durable_landed


def alert(
    owner: str,
    repo: str,
    issue: int | None,
    summary: str,
    *,
    severity: Literal["info", "warn", "critical"],
    runlog: RunLog | None = None,
    kind: str = "block",
) -> bool:
    """Post an alert through the severity-routing layer.

    Routes the alert to ``escalate()`` based on ``severity``:

    - ``"info"`` — never calls ``escalate()``; returns ``True`` immediately.
      Useful for informational events that should be logged but not escalated.
    - ``"warn"`` — calls ``escalate(owner, repo, issue, summary, kind=kind)``
      with the summary unchanged.
    - ``"critical"`` — prefixes the summary with a loud marker (the module-
      level ``_CRITICAL_PREFIX`` constant) and calls ``escalate()`` with the
      prefixed body.  The original summary remains a substring of the body
      passed to ``escalate()``.

    A runlog event is always emitted (for all severities, including info)
    when ``runlog`` is not ``None``.  Runlog emission is best-effort — any
    exception is swallowed so that the alert routing itself is never aborted
    by an observability failure.  When ``runlog`` is ``None``, emission is
    skipped silently.

    Args:
        owner: The GitHub repository owner (organisation or user login).
        repo: The repository name (without the owner prefix).
        issue: The issue number for the alert.  Pass ``None`` for repo-level
            / tick-level alerts where no valid issue target exists.
        summary: The human-readable alert summary.
        severity: Routing level — ``"info"``, ``"warn"``, or ``"critical"``.
        runlog: Optional ``RunLog`` handle for best-effort event emission.
            When ``None``, emission is skipped without error.
        kind: Escalation kind hint passed through to ``escalate()`` unchanged.
            Defaults to ``"block"``.

    Returns:
        ``True`` for ``severity="info"`` (no escalation attempted).
        For ``"warn"`` and ``"critical"``, returns the result of
        ``escalate()`` — ``True`` if the GitHub comment landed, ``False``
        otherwise.

    Raises:
        Nothing.  All exceptions from runlog emission are swallowed;
        ``escalate()`` itself does not raise.
    """
    # ------------------------------------------------------------------
    # 1. Emit a runlog escalation event (best-effort, all severities).
    # ------------------------------------------------------------------
    if runlog is not None:
        try:
            runlog.emit(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "event": "escalation",
                    "issue": issue,
                    "outcome": None,
                    "severity": severity,
                    "detail": summary,
                    "tick_id": None,
                }
            )
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # 2. Route by severity.
    # ------------------------------------------------------------------
    if severity == "info":
        return True

    if severity == "warn":
        return escalate(owner, repo, issue, summary, kind=kind)

    # severity == "critical"
    body = f"{_CRITICAL_PREFIX}{summary}"
    return escalate(owner, repo, issue, body, kind=kind)


def _post_slack(
    webhook_url: str,
    text: str,
    *,
    issue: int,
    kind: str,
) -> None:
    """POST a plain-text message to a Slack webhook URL (best-effort).

    Uses stdlib ``urllib.request`` only — no third-party dependencies.
    Any failure is logged at WARNING and silently swallowed; the caller
    is NOT affected.

    Args:
        webhook_url: The full Slack incoming-webhook URL.
        text: The message body to post.
        issue: Issue number (for log context).
        kind: Escalation kind (for log context).
    """
    payload = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            _log.info(
                "escalate: Slack notification posted (issue #%d kind=%s)",
                issue,
                kind,
            )
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "escalate: Slack POST failed (issue #%d kind=%s): %s"
            " — durable GitHub record is unaffected.",
            issue,
            kind,
            exc,
        )
