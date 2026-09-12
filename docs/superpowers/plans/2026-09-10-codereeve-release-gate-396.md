# CodeReeve 0.2 Release Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce repeatable evidence for the 0.2 installation, upgrade, rollback, and external repository cutover required by #396.

**Architecture:** Reuse the production migration and service coordinator, keeping portable fault injection separate from installed-wheel and live-systemd evidence. A versioned, synthetic upgrade fixture supplies the same inputs to the 0.2 gate and later #397/#398 upgrade runs. This decomposition follows #396 and the approved spec's Verification contract.

**Tech Stack:** Python >=3.10, pytest, uv, immutable wheel/sdist provenance, Linux systemd and cgroup v2. Sources: `pyproject.toml:L9-L28`; `docs/codereeve-service-cutover.md:L77-L99`.

**Spec:** `docs/superpowers/specs/2026-09-07-codereeve-rename-design.md` (approved; #390, #392–#398).

**Status:** Local implementation authorized 2026-09-12. Tasks 1–4 are scoped; Task 5 requires the release/closure sequencing decision below before external execution.

**touches:** `tests/release_gate/**`, `tests/fixtures/codereeve-upgrade-v1.json`, existing migration/service/build tests, release-gate documentation/evidence/release notes, README/current links, downstream issue bodies, integration/release authority, GitHub repository metadata/protection/Actions, and deployment remotes (#396).

## Global Constraints

- Product `CodeReeve`; technical identifiers `codereeve`; canonical release `0.2.0`. Legacy compatibility remains through 0.3.x and is removed in 0.4.0 (#390, #396).
- Keep the original installation environment unchanged and usable after rollback; reject overlapping distribution ownership (#396).
- Preserve custom paths, original configuration bytes, backups, third-party names, and Symphony provenance (#396; approved spec, Filesystem migration and Non-goals).
- Never start the old service until files, executable/environment selection, activation state, and writer quiescence have been verified restored (#396).
- Errors, reports, manifests, and retained public evidence must not contain secret values (#396).
- Work on `feature/396-codereeve-release-gate`, targeting `feature/396-codereeve-0-2-cutover`; the primary branch's final PR targets `main` (#396, Technical Notes).
- Merge/release/deployment/repository rename remain separate external actions. Preparation does not authorize those actions. Repository rename is last, after successful canonical installation (#396; session execution boundary).
- Keep #390 open. Do not close #392 before its primary release PR merges; do not claim #395 complete before PR #402 merges (PR #399; PR #402).

## Verified starting point and execution gates

Updated 2026-09-12:

| Evidence | Consequence |
|---|---|
| Integration HEAD `b6959fb4864d8b4a75a507e7e1b9f27f60cbc1f7` includes PR #401. | Package/configuration/service work is available for planning. |
| PR #402 merged at `1787a9a0bc390886efed4b0ac70a46d5adc00434`; #395 is closed. This child branch contains that merge. | The identity implementation dependency is satisfied (PR #402; #395). |
| `tests/service_cutover/conftest.py:L169-L192` writes a synthetic interpreter and console script. | Existing portable service tests do not prove an actual old installation remains executable. |
| `tests/service_cutover/test_recovery.py:L200-L258` enumerates durable forward prefixes; `tests/test_migration_transaction.py:L570-L605` exercises observed migration boundaries. | Reuse these tests and extend missing coverage; do not replace production transactions with a second test-only implementation. |
| `src/codereeve/verify_foundation.py:L839-L893` builds with `0.0.0+foundation`. | Foundation success is supporting evidence, not proof of a release-version 0.2.0 wheel. |
| `.github/workflows/ci.yml:L3-L5` runs full CI only for PRs to `main`. | Integration-branch fast CI cannot substitute for the final main-target CI run. |
| No disposable Linux host has been selected in this session. | Live activation and OS-level interruption proof remain unexecuted; obtain the host details before those steps. |

The legacy source candidate is immutable commit `e25749fc48aff6500744b2b202534f1d56c9b834`, whose `pyproject.toml` still declares the old distribution and six old entry points. This is a source-built upgrade fixture, not a claim that a tagged legacy release exists. Record the actual artifact version, revision, lock identity, and digest when building it (#396; `e25749f:pyproject.toml`).

## Task 1: Durable upgrade fixture and canonical-only assertions

**Files:** Create `tests/fixtures/codereeve-upgrade-v1.json`, `tests/release_gate/test_upgrade_fixture.py`, and `tests/release_gate/fixture.py`. Create `docs/codereeve-release-gate.md` to document fixture meaning and reuse. These are proposed deliverables, not existing artifacts (#396, durable-upgrade-fixture criterion).

**Interfaces:** `load_fixture(path: Path) -> dict[str, object]` validates schema/version and returns synthetic fixture data. `materialize_fixture(root: Path, fixture: dict[str, object]) -> None` writes only beneath a newly created disposable root. The fixture schema contains `schema_version`, `legacy_source_revision`, `files`, and `canonical_assertions`; file entries use relative POSIX paths, UTF-8 content, and explicit permissions. Do not put a private key or real token in the fixture (#396, secret-safe and durable-fixture criteria).

- [ ] Write failing tests for fixture availability, schema validation, path containment, and refusal of a nonempty destination. Use this contract:

  ```python
  def test_fixture_refuses_existing_root(tmp_path: Path) -> None:
      root = tmp_path / "host"
      root.mkdir()
      (root / "operator-file").write_bytes(b"preserve")
      fixture = load_fixture(FIXTURE_PATH)
      with pytest.raises(ValueError):
          materialize_fixture(root, fixture)
      assert (root / "operator-file").read_bytes() == b"preserve"
  ```

- [ ] Seed both managed legacy config and runtime inputs, host config, secrets, ruleset baseline, and an untouched Symphony sentinel. Include legacy assignments using recognized default paths and a separate custom-path case. Record expected canonical keys and preserved custom/third-party values (#396; approved spec, Filesystem migration).
- [ ] Validate paths before any writes: reject absolute paths, drive/UNC syntax, `..`, duplicate paths, and symlink parents. Preserve deterministic bytes; use only synthetic values. Run `./.venv/Scripts/python.exe -m pytest tests/release_gate/test_upgrade_fixture.py` on Windows and `.venv/bin/python -m pytest tests/release_gate/test_upgrade_fixture.py` on Linux.
- [ ] Feed the fixture through real migration inventory/apply/restore using the existing portable storage seam. Assert originals/backups byte-for-byte, merged canonical state, and retained Symphony/custom content. Use the migration APIs in `src/codereeve/migration/transaction.py:L592-L610` and `:L930-L947`.
- [ ] After migration, parse all resulting config and require zero legacy uses and zero legacy product keys. `export_legacy=False` alone is insufficient: it prevents alias export but still accepts legacy input (`src/codereeve/config_env.py:L296-L330`). Check both the parsed keys and `legacy_uses` result.
- [ ] Document these assertions as the starting contract for #397/#398; no later-release execution is part of this task (#396). Commit the fixture, loader, tests, and documentation together.

## Task 2: Installed environments and exact release artifacts

**Files:** Create `tests/release_gate/test_installed_upgrade.py`; extend `docs/codereeve-release-gate.md`. Reuse `src/codereeve/verify_foundation.py`, `hatch_build.py`, and the existing build-provenance tests without changing the foundation verifier's advertised synthetic-version contract (#396; `src/codereeve/verify_foundation.py:L876-L893`).

**Interfaces:** Consume Task 1's fixture and two explicit wheel paths. The integration test must run each environment's own interpreter/executable from outside either checkout. It must produce a secret-free evidence record containing artifact SHA-256, version, source revision, lock identity, interpreter version, and command outcomes (#396, wheel/sdist and separate-environment criteria).

- [ ] Write the failing test that creates separate old/candidate environments, installs the explicit wheels, snapshots the old environment's files, and executes the old CLI before and after a failed-upgrade rollback. Assert unchanged old package/script bytes and that the old interpreter still imports its own package. Do not use the synthetic interpreter from the portable service fixture as this evidence (`tests/service_cutover/conftest.py:L169-L192`).
- [ ] Build the old source candidate from the exact revision recorded above in a disposable checkout using its own build/lock contract. Record its real build assertions and do not describe it as a published release (`e25749f:hatch_build.py`; #396).
- [ ] Build wheel and sdist at the eventual clean candidate commit with these explicit assertions; clear both development flags first:

  ```bash
  unset CODEREEVE_BUILD_DEVELOPMENT BH_BUILD_DEVELOPMENT
  export CODEREEVE_BUILD_VERSION=0.2.0
  export CODEREEVE_BUILD_SOURCE_REVISION="$(git rev-parse HEAD)"
  uv build --wheel --sdist
  ```

  Run in the candidate worktree. `hatch_build.py:L114-L172` rejects revision mismatch and development/release ambiguity. Rebuild from the sdist and reconcile version/revision/lock/resources/entry points across all three artifacts (#396).
- [ ] Exercise canonical CLI help/version/provenance and strict installation checks through the installed wheel. Exercise legacy wrappers and require their 0.4.0 removal notice; use the existing smoke contracts in `tests/test_verify_foundation.py:L857-L1126`.
- [ ] Deliberately co-install both distributions only in an additional disposable negative-test environment, and require the installation ownership gate to reject it. Never use the retained old environment for this experiment (#396).
- [ ] Run the installed test on Python 3.10 and 3.13, then `codereeve verify --python 3.10 --python 3.13 --keep-temp` on the same committed source. Record the release-version evidence separately from the foundation proof (#396; approved spec, Verification contract).
- [ ] Commit the installed tests and evidence format; retain machine-specific raw artifacts outside tracked source. Track only sanitized evidence and durable commands (#396).

## Task 3: Complete failure and interruption matrix

**Files:** Extend `tests/service_cutover/test_recovery.py`, `tests/test_migration_transaction.py`, and Task 1's fixture consumers. Create a coverage table in `docs/codereeve-release-gate.md` mapping each production boundary to its test and evidence class (#396, every-failure-boundary criterion).

**Interfaces:** Reuse `cutover(spec, backend=..., storage=...)`, `recover(journal_path, backend=..., storage=...)`, migration `FileOperations.boundary(name, phase, path)`, and `CutoverJournal.record`. Existing definitions: `src/codereeve/service_cutover/coordinator.py:L63-L80`, `:L484-L510`; `src/codereeve/migration/transaction.py:L109-L169`.

- [ ] Record the complete observed event sequence for successful fresh, upgraded, and install-only-then-upgraded runs, plus failed runs traversing reverse recovery. Successful forward traces cannot enumerate reverse-restoration events. Assert the enumerator visits all events; fixed numeric ranges must fail if they silently omit newly added boundaries (`tests/service_cutover/test_recovery.py:L200-L258`; #396).
- [ ] For each mutation boundary, inject a caught exception before and after the effect in a fresh fixture. Before durable commit, assert files and exact service selection are restored before old activation. After durable commit, assert canonical selection and safe forward completion, never old-service restart. For corruption/operator drift assert incomplete recovery and no unsafe restart. In every case assert at most one daemon is active and backups remain (`src/codereeve/service_cutover/recovery.py:L338-L339`, `:L464-L576`; #396).
- [ ] Repeat with abrupt subprocess termination and a new recovery process on Linux. Include journal creation/write, copy/rewrite, fsync, publication, old stop, unit selection, candidate start, health, enablement, commit receipt, and reverse restoration. Derive mutation names from production observation rather than inventing an unrelated matrix (#396; migration transaction boundary API above).
- [ ] Add explicit negatives for stale heartbeat, changed invocation, timeout, critical strict doctor findings, unknown workers, environment aliases, filesystem coexistence, and secret redaction. Extend the existing health/readiness tests rather than bypassing strict gates (#396; `tests/service_cutover/test_health.py`, `tests/service_cutover/test_readiness.py`).
- [ ] Run affected suites and record portable versus actual OS durability coverage separately. Commit regression tests and the coverage table together.

## Task 4: Disposable live systemd acceptance

**Files:** Extend `docs/codereeve-release-gate.md` with exact host preparation, commands, assertions, rollback, and evidence capture. Store sanitized execution evidence under `docs/verification/`; retain private journals/backups on the disposable host (#396; `docs/codereeve-service-cutover.md:L103-L114`).

**External prerequisite:** A user-selected disposable Linux VM with PID 1 systemd, cgroup v2, a dedicated non-root service account, root coordinator access, a disposable managed repository, and provisioned test credentials. No host has been selected yet; this is a required input, not a passing gate (`docs/codereeve-service-cutover.md:L77-L99`).

- [ ] Verify host prerequisites read-only, record versions and candidate artifact identity, and snapshot the disposable host before activation. Do not reuse a production daemon's state (#396).
- [ ] Run fresh installation and legacy upgrade as separate fixture scenarios. Run render-only and install-only first, proving no unit activation. Execute the real installer against the chosen candidate environment using the documented flags (`docs/codereeve-service-cutover.md:L20-L46`).
- [ ] Require strict installation/configuration/live probes under the service identity and effective environment. Record exact executable, HOME, unit activation state, invocation identity, cgroup membership, and a heartbeat newer than start; never include secret values (#396).
- [ ] Run injected failure/interruption cases from Task 3 in disposable snapshots, recovering from each reported journal. Verify the old environment's executable selection and actual usability before treating rollback as complete (#396).
- [ ] Record every failed/skipped/unexecuted gate honestly. Commit sanitized evidence only after inspection. No portable fake or ordinary report exit code can satisfy a live-systemd criterion (#396; approved spec, Service cutover).

## Task 5: Integration, release, and last-step repository rename

**Files:** Create `docs/releases/0.2.0.md`; update README and current repository links after the actual rename. Remove completed plan files only after preserving durable rationale and redirecting their references (repository lifecycle policy; #396).

**Sequencing decision required before external execution:** #396 includes external rename/post-rename acceptance, while the repository requires the primary branch issue among its closing directives. A primary PR must not auto-close #396 before those operations complete. The final ordering of publication, main merge, rename, and issue closure must be settled with the user before marking this task executable. Any post-rename source edits invalidate the prior final artifact revision and require a fresh build/install verification. This gate does not block portable fixture/test preparation (#396; repository PR policy; `hatch_build.py:L151-L172`).

- [ ] After PR #402 merges, refresh the integration base, bring it into this child branch, and run the identity audit. Add only exact justified exceptions for the deliberate legacy upgrade fixture; do not weaken the audit (PR #402). Manually close #395 only once its merge is confirmed; keep #392 for the primary release PR (PR #399).
- [ ] Prepare release notes covering canonical commands/configuration, separate environments, migration check/apply, rollback, 0.3 compatibility/0.4 removal, and the continued 1.0 release block (#396).
- [ ] Read #361, #378–#383, #243, and #168 and verify their bodies identify the canonical interfaces they consume. PR #402 already updated these nine issues; retain those changes and update only discrepancies. Keep #390 open and the 1.0 release blocked pending 0.3/0.4 work (#396; PR #402).
- [ ] Open the primary integration PR to `main` with separate closing directives for the completed release issues, milestone 14, and `needs-review`. Keep it draft while any release-gate criterion is unmet; full main-target CI must run (`.github/workflows/ci.yml:L3-L5`; #396).
- [ ] Reconcile the actual final PR deliverables against `git diff main...HEAD --stat` and `git ls-tree HEAD`. Verify all fixture files and referenced evidence are tracked before approval (repository artifact-persistence policy).
- [ ] Before a merge or release, inspect live reviews, pending requests, mergeability, and CI on the exact candidate. Rebuild and re-verify artifacts if the source revision changes (#396; repository PR policy).
- [ ] After canonical installation succeeds and external cutover is authorized, rename the GitHub repository to `codereeve`. Verify repository identity, metadata, default-branch protections/rulesets, Actions, links, clone instructions, and relevant deployment remotes against recorded pre-rename state. Do not rely on redirects (#396).
- [ ] Publish only the artifacts whose exact source and lock identities passed the gate; verify the published artifact identities. Keep #390 open and update #396 with the release/cutover evidence (#390, #396).

## Self-review and current status

All #396 acceptance criteria map to Tasks 1–5. Task 1 covers durable/canonical-only fixture readiness; Task 2 covers separate environments, compatibility, and distribution evidence; Task 3 covers failures, interruption, conflicts, quiescence, and redaction; Task 4 covers actual clean-host/upgrade/service execution; Task 5 covers integration, notes, repository metadata, downstream issue interfaces, and issue sequencing. Coverage mapping does not resolve the explicit external sequencing decision above (#396).

Preparation only: no release gate, deployment, merge, release, or repository rename has been performed by this plan. The live-host prerequisite and Task 5 external sequencing remain open execution dependencies. Refresh their states before execution; this document is not a substitute for live evidence (#396; PR #402).
