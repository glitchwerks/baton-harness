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
| Orchestrator | **Baton** (mraza007/baton) | Purpose-built poller/dispatcher/reconciler; off-the-shelf |
| Isolation | **Single Docker container** running Baton + Claude Code | Restores host boundary lost by worktree-only isolation |
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
│  Layer 4 — Orchestration (Baton, the broker)            │
│  Poller · Dispatcher · Reconciler · after_run router    │
│  config: single WORKFLOW.md                             │
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

**`#agent-decisions`** — active interaction, fed by a custom Slack Bolt app. Every message in this channel requires action: agent asking a question, run failed, blocked issue surfaced. Uses Block Kit interactive cards for bounded approvals and thread replies for freeform steering.

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

### 3.4 Layer 4 — Orchestration (Baton)

Baton runs as a single long-lived process inside the container. Its three internal components map exactly to the broker role:

- **Poller** — every 30s, runs `gh issue list` to find issues labelled `agent-ready` (excluding `blocked`).
- **Dispatcher** — enforces `max_concurrent`, transitions the label to `agent-in-progress`, creates a git worktree per issue, and runs the configured agent command.
- **Reconciler** — detects stale runs (no progress past a timeout) and releases their slots.

All configuration in `WORKFLOW.md`:

```yaml
---
tracker:
  kind: github
  labels: ["agent-ready"]
  exclude_labels: ["blocked"]
polling:
  interval_ms: 30000
agent:
  max_concurrent: 2          # tuned to subscription rate limit, not container count
  max_turns: 5               # bounded retries per issue
  command: claude
  permission_mode: acceptEdits
mcp_servers:
  - name: github
    command: npx @modelcontextprotocol/server-github
hooks:
  before_run: |
    git fetch origin main && git rebase origin/main
  after_run: |
    /opt/harness/route-outcome.sh "$ISSUE_NUMBER"
---
You are working on issue #{{ issue.number }}: {{ issue.title }}

{{ issue.body }}

Confidence rule: if any acceptance criterion has more than one reasonable
interpretation, do NOT implement. Post a comment with your specific question,
add the `blocked` label, and stop.

When done: commit, push, and open a draft PR linking to #{{ issue.number }}.
```

**`after_run` outcome router** — a shell script that inspects what the run produced and decides what to do next. Pseudocode:

```
if PR opened and CI green       → label agent-done; notify #activity (already covered by GitHub app)
elif issue has new "blocked" label → post decision card to #agent-decisions
elif PR opened but agent flagged doubt in comment → post decision card to #agent-decisions
elif no PR, no comment           → increment retry counter; if exceeded, label agent-failed and notify
else                             → label agent-failed; notify
```

This script is the **Dial 2 of the confidence model** (see §4).

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

The orchestrator can liberally auto-retry transient failures, auto-mark clean PRs as done, and only escalate to Slack on genuine ambiguity. This protects daytime attention without forcing the agent itself to be overconfident.

**Why two dials, not one:** if the only knob is the agent's threshold, you trade off between bothering me too much (low threshold, paged constantly) and bad merges (high threshold, agent guesses). Two independent dials let the agent err toward asking (Dial 1 low) while the orchestrator filters which asks deserve my attention (Dial 2 high).

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
│  │     - baton (poller + dispatcher + reconciler)            │    │
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

**Container image contents:**
- Base: `node:22-slim` (or equivalent; Claude Code is npm-distributed)
- `@anthropic-ai/claude-code` (pinned version)
- `git`, `gh` CLI
- `baton` (pip install)
- Non-root user `agent` (required — Claude Code refuses `--dangerously-skip-permissions` under root)
- No `ANTHROPIC_API_KEY` — strictly OAuth via mounted credentials volume

**Persistence:**
- `claude-credentials` volume → `/home/agent/.claude/` (OAuth state)
- `workspace` volume → `/workspace/` (repo clones + worktrees, persists across container restarts)

**Concurrency:** `max_concurrent` in `WORKFLOW.md` is bounded by the **Claude subscription rate limit**, not by container resources. Setting it to 6 doesn't help if the subscription caps you at ~3 concurrent inference streams. Start at 2 and tune.

---

## 6. Lifecycle of an issue

End-to-end flow for a single well-scoped issue:

| Step | Layer | What happens |
|---|---|---|
| 1. Morning | Human | I label issue `agent-ready` |
| 2. ≤30s later | Baton poller | Sees the label, checks concurrency cap |
| 3. Dispatch | Baton dispatcher | Transitions label to `agent-in-progress`, creates worktree, runs `before_run` hook (rebase on main) |
| 4. Run | Claude Code | Loads CLAUDE.md + skills + issue context, reads issue body, plans, writes code, runs tests, commits, opens draft PR |
| 5. Exit | Baton | Run terminates; `after_run` hook fires |
| 6. Route | Outcome router | Inspects state of PR + issue → done / blocked / failed / retry |
| 7. Notify | Bolt bot | If blocked or failed, posts a decision card to `#agent-decisions` |
| 8. Respond | Human | Tap button or reply in thread → bot writes back to GitHub issue (label change or comment) |
| 9. Re-queue | Baton poller | If issue is back to `agent-ready` (with new comment as guidance), it picks it up again |
| 10. Evening | Human | Review PRs, merge approved ones, triage any remaining failed/blocked |

**Note on interaction model:** the agent does not pause mid-run waiting for my reply. Each run is one-shot: read → work → exit. My guidance arrives as the *next* issue comment, picked up on the *next* run. This is intentional — it keeps containers stateless and disposable, and uses GitHub as the durable record of the conversation.

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
| Silent failure — Baton dies, no notifications fire | Low | High | Systemd auto-restart on container; daily health-check Slack message; reconciler catches stuck runs |
| Slack bot WebSocket disconnects, decision cards never arrive | Medium | High | Bolt SDK auto-reconnects; second channel via GitHub→Slack app provides a fallback signal route |
| Accidental `ANTHROPIC_API_KEY` set on host, leaks into container, bills per-token | Low | High | Explicit env validation at container startup; refuse to start if set |
| OAuth credentials in volume get corrupted or rotated | Low | Medium | Re-auth flow is one-time and documented; back up `~/.claude/.credentials.json` after first auth |

---

## 9. What's explicitly out of scope

- **Infrastructure/IaC work** (Azure, Terraform, etc.) — per problem statement
- **Design decisions** (UI/UX choices) — per problem statement; only implementation
- **Cross-repo work** — Baton runs project-local; one orchestrator instance per repo
- **Planning/decomposition** (milestone → issues) — human-driven, upstream of this system
- **Test execution authoring** — GitHub Actions is a precondition, not built by the agent
- **Multi-user / team workflows** — solo developer, personal projects

---

## 10. Next steps (post-spec)

1. Read current Claude Code subscription terms and confirm headless use posture
2. Build the Docker image (Dockerfile + entrypoint)
3. Authenticate once interactively, capture the credentials volume
4. Author `CLAUDE.md` and initial PreToolUse hooks for one pilot repo
5. Configure Baton's `WORKFLOW.md` for that repo
6. Set up the Slack app (BotFather-equivalent), enable Socket Mode, capture tokens
7. Build the minimal Bolt bot (decision card post + button handler + thread reply handler)
8. Write the `after_run` outcome router script
9. Dry run with a non-critical issue end-to-end before locking it for daily use

---

**Document status:** First draft. Expected iteration on §3.4 (outcome router details), §5 (container image contents), §7 (open items). Decisions in §2 are locked unless a new constraint surfaces.
