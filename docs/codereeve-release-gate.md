# CodeReeve release-gate upgrade fixture

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
