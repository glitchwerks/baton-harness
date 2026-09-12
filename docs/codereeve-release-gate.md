# CodeReeve release-gate upgrade fixture

## Failure and interruption coverage

The #396 failure matrix derives occurrence indices from complete production
observation runs. It records separate fresh, upgraded, and install-only then
upgraded service traces, and a health-failure trace that traverses reverse
recovery for each mode. Repeated events retain distinct indices; each injected
row must fire and match its observed trace prefix. Fixed numeric ceilings are
not coverage evidence (`tests/service_cutover/test_recovery.py`,
`tests/test_migration_transaction.py`; #396).

| Production boundary or refusal | Test/evidence | Classification |
| --- | --- | --- |
| Journal creation and every service record, before and after append | `test_recovery_of_each_durable_forward_prefix` observes `CutoverJournal.record`, including reverse restoration records | Portable caught `CutoverError` and caught `BaseException` interruption; no process death |
| Old stop, manager reload, candidate start, health, enablement, old activation, before and after each call | Same matrix observes production coordinator calls through its injected service backend | Portable service model; original files, executable bytes, and activation links are checked before old activation |
| Migration copy, rewrite, verification, file flush, directory flush, backup, publication, journal creation/append, and final verification | `test_every_observed_boundary_reverses_caught_failure` enumerates every `FileOperations.boundary` occurrence | Real disposable files; directory flush is modeled |
| Reverse migration effects and durable reverse records | `test_every_observed_reverse_boundary_resumes_caught_failure` independently observes production `restore_migration` | Portable caught failure, then fresh restoration and retained backups |
| Internal service directory/exclusive-file creation, buffered write/flush, raw file fsync, modeled metadata/file/directory sync, replacement, rename, unlink and receipt link publication | `test_every_observed_service_storage_effect_recovers`; focused `test_focused_new_storage_windows_recover` | Portable caught failure at actual primitive call sites, including reverse effects and parent-intent/child-creation persistence windows |
| Same internal storage coverage after actual interpreter loss | `test_linux_process_death_at_each_covered_storage_occurrence`; `test_portable_storage_process_death_smoke` | Linux complete storage coverage plan; separate bounded portable death regressions with fresh recovery interpreters |
| Empty, partial, or foreign public bytes left during original-file restoration | `test_interrupted_public_restoration_preserves_untrusted_bytes` | Incomplete recovery, unchanged public bytes/metadata, retained verified backups, and no old activation on repeated recovery |
| Every observed forward and reverse process boundary | `test_actual_process_death_reloads_each_observed_service_boundary`; migration process tests with `exhaustive=True` | Linux-only exhaustive `os._exit(73)` matrix; skipped on Windows |
| Empty service authority, candidate started before commit, durable commit, reverse filesystem restoration | `test_portable_process_death_recovery_smoke` | Four representative real subprocess deaths and separate recovery interpreters; portable model |
| Migration journal creation, publication, final event, reverse journal/final event | Migration process tests with `exhaustive=False` select explicit classes from observed traces and include exhaustion | Bounded portable real-process regression coverage; separate recovery interpreter |
| Truncated, altered-checksum, and unknown service authority | `test_corrupt_cutover_authority_refuses_before_service_actions` | Coordinator refusal with no backend events |
| Stale heartbeat, changed invocation, missing heartbeat, timeout | `test_health_requires_fresh_heartbeat_and_stable_invocation`, `test_heartbeat_is_bound_to_process_and_time`, `test_timed_out_verification_job_requires_proven_cleanup` | Existing portable health/manager-model negatives |
| Critical or forged strict doctor evidence; secret-bearing diagnostics | `test_doctor_refuses_incomplete_or_forged_evidence`, `test_invalid_json_is_value_free`, `test_command_failure_does_not_echo_output` | Existing strict-gate and redaction negatives |
| Unknown writers, environment aliases, filesystem coexistence | `test_outside_worker_refuses_preflight`, `test_external_uid_project_writer_is_rejected`, `test_readonly_blockers_precede_even_lock_creation` | Existing portable refusal tests |
| Receipt identity, interrupted publication, and durability retry | `test_invalid_receipt_never_opens`, `test_interrupted_publication_reestablishes_durability`, `test_interrupted_creation_and_publication_retry` | Existing portable readiness tests |

Before a valid initial service journal exists, recovery must refuse
incomplete authority without service actions. Once journal authority and
restoration inputs are trusted, pre-commit cases require complete rollback.
An interrupted public restore write can leave bytes that do not match the
original. Such untrusted partial or foreign contents require incomplete
recovery and manual resolution; they must remain untouched and cannot
authorize old-service activation. Missing nodes and matching original bytes
with original or known temporary metadata still require complete recovery.
After the durable `committed`
record, recovery must finish forward and cannot restart the old service.
The expected side is captured at interruption, not inferred from a successful
recovery result. Original backups are verified after recovery; every recorded
service-state transition must have at most one active daemon
(`tests/service_cutover/process_support.py`; #396).

The storage supplement exhausts every observed upgrade forward/reverse
occurrence. It also observes fresh, install-only-then-upgrade, and explicit
fresh-secret workflows and injects every occurrence at a site not covered
by the preceding scenarios. Site comparison retains the physical primitive,
originating production function, journal/effect context, target path role,
and before/after side. Only synthetic identities and numeric backup/blob
indices are abstracted. This covers distinct predecessor staging, canonical
unit restoration, migration-not-needed, and fresh-secret storage paths
without repeating the same shared implementation for every fixture entry.
The Linux process supplement uses the same complete coverage plan. The
record/backend matrices separately retain all occurrences in all three
installation modes (`tests/service_cutover/test_storage_recovery.py`; #396).

Storage observation excludes read-only calls and helper-state persistence.
Requested initial POSIX creation modes are modeled at the raw creation
boundary, before the fixture's later metadata callback; new inodes cannot
inherit deleted-node metadata. This is test-model state, not a claim about
native Windows ownership or permissions.
Fault provenance is test-oracle input only; recovery authority still comes
from production journals. The partial-public-write refusal classification
requires the exact injected restore-write site, pending restore-input
authority, independently compared original/public bytes and metadata, and
verified backups. It does not call the production partial-copy classifier
as its oracle (`tests/service_cutover/storage_support.py`; #396).

The migration journal's optional `before_create` callback durably binds its
manifest path to the service transaction before creating child artifacts.
Recovery treats only a safely absent child directory with unchanged original
inputs as an unstarted effect. Existing partial, corrupt, foreign, or linked
artifacts still block completion
(`src/codereeve/migration/journal.py`,
`src/codereeve/service_cutover/coordinator.py`,
`src/codereeve/service_cutover/recovery.py`; #396).

Each reverse-copy attempt uses a fresh private sibling. A failed private
copy is retained rather than reused; the immutable backup and complete new
stage must verify before atomic restoration to the absent original path.
Repeated interruptions therefore retain additional evidence and consume
additional disk until operator cleanup
(`src/codereeve/migration/transaction.py`; #396).

The process helper stores only disposable fake-manager state and modeled POSIX
metadata in `external-state.json`; a new interpreter reloads those values.
It does not serialize an in-memory coordinator or substitute its own
transaction algorithm. Transaction authority is reopened through production
`recover`/`restore_migration` APIs. This proves interpreter-loss recovery on a
live filesystem, not loss of power, directory-fsync durability, real systemd
enablement, or actual cgroup quiescence
(`tests/service_cutover/process_support.py`; #396).

Exhaustive Linux process matrices are an authored gate, not completed Windows
evidence. Their runner/runtime must be measured on Linux before making CI
duration claims. A separately authorized Linux/systemd host must still prove
real service identity, cgroups, fresh heartbeat, activation selection, and
durability. Neither portable test success nor a Linux test using the fake
backend satisfies that host acceptance requirement (#396).

## Upgrade fixture contract

`tests/fixtures/codereeve-upgrade-v1.json` is the durable synthetic input
for the CodeReeve 0.2.0 upgrade gate. It represents the legacy managed
configuration, runtime state, host configuration, system secrets, ruleset
baseline, and an unrelated Symphony sentinel required by #396. Its source
revision is immutable fixture provenance rather than evidence about a live
installation (`tests/fixtures/codereeve-upgrade-v1.json`; #396).

The schema has four required fields:

- `schema_version` selects the fixture contract.
- `legacy_source_revision` records the exact source revision represented.
- `files` contains relative POSIX paths, UTF-8 text, and integer permission
  modes.
- `canonical_assertions` records complete assignment maps expected after
  migration.

Paths are literal relative paths beneath a fresh disposable root. The schema
does not expand placeholders. The materializer validates the complete input
before creating the root and rejects absolute, drive, UNC, parent-traversal,
duplicate, backslash, and symlink-parent paths
(`tests/release_gate/fixture.py`; #396).

`tests/release_gate/test_upgrade_fixture.py` builds the production
`PathLayout` inside the disposable root, then exercises the production
inventory, apply, and restore APIs. It requires exact canonical assignment
maps, no parsed legacy product keys, no reported legacy uses, byte-for-byte
runtime and ruleset content, retained backup bytes, unchanged custom and
third-party values, and an untouched Symphony sentinel
(`src/codereeve/migration/transaction.py:L592-L610`;
`src/codereeve/migration/transaction.py:L930-L947`; #396).

`PortableFixtureOperations` supplies only controlled-test quiescence and
portable storage-boundary behavior. It does not claim a real installed
interpreter, systemd state, service activation, or operating-system durability
(`tests/release_gate/fixture.py`; #396). Later gates in #397 and #398 can load
the JSON with `load_fixture`, seed a fresh root with `materialize_fixture`, and
reuse `PortableFixtureOperations` when their scope remains synthetic. A gate
which asserts actual host properties must gather those properties separately.

This contract does not execute a later release, deploy, merge, rename a
repository, or prove an installed-host cutover. Those remain separate release
actions under #396.

## Installed release proof

`scripts/verify_installed_upgrade.py` accepts explicit old and candidate wheel
paths and their independently exported locked runtime requirements. It creates
separate old, candidate, and overlap-negative environments under a caller-owned
disposable workspace. Every installed probe uses that environment's own
interpreter or generated executable from a working directory outside the
checkout, with fresh home/config directories, ambient credentials removed, and
bytecode writes disabled (`scripts/verify_installed_upgrade.py`; #396).

The durable fixture is materialized before the driver starts. The installed
candidate interpreter imports the production migration transaction, applies the
fixture with `PortableFixtureOperations`, records a deliberate post-apply
failure, and calls production restore. Only after restoration completes does the
driver run the old executable again. Package metadata files and generated old
scripts are hashed before and after this sequence, and the old interpreter must
still import its package from its own prefix
(`tests/release_gate/test_installed_upgrade.py`; #396). This is portable
migration rollback evidence. Actual service selection and activation remain the
[disposable live-systemd acceptance gate](#disposable-live-systemd-acceptance).

The overlap-negative environment deliberately co-installs the `baton-harness`
and `codereeve` distributions and requires `codereeve verify --installed` to
reject ambiguous ownership. The retained old environment is never used for that
experiment (`src/codereeve/verify_foundation.py:L530-L649`; #396).

The old input is a source-built synthetic upgrade fixture, version
`0.1.0+upgradefixture`, from immutable revision
`e25749fc48aff6500744b2b202534f1d56c9b834`. It is not a published release.
Reproduce it in a disposable extraction of `git archive` at that revision:

```bash
uv export --locked --no-emit-project --format requirements.txt --output-file runtime.txt
uv export --locked --extra dev --no-emit-project --format requirements.txt --output-file build.txt
unset CODEREEVE_BUILD_DEVELOPMENT BH_BUILD_DEVELOPMENT
unset CODEREEVE_BUILD_VERSION CODEREEVE_BUILD_SOURCE_REVISION
unset BH_BUILD_VERSION BH_BUILD_SOURCE_REVISION
export BH_BUILD_VERSION=0.1.0+upgradefixture
export BH_BUILD_SOURCE_REVISION=e25749fc48aff6500744b2b202534f1d56c9b834
uv build --build-constraints build.txt --require-hashes --wheel --out-dir dist
```

Build candidate artifacts only from the clean commit being proved. Rebuild a
wheel from the generated sdist with the same assertions, then use the three
archive validators to reconcile version, revision, lock identity, resources,
and entry points (`hatch_build.py:L114-L220`;
`src/codereeve/verify_foundation.py:L651-L839`; #396):

```bash
unset CODEREEVE_BUILD_DEVELOPMENT BH_BUILD_DEVELOPMENT
unset CODEREEVE_BUILD_VERSION CODEREEVE_BUILD_SOURCE_REVISION
unset BH_BUILD_VERSION BH_BUILD_SOURCE_REVISION
export CODEREEVE_BUILD_VERSION=0.2.0
export CODEREEVE_BUILD_SOURCE_REVISION="$(git rev-parse HEAD)"
uv export --locked --no-emit-project --format requirements.txt --output-file candidate-runtime.txt
uv build --wheel --sdist --out-dir candidate-dist
```

Run the integration gate separately for each interpreter. Machine-specific
workspaces and raw evidence stay under `.tmp/`; evidence JSON contains only
artifact hashes and provenance, interpreter identity, stable command names and
statuses, and fixed diagnostics:

```bash
export CODEREEVE_RELEASE_OLD_WHEEL=/absolute/path/to/old.whl
export CODEREEVE_RELEASE_CANDIDATE_WHEEL=/absolute/path/to/codereeve-0.2.0-py3-none-any.whl
export CODEREEVE_RELEASE_OLD_REQUIREMENTS=/absolute/path/to/old-runtime.txt
export CODEREEVE_RELEASE_CANDIDATE_REQUIREMENTS=/absolute/path/to/candidate-runtime.txt
export CODEREEVE_RELEASE_EXPECTED_OLD_REVISION=e25749fc48aff6500744b2b202534f1d56c9b834
export CODEREEVE_RELEASE_EXPECTED_CANDIDATE_REVISION="$(git rev-parse HEAD)"
export CODEREEVE_RELEASE_EXPECTED_OLD_LOCK_IDENTITY="sha256:$(sha256sum /absolute/path/to/old-source/uv.lock | cut -d' ' -f1)"
export CODEREEVE_RELEASE_EXPECTED_CANDIDATE_LOCK_IDENTITY="sha256:$(sha256sum uv.lock | cut -d' ' -f1)"
export CODEREEVE_RELEASE_EXPECTED_OLD_RUNTIME_SHA256="$(sha256sum "$CODEREEVE_RELEASE_OLD_REQUIREMENTS" | cut -d' ' -f1)"
export CODEREEVE_RELEASE_EXPECTED_CANDIDATE_RUNTIME_SHA256="$(sha256sum "$CODEREEVE_RELEASE_CANDIDATE_REQUIREMENTS" | cut -d' ' -f1)"
for version in 3.10 3.13; do
  export CODEREEVE_RELEASE_PYTHON="$version"
  .venv/Scripts/python.exe -m pytest tests/release_gate/test_installed_upgrade.py
done
```

These expected values come from the immutable fixture revision, each source
tree's `uv.lock`, and the independently generated locked runtime exports. The
driver checks all six bindings before it creates an environment or executes an
installed command. Values copied from wheel provenance are not independent
expectations (`scripts/verify_installed_upgrade.py`; #396).

Run the separate frozen-foundation proof from the same commit with
`codereeve verify --python 3.10 --python 3.13 --keep-temp`. Its synthetic
`0.0.0+foundation` identity supports the release evidence but does not replace
the explicit 0.2.0 artifact proof
(`src/codereeve/verify_foundation.py:L876-L1067`; #396).

## Disposable live-systemd acceptance

This section is an execution runbook for #396. It is not evidence that the gate
passed. Execution requires a user-selected disposable Linux VM whose PID 1 is
systemd, whose `/proc` view is complete, and whose `/sys/fs/cgroup` mount is a
cgroup v2 root. The coordinator must run as root, while the service uses a
dedicated non-root account (`src/codereeve/service_cutover/systemd.py:L211-L230`;
`docs/codereeve-service-cutover.md:L67-L76`; #396). Record the selected host,
provisioned disposable repository, credential source, and the provider's
documented snapshot identifier before starting. Keep provider commands and
credential values out of this repository.

### Prepare the candidate and host

Set these paths to the selected host's real values. `HARNESS_DIR` must be the
candidate source tree containing `config/WORKFLOW.md`; the private installer
uses that file and otherwise defaults the harness directory to its current
directory (`src/codereeve/service_cutover/cli.py:L169-L188`). The candidate
environment must be separate from the legacy environment
(`src/codereeve/service_cutover/systemd.py:L787-L802`; #396).

```bash
export HARNESS_DIR=/absolute/path/to/candidate-source
export PROJECT_ROOT=/absolute/path/to/disposable-managed-repository
export CANDIDATE_ENV=/opt/codereeve
export SERVICE_USER=codereeve

test "$(ps -p 1 -o comm=)" = systemd
test "$(id -u)" -eq 0
findmnt --noheadings --output FSTYPE --target /proc
findmnt --noheadings --output FSTYPE --target /sys/fs/cgroup
getent passwd "$SERVICE_USER"
systemctl --version
```

The two `findmnt` results must be `proc` and `cgroup2`. Record only versions,
the candidate provenance document, the non-secret service-account identity,
and the snapshot identifier. Verify separately that the disposable repository
and required credentials are provisioned; do not copy credentials or complete
configuration into public evidence. Finish all login-shell probes before the
coordinator runs because any process with the managed UID outside its controlled
cgroups blocks quiescence (`docs/codereeve-service-cutover.md:L69-L83`; #396).

Create the candidate environment and install the exact candidate wheel. Retain
the wheel, source revision, lock identity, and hashes selected by the installed
release proof above. Installation into a separate environment is required by
the service runbook (`docs/codereeve-service-cutover.md:L3-L17`; #396).

```bash
uv venv "$CANDIDATE_ENV" --python 3.13
uv pip install --python "$CANDIDATE_ENV/bin/python" /absolute/path/to/codereeve-0.2.0-py3-none-any.whl
"$CANDIDATE_ENV/bin/codereeve" provenance
```

### Run fresh and legacy-upgrade scenarios

Restore the clean snapshot between the fresh-install and legacy-upgrade
scenarios. The fresh scenario begins with neither managed unit or managed state
present. The upgrade scenario seeds the supported direct `bh-daemon` console
script, its matching separate environment, the legacy unit and fixture state.
Custom paths and unrelated Symphony data must remain unchanged
(`docs/codereeve-service-cutover.md:L78-L88`; #396).

For each scenario, render first and verify that neither unit's enabled or active
state changed. Then install without activation and require `status: installed`
plus the reported journal path. The command grammar permits only the modes and
selection flags shown here (`src/codereeve/service_cutover/cli.py:L28-L41`;
`src/codereeve/service_cutover/cli.py:L243-L283`).

```bash
"$HARNESS_DIR/bin/install-daemon-service.sh" \
  --harness-dir "$HARNESS_DIR" \
  --environment "$CANDIDATE_ENV" \
  --project-root "$PROJECT_ROOT" \
  --user "$SERVICE_USER" \
  --print-unit

systemctl --system --no-pager is-enabled bh-daemon.service codereeve.service || true
systemctl --system --no-pager is-active bh-daemon.service codereeve.service || true

"$HARNESS_DIR/bin/install-daemon-service.sh" \
  --harness-dir "$HARNESS_DIR" \
  --environment "$CANDIDATE_ENV" \
  --project-root "$PROJECT_ROOT" \
  --user "$SERVICE_USER" \
  --no-start
```

Restore the scenario snapshot after inspecting install-only evidence. Run the
full cutover with the same four explicit selections and no mode flag:

```bash
"$HARNESS_DIR/bin/install-daemon-service.sh" \
  --harness-dir "$HARNESS_DIR" \
  --environment "$CANDIDATE_ENV" \
  --project-root "$PROJECT_ROOT" \
  --user "$SERVICE_USER"
```

Do not run separate operator-shell doctor commands as acceptance evidence. The
coordinator runs installed/provenance checks and strict installation and
configuration phases in bounded transient systemd units under the configured
service user, HOME, project root, candidate PATH, and optional secrets file
(`src/codereeve/service_cutover/systemd.py:L668-L731`;
`src/codereeve/service_cutover/systemd.py:L817-L847`). It then verifies the
loaded unit selection, starts the exact candidate executable, runs the strict
live phase under the same identity, and requires a heartbeat newer than that
stable invocation's start (`src/codereeve/service_cutover/systemd.py:L1001-L1052`;
`src/codereeve/service_cutover/systemd.py:L1054-L1115`). A passing scenario
therefore requires `status: committed`, a retained journal path, only
`codereeve.service` active, the expected executable and service UID, every
service process in its unit cgroup, and the fresh heartbeat bound to the same
PID and invocation. Retain the private journal, unit environment and complete
diagnostics on the disposable host; public evidence records only sanitized
identities, digests, states, and assertions.

### Exercise interruption and recovery

Use the test-only process interruption harness and exact observed boundary
identities implemented for #396. Do not add fault-injection flags to the
production installer. Restore a disposable snapshot before each case, terminate
the coordinator only at the selected test-harness boundary, and recover from
the journal path reported for that transaction. Recovery accepts the durable
journal directly (`src/codereeve/service_cutover/cli.py:L257-L270`; #396):

```bash
"$HARNESS_DIR/bin/install-daemon-service.sh" \
  --recover /absolute/path/from-the-interrupted-transaction/journal.jsonl
```

Exit status alone is insufficient: only `status: installed` or
`status: committed` is successful (`src/codereeve/service_cutover/cli.py:L233-L240`).
Before accepting rollback, prove both services are quiescent during restore,
the legacy unit's original files and activation state are restored, its direct
executable still resolves to the retained legacy environment, and that
executable remains usable. Never activate the old service until those checks
pass (#396; `src/codereeve/service_cutover/recovery.py:L224-L230`).

If interruption leaves an original public file with empty, partial, or unknown
bytes, recovery must preserve it, retain the verified private backup, report an
incomplete result, and leave the old service stopped. The operator must compare
the public file with trusted external evidence and repair or remove it before
retrying; automatic rollback is intentionally refused while the public bytes or
metadata are untrusted (`src/codereeve/service_cutover/recovery.py:L134-L219`;
`src/codereeve/service_cutover/storage.py:L369-L387`; #396).

### Evidence and acceptance record

Create a sanitized record under `docs/verification/` only after the run. It must
name the candidate commit and artifact hashes, host/kernel/systemd versions,
snapshot identifiers, scenario, non-secret service UID and HOME, executable,
unit enabled/active states, invocation identity, cgroup membership assertion,
heartbeat freshness assertion, journal status, recovery classification, and
each gate as passed, failed, skipped, or unexecuted. Do not include credentials,
secret values, complete environment assignments, raw journals, private backups,
or unsanitized command output (`docs/codereeve-service-cutover.md:L101-L103`;
#396). Any failure, skip, incomplete recovery, source revision change, or missing
field leaves the live-systemd gate open.

The authored exhaustive Linux process-death matrices and Windows representative
process checks are separate evidence categories. Neither proves this live-host
gate, which additionally requires real systemd identity, cgroups, activation,
quiescence, and heartbeat evidence (#396;
`tests/service_cutover/test_storage_recovery.py`;
`tests/service_cutover/process_support.py`).

### Exhaustive Linux process matrix

The Linux matrix remains a separate prerequisite and is currently unmet. Run
the three groups serially from the repository root with the exact candidate
revision's `.venv/bin/python`. A fresh checkout must create `.tmp` before using
the nested pytest base-temporary paths (`tests/service_cutover/test_recovery.py`;
`tests/service_cutover/test_storage_recovery.py`;
`tests/test_migration_transaction.py`; #396).

```bash
mkdir -p .tmp

.venv/bin/python -m pytest \
  'tests/service_cutover/test_recovery.py::test_recovery_of_each_durable_forward_prefix[True-forward-upgrade]' \
  'tests/service_cutover/test_recovery.py::test_recovery_of_each_durable_forward_prefix[True-forward-installed]' \
  'tests/service_cutover/test_recovery.py::test_recovery_of_each_durable_forward_prefix[True-reverse-fresh]' \
  'tests/service_cutover/test_recovery.py::test_recovery_of_each_durable_forward_prefix[True-reverse-upgrade]' \
  'tests/service_cutover/test_recovery.py::test_recovery_of_each_durable_forward_prefix[True-reverse-installed]' \
  -x -q --basetemp=.tmp/task3-linux-caught-remainder

.venv/bin/python -m pytest \
  tests/service_cutover/test_recovery.py \
  tests/test_migration_transaction.py \
  tests/test_migration_journal.py \
  -k 'not test_recovery_of_each_durable_forward_prefix' \
  -x -q --basetemp=.tmp/task3-linux-remainder

.venv/bin/python -m pytest \
  tests/service_cutover/test_storage_recovery.py \
  -x -q --basetemp=.tmp/task3-linux-storage
```

Record the exact source revision, interpreter and tool versions, each command,
exit status, pytest summary, and retained base-temporary evidence paths. Any
failed, interrupted, or omitted group leaves this prerequisite unmet. Container
process results remain distinct from the disposable live-systemd acceptance
record (#396).
