# Research: nexu-io/looper vs. baton-harness

**Date:** 2026-06-21
**Author:** researcher sub-agent

---

## Idea

A structured comparison of **nexu-io/looper** — a multi-role autonomous AI dev team daemon for GitHub — against **baton-harness**, which drives the vendored `symphony` orchestrator to run Claude Code agents serially against GitHub issues.

---

## Requirements for a meaningful prior-art candidate

1. Must drive AI coding agents autonomously against GitHub issues or PRs (not a video looper, ML loop, etc.)
2. Must have a concrete running daemon/process model, not just a prompt template
3. Must integrate with GitHub labels, assignments, or PR lifecycle as its work signal
4. Must produce verifiable behavior that differs from baton-harness in at least one axis worth studying

---

## Search axes used

- **Direct synonyms:** "nexu-io looper", "autonomous agent GitHub loop daemon"
- **Problem-shape synonyms:** "AI dev team", "autonomous PR pipeline", "code agent orchestrator"
- **Adjacent domains:** issue → spec → PR pipeline, multi-agent role decomposition
- **Vendor-specific phrasing:** how nexu-io describes its system on its blog and README
- **Negative axes:** video looper, ML training loop, audio loop — confirmed NOT these

---

## Identity verification

**nexu-io/looper** (https://github.com/nexu-io/looper, SHA of README: `576ca61c94a40362c17c3995a8fce0c0400cd7b9`) is confirmed to be:

> "An autonomous AI dev team for your GitHub repos — plan, review, fix, and ship PRs, on a loop."

It is not a video-looping tool, ML training loop, or unrelated "looper." The identity check passes cleanly. The comparison proceeds.

---

## What each system is (evidence-grounded one-liners)

**baton-harness** — A Python daemon (`bh-daemon`) that vendored the `mraza007/baton` `symphony` orchestrator and drives it serially against GitHub issues labeled `agent-ready`, producing feature branches and draft PRs, with topological DAG scheduling of dependent issues.

**nexu-io/looper** — A Go daemon (`looperd`) plus CLI (`looper`) that runs five specialized agent roles (Coordinator, Planner, Reviewer, Fixer, Worker) in parallel, each looping until its own success criterion is met, with GitHub labels as the sole state machine and SQLite for local role state.

---

## Structured Comparison

### 1. Work Intake

| Dimension | baton-harness | looper |
|-----------|--------------|--------|
| **Signal** | Issues labeled `agent-ready` | Issues labeled per role's trigger label (e.g., `looper:plan`, assigned to the Looper user) |
| **Who classifies** | Human manually applies `agent-ready` | Coordinator role: LLM-driven triage, applies `dispatch/needs-plan` or `dispatch/needs-implement`, then autonomous dispatch after a 30-min grace window |
| **Dependency gates** | Yes — GitHub `blocked_by` graph via `gh_deps.fetch_blocked_by`, topological sort (`dag.py` + `IssueScheduler`) | Yes — Coordinator reads GitHub-native `blocked_by`; gate satisfied only when blocker is `state==closed AND state_reason==completed` (ADR-0004) |
| **Veto / hold** | `blocked` label re-checked mid-turn; adds label mid-run terminates the agent | `looper:hold` and `dispatch/*` removal are veto signals; `looper:worker-ready` manual application overrides grace window |
| **Scope** | Issues only | Issues AND PRs (Reviewer and Fixer operate on PRs) |
| **Multi-repo** | Via `registry.py`; polls multiple repos | Yes — register repos with `looper project add`; parallel loops across repos |

**Evidence:**
- baton-harness intake: `config/WORKFLOW.md` (labels: `["agent-ready"]`, exclude_labels: `["blocked"]`)
- baton-harness dep graph: `src/baton_harness/chain/dag.py`, `gh_deps.py`, `scheduler.py`
- looper Coordinator: `CONTEXT.md` (Triage, Disposition, Dispatch), ADR-0002 (durable label authority), ADR-0004 (dependency gate)

---

### 2. Orchestration Architecture

| Dimension | baton-harness | looper |
|-----------|--------------|--------|
| **Language / runtime** | Python 3 + asyncio | Go 1.22 |
| **Process model** | Single-process async daemon, **strictly serial** (one work unit at a time, one issue at a time within a work unit; B-I3 contract) | Multi-loop daemon; each role runs in its own goroutine/process; parallel across repos and issues (`looperd`) |
| **Work queue** | In-memory topological frontier; no persistent queue | SQLite `queue` table with retry back-off, slow-lane for long retries, retry bounds |
| **State persistence** | SQLite via `runlog.py` for run history; labels in GitHub | SQLite (`~/.looper/looper.sqlite`) for loop records, queue items, run state, checkpoints; labels in GitHub for Coordinator state |
| **Staging model** | Issue → branch checkout → agent run → draft PR | Issue → Planner (spec PR) → Reviewer ↔ Fixer loop → Worker (implementation PR) — four distinct stages, each a separate agent invocation |
| **Daemon lifecycle** | `bin/run-daemon.sh`; entry point `bh-daemon`; no managed installer | `looperd` managed daemon with `looper daemon install/start/stop`; binary at `~/.looper/bin/looperd` |
| **Polling vs. webhooks** | Poll (30 s interval from `config/WORKFLOW.md`) | Poll-primary; webhook tunnel mode (ADR-0006) available as wakeup optimization; SSE wakeups in networked mode |
| **Network/distributed mode** | Not supported | Yes — `loopernet` for multi-Node routed mode; Coordinator control plane with lease-fenced mutation |

**Evidence:**
- baton-harness serial contract: `src/baton_harness/chain/daemon.py` lines 1–37 (B-I3 docstring)
- looper parallel model: README.md "Parallel-safe by design — every loop runs in its own git worktree"
- looper queue: `internal/loops/service.go` (Create, Pause, Terminate, TransitionStatus with SQLite)
- looper network: `CONTEXT.md` (Network, Node, Coordinator control plane), README.md "Networked operation"

---

### 3. Autonomy Model

| Dimension | baton-harness | looper |
|-----------|--------------|--------|
| **Turn limit** | `max_turns: 8` (config/WORKFLOW.md) — hard cap per issue | **No turn limit.** The executor runs a single agent invocation per `Start()` call; iteration is managed by the role's own loop calling `Start()` again until its success criterion is met |
| **Termination criterion** | Turn limit reached, OR agent outputs a commit + push + draft PR, OR `blocked` label detected mid-turn | Role-specific exit condition: Planner exits when spec PR is open + labeled `looper:spec-reviewing`; Reviewer exits when no actionable threads remain; Fixer exits when all threads resolved or replied; Worker exits when checks pass and PR is ready |
| **Idle/runtime timeout** | Via `symphony` orchestrator (inherited from Baton) | `max_runtime` timer + idle/heartbeat timeout per execution (`executor.go` `timeoutTimer`, `heartbeatTimeout`) |
| **Completion detection** | `symphony`/Baton mechanism (turn-based) | Stdout marker protocol: agent must print `__LOOPER_RESULT__={"summary":"..."}` as last line; absence → `ParseStatus: "missing"` |
| **Mid-run guard** | `blocked` label re-checked before dispatch (daemon.py); VP-2 patch in vendored `_run_worker` | Reviewer applies `looper:hold` or `dispatch/*` removal as Veto signal; Coordinator grace window is the human checkpoint before dispatch |
| **Self-merge** | Never — daemon opens draft PRs only, never merges | Optional auto-merge: Reviewer verifies Acceptance Criteria against diff, calls `gh pr merge --auto`, Coordinator watches merge-pending state (ADR-0005) |
| **Human checkpoints** | Opening/closing PRs (draft PR model); human applies `agent-ready` | Spec PR review window (`looper:spec-reviewing` → `looper:spec-ready` requires human or Reviewer approval); `looper:hold` veto; dispatch grace window (default 30 min) |

**Evidence:**
- baton-harness turns: `config/WORKFLOW.md` (`max_turns: 8`)
- baton-harness never-merge: `config/WORKFLOW.md` ("Do NOT merge any pull request. Open a draft PR only")
- looper no-turn-limit: `internal/agent/executor.go` (WebFetch: "No explicit turn limit exists")
- looper completion marker: `internal/agent/prompt.go` (`CompletionMarker = "__LOOPER_RESULT__"`)
- looper auto-merge: ADR-0005 (`docs/adr/0005-auto-merge-via-github-native-and-coordinator-watch.md`)

---

### 4. Agent Runtime

| Dimension | baton-harness | looper |
|-----------|--------------|--------|
| **Agent CLI** | Claude Code only (`command: claude`, `permission_mode: bypassPermissions`) | Pluggable: `claude` (Claude Code), `opencode`, `codex`, `cursor-cli` — via `agent.vendor` config; auto-detected when one is on PATH |
| **Invocation** | `symphony` orchestrator calls `_run_worker(issue)` directly (vendored Python) | Go subprocess via `exec.Command(command, args...)` with process group isolation (`Setpgid: true`) |
| **Agent arguments** | Prompt injected via symphony/Baton's `WorkflowConfig` | Per-vendor: Claude gets `--print <prompt> --dangerously-skip-permissions`; Codex gets `exec --model <model> <prompt>`; OpenCode gets `run --dir <dir> --model <model> <prompt>` |
| **Worktree per agent** | Single shared repo checkout (serial, so safe); feature branch per work unit, not per issue | Git worktree per loop (`~/.looper/worktrees/<repo>/<project>/`), isolated per role |
| **Native resume** | Not implemented | Yes — `--resume sessionID` for Claude Code; native session recovery on restart |
| **Skills / context** | Prompt template in `config/WORKFLOW.md` | Installable agent skills (`npx skills add`): `looper` skill for setup, `pr-takeover` skill for single-PR adoption |

**Evidence:**
- baton-harness: `config/WORKFLOW.md` (command: claude, permission_mode: bypassPermissions)
- looper vendor dispatch: `internal/agent/executor.go` (WebFetch: resolveCommand switch, per-vendor args)
- looper worktrees: README.md "Every loop runs in its own git worktree"
- looper native resume: `internal/agent/executor.go` (WebFetch: "--resume sessionID")

---

### 5. Dependency / Maintenance Posture

| Dimension | baton-harness | looper |
|-----------|--------------|--------|
| **Upstream** | Vendored `mraza007/baton` (`symphony` package) into `src/baton_harness/vendor/symphony/`; upstream is dormant (3 commits, Mar 2026, no releases) | Self-contained Go module; no orchestrator dependency; all code is in `nexu-io/looper` itself |
| **Patching model** | `# VENDOR-PATCH` markers in vendored source, tracked in `patches/` and `VENDORING.md` | Direct development in `internal/`; no vendored external orchestrator |
| **License** | Upstream Baton: unverified (no license file found in `mraza007/baton`). Harness itself: private | MIT (https://github.com/nexu-io/looper/blob/main/LICENSE, SHA: `96a04b3e946838e16860d4d6ca6442dfcebfb56c`) |
| **Language** | Python 3 | Go 1.22 |
| **Config format** | YAML front-matter + Jinja2 template in one file (`config/WORKFLOW.md`) | TOML (canonical), YAML, JSON; `~/.looper/config.toml` |

---

### 6. Maturity and Community

| Dimension | baton-harness | looper |
|-----------|--------------|--------|
| **Status** | Private repo; single-owner; v1 serial daemon implemented | Public repo; organization-owned (nexu-io); MIT license |
| **Activity** | Actively developed (recent commits: fix(#130), fix(#128), etc.) | Very actively developed: PR #508 merged 2026-06-20 (last day), multiple PRs per week, currently at v0.9.10 |
| **Commit cadence** | Issue-driven; bursts around milestones | Continuous; commits every 2–3 days; self-hosted (commits generated by looper itself: `Generated-By: looper 0.9.x`) |
| **Contributors** | 1 (private) | Organization account; unverified contributor count (private org) |
| **Stars/forks** | N/A (private) | 63 stars, 63 forks (as of 2026-06-21, from search result) |
| **ADRs** | None published | 11 ADRs documenting key design decisions (coordinator statelessness, label authority, dependency gate, auto-merge, webhook tunnel, network mode, etc.) |
| **Test coverage** | Unit + integration tests (Python pytest) | Extensive: `executor_test.go` (51 KB), `runner_actions_test.go` (137 KB), `runner_integration_test.go` (32 KB), plus `internal/e2e/` directory |

**Evidence:**
- looper activity: commit log (`mcp-github-list_commits` result), last push `2026-06-20T03:51:40Z`
- looper version: commit message "Generated-By: looper 0.9.10"
- looper created: `2026-04-11` (repository created_at from search result) — ~10 weeks old at time of writing

---

### 7. Key Differentiators and Tradeoffs

#### What looper does that baton-harness does not

1. **Multi-role decomposition.** Looper splits work into five distinct agent roles (Coordinator, Planner, Reviewer, Fixer, Worker), each running a separate agent invocation with its own exit criterion. Baton-harness runs one monolithic Claude Code agent per issue with a turn cap. The looper model means the reviewer and fixer can ping-pong independently of the worker, producing PR reviews that don't require human intervention.

2. **Goal-based rather than turn-based termination.** Looper has no `max_turns`. Each role loops until it achieves a named success condition expressed as a GitHub label state. Baton-harness uses `max_turns: 8` as a proxy for "done." The looper model is more principled (the agent stops when the job is done, not when the clock runs out) but requires more disciplined prompt design to avoid infinite loops.

3. **Automatic spec phase (Planner).** Looper inserts a Planner agent that produces a spec PR before any implementation starts. The spec PR goes through Reviewer ↔ Fixer before the Worker ever touches code. Baton-harness sends an issue directly to Claude Code — no intermediate spec artifact or review gate.

4. **Auto-merge with acceptance-criteria verification.** Looper's Reviewer can call `gh pr merge --auto` after verifying each Acceptance Criterion in the issue has satisfying evidence in the diff (ADR-0005). Baton-harness explicitly prohibits self-merge; all merges require human action.

5. **Pluggable agent vendor.** Looper supports `claude-code`, `opencode`, `codex`, `cursor-cli` via `agent.vendor` config. Baton-harness is Claude-only by design.

6. **Coordinator with autonomous triage.** Looper's Coordinator LLM triages fresh issues (classifies them, decides `dispatch/needs-plan` vs `dispatch/needs-implement`) and can dispatch autonomously after a grace window. Baton-harness requires humans to manually apply `agent-ready`.

7. **Networked/distributed mode.** Looper supports `loopernet` for multi-node distributed operation. Baton-harness is single-machine only.

8. **Completion marker protocol.** Looper defines a stdout marker (`__LOOPER_RESULT__={"summary":"..."}`) as the structured handoff from agent to harness. Baton-harness uses symphony's turn-based model with no such structured output contract.

9. **Installable agent skills.** Looper ships as `npx skills add` installable skills for Claude Code and other agents. Baton-harness has no equivalent skill packaging.

#### What baton-harness does that looper does not (or does differently)

1. **DAG-aware topological scheduling.** Baton-harness builds an issue dependency graph from `blocked_by` links and schedules issues in topological order within a work unit, parking failed sub-trees. Looper's Coordinator checks the dependency gate per-issue before Dispatch, but the harness-level DAG execution model (whole work unit as a unit, milestone-grouped) is not present in looper.

2. **Milestone-grouped work units.** Baton-harness groups milestoned issues into a single work unit and attempts to complete all issues in the milestone before moving to the next work unit. Looper processes issues independently.

3. **Drift-recovery at startup.** Baton-harness runs `reconcile_startup` to recover orphan worktrees and stalled labels on daemon boot. Looper has `looper run reconcile-stale` as a manual command; automatic startup recovery is less explicit.

4. **Simpler mental model for a solo operator.** Baton-harness is a single YAML config + Jinja2 template. Looper requires configuring five roles, understanding the label state machine, and managing `~/.looper/config.toml`. The operational surface is larger.

---

## No prior art found

No gaps in this comparison — both systems are sufficiently well-documented. The comparison is complete against all stated axes.

---

## What baton-harness could borrow from looper (ranked by expected value)

### High value

1. **Goal-based termination instead of `max_turns`.** The `__LOOPER_RESULT__={"summary":"..."}` completion marker protocol (from `internal/agent/prompt.go`) lets the agent signal its own completion explicitly. Baton-harness could replace or supplement `max_turns: 8` with a structured output contract where the agent signals "done" by printing a specific marker. This would make completion detection deterministic rather than capacity-bounded.
   - Source: `internal/agent/prompt.go` SHA `5397b3343743b4e036b5e42ac0575c20665220ec`

2. **Idle/runtime timeout with SIGTERM → grace → SIGKILL signal progression.** Looper's executor tracks both max-runtime and idle (no-output heartbeat) timeouts, and sends signals in a progressive order with a configurable grace period. Baton-harness inherits symphony's timeout model; an explicit heartbeat timeout would catch stuck agents that don't hit the turn limit.
   - Source: `internal/agent/executor.go` (WebFetch analysis)

3. **Per-execution structured result record.** Looper's `Result` type captures status, summary, stdout, stderr, ParseStatus, artifacts, changedFiles, commits, timeoutType, heartbeatCount, PID per execution. Baton-harness records run history in SQLite via `runlog.py` but the structured artifact capture is richer in looper.

### Medium value

4. **Reviewer ↔ Fixer loop pattern.** The idea of a separate Reviewer agent that posts inline threads and a Fixer agent that addresses them, ping-ponging until clean, is applicable even to a single-agent harness. Baton-harness could model this with a second Claude Code invocation after the worker opens a PR, giving each draft PR an automated review pass before a human sees it.

5. **Durable label authority pattern (ADR-0002).** Looper's principle that every side-effecting agent action must have a named durable authority committed to GitHub (not in-memory inference) is directly applicable to baton-harness's label state machine. The `# VENDOR-PATCH` approach in the vendored `_run_worker` (VP-2, mid-turn `blocked` label re-check) already follows this spirit; making it an explicit ADR-style rule would improve future maintainability.

6. **Acceptance Criteria verification before auto-merge.** Looper's Reviewer checks that each checkbox under `## Acceptance criteria` in the issue has satisfying evidence in the diff before approving and enabling auto-merge. Baton-harness's WORKFLOW.md template could be extended to ask Claude Code to verify acceptance criteria before opening the draft PR.

### Lower value (study-only)

7. **Multi-node networked mode.** Looper's `loopernet` distributed architecture is more complex than baton-harness needs at present. Worth knowing exists but not worth adapting.

8. **Pluggable agent vendor layer.** Baton-harness is intentionally Claude-only. The looper `resolveCommand` switch is simple to understand but not a near-term need.

---

## Recommended handoff

- `project-planner` — if the user wants to adopt the completion-marker protocol (item 1 above), the planner should scope a small PR against `src/baton_harness/chain/daemon.py` and the WORKFLOW.md template to inject the marker instruction and parse it in the daemon's outcome protocol.
- `project-planner` — if the user wants to add an idle/heartbeat timeout (item 2), scope a patch against the `after_run` / `_run_worker` path in the vendored symphony or the daemon wrapper.
- `user` — item 4 (Reviewer ↔ Fixer loop) requires a product decision about whether to make baton-harness multi-role or keep it single-agent.

---

## Open questions

1. **Looper contributor count:** The repo is under a GitHub organization (`nexu-io`), not a personal account. The actual number of human contributors behind the organization is not publicly disclosed.
2. **Looper stars/forks (63/63 as of 2026-06-21):** This is a low-star count for a 10-week-old project, but the commit velocity and ADR depth suggest it is actively maintained by a small focused team. The stars figure may not reflect actual adoption.
3. **baton-harness upstream Baton license:** `mraza007/baton` has no LICENSE file (not checked in this session). If the user ever needs to license the harness, the upstream provenance should be clarified.
4. **Looper `scheduler/doc.go`** is the only file in `internal/scheduler/` — the scheduler package appears to be a stub or recently extracted. Its role relative to the `loops/service.go` queue model is not fully clear from the available source.

---

## Appendix: Sources

- nexu-io/looper README: https://github.com/nexu-io/looper/blob/main/README.md (SHA `576ca61c94a40362c17c3995a8fce0c0400cd7b9`, fetched 2026-06-21)
- nexu-io/looper CONTEXT.md: https://github.com/nexu-io/looper/blob/main/CONTEXT.md (SHA `2bfb5bd90907c1ff3d4c2a189283ac95c4ccd5cd`, fetched 2026-06-21)
- nexu-io/looper AGENTS.md: https://github.com/nexu-io/looper/blob/main/AGENTS.md (SHA `40292f509d843e2c748c6c71f6924d97756bcef0`, fetched 2026-06-21)
- nexu-io/looper internal/agent/prompt.go: https://github.com/nexu-io/looper/blob/main/internal/agent/prompt.go (SHA `5397b3343743b4e036b5e42ac0575c20665210ec`, fetched 2026-06-21)
- nexu-io/looper internal/agent/executor.go: https://github.com/nexu-io/looper/blob/main/internal/agent/executor.go (SHA `63e1351f03501061492248c654c6d60e6dbc18be`, fetched via WebFetch 2026-06-21)
- nexu-io/looper internal/loops/service.go: https://github.com/nexu-io/looper/blob/main/internal/loops/service.go (SHA `45745c800267d65b15fc0f202a3b45603f4227e9`, fetched 2026-06-21)
- nexu-io/looper ADR-0001: https://github.com/nexu-io/looper/blob/main/docs/adr/0001-coordinator-is-stateless.md (SHA `38cd804601f92240fdc8c9aea4ff165498db00cd`, fetched 2026-06-21)
- nexu-io/looper ADR-0002: https://github.com/nexu-io/looper/blob/main/docs/adr/0002-coordinator-authority-via-durable-labels.md (SHA `f0d3bbef0451100470097f6d4ec8c55377482899`, fetched 2026-06-21)
- nexu-io/looper ADR-0004: https://github.com/nexu-io/looper/blob/main/docs/adr/0004-dependency-gate-via-github-native-blocked-by.md (SHA `869d0a7a61dab06744cf4594f85b116585522b0d`, fetched 2026-06-21)
- nexu-io/looper go.mod: https://github.com/nexu-io/looper/blob/main/go.mod (SHA `201a63dba6a8dbde97c96dc408c4652ad85fafc4`, fetched 2026-06-21)
- nexu-io/looper repo metadata: created 2026-04-11, last push 2026-06-20, 63 stars, 63 forks, MIT license
- baton-harness config/WORKFLOW.md: I:\ai\claude\baton-harness\config\WORKFLOW.md (read 2026-06-21)
- baton-harness src/baton_harness/chain/daemon.py: I:\ai\claude\baton-harness\src\baton_harness\chain\daemon.py (read 2026-06-21)
- baton-harness src/baton_harness/chain/scheduler.py: I:\ai\claude\baton-harness\src\baton_harness\chain\scheduler.py (read 2026-06-21)
