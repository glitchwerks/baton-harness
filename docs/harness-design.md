# Harness design

**Status:** Living design document. Starts at pilot scope; evolves as the harness grows. This is the design of the harness *itself* — the policy layer that drives Baton. Baton is an upstream dependency, not part of this repo (decision D2).

**Companion docs:** [architecture-spec.md](./architecture-spec.md) (overall system), [spike-findings.md](./spike-findings.md) (what the spike established — referenced here, not restated), [problem-statement.md](./problem-statement.md) (constraints).

---

## Decision — Implementation language: Python (2026-06-04, closes #11)

The harness implementation language is **Python**. This supersedes the shell-script approach used in the spike. `bin/run.sh` (the launcher, issue #1, already merged) **stays shell** — it only resolves the harness root and `exec`s Baton. Everything else — the lifecycle hooks (`after_create`, `before_run`, `after_run`) and all future stateful components — is Python.

**Rationale:**

1. **Python is already in the runtime.** Baton is `pip`-installed (architecture-spec.md §5 — container image contents), so the container has a Python interpreter regardless. A Python harness adds zero new dependency.

2. **Matches the project's toolchain and standards.** The project's Python standards (PEP 8, type hints, Google docstrings, `uv`, pytest) apply directly. No equivalent tooling exists for shell here.

3. **The load-bearing components are error-prone in bash.** The outcome router parses `gh --json` output; the spike's closed shell implementation (PR #9) grepped JSON rather than parsing it. The router is explicitly "production code, not glue" (open-questions.md S2.1), and the future async CI-trigger carries the C1/C2/C3 concerns (spike-findings.md — "design it as a real component, not a webhook one-liner"). Proper JSON parsing, data structures, and pytest coverage matter there.

4. **Aids the D2 "contribute upstream to Baton" path.** Baton is itself Python.

**Continuity note:** spike finding F8 — "hooks call standalone, independently testable script files taking the issue number as an argument" — still holds in spirit. The scripts are now Python modules/entry points rather than `.sh` files, but the testability principle is unchanged.

---

## 1. What the harness is

A standalone, version-controlled repo that holds the *reusable policy and tooling* around the orchestrator: the agent prompt, the lifecycle hook scripts, per-project config, context templates, and the launcher. Baton (the orchestrator) is installed as a dependency; the harness is what makes it do the right thing.

Decision D2: this is its own repo, **not** a Baton fork. Fork only as a last resort, after "work around it" and "contribute upstream" are exhausted.

---

## 2. Integration model — point-at-path

Validated against Baton's docs (see spike-findings F11). Baton runs project-local — launched from the project directory — but its config can live elsewhere and be pointed at:

```bash
cd <project-repo> && baton start -w /agent-harness/config/WORKFLOW.md
```

- The harness repo owns the **hook entry points** (invoked by the WORKFLOW.md hooks configuration via absolute path) and the **WORKFLOW.md** (passed via `-w`).
- The **project repo** carries only its own committed `CLAUDE.md` (Claude Code discovers it from the worktree; not relocatable — F11) and its CI workflow (a precondition, not the harness's job).
- A launcher in the harness encapsulates the `cd` + `-w` invocation per project.

This keeps the harness as the single source of truth for everything shareable, with the smallest possible footprint in each project repo.

---

## 3. Repo structure (pilot scope)

Deliberately minimal. Grows without restructuring as later phases are added. The Python package foundation is tracked in issue #10.

```
agent-harness/
├── README.md
├── pyproject.toml               # package metadata, dependencies, ruff/mypy config
├── bin/
│   └── run.sh                  # launcher (shell): resolve harness root, exec baton — stays shell
├── src/
│   └── baton_harness/          # installable Python package
│       ├── __init__.py
│       ├── after_create.py     # per-worktree dependency install (npm/pip) — Baton after_create hook
│       ├── before_run.py       # branch sync onto main
│       └── after_run.py        # outcome classification + label reconciliation
├── tests/                      # pytest suite
│   └── test_after_run.py
├── config/
│   └── WORKFLOW.md              # hooks → Python entry points; the agent prompt
├── templates/
│   └── CLAUDE.md.template       # source for each project's committed CLAUDE.md
└── docs/                        # references to spec, findings
```

**Project repo carries:** its own committed `CLAUDE.md` (sourced from the template) and the CI workflow.

**CI gate:** ruff (lint + format), mypy (type checks), pytest — enforced via `.github/workflows/ci.yml`. Replaces shellcheck from the spike approach.

**Evolution path (not built for the pilot):** project #2 introduces `config/<name>/` per-project subdirectories (currently a single flat `config/WORKFLOW.md` — YAGNI until a second project appears); containerization adds a `Dockerfile`; the comms layer adds `bot/`; the async CI handling adds a `triggers/` component.

---

## 4. Components

### 4.1 Launcher — `bin/run.sh` (shell — stays shell)
Encapsulates the project-local invocation so it isn't retyped or misremembered. Resolves the harness directory, `cd`s into the target project, and starts Baton pointed at that project's config. Exports `BATON_HARNESS_DIR` so hook entry points can locate the package without hardcoding a path. This launcher is a thin shell wrapper (`exec baton …`) and is not part of the Python package — it was implemented in issue #1 and is already merged.

### 4.2 Hooks — `src/baton_harness/` (Python)
Standalone, independently testable Python modules (spike F8 confirmed the testability pattern), each invoked as an entry point and taking the issue number as an argument derived from the worktree path (`basename "$PWD"` — F2: Baton passes no env-var context to hooks). Issue number parsing, GitHub API calls, and JSON handling are all done in Python — no shell grepping of JSON output.

- **`after_create.py`** — runs once after worktree creation. Per-worktree dependency setup (`npm install` / `pip install`). Partial mitigation for the worktree-isolation limits (S2.4); does not solve shared ports/services.
- **`before_run.py`** — syncs the worktree branch onto latest `main` before the agent runs.
- **`after_run.py`** — the outcome router. Classifies what the run produced (the states from F5: `uncommitted-changes`, `no-commits`, `committed-no-pr`, `pr-opened`) and reconciles GitHub labels to a single state. Must finish under the 60s hook timeout (F11). Parses `gh --json` output via Python's `json` module rather than grepping (addresses the pattern in PR #9's spike implementation).

Each module is covered by pytest and passes ruff and mypy before merge.

### 4.3 Config — `config/WORKFLOW.md`
Single generic Baton config (flattened from `config/<project>/` — YAGNI per issue #5): tracker labels, concurrency, `max_turns`, `permission_mode: bypassPermissions` (F11/F4), the `after_create`/`before_run`/`after_run` hook wiring (entry points in `src/baton_harness/`), and the agent prompt body. The prompt uses the mechanical, numbered closing-steps pattern proven necessary in the spike (F4) and the explicit confidence/block rule (F6/F9). Per-project `config/<name>/` subdirectories are introduced when a second project appears.

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

Reconciliation is enforced in `after_run.py` to maintain a single state label (the H1 bug — both `agent-ready` and `blocked` present — is the open implementation issue to fix here).

---

## 6. Inherited design constraints

These come from the spike and must be honoured by the harness as it grows. They are *not* re-argued here; see spike-findings for the reasoning.

- **C1 — single-writer claim authority.** When the async CI/review layer is added, exactly one component may mutate claim/state. (Deferred — not in pilot.)
- **C2 — provenance allowlist.** The harness acts only on agent-authored branches/PRs and owner-labeled issues; never on arbitrary-author content. (Deferred — not in pilot, since the pilot has no event-driven trigger.)
- **C3 — bounded rework with escalation.** Every autonomous retry loop needs a budget and a human-escalation exit. (Deferred — pilot reviews PRs manually.)
- **Cost note (H-note).** A block costs up to `max_turns` full agent runs. The #6 dry run (T2) confirmed that Baton does not re-check `exclude_labels` between turns, so a blocked issue burns through its remaining turns before settling. Keep `max_turns` modest. The terminal-block fix is deferred as upstream-dependent (issue #23); see §8 for the full decision record.
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
- ~~**[test] Block cost:** does Baton's continuation retry respect `exclude_labels: ["blocked"]` and stop after the first blocked turn, or burn all `max_turns`?~~ **Resolved — #6 dry run (T2).** `exclude_labels` is checked at poll time only; Baton does not halt an in-flight run. Block costs up to `max_turns`. See §8 terminal-block decision and pilot-validation-findings.md finding 5.
- **[design] Script path resolution:** ~~do hooks hardcode the harness path, or read it from an env var exported by the launcher?~~ **Resolved.** The launcher (`bin/run.sh`, issue #1) exports `BATON_HARNESS_DIR`; hook entry points read it from the environment. Hardcoding is no longer needed.
- **[design] CLAUDE.md sync:** how does the template become the project's committed CLAUDE.md — manual copy for the pilot, or a small generate step? Manual is fine for one project.
- **[design] H1 fix — terminal-block decision (2026-06-06, closes AC3 of #4):** The block path is implemented: `after_run` enforces the single-state invariant (Priority 1 in `_reconcile_labels` — removes `agent-ready`, leaves `blocked`). This was validated live in the #6 dry run (T2, pilot-validation-findings.md finding 5).

  **The block is not terminal at the Baton level.** The #6 dry run established that `exclude_labels` is evaluated at poll time only; Baton does not re-check it between turns within an active run, and `before_run` fires once per run, not per turn. A blocked issue therefore consumes up to `max_turns` full agent invocations before settling — not one (pilot-validation-findings.md §Finding 5, T2 log timestamps).

  **The harness cannot make the block terminal on its own.** Doing so requires either an upstream Baton change (post-turn re-check of `exclude_labels`, or a per-turn hook point) or a harness-side mitigation (a `before_run` no-op guard that exits early when `blocked` is already present — cheap, but depends on `before_run` firing per turn, which it does not in current Baton). Neither path is currently available without an upstream change.

  **Pilot decision:** accept the block-cost ≈ `max_turns` as a known, bounded cost. `max_turns` is already kept modest for this reason (§6 cost note). The terminal-block fix is deferred as upstream-dependent and tracked in issue #23.

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
