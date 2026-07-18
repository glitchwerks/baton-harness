"""symphony/orchestrator.py — Main event loop: poll, dispatch, reconcile."""

from __future__ import annotations

import asyncio

# VENDOR-PATCH VP-2: needed for exclude_labels re-check label parse
import json
import logging

from .config import (
    WorkflowConfig,
    load_workflow,
)  # VENDOR-PATCH: relative import for vendoring
from .hooks import run_hook  # VENDOR-PATCH: relative import for vendoring
from .prompt import (
    render_prompt,
)  # VENDOR-PATCH: relative import for vendoring
from .state import (
    IssueState,
    OrchestratorState,
)  # VENDOR-PATCH: relative import for vendoring

# VENDOR-PATCH: relative import for vendoring; run_gh added for VP-2
# exclude_labels re-check
from .tracker import (
    GitHubTracker,
    Issue,
    parse_issue_skills,
    run_gh,
)
from .worker import Worker  # VENDOR-PATCH: relative import for vendoring

# VENDOR-PATCH: relative import for vendoring; run_cmd added for VP-5
# worktree-clean gate
from .workspace import (
    WorkspaceManager,
    run_cmd,
)

log = logging.getLogger("symphony")


class Orchestrator:
    """Main event loop: poll GitHub issues, dispatch workers, reconcile.

    Owns the poll → dispatch → reconcile → retry cycle (`_tick`) and the
    per-issue multi-turn worker loop (`_run_worker`). State is persisted
    to disk on every tick so a restart resumes in-flight and queued work.

    Attributes:
        config: The active workflow configuration (hot-reloadable via
            `workflow_path`).
        project_root: Filesystem root the git worktrees are created under.
        state_path: Path `OrchestratorState` is loaded from and persisted to.
        workflow_path: Optional path to re-read `config` from on every tick;
            `None` disables hot reload.
        state: In-memory `OrchestratorState` tracking running/retrying/
            claimed issues.
        tracker: `GitHubTracker` used to poll issues and check PR state.
        workspace: `WorkspaceManager` that creates/cleans up git worktrees.
        worker: `Worker` that runs a single Claude turn.
        progress_cb: Optional `callable(issue_number, turn)` invoked after
            each turn starts; injected by the daemon for progress reporting.
    """

    def __init__(
        self,
        config: WorkflowConfig,
        project_root: str,
        state_path: str,
        workflow_path: str | None = None,
    ) -> None:
        """Initialize the orchestrator and restore persisted state.

        Args:
            config: The workflow configuration to run with.
            project_root: Filesystem root the git worktrees are created
                under.
            state_path: Path to load/persist `OrchestratorState` from/to.
            workflow_path: Optional path to hot-reload `config` from on
                every tick. Defaults to `None` (no hot reload).
        """
        self.config = config
        self.project_root = project_root
        self.state_path = state_path
        self.workflow_path = workflow_path
        self.state = OrchestratorState(max_concurrent=config.max_concurrent)
        self.state.load(
            state_path
        )  # VENDOR-PATCH VP-6: transparent restore on startup
        self.tracker = GitHubTracker(
            labels=config.tracker_labels,
            exclude_labels=config.tracker_exclude_labels,
            assignee=config.tracker_assignee,
        )
        self.workspace = WorkspaceManager(project_root=project_root)
        self.worker = Worker(config)
        self._running_tasks: dict[int, asyncio.Task] = {}
        self._stop_event = asyncio.Event()
        # VENDOR-PATCH VP-3: per-turn progress callback (issue #33)
        # Optional callable(issue_number, turn) injected by the daemon.
        # Attribute injection — zero changes to any existing method signature.
        self.progress_cb = (
            None  # VENDOR-PATCH VP-3: per-turn progress callback (issue #33)
        )

    def _should_dispatch(self, issue: Issue) -> bool:
        if self.state.is_claimed(issue.number):
            return False
        if self.state.available_slots <= 0:
            return False
        return True

    async def _dispatch(self, issue: Issue) -> None:
        self.state.add_running(
            issue.number,
            IssueState(
                issue_number=issue.number,
                identifier=str(issue.number),
                title=issue.title,
                state=issue.state,
                turn=1,
                max_turns=self.config.max_turns,
            ),
        )

        task = asyncio.create_task(self._run_worker(issue))
        self._running_tasks[issue.number] = task
        task.add_done_callback(lambda t: self._on_worker_done(issue.number, t))

        log.info(f'START #{issue.number} "{issue.title}"')

    def _on_worker_done(self, issue_number: int, task: asyncio.Task) -> None:
        self._running_tasks.pop(issue_number, None)
        entry = self.state.remove_running(issue_number)

        try:
            exc = task.exception()
        except asyncio.CancelledError:
            exc = None

        if exc:
            log.error(f"FAIL #{issue_number}: {exc}")
            attempt = entry.turn if entry else 1
            delay = self._backoff_delay(attempt)
            self.state.schedule_retry(
                issue_number,
                attempt=attempt,
                error=str(exc),
                delay_ms=delay,
            )
        else:
            result = task.result()
            self.state.completed.add(issue_number)
            if result == "pr_created":
                # PR exists — fully done, release claim
                self.state.release(issue_number)
                log.info(f"DONE #{issue_number} — PR created, moving on")
            else:
                # No PR yet — schedule continuation retry
                self.state.schedule_retry(
                    issue_number,
                    attempt=1,
                    delay_ms=1000,
                )
                log.info(f"DONE #{issue_number} — no PR yet, will retry")

        self.state.persist(self.state_path)

    def _backoff_delay(self, attempt: int) -> int:
        delay = min(
            10000 * (2 ** (attempt - 1)), self.config.max_retry_backoff_ms
        )
        return delay

    async def _run_worker(self, issue: Issue) -> str:
        # 1. Ensure worktree
        wt = await self.workspace.ensure_worktree(
            issue.number, title=issue.title
        )

        # 2. Run after_create hook if new
        if wt.created_now:
            ok = await run_hook(
                "after_create",
                self.config.hook_after_create,
                cwd=wt.path,
                timeout_ms=self.config.hook_timeout_ms,
            )
            if not ok:
                raise RuntimeError("after_create hook failed")

        # 3. Run before_run hook
        ok = await run_hook(
            "before_run",
            self.config.hook_before_run,
            cwd=wt.path,
            timeout_ms=self.config.hook_timeout_ms,
        )
        if not ok:
            raise RuntimeError("before_run hook failed")

        # 4. Parse issue-level skills
        issue_skills = parse_issue_skills(issue.body)

        # 5. Multi-turn loop
        # VENDOR-PATCH VP-5: latch a confirmed mid-loop PR observation so a
        # flaky post-loop re-probe cannot downgrade it to "no_pr".
        pr_detected = False
        for turn in range(1, self.config.max_turns + 1):
            # Update state
            # VENDOR-PATCH VP-2: guard already present — confirmed in
            # vendored source.  This ``if issue.number in
            # self.state.running:`` check prevents a stale state.json
            # from causing a KeyError on the turn mutation (CONCERN-4).
            # No additional guard is needed; the existing check
            # satisfies the VP-2 running[N] guard requirement.
            if issue.number in self.state.running:
                self.state.running[issue.number].turn = turn

            # Render prompt
            if turn == 1:
                prompt = render_prompt(
                    self.config.prompt_template, issue, attempt=None
                )
            else:
                prompt = (
                    f"Continue working on issue #{issue.number}: "
                    f"{issue.title}. "
                    f"Check what's been done so far and continue if "
                    f"there's more to do. "
                    f"If the work is complete, commit, push, and "
                    f"create a PR."
                )

            log.info(
                f"RUN  #{issue.number} turn {turn}/{self.config.max_turns}"
            )

            # VENDOR-PATCH VP-3: per-turn progress callback (issue #33)
            # Best-effort: a callback exception is logged and swallowed so
            # it can never crash the worker run.
            if (
                self.progress_cb is not None
            ):  # VENDOR-PATCH VP-3: per-turn progress callback (issue #33)
                try:
                    self.progress_cb(issue.number, turn)
                except Exception:
                    log.exception(
                        "progress_cb raised for issue #%s turn %s — swallowed",
                        issue.number,
                        turn,
                    )

            # Run claude
            result = await self.worker.run_turn(
                prompt=prompt,
                cwd=wt.path,
                issue_skills=issue_skills,
                timeout_ms=self.config.max_retry_backoff_ms,
            )

            if not result.success:
                log.error(f"FAIL #{issue.number} turn {turn}: {result.error}")
                # Run after_run hook (best effort)
                await run_hook(
                    "after_run",
                    self.config.hook_after_run,
                    cwd=wt.path,
                    timeout_ms=self.config.hook_timeout_ms,
                )
                raise RuntimeError(result.error or "Claude turn failed")

            # Check issue state
            try:
                current_state = await self.tracker.fetch_issue_state(
                    issue.number
                )
            except Exception:
                break

            if current_state != "open":
                log.info(
                    f"CLOSE #{issue.number} — issue is now {current_state}"
                )
                break

            # VENDOR-PATCH VP-2: re-check exclude_labels after
            # fetch_issue_state. If any exclude label (e.g. "blocked")
            # is now present, terminate the turn loop immediately —
            # making a mid-run block terminal and closing the #23 root
            # cause (external Baton never re-checked between turns).
            if self.tracker.exclude_labels:
                try:
                    label_output = await run_gh(
                        [
                            "issue",
                            "view",
                            str(issue.number),
                            "--json",
                            "labels",
                        ]
                    )
                    label_data = json.loads(label_output)
                    current_labels = {
                        lbl["name"].lower()
                        for lbl in label_data.get("labels", [])
                    }
                    if current_labels & set(self.tracker.exclude_labels):
                        log.info(
                            f"BLOCK #{issue.number} — exclude label detected"
                            f" mid-turn; terminating loop"
                        )
                        break
                except Exception as _exc:  # best-effort; don't crash the run
                    log.debug(
                        f"VP-2 exclude_labels re-check failed for"
                        f" #{issue.number}: {_exc}"
                    )

            # VENDOR-PATCH VP-5: mid-loop PR-exists early-exit (#137)
            # If a PR already exists after this turn, stop burning turns.
            # Ordering: AFTER the closed-issue break and VP-2 blocked-label
            # break, so those higher-priority terminations still fire first.
            # Best-effort: a check_pr_exists exception is swallowed (matching
            # the VP-2/VP-3 swallow-and-continue pattern) so a transient gh
            # failure never crashes the run.
            try:
                if await self.tracker.check_pr_exists(issue.number):
                    # VENDOR-PATCH VP-5: only exit early when the worktree is
                    # clean AND fully pushed. A dirty tree is scored by
                    # after_run._classify as UNCOMMITTED_CHANGES before it
                    # checks PR_OPENED (→ parked, PR stranded); a clean tree
                    # that is ahead of its remote would strand the unpushed
                    # commit and let CI run on a stale PR. In either case keep
                    # looping so a later turn commits/pushes — the PR alone is
                    # not a "done" signal.
                    status = await run_cmd(
                        [
                            "git",
                            "status",
                            "--porcelain",
                            "--untracked-files=no",
                        ],
                        cwd=wt.path,
                    )
                    if status.strip():
                        log.info(
                            f"PR_DIRTY #{issue.number} — PR exists but"
                            f" worktree has uncommitted changes at"
                            f" turn {turn}; continuing"
                        )
                    else:
                        ahead = await run_cmd(
                            [
                                "git",
                                "rev-list",
                                "--count",
                                "@{upstream}..HEAD",
                            ],
                            cwd=wt.path,
                        )
                        if ahead.strip() in ("", "0"):
                            log.info(
                                f"PR_EARLY #{issue.number} — PR detected"
                                f" mid-loop at turn {turn} with clean, pushed"
                                f" worktree; exiting"
                            )
                            pr_detected = (
                                True  # VENDOR-PATCH VP-5: latch before break
                            )
                            break
                        log.info(
                            f"PR_UNPUSHED #{issue.number} — PR exists but"
                            f" {ahead.strip()} local commit(s) are unpushed at"
                            f" turn {turn}; continuing"
                        )
            except Exception as _exc:  # best-effort; don't crash the run
                log.debug(
                    f"VP-5 worktree-clean check failed for"
                    f" #{issue.number}: {_exc}"
                )

        # Run after_run hook
        await run_hook(
            "after_run",
            self.config.hook_after_run,
            cwd=wt.path,
            timeout_ms=self.config.hook_timeout_ms,
        )

        # Check if a PR was created — signals work is complete.
        # VENDOR-PATCH VP-5: a PR confirmed mid-loop is authoritative — return
        # immediately and do NOT re-probe, so a transient post-loop gh failure
        # cannot downgrade a real PR to "no_pr" (the daemon parks on "no_pr",
        # which would strand the PR). This also removes the redundant second
        # check_pr_exists call on the common success path.
        if pr_detected:
            log.info(f"PR_READY #{issue.number} — PR found, releasing claim")
            return "pr_created"
        # Fallback: no mid-loop detection (closed/blocked break, or the
        # mid-loop check raised every turn). Probe once, guarded — a transient
        # failure here means "could not determine" → "no_pr".
        try:
            pr_exists = await self.tracker.check_pr_exists(issue.number)
        except Exception as _exc:  # best-effort; don't crash the run
            log.debug(
                f"VP-5 post-loop check_pr_exists failed for"
                f" #{issue.number}: {_exc}"
            )
            pr_exists = False
        if pr_exists:
            log.info(f"PR_READY #{issue.number} — PR found, releasing claim")
            return "pr_created"
        return "no_pr"

    async def _reconcile(self) -> None:
        """Check running issues against tracker state."""
        running_numbers = list(self.state.running.keys())
        if not running_numbers:
            return

        try:
            states = await self.tracker.fetch_issue_states(running_numbers)
        except Exception as e:
            log.debug(f"reconcile: state refresh failed, keeping workers: {e}")
            return

        for num, current_state in states.items():
            if current_state == "closed":
                log.info(f"RECONCILE #{num} — closed, stopping worker")
                task = self._running_tasks.get(num)
                if task and not task.done():
                    task.cancel()
                self.state.release(num)
                try:
                    await self.workspace.cleanup_worktree(num)
                except Exception as e:
                    log.error(f"RECONCILE #{num} cleanup failed: {e}")

    async def _handle_retries(self) -> None:
        """Process due retry entries."""
        for entry in self.state.due_retries():
            num = entry.issue_number
            self.state.retry_queue.pop(num, None)

            try:
                candidates = await self.tracker.fetch_candidates()
            except Exception:
                self.state.schedule_retry(
                    num,
                    attempt=entry.attempt + 1,
                    error="retry poll failed",
                    delay_ms=self._backoff_delay(entry.attempt + 1),
                )
                continue

            issue = next((i for i in candidates if i.number == num), None)
            if issue is None:
                self.state.release(num)
                log.info(f"RELEASE #{num} — no longer a candidate")
                continue

            if self.state.available_slots <= 0:
                self.state.schedule_retry(
                    num,
                    attempt=entry.attempt + 1,
                    error="no available slots",
                    delay_ms=self._backoff_delay(entry.attempt + 1),
                )
                continue

            await self._dispatch(issue)

    async def _tick(self) -> None:
        """One poll-dispatch-reconcile cycle."""
        # 1. Reconcile
        await self._reconcile()

        # 2. Handle retries
        await self._handle_retries()

        # 3. Reload config if needed
        if self.workflow_path:
            try:
                self.config = load_workflow(self.workflow_path)
                self.state.max_concurrent = self.config.max_concurrent
                self.tracker.labels = self.config.tracker_labels
                self.tracker.exclude_labels = [
                    label.lower()
                    for label in (self.config.tracker_exclude_labels or [])
                ]
                self.tracker.assignee = self.config.tracker_assignee
            except Exception as e:
                log.error(f"RELOAD failed, keeping last config: {e}")

        # 4. Fetch candidates
        try:
            candidates = await self.tracker.fetch_candidates()
        except Exception as e:
            log.error(f"POLL failed: {e}")
            self.state.persist(self.state_path)
            return

        eligible = [i for i in candidates if self._should_dispatch(i)]
        log.info(
            f"POLL  Found {len(candidates)} issues "
            f"({len(eligible)} eligible, "
            f"{self.state.running_count}/{self.state.max_concurrent}"
            f" slots used)"
        )

        # 5. Dispatch
        for issue in eligible:
            if self.state.available_slots <= 0:
                break
            await self._dispatch(issue)

        self.state.persist(self.state_path)

    async def run(self) -> None:
        """Main loop."""
        log.info(
            f"Baton starting — polling every"
            f" {self.config.poll_interval_ms}ms, "
            f"max {self.config.max_concurrent} concurrent"
        )

        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception as e:
                log.error(f"Tick error: {e}")

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.config.poll_interval_ms / 1000,
                )
                break
            except asyncio.TimeoutError:
                pass

        # Cancel all running workers
        for task in self._running_tasks.values():
            task.cancel()
        if self._running_tasks:
            await asyncio.gather(
                *self._running_tasks.values(), return_exceptions=True
            )

        log.info("Baton stopped")

    def stop(self) -> None:
        """Signal `run` to exit after the current tick completes."""
        self._stop_event.set()
