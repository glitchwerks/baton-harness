# Plan: #40 — Above-container failure recovery (S2.3 residual)

**Issue:** baton-harness #40 — "Above-container failure recovery: orphan processes, OOM, credential corruption (S2.3)"
**Milestone:** #2 — Failure-mode hardening (last v1 blocker)
**Status:** planning — awaiting user confirmation on clarifying questions before implementation
**Author:** project-planner (terminal sub-agent)
**Date:** 2026-06-17

---

## 1. Problem framing

S2.3 (`docs/open-questions.md:69-82`) is "no failure-recovery story above the container." The in-flight / stuck-`agent-in-progress` half is already resolved by the merged #27 spec (`recovery.py` reconstruction + orphan rule 3b re-dispatch; `docs/open-questions.md:72,77`). #40 owns the three residual gaps that live *above* the container's normal crash/restart path:

| Gap | Source | Failure signature |
| --- | --- | --- |
| G1 Orphan `claude` process | `worker.py:100-105` — PID never persisted; dies with the Python process | `claude -p` segfaults or harness exits between dispatch and registration → leaked process, slot never reclaimed |
| G2 Container OOM | multiple Claude sessions hold context | all in-flight work lost, **no notification fires** (interacts with #34) |
| G3 Credential corruption mid-run | `_auth.py` reads `GH_TOKEN`/`GITHUB_TOKEN` from env (`docs/open-questions.md:80`) | every subsequent run fails silently until repaired |

**Sibling boundaries (do NOT re-solve — `docs/open-questions.md:72`):** #31 (after_run idempotence / torn labels), #33 (liveness/health detection — MERGED), #34 (unattended failure signal / observability — provides the `alert()` severity layer this plan reuses).

The hard design lens for #40, per the brief: the daemon is **serial, `max_concurrent` effectively 1** (B-I3 invariant; `docs/open-questions.md:87`), single-tenant, overnight-run. Apply YAGNI: every piece must earn its complexity against that profile, and chain-side / external solutions are preferred over vendored (VP-*) changes because re-vendoring clobbers vendored edits (`CLAUDE.md` project § + `VENDORING.md`).

---

## 2. Credential-model reconciliation (CLARIFYING QUESTION — blocks G3 scoping)

The issue says "credential-**file** corruption" / "credentials **volume**." Explore found **env-based auth only** — `_auth.py` reads `GH_TOKEN`/`GITHUB_TOKEN` from the environment (`docs/open-questions.md:80`; map §4), `worker.py:100` inherits the parent env for `ANTHROPIC_API_KEY`, and **no credential file or volume is mounted or referenced by the harness**.

Two reconciling hypotheses:
- **(a)** The deployment mounts credentials as a file the *container runtime* materializes into env vars at startup (e.g. a Docker secret, a `.env` file `source`d by an entrypoint). The harness never sees the file; corruption manifests purely as a missing/garbage env var by the time `_auth.py` runs.
- **(b)** The issue's mental model predates env-based auth and "credentials volume" is now stale terminology for "the env-injected token."

Under **both** hypotheses, the harness-observable symptom is identical: a missing, malformed, or unauthenticated token at startup. The harness cannot health-check a file it does not reference, and **must not** start probing the filesystem for credential files (that is both a credentials-hygiene risk per `CLAUDE.md § Credentials and Secrets` and scope creep). So the implementable G3 surface is **startup credential validation of the env-injected tokens**, regardless of which hypothesis holds.

> ⚠️ **Question for user (Q1):** Confirm the credential model is env-injected (no file the harness should read), so G3 is scoped to *startup env-token validation* rather than file-volume health-checking. If a real file/volume exists that the harness is expected to validate, name its path and mount point — that changes G3's design.

**Working assumption (pending Q1):** env-injected; G3 = startup validation only.

---

## 3. Per-gap implement / defer decision table

| Gap | Decision | Rationale | Earns complexity? |
| --- | --- | --- | --- |
| **G1 — orphan `claude` reap** | **IMPLEMENT (lightweight, chain-side `pgrep` sweep)** | A leaked `claude -p` wastes CPU/RAM on a memory-constrained box (feeds G2/OOM) and is the exact "harness exits mid-dispatch" case #40 names. But a **vendored PID registry** (storing PID in `OrchestratorState`, persisting it) is over-engineered for a serial daemon: at most one worker is ever in flight, and `recovery.py` already re-derives in-flight state from git+labels on each start. A startup `pgrep`-based sweep of stray `claude` processes whose issue is no longer in-flight is sufficient and adds **zero vendored surface**. | Yes — but only the cheap form |
| **G2 — OOM notification** | **IMPLEMENT (lean — startup "ungraceful prior exit" detection + alert)** | The real harm in the issue is "**no notification fires**." The harness cannot catch its own OOM-kill (SIGKILL is uncatchable; nothing runs in-process). The tractable, in-scope piece: detect on the *next* startup that the prior run ended ungracefully (a startup marker file written at boot and cleared on graceful shutdown; if present at boot → prior run died hard) and emit `alert(severity="critical")`. This is the above-container analogue of the heartbeat #33 provides *during* a run. | Yes — minimal |
| **G3 — credential validation** | **IMPLEMENT (call existing `validate_github_token()` at startup + new `ANTHROPIC_API_KEY` presence probe)** | `validate_github_token()` already exists (`_auth.py:223-351`) but is only called on-demand, never at daemon startup (map §4). Wiring it into `run_daemon` startup is near-free and directly addresses "every subsequent run fails silently." `ANTHROPIC_API_KEY` is **structurally** probed for presence only (never value-inspected, per `CLAUDE.md § Credentials and Secrets`). | Yes — mostly reuse |
| **Vendored PID registry** | **DEFER (reject for v1)** | Requires VENDOR-PATCH to `worker.py` + `orchestrator.py` + state persistence; clobber-prone on re-vendor; buys nothing the `pgrep` sweep doesn't for a serial daemon. | No |
| **`state.json` load-on-startup** | **DEFER to a NEW issue (out of #40)** | Real gap (`state.py` has `persist()` but no `load()`; daemon discards retry_queue on restart — map §3, verified: only `persist` at `state.py:87`). But it is a *state-continuity* concern, not a *failure-mode* one, and the only loss is the retry queue (re-derivable: unfinished issues still carry `agent-ready`/`agent-in-progress` labels that `recovery.py` re-reads). Bundling it bloats #40 and crosses into #31's after_run/crash-safety territory. See §7 flag. | No — separate issue |
| **Slot exhaustion / reclamation** | **DEFER (near-non-issue for serial daemon)** | `available_slots = max_concurrent - running_count` with `max_concurrent` effectively 1 (map §1). A "leaked slot" can only strand the single serial slot — and the daemon constructs a **fresh** `OrchestratorState` per work unit (map §1, §3), so slot state does not even survive across work units to leak. The G1 process sweep covers the only real-world residue (a leaked *process*, not a leaked *slot accounting entry*). Revisit when v2 concurrency lands (`docs/open-questions.md:87`). | No |

**Net v1 #40 scope:** one startup reconciliation sweep with three checks (credential validation, ungraceful-prior-exit detection, orphan-process sweep), all chain-side, all emitting through the existing `alert()` path. No vendored changes.

---

## 4. Startup reconciliation sweep — design

**Where it attaches:** `daemon.py` `run_daemon`, in the startup region after observability/`obs`/`runlog` init (`daemon.py:1457-1489`) and **before** the heartbeat thread start (`daemon.py:1491-1518`) and the main poll loop (`daemon.py:1520`). This mirrors the established best-effort startup-block pattern already in `run_daemon` (each block wrapped in `try/except … noqa: BLE001`, never raises — `daemon.py:1461-1489`).

Proposed new chain-side module: `src/baton_harness/chain/reconcile.py`, exposing `async def reconcile_startup(repo_cfgs, obs, runlog) -> None`. Called once from `run_daemon` startup.

**Order of checks (fail-fast ordering — cheapest/most-fundamental first):**

1. **Credential validation (G3) — FIRST, can be fatal.**
   - Call `validate_github_token()` (`_auth.py:223-351`). On `TokenValidationError`: emit `alert(severity="critical", summary="…")` then **exit non-zero** — a daemon with no valid GH token can do no useful work and would otherwise "fail silently on every run" (the exact S2.3 symptom). This is the one check allowed to halt startup.
   - `ANTHROPIC_API_KEY` presence probe: structural only — check the env var is set and non-empty (NEVER inspect or log the value; NEVER probe length — `CLAUDE.md § Credentials and Secrets`). Absent → `alert(severity="critical")` and exit non-zero (workers will all fail at `worker.py:100`).

2. **Ungraceful-prior-exit detection (G2) — informational/critical, non-fatal.**
   - At startup, check for a marker file `${BH_PROJECT_ROOT}/.baton-harness/daemon.alive` (sibling of the runlog, `.baton-harness/` NOT `.symphony/` — the latter is vendored-Orchestrator-owned; per the #34 memory decision).
   - If present at boot → the prior run did not shut down gracefully (likely OOM-kill or hard crash) → `alert(severity="critical", summary="prior daemon run ended ungracefully (possible OOM); in-flight work may have been lost")`. Then (re)create the marker.
   - Register an atexit / finally-block clear of the marker on graceful shutdown (`run_daemon` already has a `finally` cleanup region that joins the heartbeat thread — `daemon.py` cleanup; clear the marker there).
   - This is the only tractable OOM "notification" — the harness cannot self-report an uncatchable SIGKILL, so it reports it on the *next* boot.

3. **Orphan `claude` process sweep (G1) — warn, non-fatal.**
   - `pgrep -f 'claude -p'` (or platform-equivalent; Windows host → `Get-CimInstance Win32_Process` filter via the `powershell` skill if the daemon runs on Windows; **clarify deploy target — Q2**). For each stray process whose associated issue is **not** in the freshly-reconstructed in-flight set (from `recovery.reconstruct` / labels), the process is an orphan.
   - **v1 conservative default: detect-only, `alert(severity="warn")`** with the PID list — do NOT auto-kill in v1. This mirrors the `worktree_gc="detect"` default chosen for #33's worktree sweep (`recovery.py:534-541`, `daemon.py:1857`). A detect→reclaim flag (`BH_ORPHAN_PROC_GC`) can be added later, same shape as `worktree_gc`. Auto-killing a process whose issue association is uncertain risks killing a legitimately-running worker — the conservatism guarantee from the worktree scan (`recovery.py:560-571`) applies.
   - **Association caveat:** the harness does not persist PID→issue mapping (map §1), so "which issue does this `claude` belong to" is not directly recoverable. v1 sweep therefore reports *any* `claude -p` found at startup when the daemon believes nothing is in flight (startup = no worker should be running yet). This is sound for the serial daemon: at boot, a live `claude -p` is by definition orphaned from a prior crashed run.

**Emission contract:** ALL three checks emit exclusively through the existing `alert(owner, repo, issue, summary, severity=…, runlog=runlog)` API (`escalation.py:192-273`). No new signalling channel is built (reuses #34's severity-routing + GitHub-comment-durable + Slack-best-effort). Repo-level checks pass `issue=None` (the `alert`/`escalate` path handles `None` by skipping the GitHub comment and logging a WARNING + attempting Slack — `escalation.py:84-99,129-140`). Each check is independently `try/except`-guarded so one failing check never aborts the others or the daemon (matching `daemon.py:1461-1489`).

---

## 5. Phase breakdown

**Phase 0 — Scaffolding + tests (TDD-first per `superpowers:test-driven-development`).**
- New `src/baton_harness/chain/reconcile.py` with `reconcile_startup(...)` skeleton (no-op, best-effort guarded).
- New `tests/chain/test_reconcile.py`. Test seam follows the established daemon pattern: patch + `asyncio.run` + `once=True`, no pytest-asyncio (per `tests/chain/test_daemon.py`; #34 memory).
- Marker-file path constant on `ObsConfig` or `reconcile.py` (`${BH_PROJECT_ROOT}/.baton-harness/daemon.alive`).

**Phase 1 — G3 credential validation.**
- Wire `validate_github_token()` into `reconcile_startup` (fatal on failure → `alert` critical + non-zero exit).
- Add `ANTHROPIC_API_KEY` structural presence probe (presence-only; no value/length inspection).
- Tests: missing GH token, classic-PAT rejection, missing `ANTHROPIC_API_KEY`, happy path. Reuse `sleep_fn` injection (`_auth.py:223`) to avoid real retry delays.

**Phase 2 — G2 ungraceful-exit detection.**
- Marker write at startup; detect-if-present → `alert` critical; marker clear in `run_daemon` `finally` cleanup.
- Tests: marker present at boot → critical alert; clean boot → no alert; graceful shutdown clears marker.

**Phase 3 — G1 orphan-process sweep.**
- `pgrep`-based detect-only sweep → `alert` warn with PID list. Platform-guarded (Q2). Injected process-lister for testability (no real `pgrep` in unit tests).
- Tests: stray process at boot → warn alert; no stray → no alert; sweep failure suppressed (no daemon abort).

**Phase 4 — Wire into `run_daemon` + docs.**
- Call `reconcile_startup` at `daemon.py:~1490` (after obs init, before heartbeat thread). Guarded.
- Update `docs/open-questions.md` S2.3 (§6).
- Update `README.md` if startup behavior / new env knob (`BH_ORPHAN_PROC_GC` if added) affects how the daemon is run (`CLAUDE.md § README Maintenance`).

Each phase is an independently-mergeable sub-PR off a `feature-40-above-container-recovery` primary branch (per `CLAUDE.md § Git Commits` primary+sub-branch pattern), or a single focused PR if the user prefers given the small surface.

---

## 6. `docs/open-questions.md` S2.3 update (Phase 4)

Rewrite the S2.3 "Specific gaps" bullets (`docs/open-questions.md:76-80`) to reflect #40 resolutions:
- Orphan `claude` process → **resolved (#40): startup `pgrep` detect-only sweep emits `alert(warn)`; auto-reclaim deferred behind a future `BH_ORPHAN_PROC_GC` flag mirroring `worktree_gc`.**
- Container OOM → **resolved (#40): ungraceful-prior-exit marker detected on next boot → `alert(critical)`. The harness cannot self-report an uncatchable SIGKILL; next-boot detection is the tractable notification.**
- Credential corruption → **resolved (#40): `validate_github_token()` + `ANTHROPIC_API_KEY` presence probe run at daemon startup; fatal failure exits non-zero with a critical alert. Scope confirmed as env-injected tokens (Q1).**
- Add a note: **`state.json` load-on-startup (retry-queue continuity) deferred to new issue #TBD — see §7.**
- Flip S2.3 status from "PARTIALLY RESOLVED" → "**RESOLVED (v1)**" once #40 merges, with the state.json deferral explicitly carried as the one residual (tracked elsewhere).

---

## 7. Deferred-out flags (recommend new issues)

- **FLAG-A — `state.json` load-on-startup (retry-queue continuity).** `state.py` has `persist()` but no `load()` (verified: only `persist` at `state.py:87`); `run_daemon` discards `state.json` and starts fresh, losing `retry_queue` (map §3). Recommend a **new issue in Milestone #2 or a follow-up milestone**: implement `state.load(path)` with atomic-write + corruption fallback (current persist is non-atomic on Windows, no backup — map §3; this corruption-on-partial-write is itself a mini above-container failure mode). Crosses into #31's crash-safety territory — coordinate ownership with #31 before opening. **Recommend deferring out of #40.**
- **FLAG-B — orphan-process auto-reclaim (`BH_ORPHAN_PROC_GC=reclaim`).** v1 ships detect-only. Auto-kill is a follow-up once detect-only telemetry confirms the sweep's false-positive rate is acceptable (same maturation path #33's worktree sweep is on). **Recommend deferring; can live in #40's DoD as an explicit deferral-with-rationale rather than a new issue.**

---

## 8. Invariants & risks

- **INV-1 (best-effort startup):** `reconcile_startup` and each sub-check must NEVER raise into `run_daemon` — except the two *intentionally fatal* credential checks, which `sys.exit` non-zero **after** emitting a critical alert. All other failures are caught + logged + suppressed (matches `daemon.py:1461-1489`, `1872`).
- **INV-2 (no vendored surface):** #40 touches only chain-side files (`chain/reconcile.py`, `chain/daemon.py`, `_auth.py` is already chain-side at `src/baton_harness/_auth.py` — NOT vendored). **Zero VENDOR-PATCH** required. This is a deliberate scope guard: the rejected PID-registry approach would have forced VENDOR-PATCHes to `worker.py`/`orchestrator.py` (clobber-prone on re-vendor — `VENDORING.md`).
- **INV-3 (credentials hygiene):** `ANTHROPIC_API_KEY` and the GH token are NEVER value-inspected, length-probed, or logged. Presence/validity checks only (`CLAUDE.md § Credentials and Secrets`).
- **INV-4 (reuse signalling):** no new notification channel — `alert()`/`escalate()` only (`escalation.py:192-273`).
- **RISK-1 (platform — pgrep):** `pgrep -f` is POSIX. If the daemon runs on the Windows host (the dev env is `win32`), the sweep needs a Windows process-enumeration path. **Q2 below.** Mitigation: inject the process-lister; default to a no-op + `alert(warn, "orphan-proc sweep unsupported on this platform")` if the platform is unrecognized — never break startup.
- **RISK-2 (orphan false-positive):** at boot, a `claude -p` could (rarely) belong to an *unrelated* concurrent operation, not a crashed worker. Detect-only default + warn-severity (not auto-kill) keeps this safe — no destructive action on an uncertain signal.
- **RISK-3 (marker-file races):** if two daemon instances ever run (forbidden by B-I3, but defensively), the marker could thrash. Acceptable for v1 single-tenant; note it.

---

## 9. Sibling coordination

- **#33 (MERGED):** reuse its `worktree_gc` detect/reclaim *shape* (`recovery.py:534-541`) and its heartbeat/`alert` machinery. No conflict.
- **#34:** hard dependency — #40 reuses `alert()`/severity layer (`escalation.py:192-273`), `RunLog`, and `ObsConfig`/`.baton-harness/` path convention. Confirm #34 has merged (or sequence #40 after it). The `obs is not None` guards in `run_daemon` (`daemon.py:1506`) show #34's structures are already present in `HEAD a92dfd7`.
- **#31 (after_run idempotence / torn labels):** **coordination needed for FLAG-A only.** `state.json` atomic-write/corruption-recovery overlaps #31's crash-safety remit. Do NOT implement FLAG-A under #40; raise it with the user / #31 owner first.

---

## 10. Clarifying questions for the user

- **Q1 (credential model — blocks G3):** Confirm credentials are env-injected (no file the harness reads), so G3 = startup env-token validation. If a real file/volume exists, name its path/mount. (See §2.)
- **Q2 (deploy platform — affects G1):** Does the daemon run in a Linux container (POSIX `pgrep`) or on the Windows host? Determines the orphan-process sweep implementation; either way it stays best-effort and non-fatal.
- **Q3 (defer confirmations):** Confirm the defers — (a) vendored PID registry rejected, (b) `state.json` load-on-startup split to a new issue (FLAG-A), (c) slot reclamation deferred (serial-daemon near-non-issue), (d) orphan auto-reclaim deferred (detect-only v1, FLAG-B).
- **Q4 (PR shape):** Given the small chain-only surface, do you want one focused PR or the primary+sub-branch breakdown of §5?

---

## 11. Sources

- Issue #40 body (verbatim in dispatch brief) and Milestone #2 framing.
- `docs/open-questions.md:69-82` — S2.3 partial-resolution text and the four gap bullets; `:87` — serial `max_concurrent=1` (S2.4); `:72,77` — in-flight half resolved by design + sibling mapping.
- `src/baton_harness/vendor/symphony/worker.py:100-120` — subprocess dispatch, no PID persistence, timeout-kill.
- `src/baton_harness/vendor/symphony/state.py:87` — `persist()` exists; **no `load()`** (verified via Grep `def load|def persist` → only `persist`).
- `src/baton_harness/chain/daemon.py:1424-1518` — `run_daemon` startup region (best-effort obs/runlog/tally init pattern, heartbeat thread); `:1855-1873` — phase-3 worktree GC invocation + suppression pattern.
- `src/baton_harness/chain/escalation.py:64-189` (`escalate`), `:192-273` (`alert` severity-routing API).
- `src/baton_harness/_auth.py:223-351` — `validate_github_token()` (chain-side, on-demand-only today).
- `src/baton_harness/chain/recovery.py:534-583` — `scan_orphan_worktrees` detect/reclaim shape + conservatism guarantee (reused as G1's template).
- Embedded Explore map (HEAD `a92dfd7`) §§1-7 + integration points.
- Memory `project-baton-harness-34-observability` — `.baton-harness/` (not `.symphony/`) path decision; `alert()` severity reuse; test seam (no pytest-asyncio).
- `CLAUDE.md § Credentials and Secrets`, `§ README Maintenance`, `§ Git Commits`; project `CLAUDE.md` vendoring policy + `VENDORING.md`.
