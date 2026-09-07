# CodeReeve Package and CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete issue #392 by making `codereeve` the canonical Python distribution, package, provenance identity, and unified CLI while retaining thin Baton-era compatibility shims.

**Architecture:** Move the complete owned source tree into `src/codereeve/`, then add one command router that delegates to the existing tested handlers. Keep compatibility in a deliberately small `src/baton_harness/` shim package and a `codereeve.legacy_cli` adapter; neither may contain orchestration logic. Update the frozen-artifact verifier to prove both the canonical surface and the temporary wrappers from built artifacts, not merely from the checkout.

**Tech Stack:** Python 3.10+, Hatchling, uv, pytest, Ruff, mypy, standard-library `argparse`/`importlib.metadata`, wheel and sdist inspection.

**Spec:** `docs/superpowers/specs/2026-09-07-codereeve-rename-design.md`

## Global Constraints

- `CodeReeve` is the product name and closed-form `codereeve` is the canonical distribution, package, and executable identity from 0.2.0 (`docs/superpowers/specs/2026-09-07-codereeve-rename-design.md:L64-L79`).
- The canonical command map is exactly `codereeve daemon`, `doctor`, `provenance`, `hook after-create`, `hook before-run`, `hook after-run`, `hook force-pr-not-merge`, and `verify`, plus top-level `--version` (`docs/superpowers/specs/2026-09-07-codereeve-rename-design.md:L92-L106`).
- The supported 0.2 upgrade installs into a new environment, leaves the Baton-era environment untouched for rollback, and rejects any environment where both the `baton-harness` and `codereeve` distributions claim overlapping files or entry points (#392).
- Symphony remains vendored as `codereeve.vendor.symphony`; its component name, license, behavior, and provenance are preserved (`docs/superpowers/specs/2026-09-07-codereeve-rename-design.md:L15-L20`, #390).
- `baton_harness` and all six current `bh-*` commands remain thin, tested compatibility surfaces through 0.3.x and are removed by #398 (`docs/superpowers/specs/2026-09-07-codereeve-rename-design.md:L118-L138`).
- This issue does not implement the environment/path migration owned by #393, the service cutover owned by #394, the broad documentation sweep owned by #395, or the repository rename owned by #396.
- Preserve the existing Python floor `>=3.10` and the locked dependency model (`pyproject.toml:L13-L31`).
- All behavior changes use red-green TDD. The namespace relocation uses the `refactoring-discipline` skill and must preserve the existing test suite before new CLI behavior is layered on.
- Invoke Python with the worktree interpreter, for example `./.venv/Scripts/python.exe -m pytest` on Windows or the corresponding `.venv/bin/python` on POSIX.
- Use `.tmp/` for transient build and test output. Do not commit generated provenance records or distribution artifacts.
- Every commit remains on an issue-specific child branch/worktree whose PR targets the 0.2 primary feature branch described by #396.

---

## File Structure

### Canonical package

- Move: `src/baton_harness/` -> `src/codereeve/` — complete application source, resources, hooks, scenario code, and vendored Symphony tree.
- Create: `src/codereeve/cli.py` — the only canonical top-level command router.
- Create: `src/codereeve/legacy_cli.py` — six legacy console adapters and their single warning seam.
- Modify: `src/codereeve/__init__.py` — canonical package description and `metadata.version("codereeve")` lookup.
- Modify: `src/codereeve/chain/cli.py` — retain daemon/doctor/provenance behavior while accepting an injected display program name.
- Modify: `src/codereeve/provenance.py` — validate the `codereeve` distribution and `codereeve/build_provenance.json`.
- Modify: `src/codereeve/resources/__init__.py` — load resources from `codereeve.resources`.
- Modify: `src/codereeve/verify_foundation.py` — validate canonical artifacts and separately exercise compatibility entry points.

### Compatibility package

- Create: `src/baton_harness/__init__.py` — re-export only `codereeve.__version__` and documented top-level names.
- Create: `src/baton_harness/after_create.py` — re-export `codereeve.after_create.main`.
- Create: `src/baton_harness/before_run.py` — re-export `codereeve.before_run.main`.
- Create: `src/baton_harness/after_run.py` — re-export `codereeve.after_run.main`.
- Create: `src/baton_harness/chain/__init__.py` — compatibility package marker with no runtime logic.
- Create: `src/baton_harness/chain/cli.py` — re-export the daemon compatibility entry point.
- Create: `src/baton_harness/hooks/__init__.py` — compatibility package marker with no runtime logic.
- Create: `src/baton_harness/hooks/force_pr_not_merge.py` — re-export the canonical guard hook.
- Create: `src/baton_harness/verify_foundation.py` — re-export the canonical verifier entry point.

### Build metadata and tests

- Modify: `pyproject.toml` — distribution name, package list, canonical script, and temporary legacy scripts.
- Modify: `hatch_build.py` — canonical build variables, compatibility build-variable resolution, development version, and canonical record destinations.
- Modify: `uv.lock` — root distribution identity after `uv lock`.
- Create: `tests/test_codereeve_namespace.py` — canonical namespace and version contract.
- Create: `tests/test_codereeve_cli.py` — command-routing contract.
- Create: `tests/test_legacy_package_shims.py` — shim and legacy-console delegation contract.
- Modify: `tests/test_build_provenance.py`, `tests/test_provenance.py`, `tests/test_verify_foundation.py`, `tests/test_smoke.py`, `tests/chain/test_cli.py`, and `tests/test_resources.py` — canonical package/artifact expectations.
- Modify: all remaining tracked `src/**/*.py` and `tests/**/*.py` files returned by `rg -l "baton_harness" src tests` — canonical internal imports and patch targets.
- Modify: packaging/development and CLI sections of `README.md` — only the #392-owned install/build/command material; #395 owns the comprehensive repository-wide identity sweep.

---

### Task 1: Relocate the owned package and canonicalize build identity

**Files:**
- Create: `tests/test_codereeve_namespace.py`
- Move: `src/baton_harness/` -> `src/codereeve/`
- Modify: `src/codereeve/__init__.py`
- Modify: `src/codereeve/provenance.py`
- Modify: `src/codereeve/resources/__init__.py`
- Modify: `hatch_build.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: every file returned by `rg -l "baton_harness" src tests`
- Test: `tests/test_smoke.py`
- Test: `tests/test_build_provenance.py`
- Test: `tests/test_provenance.py`
- Test: `tests/test_resources.py`

**Interfaces:**
- Consumes: current `baton_harness` module graph and Hatch build hook. The current distribution/package/script coupling is declared in `pyproject.toml:L9-L66`, and provenance currently names both the old distribution and record path (`b40e499:src/baton_harness/provenance.py:L105-L138`).
- Produces: importable `codereeve`, `codereeve.__version__: str`, canonical package resources, canonical artifact provenance, and a test suite whose internal imports no longer depend on `baton_harness`.

- [ ] **Step 1: Write the failing canonical namespace tests**

Create `tests/test_codereeve_namespace.py`:

```python
"""Canonical CodeReeve package identity tests."""

from importlib import metadata

import codereeve
from codereeve.resources import RESOURCE_NAMES, read_bytes
from codereeve.vendor.symphony.orchestrator import Orchestrator


def test_canonical_distribution_and_package_versions_match() -> None:
    assert codereeve.__version__ == metadata.version("codereeve")


def test_canonical_resources_are_packaged() -> None:
    assert RESOURCE_NAMES
    assert all(read_bytes(name) for name in RESOURCE_NAMES)


def test_symphony_remains_vendored_under_canonical_package() -> None:
    assert Orchestrator.__module__.startswith("codereeve.vendor.symphony")
```

- [ ] **Step 2: Run the namespace test to verify the red state**

Run:

```bash
./.venv/Scripts/python.exe -m pytest tests/test_codereeve_namespace.py -v
```

Expected: collection fails with `ModuleNotFoundError: No module named 'codereeve'`.

- [ ] **Step 3: Move the complete source tree and update internal imports**

Use `git mv src/baton_harness src/codereeve`. Replace Python imports and test patch strings from `baton_harness` to `codereeve` in every file returned by:

```bash
rg -l "baton_harness" src tests
```

Do not replace prose that records historical Baton/Symphony provenance. Update `src/codereeve/__init__.py` to use:

```python
from importlib.metadata import version

__version__ = version("codereeve")
```

Update `src/codereeve/resources/__init__.py` so `_PACKAGE = "codereeve.resources"`.

- [ ] **Step 4: Canonicalize distribution metadata and build provenance**

Change `pyproject.toml` to:

```toml
[project]
name = "codereeve"
dynamic = ["version"]
description = "Disciplined autonomous software-development orchestration."

[tool.hatch.build.targets.wheel]
packages = ["src/codereeve"]
```

In `hatch_build.py`, use these constants and resolver contract:

```python
RECORD_PATH = Path("src/codereeve/build_provenance.json")
DEVELOPMENT_VERSION = "0.2.0.dev0"


def _compat_value(
    env: Mapping[str, str], canonical: str, legacy: str
) -> str | None:
    canonical_value = env.get(canonical)
    legacy_value = env.get(legacy)
    if (
        canonical_value is not None
        and legacy_value is not None
        and canonical_value != legacy_value
    ):
        raise BuildProvenanceError(
            f"conflicting build identity variables: {canonical} and {legacy}"
        )
    return canonical_value if canonical_value is not None else legacy_value
```

Resolve `CODEREEVE_BUILD_VERSION`, `CODEREEVE_BUILD_SOURCE_REVISION`, and `CODEREEVE_BUILD_DEVELOPMENT` first, with `BH_BUILD_*` as temporary aliases. Never include either value in a conflict error. Force-include the record at `src/codereeve/build_provenance.json` in sdists and `codereeve/build_provenance.json` in wheels. Update `src/codereeve/provenance.py` to call `metadata.distribution("codereeve")` and require exactly one `codereeve/build_provenance.json`.

- [ ] **Step 5: Update build tests before changing their implementation expectations**

In `tests/test_build_provenance.py`, replace record paths with `src/codereeve/build_provenance.json`, make canonical `CODEREEVE_BUILD_*` variables the primary cases, and add:

```python
def test_conflicting_canonical_and_legacy_build_values_fail_closed(
    tmp_path: Path,
) -> None:
    (tmp_path / "uv.lock").write_bytes(LOCK_CONTENT)
    with pytest.raises(
        BuildProvenanceError,
        match="CODEREEVE_BUILD_VERSION and BH_BUILD_VERSION",
    ):
        resolve_build_provenance(
            tmp_path,
            {
                "CODEREEVE_BUILD_VERSION": "0.2.0",
                "BH_BUILD_VERSION": "9.9.9",
                "CODEREEVE_BUILD_SOURCE_REVISION": REVISION,
            },
            read_head=lambda _root: REVISION,
        )


def test_legacy_build_variables_remain_compatible(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_bytes(LOCK_CONTENT)
    identity = resolve_build_provenance(
        tmp_path,
        {
            "BH_BUILD_VERSION": "0.2.0",
            "BH_BUILD_SOURCE_REVISION": REVISION,
        },
        read_head=lambda _root: REVISION,
    )
    assert identity.package_version == "0.2.0"
```

Update `tests/test_provenance.py`, `tests/test_resources.py`, and `tests/test_smoke.py` to assert `codereeve` and the canonical record path.

- [ ] **Step 6: Refresh the lock and run focused green tests**

Run:

```bash
uv lock
BH_BUILD_DEVELOPMENT=1 uv sync --locked --extra dev
./.venv/Scripts/python.exe -m pytest tests/test_codereeve_namespace.py tests/test_build_provenance.py tests/test_provenance.py tests/test_resources.py tests/test_smoke.py -v
```

Expected: the lock records the root package as `codereeve`; all focused tests pass. `BH_BUILD_DEVELOPMENT=1` is intentionally retained only to prove the temporary build alias during this task.

- [ ] **Step 7: Run the complete namespace-import regression set**

Run:

```bash
rg -n "(^|[\"'])baton_harness([.\"']|$)" src/codereeve tests
./.venv/Scripts/python.exe -m pytest --basetemp .tmp/pytest-392-task1 -q
```

Expected: `rg` returns no canonical-source or test imports; pytest passes. If the one test that requires a non-Git temp directory is affected by the in-repository base temp, rerun that exact test with a verified non-repository `--basetemp`, as established by the branch baseline.

- [ ] **Step 8: Commit the canonical namespace**

```bash
git add pyproject.toml uv.lock hatch_build.py src/codereeve tests
git commit -m "refactor(#392): move application package to codereeve"
```

---

### Task 2: Add the unified canonical command router

**Files:**
- Create: `src/codereeve/cli.py`
- Create: `tests/test_codereeve_cli.py`
- Modify: `src/codereeve/chain/cli.py`
- Modify: `tests/chain/test_cli.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: `codereeve.chain.cli.main(argv, *, prog)`, the existing hook `main` callables, `codereeve.verify_foundation.main`, and `codereeve.__version__`.
- Produces: `codereeve.cli.main(argv: Sequence[str] | None = None) -> int` and the `codereeve` console script. The command mapping is fixed by the approved spec (`docs/superpowers/specs/2026-09-07-codereeve-rename-design.md:L92-L113`).

- [ ] **Step 1: Write failing router tests**

Create `tests/test_codereeve_cli.py` with injected/monkeypatched handlers so routing is tested without launching external processes:

```python
"""Tests for the unified CodeReeve command router."""

from collections.abc import Callable

import pytest

from codereeve import cli


@pytest.mark.parametrize(
    ("argv", "handler", "forwarded"),
    [
        (["daemon", "--once"], "daemon", ["--once"]),
        (["doctor", "--strict"], "doctor", ["--strict"]),
        (["provenance"], "provenance", []),
        (["verify", "--python", "3.13"], "verify", ["--python", "3.13"]),
        (["hook", "after-create"], "hook_after_create", []),
        (["hook", "before-run"], "hook_before_run", []),
        (["hook", "after-run"], "hook_after_run", []),
        (
            ["hook", "force-pr-not-merge"],
            "hook_force_pr_not_merge",
            [],
        ),
    ],
)
def test_routes_to_exact_handler(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    handler: str,
    forwarded: list[str],
) -> None:
    calls: list[list[str]] = []
    fake: Callable[[list[str]], int] = lambda args: calls.append(args) or 17
    monkeypatch.setitem(cli.HANDLERS, handler, fake)
    assert cli.main(argv) == 17
    assert calls == [forwarded]


def test_unknown_command_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["unknown"]) == 2
    assert "usage: codereeve" in capsys.readouterr().err


def test_doctor_preserves_strict_failure_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(cli.HANDLERS, "doctor", lambda _args: 1)
    assert cli.main(["doctor", "--strict"]) == 1
```

Add tests that `codereeve --version` prints `codereeve ` followed by `codereeve.__version__`, top-level `--help` lists every command, and an absent command returns 2.

- [ ] **Step 2: Run the router tests to verify the red state**

Run:

```bash
./.venv/Scripts/python.exe -m pytest tests/test_codereeve_cli.py -v
```

Expected: collection fails because `codereeve.cli` does not exist.

- [ ] **Step 3: Make daemon output injectable without changing behavior**

Change `src/codereeve/chain/cli.py` to:

```python
def main(
    argv: list[str] | None = None,
    *,
    prog: str = "codereeve daemon",
) -> int:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Always-on daemon: polls for agent-ready issues and runs them"
            " as dependency-ordered work units."
        ),
    )
```

Replace hard-coded user-facing `bh-daemon:` prefixes in this module with `f"{prog}:"`. Keep option names, evaluation order, secret handling, early exits, and daemon invocation unchanged. Update `tests/chain/test_cli.py` to expect `codereeve daemon` and add one test proving `main([], prog="bh-daemon")` preserves legacy display output.

- [ ] **Step 4: Implement the canonical router**

Create `src/codereeve/cli.py` with this public shape:

```python
"""Unified CodeReeve command-line interface."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence

from codereeve import __version__
from codereeve import after_create, after_run, before_run, verify_foundation
from codereeve.chain import cli as daemon_cli
from codereeve.hooks import force_pr_not_merge

Handler = Callable[[list[str]], int]


def _daemon(argv: list[str]) -> int:
    return daemon_cli.main(argv, prog="codereeve daemon")


def _doctor(argv: list[str]) -> int:
    return daemon_cli.main(["--doctor", *argv], prog="codereeve doctor")


def _provenance(argv: list[str]) -> int:
    if argv:
        return _usage_error("provenance does not accept arguments")
    return daemon_cli.main(["--provenance"], prog="codereeve provenance")


def _guard(argv: list[str]) -> int:
    if argv:
        return _usage_error("hook force-pr-not-merge does not accept arguments")
    return force_pr_not_merge.main()


def _usage_error(message: str) -> int:
    print(f"codereeve: {message}", file=sys.stderr)
    print(HELP, file=sys.stderr)
    return 2


HELP = """usage: codereeve [--version] COMMAND [ARGS]

commands:
  daemon
  doctor
  provenance
  hook after-create
  hook before-run
  hook after-run
  hook force-pr-not-merge
  verify"""


HANDLERS: dict[str, Handler] = {
    "daemon": _daemon,
    "doctor": _doctor,
    "provenance": _provenance,
    "verify": lambda argv: verify_foundation.main(argv),
    "hook_after_create": lambda argv: after_create.main(argv),
    "hook_before_run": lambda argv: before_run.main(argv),
    "hook_after_run": lambda argv: after_run.main(argv),
    "hook_force_pr_not_merge": _guard,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return _usage_error("a command is required")
    if args == ["--help"] or args == ["-h"]:
        print(HELP)
        return 0
    if args == ["--version"]:
        print(f"codereeve {__version__}")
        return 0

    command = args.pop(0)
    if command == "hook":
        if not args:
            return _usage_error("a hook command is required")
        hook = args.pop(0).replace("-", "_")
        key = f"hook_{hook}"
        if key not in HANDLERS:
            return _usage_error(f"unknown hook command: {hook}")
        return HANDLERS[key](args)
    if command not in {"daemon", "doctor", "provenance", "verify"}:
        return _usage_error(f"unknown command: {command}")
    return HANDLERS[command](args)
```

Complete `main` with explicit command lookup, the exact command names from the spec, a stable help string, and exit code 2 for usage errors. Add `codereeve = "codereeve.cli:main"` under `[project.scripts]`.

- [ ] **Step 5: Run router and existing daemon CLI tests**

Run:

```bash
./.venv/Scripts/python.exe -m pytest tests/test_codereeve_cli.py tests/chain/test_cli.py tests/chain/test_cli_doctor_gate.py tests/chain/test_cli_report.py -v
```

Expected: all tests pass; canonical calls show canonical program names and the injected legacy `prog` test shows `bh-daemon`.

- [ ] **Step 6: Commit the unified CLI**

```bash
git add pyproject.toml src/codereeve/cli.py src/codereeve/chain/cli.py tests/test_codereeve_cli.py tests/chain/test_cli.py
git commit -m "feat(#392): add unified codereeve command"
```

---

### Task 3: Add temporary legacy package and console shims

**Files:**
- Create: `src/codereeve/legacy_cli.py`
- Create: `src/baton_harness/__init__.py`
- Create: `src/baton_harness/after_create.py`
- Create: `src/baton_harness/before_run.py`
- Create: `src/baton_harness/after_run.py`
- Create: `src/baton_harness/chain/__init__.py`
- Create: `src/baton_harness/chain/cli.py`
- Create: `src/baton_harness/hooks/__init__.py`
- Create: `src/baton_harness/hooks/force_pr_not_merge.py`
- Create: `src/baton_harness/verify_foundation.py`
- Create: `tests/test_legacy_package_shims.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: canonical functions from `codereeve.cli`, `codereeve.after_create`, `codereeve.before_run`, `codereeve.after_run`, `codereeve.hooks.force_pr_not_merge`, and `codereeve.verify_foundation`.
- Produces: `warn_legacy(surface: str, replacement: str) -> None`, six zero-logic legacy console adapters, and the supported legacy import modules specified by #392.

- [ ] **Step 1: Write failing compatibility tests**

Create `tests/test_legacy_package_shims.py`:

```python
"""Temporary Baton-era compatibility contracts for 0.2/0.3."""

import importlib
import warnings

import codereeve


def test_top_level_legacy_package_reexports_version() -> None:
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        legacy = importlib.import_module("baton_harness")
    assert legacy.__version__ == codereeve.__version__
    assert len(captured) == 1
    assert "removed in 0.4.0" in str(captured[0].message)


def test_legacy_entry_module_is_canonical_function() -> None:
    legacy = importlib.import_module("baton_harness.after_create")
    canonical = importlib.import_module("codereeve.after_create")
    assert legacy.main is canonical.main
```

Add equivalent identity assertions for `before_run`, `after_run`, `chain.cli`, `hooks.force_pr_not_merge`, and `verify_foundation`. Add one parameterized test that patches each canonical handler, invokes its legacy adapter, asserts exact argument forwarding/return code, and asserts one stderr notice naming the replacement and 0.4.0.

- [ ] **Step 2: Run the compatibility tests to verify the red state**

Run:

```bash
./.venv/Scripts/python.exe -m pytest tests/test_legacy_package_shims.py -v
```

Expected: import fails because the compatibility package and adapters do not exist.

- [ ] **Step 3: Implement one warning seam and six legacy adapters**

Create `src/codereeve/legacy_cli.py` with module-local deduplication:

```python
"""Temporary Baton-era console entry points."""

from __future__ import annotations

import sys

from codereeve import cli

_WARNED: set[str] = set()


def warn_legacy(surface: str, replacement: str) -> None:
    if surface in _WARNED:
        return
    _WARNED.add(surface)
    print(
        f"{surface} is deprecated; use {replacement}; removed in 0.4.0",
        file=sys.stderr,
    )


def daemon_main() -> int:
    warn_legacy("bh-daemon", "codereeve daemon")
    return cli.main(["daemon", *sys.argv[1:]])


def after_create_main() -> int:
    warn_legacy("bh-after-create", "codereeve hook after-create")
    return cli.main(["hook", "after-create", *sys.argv[1:]])


def before_run_main() -> int:
    warn_legacy("bh-before-run", "codereeve hook before-run")
    return cli.main(["hook", "before-run", *sys.argv[1:]])


def after_run_main() -> int:
    warn_legacy("bh-after-run", "codereeve hook after-run")
    return cli.main(["hook", "after-run", *sys.argv[1:]])


def force_pr_not_merge_main() -> int:
    warn_legacy(
        "bh-force-pr-not-merge",
        "codereeve hook force-pr-not-merge",
    )
    return cli.main(["hook", "force-pr-not-merge", *sys.argv[1:]])


def verify_foundation_main() -> int:
    warn_legacy("bh-verify-foundation", "codereeve verify")
    return cli.main(["verify", *sys.argv[1:]])
```

Each adapter warns once, then calls the canonical router with the same effective arguments/stdin contract.

- [ ] **Step 4: Implement import-only compatibility modules**

Create `src/baton_harness/__init__.py` exactly as:

```python
"""Temporary import compatibility for :mod:`codereeve`."""

import warnings

from codereeve import __version__

warnings.warn(
    "baton_harness is deprecated; use codereeve; removed in 0.4.0",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["__version__"]
```

Create the six other compatibility modules with these exact exports:

```python
# src/baton_harness/after_create.py
from codereeve.after_create import main

# src/baton_harness/before_run.py
from codereeve.before_run import main

# src/baton_harness/after_run.py
from codereeve.after_run import main

# src/baton_harness/chain/cli.py
from codereeve.chain.cli import main

# src/baton_harness/hooks/force_pr_not_merge.py
from codereeve.hooks.force_pr_not_merge import main

# src/baton_harness/verify_foundation.py
from codereeve.verify_foundation import main
```

Add `__all__ = ["main"]` to each module and create empty docstring-only `src/baton_harness/chain/__init__.py` and `src/baton_harness/hooks/__init__.py`. Do not copy constants, classes, or implementation code. The supported compatibility list is limited to these exact files.

- [ ] **Step 5: Point legacy console names directly at adapters**

Set the complete script table in `pyproject.toml`:

```toml
[project.scripts]
codereeve = "codereeve.cli:main"
bh-after-create = "codereeve.legacy_cli:after_create_main"
bh-before-run = "codereeve.legacy_cli:before_run_main"
bh-after-run = "codereeve.legacy_cli:after_run_main"
bh-daemon = "codereeve.legacy_cli:daemon_main"
bh-force-pr-not-merge = "codereeve.legacy_cli:force_pr_not_merge_main"
bh-verify-foundation = "codereeve.legacy_cli:verify_foundation_main"

[tool.hatch.build.targets.wheel]
packages = ["src/codereeve", "src/baton_harness"]
```

- [ ] **Step 6: Sync and run compatibility tests**

Run:

```bash
CODEREEVE_BUILD_DEVELOPMENT=1 uv sync --locked --extra dev
./.venv/Scripts/python.exe -m pytest tests/test_legacy_package_shims.py tests/test_codereeve_cli.py tests/test_smoke.py -v
```

Expected: all tests pass; canonical invocations are silent and each legacy console adapter emits exactly one notice per process.

- [ ] **Step 7: Commit compatibility shims**

```bash
git add pyproject.toml src/baton_harness src/codereeve/legacy_cli.py tests/test_legacy_package_shims.py
git commit -m "feat(#392): retain temporary Baton compatibility shims"
```

---

### Task 4: Migrate frozen-artifact and installed-entry-point verification

**Files:**
- Modify: `src/codereeve/verify_foundation.py`
- Modify: `tests/test_verify_foundation.py`
- Modify: `tests/test_build_provenance.py`
- Modify: `tests/test_provenance.py`

**Interfaces:**
- Consumes: the canonical/legacy script table from Task 3 and provenance record from Task 1.
- Produces: `CANONICAL_ENTRY_POINTS`, `LEGACY_ENTRY_POINTS`, `EXPECTED_ENTRY_POINTS`, canonical artifact inspection, canonical installed smokes, and separate compatibility smokes. The existing verifier already owns wheel entry-point and installed CLI validation (`src/baton_harness/verify_foundation.py:L28-L47`, `src/baton_harness/verify_foundation.py:L416-L481`).

- [ ] **Step 1: Write failing artifact-contract tests**

Update `tests/test_verify_foundation.py` to define wheel/sdist fixtures with:

```python
CANONICAL_ENTRY_POINTS = frozenset({"codereeve"})
LEGACY_ENTRY_POINTS = frozenset(
    {
        "bh-after-create",
        "bh-before-run",
        "bh-after-run",
        "bh-daemon",
        "bh-force-pr-not-merge",
        "bh-verify-foundation",
    }
)
```

Change package members to `codereeve/build_provenance.json`, source members to `source/src/codereeve/build_provenance.json`, and metadata directories to `codereeve-VERSION.dist-info`. Add tests that fail when:

- the canonical `codereeve` entry point is missing;
- any compatibility entry point is missing in 0.2;
- provenance exists only under `baton_harness/`;
- `codereeve` imports outside the candidate environment;
- canonical CLI smokes do not include `--version`, `provenance`, `doctor --phase installation --format json --strict`, every hook, and safe verify help;
- a legacy adapter does not emit its removal notice on stderr.
- the installed distributions contain both normalized names `codereeve` and `baton-harness`, even when all canonical entry points are present.

- [ ] **Step 2: Run the verifier tests to verify the red state**

Run:

```bash
./.venv/Scripts/python.exe -m pytest tests/test_verify_foundation.py tests/test_build_provenance.py tests/test_provenance.py -v
```

Expected: failures name old artifact paths, old distribution metadata, and missing canonical entry-point smokes.

- [ ] **Step 3: Implement canonical and compatibility verification sets**

In `src/codereeve/verify_foundation.py` define:

```python
CANONICAL_ENTRY_POINTS = frozenset({"codereeve"})
LEGACY_ENTRY_POINTS = frozenset(
    {
        "bh-after-create",
        "bh-before-run",
        "bh-after-run",
        "bh-daemon",
        "bh-force-pr-not-merge",
        "bh-verify-foundation",
    }
)
EXPECTED_ENTRY_POINTS = CANONICAL_ENTRY_POINTS | LEGACY_ENTRY_POINTS
INCOMPATIBLE_DISTRIBUTIONS = frozenset({"baton-harness"})
```

Require `codereeve/build_provenance.json` in the wheel/installation and `src/codereeve/build_provenance.json` in the sdist. Resolve installed metadata with the normalized distribution name `codereeve`. Validate that the imported `codereeve` package resides inside the candidate environment.

Extend `_validate_installed_state` with an `incompatible_distributions` argument. Normalize installed names and fail with `FoundationError("incompatible distributions installed: baton-harness")` when the intersection is non-empty. `verify_installed` must always pass `INCOMPATIBLE_DISTRIBUTIONS`; keep the existing caller-provided `forbidden_distributions` check for development-only dependencies as a separate invariant.

Split entry-point smoke data into:

```python
CANONICAL_SMOKES = (
    ("codereeve", ("--version",), None, 0),
    ("codereeve", ("provenance",), None, 0),
    (
        "codereeve",
        ("doctor", "--phase", "installation", "--format", "json", "--strict"),
        None,
        0,
    ),
    ("codereeve", ("hook", "force-pr-not-merge"), "{}", 0),
)
```

Add the three lifecycle hook smokes using safe injected environments, plus `codereeve verify --help`. Keep `codereeve verify` from recursively running the full build inside installed-smoke mode. Exercise legacy entry points separately and require the deprecation notice only on stderr so JSON stdout remains parseable.

- [ ] **Step 4: Run focused verifier tests**

Run:

```bash
./.venv/Scripts/python.exe -m pytest tests/test_verify_foundation.py tests/test_build_provenance.py tests/test_provenance.py -v
```

Expected: all focused verifier and provenance tests pass.

- [ ] **Step 5: Commit artifact verification**

```bash
git add src/codereeve/verify_foundation.py tests/test_verify_foundation.py tests/test_build_provenance.py tests/test_provenance.py
git commit -m "test(#392): verify canonical and compatibility artifacts"
```

---

### Task 5: Update #392-owned packaging and CLI documentation

**Files:**
- Modify: `README.md`
- Modify: `pyproject.toml`
- Test: `tests/test_repository_workflow_contract.py`
- Test: `tests/test_setup_env_bws_optional.py`

**Interfaces:**
- Consumes: the final command and build-variable names from Tasks 1-4.
- Produces: accurate canonical install/build/development/CLI examples and a clearly marked 0.2/0.3 compatibility table. The README currently documents the old package and commands as canonical (`README.md:L22-L23`, `README.md:L140-L171`, `README.md:L242-L279`).

- [ ] **Step 1: Add a failing documentation-contract test**

Add to `tests/test_repository_workflow_contract.py`:

```python
def test_readme_presents_codereeve_as_canonical_cli() -> None:
    text = Path("README.md").read_text(encoding="utf-8")
    assert "codereeve daemon" in text
    assert "codereeve doctor" in text
    assert "codereeve provenance" in text
    assert "codereeve hook after-create" in text
    assert "codereeve verify" in text
    assert "removed in 0.4.0" in text
```

- [ ] **Step 2: Run the documentation test to verify the red state**

Run:

```bash
./.venv/Scripts/python.exe -m pytest tests/test_repository_workflow_contract.py::test_readme_presents_codereeve_as_canonical_cli -v
```

Expected: failure because the README still presents `bh-*` commands as canonical.

- [ ] **Step 3: Update only packaging, development, and command documentation**

Update README sections for:

- distribution/package name and editable installation;
- `CODEREEVE_BUILD_VERSION`, `CODEREEVE_BUILD_SOURCE_REVISION`, and `CODEREEVE_BUILD_DEVELOPMENT`, with `BH_BUILD_*` listed only as temporary compatibility;
- wheel/sdist names and provenance examples;
- the unified command table;
- the six legacy commands, their canonical replacements, and 0.4.0 removal.
- the supported upgrade sequence: build or obtain the 0.2 wheel, create a new `.venv-codereeve`, install and run `codereeve verify` there, switch service configuration only after verification, and retain the original `.venv` unchanged until rollback is no longer required.

Leave the comprehensive environment, path, service, historical-design, and operator-documentation sweep to #393-#395. Update stale `pyproject.toml` comments so they describe the unified canonical executable and compatibility scripts accurately.

- [ ] **Step 4: Run documentation and setup contract tests**

Run:

```bash
./.venv/Scripts/python.exe -m pytest tests/test_repository_workflow_contract.py tests/test_setup_env_bws_optional.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit packaging documentation**

```bash
git add README.md pyproject.toml tests/test_repository_workflow_contract.py
git commit -m "docs(#392): document canonical CodeReeve packaging and CLI"
```

---

### Task 6: Build and verify the complete #392 deliverable

**Files:**
- Verify: every file changed by Tasks 1-5
- Verify: `docs/superpowers/specs/2026-09-07-codereeve-rename-design.md`
- Verify: issue #392 acceptance criteria

**Interfaces:**
- Consumes: all prior task outputs.
- Produces: review-ready #392 branch with canonical source/artifacts/CLI and temporary, tested compatibility wrappers.

- [ ] **Step 1: Run exact identity audits**

Run:

```bash
rg -n "from baton_harness|import baton_harness|patch\([\"']baton_harness" src/codereeve tests
rg -n "baton-harness|baton_harness|bh-" pyproject.toml hatch_build.py src/codereeve
```

Expected: the first command returns no matches. Every match from the second command is individually classified as compatibility implementation/test data, a migration/removal message, or preserved Symphony/Baton provenance; canonical implementation paths contain no accidental old identity.

- [ ] **Step 2: Run formatting, lint, and type checks**

Run:

```bash
./.venv/Scripts/python.exe -m ruff format --check src tests hatch_build.py
./.venv/Scripts/python.exe -m ruff check src tests hatch_build.py
./.venv/Scripts/python.exe -m mypy src hatch_build.py
```

Expected: all commands exit 0.

- [ ] **Step 3: Run the complete test suite**

Create the ignored temp parent if absent, then run:

```bash
mkdir -p .tmp
./.venv/Scripts/python.exe -m pytest --basetemp .tmp/pytest-392-final -q
```

Expected: all tests pass except that the known non-Git-directory characterization may require its exact test to be rerun with `--basetemp` outside the repository. Record both outputs if that host-specific condition occurs; do not suppress or xfail it.

- [ ] **Step 4: Build wheel and sdist with canonical identity**

Use the current 40-character `HEAD` revision as the source assertion:

```bash
CODE_REEVISION=$(git rev-parse HEAD)
CODEREEVE_BUILD_VERSION=0.2.0 \
CODEREEVE_BUILD_SOURCE_REVISION="$CODE_REEVISION" \
uv build --wheel --sdist --out-dir .tmp/dist-392
```

Expected artifacts are `codereeve-0.2.0-*.whl` and `codereeve-0.2.0.tar.gz`; build output contains no `baton_harness-*.whl` or `baton-harness` metadata.

- [ ] **Step 5: Run the foundation verifier against built artifacts**

Run the canonical verifier command. It builds and inspects fresh wheel/sdist artifacts, rebuilds the wheel from the sdist, and smoke-tests installed entry points on the supported Python versions:

```bash
CODEREEVE_BUILD_VERSION=0.2.0 \
CODEREEVE_BUILD_SOURCE_REVISION="$CODE_REEVISION" \
./.venv/Scripts/codereeve.exe verify --python 3.10 --python 3.13
```

Expected: exit 0 after artifact provenance, locked installation, resource, canonical entry-point, and compatibility entry-point checks.

- [ ] **Step 6: Audit the commit and artifact boundary**

Run:

```bash
git status --short
git diff main...HEAD --stat
git ls-tree HEAD -- src/codereeve src/baton_harness pyproject.toml hatch_build.py uv.lock
```

Expected: no uncommitted source changes; both package trees and every referenced source artifact are committed; `.tmp/`, wheels, sdists, and generated provenance records are not committed.

- [ ] **Step 7: Commit any verification-only corrections**

If verification required tracked corrections, repeat the exact failing command after the fix, then commit only those corrections:

```bash
git add -u
git commit -m "fix(#392): satisfy CodeReeve artifact verification"
```

If no tracked correction was needed, do not create an empty commit.

- [ ] **Step 8: Request code review before PR creation**

Invoke `superpowers:requesting-code-review`. Resolve actionable findings, rerun the affected checks, then perform the repository-required PR artifact-persistence audit. The final PR to the 0.2 primary branch must reference #392; the later primary PR to `main` must carry a separate `Closes #392` directive so squash merging closes the issue.
