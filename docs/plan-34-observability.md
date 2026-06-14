# Implementation Plan — Issue #34: Unattended failure signal / observability

**Issue:** [glitchwerks/baton-harness#34](https://github.com/glitchwerks/baton-harness/issues/34) — Unattended failure signal / observability (Failure-mode hardening milestone)
**Status:** Planning only. No implementation performed.
**Worktree:** `I:/ai/claude/baton-harness/.worktrees/plan-obs-34` (branch `plan-obs-34`).
**Date:** 2026-06-13
**Review history:** This revision incorporates a project-reviewer architectural pass and an adversarial inquisitor pass, both conducted 2026-06-13. Changed sections are noted with the originating review source.

> Citation discipline: every load-bearing claim below cites a source — the issue body provided in the dispatch brief, a verified `file:line` anchor in this worktree, or a related issue (#31/#32/#33). Anchors were re-read against source before writing; see the verification notes inline. Where I could not confirm a fact, it is flagged `unverified:` and surfaced in Open Questions rather than asserted.

---

## Frontmatter — file touches per phase

| Phase | Files touched |
|---|---|
| P0 | `chain/runlog.py` (new), `chain/obs_config.py` (new), `chain/daemon.py`, `bin/init-sandbox.sh`, `tests/chain/test_runlog.py` (new), `tests/chain/test_obs_config.py` (new) |
| P1 | `chain/escalation.py`, `chain/daemon.py`, `tests/chain/test_escalation.py` |
| P2 | `chain/labels.py` (new), `chain/daemon.py`, `after_run.py`, `tests/chain/test_labels.py` (new), `tests/chain/test_daemon.py` |
| P3 | `chain/daemon.py`, `chain/recovery.py` (read-only reference), `tests/chain/test_daemon.py` |
| P4a | `chain/daemon.py`, `chain/obs_config.py`, `tests/chain/test_daemon.py` |
| P4b | `chain/daemon.py`, `chain/obs_config.py`, `bin/run-daemon.sh`, `tests/chain/test_daemon.py` |
| P5 | `README.md`, `config/WORKFLOW.md`, `docs/harness-design.md` |

(project-reviewer: frontmatter absent from prior draft — added.)

---

## Design constraints (non-negotiable)

1. **No phase touches `vendor/symphony/`** — vendored source is the domain of VENDORING.md re-vendor patches; this epic makes zero modifications to `src/baton_harness/vendor/symphony/`. (Both reviewers confirmed this property; it is stated here so implementers do not reach for it.)
2. **R1 — config isolation** — harness-specific observability config lives in `chain/obs_config.py` backed by `BH_*` env vars, NOT in the vendored `WorkflowConfig` (`vendor/symphony/config.py:18`). Clobbered on re-vendor (VENDORING.md re-vendor checklist). This is the single most important design decision in the plan. (Both reviewers confirmed this is correct.)
3. **Serial execution invariant** — the daemon calls `orch._run_worker(issue_obj)` directly in a pop-one-issue loop (`daemon.py:910-981`, `_poll_and_run` / `_run_work_unit`). The `max_concurrent`/`asyncio.create_task` machinery in `orchestrator.py` is only reached via `Orchestrator.run()`/`_dispatch`, which the harness never calls — dead code on this path. P2/P3/P4 implementers must not reintroduce inner concurrency; doing so would break the single-writer recovery invariant (`recovery.py:99-100`, `daemon.py` docstring line 33). (inquisitor: architecture-map correction.)

---

## 1. Problem statement (from the issue)

The daemon's entire observability surface is free-text stdout/stderr. For a system whose value proposition is *unattended* overnight operation, **no signal reaches a human when things break** (issue #34 body, dispatch brief). The four acceptance criteria:

1. The single-state label invariant (exactly one of `{agent-ready, agent-done, blocked}`) is asserted, and a violation emits an alert.
2. A re-dispatch loop (same issue dispatched N times in a window) is detected and escalated.
3. Lack of Baton progress (ties to #33) raises a signal that reaches a human asynchronously (the deferred Slack escalation channel).
4. A minimal structured run/outcome record exists beyond free-text stdout, sufficient to diagnose an overnight failure at daemon-event granularity (dispatch → outcome → escalation, with issue + timestamp). See §3 item 5 for visibility scope.

This issue is a **cross-cutting enabler** for #31 (torn label state), #32 (`json.loads` crash loop), and #33 (hung Baton) — it provides the detection and signalling substrate those three consume.

**Interim detection gap:** before P2 and P3 land, torn-label state (#31) and re-dispatch loops (#32) will not be detected. #31's prevention must live in `after_run._reconcile_labels` (the only place with authority over label edits at hook side); P2 is the late-detection alarm that fires after the fact. Do not expect P2 to prevent the tear.

---

## 2. Current-state anchors (verified against source in this worktree)

| Concern | Anchor | Verified note |
|---|---|---|
| Escalation infra | `src/baton_harness/chain/escalation.py:57-182` (`escalate`), `:185-226` (`_post_slack`) | Confirmed: dual-channel (GitHub comment durable + optional Slack via `BH_SLACK_WEBHOOK_URL`, 10s urllib timeout). `kind` ∈ {block, debug} is accepted but **not used to alter the message body** (`escalation.py:104-106` docstring). No severity hierarchy. |
| Escalation call sites | `daemon.py` issue-fetch park (`:715-721`), worker-raised (`:732-738`), pr_created-no-PR (`:757-763`), merge-raised (`:790-796`), CI-gate park (`:818-824`), block/no_pr park (`:836-839`), repo-level tick failure with `issue=None` (`:965-972`) | Confirmed ~7 sites in the main worker path plus the tick-level catch. All pass `kind="debug"` except the block-park (`kind="block"`, `:827`). |
| Post-worker label re-read | `daemon.py:742-743` | Confirmed: daemon re-reads labels and checks only `"blocked" in post_labels`. No full invariant assertion. |
| Label constants | `after_run.py:76-82` (`LABEL_AGENT_READY`, `LABEL_AGENT_DONE`, `LABEL_BLOCKED`) | Confirmed. These constants are currently defined only in the hook module and are not importable by chain code without a hook→chain dependency violation. See §5 Phase 2 for the extraction plan. |
| Label edits (daemon side) | `daemon.py` `_label_edit()` calls throughout the worker path (e.g. `:712`, `:729`, `:801-803`, `:833`) | Confirmed daemon is the single label writer in the dispatch path (`recovery.py:99-100` docstring: "single writer = daemon, C1"). |
| Re-dispatch selection | `_poll_and_run` (`daemon.py:984+`), `_run_work_unit` issue pop loop | Confirmed: one work unit per tick. **No per-issue dispatch counter / attempt history anywhere** (grep-confirmed: no counter state in daemon). |
| Recovery redispatch | `recovery.py:97-100` (`RecoveryResult.redispatch`) | Confirmed: `redispatch` is a `set[int]` populated by Rule 5 (`:359-361`): any issue carrying `agent-in-progress` with no provenance merge and no open PR re-enters the dispatch path. `RecoveryResult` is a frozen dataclass; it is re-derived from GitHub label state every tick. The `redispatch` set is **not persisted** — it is reconstructed fresh on each call to `reconstruct()` (`recovery.py:290+`). |
| Liveness | `run_daemon` (`daemon.py:910-981`): `while True` + `asyncio.sleep(poll_interval_s)` (`:979`), startup log `:937-941`, stop log `:981` | Confirmed: no per-tick heartbeat. The repo-level tick is wrapped in a defensive `try/except` (`:949-974`) that escalates with `issue=None`. |
| Structured records | `merge.py` provenance trailer + marker comment (per map `:427-431`, `:527-541`); parsed by `recovery.py:114-176` (`_fetch_provenance_merges`) | Confirmed provenance is the only durable structured artifact, and it exists to rebuild the `done` set — **not** a run/outcome log. `.symphony/state.json` is vendored-Orchestrator-managed; harness does not inspect it. |
| Config | env `BH_REPO_OWNER/NAME/PROJECT_ROOT` required (`registry.py:66-74`), `BH_SLACK_WEBHOOK_URL` optional (`escalation.py:173`). `WorkflowConfig` is the **vendored** dataclass (`vendor/symphony/config.py:18`, loaded from `config/WORKFLOW.md` YAML). | Confirmed: `WorkflowConfig` lives in vendored code. New observability config must NOT go there. |
| Test seam | `tests/chain/test_daemon.py:1-70`: all I/O mocked via the `_run` seam or direct `_run_worker` patching; async driven by `asyncio.run` + `once=True`; no pytest-asyncio. | Confirmed — this is the seam every phase below is testable against. |
| CLI construction path | `chain/cli.py:171-178`: `asyncio.run(run_daemon(config, registry, once=..., poll_interval_s=...))` | Confirmed: `cli.py` calls `run_daemon` directly. `ObsConfig` should be constructed inside `run_daemon` via `load_obs_config()` — self-contained, no `cli.py` signature change required. (inquisitor: ObsConfig construction path clarification.) |

---

## 3. Recommended answer to "what pages a human"

**Build on `escalation.py`; do not invent a new channel.** Concretely:

1. **Introduce an `alert()` wrapper** with a `severity` parameter (`info | warn | critical`). Add this as a thin wrapper that calls the existing `escalate()` rather than mutating `escalate()`'s signature or contract — the `escalate()` function carries the durable-GitHub-comment guarantee that must not be broken. Severity drives **routing**, not a new transport:
   - `info` → structured record only (no GitHub comment, no Slack); cheap, high-volume (heartbeats, normal outcomes).
   - `warn` → GitHub comment (durable) + Slack if configured.
   - `critical` → GitHub comment + Slack, and the message body is prefixed with a loud marker.
   This reuses the dual-channel transport at `escalation.py:114-182` unchanged; only the routing decision is new. (inquisitor: commit to wrapper, not signature mutation.)

2. **Per-call-site severity table** — all ~7 existing `daemon.py` `escalate()` call sites must be assigned an explicit severity when the `alert()` wrapper is introduced; "default to warn" is not sufficient. Recommended severity by site:

   | `daemon.py` site | Recommended severity | Rationale |
   |---|---|---|
   | issue-fetch park (`:715-721`) | `warn` | Transient GH API failure; recoverable next tick |
   | worker-raised exception (`:732-738`) | `warn` | Recoverable per-issue failure; watch for repeated occurrence |
   | pr_created-no-PR (`:757-763`) | `warn` | Unexpected but not loop-threatening |
   | merge-raised exception (`:790-796`) | `warn` | Recoverable merge failure |
   | CI-gate park (`:818-824`) | `critical` | Overnight CI failure is a human-action blocker |
   | block/no_pr park (`:836-839`) | `warn` | Expected blocked outcome |
   | repo-level tick failure / `issue=None` (`:965-972`) | `critical` | Daemon-level failure affecting all issues |

   (inquisitor: don't leave critical-path severity implicit; CI-gate park likely warrants `critical`.)

3. **Invariant-assertion checkpoint** in the daemon worker path immediately after the post-worker label re-read (`daemon.py:742-743`). Replace the narrow `"blocked" in post_labels` check with a call to `assert_single_state(post_labels)`. This is **detection-after-the-fact**: the daemon reads labels only after the `after_run` hook subprocess has already committed its changes, so P2 is a backstop alarm, not a prevention control. PREVENTION of torn label state (#31) must live in `after_run._reconcile_labels`. A violation calls `alert(..., severity="critical")` + parks. (inquisitor: P2 reframe as detection/backstop.)

4. **Durable re-dispatch loop detection** — see §5 Phase 3 for the full design. The short version: base loop detection on a restart-surviving signal derived from `recovery.py`'s `redispatch` classification (which re-reads `agent-in-progress` orphan labels from GitHub every tick), NOT on an in-process counter. (inquisitor: B1 — in-process counter blind to crash-restart loop.)

5. **Two distinct liveness signals** — see §5 Phase 4a:
   - **Process-alive heartbeat**: emitted on a fixed cadence independent of work-unit duration (not per-tick, because a tick can legitimately block for `ci_timeout=1800s`). This drives the external dead-man's-switch.
   - **Work-progressing signal**: per-tick record in the JSONL runlog capturing dispatch → outcome. Allowed to be slow. Drives in-daemon stall detection.
   Conflating these two signals causes the dead-man's-switch to false-alarm during normal long CI polls. (Both reviewers: B2 heartbeat-decoupling.)

6. **Structured JSONL run-record** — see §5 Phase 0. Satisfies AC4 at **daemon-event granularity**: dispatch → outcome → escalation, with issue number and timestamp. The daemon only has `_run_worker`'s string return, exception text, and post-hoc label re-read; failure detail inside `_run_worker`, hooks, or `.symphony/state.json` is invisible to the harness. Walk AC4 back to this visible scope — do not claim the runlog can carry root-cause depth that hook-internal failures produce. (inquisitor: AC4 walk-back.)

**Why this shape:** the issue explicitly says to build on the existing escalation infra. Severity-as-routing keeps the one durable transport (GitHub comment) and one best-effort transport (Slack) intact while adding the alert semantics AC1–AC3 need. The JSONL record (AC4) is what makes an overnight failure diagnosable at the daemon-event level.

---

## 4. New data each AC needs, and where it lives

| Data | AC | Lives where | Rationale |
|---|---|---|---|
| `alert()` wrapper + `severity` routing | AC1–AC3 | New wrapper in `chain/escalation.py` | Preserves `escalate()` contract; routing-only addition. |
| Label constants (`LABEL_AGENT_READY`, `LABEL_AGENT_DONE`, `LABEL_BLOCKED`) | AC1 | New **`chain/labels.py`** (extracted from `after_run.py:76-82`) | `after_run.py` is a hook module (separate subprocess); chain code cannot import from it without a hook→chain dependency violation. Both P2 daemon code and `after_run.py` import from `chain/labels.py`. (inquisitor: B3 import boundary.) |
| Single-state assertion helper | AC1 | `chain/labels.py` (or `chain/invariants.py`) | Co-located with the constants it uses; importable by daemon without violating the hook boundary. |
| Re-dispatch attempt tally | AC2 | **Durable GitHub/provenance-derived signal** — count of `agent-in-progress` orphan re-dispatches accumulated across ticks via `recovery.py`'s `redispatch` set re-read every tick | An in-process counter is zeroed on daemon restart and cannot detect the crash-restart loop pattern that is the target failure mode of #32. The `redispatch` set is re-derived from GitHub labels each tick (`recovery.py:359-361`), surviving restarts. The attempt tally must be accumulated in a durable store (e.g. a running dispatch-count comment on the issue, or a `.baton-harness/dispatch-counts.json` file). (inquisitor: B1 — supersedes DaemonState approach.) |
| Heartbeat timestamp (process-alive) | AC3 | **Written to `BH_HEARTBEAT_FILE`** on a fixed asyncio timer cadence independent of `_poll_and_run` duration | Separate asyncio coroutine; not tied to tick duration. External monitor reads `mtime`. |
| Work-progressing signal | AC3 | **Per-tick JSONL record** (dispatch → outcome) in `runlog.jsonl` | Allows external cross-check; also drives in-daemon stall detection. |
| Structured run/outcome record (JSONL) | AC4 | **Persisted, harness-owned path** — `${BH_PROJECT_ROOT}/.baton-harness/runlog.jsonl` (NOT `.symphony/`, which is vendored-Orchestrator-owned) | Must survive process exit. Gitignored in target repo (`bin/init-sandbox.sh` seeds this). Content: daemon-event granularity only — dispatch, outcome, escalation, with issue + timestamp. Not hook-internal failure detail. (inquisitor: AC4 scope.) |
| Observability config | all | **`chain/obs_config.py`** — `ObsConfig` dataclass + `load_obs_config()` from `BH_*` env; constructed inside `run_daemon`, no `cli.py` change needed | Isolated from vendored `WorkflowConfig`. New vars: `BH_RUNLOG_PATH`, `BH_REDISPATCH_WINDOW_TICKS`, `BH_REDISPATCH_MAX`, `BH_HEARTBEAT_STALL_S`, `BH_HEARTBEAT_FILE`, `BH_HEARTBEAT_PING_URL`. |

---

## 5. Phased breakdown

Each phase maps to one sub-issue (P0–P4b, six total) tracked under #34 as the epic; see §9 for the issue-structure decision. Each phase is independently committable, independently testable, and lands its own PR targeting `main` directly. `Closes #34` fires only when all sub-issues have landed. Sequence is chosen so later phases consume earlier substrate.

### Phase 0 — Structured run-record substrate (AC4 foundation)

**Why first:** every other AC writes into this; building it first lets subsequent phases emit records as they go.

- New module `src/baton_harness/chain/runlog.py`: `RunLog` class with an append-only `emit(event: dict)` writing one JSON line to the configured path; a `_write_line` seam (single function) so tests patch one symbol, mirroring `escalation.py:31` `_run` seam.
- **Directory creation:** `run_daemon` must call `mkdir -p .baton-harness/` on startup before the first `RunLog.emit()`. If the directory does not exist on first write, `emit()` raises `FileNotFoundError` and the daemon loses its diagnosis record at the worst possible moment. Add a startup-time directory creation call and a test asserting it fires before any emit. (inquisitor: runlog dir creation concern.)
- **Heartbeat-file write strategy:** `BH_HEARTBEAT_FILE` writes use write-temp-then-`os.replace`. Note: `os.replace` is documented as atomic on POSIX but is NOT guaranteed atomic on Windows (the operator's OS). The implementation must acknowledge this platform caveat — on Windows a partial write or absent file after a crash is possible; the external monitor should tolerate a missing file as a stale-heartbeat equivalent rather than an error. Document this in the module docstring. (inquisitor: Windows atomicity concern.)
- Define the event schema (fields: `ts` ISO-8601 UTC, `event` enum, `issue` int|null, `outcome`, `severity`, `detail`, `tick_id`). Document in module docstring.
- New `chain/obs_config.py`: `ObsConfig` dataclass + `load_obs_config()` from env. Defaults: `BH_RUNLOG_PATH` → `${BH_PROJECT_ROOT}/.baton-harness/runlog.jsonl`, `BH_HEARTBEAT_FILE` → `${BH_PROJECT_ROOT}/.baton-harness/heartbeat`. `load_obs_config()` is called inside `run_daemon` — self-contained, no `cli.py` signature change required.
- **Gitignore requirement:** the `.baton-harness/` directory MUST appear in the target repo's `.gitignore`. `bin/init-sandbox.sh` already seeds `.symphony/` per #71/PR #72; this phase extends that same seeding block to include `.baton-harness/`. Operators running an older `init-sandbox.sh` must add the entry manually (noted in Phase 5 docs).
- Wire `run_daemon` to construct `ObsConfig` via `load_obs_config()` and a `RunLog` once on startup, passing both into `_poll_and_run` / `_run_work_unit`.
- **Tests:** patch `_write_line` seam; assert one JSON object per `emit`; assert crash-mid-write loses ≤1 line; assert `load_obs_config` honours env + defaults; assert `.baton-harness/` `mkdir -p` fires on startup.
- **Commit boundary:** runlog + obs_config land with the daemon emitting at least dispatch + outcome events.

### Phase 1 — Alert-severity layer on escalation (AC1–AC3 transport)

**Depends on:** Phase 0 (runlog emit from inside `alert()`).

- Add a thin `alert(severity: Literal["info","warn","critical"], ...)` wrapper in `chain/escalation.py` that calls `escalate()` for `warn`/`critical` and skips the GitHub+Slack path for `info`. The `escalate()` function's signature and contract (durable GitHub comment guarantee) are unchanged — `alert()` is a new entry point, not a mutation of the existing one. `critical` decorates the body with a loud prefix marker.
- Apply the per-call-site severity table from §3 item 2 — do not leave all sites at a default. This means touching `daemon.py` at each of the ~7 sites listed in the table.
- Emit a runlog `escalation` event from inside `alert()` at every severity level (so the record captures all alerts, including `info`).
- **Tests:** extend `tests/chain/test_escalation.py` — assert `info` skips the `_run` seam, `critical` calls it and decorates the body, Slack remains best-effort. Assert a runlog event is emitted for each severity (patch the RunLog `_write_line`).

### Phase 2 — Single-state label invariant assertion (AC1)

**Depends on:** Phase 1 (uses `alert(severity="critical")`), Phase 0 (emits violation events).

**Prerequisite step — extract label constants:**
Before any P2 code is written, extract `LABEL_AGENT_READY`, `LABEL_AGENT_DONE`, `LABEL_BLOCKED` from `after_run.py:76-82` into a new `chain/labels.py` module and add `assert_single_state(labels: set[str]) -> str | None` there. Update `after_run.py` to import from `chain/labels.py`; update any other importers. This extraction is a prerequisite: `daemon.py` cannot import from `after_run.py` without creating a hook→chain dependency violation (`after_run.py` is a hook module that runs as a separate subprocess). (inquisitor: B3 import boundary.)

**P2 body:**
- In `daemon.py` after the post-worker label re-read (`daemon.py:742-743`), call `assert_single_state(post_labels)`. On a non-None return (violation), call `alert(..., severity="critical")` + emit a runlog `invariant_violation` event + park the issue.
- P2 is a **backstop detection alarm**, not a prevention control. The daemon reads labels after the `after_run` subprocess has already committed them. PREVENTION of the torn-label condition (#31) must live in `after_run._reconcile_labels`. P2 is the signal that a tear got through reconcile. AC1 acceptance criteria wording must reflect this: "a violation is detected and escalated" — not "a violation is prevented."
- **Tests:** in `test_daemon.py`, drive a `_run_worker` outcome where mocked label fetch returns 0 or 2 members of the invariant set; assert `alert(severity="critical")` called, issue parked, runlog event emitted.

### Phase 3 — Re-dispatch loop detection (AC2)

**Depends on:** Phase 1, Phase 0.

**Design — durable signal, not in-process counter:**
The original plan stored per-issue dispatch timestamps in daemon process memory. This cannot detect the target failure mode of #32 (crash-restart loop): every restart zeroes the counter, so the sliding window never accumulates across restarts. The daemon is stateless between restarts — `recovery.py:reconstruct()` re-derives all state from GitHub + git on each tick (`recovery.py:290+`). (inquisitor: B1 — in-process counter blind to crash-restart loop.)

Instead: base loop detection on the `redispatch` classification that `recovery.py` already derives from live GitHub label state. `recovery.py` populates `RecoveryResult.redispatch` with any issue carrying an `agent-in-progress` orphan label (`recovery.py:359-361`) — this survives restarts because it re-reads GitHub on every call to `reconstruct()`. Loop detection uses a durable attempt tally: count how many times an issue has appeared in `redispatch` across ticks. This tally must survive restarts; options:
- Append a tally entry to a `.baton-harness/dispatch-counts.json` file (simplest; harness-owned path per §4).
- Write a structured comment on the issue when each redispatch fires; count those comments on the next tick.

The recommended default is the `.baton-harness/dispatch-counts.json` approach (file-based, no extra GH API reads). The exact mechanism is an implementation decision; the plan mandates only that the counter survives process restart.

When the tally for an issue reaches `BH_REDISPATCH_MAX` within `BH_REDISPATCH_WINDOW_TICKS` ticks, skip dispatch, park the issue, call `alert(severity="critical")`, emit a runlog `redispatch_loop` event. This caps the uncapped `recovery.py:97-100` redispatch path.

Remove the old R4 hand-wave ("a restart resets the counter; acceptable"). Replace with the durable-signal rationale above. The "thread a DaemonState object" approach is superseded by this design — there is no in-process counter to thread.

- **Tests:** unit-test the threshold helper directly (pure function over a tally file + tick count); one integration case driving two ticks asserting the second redispatch is suppressed + escalated when the tally file shows the threshold reached.

### Phase 4a — Heartbeat + stall detection (AC3, in-daemon layer)

**Depends on:** Phase 0 (heartbeat → runlog), Phase 1 (stall → critical alert).

**Two distinct signals — do not conflate:**
The per-tick `_poll_and_run` can legitimately block for the full `ci_timeout=1800s` (`_DEFAULT_CI_TIMEOUT`, `daemon.py:67`) during a CI poll. If the heartbeat is written at the top of each tick, the external monitor's dead-man's-switch sees a ~30-minute silence during a normal merge — causing false alarms and eventually escalation fatigue. (Both reviewers: B2 heartbeat-decoupling.)

Implement two separate signals:

1. **Process-alive heartbeat** — a separate `asyncio` coroutine that wakes on a fixed cadence (e.g. every 30s) independent of tick duration and writes the current UTC timestamp to `BH_HEARTBEAT_FILE` (write-temp + `os.replace`, with the Windows caveat noted in P0). This is the signal the external dead-man's-switch monitors. The cadence is not configurable in this phase — pick a conservative default shorter than `poll_interval_s`.

2. **Work-progressing signal** — the JSONL runlog `tick` event emitted once per `_poll_and_run` call. This is allowed to be slow (per `poll_interval_s`). The external monitor uses it as a secondary cross-check; the daemon uses it for in-daemon stall detection.

**Staleness threshold for stall detection:** size the threshold to approximately `3 × max_agent_runtime + grace_s`. A single agent run can take many minutes; the CI gate can add up to `ci_timeout=1800s`. A threshold of `3 × poll_interval_s` will false-alarm on normal long runs. `BH_HEARTBEAT_STALL_S` default should be set conservatively (e.g. 7200s, 2 hours); tune as #33 investigation matures. (inquisitor: P4b threshold calibration.)

Stall detection: when a tick observes the same issue still `agent-in-progress` past `BH_HEARTBEAT_STALL_S` (ties to #33), call `alert(severity="critical")` + emit a runlog `stall` event.

- **Tests:** assert the separate heartbeat coroutine fires at its cadence independent of tick duration (mock asyncio.sleep for both the outer loop and the heartbeat); assert stall escalation fires when a mocked clock advances past the budget with the issue still in-progress; assert `BH_HEARTBEAT_FILE` write is called by the heartbeat coroutine, not by `_poll_and_run`.

### Phase 4b — External dead-man's-switch (AC3, out-of-process layer)

**Why a separate phase:** the daemon cannot page about its own death. Only an out-of-process monitor satisfies AC3's "reaches a human even if the process is dead" semantics.

**Concrete harness-owned deliverable** (not docs-only):

Add a `BH_HEARTBEAT_PING_URL` env var to `ObsConfig`. When set, the process-alive heartbeat coroutine (Phase 4a) sends a best-effort HTTP GET to this URL after each heartbeat write. This follows the Healthchecks.io-style dead-man's-switch pattern: the external service expects periodic pings and alerts if they stop. The ping call has a `_ping_url` seam (a thin wrapper around `urllib.request.urlopen`) so tests can patch it; failure is log-and-continue, never raised into the daemon.

**Explicit P4b acceptance criteria:**
- AC3-alpha: `BH_HEARTBEAT_PING_URL` ping fires on each heartbeat-coroutine tick when the var is set; best-effort (exception caught and logged, not raised).
- AC3-beta: unit test asserting the ping fires at heartbeat cadence (mock `_ping_url`), and does not fire if `BH_HEARTBEAT_PING_URL` is unset.
- AC3-gamma: README documents the env var, recommends Healthchecks.io as the default external monitor, and notes that without a ping URL the process-death case is not covered.

Operator docs note: the external monitor is a setup requirement for full AC3 coverage; without it, process-death is silent. (inquisitor: AC3 verifiability — docs-only deliverable is unverifiable; ship a concrete ping call instead.)

**What the external monitor watches:**
- `BH_HEARTBEAT_FILE` mtime — primary signal; a live daemon updates this on the heartbeat cadence.
- Last-entry age in `runlog.jsonl` — secondary cross-check.
- `BH_HEARTBEAT_PING_URL` — recommended default; let the external service do the alerting.

**Staleness threshold (external):** configurable in the external service's config, not in `ObsConfig`. Suggested default: `3 × heartbeat_cadence_s + grace_s`.

### Phase 5 — Docs + config surfacing

- Update `README.md` and `config/WORKFLOW.md` to document all new `BH_*` env vars and the runlog location/schema (README maintenance rule).
- Add an ops note: where to find `runlog.jsonl`, how to tail it, what `critical` events mean, how to set up the external dead-man's-switch (P4b), and what `BH_HEARTBEAT_PING_URL` should point to.
- Note that `bin/init-sandbox.sh` seeds `.baton-harness/` into the target repo's `.gitignore` (Phase 0 adds this); operators running an older `init-sandbox.sh` must add the entry manually.
- Note the Windows heartbeat-file caveat (from P0 module docstring) in the ops docs.
- Update `docs/harness-design.md` if it enumerates the observability surface.
- **Commit boundary:** docs-only PR (or fold into Phase 4b if small).

---

## 6. Dependencies & sequencing vs #31, #32, #33

This issue is the **enabler**; it should land before or alongside fixes to the three:

- **#31 (torn label state):** #31's fix is hook-side prevention in `after_run._reconcile_labels`; that is the only place with authority over label commits. **Phase 2** provides the late-detection backstop alarm. Recommend Phase 2 lands first so #31's fix has a regression signal from day one.
- **#32 (`json.loads` crash loop):** a crash loop manifests as repeated re-dispatch; **Phase 3** (durable loop detection) caps it and the runlog captures dispatch events for diagnosis. Recommend Phase 0 + Phase 3 land before #32's fix.
- **#33 (hung Baton):** consumes **Phase 4a** (heartbeat + in-daemon stall detection). #33 is the no-progress case AC3 references explicitly. Phase 4a's `BH_HEARTBEAT_STALL_S` budget is the detection mechanism #33's fix will tune.

**Sequencing recommendation:** Phases 0 → 1 are pure substrate; merge first. Then 2 / 3 / 4a / 4b can proceed in parallel with (and slightly ahead of) #31 / #32 / #33 respectively.

---

## 7. Risks & testability notes

**Risks:**
- **R1 — vendored-config clobber** (RETAINED). Putting observability config in `WorkflowConfig` would be silently lost on re-vendor (VENDORING.md re-vendor checklist). *Mitigation:* harness-owned `obs_config.py` (§4). Most important design decision in the plan; confirmed correct by both reviewers.
- **R2 — runlog write failure on the unattended path.** If the runlog disk write fails overnight, we lose the very diagnosis record we built. *Mitigation:* runlog writes are best-effort (log-and-continue, never raise into the daemon loop), mirroring the Slack best-effort pattern (`escalation.py:218-225`).
- **R3 — escalation fatigue.** `critical` Slack posts on every transient hiccup train operators to ignore them. *Mitigation:* reserve `critical` for invariant violations, re-dispatch loops, stalls, and CI-gate failures; keep transient parks at `warn`. The per-call-site severity table (§3 item 2) is the enforcement mechanism.
- **R4 — REMOVED.** The original R4 ("in-process counter resets on restart; accepted") is superseded by the durable-signal design in Phase 3. (inquisitor: B1 removal.)
- **R5 — heartbeat false positives.** A legitimately long CI poll looks like a stall if the heartbeat is tied to tick duration. *Mitigation:* separate heartbeat coroutine (Phase 4a) decoupled from `_poll_and_run`. `BH_HEARTBEAT_STALL_S` default tuned conservatively; #33 refines the budget.
- **R6 — Windows heartbeat-file partial write** (NEW). `os.replace` is not guaranteed atomic on Windows. *Mitigation:* write-temp-then-replace strategy; external monitor treats a missing or zero-byte file as equivalent to stale. Documented in P0 module docstring and Phase 5 ops note.

**Testability:** every phase is unit-testable against the established seams:
- `_run` seam (`escalation.py:31`, daemon module-level helpers) — patch one symbol for all gh/git I/O.
- `_run_worker` direct patch (`AsyncMock`) for outcomes (`test_daemon.py:32,37`).
- `asyncio.run` + `once=True` for tick-scoped tests (no pytest-asyncio).
- New seams to add: `runlog._write_line` (single patch point); `_ping_url` in the heartbeat coroutine; an injectable `time_fn` param on the pure loop-detection helper so no monkeypatching of `time` is needed.

---

## 8. Design decisions (formerly open questions)

**Q1 — Alert channel / dead-man's-switch** — DECIDED: **Slack + external dead-man's-switch + `BH_HEARTBEAT_PING_URL` ping.**
In-process alerts route through `alert()` → `escalate()` to GitHub comment (durable) + best-effort Slack. The external dead-man's-switch (Phase 4b) is a harness-owned deliverable (the ping call + its unit test), not docs-only. See Phase 4b §5 for the full design.

**Q2 — Runlog path** — DECIDED: `${BH_PROJECT_ROOT}/.baton-harness/runlog.jsonl` (NOT under `.symphony/`). The `.baton-harness/` directory MUST be gitignored; `bin/init-sandbox.sh` is updated in Phase 0.

**Q3 — Issue structure** — DECIDED: **6 sub-issues, one per phase (P0, P1, P2, P3, P4a, P4b), all under #34 as the epic/tracking issue.** #34 closes when all 6 sub-issues have merged PRs.

**Q4 — Should `severity` replace `kind` or coexist?** — DECIDED: **coexist**. `alert()` wrapper adds `severity` as a new routing axis; `kind` is retained for caller-side categorization. If the team later decides to collapse them, that is a separate refactor.

**Q5 — `ObsConfig` construction path** — DECIDED: `load_obs_config()` is called inside `run_daemon`, constructing `ObsConfig` once on startup. `cli.py` does not need a signature change. (inquisitor: construction path clarification — added as decided question.)

---

## 9. Tracking / issue structure

**Decided (Q3):** track as **6 sub-issues, one per phase**, all filed under #34 as the epic/tracking issue. #34 closes when all 6 sub-issues have merged PRs.

| Sub-issue | Phase | Title (suggested) |
|---|---|---|
| #34-P0 | Phase 0 | Structured JSONL run-record substrate + obs_config |
| #34-P1 | Phase 1 | Alert-severity layer on escalation |
| #34-P2 | Phase 2 | Label constants extraction + single-state invariant assertion (AC1) |
| #34-P3 | Phase 3 | Durable re-dispatch loop detection + cap (AC2) |
| #34-P4a | Phase 4a | Decoupled process-alive heartbeat + in-daemon stall detection (AC3) |
| #34-P4b | Phase 4b | Heartbeat ping deliverable + external dead-man's-switch spec (AC3 out-of-process) |

**Closing-keyword footgun (per CLAUDE.md):** two known traps:

1. `Closes #100, #101` only closes #100. Each issue needs its own keyword on a separate line or clause: `Closes #X` `Closes #Y`.
2. `Closes #N` in a PR that merges into a **feature branch** (not `main`) does NOT auto-close. Since each sub-issue's PR targets `main` directly, this is not a risk for the normal path. If a feature branch is introduced to stage multiple phases, repeat the closing keywords on the integration PR.

**Epic closure:** #34 should be closed manually (with a brief summary comment) once all six sub-issue PRs have merged.
