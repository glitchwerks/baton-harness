# Smoke test spike — validate core assumptions before locking architecture

**Status:** Spike plan. Time-boxed to **2 days** of focused work. Output is a written findings memo, not production code.

**Companion docs:** [architecture-spec.md](./architecture-spec.md), [open-questions.md](./open-questions.md)

---

## Goal

Empirically validate the load-bearing assumptions in the architecture spec by running a minimal, throwaway version of the pipeline against a real GitHub repo. Answer Severity 1 questions and de-risk the riskiest Severity 2 items before committing to the full design.

## Non-goals

This spike is **explicitly not**:
- Production code. Everything is throwaway; no Dockerfile polishing, no Slack bot, no error handling beyond what's needed to observe behavior.
- Comprehensive. Many edge cases are deferred to real implementation.
- A fork of Baton. Use it as shipped.
- The final pilot repo. Use a scratch repo, ideally a tiny one.

## Definition of done

The spike is complete when there is a written findings memo (markdown, ~1–2 pages) that answers every question in §3 with either:
- A confirmed answer + evidence (logs, screenshots, test output)
- A "still unknown" with the specific blocker that prevented testing

Either outcome is acceptable. The point is to know what we know, not to make everything work.

---

## 1. Pre-spike: ToS review (Day 0, ~1 hour)

Before writing any code, read the current Claude Code subscription terms directly.

**Sources to check:**
- https://code.claude.com/docs/en/authentication
- https://www.anthropic.com/legal/consumer-terms
- Anthropic support, if anything is ambiguous

**Output:** A single paragraph in the findings memo answering: *"Is headless, orchestrator-driven use of the `claude` CLI on subscription auth compliant with Anthropic's terms as of [date of review]?"*

If the answer is "no" or "ambiguous and I'm not comfortable," **stop the spike here**. The architecture needs to revisit the executor choice before any further work.

---

## 2. Setup (Day 1, ~2 hours)

### 2.1 Environment

Run on the actual target server (or a Linux VM if not yet provisioned). No Docker for this spike — we want raw observation. Container topology is a Severity 2 concern; not blocking for these questions.

```
- Linux host (Ubuntu 22.04+ or equivalent)
- Python 3.10+
- git, gh CLI
- Claude Code installed via npm, logged in once interactively as the target user
- Baton installed: pip install -e . from a clone of mraza007/baton
```

### 2.2 Repo

Create a throwaway GitHub repo. Suggested contents:
- A `README.md` with one line about its purpose
- A single Python file with a deliberately incomplete function
- A `tests/` directory with one passing and one stubbed test
- `.github/workflows/test.yml` that runs `pytest`
- A `CLAUDE.md` with minimal repo context

### 2.3 Issues to prepare

Create four issues in the repo, each labelled appropriately for the test scenarios in §3.

| # | Title | Label | Design |
|---|---|---|---|
| 1 | "Implement add() in math.py" | `agent-ready` | Clear, unambiguous, should succeed cleanly |
| 2 | "Refactor module" (vague) | `agent-ready` | Deliberately ambiguous — agent should ask |
| 3 | "Add untestable feature" | `agent-ready` | Asks for something impossible to verify — agent should fail or block |
| 4 | "Implement subtract()" | `agent-ready` | A second clean issue, used to test concurrency |

### 2.4 Baton WORKFLOW.md

Minimal config — exercise the same shape as the production spec but without polish:

```yaml
---
tracker:
  kind: github
  labels: ["agent-ready"]
  exclude_labels: ["blocked"]
polling:
  interval_ms: 30000
agent:
  max_concurrent: 2
  max_turns: 3
  command: claude
  permission_mode: acceptEdits
hooks:
  before_run: |
    echo "[$(date)] BEFORE issue=$ISSUE_NUMBER" >> /tmp/spike.log
  after_run: |
    echo "[$(date)] AFTER issue=$ISSUE_NUMBER exit=$EXIT_CODE" >> /tmp/spike.log
    gh issue view $ISSUE_NUMBER --json labels,comments > /tmp/spike-issue-$ISSUE_NUMBER.json
---
You are working on issue #{{ issue.number }}: {{ issue.title }}

{{ issue.body }}

If the requirements are unclear, do NOT implement. Add a comment with your
specific question, add the `blocked` label, and stop. When done, commit,
push, and open a draft PR linking to #{{ issue.number }}.
```

Note the deliberately verbose hooks — we want to see exactly what `after_run` receives.

---

## 3. Test scenarios and questions to answer (Day 1–2, ~6 hours)

Each scenario maps to a Severity 1 or 2 item in open-questions.md. Run them in order; later scenarios assume earlier ones worked.

### Scenario A — Single happy-path issue (validates S1.2)

**Run:** Start Baton with only issue #1 in `agent-ready`. Wait for completion.

**Observe and record:**
- Did Baton successfully invoke `claude`? Capture the exact command line from logs.
- Did `--dangerously-skip-permissions` work? Were there permission prompts that hung?
- What was the exit code of the `claude` process?
- Did the `after_run` hook fire? With what env vars set?
- Did a PR open? Linked correctly to the issue?
- What is the final issue label state?
- How long did the end-to-end run take (from `agent-ready` set to PR opened)?

**Decision criteria:** If this doesn't work cleanly, **stop and investigate** before running anything else. This is the foundation.

---

### Scenario B — Ambiguous issue (validates Dial 1 confidence model)

**Run:** With Baton still running, label issue #2 `agent-ready`. Wait for completion.

**Observe and record:**
- Did the agent recognise ambiguity and stop, or did it guess and proceed?
- If it stopped: did it post a comment? Apply the `blocked` label?
- What does `after_run` see when this happens — exit code, file state, anything to distinguish "blocked" from "succeeded"?

**Decision criteria:** If the agent always proceeds despite the explicit confidence rule, Dial 1 (soft) isn't sufficient and PreToolUse hooks become mandatory rather than optional. Update spec accordingly.

---

### Scenario C — Failure path (validates S2.1 outcome detection)

**Run:** Label issue #3 `agent-ready`. Wait for completion.

**Observe and record:**
- What happened? PR opened with broken code? No PR? Error in logs?
- Exit code and `after_run` state.
- Is the difference between "blocked" (Scenario B) and "failed" (Scenario C) distinguishable from `after_run`'s perspective alone? If not, what GitHub API state is needed to tell them apart?

**Decision criteria:** This directly informs the outcome router design. Whatever signals are reliably distinguishable in this scenario become the basis for the router; whatever isn't distinguishable needs additional inspection logic.

---

### Scenario D — Concurrent runs (validates S1.3 rate limits and S2.4 worktree isolation)

**Run:** Label issue #4 `agent-ready` while issue #1 is *also* fresh-labelled and waiting. Both should dispatch concurrently.

**Observe and record:**
- Did Baton dispatch both, respecting `max_concurrent: 2`?
- Did both `claude` processes run in parallel without errors?
- Any rate-limit errors from Claude (429s, throttling messages)?
- Any conflicts between the two worktrees (git index lock, port collisions, file races)?
- Did both PRs open cleanly with distinct branches?
- Compare total wall-clock time to running them sequentially.

**Decision criteria:** If concurrent runs fail or hit rate limits at `max_concurrent: 2`, drop to 1 and document. If worktree conflicts appear, the spec's isolation assumption is broken — escalate to a real Docker-per-run topology.

---

### Scenario E — Stuck run (validates Baton's reconciler)

**Run:** Inject a delay manually — modify the prompt template temporarily to ask the agent to "wait 10 minutes before doing anything" on a fresh issue. Or send `SIGSTOP` to a running `claude` process. Watch what Baton does.

**Observe and record:**
- Does the reconciler eventually detect the stuck run?
- After what timeout?
- Does it kill the `claude` process, or just release the slot in bookkeeping?
- What state does the issue end up in?

**Decision criteria:** Confirms or invalidates the assumption that the reconciler is a real safety net. If it only releases bookkeeping without killing the process, document this and design startup reconciliation accordingly (S2.3).

---

### Scenario F — Mid-run kill (validates S2.3 recovery story)

**Run:** Start a run; once `claude` is actively working, `kill -9` the Baton process. Wait 30 seconds. Restart Baton.

**Observe and record:**
- What is the state of the `claude` process after Baton dies? Still running? Orphaned?
- What is the state of the issue label in GitHub?
- On Baton restart, does it pick up where it left off? Reclaim the slot? Notice the orphan?
- Is anything observably broken that would silently swallow work?

**Decision criteria:** This directly tells us how much startup reconciliation we need to build. If Baton restart is "clean enough" the design changes minimally; if it leaves orphans, we need a wrapper that reconciles on startup.

---

## 4. Output: the findings memo

After the scenarios, write a 1–2 page memo with one section per question. Format:

```
## S1.X — [Question]
**Tested:** [How]
**Observed:** [What happened, with log excerpt or screenshot]
**Conclusion:** [Resolved / Partially resolved / Still unknown]
**Implication for spec:** [What changes, if anything]
```

The memo doesn't need to be polished — it's an input to revising the architecture spec, not a deliverable to anyone else.

---

## 5. What happens after the spike

Based on memo findings, three outcomes are possible:

1. **Foundations hold.** Update architecture spec where needed (likely §3.4 outcome router and §5 isolation), and proceed to building the real pipeline. Convert remaining Severity 2 items into design tasks.

2. **Foundations are shakier than expected** (e.g. concurrency doesn't work, hooks don't fire reliably). Revise the spec to reflect actual capabilities — possibly with `max_concurrent: 1` only, or with additional wrapper code. Decide whether the limitations are acceptable.

3. **A foundation is broken** (e.g. ToS issue surfaces, Baton + Claude Code integration doesn't work at all). Stop and rethink the executor or orchestrator choice. This is exactly the outcome the spike exists to detect early.

The spike succeeds either way as long as it produces a clear answer. The risk being managed here is "discovering this in week 3 of implementation instead of day 2."

---

## 6. Time budget

| Activity | Estimate |
|---|---|
| ToS review (§1) | 1 hr |
| Environment + repo setup (§2) | 2 hrs |
| Scenarios A–C (sequential, well-behaved) | 3 hrs |
| Scenarios D–F (concurrent, failure modes) | 3 hrs |
| Findings memo (§4) | 1 hr |
| Buffer / unexpected debugging | 4 hrs |
| **Total** | **~2 working days** |

If the spike consistently exceeds this, that itself is a signal — the integration is more fragile than the spec assumes, and the design needs to absorb that reality before going further.
