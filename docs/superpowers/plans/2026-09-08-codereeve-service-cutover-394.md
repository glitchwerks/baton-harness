# CodeReeve Service Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Implement the reversible systemd installation and cutover required by #394, including interruption recovery and verification under the service identity.

**Architecture:** Keep the existing shell installer entry point, dispatching into a packaged Python coordinator. Separate pure unit rendering, Linux service observation/control, durable recovery storage, and transaction policy. Consume #393 migration through its verified shutdown callback and an optional borrowed writer lease; never weaken generic restoration's drift rejection.

**Tech Stack:** Python >=3.10, standard library, Bash launcher, system systemd manager and cgroup v2 for activation. Existing pytest/Ruff/mypy toolchain; Windows runs pure and injected backend tests but does not claim live systemd or directory crash durability.

**Spec:** `docs/superpowers/specs/2026-09-07-codereeve-rename-design.md`, especially Service cutover. #394 is the scope/acceptance authority. Dependency: PR #400 at f7ae261; branch was created from the #396 primary branch and locally fast-forwarded to this related prerequisite. Do not merge a GitHub PR as part of implementation.

## Global Constraints

- Canonical executable/unit/secrets: `codereeve`, `codereeve.service`, `/etc/codereeve/secrets.env`; retain the original environment and backups through 0.3.x (#392, #393, #394).
- Unit rendering and activation are separate. Preserve `--print-unit` and `--no-start`; never start a daemon from a render operation (#394; `bin/install-daemon-service.sh:53`).
- Pre-stop checks verify the separate installed wheel and explicit strict installation/configuration phases. Post-start checks require strict live results plus fresh evidence tied to the new systemd invocation and PID (#394; `src/codereeve/chain/cli.py:379`; `src/codereeve/chain/doctor_report.py:91`).
- Preserve the old executable/environment selection unchanged. Do not install into or modify the old virtual environment (#392; rename spec Service cutover).
- No two daemons or workers may run against the repository during cutover. A process-name scan or an available lease alone is not proof (`src/codereeve/migration/transaction.py:85`; #394).
- Scope of automatic activation is a verifiable system-service deployment: both known units, their complete cgroup subtrees, systemd restart guards, and the cooperative project lease. Reject unsupported cgroups, unexpected unit/drop-in ownership, unobservable processes, or unmanaged relevant writers. Do not silently label unknown writer ownership CLEAR (#393, #394).
- Rollback order: stop/verify new processes, restore filesystem inputs, restore unit/environment selection, restore prior enablement and activation. Any ambiguity or failed restoration leaves services stopped with incomplete recovery (#394).
- Persist intended and completed effects with checksums/fsync before/after each mutation. Recovery artifacts must not contain environment values or command output; necessary original file bytes live only in private backups (#394; #393 journal precedent).
- Third-party variables and `.symphony/` remain unchanged. Keep the top-level CodeReeve command grammar unchanged; use the installer and an internal module entry point (#392, #394).
- All real tests use disposable paths or injected command/filesystem seams. No host service stop/start, deployment, or changes under real `/etc` are authorized by this development request.
- Preserve CRLF working files. Validate committed LF blobs or LF scratch copies; use the project `.venv/Scripts/python.exe` and `uv` (#393 worktree conventions).

## Source-backed integration decisions

1. **Borrow the existing lease.** `WriterLease.hold(..., lease=...)` already validates borrowed ownership, while migration apply/restore currently acquire internally. Add optional keyword-only lease parameters with unchanged default behavior. The coordinator retains the project lease across migration and snapshot publication, releases it only for the new daemon's lifetime lease, and reacquires after proven shutdown for rollback (`src/codereeve/migration/lease.py:106`; `src/codereeve/migration/transaction.py:592`, `:926`; #394).
2. **Preserve post-start writes.** Generic restoration deliberately refuses filesystem drift. Before activation, capture the verified published migration destinations. If activation fails, stop all new writers, quarantine their changed destinations, restore the captured publication bytes/types/modes, verify, then call generic restoration. Never silently discard newly produced logs/state or relax drift checking (`src/codereeve/migration/transaction.py:824`, `:879`; #394).
3. **Hold autonomous work until commit.** Heartbeat starts before the daemon polling loop. A private cutover readiness gate keeps polling/remote issue work paused while heartbeat and health verification run. Successful durable commit releases the gate; interruption leaves work paused until recovery. Normal launches without a gate retain existing behavior (`src/codereeve/chain/daemon/poll.py:359`; #394 no-concurrent-daemons and rollback requirements).
4. **Add process evidence without changing the old heartbeat.** Keep the timestamp file format; atomically write an adjacent structured heartbeat containing PID, systemd invocation ID, and timestamp. Match both identity fields to fresh systemd state, and require time after start. systemd defines INVOCATION_ID as a unique ID for a unit runtime cycle (`src/codereeve/chain/heartbeat.py:161`; https://raw.githubusercontent.com/systemd/systemd/main/man/systemd.exec.xml, fetched 2026-09-08).
5. **Control the whole cgroup.** Require KillMode=control-group (or reject an unverifiable old unit), bounded stop, inactive state and empty complete cgroup subtree. Snapshot and hold an effective old-unit restart guard, verifying it through systemd; do not assume a runtime mask overrides a higher-priority unit. systemd documents that process/none can leave workers alive (https://raw.githubusercontent.com/systemd/systemd/main/man/systemd.kill.xml, fetched 2026-09-08; #394).

## Reviewed execution status

Tasks 1–5 are implemented and independently reviewed: rendering (`4a4e39f`), service backend (`9bf3c99`), journal/storage (`6fea1a6`), lease/heartbeat/readiness (`897e550`), and coordinator with reviewed recovery fixes (`0c6a5b6`). Task 6 and final branch review remain outstanding (#394).

The coordinator records an install-only predecessor in its initial header to survive interruption before the explicit adoption event (`b462fae`). Original interpreter snapshots attest the resolved binary while activation rechecks its lexical virtualenv selection; the original environment is never a restoration target (`b462fae`).

Review fixes add per-input restoration authority and validate all operator selections before writes; record writer-lock restoration ownership before first acquisition; and defer interrupted receipt release until current service health and selection are revalidated (`0c6a5b6`). These requirements also apply to recovery retries (#394).

Automatic activation currently refuses cross-HOME input relocation, runtime outputs outside managed state roots, and revalidation of a later invocation without the persisted startup baseline (`b462fae`, `0c6a5b6`). Documentation must identify those limits. Portable injected tests do not establish native Linux systemd behavior or directory crash durability (#394; this plan's verification boundaries).

## File responsibilities

New package `src/codereeve/service_cutover/`: `model.py` immutable inputs/results; `render.py` pure unit rendering; `systemd.py` bounded Linux command/control adapter; `health.py` strict result and fresh-heartbeat validation; `journal.py` private durable snapshots/event journal; `coordinator.py` forward/recovery policy; `cli.py` internal installer entry point; `__init__.py` narrow exports. Corresponding tests live under `tests/service_cutover/` with a shared deterministic fake backend in `conftest.py`.

Existing integration surfaces: `bin/install-daemon-service.sh`, migration transaction lease entry points, heartbeat writer and daemon poll startup, plus narrowly necessary config catalog/contract tests for the private gate. Documentation: README, system setup, smoke test, and a dedicated cutover runbook.

### Task 1: Validated service specification and pure unit rendering

**Files:** Create package init, `model.py`, `render.py`, `tests/service_cutover/test_render.py`.

**Interfaces:** `ServiceSpec(project_root: Path, environment: Path, run_user: str, workflow: Path | None, secrets: Path | None, home: Path, timeout_s: float=120.0)` is frozen and validates absolute paths, newline/NUL injection, positive finite timeout and user syntax. `render_unit(spec: ServiceSpec, *, gate: Path | None=None) -> str`. `CutoverError` carries only safe fixed diagnostics; `CutoverResult(status: str, journal_path: Path | None, recovery: str | None)`.

- [ ] Add RED tests for canonical ExecStart in a separate environment, explicit User/HOME/project root/PATH, optional EnvironmentFile, quoted paths with spaces, systemd percent/dollar escaping, conflicting units with ordering, full-cgroup kill mode, and rejected injection. Render must not read secrets, call a process, or write anything.

```python
def test_render_is_canonical(spec):
    text = render_unit(spec)
    assert 'codereeve daemon' in text
    assert 'KillMode=control-group' in text
    assert 'Conflicts=bh-daemon.service' in text
    assert 'User=runner' in text
```

- [ ] Run `.venv/Scripts/python.exe -m pytest tests/service_cutover/test_render.py -q` and establish missing-module RED.
- [ ] Implement validation and one systemd quoting helper; use `environment / 'bin/codereeve'` for Linux units, never the old `.venv` fallback. Include `After=bh-daemon.service` with the conflict so old stop precedes new start. Gate location is an optional private setting, with no secret in unit text.
- [ ] Run focused tests, Ruff/mypy and LF format checks, commit `feat(#394): render validated canonical service units`.

### Task 2: Strict service backend and health evidence

**Files:** Create `systemd.py`, `health.py`, `tests/service_cutover/conftest.py`, `test_systemd.py`, `test_health.py`.

**Interfaces:** frozen `UnitState(name, load_state, active_state, enabled_state, main_pid, invocation_id, control_group, fragment_path, dropin_paths)`; `SystemdBackend` methods `inspect(name)`, `preflight(spec)`, `stop_and_verify(name)`, `guard_old(snapshot)`, `publish(unit_bytes)`, `start(spec) -> UnitState`, `verify_health(spec, started)`, `restore_activation(snapshot)`. Injectable runner/clock/sleep/proc/cgroup roots; production uses bounded argv subprocesses and root privilege validation. Snapshot type from Task 3 may be consumed through explicit typed immutable metadata until that task lands, not an untyped placeholder.

- [ ] RED tests: malformed/missing systemctl properties, timeout, failed/missing/active units, partial cgroup visibility, remaining descendants, unsupported kill mode, conflicting existing canonical unit, foreign drop-ins, candidate environment equal to old environment, unverifiable wheel, and worker outside owned containment all refuse before migration.
- [ ] Use transient systemd verification jobs with the same User, HOME, working directory, explicit non-secret environment and EnvironmentFile as the eventual service. Before migration select compatible source paths; after migration select canonical paths. Do not pass secret values on argv or trust the interactive caller's identity/environment.
- [ ] Validate the staged unit with the target host's `systemd-analyze verify --man=no --recursive-errors=no` before stopping anything; unavailable/unsupported parser verification blocks activation. Keep its temporary input private and never start the unit during verification. Execute candidate `codereeve verify --installed`, provenance and `codereeve doctor --strict --phase installation --phase configuration --format json` before stopping old. Require expected schema, phases and a complete required-check set; missing/skipped critical evidence is failure even with exit 0. Live gate similarly selects `--phase live`. Derive check requirements from the installed report/catalog contract, not invented success text.

```python
def test_zero_exit_does_not_authorize_missing_checks(backend, spec):
    backend.runner.doctor_json = {'selected_phases': [], 'checks': []}
    with pytest.raises(CutoverError):
        backend.preflight(spec)
    assert backend.runner.stops == []
```

- [ ] Fresh heartbeat validation rejects wrong PID/invocation, missing fields, stale timestamp, future timestamp beyond tolerance, process restart during check, and deadline expiry. Re-read unit identity after evidence is read. No raw subprocess output enters exceptions.
- [ ] Verify effective restart guard after applying it, not merely command exit. Unit snapshots must support absent/disabled/enabled/masked/active prior states; unsupported state is refusal. Root process/cgroup inspection must be complete and read-only outside controlled units. Commit after focused tests/static checks.

### Task 3: Durable cutover journal and filesystem snapshots

**Files:** Create `journal.py` and, when needed for focused filesystem responsibilities, `storage.py`, with `test_journal.py`/`test_storage.py`; fill immutable snapshot declarations in model and backend integration (#394 durable recovery scope).

**Interfaces:** `CutoverJournal.create(root, spec, snapshot, *, storage)`, `open(path, *, storage)`, `record(event, metadata)`, `capture_publication(paths)`, `restore_publication()`. `ServiceSnapshot` holds validated unit metadata and private backup references; raw environment/unit/secret bytes never appear in its serialized public metadata. `Storage` defaults to real exclusive creation, fsync, no-follow inspection and same-filesystem rename; test seam permits injected durability only.

- [ ] RED: corrupt/truncated checksum chain, illegal transition, unsafe/symlink parent, backup collision, wrong permissions, changed backup/source, missing fsync, unknown manifest, interruption before/after each write/move. No recovery may infer completion from a final-looking filename.

```python
def test_corrupt_journal_cannot_authorize_restart(journal):
    journal.path.write_bytes(journal.path.read_bytes()[:-1])
    with pytest.raises(CutoverError):
        CutoverJournal.open(journal.path)
```

- [ ] Store journals under `<project>/.codereeve-cutover/<transaction-id>/`, directory 0700/files 0600. Snapshot original unit and owned drop-ins, state/enablement, environment selection metadata and necessary secret files before any service mutation. Retain backups through 0.3.x. Runtime PID/invocation identifiers are safe metadata; arbitrary command/environment content is not.
- [ ] Capture verified post-migration publication before activation. On recovery, quarantine changed runtime output on the same filesystem, restore snapshot exclusively, verify exact bytes/types/modes, then permit #393 restore. Journal each intent and completion. Restoration failures retain both copies and report incomplete.
- [ ] Snapshot and restoration must cover absence as well as existence, including POSIX uid/gid as well as modes. Necessary ownership operations use a testable storage seam and never follow symlinks. Refuse external drift of unit/config/secrets selection; do not overwrite unrelated operator edits. Commit after focused interruption/static checks.

### Task 4: Lease handoff and startup heartbeat readiness

**Files:** Modify migration transaction, `chain/heartbeat.py`, `chain/daemon/poll.py`; create private gate helper within service_cutover; tests `test_migration_transaction.py`, `tests/chain/test_heartbeat.py`, `tests/service_cutover/test_readiness.py` and affected daemon tests.

**Interfaces:** `apply_migration(context, *, operations=..., lease: WriterLease | None=None)` and `restore_migration(manifest_path, *, operations=..., lease: WriterLease | None=None)` use `WriterLease.hold` and do not release borrowed leases. Private gate helper validates a transaction-bound absolute path; daemon writes heartbeat identity sidecar and waits interruptibly before issue polling until a verified durable committed gate exists.

- [ ] RED: held borrowed lease survives apply/restore, closed/wrong-path lease rejected, legacy no-argument API unchanged, default quiescence still refuses. Gate tests prove heartbeat runs while no poll/network issue work executes; cancellation leaves lease held until heartbeat shutdown acknowledgement.

```python
def test_borrowed_lease_stays_owned(context, operations, project):
    with WriterLease.acquire(project / '.codereeve-migration.lock', purpose='test') as lease:
        apply_migration(context, operations=operations, lease=lease)
        with pytest.raises(LeaseError):
            WriterLease.acquire(lease.path, purpose='second writer')
```

- [ ] Keep timestamp heartbeat unchanged, add atomic JSON sidecar with schema/PID/invocation/timestamp. Generate process identity from runtime/systemd, never a claimed identity from the journal. Register any new product environment control explicitly with exact comparison; the gate only restricts startup and offers no safety bypass.
- [ ] Gate open requires a root-controlled, service-readable committed receipt bound to this transaction and service invocation; empty/touched files cannot release work. Keep that receipt separate from 0700 recovery backups, under /run/codereeve/cutover/<transaction-id>/. After durable success, publish/reload the permanent ungated unit before releasing the current process receipt. Later ordinary restarts use the ungated unit; a crash before finalization leaves the gated process paused for recovery. Normal ungated launches unchanged. Commit after daemon/heartbeat/transaction focused tests and static checks.

### Task 5: Full forward/recovery coordinator

**Files:** Create `coordinator.py` for forward/public API, `selection.py` for original/effective selection and publication/attestation, `recovery.py` for rollback/finalization, `test_coordinator.py`, `test_recovery.py`; integrate backend, journal and migration APIs (#394 full transaction scope).

**Interfaces:** `cutover(spec, *, backend, storage) -> CutoverResult`; `recover(journal_path, *, backend, storage) -> CutoverResult`; `render_only(spec) -> str`; `install_only(spec, *, backend, storage) -> CutoverResult`. Production defaults use real backend/storage; no public test bypass. Dedicated `.codereeve-cutover.lock` serializes the complete operation independently of daemon lifetime lease.

- [ ] RED full fake-backend traces for successful upgrade, fresh install, already-current verified installation, prior inactive/disabled/masked states, partial canonical unit, idempotent rerun, missing candidate, and every failed boundary. Assert the maximum count of active repository daemons remains <=1 at every event.

```python
def test_failed_health_restores_before_old_start(spec, backend, storage):
    backend.fail_at = 'health'
    result = cutover(spec, backend=backend, storage=storage)
    assert result.recovery == 'complete'
    assert backend.events.index('filesystem_restored') < backend.events.index('old_started')
    assert max(backend.active_counts) <= 1
```

- [ ] Sequence: pure validation; hold cutover lock; snapshot original unit/activation metadata and create a durable preflight journal; journal exact bounded verification-job intents; verify separate installed candidate/pre-stop doctor; stop old and verify descendants; guard old from restart; acquire project lease; fresh authoritative quiescence callback; invoke migration only when actions exist; durably link its journal before its first data mutation; journal and assign managed/host publication ownership for the selected service identity, preserving modes and original backups; keep system secrets root-owned; snapshot publication including uid/gid; install gated canonical unit; release project lease; start new; strict live checks/fresh heartbeat; enable canonical and disable old with journaled intent/completion; durably commit only after both states are verified; publish/reload the permanent ungated unit; release the invocation-bound work receipt; record complete. After durable commit, interruption recovery finalizes forward after revalidation instead of silently replaying pre-commit rollback.
- [ ] Service-only/fresh canonical paths skip data migration only on freshly verified no-action inventory, never by catching a migration error. The coordinator runs privileged, and #393 staged copying preserves mode but not source uid/gid (src/codereeve/migration/transaction.py:430). Verify the actual selected account can read config and write state before activation; ownership failure triggers ordinary rollback, with original sources retaining original ownership. New secrets created for a fresh installation are owned by the cutover snapshot and restored/removed on rollback; legacy secrets conversion stays with #393.
- [ ] Recovery first stops/guards new, proves cgroup and all-writer shutdown, reacquires project lease, preserves post-start output/restores publication, invokes #393 restoration, restores original unit/secrets/environment selection, then prior activation. Restore original inactive state without starting. INCOMPLETE/ambiguous/corrupt recovery never restarts old or new. A recorded commit is revalidated before treating rerun as success.
- [ ] Cutover-to-migration linkage closes interruption windows: override the storage create_journal callback to durably record its manifest before apply can mutate; unknown extra/incomplete migration transactions block automatic restart. Journal pending service effects are resolved by fresh observations, not assumed done/undone.
- [ ] Test process interruption at every persisted prefix, state drift, backup failure, restart guard failure, current-service PID replacement, lease contention, and no lost original environment. Commit after fault matrix and static checks.

### Task 6: Canonical installer, documentation and release verification

**Files:** Create `service_cutover/cli.py`, replace legacy installer implementation with narrow launcher, update existing installer tests and add `test_cli.py`; README, `docs/system-setup.md`, `docs/smoke-test-daemon.md`, `docs/codereeve-service-cutover.md`; exact contract allowlists if needed.

**Interfaces:** Preserve existing installer flags; add explicit `--environment PATH` for separate verified candidate (default `<harness>/.venv-codereeve`) and `--recover JOURNAL` for recovery. Internal module entry point via candidate Python supports same arguments. `--print-unit` pure; `--no-start` only reversible installation without enable/start; ordinary installer invokes coordinator. No new top-level codereeve command.

- [ ] RED real launcher tests with disposable candidate executable and injected systemctl: render causes zero privileged effects or secret output; no-start never starts/enables; missing candidate fails instead of modifying old environment; aliases conflict before effects; install/recover propagate nonzero/incomplete status; secrets never appear under shell tracing. Preserve user/project overrides and necessary optional BWS/file-provider behavior through shared literal config resolution.
- [ ] Thin shell resolves only bootstrap aliases before candidate discovery, disables tracing before secrets can be handled, and execs the candidate module. Python owns resolution, safe prompts, secret-file writes and all service operations. Never source host/config/secrets files as shell code.
- [ ] Docs state prerequisite separate wheel installation, exact render/install/recover commands, Linux/cgroup requirements, explicit doctor phases, retained backups, incomplete recovery, staged #400 dependency, and how to perform disposable Linux systemd acceptance. Do not claim live activation tested on this Windows host.
- [ ] Run fast suite, all service-cutover tests, affected installer/daemon/heartbeat/migration tests, Ruff/mypy, LF format and ShellCheck. Run final constrained wheel/sdist build and Python3.10/3.13 installed foundation; audit every named artifact with git ls-tree HEAD. Package the new module/launcher/docs; exclude journals and backups.
- [ ] Commit, run whole-branch independent review, address findings, reverify. Push/open #394 child PR into #396 integration branch with needs-review/milestone14 and Closes #394, explicitly dependent on PR #400. Do not merge or activate real services.

## Plan self-review and verification boundaries

Tasks 1/2/3 share frozen model declarations; Task 1 defines core request/result, Task 2 owns UnitState, Task 3 owns ServiceSnapshot without redefining earlier types. Task 4 preserves generic migration defaults and heartbeat format; Task 5 consumes the optional borrowed lease and gates, not private transaction internals. Task 6 calls the coordinator and inherits its safety boundaries. Task 5 owns every service/filesystem transition, including pre-migration and post-start windows; rendering never bypasses it to activate a service.

Existing Linux integration claims require real disposable systemd evidence. Windows validation covers deterministic process/command adapter tests, real filesystem transactions with explicit injected durability, fake service state, and Bash launcher tests. A missing live Linux host is a reported validation limitation, never a reason to simulate success under the production adapter (#394; #393 platform ruling).

Parser preflight source: https://raw.githubusercontent.com/systemd/systemd/main/man/systemd-analyze.xml (fetched 2026-09-08). Omit --generators so generators remain disabled. --recursive-errors=no makes warnings in the supplied unit fail verification; unsupported options fail closed before stopping services.

Supported activation identity: non-root dedicated accounts only, with no same-UID user processes outside controlled service cgroups and specifically owned bounded verification jobs. Reject shared login accounts, including installer ancestors under the service UID. This conservative design restriction enforces #394 all-worker containment; it may require operator account remediation. Process inventory rejects unsupported ownership and does not replace full cgroup shutdown/restart guards/cooperative lease authority.

Ruling: Journal an empty regular old-unit file as the restart guard, then daemon-reload and require LoadState=masked before migration. Why: migration inventory rejects symlink service paths (src/codereeve/migration/inventory.py:67-99,142-160), while systemd treats zero-byte regular unit files as masks (https://raw.githubusercontent.com/systemd/systemd/main/man/systemd.unit.xml:247-252, fetched 2026-09-08). Preserve original bytes/type/ownership and prior /dev/null mask exactly in private snapshots; replace known prior mask symlinks only through journaled guard publication before fresh inventory. Cost if wrong: an additional recoverable unit-file mutation; unavailable effective masking blocks data migration. Keep old masked after success and remove enablement links, retaining its original unit backup.

Ownership integration: WriterLease creates .codereeve-migration.lock mode0600 as caller (src/codereeve/migration/lease.py:36-45). Root coordinator must journal and assign the retained lock inode to the selected service UID/GID before release, otherwise fresh startup cannot acquire it. Never unlink/replace the lock inode. Recovery restores original owner/mode in place before restarting old identity; if lock was newly created, retain the inode and give the prior service identity access when restarting it. Cutover lock itself remains root-owned. Test fresh install and differing old/new identities as well as unchanged inode identity across ownership handoff.

Ruling: A pre-existing masked old deployment requires validated prior snapshot metadata to prove its former UID/environment before automatic cutover/recovery. Do not infer these from the candidate account or unit name. Why: effective masked units do not expose original User/ExecStart, leaving #394 worker/environment verification unknown. Observation and exact mask restoration remain supported; our own journal supplies prior metadata for recovery. Cost if wrong: an opaque manually masked deployment needs operator remediation before first automatic cutover.

Preflight interruption rule (#394): persist exact transient verification-job intent before invocation; abort after failed preflight only when job cleanup is proved. The old service remains running if no old-stop intent occurred. Runtime-state publication snapshots still wait for verified writer shutdown.

## Task review integration refinements

- Recovery retries must re-establish file and namespace durability before completing an interrupted quarantine or restoration, even when visible bytes already match. Preserve pending status on a retry flush failure (#394 durable recovery requirement; Task3 review of commit3db2193).
- Add a closed-schema no-action migration event from guarded to migrated, recording a validated inventory digest and exact zero action count without inventing a migration manifest. The coordinator supplies fresh inventory and shutdown/lease proof (#394; Task5 service-only path).
- Journal verification jobs in post-migration/pre-start, activated health, and committed recovery phases as well as initial preflight. Outstanding jobs must remain cleanup obligations before successful commit or terminal completion (#394 bounded health checks and interruption recovery).
- Treat original virtualenv entrypoint/pyvenv snapshots as read-only attestation. Never include old environment paths in selection-restoration writes; external drift blocks original restart (#394 preservation of the original environment).

- Preserve the selected service account HOME/.local/bin in the shared daemon/probe PATH definition; the current installer supplies that path for BWS (`bin/install-daemon-service.sh:L241`, `bin/install-daemon-service.sh:L393`; #213).
- Keep coordinator-owned unit publication outside any earlier immutable runtime-publication snapshot. Validate recorded unit/guard selection before runtime quarantine, then restore original units after generic migration restoration. Generic inventory checks units but only non-service legacy resources become migration actions (`src/codereeve/migration/inventory.py:L159-L184`; #394).
- Fresh prompted/environment secrets require a private, journal-owned preflight selection and a typed ephemeral handoff from the installer; canonical publication remains a transactional effect. No plaintext secret values enter serialized ServiceSpec or journal metadata (#394 strict pre-stop checks and reversible secret publication).

- Task5 exposes a public backend logical-to-target path mapping for real filesystem operations, retaining Linux ServiceSpec/service-command paths. Portable tests map those operations and gate paths into disposable roots without implementing separate transaction policy (#394 injected-failure validation; Task2 filesystem_root and Task3 create(root,spec) contracts).
- FreshSecrets is a frozen, repr-hidden, bounded and literal-validated bytes input to cutover/install_only. A typed stage_secrets path/digest effect precedes private preflight creation; the installer resolves inputs and performs no independent publication (#394 secret-safe transactional verification).
