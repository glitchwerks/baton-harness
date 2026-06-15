# Plan: #31 — `after_run` idempotent & crash-safe across the 60s hook timeout

**Issue:** glitchwerks/baton-harness#31 — "after_run is not idempotent or crash-safe
across the 60s hook timeout (torn label state)". Milestone: *Failure-mode hardening*.
Blocker #1, High blast radius × Medium likelihood.

**Status:** implementation complete. See addendum below for a correction to the
original Shape A reasoning; all phases committed on branch
`fix-31-after-run-idempotent`.

---

## Addendum — Codex P1 correction to the Shape A convergence claim (PR #95)

**The original "converges for free via the orphan scan" argument was incorrect.**

Section 1 below argued that a zero-state torn issue (kill between `agent-ready`
remove and `agent-done` add) is an `agent-in-progress` orphan that the existing
orphan scan re-dispatches, and that once `after_run` is idempotent the re-run
converges — so Option B (modifying the daemon backstop) was unnecessary.

A Codex P1 review on PR #95 falsified that claim with the following race:

1. `after_run` is killed between the `agent-ready` remove and the `agent-done`
   add. The issue is left with labels `{agent-in-progress}` — zero state labels.
2. `_run_worker` returns `pr_created` (the worker finished; the kill only hit the
   hook).
3. On the next daemon poll tick, the **single-state backstop**
   (`daemon.py:912-959`) fires: it detects zero state labels as a violation,
   clears `agent-in-progress`, and **parks** the issue — all in the same tick.
4. The orphan scan (`daemon.py:1434-1556`) keys on the `agent-in-progress`
   label. Because the backstop removed it in step 3, the scan finds no orphan.
   Re-dispatch never fires.
5. The issue sits at zero state labels with a closed `agent-in-progress` and an
   open PR — parked forever. This is the exact #31 failure mode, unfixed by
   Shape A alone.

**The implemented fix — a narrow, evidence-gated slice of Option B**
(commit `7b8555c`, `daemon.py` backstop at lines 912-959):

The backstop now calls `labels.target_state_from_observed` (Phase 3's pure
helper, now wired) before deciding whether to park. For the specific case of
**zero state labels + open PR + not blocked**, it converges the issue directly to
`agent-done` (adds the label, clears `agent-in-progress`, emits a
`label_invariant_converged` runlog event) rather than parking. It then sets a
`_converged` flag and falls through to the in-tick CI gate
(`worker_result == "pr_created"` → `merge_issue_branch`), merging the PR in
the **same** tick — not on a subsequent poll. (A second Codex P1 on PR #95,
commit 92798a7, caught the original "next tick re-classify via ci-gate-reentry
path (3a)" claim as wrong: the converged `agent-done` issue is seeded by
neither the agent-ready nor the agent-in-progress scan, so it would never have
been re-scanned and the PR would have remained unmerged until daemon restart.)

All other violations — no open PR, `blocked` present, or 2+ state labels —
preserve the existing critical-alert + park behavior unchanged.

This touches the serial poll loop (B-I3) only on the violation branch, and is
gated on definite terminal evidence (an open PR exists) to avoid re-dispatch
loops. The test `TestBackstopConvergence` in `tests/chain/test_daemon.py`
covers the convergence path directly.

The original §1 reasoning (A-vs-B trade-off, YAGNI, latency argument) is
preserved below as written — it reflects the state of analysis before the Codex
P1 review identified the backstop-clears-before-scan race.

---

**CI gate set the code-writer must pass (all four):**
`ruff check .` · `ruff format --check .` · `mypy src` · `pytest` (full suite).

---

## 1. Recommendation: Shape A (after_run-only), not B

**Adopt Shape A.** Make `_reconcile_labels` idempotent and add a small
observed-fact reconciler, but do **not** modify the daemon's in-loop backstop
(option B). Rationale, grounded in what already exists:

- **The convergence machinery for torn state already exists and already runs.**
  When after_run is killed between the remove and add, the issue still carries
  `agent-in-progress` — the daemon sets it *before* dispatch
  (`daemon.py:819-822`) and only clears it *after* the worker returns. A torn
  issue is therefore an `agent-in-progress` orphan, and two independent existing
  paths already re-derive and re-dispatch it:
  - the secondary orphan scan (`daemon.py:1434-1556`, #89), running every tick;
  - `recovery.py::reconstruct` Rule 5 (`recovery.py:359-362`) on daemon start.
  Both route the orphan back through dispatch → `_run_worker` → after_run. Once
  after_run is idempotent (Phase 1), that re-run *converges*. So A already gets
  in-loop convergence "for free" via the now-idempotent hook on the next pass —
  the user's own framing of option A.
- **The daemon backstop already detects torn/zero-state (AC4 detection half).**
  `assert_single_state` (`labels.py:44-85`) treats zero state labels as a
  violation, and the backstop (`daemon.py:912-959`) fires CRITICAL + alert +
  park on it. Option B would replace park-with-converge there — but that touches
  the **serial poll loop**, where the B-I3 serial invariant is load-bearing, and
  buys little: the orphan it would converge is already converged on the next
  tick by the idempotent hook. B trades a real invariant-risk for a latency
  improvement of one poll interval on an already-alerted, already-parked issue.
- **YAGNI.** B's "converge in-loop" duplicates a convergence the orphan scan
  already performs. The honest delta of this issue is **small** — see §2. Adding
  B is over-engineering against a path that self-heals one tick later.

**The one real code change is in after_run, and it is one guard.** Everything
else is test coverage of paths that already behave correctly.

If the user later observes that a single-poll-interval convergence latency on
torn state is operationally unacceptable (e.g. alert noise from the backstop
park before the orphan scan re-converges), revisit B as a follow-up issue — but
do not pre-build it.

---

## 2. AC-by-AC mapping

| AC | Status | Concrete change |
|----|--------|-----------------|
| **AC1** — `_reconcile_labels` idempotent; re-run converges to correct single state | **needs-new-code (1 guard)** | The PR_OPENED path removes `agent-ready` **unconditionally** (`after_run.py:497-514`), unlike Priority 1 (`:456`) and Priority 3 (`:549`) which guard with `if LABEL_AGENT_READY in labels`. On a re-run after a kill-between-remove-and-add, `agent-ready` is already gone; the unconditional `gh issue edit --remove-label agent-ready` hits an absent label. `gh` exits non-zero in the symmetric add-already-exists case (evidence: `bin/init-sandbox.sh:162-167` explicitly tolerates `gh`'s non-zero "already exists" exit), so the absent-remove very likely returns non-zero → current code returns `1` at `:507-514` and **never adds `agent-done`** → torn state persists across re-runs. **Fix:** guard the PR_OPENED `agent-ready` removal with `if LABEL_AGENT_READY in labels`, matching Priorities 1 & 3. Then the add-`agent-done` step still runs (and is itself idempotent only if absent → verify add-of-present behavior; if `gh --add-label` errors on a present label, guard it with `if LABEL_AGENT_DONE not in labels`). |
| **AC2** — a reconciler re-derives correct label state from observable facts (blocked present, PR open) independent of which hook last ran | **partially-satisfied — needs a thin pure function + tests** | The *inputs* already exist and are observable: `blocked` via the fetched label set; PR-open via `daemon.py::_find_issue_pr` (`:205-259`) / `recovery.py::_has_open_pr` (`:266-282`). The *derivation* is implicitly encoded across `recovery.py::reconstruct` (`:330-368`) and `_reconcile_labels`, but there is no single pure "given (blocked?, pr_open?) → target single-state" function. **Change:** extract a pure `target_state_from_observed(blocked: bool, pr_open: bool) -> str` into `labels.py` (alongside `assert_single_state`), returning `blocked` / `agent-done` / `agent-ready` per §5 precedence. This makes AC2's "re-derive independent of which hook last ran" explicit and unit-testable, and is reusable by both after_run and recovery. No behavior change to callers required in this phase — wiring is Phase 3. |
| **AC3** — crash-recovery path (Scenario F) has test coverage simulating a kill between the remove and add calls | **partially-satisfied — needs test, not new code** | The *recovery routing* already exists: a kill-between-remove-and-add leaves `agent-in-progress` on the issue, caught by orphan scan (`daemon.py:1434-1556`) and `recovery.py` Rule 5 (`:359-362`). `harness-design.md:264-266` documents this reconstruction invariant; Scenario F is implemented. **The gap is the simulation test at the after_run seam**: no existing test in `tests/test_after_run.py` re-runs `_reconcile_labels` against a torn (zero-state) label set to prove convergence. **Change:** add the kill-simulation test (see Phase 2). No production code beyond Phase 1's guard. |
| **AC4** — torn state (zero state labels) detected and corrected rather than going silent | **detection already-satisfied; correction via AC1 guard + existing convergence** | Detection: `assert_single_state` returns a violation for zero state labels (`labels.py:75-79`) and the daemon backstop acts on it (`daemon.py:920-959`: CRITICAL log + runlog event + `alert(severity=critical)` + park) — **not silent**. Correction: the idempotent hook (AC1) plus the existing orphan-scan/recovery re-dispatch (already running) converge the torn issue on the next pass. **No new detection code.** The only delta is AC1's guard (so the re-run actually corrects) plus a test asserting zero-state is caught-and-corrected, not silenced. |

**True scope delta:** one conditional guard in `after_run._reconcile_labels`
(plus a defensive add-guard), one ~6-line pure helper in `labels.py`, and the
test coverage that the ACs explicitly demand. No daemon-loop changes. No new
recovery path. Detection, alerting, orphan re-dispatch, and Scenario-F
reconstruction all already exist and are verified above against the code.

---

## 3. Phased plan (separately committable)

Each phase is an independent commit on a worktree branch off `main`
(`.worktrees/fix-31-after-run-idempotent`), test-first per the project's TDD
standard.

### Phase 1 — Make `_reconcile_labels` PR_OPENED path idempotent (AC1, AC4 correction)

**Touch:** `src/baton_harness/after_run.py` (`_reconcile_labels`, PR_OPENED
branch `:478-533`).

**Production change:**
1. Guard the `agent-ready` removal with `if LABEL_AGENT_READY in labels:`
   (mirror Priority 1 `:456` and Priority 3 `:549`). Preserve the
   remove-before-add ordering invariant (Finding B / #21) for the case where
   `agent-ready` *is* present.
2. Defensive: guard the `agent-done` add with `if LABEL_AGENT_DONE not in labels:`
   only if a probe confirms `gh issue edit --add-label <present>` exits non-zero.
   If `gh` is idempotent on add-present, leave the add unconditional (simpler).
   **Code-writer must verify this `gh` behavior empirically (or via the existing
   `_run` seam contract) before deciding** — do not assume.

**Tests (seam: patch `baton_harness.after_run._run`; pattern per `test_after_run.py:215`):**
- `test_pr_opened_rerun_after_torn_state_converges`: first `_run` side-effect
  returns labels `{agent-in-progress}` (zero state labels — `agent-ready`
  already removed, `agent-done` not yet added, i.e. the kill aftermath). Assert:
  no `--remove-label agent-ready` call is issued (label absent → guarded out),
  `--add-label agent-done` **is** issued, and `_reconcile_labels` returns `0`.
- `test_pr_opened_remove_skipped_when_agent_ready_absent`: labels
  `{agent-done}` (re-run after full success). Assert no remove call, no add call
  (or add tolerated), exit `0` — full idempotency, second run is a no-op-ish
  convergence to the same single state.
- Keep existing `test_remove_agent_ready_called_before_add_agent_done`
  (`:451`) green for the `agent-ready`-present case.

### Phase 2 — AC3 kill-between-remove-and-add simulation test (no production code)

**Touch:** `tests/test_after_run.py` (new test class
`TestReconcileCrashRecoveryScenarioF`).

**Test (seam: `baton_harness.after_run._run`):**
- `test_kill_between_remove_and_add_then_rerun_converges`: simulate the two-run
  Scenario F sequence.
  - **Run 1 (the kill):** side-effects = `[labels={agent-ready,agent-in-progress}]`
    (fetch) → `[remove agent-ready ok]` → then raise / stop (simulate the 60s
    timeout SIGKILL by making the add side-effect raise `KeyboardInterrupt` or
    by asserting only the remove fired). Assert state after run 1 is torn
    (remove issued, add not issued).
  - **Run 2 (re-dispatch after orphan scan):** fresh `_reconcile_labels(n,
    PR_OPENED)` with side-effects = `[labels={agent-in-progress}]` (torn) →
    `[add agent-done ok]`. Assert: returns `0`, `agent-done` added, no spurious
    remove of the absent `agent-ready`.
  - Assert end-state is exactly one state label (`agent-done`) — the AC1
    convergence guarantee proven across a simulated crash boundary.

This test is the literal AC3 deliverable ("test coverage simulating a kill
between the remove and add calls") at the seam the issue names.

### Phase 3 — Pure observed-fact reconciler for AC2 (extract + unit test; wire minimally)

**Touch:** `src/baton_harness/chain/labels.py` (new
`target_state_from_observed`); `tests/chain/test_labels.py` (new tests).

**Production change:** add a pure function beside `assert_single_state`:
```text
target_state_from_observed(blocked: bool, pr_open: bool) -> str
    # precedence per harness-design.md §5:
    #   blocked            -> LABEL_BLOCKED
    #   not blocked, pr_open -> LABEL_AGENT_DONE
    #   else               -> LABEL_AGENT_READY
```
Pure, no I/O, no raise — same contract style as `assert_single_state`. This is
the "reconciler that re-derives correct label state from observable facts …
independent of which hook last ran" (AC2 verbatim).

**Tests (`tests/chain/test_labels.py`):** truth-table coverage of all four
`(blocked, pr_open)` combinations → expected single-state label; assert return
is always a member of `STATE_LABELS`.

**Wiring (minimal, no behavior change required for ACs):** leave `after_run`
and `recovery` callers as-is in this PR unless the code-writer finds a
zero-risk substitution. The function's existence + unit coverage satisfies AC2;
threading it into callers is a clean-up that can be a follow-up if it would
perturb the serial loop or recovery precedence. **Do not refactor
`recovery.py::reconstruct`'s precedence ladder in this issue** — its B-I2
provenance rules outrank the simple (blocked, pr_open) derivation and must not
be flattened.

### Phase 4 — Docs + invariant note

**Touch:** `docs/harness-design.md` §5 (label state machine) / §10
(crash-recovery): add a sentence that `_reconcile_labels` is idempotent and that
torn state converges via the orphan-scan re-dispatch through the idempotent
hook. Confirm the §-status claims (Scenario E not-implemented, Scenario F
implemented) still hold after this change — they do; this issue does not touch
Scenario E.

---

## 4. Test seams (reference)

- after_run: patch `baton_harness.after_run._run` (all `git`/`gh` subprocess);
  `baton_harness.after_run.time` (backoff sleep). Established pattern:
  `tests/test_after_run.py:215`.
- daemon (not modified in this plan, but for cross-checks):
  `daemon._run`, `daemon.alert`, `daemon._run_worker`, `daemon.reconstruct`.
- recovery: `recovery._run` (sole I/O seam, `recovery.py:55-73`).

---

## 5. Sources (all verified against the code this session)

- `src/baton_harness/after_run.py:478-533` — PR_OPENED path; **unconditional**
  `--remove-label agent-ready` at `:497-514` (the AC1 idempotency break).
- `src/baton_harness/after_run.py:456`, `:549` — guarded removals in
  Priorities 1 & 3 (the pattern Phase 1 mirrors).
- `src/baton_harness/after_run.py:331-375` — `_current_labels`, `None`-sentinel
  on fetch failure (distinct from `[]`).
- `src/baton_harness/chain/labels.py:44-85` — `assert_single_state`; zero-state
  treated as violation (`:75-79`) — AC4 detection half already exists.
- `src/baton_harness/chain/daemon.py:819-822` — daemon sets `agent-in-progress`
  + removes `agent-ready` **before** dispatch (why a torn issue is an orphan).
- `src/baton_harness/chain/daemon.py:912-959` — single-state backstop:
  detect + alert(critical) + park, does **not** converge (the option-B hook
  point, deliberately not modified).
- `src/baton_harness/chain/daemon.py:1434-1556` — secondary orphan scan (#89);
  re-dispatches `agent-in-progress` orphans every tick.
- `src/baton_harness/chain/daemon.py:205-259` — `_find_issue_pr` (observable
  PR-open fact for AC2).
- `src/baton_harness/chain/recovery.py:330-368` — `reconstruct` classification;
  Rule 5 (`:359-362`) routes `agent-in-progress` orphans to redispatch
  (Scenario F crash recovery).
- `src/baton_harness/chain/recovery.py:266-282` — `_has_open_pr` (observable
  PR-open fact, reusable for AC2).
- `bin/init-sandbox.sh:162-167` — evidence that `gh` exits non-zero on the
  symmetric "label already exists" case; basis for expecting `--remove-label
  <absent>` to also be non-zero (the code-writer must still verify empirically).
- `docs/harness-design.md:264-266` — crash-recovery reconstruction invariant
  (Scenario F implemented); §5 label state machine.
- `tests/test_after_run.py:215`, `:451`, `:488` — existing PR_OPENED test
  patterns and the Finding-B ordering regression to keep green.

> 🤖 _Generated by Claude Code on behalf of @cbeaulieu-gt_
