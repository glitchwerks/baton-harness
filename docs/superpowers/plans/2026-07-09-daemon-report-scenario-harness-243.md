---
title: Daemon session-report + init-sandbox scenario harness (issue #243)
touches:
  - src/baton_harness/chain/session_report.py
  - src/baton_harness/chain/daemon.py
  - src/baton_harness/chain/cli.py
  - src/baton_harness/chain/reconcile.py
  - src/baton_harness/chain/escalation.py
  - src/baton_harness/scenario/__init__.py
  - src/baton_harness/scenario/verify.py
  - src/baton_harness/scenario/expectations.py
  - bin/init-sandbox.sh
  - bin/verify-scenarios.sh
  - bin/verify-recovery.sh
  - bin/verify-block-escalation.sh
  - .github/workflows/scenario-smoke.yml
  - .github/workflows/scenario-harness.yml
  - tests/chain/test_session_report.py
  - tests/chain/test_cli_report.py
  - tests/chain/test_daemon_report_wiring.py
  - tests/scenario/test_expectations.py
  - config/WORKFLOW.md
  - README.md
  - docs/smoke-test-daemon.md
  - docs/harness-design.md
skills_relevant:
  - python
  - claude-github-tools:github-actions
---

# Daemon session-report + init-sandbox scenario harness (#243)

## Provenance note

This plan's code claims are grounded in `file:line` citations verified by reading
`main` at commit `f8b29df` on 2026-07-09. The **issue #243 scope statement** is taken
from the router dispatch brief; I had no `gh`/GitHub tool available in this planning
session, so the issue body itself was **not independently fetched** — verify against
`gh issue view 243` before executing if the brief and issue have diverged.

The five "settled decisions" from the brief are treated as fixed constraints and are
**not** re-litigated here:

1. Full replace of #239 / PR #241 — retire `bin/verify-block-escalation.sh`.
2. Daemon session report is a first-class observability feature, and is the assertion
   substrate.
3. Three-identity security model (Admin / App-installation / Executor-PAT) is sacred;
   seeding `agent-ready` is an operator/triage action and MUST stay a separate
   privileged step distinct from the daemon run.

---

## 1. Problem & goal

The retired verifier scripts (`verify-block-escalation.sh` on the #241 branch;
`verify-recovery.sh` in `main`) assert daemon behaviour by **grepping ephemeral daemon
stderr** — `verify-recovery.sh:33-46` documents this explicitly ("Assertions grep the
captured daemon stderr for these log lines"). That is brittle: stderr wording is not a
contract, the runlog JSONL is best-effort and un-grouped (`tick_id` is always `None`),
and there is no structured record of what a daemon session actually did.

The goal is a **daemon session report** — a structured JSON artifact the daemon emits
describing everything it did in a run (issues picked up, label transitions, PRs opened,
block/escalation events, merge-gate outcomes, tick summaries) — plus a **scenario
harness** that seeds parameterized sandbox scenarios (operator identity) and asserts
per-scenario expectations against that report. The report is useful on its own for
ops/observability; the harness is the test consumer.

---

## 2. Architecture decisions (recommendations + rationale)

### 2.1 Emit point — session envelope in the `run_daemon` `finally` block (single write), plus incremental per-tick accumulation

**Recommendation:** create a mutable `SessionReport` object once near the top of
`run_daemon` (right after obs init, `daemon.py:2180`), thread it by reference down
through `_poll_and_run` → `_run_work_unit`, mutate it incrementally as events happen,
and **write it exactly once in the `finally` block** at `daemon.py:2322`.

Rationale:

- The `finally` at `daemon.py:2322` is reached by **both** `--once` (via `if once:
  break` at `daemon.py:2318`) **and** SIGTERM (the handler raises `SystemExit(0)` at
  `daemon.py:2232`, which unwinds through the `try`/`finally`) **and** normal shutdown.
  A single write there covers continuous mode + `--once` with one code path. The
  existing SIGTERM recovery test relies on this same finally running (`verify-recovery.sh`
  scenario SIGTERM asserts the marker is unlinked in the finally at `daemon.py:2330`),
  so the path is already proven to execute under SIGTERM.
- `runlog`, `obs`, `tally`, `registry` are all in scope at the finally
  (`daemon.py:2322-2332`), so the report write needs no new plumbing there.
- Per-tick summaries are captured **incrementally** (append one entry to
  `report.ticks` at the end of each outer-loop iteration, after the `for repo_cfg`
  loop and before `if once: break`), so a long-running continuous session that is
  SIGTERM'd still has a complete tick history in the single final write.

**Rejected alternative — emit at end of every tick to disk.** Continuous mode ticks
every `poll_interval_s` (default 10s per `merge.py:118`); re-serialising and rewriting
the whole report every tick is wasteful and races a concurrent reader. In-memory
accumulation + one final write is simpler and is the same best-effort discipline the
runlog already uses.

**Best-effort discipline:** the finally-block report write MUST be wrapped in
`try/except` that never raises (mirroring `RunLog.emit` at `runlog.py:117-142` and the
finally's existing swallow at `daemon.py:2331`). A report-write failure must never mask
the real exit reason or crash shutdown.

**Atomic write (M9).** Unlike `RunLog`, which appends to a growing JSONL stream via
`open(path, "a")` (`runlog.py:74`), the report is a **single-file oracle** — a crash
mid-write would truncate the one artifact the whole harness asserts against.
`SessionReport.write(path)` therefore writes to a sibling temp file
(`<path>.tmp.<pid>`) and `os.replace(tmp, path)` (atomic on POSIX; also atomic on the
Windows dev host for same-directory replace). The temp+replace is still wrapped in the
best-effort `try/except` so a filesystem failure never raises into the finally.

### 2.2 Relationship to the runlog — the report is a NEW summary artifact built from an in-memory accumulator, not a re-parse of the JSONL

**Recommendation:** the `--report` file is authored from the in-memory `SessionReport`
accumulator (source of truth). Do **not** derive it by re-reading and aggregating
`runlog.jsonl`. Additionally emit **one** new terminal runlog event — `daemon_stop` —
carrying the summary counts, so the JSONL stream gains a self-contained session envelope
(today "No `daemon_stop`/session-envelope/tick-summary event exists" per the Explore
map, and `daemon_start` at `daemon.py:2181-2191` has no matching close).

Rationale for not re-parsing the JSONL:

- `RunLog.emit` is best-effort and can silently drop lines (`runlog.py:133-141`), so the
  JSONL is not a reliable complete record.
- `tick_id` is **always `None`** at every call site (Explore map; confirmed for the
  `daemon_start` event at `daemon.py:2189` and the `escalation` event at
  `escalation.py:286`), so you cannot group JSONL events by tick after the fact.
- The escalation runlog event omits `kind` (`escalation.py:278-288` emits `ts, event,
  issue, outcome, severity, detail, tick_id` — no `kind`), so a block-vs-other
  escalation cannot be reconstructed from the JSONL. The accumulator captures `kind` at
  the call site where it is in scope (see §2.4).

So: in-memory accumulator → `--report` JSON (authoritative) **and** a `daemon_stop`
runlog line (self-contained stream envelope). This is "aggregate + augment," and the
brief's "recommend one" is answered: **the report is the new summary; the runlog gains
exactly one new envelope event, not a full re-emit.**

**`daemon_stop` event schema (S8).** The event keeps the seven-key runlog record shape
(`runlog.py:8-30`) and adds one `counts` key so a stream-only parser needs nothing but
the JSONL:

```json
{
  "ts": "2026-07-09T21:03:12.000000+00:00",
  "event": "daemon_stop",
  "issue": null,
  "outcome": null,
  "severity": "info",
  "detail": "daemon stopped (exit_reason=once_complete)",
  "tick_id": null,
  "counts": { "ticks": 1, "issues_picked_up": 3, "prs_opened": 1, "issues_merged": 1, "issues_parked": 1, "issues_skipped_blocked": 1, "escalations": 1, "merge_gate_failures": 0 }
}
```

`counts` is exactly the report's `totals` dict (§3). `RunLog.emit` writes the dict
verbatim (`runlog.py:117-135`), so the extra key needs no runlog change. Emit `daemon_stop`
immediately before the report write in the finally, both best-effort.

### 2.3 CLI surface — `--report <path>`, resolved absolute pre-`chdir`

**Recommendation:** add `--report PATH` to the argparse block after `daemon.py`/`cli.py`
`--poll-interval` (`cli.py:243-252`). Resolve it to an absolute path **before** the
`os.chdir(project_root)` at `cli.py:333`, exactly as `--workflow` is resolved at
`cli.py:258-262` (the comment there — "Resolve ... to ABSOLUTE before any chdir so the
path survives a working-directory change later" — is the precedent to mirror). Forward
the resolved path to `run_daemon(..., report_path=...)` at `cli.py:397-405`.

Default when `--report` is omitted: **always-on** (RESOLVED — Decision 3). The daemon
always writes `${BH_PROJECT_ROOT}/.baton-harness/session-report.json` (alongside the
runlog, whose default is `${BH_PROJECT_ROOT}/.baton-harness/runlog.jsonl` per
`obs_config.py:124` / `BH_RUNLOG_PATH` override); `--report <path>` overrides only the
location, never disables emission. Observability-first: a report is written every run.
The scenario harness passes an explicit `--report` to a scratch path.

### 2.4 Surfacing per-issue outcomes to the report — reuse the existing single-source-of-truth dicts, do NOT add 16 per-site calls

`merged_issues` (list) and `parked_reasons` (dict) are local to `_run_work_unit`
(`daemon.py:1208-1209`) and currently feed only the PR body (`daemon.py:2041-2046`).
The per-repo tick is wrapped in a defensive `try/except` at `daemon.py:2283-2316` that
must keep the always-on daemon alive when a work unit blows up.

**Correction (B2 — do not record park per-site).** The park logic in `_run_work_unit` is
**not** one block — there are ~16 hand-copied park sites that write into `parked_reasons`
(`daemon.py:953, 991, 1318, 1343, 1404, 1440, 1503, 1558, 1609, 1660, 1684, 1707, 1788,
1936, 1964, 2013`). Adding a `record_park` call at each of those 16 sites would be a 9th
duplicated per-site concern and is rejected. Instead, **aggregate the report's per-issue
outcomes from the `parked_reasons` / `merged_issues` dicts that already are the single
source of truth** — read them once at the end of `_run_work_unit` (the same place the PR
body reads them, `daemon.py:2041-2046`) and fold them into the report:

- For each `n` in `merged_issues` → set that issue's `outcome="merged"`.
- For each `n, reason` in `parked_reasons` → set `outcome="parked"`, `park_reason=reason`.
  The park `reason_text` already distinguishes a block from a plain failure
  (`daemon.py:1996-2002`), so the block vs non-block classification comes from data the
  daemon already computed.

**Mid-throw behaviour (explicit).** If a work unit throws mid-tick, `_run_work_unit`'s
end-of-function aggregation never runs — but the per-repo `try/except` at
`daemon.py:2295` catches the throw, and the report is then built from **whatever
`parked_reasons` / `merged_issues` held at the throw point** via a `finally` inside
`_run_work_unit` (or by having the `except` at `daemon.py:2295` read the partial dicts).
Recommendation: wrap the aggregation in a `finally` inside `_run_work_unit` so partial
outcomes land even on a mid-unit exception, and record a `tick_error` entry
(`{repo, error_str}`) in the `except` at `daemon.py:2295`. No outcome is lost, and no
per-site duplication is introduced.

The remaining report fields come from a **small number of already-single-site** hooks
(not the 16-way park logic):

- Issue picked up / dispatched → at `_launch_one_issue` (`daemon.py:276`): record
  `issue.number`, title, `picked_up_at`.
- Label edits (daemon-attested only) → at `_label_edit` (`daemon.py:519`, the daemon's
  **sole** writer, and it only ever writes `agent-in-progress` / `agent-done` /
  `agent-merged` — never `blocked`): append a `label_transition` entry. See §2.7 for why
  `blocked` can never appear here.
- PR opened → `_open_pr` (`daemon.py:1011`) **must be changed to return the PR URL**
  (S7): it currently is `-> None` and discards `proc.stdout`, which is where `gh pr
  create` prints the URL. Change its signature to `-> str | None` returning
  `proc.stdout.strip() or None`, and record `pr.url` / `opened_at` from the return value
  at its single call site. (Named as Phase 2 scope.)
- Merge-gate outcome → `_run_ci_gate` (`daemon.py:857`) is `-> None` today; it is where
  the `MergeOutcome` (`merge.py:141-154`) is known. **Decision (S7): have `_run_ci_gate`
  return the `MergeOutcome` to `_run_work_unit`**, which records `merge_gate.outcome` (and
  the merged SHA, §S5 / §3) — rather than threading the `report` object all the way down
  into the merge gate. This keeps the report-mutation surface in `_run_work_unit` and off
  the merge internals.
- Escalation (daemon-attested) → at each `escalation.alert(...)` call site the daemon
  already holds `kind` in scope (e.g. `daemon.py:2014-2023`), so record
  `{ts, severity, kind, detail}` per issue **at the daemon call site** (not from the
  runlog event, which drops `kind` — `escalation.py:278-288`). This is the trustworthy,
  non-forgeable block signal (§2.7 / §S4).

**Why this preserves isolation:** the report is created in `run_daemon` scope and passed
by reference; per-issue outcomes are folded in from the authoritative dicts in a
`finally`, so a mid-tick throw caught at `daemon.py:2295` loses nothing. The report
object is never re-created per repo/tick, so cross-repo state accumulates correctly.
`_run_work_unit`'s return type for the daemon's own control flow is unchanged (its new
report-side effects happen through the passed-in report + the `_run_ci_gate` return
value).

### 2.5 Startup-reconciliation findings (G1/G2) vs fatal gates (G3) — split by whether the finally runs

This is the crux of "decide which move to report-based checks vs stay stderr-based."

`reconcile_startup` is called at `daemon.py:2212`, **before** the `try` whose `finally`
writes the report (`try` at `daemon.py:2270`, `finally` at `daemon.py:2322`). Therefore:

- **Fatal G3 gates** (`reconcile_startup` may `raise SystemExit` on bad creds —
  `daemon.py:2208-2211` documents this; `verify-recovery.sh` G3b/G3a assert non-zero
  exit before the poll loop) exit **before** the finally runs → **no report is emitted**.
  These MUST stay **stderr-based**. Keep them in `verify-recovery.sh`.
  - G3b — `ANTHROPIC_API_KEY` set → refuse (`verify-recovery.sh:399-435`).
  - G3a — bogus `GH_TOKEN` → refuse (`verify-recovery.sh:439-487`).
  - G3c — OAuth creds absent → skip/refuse (`verify-recovery.sh:283-289`).
- **Non-fatal startup findings** (G2 stale marker, G1 orphan `claude` procs) alert but
  the daemon continues into the poll loop and reaches the finally
  (`verify-recovery.sh:491-616` assert exit 0). These **can** be report-based: create
  the `SessionReport` **before** `reconcile_startup` and pass it in so reconcile records
  a `startup.findings[]` entry (`{gate: "G2"|"G1", detail, pids?}`). The finally then
  writes them.
- **SIGTERM** graceful shutdown reaches the finally (SystemExit(0) unwinds through it),
  so record `exit_reason="sigterm"` in the report; the marker-removed assertion stays a
  filesystem check.

**Recommendation:** move G1, G2, and SIGTERM assertions to report-based checks in the
new harness; keep G3a/G3b/G3c stderr-based in a slimmed `verify-recovery.sh` (they are
genuine startup-refusal paths with no report). Document this split loudly in both files.

To let reconcile populate the report, create `SessionReport` just after obs init
(`daemon.py:2180`) and pass it as a new kwarg into `reconcile_startup`
(`daemon.py:2212-2217`). Keep reconcile's report mutation best-effort (never raise).

### 2.6 `exit_reason` control structure (S6)

The outer loop is `try` (`daemon.py:2270`) / `finally` (`daemon.py:2322`) with **no
`except`**. A bare finally cannot tell `once_complete` from `sigterm` from
`keyboard_interrupt` from `exception` — they all just unwind through it. So `exit_reason`
must be set explicitly on each exit path, not inferred in the finally. Exact restructure:

- Introduce a mutable closure holder near the top of `run_daemon`:
  `_exit_reason: list[str] = ["exception"]` (default pessimistic — if none of the paths
  below fire, something threw uncaught and `"exception"` is correct).
- In the SIGTERM handler (`daemon.py:2226-2232`), set `_exit_reason[0] = "sigterm"`
  **before** `raise SystemExit(0)`.
- Immediately before `if once: break` (`daemon.py:2318`), set
  `_exit_reason[0] = "once_complete"`.
- Add `except KeyboardInterrupt: _exit_reason[0] = "keyboard_interrupt"; raise` **inside**
  the try/finally (between the `while` body and the `finally`), so Ctrl-C is
  distinguished and still propagates. (Note `cli.py:406-407` already swallows
  `KeyboardInterrupt` at the outer `asyncio.run` boundary; setting the reason before the
  re-raise means the finally has recorded it by the time cli.py logs the interrupt.)
- The `finally` reads `_exit_reason[0]` into `report.session.exit_reason` before the
  best-effort write.

**SIGTERM main-thread caveat (S6).** `signal.signal` is wrapped in
`try/except (OSError, ValueError)` at `daemon.py:2235-2239` and **silently degrades on a
non-main thread**. If a wiring test drives `run_daemon` in a background thread, the
SIGTERM handler is never installed → SIGTERM does not raise `SystemExit` through the
finally → **no report is written and `exit_reason` stays `"exception"`/unset**. Therefore
the recovery/SIGTERM scenario (and any test asserting `exit_reason="sigterm"`) MUST run
the daemon in the **main process/thread** (subprocess, as `verify-recovery.sh` already
does — `verify-recovery.sh:387-397` starts `bh-daemon` as a real background process, not
a Python thread). Document this in the harness and the test.

### 2.7 The block signal is daemon-attested, never `blocked`-label-observed (B1)

A block scenario must NOT be asserted from the `blocked` **label**. The daemon is not the
writer of `blocked` — it only **reads** it: `has_blocked = "blocked" in post_labels` at
`daemon.py:1791`, and `blocked` is in `_DISPATCH_EXCLUDE_LABELS` (`daemon.py:113`) purely
as a read-side dispatch filter. The **worker** (executor PAT) is what sets `blocked`. Two
consequences:

1. A `label_transitions` list built only from daemon `_label_edit` calls (`daemon.py:519`,
   the daemon's sole label-write path) **can never contain `added:["blocked"]`** — so a
   `block-ambiguity` expectation that requires it is permanently unsatisfiable.
2. Reading `blocked` from live GitHub label state would make the oracle **worker-forgeable**
   (a worker could set `blocked` to fake a pass, or fail to set it to fake nothing) —
   oracle poisoning.

**Fix:** key the block oracle off **daemon-attested signals only**:

- `issue.outcome == "skipped_blocked"` (daemon's own dispatch-exclude decision, computed
  from the read at `daemon.py:1791` / the exclude filter at `daemon.py:113`), **or** a
  park whose `kind == "block"`; and
- an `escalations[]` entry with `kind == "block"`, recorded at the daemon `alert()` call
  site (§2.4), where `kind` is the daemon's own argument — not the worker's.

The `blocked` label may still appear in a `label_transition` **removed** set when the
daemon clears `agent-in-progress` etc., but no assertion keys on `added:["blocked"]`. All
schema examples and expectations below reflect this.

---

## 3. Report JSON schema (concrete, `schema_version: 1`)

Emitted by `SessionReport.to_dict()` in the new `session_report.py`. All timestamps are
ISO-8601 UTC strings (same format as the runlog `ts`, `daemon.py:2183`).

```json
{
  "schema_version": 1,
  "session": {
    "started_at": "2026-07-09T21:00:00.000000+00:00",
    "ended_at": "2026-07-09T21:03:12.000000+00:00",
    "mode": "once",
    "exit_reason": "once_complete",
    "poll_interval_s": 10.0,
    "registry": [{ "owner": "glitchwerks", "repo": "sandbox" }]
  },
  "startup": {
    "findings": [
      { "gate": "G2", "detail": "Prior daemon run ended ungracefully", "pids": null },
      { "gate": "G1", "detail": "Orphan claude processes detected at startup", "pids": [12345] }
    ]
  },
  "totals": {
    "ticks": 1,
    "issues_picked_up": 3,
    "prs_opened": 1,
    "issues_merged": 1,
    "issues_parked": 1,
    "issues_skipped_blocked": 1,
    "escalations": 1,
    "merge_gate_failures": 0
  },
  "issues": [
    {
      "number": 42,
      "repo": "glitchwerks/sandbox",
      "title": "add a greet() function",
      "picked_up_at": "2026-07-09T21:00:05.000000+00:00",
      "label_transitions": [
        { "ts": "...", "added": ["agent-in-progress"], "removed": ["agent-ready"] },
        { "ts": "...", "added": [], "removed": ["agent-in-progress"] }
      ],
      "outcome": "parked",
      "park_reason": "self-block: requirements ambiguous",
      "park_kind": "block",
      "pr": null,
      "merge_gate": null,
      "escalations": [
        { "ts": "...", "severity": "warn", "kind": "block", "detail": "Issue #42 parked: ..." }
      ]
    }
  ],
  "ticks": [
    {
      "tick_index": 0,
      "started_at": "...",
      "ended_at": "...",
      "issues_processed": [42],
      "error": null
    }
  ]
}
```

Field notes:

- `session.mode`: `"once"` when `--once`, else `"continuous"`.
- `session.exit_reason`: one of `once_complete`, `sigterm`, `keyboard_interrupt`,
  `exception`. Set explicitly on each exit path via the `_exit_reason` closure holder,
  **not** inferred in the bare finally — see §2.6 for the exact restructure and the
  SIGTERM-on-non-main-thread caveat.
- `issue.outcome` vocabulary: `merged`, `parked`, `pr_open`, `no_pr`, `skipped_blocked`
  (the last covers the mid-drain dispatch-exclude decision the daemon makes when it reads
  `blocked` at `daemon.py:1791` / the exclude filter at `daemon.py:113` — a
  **daemon-attested** decision, not the label itself; see §2.7). Derived from the
  authoritative `merged_issues` / `parked_reasons` dicts (§2.4), never from a live GitHub
  label read.
- `issue.park_kind`: the `kind` argument the daemon passed to `alert()` on the park path
  (`daemon.py:2014-2023`) — `"block"` distinguishes a self-block from a plain failure.
  Daemon-attested.
- `issue.merge_gate`: `{ "outcome": "<MergeOutcome name>", "merged_sha": "<sha>|null",
  "ts": "..." }`. `outcome` is the `MergeOutcome` enum name verbatim
  (`merge.py:141-154`), returned by `_run_ci_gate` (§2.4 / S7).
- `issue.merge_gate.merged_sha` (S5): the commit SHA the merge produced (or `null` when
  not `MERGED`). **`outcome="MERGED"` attests only that the merge command returned 0**,
  not that the CI-green tree is what merged: `evaluate_ci` checks the head SHA
  (`merge.py:551`) but `merge_issue_branch` merges the branch **ref by name**
  (`merge.py:592`) — a TOCTOU window where a push between check and merge could merge a
  different tree. Recording `merged_sha` makes the attestation verifiable after the fact.
  This matters for #139, whose goal-marker will read `issue.outcome` / `merge_gate`.
- `escalations[].kind`: captured at the daemon `alert()` call site (§2.4); this is the
  daemon-attested block signal that replaces the retired verifier's stderr grep for
  `kind=block` (§2.7).

### 3.1 Per-field provenance boundary (S4)

Every report field is one of two provenance classes. A consumer (assertion harness, #139
goal-marker, ops dashboard) MUST NOT treat a **GitHub-observed** field as trustworthy the
way a **daemon-attested** field is — observed fields are worker-forgeable (the worker
holds the executor PAT and can write labels/comments/commits).

| Field | Provenance | Source |
|---|---|---|
| `session.*` (mode, exit_reason, poll_interval_s, timestamps) | **daemon-attested** | daemon control flow (§2.6) |
| `startup.findings[]` (G1/G2) | **daemon-attested** | `reconcile_startup` internal decision (§2.5) |
| `totals.*` | **daemon-attested** | aggregation of the fields below |
| `issue.number` / `title` / `picked_up_at` | daemon-attested (identity) | `_launch_one_issue` dispatch (`daemon.py:276`) |
| `issue.outcome` | **daemon-attested** | `merged_issues`/`parked_reasons` + dispatch-exclude decision (§2.4, §2.7) |
| `issue.park_reason` / `park_kind` | **daemon-attested** | daemon's own park `reason_text` / `alert(kind=...)` |
| `issue.label_transitions[]` | **daemon-attested** | daemon's `_label_edit` writes only (`daemon.py:519`); records only labels the daemon itself sets — never `blocked` (§2.7) |
| `issue.pr.url` | daemon-attested (the daemon ran `gh pr create` and captured its stdout) | `_open_pr` return (S7) |
| `issue.merge_gate.outcome` | **daemon-attested** | `_run_ci_gate` return (§2.4) |
| `issue.merge_gate.merged_sha` | daemon-attested, but see the MERGED-≠-green-tree TOCTOU caveat above (S5) | `merge_issue_branch` |
| `issue.escalations[].kind` | **daemon-attested** | daemon `alert(kind=...)` argument, not the runlog event |

There is deliberately **no** field sourced from a live `gh issue view --json labels` read
of `blocked` state — that would be the one worker-forgeable field, and §2.7 removes it.

---

## 4. init-sandbox scenario matrix

Extend `bin/init-sandbox.sh` with a scenario selector: `--scenario <name>` flag (env
fallback `BH_SCENARIO`), default `hello` = the current fixed seeding (trivial greet()
issue + `hello-feature` milestone A/B/C, `init-sandbox.sh:250-450`) for backward
compatibility. New scenarios seed a specific issue set with operator identity (ambient
`gh` auth, `init-sandbox.sh:147-152`).

| Scenario key | Seeds | Needs real agent? | Expected report shape |
|---|---|---|---|
| `clean-implement` | 1 trivial `agent-ready` issue (greet()) | **Yes** | issue `outcome=merged` (green stub CI) or `pr_open`; `merge_gate.outcome=MERGED` |
| `block-ambiguity` | 1 `agent-ready` issue with a deliberately ambiguous body that induces a self-block | **Yes** | **daemon-attested only** (§2.7): `outcome=parked` with `park_kind=block` **and** an `escalations[]` entry with `kind=block`. NO `added:["blocked"]` label assertion (the daemon never writes `blocked`). |
| `ci-fail` | 1 `agent-ready` issue on a repo whose stub CI fails for its branch (see §4.1) | **Yes** | `merge_gate.outcome=CI_FAILED`; issue NOT merged |
| `terminal-block` | 1 issue seeded with **both** `agent-ready` **and** `blocked` in a single `gh issue create --label agent-ready --label blocked` call (§4.3) | No (dispatch excluded) | exactly **one** `issues[]` entry with `outcome=skipped_blocked` (the daemon reads `blocked` at `daemon.py:1791` / excludes via `daemon.py:113` and never dispatches) |
| `recovery` | 0 `agent-ready` issues; pre-seed stale `daemon.alive` marker (G2) and/or a decoy orphan process is spawned by the harness (G1) | No agent | `startup.findings[]` includes G2 and/or G1; `exit_reason` per run |

The `recovery` scenario overlaps `verify-recovery.sh` G1/G2/SIGTERM; those move to the
report-based harness (§2.5). The fatal G3 gates stay in `verify-recovery.sh`.

**Empty-`issues[]` trap (B1).** `terminal-block` is the one no-agent scenario that still
dispatches nothing yet must produce a non-empty report. If the seed issue does not carry
`agent-ready` (only `blocked`), the daemon's `agent-ready` scan (`daemon.py:2408-2427`)
never even picks it up and `issues[]` is empty — the assertion would then be vacuously
"satisfied" by nothing. Seeding **both** labels ensures the issue is fetched by the
`agent-ready` scan, then excluded by the `blocked` filter, producing exactly one
`skipped_blocked` entry. The acceptance criterion is therefore `len(issues)==1 and
issues[0].outcome=="skipped_blocked"`, not merely "no dispatch happened."

### 4.1 CI-fail mechanism

The current stub `ci.yml` (`init-sandbox.sh:486-514`) has all three required jobs
(`Lint (ruff)`, `Test (pytest)`, `Type check (mypy)` — matching `merge.REQUIRED_CHECKS`
at `merge.py:110-114`) exit `0`. **RESOLVED (Decision 2):** for `ci-fail`, seed a
per-scenario `ci.yml` variant whose `Test (pytest)` job runs `exit 1` (the other two jobs
still exit `0`, so the failure is unambiguously the pytest check). The variant is selected
by the scenario key and committed + pushed by init-sandbox the same way the current stub
is (`init-sandbox.sh:516-526`). Keeping `Test (pytest)` as the single failing check makes
the expected `MergeOutcome.CI_FAILED` (`merge.py:141-154`) deterministic and lets the
report assertion name exactly which check went red.

### 4.2 Loud label-attach validation (the #242 lesson)

`gh issue create --label` can silently drop labels when the token lacks Issues:write —
init-sandbox currently verifies dependency edges (`init-sandbox.sh:402-419`) but does
**not** verify that labels actually attached after `gh issue create --label`
(`init-sandbox.sh:265-269`, `:306-311`, `:335-340`, `:436-441`). Add a
`_create_issue_checked` helper:

1. `gh issue create ... --label <L>` → capture URL, extract number.
2. `gh issue view <n> --repo <slug> --json labels --jq '.labels[].name'`.
3. If any requested label is **absent**, print a loud error naming the missing label and
   the likely cause (token lacks Issues:write) and `exit 1` immediately — matching the
   fail-loud style already used for the dependency-edge round-trip check
   (`init-sandbox.sh:411-419`).

Route all scenario issue creation through this helper so a silently-dropped label is a
setup-time failure, never a mysterious mid-run no-dispatch. The helper must accept and
verify **multiple** `--label` values (needed by §4.3).

### 4.3 `terminal-block` seeding — both labels in one create (B1)

Seed the `terminal-block` issue with **both** labels atomically:

```bash
_create_issue_checked --title "terminal-block scenario" \
  --body "..." --label agent-ready --label blocked
```

and `_create_issue_checked` (§4.2) then verifies **both** `agent-ready` and `blocked`
actually attached (the #242 silent-drop failure mode is doubly likely with two labels).
Rationale: the issue must be visible to the daemon's `agent-ready` scan
(`daemon.py:2408-2427`) so it is picked up, then excluded by the `blocked` dispatch
filter (`daemon.py:113`) so it becomes exactly one `skipped_blocked` report entry (see
the empty-`issues[]` trap in §4). Seeding `blocked` alone would make the issue invisible
to the scan and the report's `issues[]` empty.

---

## 5. Assertion harness

**Recommendation: implement in Python, not bash** — `src/baton_harness/scenario/verify.py`
(a `python -m baton_harness.scenario.verify` entry point) plus `expectations.py`
(per-scenario expected-report specs). Bash + JSON assertions are painful and **`jq` is
banned on this host's Git Bash** (CLAUDE.md § Shell), so a bash consumer would fight the
platform. A thin `bin/verify-scenarios.sh` wrapper orchestrates the operator-side steps
(seed → run daemon → invoke the Python asserter) so operators keep a single entry point.

Flow per scenario:

1. **Seed** (operator identity): `bin/init-sandbox.sh --scenario <key>`.
2. **Run the real daemon** with an explicit report path:
   `bh-daemon --once --workflow <WF> --report <scratch>/report.json`
   (or continuous + SIGTERM for the `recovery`/SIGTERM scenario).
   The daemon runs with its normal inherited `GH_TOKEN` (executor PAT) — **seeding was a
   separate privileged operator step**, honouring the three-identity model.
3. **Assert**: `python -m baton_harness.scenario.verify --scenario <key> --report <scratch>/report.json`
   loads the report, matches it against the scenario's expectation spec, prints
   `[PASS]`/`[FAIL]` per assertion, exits non-zero on any failure (CI-gateable).

### 5.1 Per-scenario expectation format

An expectation is a declarative dict keyed by scenario (in `expectations.py`), matched by
a small matcher in `verify.py`. Example (`block-ambiguity`):

```python
EXPECTATIONS = {
    # Block signal is DAEMON-ATTESTED only (§2.7): park_kind=block + an escalation
    # with kind=block. NO added:["blocked"] label assertion — the daemon never
    # writes that label, so it can never appear in a daemon-sourced transition.
    "block-ambiguity": {
        "issue": {
            "outcome": "parked",
            "park_kind": "block",
            "park_reason_present": True,
            "escalations_include": [{"kind": "block"}],
        },
    },
    "clean-implement": {
        "issue": {"outcome": ["merged", "pr_open"], "pr_present": True},
    },
    "ci-fail": {
        "issue": {"merge_gate": {"outcome": "CI_FAILED"}, "outcome_not": "merged"},
    },
    # Exactly one issue entry, skipped_blocked (§4 empty-issues[] trap).
    "terminal-block": {
        "issues_len": 1,
        "issue": {"outcome": "skipped_blocked"},
    },
    "recovery": {
        "startup": {"findings_include_gates": ["G2"]},  # and/or G1
    },
}
```

**Label-transition matching is ordered, not set-based (M12).** When an expectation does
assert on `label_transitions` (e.g. the daemon-attested `removed:["agent-in-progress"]`
clearing), the matcher checks the transition **sequence in order**, not as an unordered
set — an `agent-in-progress` set-then-cleared pair is a different fact from cleared-then-set,
and a set-membership check would pass both. The matcher walks `label_transitions` in
emission order and confirms the expected sub-sequence appears in that order.

### 5.2 Preserved assertion semantics (mapping from the retired scripts)

Block-escalation chain (from `verify-block-escalation.sh`, #239, per the Explore map),
now report-based:

| Retired stderr grep | Report-based check (daemon-attested only, §2.7) |
|---|---|
| `blocked` label present | **NOT reproduced as a label check** — the daemon never writes `blocked` (`daemon.py:1791` reads it). Replaced by `outcome=parked` + `park_kind=block` (a daemon-attested self-block) |
| `agent-in-progress` cleared | `label_transitions` (ordered) include `removed:[agent-in-progress]` — daemon-attested (`_label_edit`, `daemon.py:519`) |
| `escalate:.*issue #N.*kind=block` line | `escalations_include [{kind: block}]` on issue N — daemon-attested `alert(kind=...)` argument |
| ≥2 comments | `outcome=parked` + `park_reason_present` + non-empty `escalations[]`. **Coverage delta (M12):** the old count≥2 assert would catch a **silent GitHub comment-post failure** (comment never landed) because it read the live comment count; the report-based check does **not** — it asserts the daemon *attempted* the park + escalation, not that the comments actually posted. `escalate()` already logs a WARNING and returns `False` on a failed post (`escalation.py:180-193`); a future enhancement could record that returned `durable_landed` boolean into the escalation entry to close the gap. Flagged, not silently dropped. |
| conditional Slack | out of scope for the report (Slack is gated on `BH_SLACK_WEBHOOK_URL`, `escalation.py:204-211`); "not asserted," same as the retired script |

Recovery gates (`verify-recovery.sh`):

| Gate | Where it goes |
|---|---|
| G3a (bogus token → non-zero + msg) | **stays stderr-based** — no report emitted (startup refusal before finally, §2.5) |
| G3b (`ANTHROPIC_API_KEY` set → non-zero + msg) | **stays stderr-based** |
| G3c (OAuth creds absent → skip) | **stays stderr-based** |
| G3d (git-push credential helper) | **stays stderr-based** — see M10 below; `verify-recovery.sh` does **not** currently test it, and coverage is a flagged follow-up |
| G2 (stale marker → exit 0 + msg) | **report-based**: `startup.findings` gate G2 |
| G1 (orphan proc → exit 0 + msg + PID) | **report-based**: `startup.findings` gate G1 (with `pids`) |
| SIGTERM (continuous → exit 0 + marker removed) | **report-based** `exit_reason=sigterm` (must run in main process, §2.6) + filesystem marker check retained |

**M10 — G3d exists but is untested.** `reconcile.py` carries a **G3d** git-push
credential-helper gate (added by #219 / merged in #234 — `reconcile.py:7` header;
`cli.py`/preflight wires `gh auth setup-git`, cf. `init-sandbox.sh:154-165`). Neither
`verify-recovery.sh` nor the retired block script ever exercised G3d. Slimming
`verify-recovery.sh` to "G3a/G3b/G3c" as originally written would **codify that omission**.
Corrected: the stays-stderr-based set is **G3a, G3b, G3c, and G3d**, and the plan
explicitly flags that G3d has **no** test today — adding G3d coverage (report-based if it
reaches the finally, stderr-based if it is a startup refusal) is a **follow-up** item,
tracked as its own issue.

---

## 6. Phased implementation (TDD-first)

Each phase is an independently-mergeable PR off `main` (worktree per CLAUDE.md
§ Worktrees). Tests first in every phase.

**v1 vs follow-up (RESOLVED — Decision 4, refined by B3).** v1 ships the report feature +
wiring **and a real-daemon GitHub-Actions gate** for the two no-agent scenarios — so v1
genuinely validates daemon→report fidelity, not just a matcher against hand-written
fixtures (which would be test theater):

- **v1:** Phase 0 (tracking) → Phase 1 (`SessionReport` model) → Phase 2 (daemon wiring)
  → Phase 3 (CLI `--report`) → Phase 4a (init-sandbox: the two **no-agent** scenarios
  `terminal-block` and `recovery` + loud label validation) → Phase 5a (Python matcher +
  fixtures) → **Phase 5a-ci: a GitHub-Actions job that runs the REAL daemon
  (`bh-daemon --once`) against `terminal-block` and `recovery` and asserts on the emitted
  report.** These two scenarios dispatch **no agent** (no `claude -p`, no Anthropic
  OAuth), so they run on GitHub-hosted runners — see Phase 5a-ci for the exact
  preconditions (GH token for seeding, dummy OAuth cred file to clear the G3c startup
  gate, `ANTHROPIC_API_KEY` unset for G3b).
- **Follow-up:** Phase 4b (the three **real-agent** scenarios `clean-implement`,
  `block-ambiguity`, `ci-fail` + the `ci-fail` workflow variant) → Phase 5b (the gated
  self-hosted-Linux end-to-end seed→run→assert job). Only these three need a live
  `claude -p` agent + real OAuth creds and cannot run on GitHub-hosted runners, so they
  land after the report feature is already merged and usable for observability.

The report + wiring (Phases 1–3) is independently valuable for ops and merges on its own.
The distinction refined from Decision 1: **"gated self-hosted" applies only to the
real-agent scenarios**; the no-agent scenarios get a genuine real-daemon gate in ordinary
GitHub-Actions CI (B3).

### Phase 0 — GitHub tracking

- Confirm issue #243 exists and matches this plan's scope (`gh issue view 243`); if
  scope diverged from the router brief, reconcile before coding.
- Create a milestone (e.g. **Daemon session report + scenario harness**) grouping the
  phase issues below; file one issue per phase. **Creating issues is not permission to
  start** (CLAUDE.md § Issue Tracking) — confirm with the user before Phase 1.

### Phase 1 — `SessionReport` model (pure, no daemon wiring)

- **Files:** `src/baton_harness/chain/session_report.py`,
  `tests/chain/test_session_report.py`.
- `SessionReport` dataclass + `IssueRecord` (with `outcome`, `park_reason`, `park_kind`,
  `pr`, `merge_gate` incl. `merged_sha`, `escalations`, ordered `label_transitions`) +
  `TickRecord` + `StartupFinding`; methods `record_pickup`, `record_label_edit`,
  `record_pr`, `record_escalation`, `record_startup_finding`, `set_outcomes(merged_issues,
  parked_reasons)` (the B2 aggregation entry point — NOT a per-site `record_park`),
  `record_merge_gate(outcome, merged_sha)`, `begin_tick`/`end_tick`, `set_exit_reason`,
  `to_dict()`, `write(path)`.
- `write(path)` is **atomic + best-effort (M9)**: serialise, write to `<path>.tmp.<pid>`,
  `os.replace(tmp, path)`, all inside a `try/except` that never raises (mirror the
  never-raise contract of `runlog.py:117-142`, but single-file atomic rather than append).
- Tests: schema shape incl. provenance-relevant fields; totals aggregation; idempotent
  issue upsert by number; `set_outcomes` folds `merged`/`parked`+`park_kind` correctly;
  ordered `label_transitions`; best-effort write to an unwritable path does not raise; a
  crash between temp-write and replace leaves the prior report intact (atomicity).
- **No daemon changes yet** — fully unit-testable in isolation.

### Phase 2 — daemon wiring (report accumulation + finally emit + `daemon_stop`)

- **Files:** `src/baton_harness/chain/daemon.py`,
  `src/baton_harness/chain/reconcile.py`, `src/baton_harness/chain/escalation.py`
  (optional `kind` enrichment of the escalation runlog event),
  `tests/chain/test_daemon_report_wiring.py`.
- Create `SessionReport` after obs init (`daemon.py:2180`); pass into
  `reconcile_startup` (`:2212`), `_poll_and_run` (`:2337`), `_run_work_unit` (`:1208`).
- **Signature changes (S7), named as explicit scope:**
  - `_open_pr` (`daemon.py:1011`): `-> None` → `-> str | None`, returning
    `proc.stdout.strip() or None` (the `gh pr create` URL); record `pr.url` at its call
    site from the return value.
  - `_run_ci_gate` (`daemon.py:857`): `-> None` → `-> MergeOutcome` (return the outcome to
    `_run_work_unit`), which records `merge_gate.outcome` + `merged_sha`. Do **not** thread
    the `report` object into the merge internals.
- **Per-issue outcomes (B2):** do NOT add `record_park` at the 16 park sites. Fold
  `merged_issues` / `parked_reasons` into the report in a `finally` inside `_run_work_unit`
  (§2.4) so partial state survives a mid-unit throw.
- Incremental single-site hooks only: `_launch_one_issue` (pickup), `_label_edit`
  (daemon-attested transitions — never `blocked`, §2.7), the `alert()` call sites
  (escalation `kind`).
- **`exit_reason` restructure (S6):** add the `_exit_reason = ["exception"]` closure holder;
  set `"sigterm"` in the SIGTERM handler (`:2226-2232`) before `raise`, `"once_complete"`
  before `if once: break` (`:2318`), and `except KeyboardInterrupt: _exit_reason[0] =
  "keyboard_interrupt"; raise` inside the try/finally. The finally reads `_exit_reason[0]`.
- Add `report.begin_tick`/`end_tick` around the `for repo_cfg` loop (`:2278-2316`); record
  `tick_error` in the `except` at `:2295`.
- Write the report (atomic temp+`os.replace`, §2.1/M9) + emit one `daemon_stop` runlog
  event (schema §2.2/S8) in the finally (`:2322`), both best-effort.
- Tests: drive `run_daemon(once=True)` with stubbed gh/subprocess seams; assert report
  content for pickup, daemon-attested label transitions, PR url, merge-gate outcome +
  `merged_sha`, park + `park_kind=block` + escalation `kind=block`, and
  `exit_reason=once_complete`. Assert a raised exception inside `_poll_and_run` still
  produces a report with a `tick_error` and partial per-issue outcomes, and does not crash
  the daemon (isolation). **Run the daemon in the main process for any SIGTERM/exit_reason
  assertion (§2.6)** — a thread-driven `run_daemon` won't install the SIGTERM handler.
- Regression: existing daemon tests still green; `daemon_start` still emitted; `_open_pr` /
  `_run_ci_gate` callers updated for the new return types.

### Phase 3 — CLI `--report`

- **Files:** `src/baton_harness/chain/cli.py`, `tests/chain/test_cli_report.py`.
- Add `--report PATH`; resolve absolute pre-chdir (mirror `cli.py:258-262`); forward to
  `run_daemon` (`cli.py:397-405`). Default path
  `${BH_PROJECT_ROOT}/.baton-harness/session-report.json`.
- Tests: path resolves absolute before chdir; forwarded correctly; default applied when
  omitted.

### Phase 4a (v1) — init-sandbox: no-agent scenarios + loud label validation

- **Files:** `bin/init-sandbox.sh`, `docs/smoke-test-daemon.md`, `README.md`.
- Add `--scenario`/`BH_SCENARIO`; default `hello` = existing behaviour; add the two
  **no-agent** seeds: `terminal-block` and `recovery`.
- Add `_create_issue_checked` with post-create label verification (§4.2); route scenario
  issue creation through it.
- Bash syntax check (`bash -n bin/init-sandbox.sh`) — the repo already validates scripts
  this way (memory: "All 6 refactored scripts pass bash syntax validation").

### Phase 4b (follow-up) — init-sandbox: real-agent scenarios + ci-fail workflow variant

- **Files:** `bin/init-sandbox.sh`, `docs/smoke-test-daemon.md`.
- Add the three **real-agent** seeds: `clean-implement`, `block-ambiguity`, `ci-fail`.
- Add the per-scenario `ci.yml` variant whose `Test (pytest)` job runs `exit 1` (§4.1,
  Decision 2).

### Phase 5a (v1) — assertion harness: Python matcher + fixtures (runs in GH-Actions CI)

- **Files:** `src/baton_harness/scenario/__init__.py`,
  `src/baton_harness/scenario/verify.py`,
  `src/baton_harness/scenario/expectations.py`,
  `tests/scenario/test_expectations.py`.
- Implement the matcher + expectation specs (§5.1); `python -m
  baton_harness.scenario.verify --scenario <key> --report <path>` entry point.
- Include expectation specs for **all five** scenarios (they are pure data; only the
  real-agent *execution* is deferred).
- Tests: matcher PASS/FAIL against synthetic report fixtures (one fixture per scenario);
  exit-code contract. These use fixtures, so they run in **ordinary GitHub-Actions CI**
  without a real daemon or agent.
- Caveat: the fixture-only matcher tests do **not** prove the daemon *produces* a report
  matching the fixtures — that is what Phase 5a-ci adds (B3).

### Phase 5a-ci (v1) — real-daemon GitHub-Actions gate for the no-agent scenarios (B3)

- **Files:** `.github/workflows/scenario-smoke.yml`, `bin/verify-scenarios.sh` (shared
  orchestrator, also reused by Phase 5b).
- The job (GitHub-hosted runner) for each of `terminal-block` and `recovery`:
  1. Seed via `bin/init-sandbox.sh --scenario <k>` (operator identity = a GH token
     secret with Issues:write; `gh auth setup-git` per `init-sandbox.sh:154-165`).
  2. Run the **real daemon**: `bh-daemon --once --workflow <WF> --report <scratch>/r.json`.
  3. Assert: `python -m baton_harness.scenario.verify --scenario <k> --report <scratch>/r.json`.
- **Startup-gate preconditions (why this runs without an agent):** both scenarios
  dispatch nothing, but the daemon still runs `reconcile_startup` (`daemon.py:2212`),
  which enforces the G3 gates. The job must therefore:
  - Provide a **dummy** `~/.claude/.credentials.json` so the **G3c** OAuth-presence gate
    (`verify-recovery.sh:268-289`) passes. It is never consumed because no agent is
    dispatched — this is the crux that makes a real daemon run possible in ordinary CI.
    (Structural presence only; never populated with a real credential.)
  - Ensure `ANTHROPIC_API_KEY` is **unset** (else **G3b** refuses, `verify-recovery.sh:399-435`).
  - Provide a valid fine-grained `GH_TOKEN` (else **G3a** refuses,
    `verify-recovery.sh:439-487`; and G3d's `gh auth setup-git`).
- `recovery` runs the daemon in the **main process** (subprocess, not a Python thread) so
  the SIGTERM handler installs (§2.6) and the finally writes the report. For the G1/G2
  variants, pre-seed the stale `daemon.alive` marker / spawn the decoy exactly as
  `verify-recovery.sh:514-616` does, but assert on `startup.findings[]` in the report
  rather than grepping stderr.
- This is the phase that makes v1 validate **daemon→report fidelity** end-to-end, not
  just the matcher. (Skill `claude-github-tools:github-actions` applies.)

### Phase 5b (follow-up) — gated self-hosted end-to-end harness (real-agent scenarios only)

- **Files:** `bin/verify-scenarios.sh` (shared with Phase 5a-ci), `.github/workflows/scenario-harness.yml`
  (gated self-hosted job).
- Reuse the Phase 5a-ci orchestrator, now for the three **real-agent** scenarios
  (`clean-implement`, `block-ambiguity`, `ci-fail`): seed (operator identity) → run the
  real daemon (executor PAT) → invoke the Python matcher.
- Wire as a **gated job on the self-hosted Linux deploy host** (the same host
  `verify-recovery.sh` targets, `verify-recovery.sh:7-10`) — only these scenarios need a
  live `claude -p` agent + **real** OAuth creds, which GitHub-hosted CI cannot provide
  (Decision 1). Not run on every PR; triggered manually / on the deploy host.
  (Skill `claude-github-tools:github-actions` applies to the gated-job wiring.)

### Phase 6 (follow-up) — migration / retirement

**Tagged follow-up (M11).** Phase 6 does not run until the report-based recovery coverage
is actually proven against a real daemon — i.e. **Phase 5a-ci is merged and green**. Do
not remove any `verify-recovery.sh` check before its replacement is demonstrably running,
or a coverage window opens.

- **Files:** `bin/verify-block-escalation.sh` (delete), `bin/verify-recovery.sh` (slim +
  document split), `docs/harness-design.md`, `README.md`.
- Close PR #241 / issue #239 as superseded. `bin/verify-block-escalation.sh` lives on the
  #241 branch, **not `main`** (confirmed: `Glob bin/verify-*.sh` returns only
  `verify-recovery.sh`), so closing PR #241 drops **no `main` coverage** — fine, no
  coverage window (M11). "Retire" = close PR #241 without merging + ensure nothing in
  `main` references the script.
- Slim `verify-recovery.sh` to the fatal **G3a/G3b/G3c/G3d** gates (G3d included per M10 —
  and note in the header that G3d is not yet tested, tracked as a follow-up issue). Only
  remove the G1/G2/SIGTERM blocks **after** Phase 5a-ci demonstrably asserts them via the
  report (M11); add a header note pointing G1/G2/SIGTERM to the report-based harness (§2.5).
- Document the report schema + provenance boundary (§3.1) + scenario harness in
  `docs/harness-design.md` (relates to §1/§10 vendoring design) and `README.md`.
- Note relation to #139 (looper-style stdout result marker): the `daemon_stop` runlog
  event + session report together are the harness's structured "result marker"; a future
  #139 goal-based-termination marker can read `totals`/`issue.outcome` + `merge_gate.merged_sha`
  from the report rather than parsing stdout — but MUST respect the provenance boundary
  (§3.1) and the MERGED-≠-green-tree caveat (S5) when it does.

---

## 7. Risks & mitigations

- **R1 — report write in finally masks the real exit or crashes shutdown.** Mitigation:
  wrap in try/except that never raises, mirroring `runlog.py:136-141` and the existing
  finally swallow at `daemon.py:2331`. The report write is the **last** thing in the
  finally, after `stop_event.set()` and marker unlink.
- **R2 — threading a `report` kwarg through many signatures is churny and easy to drop.**
  Mitigation: default `report: SessionReport | None = None` on every new kwarg (same
  pattern as `runlog`/`tally`/`obs` today), so `record_*` calls no-op when `None` and
  existing unit tests that don't pass a report keep working.
- **R3 — `kind` not in the runlog escalation event.** Mitigation: capture `kind` in the
  report accumulator at the `alert()` call site (§2.4), independent of the runlog event.
  Optionally add `kind` to the escalation event too (`escalation.py:278-288`) — low-risk,
  additive.
- **R4 — real-agent scenarios can't run in ordinary GitHub-Actions CI** (no OAuth creds,
  can't spawn `claude -p`). Mitigation (Decision 1, refined by B3): the **no-agent**
  scenarios run the real daemon in GH-Actions CI (Phase 5a-ci) with a dummy OAuth cred file
  to clear G3c; only the **real-agent** end-to-end loop is a gated job on the self-hosted
  Linux deploy host (Phase 5b; same host `verify-recovery.sh` targets,
  `verify-recovery.sh:7-10`). The fixture matcher tests (Phase 5a) run in normal CI but are
  explicitly *not* the fidelity gate — Phase 5a-ci is (B3).
- **R5 — scenario seeding over-privileging the executor.** Mitigation: seeding is a
  distinct `init-sandbox` operator step (ambient `gh` auth); the daemon run never seeds.
  This is the settled root-cause lesson and is structurally enforced by keeping the two
  steps in separate scripts/identities.
- **R6 — the report is a single worker-adjacent oracle.** Mitigation: §2.7 removes the one
  worker-forgeable field (live `blocked` read); §3.1 tags every remaining field as
  daemon-attested or daemon-captured-observation so no consumer (harness, #139 goal-marker)
  trusts an observed value as if it were attested; M9 makes the write atomic so the oracle
  file is never half-written.

---

## 8. Resolved decisions

All four questions raised during Phase-1 discovery were answered by the user on
2026-07-09; each answer matched the plan's recommendation. They are now fixed decisions
and are propagated into the sections/phases noted.

1. **CI strategy = gated self-hosted for real-agent scenarios; real-daemon GH-Actions
   gate for no-agent scenarios (RESOLVED, refined by review B3).** The three end-to-end
   **real-agent** scenarios (`clean-implement`, `block-ambiguity`, `ci-fail`) run as a
   gated job on the self-hosted Linux deploy host, like `verify-recovery.sh`
   (`verify-recovery.sh:7-10`). No mock-agent mode. **But** the two **no-agent** scenarios
   (`terminal-block`, `recovery`) run the **real daemon** in ordinary GitHub-Actions CI
   (Phase 5a-ci), because they dispatch no agent and so need no real OAuth — only a dummy
   OAuth cred file to clear the G3c startup gate. **Rationale:** real `claude -p` runs need
   OAuth GitHub-hosted runners can't provide, and a mock-agent path would test the mock;
   but a v1 that only ran a matcher against hand-written fixtures would be test theater
   (B3) — so v1 gets a genuine real-daemon gate for the scenarios that don't need an agent.
   **Propagated to:** §5, §7 R4, Phase 5a (fixtures) / Phase 5a-ci (real-daemon GH-Actions
   gate) / Phase 5b (gated self-hosted real-agent harness).

2. **`ci-fail` trigger = per-scenario `ci.yml` with `Test (pytest)` → `exit 1`
   (RESOLVED).** The other two required jobs still exit `0`, so the red check is
   unambiguously pytest and the expected `MergeOutcome.CI_FAILED` is deterministic.
   **Propagated to:** §4.1, Phase 4b.

3. **`--report` = always-on default (RESOLVED).** The daemon always writes
   `${BH_PROJECT_ROOT}/.baton-harness/session-report.json`; `--report <path>` overrides
   only the location, never disables emission. **Rationale:** the report is a first-class
   observability feature, so every run should leave a record. **Propagated to:** §2.3
   (CLI surface), §2.1 (emit point), Phase 3.

4. **v1 scope = phased (RESOLVED, refined by B3).** v1 = report + wiring (Phases 1–3) +
   the two no-agent scenarios (`terminal-block`, `recovery`, Phase 4a) + the Python matcher
   (Phase 5a) + **the real-daemon GitHub-Actions gate for those two scenarios (Phase
   5a-ci)**. The three real-agent scenarios (Phase 4b) + the gated self-hosted harness
   (Phase 5b) + migration/retirement (Phase 6) land in a **follow-up**; Phase 6 must not
   remove `verify-recovery.sh` G1/G2/SIGTERM coverage until Phase 5a-ci is merged and green
   (M11). The report feature merges without waiting on the deploy-host harness.
   **Propagated to:** §6 (v1-vs-follow-up banner + per-phase markers, incl. Phase 5a-ci).

## 9. Executor reconcile note (pending)

This planning session had no `gh`/GitHub tool, so the **issue #243 body was not
independently fetched** — the router authored #243 and its dispatch brief is treated as
authoritative for scope. Before executing Phase 1, the executor should run
`gh issue view 243` and reconcile any divergence between the issue body and this plan's §1
scope statement.
