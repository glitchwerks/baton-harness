# Open questions and validation requirements

**Status:** Companion document to [architecture-spec.md](./architecture-spec.md). Captures the assumptions, handwaves, and weak points identified during adversarial review of the first draft. Each item has a resolution path.

**Purpose:** Surface implementation risks before they cost days. Items are grouped by severity. Severity 1 items must be resolved before locking the architecture; Severity 2 items must be resolved before production use; Severity 3 should be resolved during initial rollout; Severity 4 are flagged for future work.

---

## Severity 1 — Load-bearing assumptions that could collapse the design

### S1.1 — ToS compliance for headless first-party use is unverified
**The assumption:** Running the real `claude` binary in containerised headless mode, driven by Baton, on subscription auth is within Anthropic's terms of service.

**Why it matters:** The entire executor decision (and rejection of OpenHands) hinges on this being compliant. If headless orchestrated use is non-compliant, the system either violates ToS or reverts to API-key per-token billing — breaking the core cost constraint.

**Resolution:** Before any implementation, read the current Claude Code subscription terms directly at code.claude.com and confirm posture on:
- Headless `claude -p` invocation by an external orchestrator
- Long-running unattended sessions
- Containerised use on subscription
If unclear, contact Anthropic support for explicit guidance. **Block all other work until resolved.**

---

### S1.2 — Baton + Claude Code integration behavior is unverified
**The assumption:** Baton's `command: claude` cleanly invokes the first-party binary; hooks fire; exit codes are meaningful; the reconciler properly kills stuck processes.

**Why it matters:** Every layer of the architecture sits on top of this integration working as imagined. We have no empirical evidence it does.

**Specific unknowns:**
- Does `--dangerously-skip-permissions` work through Baton's invocation chain?
- How does Baton handle `claude` interactive prompts (`/login`, permission prompts) in headless mode?
- What does Baton see when `claude` exits zero with no output, exits non-zero, or hangs?
- Does the reconciler kill stuck `claude` processes, or only release the slot in bookkeeping?

**Resolution:** Validate via spike (see [smoke-test-spike.md](./smoke-test-spike.md)).

---

### S1.3 — Subscription rate limits are unmeasured
**The assumption:** Max 20x supports meaningful concurrency for parallel issue runs; `max_concurrent: 2` is a reasonable starting point.

**Why it matters:** If the real concurrency cap is 1, parallelism is theatre. The June 2026 Agent SDK credit bucket may further change the model.

**Specific unknowns:**
- Throttle mechanism: concurrent streams, tokens-per-minute, daily total, or combination?
- Failure mode when exceeded: clean 429, hang, degraded response?
- Does headless `claude -p` consume the same bucket as interactive use?
- How does the June 2026 Agent SDK credit interact with this?

**Resolution:** Measure during spike with two concurrent issues. Document observed behavior. Re-measure after June 2026 Agent SDK changes take effect.

---

## Severity 2 — Real problems with known shapes but no defined solutions

### S2.1 — Outcome router is hand-waved and load-bearing
**The assumption:** A ~30-line `after_run` script can reliably distinguish done / blocked / failed / retry outcomes from Baton's run results.

**Why it matters:** This script is Dial 2 of the confidence model. Its decisions determine what reaches Slack and what gets retried. Wrong here = either bothering you constantly or silently dropping signals.

**Specific edge cases not addressed in current spec:**
- "No PR opened" — was that a failure to act, or running out of turns mid-reasoning? Looks identical after the fact.
- "PR opened + question comment" — done-with-caveat or blocked? Currently undefined.
- "Run started but Baton crashed mid-run" — `after_run` never fires; who detects this?
- Clock drift between Baton timestamps and GitHub comment timestamps when computing "new comments since run start."

**Resolution:** Catalog the actual exit signatures Baton + Claude Code produce (via spike). Then design the router against real data, not imagined cases. Likely 100–150 lines of code with comprehensive logging. Treat as production code, not glue.

---

### S2.2 — The Slack-reply-to-requeue loop has an unspecified transition
**The assumption:** When you reply in Slack to an `agent-blocked` issue, the next Baton poll picks it up automatically.

**Why it matters:** This is the entire two-way interaction promise. If the issue stays labeled `blocked`, the poller's `exclude_labels: ["blocked"]` filter skips it forever.

**Specific gap:** Who removes the `blocked` label and re-applies `agent-ready` after the Slack reply? Options:
- Bot does it automatically on any reply → risk of premature requeue on partial/incidental replies
- Bot exposes a "requeue" button in addition to reply → another action, more friction
- Reply syntax determines action (e.g. `/requeue` prefix) → discoverability burden
- Human manually changes label → breaks the "respond from Slack" promise

**Resolution:** Decide the policy explicitly. Recommended: bot exposes a `Requeue with this reply` button alongside the reply box; tapping it both posts the comment and transitions labels. Updates spec §3.2 and §6 step 9.

---

### S2.3 — No failure-recovery story above the container
**The assumption:** Systemd auto-restart handles container failure.

**Why it matters:** A restarted Baton has no memory of in-flight runs. Issues labeled `agent-in-progress` at crash time sit stuck until the reconciler timeout (potentially hours).

**Specific gaps:**
- One `claude -p` segfaults — does Baton notice and reclaim the slot, or does it stay "in-progress" forever?
- Container OOMs (multiple Claude sessions, each holding context) → all in-flight work lost, no notification fires.
- Baton dies between dispatching `claude` and registering it internally → orphan process.
- Credentials file corrupted mid-run → every subsequent run fails silently.

**Resolution:** Design startup reconciliation in Baton wrapper: on container start, query GitHub for any `agent-in-progress` issues, mark them as needing requeue (e.g. comment "previous run interrupted, retrying"). Daily health-check Slack ping as a liveness signal. Validate OOM behavior during spike with deliberately undersized container memory.

---

### S2.4 — Worktree isolation is weaker than the spec acknowledges
**The assumption:** Git worktrees + container around the stack are sufficient isolation for parallel runs.

**Why it matters:** Worktrees share filesystem, global tool state (Python venvs, node_modules), database state, and the git index lock. Two parallel `npm install`s in different worktrees can corrupt each other's `node_modules`.

**Specific risks:**
- Concurrent `npm install` / `pip install` racing on shared cache
- Local services bound to fixed ports (dev DB, dev server) — only one can claim the port
- Git index lock contention on the parent repo
- Test databases / Redis instances shared by name

**Resolution:** Either accept the constraint (`max_concurrent: 1` for any repo with these dependencies), or document a project-onboarding checklist that requires per-worktree port namespacing and per-worktree dependency caches before a project enters the workflow. Add to problem-statement.md assumptions if accepted.

---

## Severity 3 — Assumptions to test during rollout

### S3.1 — "Issue comments as durable context" scales poorly
**The assumption:** The agent reads issue body + comments as full context on each run.

**Specific concerns:** Long threads bloat context. Comments lack structure (no threading at the reply level). Stale issues that sit for weeks accumulate noise. No defined "context reset" mechanism.

**Resolution:** Add to CLAUDE.md a rule like "if the issue thread exceeds N comments, summarize and request a fresh issue." Monitor during first month of use. Consider a `context-stale` label that triggers human intervention.

---

### S3.2 — Slack Bolt + Socket Mode reliability on a home server
**The assumption:** The bot's persistent WebSocket holds up over days/weeks on a home server.

**Specific concerns:** Aggressive NAT timeouts, ISP-level connection resets, server reboot/update windows. Bolt SDK auto-reconnect is best-effort; we don't know what happens to messages during reconnection gaps.

**Resolution:** Monitor disconnect frequency during first month. Add a Slack-side keepalive ping (e.g. bot posts "alive" to a #agent-status channel hourly). Document recovery procedure when WebSocket is genuinely down. Fallback: GitHub→Slack app continues providing passive activity feed even if bot is dead.

---

### S3.3 — Agent PRs may break CI for subsequent issues
**The assumption:** GitHub Actions is a per-project precondition that the pipeline doesn't own.

**Specific concern:** If an agent PR breaks CI (introduces flaky test, breaks test command, etc.) and gets merged, every subsequent issue's PR will fail CI for an unrelated reason. The outcome router can't distinguish "this issue is broken" from "the build is broken."

**Resolution:** PR review checkpoint catches this most of the time. Document: if CI fails on `main` after a merge, label any open `agent-ready` issues with `paused-build-broken` until fixed. Could be automated via a GitHub Actions workflow that watches `main` CI.

---

### S3.4 — Bot identity vs human identity in issue comments
**The assumption:** The bot writes back to GitHub as issue comments.

**Specific gap:** Comment author identity (you-via-PAT vs bot-via-app) affects how the agent interprets the comment on next run. Mixed identities in one thread are confusing.

**Resolution:** Use a dedicated GitHub App for the bot with a clear bot username. Configure CLAUDE.md to treat comments from this bot username as proxied human input (e.g. "comments from `@agent-relay` represent the project owner's instructions").

---

## Severity 4 — Acknowledged handwaves, deferred work

### S4.1 — Pinned Claude Code version is a maintenance commitment
Claude Code releases weekly. The spec pins for reproducibility, but doesn't define a version-bump cadence. **Resolution:** Monthly review of release notes; deliberate bump with smoke test of one issue before adoption.

### S4.2 — No correlation IDs across log sources
Baton logs, Claude Code transcripts, GitHub comments, and Slack messages don't share IDs. Future debugging will require manual stitching. **Resolution:** Adopt a convention: prefix all Baton log lines with `[issue-NNN]`. Mirror into Claude Code session names via the wrapper script. Slack messages already embed issue number in the card.

### S4.3 — Cross-repo work needs a different architecture
One Baton instance per repo means one container per repo. At 3+ active projects this becomes painful. **Resolution:** Out of scope for v1. Revisit if active project count exceeds 2.

### S4.4 — Observability is deferred to phase 2
Langfuse integration with Claude Code isn't turnkey. **Resolution:** Operate on basic logs for first month. If a baffling failure occurs that the basic logs can't explain, prioritise Langfuse integration then.

---

## Themes in the failures

Three patterns emerge across the items above:

1. **Empirical gap.** Severity 1 issues all share the same root: we've designed against documented behavior, not observed behavior. Baton + Claude Code rate limits, hook behavior, and exit codes need to be exercised, not assumed.

2. **Plumbing carries policy.** The outcome router (S2.1), Slack-to-GitHub round trip (S2.2), and startup reconciliation (S2.3) are treated as glue in the spec but are where most of the system's actual behavior lives. They need to be designed as first-class components, not afterthoughts.

3. **No failure-recovery story above the container.** Inside a single run, hooks and Claude Code's own guardrails handle things. Above the run — orchestrator, bot, credentials, in-flight state — there is currently "systemd restart" and not much else. This is the largest under-designed area.

---

## Process for resolution

The Severity 1 items should be resolved (via the [smoke-test-spike](./smoke-test-spike.md) and direct ToS review) before iterating on the architecture spec. Severity 2 items become design tasks once the spike validates the foundations. Severity 3 and 4 are tracked here but don't block forward progress.

After the spike completes, update the architecture spec to reflect what was learned, and either close items here or convert them into design tasks.
