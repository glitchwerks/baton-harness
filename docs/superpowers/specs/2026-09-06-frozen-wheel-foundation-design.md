---
title: Frozen wheel installation foundation
status: approved
issue: 360
---

# Frozen wheel installation foundation

## Decision summary

The harness will gain a committed universal `uv.lock`, packaged runtime defaults,
and one invariant-driven verification command named `bh-verify-foundation`. The
same command will run locally and in CI so lock, resource, wheel, installation,
and entry-point checks cannot drift into separate implementations. This implements
the frozen-installation outcomes tracked by #360.

Python 3.10 remains the declared floor and Python 3.13 is the upper verification
endpoint (`pyproject.toml:L9-L30`; #360). Development remains editable, while the
production proof installs a built wheel non-editably with runtime dependencies only.

## Current state

The project declares Python 3.10+, three lower-bounded runtime dependencies, and a
four-package `dev` extra, but has no committed lock (`pyproject.toml:L9-L30`). Its
five console scripts and Hatch wheel package are declared in
`pyproject.toml:L32-L58`.

CI creates a Python 3.10 virtual environment and runs an unlocked editable
`uv pip install -e ".[dev]"` in each Python job
(`.github/actions/setup/action.yml:L1-L27`). The test workflow exercises that
editable environment, not a built wheel (`.github/workflows/ci.yml:L45-L55`). The
developer bootstrap and README intentionally describe the same editable workflow
(`bin/setup-env.sh:L432-L446`; `README.md:L136-L164`).

Runtime defaults currently depend on repository-relative paths. The daemon derives
`config/WORKFLOW.md` by walking four parents from its module
(`src/baton_harness/chain/cli.py:L151-L165`), while ruleset comparison derives four
JSON paths from a presumed checkout root
(`src/baton_harness/chain/ruleset_status.py:L67-L90`). Maintained shell consumers
also read top-level `config/` paths (`bin/run-daemon.sh:L150-L153`;
`bin/provision-ruleset.sh:L463-L464`; `bin/install-daemon-service.sh:L209-L212`).

## Dependency-lock contract

Commit `uv.lock` beside `pyproject.toml`. uv defines this as a universal,
cross-platform lock containing exact resolutions across Python and platform markers
and recommends committing it for reproducible installations.
https://docs.astral.sh/uv/concepts/projects/layout/ (fetched 2026-09-06)

The lock covers the base project and its `dev` extra across the declared Python range.
Hatchling is also selected in the `dev` extra so its exact build-backend resolution can
be exported as a build constraint. Dependency changes are made in `pyproject.toml`,
followed deliberately by `uv lock`. Neither CI nor validation may update the lock
implicitly.

Development setup changes from an unlocked `uv pip install` to
`uv sync --locked --extra dev`. uv installs projects editably by default during
sync, includes optional dependencies only when selected with `--extra`, and raises
instead of updating when `--locked` sees stale metadata.
https://docs.astral.sh/uv/concepts/projects/sync/ (fetched 2026-09-06)

Production verification exports only the base dependency set from the checked lock,
syncs that exact set into a temporary environment, and installs the built wheel with
`--no-deps`. This separates dependency installation from the wheel installation and
makes an accidental dev-extra dependency or resolver fallback observable. No second
requirements lock is committed; `uv.lock` remains the only dependency authority.

## Resource ownership and compatibility mirrors

Create `src/baton_harness/resources/` as the canonical home for:

- `WORKFLOW.md`;
- `ruleset.main.json`;
- `ruleset.feature.json`;
- `ruleset.compare-keys.json`;
- `ruleset.compare-keys.app.json`.

Keep byte-identical files at the existing top-level `config/` paths as temporary
compatibility mirrors for the shell consumers identified above. Issues #378–#381 own
consumer migration; #360 does not rewrite those launchers. Tests and
`bh-verify-foundation` compare every canonical resource with its mirror byte for byte,
so either side changing alone fails closed.

Python runtime consumers use `importlib.resources.files()` to locate canonical
resources. They read JSON through the resource `Traversable` API and use
`importlib.resources.as_file()` for the workflow loader's filesystem-path boundary.
Python documents that package resources need not be physical filesystem files and
that `as_file()` provides a scoped `Path` when one is required.
https://docs.python.org/3/library/importlib.resources.html (fetched 2026-09-06)

The resource helper accepts only a fixed internal resource name, checks existence,
and raises a domain-specific error naming the absent resource. Explicit
`--workflow PATH` behavior remains filesystem-based and unchanged. Missing package
resources therefore fail at startup or ruleset comparison instead of falling back to
a checkout.

## `bh-verify-foundation` contract

Add `bh-verify-foundation` to `[project.scripts]` as a sixth console entry point. It
is a developer and CI validation command implemented in Python so the same behavior
runs on Windows and Linux. Its default mode requires a project root containing
`pyproject.toml` and performs these ordered checks:

1. Run `uv lock --check`; a missing or stale lock fails immediately. uv documents
   that this checks whether the lock matches project metadata and errors without
   rewriting it.
   https://docs.astral.sh/uv/concepts/projects/sync/ (fetched 2026-09-06)
2. Compare every packaged-resource source file with its `config/` compatibility
   mirror as raw bytes.
3. Export both runtime-only and `--extra dev` resolutions from the checked lock, then
   build an sdist and wheel with the dev export supplied through
   `uv build --build-constraints ... --require-hashes`. This freezes Hatchling and its
   build transitives. `uv build` builds the wheel from the sdist when both formats are
   requested, so omitted source-distribution data is also exposed.
   https://docs.astral.sh/uv/concepts/projects/build/ (fetched 2026-09-06)
4. Inspect the wheel archive before installation: require all five resources, the
   original five runtime entry points, and `bh-verify-foundation`; reject unexpected
   duplicate resource paths.
5. For each requested interpreter, create a temporary environment, sync the exported
   runtime dependency set, install the wheel with `--no-deps`, and run `uv pip check`.
6. From a temporary working directory outside the repository, invoke the installed
   command with a hidden `--installed-smoke` mode. That mode verifies distribution
   metadata, confirms the imported package is inside the temporary environment rather
   than the checkout, loads every packaged resource, confirms the six console entry
   points, and rejects the resolved packages selected only by the declared dev extra.
7. Exercise each original entry point without external writes: `bh-daemon --help`,
   the three lifecycle hooks from a deliberately unresolvable temporary directory
   with their documented failure expected, and `bh-force-pr-not-merge` with a benign
   JSON payload. A successful smoke means the generated wrappers import and execute;
   behavioral coverage remains in the existing test suite.

The default interpreter set is 3.10 and 3.13, matching #360. Repeated `--python`
arguments allow focused local checks without changing CI policy. Temporary build and
environment directories are deleted on success and retained only when an explicit
diagnostic option requests it. Errors identify the failed invariant and return
non-zero; the command never repairs state automatically.

## CI enforcement

The shared setup action installs with `uv sync --locked --extra dev`, making every
existing Python job reject lock drift before linting, typing, or tests. The existing
`Test (pytest)` job then runs `bh-verify-foundation` after pytest. Keeping validation
inside that existing required check avoids a required-check name migration while still
making the complete foundation proof merge-blocking
(`.github/workflows/ci.yml:L14-L55`).

CI supplies no reduced interpreter list: the verifier installs and checks both Python
3.10 and 3.13. The validator is the authority for packaging invariants; CI contains
only the invocation, so developers can reproduce the exact check locally.

## Tests

Implementation follows red-green-refactor. Unit tests cover:

- resource lookup, reads, scoped workflow paths, and clear missing-resource errors;
- the full canonical-to-mirror manifest and byte-drift detection;
- lock-check, build, wheel-inspection, install, and subprocess failure reporting with
  injected command runners;
- installed-smoke metadata, path, dependency, resource, and entry-point invariants;
- CLI parsing, repeated interpreter selection, and non-zero exits.

Integration coverage builds the real wheel, inspects its archive, installs it outside
the checkout, and runs the installed smoke. CI is the authoritative two-interpreter
execution. Existing tests are updated to use canonical resource helpers while retaining
temporary-path injection where behavior under malformed configuration is under test.

## Documentation

README and `docs/system-setup.md` retain `bin/setup-env.sh` as the editable developer
bootstrap but describe its locked synchronization. A separate production-installation
section documents building the wheel, exporting locked runtime dependencies, installing
the wheel non-editably without dev extras, and running `bh-verify-foundation` before
deployment. The docs explicitly distinguish source development from immutable runtime
installation, as required by #360.

## Scope boundaries

This issue does not convert shell launchers into packaged commands, build the final
deployment bundle, pin external CLIs, define systemd policy, or change GitHub App
provenance. Those boundaries remain owned by the follow-on work listed in #360 and the
epic #361.
