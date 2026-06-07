---
title: Always-on daemon — dependency-ordered work-unit orchestration (unified DAG) (#27)
touches:
  - src/baton_harness/vendor/symphony/**
  - src/baton_harness/vendor/symphony/hooks.py
  - src/baton_harness/vendor/symphony/orchestrator.py
  - src/baton_harness/vendor/symphony/VENDORING.md
  - patches/**
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

**Status:** Implementation-ready (Rev 2, 2026-06-07). The central architecture
(option (c) — vendor `symphony`, custom always-on daemon, unified DAG) is
**decided** in the merged design docs (`docs/harness-design.md §10`,
`docs/architecture-spec.md §2/§3.4/§6/§9`, both reformed 2026-06-07 to the
vendored model). This Rev 2 folds in all `project-reviewer`
(`.tmp/spec-review-chain.md`) and `inquisitor` (`.tmp/spec-inquisitor-chain.md`)
findings plus the user-decided resolutions for the open questions (OQ-1, OQ-3a,
OQ-5, OQ-9) and the concurrency contract (B-I3 — serialize all work units in v1).
Per CLAUDE.md § Issue Tracking, the phasing in §12 is a **proposal** — creating
the tracking issues/milestone is a separate, user-confirmed step, not permission
to start coding.

**Rev 2 decisions baked in (all final — applied, not re-opened):**
- **OQ-1:** feature branch = `feature/<milestone-slug>`; un-milestoned issue →
  `feature/issue-<N>`; CI trigger = `feature/**` glob (§3.6, §5, `branches.py`).
- **OQ-3a:** post-merge terminal label = `agent-merged` (§8 C1).
- **OQ-5:** crash recovery = auto-reconstruct, with the B-I2 provenance hardening
  (daemon-authored provenance marker + persisted CI-green-at-merge fact) (§11.5).
- **OQ-9:** unblock detection = **poll** (not webhook) in v1 (§9, §11.9).
- **B-I3 (concurrency):** the daemon serializes **all** work units in v1 — one
  work unit (one DAG) in flight across the whole repo at a time (§6, §10).

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
   (`.tmp/arch-review-new-model.md:L23-L33`). The fix is **not** a simple
   default-string swap: against a *moving* `--no-ff` feature branch the cherry
   base must be the worker branch's **cut-point merge-base**, frozen for the
   `_run_worker` window, not the live feature tip (B-I1, §3.7).
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
- **CI gate per merge:** a branch may merge only after its CI is green per the
  precise definition in §3.3.1 (`architecture-spec.md:L258`). This requires the
  `feature/**` CI trigger (§3.6).
- **The harness NEVER merges `feature → main`** (hard constraint, issue #27;
  `harness-design.md:L217`).

### 3.3.1 Definition of "green" for the CI gate (resolves C-I2)

"CI green" is the load-bearing predicate of the whole "satisfied = merged with
green CI" model, so `merge.py` defines it exactly
(`.tmp/spec-inquisitor-chain.md:L60-L67`). The gate queries the PR's check-runs
(`gh api .../commits/<sha>/check-runs`, cross-checked with `gh pr checks`) and
applies these rules:

- **Green = every REQUIRED check has `status: completed` and `conclusion` in
  {`success`}.** Only checks marked **required** on the branch-protection /
  repo-required set gate the merge. Optional (non-required) checks are **ignored**
  entirely, pass or fail.
- **`neutral` and `skipped` conclusions count as PASS** (a path-filtered or
  conditional required job that legitimately did not run must not block).
- **`failure` / `cancelled` / `timed_out` / `action_required` on any required
  check = RED** → park the issue's sub-tree + escalate (no merge).
- **In-progress / queued required checks (`status` in {`queued`, `in_progress`})
  = NOT YET** → the gate **polls** with bounded backoff (poll interval and a hard
  timeout configured in `WORKFLOW.md`; default 10 s interval, 30 min ceiling).
  On timeout (a required check never completes — e.g. no available runner), treat
  as **RED** → park + escalate (`ci-timeout`), never merge on incomplete signal.
- **A required check that never reports at all** (expected-but-absent in the
  check-runs set) is treated as NOT YET, then RED on timeout — never a vacuous
  "no failures = green."

`merge.py` records the resolved green/red/timeout decision; on a green merge it
**persists the CI-green-at-merge fact** as a marker comment + the `agent-merged`
label (§11.5/B-I2) so crash recovery does not have to re-derive it from a
possibly-garbage-collected check-run.

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
4. **Escalate** — post a stall summary card to `#agent-decisions`
   (`architecture-spec.md:L136-L139`) **and** mirror the same summary as a GitHub
   issue/PR comment so a dropped Slack card never loses the durable record (N-I3).

The vendored `state.py` retry/backoff is **unused** in v1. C3 (bounded rework with
escalation, `spike-findings.md:L153-L165`) is satisfied by the park+escalate path:
the bound is implicit (zero retries) and the escalation exit is the stall summary.

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
the crash-recovery reconstruction in §11.5/OQ-5).

**`after_run` must not leave `agent-ready` on a parked outcome (resolves
project-reviewer CONCERN-2, `.tmp/spec-review-chain.md:L199-L236`).** Today
`after_run.py:L335-L342` (Priority 3) *retains* `agent-ready` on
`COMMITTED_NO_PR` / `NO_COMMITS` with the comment "retryable — leaving agent-ready
for Baton retry." Under the daemon, leaving `agent-ready` would cause the outer
poll loop to re-detect the issue as ready and silently re-dispatch it — a "retry
without retry policy" that contradicts the no-retry-v1 decision. **v1 `after_run`
behavior is therefore explicit:**

- `after_run` removes `agent-ready` on **every** terminal classification,
  including `COMMITTED_NO_PR` and `NO_COMMITS`. The Priority-3 carry-forward of
  `agent-ready` is **deleted** for the daemon model (§4.3).
- On `COMMITTED_NO_PR` / `NO_COMMITS`, `after_run` sets **`blocked`** (not
  `agent-ready`) so the daemon's outcome protocol and the recovery classifier both
  see a parked state, and the daemon park+escalates. There is **no silent
  re-dispatch**.
- The daemon's outcome protocol additionally treats any `"no_pr"` return as a park
  regardless of the label state (defense-in-depth against a stale `agent-ready`),
  per the table above.

This makes the single label authority (`after_run` for terminal state, daemon for
post-merge `agent-merged`) consistent with the no-retry-v1 policy.

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
**downstream of OQ-1** (the glob must match the resolved naming convention §11.1,
`feature/<milestone-slug>` / `feature/issue-<N>`). This is a P1 prerequisite —
without it, the CI gate (§3.3) is unenforceable.

### 3.7 Feature-branch-freeze invariant + classifier base (resolves B-I1)

The `git cherry`/rebase classifier is structurally unsound against a *moving*
`--no-ff` feature branch if the base is taken as the live feature tip. Because v1
**serializes all work units** (§6, B-I3) and dispatches **one issue at a time
within a DAG** (§6), the feature branch is naturally frozen for the duration of a
single `_run_worker` call — but the spec makes that invariant explicit and pins
the classifier base accordingly:

1. **Freeze invariant.** `feature/<slug>` is **immutable for the entire
   `before_run → turn-loop → after_run → CI-gate → merge` window of a single
   issue.** Serial-per-DAG dispatch (§6) guarantees no sibling merge can advance
   the feature tip mid-window: the daemon merges issue N into `feature/<slug>`
   only *after* N's full window closes, and dispatches N+1 only *after* that merge.
   No second worker is ever in flight. This is the load-bearing precondition that
   makes the HEAD-checkout mechanism (§3.4) and the classifier base sound.

2. **Cut-point base, not live tip.** When the daemon cuts the worker branch
   (`baton/<slug>-<N>`) off the current `feature/<slug>` HEAD, that HEAD is the
   worker's **cut-point merge-base**. `before_run` rebases the worker branch onto
   `CHAIN_BASE_BRANCH=feature/<slug>` — which, under the freeze invariant, has not
   moved since cut, so the rebase is a no-op fast-forward in the common case.
   `after_run` classifies with `git cherry <cut-point-base> HEAD`, where the base
   is the **recorded cut-point ref** (the feature SHA captured at worktree
   creation), NOT a live `git cherry origin/main` and NOT a live `feature/<slug>`
   re-read. The daemon passes the cut-point SHA to the hook via
   `CHAIN_BASE_BRANCH` (VP-1); since the branch is frozen, the ref name and the
   cut-point SHA are equivalent for the window. `after_run` resolves
   `CHAIN_BASE_BRANCH` to a concrete SHA at hook entry and uses that SHA for the
   rest of the run so a (theoretical) mid-window move cannot perturb it.

3. **Re-dispatch after unblock — re-cut from the current tip.** When a parked
   issue is unblocked (§9 step 4) and re-dispatched at a later time, the feature
   branch has by then absorbed sibling merges. The daemon does **not** replay the
   stale worker branch onto a moved tip. Instead it **re-cuts the worker branch
   fresh from the current `feature/<slug>` tip** (a new cut-point base) and runs a
   clean `_run_worker`. The agent re-reads the issue (including the human's
   guidance comment) and works from the up-to-date integration state. The
   classifier base for the re-dispatched run is the **new** cut-point, not the
   original. Any commits from the failed prior attempt are abandoned with the stale
   worktree (recovery reconstructs `done`/`parked` from git+labels, §11.5, not from
   orphan worker branches).

This makes the classifier base explicit (the recorded cut-point merge-base) at all
times, never a live `git cherry origin/main`, and resolves the B-I1 charge that the
`CHAIN_BASE_BRANCH` swap alone does not repair the moving-base defect.

**`before_run` rebase failure under the frozen base (addresses C-I1,
`.tmp/spec-inquisitor-chain.md:L50-L57`).** Symphony's `before_run` is best-effort
(non-gating: its return is logged, not used to abort the turn loop,
`orchestrator.py:L115-119`). The C-I1 worry is that a failed rebase leaves the
agent running on a stale base. Under the freeze invariant this is structurally
defused: because `feature/<slug>` has **not moved** since the worker branch was cut
from it (serial dispatch, §6), the `before_run` rebase onto the cut-point base is a
no-op fast-forward — there is no sibling commit to conflict with. A rebase conflict
is therefore not a normal-path event. The classifier base being a **recorded SHA**
(not a live re-read) means even a (theoretical) non-gating `before_run` failure
cannot mis-point the `after_run` classifier: it still measures against the cut-point
the worker was actually built on. The `--untracked-files=no` flag in `_classify`
stays correct — the agent's contract is to commit its work, so an uncommitted-only
result is a legitimate `NO_COMMITS` → park (the intended escalation), not a false
negative introduced by the daemon model.

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
| `chain/branches.py` | **Owns** the feature-branch lifecycle: create `feature/<milestone-slug>` (or `feature/issue-<N>`) off `main`; **`git -C <repo_root> checkout feature/<slug>` immediately before each `_run_worker` call** so the repo-root HEAD is the feature branch when symphony's `git worktree add … HEAD` runs (BLOCKING-1, §3.4); record the cut-point SHA for the classifier base (§3.7). | `git` via subprocess |
| `chain/merge.py` | Query CI for a per-issue PR; `--no-ff` merge into `feature/<slug>` only when **green per the §3.3.1 definition**; carry the issue number + a daemon-provenance marker in the merge-commit message; persist the CI-green-at-merge fact (`agent-merged` label + marker comment, §11.5/B-I2). | `gh pr checks` / `gh api …/check-runs`; `git merge --no-ff` |
| `chain/registry.py` | Repo-registry: the daemon poll loop iterates this (one entry in v1; repo #2 = append — the multi-repo seam, §10). v1 also serializes **work units** within the single repo (§6/§10). | none (config read) |
| `chain/recovery.py` | Crash-recovery: reconstruct the scheduler `done`/`parked` sets (OQ-5, §11.5). `done` only from **daemon-provenance** merge commits with the persisted CI-green marker; `parked` from `blocked` labels + transitive dependents; the `agent-done`+open-PR rule (re-enter CI gate); the `agent-in-progress`-orphan rule (re-evaluate, not done). | `gh issue list`, `gh api …/check-runs`, `git log feature/<slug>` |
| `chain/daemon.py` | The always-on outer loop: poll registry → detect **the one** ready work unit → build DAG → per-DAG loop (frontier → `branches.py` checkout feature HEAD → set `agent-in-progress` → `_run_worker` → outcome protocol §3.5 → CI-gate+merge → `mark_done` + remove `agent-in-progress` + `agent-merged`) → draft `feature → main` PR on completion → escalation. Processes **one work unit to completion at a time** (B-I3, §6). Never exits on a block. Removes `agent-in-progress` on every terminal outcome (C-I4). | composes the above + `_run_worker` + label edits |
| `chain/cli.py` | `bh-daemon` console entry point; wires the daemon from `WORKFLOW.md` config + repo registry. | argparse |

### 4.2 The vendored worker — `src/baton_harness/vendor/symphony/` (two patches)

| Patch | File | Change | Why |
|---|---|---|---|
| **VP-1 (P0)** | `vendor/symphony/hooks.py` | `run_hook` gains `env: dict \| None = None`; passed through to `asyncio.create_subprocess_exec`. **The override dict is MERGED into `os.environ` (`env = {**os.environ, **overrides}`), never passed as overrides-only.** Daemon passes overrides `{"CHAIN_BASE_BRANCH": <cut-point SHA>, "BH_VENV": <venv>}`. | Threads the classifier/rebase base to `before_run`/`after_run` (B2/B4) **and** `BH_VENV` for hook discovery. The current `run_hook` passes no `env=` (`.tmp/baton-deepdive-findings.md:L423`); a bare `env=overrides_only` would replace the child environment entirely, stripping `PATH`/`HOME`/etc. so `git`/`gh` silently become unresolvable (CONCERN-1, `.tmp/spec-review-chain.md:L169-L196`). |
| **VP-2** | `vendor/symphony/orchestrator.py` | Re-check `exclude_labels` inside the `_run_worker` turn loop after `fetch_issue_state` (`.tmp/baton-deepdive-findings.md:L343-L348`); also guard the `L129` turn-tracking mutation against a missing `running[N]` entry (CONCERN-4). | Makes a block terminal (closes the #23 root cause), retiring the `max_turns: 2` workaround; prevents `state.json` corruption. |

**Only these two patches.** No naming patch (§3.4 base-ref approach resolves
CONCERN-1). No retry wiring (no retry in v1, §3.5).

### 4.3 The existing flat hooks — `src/baton_harness/` (env-aware)

| File | Change | Why |
|---|---|---|
| `before_run.py` | Read `CHAIN_BASE_BRANCH` from env (default `origin/main`); resolve it to a concrete SHA at entry; rebase onto that SHA. | BLOCKING-4 — the hardcoded `git rebase origin/main` (`before_run.py:L107`) corrupts feature-branch runs. The freeze invariant (§3.7) means the resolved SHA is the cut-point base. |
| `after_run.py` | (1) Read `CHAIN_BASE_BRANCH` from env (default `origin/main`), resolve to the **cut-point SHA** at entry, and use that SHA for the `git cherry` base (`after_run.py:L158`) for the whole run (§3.7, B-I1). (2) **Delete the Priority-3 `agent-ready` carry-forward** (`after_run.py:L335-L342`): on `COMMITTED_NO_PR` / `NO_COMMITS`, remove `agent-ready` and set `blocked` (CONCERN-2) — never leave `agent-ready` for a re-poll re-dispatch. (3) **Must NOT query CI** even in chain contexts (CI gate lives in `merge.py`; CONCERN-7, `.tmp/arch-review-new-model.md:L134-L138`). | BLOCKING-2 + CONCERN-2 — the hardcoded `git cherry origin/main HEAD` mis-classifies feature-branch outcomes and the retry-carry-forward causes silent re-dispatch under the no-retry-v1 policy. `after_run` does **not** know about `agent-in-progress` — the daemon removes that label (C-I4, §8). |
| `_cli.py` | Optional: add a shared `run()` subprocess helper reused by `chain/` modules (each hook currently has its own `_run`). `resolve_issue_number` is unchanged — the daemon resolves from the bare-integer worktree dir (§3.4). | Consolidation; not load-bearing. |

### 4.4 Supporting files

- `bin/run-daemon.sh` — launcher analogous to the old `bin/run.sh`: resolves
  harness root, exports `BH_VENV`, label preflight, then runs the daemon. The
  `baton start -w` invocation is gone.
- **`bin/run.sh` — DELETE (obsolete post-vendoring; NIT).** It launched the
  external `baton start -w` poller, which no daemon model uses
  (`harness-design.md:L106`). It is superseded by `bin/run-daemon.sh` and must be
  removed in the same phase that lands `bin/run-daemon.sh` (P3), not left as a
  dead second entry point. Listed in `touches:` implicitly via the launcher swap.
- **`patches/` + `vendor/symphony/VENDORING.md` — vendor patch-management story
  (C-I3, `.tmp/spec-inquisitor-chain.md:L70-L77`).** Because upstream is *dormant,
  not dead* (monthly monitoring per the main-checkout CLAUDE.md), a re-vendor is a
  live possibility and the ~5-line VP-1 / ~10-line VP-2 edits must be (a)
  re-applicable and (b) auditable as deliberate harness divergence. The chosen
  approach (§7.1): a committed `patches/` directory holding one `.diff` per vendor
  patch (`patches/VP-1-run-hook-env.diff`, `patches/VP-2-exclude-labels-recheck.diff`)
  plus `# VENDOR-PATCH VP-N:` source-comment markers at each edit site, and a
  `vendor/symphony/VENDORING.md` recording the upstream SHA pinned at vendor time
  and a re-vendor checklist. The mypy-strict remediation (CONCERN-3) is recorded
  as a separate, non-behavioral patch so it stays distinguishable from the two
  behavior patches.
- `config/WORKFLOW.md` — **kept** (not a new chain variant). Its YAML front-matter
  is no longer parsed by an external poller, but its agent-prompt body and the
  hook wiring remain the instruction template (`harness-design.md:L64`). The
  `max_concurrent` value here is the documented single-repo concurrency budget
  (§10 seam (b)). **No `config/WORKFLOW.chain.md`** (OQ-3 moot — single daemon).
  **When VP-2 lands (P3), update the `max_turns: 2` value + its workaround comment
  (`config/WORKFLOW.md:L10`)** — the terminal-block fix retires the workaround, so
  `max_turns` is raised to reflect real work complexity and the stale comment is
  removed (NIT-3, `.tmp/spec-review-chain.md:L330-L340`).

---

## 5. The daemon run / merge loop

Feature-branch naming (OQ-1 resolved, §11.1): `feature/<milestone-slug>` for a
milestone work unit; `feature/issue-<N>` for an un-milestoned N=1 work unit (the
issue number is the collision-free key — NIT-1). Per-issue worktree/branch naming
follows symphony (`.symphony/worktrees/<N>`, `baton/<slug>-<N>`), §3.4.

**Serialization (B-I3, §6):** the outer loop processes **exactly one work unit to
completion at a time** across the whole repo. Per-DAG runs are **sequential
`await`s in the daemon loop, never concurrent `asyncio.Task`s** — so the shared
repo-root HEAD is only ever checked out to one feature branch at a time, which is
the precondition that makes the HEAD-checkout worktree mechanism (§3.4) and the
classifier-base freeze (§3.7) sound.

```
DAEMON (always-on; never exits on a block; ONE work unit in flight):
LOOP forever:
  poll registry (§10) for ready work units:
    - a milestone with >=1 `agent-ready` issue, OR
    - an un-milestoned `agent-ready` issue (its own N=1 unit)
  select ONE ready work unit (B-I3 — strictly serial; the rest wait their turn).
  await the per-DAG run to COMPLETION before selecting the next work unit.

  PER-DAG RUN (sequential await, NOT a concurrent Task):
  0. PRECONDITION: read membership (milestone) + edges (blocked_by) → build DAG.
     scheduler.prepare() — CycleError → escalate, skip this unit.
  1. Create-or-resume feature branch (idempotent w.r.t. recovery, §11.5/OQ-5):
       git -C <repo> fetch origin main
       if feature/<slug> exists (restart): recovery.reconstruct() →
           seed scheduler done/parked from git-provenance + labels (§11.5)
       else:
           git -C <repo> branch feature/<slug> origin/main
           git -C <repo> push -u origin feature/<slug>
  2. WHILE scheduler.is_active():
     a. frontier = scheduler.get_ready()  minus  parked sub-tree
     b. if frontier empty and work still pending → fully parked:
        escalate (Slack card + GitHub issue comment — N-I3 durable record);
        push feature/<slug>; open the draft PR (step 3) and EXIT this DAG run.
        The unblock→re-dispatch path is the outer poll loop re-entering this work
        unit via recovery (BLOCKING-2 / §9 step 4) — NOT a long-lived wait state.
     c. pick ONE ready issue N (serial — §6):
        - branches.py: git -C <repo_root> checkout feature/<slug>  (HEAD = feature
              branch §3.4); record cut-point SHA = feature/<slug> tip (§3.7)
        - re-cut a FRESH worker base from the current feature tip (the cut-point);
              a re-dispatched (post-park) issue is cut anew, never replayed (§3.7.3)
        - transition N's label: remove agent-ready, add agent-in-progress (§8 C1)
        - result = await orch._run_worker(N)         (vendored worker)
          · worker creates .symphony/worktrees/<N> off feature HEAD (cut-point)
          · before_run resolves CHAIN_BASE_BRANCH → cut-point SHA; rebases (VP-1)
          · claude -p turn-loop (VP-2 makes a block terminal)
          · after_run classifies vs the cut-point SHA; sets agent-done|blocked;
              removes agent-ready on EVERY terminal outcome (CONCERN-2)
        - apply the §3.5 outcome protocol to (result, blocked-label):
          · pr_created + green CI (§3.3.1 def) → merge.py --no-ff into
                feature/<slug> with a daemon-provenance merge message (§11.5);
                scheduler.mark_done(N); remove agent-in-progress + agent-done;
                add agent-merged + CI-green marker comment (§8 C1, §11.5)
          · pr_created + red CI   → scheduler.mark_parked(N); remove
                agent-in-progress; escalate
          · blocked / no_pr       → scheduler.mark_parked(N); remove
                agent-in-progress; escalate
        NOTE: agent-in-progress is removed on EVERY terminal branch above
        (success, park-red, park-blocked, park-fail) — never orphaned (C-I4, §8).
  3. COMPLETION: when no active nodes remain, push feature/<slug>; open ONE draft
     PR feature/<slug> → main. PR body: issues merged (PR/commit refs), issues
     parked (reasons), escalation summary. Claude attribution line. The harness
     NEVER merges this PR. Then return to the outer loop and select the next work
     unit.
```

**Unblock → re-dispatch mechanism (resolves BLOCKING-2,
`.tmp/spec-review-chain.md:L100-L163`).** A fully-parked DAG does **not** hold a
live coroutine waiting for a label change. Instead:

1. The per-DAG run **exits** at step 2b: it pushes the feature branch, opens (or
   updates) the draft `feature → main` PR, and ends. The in-memory scheduler is
   discarded.
2. The **human** resolves a parked issue: posts guidance on the GitHub issue and
   **removes `blocked`, then re-adds `agent-ready`** to signal the daemon (the
   human re-adds `agent-ready` because `after_run` strips it on park; this is the
   explicit re-dispatch trigger — the daemon does not infer intent from a bare
   `blocked` removal).
3. The daemon's **outer poll loop** re-detects the work unit as ready (the
   milestone again has a `≥1 agent-ready` issue) and **re-enters a per-DAG run**.
4. Re-entry runs `recovery.reconstruct()` (step 1) to rebuild `done`/`parked` from
   git-provenance + labels (§11.5), seeds the scheduler, and resumes the frontier.
   The unblocked issue is re-cut fresh from the current feature tip (§3.7.3).

This makes `chain/recovery.py` load-bearing for the **live unblock path**, not
only crash recovery — the two paths share one reconstruction algorithm (§11.5).

---

## 6. Concurrency — serialize ALL work units in v1 (B-I3 DECIDED)

**Decision (final, B-I3, `.tmp/spec-inquisitor-chain.md:L36-L43`): v1 serializes
all work units.** The daemon processes **one work unit (one DAG) at a time across
the whole repo** — no concurrent feature-branch checkouts. Within that single
in-flight DAG it also dispatches **one issue at a time**, even where the DAG
permits parallelism.

Two levels of serialization, both load-bearing:

1. **Across work units (B-I3).** The shared repo-root HEAD and the
   `.symphony/worktrees/<N>` namespace live in a single `project_root`. Two work
   units interleaving — even cooperatively across `await` points in one event loop
   — would let work unit A's `git worktree add … HEAD` branch off work unit B's
   checked-out feature HEAD, silently corrupting base refs. The inquisitor charge
   was that the spec resolved this by omission; v1 resolves it by **decision**: the
   outer loop selects one ready work unit and `await`s it to completion before
   selecting the next. There is **no** "one Task per ready work unit." This makes
   the `git checkout feature/<slug>` HEAD approach (§3.4) safe by construction —
   only one feature branch is ever checked out at any instant.

2. **Within a DAG (serial per-DAG).** One in-flight issue per work unit at a time
   (`harness-design.md:L258`): the daemon is the unambiguous single label writer
   (C1), no concurrent claim races, no lock (OQ-8 moot), and the §3.7 freeze
   invariant holds.

**Concurrency across work units is a v2 extension** requiring base-ref
parameterization (VP-3 — e.g. a dedicated staging worktree per feature branch, or
separate clones per work unit, so each work unit owns an isolated HEAD). The
scheduler already returns the whole ready set, so parallel-within-a-DAG-level is
also a clean v2 extension. Both are deferred (§14).

The cross-repo / cross-work-unit concurrency budget (`max_concurrent` in
`WORKFLOW.md`) is a **documented decision, not an in-daemon code object** (§10
seam (b); `architecture-spec.md:L253`). A `GlobalBudget` abstraction is explicitly
the wrong seam here, and in v1 it is moot — the cap is effectively 1.

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

**No third behavior patch.** `state.py` retry/backoff is unused (no retry, §3.5);
`WorkspaceManager` naming is left as-is (§3.4 base-ref approach). The mypy-strict
remediation is a separate non-behavioral patch, not a third behavior patch (§7.1).

### 7.1 Patch-management story (resolves C-I3)

The two behavior patches are surgical edits buried in ~1120 lines of vendored
third-party code. Without a management story they become indistinguishable from
formatting churn and unrecoverable across a re-vendor (upstream is *dormant, not
dead* — monthly monitoring per the main-checkout CLAUDE.md, so a re-pull is a live
possibility). The approach (`.tmp/spec-inquisitor-chain.md:L70-L77`):

- **Committed patch series in `patches/`.** One `.diff` per patch:
  `patches/VP-1-run-hook-env.diff`, `patches/VP-2-exclude-labels-recheck.diff`,
  and `patches/mypy-strict-remediation.diff` (the CONCERN-3 remediation, kept
  **separate** so the two load-bearing behavior patches stay auditable apart from
  the bulk type-annotation churn).
- **Source-comment markers.** Each edit site carries `# VENDOR-PATCH VP-N: <one-
  line why>` so a future reader can grep `VENDOR-PATCH` and find every harness
  divergence in the vendored tree.
- **`vendor/symphony/VENDORING.md`.** Records the upstream commit SHA pinned at
  vendor time, the list of applied patches, and a **re-vendor checklist**: re-copy
  upstream at the new SHA, re-apply each `patches/*.diff`, resolve collisions,
  re-run the marker grep to confirm every patch landed, update the pinned SHA.

This makes VP-1/VP-2 both re-applicable and auditable, the explicit C-I3 ask.

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

  **The label state machine (single-state invariant preserved):**

  | Label | Written by | Cleared by | Means |
  |---|---|---|---|
  | `agent-ready` | human (or daemon re-trigger via human) | daemon at dispatch; `after_run` on any terminal | queued for the daemon |
  | `agent-in-progress` | **daemon** at dispatch | **daemon** on **every** terminal outcome (§5 step 2c NOTE; C-I4) | a worker is currently running this issue |
  | `agent-done` | `after_run` (PR opened, CI unverified) | daemon on green-merge (→ `agent-merged`) | PR open, not yet merged into feature branch |
  | `blocked` | `after_run` (block / `COMMITTED_NO_PR` / `NO_COMMITS`) | human (resolve) | parked — needs human guidance/debug |
  | `agent-merged` | **daemon** after CI-gated `--no-ff` merge (OQ-3a) | terminal | merged into feature branch with green CI (provenance recorded, §11.5) |

  **`agent-in-progress` lifecycle (resolves C-I4, `.tmp/spec-inquisitor-chain.md:L80-L87`).**
  The daemon writes `agent-in-progress` at dispatch and **MUST remove it on every
  terminal outcome** (`agent-merged` success, red-CI park, blocked park, no_pr
  fail). `after_run` (firing inside `_run_worker`) does **not** know about
  `agent-in-progress` — it only manages the three labels it always has — so the
  daemon is solely responsible for clearing it after `_run_worker` returns. This
  preserves the single-state invariant `after_run._reconcile_labels` enforces:
  after a run completes, the issue carries exactly one of
  {`agent-ready`, `agent-done`, `blocked`, `agent-merged`} and **never** a leftover
  `agent-in-progress`. The recovery classifier (§11.5) adds an explicit rule for an
  issue stuck `agent-in-progress` at restart (crash mid-run): **re-evaluate, not
  done** — treat as not-yet-dispatched, clear the orphan label, and let
  `get_ready()` re-dispatch it (the worker is idempotent against a half-built
  worktree because §3.7.3 re-cuts the worker branch fresh).

  **CONCERN-3 — `agent-done` vs the merge gate — RESOLVED (OQ-3a final).**
  **`agent-done` = "PR opened, CI unverified"** (the existing flat-run meaning,
  `after_run.py:L279-L282`). After the CI-gated `--no-ff` merge succeeds, the
  daemon **relabels to the distinct terminal label `agent-merged`** (decided —
  OQ-3a), removing `agent-done`. The two labels mean different things (PR-open vs
  merged-into-feature-branch with green CI), so there is no conflict and
  `after_run`'s contract is unchanged. A red-CI issue keeps `agent-done` (the PR is
  open) but is parked; the daemon's `parked` set, not the label, gates re-dispatch.

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
| 2 | Daemon | Detects `blocked` via the outcome protocol (§3.5); **parks the affected sub-tree** (the issue + its transitive dependents); removes `agent-in-progress` (§8 C-I4); posts a stall summary card to `#agent-decisions` on Slack **and posts the same summary as a comment on the issue / the draft PR** so a dropped Slack card never loses the durable record (N-I3, `.tmp/spec-inquisitor-chain.md:L100-L103`). If the whole DAG is now parked, the per-DAG run exits (§5 step 2b); independent sibling work continues only within the *current* work unit (cross-work-unit work is serialized — §6/B-I3). |
| 3 | Human | Reads the card/comment; posts guidance **directly on the GitHub issue**; removes `blocked`; **re-adds `agent-ready`** to re-arm the issue for the daemon (explicit re-dispatch trigger — §5). |
| 4 | Daemon | Outer poll re-detects the work unit as ready; re-enters a per-DAG run via `recovery.reconstruct()` (rebuilds `done`/`parked` from git-provenance + labels, §11.5); the unblocked issue is **re-cut fresh from the current feature tip** (§3.7.3) and the agent reads the guidance comment on its next run. |

The **GitHub issue is the durable record**; **Slack is the channel** only
(`architecture-spec.md:L91`). The two-dial confidence model
(`architecture-spec.md:L170-L191`) is the threshold: Dial 1 (agent asks readily)
is the prompt's confidence/block rule (`config/WORKFLOW.md:L24-L31`); Dial 2 (which
asks reach Slack) is the daemon's escalation filter. **Slack is the channel, not
the record** — every escalation also lands as a GitHub issue/PR comment so a
dropped Slack card never silently loses a stalled work unit (N-I3). "Normal resume"
is the next poll plus the human re-adding `agent-ready` (§5 unblock mechanism); the
**crash-recovery** path is §11.5/OQ-5 and shares the same reconstruction algorithm.

---

## 10. Single-repo, serial-work-unit daemon v1 — multi-repo + concurrency deferred

**One registry entry = one repo (v1).** The binding constraint is the GitHub
dependency API (`blocked_by`/`blocking`), which is **same-repo only**
(`docs/research/2026-06-06-issue-dag-orchestration.md:L42`); a work unit cannot
span repositories by construction (`architecture-spec.md:L331`;
`harness-design.md:L249-L253`).

**v1 also serializes work units *within* a repo (B-I3): one DAG in-flight at a
time** (§6). A single repo trivially has multiple milestones, so "one registry
entry" is a repo count, not a work-unit count — without the serialization decision
an implementer could launch one `asyncio.Task` per ready work unit and corrupt the
shared repo-root HEAD. v1 forbids that: the daemon processes one work unit to
completion before selecting the next.

**Concurrency across work units is a v2 extension** requiring base-ref
parameterization (VP-3 — isolating each work unit's HEAD via a dedicated staging
worktree per feature branch or separate clones). Multi-repo and intra-repo
work-unit concurrency are both deferred behind two clean seams:

1. **Repo-registry (`chain/registry.py`).** The daemon poll loop iterates a
   one-entry repo-registry rather than closing over a single `project_root`.
   Repo #2 = a registry **append**, not a loop rewrite.
2. **Concurrency budget as a documented decision.** `max_concurrent` lives in
   `WORKFLOW.md` (`harness-design.md:L253`), **not** as an in-daemon `GlobalBudget`
   code object. In v1 the effective cap is **1** (serial work units). Real
   cross-repo/cross-work-unit enforcement belongs to a future supervising/lease
   layer (the VP-3 v2 work) — two daemons each honoring `max_concurrent=2` would
   allow 4 streams, and the seam keeps that honest rather than pretending a
   per-daemon object enforces a global cap.

---

## 11. Decision record + residual open items

The open questions are **resolved** (user-decided, 2026-06-07). They are recorded
here as decisions, not re-opened. Only two genuinely-deferred items and two
verify-before-coding items remain open at the bottom.

### 11.1 OQ-1 — Feature-branch naming — DECIDED

`feature/<milestone-slug>` for a milestone work unit; **`feature/issue-<N>`** for
an un-milestoned N=1 work unit. The issue number (not the title slug) keys the N=1
branch, which is **collision-free by construction** (resolves NIT-1 — two
similarly-titled milestones cannot collide because the milestone slug is the key
and a milestone-slug tie-breaker can append the milestone number if ever needed).
The `feature/**` CI glob (§3.6) matches both forms. `branches.py` owns this
naming.

### 11.2 OQ-3a — Post-merge terminal label — DECIDED

**`agent-merged`** (§8 C1). Written by the daemon after the CI-gated `--no-ff`
merge, removing `agent-done`. The provenance marker + CI-green fact are persisted
alongside (§11.5).

### 11.5 OQ-5 — Crash + unblock recovery reconstruction — DECIDED (auto-reconstruct, provenance-hardened)

`chain/recovery.py` auto-reconstructs the scheduler `done`/`parked`/frontier state.
This path serves **both** crash recovery (daemon dies mid-DAG) **and** the live
unblock re-entry (§5, §9 step 4). The B-I2 charge — that the original "a merge
commit naming N is the sole authority N is done" invariant is unprovable from git
alone — is resolved by **requiring daemon provenance + a persisted CI-green fact**,
not by trusting merge-message text. Reconstruction rules, in precedence order:

1. **`done`** = issues whose per-issue work merged into `feature/<slug>` **via a
   daemon-authored merge carrying a recorded provenance marker AND a persisted
   CI-green-at-merge fact.** Specifically:
   - The merge commit must carry the daemon-provenance marker the daemon writes at
     merge time (a structured trailer in the `--no-ff` merge message, e.g.
     `Baton-Harness-Merge: issue-<N> ci=green`, plus the `agent-merged` label and a
     marker comment, §3.3.1). A **human** `git merge --no-ff` during the review
     window produces no such marker and is therefore **NOT** read as done
     (closes the B-I2 forgeability gap; aligns recovery with the C2 provenance
     allowlist, §8).
   - **CI-green-at-merge is read from the persisted marker** (`agent-merged` label
     / marker comment), **not inferred from git history** and **not re-derived by
     re-querying check-runs** (which may have been garbage-collected). If the
     marker is absent but a daemon merge commit exists (a daemon that died after
     merging but before writing the marker), classify the issue under rule (3a),
     not as done.
2. **`parked`** = issues carrying `blocked` within the work unit's membership, plus
   their transitive dependents.
3. **Intermediate-state rules (resolve CONCERN-4 + C-I4):**
   - **(3a) `agent-done` + open PR + no daemon-provenance merge commit** → the
     agent finished but the CI-gate/merge was interrupted. **Re-enter the CI gate
     (`merge.py`) without re-running `_run_worker`.** This is the most common crash
     point (daemon dies between `_run_worker` returning and `merge.py` finishing).
   - **(3b) `agent-in-progress` orphan (crash mid-`_run_worker`)** → matches none of
     the above (no merge, no `blocked`, no terminal label). **Re-evaluate, not
     done:** clear the orphan `agent-in-progress`, treat the issue as
     not-yet-dispatched, and let `get_ready()` re-dispatch it. The worker is
     idempotent against a half-built worktree because §3.7.3 re-cuts the worker
     branch fresh from the current feature tip (C-I4).
4. **ready frontier** = `get_ready()` after seeding `done`/`parked` from (1)/(2)
   and routing (3a)/(3b) issues to their respective handling.

Invariant (corrected): **only a daemon-authored, CI-green-marked merge is
authoritative that N is done** — labels are advisory and a bare merge commit is not
sufficient. A `--no-ff` (not squash) merge is what the daemon always emits (§3.3);
a stray human squash leaves no `--merges` entry and falls to rule (3b)/re-dispatch
rather than a silent under-count, which is the safe direction.

### 11.9 OQ-9 — Unblock detection: poll vs webhook — DECIDED (poll)

**Poll for v1.** The daemon re-reads `blocking`/labels on each outer-loop tick; no
webhook infrastructure. The `dependency_added`/`dependency_removed` webhook actions
are `unverified:` (`docs/research/2026-06-06-issue-dag-orchestration.md:L159`).
Webhooks are a v2 latency optimization (§14).

### 11.x Residual open items (genuinely deferred or verify-before-coding)

**Deferred to v2 (out of scope, §14):**
- Cross-work-unit / cross-repo concurrency (VP-3 base-ref parameterization) — §6,
  §10.
- Webhook-driven unblock detection — §11.9.

**Verify before coding (not blocking the spec, but gates the relevant phase):**
- The exact `gh api` dependency-endpoint **response field names** (§3.1) — confirm
  with a live `gh api` call **before** writing `gh_deps.py` fixtures, so the
  scheduler/DAG tests are not written against a guessed field name (N-I2). This
  should gate **entry to P1**, not happen inside it.
- The exact set of **required CI checks** on the target repo's `feature/**`
  protection (§3.3.1) — read from branch protection / repo config before coding
  `merge.py`'s green predicate (P2).
- `unverified:` whether the live issue #27 decision-log comments carry any
  constraint absent from the merged design docs — not fetchable this session (no
  GitHub read tool). The router should spot-check the two comments against §2–§10
  before greenlight.

---

## 12. Phasing (proposal — reviewable slices → autonomous PRs)

Each phase is an independently mergeable PR into a `feature/daemon-orchestration`
integration branch (CLAUDE.md § Git Commits — primary + sub-branches). Sub-PRs
merge into the integration branch; the integration PR merges to `main`.

- **P0 — Vendor symphony + VP-1 + patch-management + pyproject deps + mypy
  scoping.** Copy `symphony/` into `src/baton_harness/vendor/symphony/` at a pinned
  upstream SHA; apply **VP-1** (`run_hook env=`, **merge-into-`os.environ`** per
  CONCERN-1); establish the patch-management story (§7.1): `patches/VP-1-*.diff`,
  `# VENDOR-PATCH` markers, `vendor/symphony/VENDORING.md` (C-I3). Resolve the
  **mypy-strict scope** for the vendored tree — recommended path: add a
  `[[tool.mypy.overrides]]` `ignore_errors = true` for `baton_harness.vendor.*`
  (or exclude `vendor/` from `mypy src`), recorded as the separate
  `patches/mypy-strict-remediation.diff` (CONCERN-3, `.tmp/spec-review-chain.md:L239-L261`)
  — full annotation deferred. Declare new runtime deps in `pyproject.toml`
  (**CONCERN-8**: add `pyyaml`, `jinja2`; **exclude `watchfiles`** since the poller
  is dropped — guard any import path referencing it). Update `before_run.py` +
  `after_run.py` to read `CHAIN_BASE_BRANCH` (default `origin/main`), resolve to a
  cut-point SHA at entry (§3.7), and **delete the Priority-3 `agent-ready`
  carry-forward** (CONCERN-2). Tests: `tests/vendor/test_run_hook_env.py` (incl.
  env-merge-not-replace), env-default regression tests on both hooks, a
  Priority-3-no-longer-leaves-`agent-ready` regression. **P0 prerequisite — VP-1 +
  deps gate everything else.**
- **P1 — DAG read + scheduler + CI trigger (pure + config).** `gh_deps.py`,
  `dag.py`, `scheduler.py`, `registry.py` + tests; the `feature/**` CI trigger in
  `ci.yml` (§3.6). **Entry gate (N-I2):** confirm the live `gh api` dependency
  field names before writing fixtures (§11.x). Lowest-risk code; proves the
  foundation.
- **P2 — branch + merge mechanics.** `branches.py` (feature-branch creation +
  **repo-root checkout of `feature/<slug>` before each `_run_worker`**, §3.4/§3.7
  freeze; cut-point SHA recording), `merge.py` (`--no-ff` + the §3.3.1 green
  predicate + daemon-provenance marker + CI-green persistence) + tests. **Entry
  prerequisite (C-I2):** read the repo's required-CI-check set before coding the
  green predicate (§11.x).
- **P3 — daemon loop + recovery + CLI + launcher + VP-2.** `daemon.py` (serial
  work-unit outer loop, B-I3; `agent-in-progress` set/clear, C-I4),
  `recovery.py` (§11.5 — provenance-hardened reconstruction, the 3a CI-gate-reentry
  rule, the 3b `agent-in-progress`-orphan rule), `cli.py` (`bh-daemon`),
  `bin/run-daemon.sh` (and **delete `bin/run.sh`**, NIT); apply **VP-2** (terminal
  block + `state.json` guard) as `patches/VP-2-*.diff` + markers; **raise
  `max_turns` + remove the stale workaround comment in `config/WORKFLOW.md`**
  (NIT-3); wire C1/C2/C3 (§8) + the §3.5 outcome protocol + the unblock re-dispatch
  mechanism (§5/§9) + escalation (Slack **and** issue-comment durable record,
  N-I3). Driver/daemon tests. Update `docs/harness-design.md` §10 from
  "decided — not yet built" to "implemented (v1, serial)".

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
| `tests/vendor/test_run_hook_env.py` | VP-1: `run_hook` threads `env=` to the subprocess; `CHAIN_BASE_BRANCH` and `BH_VENV` reach the hook; **the override dict is merged into `os.environ` — `PATH`/`HOME` survive** (CONCERN-1 regression); default (no env) is unchanged. |
| `tests/vendor/test_exclude_labels_recheck.py` | VP-2: a mid-turn `blocked` label terminates the turn loop; the `running[N]`-missing guard does not raise. |
| `tests/chain/test_gh_deps.py` | `blocked_by`/`blocking`/milestone-membership parsing from fixture JSON; pagination; same-repo error surfacing; empty-graph case. |
| `tests/chain/test_dag.py` | Graph build from edges; membership scoping; **CycleError on a cyclic fixture** (§3.2). |
| `tests/chain/test_scheduler.py` | Ready frontier; `mark_done` unblocks dependents; `mark_parked` halts the **transitive** sub-tree; `is_active` termination; the `parked`-set filter on `get_ready()`. |
| `tests/chain/test_branches.py` | `feature/<milestone-slug>` + `feature/issue-<N>` created off `main`; **repo-root HEAD is on `feature/<slug>` before `_run_worker` is called** (§3.4/BLOCKING-1/NIT-2 regression guard); the **cut-point SHA is recorded** (§3.7); idempotent on re-create. |
| `tests/chain/test_after_run_base.py` | `after_run` classifies against the **resolved cut-point SHA**, not a live `git cherry origin/main` (B-I1); on `COMMITTED_NO_PR`/`NO_COMMITS` it removes `agent-ready` and sets `blocked` — never leaves `agent-ready` (CONCERN-2). |
| `tests/chain/test_merge.py` | `--no-ff` (not squash) into the feature branch; **the §3.3.1 green predicate**: required-only gates, `neutral`/`skipped` = pass, optional ignored, `in_progress` polls then times out → RED (C-I2); merge message carries the daemon-provenance marker; `agent-merged` + CI-green marker persisted (B-I2); merge order is dependency order. |
| `tests/chain/test_recovery.py` | `done` reconstructed **only** from daemon-provenance + CI-green-marked merges — a **human merge commit naming N is NOT read as done** (B-I2); `parked` from `blocked` + transitive dependents; **rule 3a** (`agent-done`+open-PR, no merge → re-enter CI gate); **rule 3b** (`agent-in-progress` orphan → re-evaluate, clear label, re-dispatch) (C-I4); frontier seeded correctly (§11.5). |
| `tests/chain/test_daemon.py` | End-to-end loop with mocked `_run_worker` returns: happy linear DAG; parallel-level DAG (serial dispatch); **two work units run strictly serially — no concurrent feature-branch checkout** (B-I3 regression guard); a mid-DAG block parks only its sub-tree and the daemon continues independent branches within the unit; `no_pr` → park + escalate (no retry); fully-parked → exit DAG run + escalate (Slack **and** issue comment, N-I3), daemon stays alive; **unblock → human re-adds `agent-ready` → outer poll re-enters via recovery → re-cut fresh** (BLOCKING-2); **`agent-in-progress` removed on every terminal outcome** (C-I4); **never opens a non-draft `feature → main` PR / never merges to main** (hard-constraint regression guards); `agent-done` → `agent-merged` relabel after CI-gated merge (CONCERN-3/OQ-3a). |

CI gate: ruff (79 cols), mypy strict, pytest must pass before merge
(`pyproject.toml:L52-L88`; `ci.yml`). New code carries Google-style docstrings and
full type hints. **The vendored `symphony/` tree** falls under `mypy src`
(`ci.yml:L38`) once vendored; per §12 P0 / CONCERN-3 the recommended path is a
`[[tool.mypy.overrides]]` `ignore_errors = true` for `baton_harness.vendor.*` (or
excluding `vendor/` from `mypy src`), recorded as `patches/mypy-strict-remediation.diff`
— full annotation of the vendored tree is deferred so P0 has a bounded exit
condition.

---

## 14. Out of scope for v1

- Parallel dispatch within a DAG level (deferred to v2; §6).
- **Cross-work-unit / cross-repo concurrency (VP-3 — base-ref parameterization so
  each work unit owns an isolated HEAD).** v1 serializes all work units (B-I3, §6 /
  §10).
- Cross-repo dependency work units (API limitation; §3.1 / §10).
- **Webhook-driven unblock detection** — v1 polls (§11.9).
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
- BLOCKING-1..4 + CONCERN-1..8 + NIT-2 (prior review) — `.tmp/arch-review-new-model.md`
  (line ranges inline).
- Dependency API GA, MCP gap, graphlib, merge-commit finding, novelty —
  `docs/research/2026-06-06-issue-dag-orchestration.md` (line ranges inline).
- C1/C2/C3 — `docs/spike-findings.md:L131-L165`.

**Rev 2 review findings folded in (this revision):**
- project-reviewer BLOCKING-1/2, CONCERN-1..4, NIT-1..3 —
  `.tmp/spec-review-chain.md` (line ranges inline at each amendment).
- inquisitor B-I1/B-I2/B-I3, C-I1..C-I4, N-I1..N-I3 —
  `.tmp/spec-inquisitor-chain.md` (line ranges inline at each amendment).
- C-I1 (`before_run` non-gating / `--untracked-files=no`) is addressed via §3.7
  (the freeze invariant means `before_run`'s rebase onto a frozen cut-point base
  cannot conflict mid-window) and the cut-point-SHA classifier base; the
  `--untracked-files=no` flag stays correct because the agent's contract is to
  commit its work (an uncommitted-only outcome is correctly `NO_COMMITS` → park,
  which is the intended escalation, not a false negative under the frozen base).
- N-I1 (`resolve_issue_number` repo-root collision) is mitigated by `branches.py`
  always passing `cwd=wt.path` to worker hooks (the bare-integer worktree dir); the
  optional `_cli.run()` helper (§4.3) must preserve explicit `cwd` at every new
  call site.

**unverified (surfaced in §11.x):**
- The exact `gh api` dependency-endpoint response field shapes (from the research
  report, not exercised live) — verify before P1.
- The repo's required-CI-check set for the §3.3.1 green predicate — read before P2.
- Whether the live issue #27 decision-log comments carry any constraint absent
  from the merged design docs (no GitHub read tool available to the sub-agent;
  the merged docs were reformed *from* those comments on 2026-06-07 and are
  treated as authoritative).
