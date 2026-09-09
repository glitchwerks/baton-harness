# CodeReeve Repository Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development for execution and task/whole-branch review.

**Goal:** Complete #395: make repository-owned interfaces and current guidance canonical while retaining the explicit 0.2/0.3 compatibility contract.

**Architecture:** Reuse the canonical CLI, configuration resolver, and service coordinator merged by #392–#394. Change their consumers and presentation; do not create alternate configuration or migration implementations (#395).

**Tech Stack:** Python, pytest, Bash, GitHub Actions, Markdown, JSON.

**Spec:** `docs/superpowers/specs/2026-09-07-codereeve-rename-design.md` (approved design for #390); live acceptance criteria #395. Integration base: b6959fb (PR #401).

## Global Constraints

- Work in `feature/395-codereeve-repository-identity`, targeting `feature/396-codereeve-0-2-cutover`. No merge, deployment, release, or GitHub repository rename (#395, #396).
- Preserve documented legacy commands/imports/environment/path selection through 0.3.x, with removal in 0.4. Preserve Symphony/Baton provenance and frozen patches (#390, #395).
- Actual GitHub fetch/security URLs retain the existing repository slug until #396; label their audit exceptions as pre-cutover migration references (approved spec, canonical identity contract).
- Retain deployed `harness-main-no-merge` and `harness-feature-daemon-only` ruleset identifiers as explicit compatibility identifiers. Renaming these changes persisted security resources: provisioning looks up by name and compares `name`, while runtime verifies those names. This task canonicalizes template placeholders and guidance without an unrequested security-resource migration (`bin/provision-ruleset.sh:381-464`, `config/ruleset.compare-keys.json:1`, `src/codereeve/chain/ruleset_status.py:129-130`; #395 excludes service/migration behavior).
- Preserve shared non-executing parsing before effects, strict doctor phase gates, fresh-heartbeat checks, separate installation environments, and complete restoration guidance (#393–#395).
- Keep CRLF working files, use the worktree's `.venv/Scripts/python.exe`, and run focused checks per task. Run the final affected suite/static checks and committed-source wheel/sdist verification once; broaden only for new failures (repository instructions; #395).
- Controller owns `AGENTS.md` and `CLAUDE.md`; workers own assigned implementation/prose files. No concurrent implementers or unrequested refactoring (agent-authoring skill).

## Task 1: Canonical runtime, generated hooks, and CI

**Files:** `src/codereeve/_cli.py`, active presentation/help/docstrings in `src/codereeve/after_create.py`, `after_run.py`, `before_run.py`, `chain/*.py`, `hooks/*.py`, `verify_foundation.py`; `.github/actions/setup/action.yml`, `.github/workflows/ci.yml`; corresponding existing tests, especially `tests/test_after_create_drops_claude_settings.py`. Keep compatibility implementations and vendor provenance intact.

1. Add failing focused tests for generated settings using the installed `codereeve hook force-pr-not-merge` command on POSIX and Windows, including executable paths containing spaces. Exercise executable command behavior where the host permits; preserve the no-merge hook input/output contract. Source: current wrapper selection in `src/codereeve/_cli.py:107-158`, #395 hook-wiring criterion.
2. Replace the generated legacy-wrapper command with the canonical executable plus subcommand using platform-appropriate quoting. Preserve the function's return schema and callers.
3. Canonicalize active operator messages, examples, internal documentation paths, and temporary foundation prefixes. Keep actual compatibility wrapper checks and compatibility aliases. Prefer canonical environment keys after resolution without removing boundary alias materialization (#393, #395).
4. Update CI's build flag and foundation invocation to `CODEREEVE_BUILD_DEVELOPMENT` and `codereeve verify`; use the github-actions skill for workflow edits (#395).
5. Update corresponding non-compatibility fixtures/assertions, run focused tests and relevant static checks, self-review and commit. Report remaining intentional references for Task 4.

## Task 2: Canonical shell tools and templates

**Files:** `bin/*.sh`, `bin/lib/load-config.sh`, `scripts/pilot-dry-run.sh`, `scripts/probe_assert.py`, `config/WORKFLOW.md`, `config/ruleset*.json`, their `src/codereeve/resources/` mirrors, and corresponding shell/provisioning/probe tests.

1. Add focused failures for canonical launcher/help output and rendered workflow/template values. Verify rejected/conflicting config causes no subprocess/network/filesystem effects; reuse existing #393 coverage rather than reimplement precedence (#395).
2. Replace active legacy command, environment, path and product examples with canonical forms, preserving explicit compatibility bootstrap/selection paths and third-party names. The pilot launch remains retired; canonicalize its actionable replacement instructions (#395).
3. Change internal JSON placeholders to canonical names and update all renderers/fixtures together. Preserve deployed ruleset names under the explicit compatibility ruling above; mirror packaged resource contents exactly (#395).
4. Run focused affected tests plus ShellCheck on LF scratch copies, self-review and commit. Report intentional exceptions for Task 4.

## Task 3: Current guidance, project instructions, and backlog

**Files:** `README.md`, current operator/design docs under `docs/`, `.github/ISSUE_TEMPLATE/`, controller-owned `AGENTS.md` and `CLAUDE.md`. Preserve frozen historical research/spec records; update live guidance rather than falsifying history (#395).

1. Update current product description, installation/development commands, onboarding, authentication, architecture, setup, smoke tests and troubleshooting to canonical names. Document the literal grammar, unsupported-expression conversion, separate environments, exact service cutover/restoration order, strict phase gates and fresh heartbeat evidence (#393–#395).
2. Clearly separate legacy migration examples from current instructions and state compatibility through 0.3.x/removal in 0.4. Keep only necessary current GitHub slug references (approved spec).
3. Retire the completed #394 plan after verifying durable rationale exists in #394/PR #401 and redirecting committed references. Check #392/#393 issue state before deciding whether their plans have completed; preserve durable decisions before removal (repository plan lifecycle instructions).
4. Controller updates/cross-links open #361, #378–#383, #243 and #168 with canonical future command-tree guidance and #395 context. Do not invent implementation commitments for their independent designs (#395).
5. Validate executable documentation examples with focused tests or CLI help/argument probes; check resource links and commit. Report historical/migration exceptions for Task 4.

## Task 4: Exact identity audit and final integration

**Files:** new `scripts/audit_identity.py`, `tests/test_identity_audit.py`, `tests/data/identity-allowlist.json`; remaining ordinary test fixtures containing legacy defaults. Existing `tests/test_codereeve_config_contract.py` remains enforced.

1. Add RED tests for an unapproved legacy reference, changed/duplicated occurrence, stale exception, invalid category, missing reason, and legacy filename. Audit tracked UTF-8 text plus tracked paths, not ignored runtime data (#395 exact-text allowlist criterion).
2. Implement an exact-reference catalog (path, matched text, occurrence count, category and reason; line numbers diagnostic only). Allowed categories are compatibility, migration, historical and provenance. Exclude the catalog itself explicitly and validate its schema; no broad filename/glob exemption. Errors report location/token without runtime values (#395; #393 diagnostics contract).
3. Canonicalize remaining ordinary fixtures. Curate exceptions from actual source: do not automatically bless all old references as compatibility. Freeze provenance and historical exceptions; reject newly introduced or obsolete allowances (#395).
4. Run audit tests and the existing config contract, then all affected fast tests, ruff/mypy, and final whole-branch review. Address findings with a consolidated fix and scoped re-review.
5. Commit before `codereeve verify --python 3.10 --python 3.13 --keep-temp`; confirm exact-source wheel/sdist evidence. Audit referenced artifacts with `git ls-tree HEAD`, review both integration-base and main diff stats. Open the focused PR into the integration branch with `Closes #395`, milestone 14 and `needs-review`; report validation/limitations without claiming live Linux deployment (#395, #396; repository PR rules).

## Review boundaries

Tasks 1 and 2 meet at generated hook/CLI contracts; Task 2 supplies templates documented by Task 3. Task 4 consumes the final source and all explicitly retained references. Review each task for both #395 acceptance and code quality, then review the full branch. No task may waive another task's acceptance criteria (#395).
