"""symphony/orchestrator.py — Main event loop: poll, dispatch, reconcile."""
from __future__ import annotations

import asyncio
import logging

from .config import WorkflowConfig, load_workflow  # VENDOR-PATCH: relative import for vendoring
from .hooks import run_hook  # VENDOR-PATCH: relative import for vendoring
from .prompt import render_prompt  # VENDOR-PATCH: relative import for vendoring
from .state import IssueState, OrchestratorState  # VENDOR-PATCH: relative import for vendoring
from .tracker import GitHubTracker, Issue, parse_issue_skills  # VENDOR-PATCH: relative import for vendoring
from .worker import Worker  # VENDOR-PATCH: relative import for vendoring
from .workspace import WorkspaceManager  # VENDOR-PATCH: relative import for vendoring

log = logging.getLogger("symphony")


class Orchestrator:
    def __init__(
        self,
        config: WorkflowConfig,
        project_root: str,
        state_path: str,
        workflow_path: str | None = None,
    ):
        self.config = config
        self.project_root = project_root
        self.state_path = state_path
        self.workflow_path = workflow_path
        self.state = OrchestratorState(max_concurrent=config.max_concurrent)
        self.tracker = GitHubTracker(
            labels=config.tracker_labels,
            exclude_labels=config.tracker_exclude_labels,
            assignee=config.tracker_assignee,
        )
        self.workspace = WorkspaceManager(project_root=project_root)
        self.worker = Worker(config)
        self._running_tasks: dict[int, asyncio.Task] = {}
        self._stop_event = asyncio.Event()

    def _should_dispatch(self, issue: Issue) -> bool:
        if self.state.is_claimed(issue.number):
            return False
        if self.state.available_slots <= 0:
            return False
        return True

    async def _dispatch(self, issue: Issue) -> None:
        self.state.add_running(issue.number, IssueState(
            issue_number=issue.number,
            identifier=str(issue.number),
            title=issue.title,
            state=issue.state,
            turn=1,
            max_turns=self.config.max_turns,
        ))

        task = asyncio.create_task(self._run_worker(issue))
        self._running_tasks[issue.number] = task
        task.add_done_callback(lambda t: self._on_worker_done(issue.number, t))

        log.info(f"START #{issue.number} \"{issue.title}\"")

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
                issue_number, attempt=attempt,
                error=str(exc), delay_ms=delay,
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
                    issue_number, attempt=1,
                    delay_ms=1000,
                )
                log.info(f"DONE #{issue_number} — no PR yet, will retry")

        self.state.persist(self.state_path)

    def _backoff_delay(self, attempt: int) -> int:
        delay = min(10000 * (2 ** (attempt - 1)), self.config.max_retry_backoff_ms)
        return delay

    async def _run_worker(self, issue: Issue) -> str:
        # 1. Ensure worktree
        wt = await self.workspace.ensure_worktree(issue.number, title=issue.title)

        # 2. Run after_create hook if new
        if wt.created_now:
            ok = await run_hook(
                "after_create", self.config.hook_after_create,
                cwd=wt.path, timeout_ms=self.config.hook_timeout_ms,
            )
            if not ok:
                raise RuntimeError("after_create hook failed")

        # 3. Run before_run hook
        ok = await run_hook(
            "before_run", self.config.hook_before_run,
            cwd=wt.path, timeout_ms=self.config.hook_timeout_ms,
        )
        if not ok:
            raise RuntimeError("before_run hook failed")

        # 4. Parse issue-level skills
        issue_skills = parse_issue_skills(issue.body)

        # 5. Multi-turn loop
        for turn in range(1, self.config.max_turns + 1):
            # Update state
            if issue.number in self.state.running:
                self.state.running[issue.number].turn = turn

            # Render prompt
            if turn == 1:
                prompt = render_prompt(self.config.prompt_template, issue, attempt=None)
            else:
                prompt = (
                    f"Continue working on issue #{issue.number}: {issue.title}. "
                    f"Check what's been done so far and continue if there's more to do. "
                    f"If the work is complete, commit, push, and create a PR."
                )

            log.info(f"RUN  #{issue.number} turn {turn}/{self.config.max_turns}")

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
                    "after_run", self.config.hook_after_run,
                    cwd=wt.path, timeout_ms=self.config.hook_timeout_ms,
                )
                raise RuntimeError(result.error or "Claude turn failed")

            # Check issue state
            try:
                current_state = await self.tracker.fetch_issue_state(issue.number)
            except Exception:
                break

            if current_state != "open":
                log.info(f"CLOSE #{issue.number} — issue is now {current_state}")
                break

        # Run after_run hook
        await run_hook(
            "after_run", self.config.hook_after_run,
            cwd=wt.path, timeout_ms=self.config.hook_timeout_ms,
        )

        # Check if a PR was created — signals work is complete
        pr_exists = await self.tracker.check_pr_exists(issue.number)
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
                    num, attempt=entry.attempt + 1,
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
                    num, attempt=entry.attempt + 1,
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
                    l.lower() for l in (self.config.tracker_exclude_labels or [])
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
            f"{self.state.running_count}/{self.state.max_concurrent} slots used)"
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
            f"Baton starting — polling every {self.config.poll_interval_ms}ms, "
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
            await asyncio.gather(*self._running_tasks.values(), return_exceptions=True)

        log.info("Baton stopped")

    def stop(self) -> None:
        self._stop_event.set()
