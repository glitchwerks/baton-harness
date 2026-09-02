---
title: "#351 — bounded-retry park-path label restoration + terminal `agent-failed` escalation"
touches:
  - src/baton_harness/chain/daemon/work_unit.py
  - src/baton_harness/chain/daemon/gh_api_helpers.py
  - src/baton_harness/chain/daemon/park.py
  - src/baton_harness/chain/daemon/__init__.py
  - src/baton_harness/chain/daemon/poll.py
  - src/baton_harness/chain/failure_tally.py
  - src/baton_harness/chain/labels.py
  - src/baton_harness/chain/obs_config.py
  - src/baton_harness/chain/doctor.py
  - src/baton_harness/redact.py
  - src/baton_harness/vendor/symphony/hooks.py
  - src/baton_harness/vendor/symphony/orchestrator.py
  - bin/init-sandbox.sh
  - docs/harness-design.md
  - README.md
  - tests/chain/test_labels.py
  - tests/chain/test_daemon_park_label_clear.py
  - tests/chain/test_daemon.py
  - tests/chain/test_doctor.py
  - tests/chain/test_failure_tally.py
  - tests/chain/test_park.py
  - tests/test_redact.py
  - tests/vendor/test_orchestrator_hook_env.py
  # Read-only for this plan (no edits proposed) but load-bearing for the
  # findings in § 3 — the reviewer must be able to check the claims:
  - src/baton_harness/chain/recovery.py
  - src/baton_harness/chain/redispatch.py
  - src/baton_harness/chain/session_report.py
  - src/baton_harness/chain/escalation.py
  - src/baton_harness/chain/daemon/launch_gate.py
  - src/baton_harness/after_run.py
  - bin/install-daemon-service.sh
  - config/WORKFLOW.md
skills_relevant:
  - python
  - simplicity-first
---

# #351 — bounded-retry park-path label restoration + terminal `agent-failed`

**Issue:** [#351](https://github.com/glitchwerks/baton-harness/issues/351) (label
`bug`, open, filed 2026-08-08). **Baseline:** `main` @ `322b79e`.

**Decided before this plan and not re-litigated** (user call, recorded in the
dispatch brief): retry with `agent-ready` restoration on the first N failures,
then escalate to a new terminal `agent-failed` label requiring human triage once
N is exhausted. The (a)-only and (b)-only options in the issue body are closed.

This plan scopes that shape. It also records **five findings from reading `main`
that materially narrow and re-shape the change** relative to the issue body and
the dispatch brief — read § 3 before executing anything.

---

## 1. Scope

**In scope**

- A shared park decision point (`src/baton_harness/chain/daemon/park.py`) that
  every non-preflight park exit **reachable from `_run_work_unit`** routes
  through — the fourteen inline in `work_unit.py` plus the two inside
  `_run_ci_gate` (`gh_api_helpers.py`) — with an explicit per-site park
  classification.
- A durable per-issue consecutive-failure counter
  (`src/baton_harness/chain/failure_tally.py`) with count-and-reset semantics.
- A new `agent-failed` label: its taxonomy position, its provisioning
  (`bin/init-sandbox.sh`), and its doctor check.
- Threading real hook diagnostics (`returncode` + redacted `stderr` tail) from
  `run_hook` through `orchestrator.py`'s `RuntimeError` into the escalation
  comment and the session report.
- A **new** secret-redaction helper, `src/baton_harness/redact.py` — a hard
  prerequisite for the previous bullet (finding F5).
- `docs/harness-design.md` § 5 and README label-taxonomy updates.

**Out of scope (explicit non-goals)**

- **Why `before_run` fails deterministically for `cbeaulieu-gt/baton-test#2`.**
  The issue says so explicitly ("Not investigated ... requires the daemon host's
  own logs"). This plan makes that failure *legible and bounded*; it does not
  diagnose it. File separately once F4's diagnostics land and produce a real
  stderr tail.
- **CI-gate failures as a retry budget consumer.** Three sites share this
  domain — `work_unit.py:643-677` and the two inside `_run_ci_gate`
  (`gh_api_helpers.py:381-410`, `:427-456`). All are CI-red conditions, not
  agent failures. They are routed through the shared helper for uniformity but
  charge nothing. See open question Q3.
- Changing `RedispatchTally`'s own semantics (finding F1) or its
  `BH_REDISPATCH_*` defaults.
- `config/WORKFLOW.md`'s `tracker.exclude_labels` — see decision D3 note.
- Any change to `after_run.py`'s `_reconcile_labels`. Finding F2 shows it is
  already correct for the paths this plan touches.

---

## 2. What already exists (verified against `main` @ `322b79e`)

| Capability | State | Evidence |
| --- | --- | --- |
| Three mutually-exclusive state labels + pure invariant checker | Built | `src/baton_harness/chain/labels.py:21-36` (`STATE_LABELS`), `:44-85` (`assert_single_state`) |
| Pure observed-fact reconciler | Built, 3-outcome only | `src/baton_harness/chain/labels.py:93-128` (`target_state_from_observed`) |
| Durable, file-backed, **per-issue** counter surviving daemon restarts | **Built** (for redispatch) | `src/baton_harness/chain/redispatch.py:101-269`; wired at `src/baton_harness/chain/daemon/poll.py:258-267`; threaded into `_run_work_unit` at `src/baton_harness/chain/daemon/work_unit.py:278` |
| Env-var-configurable observability knobs incl. a counter file path | Built | `src/baton_harness/chain/obs_config.py:30-49` (docs), `:187-215` (parse), `:236-239` (path) |
| Correct label restoration on a failure path | Built — **exactly one site** | `src/baton_harness/chain/daemon/launch_gate.py:474-508` (preflight refusal: `add=["agent-ready"], remove=["agent-in-progress"]`) |
| Escalation comment on failure (not silent) | Built | `src/baton_harness/chain/escalation.py:134-192`; called at `work_unit.py:959-968` |
| Durable session-report escalation + park outcome | Built | `src/baton_harness/chain/session_report.py:316-366` |
| Pre-dispatch exclude-label gate shared by three call sites | Built | `src/baton_harness/chain/daemon/__init__.py:321-333` (`_DISPATCH_EXCLUDE_LABELS`); consumed at `poll.py:575`, `:605` |
| `agent-failed` label | **Does not exist** | Repo-wide grep, 2026-08-08 — zero hits in `src/`, `bin/`, `docs/`, `tests/` |
| Secret redaction helper | **Does not exist** | Repo-wide grep for `redact` in `src/**/*.py`, 2026-08-08 — only hit is a shell-side literal at `bin/install-daemon-service.sh:331` |
| Hook `returncode`/`stderr` reaching any caller | **Does not exist** | `src/baton_harness/vendor/symphony/hooks.py:19` returns `bool`; the detail is logged and discarded at `:51-56` |

---

## 3. Findings that change the plan (read before executing)

### F1 — the brief's "no persistent per-issue counter exists" premise is false (corrects the brief)

The dispatch brief states that a failure count surviving across daemon
invocations "needs a new storage mechanism". It does not. `RedispatchTally`
(`src/baton_harness/chain/redispatch.py:101-269`) is already exactly that: a
per-issue, file-backed, atomically-persisted counter that "survives daemon
restarts -- enabling detection of crash-restart loops that an in-process counter
would miss" (`redispatch.py:104-107`). It is constructed at
`poll.py:258-264` from `obs.redispatch_counts_path` (default
`${BH_PROJECT_ROOT}/.baton-harness/dispatch-counts.json`,
`obs_config.py:46-49`) and is already a parameter of `_run_work_unit`
(`work_unit.py:278`).

The brief's third premise — that the deployment is cron-driven `--once` — is
also false. Production is a **long-lived systemd service**:
`bin/install-daemon-service.sh:346` renders `ExecStart=${BH_DAEMON_BIN}
--workflow ${WORKFLOW_FILE}` with **no** `--once`, plus `Restart=on-failure`
(`:347`). `--once` is documented as "Run one tick then exit (useful for smoke
tests)" (`bin/run-daemon.sh:11`, `:37`).

Both corrections point the same way (durability is still required — for
`Restart=on-failure` restarts rather than cron re-invocation) but they change
the *shape* of the counter. See D1.

### F2 — the issue over-counts the broken sites; the post-run invariant check already protects half of them

The issue asserts that "every other non-preflight park path" leaves zero state
labels. That is not true, and the difference matters because it halves the
change surface.

The label transition that removes `agent-ready` and adds `agent-in-progress`
happens at `work_unit.py:819-828`. A park exit leaves zero state labels only if
it is reached **after** that transition **and** nothing re-added a state label.
Two mechanisms already re-add one:

1. **`after_run.py` re-establishes a single state label on most worker
   outcomes.** On `PR_OPENED` it adds `agent-done` (`after_run.py:383-384`,
   `:491-506`); on `NO_PR` / `COMMITTED_NO_PR` it adds `blocked`
   (`after_run.py:387-390`); on `TRANSIENT_ERROR` it leaves `agent-ready`
   intact (`after_run.py:23`, `:150`, `:405-411`).
2. **The single-state invariant backstop at `work_unit.py:1093-1263` gates
   everything downstream of it.** Any park exit *after* line 1093 has already
   passed `assert_single_state`, so exactly one state label is present by
   construction.

Applying both, the fourteen park exits in `_run_work_unit` classify as:

| # | Site | Lines | Zero-state today? | Evidence |
| --- | --- | --- | --- | --- |
| 1 | recovery `parked_seed` | `496-507` | **No** — `blocked` present | `recovery.py:361-364` rule 3 |
| 2 | `ci_gate_reentry`, no open PR | `517-555` | **Possibly** — recovery rule 2 guarantees no state label | `recovery.py:356-359` |
| 3 | `ci_gate_reentry`, merge exception | `588-628` | **Possibly** — same entry conditions | `recovery.py:356-369` |
| 4 | `ci_gate_reentry`, outcome ≠ `MERGED` | `643-677` | **Possibly** — same | `recovery.py:356-369` |
| 5 | redispatch threshold breach | `680-744` | **Yes** — rule 5 orphan may have lost `agent-ready` mid-run | `recovery.py:371-374` |
| 6 | pre-dispatch label-fetch failure | `766-812` | **No** — fires *before* the `:819-828` transition | `work_unit.py:766`, `:819` |
| 7 | issue-fetch failure | `848-886` | **Yes** — after the transition | `work_unit.py:819-828`, `:848` |
| 8 | preflight `app_id`/`obs` missing | `905-929` | **Yes** — after the transition; also posts **no alert at all** | `work_unit.py:905-929` |
| 9 | worker exception | `930-977` | **Yes — the confirmed reported bug** | `work_unit.py:942-977` |
| 10 | preflight refused | `979-990` | **No — already correct** | `launch_gate.py:474-485` |
| 11 | post-run label-fetch failure | `1014-1085` | **Unknown** — labels unreadable by definition | `work_unit.py:1025-1030` |
| 12 | label-invariant violation | `1093-1263` | **Yes** when zero-state and no open PR | `work_unit.py:1104-1105`, `:1249-1256` |
| 13 | `pr_created` but no PR located | `1275-1314` | **No** — downstream of the `:1093` invariant gate | `work_unit.py:1093-1094` |
| 14 | final park (`blocked` / `no_pr`) | `1347-1386` | **No** — same gate | `work_unit.py:1093-1094` |

**Plus two park exits *outside* `_run_work_unit`.** `_run_ci_gate`
(`src/baton_harness/chain/daemon/gh_api_helpers.py:296-457`) receives `sched`,
`parked_reasons`, `liveness_state`, and `merged_issues`
(`gh_api_helpers.py:305-309`) and parks internally. It is on the **highest-traffic
route** — every `pr_created` outcome reaches it (`work_unit.py:1316-1334`), as
does the convergence path (`work_unit.py:1173-1191`). Any site table scoped to
`work_unit.py` alone would declare #351 fixed while leaving the busiest path
unexamined.

| # | Site | Lines | Zero-state today? | Evidence |
| --- | --- | --- | --- | --- |
| 15 | `_run_ci_gate`, `merge_issue_branch` exception | `gh_api_helpers.py:381-410` | **No** | both callers are downstream of a state-label guarantee — see below |
| 16 | `_run_ci_gate`, outcome ≠ `MERGED` | `gh_api_helpers.py:427-456` | **No** | same |

Both are safe today for the same reason sites 13/14 are: the `pr_created` caller
is downstream of the `work_unit.py:1093` invariant gate, and the convergence
caller has just written `_target` (= `agent-done`) at `work_unit.py:1131-1139`.
Neither park exit removes that label — `:391` and `:432` remove only
`agent-in-progress`. So both are `STATE_INTACT`, and — exactly like sites 1 and
14a — a naive "restore `agent-ready` everywhere" refactor would **regress them
into two-state violations on the success path**. Enumerating them is therefore
not bookkeeping; it is what stops the fix from breaking the common case.

Note also that `gh_api_helpers.py:381-410` is a near-duplicate of `work_unit.py`
site 3 (`:588-628`) — same exception, same park shape. Routing both through
`park_issue` collapses the duplication.

Counting all sixteen: five are definitely broken (5, 7, 8, 9, 12), three are
conditionally broken (2, 3, 4), one is unknown-by-construction (11), and seven
are already correct (1, 6, 10, 13, 14, 15, 16).

*Adjacent observation, deliberately out of scope:* `_run_ci_gate`'s **MERGED**
exit removes both `agent-in-progress` and `agent-done`
(`gh_api_helpers.py:414-421`) and adds only `agent-merged`, which is a marker
rather than a member of `STATE_LABELS` (`labels.py:34-36`). A merged-but-open
issue therefore carries zero state labels. That is pre-existing behaviour on the
success path, not a regression this plan introduces, and the issue is normally
closed by the PR's `Closes #N` shortly after. Flagged here only because this plan
edits `STATE_LABELS` — do **not** "fix" it as a drive-by; file it separately if
it matters.

**Consequence for the design:** a shared helper that unconditionally restores
`agent-ready` would *regress* sites 1, 13, and 14 (each already carries a valid
state label; adding `agent-ready` produces a two-state invariant violation), and
would *compound* the tear at sites 11 and 12 (label state is unreadable or
already torn). The helper needs a **declared park classification per call
site**, not a single behaviour. See D4.

### F3 — the session report cannot carry the counter

`SessionReport` is constructed fresh per daemon session (`poll.py:250-255`) and
`write()` overwrites the destination via `os.replace`
(`session_report.py:481-494`). It holds no cross-session history. The dispatch
brief flagged this as unverified; it is now verified. The brief's second storage
option is therefore closed.

(Also stale in a sibling plan: `record_skipped_blocked` **does** now exist —
`session_report.py:226-246`, added by #343/#344, commit `322b79e`. The claim at
`docs/superpowers/plans/2026-08-05-phase-5a-ci-scenario-smoke-307.md:146-165`
that `skipped_blocked` "appears nowhere else in `src/`" no longer holds.)

### F4 — `run_hook` returns `bool`; four call sites must change together

`run_hook` (`src/baton_harness/vendor/symphony/hooks.py:12-67`) captures
`proc.returncode` and `stderr` (`:47-49`), logs a 500-char tail (`:51-56`), and
returns a bare `bool`. `orchestrator.py` has **four** call sites, and two of them
make "just raise from inside `run_hook`" invalid:

| Line | Hook | Uses return value? |
| --- | --- | --- |
| `orchestrator.py:191-198` | `after_create` | Yes — `raise RuntimeError("after_create hook failed")` |
| `orchestrator.py:201-209` | `before_run` | Yes — `raise RuntimeError("before_run hook failed")` |
| `orchestrator.py:274-279` | `after_run` (failure path) | **No — best-effort, deliberately ignored** |
| `orchestrator.py:392-397` | `after_run` (success path) | **No — best-effort** |

Tests bind to the current contract: `tests/vendor/test_orchestrator_hook_env.py`.

### F5 — BLOCKING: hook stderr in a GitHub comment is a credential-exfiltration surface

`work_unit.py:399-403` sets `orch.hook_env = {"GH_TOKEN": worker_gh_pat,
"GITHUB_TOKEN": worker_gh_pat}`, and `hooks.py:29` merges that into the hook
subprocess environment. A failing `git`/`gh` invocation inside a hook can echo a
token-bearing remote URL or an auth error into stderr. `escalate()` posts its
`summary` verbatim as a GitHub issue comment (`escalation.py:160-172`) — `alert()`
(roughly `escalation.py:211-296`) is a separate, later severity-routing wrapper
that calls `escalate()` for `warn`/`critical` severities, one call-frame up from
the verbatim post — and
`glitchwerks/baton-harness`'s sandbox repos are public
(`docs/superpowers/plans/2026-08-05-phase-5a-ci-scenario-smoke-307.md:244-247`,
`:271`).

There is **no redaction helper anywhere in `src/`** (verified by repo-wide grep
on 2026-08-08). `hooks.py:53-55`'s 500-char truncation is not redaction, and it
currently writes raw stderr to the systemd journal
(`install-daemon-service.sh:349-350`).

Per `CLAUDE.md § Credentials and Secrets`, redaction is a hard prerequisite of
task item 5, not a follow-up. See D5.

---

## 4. Decisions

### D1 — counter storage: a new `failure_tally.py`, not a reuse of `RedispatchTally` (RECOMMENDED)

**Decision:** add `src/baton_harness/chain/failure_tally.py` — a sibling of
`redispatch.py` that copies its proven mechanics (tolerant `_load` that never
raises, atomic best-effort `_persist` via temp-sibling + `os.replace`,
`obs_config`-supplied path and threshold) but exposes **count-and-reset**
semantics instead of a sliding tick window:

```
class FailureTally:
    def record_and_check(self, issue: int) -> tuple[int, bool]: ...  # (new_count, exhausted)
    def reset(self, issue: int) -> None: ...                          # delete the key
    def peek(self, issue: int) -> int: ...
    def set_alerted(self, issue: int) -> None: ...                    # mark alert-once flag durable
    def has_alerted(self, issue: int) -> bool: ...                    # read the flag
```

Backing file: `${BH_PROJECT_ROOT}/.baton-harness/failure-counts.json`
(`BH_FAILURE_COUNTS_PATH` override), schema
`{"issues": {"<n>": <int>}, "alerted": ["<n>", ...]}` — a flat list of
issue-number strings that have already received the T4b
`{agent-ready, agent-failed}` alert (Q5, decided option (b)). `set_alerted`
adds the issue's string key to `alerted` (idempotent); `has_alerted` checks
membership.

**Why not reuse `RedispatchTally`:** its unit of account is a tick mark inside
a sliding window, pruned on every write (`redispatch.py:176-188`). The lifecycle
this plan must express — reset to zero on a successful merge, and no decay — is
not representable in it without changing its meaning for the shipped #77
loop-detector. Worse, a tick under the real deployment is one poll cycle:
`polling.interval_ms: 30000` (`config/WORKFLOW.md:7`) against a long-lived
systemd process (F1). A window of any operationally meaningful size is therefore
either seconds (useless) or thousands of ticks (indistinguishable from a plain
count). A plain count is both simpler and more correct here.

**Alternative deliberately named and rejected:** generalise `RedispatchTally`
into a namespaced multi-counter. Rejected under `simplicity-first` — it couples
a shipped loop-detector to new lifecycle rules for no reduction in total code,
and the two counters have genuinely different semantics.

**Interaction with the redispatch counter (the brief did not specify this).**
The two counters stay **independent**. A redispatch breach
(`work_unit.py:680-744`) does **not** consume a failure credit — it is already
its own exhausted-budget escalation. What changes is that it now writes the same
**terminal label** as failure-budget exhaustion, so operators see one terminal
state rather than two (see D3, site 5 in the D4 table).

### D2 — retry budget: `N = 2`, configurable via `BH_MAX_ISSUE_FAILURES` (RECOMMENDED)

**Semantics (state precisely; the redispatch counter's off-by-one is a known
footgun — `redispatch.py:65-72`):** `failure_count` is the number of
*consecutive charged failures* recorded for the issue. On each charged park the
counter is incremented, then:

- `new_count < N` → restore `agent-ready` (auto-retry on the next poll tick)
- `new_count >= N` → write `agent-failed` (terminal)

So `N = 2` means: first charged failure retries, second charged failure is
terminal. Total agent runs consumed per issue before human triage: **2**.

**Why 2, not 3:** the issue's own evidence is that the failure was deterministic
and reproduced identically across two runs, including after a human reset
(issue #351 body, "Design question" § option (b)). One retry is enough to
absorb a transient blip; a second is pure cost. Note the deliberate contrast
with `_DEFAULT_REDISPATCH_MAX = 3` (`obs_config.py:34-36`) — that counter meters
crash-restart orphan re-dispatches, which are frequently transient and do not
each cost a full agent run. Document the contrast in the env-var table so nobody
"harmonises" them later.

**Only charged parks increment** (D4). Transient GitHub API failures
(sites 3, 7, 8) must not burn a 2-run budget.

### D3 — `agent-failed` is a **fourth mutually-exclusive state label**, and is added to `_DISPATCH_EXCLUDE_LABELS` (RECOMMENDED)

Three shapes were considered.

| Shape | Verdict |
| --- | --- |
| **A. 4th member of `STATE_LABELS`** | **Recommended** |
| B. `blocked` (state) + `agent-failed` (marker), like `agent-merged` | Rejected |
| C. Standalone marker, no state label at all | Rejected |

**Why not B.** It is genuinely the lowest-surface option — it reuses
`_DISPATCH_EXCLUDE_LABELS` (`daemon/__init__.py:333`), the tracker's
`exclude_labels` (`config/WORKFLOW.md:5`), and `recovery.py:361-364` rule 3 with
zero invariant change. But it conflates two operationally distinct states.
`blocked` means "the agent asked a question and needs a human answer" — that is
what the whole confidence/block rule in `config/WORKFLOW.md:36-43` produces, and
what `alert(kind="block")` signals. `agent-failed` means "the harness could not
execute this issue N times". Operators triage those differently, and
`label:blocked` searches would mix them permanently. The existing marker
precedent (`agent-merged`) is *load-bearing* in `recovery.py:352` rule 1; a
marker nothing reads is just a tag.

**Why not C.** Without a state label, the issue is left in the exact
zero-state condition #351 exists to eliminate.

**What A requires** (each item is a real, verified reconciliation cost — this is
the honest bill for the recommendation):

1. `labels.py:21-36` — add `LABEL_AGENT_FAILED = "agent-failed"` and extend
   `STATE_LABELS` to four members. Update the module docstring's "three
   mutually-exclusive" wording (`labels.py:3-5`).
2. `work_unit.py:1101-1103` **hardcodes a duplicate of the three-label set**
   (`post_labels & set(["agent-ready", "agent-done", "blocked"])`) rather than
   importing `STATE_LABELS`. It will silently diverge. Replace it with
   `STATE_LABELS` as part of this change — this is a latent bug worth killing
   regardless of the outcome of this decision.
3. `target_state_from_observed` (`labels.py:93-128`) can never return
   `agent-failed`, and its docstring promises "always a member of
   `STATE_LABELS`" (`:111`) — still true, but the convergence backstop at
   `work_unit.py:1105-1204` must not "converge" an `agent-failed` issue to
   `agent-done` just because a PR happens to exist. Guard the convergence branch
   on `LABEL_AGENT_FAILED not in post_labels`.
4. `poll.py:624-637`'s `len(live_labels & STATE_LABELS) >= 2` alert changes
   meaning (it subtracts `LABEL_BLOCKED` at `:625` to compute `extra`). Verify
   the message still reads correctly when the pair is
   `{agent-ready, agent-failed}`.
5. **`agent-failed` joins `_DISPATCH_EXCLUDE_LABELS`**
   (`daemon/__init__.py:333`), **and the snapshot filter gains an alert.** The
   exclude entry closes the human-triage trap: the primary poll query filters
   only on `--label agent-ready` (`poll.py:532-547`), so without it an issue
   re-labelled `agent-ready` while still carrying `agent-failed` would be
   **dispatched and burn a full agent run** before the post-run invariant caught
   it.

   **The exclude entry alone is not sufficient, and an earlier draft of this
   plan got this wrong.** The snapshot filter at `poll.py:572-583` sends an
   excluded issue down the `elif` branch, which calls **only**
   `record_skipped_blocked` — no alert. That issue is never appended to
   `ready_issues`, so it never reaches the live re-check at `:594-644` and the
   `>=2` operator alert at `:624-637` **never fires for it**. Left as-is, an
   `{agent-ready, agent-failed}` issue would be silently skipped on every tick,
   forever — a fresh instance of the invisibility failure #351 exists to
   eliminate, introduced by the mitigation itself.

   So the exclude entry must be paired with a new alert in the snapshot-filter
   `elif`: when the excluded label set contains `agent-failed` **and**
   `agent-ready` is present, emit a `critical` alert telling the operator to
   remove `agent-failed` to re-enable the issue. Rate-limiting is a real concern
   (the condition persists across ticks) — see task T4b and open question Q5.

   *Accepted side effect:* the skip is recorded via `record_skipped_blocked`
   (`session_report.py:226-246`), so the report outcome string reads
   `"skipped_blocked"` for an `agent-failed` skip. Slightly misleading name,
   accurate semantics; not worth a schema change.

**`config/WORKFLOW.md`'s `tracker.exclude_labels` is deliberately left
unchanged.** The vendored VP-2 mid-turn re-check consumes it, and `agent-failed`
is written only *after* a run ends, so an entry there would be inert. Adding
dead config is worse than omitting it. Record this so a reviewer does not read
the omission as an oversight.

### D4 — one shared park decision point with a **declared** classification per call site

Create `src/baton_harness/chain/daemon/park.py` exporting `park_issue(...)`,
re-exported from `daemon/__init__.py` and reached from `work_unit.py` as
`_daemon_mod.park_issue(...)` — the "patch where it's looked up" convention this
package already relies on (`work_unit.py:31-48`, established by
`docs/superpowers/plans/2026-07-20-module-refactor-proposal-268.md`).

To keep the signature honest against `_run_work_unit`'s many threaded locals,
build a frozen `ParkContext` once before the Step-2 loop (owner, repo,
`installation_token`, `report`, `runlog`, `sched`, `liveness_state`,
`parked_reasons`, `failure_tally`) and pass per-call: issue number,
`ParkClass`, `reason`, `detail`, `severity`, `kind`.

```python
class ParkClass(Enum):
    CHARGED       # a worker run was consumed and failed -> increment; restore or terminalise
    UNCHARGED     # infra/transient failure, no worker run consumed -> restore, do not increment
    STATE_INTACT  # a valid state label is already present -> do not write one, do not increment
    UNKNOWN_STATE # labels unreadable or already torn -> write nothing, do not increment
    TERMINAL      # budget already exhausted by another mechanism -> write agent-failed directly
```

**`CHARGED` handling must clear the full label set, not just
`agent-in-progress`.** A `CHARGED` site is not guaranteed to be zero-state when
it fires — it can be reached after another mechanism (`after_run.py`, the
convergence backstop) has already re-added a state label (F2). If `park_issue`
only ever removes `agent-in-progress` before writing the target label
(`agent-ready` or `agent-failed`), a `CHARGED` site that already carries e.g.
`agent-done` ends up with two state labels post-park — a violation the plan's
own post-condition assertion (D4) exists to catch, not one it should ever
trigger by construction. So on `CHARGED`, `park_issue` must first remove
**every member of `STATE_LABELS` currently present** on the issue, then add
exactly the target label. This is what makes site 13 (below) safe.

**Per-site assignment** (derived from F2's table; the classification is what the
reviewer checks, not merely "does the site call the helper"):

| # | Site | Lines | Class | Note |
| --- | --- | --- | --- | --- |
| 1 | recovery `parked_seed` | `496-507` | `STATE_INTACT` | `blocked` guaranteed by `recovery.py:361-364` |
| 2 | `ci_gate_reentry`, no open PR | `517-555` | `UNCHARGED` | fail-safe restore only if zero state labels observed |
| 3 | `ci_gate_reentry`, merge exception | `588-628` | `UNCHARGED` | transient git/gh |
| 4 | `ci_gate_reentry`, outcome ≠ `MERGED` | `643-677` | `UNCHARGED` | CI-red, not agent failure — see Q3 |
| 5 | redispatch threshold breach | `680-744` | `TERMINAL` | writes `agent-failed`; counter untouched (D1) |
| 6 | pre-dispatch label-fetch failure | `766-812` | `UNCHARGED` | already correct today; route anyway for uniformity |
| 7 | issue-fetch failure | `848-886` | `UNCHARGED` | **currently broken** |
| 8 | preflight `app_id`/`obs` missing | `905-929` | `UNCHARGED` | **currently broken**, and currently posts no alert — add one |
| 9 | worker exception | `930-977` | **`CHARGED`** | **the reported bug** |
| 10 | preflight refused | `979-990` | *(no change)* | labels handled in `launch_gate.py:474-485` |
| 11 | post-run label-fetch failure | `1014-1085` | `UNKNOWN_STATE` | preserve today's behaviour + critical alert |
| 12 | label-invariant violation | `1093-1263` | `UNKNOWN_STATE` | **currently broken**, but writing a label here compounds the tear |
| 13 | `pr_created` but no PR located | `1275-1314` | **`CHARGED`** | a worker run was consumed; **not a no-op** — `after_run.py` has already added `agent-done` (plus `agent-in-progress` still present) by this point, so `park_issue` must remove both before writing `agent-ready`/`agent-failed`, or the result is a two-state violation (see the `CHARGED` handling rule above) |
| 14a | final park, `has_blocked` | `1347-1386` | `STATE_INTACT` | |
| 14b | final park, `no_pr` | `1347-1386` | **`CHARGED`** | split the branch |
| 15 | `_run_ci_gate`, merge exception | `gh_api_helpers.py:381-410` | `STATE_INTACT` | near-duplicate of site 3 |
| 16 | `_run_ci_gate`, outcome ≠ `MERGED` | `gh_api_helpers.py:427-456` | `STATE_INTACT` | CI-red — see Q3 |

**Line-number drift on sites 15/16 (`gh_api_helpers.py`), as of `main` @
`3eefe61`.** This plan's stated baseline is `main` @ `322b79e`. Commit `2c0519f`
(#353) has since landed a `CiDiagnostic` value object threaded through
`_run_ci_gate`, shifting line numbers without changing either site's structure
or `ParkClass` (both remain `STATE_INTACT`). Corrected ranges: `_run_ci_gate`'s
signature is now at `:302-321` (this plan says `:296-315`); the merge-exception
park block (site 15) is now `:389-418` (this plan says `:381-410`); the
non-MERGED park block (site 16) is now `:435-495` (this plan says `:427-456` —
the real block runs about 39 lines further, due to new `diag.describe()` /
`diag.classifier()` calls). Use the corrected ranges above when implementing;
the rest of this plan's citations are not re-baselined against `3eefe61`.

**Post-condition assertion (fail-safe, not fail-silent).** After its label edit,
`park_issue` re-reads live labels via `_daemon_mod._fetch_issue_labels` and runs
`assert_single_state`. A violation emits a `critical` alert naming the site's
`ParkClass`. This is what makes the declared classification *verifiable at
runtime*, not merely reviewable — and it is the mechanism that would have caught
#351 on the first occurrence.

Two specifics the implementer must not have to invent:

- **`_fetch_issue_labels` returning `None`.** The re-read can fail for exactly
  the reason site 11 exists. On `None`, **skip the assertion silently** — log at
  `debug`, emit nothing. The site's own `critical` alert (`work_unit.py:1049-1061`)
  has already fired for that condition, and a second alert on every such park is
  noise, not signal.
- **Cost.** This adds **one `gh` call per park**. Parks are rare relative to poll
  ticks, so this is acceptable — but state it so nobody discovers it as a
  surprise in an API-budget review.

`UNKNOWN_STATE` skips the restore but still attempts the assertion, so sites 11
and 12 become loud rather than silent whenever the labels *are* readable. That is
the correct answer for "labels are torn": tell a human, do not guess.

### D5 — counter lifecycle: increment on `CHARGED`, reset on merge and on terminalisation, no decay (RECOMMENDED)

| Event | Effect on `failure_count[n]` | Where |
| --- | --- | --- |
| `CHARGED` park | `+= 1` | `park.py` |
| `UNCHARGED` / `STATE_INTACT` / `UNKNOWN_STATE` park | unchanged | `park.py` |
| Issue merged (`sched.mark_done` + appended to `merged_issues`) | **reset (delete key)** | `work_unit.py:630-642`, `:491-494`, and inside `_run_ci_gate`'s MERGED exit |
| Counter reaches `N` → `agent-failed` written | **reset (delete key)** | `park.py` |
| Human removes `agent-failed`, adds `agent-ready` | *(no daemon action needed — already 0)* | — |
| Time / tick passage | **no decay** | — |

**Human re-label reset is handled by resetting at terminalisation, not by
observing the human.** Detecting a human label edit would require polling
timeline events or diffing label snapshots across ticks — new API cost and a new
failure mode, for a case the reset-on-write already covers. This is the
`simplicity-first` answer and it is exact: by the time a human can remove
`agent-failed`, the count is already zero.

*Accepted edge case:* a human who re-labels an issue sitting at count 1 (not yet
exhausted) gets `N - 1` remaining attempts rather than a fresh N. Arguably the
correct behaviour — nothing about the underlying failure changed.

**No decay.** Named and rejected: a sliding time/tick window. Under the real
deployment (F1: long-lived systemd, 30 s poll) a window is either meaninglessly
short or equivalent to no window. A plain count is easier to reason about and to
explain in the escalation comment ("attempt 2 of 2").

**Eviction:** reset deletes the key, so the file only ever holds issues with an
in-flight, unexhausted failure streak. No compaction task needed.

**`park_reason` strings are pinned, not left to prose.** `session_report.py:366`
derives `park_kind` by case-folded **substring match** on `"block"`:
`issue.park_kind = "block" if "block" in reason.casefold() else None`. A
terminal reason phrased "…blocked pending human triage" would silently be
classified `park_kind: "block"` — i.e. indistinguishable in the report from a
genuine agent-raised block. Pin the two new strings and forbid the word:

| Situation | Exact `park_reason` | Resulting `park_kind` |
| --- | --- | --- |
| `CHARGED`, below budget | `f"worker failure {count}/{N} — retrying"` | `None` |
| `CHARGED`, budget exhausted | `f"worker failure {count}/{N} — agent-failed (human triage required)"` | `None` |
| `TERMINAL` (redispatch breach, site 5) | `"redispatch loop — agent-failed (human triage required)"` | `None` |

`park_kind` deliberately stays `None` for all three: `"block"` means *the agent
asked a question*, which is precisely the distinction D3 exists to preserve.
Introducing a third `park_kind` value would be a report-schema change and is not
proposed — the `agent-failed` label itself is the durable terminal signal, and
the exact attempt count is in the escalation comment (D6 step 5).

Existing reason strings are unchanged; the implementer must not "tidy" them,
because `"blocked (recovery)"` (`work_unit.py:506`) relies on the substring match
to produce `park_kind: "block"` correctly.

### D6 — diagnostic threading, with redaction as a hard gate (resolves F4 + F5)

**Step 1 — new `src/baton_harness/redact.py`** (package root, deliberately *not*
`chain/`, so the vendored tree can import it without creating a
`vendor → chain` dependency; `src/baton_harness/_auth.py` sets the
package-root precedent):

```python
def redact_secrets(text: str, *, extra_values: Iterable[str] = ()) -> str: ...
```

- Pattern pass: `ghs_\w+`, `ghp_\w+`, `gho_\w+`, `ghu_\w+`, `ghr_\w+`,
  `github_pat_\w+`, and `://[^@/\s]+:[^@/\s]+@` (token-bearing remote URLs).
  Prefixes are already named as constants at `_auth.py:90`, `:93`, `:357`.
- Value pass: exact-substring replacement of every non-empty string in
  `extra_values` — used to pass the tokens the harness itself injected
  (`orch.hook_env`, `work_unit.py:399-403`) and `BWS_ACCESS_TOKEN`.
- Replacement token: `«redacted»` (or `<redacted>`, matching
  `install-daemon-service.sh:331`).
- Pure, no I/O, never raises. Unit-tested in isolation.

**Step 2 — `hooks.py`: redact *before* truncating.** Change `run_hook` to return
a small frozen `HookResult(ok: bool, returncode: int | None, stderr_tail: str)`.
`env` here is `hook_env`, which only ever holds `GH_TOKEN`/`GITHUB_TOKEN`
(`work_unit.py:399-403`) — `BWS_ACCESS_TOKEN` lives in `os.environ`, not
`hook_env`, so `extra_values=env.values()` alone silently excludes it. The call
must explicitly add it:
`stderr_tail` is
`redact_secrets(stderr.decode(errors="replace"), extra_values=[*env.values(), os.environ.get("BWS_ACCESS_TOKEN", "")])[:500]`.
Order matters: truncating first can split a token across the cut and defeat the
pattern pass. Apply the same redaction to the existing log line at
`hooks.py:51-56`, which today writes raw stderr to the journal.

**Step 3 — `orchestrator.py`: attach the detail to the exception.** At
`:197-198` and `:208-209`:

```python
raise RuntimeError(
    f"{name} hook failed (rc={res.returncode}): {res.stderr_tail}"
)
```

`:274-279` and `:392-397` need no change beyond tolerating the new return type
(they already discard it) — but confirm `tests/vendor/test_orchestrator_hook_env.py`
does not assert the `bool` contract.

**Step 4 — the detail reaches the operator.** `work_unit.py:956-976` already
interpolates `{exc}` into both `escalation_detail` and
`report.record_escalation`, so the richer message flows through with no further
change. Apply `redact_secrets` **again** at the `park.py` boundary as
belt-and-braces before `alert()` — cheap, and it covers detail strings from
non-hook sources (e.g. a `gh` stderr surfacing at site 3). Note that the
verbatim GitHub-comment post happens one call-frame down inside `escalate()`
(`escalation.py:109-192`), not inside `alert()` itself (`alert()` is the
severity-routing wrapper, roughly `escalation.py:211-296`, that calls
`escalate()`) — the `park.py`-boundary placement is still correct, this is
just a naming correction (see F5).

**Step 5 — the escalation comment states the budget.** Both the retry and the
terminal comment must say which it is, e.g.
`"attempt 1 of 2 failed — restoring agent-ready for retry"` versus
`"attempt 2 of 2 failed — applying agent-failed; human triage required"`, plus a
pointer to `${BH_PROJECT_ROOT}/.baton-harness/session-report.json`, whose
existence is currently surfaced nowhere (issue #351 body, "Correction to the
original report").

---

## 5. Sequenced tasks

Test-first split applies at implementation time (router policy); the tests named
in each task are the failing-test contract, not an afterthought.

### T1 — `src/baton_harness/redact.py` + `tests/test_redact.py`

Pure module, no dependants yet. Ship it first so T3 cannot land without it.
Tests: each token prefix redacted; a token split across the 500-char boundary is
still redacted (order-of-operations regression); `extra_values` exact-substring
pass; empty/None-ish inputs; the function never raises; a `BWS_ACCESS_TOKEN`-shaped
value passed via `extra_values` never appears in `stderr_tail` (regression test
for the D6 step 2 redaction gap — `env.values()` alone does not cover it).

### T2 — `src/baton_harness/chain/failure_tally.py` + `obs_config` knobs + `tests/chain/test_failure_tally.py`

Mirror `redispatch.py`'s structure. Add `BH_MAX_ISSUE_FAILURES` (default `2`)
and `BH_FAILURE_COUNTS_PATH` to `obs_config.py` following the exact guarded-parse
pattern at `:187-215` (non-numeric → WARNING + default, never raise) and the
derived-path pattern at `:236-239`. Extend the `ObsConfig` dataclass
(`:124-132`) and its docstring (`:106-117`).

Tests: fresh file; increment sequence returning `(count, exhausted)`;
`exhausted` is `count >= N`, exercised at `N-1`, `N`, `N+1`; `reset` deletes the
key; corrupt/missing/unreadable file → empty state, no raise; persist failure
swallowed; state survives a re-instantiation from the same path; `has_alerted`
returns `False` before `set_alerted` is called and `True` after, and this
survives a re-instantiation from the same path (same durability guarantee as
the count itself).

### T3 — vendored hook diagnostics (`hooks.py` + `orchestrator.py`)

Per D6 steps 2-3. Per `CLAUDE.md § Upstream dependency`, the vendored tree is
owned code post-#224: fix in place, **no `patches/*.diff`, no `VENDORING.md`
entry**. Keep the `VENDOR-PATCH` comment convention for traceability.

Tests: `run_hook` returns `HookResult` with the real `returncode` on failure;
`stderr_tail` is redacted; a `GH_TOKEN` value passed via `env=` never appears in
`stderr_tail`; `RuntimeError` from `before_run` carries rc + tail; the two
best-effort `after_run` call sites still swallow failures.

### T4 — `agent-failed` label taxonomy (`labels.py`, `daemon/__init__.py`, `work_unit.py:1101-1103`, `poll.py`)

Per D3 items 1-5. **`work_unit.py:1101-1103`'s hardcoded duplicate must be
replaced with `STATE_LABELS` in this task** — landing item 1 without item 2
creates a silent divergence. **Update `target_state_from_observed`'s docstring**
(`labels.py:111`) to state explicitly that this function never returns
`LABEL_AGENT_FAILED` — it only takes bool inputs and can never construct it —
and that `agent-failed` is written only by `park_issue` when a charged failure
budget is exhausted. The "always a member of `STATE_LABELS`" claim stays true
after `agent-failed` becomes a 4th member, but without this note a future
maintainer is likely to "complete the set" by adding an `agent_failed`
parameter, which would break the convergence backstop's assumption that this
function is always safe to call unconditionally.

Tests: `test_labels.py:46-78` currently asserts `len(STATE_LABELS) == 3` and
exact set membership — update to four. Add: the convergence backstop does **not**
converge an `agent-failed` issue with an open PR; an `{agent-ready,
agent-failed}` issue is skipped *before* dispatch (no worker call).

### T4b — `poll.py` snapshot-filter alert for a silently-excluded `agent-failed` issue

Per D3 item 5's correction. In the `elif` at `poll.py:577-583`, when the issue's
label set contains **both** `agent-failed` and `agent-ready`, emit a `critical`
alert naming the required operator action (remove `agent-failed`). Without this
the exclude entry converts a wrong-but-loud behaviour into a silent one.

**Depends on T2's `set_alerted`/`has_alerted`.** Per Q5 (decided option (b)), the
alert must fire once per issue, durably, not once per tick — this task gates
the alert on `failure_tally.has_alerted(issue)` and calls
`failure_tally.set_alerted(issue)` immediately after emitting it. T2 must ship
before this task can be implemented.

Tests: an `{agent-ready, agent-failed}` issue produces exactly one alert total
across repeated ticks (gated on `has_alerted`, per Q5) and is absent from
`ready_issues` on every tick; an `{agent-ready, blocked}` issue's behaviour is
**unchanged** (no new alert from this branch — that case is already handled by
the live re-check at `:624-637`).

Rate-limiting: without the `has_alerted` guard, the condition persists until a
human acts and this alert would repeat every tick (30 s under the real
deployment, F1) — Q5 decided against that (option (b)), which is why this task
depends on T2's durable flag rather than an in-memory or unthrottled alert.

### T5 — `src/baton_harness/chain/daemon/park.py` + route all sites

Per D4. Land the helper and the `ParkContext`, re-export from
`daemon/__init__.py`, then convert the **sixteen** sites one class at a time
(`STATE_INTACT` first — they are no-ops and prove the plumbing; then
`UNCHARGED`; then `CHARGED`; then `TERMINAL`; `UNKNOWN_STATE` last). Sites 15 and
16 live in `gh_api_helpers.py`, not `work_unit.py`; `_run_ci_gate` needs the
`ParkContext` (or the `failure_tally`) threaded into its signature
(`gh_api_helpers.py:296-315`) to reach the helper. `_run_ci_gate` has **two**
callers, and both need this signature-threading update, not just the more
visible one: the `pr_created` path (`work_unit.py:1316-1334`) and the
convergence path (`work_unit.py:1173-1191`). D5's table places the
failure-tally reset inside `_run_ci_gate`'s MERGED exit, so missing the
convergence-path caller specifically would leave the convergence → CI-gate →
park chain unable to reach `park.py`, and the failure-tally reset on merge
absent from the highest-traffic merge path — missing either caller breaks the
threading for that route.

Tests (`tests/chain/test_park.py`, plus additions to `test_daemon.py`):

- A `CHARGED` park at count `N-1` restores `agent-ready` and does **not** add
  `agent-failed`.
- A `CHARGED` park at count `N` adds `agent-failed`, removes `agent-ready` and
  `agent-in-progress`, and **resets** the counter.
- A `CHARGED` park at a site where a *non-`agent-ready`* state label is already
  present (site 13's `agent-done` is the concrete case) removes that label too,
  not just `agent-in-progress`, and produces exactly one state label after the
  park — the regression test for the two-state violation the naive
  "remove only `agent-in-progress`" implementation would produce.
- An `UNCHARGED` park restores `agent-ready` and leaves the counter unchanged
  (drive this through the issue-fetch-failure site, #7 — the cheapest real path).
- A `STATE_INTACT` park adds **no** state label (regression guard against the
  naive "restore everywhere" implementation — this is the assertion that
  protects sites 1, 14a).
- An `UNKNOWN_STATE` park writes no state label and emits a critical alert.
- The post-condition assertion fires a critical alert when a park leaves zero or
  ≥2 state labels.
- A successful merge resets a non-zero counter to 0.
- A successful merge reached via the **convergence path**
  (`work_unit.py:1173-1191` → `_run_ci_gate`) also resets the failure counter,
  not just a merge reached via the `pr_created` path (`work_unit.py:1316-1334`)
  — regression test for the second `_run_ci_gate` caller.
- **Coverage guard — must span both modules.** An enumeration test asserting
  that every park exit reachable from `_run_work_unit`, **including those inside
  `_run_ci_gate`**, goes through `park_issue`: patch `daemon_mod.park_issue` and
  assert `_label_edit` is never called with `remove=["agent-in-progress"]` from
  either `work_unit.py` **or** `gh_api_helpers.py` outside the helper. A guard
  scoped to `work_unit.py` alone is the specific defect to avoid — it would have
  let sites 15/16 pass unexamined, and it lets site 17 get added next quarter and
  silently reintroduce #351.
  **Known exclusion, deliberate:** `poll.py:638-645` also calls
  `_label_edit(remove=["agent-in-progress"])`, but it sits inside the live
  re-check's excluded-issue branch — pre-dispatch `agent-in-progress` cleanup,
  not a park decision, and unreachable for `agent-failed` issues (they are
  filtered out of `ready_issues` before reaching that branch). It is
  deliberately outside this guard's scope; do not widen the guard to catch it.

Known existing tests to re-verify (they assert exact `_label_edit` calls on park
paths): `tests/chain/test_daemon_park_label_clear.py` (sites 1 and 2 — both
`STATE_INTACT`/`UNCHARGED`, so the `remove` assertion should survive, but the
call now originates in `park.py`), and the park-path assertions in
`tests/chain/test_daemon.py`.

### T6 — provisioning + doctor

- `bin/init-sandbox.sh:268-272` — add `_create_label "agent-failed" "<color>"`.
  Pick a colour distinct from `blocked` (`e4e669`) and `agent-in-progress`
  (`d93f0b`); `b60205` (deep red) reads as terminal-failure.
- `src/baton_harness/chain/doctor.py:757-767` — add `"agent-failed"` to
  `required`. Update `tests/chain/test_doctor.py`'s expected-missing assertions.
- **Existing sandboxes need the label created manually** before the daemon
  first tries to apply it. `_label_edit`'s behaviour against a nonexistent label
  is not verified by this plan — the implementer must check it and, if it fails
  the edit, note the ordering requirement in the README.

### T7 — docs

- `docs/harness-design.md:127-137` (§ 5) — replace the 3-outcome diagram with
  the N-retry-then-terminal machine, name `agent-failed` and the
  `BH_MAX_ISSUE_FAILURES` budget, and state that `agent-failed` is a member of
  both `STATE_LABELS` and `_DISPATCH_EXCLUDE_LABELS`. Also correct § 6's C3 line
  (`:147`) — "bounded rework with escalation ... (Deferred — pilot reviews PRs
  manually)" is no longer deferred for the dispatch loop; this plan implements
  it.
- `README.md` — the env-var table (around `:360`) gains `BH_MAX_ISSUE_FAILURES`
  and `BH_FAILURE_COUNTS_PATH`, with the D2 note on why the default differs from
  `BH_REDISPATCH_MAX`. The label list gains `agent-failed` and the human-triage
  procedure (remove `agent-failed`, add `agent-ready`).
- `docs/repository-onboarding.md` — carries a label list; add `agent-failed`.
  *(Verify: identified by grep as containing `agent-ready`; the exact section was
  not read for this plan.)*

---

## 6. Acceptance criteria

1. Every park exit **reachable from `_run_work_unit`, including the two inside
   `_run_ci_gate` (`gh_api_helpers.py:381-410`, `:427-456`)**, routes through
   `park_issue` with an explicitly declared `ParkClass`, enforced by a T5
   coverage guard that spans both modules — not by review.
2. The worker-exception path (site 9) leaves the issue carrying exactly one
   state label on every outcome: `agent-ready` below the budget, `agent-failed`
   at it.
3. `STATE_INTACT` sites (1, 14a, **15, 16**) add **no** state label — asserted by
   test. Sites 15/16 are on the `pr_created` success path, so this is the
   regression guard for the common case, not an edge case.
4. The failure counter survives process restart (write, re-instantiate from the
   same path, read) and resets on merge and on terminalisation.
5. `run_hook` returns `returncode` and a **redacted** `stderr_tail`; a
   `GH_TOKEN` injected via `orch.hook_env` provably never appears in the
   escalation detail, the session report, or the daemon log — asserted by test,
   not by inspection.
6. `STATE_LABELS` has four members and `work_unit.py` no longer hardcodes a
   duplicate three-label set.
7. An issue carrying both `agent-ready` and `agent-failed` is skipped before
   dispatch (no agent run consumed) and produces an operator alert.
8. `bin/init-sandbox.sh` provisions `agent-failed`; `doctor.py`'s
   `LABELS_PRESENT` check requires it.
9. `docs/harness-design.md` § 5 describes the implemented machine, and § 6's C3
   line no longer says the escalation budget is deferred.
10. `ruff` + `mypy --strict` clean, including the vendored tree (no exclusions
    post-#224).

---

## 7. Risks

- **R-A — the redaction is incomplete and a token still reaches a public
  comment.** Highest-severity risk in the plan; nothing about it is red in CI.
  Mitigations: value-based pass (not just patterns) seeded from the tokens the
  harness itself injects **plus an explicit `os.environ.get("BWS_ACCESS_TOKEN",
  "")` injection into `extra_values`** — `hook_env` alone does not carry
  `BWS_ACCESS_TOKEN` (D6 step 2), so this must be a deliberate addition, not an
  assumed consequence of the value pass; redact-then-truncate ordering with an
  explicit regression test; redaction applied at both the `hooks.py` capture
  point and the `park.py` alert boundary. Residual: a hook that base64-encodes
  or line-wraps a token defeats both passes. Accept, and prefer omitting
  stderr entirely if T1's tests cannot demonstrate the value pass working
  end-to-end.
- **R-B — the shared helper regresses an already-correct site.** F2 shows seven
  sites are correct today; a naive "restore everywhere" refactor breaks five of
  them into two-state violations — **including sites 15 and 16, which sit on the
  `pr_created` success path**, so the regression would hit the common case, not
  a corner. Mitigations: declared per-site `ParkClass`, the AC-3 `STATE_INTACT`
  test, and the runtime post-condition assertion.
- **R-C — `agent-failed` traps a human mid-triage.** Closed by D3 item 5, but
  **only when both halves ship**: the `_DISPATCH_EXCLUDE_LABELS` entry stops the
  wasted agent run, and T4b's snapshot-filter alert stops the resulting skip
  from being silent. Shipping the exclude entry alone converts a loud-but-wrong
  behaviour into a quiet-and-wrong one. If Q1 resolves toward the marker shape
  instead, this risk returns and needs its own mitigation.
- **R-D — two budgets diverge in operators' heads.** `BH_REDISPATCH_MAX=3` and
  `BH_MAX_ISSUE_FAILURES=2` meter different things. Mitigation: D2's explicit
  contrast note, restated in the README table.
- **R-E — `_label_edit` against a label that does not exist in an already-
  provisioned sandbox.** T6 flags this as unverified. If the edit fails, the
  terminal escalation silently does nothing — the exact class of bug #351 is.
  The implementer must verify before merge.
- **R-F — the counter file and the session report disagree.** The counter is
  durable across sessions; the report is not (F3). An operator reading only the
  report sees "parked" with no attempt number. Mitigation: D6 step 5 puts the
  attempt count in the escalation comment text itself, which *is* durable on
  GitHub.

---

## 8. Open questions for the user

1. **Is D3's fourth-state-label recommendation accepted, or would you rather
   have `blocked` + an `agent-failed` marker?** This is the most consequential
   choice in the plan. Fourth-state-label buys a clean operational distinction
   and a free pre-dispatch guard, at the cost of five reconciliation edits
   (D3 items 1-5) across `labels.py`, `work_unit.py`, `poll.py`, and
   `daemon/__init__.py`. The marker shape needs none of them but permanently
   merges "agent asked a question" and "harness could not run this" into one
   `blocked` bucket.

   **Decided (2026-08-16):** Option A, the fourth mutually-exclusive state
   label (not the `blocked` + marker shape). `agent-failed` becomes a 4th
   member of `STATE_LABELS`, with all five reconciliation edits from D3
   items 1-5.

2. **Is `N = 2` the right budget?** D2 argues 2 from the issue's own evidence
   (deterministic failure, reproduced identically twice). `N = 3` costs one more
   full agent run per doomed issue and buys tolerance for a two-in-a-row
   transient. Also confirm the semantics reading: `N = 2` means one auto-retry,
   terminal on the second failure.

   **Decided (2026-08-16):** `N = 2`. One auto-retry, terminal on the second
   consecutive charged failure.

3. **Should CI-red parks consume the failure budget?** Three sites share this
   domain: site 4 (`work_unit.py:643-677`) and sites 15/16 inside `_run_ci_gate`
   (`gh_api_helpers.py:381-410`, `:427-456`). This plan says **no** for all
   three — CI failure is a code-quality signal, not a harness-execution failure,
   and an agent may legitimately need several CI rounds. But they are currently
   parks with a `critical` alert and no budget at all, so a permanently-red PR
   parks forever with no terminal state and no bound. If you want that bounded,
   it needs either its own budget or `CHARGED` classification — and it should be
   decided for all three together, not site by site.

   **Decided (2026-08-16):** No, CI-red parks (the 3 sites: `work_unit.py:643-677`,
   `gh_api_helpers.py:381-410`, `gh_api_helpers.py:427-456`) do NOT consume the
   failure budget. They stay `UNCHARGED`/`STATE_INTACT` per the existing D4
   classification table — no change to that table is needed.

4. **`agent-failed` colour.** `b60205` (deep red) is proposed; say if you have a
   convention.

   **Decided (2026-08-16):** `b60205` (deep red), as proposed.

5. **How loud should the `{agent-ready, agent-failed}` alert be?** T4b's alert
   fires from the poll snapshot filter, and the condition persists until a human
   removes the label — so at the real 30 s poll interval (`config/WORKFLOW.md:7`)
   it would post a GitHub comment every tick. Options: (a) alert once per daemon
   process lifetime per issue (in-memory set — lost on `Restart=on-failure`);
   (b) alert once per issue durably, reusing the `failure_tally` file with a
   flag; (c) log-only, no GitHub comment, accepting that the operator must read
   the journal. Recommendation is **(b)** — it is the only option that survives
   a restart, and the storage already exists — but it is a genuine cost/noise
   trade-off, so it is yours to call.

   **Decided (2026-08-16):** Option (b), durable alert once per issue, reusing
   the `failure_tally` file with a flag so it survives a daemon restart.

---

## 9. Citations index

Claims in this plan are backed by the following, each read directly on
2026-08-08 against `main` @ `322b79e`:

- `src/baton_harness/chain/daemon/work_unit.py` — `:267-341` (signature/params),
  `:433-1405` (Step-2 loop and all fourteen park exits), `:399-403`
  (`hook_env` token injection), `:819-828` (the state transition), `:1093-1263`
  (invariant backstop + hardcoded label duplicate at `:1101-1103`)
- `src/baton_harness/chain/labels.py` — `:21-36`, `:44-85`, `:93-128`
- `src/baton_harness/chain/redispatch.py` — `:53-93`, `:101-269`
- `src/baton_harness/chain/obs_config.py` — `:30-49`, `:106-132`, `:184-239`,
  `:276-286`
- `src/baton_harness/chain/recovery.py` — `:17-31` (precedence docstring),
  `:340-387` (classification)
- `src/baton_harness/chain/session_report.py` — `:142-183`, `:226-246`,
  `:316-366`, `:481-510`
- `src/baton_harness/chain/escalation.py` — `:109-192` (`escalate()`, not
  `alert()` — `alert()` is a separate, later severity-routing wrapper, roughly
  `:211-296`, that calls `escalate()`)
- `src/baton_harness/chain/daemon/poll.py` — `:248-267`, `:335-403`, `:526-644`,
  `:786-810`, `:813-930`
- `src/baton_harness/chain/daemon/gh_api_helpers.py` — `:296-457` (`_run_ci_gate`
  signature, merge-exception park, MERGED exit, non-MERGED park)
- `src/baton_harness/chain/daemon/__init__.py` — `:93-109`, `:319-333`
- `src/baton_harness/chain/daemon/launch_gate.py` — `:420-516`
- `src/baton_harness/chain/doctor.py` — `:757-799`
- `src/baton_harness/after_run.py` — `:15-44`, `:363-390` (docstring
  precedence), `:405-411`, `:457-506`
- `src/baton_harness/vendor/symphony/hooks.py` — `:12-67`
- `src/baton_harness/vendor/symphony/orchestrator.py` — `:183-280`, `:391-397`
- `src/baton_harness/_auth.py` — `:88-97`, `:352-397`
- `bin/init-sandbox.sh` — `:255-274`
- `bin/install-daemon-service.sh` — `:330-355`
- `bin/run-daemon.sh` — `:1-64`, `:243-251`
- `config/WORKFLOW.md` — `:1-17`, `:36-43`
- `docs/harness-design.md` — `:127-137` (§ 5), `:141-150` (§ 6 C3)
- `tests/chain/test_labels.py` — `:46-78`
- Issue [#351](https://github.com/glitchwerks/baton-harness/issues/351) — full
  body retrieved 2026-08-08

Unverified claims, marked as such in-place: the exact `docs/repository-onboarding.md`
section requiring the label list (T7); `_label_edit`'s behaviour against a
nonexistent GitHub label (T6 / R-E).

---

## Restoration record (2026-09-02)

This historical plan was recovered from a local Git stash and first committed
under issue #369 after an artifact audit found that PR #363 cited the file even
though it had never been committed. Lines 1–1005 above are preserved as
recovered so existing line-specific references remain valid.
