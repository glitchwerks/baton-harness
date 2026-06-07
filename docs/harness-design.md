# Harness design

**Status:** Living design document. Starts at pilot scope; evolves as the harness grows. This is the design of the harness *itself* — the policy layer built around the vendored `symphony` orchestrator.

**Vendoring status [decided — not yet built]:** `symphony/` (the `mraza007/baton` Python package) will be copied into `src/baton_harness/vendor/symphony/` and called directly as a library. The current pilot runs the existing external-process model; §1 and §2 record the decided target and tag it accordingly.

**Companion docs:** [architecture-spec.md](./architecture-spec.md) (overall system), [spike-findings.md](./spike-findings.md) (what the spike established — referenced here, not restated), [problem-statement.md](./problem-statement.md) (constraints).

---

## Decision — Implementation language: Python (2026-06-04, closes #11)

The harness implementation language is **Python**. This supersedes the shell-script approach used in the spike. `bin/run.sh` (the launcher, issue #1, already merged) **stays shell** — it only resolves the harness root and `exec`s Baton. Everything else — the lifecycle hooks (`after_create`, `before_run`, `after_run`) and all future stateful components — is Python.

**Rationale:**

1. **Python is already in the runtime.** Baton is `pip`-installed (architecture-spec.md §5 — container image contents), so the container has a Python interpreter regardless. A Python harness adds zero new dependency.

2. **Matches the project's toolchain and standards.** The project's Python standards (PEP 8, type hints, Google docstrings, `uv`, pytest) apply directly. No equivalent tooling exists for shell here.

3. **The load-bearing components are error-prone in bash.** The outcome router parses `gh --json` output; the spike's closed shell implementation (PR #9) grepped JSON rather than parsing it. The router is explicitly "production code, not glue" (open-questions.md S2.1), and the future async CI-trigger carries the C1/C2/C3 concerns (spike-findings.md — "design it as a real component, not a webhook one-liner"). Proper JSON parsing, data structures, and pytest coverage matter there.

4. **Matches the vendored `symphony` source language.** `symphony` is Python; shared tooling (ruff, mypy, pytest) applies uniformly across harness and vendor tree. (The original rationale was "aids the D2 contribute-upstream path" — superseded by the vendoring decision; see §1.)

**Continuity note:** spike finding F8 — "hooks call standalone, independently testable script files taking the issue number as an argument" — still holds in spirit. The scripts are now Python modules/entry points rather than `.sh` files, but the testability principle is unchanged.

---

## 1. What the harness is

A standalone, version-controlled repo that holds the *reusable policy and tooling* around the orchestrator: the agent prompt, the lifecycle hook scripts, per-project config, context templates, and the launcher. The orchestration engine (`symphony`) is vendored into the repo and called directly as a library; the harness is what makes it do the right thing.

**Vendoring decision [decided — not yet built]:** `symphony/` (the `mraza007/baton` Python package, MIT licensed, ~1120 lines) will be copied into `src/baton_harness/vendor/symphony/`. The harness calls `Orchestrator._run_worker(issue)` directly — no subprocess, no `baton start` invocation. Upstream `mraza007/baton` is frozen/dormant (3 commits, Mar 2026, zero external PRs ever merged); vendoring makes the harness the de facto maintainer of the orchestrator source.

**Relationship to D2:** Decision D2 ("harness is its own repo, not a Baton fork") is not violated — the harness repo remains independent. D2's "fork only as a last resort" framing is superseded: vendoring the source into a subdirectory of the harness package is the selected path, chosen over both external-dependency management and a full fork. D2 as a historical decision record is preserved in [spike-findings.md](./spike-findings.md) with a supersession note.

**Two fixes vendoring unlocks [decided — not yet built]:**
1. Thread `env=` through `run_hook` — fixes the `before_run` rebase-onto-main bug (workaround currently in place in `before_run.py`).
2. Re-check `exclude_labels` inside the `_run_worker` turn loop — makes a block terminal, retiring the `max_turns: 2` workaround (see §6 cost note, §8, and `config/WORKFLOW.md`).

Issue #23 (terminal-block / upstream-dependency framing) is **closed**: the workaround merged in PR #26; the root-cause fix is harness-internal post-vendoring, with no remaining upstream dependency.

---

## 2. Integration model

**Current pilot [implemented]:** The external-process model (validated in the spike and pilot — see spike-findings F11 and pilot-validation-findings.md). Baton runs project-local; the harness config lives here and is pointed at via `baton start -w`:

```bash
cd <project-repo> && baton start -w /agent-harness/config/WORKFLOW.md
```

- The harness repo owns the **hook entry points** and the **WORKFLOW.md** (passed via `-w`).
- The **project repo** carries only its own committed `CLAUDE.md` (Claude Code discovers it from the worktree; not relocatable — F11) and its CI workflow (a precondition, not the harness's job).
- `bin/run.sh` encapsulates the `cd` + `-w` invocation per project.

**Decided target [decided — not yet built]:** The `baton start -w` subprocess seam is deleted by vendoring. The integration model becomes:

```python
from baton_harness.vendor.symphony.orchestrator import Orchestrator
result = await orchestrator._run_worker(issue)  # returns "pr_created" | "no_pr"
```

The harness (or chain driver) calls `Orchestrator._run_worker(issue)` directly — no global singletons, no poller loop, no subprocess. The `WORKFLOW.md` YAML front-matter is no longer parsed by an external process; its agent prompt body content continues to serve as the instruction template. The `BH_VENV` activation workaround in hooks becomes retireable once the vendored `run_hook` passes `env=` directly. The per-project `CLAUDE.md` constraint is unchanged (F11 still holds).

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

### 4.1 Launcher — `bin/run.sh` (shell — stays shell) [implemented]
Encapsulates the project-local invocation so it isn't retyped or misremembered. Resolves the harness directory, `cd`s into the target project, and starts Baton pointed at that project's config. Exports `BATON_HARNESS_DIR` so hook entry points can locate the package without hardcoding a path. This launcher is a thin shell wrapper (`exec baton …`) and is not part of the Python package — it was implemented in issue #1 and is already merged.

> **Post-vendoring [decided — not yet built]:** `bin/run.sh` in its current `baton start -w` form becomes obsolete. The entry point for the chain driver or direct harness invocation will replace it; the `BATON_HARNESS_DIR` export pattern may be retained for backward compatibility with existing per-project `CLAUDE.md` setups.

### 4.2 Hooks — `src/baton_harness/` (Python)
Standalone, independently testable Python modules (spike F8 confirmed the testability pattern), each invoked as an entry point and taking the issue number as an argument derived from the worktree path (`basename "$PWD"` — F2: Baton passes no env-var context to hooks) [implemented]. Issue number parsing, GitHub API calls, and JSON handling are all done in Python — no shell grepping of JSON output.

> **Post-vendoring [decided — not yet built]:** The `basename "$PWD"` workaround is retireable once the vendored `run_hook` is patched to thread `env=` through to hook calls — at which point `ISSUE_NUMBER` can be passed directly. The workaround remains in place until then.

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
- **Cost note (H-note).** A block costs up to `max_turns` full agent runs [implemented with `max_turns: 2` workaround]. The #6 dry run (T2) confirmed that the external-process Baton does not re-check `exclude_labels` between turns, so a blocked issue burns through its remaining turns before settling. Keep `max_turns` modest in the current pilot. Under vendoring [decided — not yet built], the `_run_worker` turn-loop patch makes a block terminal — retiring the `max_turns: 2` cost workaround. Issue #23 (tracking this fix) is closed; the upstream-dependency framing that was its premise no longer applies. See §8 for the full terminal-block decision record.
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

  **The block is not terminal at the external-process Baton level [implemented state].** The #6 dry run established that `exclude_labels` is evaluated at poll time only; Baton does not re-check it between turns within an active run, and `before_run` fires once per run, not per turn. A blocked issue therefore consumes up to `max_turns` full agent invocations before settling — not one (pilot-validation-findings.md §Finding 5, T2 log timestamps).

  **Pilot decision [implemented]:** accept the block-cost ≈ `max_turns` as a known, bounded cost. `max_turns: 2` in `config/WORKFLOW.md` is the workaround. Issue #23 tracked this fix; it was closed (PR #26) on the workaround.

  **Post-vendoring resolution [decided — not yet built]:** under vendoring, the terminal-block fix is ~10 lines inside the vendored `_run_worker` turn loop — a harness-internal change with no upstream dependency. Once applied, `max_turns: 2` can be raised to a value reflecting real work complexity. The "upstream-dependent" framing in older passages is obsolete; do not propagate it.

---

## 9. Relationship to the architecture spec

The architecture spec (§3.4) described the orchestration layer abstractly. This document is the concrete realization of that layer. Where they diverge, the spike findings are the tiebreaker — several spec assumptions were corrected by the spike (notably F3 label ownership, F5 outcome states, F9 block-vs-fail). For new-model decisions (vendoring, chain driver), the session-context 2026-06-06 supersedes both spike findings and architecture-spec as the tiebreaker.

---

## 10. Chain-driver orchestration: dependency-ordered milestones

**Status [decided — not yet built]:** The design is decided and being implemented as issue #27. This section records the decided shape. For the full spec, see [docs/superpowers/specs/dependency-chain-orchestration.md](../docs/superpowers/specs/dependency-chain-orchestration.md).

### The problem (unchanged)
A milestone is a dependency graph, not a flat bag of independent issues. The flat `agent-ready` model cannot express ordering: mark all issues ready and the agent may attempt N before N-1 exists; mark only issue 1 ready and you are the manual scheduler, defeating the unattended premise.

### The decided design — chain driver (#27)

A **chain driver** component owns a `feature/<slug>` branch. Per-issue branches are cut off the feature branch (not `main`). The driver:

1. Reads the DAG from GitHub's native `blocked_by` / `blocking` dependency API (`gh api` — same-repo only; not exposed by MCP).
2. Schedules execution using `graphlib.TopologicalSorter` (stdlib — zero new dependencies; free cycle detection).
3. Calls `Orchestrator._run_worker(issue)` directly for each ready issue (vendored `symphony` — no Baton poller to hand off to).
4. Merges completed per-issue branches back into the feature branch with `--no-ff` (not squash — avoids ghost diffs in dependent branches). "Dependency satisfied" = **merged into the feature branch**, not "PR opened" and not "merged to main."
5. Halts the affected sub-tree on block or failure; continues independent branches; escalates via summary.
6. When all issues complete: opens one draft `feature/<slug> → main` PR for human review. The harness never merges to `main`.

**Serial in v1** (cleanest C1 mitigation). `before_run`'s rebase target is the feature branch, not `main`, for chain branches.

### Why this resolves the merge-gating tension

The earlier analysis in this section identified "milestone latency ≈ critical-path depth × review cadence" as the key design tension. The chain driver collapses this: intra-chain merges happen at agent + CI speed; the human reviews exactly once (the final `feature → main` PR). The ~7-evenings latency estimate for a linear 7-issue milestone was the cost of the old "merge to main at each step" model — it does not apply to the chain driver design.

### What the pilot workaround looked like
For the pilot (pre-chain driver): manual scheduling — apply `agent-ready` to the next eligible issue(s) as their blockers merge. That workaround is superseded by the chain driver.

### Sub-problems addressed in the design
- **Failure/block propagation:** halt the sub-tree, continue independent branches, escalate via summary. Block path preserved via `after_run` still firing inside `_run_worker` (Dial 2 intact).
- **Cycle detection:** `graphlib.TopologicalSorter` raises on cycles at construction time.
- **C1 interaction:** chain driver is the sole promoter during a chain run; serial execution eliminates concurrent claim races.
- **CI trigger:** currently fires only on `main`; must be extended to feature branches before chain branches can have CI-gated merges. (This is the one BLOCKING that survives option (c) — see project-reviewer findings on issue #27.)
