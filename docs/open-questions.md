# Open questions and validation requirements

**Status:** Companion document to [architecture-spec.md](./architecture-spec.md). Captures the assumptions, handwaves, and weak points identified during adversarial review of the first draft. Each item has a resolution path.

**Purpose:** Surface implementation risks before they cost days. Items are grouped by severity. Severity 1 items must be resolved before locking the architecture; Severity 2 items must be resolved before production use; Severity 3 should be resolved during initial rollout; Severity 4 are flagged for future work.

---

## Severity 1 — Load-bearing assumptions that could collapse the design

### S1.1 — ToS compliance for headless first-party use
**RESOLVED — accepted as known risk (2026-06-07, see issue #37).**

**Background:** The entire executor decision (and rejection of OpenHands) hinges on subscription-auth headless use being compliant. If it is non-compliant, the system either violates ToS or reverts to API-key per-token billing — breaking the core cost constraint.

**Why it cannot be resolved by reading the terms:** Anthropic has revised its terms twice in 2026 and has remained deliberately vague on headless/orchestrator-driven use of the first-party `claude` binary. There is no authoritative posture to confirm against; the original resolution path ("read terms and confirm") cannot be satisfied.

**Decision:** Proceed, accepting the risk as known and bounded.

**Rationale and mitigations:**
- The first-party `claude` binary is the most-compliant available path for subscription-auth use; third-party executors (e.g. OpenHands) are less aligned with Anthropic's intended use surface.
- The existing risk table (§8) already includes this as a low-likelihood / high-impact risk with the mitigation: "First-party binary stays compliant; track terms updates; willingness to add API-key fallback if subscription path is closed."
- A fallback to API-key billing is an implementation-day switch — the executor interface is the same; only the auth credential changes.
- Terms will be re-examined at each major Anthropic revision; if explicit guidance (permissive or restrictive) appears, this item is revisited.

---

### S1.2 — Baton + Claude Code integration behavior is unverified
**RESOLVED — spike and pilot validation complete.**

The external-process integration was validated in the smoke-test spike (Scenario A — viability verdict: yes; see [harness-design.md § Spike viability verdict](./harness-design.md#spike-viability-verdict)) and the pilot dry run (T1, T2 — issue #6). The specific unknowns (`--dangerously-skip-permissions`, hook firing, exit codes) were answered empirically.

Under the vendored-symphony model [implemented, issue #27 P0], this concern dissolves: there is no Baton subprocess integration seam. Integration is `Orchestrator._run_worker(issue)` — a direct Python call to vendored source, validated via the deep-dive analysis (see `harness-design.md §1` and issue #27). No integration spike is needed for the subprocess path; the vendored interface is a clean callable.

---

### S1.3 — Subscription rate limits are unmeasured
**STILL OPEN.** Now tracked in **issue #39** (pilot-phase measurement; gates raising concurrency above 1; non-acute for serial v1). Rate limits remain unvalidated under real representative load. The spike ran only at concurrency 1 with trivial issues (see [harness-design.md § Spike viability verdict](./harness-design.md#spike-viability-verdict) — rate-limits caveat). This concern is not affected by vendoring or chain-driver design.

**Chain-driver context:** the chain driver is serial in v1 (concurrency = 1 during a chain run), which makes this less acute for v1 chain workloads but leaves general concurrency behavior unvalidated.

**Resolution path:** run a handful of representative issues (real repo, substantial codebase, `max_concurrent: 2`) and measure token consumption and throttling behavior. This is a pilot-phase measurement, not a design question.

**Original unknowns remain:**
- Throttle mechanism: concurrent streams, tokens-per-minute, daily total, or combination?
- Failure mode when exceeded: clean 429, hang, degraded response?
- Does headless `claude -p` consume the same bucket as interactive use?

---

## Severity 2 — Real problems with known shapes but no defined solutions

### S2.1 — Outcome router is hand-waved and load-bearing
**RESOLVED — `after_run.py` implemented and tested.**

`after_run.py` is implemented as a full Python module in the `baton_harness` package, with pytest coverage and ruff/mypy compliance (issue #3, merged). It correctly classifies the four outcome states identified by the spike (F5: `uncommitted-changes`, `no-commits`, `committed-no-pr`, `pr-opened`) and reconciles GitHub labels to a single state. The "Dial 2" router that was described as hand-waved is now production code, not glue.

Remaining known gap: `after_run` classifies `pr-opened` as `agent-done` without checking CI status (F10). Draft-PR compliance (pilot finding, Scenario A nit) is a separate pre-existing gap tracked in issue #21. Neither gap is a structural concern about the router's design.

---

### S2.2 — The Slack-reply-to-requeue loop has an unspecified transition
**RESOLVED (v1) — see spec §9.**

The merged #27 spec resolves the transition. On a parked/blocked issue, the **human** posts guidance directly on the GitHub issue, removes the `blocked` label, and **re-adds `agent-ready`** — this is the explicit re-dispatch trigger the daemon's outer poll loop detects (spec §5 / §9, step 3). The GitHub issue is the durable record; Slack is the channel only. The bot-automated "Requeue with this reply" button (the option this item floated) is **deferred to the Slack/comms layer (v2)**, not needed for v1's manual flow.

---

### S2.3 — No failure-recovery story above the container
**RESOLVED (v1) — #40 merged.**

**In-flight / stuck-state recovery: RESOLVED by design.** The merged spec's `recovery.py` reconstruction (§11.5) re-derives the scheduler `done`/`parked`/frontier state from git provenance and labels on each daemon start. The `agent-in-progress` orphan rule (§8 / §11.5 rule 3b) handles issues stuck in-progress at crash time: the daemon re-evaluates them and re-dispatches rather than treating them as done. `after_run` crash-safety is tracked in **#31**; liveness in **#33**; failure notification in **#34**.

**Operational hardening: resolved by #40** — startup reconciliation sweep (`src/baton_harness/chain/reconcile.py`) wired into `run_daemon` at `daemon.py:1497`, before the poll loop.

**Specific gaps — resolution status:**
- **Orphan `claude` process** → **resolved (#40).** Startup `pgrep -f 'claude -p'` detect-only sweep (`reconcile.py:161-177`): if stray processes are found at boot, `alert(severity="warn")` fires with the PID list. Auto-reclaim is deferred behind a future `BH_ORPHAN_PROC_GC` flag, mirroring the `BH_WORKTREE_GC` shape from #33.
- **Container OOM / ungraceful exit** → **resolved (#40).** A `.baton-harness/daemon.alive` marker is written at startup and cleared on graceful shutdown. If the marker is present on the next boot, the prior run ended ungracefully (likely OOM-kill or hard crash) — `alert(severity="critical")` fires (`reconcile.py:138-154`). SIGTERM now also clears the marker before exit (`daemon.py:1500-1520`), so a graceful `docker stop` does not leave the marker in place and false-alarm on next boot. The harness cannot self-report an uncatchable SIGKILL; next-boot detection is the tractable notification.
- **Credential corruption / missing credentials** → **resolved (#40, #108).** Two startup gates run before the poll loop (`reconcile.py:105-135`): (1) `validate_github_token()` — fatal if the GitHub token is missing or invalid (`reconcile.py:105-116`); (2) `ANTHROPIC_API_KEY` guard — **fatal if the key IS set** (`reconcile.py:119-135`). The architecture mandates OAuth via a mounted credentials volume (`architecture-spec.md` L318); a non-empty `ANTHROPIC_API_KEY` means per-token billing is active, which must be refused at startup. Either failure emits `alert(severity="critical")` then calls `sys.exit(1)`. The OAuth credential-volume health-check (G3c gate — presence/readability of the mounted `~/.claude/.credentials.json` via `open()`) is **resolved by #108** (`reconcile.py` — `_OAUTH_CRED_PATH` seam, structural-only check, fatal on absent or unreadable).
- **`state.json` load-on-startup (retry-queue continuity)** → **deferred to issue #106.** `state.py` has `persist()` but no `load()`; the daemon starts with a fresh retry queue on each restart. In-flight issues are recoverable from labels + git provenance (`recovery.py`), so this is a state-continuity gap rather than a failure-mode gap. Tracked separately from #40.

---

### S2.4 — Worktree isolation is weaker than the spec acknowledges
**RESOLVED (v1) — see spec §6.**

The merged spec **serializes all work units in v1** — one DAG in flight repo-wide, one issue dispatched at a time (effective `max_concurrent = 1`) — which is exactly the "accept the constraint" resolution this item proposed. The isolation risks below are real but only bite under concurrent dispatch, which v1 forbids by design. Per-worktree port-namespacing and dependency-cache isolation are deferred to the **v2 cross-work-unit concurrency** work (spec §6 / §14).

**Specific risks (v2 prerequisites before enabling concurrency):**
- Concurrent `npm install` / `pip install` racing on shared cache
- Local services bound to fixed ports (dev DB, dev server) — only one can claim the port
- Git index lock contention on the parent repo
- Test databases / Redis instances shared by name

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

Three patterns emerged across the items above:

1. **Empirical gap.** S1.3 (rate limits) remains the one open Severity 1 item; it requires representative load measurement, not design work. All others were resolved — either empirically (spike/pilot) or by implementation.

2. **Plumbing carries policy.** The outcome router (S2.1) and startup reconciliation (S2.3) turned out to be first-class components; both are implemented. S2.2 (Slack-to-GitHub round trip) is resolved for v1: the human drives the label transition directly on the GitHub issue.

3. **Failure recovery above the container.** Stuck-state recovery is resolved by design (`recovery.py`, issue #27 §11.5); operational hardening (OOM, orphan processes, credential gates) resolved by #40 (`reconcile.py` startup sweep); OAuth credential-volume health-check (G3c gate) resolved by #108. One explicit residual remains: `state.json` load-on-startup for retry-queue continuity (issue #106).

---

## Process for resolution

**As of 2026-06-09 (post-#27 merge):** S1.1 (ToS) accepted risk (issue #37). S1.2 (Baton integration) dissolved by vendoring (issue #27 P0). S1.3 (rate limits) remains open — tracked in issue #39; measure before raising concurrency above 1. S2.1 (outcome router) implemented. S2.2 (Slack-to-requeue) resolved for v1 (human drives label transition — spec §9). S2.3 (failure recovery): stuck-state recovery implemented (`recovery.py`, issue #27 §11.5); operational hardening (OOM/orphan processes/credential gates) resolved by #40 (`reconcile.py` startup sweep); OAuth credential-volume health-check (G3c gate) resolved by #108; one residual deferred — retry-queue continuity (`state.json` load-on-startup, issue #106). S2.4 (worktree isolation) resolved for v1 by serialization. Severity 3 and 4 remain valid monitors for rollout.
