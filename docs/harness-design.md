# Harness design

**Status:** Living design document. Starts at pilot scope; evolves as the harness grows. This is the design of the harness *itself* — the policy layer that drives Baton. Baton is an upstream dependency, not part of this repo (decision D2).

**Companion docs:** [architecture-spec.md](./architecture-spec.md) (overall system), [spike-findings.md](./spike-findings.md) (what the spike established — referenced here, not restated), [problem-statement.md](./problem-statement.md) (constraints).

---

## 1. What the harness is

A standalone, version-controlled repo that holds the *reusable policy and tooling* around the orchestrator: the agent prompt, the lifecycle hook scripts, per-project config, context templates, and the launcher. Baton (the orchestrator) is installed as a dependency; the harness is what makes it do the right thing.

Decision D2: this is its own repo, **not** a Baton fork. Fork only as a last resort, after "work around it" and "contribute upstream" are exhausted.

---

## 2. Integration model — point-at-path

Validated against Baton's docs (see spike-findings F11). Baton runs project-local — launched from the project directory — but its config can live elsewhere and be pointed at:

```bash
cd <project-repo> && baton start -w /agent-harness/config/<project>/WORKFLOW.md
```

- The harness repo owns the **scripts** (hooks call them by absolute path) and the **WORKFLOW.md** (passed via `-w`).
- The **project repo** carries only its own committed `CLAUDE.md` (Claude Code discovers it from the worktree; not relocatable — F11) and its CI workflow (a precondition, not the harness's job).
- A launcher in the harness encapsulates the `cd` + `-w` invocation per project.

This keeps the harness as the single source of truth for everything shareable, with the smallest possible footprint in each project repo.

---

## 3. Repo structure (pilot scope)

Deliberately minimal. Grows without restructuring as later phases are added.

```
agent-harness/
├── README.md
├── bin/
│   └── run.sh                  # launcher: cd into project, baton start -w <config>
├── scripts/
│   ├── after-create.sh         # per-worktree dependency install (npm/pip) — Baton after_create hook
│   ├── before-run.sh           # branch sync onto main
│   └── after-run.sh            # outcome classification + label reconciliation
├── config/
│   └── <pilot-project>/
│       └── WORKFLOW.md          # hooks → absolute paths into scripts/; the agent prompt
├── templates/
│   └── CLAUDE.md.template       # source for each project's committed CLAUDE.md
└── docs/                        # references to spec, findings
```

**Project repo carries:** its own committed `CLAUDE.md` (sourced from the template) and the CI workflow.

**Evolution path (not built for the pilot):** project #2 turns `config/<pilot-project>/` into multiple `config/<name>/` dirs and extracts a WORKFLOW.md template; containerization adds a `Dockerfile`; the comms layer adds `bot/`; the async CI handling adds a `triggers/` component.

---

## 4. Components

### 4.1 Launcher — `bin/run.sh`
Encapsulates the project-local invocation so it isn't retyped or misremembered. Resolves the harness directory, `cd`s into the target project, and starts Baton pointed at that project's config. Exports the harness path so hooks can resolve script locations if they don't hardcode them.

### 4.2 Hooks — `scripts/`
Standalone, independently testable shell scripts (spike F8 confirmed this pattern), each taking the issue number as an argument derived from the worktree path (`basename "$PWD"` — F2: Baton passes no env-var context to hooks).

- **`after-create.sh`** — runs once after worktree creation. Per-worktree dependency setup (`npm install` / `pip install`). Partial mitigation for the worktree-isolation limits (S2.4); does not solve shared ports/services.
- **`before-run.sh`** — syncs the worktree branch onto latest `main` before the agent runs.
- **`after-run.sh`** — the outcome router. Classifies what the run produced (the states from F5: `uncommitted-changes`, `no-commits`, `committed-no-pr`, `pr-opened`) and reconciles GitHub labels to a single state. Must finish under the 60s hook timeout (F11).

### 4.3 Config — `config/<project>/WORKFLOW.md`
Per-project Baton config: tracker labels, concurrency, `max_turns`, `permission_mode: bypassPermissions` (F11/F4), the `after_create`/`before_run`/`after_run` hook wiring (absolute paths into `scripts/`), and the agent prompt body. The prompt uses the mechanical, numbered closing-steps pattern proven necessary in the spike (F4) and the explicit confidence/block rule (F6/F9).

### 4.4 Context template — `templates/CLAUDE.md.template`
Source for each project's `CLAUDE.md`. Because CLAUDE.md is irreducibly project-local (F11), the live file is committed to the project repo; this template is the harness-owned source it's generated from. Should encode the conventions the agent needs plus the boundaries from the problem statement (e.g. no infra changes, no design decisions, implementation only).

---

## 5. Label state machine (harness-owned)

The harness owns GitHub label transitions, because Baton does not (spike F3 — Baton tracks run state internally and does not mutate GitHub labels). The human-facing states:

```
agent-ready ──▶ (run) ──▶ agent-done       (PR opened; pilot: human verifies CI at review)
                      └──▶ blocked          (agent needs input; single source of truth)
                      └──▶ agent-ready      (retryable failure; left for Baton's own retry)
```

Reconciliation is enforced in `after-run.sh` to maintain a single state label (the H1 bug — both `agent-ready` and `blocked` present — is the open implementation issue to fix here).

---

## 6. Inherited design constraints

These come from the spike and must be honoured by the harness as it grows. They are *not* re-argued here; see spike-findings for the reasoning.

- **C1 — single-writer claim authority.** When the async CI/review layer is added, exactly one component may mutate claim/state. (Deferred — not in pilot.)
- **C2 — provenance allowlist.** The harness acts only on agent-authored branches/PRs and owner-labeled issues; never on arbitrary-author content. (Deferred — not in pilot, since the pilot has no event-driven trigger.)
- **C3 — bounded rework with escalation.** Every autonomous retry loop needs a budget and a human-escalation exit. (Deferred — pilot reviews PRs manually.)
- **Cost note (H-note).** A block is retried as a continuation up to `max_turns`, so a block can cost up to `max_turns` full runs. Keep `max_turns` modest until the open question in §8 is answered.
- **Outcome ≠ green CI (F10).** "PR opened" is not "correct." In the pilot, the human is the CI gate at review; automating this is a later phase.

---

## 7. Out of scope for the pilot harness

Explicitly deferred so the pilot stays minimal:

- **Docker containerization** — pilot runs on the host.
- **Slack / comms layer** — observe via GitHub directly.
- **Async CI-completion trigger and auto-rework** — human reviews PRs; this is what defers C1/C2/C3 entirely.
- **Multi-project templating** — single pilot project; templatize when project #2 appears.
- **Observability tooling (Langfuse etc.)** — basic logs only.

---

## 8. Open questions (resolve at pilot entry or during)

Two are docs-can't-answer test targets; the rest are design decisions to make as the harness evolves.

- **[test] Absolute `-w` path:** confirm `baton start -w <absolute-path-outside-project>` works (docs show only a relative example). ~2 min.
- **[test] Block cost:** does Baton's continuation retry respect `exclude_labels: ["blocked"]` and stop after the first blocked turn, or burn all `max_turns`? Determines per-block cost. (H-note.)
- **[design] Script path resolution:** do hooks hardcode the harness path, or read it from an env var exported by the launcher? Lean env var for portability.
- **[design] CLAUDE.md sync:** how does the template become the project's committed CLAUDE.md — manual copy for the pilot, or a small generate step? Manual is fine for one project.
- **[design] H1 fix:** make a block terminal (stop the continuation retry) rather than only reconciling labels — depends on the block-cost test outcome.

---

## 9. Relationship to the architecture spec

The architecture spec (§3.4) described the orchestration layer abstractly. This document is the concrete, Baton-specific realization of that layer for the pilot. Where they diverge, the spike findings are the tiebreaker — several spec assumptions were corrected by the spike (notably F3 label ownership, F5 outcome states, F9 block-vs-fail). The spec should eventually be updated to match; until then, this design doc reflects current ground truth for the harness.

---

## 10. Future exploration: sequential / dependency-ordered milestones

**Status:** Pathfinding item for a later phase. Not pilot scope. Captured because it materially shapes the harness and interacts with the human-merge checkpoint in a non-obvious way.

### The problem
A milestone is usually a dependency graph, not a flat bag of independent issues. Example decomposition:

```
1 → 2 → 3 → [4, 5, 6 parallel] → 7
```

The flat `agent-ready` model can't express this. If you mark all of 1–7 ready, the agent may attempt 3 before 1 and 2 exist. If you mark only 1 ready (the safe choice), 2–7 never run until something promotes them. Today that "something" is you, manually, which defeats the unattended premise for any multi-step milestone.

### The enabler — GitHub-native issue dependencies
GitHub issue dependencies are GA: mark issues `blocked by` / `blocking` (up to 50 each), with REST API and webhook support. This means the DAG can be expressed natively in GitHub — set by you during planning (consistent with "human plans, harness executes" — decomposition stays human-driven; only the *reading and scheduling* is automated). The harness never infers dependencies; it reads the ones you declared.

### Architectural shape (to explore)
A harness-layer **promoter/scheduler** component, separate from Baton:
- Reads each issue's `blocked_by` relationships via the GitHub API.
- Watches for dependency satisfaction; promotes newly-eligible issues (the "ready frontier" of the DAG) to `agent-ready`.
- Baton stays flat — it just runs whatever is `agent-ready`. The DAG logic lives entirely in the harness (consistent with D2).

This keeps Baton unchanged and puts ordering policy where policy belongs.

### The non-obvious tension — merge-gating × DAG depth bounds throughput
"Dependency satisfied" should mean **merged to main**, not just "PR opened": issue N's worktree branches from `main`, so it needs issue N-1's code actually merged to build on it. But merges happen at the **human evening checkpoint** (the endpoint the human owns).

Consequence: a sequential chain advances at most one level per review cycle. A mostly-linear 7-issue milestone could take ~7 evenings, because each step waits for you to merge the previous one. Parallelism within a level (4,5,6) collapses that level to one cycle but does nothing for chain *depth*. **Milestone latency ≈ critical-path depth × review cadence — bounded by your review rhythm, not agent speed.** This is the key thing to design around, and it's easy to miss.

### Exploration directions for that tension (not decisions)
- **Stacked branches:** issue N branches off issue N-1's *branch* rather than `main`, so N can start when N-1's PR is *open* (not merged). Decouples chain progress from merge timing; cost is stacked-PR rebase/conflict complexity and reviewing a stack.
- **Scoped auto-merge:** auto-merge on green CI for low-risk intra-milestone issues, so the chain advances unattended. Conflicts with "human owns merge" — would need careful risk-scoping and is a real trust decision.
- **Per-level batch review:** you review/merge a whole ready level in one sitting, making cadence per-level rather than per-issue. Cheapest, purely a workflow habit.

### Other sub-problems to handle
- **Failure/block propagation:** if a blocker blocks or fails, its dependents must *not* be promoted; surface the stalled sub-tree and escalate, rather than silently stalling.
- **Cycle detection:** reject mis-specified cyclic dependency graphs at promotion time.
- **C1 interaction:** the promoter is another writer of `agent-ready` labels — it compounds the single-writer-claim concern (C1). The promoter and any other claim-mutating component must coordinate.
- **C2 interaction:** dependencies must be trusted-owner-set only (provenance), same as issues.

### Pilot workaround (keeps this out of pilot scope)
For the pilot, **you are the scheduler**: manually add `agent-ready` to the next eligible issue(s) as their blockers merge. Functional, fully manual, and defers the entire promoter component — while the merge-gating tension above is still worth being aware of even when promoting by hand.
