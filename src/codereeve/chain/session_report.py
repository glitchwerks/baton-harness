"""Deterministic session-report model for daemon activity."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)


@dataclass
class StartupFinding:
    """Record a startup-gate finding.

    Attributes:
        gate: Identifier of the startup gate.
        detail: Human-readable description of the finding.
        pids: Process identifiers associated with the finding.
    """

    gate: str
    detail: str
    pids: list[int] | None


@dataclass
class LabelTransition:
    """Record an ordered issue-label edit.

    Attributes:
        ts: Caller-supplied timestamp for the edit.
        added: Labels added by the edit.
        removed: Labels removed by the edit.
    """

    ts: str
    added: list[str]
    removed: list[str]


@dataclass
class PrRecord:
    """Record a pull request opened for an issue.

    Attributes:
        url: URL of the pull request.
        opened_at: Caller-supplied pull-request opening timestamp.
    """

    url: str
    opened_at: str


@dataclass
class MergeGateRecord:
    """Record the latest merge-gate result for an issue.

    Attributes:
        outcome: Merge-gate outcome.
        merged_sha: Merge commit SHA when the issue was merged.
        ts: Caller-supplied timestamp for the gate result.
    """

    outcome: str
    merged_sha: str | None
    ts: str


@dataclass
class EscalationRecord:
    """Record an issue escalation.

    Attributes:
        ts: Caller-supplied timestamp for the escalation.
        severity: Escalation severity.
        kind: Escalation category.
        detail: Human-readable escalation detail.
    """

    ts: str
    severity: str
    kind: str
    detail: str


@dataclass
class IssueRecord:
    """Collect all report state associated with one issue.

    Attributes:
        number: GitHub issue number.
        repo: Repository containing the issue.
        title: Issue title.
        picked_up_at: Caller-supplied pickup timestamp.
        skipped_at: Caller-supplied skip timestamp.
        label_transitions: Ordered label edits for the issue.
        outcome: Final issue outcome, when known.
        park_reason: Human-readable reason for parking the issue.
        park_kind: Derived parking category.
        pr: Latest pull request recorded for the issue.
        merge_gate: Latest merge-gate result for the issue.
        escalations: Ordered escalations for the issue.
    """

    number: int
    repo: str | None = None
    title: str | None = None
    picked_up_at: str | None = None
    skipped_at: str | None = None
    label_transitions: list[LabelTransition] = field(default_factory=list)
    outcome: str | None = None
    park_reason: str | None = None
    park_kind: str | None = None
    pr: PrRecord | None = None
    merge_gate: MergeGateRecord | None = None
    escalations: list[EscalationRecord] = field(default_factory=list)


@dataclass
class TickRecord:
    """Record one completed daemon polling tick.

    Attributes:
        tick_index: Zero-based tick index.
        started_at: Caller-supplied tick start timestamp.
        ended_at: Caller-supplied tick end timestamp.
        issues_processed: Issue numbers processed during the tick.
        error: Error detail when the tick failed.
    """

    tick_index: int
    started_at: str
    ended_at: str
    issues_processed: list[int]
    error: str | None


class SessionReport:
    """Accumulate a deterministic report of one daemon session.

    Attributes:
        mode: Daemon operating mode.
        poll_interval_s: Poll interval in seconds.
        registry: Repository registry active for the session.
        started_at: Caller-supplied session start timestamp.
        ended_at: Caller-supplied session end timestamp.
        exit_reason: Reason the session ended.
    """

    def __init__(
        self,
        *,
        mode: str,
        poll_interval_s: float,
        registry: list[dict[str, Any]],
        started_at: str,
    ) -> None:
        """Initialize an empty session report.

        Args:
            mode: Daemon operating mode.
            poll_interval_s: Poll interval in seconds.
            registry: Repository registry active for the session.
            started_at: Caller-supplied session start timestamp.
        """
        self.mode = mode
        self.poll_interval_s = poll_interval_s
        self.registry = [dict(entry) for entry in registry]
        self.started_at = started_at
        self.ended_at: str | None = None
        self.exit_reason: str | None = None
        self._startup_findings: list[StartupFinding] = []
        self._issues: dict[int, IssueRecord] = {}
        self._ticks: list[TickRecord] = []
        self._active_tick_started_at: str | None = None
        self._picked_up_numbers: set[int] = set()
        self._pr_calls = 0
        self._escalation_calls = 0
        self._merge_gate_failures = 0

    def record_startup_finding(
        self,
        *,
        gate: str,
        detail: str,
        pids: list[int] | None,
    ) -> None:
        """Append a startup-gate finding.

        Args:
            gate: Identifier of the startup gate.
            detail: Human-readable description of the finding.
            pids: Process identifiers associated with the finding.
        """
        copied_pids = None if pids is None else list(pids)
        self._startup_findings.append(
            StartupFinding(gate=gate, detail=detail, pids=copied_pids)
        )

    def record_pickup(
        self,
        number: int,
        *,
        repo: str,
        title: str,
        picked_up_at: str,
    ) -> None:
        """Create or update an issue when it is picked up.

        Args:
            number: GitHub issue number.
            repo: Repository containing the issue.
            title: Issue title.
            picked_up_at: Caller-supplied pickup timestamp.
        """
        issue = self._get_issue(number)
        issue.repo = repo
        issue.title = title
        issue.picked_up_at = picked_up_at
        self._picked_up_numbers.add(number)

    def record_skipped_blocked(
        self,
        number: int,
        *,
        repo: str,
        title: str,
        skipped_at: str,
    ) -> None:
        """Create or update an issue skipped before dispatch.

        Args:
            number: GitHub issue number.
            repo: Repository containing the issue.
            title: Issue title.
            skipped_at: Caller-supplied skip timestamp.
        """
        issue = self._get_issue(number)
        issue.repo = repo
        issue.title = title
        issue.skipped_at = skipped_at
        issue.outcome = "skipped_blocked"

    def record_label_edit(
        self,
        number: int,
        *,
        added: list[str],
        removed: list[str],
        ts: str,
    ) -> None:
        """Append an ordered label transition for an issue.

        Args:
            number: GitHub issue number.
            added: Labels added by the edit.
            removed: Labels removed by the edit.
            ts: Caller-supplied timestamp for the edit.
        """
        self._get_issue(number).label_transitions.append(
            LabelTransition(
                ts=ts,
                added=list(added),
                removed=list(removed),
            )
        )

    def record_pr(
        self,
        number: int,
        *,
        url: str,
        opened_at: str,
    ) -> None:
        """Record a pull request opened for an issue.

        Args:
            number: GitHub issue number.
            url: URL of the pull request.
            opened_at: Caller-supplied pull-request opening timestamp.
        """
        self._get_issue(number).pr = PrRecord(
            url=url,
            opened_at=opened_at,
        )
        self._pr_calls += 1

    def record_merge_gate(
        self,
        number: int,
        *,
        outcome: str,
        merged_sha: str | None,
        ts: str,
    ) -> None:
        """Record a merge-gate result for an issue.

        Args:
            number: GitHub issue number.
            outcome: Merge-gate outcome.
            merged_sha: Merge commit SHA when the issue was merged.
            ts: Caller-supplied timestamp for the gate result.
        """
        self._get_issue(number).merge_gate = MergeGateRecord(
            outcome=outcome,
            merged_sha=merged_sha,
            ts=ts,
        )
        if outcome != "MERGED":
            self._merge_gate_failures += 1

    def record_escalation(
        self,
        number: int,
        *,
        kind: str,
        severity: str,
        detail: str,
        ts: str,
    ) -> None:
        """Append an escalation for an issue.

        Args:
            number: GitHub issue number.
            kind: Escalation category.
            severity: Escalation severity.
            detail: Human-readable escalation detail.
            ts: Caller-supplied timestamp for the escalation.
        """
        self._get_issue(number).escalations.append(
            EscalationRecord(
                ts=ts,
                severity=severity,
                kind=kind,
                detail=detail,
            )
        )
        self._escalation_calls += 1

    def set_outcomes(
        self,
        *,
        merged_issues: list[int],
        parked_reasons: dict[int, str],
    ) -> None:
        """Fold merged and parked outcomes into issue records.

        Args:
            merged_issues: Issue numbers that were merged.
            parked_reasons: Parking reasons keyed by issue number.
        """
        for number in merged_issues:
            issue = self._get_issue(number)
            issue.outcome = "merged"
            issue.park_reason = None
            issue.park_kind = None

        for number, reason in parked_reasons.items():
            issue = self._get_issue(number)
            issue.outcome = "parked"
            issue.park_reason = reason
            issue.park_kind = "block" if "block" in reason.casefold() else None

    def begin_tick(self, *, started_at: str) -> None:
        """Begin a polling tick.

        Args:
            started_at: Caller-supplied tick start timestamp.

        Raises:
            RuntimeError: If a tick is already active.
        """
        if self._active_tick_started_at is not None:
            raise RuntimeError("cannot begin a tick while one is active")
        self._active_tick_started_at = started_at

    def end_tick(
        self,
        *,
        issues_processed: list[int],
        ended_at: str,
        error: str | None = None,
    ) -> None:
        """Complete the active polling tick and append its record.

        Args:
            issues_processed: Issue numbers processed during the tick.
            ended_at: Caller-supplied tick end timestamp.
            error: Error detail when the tick failed.

        Raises:
            RuntimeError: If no tick has been started.
        """
        if self._active_tick_started_at is None:
            raise RuntimeError("cannot end a tick before beginning one")

        self._ticks.append(
            TickRecord(
                tick_index=len(self._ticks),
                started_at=self._active_tick_started_at,
                ended_at=ended_at,
                issues_processed=list(issues_processed),
                error=error,
            )
        )
        self._active_tick_started_at = None

    def set_exit_reason(self, reason: str, *, ended_at: str) -> None:
        """Record why and when the session ended.

        Args:
            reason: Session exit reason.
            ended_at: Caller-supplied session end timestamp.
        """
        self.exit_reason = reason
        self.ended_at = ended_at

    def to_dict(self) -> dict[str, Any]:
        """Return the complete report as a JSON-serializable dictionary.

        Returns:
            A plain dictionary conforming to session-report schema
            version 1.
        """
        issues = [
            self._issue_to_dict(issue) for issue in self._issues.values()
        ]
        ticks = [
            {
                "tick_index": tick.tick_index,
                "started_at": tick.started_at,
                "ended_at": tick.ended_at,
                "issues_processed": list(tick.issues_processed),
                "error": tick.error,
            }
            for tick in self._ticks
        ]
        findings = [
            {
                "gate": finding.gate,
                "detail": finding.detail,
                "pids": (None if finding.pids is None else list(finding.pids)),
            }
            for finding in self._startup_findings
        ]

        return {
            "schema_version": 1,
            "session": {
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "mode": self.mode,
                "exit_reason": self.exit_reason,
                "poll_interval_s": self.poll_interval_s,
                "registry": [dict(entry) for entry in self.registry],
            },
            "startup": {"findings": findings},
            "totals": {
                "ticks": len(self._ticks),
                "issues_picked_up": len(self._picked_up_numbers),
                "prs_opened": self._pr_calls,
                "issues_merged": sum(
                    issue.outcome == "merged"
                    for issue in self._issues.values()
                ),
                "issues_parked": sum(
                    issue.outcome == "parked"
                    for issue in self._issues.values()
                ),
                "escalations": self._escalation_calls,
                "merge_gate_failures": self._merge_gate_failures,
            },
            "issues": issues,
            "ticks": ticks,
        }

    def write(self, path: str | Path) -> None:
        """Atomically write the report, swallowing all failures.

        Args:
            path: Destination JSON path.
        """
        temporary: Path | None = None
        try:
            destination = Path(path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(f"{path}.tmp.{os.getpid()}")
            text = json.dumps(self.to_dict(), indent=2) + "\n"
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, destination)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "session_report: failed to write report %r: %s",
                str(path),
                exc,
            )
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _get_issue(self, number: int) -> IssueRecord:
        """Return an issue record, creating it when necessary."""
        issue = self._issues.get(number)
        if issue is None:
            issue = IssueRecord(number=number)
            self._issues[number] = issue
        return issue

    @staticmethod
    def _issue_to_dict(issue: IssueRecord) -> dict[str, Any]:
        """Convert an issue record to a plain dictionary."""
        pr = None
        if issue.pr is not None:
            pr = {
                "url": issue.pr.url,
                "opened_at": issue.pr.opened_at,
            }

        merge_gate = None
        if issue.merge_gate is not None:
            merge_gate = {
                "outcome": issue.merge_gate.outcome,
                "merged_sha": issue.merge_gate.merged_sha,
                "ts": issue.merge_gate.ts,
            }

        return {
            "number": issue.number,
            "repo": issue.repo,
            "title": issue.title,
            "picked_up_at": issue.picked_up_at,
            "skipped_at": issue.skipped_at,
            "label_transitions": [
                {
                    "ts": transition.ts,
                    "added": list(transition.added),
                    "removed": list(transition.removed),
                }
                for transition in issue.label_transitions
            ],
            "outcome": issue.outcome,
            "park_reason": issue.park_reason,
            "park_kind": issue.park_kind,
            "pr": pr,
            "merge_gate": merge_gate,
            "escalations": [
                {
                    "ts": escalation.ts,
                    "severity": escalation.severity,
                    "kind": escalation.kind,
                    "detail": escalation.detail,
                }
                for escalation in issue.escalations
            ],
        }
