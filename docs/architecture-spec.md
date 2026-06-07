# Architecture spec — autonomous agent development workflow

**Status:** First draft. Captures locked decisions and open items as of the planning phase.
**Companion docs:** [problem-statement.md](./problem-statement.md), [research-findings.md](./research-findings.md)

---

## 1. Purpose

This spec defines the architecture of a self-hosted autonomous coding workflow that lets a solo developer hand off scoped GitHub work to an agent system in the morning and return to reviewed PRs and clearly articulated blocked questions in the evening — with no required intervention during the workday.

The architecture is bounded by two human checkpoints (morning approval, evening review) and built around five layers: a comms layer for async interaction, GitHub as the source of truth, an orchestration harness as the broker, a containerised execution layer, and the human at both ends.

---

## 2. Locked decisions

| Layer | Component | Rationale |
|---|---|---|
| Source of truth | **GitHub** (issues, PRs, milestones, labels) | Already where work lives; avoids parallel tracking |
| Executor | **Claude Code** (`claude` CLI, first-party binary) | Only ToS-compliant path on subscription cost model |
| Orchestrator | **custom always-on daemon** + **vendored `symphony._run_worker`** (worker) [decided — not yet built] | Daemon owns DAG scheduling, feature-branch lifecycle, Slack escalation, sub-tree parking; worker (`_run_worker`) owns per-issue worktree + turn-loop; symphony's poll/dispatch loop dropped |
| Isolation | **Single Docker container** running baton-harness (vendored symphony) + Claude Code | Restores host boundary lost by worktree-only isolation |
| Comms | **Slack Bolt bot (Socket Mode)** + official GitHub→Slack app | Two channels, two purposes — active decisions + passive activity |
| CI | **GitHub Actions** (per-project precondition) | Verification is the project's responsibility, not the pipeline's |
| Auth (Claude) | **OAuth via mounted volume** — no `ANTHROPIC_API_KEY` | Subscription-only; prevents accidental per-token billing |
| Auth (GitHub) | **Pre-configured PAT** available to harness | Specified in problem statement |

---

## 3. The five-layer architecture

```
┌─────────────────────────────────────────────────────────┐
│  Layer 1 — Human checkpoints (YOU)                      │
│  morning: approve issues  ·  evening: review/merge PRs  │
└─────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────┐
│  Layer 2 — Async communication (Slack)                  │
│  ┌──────────────────────┐  ┌──────────────────────┐     │
│  │ #agent-decisions     │  │ #agent-activity      │     │
│  │ Bolt bot, two-way    │  │ GitHub→Slack app     │     │
│  │ Block Kit + replies  │  │ passive notifications│     │
│  └──────────────────────┘  └──────────────────────┘     │
└─────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────┐
│  Layer 3 — Source of truth (GitHub)                     │
│  milestone → issues = queue                             │
│  labels: agent-ready → in-progress → done/blocked/failed│
│  PRs + Actions = output + verification                  │
└─────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────┐
│  Layer 4 — Orchestration (always-on daemon + worker)    │
│  custom daemon: DAG schedule, feature-branch, Slack     │
│  worker: vendored symphony._run_worker (per-issue)      │
│  after_run router · config: WORKFLOW.md (prompt body)   │
└─────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────┐
│  Layer 5 — Execution (Claude Code in git worktree)      │
│  CLAUDE.md · skills · plugins · hooks (deterministic)   │
│  runs inside Docker container, OAuth via volume         │
└─────────────────────────────────────────────────────────┘
```

### 3.1 Layer 1 — Human checkpoints

Two human-owned bookends; everything between is the system's responsibility.

**Morning (≤30 min):** review the milestone, confirm issues are well-scoped and have clear acceptance criteria, apply the `agent-ready` label to each issue that should run. Nothing runs without this label.

**Evening:** review draft PRs, respond to `agent-blocked` issues, triage `agent-failed` issues, merge what's ready. PRs remain draft until merged by me.

**Daytime:** respond to Slack decision cards as needed; no active monitoring required.

### 3.2 Layer 2 — Async communication

Two distinct channels, two distinct purposes.

**`#agent-activity`** — passive awareness, fed by the official GitHub→Slack app. Notifications on PR open, CI status, issue activity. Mutable; can be muted; zero code to maintain.

**`#agent-decisions`** — active interaction, fed by a custom Slack Bolt app. Every message in this channel requires action: agent asking a threshold-crossing question, run failed, blocked sub-tree surfaced. Uses Block Kit interactive cards for bounded approvals and thread replies for freeform steering.

**Guidance flow [decided — not yet built]:** this channel is the notification surface for the always-on daemon's escalation path. When the daemon detects a `blocked` label (agent has posted a question on the issue), it posts a stall summary card here. The human posts guidance directly on the GitHub issue and removes `blocked`; the daemon's next poll sees the label gone and resumes the parked sub-tree. Slack is the *channel*; the GitHub issue is the *durable record* the agent reads.

**Connection method:** Slack **Socket Mode**. The bot opens an outbound WebSocket to Slack; no inbound webhook, no reverse proxy, no public TLS endpoint on the server. Bot runs as a persistent process (systemd or a sidecar container).

**State correlation:** every decision card embeds the issue number in the button `value` and the thread `thread_ts`. Replies are routed back to the originating issue. State persists on the GitHub issue as a comment — Slack is the *channel*, GitHub is the *record*.

### 3.3 Layer 3 — Source of truth

GitHub is the *only* place state lives. Nothing tracked in side databases or files.

**Queue:** a milestone with its associated issues. Order is informal; the orchestrator picks any issue labelled `agent-ready` and not labelled `blocked`.

**Label state machine:**
```
agent-ready  ─▶  agent-in-progress  ─▶  agent-done
                                    ─▶  agent-blocked   (needs my input)
                                    ─▶  agent-failed    (auto-retries exhausted)
```

Transitions are owned by the orchestrator (Layer 4); the human owns initial `agent-ready` application and final disposition of `agent-blocked` / `agent-failed` / merged-PR.

**Durable context:** when the agent has questions or I have answers, they live as **issue comments**. The next run reads the issue thread (including comments) as its instructions. There is no side channel for "guidance" — everything that influences the agent is visible on the issue.

**CI:** GitHub Actions, configured per-project before the project enters the workflow. Runs on every PR; the agent does not own test execution.

### 3.4 Layer 4 — Orchestration

**Current pilot [implemented]:** The external-process Baton model. Baton runs as a single long-lived process; its three internal components (Poller, Dispatcher, Reconciler) handle issue polling and dispatch. Config lives in `WORKFLOW.md`, passed via `baton start -w`. The `after_run` hook fires after each run and drives label reconciliation.

**Decided target — orchestrator/worker split [decided — not yet built]:**

The orchestration layer is split into two components with distinct responsibilities:

**Orchestrator = custom always-on daemon.** A persistent harness process that never exits between work units or on a block. It watches for ready work units, builds and schedules the DAG (`graphlib.TopologicalSorter`), owns the `feature/<slug>` branch lifecycle (creation, CI-gated `--no-ff` merge of per-issue branches, draft `feature → main` PR at completion), drives Slack escalation, and parks/resumes sub-trees. Symphony's flat poll/dispatch loop (`run`/`_tick`/`_dispatch`/`_on_worker_done`), `cli.start`, and `watchfiles` are **dropped** — the custom daemon replaces them entirely.

**Worker = vendored `symphony._run_worker`.** Called by the daemon as a library function per issue. Responsible for: creating the per-issue git worktree, firing `before_run` and `after_run` hooks, running the `claude -p` turn-loop, and detecting PR creation. The worker is the boundary between the daemon and Claude Code. Worktrees and branches follow symphony's existing naming: `.symphony/worktrees/<N>` (bare-integer directory) and `baton/<slug>-<N>` branches. The daemon resolves the issue number from the worktree directory basename (works post-PR #20). Base-ref to the feature branch: the daemon checks out `feature/<slug>` as HEAD before calling `_run_worker`, so symphony's HEAD-based worktree creation naturally targets the feature branch.

**Vendor patches (minimal) [decided — not yet built]:**
- **VP-1 (P0):** `run_hook` gains an `env=` parameter — threads `CHAIN_BASE_BRANCH` (correct `before_run` rebase target for feature-branch runs) and `BH_VENV` (hook discovery) through to hook calls.
- **VP-2:** Re-check `exclude_labels` inside the `_run_worker` turn loop — makes a block terminal, retiring the `max_turns: 2` workaround (issue #23).

No naming patch (CONCERN-1 resolved by base-ref approach above). No retry wiring (no retry in v1 — see §6).

**`before_run` rebase target [implemented / decided — not yet built]:** `origin/main` for standalone work units [implemented]. For work units under a milestone (feature-branch runs), the rebase target is the feature branch — threaded via `CHAIN_BASE_BRANCH` enabled by VP-1 [decided — not yet built].

**`after_run` outcome router** [implemented] — inspects what the run produced and decides what to do next. Implemented as a Python module (`after_run.py`) in the `baton_harness` package; see the implementation-language decision in [harness-design.md](./harness-design.md). Pseudocode:

```
if PR opened and CI green       → label agent-done; notify #activity (already covered by GitHub app)
elif issue has new "blocked" label → post stall summary to #agent-decisions; park sub-tree
elif PR opened but agent flagged doubt in comment → post decision card to #agent-decisions
elif no PR, no comment           → label agent-failed; park sub-tree; escalate to #agent-decisions
else                             → label agent-failed; escalate
```

No retry in v1: a `no_pr` / block / failure → park sub-tree + Slack-escalate, full stop. The vendored `state.py` retry/backoff is unused.

This script is the **Dial 2 of the confidence model** (see §4). It fires inside `_run_worker` under the vendored model — the Slack clarification path is central to the model, not deferred.

### 3.5 Layer 5 — Execution (Claude Code)

Each dispatched issue runs `claude -p` against an isolated git worktree, inside the same container as Baton.

**Context loaded by Claude Code, in order of priority:**

1. **`CLAUDE.md`** — always-on repo context: build commands, test commands, conventions, architectural boundaries, do/don't rules
2. **`.claude/skills/`** — auto-triggered skills; community skills + project-specific skills
3. **`AGENTS.md`** — open-standard context if present (Claude Code reads it)
4. **The issue body and comments** — the actual task

**Tool surface:** bash, file edit, MCP servers (GitHub MCP for issue/PR ops, others per project).

**Deterministic guardrails — Claude Code hooks:** PreToolUse hooks are shell commands that fire before every tool invocation and can block actions outright. This is the **Dial 1** mechanism (see §4). Example hooks:

| Hook | Trigger | Action |
|---|---|---|
| `block-push-to-main` | PreToolUse, Bash matching `git push.*main` | exit non-zero; agent must use a branch |
| `block-infra-paths` | PreToolUse, Edit matching `/infra/**` or `*.tf` | exit non-zero; out of scope per problem statement |
| `block-credential-paths` | PreToolUse, Edit/Read matching `**/.env`, `**/credentials*` | exit non-zero |
| `force-pr-not-merge` | PreToolUse, Bash matching `gh pr merge` | exit non-zero; merges are human-only |

---

## 4. The two-dial confidence model

The model that resolves the original tension: how do I tighten thresholds enough to avoid bad merges without stalling so early that nothing gets done?

**Dial 1 — Agent-level (Layer 5).** When does the agent pause or refuse to act?

| Mechanism | Type | Guarantee |
|---|---|---|
| Prompt + CLAUDE.md instructions | Soft (LLM-judged) | Probabilistic — usually but not always |
| Claude Code PreToolUse hooks | Hard (shell-level) | Deterministic — actions are blocked, period |

Use prompts/CLAUDE.md for *judgment* calls ("if confidence below X, ask"). Use hooks for *invariants* ("never push to main"). The hook system is what makes Dial 1 actually trustworthy.

**Dial 2 — Orchestrator-level (Layer 4).** Which of the agent's signals reach me?

Tuned via:
- `WORKFLOW.md` — `max_concurrent`, `max_turns`, label filters
- `after_run` outcome router — branches based on what the run produced

The always-on daemon [decided — not yet built] is the load-bearing mechanism for Dial 2: it is the component that detects a `blocked` signal, evaluates whether it meets the threshold for Slack escalation, and parks the affected sub-tree while allowing independent work units to continue. Transient failures that fall below the threshold are parked and escalated in v1 (no auto-retry); only the sub-tree is halted — the daemon stays alive. This protects daytime attention without forcing the agent itself to be overconfident.

**Why two dials, not one:** if the only knob is the agent's threshold, you trade off between bothering me too much (low threshold, paged constantly) and bad merges (high threshold, agent guesses). Two independent dials let the agent err toward asking (Dial 1 low) while the daemon filters which asks reach Slack (Dial 2 high).

---

## 5. Deployment topology

Single Docker container running Baton, Claude Code, and supporting tools. The bot runs alongside but as a separate process.

```
┌─────────────────────── server (Linux host) ───────────────────────┐
│                                                                   │
│  ┌─── docker container ──────────────────────────────────────┐    │
│  │                                                           │    │
│  │   user: agent (non-root)                                  │    │
│  │   /home/agent/.claude/  ◀── OAuth credentials (volume)    │    │
│  │   /workspace/repos/<project>/                             │    │
│  │     └─ .git/worktrees/issue-NNN/  (per-issue isolation)   │    │
│  │                                                           │    │
│  │   processes:                                              │    │
│  │     - baton-harness (vendored symphony orchestrator)      │    │
│  │     - one `claude -p` per concurrent issue                │    │
│  │                                                           │    │
│  │   env: ANTHROPIC_API_KEY MUST NOT BE SET                  │    │
│  │                                                           │    │
│  └───────────────────────────────────────────────────────────┘    │
│                                                                   │
│  ┌─── slack-bot container (or systemd) ──────────────────────┐    │
│  │   Bolt app, Socket Mode (outbound WebSocket only)         │    │
│  │   reads/writes: GitHub API + Slack API                    │    │
│  └───────────────────────────────────────────────────────────┘    │
│                                                                   │
└───────────────────────────────────────────────────────────────────┘
```

**Container image contents [decided — not yet built for vendored model]:**
- Base: `node:22-slim` (or equivalent; Claude Code is npm-distributed)
- `@anthropic-ai/claude-code` (pinned version)
- `git`, `gh` CLI
- `baton_harness` package (pip install) — includes vendored `symphony/` source; no separate `baton` pip install required under the vendored model
- Non-root user `agent` (required — Claude Code refuses `--dangerously-skip-permissions` under root)
- No `ANTHROPIC_API_KEY` — strictly OAuth via mounted credentials volume

**Persistence:**
- `claude-credentials` volume → `/home/agent/.claude/` (OAuth state)
- `workspace` volume → `/workspace/` (repo clones + worktrees, persists across container restarts)

**Concurrency:** `max_concurrent` in `WORKFLOW.md` is bounded by the **Claude subscription rate limit**, not by container resources. Setting it to 6 doesn't help if the subscription caps you at ~3 concurrent inference streams. Start at 2 and tune.

---

## 6. Lifecycle of a work unit [decided — not yet built]

There is one execution path, parameterized by the DAG. A **work unit** is either a milestone (all its issues form one DAG → one `feature/<slug>` branch → one draft `feature → main` PR) or a single un-milestoned issue (its own N=1 DAG → its own feature branch → its own PR). N=1 is the degenerate case of the same logic — not a separate code path.

The always-on daemon owns the outer loop and never exits between work units or on a block.

**Current pilot [implemented]:** the flat label-polling model below; the always-on daemon model is decided but not yet built.

### 6.1 Work-unit dispatch

| Step | Layer | What happens |
|---|---|---|
| 1. Morning | Human | Label the root issue (or milestone issues) `agent-ready` |
| 2. ≤30s later | Daemon (pilot: external Baton poller) | Detects `agent-ready` label; resolves work unit (milestone lookup or single issue); builds the DAG |
| 3. Feature branch | Daemon | Creates `feature/<slug>` branch (or uses `main` for N=1 in the pilot); daemon checks out this branch as HEAD before calling `_run_worker` |
| 4. Per-issue dispatch | Daemon | Topological order via `graphlib.TopologicalSorter`; for each ready issue: transitions label to `agent-in-progress`, calls `_run_worker(issue)` |
| 5. Run | Worker (`_run_worker`) | Creates per-issue worktree off feature branch; fires `before_run` (rebase onto feature branch); runs `claude -p` turn-loop; fires `after_run` |
| 6. Route | `after_run` outcome router | PR opened → CI-gated `--no-ff` merge into feature branch → "dependency satisfied"; block or fail → park sub-tree + Slack escalation |
| 7. Continue | Daemon | Independent branches in the DAG continue; parked sub-tree waits for guidance |
| 8. Completion | Daemon | All issues in the DAG complete → daemon opens one draft `feature/<slug> → main` PR; harness never merges to `main` |
| 9. Evening | Human | Review the draft PR; merge when satisfied; triage any parked blocked/failed issues |

### 6.2 Guidance flow for blocked issues [decided — not yet built]

The agent does not pause mid-run waiting for a reply. Each run is one-shot: read → work → exit. When the agent hits a threshold-crossing question it cannot resolve, it posts the question as a comment on the issue and applies the `blocked` label before exiting.

| Step | Who | What happens |
|---|---|---|
| 1 | Worker | Agent comments the question on the issue; applies `blocked` label; `_run_worker` exits |
| 2 | Daemon | `after_run` detects `blocked`; daemon parks the affected sub-tree; posts a stall summary card to `#agent-decisions` on Slack |
| 3 | Human | Reads the card; posts guidance directly on the GitHub issue; removes `blocked` |
| 4 | Daemon | Next poll sees `blocked` gone; resumes the parked sub-tree; re-dispatches the issue |

The GitHub issue is the durable record (the agent reads the answer from the issue comment on its next run). Slack is the notification channel only. Sub-tree parking is local to the blocked branch — the daemon continues independent branches and other work units uninterrupted.

**No retry in v1:** a `no_pr` / block / failure parks the sub-tree and escalates. The vendored `state.py` retry/backoff is unused.

For the full DAG spec, see [harness-design.md §10](./harness-design.md) and [docs/superpowers/specs/dependency-chain-orchestration.md](../docs/superpowers/specs/dependency-chain-orchestration.md).

---

## 7. Open items (deferred to phase 2 or implementation)

### 7.1 Observability — deferred to phase 2

Langfuse is the leading candidate but integrates with Claude Code less cleanly than it did with OpenHands' LiteLLM routing. Initial pipeline will rely on:

- Baton's terminal logging (operational signal)
- Claude Code session transcripts (where it stores them, configurable)
- GitHub issue comments (semantic signal — what the agent actually said)

Full tracing dashboard is a phase-2 addition once the pipeline is running and gaps are concrete.

### 7.2 PR review assistance — deferred

CodeRabbit (free for public repos) or Qodo Merge (self-hosted). Decision depends on which projects are public vs private. Not blocking for initial pipeline.

### 7.3 Outcome detection sharpness

The `after_run` script must inspect GitHub state because exit codes alone don't distinguish done / blocked / failed. Implementation detail: the script reads issue labels, checks for new comments since run start, and checks for an opened PR linked to the issue. Heuristic; needs tuning during initial use.

### 7.4 Slack bot scope creep

Start narrow: only decision cards and reply handling. Resist building activity-feed features the GitHub→Slack app already provides for free. Re-evaluate after a month of use.

### 7.5 ToS posture on headless first-party use

Anthropic's terms have been revised twice in 2026. Running the real `claude` binary containerised on subscription is normal use; running it *headlessly via orchestrator* is a less-explicitly-blessed grey zone. Action: read current Claude Code subscription terms directly before locking the architecture; reconfirm at major terms updates.

---

## 8. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Subscription rate limit caps real concurrency below desired | High | Medium | Start `max_concurrent: 2`; tune empirically; accept that throughput is capped by plan |
| Worktree-level isolation isn't enough (port collisions on shared dev services) | Medium | Medium | Container around the whole stack already adds a boundary; per-issue port namespacing in `before_run` if needed |
| Agent makes wrong design choice on ambiguous issue | Medium | High | Two-dial model; deterministic hooks; explicit confidence rule in prompt |
| Anthropic ToS changes break the supported path | Low | High | First-party binary stays compliant; track terms updates; willingness to add API-key fallback if subscription path is closed |
| Silent failure — harness process dies, no notifications fire | Low | High | Systemd auto-restart on container; daily health-check Slack message; reconciler catches stuck runs |
| Slack bot WebSocket disconnects, decision cards never arrive | Medium | High | Bolt SDK auto-reconnects; second channel via GitHub→Slack app provides a fallback signal route |
| Accidental `ANTHROPIC_API_KEY` set on host, leaks into container, bills per-token | Low | High | Explicit env validation at container startup; refuse to start if set |
| OAuth credentials in volume get corrupted or rotated | Low | Medium | Re-auth flow is one-time and documented; back up `~/.claude/.credentials.json` after first auth |

---

## 9. What's explicitly out of scope

- **Infrastructure/IaC work** (Azure, Terraform, etc.) — per problem statement
- **Design decisions** (UI/UX choices) — per problem statement; only implementation
- **Cross-repo work** — v1 is a **single-repo daemon** (one daemon per repo). The binding constraint is the GitHub dependency API (`blocked_by`/`blocking`), which is same-repo only; a work unit cannot span repositories by definition. Multi-repo is explicitly deferred, with two seams kept clean for it: (a) the daemon poll loop iterates a one-entry repo-registry rather than closing over a single `project_root` (repo #2 = registry append, not a loop rewrite); (b) the concurrency budget is a documented decision — `max_concurrent` in `WORKFLOW.md` — not an in-daemon code object; global cross-repo enforcement is a future supervising/lease layer.
- **Planning/decomposition** (milestone → issues) — human-driven, upstream of this system
- **Test execution authoring** — GitHub Actions is a precondition, not built by the agent
- **Multi-user / team workflows** — solo developer, personal projects

---

## 10. Next steps (post-spec)

1. Read current Claude Code subscription terms and confirm headless use posture
2. Build the Docker image (Dockerfile + entrypoint)
3. Authenticate once interactively, capture the credentials volume
4. Author `CLAUDE.md` and initial PreToolUse hooks for one pilot repo
5. Configure the harness `config/WORKFLOW.md` for that repo
6. Set up the Slack app (BotFather-equivalent), enable Socket Mode, capture tokens
7. Build the minimal Bolt bot (decision card post + button handler + thread reply handler)
8. Write the `after_run` outcome router script
9. Dry run with a non-critical issue end-to-end before locking it for daily use

---

**Document status:** Reformed 2026-06-07 (issue #28) to reflect the vendored-symphony model and chain-driver architecture. Sections tagged `[implemented]` describe the current pilot state; sections tagged `[decided — not yet built]` describe the agreed target (issue #27). Decisions in §2 are updated to the vendored model.
