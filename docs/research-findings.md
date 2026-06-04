# Research findings — autonomous agent development workflow

Relevant tools and solutions found during initial research phase. Evaluated against the problem statement constraints: self-hosted preferred, GitHub as source of truth, subscription-based cost model for core work, no mid-day interruptions required.

**Scope:** Planning and decomposition (milestone → issues) is out of scope — that is a human-driven activity. This assumes a milestone with a list of well-scoped issues already exists. The problem is how to schedule and execute that work reliably, detect drift or failure without active attention, and surface outcomes through async communication.

**Critical constraint surfaced during research:**
Anthropic's 2026 terms of service prohibit using OAuth tokens from Claude subscription accounts in third-party tools or agents. This constraint disqualifies any executor that is not Anthropic's first-party Claude Code binary — i.e. any solution that requires extracting an OAuth token and passing it to a third-party client. This eliminates OpenHands as a viable option under the subscription-based cost model.

**Updated assumptions captured during research:**
- All projects entering the autonomous workflow will have CI and GitHub Actions configured as a precondition. Test execution is not the pipeline's responsibility.
- Notification and async communication is a first-class requirement. The ideal interaction model is Slack for lightweight responses (unblocking a question, acknowledging a stall) and GitHub for substantive reviews (PR approval, issue triage).
- The executor must be Anthropic's first-party Claude Code binary (`claude` CLI) to remain compliant with subscription authentication terms.

---

## Task execution agents

These tools take a GitHub issue and produce a PR without human intervention during the run.

| Tool | Type | Scope | Pros | Cons | Cost | Compliance |
|---|---|---|---|---|---|---|
| **Claude Code** (Anthropic) | Proprietary | Single-issue executor (via Baton orchestration) | First-party binary; runs on subscription without violating ToS; 29 lifecycle hook events for deterministic control; native CLAUDE.md + skills + plugins; full Claude ecosystem | Requires Anthropic's own infrastructure; no open-source alternatives for the binary itself | Covered by Claude Max subscription | ✅ Compliant — OAuth on first-party binary is the supported path |
| **OpenHands** (All-Hands-AI) | OSS | Single-issue executor | MIT licensed; self-hostable via Docker; model-agnostic via LiteLLM; 70k+ stars; sandbox + stuck detection | **DISQUALIFIED: Requires OAuth token in third-party tool (ToS violation) OR per-token API key (breaks cost constraint). Not viable under subscription-based model.** | Free to self-host; model cost separate | ❌ Non-compliant on subscription; requires API key for per-token cost |
| **mini-SWE-agent** (Princeton/Stanford) | OSS | Single-issue executor | ~100 lines of Python; 74%+ SWE-bench; bash-only; runs any LLM | Same ToS issue as OpenHands — third-party client requiring extracted token | Free; model cost separate | ❌ Non-compliant on subscription |
| **SWE-agent** (Princeton/Stanford) | OSS | Single-issue executor | Configurable via YAML; strong SWE-bench scores; purpose-built | Same ToS issue; largely superseded by mini-SWE-agent | Free; model cost separate | ❌ Non-compliant on subscription |

**The OpenHands decision:** OpenHands was initially evaluated as a viable option because it supports model-agnostic routing through LiteLLM, which could in theory run on a Claude subscription. However, this routing requires extracting an OAuth token from your Claude subscription account and feeding it to the third-party OpenHands client — which Anthropic's 2026 consumer terms explicitly prohibit. The alternative, using an API key with per-token billing, violates the subscription-based cost constraint. OpenHands is therefore not a viable option for this use case and has been eliminated from further consideration.

---

## Pipeline orchestration and scheduling

These address the outer shell: polling the issue queue, dispatching jobs, enforcing concurrency limits, and routing outcomes.

| Tool | Type | Scope | Pros | Cons | Cost |
|---|---|---|---|---|---|
| **Baton** (mraza007) | OSS | Orchestrator + scheduler | GitHub-native polling; one WORKFLOW.md file for all configuration; built-in poller/dispatcher/reconciler; git worktree isolation per issue; first-class Claude Code support; minimal dependencies | Worktree isolation is coarser than containers (shared host); no visual dashboard | Free |
| **System cron + shell/Python** | Built-in | Scheduler + queue poll | Zero dependencies; always available on Linux; trivial to reason about; ~50–100 lines covers queue poll, dispatch, concurrency cap, label transitions | No UI or observability; silent failure modes unless logging is added explicitly; no retry/backoff built in | Free |
| **n8n** | OSS (fair-code) | Visual workflow automation | Self-hostable via Docker; native GitHub node (triggers on label events, reads/writes issues); cron and webhook triggers; AI-native; visual editor reduces maintenance burden | Fair-code licence (not fully OSS); additional service to host; overkill for the straightforward Baton-like workflow | Free self-hosted community edition |
| **Prefect** | OSS | Python workflow orchestration | Python-native; built-in scheduling, retries, failure recovery, observability dashboard | Heavier than needed for solo use; separate service to run | Free self-hosted OSS core |
| **Temporal** | OSS | Durable workflow execution | Survives server crashes; built-in retries, signals, timers | Production self-hosting requires Kubernetes + PostgreSQL + Elasticsearch — significant overhead for one developer | Free OSS; Cloud from $25/mo |

**Recommendation:** Baton is the clear fit for this use case. It's purpose-built for the exact orchestration pattern needed (GitHub polling → concurrency control → Claude Code execution), all configuration lives in one file, and it integrates natively with Claude Code without needing to extract credentials or implement glue code.

---

## Notification and async communication

This covers how outcomes reach you proactively and how you respond without leaving your current context.

### GitHub → Slack notification (passive activity)

| Tool | Type | Scope | Pros | Cons | Cost |
|---|---|---|---|---|---|
| **GitHub official Slack app** (github.com/integrations/slack) | Proprietary (free) | GitHub event → Slack notification | Official integration; subscribe channels to repos; notifies on PR open, review request, workflow run completion, issue activity; no infrastructure to host | One-directional — notifications only, no ability to act back on GitHub from Slack | Free |
| **GitHub Actions → Slack webhook** | DIY via GitHub Actions | Event-driven Slack message | Runs inside existing GitHub Actions — no extra infrastructure; full control over message content and triggers; label-specific notifications | Must be written and maintained per repo; no two-way interaction | Free (Slack incoming webhook is free) |

### Two-way Slack interaction (respond from Slack, write back to GitHub)

| Tool | Type | Scope | Pros | Cons | Cost |
|---|---|---|---|---|---|
| **Custom Slack bot (Bolt SDK, Socket Mode)** | DIY OSS | Slack → GitHub write-back | Full control; post Block Kit decision cards with buttons; when clicked, updates GitHub issue label or posts comment; Socket Mode avoids public webhook exposure; runs on your server | Must be built and hosted; requires a persistent listener on your server | Free (Slack API is free for standard use) |
| **n8n Slack trigger + GitHub node** | OSS (fair-code) | Interactive Slack → GitHub | Slack interactive message buttons can trigger n8n workflows; n8n writes back to GitHub; visual, no custom code | Requires n8n; interactive payloads need a public webhook URL (or VPN like Tailscale) | Free self-hosted |

**Recommendation:** GitHub official Slack app for passive notifications (free, zero-maintenance). Custom Slack Bolt bot with Socket Mode for active decision cards and two-way interaction. Socket Mode avoids the public-webhook requirement, keeping your server less exposed.

---

## Drift and failure detection

Two distinct concerns: operational drift (run stalled, label stuck, process died silently) and semantic drift (run completed but output is wrong or misaligned).

### Operational monitoring

| Tool | Type | Scope | Pros | Cons | Cost |
|---|---|---|---|---|---|
| **GitHub label FSM + watchdog script** | DIY | Stall detection | Extremely simple: query for issues stuck in `agent-in-progress` beyond a timeout threshold and escalate; no extra services; native to GitHub | Must be written; no visualisation; blind to what happened inside the run | Free |
| **Baton's built-in reconciler** | OSS | Stale run detection | Included in Baton; detects runs that haven't updated in configurable timeout; no extra work | Timeout-based rather than semantic (coarser than stuck-detection logic) | Included with Baton |

### Agent observability (diagnosing run failures)

| Tool | Type | Scope | Pros | Cons | Cost |
|---|---|---|---|---|---|
| **Claude Code hooks system** | Proprietary | Lifecycle event hooks | 29 programmable hook events across session lifecycle, tool use, file changes, agent coordination; hooks are deterministic shell commands, not LLM-based; guarantee enforcement (block patterns before they execute) | Hooks don't inherently generate traces or dashboards; you build what you need via hook commands | Included with Claude Code |
| **Langfuse** | OSS (MIT) | LLM trace + eval | MIT licensed; self-hostable via Docker Compose; full trace visibility (prompts, completions, tool calls, token usage, latency); OpenTelemetry-native | Originally designed around LiteLLM routing (which OpenHands uses); integration with Claude Code's hooks requires custom wiring. Not yet a turnkey solution for Claude Code on subscription. Likely phase 2. | Free self-hosted; Cloud plans from $39/seat/mo |

---

## PR review assistance

Agent-generated PRs require review before merging. These tools reduce the review burden by providing automated analysis of the diff before you look at it.

| Tool | Type | Scope | Pros | Cons | Cost |
|---|---|---|---|---|---|
| **Qodo Merge PR-Agent** (formerly CodiumAI) | OSS | AI PR review | Fully self-hostable via Docker, GitHub Actions, or GitLab CI; works with GitHub, GitLab, Bitbucket, Gitea; open-source transparency; persistent rules system with memory | Requires setup and maintenance; quality depends on underlying model configured | Free OSS self-hosted; Pro $30/user/mo |
| **CodeRabbit** | Proprietary (SaaS) | AI PR review | Two-click setup; PR summaries, inline comments, 1-click fix suggestions, security scans; **free for public/OSS repos**; no infrastructure to run | Cloud SaaS — code leaves your infrastructure (mitigated by zero-retention policy and SOC 2 certification); per-seat cost on private repos | **Free for OSS; Pro $24/user/mo; Enterprise self-hosted** |

**Recommendation:** CodeRabbit free tier if your projects are public. Qodo Merge self-hosted if privacy is required. Likely a phase-2 addition after the core pipeline is running.

---

## Summary observations

- **OpenHands has been disqualified** due to the terms-of-service constraint. Running a third-party agent with an extracted OAuth token from your subscription violates Anthropic's 2026 consumer terms. Using an API key instead breaks the subscription cost model. It is not viable.
- **Claude Code via Baton is the clear path forward.** The first-party Claude Code binary can be authenticated on your subscription without violating ToS, Baton provides the orchestration layer off-the-shelf, and Claude Code's hook system provides the deterministic control surfaces you need for the two-dial confidence model.
- **The executor decision is now settled** by the ToS constraint, not by weighing capabilities. The first-party binary is the only compliant path on subscription, which fortunately also has the strongest control surface (hooks + CLAUDE.md + skills + plugins).
- **Baton is the minimum viable harness.** Its poller/dispatcher/reconciler is exactly what you specified; all configuration fits in one WORKFLOW.md; and it integrates natively with Claude Code without credential extraction or glue code.
- **Isolation via Docker is achievable** — Baton + Claude Code can run inside a single container, restoring per-execution boundaries while keeping the subscription auth clean (OAuth credentials mounted as a volume, no API key in the image).
- **Observability is a known gap** — Langfuse's integration with Claude Code on subscription is not yet turnkey and is deferred to phase 2. Hooks can generate operational signals; full-session tracing requires future work.
- **Notification and async communication are solved** — GitHub Slack app (free, passive) + custom Bolt bot (Socket Mode, active two-way).
