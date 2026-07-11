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
  - README.md
  - docs/smoke-test-daemon.md
skills_relevant:
  - python
  - github-actions
---

# Unified preflight "doctor" / smoke-test — plan for #193

> **PENDING USER RATIFICATION BEFORE IMPLEMENTATION.** All 8 design decisions were accepted as
> **provisional defaults** by the router on 2026-07-01 while the user was away (see § Decisions
> (locked)). They must be **re-ratified by the user** before any code is written. This plan is
> otherwise final.

> Citation note: file:line anchors below were read and verified on 2026-07-01 against the
> working tree at `I:/ai/claude/baton-harness` (branch `main`, HEAD `c9fc4f1`). GitHub
> issue/PR state was taken from the dispatch brief and agent memory during planning (this
> pass had no `gh`/GitHub-MCP access), so live-state claims were marked `unverified:` — with
> two exceptions the router confirmed this session: **PR #194 is OPEN (`Fixes #192`) and issue
> #192 exists (router-confirmed 2026-07-01).** #193's own state remains `unverified:` here.
> Re-check any remaining `unverified:` state before acting on it.

## 1. Problem & goal

The harness has **five separate preflight surfaces** that each check a slice of "is this host +
repo initialized to run the daemon", with no single source of truth and no operator-facing
"is everything ready?" command. A misconfigured repo can boot the daemon and then silently
**park every issue** at the per-launch ruleset gate (`daemon.py:149-153`), or fail deep inside
`bootstrap_secrets` with a cryptic `BwsClientError` instead of a clear "BWS_ACCESS_TOKEN is not
set" message.

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

| # | Where | Check | Anchor |
|---|-------|-------|--------|
| a | `bin/run-daemon.sh` | host.env sourced; `BH_PROJECT_ROOT` set | run-daemon.sh:83-110 |
| b | `bin/run-daemon.sh` | `.bh/config.env` exists | run-daemon.sh:159-164 |
| c | `bin/run-daemon.sh` | `BH_REPO_OWNER`/`BH_REPO_NAME` present in config | run-daemon.sh:165-172 |
| d | `bin/run-daemon.sh` | 5 required labels exist in target repo | run-daemon.sh:174-211 |
| e | `bin/run-daemon.sh` | `.symphony/` is gitignored in `BH_PROJECT_ROOT` | run-daemon.sh:213-226 |
| f | `cli.py main` | workflow config loads | cli.py:261-269 |
| g | `cli.py main` | `.bh/config.env` keys valid + `gh api` repo exists | cli.py:273-286, sandbox_config.py:48-54,230-240 |
| h | `cli.py main` | registry loads; `BH_PROJECT_ROOT` is a dir | cli.py:289-336 |
| i | `cli.py main` | force-pr-not-merge hook self-test | cli.py:179-201,338-346 |
| j | `cli.py main` | `bootstrap_secrets` (BWS PEM fetch); `validate_daemon_token` | cli.py:354-372 |
| k | `reconcile.py` | G3a GitHub token valid (fatal) | reconcile.py:126-152 |
| l | `reconcile.py` | G3b `ANTHROPIC_API_KEY` unset (fatal) | reconcile.py:154-172 |
| m | `reconcile.py` | G3c OAuth cred volume present (fatal) | reconcile.py:174-199 |
| n | `reconcile.py` | G2 stale `daemon.alive` marker (non-fatal critical) | reconcile.py:201-222 |
| o | `reconcile.py` | G1 orphan `claude -p` procs (non-fatal warn) | reconcile.py:224-246 |
| p | `daemon.py` | **per-launch** ruleset MATCH gate | daemon.py:104-187 |

Reusable helpers the doctor should call rather than reimplement:
- `ruleset_is_provisioned(owner, repo, *, app_id, runner)` → 4-state `RulesetStatus`
  (MATCH/DRIFT/ABSENT/ERROR) — ruleset_status.py:79-93,295-344.
- `sandbox_config.read_and_validate(path, *, run)` — parses + validates config.env keys and
  exports the `BWS_*` twins (sandbox_config.py:149-271). `_REQUIRED_KEYS` at :48-54.
- `_auth.validate_daemon_token` / `validate_github_token` — token validation
  (imported in reconcile.py:33-37).
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
- `Check` = a callable seam `(ctx: DoctorContext) -> CheckResult`. Every external dependency
  (subprocess runner, `ruleset_is_provisioned`, `fetch_secret`, filesystem) is reached through an
  injected callable on `DoctorContext`, mirroring the existing injected-seam test style
  (`runner=`, `run=`, `fetch_secret=`).
- `CATALOG: list[Check]` — the full check list (§6), each tagged with `severity` and a
  `phase` attribute (`PRE_BOOTSTRAP` / `POST_BOOTSTRAP`) so runners can filter.
- `run_report(ctx) -> list[CheckResult]` — runs every applicable check, never aborts early
  (standalone mode).
- `run_gate(ctx, phase) -> None` — runs the checks for one phase; raises `SystemExit(1)` after
  emitting a CRITICAL alert on the first CRITICAL `FAIL` (daemon mode).

Rationale for a shared catalog: the classification, the message text, and the "fix" strings live
in exactly one place, so the standalone report and the daemon gate can never drift. This is the
"consolidate, don't duplicate" constraint made structural.

## 4. Two-phase daemon integration (the key placement decision)

The daemon path cannot run every check at one point, because some checks need auth that only
exists **after** `bootstrap_secrets` mints the installation token, while others should fail
**before** the expensive vault round-trip:

- **Phase A — PRE_BOOTSTRAP**, called from `cli.main` immediately after
  `sandbox_config.read_and_validate` + `load_registry` populate `os.environ` and before
  `bootstrap_secrets` (insert at cli.py ~347, before the `bootstrap_secrets()` call at
  cli.py:354-361). No network/auth needed. Covers: CLIs on PATH, `BH_PROJECT_ROOT`,
  config.env completeness, `BWS_ACCESS_TOKEN` presence, `.symphony/` gitignore,
  `ANTHROPIC_API_KEY` unset. Fails fast with clean messages instead of a deep
  `BwsClientError`.
- **Phase B — POST_BOOTSTRAP**, folded into `reconcile_startup` (reconcile.py) which already runs
  once at boot with the minted `installation_token` (awaited at daemon.py:2087-2092). Covers the
  auth-needing checks: rulesets (via `ruleset_is_provisioned` using the App token), required
  labels, GitHub-token validity (existing G3a), OAuth volume (existing G3c), and repo-admin
  (informational). Phase B runs BEFORE the existing G2/G1 recovery checks.

This respects the **Gap 1A invariant** (cli.py:374-376: `cli.py` must NOT call
`reconcile_startup`). `cli.main` calls `doctor.run_gate(..., phase=PRE_BOOTSTRAP)` — a different
function — not `reconcile_startup`.

The standalone runner does not have this constraint: `bh-daemon --doctor` runs `run_report(ctx)`
over the **full** catalog in one pass, using ambient `gh` auth (`gh auth status`) for the
auth-needing checks, and reports MATCH/DRIFT/ABSENT/ERROR / can't-check rather than exiting.

## 5. Per-launch ruleset gate: keep both (recommended)

Recommendation: **add** the startup CRITICAL ruleset check (Phase B) **and keep** the existing
per-launch gate (`_should_launch_worker`, daemon.py:104-187) unchanged.

- The startup gate makes a misconfigured repo fail fast at boot (the common case #193 targets)
  instead of parking every issue one-by-one.
- The per-launch gate is the only thing that catches a ruleset being **deleted or drifted
  mid-run** (someone edits branch protection while the daemon is live). A boot-only check has a
  blind spot for the entire daemon lifetime. The per-launch gate is already cheap and already
  degrades gracefully (parks + comments; daemon.py:308-330).

They share `ruleset_is_provisioned`, so this is defense-in-depth, not duplication. This is a
genuine design decision — see Q5.

G2/G1 stay in `reconcile.py`: they are post-crash **recovery-state** checks, not readiness
prerequisites, so folding them into the readiness doctor would blur a clean boundary.

## 6. Check catalog with classification + rationale

Legend: **Phase** A = PRE_BOOTSTRAP (cli.py, no auth), B = POST_BOOTSTRAP (reconcile.py, App
token). Standalone runs all of them (auth-needing ones via ambient `gh`).

| ID | Item | Severity | Phase | Rationale / source |
|----|------|----------|-------|--------------------|
| `CLI_GH` | `gh` on PATH | CRITICAL | A | daemon + vendored tracker shell out to `gh` constantly |
| `CLI_BWS` | `bws` on PATH | CRITICAL | A | `bootstrap_secrets` fetches the PEM via the `bws` binary (bws_client.py:146) |
| `CLI_CLAUDE` | `claude` on PATH | CRITICAL | A | workers run `claude -p`; without it no work is possible (Q6) |
| `CLI_UV` | `uv` on PATH | WARNING | A | needed only at setup/install; the venv already exists at daemon runtime (Q6) |
| `ENV_PROJECT_ROOT` | `BH_PROJECT_ROOT` set and is a dir | CRITICAL | A | daemon chdirs to it (cli.py:311-336) |
| `ENV_HOST_ENV` | `~/.config/baton-harness/host.env` present | WARNING | A | one way to set `BH_PROJECT_ROOT`; env override is also valid (run-daemon.sh:83-110) |
| `CFG_CONFIG_ENV` | `.bh/config.env` exists | CRITICAL | A | run-daemon.sh:159-164; cli.py:275-286 |
| `CFG_REQUIRED_KEYS` | required config keys valid | CRITICAL | A | reuse `sandbox_config` `_REQUIRED_KEYS` (sandbox_config.py:48-54) |
| `CFG_OPTIONAL_SECRET_IDS` | optional `BWS_*_SECRET_ID` shape-valid if set | WARNING | A | sandbox_config.py:219-226 |
| `ENV_BWS_ACCESS_TOKEN` | `BWS_ACCESS_TOKEN` present + non-empty (shape only) | CRITICAL | A | bootstrap pops it (app_auth.py:402/446); this is the enforcing version of #192 |
| `GITIGNORE_SYMPHONY` | `.symphony/` gitignored in repo | CRITICAL | A | run-daemon.sh:213-226; else `gh pr create` warns + state pollutes tree |
| `FORCE_PR_TRIPWIRE` | force-pr-not-merge hook self-test passes | CRITICAL | A | existing fatal check cli.py:179-201,338-346 (fold-in optional, Q8) |
| `GH_AUTH` | gh token valid | CRITICAL | B | standalone: `gh auth status`; daemon: existing G3a `validate_daemon_token` (reconcile.py:126-152) |
| `GH_REPO_ADMIN` | actor has repo admin (ruleset provisioning) | WARNING | B | informational, "where checkable" per issue (Q6) |
| `RULESET_MAIN` | `harness-main-no-merge` present + MATCH | CRITICAL | B | `ruleset_is_provisioned` (ruleset_status.py:295) |
| `RULESET_FEATURE` | `harness-feature-daemon-only` present + MATCH | CRITICAL | B | same call evaluates both rulesets together |
| `CRED_ANTHROPIC_UNSET` | `ANTHROPIC_API_KEY` NOT set | CRITICAL | A/B | existing G3b (reconcile.py:154-172); env-only, but keep text stable |
| `CRED_OAUTH_VOLUME` | `~/.claude/.credentials.json` present + readable | CRITICAL (daemon) / WARN (standalone dev-box) | B | existing G3c (reconcile.py:174-199) |
| `LABELS_PRESENT` | 5 required labels exist in target repo | CRITICAL | B | move from run-daemon.sh:174-211 (Q8) |
| `VAULT_PEM_DRYRUN` | live `bws` fetch of PEM secret, non-empty (never printed) | opt-in, standalone only | B | see §11 + Q2; excluded from auto-gate (redundant with bootstrap ~2s later) |

## 7. Mode 1 — standalone doctor

- Invocation: `bh-daemon --doctor` (recommended; minimal surface — `bin/run-daemon.sh` already
  passes `"$@"` through at run-daemon.sh:243). Alternative: a separate `bh-doctor` console script
  in `pyproject.toml` `[project.scripts]` (currently only `bh-daemon`, pyproject.toml:45-49). See
  Q1.
- Behavior: parse args, load config.env + registry (reusing cli.py's existing load path), run
  `doctor.run_report(ctx)` over the full catalog, print the report, **do not** start the daemon
  (return before `bootstrap_secrets` / `run_daemon`).
- Dev-box carve-out (hard constraint): the standalone mode runs **all** checks and prints the
  full report; it never aborts early on a CRITICAL fail. Auth/admin/ruleset/OAuth-volume checks
  that legitimately cannot pass on a dev box report `WARN`/`SKIP` with an explanation, not a hard
  block.
- Exit code: **0 by default** (pure diagnostic), with `--strict` returning `1` when any CRITICAL
  check FAILs (for CI use). See Q7.

## 8. Mode 2 — daemon auto-gate

- Phase A: `cli.main` calls `doctor.run_gate(ctx, phase=PRE_BOOTSTRAP)` after config/registry
  load, before `bootstrap_secrets` (cli.py ~347). A CRITICAL FAIL emits a message to stderr and
  `sys.exit(1)` — the daemon does no work.
- Phase B: `reconcile_startup` calls `doctor.run_gate(ctx, phase=POST_BOOTSTRAP)` as its first
  step (before the existing G2/G1 blocks), using the minted `installation_token`. A CRITICAL FAIL
  emits `alert(..., severity="critical")` then `sys.exit(1)`, matching the existing G3a/b/c fatal
  pattern (reconcile.py:140-199) so `bin/verify-recovery.sh` assertions keep passing.
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
Secrets; mirrors the structural-only checks already in reconcile.py:157-160,176-178 and
verify-recovery.sh:209,277-278).

## 10. Vocabulary unification

Two naming vocabularies exist today: `G#` (reconcile.py startup gates) and
`_should_launch_worker`/#144 (per-launch, daemon.py). The doctor introduces stable catalog IDs
(§6). Recommendation: the catalog IDs become the canonical vocabulary; the existing `G#` gates
that migrate into the catalog keep their **exact message text** (so verify-recovery.sh greps at
verify-recovery.sh:426,478,532,601 still match) but gain a catalog ID alias. Do **not** renumber
the surviving `G#` names — the pinned tests (test_reconcile.py) and verify-recovery.sh depend on
them. See Q4.

## 11. Live bws PEM-fetch dry-run

Recommendation: **opt-in `--check-vault` on the standalone doctor only; excluded from the
auto-gate.** A live fetch is the only check that (a) makes a network round-trip and (b) touches
secret material. In the daemon path it is redundant — `bootstrap_secrets` performs the real fetch
seconds later (cli.py:354-361) and any failure surfaces there. For the standalone operator it has
real value (confirms the secret ID + `BWS_ACCESS_TOKEN` actually resolve a non-empty PEM). When
run, it checks non-empty only and never prints the material. See Q2.

## 12. Migration of shell-side checks + #192/#194

- **run-daemon.sh** (per D8): **no behavior change.** The `LABELS_PRESENT` and
  `GITIGNORE_SYMPHONY` shell blocks (run-daemon.sh:174-226) and the pre-Python
  `BH_PROJECT_ROOT`/config.env checks (run-daemon.sh:93-172) stay as a fast fail-fast gate before
  `bh-daemon` starts. The Python catalog covers the same two checks so the daemon + standalone
  paths have an in-process source of truth; the shell copies are the intentional, cheap early
  gate. This softens strict "consolidate" only for these two shell checks, which is the locked
  choice.
- **verify-recovery.sh**: unchanged in behavior; it is a recovery-gate **test harness**
  (Linux-only), not a preflight. It will keep passing because D4 preserves the migrated credential
  gates' exact message text (§10).
- **#192 / PR #194** (router-confirmed 2026-07-01: PR #194 is **OPEN**, `Fixes #192`; issue #192
  exists): #192 adds a non-fatal `BWS_ACCESS_TOKEN` notice to `bin/setup-env.sh` at **install
  time**, before the daemon exists. Decision (locked): **leave #192 as the dev-setup-time
  complement.** The doctor's `ENV_BWS_ACCESS_TOKEN` check is the **runtime-enforcing** version at a
  different lifecycle moment. They do not conflict; the slight overlap is intentional (a friendly
  nudge during setup, a hard gate at daemon boot). Because #194 is still open, Phase 5 must
  re-check its state before touching `bin/setup-env.sh` (see §15).

## 13. Decisions (locked)

Accepted as provisional defaults by the router on 2026-07-01 (user away); **re-ratify with the
user before implementation.** Each row is the locked choice + one-line rationale.

| # | Decision (locked) | Rationale |
|---|-------------------|-----------|
| D1 | Invocation = `bh-daemon --doctor` flag; no separate entry point | Minimal surface; `run-daemon.sh:243` already forwards `"$@"`; avoids a new `[project.scripts]` entry |
| D2 | Live PEM-fetch = opt-in `--check-vault`, standalone only; excluded from the auto-gate | Redundant in the daemon path (`bootstrap_secrets` does the real fetch seconds later, cli.py:354-361); only touches secret material when the operator opts in |
| D3 | Daemon gate is two-phase: Phase A pre-bootstrap in `cli.main`; Phase B post-bootstrap in `reconcile_startup` | No-auth checks fail fast before the vault round-trip; auth-needing checks run once the App token exists; respects the Gap 1A invariant (cli.py:374-376) |
| D4 | G3a/b/c reuse the existing validators; `reconcile.py` stays the caller with identical message text | Lowest risk to the pinned `test_reconcile.py` and to `verify-recovery.sh` greps (verify-recovery.sh:426,478,532,601) |
| D5 | Keep both ruleset gates: new startup CRITICAL (Phase B) + existing per-launch (`_should_launch_worker`) | Startup gate fails fast on a misconfigured repo; per-launch gate is the only guard against a ruleset deleted/drifted mid-run |
| D6 | Classification: `claude` CRITICAL at boot; `uv` WARNING/setup-only; repo-admin WARNING/informational | Workers run `claude -p`; the venv already exists at runtime so `uv` is setup-time; repo-admin is "where checkable" per the issue |
| D7 | Standalone exit = 0 report-only by default; `--strict` returns 1 when any CRITICAL FAILs | Diagnostic by default (dev-box carve-out); `--strict` makes it CI-usable |
| D8 | Keep `LABELS_PRESENT` + `.symphony`-gitignore checks in `run-daemon.sh` as a fast pre-Python gate; the Python catalog covers them too | No `run-daemon.sh` behavior change; the shell gate stays a quick fail-fast, the catalog is the in-process source of truth for the daemon + standalone paths |

## 14. Test strategy

Mirror the established injected-seam style (unittest.mock `patch`/`AsyncMock` over injected
callables; no pytest-asyncio; `once=True` + `asyncio.run` for daemon paths — per
test_daemon_preflight.py and tests/chain/conftest.py:13-30 which autouse-patches
`reconcile_startup` to a no-op).

- `tests/chain/test_doctor.py` — unit-test each `Check` with a hand-built `DoctorContext` whose
  runner/`fetch_secret`/filesystem seams are mocked. Cover PASS / FAIL / WARN / SKIP per check;
  assert `detail` and `fix` strings are present and that no secret value ever appears in any
  `CheckResult` field (mirrors test_alert_post.py's "webhook URL not logged" pattern noted at
  test_daemon_preflight.py:59). Cover `run_report` (never aborts) and `run_gate` (raises
  `SystemExit` on first CRITICAL fail).
- `tests/chain/test_cli_doctor_gate.py` — Phase A: patch the doctor gate and assert `cli.main`
  calls it before `bootstrap_secrets` and exits 1 on CRITICAL fail without reaching
  `run_daemon`. Assert `--doctor` runs `run_report` and returns without starting the daemon.
- `tests/chain/test_reconcile.py` (extend) — Phase B: assert `reconcile_startup` invokes the
  post-bootstrap gate before G2/G1, and that a CRITICAL ruleset/label fail exits 1 with an alert.
  Tests that exercise reconcile override the autouse no-op fixture with explicit patches
  (conftest.py:22-24).
- Ruleset checks reuse the `RulesetStatus` fixtures already exercised in test_ruleset_status.py.

## 15. Phased task breakdown (TDD-first)

Each phase is one focused PR off `main` (worktree per CLAUDE.md § Worktrees). Write the failing
test first, then the implementation. Every phase reflects the locked design in § Decisions
(locked).

- **Phase 0 — re-ratify + confirm.** Get the user's explicit re-ratification of D1-D8 (the router
  accepted them as provisional defaults, not the user). No code until this clears.
- **Phase 1 — `doctor.py` catalog + no-auth checks (Phase A set).** `Severity`, `CheckStatus`,
  `CheckResult`, `DoctorContext`, `Check`, `CATALOG` (with `severity` + `phase` tags per D3/D6),
  `run_report` (never aborts), `run_gate` (raises `SystemExit(1)` on first CRITICAL fail).
  Implement the PRE_BOOTSTRAP checks: `CLI_GH`/`CLI_BWS`/`CLI_CLAUDE` CRITICAL + `CLI_UV` WARNING
  (D6), `ENV_PROJECT_ROOT`, `CFG_CONFIG_ENV`, `CFG_REQUIRED_KEYS` (reuse `sandbox_config`),
  `CFG_OPTIONAL_SECRET_IDS` WARNING, `ENV_BWS_ACCESS_TOKEN` (presence/shape only),
  `GITIGNORE_SYMPHONY`, `CRED_ANTHROPIC_UNSET`, `ENV_HOST_ENV` WARNING. Tests: test_doctor.py. No
  wiring yet.
- **Phase 2 — standalone `bh-daemon --doctor` (D1).** Add the `--doctor` flag to `cli.py`
  argparse (no separate entry point); run `run_report`, print the report, return before daemon
  start. Add `--strict` (D7): exit 0 report-only by default, exit 1 on any CRITICAL FAIL under
  `--strict`. Tests: test_cli_doctor_gate.py (report path + both exit modes).
- **Phase 3 — Phase-A daemon gate (D3).** Wire `run_gate(ctx, PRE_BOOTSTRAP)` into `cli.main`
  after config/registry load and before `bootstrap_secrets` (~cli.py:347). Tests:
  test_cli_doctor_gate.py (asserts the gate runs before bootstrap and exits 1 on CRITICAL fail
  without reaching `run_daemon`).
- **Phase 4 — auth-needing checks + Phase-B gate (D3/D4/D5).** Add `GH_AUTH`, `RULESET_MAIN`,
  `RULESET_FEATURE` (reuse `ruleset_is_provisioned`), `LABELS_PRESENT`, `GH_REPO_ADMIN` WARNING,
  `CRED_OAUTH_VOLUME`. Per D4, the credential checks reuse the existing validators and keep
  identical message text; `reconcile.py` remains the caller for G3a/b/c. Wire
  `run_gate(ctx, POST_BOOTSTRAP)` into `reconcile_startup` as its first step (before G2/G1). Per
  D5, leave the per-launch `_should_launch_worker` gate untouched. Tests: test_doctor.py +
  test_reconcile.py (reconcile tests override the conftest autouse no-op, conftest.py:22-24).
- **Phase 5 — docs + overlap re-check (D8).** **No `run-daemon.sh` behavior change** — the shell
  `LABELS_PRESENT`/`GITIGNORE_SYMPHONY` checks stay as the fast pre-Python gate. Update
  `README.md` + `docs/smoke-test-daemon.md` to document `bh-daemon --doctor`, `--strict`,
  `--check-vault`, and the two-phase boot gate. **Before touching `bin/setup-env.sh`: re-check
  the live state of PR #194 / issue #192** (`gh pr view 194` / `gh issue view 192`). If #194 is
  still open, do **not** modify `setup-env.sh` — #192's setup-time `BWS_ACCESS_TOKEN` notice
  stays as the complement to the runtime doctor (D-note in §12); coordinate rather than collide.
  If #194 has merged, confirm its check still reads as the setup-time complement and adjust docs
  only.
- **Phase 6 — opt-in `--check-vault` (D2).** Live `bws` PEM dry-run, standalone-only, non-empty
  check that never prints the material. Tests: test_doctor.py (mocked `fetch_secret` seam;
  assert the value never lands in any `CheckResult` field).

## 16. Out of scope

- Auto-remediation (the doctor reports fixes; it does not run them).
- Any change to G2/G1 recovery-state checks (they stay in `reconcile.py`).
- Windows support for the auth-needing checks beyond what `gh`/`bws` already provide;
  `verify-recovery.sh` remains Linux-only (verify-recovery.sh:7-10).
- Renumbering or renaming the existing `G#` gates.
