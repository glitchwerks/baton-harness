---
title: Unified preflight "doctor" / smoke-test (issue #193)
touches:
  - src/baton_harness/chain/doctor.py
  - src/baton_harness/chain/cli.py
  - src/baton_harness/chain/reconcile.py
  - src/baton_harness/chain/daemon.py
  - src/baton_harness/chain/sandbox_config.py
  - bin/run-daemon.sh
  - tests/chain/test_doctor.py
  - tests/chain/test_cli_doctor_gate.py
  - tests/chain/test_reconcile.py
  - tests/chain/test_reconcile_oauth_cred.py
  - tests/chain/test_reconcile_git_credential_helper.py
  - README.md
  - docs/smoke-test-daemon.md
skills_relevant:
  - python
  - github-actions
---

# Unified preflight "doctor" / smoke-test — plan for #193

> **PENDING USER RATIFICATION BEFORE IMPLEMENTATION.** The original 8 design decisions (D1-D8) were
> accepted as **provisional defaults** by the router on 2026-07-01 while the user was away; **D9**
> (Rev 2) and **D10** (Rev 3) are newer and also unratified (see § Decisions (locked)). All ten
> must be **ratified by the user** before any code is written. This plan is otherwise final.

> ## Rev 3 — project-reviewer findings pass (2026-07-19)
>
> Resolves the six findings (2 BLOCKING, 4 CONCERN) plus the minor-cleanup list from
> project-reviewer. Plan-editing only; no design decision's substance flipped. Each finding was
> addressed — none silently dropped. Changelog:
>
> - **BLOCKING #1 — exception contract for `Check` callables (§3).** Added an explicit,
>   load-bearing contract: a `Check` that raises is caught by the runner and becomes
>   `CheckResult(status=FAIL, severity=check.severity, detail=repr(exc), fix=check.fix)`; the
>   exception never propagates out of `run_report` **or** `run_gate`. Without this, an uncaught
>   exception in `run_gate` would bypass `SystemExit(1)` and dump a raw traceback — the exact
>   cryptic-crash failure #193 exists to eliminate. New §14 test line covers it.
> - **BLOCKING #2 — Phase B scope disambiguated (§4, §8, §14, Phase 4).** Committed to the safe
>   reading: `run_gate(POST_BOOTSTRAP)` inserts **after** the native G3a/b/c/d block (between
>   `reconcile.py:280` end-of-G3d and `:282` start-of-G2), **not** as reconcile's first step, and
>   executes **only** the net-new checks `RULESET_MAIN`, `RULESET_FEATURE`, `LABELS_PRESENT`,
>   `GH_REPO_ADMIN`. Rationale (now cited): `verify-recovery.sh` greps daemon stderr for
>   `"ANTHROPIC_API_KEY must not be set"` (G3b, verify-recovery.sh:428) and
>   `"Startup credential check failed"` (G3a, verify-recovery.sh:480); if run_gate ran first and
>   `SystemExit`ed on a failing ruleset/label check, native G3a/G3b would never emit their text and
>   those greps would fail. §4's "Covers G3a/G3c" language is reframed as describing the
>   **standalone** runner's full-catalog coverage only — not what run_gate executes in the daemon.
>   Every "first step / before G2/G1" phrasing (§4, §8, §14, Phase 4) was rewritten.
> - **CONCERN — `installation_token` on `DoctorContext` (§3, §4).** Added
>   `installation_token: str = ""` to `DoctorContext` so Phase B checks receive the minted App
>   token by value; §4 now states `reconcile_startup` threads its own `installation_token`
>   parameter (reconcile.py:145) into the context it builds — honoring the "token by value, never
>   via env" invariant (cli.py:404-414).
> - **CONCERN — `daemon_native` field (§3, §6).** A single `phase` letter could not distinguish
>   "handled by native G3/tripwire code in the daemon" from "run_gate executes it." Added
>   `daemon_native: bool = False` to the `Check`/`CheckResult` description. `run_gate` **excludes**
>   any `daemon_native=True` check (native code already covers it); `run_report` (standalone)
>   includes everything. daemon-native set =
>   `{GH_AUTH, CRED_ANTHROPIC_UNSET, CRED_OAUTH_VOLUME, GIT_CRED_HELPER, FORCE_PR_TRIPWIRE}`. §6's
>   table gains a `daemon_native` column. The A/B dual phase marks (`CRED_ANTHROPIC_UNSET`,
>   `GIT_CRED_HELPER`) collapse to `A`; `CRED_OAUTH_VOLUME` keeps `B`. A table note records that
>   for daemon-native rows, `phase` only tells `run_report` the standalone auth-class — run_gate
>   skips them regardless of phase.
> - **CONCERN — Phase 4 reconcile-test fixtures (Phase 4, frontmatter).** Phase 4 now explicitly
>   requires an autouse `_patch_doctor_run_gate` (or equivalent) neutralizing fixture in **all
>   three** reconcile test files, mirroring the `_patch_oauth_cred_path`/
>   `_patch_git_credential_helper` precedent — otherwise the net-new fatal checks
>   (`RULESET_MAIN`/`RULESET_FEATURE`/`LABELS_PRESENT`) fire and break those suites. Added
>   `tests/chain/test_reconcile_oauth_cred.py` and `tests/chain/test_reconcile_git_credential_helper.py`
>   to the frontmatter `touches:` list.
> - **CONCERN — wrong conftest citation (Phase 4).** Dropped the misleading `conftest.py:22-24`
>   reference. Verified: `conftest.py` (lines 26-30) patches
>   `baton_harness.chain.daemon.reconcile_startup` — the daemon.py import path — not
>   `baton_harness.chain.reconcile.reconcile_startup`, so tests calling
>   `reconcile.reconcile_startup(...)` directly never hit it. Phase 4's note now describes the
>   per-file autouse fixtures that actually govern reconcile tests.
> - **BLOCKING #2 side-effect check (§4/§6).** As the task required, I confirmed the safe reading's
>   ripple into §6: because `CRED_ANTHROPIC_UNSET` (G3b) is daemon-native, `run_gate(PRE_BOOTSTRAP)`
>   no longer executes it in the daemon path, so "ANTHROPIC_API_KEY unset" was **removed** from
>   §4's Phase-A daemon-covers list (native G3b still owns it at reconcile timing — no behavior
>   change from today). Same logic pulls `GIT_CRED_HELPER` and `FORCE_PR_TRIPWIRE` out of the
>   Phase-A run_gate execution set. No other §6 phase assignment changed.
> - **Placement side-effect on verify-recovery.sh recovery scenarios (§12).** Verified the second
>   integrity half the placement touches: G2/G1/SIGTERM scenarios must reach the poll loop *past*
>   the new gate, so they now require the target repo to have both rulesets + 5 labels provisioned.
>   The harness already runs against the real, fully-provisioned repo (verify-recovery.sh:149,236,
>   270-286), so they still pass — but this is a **documented precondition change**, not "unchanged
>   behavior." §12 rewritten to state both halves and flag the after-G1 alternative placement if the
>   CI test repo isn't ruleset/label-provisioned. (The bare "unchanged" claim was stale under the new
>   placement.)
> - **Minor cleanups.** (a) `FORCE_PR_TRIPWIRE` given a concrete decision (**D10**): phase A,
>   CRITICAL, `daemon_native=True` (existing cli.py:191-213,351-359 tripwire owns the daemon path;
>   standalone reports it via a `Check` now authored in Phase 1; fold-in deferred) — resolving its
>   dangling "Q8". (b) All dangling `Q1/Q2/Q4/Q5/Q6/Q7/Q8`
>   references resolved to their locked `D1–D8` decisions (Q1→D1, Q2→D2, Q4→D4, Q5→D5, Q6→D6,
>   Q7→D7, Q8→D8). (c) The "G3d text preserved for operator consistency, **not** a test
>   dependency" claim (§6, §10, D4 row) was **corrected**: G3d's alert text IS pinned by
>   `tests/chain/test_reconcile_git_credential_helper.py:305` (asserts `"gh auth setup-git" in
>   message`) — it is not pinned by `verify-recovery.sh` (which has no G3d scenario), but it is a
>   test dependency via that reconcile test.

> ## Rev 2 — re-validation pass (2026-07-19)
>
> The user, asked to ratify as-is, replied: *"This may need to be revisited as there have been
> quite a few architectural changes."* This revision re-verifies every file:line anchor against
> the current working tree and reconciles the plan with four changes that landed since
> 2026-07-01. **No design decision's substance flipped** — every D1–D8 still holds on the merits;
> only their embedded line anchors drifted. The changelog:
>
> - **Line-number drift fixed everywhere.** `reconcile.py`'s startup gates, `cli.py`'s load/gate
>   path, `daemon.py`'s `_should_launch_worker`, `sandbox_config.py`, `ruleset_status.py`,
>   `run-daemon.sh`, `verify-recovery.sh`, and `pyproject.toml` all shifted. Anchors updated in
>   place (not appended), so the body carries no stale citations.
> - **New gate G3d (issue #219) added to the catalog.** `reconcile.py` now runs **six** startup
>   gates (G3a/b/c/**d** + G2/G1), not five. G3d is a `git config` credential-helper presence probe
>   (`reconcile.py:257-280`). Added as catalog row `GIT_CRED_HELPER` (§6) and as **D9** (§13) so it
>   enters the ratification conversation. **Flagged for user attention** — see the return summary.
> - **#192 / PR #194 resolved.** PR #194 **merged 2026-07-03**; issue #192 is **closed**
>   (WebFetch-verified 2026-07-19, `github.com/glitchwerks/baton-harness/pull/194`). The setup-time
>   `BWS_ACCESS_TOKEN` notice is now live on `main` at `bin/setup-env.sh:508-514`. Phase 5's
>   "re-check later" placeholder is replaced with a concrete instruction (§12, §15).
> - **#200 App-auth verified.** The `ENV_BWS_ACCESS_TOKEN` check targets `app_auth.py`'s
>   `os.environ.pop("BWS_ACCESS_TOKEN", ...)` at `app_auth.py:402/446` — **still accurate** after
>   #200 shipped `app_auth.py`. No change needed.
> - **#224 (vendor assimilation) irrelevant to doctor design** — noted; the vendored `symphony`
>   tree is now normally lint/type-checked, no exclusions to route around if the doctor ever
>   touches it.
>
> Anchors re-confirmed **unchanged** and deliberately NOT edited: `app_auth.py:402/446`,
> `bws_client.py:146`, `verify-recovery.sh:7-10` (Linux-only note).

> Citation note (Rev 1, 2026-07-01): file:line anchors were originally read against the working
> tree at `I:/ai/claude/baton-harness` (branch `main`, HEAD `c9fc4f1`). That pass had no
> `gh`/GitHub-MCP access. Rev 2 (2026-07-19) re-verified every anchor against current `main` and
> resolved the live GitHub state for #192/#194 via WebFetch (public repo). No `unverified:`
> live-state claims remain.

## 1. Problem & goal

The harness has **five separate preflight surfaces** that each check a slice of "is this host +
repo initialized to run the daemon", with no single source of truth and no operator-facing
"is everything ready?" command. One of those surfaces — `reconcile.py`'s startup sweep — now runs
**six** fatal/recovery gates (G3a/b/c/**d** + G2/G1) after issue #219 added G3d (git
credential-helper presence, `reconcile.py:257-280`); it ran five when Rev 1 was drafted. A
misconfigured repo can boot the daemon and then silently **park every issue** at the per-launch
ruleset gate (`_should_launch_worker`, `daemon.py:206-411`; the park+comment path fires at
`daemon.py:376-411` and issue-park at `daemon.py:2245`), or fail deep inside `bootstrap_secrets`
with a cryptic `BwsClientError` instead of a clear "BWS_ACCESS_TOKEN is not set" message.

#193 asks for one consolidated **doctor** with two modes:

1. **Standalone** — operator-facing, checks every init prerequisite, reports pass/fail with the
   exact fix step for each gap, does not start the daemon.
2. **Auto-gate** — runs at daemon startup and **hard-exits before any work** if a CRITICAL item is
   uninitialized; non-critical gaps warn and proceed.

Hard constraints (from the issue): consolidate rather than duplicate; never read/print secret
values (presence/shape only); the standalone mode must not hard-block dev-box contexts where a
check legitimately cannot pass; every message names the item, the env var/file, and the
provisioning step.

## 2. Current preflight surface (the consolidation target)

| # | Where | Check | Anchor (Rev 2, 2026-07-19) |
|---|-------|-------|--------|
| a | `bin/run-daemon.sh` | host.env sourced; `BH_PROJECT_ROOT` set | run-daemon.sh:80-112 |
| b | `bin/run-daemon.sh` | `.bh/config.env` exists | run-daemon.sh:161-165 |
| c | `bin/run-daemon.sh` | `BH_REPO_OWNER`/`BH_REPO_NAME` present in config | run-daemon.sh:167-172 |
| d | `bin/run-daemon.sh` | 5 required labels exist in target repo | run-daemon.sh:186-214 |
| e | `bin/run-daemon.sh` | `.symphony/` is gitignored in `BH_PROJECT_ROOT` | run-daemon.sh:216-228 |
| f | `cli.py main` | workflow config loads | cli.py:274-282 |
| g | `cli.py main` | `.bh/config.env` keys valid + `gh api` repo exists | cli.py:288-299, sandbox_config.py:60-66,288 |
| h | `cli.py main` | registry loads; `BH_PROJECT_ROOT` is a dir | cli.py:302-349 |
| i | `cli.py main` | force-pr-not-merge hook self-test | cli.py:191-213,351-359 |
| j | `cli.py main` | `bootstrap_secrets` (BWS PEM fetch); `validate_daemon_token` | cli.py:361-402 |
| k | `reconcile.py` | G3a GitHub token valid (fatal) | reconcile.py:182-208 |
| l | `reconcile.py` | G3b `ANTHROPIC_API_KEY` unset (fatal) | reconcile.py:210-228 |
| m | `reconcile.py` | G3c OAuth cred volume present (fatal) | reconcile.py:230-255 |
| m2 | `reconcile.py` | **G3d git credential-helper present (fatal, #219 — NEW since Rev 1)** | reconcile.py:257-280 |
| n | `reconcile.py` | G2 stale `daemon.alive` marker (non-fatal critical) | reconcile.py:282-303 |
| o | `reconcile.py` | G1 orphan `claude -p` procs (non-fatal warn) | reconcile.py:305-327 |
| p | `daemon.py` | **per-launch** ruleset MATCH gate | daemon.py:206-411 |

Reusable helpers the doctor should call rather than reimplement:
- `ruleset_is_provisioned(owner, repo, ...)` → 4-state `RulesetStatus`
  (MATCH/DRIFT/ABSENT/ERROR) — `RulesetStatus` enum at ruleset_status.py:114-146;
  `ruleset_is_provisioned` def at ruleset_status.py:581.
- `sandbox_config.read_and_validate(path, *, run)` — parses + validates config.env keys and
  exports the `BWS_*` twins (sandbox_config.py:206). `_REQUIRED_KEYS` at :60-66; the optional
  secret-ID shape check at :196-197; the `gh api repos/{owner}/{repo}` existence probe at :288.
- `_auth.validate_daemon_token` / `validate_github_token` — token validation
  (imported in reconcile.py:39-43).
- `bws_client.fetch_secret(secret_id, *, access_token, run)` — shells out to the **`bws`
  binary** (`["bws", "secret", "get", secret_id]`, bws_client.py:146). This confirms the `bws`
  CLI is a **runtime** prerequisite, not just a setup-time one.

Note conceptual split: items (n)/(o) are **post-crash recovery-state** checks, not
"is-this-initialized" readiness checks. The doctor is a readiness tool; G2/G1 stay in
`reconcile.py` (see §5).

## 3. Proposed architecture

One shared catalog, two runners, two integration points.

**New module `src/baton_harness/chain/doctor.py`:**

- `Severity(Enum)` = `CRITICAL | WARNING`.
- `CheckStatus(Enum)` = `PASS | FAIL | WARN | SKIP` (`SKIP` = not applicable in this phase/context,
  e.g. an auth-needing check in the pre-bootstrap phase, or a dev-box check that cannot run).
- `CheckResult` dataclass: `check_id: str`, `title: str`, `severity: Severity`,
  `status: CheckStatus`, `detail: str`, `fix: str`. **No field ever holds a secret value.**
- `Check` = a callable seam `(ctx: DoctorContext) -> CheckResult` carrying static metadata
  attributes `severity: Severity`, `phase` (`PRE_BOOTSTRAP` / `POST_BOOTSTRAP`), and
  `daemon_native: bool` (default `False`). Every external dependency (subprocess runner,
  `ruleset_is_provisioned`, `fetch_secret`, filesystem) is reached through an injected callable on
  `DoctorContext`, mirroring the existing injected-seam test style (`runner=`, `run=`,
  `fetch_secret=`).
- `DoctorContext` — the injected-seam bundle: the subprocess runner, `ruleset_is_provisioned`,
  `fetch_secret`, filesystem accessors, **and `installation_token: str = ""`** (the minted GitHub
  App installation token, passed **by value**, never read from `os.environ` — mirrors the
  token-by-value invariant threaded through `cli.py:404-414` / `reconcile_startup`'s
  `installation_token` parameter at reconcile.py:145). Phase B / auth-needing checks
  (`RULESET_MAIN`, `RULESET_FEATURE`, `GH_AUTH`, `LABELS_PRESENT`) read the token from this field.
- `daemon_native` semantics: `True` marks a check whose behavior is **already executed by existing
  native daemon-path code** — either a `reconcile.py` `G3` credential gate or the `cli.py`
  force-pr-not-merge tripwire. `run_gate` (daemon) **excludes** every `daemon_native=True` check so
  the native code remains the sole executor (preserving its exact alert text and message
  ordering); `run_report` (standalone) **includes** them (the native code never runs in the
  standalone path). daemon-native set =
  `{GH_AUTH, CRED_ANTHROPIC_UNSET, CRED_OAUTH_VOLUME, GIT_CRED_HELPER, FORCE_PR_TRIPWIRE}`.
- `CATALOG: list[Check]` — the full check list (§6), each tagged with `severity`, `phase`, and
  `daemon_native` so runners can filter.
- `run_report(ctx) -> list[CheckResult]` — runs every applicable check (all phases, `daemon_native`
  included), never aborts early (standalone mode).
- `run_gate(ctx, phase) -> None` — runs the non-`daemon_native` checks for one phase; raises
  `SystemExit(1)` after emitting a CRITICAL alert on the first CRITICAL `FAIL` (daemon mode).

**Exception contract (load-bearing for daemon reliability).** A `Check` callable that raises is
**caught by the runner** — the exception never propagates out of `run_report` **or** `run_gate`.
The result becomes `CheckResult(status=FAIL, severity=check.severity, detail=repr(exc),
fix=check.fix)`, which then flows through the normal FAIL path (reported in standalone mode;
triggers the CRITICAL-fail `SystemExit(1)` in gate mode if `severity` is CRITICAL). This is
non-negotiable for `run_gate`: an uncaught exception there would bypass `SystemExit(1)` entirely
and dump a raw traceback — reintroducing precisely the cryptic-crash failure mode #193 exists to
replace with a clean, actionable message. `run_report` must likewise never abort early on a raising
check (one broken check must not hide the rest of the report).

Rationale for a shared catalog: the classification, the message text, and the "fix" strings live
in exactly one place, so the standalone report and the daemon gate can never drift. This is the
"consolidate, don't duplicate" constraint made structural.

## 4. Two-phase daemon integration (the key placement decision)

The daemon path cannot run every check at one point, because some checks need auth that only
exists **after** `bootstrap_secrets` mints the installation token, while others should fail
**before** the expensive vault round-trip:

- **Phase A — PRE_BOOTSTRAP**, called from `cli.main` immediately after
  `sandbox_config.read_and_validate` + `load_registry` populate `os.environ` and before
  `bootstrap_secrets` (insert after the force-pr-not-merge tripwire at cli.py:351-359 and before
  the `bootstrap_secrets()` call at cli.py:367-370 — i.e. ~cli.py:361-366). No network/auth
  needed. `run_gate(PRE_BOOTSTRAP)` executes the **non-`daemon_native`** Phase-A checks: CLIs on
  PATH, `BH_PROJECT_ROOT`, config.env completeness + required keys, optional-secret-ID shape,
  `BWS_ACCESS_TOKEN` presence, `.symphony/` gitignore. It does **not** re-run `ANTHROPIC_API_KEY`
  unset (`CRED_ANTHROPIC_UNSET`), `GIT_CRED_HELPER`, or `FORCE_PR_TRIPWIRE` — those are
  `daemon_native` and stay owned by their existing native code (G3b, G3d, and the cli.py tripwire
  respectively; §6). Fails fast with clean messages instead of a deep `BwsClientError`.
- **Phase B — POST_BOOTSTRAP**, folded into `reconcile_startup` (reconcile.py) which already runs
  once at boot with the minted `installation_token` (awaited at daemon.py:2759-2764).
  `reconcile_startup` threads its own `installation_token` parameter (reconcile.py:145) into the
  `DoctorContext` it builds for the gate (`installation_token` field, §3) — token by value, never
  via env. `run_gate(POST_BOOTSTRAP)` executes **only the net-new, non-`daemon_native` checks**:
  `RULESET_MAIN`, `RULESET_FEATURE` (via `ruleset_is_provisioned` using the App token),
  `LABELS_PRESENT`, and `GH_REPO_ADMIN` (informational). It does **not** re-execute the native
  credential gates G3a/b/c/**d** — those are `daemon_native` and run in place (see placement
  below). **Standalone-runner coverage vs. daemon-gate coverage are different scopes:** the phrase
  "covers GitHub-token validity (G3a) / OAuth volume (G3c)" describes what the **standalone**
  `run_report` catalog covers in one pass; in the daemon path those checks fire as native
  G3a/G3c, not via `run_gate`.

**Phase B placement (safe reading — resolves the §4↔D4 contradiction).**
`run_gate(POST_BOOTSTRAP)` inserts **AFTER** the native G3a/b/c/d credential block and **before**
G2 — concretely, between the end of G3d (`reconcile.py:280`) and the start of G2
(`reconcile.py:282`). It must **not** run as `reconcile_startup`'s first step. Rationale (the
concrete constraint): `bin/verify-recovery.sh` greps daemon stderr for
`"ANTHROPIC_API_KEY must not be set"` (G3b, verify-recovery.sh:428) and
`"Startup credential check failed"` (G3a, verify-recovery.sh:480). In that harness's environment
the net-new ruleset/label checks will almost certainly FAIL; if `run_gate` ran first it would
`SystemExit(1)` on `RULESET_MAIN` and native G3a/G3b would never emit their alert text, breaking
both greps. Placing the gate after G3d guarantees the credential gates fire first.

This respects the **Gap 1A invariant** (cli.py:404-406: `cli.py` must NOT call
`reconcile_startup`). `cli.main` calls `doctor.run_gate(..., phase=PRE_BOOTSTRAP)` — a different
function — not `reconcile_startup`.

The standalone runner does not have this constraint: `bh-daemon --doctor` runs `run_report(ctx)`
over the **full** catalog in one pass, using ambient `gh` auth (`gh auth status`) for the
auth-needing checks, and reports MATCH/DRIFT/ABSENT/ERROR / can't-check rather than exiting.

## 5. Per-launch ruleset gate: keep both (recommended)

Recommendation: **add** the startup CRITICAL ruleset check (Phase B) **and keep** the existing
per-launch gate (`_should_launch_worker`, daemon.py:206-411) unchanged.

- The startup gate makes a misconfigured repo fail fast at boot (the common case #193 targets)
  instead of parking every issue one-by-one.
- The per-launch gate is the only thing that catches a ruleset being **deleted or drifted
  mid-run** (someone edits branch protection while the daemon is live). A boot-only check has a
  blind spot for the entire daemon lifetime. The per-launch gate is already cheap and already
  degrades gracefully (parks + comments; daemon.py:376-411).

They share `ruleset_is_provisioned`, so this is defense-in-depth, not duplication. This is a
genuine design decision — locked as D5 (§13).

G2/G1 stay in `reconcile.py`: they are post-crash **recovery-state** checks, not readiness
prerequisites, so folding them into the readiness doctor would blur a clean boundary.

## 6. Check catalog with classification + rationale

Legend: **Phase** A = PRE_BOOTSTRAP (cli.py, no auth), B = POST_BOOTSTRAP (reconcile.py, App
token). **Native** = `daemon_native` (§3): `y` means the daemon path runs this via existing native
code (G3 gate or cli.py tripwire) and `run_gate` **skips** it; `run_report` (standalone) runs
every row regardless, auth-needing ones via ambient `gh`.

| ID | Item | Severity | Phase | Native | Rationale / source (Rev 2 anchors) |
|----|------|----------|-------|--------|--------------------|
| `CLI_GH` | `gh` on PATH | CRITICAL | A | n | daemon + vendored tracker shell out to `gh` constantly |
| `CLI_BWS` | `bws` on PATH | CRITICAL | A | n | `bootstrap_secrets` fetches the PEM via the `bws` binary (bws_client.py:146) |
| `CLI_CLAUDE` | `claude` on PATH | CRITICAL | A | n | workers run `claude -p`; without it no work is possible (D6) |
| `CLI_UV` | `uv` on PATH | WARNING | A | n | needed only at setup/install; the venv already exists at daemon runtime (D6) |
| `ENV_PROJECT_ROOT` | `BH_PROJECT_ROOT` set and is a dir | CRITICAL | A | n | daemon chdirs to it (cli.py:327-349) |
| `ENV_HOST_ENV` | `~/.config/baton-harness/host.env` present | WARNING | A | n | one way to set `BH_PROJECT_ROOT`; env override is also valid (run-daemon.sh:80-112) |
| `CFG_CONFIG_ENV` | `.bh/config.env` exists | CRITICAL | A | n | run-daemon.sh:161-165; cli.py:288-299 |
| `CFG_REQUIRED_KEYS` | required config keys valid | CRITICAL | A | n | reuse `sandbox_config` `_REQUIRED_KEYS` (sandbox_config.py:60-66) |
| `CFG_OPTIONAL_SECRET_IDS` | optional `BWS_*_SECRET_ID` shape-valid if set | WARNING | A | n | sandbox_config.py:196-197 |
| `ENV_BWS_ACCESS_TOKEN` | `BWS_ACCESS_TOKEN` present + non-empty (shape only) | CRITICAL | A | n | bootstrap pops it (app_auth.py:402/446 — Rev-2 verified accurate after #200 shipped `app_auth.py`); this is the enforcing version of #192 |
| `GITIGNORE_SYMPHONY` | `.symphony/` gitignored in repo | CRITICAL | A | n | run-daemon.sh:216-228; else `gh pr create` warns + state pollutes tree |
| `FORCE_PR_TRIPWIRE` | force-pr-not-merge hook self-test passes | CRITICAL | A | **y** | existing native cli.py tripwire (cli.py:191-213,351-359) owns the daemon path; standalone reports it via `run_report`. Fold-in deferred — see D10 |
| `GH_AUTH` | gh token valid | CRITICAL | B | **y** | standalone: `gh auth status`; daemon: existing G3a `validate_daemon_token` (reconcile.py:182-208) |
| `GH_REPO_ADMIN` | actor has repo admin (ruleset provisioning) | WARNING | B | n | informational, "where checkable" per issue (D6); net-new run_gate check (D3/D4) |
| `RULESET_MAIN` | `harness-main-no-merge` present + MATCH | CRITICAL | B | n | `ruleset_is_provisioned` (ruleset_status.py:581); net-new run_gate check |
| `RULESET_FEATURE` | `harness-feature-daemon-only` present + MATCH | CRITICAL | B | n | same call evaluates both rulesets together; net-new run_gate check |
| `CRED_ANTHROPIC_UNSET` | `ANTHROPIC_API_KEY` NOT set | CRITICAL | A | **y** | existing G3b (reconcile.py:210-228); env-only, native code keeps text stable |
| `CRED_OAUTH_VOLUME` | `~/.claude/.credentials.json` present + readable | CRITICAL (daemon) / WARN (standalone dev-box) | B | **y** | existing G3c (reconcile.py:230-255) |
| `GIT_CRED_HELPER` | git credential helper configured for github.com push | CRITICAL | A | **y** | **NEW (#219): existing G3d (reconcile.py:257-280).** Local `git config --get-all` probe of key NAMES only — no GitHub auth, no secret material; *can* pass on a dev box, so no dev-box carve-out. Fix: `gh auth setup-git`. See D9. |
| `LABELS_PRESENT` | 5 required labels exist in target repo | CRITICAL | B | n | move from run-daemon.sh:186-214 (D8); net-new run_gate check |
| `VAULT_PEM_DRYRUN` | live `bws` fetch of PEM secret, non-empty (never printed) | opt-in, standalone only | B | n | see §11 + D2; excluded from auto-gate (redundant with bootstrap ~2s later) |

**`daemon_native` / phase note (Rev 3).** For the five `daemon_native=y` rows (`FORCE_PR_TRIPWIRE`,
`GH_AUTH`, `CRED_ANTHROPIC_UNSET`, `CRED_OAUTH_VOLUME`, `GIT_CRED_HELPER`) the `Phase` letter is
**documentation-only** — it tells `run_report` the standalone auth-class of the check, but
`run_gate` skips these rows in **both** phases (their native code — the G3 gate or the cli.py
tripwire — is the sole executor in the daemon path). This is why the former `A/B` dual marks on
`CRED_ANTHROPIC_UNSET` and `GIT_CRED_HELPER` collapse to a single `A`: the ambiguity they encoded
("Phase A gate, or native G3?") is now carried by `daemon_native`, not by a dual phase letter.
`CRED_OAUTH_VOLUME` keeps `B` (its standalone auth-class); the letter is likewise moot for the
daemon path.

**G3d classification note (Rev 3, corrects Rev 2).** `GIT_CRED_HELPER` is `phase A`,
`daemon_native=y`: an auth-free local probe that the standalone `run_report` executes, but which in
the daemon path stays owned by native G3d (per D4/D9). Unlike G3a/b/c, **G3d has no
`verify-recovery.sh` scenario** — that harness exercises only G3b/G3a/G2/G1/SIGTERM
(verify-recovery.sh:4), so the D4 "keep exact text so the greps pass" rationale does not bind G3d
via `verify-recovery.sh`. **However, G3d's text IS a test dependency**: its alert text is pinned by
`tests/chain/test_reconcile_git_credential_helper.py:305`, which asserts `"gh auth setup-git" in
message`. Preserve G3d's text to keep that reconcile test green (the Rev-2 claim that it was "not a
test dependency" was wrong).

## 7. Mode 1 — standalone doctor

- Invocation: `bh-daemon --doctor` (recommended; minimal surface — `bin/run-daemon.sh` already
  passes `"$@"` through at run-daemon.sh:245). Alternative: a separate `bh-doctor` console script
  in `pyproject.toml` `[project.scripts]` (currently only `bh-daemon`, pyproject.toml:46-50). Locked
  as D1 (§13).
- Behavior: parse args, load config.env + registry (reusing cli.py's existing load path), run
  `doctor.run_report(ctx)` over the full catalog, print the report, **do not** start the daemon
  (return before `bootstrap_secrets` / `run_daemon`).
- Dev-box carve-out (hard constraint): the standalone mode runs **all** checks and prints the
  full report; it never aborts early on a CRITICAL fail. Auth/admin/ruleset/OAuth-volume checks
  that legitimately cannot pass on a dev box report `WARN`/`SKIP` with an explanation, not a hard
  block.
- Exit code: **0 by default** (pure diagnostic), with `--strict` returning `1` when any CRITICAL
  check FAILs (for CI use). Locked as D7 (§13).

## 8. Mode 2 — daemon auto-gate

- Phase A: `cli.main` calls `doctor.run_gate(ctx, phase=PRE_BOOTSTRAP)` after config/registry
  load, before `bootstrap_secrets` (cli.py ~347). A CRITICAL FAIL emits a message to stderr and
  `sys.exit(1)` — the daemon does no work.
- Phase B: `reconcile_startup` calls `doctor.run_gate(ctx, phase=POST_BOOTSTRAP)` **after the
  native G3a/b/c/d credential block and before G2** — inserted between `reconcile.py:280` (end of
  G3d) and `reconcile.py:282` (start of G2), using the minted `installation_token` (threaded into
  the `DoctorContext`, §4). It runs only the net-new non-`daemon_native` checks
  (`RULESET_MAIN`/`RULESET_FEATURE`/`LABELS_PRESENT`/`GH_REPO_ADMIN`); the native G3a/b/c/d gates
  fire first and unchanged. A CRITICAL FAIL emits `alert(..., severity="critical")` then
  `sys.exit(1)`, matching the existing G3a/b/c/**d** fatal pattern (reconcile.py:182-280). Placing
  the gate **after** G3d — not first — is what keeps `bin/verify-recovery.sh`'s credential-text
  greps (G3b at :428, G3a at :480) passing: a run_gate that exited first on a failing ruleset
  check would suppress those alerts (§4 placement rationale).
- Non-critical (WARNING) gaps emit a warn-level alert and proceed.

## 9. Message format

`CheckResult` renders as:

```
[PASS] gh CLI on PATH
[FAIL] sandbox config present            (CRITICAL)
       item:   .bh/config.env
       looked: ${BH_PROJECT_ROOT}/.bh/config.env
       fix:    run bin/init-sandbox.sh, or create it by hand (see docs/smoke-test-daemon.md)
[WARN] uv CLI on PATH                     (WARNING)
       item:   uv
       fix:    curl -LsSf https://astral.sh/uv/install.sh | sh   (setup-time only)
```

Secret discipline: `ENV_BWS_ACCESS_TOKEN` and `VAULT_PEM_DRYRUN` report presence/shape only
(e.g. "set, 44 chars" or "present, non-empty") — never the value (CLAUDE.md § Credentials and
Secrets; mirrors the structural-only checks already in reconcile.py:214-216,231-234 (G3b/G3c),
the new G3d name-only probe at reconcile.py:257-280, and verify-recovery.sh:221,268-286).

## 10. Vocabulary unification

Two naming vocabularies exist today: `G#` (reconcile.py startup gates) and
`_should_launch_worker`/#144 (per-launch, daemon.py). The doctor introduces stable catalog IDs
(§6). Recommendation: the catalog IDs become the canonical vocabulary; the existing `G#` gates
that migrate into the catalog keep their **exact message text** (so the verify-recovery.sh
alert-text greps still match — G3b at verify-recovery.sh:428, G3a at verify-recovery.sh:480, plus
the later G2/G1 scenario assertions) but gain a catalog ID alias. Do **not** renumber the
surviving `G#` names — the pinned tests (test_reconcile.py) and verify-recovery.sh depend on them.
**Rev-3 correction (was Rev-2 note):** G3d (#219) is intentionally absent from verify-recovery.sh
(that harness covers only G3b/G3a/G2/G1/SIGTERM — verify-recovery.sh:4), so no `verify-recovery.sh`
grep depends on G3d's text. But G3d's text **is** a test dependency via a different suite:
`tests/chain/test_reconcile_git_credential_helper.py:305` asserts `"gh auth setup-git" in message`.
So keep G3d's exact text stable for that reconcile test, not for verify-recovery.sh. Locked as D4
(§13, extended to G3d by D9).

## 11. Live bws PEM-fetch dry-run

Recommendation: **opt-in `--check-vault` on the standalone doctor only; excluded from the
auto-gate.** A live fetch is the only check that (a) makes a network round-trip and (b) touches
secret material. In the daemon path it is redundant — `bootstrap_secrets` performs the real fetch
seconds later (cli.py:367-370) and any failure surfaces there. For the standalone operator it has
real value (confirms the secret ID + `BWS_ACCESS_TOKEN` actually resolve a non-empty PEM). When
run, it checks non-empty only and never prints the material. Locked as D2 (§13). (Rev-2 anchor: the
redundant real fetch happens at `bootstrap_secrets` → cli.py:367-370.)

## 12. Migration of shell-side checks + #192/#194

- **run-daemon.sh** (per D8): **no behavior change.** The `LABELS_PRESENT` and
  `GITIGNORE_SYMPHONY` shell blocks (run-daemon.sh:186-228) and the pre-Python
  `BH_PROJECT_ROOT`/config.env checks (run-daemon.sh:95-172) stay as a fast fail-fast gate before
  `bh-daemon` starts. The Python catalog covers the same two checks so the daemon + standalone
  paths have an in-process source of truth; the shell copies are the intentional, cheap early
  gate. This softens strict "consolidate" only for these two shell checks, which is the locked
  choice.
- **verify-recovery.sh**: it is a recovery-gate **test harness** (Linux-only), not a preflight.
  Two integrity halves must hold under the Rev-3 gate placement (§4/§8):
  - **Credential-text half (verified):** the G3b/G3a scenarios keep passing because the new
    `run_gate(POST_BOOTSTRAP)` sits *after* the native G3a/b/c/d block, so those gates emit their
    exact text first (D4/§10) and the stderr greps at verify-recovery.sh:428/:480 still match.
  - **Recovery-scenario half (NEW precondition, Rev 3):** the G2/G1/SIGTERM scenarios must *reach
    the poll loop*, which now means passing the net-new fatal checks
    (`RULESET_MAIN`/`RULESET_FEATURE`/`LABELS_PRESENT`) inserted between G3d (reconcile.py:280) and
    G2 (:282). The harness already runs against a **real, fully-provisioned** target repo — it
    requires `BH_REPO_OWNER`/`BH_REPO_NAME`, a valid `GH_TOKEN`, a queryable repo, and OAuth creds
    (verify-recovery.sh:149,236,270-286) — and that canonical repo is exactly the one the daemon
    operates on, so its rulesets + 5 labels are provisioned by definition. The G2/G1/SIGTERM
    scenarios therefore still reach the poll loop. **This is a documented behavior change to the
    harness's preconditions** (not "unchanged"): running verify-recovery.sh against a repo *without*
    both rulesets and the 5 labels provisioned would now hard-exit at the new gate before G2/G1.
    Phase 4 must confirm the G2/G1/SIGTERM scenarios still pass against the canonical repo; if the
    CI/test repo used for verify-recovery.sh is not ruleset/label-provisioned, either provision it
    or move the gate to *after* G1 (an alternative placement that keeps recovery scenarios reachable
    while still failing fast before the poll loop).
- **#192 / PR #194 — RESOLVED (Rev 2, 2026-07-19).** PR #194 **merged 2026-07-03** and issue #192
  is **closed** (WebFetch-verified: `github.com/glitchwerks/baton-harness/pull/194`). The
  setup-time non-fatal `BWS_ACCESS_TOKEN` notice is now live on `main` at
  **bin/setup-env.sh:508-514** (exit-0 warning, never persists the token). Decision (unchanged):
  **the setup-time notice and the doctor's runtime `ENV_BWS_ACCESS_TOKEN` gate are complementary**
  — a friendly nudge at dev-setup time (already shipped), a hard gate at daemon boot (this plan).
  They do not conflict. **Concrete consequence for Phase 5:** do **not** modify `bin/setup-env.sh`
  — the notice already exists; this concern is now docs-only (see §15).

## 13. Decisions (locked)

Accepted as provisional defaults by the router on 2026-07-01 (user away); **re-ratify with the
user before implementation.** Each row is the locked choice + one-line rationale. **Rev-2 verdict
(2026-07-19): no decision's substance flipped — every D1–D8 still holds on the merits; only their
embedded line anchors drifted, and those are corrected below. D9 (G3d) is new and needs an
explicit ratification.**

| # | Decision (locked) | Rationale (Rev-2 anchors) | Rev-2 substance verdict |
|---|-------------------|-----------|-----------|
| D1 | Invocation = `bh-daemon --doctor` flag; no separate entry point | Minimal surface; `run-daemon.sh:245` already forwards `"$@"`; avoids a new `[project.scripts]` entry (pyproject.toml:46-50 defines only `bh-daemon`) | Holds — anchor moved 243→245, forward mechanism unchanged |
| D2 | Live PEM-fetch = opt-in `--check-vault`, standalone only; excluded from the auto-gate | Redundant in the daemon path (`bootstrap_secrets` does the real fetch seconds later, cli.py:367-370; the actual PEM fetch is now inside `build_installation_token_provider`); only touches secret material when the operator opts in | Holds — redundancy argument unchanged; anchor moved |
| D3 | Daemon gate is two-phase: Phase A pre-bootstrap in `cli.main`; Phase B post-bootstrap in `reconcile_startup` | No-auth checks fail fast before the vault round-trip; auth-needing checks run once the App token exists; respects the Gap 1A invariant (cli.py:404-406) | Holds — invariant intact, anchor moved 374-376→404-406 |
| D4 | G3a/b/c (**and now G3d**) reuse the existing validators; `reconcile.py` stays the caller with identical message text | Lowest risk to the pinned `test_reconcile.py` and to `verify-recovery.sh` greps (G3b at :428, G3a at :480, later G2/G1 scenarios). **G3d caveat:** no `verify-recovery.sh` grep depends on G3d (harness omits it, verify-recovery.sh:4), but G3d's text IS pinned by `test_reconcile_git_credential_helper.py:305` (`"gh auth setup-git" in message`) — preserve its text for that reconcile test (Rev-3 correction: it is a test dependency) | Holds + extended to G3d (see D9) |
| D5 | Keep both ruleset gates: new startup CRITICAL (Phase B) + existing per-launch (`_should_launch_worker`, daemon.py:206-411) | Startup gate fails fast on a misconfigured repo; per-launch gate is the only guard against a ruleset deleted/drifted mid-run | Holds — anchor moved 104-187→206-411 |
| D6 | Classification: `claude` CRITICAL at boot; `uv` WARNING/setup-only; repo-admin WARNING/informational | Workers run `claude -p`; the venv already exists at runtime so `uv` is setup-time; repo-admin is "where checkable" per the issue | Holds — no code dependency |
| D7 | Standalone exit = 0 report-only by default; `--strict` returns 1 when any CRITICAL FAILs | Diagnostic by default (dev-box carve-out); `--strict` makes it CI-usable | Holds — no code dependency |
| D8 | Keep `LABELS_PRESENT` + `.symphony`-gitignore checks in `run-daemon.sh` as a fast pre-Python gate; the Python catalog covers them too | No `run-daemon.sh` behavior change; the shell gate stays a quick fail-fast, the catalog is the in-process source of truth for the daemon + standalone paths | Holds — shell blocks now at run-daemon.sh:186-228 |
| **D9 (NEW)** | **Add G3d (git credential-helper presence, #219) to the doctor catalog as `GIT_CRED_HELPER`: CRITICAL, phase A, `daemon_native=True`, `reconcile.py` stays the caller with its exact text (mirrors D4).** | G3d is a fatal readiness prerequisite exactly like G3a/b/c: without a git credential helper, `git push` fails and the daemon cannot open PRs (its core job). It is an auth-free local `git config` probe (`reconcile.py:257-280`), so it can pass on a dev box (no dev-box carve-out). Alternative considered: leave it out because it's a "git-plumbing" check rather than a harness-readiness check — **rejected**, because a boot that can't push is exactly the "silently misconfigured repo" failure #193 exists to surface. | **NEEDS EXPLICIT RATIFICATION** — did not exist at Rev 1 |
| **D10 (Rev 3)** | **`FORCE_PR_TRIPWIRE`: phase A, CRITICAL, `daemon_native=True`. The existing native cli.py force-pr-not-merge tripwire (cli.py:191-213,351-359) remains the sole executor in the daemon path; `run_report` (standalone) reports it; fold-in into `run_gate` is deferred.** | Resolves the Rev-2 dangling "fold-in optional, Q8". The tripwire already runs natively at cli.py:351-359, immediately before the ~cli.py:361-366 Phase-A gate insertion point; running it again inside `run_gate(PRE_BOOTSTRAP)` would double-execute it. Marking it `daemon_native` (generalized to "already executed by native daemon-path code" — G3 gate *or* cli.py tripwire) keeps `run_gate` from re-running it while the standalone doctor still reports it. Folding the tripwire fully into the catalog is a future cleanup, not required for #193. | Rev-3 decision — resolves a Rev-2 minor-cleanup item |

## 14. Test strategy

Mirror the established injected-seam style (unittest.mock `patch`/`AsyncMock` over injected
callables; no pytest-asyncio; `once=True` + `asyncio.run` for daemon paths — per
test_daemon_preflight.py and tests/chain/conftest.py:13-30 which autouse-patches
`reconcile_startup` to a no-op).

- `tests/chain/test_doctor.py` — unit-test each `Check` with a hand-built `DoctorContext` whose
  runner/`fetch_secret`/filesystem seams are mocked. Cover PASS / FAIL / WARN / SKIP per check;
  assert `detail` and `fix` strings are present and that no secret value ever appears in any
  `CheckResult` field (mirrors the secret-not-logged assertions in test_alert_post.py; Rev-2 note:
  the old precise `test_daemon_preflight.py:59` anchor was stale — that line now describes a
  ruleset regression guard — so this reference points at the test_alert_post.py pattern directly).
  Cover `run_report` (never aborts) and `run_gate` (raises `SystemExit` on first CRITICAL fail).
- **Exception contract (§3, BLOCKING #1).** Inject a deliberately-raising `Check` through the seam
  and assert it is caught and surfaced as `CheckResult(status=FAIL, severity=check.severity,
  detail=repr(exc), fix=check.fix)` — assert that exact shape. Assert the exception never
  propagates out of either runner: `run_report` still returns results for the remaining checks, and
  `run_gate` on a raising CRITICAL check exits via `SystemExit(1)` (never a bare traceback).
- **`daemon_native` filter (§3).** With a catalog stub containing one `daemon_native=True` and one
  `daemon_native=False` check, assert `run_gate` **skips** the `daemon_native=True` one (a failing
  native-owned check does not fire through `run_gate`) while `run_report` **includes** it.
- `tests/chain/test_cli_doctor_gate.py` — Phase A: patch the doctor gate and assert `cli.main`
  calls it before `bootstrap_secrets` and exits 1 on CRITICAL fail without reaching
  `run_daemon`. Assert `--doctor` runs `run_report` and returns without starting the daemon.
- `tests/chain/test_reconcile.py` (extend) — Phase B: assert `reconcile_startup` invokes the
  post-bootstrap gate **after the native G3a/b/c/d block and before G2** (not first), and that a
  CRITICAL ruleset/label fail exits 1 with an alert. Note the reconcile suites run
  `reconcile.reconcile_startup(...)` directly and therefore do **not** hit the `conftest.py`
  autouse fixture (it patches `baton_harness.chain.daemon.reconcile_startup` — the daemon.py import
  path — not `reconcile.reconcile_startup`); the new fatal Phase-B checks are neutralized per-file
  instead (see Phase 4).
- Ruleset checks reuse the `RulesetStatus` fixtures already exercised in test_ruleset_status.py.

## 15. Phased task breakdown (TDD-first)

Each phase is one focused PR off `main` (worktree per CLAUDE.md § Worktrees). Write the failing
test first, then the implementation. Every phase reflects the locked design in § Decisions
(locked).

- **Phase 0 — re-ratify + confirm.** Get the user's explicit re-ratification of D1-D8 (the router
  accepted them as provisional defaults, not the user), **plus explicit ratification of D9 (G3d in
  the catalog) and D10 (`FORCE_PR_TRIPWIRE` daemon-native disposition)**, both of which are newer
  than the original 8. No code until this clears.
- **Phase 1 — `doctor.py` catalog + no-auth checks (Phase A set).** `Severity`, `CheckStatus`,
  `CheckResult`, `DoctorContext`, `Check`, `CATALOG` (with `severity` + `phase` tags per D3/D6),
  `run_report` (never aborts), `run_gate` (raises `SystemExit(1)` on first CRITICAL fail).
  Implement the PRE_BOOTSTRAP checks: `CLI_GH`/`CLI_BWS`/`CLI_CLAUDE` CRITICAL + `CLI_UV` WARNING
  (D6), `ENV_PROJECT_ROOT`, `CFG_CONFIG_ENV`, `CFG_REQUIRED_KEYS` (reuse `sandbox_config`),
  `CFG_OPTIONAL_SECRET_IDS` WARNING, `ENV_BWS_ACCESS_TOKEN` (presence/shape only),
  `GITIGNORE_SYMPHONY`, `CRED_ANTHROPIC_UNSET`, **`GIT_CRED_HELPER` (G3d, auth-free `git config`
  probe — per D9)**, `FORCE_PR_TRIPWIRE` (wraps the existing cli.py self-test logic at
  cli.py:191-213; `daemon_native=True` per D10 — authored here for `run_report`, native tripwire
  owns the daemon path), `ENV_HOST_ENV` WARNING. All these `Check` callables (including the
  `daemon_native=True` ones) are authored here so the standalone `run_report` can execute them;
  wiring comes later. Tests: test_doctor.py. No wiring yet.
  **Phase-placement note (Rev 3):** `GIT_CRED_HELPER` and `CRED_ANTHROPIC_UNSET` are authored in the
  `phase A` group (auth-free local probes), but both are `daemon_native=True` (D9/D10 semantics,
  §3): `run_report` (standalone) executes them, while `run_gate` **skips** them so their native
  `reconcile.py` gates (G3d, G3b) stay the sole executor in the daemon path — the daemon does not
  re-run them at Phase A. This is why they are NOT in the Phase-3 `run_gate(PRE_BOOTSTRAP)`
  execution set (Phase 3). `reconcile.py` keeps its native G3b/G3d unchanged (D4/D9).
- **Phase 2 — standalone `bh-daemon --doctor` (D1).** Add the `--doctor` flag to `cli.py`
  argparse (no separate entry point); run `run_report`, print the report, return before daemon
  start. Add `--strict` (D7): exit 0 report-only by default, exit 1 on any CRITICAL FAIL under
  `--strict`. Tests: test_cli_doctor_gate.py (report path + both exit modes).
- **Phase 3 — Phase-A daemon gate (D3).** Wire `run_gate(ctx, PRE_BOOTSTRAP)` into `cli.main`
  after config/registry load and after the force-pr-not-merge tripwire (cli.py:351-359), before
  `bootstrap_secrets` (cli.py:367-370) — i.e. insert at ~cli.py:361-366. Tests:
  test_cli_doctor_gate.py (asserts the gate runs before bootstrap and exits 1 on CRITICAL fail
  without reaching `run_daemon`).
- **Phase 4 — auth-needing checks + Phase-B gate (D3/D4/D5/D9).** Add the net-new,
  non-`daemon_native` Phase-B checks — `RULESET_MAIN`, `RULESET_FEATURE` (reuse
  `ruleset_is_provisioned`), `LABELS_PRESENT`, `GH_REPO_ADMIN` WARNING — plus author `GH_AUTH` and
  `CRED_OAUTH_VOLUME` as `Check` callables for `run_report` (both are `daemon_native=True`, so the
  daemon path keeps native G3a/G3c; `run_gate` skips them). `GIT_CRED_HELPER`/G3d is authored in
  Phase 1 and is likewise `daemon_native` (native G3d owns the daemon path), so this phase touches
  nothing there. Per D4, the native credential gates reuse the existing validators and keep
  identical message text; `reconcile.py` remains the caller for G3a/b/c/**d**. Wire
  `run_gate(ctx, POST_BOOTSTRAP)` into `reconcile_startup` **after the native G3a/b/c/d block and
  before G2** — inserted between `reconcile.py:280` (end of G3d) and `reconcile.py:282` (start of
  G2), **not** as reconcile's first step (§4/§8 placement rationale: a first-position gate that
  `SystemExit`s on a failing ruleset check would suppress the native G3a/G3b alert text that
  `verify-recovery.sh:428`/`:480` grep for). Thread `reconcile_startup`'s `installation_token`
  parameter (reconcile.py:145) into the `DoctorContext` built for the gate. Per D5, leave the
  per-launch `_should_launch_worker` gate untouched.
  **Reconcile-test fixtures (required — established codebase pattern).** Wiring
  `run_gate(POST_BOOTSTRAP)` in makes the net-new fatal checks (`RULESET_MAIN`, `RULESET_FEATURE`,
  `LABELS_PRESENT`) fire inside every reconcile test that calls
  `reconcile.reconcile_startup(...)` directly. Following the precedent of `_patch_oauth_cred_path`
  (neutralizes G3c) and `_patch_git_credential_helper` (neutralizes G3d), add an autouse
  `_patch_doctor_run_gate` (or equivalent) neutralizing fixture to **all three** reconcile test
  files — `tests/chain/test_reconcile.py`, `tests/chain/test_reconcile_oauth_cred.py`, and
  `tests/chain/test_reconcile_git_credential_helper.py` — otherwise those suites break. (The
  `conftest.py` autouse no-op does **not** cover this: it patches
  `baton_harness.chain.daemon.reconcile_startup`, the daemon.py import path, which these
  direct-call tests bypass.) Tests: test_doctor.py + the three reconcile files.
  **G3d text note:** G3d has no `verify-recovery.sh` scenario, but its exact alert text IS pinned by
  `tests/chain/test_reconcile_git_credential_helper.py:305` (asserts `"gh auth setup-git" in
  message`); `test_reconcile.py` neutralizes G3d via the autouse `_patch_git_credential_helper`
  fixture (test_reconcile.py:126-147) — keep that text stable.
- **Phase 5 — docs + overlap (D8).** **No `run-daemon.sh` behavior change** — the shell
  `LABELS_PRESENT`/`GITIGNORE_SYMPHONY` checks stay as the fast pre-Python gate. Update
  `README.md` + `docs/smoke-test-daemon.md` to document `bh-daemon --doctor`, `--strict`,
  `--check-vault`, and the two-phase boot gate. **`bin/setup-env.sh`: do NOT modify it** —
  Rev-2-confirmed (2026-07-19) that PR #194 merged 2026-07-03 and issue #192 is closed, so the
  setup-time non-fatal `BWS_ACCESS_TOKEN` notice already exists on `main` at
  bin/setup-env.sh:508-514. The doctor's runtime `ENV_BWS_ACCESS_TOKEN` gate is the intentional
  complement (see §12); this concern is docs-only. When documenting, cross-reference the two
  (setup-time nudge vs. boot-time hard gate) so operators understand the two-lifecycle-moment
  design.
- **Phase 6 — opt-in `--check-vault` (D2).** Live `bws` PEM dry-run, standalone-only, non-empty
  check that never prints the material. Tests: test_doctor.py (mocked `fetch_secret` seam;
  assert the value never lands in any `CheckResult` field).

## 16. Out of scope

- Auto-remediation (the doctor reports fixes; it does not run them).
- Any change to G2/G1 recovery-state checks (they stay in `reconcile.py`).
- Windows support for the auth-needing checks beyond what `gh`/`bws` already provide;
  `verify-recovery.sh` remains Linux-only (verify-recovery.sh:7-10).
- Renumbering or renaming the existing `G#` gates.
