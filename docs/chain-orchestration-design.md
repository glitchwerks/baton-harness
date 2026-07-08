# Chain Orchestration Design

**Purpose:** Reference for the always-on dependency-chain daemon (`src/baton_harness/chain/`) — how it works and why. This document complements [`harness-design.md`](./harness-design.md) (the parent harness design); consult §10 there for the implementation history and the resolved open questions. Implemented in issue #27 (phases P0–P3, PRs #46–#49).

---

## 1. Problem

A milestone is a dependency graph, not a flat bag of independent issues. The flat `agent-ready` model cannot express ordering: mark all issues ready and the agent may attempt issue N before issue N-1 exists; mark only the first issue ready and a human must re-label at each step — defeating the unattended premise.

The chain layer solves this by owning the feature branch, the topological dispatch sequence, CI-gated intra-DAG merges, and escalation — while keeping the vendored `symphony._run_worker` as the per-issue execution engine. See `harness-design.md §10` for the rationale for the always-on daemon model over alternatives.

---

## 2. Unified execution model — everything is a DAG

There is **one** execution path, parameterized by the DAG:

- **A milestone** — all open issues in the milestone form one DAG, executed on one `feature/<slug>` branch, finalized as one ready-for-review `feature → main` PR.
- **An un-milestoned issue** — its own N=1 DAG on a `feature/issue-<N>` branch, its own PR.

N=1 is the degenerate DAG. There is no separate flat-run entry point. The daemon processes these identically.

**Work-unit membership = milestone.** Issues not belonging to any milestone each become their own N=1 work unit. This resolves OQ-2 from the original spec.

---

## 3. DAG construction

### 3.1 Dependency source — native GitHub REST API

Dependencies are read from GitHub's native issue-dependencies REST API (GA 2025-08-21), not from issue body text. `gh_deps.py` calls:

```
gh api repos/{owner}/{repo}/issues/{N}/dependencies/blocked_by
```

and returns a JSON array of blocker issue objects. Output is parsed with `json.loads` — never grepped (consistent with `after_run.py` discipline).

**Same-repo only.** The API does not support cross-repository dependencies; a work unit cannot span repositories. The daemon enforces this by construction — `build_dag` in `dag.py` silently drops any blocker numbers outside the membership set.

### 3.2 DAG build — `dag.py` (pure)

`dag.build_dag(membership, blocked_by)` accepts the membership frozenset and the pre-fetched `{issue: [blockers]}` map. It returns a `DagResult` holding:

- `graph` — `{issue: [in-scope blocker_issues]}` adjacency map, one key per member.
- `membership` — the original frozenset, preserved for downstream consumers.

`dag.py` performs no I/O and no cycle detection. It is a pure data transformer.

### 3.3 Topological scheduler — `scheduler.py` (pure, stdlib)

`IssueScheduler` wraps `graphlib.TopologicalSorter` (Python stdlib ≥3.9; project requires ≥3.10 — no new runtime dependencies).

Key behaviors:

- **`prepare()`** — raises `graphlib.CycleError` on a cyclic graph. Cycle detection is free.
- **`get_ready()`** — returns the ready frontier (issues whose blockers are all `mark_done`'d) minus the `parked` set.
- **`mark_done(N)`** — called only after issue N's branch has been CI-gate merged into the feature branch. This is the strict satisfaction signal; "PR opened" is not enough.
- **`mark_parked(N)`** — parks N and its transitive dependents. Parked nodes never reappear in `get_ready()`.
- **`is_active()`** — returns False when every node has reached a terminal state (done or parked).

`TopologicalSorter` does not model failure. `IssueScheduler` maintains a separate `parked` set and filters the ready output against it.

---

## 4. Branch model

**Feature-branch naming:**
- Milestone work unit: `feature/<milestone-slug>` (slug derived from milestone title).
- Un-milestoned N=1 unit: `feature/issue-<N>` (issue number is collision-free by construction).

The feature branch is created off `origin/main` and pushed to origin before any worker dispatches — the agent's `gh pr create --base "$BH_FEATURE_BRANCH"` requires the base to exist remotely.

**Per-issue branches** follow symphony's own naming convention: `baton/<slug>-<N>`. The `branches.py` module manages feature-branch creation (`create_feature_branch`), checkout (`checkout_feature_branch`), and cut-point capture (`record_cut_point`).

**Cut-point.** Before each `_run_worker` dispatch, the daemon records the current tip SHA of the feature branch (`git rev-parse feature/<slug>`). This SHA is passed to the worker as `CHAIN_BASE_BRANCH` via the VP-1 `env=` thread. `before_run` rebases onto this SHA, not onto `origin/main` — ensuring per-issue branches build on the correct merged state. See `harness-design.md §1` for the VP-1 vendor patch.

---

## 5. Daemon loop — `daemon.py`

The daemon is the orchestration core. Its concurrency contract, known as **B-I3**, is the load-bearing invariant: **all work units are processed with sequential `await` calls — the daemon NEVER spawns concurrent `asyncio.Task` objects for work units or for individual issues within a DAG.** This guarantees the shared repo-root HEAD is only ever checked out to one feature branch at a time and makes the daemon the unambiguous single writer of `agent-ready`/`agent-in-progress` promotions (C1).

### Outer poll loop

```
while True:
    for each repo in registry:
        fetch open agent-ready issues (not also blocked)
        select ONE ready work unit (milestone or N=1)
        await _run_work_unit(...)
    sleep poll_interval
```

Milestoned work units take priority over un-milestoned ones. Within each category, lowest number wins.

### Per-work-unit loop

```
Step 0: build DAG + prepare scheduler (CycleError → escalate + skip)
Step 1: create or resume feature branch; early-push to origin
        reconstruct scheduler state (recovery.py)
Step 2: while scheduler.is_active():
            frontier = scheduler.get_ready()
            pick ONE issue N (serial)
            apply recovery rules (see §6)
            if fresh dispatch:
                checkout_feature_branch
                record cut_point
                label: agent-ready → agent-in-progress  (C1)
                set CHAIN_BASE_BRANCH = cut_point
                set BH_FEATURE_BRANCH = branch_name
                worker_result = await orch._run_worker(issue)
                re-read labels (after_run may have set blocked)
                apply §7 outcome protocol
Step 3: push feature branch
        open one ready-for-review PR  feature/<slug> → main  (never merge to main)
```

---

## 6. Crash and unblock recovery — `recovery.py`

The daemon auto-reconstructs scheduler state on every start. `recovery.reconstruct` inspects git log and GitHub label state to seed the scheduler before the loop begins, enabling both crash recovery and live re-entry when a previously parked issue is unblocked by a human.

**Classification precedence (first match wins):**

1. **done** — feature branch git log contains a `--no-ff` merge commit with the exact trailer `Baton-Harness-Merge: issue-<N> ci=green` **AND** the issue carries `agent-merged`. Both signals required (B-I2 provenance invariant: a human `git merge` produces no trailer and is not read as done).
2. **ci_gate_reentry (3a)** — provenance merge commit present but `agent-merged` label absent. Daemon died after merging but before writing the label. Re-enter the CI gate without re-running `_run_worker`.
3. **parked_seed** — issue carries `blocked` label.
4. **ci_gate_reentry (3a)** — `agent-done` + open PR + no daemon-provenance merge commit. Agent finished; CI gate/merge was interrupted.
5. **redispatch (3b)** — `agent-in-progress` orphan (crash mid-`_run_worker`). Daemon clears the orphan label (C1 single-writer), then re-dispatches the issue fresh. A re-dispatch loop detector (`redispatch.py`) parks the issue if it is re-dispatched beyond a configured threshold within a rolling tick window.
6. **else** — not yet dispatched; appears on the ready frontier normally.

---

## 7. Outcome protocol (§3.5)

After `_run_worker` returns, the daemon re-reads the issue's labels (`after_run` may have set `blocked`) and applies a single-state invariant check (labels must be in exactly one terminal state) before branching:

```
worker_result == "pr_created" AND "blocked" not in labels:
    → CI gate (merge.py)
    → MERGED:  remove agent-in-progress + agent-done; add agent-merged
               scheduler.mark_done(N)
    → CI_FAILED / CI_TIMEOUT / CONFLICT:
               remove agent-in-progress
               scheduler.mark_parked(N)
               escalate

else (blocked label present, or no_pr):
    remove agent-in-progress
    scheduler.mark_parked(N)
    escalate
```

**Block vs fail:** `after_run` sets `blocked` for underspecified or impossible work; `no_pr` indicates agent failure. The daemon treats both identically for sub-tree propagation (parked), but distinguishes them in the escalation message (kind `"block"` vs `"debug"`).

**`agent-in-progress` invariant (C-I4):** the `agent-in-progress` label MUST be cleared on every terminal branch — success and all park paths. This is enforced at every branch in `_run_work_unit`.

---

## 8. CI-gated merge — `merge.py`

A per-issue branch may merge into the feature branch only after CI is green on that issue's PR. **"Dependency satisfied" = merged into the feature branch** (not "PR opened", not "merged to main").

**Green predicate:**
- **GREEN:** every required check has `status: completed` and `conclusion` in `{success, neutral, skipped}`.
- **RED:** any required check with `conclusion` in `{failure, cancelled, timed_out, action_required}`.
- **NOT-YET:** any required check is `queued`/`in_progress`, or a configured required check is absent. Poll with bounded backoff (default 10s interval, 30-minute ceiling). Hard timeout → RED (`CI_TIMEOUT`).

**No vacuous green.** Zero matching checks → NOT-YET until hard timeout, then RED. An absent check never passes.

**Required-check set:** hardcoded in `REQUIRED_CHECKS` in `merge.py` (`"Lint (ruff)"`, `"Test (pytest)"`, `"Type check (mypy)"`) — the repo exposes no branch-protection required-check API (returns 404). TODO: wire to `config/WORKFLOW.md` so operators can override without editing code.

**CI trigger prerequisite.** `ci.yml` was extended to include the `feature/**` branch glob so PRs targeting the feature branch trigger CI. Without this extension, the CI gate would be unenforceable.

**Merge commit.** Uses `git merge --no-ff` (not squash). Squashing diverges git history and forces `--onto` rebasing of every dependent branch. Merge-commit order follows the topological ready queue — lowest (earliest) blockers first; out-of-order merges create ghost diffs.

**Provenance trailer.** On a green merge, `merge.py` writes the trailer `Baton-Harness-Merge: issue-<N> ci=green` to the merge commit message, adds the `agent-merged` label to the issue, and posts a marker comment. These three signals are what `recovery.py` uses to reconstruct the `done` set reliably without re-querying GC'd check-runs (B-I2).

---

## 9. Escalation — `escalation.py`

Dual-channel: GitHub issue comment (always attempted, the durable record) + optional Slack via `BH_SLACK_WEBHOOK_URL` (best-effort, never blocks the durable record). A Slack failure is logged at WARNING and does not affect the return value.

Escalation fires on: park paths (block, CI failure, CI timeout, merge conflict), label invariant violations, cycle detection, redispatch loop detection, and daemon tick exceptions.

The daemon **never exits on a block.** A parked sub-tree is escalated and the loop continues with independent branches. The daemon exits a work unit cleanly when the frontier is empty (all done or parked, or all remaining issues are un-greenlit milestone members awaiting human `agent-ready` labels).

---

## 10. C1/C2/C3 contracts (inherited from `harness-design.md §6`)

- **C1 — single-writer claim authority.** The daemon is the sole writer of `agent-in-progress` and the sole promoter of issues from `agent-ready` to in-flight. `after_run` (inside `_run_worker`) writes terminal labels; the daemon and `after_run` never target the same issue at the same instant because dispatch is serial. With a unified single daemon and one execution path, there is no concurrent label-writer conflict and no lock is required (B3 dissolved — `harness-design.md §10`).
- **C2 — provenance allowlist.** The daemon acts only on issues carrying the trusted milestone label and only merges branches authored by the daemon itself (identified by the `Baton-Harness-Merge` provenance trailer). It never merges a human-authored or external branch into the feature branch.
- **C3 — bounded rework + escalation.** No auto-rework in v1. A failed or blocked issue parks its sub-tree and triggers escalation. The re-dispatch loop detector (`redispatch.py`) is the rework bound: an issue that is orphaned and re-dispatched more than `redispatch_max` times within `redispatch_window_ticks` ticks is parked rather than retried indefinitely.

---

## 11. "Done — ready for human review" signal

When the per-DAG loop terminates (all nodes done or parked, or frontier has only un-greenlit members):

1. Push the feature branch to origin.
2. Open exactly **one ready-for-review PR** `feature/<slug> → main`. PR body lists merged issues (one `Closes #N` keyword per line — GitHub does not parse comma-continuation) and parked issues with reasons.
3. The daemon **stops processing this work unit.** It never merges `feature → main`. That is a hard constraint (issue #27; `harness-design.md §10`).

If the feature branch has zero commits over `origin/main`, the PR is skipped (prevents empty PRs when no issue produced commits).

---

## 12. Module map

| Module | Role | Pure? |
|---|---|---|
| `chain/gh_deps.py` | Fetch `blocked_by`/`blocking` from GitHub REST API; parse with `json.loads` | No (I/O) |
| `chain/dag.py` | Build scoped `{issue: [blockers]}` adjacency map | Yes |
| `chain/scheduler.py` | Wrap `graphlib.TopologicalSorter`; ready frontier; `mark_done`/`mark_parked`; transitive sub-tree parking | Yes (stdlib) |
| `chain/branches.py` | Feature-branch creation, checkout, cut-point capture | No (git) |
| `chain/merge.py` | CI green-predicate poller; `--no-ff` merge into feature branch; provenance trailer | No (git/gh) |
| `chain/recovery.py` | Crash/unblock reconstruction; seeds scheduler done/parked state | No (git/gh) |
| `chain/daemon.py` | Outer poll loop + per-DAG serial runner; owns C1 single-writer contract | No (all I/O) |
| `chain/escalation.py` | Dual-channel escalation (GitHub comment + optional Slack) | No (gh/HTTP) |
| `chain/labels.py` | Single-state label invariant assertion (`assert_single_state`) | Yes |
| `chain/redispatch.py` | Re-dispatch loop detection tally | Yes |
| `chain/heartbeat.py` | Liveness heartbeat OS thread; detects stalls during the CI gate | No (I/O) |
| `chain/runlog.py` | Structured JSONL event log (best-effort; never raises into loop) | No (file) |
| `chain/registry.py` | `RepoConfig` dataclass; one-entry repo registry in v1 | Yes |
| `chain/obs_config.py` | Observability config loader (`ObsConfig`) | No (file) |
| `chain/cli.py` | `bh-daemon` console entry point (argparse) | No |

All modules use a single module-local `_run` subprocess seam — patchable in tests without mocking the entire `subprocess` module (spike finding F8).

---

## 13. Observability

**Structured run log** (`runlog.py`) — JSONL file; best-effort emission at dispatch, outcome, escalation, and daemon-start events. A failure writing the log is logged at WARNING and never propagates into the loop.

**Heartbeat monitor** (`heartbeat.py`) — OS thread running independently of the asyncio loop, writing liveness heartbeats even while the event loop is blocked inside the synchronous CI-poll sleep (up to 30 minutes). `LivenessState` is shared between daemon and monitor; field assignments are atomic under the GIL (best-effort liveness; no lock required).

---

## 14. Scope limits and deferred work

- **Parallel dispatch within a DAG level** — deferred to v2. The scheduler already returns the full ready set; the serial constraint is the only gate. See `harness-design.md §10` note on the v2 extension.
- **Cross-repo chains** — not possible; GitHub dependency API is same-repo only.
- **Auto-merge of `feature → main`** — hard constraint; never automated.
- **Required-check set in config** — currently hardcoded in `merge.py`; TODO item to wire to `config/WORKFLOW.md`.
- **Webhook-driven unblock detection** — v1 is poll-driven; webhook-driven unblock is a v2 latency optimization.
- **Multi-repo daemon** — seam is present (registry list, `max_concurrent` in `WORKFLOW.md`); v1 registry has one entry.

**unverified:** the exact `gh api` dependency-endpoint response shapes were confirmed against the research report's API summary (fetched 2026-06-06, citing GitHub REST docs) but not exercised live in this session. Confirm field names against a live `gh api` call before depending on them in new code.
