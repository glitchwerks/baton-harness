# Implementation Plan — Issue #33: Baton-worker liveness / health detection

**Issue:** glitchwerks/baton-harness#33 — Baton-worker liveness/health detection (Failure-mode hardening).
**Status:** Planning only. No implementation performed.
**Repo state:** `main` @ `c723dfe`.
**Date:** 2026-06-16
**Scope:** FULL fix (user-confirmed). The four ACs below are addressed in full; this plan does not re-litigate scope.
**Applied lens:** `simplicity-first` — smallest vendored touch, reuse the existing `LivenessState`/`alert()`/runlog seams rather than building new infrastructure.

> Citation discipline: every load-bearing claim cites a verified `file:line` anchor (re-read against source on 2026-06-16 in this checkout), the issue body, or the dispatch audit. Where a claim could not be confirmed it is flagged `unverified:` and surfaced in Open Questions, not asserted.

---

## Review revisions (project-reviewer, 2026-06-16)

Architectural review identified two blocking design flaws and three concerns before implementation began. All are resolved in-plan — no code was written. Provenance: project-reviewer findings folded in by doc-writer on 2026-06-16 per `standards/cite-sources.md` discipline; changes are binding on P2 and P1 implementers.

**What changed:**

- **IS-1 (BLOCKING-2):** Pre-first-turn false-fire trap. Resolution moved: `last_progress_at` is now initialized inside `progress_cb` on its *first call* (turn-loop entry), NOT before `await _run_worker`. Pre-turn setup (worktree creation, hooks) is covered by the wall-clock backstop only. VP-3 brief and daemon-touch spec updated accordingly.
- **IS-2 (BLOCKING-1):** Torn-read of `phase` + `last_progress_at`. Resolution changed: there is **no standalone `phase` field**. Worker-activeness is instead bound to the existing `in_progress_since` via a new `_worker_active: bool` flag written inside `mark_in_progress(worker_active=True)` and cleared inside the existing `clear()` — same method call, no torn pair. The heartbeat snapshot reads `worker_active_snap` *after* `since_snap`; because the writer always sets `in_progress_since` before `_worker_active`, the existing `if since_snap is not None` guard already gates the progress predicate — no lock required, write-ordering is explicit and binding.
- **P2 daemon-touch scope (CONCERN):** Now enumerates ALL `mark_in_progress` call sites: `daemon.py:L836` (`ci_gate_reentry`, `worker_active=False`) and `daemon.py:L965` (fresh dispatch, `worker_active=True`), plus the `_run_worker` entry. The `ci_gate_reentry` path explicitly sets `worker_active=False`, structurally exempting it from the progress predicate.
- **OQ-2 resolved (CONCERN):** `BH_WORKER_PROGRESS_STALL_S` default promoted from `unverified: 1800.0 s` to `verified: 1800.0 s` (6× the 300 s per-turn timeout; `config.py:L31` `max_retry_backoff_ms=300_000`, `orchestrator.py:L155` `timeout_ms`). Moved from Open Questions to a committed decision in the P2 spec. Code comment in `obs_config.py` must cross-reference the per-turn timeout.
- **P1 async seam (CONCERN):** `workspace.cleanup_worktree` is `async def` (`workspace.py:L100`). `scan_orphan_worktrees` and its reclaim path are now specified as `async`, called with `await` from `daemon.py`'s async context.
- **P1 GC-vs-redispatch-set (CONCERN):** Documented that `reclaim` must never touch worktrees for issues in the redispatch set; implementation is already correct via the label predicate but requires an explicit comment.
- **NIT-1 (P3):** Wall-clock `heartbeat_stall_s=7200` backstop is meaningful only in NON_WORKER phase and as a catch-all if the progress signal itself breaks; noted in §11 doc spec.
- **NIT-2 (P3):** VP-3 re-vendor checklist step 3 wording confirmed present; P3 verifies on merge.

---

## Acceptance criteria (issue #33)

1. **AC1** — Liveness check detects when Baton/worker has stopped *making progress* within a bounded window.
2. **AC2** — Orphaned worktrees/branches (created but never reconciled to a terminal label) are detected.
3. **AC3** — A hung `claude`/worker subprocess is *distinguishable* from an actively-progressing one.
4. **AC4** — Vendoring relationship documented. **Already MET** (`docs/harness-design.md:L296-352` §11; `VENDORING.md:L11-18`). This plan updates §11 for the new progress-bound semantics (P3).

### Current-state audit (verified file:line)

- **Concurrency:** two OS threads. The asyncio loop `await`s `orch._run_worker(issue_obj)` for the whole run (`daemon.py:L1013`); a separate `threading.Thread(daemon=True)` runs `run_heartbeat_loop` (`daemon.py:L1481-1491`), ticking every 30 s via `stop_event.wait(interval_s)` (`heartbeat.py:L415-430`), deliberately independent so it survives asyncio/CI-gate blockage (`heartbeat.py:L21-27` docstring).
- **AC1 PARTIAL:** stall clock `in_progress_since` is set once per dispatch (`daemon.py:L964-967`) and read in `_heartbeat_tick` (`heartbeat.py:L300-314`), comparing against `obs.heartbeat_stall_s` (default `7200.0`, `obs_config.py:L72`). It measures wall-clock-since-dispatch, never lack-of-turn-progress — never bumped during `_run_worker`.
- **AC2 GAPPED:** `ensure_worktree` (`workspace.py:L72-98`) runs at `_run_worker` step 1 (`orchestrator.py:L104-105`), which the daemon enters at `daemon.py:L1013` *after* `_label_edit(add=["agent-in-progress"])` at `daemon.py:L959-961`. `cleanup_worktree` exists (`workspace.py:L100-117`) but no recovery path calls it. The orphan scan is label-keyed (`recovery.py:L359-362`, Rule 5 redispatch keys on `agent-in-progress`), so a worktree whose label was never set or was torn is invisible. No `git worktree list` sweep exists anywhere.
- **AC3 GAPPED:** the vendored turn loop (`orchestrator.py:L128-208`) has no per-turn liveness write. `worker.run_turn` already enforces a hard subprocess timeout (`worker.py:L107-120`, `asyncio.wait_for` + `proc.kill()`), but a worker that loops *across* turns (each turn under timeout, zero forward progress) is indistinguishable from a busy one until the 7200 s wall clock fires.

---

## THE INVARIANT SWEEP (read before any phase)

The current stall mechanism holds these invariants. The new per-turn-progress signal is checked against each. Resolutions here are binding on P2.

### IS-1 — CI-gate / non-worker-phase FALSE-FIRE trap (CRITICAL)

**Invariant today:** the heartbeat keeps ticking during multi-minute non-worker phases — the synchronous CI gate (`merge.py` `time.sleep`, called out in `heartbeat.py:L24-26` and `daemon.py:L1474-1476` as the reason the thread is a real OS thread), PR creation, recovery, and draft-PR open. During these phases **zero worker turns happen**, by design.

**The trap:** if "stall" is redefined as "no per-turn progress in window," every legitimate CI-gate wait FALSE-fires a critical stall alert.

**Resolution (binding):** introduce a `_worker_active: bool` flag on `LivenessState`, bound to the existing `in_progress_since` field — not a standalone `phase` field. Worker-activeness is written by `mark_in_progress(worker_active=True)` and cleared inside the existing `clear()`, in the same method body, so there is never a torn `phase`+`last_progress_at` pair (see IS-2).

The two logical phases are:

- `WORKER_ACTIVE` (`_worker_active=True`) — entered when `_run_worker` begins and real turns are executing. **`last_progress_at` is initialized on the `progress_cb` FIRST CALL** (turn-loop entry), not before `await orch._run_worker`. This means pre-turn setup (worktree creation, `after_create`/`before_run` hooks at `orchestrator.py:L104-105`) is covered by the wall-clock backstop only — the progress window does not open until the first turn fires. In this phase the *progress-bound* stall predicate applies: stall = no per-turn progress timestamp bump within `BH_WORKER_PROGRESS_STALL_S`.
- `NON_WORKER` (`_worker_active=False`) — the daemon's default state and the state it returns to around the CI gate and all non-worker phases. In this phase **only the existing wall-clock liveness applies** (`heartbeat_stall_s`), exactly as today. The new progress predicate is *not evaluated*.

The progress-bound check is therefore **gated on `_worker_active`**, the CI gate is structurally exempt, and a slow pre-turn hook cannot false-fire. The wall-clock `heartbeat_stall_s` check is retained unchanged as a coarse backstop in both phases (a worker hung for 2 h still fires via the existing path even if the progress signal itself is wedged).

This is the single most important design decision in the plan. P2 must not ship a progress predicate that evaluates in `NON_WORKER` phase.

### IS-2 — Thread-safety of the new progress field

**Invariant today:** `LivenessState` is written only from the asyncio/daemon thread (single writer) and read from the heartbeat thread; the dataclass docstring (`heartbeat.py:L66-71`) asserts GIL-atomic field assignment is sufficient and no lock is needed. `_heartbeat_tick` already snapshots all fields into locals before logic (`heartbeat.py:L299-303`) specifically to avoid torn reads.

**New access pattern:** `last_progress_at` is written per turn from the asyncio-loop thread (inside `_run_worker`, via the injected `progress_cb` — see IS-3) and read each tick from the heartbeat thread. Still single-writer, so GIL-atomic-assignment holds.

**Torn-read risk — eliminated by write-ordering (not a lock):** the prior plan draft proposed a standalone `phase` field alongside `last_progress_at`, creating a torn-read hazard: the daemon could `clear()` between the heartbeat's reads of `phase` and `last_progress_at`, yielding `_worker_active=True` with `last_progress_at=None` → `(now - None)` TypeError or false stall.

**Resolution (binding):** there is **no standalone `phase` field**. Instead:

- `mark_in_progress(worker_active: bool = True)` sets `_worker_active` in the **same method body** as `in_progress_since`, so they are always consistent.
- `clear()` clears `_worker_active` in the **same method body** as `in_progress_since`.
- The heartbeat snapshot block (`heartbeat.py:L299-303`) reads `worker_active_snap` AFTER `since_snap`. Because the writer always sets `in_progress_since` before `_worker_active`, the existing `if since_snap is not None` guard already gates the progress predicate. No interleaving can produce `_worker_active=True` with `since_snap=None`.
- **No `threading.Lock` is required.** Write-ordering is the fix; a lock would contradict the established `LivenessState` design (`heartbeat.py:L66-71`) and the `simplicity-first` lens.

Extend the existing snapshot-into-locals discipline in `_heartbeat_tick` to cover `worker_active_snap` and `last_progress_at_snap`. Both are single scalars, single-writer — GIL guarantee is sufficient.

### IS-3 — Vendored-patch boundary (the AC1/AC3 signal source)

**Invariant:** edits to `src/baton_harness/vendor/symphony/orchestrator.py` are vendored-source edits and MUST follow the project's vendoring policy: tracked diff in `patches/`, `# VENDOR-PATCH` markers, a `VENDORING.md` entry, and presence in the re-vendor checklist (`VENDORING.md:L80-123`; `CLAUDE.md § Upstream dependency`).

**Resolution (binding) — smallest possible vendored touch:** the liveness logic lives in **chain code**, not vendored code. The vendored diff is a single optional injected callback:

- Add an **optional** `progress_cb: Callable[[int, int], None] | None = None` parameter to `Orchestrator.__init__` (or set it as an attribute post-construction at `daemon.py:L725-729` — preferred, because attribute injection touches *zero* vendored signature lines and is even smaller). The callback receives `(issue_number, turn)`.
- In the turn loop (`orchestrator.py:L148`, right where `log.info(f"RUN #{issue.number} turn {turn}/...")` already fires once per turn), add **one guarded line**: `if self.progress_cb is not None: self.progress_cb(issue.number, turn)`. Wrapped in try/except so a callback fault never crashes the vendored run.

This is patch **VP-3**. The callback body (which stamps `LivenessState.last_progress_at`) lives entirely in `chain/daemon.py` / `chain/heartbeat.py`. The vendored diff is ~3 lines + the attribute. This is the minimal seam that satisfies AC1 (progress detection) and AC3 (per-turn signal distinguishes hung-across-turns from busy).

**Why the turn-loop site and not `worker.run_turn`:** progress = "a turn boundary was reached," which is exactly the `for turn in range(...)` loop in `orchestrator.py`. `run_turn` already has its own intra-turn timeout (`worker.py:L107-120`); the gap AC3 names is *across* turns, so the loop is the correct seam.

### IS-4 — Stall semantics change → alert/escalation routing

**Invariant today:** stall alerts route through `alert(owner, repo, issue, msg, severity="critical", kind="debug", runlog=runlog)` (`heartbeat.py:L317-331`), are **debounced once per episode** via `state._stall_alerted` (`heartbeat.py:L305, L335-339`), and emit a `{"event": "stall"}` runlog record (`heartbeat.py:L343-355`). `alert()` posts a durable GitHub comment first, Slack best-effort (`escalation.py:L70-120`).

**Resolution (binding):** the new progress-bound stall **reuses the same `alert()` call and the same runlog `stall` event** — it does not add a parallel alerting path (this also keeps it aligned with #34's escalation work). To avoid double-firing with the existing wall-clock stall:

- The progress-bound stall and the wall-clock stall share the **single** `_stall_alerted` debounce latch. Whichever fires first latches; the other is suppressed for the episode. `mark_in_progress` / `clear` already reset the latch (`heartbeat.py:L111, L124`) at episode boundaries, so a new dispatch re-arms both.
- The runlog `stall` event gains a `detail` distinguishing the trigger (e.g. `"progress-stall after Ns (no turn advance)"` vs the existing `"stall detected after Ns"`) so the two are diagnosable downstream, but they remain the *same* event type and severity.

### IS-5 — AC2 worktree-GC safety (never prune a live worktree)

**Invariant to preserve:** an in-flight worker's worktree must never be pruned, and a worktree with uncommitted/unpushed work must never be deleted on suspicion alone.

**Resolution (binding) — conservative, idempotent, detect-don't-destroy by default:**

- The GC sweep runs `git worktree list --porcelain` (read-only) at the **start of recovery**, before dispatch, in the existing recovery pass (`recovery.py`), where the active-issue set and label state are already in hand.
- **Liveness predicate for a worktree** — a worktree is **live** (never touched) if ANY hold:
  - its issue number is in the current `running` / membership set being processed this cycle, OR
  - the issue carries `agent-in-progress` (cross-ref the same label Rule 5 uses, `recovery.py:L336, L359-362`), OR
  - the worktree dir contains uncommitted changes (`git -C <wt> status --porcelain` non-empty) OR unpushed commits (`git -C <wt> log @{u}.. --oneline`, or no upstream → treat as unpushed → live).
- A worktree is an **orphan candidate** only if its issue maps to a **terminal** state (done / merged) AND none of the live predicates hold.
- **Default action is DETECT, not destroy** (matches AC2 wording "are *detected*"): an orphan candidate is reported via `alert(..., severity="warn", kind="debug")` and a runlog `{"event": "orphan_worktree"}` record. Actual reclamation via the existing `cleanup_worktree` (`workspace.py:L100-117`) is **opt-in** behind `BH_WORKTREE_GC=reclaim` (default `detect`). This keeps P1 zero-risk to ship and defers the destructive path behind an explicit flag. (See Open Question OQ-1.)
- The sweep is idempotent: re-running it on the same state produces the same detection set and never acts twice on a reclaimed worktree (the dir is gone after reclaim).

---

## Phase breakdown

| Phase | Independent? | Vendored patch? | Summary |
|---|---|---|---|
| **P1** — AC2 worktree-GC (detect-first) | Independent (ships first, lowest risk) | No | `git worktree list` sweep in recovery; orphan detection; opt-in reclaim |
| **P2** — AC1/AC3 per-turn liveness | Independent of P1; internally the vendored hook + state field + heartbeat consumption are one unit | **Yes (VP-3)** | Phase + progress-timestamp on `LivenessState`; minimal vendored callback; progress-bound stall gated on `WORKER_ACTIVE` |
| **P3** — Docs §11 + close-out | Depends on P1 **and** P2 (documents both) | No | Update `harness-design.md` §11, `obs_config.py` docstring, `VENDORING.md`, README |

P1 and P2 are **independent and separately committable** — either can merge first. P3 depends on both (it documents the shipped behavior of each). Each phase is one PR.

This project runs **test-first**: `test-implementer` writes frozen tests against the seams named below, then `code-writer` implements until green. Each phase lists its testable seams explicitly.

---

## Phase P1 — AC2 worktree orphan-GC (detect-first)

**Goal:** detect worktrees created but never reconciled to a terminal label; optionally reclaim behind a flag. No vendored touch. Ships first.

### Files touched
- `src/baton_harness/chain/recovery.py` — add an **`async`** `scan_orphan_worktrees(...)` function and call it in the recovery pass (read `git worktree list --porcelain`, apply the IS-5 liveness predicate, return orphan-candidate set). Must be `async` because `workspace.cleanup_worktree` is `async def` (`workspace.py:L100`); called with `await` from `daemon.py`'s async context.
- `src/baton_harness/chain/obs_config.py` — add `worktree_gc: Literal["detect","reclaim"]` field + `BH_WORKTREE_GC` env parse (default `"detect"`), following the existing guarded-parse pattern (`obs_config.py:L160-203`); update module docstring env table.
- `src/baton_harness/chain/daemon.py` — wire the sweep result into the recovery path with `await`; on orphan candidates emit `alert(severity="warn")` + runlog `orphan_worktree` event; when `worktree_gc == "reclaim"`, call `await orch.workspace.cleanup_worktree(n)` (`workspace.py:L100-117`) for confirmed orphans only.
- `tests/chain/test_recovery.py` (or new `tests/chain/test_worktree_gc.py`) — see seams.
- `tests/chain/test_obs_config.py` — `BH_WORKTREE_GC` parse + default.

### Contract / behavior
- Sweep is **read-only by default**; reclaim is opt-in via `BH_WORKTREE_GC=reclaim`.
- A worktree is never reclaimed if live per IS-5 (active issue, `agent-in-progress`, or dirty/unpushed tree).
- **GC vs redispatch set:** `reclaim` must never reap worktrees for issues currently in the redispatch set — those issues carry `agent-in-progress` and are already protected by the IS-5 label predicate. This interaction is already correct structurally; the implementation must add an explicit comment at the label-predicate check to document this guarantee.
- Detection emits a `warn` alert + `orphan_worktree` runlog event; reclaim additionally emits `worktree_reclaimed`.
- Sweep never raises into the daemon loop (guarded, consistent with `daemon.py:L1506` FIX-2 pattern).

### Testable seams
- `git worktree list --porcelain` invocation → patch the subprocess seam (mirror how recovery/escalation patch `_run`, `escalation.py:L38-56`). Feed canned porcelain output: mix of live (active-issue, dirty, unpushed) and orphan (terminal-label, clean, pushed) worktrees; assert only true orphans are flagged.
- `git -C <wt> status --porcelain` and `git -C <wt> log @{u}..` seams → patch to simulate dirty / unpushed / clean; assert dirty and unpushed are classified live.
- `cleanup_worktree` → patch on the `WorkspaceManager`; assert it is called **only** when `worktree_gc == "reclaim"` AND the worktree is a confirmed orphan; assert it is **never** called in `detect` mode.
- `alert` and `runlog.emit` → patch; assert `orphan_worktree` event shape and `severity="warn"`.
- `obs_config` → assert `BH_WORKTREE_GC` unset → `"detect"`; `=reclaim` → `"reclaim"`; garbage → warns and falls back to `"detect"`.

### CI gate
`ruff check .` · `ruff format --check .` · `mypy src` · `pytest -q` (full suite). All green.

---

## Phase P2 — AC1/AC3 per-turn liveness via minimal vendored hook

**Goal:** distinguish a hung worker (no turn advance within a bounded window) from a busy one, **only** during the worker-active phase. CI-gate-safe per IS-1.

### Files touched
- `src/baton_harness/vendor/symphony/orchestrator.py` — **VP-3** (see VENDOR-PATCH checklist below): add optional `progress_cb` attribute + one guarded call at the turn-loop head (`orchestrator.py:L148`).
- `src/baton_harness/chain/heartbeat.py` — extend `LivenessState` with `_worker_active: bool` (default `False`) and `last_progress_at: datetime | None` (default `None`); update `mark_in_progress(worker_active: bool = True)` to set `_worker_active` in the same method body as `in_progress_since`; update `clear()` to clear `_worker_active` in the same body as `in_progress_since`; add `note_progress(now)` mutator; extend `_heartbeat_tick` snapshot-into-locals (`heartbeat.py:L299-303`) to add `worker_active_snap` (read AFTER `since_snap`) and `last_progress_at_snap`; add the **`_worker_active`-gated** progress-stall predicate alongside the existing wall-clock one, sharing the `_stall_alerted` latch (IS-4). No standalone `phase` enum or `mark_worker_active()`/`mark_non_worker()` calls — phase is derived from `_worker_active`.
- `src/baton_harness/chain/obs_config.py` — add `worker_progress_stall_s: float` + `BH_WORKER_PROGRESS_STALL_S` env parse (verified default `1800.0 s` — 6× the 300 s per-turn timeout at `config.py:L31`; see resolved OQ-2), guarded-parse pattern; docstring env table must include a comment cross-referencing `max_retry_backoff_ms` / `config.py:L31`.
- `src/baton_harness/chain/daemon.py` — **all `mark_in_progress` call sites must be updated**:
  - `daemon.py:L836` (`ci_gate_reentry` path): call `mark_in_progress(worker_active=False)` — this path waits on a CI gate with no worker turns; the progress predicate must NOT fire here.
  - `daemon.py:L965` (fresh dispatch, before `_run_worker`): call `mark_in_progress(worker_active=True)`.
  - Inside `_run_worker` entry (first `progress_cb` call, turn-loop top): `last_progress_at` is initialized here, not before `await orch._run_worker` — per IS-1 resolution.
  - Set `orch.progress_cb` to a closure that calls `liveness_state.note_progress(now)` after constructing `orch` (`daemon.py:L725-729`); the callback initializes `last_progress_at` on first call.
  - The existing `liveness_state.clear()` sites (e.g. `daemon.py:L1017-1018`) already clear `_worker_active` via the updated `clear()` — no additional phase-reset calls needed.
- `patches/VP-3-progress-callback.diff` — new tracked patch.
- `src/baton_harness/vendor/symphony/VENDORING.md` — VP-3 entry + re-vendor-checklist line.
- `tests/chain/test_heartbeat.py` — see seams.
- `tests/chain/test_daemon.py` — `_worker_active` transitions around `_run_worker` and `ci_gate_reentry`.

### Contract / behavior
- `_worker_active=True`: progress-stall fires when `(now - last_progress_at) > worker_progress_stall_s` (strictly greater, matching `heartbeat.py:L314`), debounced via the shared `_stall_alerted` latch. Default `worker_progress_stall_s = 1800.0 s` (verified: 6× the 300 s per-turn timeout; `config.py:L31`).
- `_worker_active=False` (NON_WORKER): progress predicate is **not evaluated**; only the existing wall-clock `heartbeat_stall_s` applies. **CI-gate wait never false-fires** (IS-1).
- `ci_gate_reentry` path (`daemon.py:L836`) calls `mark_in_progress(worker_active=False)` — structurally NON_WORKER; no progress stall can fire during a CI gate wait.
- `progress_cb` is best-effort: a callback exception is logged and swallowed inside the vendored try/except; the worker run is never crashed by liveness instrumentation.
- Wall-clock `heartbeat_stall_s` backstop retained unchanged in both phases.

### Testable seams
- `LivenessState.note_progress` / `mark_in_progress(worker_active=True|False)` / `clear` — direct unit tests: assert `_worker_active` and `in_progress_since` set/cleared in the same call; assert `last_progress_at` updated by `note_progress`; assert latch reset semantics.
- `mark_in_progress(worker_active=False)` — assert `_worker_active=False` while `in_progress_since` is set (the NON_WORKER-with-stall-clock state used by `ci_gate_reentry`).
- `_heartbeat_tick` with injected `now` (already injectable, `heartbeat.py:L221`): assert with `_worker_active=True` and stale `last_progress_at` → progress-stall alert fires once (debounced); assert with `_worker_active=False` and the *same* stale timestamp → **no** progress alert (the IS-1 regression test — this is the highest-value test in the plan).
- First-call initialization: assert `last_progress_at` remains `None` until `progress_cb` is first called (no timestamp before turn-loop entry).
- Shared-latch test: wall-clock stall fires first → progress stall suppressed (and vice versa); `mark_in_progress(worker_active=True)` re-arms both.
- `daemon.py ci_gate_reentry` path: assert `mark_in_progress(worker_active=False)` is called at `L836`, not `worker_active=True`.
- `progress_cb` fault injection: callback raises → `_run_worker` turn loop still completes (assert no propagation out of the vendored guard).
- `alert` / `runlog.emit` patched: assert progress-stall `detail` string distinguishes it from wall-clock stall (IS-4).
- `obs_config`: `BH_WORKER_PROGRESS_STALL_S` parse + default `1800.0` + garbage-fallback.

### VENDOR-PATCH checklist (VP-3) — mandatory
Per `VENDORING.md:L80-123` and `CLAUDE.md § Upstream dependency`:
1. Edit `orchestrator.py`: add `progress_cb` attribute (default `None`) + one guarded call at `orchestrator.py:L148`. Mark with `# VENDOR-PATCH VP-3: per-turn progress callback (issue #33)`.
2. Generate `patches/VP-3-progress-callback.diff` (relative to repo root) capturing exactly that diff.
3. Add a **VP-3 section** to `VENDORING.md` "Applied patches" (file, patch path, description, marker) mirroring the VP-1/VP-2 entries (`VENDORING.md:L30-59`).
4. Add `git apply patches/VP-3-progress-callback.diff` to the re-vendor checklist step 3 (`VENDORING.md:L96-100`) and add the VP-3 marker to the step-5 grep expectation (`VENDORING.md:L105-113`).
5. Confirm `grep -rn "VENDOR-PATCH" src/baton_harness/vendor/` shows VP-3 in `orchestrator.py`.

### CI gate
`ruff check .` · `ruff format --check .` · `mypy src` · `pytest -q`. Note: `mypy` ignores the vendored tree (`mypy.overrides` per `VENDORING.md:L72-78`), so the VP-3 callback attribute needs no annotation burden there — but the **chain-side** closure and `LivenessState` fields are fully type-checked.

---

## Phase P3 — Docs §11 update + close-out

**Goal:** document the new progress-bound semantics; close #33.
**Depends on:** P1 and P2 (documents both).

### Files touched
- `docs/harness-design.md` — update §11 (`L296-352`): add a "progress-bound stall (worker-active phase)" subsection distinguishing the two phases and the two stall predicates (wall-clock backstop vs progress-bound); add AC2 orphan-worktree-GC subsection; cross-reference VP-3. Update §11 heading provenance to note issue #33.
- `src/baton_harness/chain/obs_config.py` — ensure the two new env vars (`BH_WORKER_PROGRESS_STALL_S`, `BH_WORKTREE_GC`) are in the module docstring env table (if not fully done in P1/P2).
- `README.md` — add the two new `BH_*` env vars to the env-var reference if the README documents them (per `CLAUDE.md § README Maintenance`).
- `VENDORING.md` — confirm VP-3 entry landed (P2 work; P3 verifies).

### Contract / behavior
Docs-only. No code change. §11 must accurately describe: (a) the `_worker_active` flag and the two logical phases it represents, (b) which predicate applies in which phase, (c) the CI-gate exemption (IS-1) including the `ci_gate_reentry` path's explicit `worker_active=False`, (d) the orphan-worktree detect/reclaim modes (IS-5).

**NIT-1:** Note in §11 that the wall-clock `heartbeat_stall_s=7200` backstop is meaningful primarily in NON_WORKER phase and as a catch-all if the progress signal itself breaks (the progress stall at 1800 s always latches first during WORKER_ACTIVE — 1800 s « 7200 s).

**NIT-2:** Verify at P3 merge that `VENDORING.md` re-vendor checklist step 3 includes `git apply patches/VP-3-progress-callback.diff` (P2 is responsible for adding this; P3 confirms it is present before closing #33).

### Testable seams
None (docs). Verification = manual read-through + the §11 claims re-checked against the merged P1/P2 code (cite-sources discipline).

### CI gate
`ruff` / `mypy` no-ops on docs; run full `pytest -q` to confirm nothing regressed. Close #33 via `Closes #33` in the P3 PR body (the integration PR into `main`).

---

## Risks & open questions

- **OQ-1 (AC2 default mode):** P1 defaults to **detect-only** (`BH_WORKTREE_GC=detect`), with reclaim opt-in. AC2 says orphans must be *detected* — detect-only fully satisfies the literal AC. Confirm the user is content shipping reclaim behind a flag rather than auto-reclaiming, or wants `reclaim` as the default. *Recommendation: detect-default — destructive auto-GC of worktrees is the higher-risk option and `simplicity-first` favors the conservative default.*
- **OQ-2 RESOLVED — progress-stall window:** verified `1800.0 s` (6× the per-turn timeout). Source: `max_retry_backoff_ms=300_000` ms (`config.py:L31`) passed as `timeout_ms` to `run_turn` (`orchestrator.py:L155`); `max_turns=8` (`config/WORKFLOW.md`). `progress_cb` fires at turn-loop top, so the stall window is bounded by one turn timeout (~300 s). Default `1800.0 s` (30 min) is a 6× safety margin over a single hung turn. P2 ships `BH_WORKER_PROGRESS_STALL_S` with this verified default; `obs_config.py` must add a comment cross-referencing `max_retry_backoff_ms` / `config.py:L31`.
- **Risk — re-vendor drift (VP-3):** VP-3 adds a third tracked patch to re-apply on every re-vendor. Mitigated by attribute-injection (no signature change) keeping the diff minimal and by the mandatory `VENDORING.md` checklist update in P2.
- **Risk — shared debounce latch (IS-4):** sharing `_stall_alerted` between wall-clock and progress stalls means the *first* stall in an episode suppresses the second. This is intentional (one alert per stuck episode), but if the user wants both signals surfaced independently, the latch must be split per-predicate — flag as a design fork only if OQ surfaces it.

---

## Sources

- Issue glitchwerks/baton-harness#33 (dispatch brief: four ACs + completed recon audit).
- Verified file:line anchors (re-read 2026-06-16): `heartbeat.py:L21-27,L66-71,L89-124,L216-430`; `daemon.py:L725-729,L836,L959-967,L1013-1030,L1470-1491`; `orchestrator.py:L103-208`; `worker.py:L74-167`; `workspace.py:L72-117,L100`; `recovery.py:L330-375`; `obs_config.py:L38-72,L113-223`; `escalation.py:L38-120`; `config.py:L31` (`max_retry_backoff_ms=300_000`); `config/WORKFLOW.md` (`max_turns=8`).
- Policy: `CLAUDE.md § Upstream dependency`; `src/baton_harness/vendor/symphony/VENDORING.md:L30-123`.
- Docs: `docs/harness-design.md:L296-352` (§11, AC4 already MET).
- Format precedent: `docs/plan-34-observability.md` (sibling plan, naming + structure).
- Review: project-reviewer findings, 2026-06-16 (BLOCKING-1, BLOCKING-2, and four CONCERNs/NITs folded in by doc-writer same date).
