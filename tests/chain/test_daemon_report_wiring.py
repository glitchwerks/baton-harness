"""Unit tests for daemon SessionReport wiring (issue #302, #243 Phase 2).

These tests pin the behavioral contract delivered by Phase 2 of
``docs/superpowers/plans/2026-07-09-daemon-report-scenario-harness-243.md``
(daemon wiring: report accumulation + finally emit + ``daemon_stop``):
``run_daemon`` accumulates a ``SessionReport`` across a run and writes it
to an explicit ``report_path`` from a ``finally`` block on every terminal
exit path (normal completion, SIGTERM, and an in-tick exception), and the
S7 signature changes to ``_run_ci_gate`` and ``_open_pr`` let that report
record a PR url and a merge-gate outcome/sha. This file is the regression
suite pinning that contract going forward, not a red/not-yet-implemented
snapshot.

All tests drive the real ``run_daemon`` with the same ``_run``/subprocess
seam-stubbing convention already established in ``tests/chain/test_daemon.py``
and ``tests/chain/test_daemon_orphan_scan.py`` (helpers are duplicated here,
not imported, to keep this file self-contained per that established
convention). Report assertions read the **written JSON artifact** from an
explicit ``report_path`` rather than reaching into any in-memory ``report``
object, so these tests pin the observable contract (the file on disk) and
stay agnostic to how the implementer threads the report through internal
call sites.

Coverage:
- Comprehensive happy path: issue pickup, daemon-attested label transitions,
  a PR url recorded from ``_open_pr``'s new return value, and a merge-gate
  outcome + ``merged_sha`` recorded from ``_run_ci_gate``'s new return value
  (S7).
- A post-worker single-state ``blocked`` label produces a ``parked`` issue
  outcome with ``park_kind="block"`` and an escalation with ``kind="block"``
  (B2 aggregation via ``set_outcomes``, §2.4/§2.7).
- A plain ``once=True`` run with no ready issues records exactly one tick and
  ``exit_reason="once_complete"`` (S6).
- A SIGTERM delivered mid-tick (simulated by invoking the captured
  ``signal.signal`` handler synchronously from inside the ``_run`` seam, in
  the **same thread/process** — no background thread is used, per the S6
  main-thread caveat) records ``exit_reason="sigterm"``.
- A raised exception during a later work unit's DAG construction does not
  crash the daemon and still leaves the earlier work unit's outcome (and a
  ``tick_error``) in the written report (isolation).
- Two narrow, plumbing-independent pins of the S7 signature changes:
  ``_run_ci_gate`` returns the ``MergeOutcome`` directly (called with the
  exact kwarg convention already proven in
  ``TestRunCiGateForwardsToken.test_run_ci_gate_forwards_token_to_merge_issue_branch``),
  and ``_open_pr`` returns the ``gh pr create`` stdout URL instead of
  ``None`` (observed via a call-through spy since no existing test names its
  parameters).

Assumptions made when concretizing signatures the plan only sketches (see the
router return for the full list): ``run_daemon`` gains a new
``report_path: Path | None = None`` keyword argument in **this** phase (not
deferred to Phase 3) — grounded in the plan's own Phase 3 text, which
describes forwarding the CLI's resolved path as
``run_daemon(..., report_path=...)``, and in Phase 2's requirement that the
finally block already write *somewhere* before the CLI flag exists. Every
test below passes ``report_path`` explicitly (never relying on the
always-on default path) so assertions never depend on ``BH_PROJECT_ROOT``.
The exact commit SHA source for ``merge_gate.merged_sha`` is left
unspecified by the plan (it could be the known PR head SHA or a
post-merge ``git rev-parse``), so the ``_run`` seam returns the **same**
sentinel SHA from every git command that could plausibly supply it
(the PR-list ``headRefOid`` and the ``git rev-parse`` fallback), and the
assertion checks that sentinel rather than favoring one source over the
other.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import baton_harness.chain.daemon as daemon_mod
from baton_harness.chain.daemon import run_daemon
from baton_harness.chain.merge import MergeOutcome
from baton_harness.chain.recovery import RecoveryResult
from baton_harness.chain.registry import RepoConfig
from baton_harness.vendor.symphony.config import WorkflowConfig

# ---------------------------------------------------------------------------
# Helpers (mirrors tests/chain/test_daemon.py's conventions; duplicated
# rather than imported so this file stays self-contained, per the existing
# tests/chain/test_daemon_orphan_scan.py precedent).
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OWNER = "glitchwerks"
_REPO_NAME = "baton-harness"

# Sentinel SHA used for every git command that could plausibly supply the
# report's merge_gate.merged_sha (see module docstring "Assumptions").
_FEED_SHA = "feedface" * 5


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    """Return a successful CompletedProcess."""
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=""
    )


def _minimal_wf_config() -> WorkflowConfig:
    """Return a minimal WorkflowConfig."""
    return WorkflowConfig(
        prompt_template="Work on #{{ issue.number }}",
        tracker_labels=["agent-ready"],
        tracker_exclude_labels=["blocked"],
        tracker_assignee=None,
        max_concurrent=1,
        max_turns=8,
        hook_after_create=None,
        hook_before_run=None,
        hook_after_run=None,
        hook_timeout_ms=5000,
        poll_interval_ms=1000,
        max_retry_backoff_ms=10000,
    )


def _repo_cfg(project_root: Path) -> RepoConfig:
    """Return a minimal RepoConfig rooted at ``project_root``.

    Args:
        project_root: Directory to use as the config's project root.
            Callers must pass the test's ``tmp_path`` fixture, never the
            real repo checkout — ``run_daemon`` startup writes a
            ``.baton-harness/daemon.alive`` liveness marker (and a work
            unit creates a ``.symphony/state.json``) under this path, so
            a real-repo root would leak untracked artifacts into the
            working tree.

    A ``.git`` marker directory is created under ``project_root`` (if
    absent) so ``_launch_one_issue``'s ``has_git_dir`` check (see
    ``launch_gate.py``) does not fail closed on the
    ``_NON_GIT_REPO_ROOT`` sentinel path. This does not make
    ``project_root`` a real git repo or invoke any real git command —
    ``_probe_worker_push_denied``, the only consumer of a "real" git
    worktree once ``has_git_dir`` is true, is itself fully mocked by the
    autouse ``_auto_patch_push_probe_daemon`` fixture in
    ``tests/conftest.py`` for every test in this module.
    """
    (project_root / ".git").mkdir(exist_ok=True)
    return RepoConfig(
        owner=_OWNER,
        repo=_REPO_NAME,
        project_root=project_root,
    )


def _isolate_work_unit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard against ambient ``os.environ`` leaks from the work-unit path.

    ``_run_work_unit`` writes ``BH_VENV``, ``CHAIN_BASE_BRANCH``, and
    ``BH_FEATURE_BRANCH`` directly to the real ``os.environ`` (not via
    ``monkeypatch``) as a side effect of dispatching a work unit. Those
    writes would otherwise outlive the test and bleed into later tests in
    the same pytest process. Calling ``monkeypatch.delenv`` here — before
    ``run_daemon`` runs — arms ``monkeypatch``'s teardown to restore each
    key to its pre-test state (absent, in the normal case) regardless of
    what the code under test writes to it in between.

    Args:
        monkeypatch: The test's ``monkeypatch`` fixture.
    """
    for key in ("BH_VENV", "CHAIN_BASE_BRANCH", "BH_FEATURE_BRANCH"):
        monkeypatch.delenv(key, raising=False)


def _make_issue(number: int, labels: list[str]) -> dict[str, Any]:
    """Build a minimal un-milestoned gh issue dict."""
    return {
        "number": number,
        "title": f"Issue {number}",
        "state": "open",
        "body": "",
        "url": f"https://github.com/o/r/issues/{number}",
        "labels": [{"name": lbl} for lbl in labels],
        "milestone": None,
        "assignees": [],
    }


def _gh_noun_verb(cmd: list[str]) -> tuple[str | None, str | None]:
    """Extract the ``gh <noun> <verb>`` tokens from a ``_run`` argv.

    Mirrors ``tests/chain/test_daemon.py``'s helper of the same name.
    """
    is_gh = bool(cmd) and cmd[0] == "gh"
    noun = cmd[1] if is_gh and len(cmd) > 1 else None
    verb = cmd[2] if is_gh and len(cmd) > 2 else None
    return noun, verb


def _make_run_side_effect(
    *,
    ready_issues: list[dict[str, Any]],
    issue_branch_by_number: dict[int, str],
    post_worker_labels: str = "agent-done",
) -> Any:  # noqa: ANN401
    """Build a ``_run`` side-effect handling common gh/git commands.

    Args:
        ready_issues: Issues returned for ``gh issue list --label
            agent-ready``.
        issue_branch_by_number: Maps issue number to its issue-branch name
            for the ``gh pr list`` stub.
        post_worker_labels: The single label name returned by ``gh issue
            view`` (i.e. the post-worker label state a real
            ``_fetch_issue_labels`` implementation would observe). Only
            used when a test does not separately patch
            ``daemon._fetch_issue_labels`` directly.

    Returns:
        A callable matching ``daemon_mod._run``'s signature.
    """

    def side_effect(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        noun, verb = _gh_noun_verb(cmd)

        if noun == "issue" and verb == "list" and "agent-ready" in cmd:
            return _ok(json.dumps(ready_issues))
        if noun == "issue" and verb == "list" and "agent-in-progress" in cmd:
            return _ok(json.dumps([]))
        if noun == "issue" and verb == "view":
            nums = [p for p in cmd if p.isdigit()]
            n = int(nums[0]) if nums else 10
            return _ok(
                json.dumps(
                    {
                        "number": n,
                        "title": f"Issue {n}",
                        "state": "open",
                        "body": "",
                        "url": f"https://github.com/o/r/issues/{n}",
                        "labels": [{"name": post_worker_labels}],
                        "assignees": [],
                    }
                )
            )
        if noun == "issue" and verb == "edit":
            return _ok()
        if noun == "pr" and verb == "list":
            prs = [
                {
                    "number": n,
                    "headRefName": branch,
                    "headRefOid": _FEED_SHA,
                }
                for n, branch in issue_branch_by_number.items()
            ]
            return _ok(json.dumps(prs))
        if noun == "pr" and verb == "create":
            return _ok("https://github.com/o/r/pull/99")
        if "git" in cmd and "push" in cmd:
            return _ok()
        if "ls-remote" in cmd:
            return _ok("")
        if "rev-parse" in cmd:
            return _ok(f"{_FEED_SHA}\n")
        return _ok()

    return side_effect


def _patch_run_worker(return_value: str = "pr_created") -> Any:  # noqa: ANN401
    """Patch Orchestrator._run_worker with an AsyncMock."""
    from unittest.mock import AsyncMock

    return patch(
        "baton_harness.vendor.symphony.orchestrator.Orchestrator._run_worker",
        new_callable=AsyncMock,
        return_value=return_value,
    )


def _common_success_patches() -> Any:  # noqa: ANN401
    """Return the standard non-``_run`` patch stack for a clean work unit."""
    import contextlib

    @contextlib.contextmanager
    def ctx() -> Any:  # noqa: ANN401
        with (
            patch(
                "baton_harness.chain.daemon.fetch_blocked_by",
                return_value=[],
            ),
            patch("baton_harness.chain.branches.create_feature_branch"),
            patch("baton_harness.chain.branches.checkout_feature_branch"),
            patch(
                "baton_harness.chain.branches.record_cut_point",
                return_value="deadbeef" * 5,
            ),
            patch(
                "baton_harness.chain.recovery.reconstruct",
                return_value=RecoveryResult(
                    done=set(),
                    parked_seed=set(),
                    ci_gate_reentry=set(),
                    redispatch=set(),
                ),
            ),
            patch(
                "baton_harness.chain.daemon.merge_issue_branch",
                return_value=MergeOutcome.MERGED,
            ),
            patch("baton_harness.chain.daemon.alert", return_value=True),
        ):
            yield

    return ctx


# ---------------------------------------------------------------------------
# Happy path: pickup + label transitions + PR url + merge-gate outcome/sha
# ---------------------------------------------------------------------------


def test_report_captures_pickup_label_transitions_pr_url_and_merge_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Report records pickup, label transitions, PR url, and merge gate.

    A single clean un-milestoned issue merges. The written report must
    show, for that issue: correct pickup identity fields, at least one
    daemon-attested ``agent-in-progress`` add and a later removal (ordered
    per §5.1 M12, but this test only checks presence — full ordering is a
    Phase 1 ``SessionReport`` concern already covered by
    ``test_session_report.py``), the PR url returned by the new ``_open_pr``
    (S7), and a ``merge_gate`` outcome of ``"MERGED"`` with a ``merged_sha``
    (S7 / S5).
    """
    _isolate_work_unit_env(monkeypatch)
    ready_issues = [_make_issue(10, ["agent-ready"])]
    report_path = tmp_path / "session-report.json"

    with (
        patch.object(
            daemon_mod,
            "_run",
            side_effect=_make_run_side_effect(
                ready_issues=ready_issues,
                issue_branch_by_number={10: "baton/issue-10-10"},
            ),
        ),
        _common_success_patches()(),
        _patch_run_worker("pr_created"),
    ):
        asyncio.run(
            run_daemon(
                _minimal_wf_config(),
                [_repo_cfg(tmp_path)],
                once=True,
                poll_interval_s=0,
                report_path=report_path,
            )
        )

    assert report_path.exists(), (
        "run_daemon must write a session report to report_path"
    )
    data = json.loads(report_path.read_text(encoding="utf-8"))

    issues = data["issues"]
    assert len(issues) == 1, f"expected exactly one issue record, got {issues}"
    issue = issues[0]

    assert issue["number"] == 10
    assert issue["repo"] == f"{_OWNER}/{_REPO_NAME}"
    assert issue["picked_up_at"], "picked_up_at must be a non-empty timestamp"
    assert issue["outcome"] == "merged"

    transitions = issue["label_transitions"]
    assert transitions, "label_transitions must be non-empty"
    added_sets = [t["added"] for t in transitions]
    removed_sets = [t["removed"] for t in transitions]
    assert any("agent-in-progress" in a for a in added_sets), (
        "daemon-attested dispatch must record adding agent-in-progress; "
        f"transitions={transitions}"
    )
    assert any("agent-in-progress" in r for r in removed_sets), (
        "daemon-attested completion must record removing agent-in-progress; "
        f"transitions={transitions}"
    )

    assert issue["pr"] is not None, "pr must be recorded for the opened PR"
    assert issue["pr"]["url"] == "https://github.com/o/r/pull/99", (
        f"pr.url must be the gh pr create stdout URL; got {issue['pr']!r}"
    )

    assert issue["merge_gate"] is not None, "merge_gate must be recorded"
    assert issue["merge_gate"]["outcome"] == "MERGED", (
        f"merge_gate.outcome must be the MergeOutcome name; "
        f"got {issue['merge_gate']!r}"
    )
    assert issue["merge_gate"]["merged_sha"] == _FEED_SHA, (
        "merge_gate.merged_sha must be threaded from the daemon's own git "
        f"output; got {issue['merge_gate']!r}"
    )

    assert data["session"]["exit_reason"] == "once_complete"


# ---------------------------------------------------------------------------
# Park + block kind + escalation (B2 / §2.7)
# ---------------------------------------------------------------------------


def test_report_captures_parked_issue_with_block_kind_and_escalation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-worker single ``blocked`` label parks with park_kind=block.

    Mirrors the existing (pre-Phase-2) blocked-park path exercised by
    ``test_daemon.py::test_single_blocked_post_worker_does_not_fire_invariant_critical``
    (patch ``daemon._fetch_issue_labels`` to return ``{"blocked"}``). Phase 2
    does not change *when* the daemon parks — only that the park now also
    lands in the report via ``set_outcomes``' park_kind derivation (§2.4:
    a case-insensitive "block" substring match on the park reason text) and
    the daemon's own ``alert(kind="block")`` argument (§2.7).
    """
    _isolate_work_unit_env(monkeypatch)
    ready_issues = [_make_issue(20, ["agent-ready"])]
    report_path = tmp_path / "session-report.json"

    with (
        patch.object(
            daemon_mod,
            "_run",
            side_effect=_make_run_side_effect(
                ready_issues=ready_issues,
                issue_branch_by_number={20: "baton/issue-20-20"},
            ),
        ),
        patch(
            "baton_harness.chain.daemon._fetch_issue_labels",
            return_value={"blocked"},
        ),
        _common_success_patches()(),
        _patch_run_worker("pr_created"),
    ):
        asyncio.run(
            run_daemon(
                _minimal_wf_config(),
                [_repo_cfg(tmp_path)],
                once=True,
                poll_interval_s=0,
                report_path=report_path,
            )
        )

    data = json.loads(report_path.read_text(encoding="utf-8"))
    issues = data["issues"]
    assert len(issues) == 1, f"expected exactly one issue record, got {issues}"
    issue = issues[0]

    assert issue["number"] == 20
    assert issue["outcome"] == "parked", (
        f"a single blocked post-worker label must park the issue; "
        f"got outcome={issue['outcome']!r}"
    )
    assert issue["park_reason"], "park_reason must be recorded"
    assert issue["park_kind"] == "block", (
        "park_kind must be derived as 'block' from the daemon's own park "
        f"reason text; got {issue['park_kind']!r}"
    )

    escalations = issue["escalations"]
    assert escalations, "escalations must be non-empty for a blocked park"
    assert any(e["kind"] == "block" for e in escalations), (
        "at least one escalation must carry the daemon-attested "
        f"kind='block' argument; got escalations={escalations}"
    )


# ---------------------------------------------------------------------------
# exit_reason=once_complete + single tick (S6 / tick tracking)
# ---------------------------------------------------------------------------


def test_report_records_once_complete_exit_reason_and_single_tick(
    tmp_path: Path,
) -> None:
    """A normal ``once=True`` run with no ready issues records one tick.

    Pins the ``_exit_reason`` restructure's normal-exit branch (S6: set to
    ``"once_complete"`` immediately before ``if once: break``) and the basic
    ``begin_tick``/``end_tick`` bracketing (one tick per poll cycle).
    """
    report_path = tmp_path / "session-report.json"

    with (
        patch.object(
            daemon_mod,
            "_run",
            side_effect=_make_run_side_effect(
                ready_issues=[],
                issue_branch_by_number={},
            ),
        ),
        _common_success_patches()(),
    ):
        asyncio.run(
            run_daemon(
                _minimal_wf_config(),
                [_repo_cfg(tmp_path)],
                once=True,
                poll_interval_s=0,
                report_path=report_path,
            )
        )

    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert data["session"]["exit_reason"] == "once_complete"
    assert data["totals"]["ticks"] == 1, (
        f"expected exactly one recorded tick; totals={data['totals']!r}"
    )
    assert len(data["ticks"]) == 1
    assert data["ticks"][0]["error"] is None


# ---------------------------------------------------------------------------
# SIGTERM mid-tick (S6 main-thread caveat: simulated in-process, no thread)
# ---------------------------------------------------------------------------


def test_sigterm_mid_tick_records_sigterm_exit_reason(
    tmp_path: Path,
) -> None:
    """A SIGTERM delivered mid-tick records exit_reason="sigterm".

    The SIGTERM handler installed by ``run_daemon`` (via ``signal.signal``,
    already covered by
    ``test_daemon.py::test_run_daemon_registers_sigterm_handler_on_startup``)
    is captured via a spy, then invoked **synchronously from inside the
    ``_run`` seam** — i.e. in the same thread that is executing
    ``run_daemon``'s poll loop, before the ``if once: break`` line would
    ever run. This deliberately avoids a background thread: per §2.6's
    main-thread caveat, ``signal.signal`` silently degrades off the main
    thread, so a thread-driven ``run_daemon`` would never install a real
    handler and this test would then be asserting nothing.
    """
    import signal as _signal

    report_path = tmp_path / "session-report.json"
    registered_handler: list[Any] = []
    _real_signal = _signal.signal

    def spy_signal(signum: int, handler: Any) -> Any:  # noqa: ANN401
        if signum == _signal.SIGTERM:
            registered_handler.append(handler)
        return _real_signal(signum, handler)

    list_calls = {"n": 0}
    base_side_effect = _make_run_side_effect(
        ready_issues=[], issue_branch_by_number={}
    )

    def run_side_effect(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        noun, verb = _gh_noun_verb(cmd)
        if noun == "issue" and verb == "list" and "agent-ready" in cmd:
            list_calls["n"] += 1
            if list_calls["n"] == 1 and registered_handler:
                # Simulate an async SIGTERM arriving mid-tick, in-process.
                registered_handler[0](_signal.SIGTERM, None)
        return base_side_effect(cmd)

    with (
        patch("signal.signal", side_effect=spy_signal),
        patch.object(daemon_mod, "_run", side_effect=run_side_effect),
        _common_success_patches()(),
    ):
        try:
            asyncio.run(
                run_daemon(
                    _minimal_wf_config(),
                    [_repo_cfg(tmp_path)],
                    once=True,
                    poll_interval_s=0,
                    report_path=report_path,
                )
            )
        except SystemExit:
            pass

    assert registered_handler, (
        "run_daemon must register a SIGTERM handler for this test to "
        "simulate delivery"
    )
    assert report_path.exists(), (
        "the finally block must write the report even on a SIGTERM exit"
    )
    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert data["session"]["exit_reason"] == "sigterm", (
        "a SIGTERM raised mid-tick must be recorded as exit_reason='sigterm', "
        f"not overwritten by the normal once_complete path; session="
        f"{data['session']!r}"
    )


# ---------------------------------------------------------------------------
# tick_error isolation: a later work unit's exception must not lose the
# earlier work unit's already-accumulated state, and must not crash.
# ---------------------------------------------------------------------------


def test_tick_error_recorded_and_partial_state_survives_on_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception building issue #99's DAG records a tick_error.

    Two un-milestoned ready issues are dispatched in the same tick (the
    existing multi-work-unit drain behavior — see
    ``test_daemon.py::test_second_work_unit_skipped_when_blocked_mid_drain``
    for the same two-issues-in-one-tick pattern). ``_select_work_unit``
    picks the lower issue number first, so #10 fully merges before
    ``fetch_blocked_by`` raises for #99's DAG construction. The already-
    accumulated #10 outcome must survive in the written report, a
    ``tick_error`` must be recorded, and ``run_daemon`` must return
    normally (isolation is pre-existing daemon behavior — see
    ``test_daemon.py::test_work_unit_exception_daemon_survives_and_proceeds``
    — Phase 2 only adds the ``tick_error`` *recording* on top of it).

    ``_fetch_issue_labels`` is patched directly (rather than relying on
    the shared ``_run``-based ``gh issue view`` stub, which returns the
    same label set for every issue number) because #99 is the SECOND
    work unit selected in this drain (``_drain_idx == 1``): the mid-drain
    live re-check added by VP-2/#132 (``_poll_and_run``, un-related to
    and out of scope for #302) re-fetches #99's live labels before
    letting it dispatch, and excludes it if ``agent-ready`` is not
    present. A constant ``gh issue view`` stub would report #99 as
    ``agent-done`` there and the work unit would be skipped — never
    reaching ``fetch_blocked_by`` at all, so the exception this test
    needs would never fire. Mirrors the same direct-patch workaround
    already used by
    ``test_daemon.py::test_second_work_unit_skipped_when_blocked_mid_drain``
    for exactly this mid-drain gate.
    """
    _isolate_work_unit_env(monkeypatch)
    ready_issues = [
        _make_issue(10, ["agent-ready"]),
        _make_issue(99, ["agent-ready"]),
    ]
    report_path = tmp_path / "session-report.json"

    def fake_fetch_blocked_by(
        owner: str,
        repo: str,
        number: int,
        **kwargs: Any,  # noqa: ANN401
    ) -> list[int]:
        if number == 99:
            raise RuntimeError("simulated poll failure for issue 99")
        return []

    def fake_fetch_issue_labels(
        owner: str,
        repo: str,
        number: int,
        installation_token: str = "",
    ) -> set[str] | None:
        """Live labels used by every daemon-side re-check on this drain.

        #99 must keep carrying ``agent-ready`` through every re-check
        (tick-start torn-state check and the mid-drain gate) so it is
        never skipped before its DAG construction runs and
        ``fetch_blocked_by`` raises. #10's post-worker state stays
        ``agent-done`` (no ``agent-ready``/``blocked``), matching the
        happy-path convention elsewhere in this file, so it merges
        normally.
        """
        if number == 99:
            return {"agent-ready"}
        return {"agent-done"}

    with (
        patch.object(
            daemon_mod,
            "_run",
            side_effect=_make_run_side_effect(
                ready_issues=ready_issues,
                issue_branch_by_number={
                    10: "baton/issue-10-10",
                    99: "baton/issue-99-99",
                },
            ),
        ),
        patch(
            "baton_harness.chain.daemon._fetch_issue_labels",
            side_effect=fake_fetch_issue_labels,
        ),
        patch(
            "baton_harness.chain.daemon.fetch_blocked_by",
            side_effect=fake_fetch_blocked_by,
        ),
        patch("baton_harness.chain.branches.create_feature_branch"),
        patch("baton_harness.chain.branches.checkout_feature_branch"),
        patch(
            "baton_harness.chain.branches.record_cut_point",
            return_value="deadbeef" * 5,
        ),
        patch(
            "baton_harness.chain.recovery.reconstruct",
            return_value=RecoveryResult(
                done=set(),
                parked_seed=set(),
                ci_gate_reentry=set(),
                redispatch=set(),
            ),
        ),
        patch(
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon.alert", return_value=True),
        _patch_run_worker("pr_created"),
    ):
        # Must NOT raise — the daemon survives the mid-tick exception
        # (pre-existing isolation; Phase 2 only adds the tick_error
        # recording on top of it).
        asyncio.run(
            run_daemon(
                _minimal_wf_config(),
                [_repo_cfg(tmp_path)],
                once=True,
                poll_interval_s=0,
                report_path=report_path,
            )
        )

    data = json.loads(report_path.read_text(encoding="utf-8"))

    by_number = {i["number"]: i for i in data["issues"]}
    assert 10 in by_number, (
        f"issue #10's outcome must survive #99's mid-tick failure; "
        f"issues={data['issues']!r}"
    )
    assert by_number[10]["outcome"] == "merged"

    ticks = data["ticks"]
    assert ticks, "at least one tick must be recorded"
    assert any(t["error"] for t in ticks), (
        f"a tick_error must be recorded for the #99 failure; ticks={ticks!r}"
    )


# ---------------------------------------------------------------------------
# S7 signature pins (plumbing-independent — call the changed functions
# directly rather than only observing them through the full report).
# ---------------------------------------------------------------------------


def test_run_ci_gate_returns_merge_outcome_directly() -> None:
    """S7: _run_ci_gate returns the MergeOutcome instead of None.

    Uses the exact kwarg convention already proven correct by
    ``test_daemon.py::TestRunCiGateForwardsToken.
    test_run_ci_gate_forwards_token_to_merge_issue_branch`` so this test's
    own call cannot be the source of a false red.
    """
    mock_sched = MagicMock()
    mock_sched.mark_done = MagicMock()
    mock_sched.mark_parked = MagicMock()

    with (
        patch(
            "baton_harness.chain.daemon.merge_issue_branch",
            return_value=MergeOutcome.MERGED,
        ),
        patch("baton_harness.chain.daemon.alert", return_value=True),
        patch("baton_harness.chain.daemon._label_edit"),
    ):
        result = daemon_mod._run_ci_gate(
            owner=_OWNER,
            repo=_REPO_NAME,
            n=42,
            issue_branch="baton/issue-42-42",
            pr_head_sha=_FEED_SHA,
            repo_root=_REPO_ROOT,
            branch_name="feature/test-slug",
            sched=mock_sched,
            liveness_state=None,
            runlog=None,
            merged_issues=[],
            parked_reasons={},
            ci_poll_interval=0.1,
            ci_timeout=5.0,
            installation_token="ghs_TEST_token",
        )

    assert result == MergeOutcome.MERGED, (
        "S7: _run_ci_gate must return the MergeOutcome to its caller "
        f"instead of None; got {result!r}"
    )


def test_open_pr_returns_pr_create_stdout_url_instead_of_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S7: _open_pr returns the gh pr create stdout URL, not None.

    No existing test calls ``_open_pr`` with explicit arguments (its
    parameters are not part of any proven-correct convention this file can
    reuse), so this test drives it through the real ``run_daemon`` flow and
    spies on the **return value** of the real function via a call-through
    side effect, rather than guessing its signature for a direct call.

    An explicit ``report_path`` under ``tmp_path`` is still passed (even
    though this test does not assert on the report) so a Phase-2-and-later
    ``run_daemon`` never falls back to writing its always-on default report
    path into the real working directory during this test.
    """
    _isolate_work_unit_env(monkeypatch)
    report_path = tmp_path / "session-report.json"
    ready_issues = [_make_issue(10, ["agent-ready"])]
    real_open_pr = daemon_mod._open_pr
    captured_returns: list[Any] = []

    def spy_open_pr(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        result = real_open_pr(*args, **kwargs)
        captured_returns.append(result)
        return result

    with (
        patch.object(
            daemon_mod,
            "_run",
            side_effect=_make_run_side_effect(
                ready_issues=ready_issues,
                issue_branch_by_number={10: "baton/issue-10-10"},
            ),
        ),
        patch.object(daemon_mod, "_open_pr", side_effect=spy_open_pr),
        _common_success_patches()(),
        _patch_run_worker("pr_created"),
    ):
        asyncio.run(
            run_daemon(
                _minimal_wf_config(),
                [_repo_cfg(tmp_path)],
                once=True,
                poll_interval_s=0,
                report_path=report_path,
            )
        )

    assert captured_returns, "_open_pr was never called"
    assert captured_returns[0] == "https://github.com/o/r/pull/99", (
        "S7: _open_pr must return proc.stdout.strip() (the gh pr create "
        f"URL) instead of None; got {captured_returns[0]!r}"
    )
