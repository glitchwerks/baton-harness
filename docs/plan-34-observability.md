# Implementation Plan — Issue #34: Unattended failure signal / observability

**Issue:** [glitchwerks/baton-harness#34](https://github.com/glitchwerks/baton-harness/issues/34) — Unattended failure signal / observability (Failure-mode hardening milestone)
**Status:** Planning only. No implementation performed.
**Worktree:** `I:/ai/claude/baton-harness/.worktrees/symphony-gitignore-71` (reused; no fresh worktree required — the daemon source and tests are present here and the plan is the sole deliverable).
**Date:** 2026-06-13

> Citation discipline: every load-bearing claim below cites a source — the issue body provided in the dispatch brief, a verified `file:line` anchor in this worktree, or a related issue (#31/#32/#33). Anchors were re-read against source before writing; see the verification notes inline. Where I could not confirm a fact (e.g. whether Slack is the *committed* async channel), it is flagged `unverified:` and surfaced in Open Questions rather than asserted.

---

## 1. Problem statement (from the issue)

The daemon's entire observability surface is free-text stdout/stderr. For a system whose value proposition is *unattended* overnight operation, **no signal reaches a human when things break** (issue #34 body, dispatch brief). The four acceptance criteria:

1. The single-state label invariant (exactly one of `{agent-ready, agent-done, blocked}`) is asserted, and a violation emits an alert.
2. A re-dispatch loop (same issue dispatched N times in a window) is detected and escalated.
3. Lack of Baton progress (ties to #33) raises a signal that reaches a human asynchronously (the deferred Slack escalation channel).
4. A minimal structured run/outcome record exists beyond free-text stdout, sufficient to diagnose an overnight failure without re-running.

This issue is a **cross-cutting enabler** for #31 (torn label state), #32 (`json.loads` crash loop), and #33 (hung Baton) — it provides the detection + signalling substrate those three consume.

---

## 2. Current-state anchors (verified against source in this worktree)

| Concern | Anchor | Verified note |
|---|---|---|
| Escalation infra | `src/baton_harness/chain/escalation.py:57-182` (`escalate`), `:185-226` (`_post_slack`) | Confirmed: dual-channel (GitHub comment durable + optional Slack via `BH_SLACK_WEBHOOK_URL`, 10s urllib timeout). `kind` ∈ {block, debug} is accepted but **not used to alter the message body** (`escalation.py:104-106` docstring: "Not currently used to alter the message body; kept for caller-side logging and future filtering"). No severity hierarchy. |
| Escalation call sites | `daemon.py` issue-fetch park (`:715-721`), worker-raised (`:732-738`), pr_created-no-PR (`:757-763`), merge-raised (`:790-796`), CI-gate park (`:818-824`), block/no_pr park (`:836-839`), repo-level tick failure with `issue=None` (`:965-972`) | Confirmed ~7 sites in the main worker path plus the tick-level catch. All pass `kind="debug"` except the block-park (`kind="block"`, `:827`). |
| Post-worker label re-read | `daemon.py:742-743` | Confirmed: daemon re-reads labels and checks only `"blocked" in post_labels`. No full invariant assertion. |
| Label constants | `after_run.py:76-82` (`LABEL_AGENT_READY`, `LABEL_AGENT_DONE`, `LABEL_BLOCKED`) | Confirmed. Invariant enforced only in `after_run.py` `_reconcile_labels` (per map; reconcile is the hook side, not the daemon side). |
| Label edits (daemon side) | `daemon.py` `_label_edit()` calls throughout the worker path (e.g. `:712`, `:729`, `:801-803`, `:833`) | Confirmed daemon is the single label writer in the dispatch path (recovery.py docstring `:99-100`: "single writer = daemon, C1"). |
| Re-dispatch selection | `_poll_and_run` (`daemon.py:984+`), `_run_work_unit` issue pop loop | Confirmed: one work unit per tick. **No per-issue dispatch counter / attempt history anywhere** (grep-confirmed: no counter state in daemon). |
| Recovery redispatch | `recovery.py:97-100` (`RecoveryResult.redispatch`) | Confirmed: detection-based, fires once per run, uncapped. `RecoveryResult` (`recovery.py:81-106`) is a frozen dataclass, **in-memory only**. |
| Liveness | `run_daemon` (`daemon.py:910-981`): `while True` + `asyncio.sleep(poll_interval_s)` (`:979`), startup log `:937-941`, stop log `:981` | Confirmed: no per-tick heartbeat. The repo-level tick is wrapped in a defensive `try/except` (`:949-974`) that escalates with `issue=None`. |
| Structured records | `merge.py` provenance trailer + marker comment (per map `:427-431`, `:527-541`); parsed by `recovery.py:114-176` (`_fetch_provenance_merges`) | Confirmed provenance is the only durable structured artifact, and it exists to rebuild the `done` set — **not** a run/outcome log. `.symphony/state.json` is vendored-Orchestrator-managed; harness does not inspect it (registry.py / harness-design §10). |
| Config | env `BH_REPO_OWNER/NAME/PROJECT_ROOT` required (`registry.py:66-74`), `BH_SLACK_WEBHOOK_URL` optional (`escalation.py:173`). `WorkflowConfig` is the **vendored** dataclass (`vendor/symphony/config.py:18`, loaded from `config/WORKFLOW.md` YAML). | Confirmed: `WorkflowConfig` lives in vendored code. **Design consequence (see §4):** harness-specific observability config must NOT be added to the vendored `WorkflowConfig` — it would be clobbered on re-vendor (VENDORING.md re-vendor checklist). New config goes in a harness-owned surface. |
| Test seam | `tests/chain/test_daemon.py:1-70`: all I/O mocked via the `_run` seam or direct `_run_worker` patching; async driven by `asyncio.run` + `once=True`; no pytest-asyncio. | Confirmed — this is the seam every phase below is testable against. |

---

## 3. Recommended answer to "what pages a human"

**Build on `escalation.py`; do not invent a new channel.** Concretely:

1. **Introduce an alert-severity concept** layered over the existing `kind`. Add a `severity` parameter (`info | warn | critical`) to `escalate()` (or a thin `alert(...)` wrapper that calls `escalate`). Severity drives **routing**, not a new transport:
   - `info` → structured record only (no GitHub comment, no Slack); cheap, high-volume (heartbeats, normal outcomes).
   - `warn` → GitHub comment (durable) + Slack if configured.
   - `critical` → GitHub comment + Slack, and the message body is prefixed with a loud marker so it is unmissable in the Slack channel.
   This reuses the dual-channel transport at `escalation.py:114-182` unchanged; it only adds a routing decision in front of it and a body decoration. The existing `kind` stays for caller-side categorization; `severity` is the new axis the issue's "alert" language needs.

2. **Invariant-assertion checkpoint** in the daemon worker path immediately after the post-worker label re-read (`daemon.py:742-743`). Replace the narrow `"blocked" in post_labels` check with a helper `assert_single_state(post_labels)` that verifies exactly one of `{agent-ready, agent-done, blocked}` is present (using the constants at `after_run.py:76-82`). A violation (zero or ≥2 of the set) calls `escalate(..., severity="critical")` — this satisfies **AC1** and directly feeds #31 (torn label state).

3. **Dispatch-attempt counter + threshold.** Add in-process per-issue attempt tracking (a `dict[int, list[float]]` of dispatch timestamps, or a small dataclass) owned by the daemon loop. On each dispatch in `_run_work_unit`, record `(issue, now)`. Before dispatching, prune entries outside a sliding window and, if the count within the window ≥ threshold, escalate `severity="critical"` and **skip/park** the issue instead of re-dispatching. Satisfies **AC2** and caps the uncapped `recovery.py:97-100` redispatch path.

4. **Per-tick heartbeat + external dead-man's-switch.** At the top (or tail) of each `_poll_and_run` tick, write a heartbeat timestamp both to the structured JSONL record and to a well-known **heartbeat file** (`${BH_PROJECT_ROOT}/.baton-harness/heartbeat`) so that an out-of-process monitor can observe liveness by inspecting `mtime`. AC3's "lack of progress reaches a human" has two distinct layers: (a) the in-daemon stall check (ties to #33's hung-Baton detection) — escalate when a tick observes the same in-progress issue stalled past `BH_HEARTBEAT_STALL_S`; and (b) an **external dead-man's-switch** — the only component that satisfies "reaches a human even if the daemon process is dead." The daemon cannot page about its own death; only an out-of-process monitor can. The external monitor (e.g. a cron job, uptime service, or external scheduler) tails the heartbeat file mtime and/or the last entry age in `runlog.jsonl`; if the heartbeat goes stale beyond a configurable threshold it emits an alert. This is a **separate deliverable**, operated outside the daemon process, and is covered in Phase 4b (see §5).

5. **Structured JSONL run-record.** A new harness-owned `chain/runlog.py` module appends one JSON object per significant event (dispatch, outcome, invariant-violation, heartbeat, escalation) to a file under a harness-owned path (see §4 for *where*). This satisfies **AC4** and is the substrate the other three ACs write into. JSONL (one object per line, append-only) is chosen over a single JSON document so a crash mid-write loses at most one line and the file is tailable live.

**Why this shape:** the issue explicitly says to build on the existing escalation infra ("e.g. the deferred Slack escalation channel"). Severity-as-routing keeps the one durable transport (GitHub comment) and one best-effort transport (Slack) intact while adding the alert semantics AC1–AC3 need, and the JSONL record (AC4) is what makes an overnight failure diagnosable without re-running.

---

## 4. New data each AC needs, and where it lives

| Data | AC | Lives where | Rationale |
|---|---|---|---|
| `severity` axis on escalations | AC1–AC3 | New param/wrapper in `chain/escalation.py` | Reuses existing transport; routing-only change. |
| Single-state assertion result | AC1 | Computed inline in daemon worker path from `post_labels` (`daemon.py:742`) using `after_run.py:76-82` constants | No persistence needed — it is a per-dispatch check. |
| Per-issue dispatch timestamps + window | AC2 | **In-process** daemon state (a field on a small `DaemonState`/dict held across ticks within `run_daemon`) | A re-dispatch *loop* is a within-run phenomenon; a fresh daemon start legitimately re-dispatches. In-process is correct and avoids persistence complexity. Recovery (`recovery.py`) already reconstructs cross-restart `done`/`parked` state from durable provenance, so cross-restart attempt history is not required for AC2. |
| Heartbeat timestamp | AC3 | **In-process** for the live check; **emitted to JSONL**; **written to `BH_HEARTBEAT_FILE`** (mtime-based liveness signal for the external monitor) | The in-process copy drives the daemon's own stall detection; the JSONL copy and the heartbeat file are what the external dead-man's-switch (Phase 4b) watches. |
| Structured run/outcome record (JSONL) | AC4 | **Persisted, harness-owned path** — `${BH_PROJECT_ROOT}/.baton-harness/runlog.jsonl` (NOT `.symphony/`, which is vendored-Orchestrator-owned per registry.py / harness-design §10) | Must survive process exit to be diagnosable post-mortem. Keeping it out of `.symphony/` avoids colliding with vendored state and avoids the re-vendor clobber risk. `.baton-harness/` MUST be gitignored in the target repo — `bin/init-sandbox.sh` (updated per #71/PR #72) seeds this entry alongside `.symphony/`. Path configurable via new env var (below). |
| Observability config (window size, attempt threshold, heartbeat-stall budget, severity thresholds, runlog path) | all | **Harness-owned config surface — NOT vendored `WorkflowConfig`** | `WorkflowConfig` is vendored (`vendor/symphony/config.py:18`) and would be clobbered on re-vendor. Proposed: a new `chain/obs_config.py` dataclass loaded from env (`BH_*`) with sane defaults, mirroring how `registry.py` loads `RepoConfig` from env. New env vars: `BH_RUNLOG_PATH`, `BH_REDISPATCH_WINDOW_S`, `BH_REDISPATCH_MAX`, `BH_HEARTBEAT_STALL_S`. WORKFLOW.md may *document* these but should not be the load source (it deserializes into the vendored `WorkflowConfig`). |

---

## 5. Phased breakdown

Each phase maps to one sub-issue (P0–P4b, six total) tracked under #34 as the epic; see §9 for the issue-structure decision. Each phase is independently committable, independently testable against the `_run` / `_run_worker` seam, and lands its own PR targeting `main` directly (small, independent). `Closes #34` fires only when all sub-issues have landed. Sequence is chosen so later phases consume earlier substrate.

### Phase 0 — Structured run-record substrate (AC4 foundation)
**Why first:** every other AC writes into this; building it first lets subsequent phases emit records as they go.
- New module `src/baton_harness/chain/runlog.py`: `RunLog` with an append-only `emit(event: dict)` writing one JSON line to the configured path; a `_run`-style I/O seam (a single `_write_line` function) so tests patch one symbol (mirrors `escalation.py:31` `_run` seam, finding F8).
- Define the event schema (fields: `ts` ISO-8601 UTC, `event` enum, `issue` int|null, `outcome`, `severity`, `detail`, `tick_id`). Document the schema in a module docstring.
- New `chain/obs_config.py`: `ObsConfig` dataclass + `load_obs_config()` from env, defaulting `BH_RUNLOG_PATH` to `${BH_PROJECT_ROOT}/.baton-harness/runlog.jsonl` (decided — §8 Q2). Also default `BH_HEARTBEAT_FILE` to `${BH_PROJECT_ROOT}/.baton-harness/heartbeat`.
- **Gitignore requirement (cross-ref #71/PR #72):** the `.baton-harness/` directory MUST appear in the target repo's `.gitignore`. `bin/init-sandbox.sh` already seeds `.symphony/` per #71/PR #72; this phase extends that same seeding block to include `.baton-harness/`. The operator docs (Phase 5) must note this as a setup requirement.
- Wire `run_daemon` to construct a `RunLog` once and thread it (or hold on a daemon-state object) into `_poll_and_run` / `_run_work_unit`.
- **Tests:** patch the `_write_line` seam, assert one JSON object per `emit`, assert crash-mid-write loses ≤1 line (write an invalid event after a valid one, assert the valid one parses). Assert `load_obs_config` honours env + defaults.
- **Commit boundary:** runlog + obs_config land with the daemon emitting at least dispatch + outcome events.

### Phase 1 — Alert-severity layer on escalation (AC1–AC3 transport)
- Add `severity: Literal["info","warn","critical"] = "warn"` to `escalate()` (or a thin `alert()` wrapper). `info` → runlog only; `warn`/`critical` → existing GitHub+Slack path; `critical` decorates the body with a loud marker.
- Every existing call site (`daemon.py:715,732,757,790,818,836,965`) keeps current behaviour by defaulting to `warn` — **no behavioural regression**; the change is additive.
- Emit a runlog `escalation` event from inside `escalate` regardless of severity (so the record captures every alert).
- **Tests:** extend `tests/chain/test_escalation.py` — assert `info` skips the `gh` `_run` seam, `critical` calls it and decorates the body, Slack still best-effort. Assert a runlog event is emitted (patch the RunLog).

### Phase 2 — Single-state invariant assertion (AC1)
**Depends on:** Phase 1 (uses `severity="critical"`), Phase 0 (emits violation events).
- Add `assert_single_state(labels: set[str]) -> str | None` to `after_run.py` (co-located with the constants `:76-82`) returning the violated condition or `None`.
- In `daemon.py` after the post-worker re-read (`:742-743`), call it; on violation, `escalate(..., severity="critical")` + runlog `invariant_violation` event, then park (do not silently continue).
- **Tests:** in `test_daemon.py`, drive a `_run_worker` outcome where mocked `_fetch_issue_labels` returns 0 or 2 of the invariant set; assert escalate-critical called, assert issue parked, assert runlog event. The existing `_run`/`_run_worker` patching handles all I/O.

### Phase 3 — Re-dispatch loop detection (AC2)
**Depends on:** Phase 1, Phase 0.
- Add per-issue dispatch-timestamp tracking to the daemon's cross-tick in-process state. Before each `_run_worker` dispatch (`daemon.py:726`), prune outside `BH_REDISPATCH_WINDOW_S` and check count ≥ `BH_REDISPATCH_MAX`.
- On threshold breach: skip dispatch, park the issue, `escalate(severity="critical")`, runlog `redispatch_loop` event. This also caps the uncapped `recovery.py:97-100` redispatch.
- **Tests:** `once=False` is awkward; instead unit-test the window/threshold helper directly (pure function over a timestamp list + clock), then one `test_daemon` case driving two ticks (`asyncio.run` twice, or a 2-iteration loop) asserting the second dispatch is suppressed + escalated.

### Phase 4a — Heartbeat + stall detection (AC3, in-daemon layer)
**Depends on:** Phase 0 (heartbeat → runlog), Phase 1 (stall → critical).
- At the top of each `_poll_and_run` tick (`daemon.py:984+`): (a) emit a `heartbeat` runlog event + INFO log, and (b) **write the current UTC timestamp to `BH_HEARTBEAT_FILE`** (atomic replace or open-truncate-write). The file write is best-effort (log-and-continue on failure, mirroring R2 in §7); the `mtime` of that file is what the external monitor reads.
- Store last-progress timestamp in in-process state for stall detection.
- Stall detection: when a tick observes an issue still `agent-in-progress` past `BH_HEARTBEAT_STALL_S` (ties to #33), `escalate(severity="critical")` + runlog `stall` event.
- **Tests:** assert heartbeat runlog event emitted per tick (`once=True`); assert `BH_HEARTBEAT_FILE` write is called each tick (patch the file-write helper); assert stall escalation fires when a mocked clock advances past the budget with the issue still in-progress.

### Phase 4b — External dead-man's-switch (AC3, out-of-process layer)
**Why a separate phase:** the daemon cannot page about its own death. Only an out-of-process monitor satisfies AC3's "reaches a human even if the process is dead" semantics. This is a **separate deliverable**, independent of the daemon process, and is the only component that fully closes AC3.

**What it watches:**
- `BH_HEARTBEAT_FILE` mtime — primary signal; a live daemon updates this every tick.
- Last-entry age in `runlog.jsonl` — secondary / cross-check; useful if the heartbeat file write fails but the runlog write succeeded (both are best-effort).

**Staleness threshold:** configurable (suggested default: `3 × poll_interval_s + grace_seconds`); the exact value belongs in the external monitor's config, not in the daemon's `ObsConfig`.

**Implementation options** (the plan does not mandate one; the operator chooses based on available infra):
- A cron job (`cron`/`Task Scheduler`) that checks `BH_HEARTBEAT_FILE` mtime and calls a webhook or sends an email if stale.
- An external uptime/health-check service (e.g. Healthchecks.io, Better Uptime) configured as a dead-man's-switch: the daemon PINGs the service each tick (add a `BH_HEARTBEAT_PING_URL` env var, hit it with a best-effort HTTP GET); the service alerts if no ping arrives within the window.
- A lightweight sidecar process on the same host that does the mtime check.

**Operator docs note (Phase 5):** the external monitor is a setup requirement for full AC3 coverage; without it, process-death is silent.

**Tests:** this phase is primarily documentation and integration-point definition. If a ping-URL variant is chosen, the ping call gets a `_run`-style seam and a unit test asserting it fires each tick and is best-effort (does not raise into the daemon loop).

### Phase 5 — Docs + config surfacing
- Update `README.md` and `config/WORKFLOW.md` to document the new `BH_*` env vars (`BH_RUNLOG_PATH`, `BH_HEARTBEAT_FILE`, `BH_HEARTBEAT_PING_URL`, `BH_REDISPATCH_WINDOW_S`, `BH_REDISPATCH_MAX`, `BH_HEARTBEAT_STALL_S`) and the runlog location/schema (README maintenance rule).
- Add a short ops note: where to find `runlog.jsonl`, how to tail it, what `critical` events mean, and how to set up the external dead-man's-switch (Phase 4b). Note that without the external monitor, AC3's process-death case is not covered.
- Note that `bin/init-sandbox.sh` seeds `.baton-harness/` into the target repo's `.gitignore` (Phase 0 adds this alongside the existing `.symphony/` entry per #71/PR #72); operators running an older `init-sandbox.sh` must add the entry manually.
- Update `docs/harness-design.md` if it enumerates the observability surface.
- **Commit boundary:** docs-only PR (or fold into Phase 4b if small).

---

## 6. Dependencies & sequencing vs #31, #32, #33

This issue is the **enabler**; it should land before or alongside fixes to the three:

- **#31 (torn label state):** consumes **Phase 2** (single-state assertion). #31's fix is "repair/prevent the torn state"; #34 Phase 2 is "detect + alert on it". Recommend #34 Phase 2 lands first so #31's fix has a regression signal. (Relationship per issue #34 body: "cross-cutting enabler for #31".)
- **#32 (`json.loads` crash loop):** consumes **Phase 3** (re-dispatch loop detection) and **Phase 0** (structured record). A crash loop manifests as repeated re-dispatch; Phase 3 detects/caps it and the runlog captures the crash detail for diagnosis. Recommend Phase 0 + Phase 3 land before #32's fix.
- **#33 (hung Baton):** consumes **Phase 4a** (heartbeat + in-daemon stall detection). #33 is the no-progress case AC3 references explicitly. Phase 4a's stall budget is the detection mechanism #33's fix will tune. Phase 4b (external dead-man's-switch) is independent of #33 and can land separately.

**Sequencing recommendation:** #34 Phases 0→1 are pure substrate and should merge first (no dependency on the three). Then 2/3/4 can proceed in parallel with (and slightly ahead of) #31/#32/#33 respectively, since each provides the detection signal the corresponding fix needs.

---

## 7. Risks & testability notes

**Risks:**
- **R1 — vendored-config clobber.** Putting observability config in `WorkflowConfig` would be silently lost on re-vendor (VENDORING.md re-vendor checklist). *Mitigation:* harness-owned `obs_config.py` (§4) — this is the single most important design decision in the plan.
- **R2 — runlog write failure on the unattended path.** If the runlog disk write fails overnight, we lose the very diagnosis record we built. *Mitigation:* runlog writes are best-effort (log-and-continue, never raise into the daemon loop), mirroring the Slack best-effort pattern (`escalation.py:218-225`).
- **R3 — escalation fatigue.** `critical` Slack posts on every transient hiccup train operators to ignore them. *Mitigation:* reserve `critical` for invariant violations, re-dispatch loops, and stalls; keep transient parks at `warn`.
- **R4 — in-process attempt state lost on restart.** A daemon restart resets dispatch counters (§4 accepts this). *Risk:* a genuine crash-restart loop could evade AC2. *Mitigation:* accepted; the runlog (durable) still records every dispatch, so a post-mortem can see the cross-restart loop even if the live check resets. The external dead-man's-switch (Phase 4b) provides an independent signal if repeated crash-restarts cause a liveness gap.
- **R5 — heartbeat false positives.** A legitimately long agent run looks like a stall. *Mitigation:* `BH_HEARTBEAT_STALL_S` default tuned generously; this is the knob #33 refines.

**Testability:** every phase is unit-testable against the established seams:
- `_run` seam (`escalation.py:31`, daemon module-level helpers) — patch one symbol for all gh/git I/O (`test_daemon.py:1-9` documents this).
- `_run_worker` direct patch (`AsyncMock`) for outcomes (`test_daemon.py:32,37`).
- `asyncio.run` + `once=True` for tick-scoped tests (no pytest-asyncio) (`test_daemon.py:8-9`).
- New seams to add: `runlog._write_line` (single patch point) and an injectable clock for window/stall tests (pass `now: float` or a `time_fn` param to the pure helpers so no monkeypatching of `time` is needed).

---

## 8. Design decisions (formerly open questions)

**Q1 — Alert channel / dead-man's-switch** — DECIDED: **Slack + external dead-man's-switch.**
In-process alerts (invariant violation, re-dispatch loop, detected stall) route through the existing `escalate()` severity path to GitHub comment (durable) + best-effort Slack (`escalation.py:173`). Additionally, because the daemon cannot page about its own death, the plan includes an **external dead-man's-switch** (Phase 4b) as a separate deliverable: an out-of-process monitor watches the heartbeat file mtime and/or last `runlog.jsonl` entry age and alerts if the heartbeat goes stale beyond a configurable threshold. This is the only component that fully satisfies AC3's "even if the process is dead" semantics. See Phase 4a (§5) for the in-daemon layer and Phase 4b for the external layer.

**Q2 — Runlog path** — DECIDED: `${BH_PROJECT_ROOT}/.baton-harness/runlog.jsonl` (NOT under `.symphony/`, which is vendored-Orchestrator-owned per `registry.py` / `harness-design §10`). The `.baton-harness/` directory MUST be gitignored in the target repo; `bin/init-sandbox.sh` is updated (this plan, Phase 0) to seed `.baton-harness/` into the target repo's `.gitignore` alongside the existing `.symphony/` entry added by #71/PR #72. Operators running an older `init-sandbox.sh` must add the entry manually (noted in Phase 5 operator docs). Path remains overridable via `BH_RUNLOG_PATH`.

**Q3 — Issue structure** — DECIDED: **6 sub-issues, one per phase (P0, P1, P2, P3, P4a, P4b), all under #34 as the epic/tracking issue.** See §9 for details.

**Q4 — Should `severity` replace `kind` or coexist?** This plan keeps both (`kind` for caller-side category, `severity` for routing/transport decision). If the team would rather collapse them, that changes the escalation signature and every call site. Minor, but worth confirming before Phase 1 implementation.

---

## 9. Tracking / issue structure

**Decided (Q3):** track as **6 sub-issues, one per phase**, all filed under #34 as the epic/tracking issue. #34 closes when all 6 sub-issues have merged PRs.

| Sub-issue | Phase | Title (suggested) |
|---|---|---|
| #34-P0 | Phase 0 | Structured JSONL run-record substrate + obs_config |
| #34-P1 | Phase 1 | Alert-severity layer on escalation |
| #34-P2 | Phase 2 | Single-state label invariant assertion (AC1) |
| #34-P3 | Phase 3 | Re-dispatch loop detection + cap (AC2) |
| #34-P4a | Phase 4a | Per-tick heartbeat file + in-daemon stall detection (AC3) |
| #34-P4b | Phase 4b | External dead-man's-switch spec + operator docs (AC3 out-of-process) |

**Closing-keyword footgun (per CLAUDE.md):** two known traps apply here:

1. `Closes #100, #101` only closes #100. Each issue needs its own keyword on a separate line or clause: `Closes #X` `Closes #Y`.
2. `Closes #N` in a PR that merges into a **feature branch** (not `main`) does NOT auto-close the issue — GitHub only fires on the default branch. Since each sub-issue's PR targets `main` directly, this is not a risk for the normal path. If a feature branch is introduced (e.g. to stage multiple phases together), the closing keywords MUST be repeated on the integration PR that merges into `main`; they do not propagate from the sub-branch PRs.

**Epic closure:** #34 should be closed manually (with a brief summary comment) once all six sub-issue PRs have merged, rather than relying on a `Closes #34` on any single PR — that avoids the ambiguity of which phase is "last" and keeps the epic visible as a tracking item until all work lands.
