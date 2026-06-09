# Pilot validation findings — baton-harness

**Status:** Final. Produced as the deliverable for issue #6 (pilot validation + end-to-end dry run).
**Companion docs:** [harness-design.md](./harness-design.md), [spike-findings.md](./spike-findings.md), [architecture-spec.md](./architecture-spec.md)
**Run driven by:** `scripts/pilot-dry-run.sh`
**Target repo:** `cbeaulieu-gt/promptsmith` (Python prompt-template-engine test bed)

---

## Purpose

This note records a live dry run of the baton-harness pilot against `cbeaulieu-gt/promptsmith`. Its goals were:

1. Confirm that an absolute `-w` path works with `baton start` (T1).
2. Run one clean issue end-to-end through the full lifecycle — `before_run`, agent execution, PR creation, `after_run` label reconciliation (Scenario A).
3. Measure the cost and terminal behavior of a blocked issue (T2).
4. Surface any integration defects not visible during the smoke-test spike.

---

## Setup

- Labels `agent-ready`, `agent-done`, and `blocked` created in the `promptsmith` repo before the run. (Finding 3 establishes that all three must pre-exist.)
- **Scenario A** used existing promptsmith issue #2 ("Project scaffolding").
- **T2** used purpose-built promptsmith issue #18 ("render: add output formatting option"), deliberately written to expose an ambiguous specification.
- Baton runs hooks via `bash -lc` (login shell, re-derived PATH). Worktrees land at `.symphony/worktrees/<issue-number>`.

---

## Results

| Test | What it validates | Outcome | Evidence |
|---|---|---|---|
| T1 — absolute `-w` path | `bin/run.sh` passes an absolute `config/WORKFLOW.md` path to `baton start -w` | **PASS** | Baton launched and polled with no path error |
| Scenario A — happy path (promptsmith #2) | Full lifecycle: `before_run` → agent → PR → `after_run` | **PASS** | PR #19 opened, body `Closes #2`; final label state: `agent-done` only |
| T2 — block cost (promptsmith #18) | Agent detects ambiguity; block is applied and respected | **PASS** (measurement) | `13:05:06 POLL Found 0 issues` after `blocked` applied; `13:05:10 RUN #18 turn 2/3` confirms block is not terminal within a run |

All three acceptance measurements passed.

### Scenario A detail

`before_run` rebased clean. The agent scaffolded the project and opened PR #19, body `Closes #2`. `after_run` classified the outcome as `pr-opened` and reconciled the issue to the single label `agent-done`, removing `agent-ready`.

Minor compliance nit: PR #19 was created non-draft despite the prompt's `gh pr create --draft` closing step. This is an F4 compliance issue (smoke-test spike finding F4 — [spike-findings.md](./spike-findings.md)) to verify separately; it does not affect the lifecycle correctness of this run.

### T2 detail

The agent correctly detected ambiguity in issue #18: it quoted the contradictory "applied by default" vs "preserved when option not used" criteria, and additionally flagged that the `render` command dependency referenced from promptsmith #12 does not yet exist. It posted a precise clarifying comment, applied `blocked`, and stopped working.

Key measurement: a block does **not** short-circuit the in-flight run. The log shows Baton excluding the issue from its next poll at `13:05:06`, then dispatching turn 2/3 at `13:05:10` — confirming that `exclude_labels: [blocked]` is checked at poll time, not between turns within an active run. This is documented as Finding 5 below.

Also confirmed: `before_run` fires once before turn 1, not once per turn. `after_run`'s blocked path classified `no-commits` → Priority 1 → removed `agent-ready`, left `blocked`. The `exclude_labels: [blocked]` config then prevented re-dispatch on subsequent polls. The blocked issue did not loop.

---

## Findings

| # | Finding | Cause | Impact | Status |
|---|---|---|---|---|
| 1 | Hooks `rc=127` (`command not found`) | `bash -lc` re-derives PATH; venv-installed `bh-*` console scripts not on it | All hooks fail on a clean run; no lifecycle execution at all | Fixed — PR #20 |
| 2 | Issue number unresolved from bare-`N` worktree | `resolve_issue_number` assumed `<prefix>-<issue>` names; Baton uses bare `<issue>` (`.symphony/worktrees/2`) | Hook cannot determine which issue it is operating on | Fixed — PR #20 |
| 3 | Target repo must have all three labels pre-created | A missing `agent-done` causes `after_run`'s `gh issue edit --add-label` to fail | `after_run` fails, leaving `agent-ready` in place and triggering Finding 4 | Open — issue #21 |
| 4 | `after_run` failure causes infinite re-dispatch loop | When `after_run` fails before removing `agent-ready`, Baton sees the issue as unclaimed on the next poll and re-dispatches it | Unbounded agent runs against the same issue; quota burn | Open — issue #21 |
| 5 | A block is not terminal within an in-flight run | Baton does not re-check `exclude_labels` between turns; `before_run` fires once, so a block applied mid-run cannot halt subsequent turns | A blocked issue costs up to `max_turns` full agent invocations, not one | **Resolved [implemented, VP-2, issue #27 P3]:** `_run_worker` turn-loop patch applied in the vendored source (~10 lines); `max_turns: 2` workaround retired. Upstream-dependency framing no longer applies. |

### Finding 1 — Hooks `rc=127`

Baton invokes hooks with `bash -lc`, which opens a login shell and re-derives PATH from the system profile. The venv-installed `bh-before-run` and `bh-after-run` console scripts are on the venv's PATH, which is not sourced by a login shell.

Fix: `bin/run.sh` exports `BH_VENV` pointing at the harness virtual environment; hooks self-activate the venv as their first step via `. "$BH_VENV/bin/activate"`. This is the only reliable approach given that Baton's hook invocation model is a fixed `bash -lc` — there is no hook-environment configuration to exploit. Merged in PR #20.

### Finding 2 — Issue number unresolved

The smoke-test spike assumed Baton named worktrees with a prefix (`<prefix>-<issue>`). Baton's actual convention is a bare issue number: `.symphony/worktrees/2`. The `resolve_issue_number` function's regex did not match this form, so it returned `None`, and every hook operation that depended on the issue number was a no-op.

Fix: the regex was made prefix-optional. Merged in PR #20.

### Finding 3 — Missing labels cause `after_run` to fail

GitHub's `gh issue edit --add-label` fails at the API call level when the label does not exist in the repository. The error is not gracefully handled — `after_run` exits non-zero, which triggers Finding 4.

Required action (issue #21): add a `bin/run.sh` preflight check that confirms all three required labels (`agent-ready`, `agent-done`, `blocked`) exist in the target repo before starting Baton. Add the label requirement to the README as a setup prerequisite.

### Finding 4 — `after_run` failure causes unbounded re-dispatch

When `after_run` exits non-zero before it can remove `agent-ready`, the issue retains the `agent-ready` label. Baton's next poll finds it and dispatches a new run. This repeats on every poll cycle with no natural stop condition — the run is effectively unbounded until Baton is killed manually.

The fix (issue #21) is twofold: (a) Finding 3's preflight eliminates the label-missing trigger; (b) `after_run` must reorder its reconciliation so that removing `agent-ready` is the first mutation, not the last — ensuring the issue is no longer dispatchable even if a subsequent step fails.

### Finding 5 — Block costs up to `max_turns`

The log sequence from T2 is the direct evidence:

```
13:05:06  POLL  Found 0 issues          ← blocked label applied; issue excluded from poll
13:05:10  RUN   #18 turn 2/3            ← turn 2 dispatched anyway; block did not halt this
```

`exclude_labels` is evaluated at the poll stage. Once Baton has dispatched a run, it does not re-evaluate `exclude_labels` between turns. `before_run` fires once at run start, before turn 1 — there is no per-turn hook point from which a block could be made terminal using the existing hook API.

**Implication for issue #4 (terminal-block design):** at pilot time, the terminal-block behavior required either an upstream change to Baton (a post-turn re-check of `exclude_labels`, or a per-turn hook), or a harness-side strategy of setting `max_turns: 1` for issue classes where blocking is likely, accepting a one-turn cost model.

> **[SUPERSEDED 2026-06-06 by option-(c) vendoring — see harness-design.md §1]** Under the vendored-symphony model, this constraint dissolves. The terminal-block fix is ~10 lines inside the vendored `_run_worker` turn loop — a harness-internal change. The "upstream-dependent" framing no longer applies; `max_turns: 2` is retireable once the vendored fix is applied. Issue #23 is closed.

---

## Spec revisions required

The following documentation and design gaps were exposed by the dry run and need action before the harness is considered pilot-complete.

**README — required target-repo labels.** The README must specify that the three labels (`agent-ready`, `agent-done`, `blocked`) must be created in the target repository before running the harness. This is a hard prerequisite: absence of any one of them produces the Finding 3/4 failure chain. This is part of issue #21's scope.

**Issue #4 — terminal-block design [resolved].** Finding 5's constraint (no per-turn hook) was resolved by vendoring symphony and patching `_run_worker` directly (VP-2, issue #27 P3). A mid-run `blocked` label now terminates the turn loop. The `max_turns: 2` workaround is retired.

**F4 compliance — draft PR verification.** Scenario A's PR #19 was created non-draft despite the closing step using `gh pr create --draft`. The prompt's REQUIRED STEPS section should be verified to confirm it still passes `--draft`, and the `after_run` classifier's `pr-opened` branch should confirm draft status before classifying as `agent-done`. This is a pre-existing gap from spike finding F4 that persisted into the pilot.

---

## Acceptance criteria (issue #6)

| Criterion | Met? | Evidence |
|---|---|---|
| T1 and T2 answered with evidence | Yes | Results table above; T2 log timestamps |
| One clean issue runs end-to-end to a PR with correct final label state | Yes | Scenario A: PR #19 (`Closes #2`), final label `agent-done` |
| Short findings note written | Yes | This document |
| Required spec revisions flagged | Yes | Spec revisions section above |
