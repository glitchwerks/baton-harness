# CodeReeve Rename Design

**Status:** Approved
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
(`b40e499:src/baton_harness/chain/obs_config.py:L16-L60`,
`b40e499:src/baton_harness/chain/reconcile.py:L19-L19`).

The service installer currently emits `bh-daemon.service`, defaults secrets to
`/etc/bh-daemon/secrets.env`, and starts the `bh-daemon` executable
(`bin/install-daemon-service.sh:L2-L6`,
`bin/install-daemon-service.sh:L323-L399`). Build provenance is also coupled to
the old distribution and package paths (`hatch_build.py:L20-L20`,
`hatch_build.py:L69-L117`, `b40e499:src/baton_harness/provenance.py:L105-L138`). These
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
(`b40e499:src/baton_harness/chain/sandbox_config.py:L65-L92`).

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
one argument parser (`b40e499:src/baton_harness/chain/cli.py:L263-L350`). The canonical
CLI separates those modes at the command boundary but delegates to the same
application functions. Legacy executables are wrappers around canonical
handlers; they do not retain separate implementations.

Future 1.0-gate work must extend the unified command tree rather than add new
top-level executables. In particular, #378-#381 should land as CodeReeve
subcommands after 0.2.0.

## Installation environment ownership

Install and verify CodeReeve in a separate environment while retaining the
Baton-era environment unchanged for rollback. Reject overlapping distribution
ownership of legacy package files or console scripts in one environment;
compatibility shims belong to the CodeReeve distribution in its new environment.
The service coordinator switches executable/environment selection only during
cutover (#392, #394, #396).

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

Each product-owned `BH_*` variable receives a `CODEREEVE_*` equivalent.
`BATON_HARNESS_DIR` becomes `CODEREEVE_ROOT`; third-party variables remain
unchanged (#393).

Python and shell callers share one non-executing parser for literal
assignments, optional `export`, quotes, comments, and blank lines. Reject
executable shell expressions with source/line guidance before evaluating
anything; never print the rejected value. Shell callers must not source an
environment file before invoking this parser (#393, #395).

Resolve precedence independently for each spelling: operator environment,
managed config, then host config. Unset means absent; empty means explicitly
set. Compare the resulting old/new pair exactly; no key has a normalization
rule in 0.2.0. Canonical-only uses canonical, legacy-only uses legacy, and an
identical pair uses canonical with one compatibility record. A divergent pair
fails before network calls, filesystem mutation, secret fetch, or daemon
launch. Diagnostics identify keys and sources without values (#393; commits 8a15485 and f7172c7).

## Filesystem migration

`codereeve migrate` provides the automated migration path (#393).

### `codereeve migrate --check`

Check is read-only. Inventory environment pairs, host/managed config, runtime
state, secrets, service units, unsafe file types, target collisions, and
incomplete recovery evidence. Return `current`/0, `ready`/2, or `blocked`/1;
text and schema-versioned JSON report the same result without secret values
(#393, #396).

### `codereeve migrate --apply`

Apply requires a ready inventory and verified quiescence of all state writers;
refuse concurrent migration or unverifiable quiescence. #394 owns stopping and
starting services; #393 exports apply/restore primitives and fails closed when
standalone apply cannot establish safety (#393, #394).

Inventory both legacy sources before mutation. Reject canonical destinations
that existed before this transaction and duplicate target names across legacy
inputs. Assemble the combined managed destination in a private staging
directory, verify it, then publish atomically where supported. Distinguish
transaction-created staging paths from pre-existing canonical state; do not
merge into an existing canonical directory (#393).

Convert recognized legacy assignments to canonical keys and rewrite only
recognized product-owned default paths. Preserve custom paths, third-party
variables, original bytes in backups, and confidentiality-preserving
permissions. Validate migrated configuration with compatibility disabled so
0.4 does not strand files that still depend on aliases (#393, #396).

Keep timestamped backups and a durable, secret-safe manifest/journal recording
paths, operation IDs, metadata, hashes, and progress, not file contents or
environment values. Verify each publication and persist recovery evidence at
every mutation boundary. Retain originals/backups until verification succeeds;
retain backup/recovery artifacts through 0.3.x (#393, #394).

Caught failures restore completed operations in reverse. Interrupted runs use
the persisted journal; corruption or external changes block unsafe restoration
and produce manual recovery guidance. An incomplete recovery must never be
reported as success or authorize restarting the old service (#393, #394).

Managed inputs are `.bh/config.env`, `.bh/ruleset-baseline.json`, and
`.baton-harness/` contents, mapped under `.codereeve/`. Host config moves from
`~/.config/baton-harness/host.env` to `~/.config/codereeve/host.env`; system
secrets move from `/etc/bh-daemon/secrets.env` to
`/etc/codereeve/secrets.env`. `.symphony/` remains untouched (#393; commit a427b0b).

## Service cutover

#394 owns one transaction coordinating #392's separate installation environment
and #393's filesystem apply/restore primitives:

1. Verify the canonical wheel in a separate environment; retain the original
   environment unchanged. Run strict installation/configuration checks under
   the intended service identity and effective environment.
2. Render the canonical unit without activation, preserving the original unit
   and service activation state.
3. Stop the old daemon and workers and verify quiescence before state changes.
4. Apply migration, switch the unit/executable selection, and start CodeReeve.
5. Require strict live checks and a fresh heartbeat from the newly started
   process within bounded timeouts. Stale or unverifiable evidence fails.
6. On success disable the old unit and retain its backups through 0.3.x.
7. On failure stop and verify quiescence of CodeReeve, restore state/config/
   secrets and the previous unit/executable/environment selection, then restore
   the previous service activation state. Incomplete restoration blocks restart.

Persist a cutover journal across the service/filesystem boundaries so recovery
also works after process interruption. Never run both daemons against the same
managed repository (#394, #396).

The installer already has `--no-start` and `--print-unit` modes. Build the
transaction coordinator on those separable operations rather than treating
rendering as inseparable from activation (#394). Doctor gates must use
`--strict` with explicit required phases or an equivalent structured fail-closed
gate; ordinary report exit status is insufficient (#392, #394).

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

Once 0.2.0 is released, work on #361, #378-#383, #243, and #168 may proceed on
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

The existing child issues #392-#398 under #390 own these
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
- the retained Symphony import, resources, state behavior, and provenance;
- separate-environment ownership and preservation of the original installation;
- two-source staging, converted configuration validated without compatibility,
  executable-expression rejection, and recovery after process interruption;
- strict service-context checks, worker quiescence, and stale-heartbeat rejection.

#396 provides a durable upgrade fixture for #397/#398 to exercise through
0.2 -> 0.3 -> 0.4. Actual later-release runs belong to those later issues; the
0.2 gate verifies canonical-only configuration readiness (#396).

The existing foundation verifier enumerates installed entry points and performs
installed CLI smoke tests (`b40e499:src/baton_harness/verify_foundation.py:L37-L37`,
`b40e499:src/baton_harness/verify_foundation.py:L416-L481`). That verifier must become a
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

## Execution authority

The approved identity contract is synchronized with the review corrections in
#392-#396. Child issues remain the source of truth for implementation scope and
acceptance criteria; #397 and #398 own the later warning/removal releases.
