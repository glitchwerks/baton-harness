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
- [ ] Execute candidate `codereeve verify --installed`, provenance and `codereeve doctor --strict --phase installation --phase configuration --format json` before stopping old. Require expected schema, phases and a complete required-check set; missing/skipped critical evidence is failure even with exit 0. Live gate similarly selects `--phase live`. Derive check requirements from the installed report/catalog contract, not invented success text.

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

**Files:** Create `journal.py`, `test_journal.py`; fill immutable snapshot declarations in model and backend integration.

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
- [ ] Snapshot and restoration must cover absence as well as existence. Refuse external drift of unit/config/secrets selection; do not overwrite unrelated operator edits. Commit after focused interruption/static checks.

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
- [ ] Gate open requires a complete, validated cutover commit associated with this service invocation; empty/touched files cannot release work. Normal ungated launches unchanged. Commit after daemon/heartbeat/transaction focused tests and static checks.

### Task 5: Full forward/recovery coordinator

**Files:** Create `coordinator.py`, `test_coordinator.py`, `test_recovery.py`; integrate backend, journal and migration APIs.

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

- [ ] Sequence: pure validation; separate installed candidate/pre-stop doctor; journal original state and unit; hold cutover lock; stop old and verify descendants; guard old from restart; acquire project lease; fresh authoritative quiescence callback; invoke migration only when actions exist; durably link its journal before its first data mutation; snapshot publication; install gated canonical unit; release project lease; start new; strict live checks/fresh heartbeat; enable canonical and disable old with journaled intent/completion; durably commit only after both states are verified; release work gate.
- [ ] Service-only/fresh canonical paths skip data migration only on freshly verified no-action inventory, never by catching a migration error. New secrets created for a fresh installation are owned by the cutover snapshot and restored/removed on rollback; legacy secrets conversion stays with #393.
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
