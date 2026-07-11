# Harness design

**Status:** Living design document. Starts at pilot scope; evolves as the harness grows. This is the design of the harness *itself* — the policy layer built around the vendored `symphony` orchestrator.

**Vendoring status [implemented]:** `symphony/` (the `mraza007/baton` Python package) is vendored into `src/baton_harness/vendor/symphony/` and called directly as a library (issue #27, P0). §1 and §2 describe the implemented model.

**Companion docs:** [architecture-spec.md](./architecture-spec.md) (overall system). The spike findings that ground design decisions are inlined in [§ Decision records](#decision-records) below. Project constraints are in [§ Constraints](#constraints) below.

---

## Decision — Implementation language: Python (2026-06-04, closes #11)

The harness implementation language is **Python**. This supersedes the shell-script approach used in the spike. `bin/run-daemon.sh` (the launcher, originally `bin/run.sh` issue #1; superseded by `bin/run-daemon.sh` under vendoring — see §4.1) **stays shell** — it resolves the harness root, runs an env/label preflight, and `exec`s the `bh-daemon` entry point. Everything else — the lifecycle hooks (`after_create`, `before_run`, `after_run`) and all future stateful components — is Python.

**Rationale:**

1. **Python is already in the runtime.** Baton is `pip`-installed (architecture-spec.md §5 — container image contents), so the container has a Python interpreter regardless. A Python harness adds zero new dependency.

2. **Matches the project's toolchain and standards.** The project's Python standards (PEP 8, type hints, Google docstrings, `uv`, pytest) apply directly. No equivalent tooling exists for shell here.

3. **The load-bearing components are error-prone in bash.** The outcome router parses `gh --json` output; the spike's closed shell implementation (PR #9) grepped JSON rather than parsing it. The router is explicitly "production code, not glue" (open-questions.md S2.1), and the future async CI-trigger carries the C1/C2/C3 concerns (see [§ Constraints — Design concerns C1–C3](#constraints) — "design it as a real component, not a webhook one-liner"). Proper JSON parsing, data structures, and pytest coverage matter there.

4. **Matches the vendored `symphony` source language.** `symphony` is Python; shared tooling (ruff, mypy, pytest) applies uniformly across harness and vendor tree. (The original rationale was "aids the D2 contribute-upstream path" — superseded by the vendoring decision; see §1.)

**Continuity note:** spike finding F8 — "hooks call standalone, independently testable script files taking the issue number as an argument" — still holds in spirit. The scripts are now Python modules/entry points rather than `.sh` files, but the testability principle is unchanged.

---

## 1. What the harness is

A standalone, version-controlled repo that holds the *reusable policy and tooling* around the orchestrator: the agent prompt, the lifecycle hook scripts, per-project config, context templates, and the launcher. The orchestration engine (`symphony`) is vendored into the repo and called directly as a library; the harness is what makes it do the right thing.

**Vendoring decision [implemented]:** `symphony/` (the `mraza007/baton` Python package, MIT licensed, ~1120 lines) is vendored at `src/baton_harness/vendor/symphony/` (issue #27, P0). The harness calls `Orchestrator._run_worker(issue)` directly — no subprocess, no `baton start` invocation. Upstream `mraza007/baton` is frozen/dormant (3 commits, Mar 2026, zero external PRs ever merged); vendoring makes the harness the de facto maintainer of the orchestrator source.

**Relationship to D2:** Decision D2 ("harness is its own repo, not a Baton fork") is not violated — the harness repo remains independent. D2's "fork only as a last resort" framing is superseded: vendoring the source into a subdirectory of the harness package is the selected path, chosen over both external-dependency management and a full fork. D2 as a historical decision record is preserved in [§ Decision records — D2](#d2--harness-is-its-own-repo-not-a-baton-fork) with a supersession note.

**Two fixes vendoring unlocked [implemented]:**
1. Thread `env=` through `run_hook` (VP-1, P0) — fixes the `before_run` rebase-onto-main bug; `BH_VENV` also threaded.
2. Re-check `exclude_labels` inside the `_run_worker` turn loop (VP-2, P3) — makes a block terminal, retiring the `max_turns: 2` workaround (see §6 cost note, §8, and `config/WORKFLOW.md`).

Issue #23 (terminal-block / upstream-dependency framing) is **closed**: the workaround merged in PR #26; the root-cause fix is harness-internal post-vendoring, with no remaining upstream dependency.

---

## 2. Integration model

**Historical pilot (superseded by vendoring):** The external-process model (validated in the spike and pilot — see [§ Decision records — F11](#f11--baton-capabilities-from-docs) and issue #6 dry-run results). Baton runs project-local; the harness config lives here and is pointed at via `baton start -w`:

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

### 4.1 Launcher — `bin/run-daemon.sh` (shell — active launcher) [implemented]
Starts the always-on daemon. Validates that `BH_REPO_OWNER`, `BH_REPO_NAME`, and `BH_PROJECT_ROOT` are set, runs label and sandbox preflight checks, exports `BATON_HARNESS_DIR` so hook entry points can locate the package without hardcoding a path, then invokes the `bh-daemon` Python entry point. This launcher is a thin shell wrapper and is not part of the Python package.

> **Historical note:** The original `bin/run.sh` encapsulated the `baton start -w` external-process invocation (issue #1). It was deleted when `bin/run-daemon.sh` landed in P3 (vendoring). The `BATON_HARNESS_DIR` export pattern is preserved in `bin/run-daemon.sh` for backward compatibility with existing per-project `CLAUDE.md` setups.

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

Reconciliation is enforced in `after_run.py` to maintain a single state label (the H1 bug — both `agent-ready` and `blocked` present — is the open implementation issue to fix here). `_reconcile_labels` is idempotent: re-running it against any label set, including a torn or zero-state set left by a mid-run kill, converges to the correct single state (#31). A pure helper `labels.target_state_from_observed(blocked, pr_open) -> str` re-derives the target single-state label from observable facts independent of which hook last ran (#31).

---

## 6. Inherited design constraints

These come from the spike and must be honoured by the harness as it grows. The full reasoning is in [§ Constraints — Design concerns C1–C3](#c1--single-writer-claim-authority-multi-writer-race) below.

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

- ~~**[test] Absolute `-w` path:** confirm `baton start -w <absolute-path-outside-project>` works (docs show only a relative example). ~2 min.~~ **Obsolete under vendoring** — `baton start -w` is retired; the active launcher is `bin/run-daemon.sh`.
- ~~**[test] Block cost:** does Baton's continuation retry respect `exclude_labels: ["blocked"]` and stop after the first blocked turn, or burn all `max_turns`?~~ **Resolved — #6 dry run (T2).** `exclude_labels` is checked at poll time only; Baton does not halt an in-flight run. Block costs up to `max_turns`. See §8 terminal-block decision (issue #6, finding 5).
- **[design] Script path resolution:** ~~do hooks hardcode the harness path, or read it from an env var exported by the launcher?~~ **Resolved.** The launcher (`bin/run-daemon.sh`) exports `BATON_HARNESS_DIR`; hook entry points read it from the environment. Hardcoding is no longer needed.
- **[design] CLAUDE.md sync:** how does the template become the project's committed CLAUDE.md — manual copy for the pilot, or a small generate step? Manual is fine for one project.
- **[design] H1 fix — terminal-block decision (2026-06-06, closes AC3 of #4):** The block path is implemented: `after_run` enforces the single-state invariant (Priority 1 in `_reconcile_labels` — removes `agent-ready`, leaves `blocked`). This was validated live in the #6 dry run (T2; issue #6 finding 5).

  **The block is not terminal at the external-process Baton level [implemented state].** The #6 dry run established that `exclude_labels` is evaluated at poll time only; Baton does not re-check it between turns within an active run, and `before_run` fires once per run, not per turn. A blocked issue therefore consumes up to `max_turns` full agent invocations before settling — not one (issue #6 §Finding 5, T2 log timestamps).

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
- A **milestone** — all its issues = one DAG → one `feature/<slug>` branch → one ready-for-review `feature → main` PR.
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
- Ready-for-review `feature/<slug> → main` PR when all issues in the DAG complete. The harness never merges to `main`.

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

- **Green = every REQUIRED check has `status: completed` and `conclusion` in `{success, neutral, skipped}`.** The required set defaults to the `REQUIRED_CHECKS` constant in `merge.py` (`Lint (ruff)`, `Test (pytest)`, `Type check (mypy)`) — this repo exposes **no** branch-protection required-check set (the API returns 404), so per the C-I2 resolution the set lives outside branch protection. As of #225 (closed 2026-07-06, vendor-patch VP-8) it is operator-configurable via a top-level `required_checks:` key in `config/WORKFLOW.md` (`WorkflowConfig.required_checks` in `config.py`, resolved through `daemon._effective_required_checks`); the hardcoded constant is now only the fallback when no override is present. Optional checks are ignored.
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
   - **(3b) `agent-in-progress` orphan** (crash mid-`_run_worker`) → clear the orphan label; treat the issue as not-yet-dispatched; let `get_ready()` re-dispatch it. Worker is idempotent — re-cuts the worker branch fresh. Torn label state from a kill between the `agent-ready` remove and the `agent-done` add (the #31 failure mode) is handled by the single-state backstop: when it detects zero state labels with an open PR and no `blocked` label, it converges directly to `agent-done` via `labels.target_state_from_observed` (rather than parking) and falls through to the in-tick CI gate (`worker_result == "pr_created"` → `merge_issue_branch`), merging the PR in the same tick; all other violations still alert and park (#31, PR #95).
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

---

## 11. Daemon liveness monitoring [implemented — issues #79, #33]

The heartbeat thread (`src/baton_harness/chain/heartbeat.py`) provides two independent liveness signals on every tick. The nominal cadence is 30 s (`_DEFAULT_HEARTBEAT_CADENCE_S`); when `BH_HEARTBEAT_PING_URL` is set, the actual interval between consecutive ping arrivals is 30 s plus the ping's own latency, because the ping runs synchronously as the last step of each tick.

### Two monitoring modes

**Mode A — local heartbeat file (`BH_HEARTBEAT_FILE`)**

On each tick the daemon writes an ISO-8601 UTC timestamp to a file. The default path is `${BH_PROJECT_ROOT}/.baton-harness/heartbeat`; override with `BH_HEARTBEAT_FILE` (absolute path). An external process — a cron job, a container health-check, or a file-age alert — can compare the file's modification time or contents against a threshold to determine whether the daemon is alive.

When to use: local deployments where a network endpoint is unavailable or unwanted; as a complement to the webhook ping (both signals are always written regardless of whether Mode B is configured).

**Mode B — external dead-man's-switch webhook ping (`BH_HEARTBEAT_PING_URL`)**

When `BH_HEARTBEAT_PING_URL` is set, the daemon issues a best-effort HTTP GET to that URL on every tick. If the env var is unset, no ping is sent and Mode A is the only liveness signal. The mechanism is compatible with any Healthchecks.io-style ping-URL service (`obs_config.py` line 233: `os.environ.get("BH_HEARTBEAT_PING_URL") or None`).

When to use: unattended server deployments where you want an alert delivered without polling a local filesystem; the hosted service handles the alerting logic and silencing.

### Wiring an external monitor (Healthchecks.io example)

1. Create a new check at <https://healthchecks.io>. Set the **Period** to 30 s (matching `_DEFAULT_HEARTBEAT_CADENCE_S`). Set the **Grace** period — see "Staleness threshold guidance" below.
2. Copy the check's ping URL (e.g. `https://hc-ping.com/<uuid>`).
3. Export it before starting the daemon:
   ```bash
   export BH_HEARTBEAT_PING_URL=https://hc-ping.com/<uuid>
   bin/run-daemon.sh
   ```
4. The daemon will GET that URL once per heartbeat tick (nominally every 30 s; the actual interval is 30 s plus the ping's own latency). When pings stop arriving, the service fires its configured alarm after the grace period expires.

The same approach works with any service that accepts a GET ping and alarms on silence: UptimeRobot (heartbeat monitor type), Better Uptime (heartbeat), or a self-hosted equivalent.

### Staleness threshold guidance

Set the external monitor's expected-period to the heartbeat cadence:

| Parameter | Recommended value | Derivation |
|---|---|---|
| Expected period | 30 s | `_DEFAULT_HEARTBEAT_CADENCE_S` (`heartbeat.py` line 51) |
| Grace / alert threshold | 105 s (≈ 3 × 30 s + 15 s margin) | 3 missed beats + one ping-timeout (`_DEFAULT_PING_TIMEOUT_S` = 5 s, `heartbeat.py` line 55) + a 15 s fudge for system scheduling jitter; the grace window naturally absorbs the extra latency added by synchronous pings |

A threshold of one or two missed beats produces spurious alarms from ordinary scheduling jitter or transient network delays. Three missed beats (90 s) plus a 15 s margin (105 s) is a practical minimum before declaring the daemon dead.

### Failure semantics

**Ping failures are non-fatal.** A transient network error, DNS timeout, or unreachable endpoint is logged as a WARNING and swallowed (`heartbeat.py` lines 365–368). The heartbeat thread continues. The 5 s timeout (`_DEFAULT_PING_TIMEOUT_S`) bounds normal HTTP socket waits; it does not cover all of URL handling (DNS resolution, TLS negotiation, redirects), so the actual delay from a slow ping can exceed the timeout in edge cases. Because the ping is the last step of `_heartbeat_tick` (after stall-alert detection), a slow ping can only delay the next tick's start — stall alerting is never affected, and the 105 s grace threshold readily absorbs the added jitter. A silent ping failure therefore means only that the external service missed one check-in, not that the daemon has crashed. Verify against the local heartbeat file before treating a single missed ping as an incident.

**Local file failures are also non-fatal.** A filesystem error during `_write_heartbeat` is likewise logged and swallowed (`heartbeat.py` lines 266–269); the thread continues. On Windows, `os.replace` is not guaranteed atomic — a crash between the write and the rename may leave an absent or partial file. External monitors must treat a missing or unreadable heartbeat file as *stale*, not as a parse error. The `_write_heartbeat` docstring (`heartbeat.py` lines 133–143) records this caveat.

**Summary of the two-signal interpretation:**

| Local file fresh | Ping arriving | Interpretation |
|---|---|---|
| Yes | Yes | Daemon healthy |
| Yes | No | Transient ping failure — likely network; wait one more interval before alarming |
| No | Yes | File write failed — unusual; check daemon logs |
| No | No | Likely crash or process exit — treat as down |

### Progress-bound stall detection (worker-active phase) [issue #33]

Issue #33 (P2) added a second stall predicate that is sensitive to per-turn worker progress rather than only wall-clock time. This is implemented alongside the existing wall-clock predicate inside `_heartbeat_tick` (`heartbeat.py` lines 395–464).

**Two logical phases controlled by `_worker_active`**

The `LivenessState` dataclass (`heartbeat.py` lines 63–153) carries a `_worker_active` boolean field (line 89) that the daemon sets at the two `mark_in_progress` call sites to encode which logical phase an issue is in:

| Phase | `worker_active=` | Set at | Stall predicates active |
|---|---|---|---|
| WORKER_ACTIVE | `True` | Fresh dispatch (`daemon.py` line 993) | Wall-clock AND progress-bound |
| NON_WORKER | `False` | CI-gate re-entry (`daemon.py` line 859) | Wall-clock only |

**WORKER_ACTIVE phase** (`worker_active=True`, set by fresh dispatch at `daemon.py` line 993): the issue is actively running Claude Code worker turns. In this phase both stall predicates apply. The progress-bound predicate fires when `(now - last_progress_at) > obs.worker_progress_stall_s` (`heartbeat.py` lines 401–415), where `last_progress_at` is stamped by the VP-3 `progress_cb` hook at each turn-loop entry in the vendored orchestrator (`orchestrator.py` lines 154–165; `note_progress` is `heartbeat.py` lines 141–153). The default threshold is 1800 s (6× the 300 s per-turn timeout; derivation: `max_retry_backoff_ms=300_000` at `config.py` line 31; see `obs_config.py` lines 90–91). Override with `BH_WORKER_PROGRESS_STALL_S` (`obs_config.py` lines 59–64, 261–274).

**NON_WORKER phase** (`worker_active=False`, set by `ci_gate_reentry` at `daemon.py` line 859): the issue is waiting on a CI gate poll. No worker turns occur, so `progress_cb` never fires — `last_progress_at` is not updated. The progress-bound predicate is explicitly gated off in this phase (`heartbeat.py` line 401: `if worker_active_snap and ...`) so a CI poll wait, which can block up to `ci_timeout` seconds, can never false-fire a progress-stall alert (IS-1).

**Wall-clock backstop (`heartbeat_stall_s`, default 7200 s)**

The original wall-clock predicate (`heartbeat.py` lines 347–393) — present since issue #79 — fires when `(now - in_progress_since) > obs.heartbeat_stall_s`. It applies in BOTH phases. Override with `BH_HEARTBEAT_STALL_S`.

NIT-1: the 7200 s backstop is meaningful primarily in NON_WORKER phase (where the progress predicate is off) and as a catch-all if the VP-3 progress signal itself breaks. During WORKER_ACTIVE the 1800 s progress threshold always latches first (1800 s « 7200 s), so in practice the backstop is a safety net rather than the primary signal.

**Shared `_stall_alerted` debounce latch (IS-4)**

Both predicates share the `_stall_alerted` boolean (`heartbeat.py` line 87). Once either predicate fires and the alert is delivered, `_stall_alerted` is set to `True` (`heartbeat.py` lines 372 and 440), preventing a second alert for the same stuck episode regardless of which predicate fires second. The latch resets only when `mark_in_progress` or `clear` is called (`heartbeat.py` lines 123, 138). Each predicate emits a distinguishable `detail` string in the runlog stall event so the two causes are identifiable from logs (`heartbeat.py` lines 383–388 and 451–456).

**Progress signal source (VP-3)**

`last_progress_at` is updated via the `progress_cb` attribute injected into the vendored `Orchestrator` at dispatch time (`daemon.py` lines 737–747; `orchestrator.py` line 44). The callback is invoked at the top of each turn loop iteration (`orchestrator.py` lines 154–165) and calls `LivenessState.note_progress` (`heartbeat.py` lines 141–153). The callback is best-effort: any exception is logged and swallowed by the VP-3 guard (`orchestrator.py` lines 160–165) so a callback failure cannot crash the worker run.

**Failure semantics**

Progress-stall failures are non-fatal. They fire a `severity="critical"` alert via the same `alert()` path as the wall-clock predicate and emit a `{"event": "stall", ...}` runlog event. The heartbeat thread continues; no automatic recovery action is taken (the operator must inspect and intervene).

---

### Worktree orphan-GC (AC2) [issue #33]

After each poll tick's work units complete, the daemon runs a worktree orphan-GC sweep (`daemon.py` lines 1607–1873) by calling `scan_orphan_worktrees` (`recovery.py` lines 534–671). The sweep detects — and optionally reclaims — worktrees whose associated issue has become terminal without the normal cleanup path running (e.g. after a daemon crash mid-run).

**IS-5 liveness predicate**

A worktree is an orphan only when ALL four conditions hold (`recovery.py` lines 606–632):

1. The issue's GitHub state is `"CLOSED"` (terminal). OPEN issues and any fetch failure are treated as live (conservative).
2. The issue number is not in `running_issues` — the set of issues processed in the current tick (IS-5 predicate a; `recovery.py` line 611).
3. The issue does not carry the `agent-in-progress` label (IS-5 predicate b; `recovery.py` lines 622–625). The label and state are fetched together in a single `gh issue view --json state,labels` call (`recovery.py` lines 478–531).
4. The worktree has no uncommitted changes and no unpushed commits (IS-5 predicate c; `recovery.py` lines 627–629; `_is_worktree_live` at `recovery.py` lines 442–475).

The conservatism guarantee: a worktree that is OPEN, fetch-failed, currently running, carries `agent-in-progress`, has a dirty tree, or has unpushed commits is always kept.

**Detect vs reclaim modes**

The mode is read from `obs.worktree_gc` (`obs_config.py` lines 52–57, 131) and defaults to `"detect"`. Override with `BH_WORKTREE_GC`.

| `BH_WORKTREE_GC` | Behaviour |
|---|---|
| `detect` (default) | Logs a `severity="warn"` alert and emits an `orphan_worktree` runlog event for each confirmed orphan; never removes the worktree (`recovery.py` lines 634–657). Safe default — IS-5 detect-first. |
| `reclaim` | As above, then calls `cleanup_worktree(issue_num)` to remove the worktree (`recovery.py` lines 659–663). Opt-in; destructive. |

Any unrecognised value logs a WARNING and falls back to `"detect"` (`obs_config.py` lines 249–254).

**Failure semantics**

The sweep is guarded: the entire `scan_orphan_worktrees` body is wrapped in a `try/except` (`recovery.py` lines 594–670), returning an empty set on any exception. The daemon's call site adds a further outer guard (`daemon.py` lines 1872–1873) so a sweep failure is logged at DEBUG and never disrupts the daemon loop.

---

## 12. Two-identity subprocess auth model (identity broker) [implemented — issue #222]

Every subprocess the daemon spawns needs a GitHub credential decision: does this process act as the harness's own privileged identity, or as the unprivileged worker doing the actual issue work? A single shared credential cannot answer both cases — a fine-grained PAT cannot hold the `checks` permission the daemon's CI reads need, and a feature-branch push to a ruleset-protected branch requires the bypass actor to be the GitHub App itself, not a PAT (issue #220; see `_authed_git_push` in `daemon.py`). So the harness models exactly two identities and never collapses them into one ambient environment.

`src/baton_harness/chain/identity.py` defines the model:

| Identity | Env keys carried | Used for | Failure mode |
|---|---|---|---|
| `Identity.APP` | `GH_TOKEN`, `GITHUB_TOKEN`, `GH_INSTALLATION_TOKEN` (all three set to the resolved installation token) | Daemon-side privileged ops: feature-branch push (`_authed_git_push`, `daemon.py`), ruleset preflight `gh api` reads (`_build_preflight_runner`, `daemon.py` lines 206–237), label edits, CI reads | Missing/empty `installation_token` raises `ValueError` — a hard fail, never a silently empty credential (`identity.py` lines 33–46) |
| `Identity.WORKER` | None of the three privileged keys — all stripped unconditionally, plus any env value that happens to equal the resolved token is filtered out even if a caller passes one in | The Claude Code worker subprocess and other unprivileged chain spawns (git branch ops, sandboxed tool calls) | No exception path; the identity is deliberately additive-safe — asking for `WORKER` can only remove credentials, never grant them (`identity.py` lines 48–59) |

### `env_for` is the single resolution point

`env_for(identity, *, installation_token=None) -> dict[str, str]` (`identity.py` lines 25–59) is the only function in the codebase that decides what a spawned subprocess's GitHub credentials look like. It starts from a fresh copy of `os.environ` (never mutates the real one) and either injects the three privileged keys (`APP`) or strips them (`WORKER`). Every subprocess spawn in `chain/` passes an explicit `env=env_for(...)` built at the call site — `cli.py:204`, `branches.py:73`, `ruleset_status.py:290`, `sandbox_config.py:164`, and `daemon.py:236` all call `env_for(Identity.WORKER)` for their respective spawns, and `daemon.py`'s preflight runner and `_authed_git_push` resolve `Identity.APP` via `gh_env` (`app_auth.py` line 511, itself a thin wrapper: `return env_for(Identity.APP, installation_token=installation_token)`). No spawn site inherits an ambient GitHub token by omission.

This centralizes what used to be five near-identical per-module `_gh_env` helpers (`daemon`, `escalation`, `gh_deps`, `merge`, `recovery` — PR #163 reviewer W1) into one broker, and extends that consolidation to the worker side: the `cli.py` vault-fetch path that used to do `os.environ["GH_TOKEN"] = ...` before spawning the worker was removed as part of this change, so the installation token is never written to `os.environ` at all — it is passed by value into the one subprocess call that needs it and discarded (`app_auth.py` module docstring, "Security invariant (env-discipline seam)").

### Guard: every spawn must declare its identity

`tests/chain/test_identity_spawn_guard.py` AST-walks every `.py` file in `src/baton_harness/chain/` looking for a `subprocess.run`/`Popen`/`call` invocation that omits an explicit `env=` keyword. A call site is exempted only by a trailing `# identity: env-exempt` comment on the same source line, reserved for genuinely auth-agnostic spawns with no credential surface — e.g. the `pgrep` liveness probe and a `git config --get-all` key read in `reconcile.py`. The guard fails closed: any new bare spawn in `chain/` breaks this test until it either resolves an explicit identity via `env_for` or is marked exempt at review time.

---

## Decision records

Historical decisions made during the spike that ground the design. These were previously in `docs/spike-findings.md` (deleted — content moved here, issue #114).

### D1 — ToS posture: risk accepted (revisit at terms changes)

Reviewed the current Claude Code / consumer terms. No explicit verbiage was found prohibiting headless, orchestrator-driven use of the first-party `claude` binary on subscription auth. The ambiguity appears deliberate (Anthropic retaining flexibility). Absence of prohibition is not permission, but it is not prohibition either.

**Decision:** Risk accepted for a personal project. This is a *monitored assumption*, not a closed gate — terms moved twice in 2026, so re-check at any major Claude Code or consumer-terms update. The API-key fallback (architecture-spec.md §8 risk table) stays designed-in as the escape hatch: a terms change closing the subscription path becomes a cost increase, not a dead end.

> See also open-questions.md S1.1 for the fuller resolved treatment: proceeding accepted as known and bounded risk, with re-examination at each major Anthropic terms revision.

### D2 — Harness is its own repo, not a Baton fork

The policy layer (outcome router, hooks, prompts, templates, eventual Slack bot + Dockerfile) lives in a standalone version-controlled repo with the orchestrator as an upstream dependency. Fork only as a last resort, after "work around it" and "contribute upstream" are exhausted. *Detailed harness architecture deferred until the spike completes.*

> **[SUPERSEDED 2026-06-06 by option-(c) vendoring — see §1]** D2's "own repo, not a Baton fork" framing is not violated, but its "upstream dependency" and "contribute upstream" premises are superseded: `symphony/` is vendored into `src/baton_harness/vendor/symphony/` and called directly. The harness remains its own repo; D2 as a historical decision record is preserved here.

---

### Spike viability verdict

**The smoke test's core question — "could this work?" — is answered: yes.**
- **Scenario A:** a labeled issue produced a correct PR that passed pytest CI and was merged. The core loop closes end-to-end.
- **Scenarios B & C:** ambiguous and impossible work degrade into graceful blocks with actionable questions — not wrong guesses or garbage output.
- Repeated headless runs throughout the spike did not hit a rate-limit wall — **but see the rate-limits caveat below; this evidence is weak.**

**Rate limits are NOT validated.** The runs that didn't throttle were trivial (`add()`, `greet()`, blocks): tiny context, few turns, minimal tokens, concurrency 1. Subscription limits are typically keyed on token throughput over rolling windows, so the drivers that push toward the ceiling — large per-run context (real repo + CLAUDE.md + multiple files + accumulated tool results), high turn counts (15–30 for a real feature vs 2 for a toy), concurrency > 1, and sustained overnight load — were all minimized or absent. The spike supports only the narrow claim "low-demand headless runs at concurrency 1 don't immediately throttle," not "the subscription supports the intended workload." This is a **pilot-phase** question: it can only be answered by running genuinely representative issues against a real project and watching token consumption and throttling. It matters more than the deferred robustness items because it can break the *cost model* (forcing reduced concurrency or the per-token API-key fallback).

This clears the viability bar. Everything beyond it — CI auto-detection (F10), stuck-run / crash recovery (Scenarios E, F), and the C1–C3 harness concerns — is robustness and design, **not** viability, and belongs to the harness-design phase.

**Remaining viability-grade question — rate limits under real load.** Tracked in issue #39 (pilot-phase measurement; gates raising concurrency above 1; non-acute for serial v1). If realistic load throttles hard, the fallback is reduced concurrency (slower) or the API-key path (per-token cost).

---

### Spike findings

Empirical results from the smoke-test spike.

#### F1 — Tool identity: CLOSED — it is `mraza007/baton`
The README confirms it: "inspired by OpenAI's Symphony spec, rebuilt from scratch for Claude Code." The `.symphony/worktrees/` path is simply Baton's Python package name (`symphony/orchestrator.py`, `symphony/config.py`, etc.) — its worktree directory, not a different tool. Resolved via docs; no test needed. The D2 fork-vs-dependency reasoning and all Baton docs apply.

#### F2 — Env vars not passed to hooks
`$ISSUE_NUMBER` and `$EXIT_CODE` come back empty in `before_run`/`after_run`. The orchestrator does not export the variable names the spec's WORKFLOW.md assumed.
**Workaround (retired):** derive issue number from `basename "$PWD"` (worktree dir is named by issue number); use shell `$?` for exit status.

> **Forward state [implemented, VP-1, issue #27 P0]:** Under vendoring, the `env=` threading fix inside `run_hook` passes `ISSUE_NUMBER` directly. The `basename "$PWD"` workaround is retired.

#### F3 — Orchestrator does NOT manage GitHub labels for run state
It tracks run state internally (the `.symphony` state machine), and does not mutate GitHub labels on dispatch. Confirmed by observing `agent-ready` persist (not transition to `agent-in-progress`) during a run.
**Design implication (significant):** the architecture assumed the orchestrator owned the `agent-ready → agent-in-progress → terminal` label machine. It does not. **Label lifecycle must be owned by the harness hook layer.** (See §5 for the implemented label state machine.)

#### F4 — Agent writes code but not the closing git/PR steps
Without explicit instruction, the agent implements and verifies, then stops — leaving changes unstaged, no commit, no PR.
**Fix:** mechanical, numbered REQUIRED STEPS in the prompt (stage → commit → push → `gh pr create --draft`), with "report which step failed and STOP" framing.
**Design implication:** the agent prompt must be more mechanical than drafted. Implementation is the agent's default "done"; shipping is a separate, explicitly-instructed phase.

#### F5 — Outcome states are richer than the spec's done/blocked/failed
Real terminal states observed: `uncommitted-changes`, `no-commits`, `committed-no-pr`, `pr-opened`. "No PR" is not a single failure — it has multiple distinct causes that need distinct handling.
**Design implication:** the outcome router (Dial 2) must distinguish more states than the spec listed. The `after_run` classifier now detects these four; the done/blocked/failed model in architecture-spec §3.3 is too coarse.

#### F6 — Dial 1 soft-confidence works (PASS)
In Scenario B, the agent recognized genuine ambiguity, declined to guess, applied `blocked`, and left an *actionable* comment stating what it needed. Prompt-based "stop if unsure" held — which was a real open question, since soft instructions don't always survive contact with an eager model.
**Design implication:** validates that Dial 1 can be partly carried by the prompt. Deterministic PreToolUse hooks still needed for hard invariants, but soft-confidence for judgment calls is viable.

#### F7 — `max_turns` is retries-per-issue, not turns-within-a-session
Hitting the cap produced fresh restarts of the whole run, not continued reasoning. Each retry is a separate `claude` invocation against the same issue.
**Design implication:** `max_turns` calibration is about retry budget and cost, not reasoning depth.

#### F8 — Hooks can call external script files (confirmed pattern)
Hooks invoke standalone scripts that take the issue number as an argument and are independently testable. The testability principle carries forward.

> **Forward note (2026-06-04):** the shell-script convenience established here was superseded by the implementation-language decision: hooks are now Python modules in the `baton_harness` package. The testability principle holds; the language does not.

#### F9 — Agent funnels underspecified/impossible work to BLOCK, not FAIL
The agent converts "I can't do this confidently" into block-and-ask, not into failure or garbage output. Underspecified / contradictory / impossible inputs reliably surface as *questions*. This is excellent for the async model — imperfectly-scoped work degrades into "needs input," not "wasted run." It also means the block→respond→requeue loop (S2.2) is the **dominant** path for anything less than perfectly specified, not an edge case — raising its design priority.
**Reframes S2.1:** the important distinction is **block vs done**, not block vs fail.

#### F10 — Outcome classifier marks pr-opened as done WITHOUT checking CI
The `after_run` classifier treats any opened PR as `agent-done`. It does not check CI status. A PR with red CI would currently be marked done. This is the silent-bad-merge risk (S3.3) made concrete: "done" presently means "a PR exists," not "a correct PR exists."
**Design implication:** Dial 2's `done` determination must incorporate CI status, or introduce a distinct `needs-review-ci-red` state. (In the implemented daemon, CI green is gated before merging — see §10.)

#### F11 — Baton capabilities (from docs)
Authoritative facts from the README, relevant to the harness:
- **Config-by-path:** `baton start -w <path>` accepts a custom WORKFLOW.md path. (Superseded by vendoring — the `-w` path is no longer used.)
- **`after_create` hook:** runs once after worktree creation (e.g. `npm install`). Partial mitigation for S2.4 isolation (deps, not ports/services).
- **`{{ attempt }}` variable:** retry attempt number available in the prompt.
- **Hook timeout:** `hooks.timeout_ms` default 60s.
- **`permission_mode: bypassPermissions`** is a documented valid value — confirms the F4 fix direction.
- **CLAUDE.md is project-local:** Baton doesn't manage it; Claude Code discovers it from the worktree (a repo checkout), so it must be committed to the project repo.

---

### H-note (cost) — a block costs up to `max_turns` runs

A block produces no PR, so Baton retries it as a continuation up to `max_turns`. A blocked issue therefore consumes up to `max_turns` full Claude runs before settling — a cost-model factor, not just the H1 label bug.

The open sub-question (does the continuation check re-read labels and respect `exclude_labels: ["blocked"]`?) was answered by the #6 dry run: **no**. Baton evaluates `exclude_labels` at poll time only. Once a run is dispatched, it is not halted between turns. Block costs `max_turns`, not ~1 run.

Pilot decision: accept the `max_turns` cost as a known bound; keep `max_turns` modest. Full decision record: §8 above — "[design] H1 fix — terminal-block decision."

> **[SUPERSEDED 2026-06-06 by option-(c) vendoring — see §1]** Under vendoring, this fix is ~10 lines inside the vendored `_run_worker` turn loop — a harness-internal change with no upstream dependency. The `max_turns: 2` workaround is retireable post-vendoring. Issue #23 is closed.

---

## Constraints

Project constraints that bound the harness. Previously in `docs/problem-statement.md` (deleted — content moved here, issue #114).

### Context

I am a senior software engineer starting a new job that will significantly reduce my available time for personal project development. The system must operate without requiring my attention during working hours.

### Infrastructure

- Self-hosted. I have a server. No cloud-hosted agent systems.
- GitHub is the single source of truth. Issues, PRs, and milestones are the interface. No external task files or parallel tracking systems.

### Cost

- Core agent work (planning, decomposition, coding, review) must run on subscription to avoid per-token costs at scale. Target: Claude Code Max 20x ($200/mo), which includes API credits for lightweight orchestration use.
- Orchestration harness (queue polling, label transitions, notifications, dispatch logic) may use API calls. These are cheap, infrequent, and covered by the Max 20x credit allocation.

### Terms of service

- Executor must be Anthropic's first-party Claude Code binary (`claude` CLI). OAuth tokens from Claude subscription accounts cannot be used in third-party tools or agents per Anthropic's 2026 consumer terms — using a third-party agent with an extracted OAuth token violates the terms of service.
- Only compliant paths: (1) running the real Claude Code binary (subscription-authenticated), or (2) using an API key with per-token billing. Since subscription is the cost model, the executor must be the first-party binary.
- The [D1 decision record](#d1--tos-posture-risk-accepted-revisit-at-terms-changes) above records the specific risk assessment made during the spike.

### Time

- Morning setup: up to 30 minutes to define and approve the starting point before work.
- Evening review: available to review output, respond to blocked issues, and approve PRs.
- During the day: minimally available. The system may surface threshold-crossing questions via Slack and wait for async guidance; it must not surface every minor question. [implemented — daemon escalates blocked sub-trees via Slack + GitHub issue comment]

### Accountability model

The system operates between two human checkpoints that I own:

**Starting point — approved by me**
I define the milestone or feature scope, write acceptance criteria, and explicitly signal that the work is ready to be planned and executed. Nothing runs without this approval.

**Mid-run escalation — answered async by me [implemented]**
When the agent hits a threshold-crossing question it cannot resolve, it posts the question as a comment on the issue and applies the `blocked` label. The orchestrator pings me on Slack with a stall summary. I post guidance on the issue and remove `blocked`; the daemon's next poll resumes the parked sub-tree. The GitHub issue is the durable record; Slack is the notification channel. Questions that fall below the threshold do not reach me — the system handles them autonomously.

**End point — approved by me**
No PR merges to `main` without my review. Blocked issues sit idle until I respond. Nothing ships without my sign-off.

> **Note on chain-driver orchestration [implemented, issue #27]:** The always-on daemon performs `git merge --no-ff` of completed per-issue branches into the feature branch without per-issue human review. This is an intra-feature-branch operation — analogous to a developer's own local `git merge` while building a feature. The "human owns merge" checkpoint operates at the `feature → main` boundary: the harness opens a single ready-for-review `feature → main` PR; the human reviews and merges that. The daemon never merges to `main`.

Everything between those two checkpoints is the agent's responsibility, with threshold-crossing questions escalated asynchronously as described above.

### Non-goals

- Not fully autonomous end-to-end. Human approval is required at both ends of every work unit, with minimal async guidance on threshold-crossing questions the system escalates during a run.
- Not zero-setup. Reasonable morning prep time (up to 30 minutes) is acceptable.
- Not a team workflow. This is a solo developer working on personal projects.
- Not perfectly pre-scoped work. The system must handle ambiguity gracefully — surfacing it rather than guessing — rather than requiring pristine issue backlogs upfront.

### Assumptions

**Work scope**
- Unattended agent runs are restricted to application-layer work only. Infrastructure changes — cloud resources, deployment configuration, IaC (e.g. Azure, Terraform) — are explicitly out of scope and will not be included in any milestone handed to the system.
- UI implementation is in scope. Design decisions (layout, visual direction, component choices) are not — those must be resolved by me before a milestone is handed off. The agent implements against a defined design, it does not make design decisions.

**Authentication and credentials**
- Any service the agent needs to call during a run must have authentication pre-configured before the run starts. The agent will not be asked to acquire, rotate, or manage credentials.
- GitHub API access is pre-configured via a personal access token available to the harness. Other service credentials (e.g. a third-party API a project depends on) are set up per-project in the environment before that project is onboarded to the workflow.

### Success criteria

- I can define a milestone before leaving in the morning and return to PRs and/or clearly articulated blocked questions at end of day.
- No action is required from me between kickoff and review, beyond answering the rare, threshold-crossing questions the system escalates.
- The system fails safely — ambiguity surfaces as a blocked issue, not silent wrong output or wasted compute.
- Core work cost stays within subscription bounds regardless of how many issues run in a day.

---

### Design concerns C1–C3

These surfaced during the spike while designing the async CI-completion trigger (the second outcome-routing stage from F10). Both are consequences of having a second process that reacts to PR/CI events outside the orchestrator's own run loop. Neither is an edge case. All three are deferred from the pilot (§7).

#### C1 — Single-writer claim authority (multi-writer race)

The CI-completion trigger is a separate process from the orchestrator. When a PR fails CI and must be reworked, something re-routes it. If that means re-labeling the issue `agent-ready`, the poller re-grabs it — but:
- GitHub labels are **not an atomic lock**; check-then-set races. Two workers, or a worker and the CI-trigger, can both claim the same item.
- The orchestrator's internal claim tracking (the `.symphony` state machine) only covers its own single-process runs — it does not coordinate with an external CI-trigger or with multiple instances.
- A failed PR is a **different work unit** than a fresh issue (rework the existing branch, don't start fresh); claiming must account for that.

**Risk:** multiple agents grab the same failed PR at once → duplicated work, wasted subscription quota, conflicting pushes to the same branch.

**Solution shape (to design):** single-writer claim authority. The CI-trigger should *signal* the orchestrator rather than re-queue directly; the orchestrator solely owns claim/state mutation. Alternative: an atomically-applied lock label or an external lock. Principle: exactly one component mutates claim state.

#### C2 — PR provenance / authorization filter (security + burn)

A harness that reacts to PR/CI events must act **only** on PRs the agent itself created — never on PRs from human contributors or external parties.

**Risks if provenance isn't checked:**
- **Burn:** agent spends subscription quota reworking PRs it should never touch.
- **Security:** an external PR or issue is an untrusted-input vector — an autonomous agent with repo write access acting on stranger-supplied content (instructions embedded in a PR body, malicious code it then "fixes" and pushes). Acute on public repos, where anyone can open a PR or issue.

**Solution shape (to design):** provenance allowlist. Only agent-authored branches/PRs — identifiable by branch prefix (`agent/issue-N`) or the agent's bot identity — are eligible for harness action. Human/external PRs, and issues from non-trusted authors, are ignored by the harness entirely. The agent only works items labeled by the trusted owner; it never auto-engages on arbitrary-author PRs or issues. Directly tied to the instruction-source-boundary principle: observed content (a stranger's PR) is data, not a command.

#### C3 — Bounded rework with escalation (avoid infinite fix→fail→fix loops)

When a PR fails CI (or fails review), the trigger re-engages an agent to fix it. Without a budget this can loop indefinitely: fix → CI red → fix → CI red → … burning subscription quota and never converging.

**Distinct from `max_turns` (F7):** `max_turns` bounds retries to get a PR *open*, inside the orchestrator's run loop. This bounds rework attempts *after* a PR exists, driven by the async CI/review trigger across multiple CI cycles. The orchestrator considers the issue done once the PR opened, so its budget doesn't cover this loop. They do not compose automatically — the rework loop needs its own budget and counter.

**Requirements:**
- A per-PR rework counter stored in GitHub (source of truth) — marker comment or label — mutated only by the single claim authority (ties C1).
- After N rework attempts: stop auto-reworking, mark blocked/failed, notify via Slack (escalate to human).
- Ideally distinguish fixable failures (lint, test) from environmental/infra failures (runner down, flaky network) — retrying the latter is pure waste. (Refinement, not required for v1.)

**General principle (applies system-wide):** every autonomous retry loop needs (a) a bounded budget and (b) a defined human-escalation exit when exhausted. Applies to CI-rework, review-feedback rework, and any future loop. Escalation target is always: stop, mark blocked/failed, notify human. Converts infinite machine loops into finite loops that terminate at a human.

> C1, C2, and C3 are all requirements on the *same* component — the async CI/review-completion trigger (outcome-routing stage 2). That component is non-trivial: it needs claim coordination (C1), a provenance filter (C2), and rework-budget tracking (C3). Design it as a real component, not a webhook one-liner.
