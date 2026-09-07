# CodeReeve Rename Design

**Status:** Draft for written review
**Epic:** #390
**Milestone:** CodeReeve rename (#14)
**Target releases:** 0.2.0, 0.3.0, and 0.4.0

## Purpose

Rename the product from **baton-harness** to **CodeReeve** before 1.0. The
rename establishes an independent identity for the autonomous development
orchestrator while retaining temporary compatibility for existing operators.
The product name is `CodeReeve`; technical identifiers use the closed form
`codereeve`. The release and compatibility commitments are tracked by #390.

This is a complete product rename, not a change to the provenance of the
vendored orchestration engine. The repository currently treats `symphony` as
owned vendored code under the application package, and that component name and
its provenance remain intact (`docs/harness-design.md:L3-L5`,
`docs/harness-design.md:L31-L35`).

## Current state

The current distribution is named `baton-harness`, its Python source package is
`baton_harness`, and it publishes six `bh-*` console scripts
(`pyproject.toml:L10-L10`, `pyproject.toml:L47-L60`). Operator configuration is
split between `~/.config/baton-harness/host.env` and the managed repository's
`.bh/config.env` (`bin/lib/load-config.sh:L135-L163`). CodeReeve-owned runtime
files currently use `.baton-harness`, including the heartbeat, run log,
dispatch counters, failure counters, and liveness marker
(`src/baton_harness/chain/obs_config.py:L16-L60`,
`src/baton_harness/chain/reconcile.py:L19-L19`).

The service installer currently emits `bh-daemon.service`, defaults secrets to
`/etc/bh-daemon/secrets.env`, and starts the `bh-daemon` executable
(`bin/install-daemon-service.sh:L2-L6`,
`bin/install-daemon-service.sh:L323-L399`). Build provenance is also coupled to
the old distribution and package paths (`hatch_build.py:L20-L20`,
`hatch_build.py:L69-L117`, `src/baton_harness/provenance.py:L105-L138`). These
surfaces must move together so a built artifact cannot report one identity
while executing another.

## Goals

- Make CodeReeve the only canonical identity in 0.2.0.
- Keep documented Baton-era commands, environment variables, imports, and
  paths working as temporary compatibility surfaces through 0.3.x.
- Give operators a dry-run-first, reversible migration that never silently
  chooses between conflicting configuration.
- Escalate compatibility warnings in 0.3.0 and remove the legacy surfaces in
  0.4.0.
- Allow 1.0-gate implementation to resume after the 0.2.0 cutover while keeping
  the 1.0 release blocked until the rename epic is complete (#390).

## Non-goals

- Renaming the vendored `symphony` component or rewriting its provenance.
- Changing scheduling, orchestration, merge, or authorization behavior except
  where necessary to preserve it through the identity migration.
- Adding model providers or execution backends.
- Supporting undocumented internal Python module paths indefinitely.
- Keeping Baton-era compatibility in 1.0.

## Canonical identity contract

| Surface | Canonical identity from 0.2.0 |
|---|---|
| Product and documentation name | `CodeReeve` |
| GitHub repository | `codereeve` |
| Python distribution | `codereeve` |
| Python package | `codereeve` |
| Top-level executable | `codereeve` |
| Product environment prefix | `CODEREEVE_` |
| Managed-repository config | `<project>/.codereeve/config.env` |
| Managed-repository runtime state | `<project>/.codereeve/` |
| Per-host config | `~/.config/codereeve/host.env` |
| System secrets | `/etc/codereeve/secrets.env` |
| systemd unit | `codereeve.service` |
| Vendored engine | `codereeve.vendor.symphony` |

The repository rename is the last external cutover step, after the canonical
wheel, CLI, migration, and service installation have passed verification
(#390). Until then, documentation may need to show the old GitHub slug only
where it is literally required to fetch the repository.

Third-party names are not product branding and remain unchanged. Examples
include `BWS_ACCESS_TOKEN`, `BWS_*_SECRET_ID`, `GH_TOKEN`, and
`ANTHROPIC_API_KEY`. The current config parser already distinguishes these
provider-specific values from `BH_*` application identity
(`src/baton_harness/chain/sandbox_config.py:L65-L92`).

## Unified CLI

0.2.0 introduces one canonical executable with subcommands:

| Existing interface | Canonical interface |
|---|---|
| `bh-daemon [options]` | `codereeve daemon [options]` |
| `bh-daemon --doctor ...` | `codereeve doctor ...` |
| `bh-daemon --version` | `codereeve --version` |
| `bh-daemon --provenance` | `codereeve provenance` |
| `bh-after-create` | `codereeve hook after-create` |
| `bh-before-run` | `codereeve hook before-run` |
| `bh-after-run` | `codereeve hook after-run` |
| `bh-force-pr-not-merge` | `codereeve hook force-pr-not-merge` |
| `bh-verify-foundation` | `codereeve verify` |

The current daemon combines runtime, doctor, version, and provenance modes in
one argument parser (`src/baton_harness/chain/cli.py:L263-L350`). The canonical
CLI separates those modes at the command boundary but delegates to the same
application functions. Legacy executables are wrappers around canonical
handlers; they do not retain separate implementations.

Future 1.0-gate work must extend the unified command tree rather than add new
top-level executables. In particular, #378-#381 should land as CodeReeve
subcommands after 0.2.0.

## Python package compatibility

Application source moves to `src/codereeve/`. The vendored tree moves with it
to `src/codereeve/vendor/symphony/`; files inside the vendored component retain
their Symphony naming and provenance.

The 0.2 and 0.3 wheels also contain a small `baton_harness` compatibility
package. Its supported contract is:

- `import baton_harness` continues to expose the package version and documented
  top-level public names;
- the six legacy console scripts continue to work but dispatch into
  `codereeve`;
- legacy entry-point module imports needed by those scripts remain available;
- all new development, internal imports, tests, patches, and documentation use
  `codereeve` only.

The compatibility package must not contain copied business logic. Every shim
is tested to resolve to the canonical implementation. Undocumented deep imports
are migrated within this repository but are not added to the public
compatibility guarantee. The complete legacy package is removed in 0.4.0.

## Environment compatibility and conflict handling

Each product-owned `BH_*` variable receives a `CODEREEVE_*` equivalent. The
special `BATON_HARNESS_DIR` variable becomes `CODEREEVE_ROOT`. Examples include
`BH_PROJECT_ROOT` -> `CODEREEVE_PROJECT_ROOT`, `BH_VENV` ->
`CODEREEVE_VENV`, and `BH_FEATURE_BRANCH` -> `CODEREEVE_FEATURE_BRANCH`. The
existing code reads these names across registry, observability, configuration,
and hook execution (`src/baton_harness/chain/registry.py:L66-L73`,
`src/baton_harness/chain/obs_config.py:L175-L204`,
`config/WORKFLOW.md:L14-L16`).

Resolution follows one shared rule in 0.2 and 0.3:

1. If only the canonical name is present, use it.
2. If only the legacy name is present, use it and emit the release-appropriate
   deprecation notice.
3. If both are present with the same normalized value, use the canonical name
   and emit one notice.
4. If both are present with different normalized values, fail closed before
   any network call, filesystem mutation, secret fetch, or daemon launch.

Conflict diagnostics name the keys and their sources but never print either
value. One resolver implements this policy for Python and shell-facing callers;
shell launchers should call the resolver rather than reimplement precedence.
This preserves the current "operator environment wins" behavior only when the
legacy and canonical sources do not conflict
(`bin/lib/load-config.sh:L21-L30`).

## Filesystem migration

`codereeve migrate` is the only supported automated migration path.

### `codereeve migrate --check`

The check is read-only. It inventories the legacy and canonical host config,
managed-repository config, runtime state, system secrets, and service unit. It
also checks renamed environment pairs without exposing values. It returns:

- exit 0 and status `current` when no migration is needed;
- exit 2 and status `ready` when a migration is needed and every move is safe;
- exit 1 and status `blocked` for coexistence, conflicting variables,
  permissions, unsupported file types, or another unsafe condition.

Text is the human default; `--format json` provides a stable automation shape.

### `codereeve migrate --apply`

Apply first runs the same checks and makes no changes unless the result is
`ready`. For each source it:

1. creates a UTC-timestamped sibling backup without following symlinks;
2. records source, destination, metadata, and hashes in a migration manifest;
3. performs an atomic same-filesystem rename where supported;
4. fsyncs files and parent directories where the platform exposes that
   capability;
5. verifies the canonical location before proceeding to the next step.

If both a legacy and canonical directory exist, migration stops. It never
merges them. A partial failure automatically restores completed moves in
reverse order. The manifest prints exact manual restoration instructions and
is retained with the timestamped backups. No backup is deleted automatically
in 0.2 or 0.3.

The managed-repository moves are:

- `.bh/config.env` -> `.codereeve/config.env`;
- `.baton-harness/*` -> `.codereeve/*`.

The host move is `~/.config/baton-harness/host.env` ->
`~/.config/codereeve/host.env`. The system move is
`/etc/bh-daemon/secrets.env` -> `/etc/codereeve/secrets.env`. The vendored
engine's `.symphony/` directory is not renamed by this epic because it remains
Symphony-owned state; CodeReeve-owned state is the content currently rooted at
`.baton-harness` (`docs/smoke-test-daemon.md:L11-L11`,
`src/baton_harness/chain/obs_config.py:L16-L60`).

## Service cutover

The service installer renders `codereeve.service` against the canonical
executable and environment paths. Cutover is transactional at the operator
level:

1. verify the canonical wheel and run `codereeve doctor` while the old service
   remains available;
2. render the new unit without enabling it;
3. stop `bh-daemon.service`, start `codereeve.service`, and verify startup,
   heartbeat, and live doctor checks;
4. on success, disable the old unit; on failure, stop the new unit and restart
   the old one;
5. retain the old unit and secrets backup through the compatibility window.

The procedure must never run both daemons concurrently against one managed
repository. The current installer both writes and immediately enables the old
unit, so rendering and activation need separable operations before this
cutover is safe (`bin/install-daemon-service.sh:L504-L521`).

## Release sequence

### 0.2.0 — canonical cutover

- Ship the `codereeve` distribution, package, unified CLI, variables, paths,
  migration command, service unit, documentation, and tests.
- Retain legacy wrappers and aliases with one concise deprecation notice per
  process.
- Verify fresh install, legacy upgrade, conflict rejection, rollback, wheel
  provenance, and service cutover.
- Rename the GitHub repository only after the new installation works.
- Update open 1.0-gate issues so their acceptance criteria name canonical
  interfaces.

Once 0.2.0 is released, work on #358, #378-#383, #243, and #168 may proceed on
the canonical identity. This ordering prevents those identity-sensitive items
from creating new Baton-era interfaces (#390).

### 0.3.0 — warning escalation

- Keep the same compatibility behavior.
- Make deprecation notices prominent in interactive output, migration checks,
  service status guidance, and release notes.
- Ensure every notice names the 0.4.0 removal and its canonical replacement.

### 0.4.0 — legacy removal

- Remove the `baton_harness` compatibility package, `bh-*` scripts, `BH_*`
  aliases, `BATON_HARNESS_DIR`, legacy config/state lookup, and the old service
  installer paths.
- Fail with a focused migration message when a legacy filesystem location is
  detected rather than silently ignoring it.
- Remove compatibility-only tests and replace them with negative tests proving
  the old interfaces are gone.

The 1.0 release remains blocked until 0.4.0 and the rest of #390 are complete,
but 1.0-gate implementation does not need to remain idle after 0.2.0.

## Work decomposition

The implementation plan should create child issues under #390 for these
bounded work streams:

1. canonical package, build provenance, and unified CLI;
2. shared environment resolver and config/state migration;
3. service installer and transactional cutover;
4. repository-wide source, tests, hooks, templates, and documentation identity;
5. 0.2.0 compatibility verification, release, and repository rename;
6. 0.3.0 warning escalation;
7. 0.4.0 legacy removal.

The 0.2 work uses a primary feature branch with child branches, as required by
#390. Later release work branches from the then-current `main` so unrelated
1.0-gate work is not forced to wait behind the whole epic.

## Verification contract

0.2.0 is not ready until automated tests cover:

- fresh canonical installation with no legacy artifacts;
- upgrade from each documented legacy config, state, command, and service
  surface;
- identical and conflicting legacy/canonical environment pairs;
- legacy/canonical directory coexistence;
- migration check, apply, injected partial failure, automatic restoration, and
  manual restoration instructions;
- canonical and legacy CLI dispatch to the same implementation;
- wheel metadata, resources, entry points, and immutable provenance under the
  CodeReeve identity;
- service success and rollback paths without concurrent daemons;
- absence of secret values from errors, reports, manifests, and logs;
- the retained Symphony import, resources, state behavior, and provenance.

The existing foundation verifier enumerates installed entry points and performs
installed CLI smoke tests (`src/baton_harness/verify_foundation.py:L37-L37`,
`src/baton_harness/verify_foundation.py:L416-L481`). That verifier must become a
canonical CodeReeve consumer and test the temporary legacy wrappers separately.

## Risks and controls

- **Split identity in built artifacts:** update distribution metadata, package
  resources, build hook paths, and provenance validation in one child issue;
  verify from built wheel and sdist, not only the source tree.
- **Ambiguous configuration:** centralize pair resolution and reject divergent
  values before side effects.
- **State loss:** dry-run first, refuse coexistence, preserve timestamped
  backups, record a manifest, and roll back partial application.
- **Duplicate autonomous workers:** make service rendering separate from
  activation and explicitly verify only one unit is active.
- **Compatibility logic becoming permanent:** tie each shim to the 0.4.0 child
  issue and make legacy use measurable through warnings and tests.
- **New Baton-era interfaces landing during the transition:** do not resume
  identity-sensitive 1.0 work until the 0.2.0 cutover is released (#390).

## Approval questions

The design is closed form except for written-review corrections. Approval of
this document authorizes conversion into an implementation plan and creation of
the seven child issues; it does not authorize implementation of those child
issues without their normal issue-level confirmation.
