---
title: "#353 — CI-gate park diagnostics: distinguish 'no CI ever ran' from 'CI still pending'"
touches:
  - src/baton_harness/chain/merge.py
  - src/baton_harness/chain/daemon/gh_api_helpers.py
  - docs/harness-design.md
  - docs/chain-orchestration-design.md
  - docs/smoke-test-daemon.md
  - tests/chain/test_merge.py
  - tests/chain/test_daemon.py
  - tests/chain/test_daemon_report_wiring.py
  # Read-only for this plan (no edits proposed) but load-bearing for the
  # findings in § 3 — the reviewer must be able to check the claims:
  - src/baton_harness/chain/daemon/work_unit.py
  - src/baton_harness/chain/session_report.py
  - src/baton_harness/chain/escalation.py
  - src/baton_harness/chain/daemon/__init__.py
  - src/baton_harness/scenario/verify.py
  - config/WORKFLOW.md
  - tests/chain/test_session_report.py
skills_relevant:
  - python
  - simplicity-first
---

# #353 — CI-gate park diagnostics

**Issue:** [#353](https://github.com/glitchwerks/baton-harness/issues/353)
(open, filed 2026-08-09, no labels). **Baseline:** `main` @ `4128e90`.

**The defect in one line:** when the CI gate exhausts its 30-minute poll
budget, the park comment says `"Issue #N parked: CI timed out (CI_TIMEOUT)."`
and nothing else — so an operator cannot tell "the branch has no CI workflow
file, so no job will ever appear" from "the CI runner is slow". Both produce
byte-identical output today.

This plan scopes the diagnostic fix. It records **nine findings from reading
`main` that materially narrow the change** relative to both the issue body and
the dispatch brief — in particular, the fix does **not** need to touch
`_classify_check_runs` (the safety-critical green predicate) and does **not**
need any signature change reaching `work_unit.py`. Read § 3 before executing
anything.

---

## 1. Scope

**In scope**

- A `CiDiagnostic` value object in `src/baton_harness/chain/merge.py` carrying
  the structured facts the poll loop already sees and currently discards:
  which required checks were never observed in any poll, which were observed
  but never completed, which non-required job names *were* seen, poll count,
  and elapsed time.
- Accumulation of those facts inside `evaluate_ci`'s existing poll loop, and
  population of the diagnostic on the RED and TIMEOUT exits.
- Threading the diagnostic as an **opt-in mutable out-parameter** through
  `merge_issue_branch` → `evaluate_ci`, constructed inside `_run_ci_gate`.
- Enriching `_run_ci_gate`'s park alert body (`gh_api_helpers.py:447-456`) and
  its `parked_reasons` entry (`:439`) with that detail.
- Recording the same detail durably in the session report via the **existing**
  `record_escalation` API — closing a pre-existing gap where CI-gate parks
  never appear in the report's `escalations` list at all (finding F4). This
  fires for all three non-MERGED outcomes, including `MERGE_CONFLICT`; see D7
  for why that is correct and what it does *not* add.
- A four-way headline classification with a fixed vocabulary (§ D3).
- A pre-declared `reason` / `detail` split in the alert-building code, so
  #351's future `park_issue()` helper can absorb this enrichment without
  re-deriving or dropping it (§ D8).
- Documentation of the new behaviour in the three places that describe the CI
  gate's timeout semantics — `docs/harness-design.md` § "CI green predicate",
  `docs/chain-orchestration-design.md` § 5, and `docs/smoke-test-daemon.md`'s
  operator-facing failure table — plus the stale three-check list correction
  (finding F6) and the stale coverage enumeration in `test_merge.py` (F9).

**Out of scope (explicit non-goals)**

- **Any change to `_classify_check_runs` (`merge.py:352-407`) or to the "no
  vacuous green" predicate.** Finding F1 shows the change is unnecessary; the
  module docstring at `merge.py:30-46` documents that behaviour as
  load-bearing and it must stay byte-identical in effect.
- **Any change to `MergeOutcome` or `CiResult` membership.** See D2.
- **Any change to `MergeGateRecord` / `record_merge_gate`.** See D5 — the
  decision is explicitly *not* to add a `detail:` field.
- **Fail-fast early exit** when zero workflow runs have been observed for K
  consecutive polls. This is the change that would actually have saved the 30
  minutes, and it is genuinely attractive — but it changes the gate's
  *behaviour*, not its *diagnostics*, and it carries a false-negative risk
  (GitHub can be slow to register a workflow run). Separate issue. See Q2.
- **A preflight check that a target feature branch's tree contains the
  required CI workflow file** before dispatching issues against it
  (`doctor.py` / `launch_gate.py` hardening). Structurally different from
  message diagnostics; recommended by the investigation that produced #353.
  See Q3.
- **Any subprocess stderr in the new diagnostic.** Hard boundary — see F5 and
  R-B. The diagnostic carries only structured GitHub Actions API fields (job
  names, `status`, `conclusion`), which are not a credential surface. Raw
  `gh`/`git` stderr is, and there is still no redaction helper in `src/`
  (`docs/superpowers/plans/2026-08-08-park-path-label-restoration-351.md:110-112`,
  `:259-277`).
- **Fixing the pre-existing exfiltration surface on the merge-exception park
  path** (F5). Named, not fixed; it belongs with #351's `redact.py`.

---

## 2. What already exists (verified against `main` @ `4128e90`)

| Capability | State | Evidence |
| --- | --- | --- |
| Raw per-poll job snapshot (`name`, `status`, `conclusion`, and every other API field) | **Built, and already in scope inside the poll loop** | `merge.py:229-341` (`_query_action_jobs` returns full job dicts, `:336-341`); bound to `runs` at `merge.py:464-467` |
| Green predicate reducing a snapshot to `GREEN` / `RED` / `None` | Built | `merge.py:352-407` |
| Poll loop with deadline | Built | `merge.py:461-488` |
| A `CiResult` variant distinguishing "never observed" from "observed, pending" | **Does not exist** | `merge.py:137-148` — three members only |
| Any record of *which* checks were missing/pending at timeout | **Does not exist** | `merge.py:476-478`, `:487-488` return a bare `CiResult.TIMEOUT`; `runs` goes out of scope |
| Per-outcome park reason string | Built, hardcoded 3-way | `gh_api_helpers.py:439-446` |
| Park alert with severity routing | Built | `gh_api_helpers.py:447-456`; `escalation.py:211-312` |
| Alert body posted verbatim as the GitHub comment | Built | `escalation.py:160-172` (argv list, `--body summary`; no shell) |
| Runlog event carrying the alert body | Built — **free with this change** | `escalation.py:271-283` emits `detail=summary` for every severity |
| Durable session-report escalation record with a `detail` field | Built | `session_report.py:73-87`, `:316-342`, serialised at `:549-557` |
| CI-gate park calling `record_escalation` | **Does not exist** | `gh_api_helpers.py:427-457` — calls `alert` but never `report.record_escalation` |
| `detail` field on `MergeGateRecord` | Does not exist | `session_report.py:58-70`, serialised at `:522-528` |
| Operator override of the required-check set | Built | `gh_api_helpers.py:265-293`; **not set in this repo** — `config/WORKFLOW.md` has no `required_checks:` key (grep, 2026-08-09), so the hardcoded four-element fallback (`merge.py:119-124`) is live |

---

## 3. Findings that change the plan (read before executing)

### F1 — `_classify_check_runs` does not need to change (corrects the issue body)

The issue names three failure points; its first is
"`_classify_check_runs()` returns `None` for both absent checks and
in-progress checks, making them indistinguishable"
([#353](https://github.com/glitchwerks/baton-harness/issues/353), retrieved
2026-08-09 — see the provenance note in § 9).

That is an accurate description of `merge.py:387-404`, but it is **not the
place the fix has to happen**. `evaluate_ci` already holds the same raw
snapshot the classifier consumed:

```python
runs = _query_action_jobs(owner, repo, sha, installation_token=...)   # :464-466
result = _classify_check_runs(runs, required)                         # :467
```

Every fact the diagnostic needs — which names appeared, with what `status` and
`conclusion` — is derivable from `runs` in `evaluate_ci`'s own frame, at
`merge.py:463-467`, with no change to the classifier's signature, return type,
or semantics.

This matters for three reasons:

1. `_classify_check_runs` is the "no vacuous green" predicate. Its correctness
   is the single most safety-critical property in the module
   (`merge.py:30-46`). Leaving the function byte-identical means the change
   cannot regress it.
2. "Never observed **in any poll**" is not a property of one snapshot at all —
   it requires accumulation across the loop, which only `evaluate_ci` can do.
   A check could appear at poll 40; a last-snapshot-only classifier would still
   be wrong about it. So the classifier is structurally the wrong home.
3. It removes the largest chunk of diff from the riskiest file region.

**Consequence:** T1 accumulates `ever_observed` in `evaluate_ci` and delegates
the reduction to a new *pure* helper `_build_ci_diagnostic(...)` which is
directly unit-testable without the poll loop or any subprocess patching.

### F2 — no signature change reaches `work_unit.py` (corrects the dispatch brief)

The brief states that "threading richer detail through needs either a new
`MergeOutcome`-adjacent return shape or an out-parameter" on `_run_ci_gate`
(`gh_api_helpers.py:296-315`). Neither is needed.

`_run_ci_gate` is *itself* the consumer: it calls `merge_issue_branch`
(`:368-380`) and builds the alert (`:447-456`) in the same function body. It
can construct the `CiDiagnostic` locally, pass it down, and read it back — no
new parameter, no new return value.

Therefore **`work_unit.py` is untouched by this plan.** Both call sites
(`work_unit.py:1173-1191` convergence path, `work_unit.py:1316-1334`
`pr_created` path) keep their current kwargs and keep calling
`report.record_merge_gate(n, outcome=_outcome.name, ...)`
(`:1192-1203`, `:1335-1346`) unchanged. This is the single biggest simplicity
claim in the plan; if a reviewer sees `work_unit.py` in the diff, something
went wrong.

It is also what closes off D5 (see below): a `detail` on `MergeGateRecord`
would be written by callers that provably do not have the diagnostic.

### F3 — the two "must update" daemon tests do not assert the strings (corrects the explore map)

The brief states that
`tests/chain/test_daemon.py::test_ci_gate_failed_park_routes_through_alert_severity_critical`
"asserts the EXACT summary pattern `Issue #{n} parked: {reason} ({outcome.name}).`".
It does not. The pattern appears only in the **docstring**
(`test_daemon.py:2677-2678`); the only assertion is on
`severity == "critical"` (`:2745-2754`). The test survives enrichment
unchanged; the docstring becomes stale and must be corrected as a doc-only
edit.

Likewise `test_converged_no_pr_result_ci_failed_parks_coherently`
(`test_daemon.py:5102-5244`): its docstring claims "Park reason contains CI
failure context" (`:5122-5123`), but the body asserts only
`mock_merge_fn.called`, `mark_parked`, `not mark_done`, and
`liveness_clear.call_count` (`:5222-5244`). No park-reason assertion exists.
Survives unchanged.

**Consequence:** the existing daemon-suite churn from this change is far
smaller than the brief implies — but there is a *different* hazard the brief
missed, and it is real. See D4's degradation requirement and R-A: most daemon
tests patch `merge_issue_branch` with a `MagicMock`
(`test_daemon.py:2728-2731`, `test_daemon_report_wiring.py:773-776`), which
**silently accepts and ignores** the new `diagnostic=` kwarg. The diagnostic
therefore arrives back at `_run_ci_gate` unpopulated in every such test, and
the enriched alert must degrade to today's exact string rather than crash or
interpolate `None`.

### F4 — CI-gate parks are absent from the session report's escalations list

`_run_ci_gate` fires `alert(..., severity="critical", kind="debug", ...)` on
every non-MERGED outcome (`gh_api_helpers.py:447-456`) but never calls
`report.record_escalation`. Its `report` parameter (`:314`) is forwarded only
to `_label_edit` (`:387-394`, `:414-421`, `:428-435`).

The convention elsewhere is to pair them — `work_unit.py:1289-1313` calls
`report.record_escalation(...)` alongside its alert. So today a reader of
`.baton-harness/session-report.json` sees, for a CI-timeout park:

- `merge_gate: {outcome: "CI_TIMEOUT", merged_sha: null, ts: ...}`
- `park_reason: "CI gate: CI_TIMEOUT"`
- `escalations: []`

`EscalationRecord` already has exactly the `detail: str` field this diagnostic
needs (`session_report.py:73-87`), already serialised
(`session_report.py:549-557`), already rolled into `totals.escalations`
(`:474`). Closing the gap costs four lines and one import, needs no schema
change, and is the reason D5 rejects `MergeGateRecord.detail`.

**Verified not to break a pinned count:** `totals["escalations"] == 1` at
`tests/chain/test_session_report.py:274` is a pure `SessionReport` unit test
that constructs records directly (`:230-266`) and never runs the daemon —
unaffected. `test_daemon.py:1354-1359` asserts the *absence* of
`kind="block"` escalations on an unrelated path; the new record uses
`kind="debug"` (matching the alert at `gh_api_helpers.py:453`) — unaffected.

### F5 — BLOCKING for stderr, not for this change: the merge-exception park path already leaks

`_run_ci_gate`'s exception handler posts `f"Issue #{n} merge raised an
exception: {exc}"` to a public GitHub comment
(`gh_api_helpers.py:399-408` → `escalation.py:160-172`). Several exceptions
reaching that handler embed raw `gh` stderr verbatim:
`merge.py:286-293` (`CiAuthError` / `RuntimeError` on `actions/runs`),
`:320-327` (same for `.../jobs`). Those `gh` calls run with a
`GH_TOKEN`-bearing env (`merge.py:277-280`, `gh_env`), and the sandbox repos
are public.

This is a **pre-existing** surface, unchanged by this plan, and it is exactly
the class of defect #351's F5 documents
(`docs/superpowers/plans/2026-08-08-park-path-label-restoration-351.md:259-277`).
It is named here for two reasons:

1. It is the reason the new diagnostic is restricted to structured API fields.
   Job names, `status`, and `conclusion` come from a parsed JSON body, never
   from a process's stderr — no token can reach them.
2. Someone will eventually want the exception text in the diagnostic too. The
   answer is: **not until `src/baton_harness/redact.py` exists** (#351 T1).
   Record the dependency; do not fold it in here.

### F6 — two prose blocks inside the files this plan already edits are stale

- `docs/harness-design.md:257` and `tests/chain/test_merge.py:10-11` both say
  the required set is three checks — `Lint (ruff)`, `Test (pytest)`,
  `Type check (mypy)`. `merge.py:119-124` has **four**; `Lint (shellcheck)` is
  missing from both. `:257` is inside the exact section T6 edits; `:10-11` is
  inside the exact file T1 edits.
- `tests/chain/test_merge.py:48-60` carries a `Coverage:` enumeration of what
  the file tests. T1 adds six-plus new `evaluate_ci` diagnostic tests; without
  extending the list, T1 leaves a stale inventory in a docstring it is already
  editing.

Both are corrected in-place rather than left for a reviewer to wonder about.

### F9 — the park-comment strings are pinned nowhere; three doc surfaces describe them

A repo-wide grep for `CI gate:` / `CI timed out` / `park_reason` /
`CI_TIMEOUT` outside `tests/` (2026-08-09) returns exactly one producer —
`gh_api_helpers.py:439-446` — and **no** consumer that matches either string
exactly. Specifically:

- **The scenario harness does not pin `park_reason`.** Its only related
  expectation key is `park_reason_present`, evaluated as
  `bool(issue.get("park_reason")) == expected` (`src/baton_harness/scenario/verify.py:198-208`);
  the recognised-key set (`:134-143`) contains no exact-match park-reason key.
  So D6's classifier suffix cannot break a scenario expectation. `verify.py`
  is in `touches:` as read-only for this reason.
- **Three docs describe the timeout semantics** and go stale on T3:
  `docs/harness-design.md:253-262`, `docs/chain-orchestration-design.md:147`
  and `:171` (a live reference doc complementing `harness-design.md`, per its
  own `:3`), and `docs/smoke-test-daemon.md:366-386`. The last is the most
  important: `:372` is an operator-facing failure table whose row reads
  *"No CI workflow at all | Required checks never arrive → 30-minute wait →
  CI_TIMEOUT → issue parked"* — i.e. this exact incident was already a
  *documented* expected behaviour, with no hint that the park comment would
  say nothing about it. T6 covers all three.

*Flagged, deliberately not fixed:* `docs/smoke-test-daemon.md:386` claims a
CI_TIMEOUT-parked issue "carries the `blocked` label". `_run_ci_gate`'s
non-MERGED branch removes only `agent-in-progress` and adds nothing
(`gh_api_helpers.py:428-435`), so that is wrong today. It is label-state
semantics, which is #351's domain (its F2 classifies this site as
`STATE_INTACT`,
`docs/superpowers/plans/2026-08-08-park-path-label-restoration-351.md:191-203`) —
correcting it here would put this plan in the middle of a decision it does not
own.

### F7 — `evaluate_ci(required=[])` is vacuously GREEN (flag, do not fix)

`merge.py:458-459` defaults `required` only when it is `None`. An explicit
empty list makes the `for` loop at `:387` iterate zero times and
`_classify_check_runs` return `GREEN` at `:407` — a vacuous pass, contradicting
the module docstring's central promise (`:30-36`).

Unreachable in production: `_effective_required_checks` returns
`config.required_checks` only when truthy (`gh_api_helpers.py:281-282`) and
otherwise the non-empty constant (`:293`). Flagged so it is a known,
deliberate non-goal rather than an undiscovered hole; file separately if
wanted.

### F8 — the grounding investigation is ephemeral

The real-world trigger for #353 was investigated in
`.tmp/2026-08-09-investigator-pr6-incomplete.md`. `.tmp/` is gitignored (it
does not appear in `git status`), so that file will not survive. Its
load-bearing conclusions are therefore restated inline here:

- `cbeaulieu-gt/baton-test` PR #6 (issue #2, branch
  `baton/add-hello-function-2` → base `feature/hello-feature`) parked at
  `2026-08-09T00:28:24Z` with `"Issue #2 parked: CI timed out (CI_TIMEOUT)."`,
  ~1800 s after the PR head SHA was created — the full
  `_DEFAULT_TIMEOUT = 1800.0` budget (`merge.py:129`).
- `gh api repos/cbeaulieu-gt/baton-test/actions/runs?head_sha=8d3325f9...`
  returned **zero** workflow runs; `.../commits/8d3325f9.../check-runs`
  returned `{"total_count": 0, "check_runs": []}`.
- Cause: `feature/hello-feature` HEAD dates from `2026-07-27`, while
  `.github/workflows/ci.yml` was first added to `main` on `2026-08-02` — the
  branch lineage's tree has no workflow file, so no run can ever fire.
- Two hypotheses were explicitly *ruled out* and are what make the four-way
  classification in D3 worth building: (a) required-check **name mismatch**
  between `REQUIRED_CHECKS` and the workflow's job names, and (b) **Actions
  disabled** for the repo. A diagnostic that cannot separate those from
  "no workflow file" leaves an operator in the same position.

---

## 4. Decisions

### D1 — a mutable `CiDiagnostic` out-parameter, not a new return type (RECOMMENDED)

**Decision:** add to `src/baton_harness/chain/merge.py`:

```python
_DIAG_NAME_CAP: int = 10          # max job names interpolated into a message

@dataclass
class CiDiagnostic:
    """Structured facts about why a CI evaluation did not reach GREEN."""
    required: tuple[str, ...] = ()
    never_observed: tuple[str, ...] = ()          # required, absent from EVERY poll
    other_observed: tuple[str, ...] = ()          # non-required names ever seen (capped)
    states: tuple[tuple[str, str], ...] = ()      # (required name, last-known state)
    failed: tuple[tuple[str, str], ...] = ()      # (name, conclusion) — RED only
    polls: int = 0
    elapsed_s: float = 0.0
    populated: bool = False

    def classifier(self) -> str: ...   # short fixed-vocabulary token; "" when unpopulated
    def describe(self) -> str: ...     # multi-line operator-facing block
```

Threading: `evaluate_ci(..., diagnostic: CiDiagnostic | None = None)` and
`merge_issue_branch(..., diagnostic: CiDiagnostic | None = None)`; the latter
forwards to the former. Default `None` on both — the parameter is opt-in and
every existing call keeps working unchanged.

**Why an out-parameter rather than changing the return type.** Returning
`tuple[CiResult, CiDiagnostic]` from `evaluate_ci` (or
`tuple[MergeOutcome, CiDiagnostic]` from `merge_issue_branch`) is the
"cleaner" shape in the abstract, and it is the wrong call here. It costs:

| Cost | Evidence |
| --- | --- |
| Every direct `evaluate_ci` assertion in the merge suite | `tests/chain/test_merge.py` — `assert result == CiResult.GREEN` and friends appear throughout (e.g. `:961`, `:985`, `:1020`, `:1052`, `:1071`) |
| `merge_issue_branches` — **test-maintenance cost only** | `merge.py:781-794` unwraps a bare `MergeOutcome` and compares it at `:794`, but it has **no production caller**: repo-wide grep (2026-08-09) finds it only at its own definition (`merge.py:729`) and in `tests/chain/test_merge.py:1680`, `:1688`, `:1706`, `:1737`. Weigh it as test churn, not as a production API break |
| The S7 signature pin | `tests/chain/test_daemon_report_wiring.py:760-801` — asserts `_run_ci_gate` returns `MergeOutcome.MERGED` directly |
| Every daemon test patching `merge_issue_branch` with a plain return value | `test_daemon.py:2728-2731`, `:5200-5203`, `test_daemon_report_wiring.py:773-776` |

None of that churn buys anything the out-parameter does not.

**Why this is not un-idiomatic here:** `_run_ci_gate` already takes two
mutable accumulators by exactly this pattern —
`merged_issues: list[int]` and `parked_reasons: dict[int, str]`
(`gh_api_helpers.py:308-309`), documented as "Mutable list/dict accumulating…"
(`:349-350`). The convention is established in the function that will own the
new object.

**Alternatives named and rejected:**

- *Module-level "last diagnostic" global.* Rejected — hidden state, not
  reentrant, and the daemon's future parallelism plans would silently corrupt
  it.
- *`evaluate_ci` raises a `CiTimeout` carrying the detail.* Rejected —
  converts a normal outcome into control flow, and `merge_issue_branch`'s
  `RED`/`TIMEOUT` handling at `:565-568` would have to become a `try`.
- *Return the diagnostic only from a second, separate query call after
  timeout.* Rejected — costs an extra `gh api` round trip and cannot recover
  "never observed in **any** poll", which is the whole point.

### D2 — no new `CiResult` or `MergeOutcome` variant (RECOMMENDED)

`CiResult.TIMEOUT` and `MergeOutcome.CI_TIMEOUT` keep their exact current
membership and meaning.

**Why not add e.g. `CI_NEVER_RAN`.** Three reasons, in order of weight:

1. **It cannot encode the real condition.** The interesting cases are a
   *partition* of the required set — some checks absent, others pending. An
   enum variant forces a lossy choice; a set of names does not.
2. **`MergeOutcome.name` is a public-ish string.** It is written into
   `parked_reasons[n]` (`gh_api_helpers.py:439`) and into the session report's
   `merge_gate.outcome` (`work_unit.py:1196`, `:1339`;
   `session_report.py:525`). Adding a member changes the report's outcome
   vocabulary for every downstream consumer, including the scenario
   expectation harness (`tests/scenario/test_expectations.py`).
3. **`_merge_gate_failures` counts `outcome != "MERGED"`**
   (`session_report.py:313-314`) — correct for a new member, but the point is
   that each new member is a reconciliation obligation across files, bought for
   a strictly weaker representation than the one D1 already provides.

The outcome is genuinely still "timed out without reaching green". What was
missing is *why*, and why is data, not an enum.

### D3 — a four-way headline classification with a fixed vocabulary (RECOMMENDED)

At timeout, let `required` be the effective required set, `ever_observed` the
union of **all** job names seen across every poll (required or not), and
`last` the final snapshot. Then:

| # | Condition | Headline | Classifier token |
| --- | --- | --- | --- |
| 1 | `ever_observed` is empty | "No GitHub Actions jobs were observed at all for this SHA (polls={polls}, elapsed={elapsed:.0f}s). The branch may not contain a CI workflow file, the workflow's trigger may not match this branch, or Actions may be disabled for the repository." | `no jobs observed` |
| 2 | `never_observed == required` and `ever_observed` non-empty | "None of the {R} required checks ever appeared, but {K} other job(s) did: {names}. This is the signature of a required-check name mismatch — compare `required_checks:` in `config/WORKFLOW.md` against the workflow's job names." | `required checks never appeared` |
| 3 | `never_observed` is a non-empty **proper** subset of `required` | "{a} required check(s) never appeared: {names}; {b} appeared but never completed: {names}." | `required checks partially missing` |
| 4 | `never_observed` is empty | "All {R} required checks appeared but did not all reach a completed passing state: {name=state, …}." | `required checks never completed` |

Cases 1–4 are exhaustive and mutually exclusive by construction (they
partition on `|never_observed|` and then on `|ever_observed|`). Cases 1 and 2
are exactly the two hypotheses the #353 investigation had to rule out by hand
(F8); case 4 is genuine slow/hung CI; case 3 is the mixed state that no single
enum variant could have expressed (D2).

**Per-check `states`.** For each required check, one of:

- `"never observed"` — not in `ever_observed`;
- `"<status>/<conclusion or -->"` — from `last`;
- `"observed earlier, absent from final poll"` — in `ever_observed` but not in
  `last`.

This third state is not hypothetical padding: it is the only honest answer when
a workflow run is deleted or re-created mid-poll, and without it case 4's
message would assert something false.

**Counter rendering — pick the shape now, because the tests will freeze it.**
Render poll and elapsed counters as `key=value` pairs
(`polls=1, elapsed=0s`), never as English with a pluralised noun. T1's tests
run with `timeout=0`, which produces exactly one poll and zero elapsed
seconds; a prose template would emit "across 1 polls (0s)" and the assertion
would then pin that. `{elapsed:.0f}s` for the same reason — a bare float
renders `1800.0000238418579s`.

**Interpolation cap.** `other_observed` and any name list rendered into a
message is capped at `_DIAG_NAME_CAP = 10` entries followed by `"+K more"`.
GitHub's 65 536-char comment limit is not a realistic ceiling here, but
unbounded third-party API data flowing into a comment body is worth bounding
on principle, and a monorepo with 60 matrix jobs makes the uncapped version
genuinely unreadable.

### D4 — append to the existing alert line; never replace it (RECOMMENDED)

The alert body's **first line stays byte-identical** to today's:

```
Issue #{n} parked: {reason} ({outcome.name}).
```

with `reason` still drawn from the unchanged three-way map at
`gh_api_helpers.py:440-446`. The diagnostic is appended as subsequent lines:

```
Issue #12 parked: CI timed out (CI_TIMEOUT).

No GitHub Actions jobs were observed at all for this SHA (polls=181,
elapsed=1801s). The branch may not contain a CI workflow file, the
workflow's trigger may not match this branch, or Actions may be disabled
for the repository.

Required checks (4):
  - Lint (ruff): never observed
  - Lint (shellcheck): never observed
  - Test (pytest): never observed
  - Type check (mypy): never observed
Other job names observed: none
```

Three consequences the implementer must not have to infer:

- **The three-way `reason` conditional at `:440-446` stays three-way.** Only
  the `CI_TIMEOUT` arm (and the `CI_FAILED` arm if T5 ships) gains appended
  detail; the merge-conflict `else` arm is unchanged. Do not restructure the
  conditional into a table.
- **`severity` stays `"critical"` and `kind` stays `"debug"`**
  (`:452-453`). `kind` does not affect the posted body
  (`escalation.py:219`, `:250-251`, `:304-312`) and needs no change.
- **Unpopulated diagnostic degrades to today's exact one-line string.** This
  is a hard requirement, not a nicety: F3 shows the daemon suite patches
  `merge_issue_branch` with `MagicMock`s that swallow the `diagnostic=` kwarg,
  so `populated is False` is the *normal* state in tests and in
  `merge_issue_branches` (which does not pass the kwarg). `describe()` must
  return `""` and `classifier()` must return `""` when unpopulated, and the
  builder must emit no trailing whitespace or empty parenthetical in that case.

**Log the degradation — but only where unpopulated is anomalous.** When
`outcome in {CI_FAILED, CI_TIMEOUT}` **and** `populated is False`, emit a
`_log.debug` line saying the diagnostic was not captured; in production that
would mean it silently vanished, which is the same invisibility failure class
this plan exists to eliminate, and it would otherwise leave no trace anywhere.

The guard is not cosmetic. `MERGE_CONFLICT` is **unpopulated by
construction** (see D7's § "why MERGE_CONFLICT carries no diagnostic"), so an
unguarded log line would fire on every merge conflict and train a reader to
ignore the signal. `debug` level, so it costs nothing in the journal by
default.

**Free side effect, no work required:** `alert()` emits a runlog event with
`detail=summary` for every severity (`escalation.py:271-283`), so the enriched
body lands in the JSONL runlog automatically. No separate runlog event is
needed, and none should be added.

### D5 — record the detail via `record_escalation`; do **not** add `MergeGateRecord.detail` (RECOMMENDED)

**Decision:** inside `_run_ci_gate`'s non-MERGED branch, immediately after the
`alert(...)` call, add:

```python
if report is not None:
    report.record_escalation(
        n,
        kind="debug",
        severity="critical",
        detail=<the same enriched summary passed to alert>,
        ts=datetime.now(timezone.utc).isoformat(),
    )
```

`gh_api_helpers.py` does **not** currently import `datetime` (`:19-33`) — the
task adds `from datetime import datetime, timezone`, matching
`work_unit.py`'s usage at `:1312`.

**Why not a `detail:` field on `MergeGateRecord`.** The decisive argument is
mechanical, not aesthetic: `record_merge_gate` is called by the **caller** of
`_run_ci_gate` (`work_unit.py:1192-1203`, `:1335-1346`), which receives only a
`MergeOutcome` and provably has no access to the diagnostic (F2). Populating a
`detail` field there would require *either* changing `_run_ci_gate`'s return
type — breaking the S7 signature pin at
`test_daemon_report_wiring.py:760-801` and pulling `work_unit.py` into the
diff — *or* having `_run_ci_gate` call `record_merge_gate` itself, duplicating
a call the caller already makes and racing it for the same field.

The secondary arguments all point the same way: `EscalationRecord.detail`
already exists and is already serialised (`session_report.py:73-87`,
`:549-557`); the report's `escalations` list is the natural home for
human-readable narrative while `merge_gate` is the structured outcome; the two
live on the same `IssueRecord` (`session_report.py:109-120`) so
cross-referencing is trivial; and F4 shows the escalations list is *missing*
this park entirely today, so the change fixes a real gap instead of creating a
parallel one.

**Regression guard:** a test asserting the serialised `merge_gate` block still
has exactly the keys `{outcome, merged_sha, ts}` (`session_report.py:524-528`)
— so a future contributor cannot quietly reintroduce the rejected shape.

### D6 — `parked_reasons` gets a fixed-vocabulary classifier only, never check names (RECOMMENDED)

```python
parked_reasons[n] = f"CI gate: {outcome.name}"                       # today
parked_reasons[n] = f"CI gate: {outcome.name} ({diag.classifier()})" # proposed, when populated
```

with `classifier()` drawn **only** from the closed vocabulary in D3's last
column, plus `required check failed` (T5) — never from interpolated check
names.

**Why this constraint exists.** `session_report.set_outcomes` derives
`park_kind` by a case-folded **substring** match on `"block"`:

```python
issue.park_kind = "block" if "block" in reason.casefold() else None   # session_report.py:366
```

Required check names are **operator-supplied data** (`required_checks:` in
`config/WORKFLOW.md`, resolved at `gh_api_helpers.py:281-282`). A target repo
with a job named e.g. `Blocklist lint` or `Block scanner` would, if names were
interpolated into `parked_reasons`, silently flip that issue's `park_kind` to
`"block"` — making a CI timeout indistinguishable in the report from an agent
raising a genuine question. That is the exact confusion #351's D5 pins down
(`docs/superpowers/plans/2026-08-08-park-path-label-restoration-351.md:527-548`).

The closed vocabulary closes it by construction. Verify at review: no token in
the vocabulary contains the substring `block`. The full names live in the
GitHub comment and in the escalation record, neither of which feeds the
`park_kind` derivation.

### D7 — `MERGE_CONFLICT` shares the park branch but carries no diagnostic; the escalation record still fires for all three (RECOMMENDED)

`_run_ci_gate`'s non-MERGED branch (`gh_api_helpers.py:427-457`) is shared by
**three** outcomes — `CI_FAILED`, `CI_TIMEOUT`, and `MERGE_CONFLICT`. The
third is different in kind: a merge conflict means CI *passed*. This decision
states what happens on that arm, because leaving it implicit invites exactly
the wrong implementation.

**Why `MERGE_CONFLICT` carries no diagnostic — by construction, not by
guard.** `merge_issue_branch` returns early on RED and TIMEOUT
(`merge.py:565-568`); everything below `:570` is reachable only when
`evaluate_ci` returned `GREEN`, and the conflict is detected further down
still, at `:599-611`. D1 populates the diagnostic **only** on the RED and
TIMEOUT exits of `evaluate_ci`. Therefore on the `MERGE_CONFLICT` arm the
object is guaranteed `populated is False`, and D4's degradation rule renders
today's exact one-line body.

So the failure mode a reviewer would reasonably fear — a merge-conflict park
comment appended with *"5 polls, all 4 required checks present, no timeouts"*,
i.e. CI data describing a **successful** CI run presented as if it explained
the park — **cannot occur**. It is closed by the population rule, with no
conditional at the call site. Nothing needs adding; what needs adding is the
test that pins it, because the property is invisible in the diff.

**Decision A (message content):** the `MERGE_CONFLICT` park comment and
`parked_reasons` entry stay byte-identical to `main`. Asserted by test (T3).

**Decision B (escalation record): fire `record_escalation` for all three
outcomes, unconditionally** — the reviewer's option (b). Rationale:

1. **The record cannot mislead, because its `detail` is *defined as* the
   summary that was passed to `alert`.** It can never assert something the
   operator was not also told on the issue. On the conflict arm that summary
   is today's one-liner, so the record contains no CI data at all.
2. **A guard would leave finding F4's gap half-open.** F4 is "CI-gate parks
   are absent from the report's `escalations` list". Guarding on
   `outcome in {CI_FAILED, CI_TIMEOUT}` fixes that for two outcomes and leaves
   the third alerting-but-unrecorded — an asymmetry a future reader would have
   to have explained to them, in a list whose whole purpose is "what did the
   daemon shout about".
3. **Unconditional is less code** (no branch) and preserves a clean invariant:
   *every* `alert` from this function has exactly one matching
   `EscalationRecord`.

**Named, accepted scope side effect:** this means #353 incidentally closes the
escalation-record gap for merge conflicts too. That is one line of code and
zero new data — but it is a real widening beyond "CI timeout diagnostics", so
it is recorded here rather than discovered in review.

**Alternative named and rejected:** guard the `record_escalation` call on
`outcome in {CI_FAILED, CI_TIMEOUT}`. Rejected for reasons 2 and 3 above; it
buys no risk reduction, because reason 1 means the conflict record is
harmless either way.

Note the contrast with D4's debug log, which **is** guarded to
`{CI_FAILED, CI_TIMEOUT}`. The two are not inconsistent: the escalation record
mirrors something that happened (an alert fired), while the debug log asserts
something anomalous (a diagnostic that should exist does not). Unpopulated is
normal on the conflict arm, so only the second needs the guard.

### D8 — pre-declare a `detail` slot so #351 can absorb this without re-deriving it (RECOMMENDED)

**Sequencing decision (made by the router with the user, recorded here, not
re-litigated):** #353 and #351 stay **separate plans**, and **#353 lands
first**. #353 has no dependency on #351 — `park_issue()` does not exist yet
and this plan does not need it. **#351's plan file is not edited by this
plan**; it is a separate open plan owned by a separate issue.

What that leaves is a coordination gap: #351's T5 will later route
`gh_api_helpers.py:427-456` (its "site 16") through a new `park_issue()`
helper
(`docs/superpowers/plans/2026-08-08-park-path-label-restoration-351.md:670-678`).
Whoever implements that must not have to guess how #353's enrichment survives
the move. So the contract is declared **on the producing side, in this plan**,
as a requirement on #353's own code shape:

**Requirement 1 — the alert body must be two separately-addressable locals,
never one interpolated f-string.**

```python
_PARK_DETAIL_SEP = "\n\n"          # module-level constant in gh_api_helpers.py

park_reason_line = f"Issue #{n} parked: {reason} ({outcome.name})."   # unchanged from main
park_detail      = diag.describe() if diag.populated else ""          # "" on MERGE_CONFLICT (D7)
summary = (
    f"{park_reason_line}{_PARK_DETAIL_SEP}{park_detail}"
    if park_detail
    else park_reason_line
)
```

The empty-detail branch must drop the separator entirely — no trailing
whitespace (this is the same requirement D4 states for degradation, expressed
as code shape).

**Requirement 2 — the same two-part split for `parked_reasons`**: a base
`f"CI gate: {outcome.name}"` plus an optional ` ({classifier})` suffix (D6),
composed the same way.

**Declared contract for `park_issue()` when #351 builds it:** it accepts an
optional `detail: str = ""` parameter alongside its `reason`, and composes the
alert body as `reason` + `_PARK_DETAIL_SEP` + `detail`, dropping the separator
when `detail` is empty. Routing site 16 then becomes
`park_issue(..., reason=park_reason_line, detail=park_detail)` — a mechanical
move with nothing to re-derive and nothing to drop.

**The concrete failure this prevents.** If `_run_ci_gate` instead builds one
opaque enriched string, the #351 implementer routing site 16 has two options,
both bad:

- pass the whole enriched blob as `reason` — which then flows into
  `parked_reasons` and **re-opens D6's `park_kind` substring hazard**, because
  operator-supplied check names would now be inside the park reason; or
- drop the enrichment to keep `reason` short — silently undoing #353.

Requirement 1 is what makes the third option (keep both, move both) the
obvious one. That is why this is a design requirement on #353 and not a
stylistic preference.

---

## 5. Sequenced tasks

Test-first split applies at implementation time (router policy); the tests
named in each task are the failing-test contract, not an afterthought.

### T1 — `CiDiagnostic` + accumulation in `evaluate_ci` (`src/baton_harness/chain/merge.py`)

Per D1 and D3. Three pieces:

1. The `CiDiagnostic` dataclass, `_DIAG_NAME_CAP`, `classifier()`,
   `describe()`.
2. A **pure** `_build_ci_diagnostic(required, ever_observed, last_runs, polls,
   elapsed_s, *, red: bool) -> CiDiagnostic` — no I/O, no time calls, fully
   unit-testable in isolation.
3. `evaluate_ci` gains `diagnostic: CiDiagnostic | None = None`; captures
   `start = time.monotonic()` alongside the existing deadline computation
   (`:461`); accumulates `polls`, `ever_observed`, and `last_runs` in the loop
   (`:463-467`); and, on the RED (`:471-472`) and TIMEOUT (`:476-478`,
   `:487-488`) exits only, mutates the caller's object in place from
   `_build_ci_diagnostic`. GREEN leaves it unpopulated — no consumer needs it.

**`_classify_check_runs` (`:352-407`) is not edited.** Its single direct test
reference (`tests/chain/test_merge.py`, one occurrence) is untouched.

Failing-test contract (`tests/chain/test_merge.py`):

- `_build_ci_diagnostic` produces each of D3's four classes, asserted on
  `classifier()` and on `never_observed` / `other_observed` membership.
- The `"observed earlier, absent from final poll"` state is produced when a
  name is in `ever_observed` but not in the final snapshot.
- **The #353 regression test:** `evaluate_ci` with `_query_action_jobs`
  returning `[]`, `timeout=0`, and a supplied diagnostic → `populated is
  True`, `never_observed == tuple(REQUIRED_CHECKS)`, `other_observed == ()`,
  `classifier() == "no jobs observed"`. This is the PR #6 scenario (F8).
- **The name-mismatch case:** only irrelevant jobs → `other_observed`
  contains that name, `classifier() == "required checks never appeared"`.
- **The absent-vs-pending distinction, asserted directly:** all four required
  jobs `in_progress` → `never_observed == ()` and every entry of `states` is
  `"in_progress/--"`, `classifier() == "required checks never completed"`.
  Contrast with the two tests above — this pair *is* the issue's core claim
  ("indistinguishable") being falsified.
- Partial case: two required jobs present-and-pending, two absent → both lists
  non-empty, `classifier() == "required checks partially missing"`.
- GREEN exit leaves `populated is False`.
- `diagnostic=None` (the default) → behaviour identical; the existing suite is
  the guard, and none of its assertions may be weakened.
- `polls` and `elapsed_s` are recorded and monotonic.
- `_DIAG_NAME_CAP`: >10 other job names → capped list plus a `"+K more"`
  marker; total rendered length bounded.

Existing tests to **extend, not replace** (their `result != CiResult.GREEN`
assertions are the no-vacuous-green guard and must survive verbatim):

- `test_absent_required_job_is_not_vacuous_green` (`test_merge.py:1024-1054`)
- `test_empty_jobs_list_is_not_vacuous_green` (`:1056-1073`)
- `test_in_progress_required_job_polls_then_times_out` (`:991-1022`)

Also in this task, per F6 — two docstring edits in the file T1 is already
touching: correct the stale "three actual CI check names"
(`test_merge.py:10-11`) to four, and extend the `Coverage:` enumeration
(`:48-60`) with the new diagnostic tests so the file's own inventory stays
true.

### T2 — thread `diagnostic=` through `merge_issue_branch`

Per D1. `merge_issue_branch` (`merge.py:491-504`) gains
`diagnostic: CiDiagnostic | None = None` and forwards it to `evaluate_ci`
(`:555-563`). Docstring updated; `Returns:` section unchanged.

**`merge_issue_branches` (`merge.py:729-799`) is deliberately not changed** —
it does not pass the kwarg, so its `evaluate_ci` calls leave the diagnostic
unpopulated, which is correct for a function with no message-emitting
responsibility. It also has **no production caller** (repo-wide grep,
2026-08-09: only its definition at `merge.py:729` and four references in
`tests/chain/test_merge.py:1680-1737`), so the omission has zero runtime
blast radius. Recorded here so it reads as a decision, not an oversight.

Failing-test contract: `merge_issue_branch` driven to a timeout with a
supplied diagnostic → populated; with no diagnostic → unchanged return and no
exception.

### T3 — enrich the park alert and `parked_reasons` (`gh_api_helpers.py`)

Per D4, D6, D7 and D8, in the non-MERGED branch (`:427-457`):

1. Construct `diag = CiDiagnostic()` before the `try` (`:367`) and pass
   `diagnostic=diag` in the `merge_issue_branch` call (`:368-380`).
2. Add the `_PARK_DETAIL_SEP` module constant and build the summary from the
   two named locals `park_reason_line` / `park_detail` **exactly as D8's
   Requirement 1 specifies** — this is a contract, not a style choice.
3. `parked_reasons[n] = f"CI gate: {outcome.name}"` plus the
   `({classifier})` suffix when populated (D8 Requirement 2).
4. Add D4's guarded degradation `_log.debug` — `{CI_FAILED, CI_TIMEOUT}`
   only, never on `MERGE_CONFLICT`.

Import `CiDiagnostic` from `baton_harness.chain.merge` alongside the existing
`REQUIRED_CHECKS, MergeOutcome` import (`:29`). It is a type, not a patched
seam, so it does **not** go through `_daemon_mod` (`:9-16`).

Failing-test contract (`tests/chain/test_daemon.py`):

- With a real `evaluate_ci` driven to timeout on an empty job list, the
  `alert` call's summary starts with the unchanged
  `"Issue #{n} parked: CI timed out (CI_TIMEOUT)."` **and** contains the
  case-1 headline and the per-check `never observed` lines.
- `parked_reasons[10] == "CI gate: CI_TIMEOUT (no jobs observed)"`.
- **Degradation guard:** with `merge_issue_branch` patched as a `MagicMock`
  returning `MergeOutcome.CI_TIMEOUT` (the shape used at
  `test_daemon.py:2728-2731`), the summary equals today's exact one-line
  string with no trailing whitespace and `parked_reasons[10] ==
  "CI gate: CI_TIMEOUT"`. This is the assertion that keeps the rest of the
  daemon suite green (F3, R-A).
- **D7 Decision A pin — drive a real `MERGE_CONFLICT`** (patch
  `merge.py::_run` so `evaluate_ci` reaches GREEN and the `git merge` step
  fails, per `merge.py:587-611`) and assert the alert summary and
  `parked_reasons[n]` are byte-identical to `main`, that `diag.populated is
  False`, and that no diagnostic block was appended. This is the test that
  makes D7's "by construction" claim checkable rather than merely argued —
  and the one that would catch a future change moving population to a
  non-GREEN-gated exit.
- **D8 Requirement 1 pin** — with a populated diagnostic, the summary equals
  `park_reason_line + _PARK_DETAIL_SEP + park_detail`; with an unpopulated one
  it equals `park_reason_line` with **no** trailing whitespace
  (`summary == summary.rstrip()`).

Doc-only edits in this task: the stale docstrings at `test_daemon.py:2677-2678`
and `:5122-5123` (F3) — correct "matches" to "begins with", and note that no
assertion depends on the pattern.

### T4 — durable escalation record (`gh_api_helpers.py`, `session_report.py` read-only)

Per D5 and D7 Decision B. Add the `record_escalation` call and the `datetime`
import. No `session_report.py` edit.

**The call is unconditional within the non-MERGED branch** — it fires for
`CI_FAILED`, `CI_TIMEOUT`, and `MERGE_CONFLICT` alike, and its `detail` is
**the same `summary` string already passed to `alert`**, not a separately
constructed one. Passing the identical variable is what makes D7 Decision B's
"cannot mislead" argument true at the code level rather than by convention;
an implementer who rebuilds the string here can break it silently.

Failing-test contract (`tests/chain/test_daemon_report_wiring.py`):

- After a CI-timeout park, `issues[].escalations` contains exactly one entry
  with `kind == "debug"`, `severity == "critical"`, and a `detail` carrying
  the headline; `totals.escalations` reflects it.
- **D7 Decision B pin** — after a `MERGE_CONFLICT` park, `escalations`
  contains exactly one entry whose `detail` equals the bare one-line summary
  and contains **no** diagnostic block, no check names, and no poll counters.
- The recorded `detail` is character-identical to the summary passed to
  `alert` (assert against the captured `alert` call args, not a re-derived
  expected string).
- **Schema guard:** the serialised `merge_gate` object has exactly the keys
  `{"outcome", "merged_sha", "ts"}` — the rejected-shape regression guard from
  D5.
- `park_kind` is `None` for a CI-gate park (D6's substring hazard, asserted
  rather than reasoned about).

### T5 — RED (`CI_FAILED`) detail *(separable — cut this task without affecting T1-T4)*

Populate `CiDiagnostic.failed` with `(name, conclusion)` pairs for required
checks whose conclusion is in `_FAIL_CONCLUSIONS` (`merge.py:347-349`) in the
final snapshot, and extend `describe()` / `classifier()` (`required check
failed`) accordingly. `_classify_check_runs` returns RED on the *first* failing
required check (`:398-399`), but the final snapshot may show several — report
all of them.

Rationale for including it: `"CI check failed (CI_FAILED)"` is the same defect
as `"CI timed out"` — an outcome with no subject — and the plumbing is already
built by T1-T3. Rationale for keeping it separable: it is not what #353 asks
for. See Q1.

**Do not** attempt to disambiguate the gate-exception path, which also returns
`MergeOutcome.CI_FAILED` (`gh_api_helpers.py:410`) — see R-D.

### T6 — docs (three surfaces, per F9)

- **`docs/harness-design.md`** § "CI green predicate (load-bearing
  definition)" (`:253-262`) — add a bullet stating that on RED/TIMEOUT the
  gate now captures a `CiDiagnostic` (which required checks were never
  observed vs. observed-but-incomplete, which other job names appeared, poll
  count, elapsed time), that it is surfaced in the park comment and in the
  session report's `escalations`, and that `_classify_check_runs` and the
  green predicate itself are **unchanged**. Correct the stale three-check list
  at `:257` to the four in `merge.py:119-124` (F6).
- **`docs/chain-orchestration-design.md`** — `:171` restates the NOT-YET →
  `CI_TIMEOUT` rule and `:147` the outcome fan-out. Add the same one-line
  diagnostic note so the two design docs do not diverge.
- **`docs/smoke-test-daemon.md`** — `:366` (the "no vacuous pass" paragraph)
  and the failure table row at `:372` ("No CI workflow at all") are the
  operator-facing description of exactly the #353 incident. Extend both to say
  what the park comment now reports. Also mention it at `:386`, where an
  operator is told to "inspect the daemon logs to see the exact outcome" —
  that instruction is now partly obsolete, since the outcome detail is in the
  issue comment.
  **Do not** touch `:386`'s `blocked`-label claim — F9 flags it as wrong but
  out of scope.
- No README change: this adds no env var, no command, and no label.

---

## 6. Acceptance criteria

1. A CI timeout where **zero** Actions jobs were ever observed produces a park
   comment naming that fact, the poll count, the elapsed time, and every
   required check as `never observed` — asserted by test against the PR #6
   shape (empty job list), not by inspection.
2. A CI timeout where all required checks were observed but stayed
   `in_progress` produces a **different** classifier and message from
   criterion 1 — the two are asserted in sibling tests, which is the literal
   falsification of the issue's "indistinguishable" claim.
3. A CI timeout where only non-required jobs appeared names those jobs and
   points at required-check name mismatch.
4. `_classify_check_runs` (`merge.py:352-407`) is **unmodified**, and every
   pre-existing `result != CiResult.GREEN` assertion in `test_merge.py` passes
   unchanged.
5. `src/baton_harness/chain/daemon/work_unit.py` is **not in the diff**.
6. With `merge_issue_branch` patched as a `MagicMock` (diagnostic unpopulated),
   the park alert summary and `parked_reasons` entry are byte-identical to
   `main`'s — asserted by test.
7. `parked_reasons[n]` contains no interpolated check name and no substring
   `block`; `park_kind` for a CI-gate park is `None`.
8. The session report's `issues[].escalations` contains the CI-gate park
   detail, character-identical to the summary passed to `alert`, for all
   three non-MERGED outcomes; `merge_gate` still serialises exactly
   `{outcome, merged_sha, ts}`.
8a. A `MERGE_CONFLICT` park's comment, `parked_reasons` entry, and escalation
    `detail` are byte-identical to `main` and carry no CI data — asserted by
    a test that drives a real conflict, not by argument (D7).
8b. The alert body is composed from two separately-addressable locals joined
    by `_PARK_DETAIL_SEP`, with the separator dropped when the detail is
    empty, so #351's future `park_issue(reason=…, detail=…)` can absorb it
    unchanged (D8).
9. No subprocess stderr appears in any field of `CiDiagnostic`.
10. All three docs that describe the CI-gate timeout —
    `docs/harness-design.md` § "CI green predicate",
    `docs/chain-orchestration-design.md:147`/`:171`, and
    `docs/smoke-test-daemon.md`'s "No CI workflow at all" failure row —
    describe the diagnostic; `harness-design.md:257` lists four required
    checks.
11. `ruff` + `mypy --strict` clean.

---

## 7. Risks

- **R-A — the diagnostic is unpopulated in the common test path, and an
  implementer discovers this as a wave of failures.** F3 establishes that most
  daemon tests patch `merge_issue_branch` with a `MagicMock` that swallows the
  `diagnostic=` kwarg. Mitigation: D4's explicit degradation requirement plus
  the dedicated guard test in T3. Without it, the enriched message would render
  as `"… (CI_TIMEOUT). \n\n"` or crash on an empty `states` tuple.
- **R-B — operator-supplied check names flow into a public GitHub comment.**
  Not an injection risk: `escalate` passes `--body summary` as an argv element
  with no shell (`escalation.py:160-172`). Residual risks are markdown
  mangling and unbounded length; mitigated by `_DIAG_NAME_CAP` (D3). Explicitly
  **not** mitigated by redaction, because the diagnostic never carries stderr
  (F5) — if a future change adds stderr, redaction becomes a hard prerequisite
  and this risk changes class entirely.
- **R-C — the `park_kind` substring hazard.** Closed by D6's fixed vocabulary,
  and only by it. If a future contributor "improves" `parked_reasons` by
  appending check names, a repo with a job named `Block scanner` silently
  mislabels its park. The AC-7 test is the durable guard.
- **R-D — `MergeOutcome.CI_FAILED` remains ambiguous.** It means *either* real
  CI-red *or* "the gate raised an exception" (`gh_api_helpers.py:410`). The
  exception path parks with `severity="warn"` and a different message shape
  (`:399-408`) and gets **no** diagnostic, because `CiAuthError`,
  `RuntimeError`, and `ValueError` all propagate past `evaluate_ci`
  (`merge.py:435-436`) before it can populate anything. Consequence to state
  loudly in the docs task: **a persistent 403 will not produce the new
  diagnostic.** Pre-existing wart, deliberately not fixed here.
- **R-E — the 30 minutes are still burned.** This change makes the failure
  legible, not fast. An operator still waits the full
  `_DEFAULT_TIMEOUT = 1800.0` (`merge.py:129`) before learning anything. If
  that is unacceptable, Q2 is the lever.
- **R-F — unbounded accumulation across polls.** `ever_observed` is a set of
  distinct job names over ~180 polls; bounded by the repo's job count, not by
  poll count. `last_runs` holds one snapshot. Negligible, stated so it is not
  re-derived in review.
- **R-H — multi-line comment bodies become routine on this path.** Today the
  CI-gate park comment is a single line; only the exception branch can produce
  a multi-line body (`gh_api_helpers.py:399-408` interpolating a multi-line
  `{exc}`). That precedent plus a Linux/systemd production host
  (`bin/install-daemon-service.sh`) makes `gh issue comment --body` with
  embedded newlines almost certainly fine — the body is a single argv element
  with no shell (`escalation.py:160-172`). Stated rather than assumed because
  the entire value of this change is the body rendering correctly; the T3 test
  that asserts on the multi-line summary is the practical check.
- **R-G — collision with #351, now mitigated by contract rather than by
  sequencing alone.** #351's T5 routes `gh_api_helpers.py:427-456` (its "site
  16") through a new `park.py` helper
  (`docs/superpowers/plans/2026-08-08-park-path-label-restoration-351.md:670-678`).
  The two changes do not conflict semantically — #351 owns *labels and
  counters*, #353 owns *message content* — but they edit adjacent lines, and
  the enrichment can be silently dropped in the move. Mitigations, in order of
  strength: (1) **D8's pre-declared `reason` / `detail` split**, which makes
  carrying the enrichment through `park_issue()` mechanical and makes dropping
  it a visible deletion rather than an omission; (2) the agreed sequencing —
  #353 lands first, and it has no dependency on `park_issue()`. Residual: if
  #351 builds `park_issue()` without a `detail` parameter despite D8, the
  implementer hits the two-bad-options fork D8 describes. The AC-8b test is
  what fails loudly at that point.

---

## 8. Open questions for the user

1. **Should the RED (`CI_FAILED`) extension (T5) be in scope?** Recommended
   **yes**: `"Issue #N parked: CI check failed (CI_FAILED)."` has the same
   defect #353 describes — an outcome with no subject — and T1-T3 already build
   every mechanism it needs, so the marginal cost is one dataclass field and
   one message branch. Recommended as its own task precisely so you can cut it
   without touching anything else.

2. **Should `evaluate_ci` gain a fail-fast exit when zero jobs have been
   observed for K consecutive polls?** This, not the diagnostics, is what would
   have saved the 30 minutes in the PR #6 incident. It is deliberately excluded
   here because it changes gate *behaviour* and carries a false-negative risk
   (GitHub can take a while to register a workflow run, so a too-eager K would
   park healthy PRs). The safety direction is favourable — failing fast to
   TIMEOUT never produces a vacuous merge — so it is a real candidate for a
   sibling issue with a generous K (e.g. 5 minutes of zero-run polls). Want it
   filed?

3. **Should the preflight check be filed?** The #353 investigation
   recommended verifying that a target feature branch's tree actually contains
   a workflow defining the required check names, before dispatching issues
   against it (`doctor.py` / `launch_gate.py`, alongside the existing
   branch-protection preflight). That would have prevented the incident
   entirely rather than diagnosing it. Structurally different from #353;
   file separately?

4. ~~**Ordering against #351.**~~ **RESOLVED 2026-08-09** (router + user, in
   response to the `project-reviewer` pass): the plans stay **separate** and
   **#353 lands first** — it has no dependency on #351's not-yet-built
   `park_issue()`. The coordination gap is closed by contract rather than by
   merging plans: see **D8**, which pre-declares the `reason` / `detail`
   split on #353's side so #351's `park_issue()` has a defined slot to accept
   it from day one. #351's plan file is deliberately **not** edited. No user
   input needed; recorded here so the resolution is not re-litigated.

5. **How much of the raw snapshot is worth keeping?** The plan captures job
   names, `status`, `conclusion`, poll count, and elapsed time. The API also
   returns `html_url` per job and `workflow_id` / `event` per run
   (`merge.py:294-341` fetches both payloads). A direct link to the failing job
   would be genuinely useful in the RED case (T5) — but it is a URL built from
   third-party data flowing into a public comment, and it grows the message.
   Include job `html_url` for failed checks, or keep names only?

---

## 9. Citations index

Claims in this plan are backed by the following, each read directly on
2026-08-09 against `main` @ `4128e90`:

- `src/baton_harness/chain/merge.py` — `:1-88` (module docstring, incl. the
  "no vacuous green" contract at `:30-46`), `:119-124` (`REQUIRED_CHECKS`),
  `:126-129` (poll defaults), `:137-148` (`CiResult`), `:151-166`
  (`MergeOutcome`), `:229-341` (`_query_action_jobs`), `:344-349` (conclusion
  sets), `:352-407` (`_classify_check_runs`), `:415-488` (`evaluate_ci`),
  `:491-631` (`merge_issue_branch`; the RED/TIMEOUT early returns at
  `:565-568` and the conflict path at `:587-611` are what make D7's
  "`MERGE_CONFLICT` is unpopulated by construction" claim checkable),
  `:729-799` (`merge_issue_branches` — definition only; no production caller)
- `src/baton_harness/chain/daemon/gh_api_helpers.py` — `:1-45` (module
  docstring, imports, `_daemon_mod` seam rule), `:265-293`
  (`_effective_required_checks`), `:296-457` (`_run_ci_gate` in full)
- `src/baton_harness/chain/daemon/work_unit.py` — `:1150-1204` (convergence
  CI-gate call + `record_merge_gate`), `:1289-1313` (the alert +
  `record_escalation` pairing convention), `:1316-1346` (`pr_created` CI-gate
  call + `record_merge_gate`)
- `src/baton_harness/chain/session_report.py` — `:58-70` (`MergeGateRecord`),
  `:73-87` (`EscalationRecord`), `:90-120` (`IssueRecord`), `:292-314`
  (`record_merge_gate`), `:316-342` (`record_escalation`), `:344-366`
  (`set_outcomes`, incl. the `park_kind` substring derivation at `:366`),
  `:460-479` (`totals`), `:512-558` (`_issue_to_dict`)
- `src/baton_harness/chain/escalation.py` — `:140-208` (`escalate`'s argv
  `gh issue comment --body summary` and the Slack fan-out), `:211-312`
  (`alert`: signature, `kind` semantics at `:219`/`:250-251`, runlog emission
  at `:271-283`, severity routing at `:290-312`)
- `src/baton_harness/scenario/verify.py` — `:134-143` (recognised expectation
  keys), `:186-208` (`park_kind` exact match, `park_reason_present`
  presence-only)
- `docs/harness-design.md` — `:253-262` (§ "CI green predicate"; the stale
  three-check list is at `:257`)
- `docs/chain-orchestration-design.md` — `:3` (states it is a live companion
  to `harness-design.md`), `:147`, `:171`
- `docs/smoke-test-daemon.md` — `:366`, `:372` (the "No CI workflow at all"
  failure-table row), `:376`, `:386`
- `config/WORKFLOW.md` — grep for `required_checks`, 2026-08-09: **zero hits**
  (this repo runs on the hardcoded fallback)
- `tests/chain/test_merge.py` — `:1-60` (module docstring; stale three-check
  claim at `:10-11`), `:988-1073` (`TestEvaluateCiPollingAndTimeout`, incl.
  the three tests T1 extends)
- `tests/chain/test_daemon.py` — `:1354-1359` (block-escalation absence
  assertion), `:2665-2754`
  (`test_ci_gate_failed_park_routes_through_alert_severity_critical`; the
  claimed pattern is docstring-only at `:2677-2678`, the assertion is at
  `:2745-2754`), `:5102-5244`
  (`test_converged_no_pr_result_ci_failed_parks_coherently`; asserts no park
  reason)
- `tests/chain/test_daemon_report_wiring.py` — `:754-801` (S7 signature pin on
  `_run_ci_gate`'s return type), `:474-478` (escalations assertions)
- `tests/chain/test_session_report.py` — `:228-275`
  (`test_totals_aggregation_reflects_recorded_state`; the
  `totals["escalations"] == 1` pin at `:274` is a pure unit test, unaffected)
- `docs/superpowers/plans/2026-08-08-park-path-label-restoration-351.md` —
  `:110-112` (no redaction helper exists), `:259-277` (F5, the
  stderr-in-public-comment surface), `:527-548` (D5, the `park_kind`
  substring pin), `:670-678` (T5, the `park.py` routing that creates R-G)
- Issue [#353](https://github.com/glitchwerks/baton-harness/issues/353) —
  **provenance caveat:** `mcp__github__*` tools were not available in the
  session that produced this plan, so the issue was retrieved via `WebFetch`
  on 2026-08-09 as a *model-generated summary* of the rendered page, not
  verbatim body text. The summary supports every claim this plan attributes to
  it (notably the three named failure points and the "operators cannot
  distinguish" impact statement), but F1's characterisation of the issue's
  proposed fix should be re-checked against the verbatim body before merge if
  it matters to the reviewer.

Ephemeral source, restated inline rather than relied upon (F8):
`.tmp/2026-08-09-investigator-pr6-incomplete.md` — `:1-29` (failure + root
cause), `:54-92` (evidence chain: comment timeline, zero workflow runs, branch
dating), `:112-134` (the two ruled-out hypotheses that motivate D3's
classification), `:141-155` (the preflight follow-up recommendation behind Q3).
`.tmp/` is gitignored, so this file will not survive; do not cite it from any
committed artefact.

**Stated negative results** (these are what make the `touches:` list
defensible — each was searched for and *not* found):

- Repo-wide grep for `CI gate:` / `CI timed out` / `park_reason` /
  `CI_TIMEOUT` outside `tests/` (2026-08-09): no consumer matches either park
  string exactly; the only producer is `gh_api_helpers.py:439-446` (F9).
- `tests/` grep for the same strings: the only hits are two **docstrings**
  (`test_daemon.py:2677-2678`, `:5122-5123`) and one unrelated literal
  (`test_session_report.py:153`). No assertion pins a park string (F3).
- `tests/scenario/` contains only `test_expectations.py` and `__init__.py`
  (Glob, 2026-08-09) — no fixture files that could pin a park reason.
- `config/WORKFLOW.md` grep for `required_checks`: zero hits.
- `gh_api_helpers.py:19-33`: no `datetime` import today (T4 adds it).
- Repo-wide grep for `merge_issue_branches` (2026-08-09): **no production
  caller** — only its definition (`merge.py:729`) and
  `tests/chain/test_merge.py:1680`, `:1688`, `:1706`, `:1737`. This
  downgrades the D1 cost-table row from a production-API concern to test
  maintenance, and it is why T2 leaves the function unchanged with zero
  runtime blast radius.

Unverified claims, marked as such in-place: none. Flagged-but-unfixed:
F7 (`evaluate_ci(required=[])` is vacuously GREEN — unreachable in production,
not fixed by this plan).
