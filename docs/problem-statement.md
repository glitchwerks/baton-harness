# Problem statement — autonomous agent development workflow

## Context

I am a senior software engineer starting a new job that will significantly reduce my available time for personal project development. I want to continue making meaningful progress on personal projects despite this constraint.

## Core problem

I need a workflow where I can define work at a high level — milestones, features, scoped tasks — hand it off to an autonomous agent system, and return to reviewed, actionable output at the end of the day. The system must operate without requiring my attention during working hours.

## Constraints

### Infrastructure
- Self-hosted. I have a server. No cloud-hosted agent systems.
- GitHub is the single source of truth. Issues, PRs, and milestones are the interface. No external task files or parallel tracking systems.

### Cost
- Core agent work (planning, decomposition, coding, review) must run on subscription to avoid per-token costs at scale. Target: Claude Code Max 20x ($200/mo), which includes API credits for lightweight orchestration use.
- Orchestration harness (queue polling, label transitions, notifications, dispatch logic) may use API calls. These are cheap, infrequent, and covered by the Max 20x credit allocation.

### Terms of service
- Executor must be Anthropic's first-party Claude Code binary (`claude` CLI). OAuth tokens from Claude subscription accounts cannot be used in third-party tools or agents per Anthropic's 2026 consumer terms — using a third-party agent with an extracted OAuth token violates the terms of service.
- Only compliant paths: (1) running the real Claude Code binary (subscription-authenticated), or (2) using an API key with per-token billing. Since subscription is the cost model, the executor must be the first-party binary.

### Time
- Morning setup: up to 30 minutes to define and approve the starting point before work.
- Evening review: available to review output, respond to blocked issues, and approve PRs.
- During the day: minimally available. The system may surface threshold-crossing questions via Slack and wait for async guidance; it must not surface every minor question. [implemented — daemon escalates blocked sub-trees via Slack + GitHub issue comment]

## Accountability model

The system operates between two human checkpoints that I own:

**Starting point — approved by me**
I define the milestone or feature scope, write acceptance criteria, and explicitly signal that the work is ready to be planned and executed. Nothing runs without this approval.

**Mid-run escalation — answered async by me [implemented]**
When the agent hits a threshold-crossing question it cannot resolve, it posts the question as a comment on the issue and applies the `blocked` label. The orchestrator pings me on Slack with a stall summary. I post guidance on the issue and remove `blocked`; the daemon's next poll resumes the parked sub-tree. The GitHub issue is the durable record; Slack is the notification channel. Questions that fall below the threshold do not reach me — the system handles them autonomously.

**End point — approved by me**
All PRs are draft until I review and merge. Blocked issues sit idle until I respond. Nothing ships without my sign-off.

> **Note on chain-driver orchestration [implemented, issue #27]:** The always-on daemon performs `git merge --no-ff` of completed per-issue branches into the feature branch without per-issue human review. This is an intra-feature-branch operation — analogous to a developer's own local `git merge` while building a feature. The "human owns merge" checkpoint operates at the `feature → main` boundary: the harness opens a single draft `feature → main` PR; the human reviews and merges that. The daemon never merges to `main`.

Everything between those two checkpoints is the agent's responsibility, with threshold-crossing questions escalated asynchronously as described above.

## Non-goals

- Not fully autonomous end-to-end. Human approval is required at both ends of every work unit, with minimal async guidance on threshold-crossing questions the system escalates during a run.
- Not zero-setup. Reasonable morning prep time (up to 30 minutes) is acceptable.
- Not a team workflow. This is a solo developer working on personal projects.
- Not perfectly pre-scoped work. The system must handle ambiguity gracefully — surfacing it rather than guessing — rather than requiring pristine issue backlogs upfront.

## Assumptions

### Work scope
- Unattended agent runs are restricted to application-layer work only. Infrastructure changes — cloud resources, deployment configuration, IaC (e.g. Azure, Terraform) — are explicitly out of scope and will not be included in any milestone handed to the system.
- UI implementation is in scope. Design decisions (layout, visual direction, component choices) are not — those must be resolved by me before a milestone is handed off. The agent implements against a defined design, it does not make design decisions.

### Authentication and credentials
- Any service the agent needs to call during a run must have authentication pre-configured before the run starts. The agent will not be asked to acquire, rotate, or manage credentials.
- GitHub API access is pre-configured via a personal access token available to the harness. Other service credentials (e.g. a third-party API a project depends on) are set up per-project in the environment before that project is onboarded to the workflow.

## Success criteria

- I can define a milestone before leaving in the morning and return to PRs and/or clearly articulated blocked questions at end of day.
- No action is required from me between kickoff and review, beyond answering the rare, threshold-crossing questions the system escalates.
- The system fails safely — ambiguity surfaces as a blocked issue, not silent wrong output or wasted compute.
- Core work cost stays within subscription bounds regardless of how many issues run in a day.
