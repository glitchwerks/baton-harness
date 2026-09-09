# CodeReeve configuration and state migration

This runbook covers the 0.2 migration from Baton Harness names and paths to
CodeReeve. It handles configuration and state only; service rendering,
activation, and rollback remain with the coordinator tracked by #394. The
underlying migration transaction is tracked by #393
(`docs/superpowers/specs/2026-09-07-codereeve-rename-design.md:L149-L214`).

## Sources and precedence

The migration inventories these path pairs before any write. A canonical and
legacy path in the same row may not coexist, and managed config, baseline, and
state inputs are assembled as one canonical `.codereeve/` publication rather
than merged into an existing directory (#393;
`src/codereeve/migration/inventory.py:L114-L207`).

| Scope | Temporary legacy source | Canonical destination |
|---|---|---|
| Managed config | `<project>/.bh/config.env` | `<project>/.codereeve/config.env` |
| Ruleset baseline | `<project>/.bh/ruleset-baseline.json` | `<project>/.codereeve/ruleset-baseline.json` |
| Runtime state | `<project>/.baton-harness/` | `<project>/.codereeve/` |
| Host config | `~/.config/baton-harness/host.env` | `~/.config/codereeve/host.env` |
| System secrets | `/etc/bh-daemon/secrets.env` | `/etc/codereeve/secrets.env` |
| Service unit, inventory only | `/etc/systemd/system/bh-daemon.service` | `/etc/systemd/system/codereeve.service` |

The path definitions are centralized in
`src/codereeve/paths.py:L59-L135`. `.symphony/` belongs to Symphony and remains
unchanged throughout the migration (#393;
`docs/superpowers/specs/2026-09-07-codereeve-rename-design.md:L203-L214`).

For each variable spelling independently, migration precedence is operator
environment, managed config, host config, then system secrets. An unset name is
absent; an explicitly empty assignment is present and wins over lower sources.
After precedence is resolved for both spellings, a `CODEREEVE_*` and temporary
legacy value must compare exactly. Identical pairs select the canonical name;
different pairs block without printing either value. `BATON_HARNESS_DIR` pairs
with `CODEREEVE_ROOT`. Third-party names such as `BWS_*`, `GH_TOKEN`, and
`ANTHROPIC_API_KEY` are unchanged (#393;
`src/codereeve/config_env.py:L158-L193`,
`src/codereeve/config_env.py:L280-L318`).

## Read-only check

Run a check while preparing the maintenance window:

```bash
codereeve migrate --check
codereeve migrate --check --format json
```

Both formats carry the same ordered actions and findings. JSON uses schema
version 1. The status is also the process exit code shown below (#393;
`src/codereeve/migration/model.py:L148-L213`).

| Status | Exit code | Meaning |
|---|---:|---|
| `current` | 0 | No legacy data or service migration is needed. |
| `ready` | 2 | A local migration plan exists; apply still needs fresh coordinator proof. |
| `blocked` | 1 | A conflict, unsafe path, active writer, or incomplete transaction prevents apply. |

Check mode only inventories files and supplied evidence. It does not acquire a
lease, run a process, query a service, or mutate state (#393;
`src/codereeve/migration/inventory.py:L1-L7`). Treat exit 2 as planned work, not
as an error from the checker.

## Apply and retained evidence

Before apply, stop every daemon and other process that can write the legacy or
canonical state. An authoritative coordinator must verify all writers stopped,
keep them stopped, and retain the project migration lease for the transaction.
An old context snapshot, missing marker, process-name scan, or lock probe is not
quiescence evidence (#394;
`src/codereeve/migration/transaction.py:L82-L108`).

Apply accepts only a ready data migration. Current and blocked states are safe
refusals:

```bash
codereeve migrate --apply
codereeve migrate --apply --format json
```

The transaction inventories again, creates and verifies private staging,
renames each original to a unique backup, publishes verified destinations, and
records each boundary in a durable journal. It never merges into a pre-existing
canonical destination (#393;
`src/codereeve/migration/transaction.py:L588-L743`).

Successful output names `manifest_path`, `journal_path`, every `backup_path`,
and the combined `manual_restoration_path` list. The manifest is checksummed and
contains `transaction_id`, `actions`, and `entries`; `journal.jsonl` records the
ordered transaction events. Keep the manifest, journal, private stages, and all
backups through the 0.3 release line (#393;
`src/codereeve/migration/journal.py:L150-L186`).

## Failure and manual restoration

A caught apply failure attempts automatic restoration in reverse order. Output
distinguishes `complete`, `not_needed`, `incomplete`, and unavailable
restoration evidence. `complete` means this invocation performed rollback and
freshly revalidated the current filesystem. `not_needed` means an
already-restored journal received fresh coordinator authorization and the
filesystem was freshly revalidated without another rollback. Both are
successful restoration evidence, but the enclosing apply still failed and
remains `blocked`/1 rather than becoming an applied migration. `incomplete` or
unavailable evidence must retain every artifact and must never authorize a
daemon restart (#393, #394;
`src/codereeve/migration/transaction.py:L820-L920`).

If automatic restoration is incomplete, keep all writers stopped and use the
exact absolute paths in the manifest rather than reconstructing names:

1. Retain the manifest, `journal.jsonl`, backups, and private stages. Verify the
   manifest checksum and use its `transaction_id` to keep evidence from
   different attempts separate (#393;
   `src/codereeve/migration/journal.py:L457-L530`).
2. Read the durable planned/before/after events. Process completed publication
   operations in reverse order, moving each canonical destination back to its
   recorded private stage only when the manifest `entries` still match (#393;
   `src/codereeve/migration/transaction.py:L768-L817`).
3. Process completed backup operations in reverse order. Copy each recorded
   backup to a private sibling, verify its type, mode, size, and hash against
   the manifest `entries`, then atomically restore the original `actions`
   source. Retain the backup as evidence (#393;
   `src/codereeve/migration/transaction.py:L768-L817`).
4. Revalidate every original input and confirm each canonical publication is
   absent before recording restoration success (#393;
   `src/codereeve/migration/transaction.py:L875-L920`).

There is no public restore command in 0.2. The #394 coordinator imports
`restore_migration`, supplies fresh quiescence authority, and uses the manifest
path returned by apply. The restoration API revalidates the filesystem even
when its persisted journal previously ended as restored (#393, #394;
`src/codereeve/migration/transaction.py:L922-L958`).

Rollback configuration and state before #394 activates the canonical service.
Do not modify the service unit as part of these steps; the migration only
inventories it and reports the external cutover prerequisite (#393, #394;
`src/codereeve/migration/inventory.py:L114-L193`).

## Current platform limits

The standalone command deliberately lacks the #394 authoritative coordinator,
so a real `--apply` refuses even after a ready check. The default operations do
not infer clearance from context, locks, heartbeats, markers, or process names
(#394; `src/codereeve/migration/transaction.py:L82-L108`).

Python's Windows runtime used here has no supported directory durability
implementation, so real Windows apply is also blocked before the migration lock
or data mutation. Portable tests inject the missing directory-sync capability;
that test seam is not a public bypass flag (#393;
`src/codereeve/migration/journal.py:L44-L65`,
`src/codereeve/migration/transaction.py:L606-L616`).
