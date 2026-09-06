---
title: Runtime provenance and machine-readable preflight
status: proposed
issue: 358
---

# Runtime provenance and machine-readable preflight

## Decision summary

The harness will embed an immutable, schema-versioned provenance record in every
distribution and expose it through `bh-daemon --provenance`. Standard sdist and
wheel builds will require explicit version and 40-character source-revision
assertions, verify those assertions against available source state, and fail before
publishing an artifact when validation fails. `bh-daemon --version` will report the
installed distribution version. These outcomes are required by #358.

The existing doctor will become a non-mutating, phase-selectable preflight with
stable text and JSON renderers. Its phases will be `installation`, `configuration`,
and `live`; an omitted `--phase` will run all three. Standalone doctor remains
advisory unless `--strict` is supplied, while daemon startup treats critical failures
as terminal. The standalone and daemon paths will use the same config resolver,
doctor context factory, catalog, execution engine, and redaction boundary (#358).

This work extends the frozen-wheel foundation rather than replacing it. That
foundation already builds an sdist and a wheel, installs the wheel outside the
checkout, validates installed entry points and resources, and smoke-runs the daemon
wrapper (`src/baton_harness/verify_foundation.py:L294-L409`;
`src/baton_harness/verify_foundation.py:L474-L560`).

## Current state

Package identity is currently duplicated as a static `0.1.0` in project metadata and
in `baton_harness.__version__` (`pyproject.toml:L9-L12`;
`src/baton_harness/__init__.py:L1-L16`). The build uses Hatchling and declares the
wheel package directly, but has no project-local metadata or build hook
(`pyproject.toml:L1-L7`; `pyproject.toml:L59-L60`).

`bh-daemon` already exposes six installed console commands through project metadata,
including the daemon and foundation verifier (`pyproject.toml:L47-L53`). Its doctor
flags currently render text only, build a `DoctorContext` inline, and preserve an
advisory default with `--strict` enforcing critical failures
(`src/baton_harness/chain/cli.py:L245-L314`). Daemon startup separately constructs a
second context and gates the old pre-bootstrap phase
(`src/baton_harness/chain/cli.py:L416-L443`).

The doctor catalog already provides stable check IDs, status and severity enums, and
remediation text, but its two phases are tied to secret-bootstrap timing rather than
operator-selectable concerns (`src/baton_harness/chain/doctor.py:L56-L76`;
`src/baton_harness/chain/doctor.py:L79-L145`). `run_report` always runs the entire
catalog, while `run_gate` filters one phase and exits on the first critical failure
(`src/baton_harness/chain/doctor.py:L1242-L1300`).

Config handling is duplicated and mutating. Doctor has its own permissive config
parser (`src/baton_harness/chain/doctor.py:L183-L208`), while daemon config parsing
performs a GitHub API probe and then exports resolved values into `os.environ`
(`src/baton_harness/chain/sandbox_config.py:L253-L350`;
`src/baton_harness/chain/sandbox_config.py:L351-L375`). The daemon infers
`.bh/config.env` from `BH_PROJECT_ROOT` and offers no explicit config path
(`src/baton_harness/chain/cli.py:L332-L347`).

The shared redactor currently recognizes common GitHub-token prefixes, URL userinfo,
and caller-supplied exact values, and returns only a redaction marker if pattern
substitution raises (`src/baton_harness/redact.py:L8-L42`). It does not yet define PEM
or OAuth credential patterns required by #358.

## Build identity contract

### Inputs and validation

`pyproject.toml` will declare `version` as dynamic and register in-repository custom
Hatch metadata and build hooks implemented in `hatch_build.py`. Hatch documents that
a custom metadata hook can mutate project metadata and that its script path is
configured under `[tool.hatch.metadata.hooks.custom]`.
https://hatch.pypa.io/1.16/plugins/metadata-hook/custom/ (fetched 2026-09-06)

Standard builds require both of these environment values:

- `BH_BUILD_VERSION`: a non-empty PEP 440 version accepted by the packaging metadata
  layer;
- `BH_BUILD_SOURCE_REVISION`: exactly 40 hexadecimal characters, normalized to
  lowercase.

When a `.git` worktree is available, the asserted revision must equal `git HEAD`.
Every build computes `lock_identity` as `sha256:` followed by the lowercase SHA-256
digest of the exact `uv.lock` bytes. A missing or unreadable lock is terminal. When a
generated provenance record is already present—as it will be when Hatch builds a
wheel from the generated sdist—it must exactly match the asserted version, revision,
lock identity, and development flag. Missing inputs, malformed inputs, a Git mismatch,
or a generated-record mismatch fail the build before an artifact is accepted (#358).

Editable developer installs opt in with `BH_BUILD_DEVELOPMENT=1`. That path requires
a Git checkout, derives the source revision from `HEAD`, and uses the configured
development base `0.1.0.dev0` plus a `g<12-character-revision>` local-version segment.
It computes the same lock identity and records `development: true`. Repository setup
and CI editable-sync commands will set this flag explicitly; a normal `uv build`
without the standard identity inputs therefore cannot silently emit a development
artifact. uv documents that workspace projects are installed editably by default
during sync.
https://docs.astral.sh/uv/concepts/projects/dependencies/ (fetched 2026-09-06)

### Generated record and archive behavior

The custom build hook will create the record in a build-owned temporary location and
add it with Hatch's `force-include` mechanism. It will not rewrite a tracked source
file. Hatch exposes `initialize`/`finalize` build-hook lifecycle methods and build data
including `artifacts` and `force_include` for files generated during a build.
https://hatch.pypa.io/1.0/plugins/build-hook/ (fetched 2026-09-06)

The archive path will be `baton_harness/build_provenance.json` in wheels and
`src/baton_harness/build_provenance.json` in sdists. The sdist will also contain
`hatch_build.py` and `uv.lock`, allowing a wheel built from that archive without
`.git` to validate the carried record against the explicit build assertions and exact
lock bytes. This matches uv's default build pipeline, which builds an sdist and then
builds the wheel from that sdist.
https://docs.astral.sh/uv/concepts/projects/build/ (fetched 2026-09-06)

The version written to Core Metadata is the same value written to the provenance
record. The packaging specification defines the distribution `Version` field as a
required metadata field.
https://packaging.python.org/en/latest/specifications/core-metadata/ (fetched
2026-09-06)

The record has this versioned schema:

```json
{
  "schema_version": 1,
  "package_version": "1.0.0",
  "source_revision": "0123456789abcdef0123456789abcdef01234567",
  "lock_identity": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "development": false
}
```

All five fields are required and additional fields are rejected for schema version 1.
`source_revision` and the digest portion of `lock_identity` are lowercase fixed-length
hex strings. `development` is a JSON boolean.

## Runtime provenance contract

Add `baton_harness.provenance` as the sole loader and validator for the packaged
record. It will read the record through `importlib.resources`, validate the complete
schema, obtain the installed version with `importlib.metadata.version("baton-harness")`,
and require exact equality with `package_version`. It will not inspect `.git` or infer
identity from a checkout (#358).

`baton_harness.__version__` will use installed distribution metadata instead of a
second literal. `bh-daemon --version` will print `bh-daemon <package_version>` and
exit before config or credential work. `bh-daemon --provenance` will emit the validated
record as JSON and likewise exit early. Missing, malformed, unsupported, or
version-inconsistent records return non-zero with a secret-safe diagnostic. Editable
installs receive the same generated record, with `development: true`, so runtime code
has no implicit fallback.

## Preflight phase model

Replace the two bootstrap-timing phases with these public, stable values:

| Phase | Boundary | Representative checks |
| --- | --- | --- |
| `installation` | Offline and credential-free installed-artifact integrity | provenance schema/version, package imports/resources, installed entry points, packaged workflow parsing, force-PR tripwire |
| `configuration` | Local host and target-repository configuration without credential use or remote calls | explicit config parsing/resolution, project root, provider shape, `.gitignore`, required CLI presence, credential-file/mount shape without reading secret contents |
| `live` | Credential-bearing or remote authority checks | vault key usability, GitHub authentication, repository access/rulesets/labels/admin, credential helper and OAuth usability |

The installation phase must run successfully in an isolated installed environment
with GitHub, BWS, and Claude credentials absent, and must not require a repository
checkout. Its tripwire reuses the existing subprocess self-test, which deliberately
runs in a temporary directory with worker identity
(`src/baton_harness/chain/cli.py:L165-L187`). Configuration checks may read only the
selected local config and filesystem metadata. Any check that needs a secret value,
credential helper, external CLI execution for authentication, or network access
belongs to `live` (#358).

`--phase {installation,configuration,live}` will be repeatable. Omitting it selects
all phases in the order above. `--format {text,json}` defaults to text. The existing
`--check-vault` compatibility option will select the vault check in the `live` phase
and retain its current non-zero-on-failure behavior
(`src/baton_harness/chain/cli.py:L250-L296`).

The catalog remains the only check registry. Every `CheckResult` carries stable
`id`, `phase`, `status`, `severity`, `title`, `detail`, and `remediation` values.
Statuses serialize as `pass`, `fail`, `warn`, or `skip`; severities serialize as
`critical` or `warning`. A check exception becomes a failed result and is passed
through the same redaction path as ordinary detail.

## Config resolution and daemon integration

Split sandbox config into a pure resolver and an explicit environment application
step. The resolver accepts a path and environment mapping, parses strictly, applies
the documented non-empty environment overrides, validates local shapes, and returns
an immutable resolved config plus its selected path. It performs no network call and
does not mutate process state. The application step exports compatibility variables
only after resolution succeeds. The existing precedence rule—non-empty environment
value over file value—is already centralized in `resolve_overridable_keys`
(`src/baton_harness/chain/sandbox_config.py:L227-L250`).

Both standalone doctor and daemon startup use a shared context factory. The explicit
`--config PATH` wins when supplied; otherwise the resolver uses
`$BH_PROJECT_ROOT/.bh/config.env`. An explicit path does not require
`BH_PROJECT_ROOT` merely to locate the file. Repository-existence validation moves
from config parsing to the `live` phase because it currently invokes `gh api`
(`src/baton_harness/chain/sandbox_config.py:L338-L349`).

Daemon startup executes installation and configuration gates before bootstrapping
secrets, then executes the live gate with the bootstrapped authority before entering
the event loop. All selected critical failures are collected and rendered before the
gate returns non-zero; daemon startup does not continue after a critical failure.
Standalone doctor renders the same results but exits zero by default, preserving its
current advisory behavior, and exits one under `--strict` when any critical check
fails (`src/baton_harness/chain/cli.py:L298-L314`). Argparse usage errors retain exit
code 2.

## JSON report and redaction

`--doctor --format json` emits exactly one JSON document to stdout:

```json
{
  "schema_version": 1,
  "provenance": {
    "package_version": "1.0.0",
    "source_revision": "0123456789abcdef0123456789abcdef01234567",
    "lock_identity": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "development": false
  },
  "selected_phases": ["installation"],
  "summary": {
    "pass": 5,
    "fail": 0,
    "warn": 0,
    "skip": 0,
    "critical_failures": 0
  },
  "checks": [
    {
      "id": "PKG_PROVENANCE",
      "phase": "installation",
      "status": "pass",
      "severity": "critical",
      "title": "Runtime provenance is valid",
      "detail": "Installed metadata matches the packaged provenance record.",
      "remediation": "Reinstall a verified baton-harness artifact."
    }
  ]
}
```

The serializer validates and redacts every human-readable field immediately before
writing either text or JSON. Redaction expands the existing fail-closed helper to
cover GitHub tokens, exact known secret values, URL userinfo and sensitive query
parameters, PEM blocks, and OAuth/API credential fields. Known secret environment
values are supplied as exact redaction inputs, but are never copied into the report.
If serialization or redaction cannot safely complete, doctor emits a fixed diagnostic
and returns non-zero instead of emitting partial JSON. This fulfills the no-token,
no-PEM, no-OAuth-material, and no-credential-bearing-URL boundary in #358.

Logs and diagnostics go to stderr so JSON stdout remains parseable. Ordering is
deterministic: selected phase order, catalog order, and fixed summary/check keys.

## Foundation verifier changes

`bh-verify-foundation` will supply deterministic standard-build identity to the real
`uv build`, inspect the sdist and wheel provenance records, and compare each record
with wheel Core Metadata and the exact lock digest. It will extract the sdist into a
temporary source tree, confirm no `.git` directory is present, and build/validate a
wheel there with the same asserted identity. The existing verifier already provides
bounded subprocess execution and normalizes non-zero commands into `FoundationError`
(`src/baton_harness/verify_foundation.py:L61-L144`).

The installed smoke will additionally call `bh-daemon --version`,
`bh-daemon --provenance`, and the credential-free installation doctor JSON phase. It
will parse the JSON rather than matching display text. The verifier remains the one
local/CI authority for source archive, wheel, non-editable installation, resource,
and entry-point integrity, consistent with its current contract
(`docs/superpowers/specs/2026-09-06-frozen-wheel-foundation-design.md:L97-L138`).

## Tests

Implementation follows red-green-refactor. Tests required by #358 cover:

- missing, malformed, and Git-inconsistent standard build assertions;
- mismatched generated provenance when building from a source archive without
  `.git`;
- exact lock hashing, sdist/wheel inclusion, Core Metadata equality, and editable
  `development: true` identity;
- runtime schema rejection, unsupported schema versions, missing records, and
  distribution-version mismatch;
- `--version` and `--provenance` early exits without config or credentials;
- stable JSON keys and enum values, deterministic ordering, phase selection, default
  all-phase selection, advisory versus strict exit status, and daemon fail-closed
  gates;
- shared explicit/default config resolution without mutation, followed by explicit
  daemon environment application;
- token, URL, PEM, OAuth, exception, and exact-value redaction in both renderers;
- an installed, outside-checkout, credential-free installation phase covering
  imports, entry points, packaged workflow parsing, and the merge-prevention
  tripwire; and
- separate configuration and live-phase failure paths.

Existing doctor and daemon tests will be migrated to the public phase values and
shared context factory. No modified production path will be left without a regression
or behavior test, per the repository testing policy.

## Documentation and compatibility

Update `README.md` and `docs/system-setup.md` with the build identity variables,
editable-development flag, provenance commands, doctor phase/format/config options,
and exit semantics. This is required because the work changes build, install, and
local operational commands. Existing plain-text doctor output remains human-oriented;
automation should use JSON schema version 1. `--doctor`, `--strict`, and
`--check-vault` remain accepted, while the new options only refine selection and
rendering.

No new runtime dependency is introduced. The hook implementation uses Hatchling's
documented in-tree custom hooks, and runtime validation uses Python standard-library
metadata, resources, hashing, JSON, and regular-expression facilities. Hatch's custom
build-hook documentation permits project-local hook scripts configured by path.
https://hatch.pypa.io/1.16/plugins/build-hook/custom/ (fetched 2026-09-06)

## Scope boundaries

This issue does not publish an artifact, choose a release number, change daemon
authorization policy, provision GitHub or Bitwarden resources, or make doctor repair
operator state. It does not require live `.git` metadata at runtime and does not add
an auto-update mechanism. Deployment-bundle assembly remains outside #358 and under
the release-gate work grouped by #361.
