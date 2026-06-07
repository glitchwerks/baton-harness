---
title: Always-on daemon — dependency-ordered work-unit orchestration (unified DAG) (#27)
touches:
  - src/baton_harness/vendor/symphony/**
  - src/baton_harness/vendor/symphony/hooks.py
  - src/baton_harness/vendor/symphony/orchestrator.py
  - src/baton_harness/chain/__init__.py
  - src/baton_harness/chain/gh_deps.py
  - src/baton_harness/chain/dag.py
  - src/baton_harness/chain/scheduler.py
  - src/baton_harness/chain/branches.py
  - src/baton_harness/chain/merge.py
  - src/baton_harness/chain/daemon.py
  - src/baton_harness/chain/registry.py
  - src/baton_harness/chain/recovery.py
  - src/baton_harness/chain/cli.py
  - src/baton_harness/before_run.py
  - src/baton_harness/after_run.py
  - src/baton_harness/_cli.py
  - bin/run-daemon.sh
  - config/WORKFLOW.md
  - pyproject.toml
  - .github/workflows/ci.yml
  - tests/chain/test_gh_deps.py
  - tests/chain/test_dag.py
  - tests/chain/test_scheduler.py
  - tests/chain/test_branches.py
  - tests/chain/test_merge.py
  - tests/chain/test_daemon.py
  - tests/chain/test_recovery.py
  - tests/vendor/test_run_hook_env.py
  - tests/vendor/test_exclude_labels_recheck.py
  - docs/harness-design.md
skills_relevant:
  - python
  - claude-github-tools:github-actions
---

# Always-on daemon — dependency-ordered work-unit orchestration (#27)

**Status:** Draft spec for review. The central architecture (option (c) — vendor
`symphony`, custom always-on daemon, unified DAG) is **decided** in the merged
design docs (`docs/harness-design.md §10`, `docs/architecture-spec.md §2/§3.4/§6/§9`,
both reformed 2026-06-07 to the vendored model). This spec is the lower-level
realization of that decision. Per CLAUDE.md § Issue Tracking, the phasing in §12
is a **proposal** — creating the tracking issues/milestone is a separate,
user-confirmed step, not permission to start coding.

**Source intent:** issue #27 (`.tmp/issue-chaining-feature.md:L1-L49`). Extend the
harness from running a single `agent-ready` issue to autonomously executing a
*dependency DAG* of issues end-to-end, leaving one feature branch per work unit
for human review and **never** merging to `main`.

**Authoritative sources for the decided model (read in this order):**
- The two decision-log comments on issue #27 ("Design decision log — operational
  model" and "Decision log — addendum (orchestrator/worker model + single-repo
  gate)"). `unverified:` their live text was not fetched in this session (no
  GitHub read tool available to the sub-agent); the merged design docs below were
  reformed *from* those comments on 2026-06-07 and are treated as the
  authoritative encoding.
- `docs/harness-design.md:L189-L260` (§10 — the full decided daemon/worker shape,
  BLOCKING resolutions, OQ resolutions, single-repo gate).
- `docs/architecture-spec.md:L116-L144` (§3.4 orchestrator/worker + vendor patches),
  `:L241-L278` (§6 work-unit lifecycle + guidance flow), `:L327-L335` (§9 single-repo).
- `.tmp/baton-deepdive-findings.md` (symphony internals: the `_run_worker` seam
  §1.2, `run_hook` §1.3, `WorkspaceManager` §1.4, the coordination-seam dissolution
  §2.3, file:line index in the appendix).
- `.tmp/arch-review-new-model.md` (4 BLOCKINGs + 8 CONCERNs the spec must honor).
- `docs/research/2026-06-06-issue-dag-orchestration.md` (external prior art:
  dependency API, graphlib, merge-commit finding, novelty).

---

## 1. Problem & current ground truth

The harness today runs **one** issue at a time, flat. A direct read of the source
confirms there is **no chaining/DAG code** — this is net-new on top of the §10
decided concept. Three facts from the current code shape the work:

1. **The existing `before_run` rebases onto `main`, hardcoded.**
   `before_run.py:L107` runs `git rebase origin/main`. Per-issue branches under a
   milestone work unit must rebase onto the **feature branch**, not `main`. This
   is BLOCKING-4 (`.tmp/arch-review-new-model.md:L49-L57`): left unchanged, a
   per-issue branch cut from `feature/<slug>` and rebased onto `origin/main`
   silently loses the commits of earlier issues on the feature branch.
2. **`after_run`'s F5 classifier compares against `origin/main`, hardcoded.**
   `after_run.py:L158` runs `git cherry origin/main HEAD`. For a feature-branch
   work unit this is the wrong base — it inflates the "ahead" delta with the
   feature branch's own commits. This is BLOCKING-2
   (`.tmp/arch-review-new-model.md:L23-L33`).
3. **"Done" today means "PR opened", not "merged".** `after_run.py:L278-L282`
   adds `agent-done` when a PR is open and explicitly does **not** check CI (the
   F10 caveat). Issue #27's satisfaction signal is **merged into the feature
   branch** with green CI — a stricter condition (`harness-design.md:L214`).

The vendored-symphony decision (CLAUDE.md § Upstream dependency; `harness-design.md §1`)
changes the orchestration substrate: `symphony` is copied into
`src/baton_harness/vendor/symphony/` and the daemon calls
`Orchestrator._run_worker(issue)` directly. Symphony's flat poll/dispatch loop
(`run`/`_tick`/`_dispatch`/`_on_worker_done`), `cli.start`, and `watchfiles` are
**dropped** (`harness-design.md:L219`; `.tmp/baton-deepdive-findings.md:L40-L51`
for the `_run_worker` seam).

---

## 2. CENTRAL ARCHITECTURE DECISION — option (c): vendored symphony + custom always-on daemon

**Decision (already made in the merged docs; recorded here for the lower-level spec):**
Build a **custom always-on daemon** (the orchestrator) that calls the **vendored
`symphony._run_worker`** as a per-issue worker library function. There is **one**
execution path, parameterized by the DAG. This is **option (c)** from the issue #27
brief (`.tmp/issue-chaining-feature.md:L35`), superseding the option-(a)
black-box-wrapper design of the prior revision of this spec.

### What changed from option (a)

| Dimension | Option (a) — wrap Baton as a black box (prior spec) | Option (c) — vendor + daemon (this spec) |
|---|---|---|
| Worker invocation | Promote `agent-ready`; Baton's poller picks it up | Daemon calls `_run_worker(issue)` directly as a library |
| Coordination seam | Label-poll loop: "wait for terminal label on N" | None — the `_run_worker` call returns `"pr_created"`/`"no_pr"` directly (`.tmp/baton-deepdive-findings.md:L51`) |
| Poller config | `config/WORKFLOW.chain.md` to tame the flat poller | **Dropped** — no poller runs (`.tmp/arch-review-new-model.md:L160-L164`, NIT-2) |
| C1 lock | Lock label / lock file on the feature branch (OQ-8) | **Moot** — single daemon, sole label writer (`harness-design.md:L230`) |
| `before_run` rebase target | Could not thread env cleanly under wrapper | VP-1 adds `env=` to `run_hook`; daemon threads `CHAIN_BASE_BRANCH` |
| Process model | Driver subprocess that exits when chain done + `baton start` daemon | One persistent daemon; never exits on a block |
| Flat vs chain | Two execution paths (flat Baton + chain driver) | One path; N=1 is the degenerate DAG |

### Rationale (condensed from the merged decision; full reasoning in the deep-dive)

1. **The coordination seam is overhead, not safety.** The deep-dive
   (`.tmp/baton-deepdive-findings.md:L374-L381`) is explicit: the label-polling
   loop, `WORKFLOW.chain.md`, and the C1 lock are all artifacts of treating a
   ~1120-line MIT codebase as an opaque subprocess. A direct
   `await orch._run_worker(issue)` eliminates all three.
2. **Upstream is frozen — vendoring makes us the de facto maintainer anyway.**
   `mraza007/baton` has 3 commits (Mar 2026), no external PRs ever merged, MIT
   licensed (`.tmp/baton-deepdive-findings.md:L332`, `:L380-L381`; CLAUDE.md
   § Upstream dependency). The "fork maintenance cost" argument that justified
   option (a) evaporates — we own the source regardless.
3. **The `before_run`/`after_run` env-threading problem is unsolvable cleanly
   under option (a).** `run_hook` passes no `env=`
   (`.tmp/baton-deepdive-findings.md:L423`, `hooks.py:L22-L27`). Under vendoring,
   adding `env=` is a ~5-line patch to a module we own (VP-1).

### Why not (a) or (b)

- **(a) Wrap Baton as a black box** — superseded. Its coordination seam is pure
  overhead; see the table above. Reconsider only if the operational posture ever
  demands a separate exit-on-completion driver subprocess
  (`.tmp/baton-deepdive-findings.md:L404-L409`) — not the case for the always-on
  daemon model.
- **(b) Fork Baton and thread chain logic into its run loop** — rejected: threads
  the most complex new logic into an async loop never designed for cross-issue
  coordination, and the loop is hard to unit-test
  (`.tmp/baton-deepdive-findings.md:L297-L309`).

---

## 3. The unified execution model — 1 work unit = 1 DAG (N ≥ 1)

There is **one** execution path, parameterized by the DAG
(`harness-design.md:L196-L204`; `architecture-spec.md:L243`). A **work unit** is:

- A **milestone** — all its issues form one DAG → one `feature/<slug>` branch →
  one draft `feature → main` PR.
- A **single un-milestoned issue** — its own N=1 DAG → its own feature branch →
  its own PR.

N=1 is the degenerate DAG handled by the same logic; there is **no separate
flat-run entry point**. This dissolves BLOCKING-3
(`.tmp/arch-review-new-model.md:L37-L45`): with one daemon and one path, there is
no flat-run / chain-run coexistence and therefore no multi-writer label race
(`harness-design.md:L230`).

**Work-unit membership = milestone** (OQ-2 resolved; `harness-design.md:L238`).
Issues not belonging to any milestone each become their own N=1 work unit.

### 3.1 DAG source of truth — native issue-dependencies REST API

**Decision:** Read the execution-order DAG from GitHub's **native issue
dependencies REST API** (`blocked_by` / `blocking`), GA since 2025-08-21
(`docs/research/2026-06-06-issue-dag-orchestration.md:L28-L45`).

- `mcp__github__*` does **not** expose dependency endpoints (MCP issue #950 /
  unmerged PR #1927 — `docs/research/2026-06-06-issue-dag-orchestration.md:L44`).
  The daemon calls `gh api` REST directly:
  - `gh api repos/{owner}/{repo}/issues/{n}/dependencies/blocked_by` → blocker
    Issue objects (paginated, max 100).
  - `gh api repos/{owner}/{repo}/issues/{n}/dependencies/blocking` → issues this
    one blocks.
- Output parsed with `json.loads` (never grepped — the `after_run.py:L180`
  discipline).
- **Same-repo only** — confirmed limitation
  (`docs/research/2026-06-06-issue-dag-orchestration.md:L42`). This is the binding
  constraint behind the single-repo-daemon gate (§10).

`unverified:` the exact `gh api` response shapes for the dependency endpoints were
not exercised live this session — they come from the research report's API
summary (citing GitHub REST docs, fetched 2026-06-06). Confirm field names against
a live `gh api` call during P1 before depending on them.

### 3.2 Scheduler — `graphlib.TopologicalSorter`

**Decision:** Use Python stdlib `graphlib.TopologicalSorter`
(`docs/research/2026-06-06-issue-dag-orchestration.md:L64-L76`). Stdlib since 3.9;
the project requires `>=3.10` (`pyproject.toml:L13`).

- Build the graph `{issue: [blocker_issues]}` from `blocked_by` edges.
- `.prepare()` raises `CycleError` on a cyclic graph → free cycle detection
  (`harness-design.md:L257`).
- `.get_ready()` → issues whose blockers are all `.done()` = the ready frontier.
- `.done(issue)` is called only after that issue's branch **merges into the
  feature branch** with green CI (§3.3).
- `.is_active()` is the per-work-unit loop-termination condition.

**Partial-failure caveat** (`docs/research/2026-06-06-issue-dag-orchestration.md:L75`):
`TopologicalSorter` does not model failure. The daemon keeps a separate
`parked` set (failed/blocked issues + their transitive dependents) and filters
`get_ready()` output against it (§3.5).

### 3.3 Satisfaction = merged into the feature branch (CI-gated `--no-ff`)

Per `harness-design.md:L214`: "Dependency satisfied" = **merged into the feature
branch**, not "PR opened", not "merged to main." The daemon marks
`scheduler.done(N)` only after N's per-issue branch has merged into
`feature/<slug>` and CI was green at merge time.

- **Merge strategy: `--no-ff` merge commit, not squash.** Squashing an integration
  branch forces `--onto` rebasing of every dependent branch and produces ghost
  diffs (Graphite bottom-up finding,
  `docs/research/2026-06-06-issue-dag-orchestration.md:L86-L88`).
- **Bottom-up merge order:** the `graphlib` ready-queue naturally yields
  dependency order; merge lowest-first.
- **CI gate per merge:** a branch may merge only after its CI is green
  (`architecture-spec.md:L258`). This requires the `feature/**` CI trigger (§3.6).
- **The harness NEVER merges `feature → main`** (hard constraint, issue #27;
  `harness-design.md:L217`).

### 3.4 Base-ref to the feature branch — daemon checks out HEAD (no naming patch)

The daemon **checks out `feature/<slug>` as HEAD before calling `_run_worker`**
(`harness-design.md:L213`; `architecture-spec.md:L122`). Symphony's
`WorkspaceManager.ensure_worktree` does `git worktree add -b <branch> <path> HEAD`
(`.tmp/baton-deepdive-findings.md:L171-L172`), so HEAD-based worktree creation
naturally targets the feature branch — **no `WorkspaceManager` naming patch is
needed** (resolves CONCERN-1, `.tmp/arch-review-new-model.md:L63-L69`).

**Symphony naming is kept:** `.symphony/worktrees/<N>` (bare-integer dir) and
`baton/<slug>-<N>` branches (`.tmp/baton-deepdive-findings.md:L156-L172`). The
issue number is resolved from the worktree dir basename, which is the bare
integer `<N>` — already handled by `resolve_issue_number` (`_cli.py:L37`, the
"Baton (symphony) form"). The CONCERN-1 worry about `baton/<slug>-<N>` *branch*
names is moot because the daemon resolves the issue number from the **worktree
directory** (a bare integer), not the branch name.

### 3.5 Failure / block handling — park the affected sub-tree, continue independent branches, escalate (no retry)

**Decision (B1 resolved; `harness-design.md:L232`):** **no retry in v1.** When an
issue **fails** (`no_pr`) or **blocks**:

1. Do **not** mark it `done`; add it (and its transitive dependents) to the
   `parked` set.
2. **Do not dispatch its dependents** — the parked sub-tree halts
   (`harness-design.md:L256`).
3. **Continue dispatching independent ready issues** whose blockers are
   unaffected. The daemon **never exits on a block** (`harness-design.md:L215`).
4. **Escalate via Slack** — post a stall summary card to `#agent-decisions`
   (`architecture-spec.md:L136-L139`).

The vendored `state.py` retry/backoff is **unused** in v1. C3 (bounded rework with
escalation, `spike-findings.md:L153-L165`) is satisfied by the park+escalate path:
the bound is implicit (zero retries) and the escalation exit is the Slack summary.

**Block vs fail (F9):** the agent funnels underspecified/impossible work to
`blocked`, not failure. The daemon treats both identically for sub-tree
propagation but distinguishes them in the escalation summary (clarify vs debug).

**Outcome-handling protocol (resolves BLOCKING-1, `.tmp/arch-review-new-model.md:L13-L19`).**
`_run_worker` returns `"pr_created"` or `"no_pr"` (`.tmp/baton-deepdive-findings.md:L51`).
Because the daemon calls it directly, `_on_worker_done` never fires
(`.tmp/baton-deepdive-findings.md:L106` — practical blocker 3); the daemon owns
outcome handling itself:

| `_run_worker` return | `blocked` label present? | Daemon action |
|---|---|---|
| `"pr_created"` | no | CI-gate (§3.3); green → `--no-ff` merge + `scheduler.done(N)`; red → park sub-tree + escalate |
| `"pr_created"` | yes | park sub-tree + escalate (block overrides; the agent flagged doubt) |
| `"no_pr"` | yes | park sub-tree + escalate (clarify) |
| `"no_pr"` | no | park sub-tree + escalate (debug — failed) — **no retry** |

The daemon does **not** retry `"no_pr"`. The missing `_on_worker_done` callback is
intentionally not replaced: its only jobs were retry scheduling and
`OrchestratorState` persistence (`.tmp/baton-deepdive-findings.md:L107`), neither
of which v1 uses (no retry; the daemon owns its own scheduler state in-memory +
the crash-recovery reconstruction in §11/OQ-5).

`OrchestratorState` note (CONCERN-4, `.tmp/arch-review-new-model.md:L100-L108`):
the daemon does not depend on `state.json`. VP-2 should additionally guard the
`L129` turn-tracking mutation against a missing `running[N]` entry so a stale
`state.json` cannot corrupt a later run — folded into VP-2 (§7).

### 3.6 CI trigger must include the feature branch — `feature/**` glob (OQ-4 resolved)

`ci.yml:L4-L7` triggers CI only on `main`. The decided resolution (OQ-4;
`harness-design.md:L239`; CONCERN-5, `.tmp/arch-review-new-model.md:L112-L118`) is
the **`feature/**` glob** — add it to `pull_request.branches`. The per-run branch
name option is **rejected as incoherent**: `ci.yml` is a static file in the repo
and cannot be parameterized at runtime by the daemon. OQ-4 is therefore
**downstream of OQ-1** (the glob must match whatever naming convention §11/OQ-1
confirms). This is a P1 prerequisite — without it, the CI gate (§3.3) is
unenforceable.

---

## 4. Component / module breakdown

Two trees change: the **vendored worker** (two patches) and a **new `chain/`
package** holding the daemon. The existing flat hooks gain env-awareness. Each
`chain/` module is independently testable in the existing mock-subprocess style
(F8, `spike-findings.md` testability principle).

### 4.1 The daemon (orchestrator) — new `src/baton_harness/chain/`

| Module | Responsibility | Key external calls |
|---|---|---|
| `chain/gh_deps.py` | Read `blocked_by`/`blocking` and milestone membership via `gh api`; parse with `json.loads`. | `gh api …/dependencies/blocked_by`, `…/blocking`; `gh api …/milestones/{m}` / `gh issue list --milestone` |
| `chain/dag.py` | Build `{issue: [blockers]}`; expose the membership set; cycle detection via `graphlib.prepare`. | none (pure) |
| `chain/scheduler.py` | Wrap `graphlib.TopologicalSorter`: ready frontier, `mark_done`, `mark_parked`, `is_active`, transitive sub-tree parking, `parked`-set filtering of `get_ready()`. | none (pure, stdlib) |
| `chain/branches.py` | Create `feature/<slug>` off `main`; check out `feature/<slug>` as HEAD before each `_run_worker` call (§3.4). | `git` via subprocess |
| `chain/merge.py` | Query CI for a per-issue PR; `--no-ff` merge into `feature/<slug>` when green (CI gate, §3.3). | `gh pr checks` / `gh api …/check-runs`; `git merge --no-ff` |
| `chain/registry.py` | Repo-registry: the daemon poll loop iterates this (one entry in v1; repo #2 = append — the multi-repo seam, §10). | none (config read) |
| `chain/recovery.py` | Crash-recovery: reconstruct the scheduler `done`/`parked` sets from GitHub labels + merged commits on the feature branch (OQ-5, §11). | `gh issue list`, `git log feature/<slug>` |
| `chain/daemon.py` | The always-on outer loop: poll registry → detect ready work unit → build DAG → per-DAG loop (frontier → checkout feature HEAD → `_run_worker` → outcome protocol §3.5 → CI-gate+merge → `mark_done`) → draft `feature → main` PR on completion → Slack escalation. Never exits on a block. | composes the above + `_run_worker` + label edits |
| `chain/cli.py` | `bh-daemon` console entry point; wires the daemon from `WORKFLOW.md` config + repo registry. | argparse |

### 4.2 The vendored worker — `src/baton_harness/vendor/symphony/` (two patches)

| Patch | File | Change | Why |
|---|---|---|---|
| **VP-1 (P0)** | `vendor/symphony/hooks.py` | `run_hook` gains `env: dict \| None = None`; passed through to `asyncio.create_subprocess_exec`. Daemon passes `{"CHAIN_BASE_BRANCH": feature_branch, "BH_VENV": <venv>}`. | Threads the rebase target to `before_run`/`after_run` (B2/B4) **and** `BH_VENV` for hook discovery (CONCERN-2). The current `run_hook` passes no `env=` (`.tmp/baton-deepdive-findings.md:L423`). |
| **VP-2** | `vendor/symphony/orchestrator.py` | Re-check `exclude_labels` inside the `_run_worker` turn loop after `fetch_issue_state` (`.tmp/baton-deepdive-findings.md:L343-L348`); also guard the `L129` turn-tracking mutation against a missing `running[N]` entry (CONCERN-4). | Makes a block terminal (closes the #23 root cause), retiring the `max_turns: 2` workaround; prevents `state.json` corruption. |

**Only these two patches.** No naming patch (§3.4 base-ref approach resolves
CONCERN-1). No retry wiring (no retry in v1, §3.5).

### 4.3 The existing flat hooks — `src/baton_harness/` (env-aware)

| File | Change | Why |
|---|---|---|
| `before_run.py` | Read `CHAIN_BASE_BRANCH` from env (default `origin/main`); rebase onto it. | BLOCKING-4 — the hardcoded `git rebase origin/main` (`before_run.py:L107`) corrupts feature-branch runs. |
| `after_run.py` | Read `CHAIN_BASE_BRANCH` from env (default `origin/main`) for the `git cherry` base (`after_run.py:L158`). **Must NOT query CI** even in chain contexts (CI gate lives in `merge.py`; CONCERN-7, `.tmp/arch-review-new-model.md:L134-L138`). | BLOCKING-2 — the hardcoded `git cherry origin/main HEAD` mis-classifies feature-branch outcomes. |
| `_cli.py` | Optional: add a shared `run()` subprocess helper reused by `chain/` modules (each hook currently has its own `_run`). `resolve_issue_number` is unchanged — the daemon resolves from the bare-integer worktree dir (§3.4). | Consolidation; not load-bearing. |

### 4.4 Supporting files

- `bin/run-daemon.sh` — launcher analogous to the old `bin/run.sh` (which becomes
  obsolete post-vendoring, `harness-design.md:L106`): resolves harness root,
  exports `BH_VENV`, label preflight, then runs the daemon. The `baton start -w`
  invocation is gone.
- `config/WORKFLOW.md` — **kept** (not a new chain variant). Its YAML front-matter
  is no longer parsed by an external poller, but its agent-prompt body and the
  hook wiring remain the instruction template (`harness-design.md:L64`). The
  `max_concurrent` value here is the documented single-repo concurrency budget
  (§10 seam (b)). **No `config/WORKFLOW.chain.md`** (OQ-3 moot — single daemon).

---

## 5. The daemon run / merge loop

Feature-branch naming: `feature/<milestone-slug>` for a milestone work unit;
`feature/<issue-title-slug>` for an N=1 work unit (OQ-1, §11 — confirm
convention). Per-issue worktree/branch naming follows symphony
(`.symphony/worktrees/<N>`, `baton/<slug>-<N>`), §3.4.

```
DAEMON (always-on; never exits on a block):
LOOP forever:
  poll registry (§10) for ready work units:
    - a milestone with >=1 `agent-ready` issue, OR
    - an un-milestoned `agent-ready` issue (its own N=1 unit)
  for each newly-ready work unit, start a per-DAG run:

  PER-DAG RUN:
  0. PRECONDITION: read membership (milestone) + edges (blocked_by) → build DAG.
     scheduler.prepare() — CycleError → escalate (Slack), skip this unit.
  1. Create feature branch (idempotent w.r.t. recovery, §11/OQ-5):
       git -C <repo> fetch origin main
       git -C <repo> branch feature/<slug> origin/main   (skip if exists; on
           restart, reconstruct done/parked via chain/recovery.py)
       git -C <repo> push -u origin feature/<slug>
  2. WHILE scheduler.is_active():
     a. frontier = scheduler.get_ready()  minus  parked sub-tree
     b. if frontier empty and work still pending → fully parked: escalate, end
        this DAG run (daemon stays alive, moves to next work unit).
     c. pick ONE ready issue N (serial — §6):
        - git -C <repo> checkout feature/<slug>      (HEAD = feature branch §3.4)
        - transition N's label agent-ready → agent-in-progress
        - result = await orch._run_worker(N)         (vendored worker)
          · worker creates .symphony/worktrees/<N> off feature HEAD
          · before_run rebases onto CHAIN_BASE_BRANCH=feature/<slug> (VP-1)
          · claude -p turn-loop (VP-2 makes a block terminal)
          · after_run classifies vs CHAIN_BASE_BRANCH; sets agent-done|blocked
        - apply the §3.5 outcome protocol to (result, blocked-label):
          · pr_created + green CI → merge.py --no-ff into feature/<slug>;
                                    scheduler.mark_done(N); relabel (§8 CONCERN-3)
          · pr_created + red CI   → scheduler.mark_parked(N); escalate
          · blocked / no_pr       → scheduler.mark_parked(N); escalate
  3. COMPLETION: when no active nodes remain, push feature/<slug>; open ONE draft
     PR feature/<slug> → main. PR body: issues merged (PR/commit refs), issues
     parked (reasons), escalation summary. Claude attribution line. The harness
     NEVER merges this PR.
```

---

## 6. Concurrency — serial per-DAG (v1)

**Decision:** v1 dispatches **one in-flight issue per work unit at a time**, even
where the DAG permits parallelism. Rationale (`harness-design.md:L258`):

- Serial per-DAG execution makes the daemon the unambiguous single label writer
  (C1) and eliminates concurrent claim races without any lock (OQ-8 moot).
- Parallel-within-a-DAG-level is a clean v2 extension (the scheduler already
  returns the whole ready set); deferred.

The cross-repo / cross-work-unit concurrency budget (`max_concurrent` in
`WORKFLOW.md`) is a **documented decision, not an in-daemon code object** (§10
seam (b); `architecture-spec.md:L253`). A `GlobalBudget` abstraction is explicitly
the wrong seam here.

---

## 7. Vendor patches (the only two)

**VP-1 — `run_hook` gains `env=` (P0 prerequisite).** Without it, the daemon
cannot thread `CHAIN_BASE_BRANCH` (so `before_run`/`after_run` rebase/classify
against the wrong base — BLOCKING-2/4) **or** `BH_VENV` (so `bh-*` hook entry
points are undiscoverable in the library-call topology — CONCERN-2,
`.tmp/arch-review-new-model.md:L73-L81`). This patch must land at the **same
commit as vendoring**. ~5 lines (`.tmp/baton-deepdive-findings.md:L386-L388`).

**VP-2 — `exclude_labels` re-check in the `_run_worker` turn loop + `state.json`
guard.** Re-check `exclude_labels` after `fetch_issue_state` inside the turn loop
(`.tmp/baton-deepdive-findings.md:L343-L348`) so a mid-run `blocked` label makes
the block terminal — closing the #23 root cause and retiring `max_turns: 2`
(`config/WORKFLOW.md:L10`; `harness-design.md:L179`). Bundle the `L129` missing-
`running[N]` guard (CONCERN-4) here. ~10 lines.

**No third patch.** `state.py` retry/backoff is unused (no retry, §3.5);
`WorkspaceManager` naming is left as-is (§3.4 base-ref approach).

---

## 8. C1 / C2 / C3 under the single daemon

`spike-findings.md:L131-L165` attaches C1/C2/C3 to any label-mutating component.
The daemon is exactly such a component.

- **C1 — single-writer claim authority.** The daemon is the **sole** label writer
  during a run: it owns `agent-ready → agent-in-progress → {agent-done | blocked}`
  and the merge-gate relabel. With one daemon and one execution path, there is no
  flat-run poller and no second writer (BLOCKING-3 dissolved; `harness-design.md:L230`).
  `after_run` (firing inside `_run_worker`) sets a terminal label on the issue the
  daemon is currently dispatching; serial per-DAG execution means the daemon and
  `after_run` never target two different issues at the same instant. **No lock**
  (OQ-8 moot).

  **CONCERN-3 — `agent-done` vs the merge gate (`.tmp/arch-review-new-model.md:L85-L96`).**
  Resolution: **`agent-done` is defined as "PR opened, CI unverified"** (the
  existing flat-run meaning, `after_run.py:L279-L282`). After the CI-gated `--no-ff`
  merge succeeds, the daemon **relabels the issue to a distinct terminal label
  `agent-merged`** (removing `agent-done`). This keeps `after_run`'s contract
  unchanged and makes the daemon the writer of the *post-merge* state — there is
  no conflict because the two labels mean different things (PR-open vs
  merged-into-feature-branch). A red-CI issue keeps `agent-done` (the PR is open)
  but is parked; the daemon's `parked` set, not the label, gates re-dispatch (and
  there is no re-dispatch in v1). `unverified:` confirm `agent-merged` is the
  preferred terminal label name vs. reusing `agent-done` semantics — surfaced as a
  minor open decision in §11.

- **C2 — provenance allowlist.** The daemon acts only on (i) issues in the
  trusted-owner's milestone / carrying `agent-ready`, and (ii) branches/PRs the
  agent itself created (the `baton/<slug>-<N>` prefix / agent identity). It never
  merges a human-authored or external PR into the feature branch
  (`spike-findings.md:L144-L151`).

- **C3 — bounded rework + escalation.** No auto-rework in v1 (no CI-rework loop is
  built here). A parked issue halts its sub-tree and is escalated via Slack. The
  bound is implicit (zero retries); the escalation exit is the stall summary. A
  future v2 intra-chain rework loop inherits the C3 counter requirement
  (`spike-findings.md:L153-L163`).

---

## 9. Guidance flow + sub-tree parking

The agent does not pause mid-run; each `_run_worker` call is one-shot
(`architecture-spec.md:L265`). The minimally-available guidance flow
(`architecture-spec.md:L263-L274`; `harness-design.md:L256`):

| Step | Who | What happens |
|---|---|---|
| 1 | Worker | Agent comments its question on the issue; applies `blocked`; `_run_worker` exits returning `"no_pr"` (or `"pr_created"` with doubt). `after_run` (firing inside `_run_worker`) leaves `blocked` and strips `agent-ready` (`after_run.py:L246-L275`). |
| 2 | Daemon | Detects `blocked` via the outcome protocol (§3.5); **parks the affected sub-tree** (the issue + its transitive dependents); posts a stall summary card to `#agent-decisions` on Slack. Continues independent branches and other work units uninterrupted. |
| 3 | Human | Reads the card; posts guidance **directly on the GitHub issue**; removes `blocked`. |
| 4 | Daemon | Next poll sees `blocked` gone; un-parks the sub-tree; re-dispatches the issue (the agent reads the answer from the issue comment on its next run). |

The **GitHub issue is the durable record**; **Slack is the channel** only
(`architecture-spec.md:L91`). The two-dial confidence model
(`architecture-spec.md:L170-L191`) is the threshold: Dial 1 (agent asks readily)
is the prompt's confidence/block rule (`config/WORKFLOW.md:L24-L31`); Dial 2 (which
asks reach Slack) is the daemon's escalation filter. "Normal resume" is just the
next poll (the daemon is always-on); the **crash-recovery** path is §11/OQ-5.

---

## 10. Single-repo daemon v1 — multi-repo deferred with two seams

**One daemon per repo (v1).** The binding constraint is the GitHub dependency API
(`blocked_by`/`blocking`), which is **same-repo only**
(`docs/research/2026-06-06-issue-dag-orchestration.md:L42`); a work unit cannot
span repositories by construction (`architecture-spec.md:L331`;
`harness-design.md:L249-L253`). Multi-repo is deferred with two clean seams:

1. **Repo-registry (`chain/registry.py`).** The daemon poll loop iterates a
   one-entry repo-registry rather than closing over a single `project_root`.
   Repo #2 = a registry **append**, not a loop rewrite.
2. **Concurrency budget as a documented decision.** `max_concurrent` lives in
   `WORKFLOW.md` (`harness-design.md:L253`), **not** as an in-daemon `GlobalBudget`
   code object. Real cross-repo enforcement belongs to a future
   supervising/lease layer — two daemons each honoring `max_concurrent=2` would
   allow 4 streams, and the seam keeps that honest rather than pretending a
   per-daemon object enforces a global cap.

---

## 11. Open decisions for the user

Genuine choices surfaced rather than guessed. Items resolved in the merged docs
are recorded in §3/§7/§8 and are **not** re-opened here.

- **OQ-1 — Feature-branch naming (load-bearing; gates OQ-4 glob).** Proposed:
  `feature/<milestone-slug>` for a milestone work unit, `feature/<issue-title-slug>`
  for an N=1 unit. Confirm the prefix/convention — the `feature/**` CI glob (§3.6)
  must match it. **Recommendation:** the `feature/` prefix as proposed.
- **OQ-3a — Post-merge terminal label name (minor; CONCERN-3 sub-decision).**
  §8 resolves the `agent-done`-vs-merge-gate conflict by relabeling to a distinct
  `agent-merged` after the CI-gated merge. Confirm the label name (`agent-merged`
  vs `chain-merged` vs another), or confirm that no post-merge label is needed at
  all (the draft `feature → main` PR is the only completion signal). **Recommendation:**
  `agent-merged`.
- **OQ-5 — Crash-recovery reconstruction signals (load-bearing; CONCERN-6).** The
  daemon is always-on, so normal "resume" is the next poll. **Crash recovery** (the
  daemon process dies mid-DAG, in-memory scheduler state lost) needs an explicit
  reconstruction algorithm in `chain/recovery.py`. **Proposed signals**, in
  precedence order:
  1. `done` set = issues whose per-issue branch is **merged into `feature/<slug>`**,
     detected via `git log feature/<slug> --merges` + the merge-commit message
     convention (carry the issue number in the `--no-ff` merge message).
  2. `parked` set = issues carrying `blocked` (clarify/block) within the work
     unit's membership, plus their transitive dependents.
  3. ready frontier = `get_ready()` after seeding `done`/`parked` from (1)/(2).

  The invariant that makes this unambiguous: **a merge commit on `feature/<slug>`
  naming issue N is the sole authority that N is done** (labels are advisory;
  git state is the record). Confirm this reconstruction is acceptable for v1, or
  confirm v1 ships **manual recovery only** (documented git/label steps, no
  `chain/recovery.py`) — `.tmp/arch-review-new-model.md:L130` notes the
  session-context "leans manual/scheduled." **Recommendation:** implement the
  label+git reconstruction (it is cheap and removes the manual-surgery footgun).
- **OQ-9 — Unblock detection: poll vs webhook (low-stakes).** v1 is poll-driven
  (the daemon re-reads `blocking` after each merge); no webhook infra. The
  `dependency_added`/`dependency_removed` webhook actions are unverified
  (`docs/research/2026-06-06-issue-dag-orchestration.md:L159`). **Recommendation:**
  poll for v1; revisit webhooks only if latency matters.

**Could not resolve from the sources (surfaced, not invented):**
- The exact `gh api` dependency-endpoint **response field names** (§3.1) — verify
  live during P1 before coding `gh_deps.py`.
- Whether the live issue #27 decision-log comments contain any constraint **not**
  carried into the merged design docs — I could not fetch them this session (no
  GitHub read tool). The reviewer/router should spot-check the two comments
  against §2–§10 before greenlight.

---

## 12. Phasing (proposal — reviewable slices → autonomous PRs)

Each phase is an independently mergeable PR into a `feature/daemon-orchestration`
integration branch (CLAUDE.md § Git Commits — primary + sub-branches). Sub-PRs
merge into the integration branch; the integration PR merges to `main`.

- **P0 — Vendor symphony + VP-1 + pyproject deps.** Copy `symphony/` into
  `src/baton_harness/vendor/symphony/`; apply **VP-1** (`run_hook env=`); declare
  the new runtime deps in `pyproject.toml` (**CONCERN-8**: add `pyyaml`, `jinja2`;
  **exclude `watchfiles`** since the poller is dropped — guard any import path that
  references it; `.tmp/arch-review-new-model.md:L142-L148`). Update `before_run.py`
  + `after_run.py` to read `CHAIN_BASE_BRANCH` from env (default `origin/main`).
  Tests: `tests/vendor/test_run_hook_env.py`, env-default regression tests on both
  hooks. **This is the P0 prerequisite — VP-1 + deps gate everything else.**
- **P1 — DAG read + scheduler + CI trigger (pure + config).** `gh_deps.py`,
  `dag.py`, `scheduler.py`, `registry.py` + tests; the `feature/**` CI trigger in
  `ci.yml` (§3.6, downstream of OQ-1). Lowest-risk code; proves the foundation.
- **P2 — branch + merge mechanics.** `branches.py` (feature-branch creation +
  feature-HEAD checkout, §3.4), `merge.py` (`--no-ff` + CI gate) + tests.
- **P3 — daemon loop + recovery + CLI + launcher + VP-2.** `daemon.py`,
  `recovery.py` (OQ-5), `cli.py` (`bh-daemon`), `bin/run-daemon.sh`; apply **VP-2**
  (terminal block + `state.json` guard); wire C1/C2/C3 (§8) and the §3.5 outcome
  protocol + Slack escalation. Driver/daemon tests. Update `docs/harness-design.md`
  §10 from "decided — not yet built" to "implemented (v1, serial)".

A GitHub **Milestone** ("Always-on daemon / #27") groups one tracking issue per
phase. Per CLAUDE.md § Issue Tracking, creating these issues is **not** permission
to start — await user confirmation.

---

## 13. Test plan

All tests follow the existing mock-subprocess pattern (patch each module's
`_run`/`gh api` seam; the ~1,376 lines of existing tests are the style reference).
No live-API integration tests in v1; `gh_deps` is tested against captured JSON
fixtures.

| Test file | Covers |
|---|---|
| `tests/vendor/test_run_hook_env.py` | VP-1: `run_hook` threads `env=` to the subprocess; `CHAIN_BASE_BRANCH` and `BH_VENV` reach the hook; default (no env) is unchanged. |
| `tests/vendor/test_exclude_labels_recheck.py` | VP-2: a mid-turn `blocked` label terminates the turn loop; the `running[N]`-missing guard does not raise. |
| `tests/chain/test_gh_deps.py` | `blocked_by`/`blocking`/milestone-membership parsing from fixture JSON; pagination; same-repo error surfacing; empty-graph case. |
| `tests/chain/test_dag.py` | Graph build from edges; membership scoping; **CycleError on a cyclic fixture** (§3.2). |
| `tests/chain/test_scheduler.py` | Ready frontier; `mark_done` unblocks dependents; `mark_parked` halts the **transitive** sub-tree; `is_active` termination; the `parked`-set filter on `get_ready()`. |
| `tests/chain/test_branches.py` | `feature/<slug>` created off `main`; feature branch checked out as HEAD before a worker call (§3.4 regression guard); idempotent on re-create. |
| `tests/chain/test_merge.py` | `--no-ff` (not squash) into the feature branch; CI gate blocks merge when checks not green; merge order is dependency order. |
| `tests/chain/test_recovery.py` | `done` reconstructed from merge commits on `feature/<slug>`; `parked` from `blocked` labels + transitive dependents; frontier seeded correctly (OQ-5). |
| `tests/chain/test_daemon.py` | End-to-end loop with mocked `_run_worker` returns: happy linear DAG; parallel-level DAG (serial dispatch); a mid-DAG block parks only its sub-tree and the daemon continues independent branches; `no_pr` → park + escalate (no retry); fully-parked → escalate + end DAG run, daemon stays alive; **never opens a non-draft `feature → main` PR / never merges to main** (hard-constraint regression guards); `agent-done` → `agent-merged` relabel after CI-gated merge (CONCERN-3). |

CI gate: ruff (79 cols), mypy strict, pytest must pass before merge
(`pyproject.toml:L52-L88`; `ci.yml`). New code carries Google-style docstrings and
full type hints. **The vendored `symphony/` tree** is included in lint/type/test
scope once vendored — `mypy src` (`ci.yml:L38`) will cover it; confirm symphony
passes mypy strict or scope it appropriately (P0 task).

---

## 14. Out of scope for v1

- Parallel dispatch within a DAG level (deferred to v2; §6).
- Cross-repo dependency work units (API limitation; §3.1 / §10).
- Auto-merge of the final `feature → main` PR (hard constraint — human only; §5).
- Intra-chain CI-rework loop and the `state.py` retry/backoff (no retry in v1;
  §3.5 / §7).
- `github/gh-stack` integration (private preview;
  `docs/research/2026-06-06-issue-dag-orchestration.md:L120-L127`) — revisit at GA.

---

## 15. Verification status of claims

Per CLAUDE.md § Cite Sources, load-bearing claims were verified against the cited
files this session:
- Hook topology, hardcoded `origin/main` rebase/cherry, "done = PR-opened" — read
  directly from `before_run.py`, `after_run.py`, `_cli.py`.
- CI triggers only on `main` — `.github/workflows/ci.yml:L4-L7`.
- Zero runtime deps / requires-python — `pyproject.toml:L13`, `:L17`.
- `max_turns: 2` workaround + #23-closed framing — `config/WORKFLOW.md:L10`;
  `harness-design.md:L148`, `:L177-L179`.
- Decided daemon/worker model, BLOCKING/OQ resolutions, single-repo gate —
  `docs/harness-design.md §10` (`:L189-L260`), `docs/architecture-spec.md §2/§3.4/§6/§9`.
- Symphony `_run_worker` seam, `run_hook` no-env, `WorkspaceManager` HEAD-based
  worktree, coordination-seam dissolution — `.tmp/baton-deepdive-findings.md`
  (line ranges inline).
- BLOCKING-1..4 + CONCERN-1..8 + NIT-2 — `.tmp/arch-review-new-model.md` (line
  ranges inline).
- Dependency API GA, MCP gap, graphlib, merge-commit finding, novelty —
  `docs/research/2026-06-06-issue-dag-orchestration.md` (line ranges inline).
- C1/C2/C3 — `docs/spike-findings.md:L131-L165`.

**unverified (surfaced in §11):**
- The exact `gh api` dependency-endpoint response field shapes (from the research
  report, not exercised live).
- Whether the live issue #27 decision-log comments carry any constraint absent
  from the merged design docs (no GitHub read tool available to the sub-agent;
  the merged docs were reformed *from* those comments on 2026-06-07 and are
  treated as authoritative).
- The preferred post-merge terminal label name (`agent-merged`) — OQ-3a.
