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
| Every observed forward and reverse process boundary | `test_actual_process_death_reloads_each_observed_service_boundary`; migration process tests with `exhaustive=True` | Linux-only exhaustive `os._exit(73)` matrix; skipped on Windows |
| Empty service authority, candidate started before commit, durable commit, reverse filesystem restoration | `test_portable_process_death_recovery_smoke` | Four representative real subprocess deaths and separate recovery interpreters; portable model |
| Migration journal creation, publication, final event, reverse journal/final event | Migration process tests with `exhaustive=False` select explicit classes from observed traces and include exhaustion | Bounded portable real-process regression coverage; separate recovery interpreter |
| Truncated, altered-checksum, and unknown service authority | `test_corrupt_cutover_authority_refuses_before_service_actions` | Coordinator refusal with no backend events |
| Stale heartbeat, changed invocation, missing heartbeat, timeout | `test_health_requires_fresh_heartbeat_and_stable_invocation`, `test_heartbeat_is_bound_to_process_and_time`, `test_timed_out_verification_job_requires_proven_cleanup` | Existing portable health/manager-model negatives |
| Critical or forged strict doctor evidence; secret-bearing diagnostics | `test_doctor_refuses_incomplete_or_forged_evidence`, `test_invalid_json_is_value_free`, `test_command_failure_does_not_echo_output` | Existing strict-gate and redaction negatives |
| Unknown writers, environment aliases, filesystem coexistence | `test_outside_worker_refuses_preflight`, `test_external_uid_project_writer_is_rejected`, `test_readonly_blockers_precede_even_lock_creation` | Existing portable refusal tests |
| Receipt identity, interrupted publication, and durability retry | `test_invalid_receipt_never_opens`, `test_interrupted_publication_reestablishes_durability`, `test_interrupted_creation_and_publication_retry` | Existing portable readiness tests |

Before a valid initial service journal exists, recovery must refuse
incomplete authority without service actions. Once journal authority exists,
pre-commit cases require complete rollback. After the durable `committed`
record, recovery must finish forward and cannot restart the old service.
The expected side is captured at interruption, not inferred from a successful
recovery result. Original backups are verified after recovery; every recorded
service-state transition must have at most one active daemon
(`tests/service_cutover/process_support.py`; #396).

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
clean-host gate in Task 4.

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
