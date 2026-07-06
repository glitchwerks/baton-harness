# Alternative orchestration architectures (pivot reference)

**Status**: Reference document, written 2026-07-05, before the v1 real-milestone experiment (issue #39). Not a commitment to change — a pre-analyzed fallback menu, so a pivot away from the always-on daemon is cheap to execute if the experiment shows the daemon underdelivers.

**Source**: unless otherwise cited, claims below originate from the 2026-07-05 architectural evaluation (session analysis) that produced GitHub milestone "Operational hardening" (#9, description: "Origin: 2026-07-05 architectural evaluation").

---

## 1. Locked constraints (unchanged in any pivot)

Any architecture considered below has to fit the same constraints the current daemon was built against:

- Solo dev, no team.
- ~30-minute morning window to define/approve scope, evening window for review and unblocking.
- Minimal attention available during the day.
- Subscription-only cost model (Claude Max), which requires the first-party `claude` CLI per the accepted ToS risk posture.
- Self-hosted server — no cloud-hosted agent platform.
- GitHub (issues, PRs, milestones) as the single source of truth.

Source: `docs/harness-design.md` § Constraints (Context, Infrastructure, Cost, Terms of service, Time) and § Decision records, D1 — ToS posture.

---

## 2. The throughput reframe (core reasoning)

The always-on daemon was designed on the assumption that the bottleneck is machine wall-clock during the working day. The 2026-07-05 evaluation concluded that assumption is wrong, for the following chain of reasoning:

- A well-scoped issue takes Claude Code somewhere between a few minutes and an hour of compute. An 8-hour unattended day is therefore mostly *waiting* — polling, idling between DAG steps, parked on a blocked question — not computing.
- The real throughput function is **spec quality × block-resolution latency × human review capacity**. All three are human-window variables, not machine-availability variables.
- If an agent blocks at 10am and the operator answers it during the evening review, the effective block latency is "evening" regardless of whether the run happened at 10am or overnight. The daytime Slack-escalation path (`docs/harness-design.md` § Accountability model, "Mid-run escalation") buys very little in practice: the operator is at their day job during the day, which is the entire reason the constraint exists in the first place.
- **Corollary 1**: an overnight batch — kicked off right after the evening review, with results waiting the next morning — produces nearly identical output to a full daytime unattended run, without any daytime-escalation machinery.
- **Corollary 2**: an interactive evening session, where the operator is present and answers blocks in seconds, can outperform a full unattended day precisely because block latency collapses from hours to seconds.
- The original problem statement locked in "operates during working hours" as a requirement. Re-reading it, that phrasing describes an implementation choice, not the actual requirement. The actual requirement is: **projects advance using only the morning and evening windows** — it does not require the compute to run *during* those hours, only that results are ready by the next checkpoint.

This reframe is the single biggest lever in the pivot menu below: several of the alternatives below are strictly cheaper than the daemon and, per this reasoning, lose little or nothing in practice.

---

## 3. Durable vs. replaceable components

Not everything built so far is daemon-specific. Splitting the current implementation into what would survive a pivot versus what would be discarded:

**Durable across any orchestrator (the real asset):**

- The label state machine: `agent-ready` / `agent-in-progress` / `agent-done` / `agent-merged` / `blocked` (`docs/harness-design.md` §5).
- The block-don't-guess prompt discipline (`config/WORKFLOW.md` confidence rule; validated at spike time as finding F6 and F9 in `docs/harness-design.md` § Spike findings).
- The draft-PR-only policy plus the server-side ruleset boundary — the harness never merges to `main` (`docs/harness-design.md` § Accountability model, "End point").
- The DAG-per-milestone execution model — a milestone is a dependency graph, scheduled topologically (`docs/harness-design.md` §10, "Everything is a DAG").
- The `templates/CLAUDE.md.template` context template.
- The two-checkpoint accountability model (approved start, approved end; `docs/harness-design.md` § Accountability model).

**Replaceable (daemon-specific plumbing):**

- The always-on daemon process and its poll loop.
- Heartbeat / liveness / stall detection (`docs/harness-design.md` §11).
- Crash recovery (`chain/recovery.py`; `docs/harness-design.md` §10, "Crash recovery reconstruction").
- The `daemon.alive` liveness marker.
- The systemd deployment (`bin/install-daemon-service.sh`, issue #209).
- Most of the dual-auth surface (GitHub App identity vs. PAT) that has driven the bulk of the 2026-07 incident cluster (issues #206, #210–#215, #219, #220).

By rough estimate at evaluation time: the replaceable set is roughly 70% of the harness's line count and accounts for roughly 95% of the 2026-07 incident cluster. That imbalance — most of the code and nearly all of the recent bug volume sitting in the *replaceable* half — is what makes a pivot worth pre-planning rather than something to only consider after a failure.

---

## 4. Architecture options

| Option | Shape | Keeps daemon code? | Daytime escalation? | Migration cost |
|---|---|---|---|---|
| A. Current — always-on daemon | Poll → DAG → unattended day runs | Yes (baseline) | Yes, real-time | None (already built) |
| B. Event-driven (GitHub Actions + self-hosted runner) | Label/assign → workflow fires → agent runs on owner's own server → draft PR | No — deletes daemon/poll/heartbeat/recovery | No — event-triggered, not continuously polling | Moderate — re-express DAG sequencing in workflow YAML |
| C. Scheduled batch (cron overnight ± lunch tick) | Same code, invoked with `--once` per window | Yes — same code, different invocation | No | Lowest — no code change, just an invocation change |
| D. Interactive parallel evening sessions | N worktree sessions during the review window, human as live orchestrator | No — deletes all orchestration infra | N/A — no daytime run at all | N/A (floor / benchmark, not a build) |

### A. Current — always-on daemon (baseline)

The implemented system: `bh-daemon` polls the repo registry, builds a DAG per milestone, and runs the vendored `symphony._run_worker` against each DAG-ready issue unattended through the working day (`docs/harness-design.md` §10). It fits every constraint in §1 by construction — it was designed against them. Its cost is roughly 12 KLOC of self-maintained orchestration infrastructure, with the auth/heartbeat/recovery surface owned in-repo and, per the 2026-07 incident cluster, prone to auth- and environment-identity bugs (issues #206, #210–#215, #219, #220).

### B. Event-driven — GitHub Actions + self-hosted runner + `claude-code-action`

An issue gets labeled or assigned, a GitHub Actions workflow fires, Claude Code executes on the owner's own server via a self-hosted runner (satisfying the self-hosted infrastructure constraint), and the workflow opens a draft PR. DAG sequencing across a milestone would be driven by `workflow_run` triggers or merge-triggered follow-on jobs rather than an in-process scheduler.

This option deletes the daemon, poll loop, heartbeat/liveness/stall detection, crash recovery, and the systemd unit outright, and removes most of the dual-auth surface, since GitHub Actions provides a scoped `GITHUB_TOKEN` natively rather than requiring the harness to broker App-vs-PAT identity itself.

Costs: real migration effort (rewriting the orchestration layer against Actions primitives); DAG sequencing expressed in workflow YAML is clunkier than the current `graphlib`-based scheduler (`docs/harness-design.md` §10); and before committing, the Max-subscription OAuth path for `claude-code-action` needs to be checked against the same ToS posture as decision record D1 (`docs/harness-design.md` § Decision records).

On that last point: `claude-code-action` does support Pro/Max subscription auth via `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`, using the existing subscription quota rather than per-token API billing (verified via WebSearch, 2026-07-05 — anthropics/claude-code-action docs and GitHub Marketplace listing). The community-reported caveat is that the generated OAuth token expires in roughly a day, which matters for a long-lived self-hosted-runner deployment and would need a refresh strategy. Separately, `unverified:` whether GitHub's own Copilot coding agent (which does support self-hosted runners via Actions Runner Controller, per a 2026-07-05 WebSearch of the GitHub changelog) is itself a viable substitute here — it is Copilot-branded and not a Claude Code integration, so it is noted only as evidence that the industry has converged on this event-driven-Actions shape, not as a candidate to adopt directly.

**Compliance caveat:** this sub-path is contingent on revisiting D1, not currently compliant with it. `claude-code-action` running subscription OAuth (via `claude setup-token` / `CLAUDE_CODE_OAUTH_TOKEN`) executes that OAuth token inside a third-party GitHub Action, whereas D1 currently permits subscription auth only on the first-party `claude` binary (`docs/harness-design.md` § Decision records, D1 — ToS posture). Option B cannot be treated as ToS-compliant unless and until D1 is revisited to explicitly cover third-party-Action execution of the subscription OAuth token.

### C. Scheduled batch — cron overnight ± lunch tick

The same daemon code, invoked with a `--once` flag on a schedule (e.g., right after the evening review, and optionally once at midday) instead of running continuously. This is the cheapest possible change: no new code, just a different invocation and the removal of the always-on process supervision around it. It keeps everything in §3's "durable" list plus all the existing daemon code — it just runs it in windows instead of continuously, so heartbeat/stall/liveness monitoring shrinks in relevance (a batch run either completes or doesn't; there's no multi-hour idle daemon to watch).

Per the §2 reframe, this option loses almost nothing relative to full daytime operation, because the daytime escalation path was rarely going to be answered mid-day anyway.

### D. Interactive parallel evening sessions

N parallel worktree sessions run during the evening review window, with the human acting as the live orchestrator — assigning work, answering questions in real time, and deciding what merges. This deletes all orchestration infrastructure outright. It caps total throughput at whatever one evening session can hold, and it abandons the unattended-daytime premise entirely.

Its value here is as a **floor**, not a candidate to adopt wholesale: any orchestrator this evaluation keeps must produce better throughput than a human directly running parallel evening sessions, or the orchestration layer isn't earning its complexity.

---

## 5. Pivot decision criteria (tied to the #39 experiment)

Issue #39 ("Measure subscription rate-limit behavior under representative load") is the vehicle for running one real milestone against a real downstream project. During that run, three things should be measured:

1. **(a)** How often the daytime Slack escalation was actually answered mid-day (versus answered at the evening review regardless of when it fired).
2. **(b)** Agent compute-hours actually consumed versus wall-clock hours the daemon ran.
3. **(c)** Weekly hours spent maintaining the harness itself versus hours of downstream project output produced through it.

**Prediction recorded 2026-07-05** (2026-07-05 architectural evaluation, session analysis): (a) ≈ never; (b) < 2 hours/day; (c) unfavorable — harness maintenance time will exceed downstream output time.

**Decision rule:**

- If the prediction holds → adopt **C** immediately (trivial: cron the existing `--once` path), migrate the §3 durable policy layer to **B** at leisure, and retire the daemon.
- If daytime interactivity turns out to have real, measured value (i.e., (a) is not "≈ never") → the daemon earned its keep. Continue hardening it under the existing "Operational hardening" milestone (#222–#224) rather than pivoting.

---

## 6. Standing risk noted at evaluation time

At the time of this evaluation, the harness had become the project: issue numbers were in the 220s, no downstream personal project had received a merged PR through the harness, and the daemon was still running only against a sandbox repo. The rule adopted in response: **freeze harness feature work until a real downstream project ships a merged PR through it**, and measure harness milestones in downstream PRs produced, not in harness issues closed.

---

## Cross-links

- Issue #39 — the real-milestone experiment this document's decision criteria (§5) are tied to.
- Issue #226 — tracking issue for this document.
- Milestone "Operational hardening" (#222–#224, plus #39) — where hardening continues if the daemon is kept per §5.
- Milestone "Looper-inspired enhancements" (#139–#141) — durable-authority and goal-based-termination work that applies under any architecture in §4, not just the current daemon.
- `docs/research/2026-06-21-looper-vs-baton-harness.md` — the prior-art comparison this document extends into a pivot-planning frame.
- `docs/harness-design.md` § Constraints, §10, § Decision records — the constraint and design-record source for §1, §3, and §4.B.
