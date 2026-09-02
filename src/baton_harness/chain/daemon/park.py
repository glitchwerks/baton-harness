"""Shared, fail-safe issue parking transition."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Literal

import baton_harness.chain.daemon as _daemon_mod
from baton_harness.chain.app_auth import InstallationTokenSource
from baton_harness.chain.failure_tally import FailureTally
from baton_harness.chain.labels import (
    LABEL_AGENT_FAILED,
    LABEL_AGENT_READY,
    STATE_LABELS,
)
from baton_harness.chain.runlog import RunLog
from baton_harness.chain.session_report import SessionReport

_log = logging.getLogger("baton_harness.chain.daemon")


class ParkClass(Enum):
    """Declared state-machine treatment for a park site."""

    CHARGED = auto()
    UNCHARGED = auto()
    STATE_INTACT = auto()
    UNKNOWN_STATE = auto()
    TERMINAL = auto()


@dataclass(frozen=True)
class ParkContext:
    """Shared dependencies and mutable work-unit state for parking."""

    owner: str
    repo: str
    installation_token: InstallationTokenSource
    report: SessionReport | None
    runlog: RunLog | None
    sched: Any
    liveness_state: Any | None
    parked_reasons: dict[int, str]
    failure_tally: FailureTally | None


def park_issue(
    context: ParkContext,
    issue: int,
    park_class: ParkClass,
    *,
    reason: str,
    detail: str | None,
    severity: Literal["info", "warn", "critical"],
    kind: str,
) -> None:
    """Apply one declared park transition and verify its post-condition."""
    labels = _daemon_mod._fetch_issue_labels(
        context.owner,
        context.repo,
        issue,
        installation_token=context.installation_token,
    )
    add: list[str] = []
    remove = ["agent-in-progress"]
    exhausted = False

    if park_class is ParkClass.CHARGED:
        if context.failure_tally is not None:
            _, exhausted = context.failure_tally.record_and_check(issue)
        if labels is not None:
            remove.extend(sorted(labels & STATE_LABELS))
            add = [
                LABEL_AGENT_FAILED if exhausted else LABEL_AGENT_READY
            ]
    elif park_class is ParkClass.TERMINAL:
        if labels is not None:
            remove.extend(sorted(labels & STATE_LABELS))
            add = [LABEL_AGENT_FAILED]
    elif park_class is ParkClass.UNCHARGED and labels is not None:
        if not labels & STATE_LABELS:
            add = [LABEL_AGENT_READY]

    edit_kwargs: dict[str, Any] = {
        "remove": list(dict.fromkeys(remove)),
        "installation_token": context.installation_token,
        "report": context.report,
    }
    if add:
        edit_kwargs["add"] = add
    edit_succeeded = _daemon_mod._label_edit(
        context.owner,
        context.repo,
        issue,
        **edit_kwargs,
    )

    if context.liveness_state is not None:
        context.liveness_state.clear()
    context.sched.mark_parked(issue)
    context.parked_reasons[issue] = reason

    if detail:
        _daemon_mod.alert(
            context.owner,
            context.repo,
            issue,
            detail,
            severity=severity,
            kind=kind,
            runlog=context.runlog,
            installation_token=context.installation_token,
        )
        if context.report is not None:
            context.report.record_escalation(
                issue,
                kind=kind,
                severity=severity,
                detail=detail,
                ts=datetime.now(timezone.utc).isoformat(),
            )

    post_labels = _daemon_mod._fetch_issue_labels(
        context.owner,
        context.repo,
        issue,
        installation_token=context.installation_token,
    )
    if post_labels is None:
        _log.debug(
            "daemon: park post-condition labels unreadable for #%d", issue
        )
        return
    violation = _daemon_mod.assert_single_state(post_labels)
    if (
        exhausted
        and edit_succeeded
        and post_labels & STATE_LABELS == {LABEL_AGENT_FAILED}
        and context.failure_tally is not None
    ):
        context.failure_tally.reset(issue)
    if violation is not None:
        _daemon_mod.alert(
            context.owner,
            context.repo,
            issue,
            f"Issue #{issue} park post-condition failed for "
            f"{park_class.name}: {violation}",
            severity="critical",
            kind="block",
            runlog=context.runlog,
            installation_token=context.installation_token,
        )
