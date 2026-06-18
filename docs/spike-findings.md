# Spike findings — smoke test

**Status:** Living document, updated as the spike progresses. Source for the eventual findings memo and the architecture spec revisions.
**Companion docs:** [architecture-spec.md](./architecture-spec.md), [open-questions.md](./open-questions.md)
**Last updated:** Spike day 1 — viability established. Core loop confirmed end-to-end (Scenario A merged on green CI); graceful degradation confirmed (B, C).

## Viability verdict

**The smoke test's core question — "could this work?" — is answered: yes.**
- **Scenario A:** a labeled issue produced a correct PR that passed pytest CI and was merged. The core loop closes end-to-end.
- **Scenarios B & C:** ambiguous and impossible work degrade into graceful blocks with actionable questions — not wrong guesses or garbage output.
- Repeated headless runs throughout the spike did not hit a rate-limit wall — **but see the caveat below; this evidence is weak.**

**Rate limits are NOT validated.** The runs that didn't throttle were trivial (`add()`, `greet()`, blocks): tiny context, few turns, minimal tokens, concurrency 1. Subscription limits are typically keyed on token throughput over rolling windows, so the drivers that push toward the ceiling — large per-run context (real repo + CLAUDE.md + multiple files + accumulated tool results), high turn counts (15–30 for a real feature vs 2 for a toy), concurrency > 1, and sustained overnight load — were all minimized or absent. The spike supports only the narrow claim "low-demand headless runs at concurrency 1 don't immediately throttle," not "the subscription supports the intended workload." This is a **pilot-phase** question: it can only be answered by running genuinely representative issues against a real project and watching token consumption and throttling. It matters more than the deferred robustness items because it can break the *cost model* (forcing reduced concurrency or the per-token API-key fallback).

This clears the viability bar. Everything beyond it — CI auto-detection (F10), stuck-run / crash recovery (Scenarios E, F), and the C1–C3 harness concerns — is robustness and design, **not** viability, and belongs to the harness-design phase.

**Remaining viability-grade question — rate limits under real load.** Not answerable with toy issues (see above). Belongs to a pilot phase: run a handful of genuinely representative issues against a real repo and measure token consumption and throttling. If realistic load throttles hard, the fallback is reduced concurrency (slower) or the API-key path (per-token cost). A quick concurrency sanity check (`max_concurrent` ≥ 2 with two real issues) is worth doing early in the pilot, but the toy-issue version proves nothing.

---

## Decisions made during the spike

### D1 — ToS posture: risk accepted (revisit at terms changes)
Reviewed the current Claude Code / consumer terms. No explicit verbiage was found prohibiting headless, orchestrator-driven use of the first-party `claude` binary on subscription auth. The ambiguity appears deliberate (Anthropic retaining flexibility). Absence of prohibition is not permission, but it is not prohibition either.

**Decision:** Risk accepted for a personal project. This is a *monitored assumption*, not a closed gate — terms moved twice in 2026, so re-check at any major Claude Code or consumer-terms update. The API-key fallback (spec §8 risk table) stays designed-in as the escape hatch: a terms change closing the subscription path becomes a cost increase, not a dead end.

### D2 — Harness is its own repo, not a Baton fork
The policy layer (outcome router, hooks, prompts, templates, eventual Slack bot + Dockerfile) lives in a standalone version-controlled repo with the orchestrator as an upstream dependency. Fork only as a last resort, after "work around it" and "contribute upstream" are exhausted. *Detailed harness architecture deferred until the spike completes.*

> **[SUPERSEDED 2026-06-06 by option-(c) vendoring — see harness-design.md §1]** D2's "own repo, not a Baton fork" framing is not violated, but its "upstream dependency" and "contribute upstream" premises are superseded: `symphony/` is vendored into `src/baton_harness/vendor/symphony/` and called directly. The harness remains its own repo; D2 as a historical decision record is preserved here.

---

## Scenario status

| Scenario | What it tests | Status |
|---|---|---|
| A — happy path | Single clean issue → PR | **PASS** — correct PR, green pytest CI, merged. Core loop confirmed end-to-end |
| B — ambiguous → block | Agent recognizes ambiguity, blocks | Core behavior passing (see F6); harness label bug remains (H1) |
| C — failure path | Genuine failure, distinguishable from block | Ran; produced a BLOCK not a fail (see F9). Genuine failure path still untested |
| D — concurrency + rate limits | `max_concurrent: 2`, throttling behavior | Pending |
| E — stuck run / reconciler | Does reconciler detect + kill hung runs | Pending |
| F — mid-run kill / recovery | In-flight state when orchestrator dies | Pending |

---

## Findings

### F1 — Tool identity: CLOSED — it is `mraza007/baton`
The README confirms it: "inspired by OpenAI's Symphony spec, rebuilt from scratch for Claude Code." The `.symphony/worktrees/` path is simply Baton's Python package name (`symphony/orchestrator.py`, `symphony/config.py`, etc.) — its worktree directory, not a different tool. Resolved via docs; no test needed. The D2 fork-vs-dependency reasoning and all Baton docs apply.

### F2 — Env vars not passed to hooks
`$ISSUE_NUMBER` and `$EXIT_CODE` come back empty in `before_run`/`after_run`. The orchestrator does not export the variable names the spec's WORKFLOW.md assumed.
**Workaround:** derive issue number from `basename "$PWD"` (worktree dir is named by issue number); use shell `$?` for exit status.
**Spec implication:** don't assume hook env vars; derive context from the worktree path. If this proves fragile, it's a candidate for an upstream contribution.
**Docs confirm:** the README documents no env-var contract for hooks — hooks are raw shell run in the worktree cwd. So `basename "$PWD"` isn't a workaround around a bug, it's the intended way to get issue context in a hook. Prompt template variables (`{{ issue.number }}` etc.) are Jinja2 for the *prompt*, not hook env. Resolved.

> **Forward state [implemented, VP-1, issue #27 P0]:** Under vendoring, the `env=` threading fix inside `run_hook` passes `ISSUE_NUMBER` directly. The `basename "$PWD"` workaround is retired.

### F3 — Orchestrator does NOT manage GitHub labels for run state
It tracks run state internally (the `.symphony` state machine), and does not mutate GitHub labels on dispatch. Confirmed by observing `agent-ready` persist (not transition to `agent-in-progress`) during a run.
**Spec implication (significant):** the architecture assumed the orchestrator owned the `agent-ready → agent-in-progress → terminal` label machine. It does not. **Label lifecycle must be owned by the harness hook layer.** Update spec §3.3 / §3.4.

### F4 — Agent writes code but not the closing git/PR steps
Without explicit instruction, the agent implements and verifies, then stops — leaving changes unstaged, no commit, no PR. Confirmed: files modified, changes unstaged, no commits on branch.
**Fix:** mechanical, numbered REQUIRED STEPS in the prompt (stage → commit → push → `gh pr create --draft`), with "report which step failed and STOP" framing.
**Spec implication:** the agent prompt must be more mechanical than drafted. Implementation is the agent's default "done"; shipping is a separate, explicitly-instructed phase.

### F5 — Outcome states are richer than the spec's done/blocked/failed
Real terminal states observed: `uncommitted-changes`, `no-commits`, `committed-no-pr`, `pr-opened`. "No PR" is not a single failure — it has multiple distinct causes that need distinct handling.
**Spec implication:** the outcome router (Dial 2) must distinguish more states than the spec listed. The `after_run` classifier now detects these four; the done/blocked/failed model in §3.3 is too coarse.

### F6 — Dial 1 soft-confidence works (PASS)
In Scenario B, the agent recognized genuine ambiguity, declined to guess, applied `blocked`, and left an *actionable* comment stating what it needed. Prompt-based "stop if unsure" held — which was a real open question, since soft instructions don't always survive contact with an eager model.
**Spec implication:** validates that Dial 1 can be partly carried by the prompt. Deterministic PreToolUse hooks still needed for hard invariants, but soft-confidence for judgment calls is viable.

### F7 — `max_turns` is retries-per-issue, not turns-within-a-session
Hitting the cap produced fresh restarts of the whole run, not continued reasoning. Each retry is a separate `claude` invocation against the same issue.
**Spec implication:** `max_turns` calibration is about retry budget and cost, not reasoning depth. The spike's initial `max_turns: 3` cut off legitimate work because the real problem (F4) made every attempt "fail." Raised to 5; real fix was the prompt.

### F8 — Hooks can call external script files (confirmed pattern)
Hooks invoke standalone scripts (`~/baton-harness/*.sh`) that take the issue number as an argument and are independently testable (`after-run.sh 2` against a worktree, no orchestrator needed). Scripts live outside the repo so the agent can't touch its own harness and they aren't duplicated per-project.
**Spec implication:** the outcome router is a real, testable, version-controlled script — not inline hook logic. This is the harness layer beginning to take shape (ties to D2).
**Forward note (2026-06-04):** the shell-script convenience established here was superseded by the implementation-language decision: hooks are now Python modules in the `baton_harness` package. The testability principle holds; the language does not. See harness-design.md — "Decision — Implementation language: Python."

### F9 — Agent funnels underspecified/impossible work to BLOCK, not FAIL
Scenario C used an untestable, contradictory issue (`lucky_number()` with no verifiable contract) expecting a genuine failure. The agent instead **blocked** — correctly — identifying the specific contradictions (determinism unspecified, "for today" vs "value doesn't matter," no testable assertion possible) and asking for the contract. Identical outcome class to Scenario B.
**Significance:** the agent converts "I can't do this confidently" into block-and-ask, not into failure or garbage output. Underspecified / contradictory / impossible inputs reliably surface as *questions*. This is excellent for the async model — imperfectly-scoped work degrades into "needs input," not "wasted run." It also means the block→respond→requeue loop (S2.2) is the **dominant** path for anything less than perfectly specified, not an edge case — raising its design priority.
**Reframes S2.1:** the important distinction is **block vs done**, not block vs fail. A genuine "failed" state is narrower and still untested — it needs a task clear enough that the agent attempts it, which then fails at *execution* (broken env, unresolvable error, red CI). "Impossible/untestable" tests blocking, not failing. Test design must isolate execution failure from specification failure.

### F10 — Outcome classifier marks pr-opened as done WITHOUT checking CI
The `after_run` classifier treats any opened PR as `agent-done`. It does not check CI status. A PR with red CI would currently be marked done. This is the silent-bad-merge risk (S3.3) made concrete: "done" presently means "a PR exists," not "a correct PR exists."
**Spec implication:** Dial 2's `done` determination must incorporate CI status, or introduce a distinct `needs-review-ci-red` state. The outcome router must wait for / query CI before declaring success.

### F11 — Baton capabilities (from docs)
Authoritative facts from the README, relevant to the harness:
- **Config-by-path:** `baton start -w <path>` accepts a custom WORKFLOW.md path. (Residual check: docs show a relative-path example; confirm an *absolute* path outside the project cwd works.)
- **`after_create` hook:** runs once after worktree creation (e.g. `npm install`). Partial mitigation for S2.4 isolation (deps, not ports/services).
- **`{{ attempt }}` variable:** retry attempt number available in the prompt.
- **Hook timeout:** `hooks.timeout_ms` default 60s.
- **`permission_mode: bypassPermissions`** is a documented valid value (used in the README's own example) — confirms the F4 fix direction.
- **CLAUDE.md is project-local:** Baton doesn't manage it; Claude Code discovers it from the worktree (a repo checkout), so it must be committed to the project repo.

→ Design implications (integration model, repo structure) live in `harness-design.md`, not here.

### H-note (cost) — a block costs up to `max_turns` runs

**Sub-question resolved — #6 dry run (T2); decision recorded in harness-design.md §8.**

A block produces no PR, so Baton retries it as a continuation up to `max_turns`. A blocked issue therefore consumes up to `max_turns` full Claude runs before settling — a cost-model factor, not just the H1 label bug.

The open sub-question (does the continuation check re-read labels and respect `exclude_labels: ["blocked"]`?) was answered by the #6 dry run: **no**. Baton evaluates `exclude_labels` at poll time only. Once a run is dispatched, it is not halted between turns. Block costs `max_turns`, not ~1 run.

Pilot decision: accept the `max_turns` cost as a known bound; keep `max_turns` modest. Full decision record: harness-design.md §8 — "[design] H1 fix — terminal-block decision."

> **[SUPERSEDED 2026-06-06 by option-(c) vendoring — see harness-design.md §1]** The terminal-block fix was described here as "deferred as upstream-dependent (requires a post-turn `exclude_labels` re-check or per-turn hook in Baton)." Under vendoring, this fix is ~10 lines inside the vendored `_run_worker` turn loop — a harness-internal change with no upstream dependency. The `max_turns: 2` workaround is retireable post-vendoring. Issue #23 is closed.

---

## Harness bugs to fix (deferred — implementation, not architecture)

### H1 — Label reconciliation not achieving single-state invariant on block path
After a block, the issue still carries both `agent-ready` and `blocked`. The `after_run` reconciliation didn't resolve it.
**Docs-informed cause:** hooks run "after each agent turn," so `after_run` *does* fire on the block path — which makes cause (a) "hook doesn't fire" unlikely and points to (b) the `gh issue edit --remove-label` failing silently behind `2>/dev/null || true`, or a retry interaction (the block is retried as a continuation up to `max_turns`, and each turn re-evaluates state). 
**Diagnostic when picked up:** check `/tmp/spike.log` for the `AFTER` line on the blocked issue. Present with `outcome=blocked` but label remains → silent edit failure (b); drop the `2>/dev/null` to see the real error. Also tie to the H-note: a clean fix likely needs the block to be *terminal* (stop the continuation retry), not just reconciled — see the open sub-question on whether continuation respects `exclude_labels`.

---

## Design concerns raised (resolve before harness build)

These surfaced while designing the async CI-completion trigger (the second outcome-routing stage from F10). Both are consequences of having a second process that reacts to PR/CI events outside the orchestrator's own run loop. Neither is an edge case.

### C1 — Claiming / locking for re-queued failures (multi-writer race)
The CI-completion trigger is a separate process from the orchestrator. When a PR fails CI and must be reworked, something re-routes it. If that means re-labeling the issue `agent-ready`, the poller re-grabs it — but:
- GitHub labels are **not an atomic lock**; check-then-set races. Two workers, or a worker and the CI-trigger, can both claim the same item.
- The orchestrator's internal claim tracking (the `.symphony` state machine) only covers its own single-process runs — it does not coordinate with an external CI-trigger or with multiple instances.
- A failed PR is a **different work unit** than a fresh issue (rework the existing branch, don't start fresh); claiming must account for that.

**Risk:** multiple agents grab the same failed PR at once → duplicated work, wasted subscription quota, conflicting pushes to the same branch.
**Solution shape (to design):** single-writer claim authority. The CI-trigger should *signal* the orchestrator rather than re-queue directly; the orchestrator solely owns claim/state mutation. Alternative: an atomically-applied lock label or an external lock. Principle: exactly one component mutates claim state.

### C2 — PR provenance / authorization filter (security + burn)
A harness that reacts to PR/CI events must act **only** on PRs the agent itself created — never on PRs from human contributors or external parties.

**Risks if provenance isn't checked:**
- **Burn:** agent spends subscription quota reworking PRs it should never touch.
- **Security:** an external PR or issue is an untrusted-input vector — an autonomous agent with repo write access acting on stranger-supplied content (instructions embedded in a PR body, malicious code it then "fixes" and pushes). Acute on public repos, where anyone can open a PR or issue.

**Solution shape (to design):** provenance allowlist. Only agent-authored branches/PRs — identifiable by branch prefix (`agent/issue-N`) or the agent's bot identity — are eligible for harness action. Human/external PRs, and issues from non-trusted authors, are ignored by the harness entirely. The agent only works items labeled by the trusted owner; it never auto-engages on arbitrary-author PRs or issues. Directly tied to the instruction-source-boundary principle: observed content (a stranger's PR) is data, not a command.

### C3 — Bounded rework with escalation (avoid infinite fix→fail→fix loops)
When a PR fails CI (or fails review), the trigger re-engages an agent to fix it. Without a budget this can loop indefinitely: fix → CI red → fix → CI red → … burning subscription quota and never converging.

**Distinct from `max_turns` (F7):** `max_turns` bounds retries to get a PR *open*, inside the orchestrator's run loop. This bounds rework attempts *after* a PR exists, driven by the async CI/review trigger across multiple CI cycles. The orchestrator considers the issue done once the PR opened, so its budget doesn't cover this loop. They do not compose automatically — the rework loop needs its own budget and counter.

**Requirements:**
- A per-PR rework counter stored in GitHub (source of truth) — marker comment or label — mutated only by the single claim authority (ties C1).
- After N rework attempts: stop auto-reworking, mark blocked/failed, notify via Slack (escalate to human).
- Ideally distinguish fixable failures (lint, test) from environmental/infra failures (runner down, flaky network) — retrying the latter is pure waste. (Refinement, not required for v1.)

**General principle (applies system-wide):** every autonomous retry loop needs (a) a bounded budget and (b) a defined human-escalation exit when exhausted. Applies to CI-rework, review-feedback rework, and any future loop. Escalation target is always: stop, mark blocked/failed, notify human. Converts infinite machine loops into finite loops that terminate at a human.

**Note:** C1, C2, and C3 are all requirements on the *same* component — the async CI/review-completion trigger (outcome-routing stage 2). That component is non-trivial: it needs claim coordination (C1), a provenance filter (C2), and rework-budget tracking (C3). Design it as a real component, not a webhook one-liner.

---

## Open questions still to resolve via spike

- **S1.3 — rate limits:** unmeasured. Scenario D will surface concurrency cap and throttle failure mode.
- **S2.3 — recovery:** untested. Scenarios E and F will show reconciler behavior and mid-run-kill state.
- **S2.1 — failure vs block distinguishability:** partially answered and reframed (F9). The agent blocks on underspecified/impossible work rather than failing, so block-vs-done is the common distinction. Genuine execution-failure remains untested — needs a clear-but-failing-at-execution test case.
- **F1 — tool identity:** one command away from resolution.

---

## Running themes (carried from open-questions.md, confirmed by spike)

1. **Empirical gap was real.** Multiple spec assumptions (label ownership F3, env vars F2, agent shipping behavior F4, outcome states F5) were wrong or incomplete and only surfaced on contact. The spike is doing its job.
2. **Plumbing carries policy.** Label transitions (F3/H1) and outcome routing (F5) — treated as glue in the spec — are where the actual behavior lives. They need first-class, tested implementation.
3. **Failure-recovery above the run is still untested** (Scenarios E/F pending) and remains the largest unverified area.
