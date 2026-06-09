# Harness design

**Status:** Living design document. Starts at pilot scope; evolves as the harness grows. This is the design of the harness *itself* — the policy layer built around the vendored `symphony` orchestrator.

**Vendoring status [implemented]:** `symphony/` (the `mraza007/baton` Python package) is vendored into `src/baton_harness/vendor/symphony/` and called directly as a library (issue #27, P0). §1 and §2 describe the implemented model.

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

**Vendoring decision [implemented]:** `symphony/` (the `mraza007/baton` Python package, MIT licensed, ~1120 lines) is vendored at `src/baton_harness/vendor/symphony/` (issue #27, P0). The harness calls `Orchestrator._run_worker(issue)` directly — no subprocess, no `baton start` invocation. Upstream `mraza007/baton` is frozen/dormant (3 commits, Mar 2026, zero external PRs ever merged); vendoring makes the harness the de facto maintainer of the orchestrator source.

**Relationship to D2:** Decision D2 ("harness is its own repo, not a Baton fork") is not violated — the harness repo remains independent. D2's "fork only as a last resort" framing is superseded: vendoring the source into a subdirectory of the harness package is the selected path, chosen over both external-dependency management and a full fork. D2 as a historical decision record is preserved in [spike-findings.md](./spike-findings.md) with a supersession note.

**Two fixes vendoring unlocked [implemented]:**
1. Thread `env=` through `run_hook` (VP-1, P0) — fixes the `before_run` rebase-onto-main bug; `BH_VENV` also threaded.
2. Re-check `exclude_labels` inside the `_run_worker` turn loop (VP-2, P3) — makes a block terminal, retiring the `max_turns: 2` workaround (see §6 cost note, §8, and `config/WORKFLOW.md`).

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

**Implemented model:** The `baton start -w` subprocess seam is deleted by vendoring (issue #27, P0). The integration model is:

```python
from baton_harness.vendor.symphony.orchestrator import Orchestrator
result = await orchestrator._run_worker(issue)  # returns "pr_created" | "no_pr"
```

The daemon calls `Orchestrator._run_worker(issue)` directly — no global singletons, no poller loop, no subprocess. The `WORKFLOW.md` YAML front-matter is no longer parsed by an external process; its agent prompt body content continues to serve as the instruction template. `BH_VENV` is threaded via the vendored `run_hook env=` (VP-1). The per-project `CLAUDE.md` constraint is unchanged (F11 still holds).

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

> **Post-vendoring [implemented]:** `bin/run.sh` (the `baton start -w` launcher) was deleted when `bin/run-daemon.sh` landed in P3. The `BATON_HARNESS_DIR` export pattern is preserved in `bin/run-daemon.sh` for backward compatibility with existing per-project `CLAUDE.md` setups.

### 4.2 Hooks — `src/baton_harness/` (Python)
Standalone, independently testable Python modules (spike F8 confirmed the testability pattern), each invoked as an entry point and taking the issue number as an argument derived from the worktree path (`basename "$PWD"` — F2: Baton passes no env-var context to hooks) [implemented]. Issue number parsing, GitHub API calls, and JSON handling are all done in Python — no shell grepping of JSON output.

> **Post-vendoring [implemented]:** The vendored `run_hook` (VP-1, P0) now threads `env=` to hook calls, passing `ISSUE_NUMBER` directly. The `basename "$PWD"` workaround is retired.

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
- **Cost note (H-note).** A block costs up to `max_turns` full agent runs in the external-process pilot. The #6 dry run (T2) confirmed that the external-process Baton did not re-check `exclude_labels` between turns. Under vendoring [implemented, VP-2, P3], the `_run_worker` turn-loop patch makes a block terminal — retiring the `max_turns: 2` cost workaround. Issue #23 (tracking this fix) is closed. See §8 for the full terminal-block decision record.
- **Outcome ≠ green CI (F10).** "PR opened" is not "correct." In the pilot, the human is the CI gate at review; automating this is a later phase.

---

## 7. Out of scope for the pilot harness

Explicitly deferred so the pilot stays minimal:

- **Docker containerization** — pilot runs on the host.
- **Slack / comms layer** — core to the model; the daemon escalates to Slack via webhook when `BH_SLACK_WEBHOOK_URL` is set (implemented as a v1 minimal path). Full Bolt bot / Block Kit interactive cards remain deferred to v2.
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

  **Post-vendoring resolution [implemented (VP-2, P3)]:** the terminal-block fix was applied inside the vendored `_run_worker` turn loop — a harness-internal change with no upstream dependency. `max_turns: 2` is retired as a workaround and now set to a value reflecting real work complexity. The "upstream-dependent" framing in older passages is obsolete.

---

## 9. Relationship to the architecture spec

The architecture spec (§3.4) described the orchestration layer abstractly. This document is the concrete realization of that layer. Where they diverge, the spike findings are the tiebreaker — several spec assumptions were corrected by the spike (notably F3 label ownership, F5 outcome states, F9 block-vs-fail). For new-model decisions (vendoring, chain driver), the session-context 2026-06-06 supersedes both spike findings and architecture-spec as the tiebreaker.

---

## 10. Always-on daemon: dependency-ordered work units [implemented (v1, serial)]

**Status:** Implemented as issue #27, P3.  The v1 serial daemon is live in
`src/baton_harness/chain/daemon.py`.  Launcher: `bin/run-daemon.sh`.  CLI
entry point: `bh-daemon` (see `pyproject.toml`).  The implementation spec that
drove P0–P3 was retired after merge (issue #53); its durable design rationale is
preserved in this §10, and the implementation history is in issue #27 and
PRs #46–#49.

### The problem (unchanged)
A milestone is a dependency graph, not a flat bag of independent issues. The flat `agent-ready` model cannot express ordering: mark all issues ready and the agent may attempt N before N-1 exists; mark only issue 1 ready and you are the manual scheduler, defeating the unattended premise.

### Everything is a DAG — unified execution model

There is **one** execution path, parameterized by the DAG. A **work unit** is:
- A **milestone** — all its issues = one DAG → one `feature/<slug>` branch → one draft `feature → main` PR.
- A **single un-milestoned issue** — its own N=1 DAG → its own feature branch → its own PR.

N=1 is the degenerate DAG. The same logic handles both; there is no separate flat-run entry point.

**Work-unit membership = milestone** (OQ-2 resolved). Issues not belonging to any milestone each become their own N=1 work unit.

### Orchestrator/worker split

**Orchestrator = custom always-on daemon.** Owns:
- Poll loop iterating the repo-registry (one entry in v1; repo #2 = append, not rewrite — the multi-repo seam).
- Work-unit detection (milestone label or un-milestoned `agent-ready` issue).
- DAG construction and scheduling (`graphlib.TopologicalSorter` — stdlib; cycle detection free).
- `feature/<slug>` branch creation and lifetime.
- Calling `_run_worker(issue)` for each DAG-ready issue (checking out `feature/<slug>` as HEAD first).
- CI-gated `--no-ff` merge of completed per-issue branches into the feature branch. "Dependency satisfied" = **merged into the feature branch**; not "PR opened"; not "merged to main."
- Sub-tree parking on block or failure; continues independent branches; the daemon never exits on a block.
- Slack escalation (stall summary to `#agent-decisions`) when a sub-tree is parked.
- Draft `feature/<slug> → main` PR when all issues in the DAG complete. The harness never merges to `main`.

Symphony's flat poll/dispatch loop (`run`/`_tick`/`_dispatch`/`_on_worker_done`), `cli.start`, and `watchfiles` are **dropped** — the custom daemon replaces them.

**Worker = vendored `symphony._run_worker`.** Called by the daemon as a library function. Owns:
- Per-issue git worktree creation (`.symphony/worktrees/<N>`, `baton/<slug>-<N>` branches — symphony naming preserved).
- `before_run` hook (rebase onto feature branch, via `CHAIN_BASE_BRANCH` threaded by VP-1).
- `claude -p` turn-loop.
- `after_run` hook (outcome classification, label reconciliation, Dial 2 filtering).
- PR detection and return value (`"pr_created"` | `"no_pr"`).

### BLOCKING resolutions from the architecture review

**B3 dissolved.** With a single daemon and one execution path, there is no flat-run / chain-run coexistence and no label-writer conflict. One daemon; one path; no lock (OQ-8 moot).

**B1 resolved — outcome protocol (no retry in v1).** A `no_pr` / block / failure → park sub-tree + Slack-escalate, full stop. The vendored `state.py` retry/backoff is unused in v1. C3 (bounded rework with escalation) is satisfied by the park + escalate path.

**B2 / B4 resolved — `run_hook env=` (VP-1, P0).** `run_hook` gains an `env=` parameter (vendor patch VP-1). This threads `CHAIN_BASE_BRANCH` to `before_run` (correct rebase target for feature-branch runs) and `BH_VENV` (hook discovery). Both the `before_run` base-ref fix and the `BH_VENV` activation workaround depend on this single patch — it is the P0 prerequisite.

### Open question resolutions

All OQs resolved as of issue #27 (P0–P3):

- **OQ-1 (feature-branch naming):** `feature/<milestone-slug>` for milestone work units; `feature/issue-<N>` for un-milestoned N=1 work units. Issue number keys the N=1 branch (collision-free by construction). `feature/**` CI glob matches both forms.
- **OQ-2 (membership):** milestone defines a work unit; un-milestoned issues are each their own N=1 work unit.
- **OQ-3a (post-merge terminal label):** `agent-merged` — written by the daemon after the CI-gated `--no-ff` merge, removing `agent-done`. The provenance marker + CI-green fact are persisted alongside (see "Crash recovery" below).
- **OQ-4 (CI trigger):** must use the `feature/**` glob; a runtime-parameterized `ci.yml` is incoherent. Extended as a P1 prerequisite.
- **OQ-5 (crash recovery):** auto-reconstruct via `recovery.py` — see "Crash recovery" below.
- **OQ-8 (lock):** moot — single daemon, no concurrent label writers.
- **OQ-9 (unblock detection):** poll in v1 — daemon re-reads `blocking`/labels on each outer-loop tick. Webhook-driven detection is a v2 latency optimization.

### CI green predicate (load-bearing definition)

"CI green" for the `merge.py` CI gate means:

- **Green = every REQUIRED check has `status: completed` and `conclusion` in `{success, neutral, skipped}`.** The required set is the hardcoded `REQUIRED_CHECKS` constant in `merge.py` (`Lint (ruff)`, `Test (pytest)`, `Type check (mypy)`) — this repo exposes **no** branch-protection required-check set (the API returns 404), so per the C-I2 resolution the set lives in code (TODO: wire to `config/WORKFLOW.md`). Optional checks are ignored.
- **`failure` / `cancelled` / `timed_out` / `action_required` on any required check = RED** → park sub-tree + escalate.
- **`queued` / `in_progress` on required checks = NOT YET** → poll with bounded backoff (default 10 s interval, 30 min ceiling); on hard timeout → `CI_TIMEOUT` → RED, never merge on incomplete signal.
- **A required check that never reports at all** is treated as NOT YET, then RED on timeout — no vacuous pass from absent checks.

This definition is the `REQUIRED_CHECKS` constant in `src/baton_harness/chain/merge.py` and the `evaluate_ci` function.

### Crash recovery reconstruction (invariant)

`chain/recovery.py` auto-reconstructs the scheduler `done`/`parked` state on daemon start. This path serves both crash recovery and the live unblock re-entry (§9). Reconstruction rules in precedence order:

1. **`done`** = issues whose per-issue branch merged into `feature/<slug>` via a **daemon-authored** merge commit carrying a recorded provenance marker **AND** a persisted CI-green-at-merge fact (`agent-merged` label + marker comment). A human `git merge` produces no such marker and is not read as done (closes the B-I2 forgeability gap; enforces the C2 provenance allowlist).
2. **`parked`** = issues carrying `blocked` within the work unit's membership, plus their transitive dependents.
3. **Intermediate-state rules:**
   - **(3a) `agent-done` + open PR + no daemon-provenance merge commit** → the agent finished but the CI-gate/merge was interrupted. Re-enter the CI gate (`merge.py`) without re-running `_run_worker`.
   - **(3b) `agent-in-progress` orphan** (crash mid-`_run_worker`) → clear the orphan label; treat the issue as not-yet-dispatched; let `get_ready()` re-dispatch it. Worker is idempotent — re-cuts the worker branch fresh.
4. **Ready frontier** = `get_ready()` after seeding `done`/`parked` from (1)/(2) and routing (3a)/(3b).

### Why this resolves the merge-gating tension

The earlier analysis identified "milestone latency ≈ critical-path depth × review cadence" as the key design tension. The always-on daemon collapses this: intra-DAG merges happen at agent + CI speed; the human reviews exactly once (the final `feature → main` PR). The ~7-evenings latency estimate for a linear 7-issue milestone was the cost of the old "merge to main at each step" model — it does not apply here.

### What the pilot workaround looked like
For the pilot (pre-daemon): manual scheduling — apply `agent-ready` to the next eligible issue(s) as their blockers merge. That workaround is superseded by the always-on daemon.

### Single-repo gate in v1

One daemon per repo. The binding constraint is the GitHub dependency API (`blocked_by`/`blocking`), which is same-repo only; a work unit cannot span repositories by construction. Multi-repo is deferred with two seams:
1. The daemon poll loop iterates a one-entry repo-registry rather than closing over a single `project_root`. Repo #2 = registry append; not a loop rewrite.
2. The concurrency budget (`max_concurrent`) is a documented decision in `WORKFLOW.md`, not an in-daemon code object. A `GlobalBudget` abstraction is a wrong seam here — real enforcement belongs to a future supervising/lease layer. Two daemons each honoring `max_concurrent=2` would allow 4 streams; the seam keeps that honest.

### Sub-problems addressed in the design
- **Failure/block propagation:** halt the sub-tree, continue independent branches, escalate via summary. Block path preserved via `after_run` still firing inside `_run_worker` (Dial 2 intact).
- **Cycle detection:** `graphlib.TopologicalSorter` raises on cycles at construction time.
- **C1 interaction:** daemon is the sole promoter during a run; serial per-DAG execution eliminates concurrent claim races.
- **CI trigger:** must be extended to `feature/**` glob before CI-gated merges into the feature branch can work. This is a prerequisite before the daemon can complete CI-gated merges — captured as an implementation prerequisite for issue #27.
