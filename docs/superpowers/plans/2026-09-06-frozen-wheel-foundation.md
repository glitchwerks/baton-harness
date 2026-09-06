# Frozen Wheel Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a locked, non-editable wheel installation whose runtime defaults and six console commands work without a repository checkout.

**Architecture:** `uv.lock` is the sole dependency authority. Canonical defaults live in `baton_harness.resources`, with byte-identical `config/` mirrors retained for shell consumers. `bh-verify-foundation` centralizes repository, wheel, and installed-environment invariants and runs inside the existing pytest CI check.

**Tech Stack:** Python 3.10+, `importlib.resources`, `tomllib`, `zipfile`, uv, Hatchling, pytest, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-06-frozen-wheel-foundation-design.md`

## Global Constraints

- Keep `requires-python = ">=3.10"`; verify Python 3.10 and 3.13 (`pyproject.toml:L9-L30`; #360).
- `uv.lock` is the only committed dependency lock; validation must never rewrite it. https://docs.astral.sh/uv/concepts/projects/sync/ (fetched 2026-09-06)
- Canonical defaults live in `src/baton_harness/resources/`; `config/` stays byte-identical until #378–#381.
- Production installs the wheel non-editably with runtime dependencies only; development remains editable (#360).
- Preserve the existing five scripts (`pyproject.toml:L32-L51`) and add only `bh-verify-foundation`.
- Keep CI check names stable by invoking the validator in `Test (pytest)` (`.github/workflows/ci.yml:L45-L55`).
- Follow red-green-refactor for every behavior change.

---

### Task 1: Canonical packaged resources

**Files:**
- Create: `src/baton_harness/resources/__init__.py`
- Create: `src/baton_harness/resources/WORKFLOW.md`
- Create: `src/baton_harness/resources/ruleset.main.json`
- Create: `src/baton_harness/resources/ruleset.feature.json`
- Create: `src/baton_harness/resources/ruleset.compare-keys.json`
- Create: `src/baton_harness/resources/ruleset.compare-keys.app.json`
- Create: `tests/test_resources.py`

**Interfaces:**
- Produces: `RESOURCE_NAMES: tuple[str, ...]`, `PackagedResourceError`.
- Produces: `resource(name: str) -> Traversable`, `read_bytes(name: str) -> bytes`, `read_text(name: str) -> str`, `as_path(name: str) -> AbstractContextManager[Path]`.
- Produces: `assert_config_mirrors(root: Path) -> None`.

- [ ] **Step 1: Write the failing resource tests**

```python
REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED = (
    "WORKFLOW.md",
    "ruleset.main.json",
    "ruleset.feature.json",
    "ruleset.compare-keys.json",
    "ruleset.compare-keys.app.json",
)


def test_packaged_resource_manifest_is_complete() -> None:
    assert RESOURCE_NAMES == EXPECTED
    assert all(resource(name).is_file() for name in EXPECTED)


def test_packaged_resources_match_mirrors() -> None:
    assert_config_mirrors(REPO_ROOT)
    assert all(
        read_bytes(name) == (REPO_ROOT / "config" / name).read_bytes()
        for name in EXPECTED
    )


def test_missing_mirror_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(PackagedResourceError, match="missing config mirror"):
        assert_config_mirrors(tmp_path)
```

- [ ] **Step 2: Verify RED**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_resources.py -q`

Expected: collection fails because `baton_harness.resources` is absent.

- [ ] **Step 3: Implement the resource API**

```python
RESOURCE_NAMES = (
    "WORKFLOW.md",
    "ruleset.main.json",
    "ruleset.feature.json",
    "ruleset.compare-keys.json",
    "ruleset.compare-keys.app.json",
)


class PackagedResourceError(RuntimeError):
    """Raised when a required packaged resource invariant is broken."""


def resource(name: str) -> Traversable:
    if name not in RESOURCE_NAMES:
        raise PackagedResourceError(f"unknown packaged resource: {name}")
    candidate = files(__package__).joinpath(name)
    if not candidate.is_file():
        raise PackagedResourceError(f"packaged resource missing: {name}")
    return candidate


def assert_config_mirrors(root: Path) -> None:
    for name in RESOURCE_NAMES:
        mirror = root / "config" / name
        if not mirror.is_file():
            raise PackagedResourceError(f"missing config mirror: {mirror}")
        if mirror.read_bytes() != resource(name).read_bytes():
            raise PackagedResourceError(f"config mirror drift: {mirror}")
```

Implement `read_bytes`, `read_text`, and `as_path` as direct wrappers around `resource`. Copy the current five `config/` files byte-for-byte into the resource package. Python specifies `files()`/`Traversable` for package data and `as_file()` when a real path is required. https://docs.python.org/3/library/importlib.resources.html (fetched 2026-09-06)

- [ ] **Step 4: Verify GREEN and quality gates**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_resources.py -q`

Run: `./.venv/Scripts/python.exe -m ruff check src/baton_harness/resources tests/test_resources.py`

Run: `./.venv/Scripts/python.exe -m mypy src/baton_harness/resources`

Expected: all commands pass.

- [ ] **Step 5: Commit**

```bash
git add src/baton_harness/resources tests/test_resources.py
git commit -m "feat(#360): package canonical runtime resources"
```

---

### Task 2: Load defaults from the installed package

**Files:**
- Modify: `src/baton_harness/chain/cli.py:151-165,215-227,319-336`
- Modify: `src/baton_harness/chain/ruleset_status.py:67-90,159-220,298-330`
- Modify: `tests/chain/test_cli.py:1-10,276-332`
- Modify: `tests/test_ruleset_status.py:730-930,1312-1320`

**Interfaces:**
- Consumes: `as_path` and `resource` from Task 1.
- Produces: `_workflow_path(workflow: str | None) -> AbstractContextManager[Path]`.
- Preserves: ruleset constants remain monkeypatchable with `Path` test fixtures.

- [ ] **Step 1: Write failing default-loading tests**

```python
def test_default_workflow_comes_from_package() -> None:
    with cli._workflow_path(None) as path:
        assert path.read_bytes() == read_bytes("WORKFLOW.md")


def test_explicit_workflow_remains_absolute(tmp_path: Path) -> None:
    supplied = tmp_path / "WORKFLOW.md"
    supplied.write_text("---\n---\n", encoding="utf-8")
    with cli._workflow_path(str(supplied)) as path:
        assert path == supplied.resolve()
```

Add ruleset assertions that `_MAIN_CFG`, `_FEATURE_CFG`, `_COMPARE_KEYS_CFG`, and `_COMPARE_KEYS_APP_CFG` read the matching packaged bytes. Keep existing missing/malformed monkeypatched-`Path` tests.

- [ ] **Step 2: Verify RED**

Run: `./.venv/Scripts/python.exe -m pytest tests/chain/test_cli.py tests/test_ruleset_status.py -q`

Expected: new assertions fail because both modules still derive a checkout root.

- [ ] **Step 3: Implement package-based loading**

```python
def _workflow_path(workflow: str | None) -> AbstractContextManager[Path]:
    if workflow:
        return nullcontext(Path(workflow).resolve())
    return as_path("WORKFLOW.md")
```

Load workflow configuration inside the returned context so extracted files remain alive. Replace four ruleset constants with `resource(<name>)`. Change `_load_keys_from_path` to a protocol exposing `is_file()` and `read_text()` instead of requiring `Path.exists()`. Update help/error text to name the packaged default.

- [ ] **Step 4: Verify GREEN and regression gates**

Run: `./.venv/Scripts/python.exe -m pytest tests/chain/test_cli.py tests/test_ruleset_status.py tests/test_resources.py -q`

Run: `./.venv/Scripts/python.exe -m ruff check src/baton_harness/chain/cli.py src/baton_harness/chain/ruleset_status.py tests/chain/test_cli.py tests/test_ruleset_status.py`

Run: `./.venv/Scripts/python.exe -m mypy src`

Expected: all commands pass.

- [ ] **Step 5: Commit**

```bash
git add src/baton_harness/chain/cli.py src/baton_harness/chain/ruleset_status.py tests/chain/test_cli.py tests/test_ruleset_status.py
git commit -m "feat(#360): load defaults from package resources"
```

---

### Task 3: Lock dependencies and editable setup

**Files:**
- Create: `uv.lock`
- Create: `tests/test_dependency_lock_contract.py`
- Modify: `.github/actions/setup/action.yml:1-27`
- Modify: `bin/setup-env.sh:42-51,432-446`
- Modify: `README.md:130-185`
- Modify: `docs/system-setup.md:60-85`

**Interfaces:**
- Produces: one universal lock containing base and optional dependencies.
- Produces: `uv sync --locked --extra dev` as the CI/developer setup command.
- Preserves: editable project installation in `.venv`.

- [ ] **Step 1: Write failing lock-contract tests**

```python
REPO_ROOT = Path(__file__).resolve().parents[1]


def test_uv_lock_exists() -> None:
    lock = REPO_ROOT / "uv.lock"
    assert lock.is_file()
    assert 'name = "baton-harness"' in lock.read_text(encoding="utf-8")


def test_setup_paths_use_locked_sync() -> None:
    action = (REPO_ROOT / ".github/actions/setup/action.yml").read_text()
    setup = (REPO_ROOT / "bin/setup-env.sh").read_text()
    assert "uv sync --locked --extra dev" in action
    assert "--locked --extra dev" in setup
    assert 'uv pip install -e ".[dev]"' not in action
```

- [ ] **Step 2: Verify RED**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_dependency_lock_contract.py -q`

Expected: `uv.lock` and locked-sync assertions fail.

- [ ] **Step 3: Generate the lock and change setup paths**

Run: `uv lock`

Run: `uv lock --check`

Replace the composite action install step with `uv sync --locked --extra dev`. Retain explicit venv creation in `bin/setup-env.sh`, then run `uv sync --project "${BATON_HARNESS_DIR}" --locked --extra dev`. Update README/setup text to call it a locked editable developer environment. uv resolves optional dependencies into the universal lock and installs selected extras with `--extra`. https://docs.astral.sh/uv/concepts/resolution/ (fetched 2026-09-06); https://docs.astral.sh/uv/concepts/projects/sync/ (fetched 2026-09-06)

- [ ] **Step 4: Verify GREEN**

Run: `uv sync --locked --extra dev`

Run: `./.venv/Scripts/python.exe -m pytest tests/test_dependency_lock_contract.py tests/test_setup_env_bws_optional.py -q`

Expected: sync leaves the lock unchanged and tests pass.

- [ ] **Step 5: Commit**

```bash
git add uv.lock .github/actions/setup/action.yml bin/setup-env.sh README.md docs/system-setup.md tests/test_dependency_lock_contract.py
git commit -m "build(#360): enforce locked development installs"
```

---

### Task 4: Add `bh-verify-foundation`

**Files:**
- Create: `src/baton_harness/verify_foundation.py`
- Create: `tests/test_verify_foundation.py`
- Modify: `pyproject.toml:32-58`

**Interfaces:**
- Consumes: Task 1 resource manifest and mirror assertion.
- Produces: `EXPECTED_ENTRY_POINTS: frozenset[str]`, `FoundationError`.
- Produces: `inspect_wheel(wheel: Path) -> None`, `verify_repository(root: Path, python_versions: Sequence[str], runner: Runner = run_command) -> None`, `verify_installed() -> None`, `main(argv: Sequence[str] | None = None) -> int`.
- CLI: repeated `--python`; defaults to `3.10` and `3.13`; `--keep-temp`; hidden `--installed-smoke`.

- [ ] **Step 1: Write failing parsing and archive tests**

```python
def test_cli_defaults_to_floor_and_313(monkeypatch) -> None:
    called = []
    monkeypatch.setattr(
        verify_foundation,
        "verify_repository",
        lambda root, python_versions, **kwargs: called.append(tuple(python_versions)),
    )
    assert verify_foundation.main([]) == 0
    assert called == [("3.10", "3.13")]


def test_wheel_without_resource_fails(tmp_path: Path) -> None:
    wheel = make_wheel(tmp_path, resource_names=())
    with pytest.raises(FoundationError, match="wheel resource missing"):
        inspect_wheel(wheel)
```

The ZIP helper writes configurable resource members and `.dist-info/entry_points.txt`; it does not invoke the build backend.

- [ ] **Step 2: Verify RED**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_verify_foundation.py -q`

Expected: collection fails because the module is absent.

- [ ] **Step 3: Implement parsing, errors, and archive checks**

```python
EXPECTED_ENTRY_POINTS = frozenset({
    "bh-after-create", "bh-before-run", "bh-after-run", "bh-daemon",
    "bh-force-pr-not-merge", "bh-verify-foundation",
})


def inspect_wheel(wheel: Path) -> None:
    with ZipFile(wheel) as archive:
        names = archive.namelist()
        for resource_name in RESOURCE_NAMES:
            suffix = f"baton_harness/resources/{resource_name}"
            if sum(name.endswith(suffix) for name in names) != 1:
                raise FoundationError(f"wheel resource missing or duplicated: {resource_name}")
        missing = EXPECTED_ENTRY_POINTS - _wheel_entry_points(archive)
        if missing:
            raise FoundationError(f"wheel entry points missing: {sorted(missing)}")
```

Add `bh-verify-foundation = "baton_harness.verify_foundation:main"` to `[project.scripts]`.

- [ ] **Step 4: Verify archive GREEN**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_verify_foundation.py -q`

Expected: parsing and archive tests pass.

- [ ] **Step 5: Write failing orchestration tests**

Use a recording runner and assert this order for one requested interpreter:

```text
uv lock --check
uv build --locked --sdist --wheel --out-dir <temp>
uv export --locked --no-emit-project --format requirements.txt --output-file <temp>
uv venv <temp-venv> --python 3.10
uv pip sync --python <temp-python> <requirements>
uv pip install --python <temp-python> --no-deps <wheel>
<temp-verifier> --installed-smoke
```

Assert `PYTHONPATH` is removed, smoke cwd is outside the repository, interpreter order is preserved, and any non-zero command stops with `FoundationError` containing stderr.

- [ ] **Step 6: Verify orchestration RED**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_verify_foundation.py -q`

Expected: orchestration assertions fail because repository verification is absent.

- [ ] **Step 7: Implement repository orchestration**

Use temporary build/environment directories, platform-specific venv executable paths, and injected `subprocess.run`. Call `assert_config_mirrors` before building, require exactly one wheel, inspect it, then perform the exact locked export/install sequence for each interpreter. `uv build --sdist --wheel` produces the wheel from the sdist. https://docs.astral.sh/uv/concepts/projects/build/ (fetched 2026-09-06)

- [ ] **Step 8: Write failing installed-smoke tests**

Independently assert `verify_installed` rejects an editable `direct_url.json`, a package imported outside `sys.prefix`, a missing resource, a missing console entry point, and a dev-only installed distribution. Add one complete success case asserting all five safe wrapper invocations occur.

- [ ] **Step 9: Verify installed-smoke RED**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_verify_foundation.py -q`

Expected: installed-state assertions fail because `verify_installed` is absent.

- [ ] **Step 10: Implement installed smoke**

Use `importlib.metadata.distribution("baton-harness")`; reject editable metadata and imports outside `sys.prefix`; load all five package resources; require all six entry points. Invoke `bh-daemon --help`, invoke the lifecycle hooks from an unresolvable temporary directory expecting their documented early exit, and feed `{}` to `bh-force-pr-not-merge` expecting zero (`src/baton_harness/after_create.py:L394-L420`; `src/baton_harness/before_run.py:L104-L135`; `src/baton_harness/after_run.py:L615-L647`). Pass the dev-only distribution names through a temporary JSON manifest and reject those not shared by the runtime closure.

- [ ] **Step 11: Verify GREEN and quality gates**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_verify_foundation.py tests/test_resources.py -q`

Run: `./.venv/Scripts/python.exe -m ruff check src/baton_harness/verify_foundation.py tests/test_verify_foundation.py`

Run: `./.venv/Scripts/python.exe -m mypy src`

Expected: all commands pass.

- [ ] **Step 12: Commit**

```bash
git add pyproject.toml src/baton_harness/verify_foundation.py tests/test_verify_foundation.py
git commit -m "feat(#360): add frozen foundation verifier"
```

---

### Task 5: CI, production docs, and final verification

**Files:**
- Modify: `.github/workflows/ci.yml:45-55`
- Modify: `README.md:130-185,240-252`
- Modify: `docs/system-setup.md:60-100`
- Modify: `tests/test_dependency_lock_contract.py`

**Interfaces:**
- Consumes: installed `bh-verify-foundation` from Task 4.
- Produces: merge-blocking validation inside the existing `Test (pytest)` job.
- Produces: documented editable-development and non-editable-production workflows.

- [ ] **Step 1: Extend failing CI/documentation contract tests**

```python
def test_pytest_job_runs_foundation_verifier() -> None:
    workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text()
    assert "name: Verify frozen wheel foundation" in workflow
    assert "run: .venv/bin/bh-verify-foundation" in workflow


def test_docs_distinguish_install_modes() -> None:
    for relative in ("README.md", "docs/system-setup.md"):
        body = (REPO_ROOT / relative).read_text()
        assert "uv sync --locked --extra dev" in body
        assert "bh-verify-foundation" in body
        assert "non-editable" in body.lower()
        assert "runtime dependencies only" in body.lower()
```

- [ ] **Step 2: Verify RED**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_dependency_lock_contract.py -q`

Expected: CI and production-documentation assertions fail.

- [ ] **Step 3: Wire CI and documentation**

Add this step immediately after pytest without creating or renaming a job:

```yaml
- name: Verify frozen wheel foundation
  run: .venv/bin/bh-verify-foundation
```

Document `uv sync --locked --extra dev` as editable development and `bh-verify-foundation` as the full lock/mirror/wheel/Python 3.10+3.13 production proof. State that production installation is non-editable and contains runtime dependencies only.

- [ ] **Step 4: Verify contract GREEN**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_dependency_lock_contract.py -q`

Expected: all contract tests pass.

- [ ] **Step 5: Run the real foundation validator**

Run: `./.venv/Scripts/bh-verify-foundation.exe`

Expected: lock, mirrors, wheel, both Python versions, installed resources, runtime-only dependency set, and all six entry points pass outside the checkout.

- [ ] **Step 6: Run the complete project gates**

Run: `./.venv/Scripts/python.exe -m ruff check .`

Run: `./.venv/Scripts/python.exe -m ruff format --check .`

Run: `./.venv/Scripts/python.exe -m mypy src`

Run: `./.venv/Scripts/python.exe -m pytest -q`

Run: `bash -n bin/*.sh bin/lib/*.sh`

Expected: all commands pass. Pre-#360 baseline: 1742 passed, 22 skipped, plus one non-failing restricted-environment pytest-cache warning.

- [ ] **Step 7: Audit artifact persistence**

Run: `git diff main...HEAD --stat`

Run: `git ls-tree HEAD -- uv.lock src/baton_harness/resources/WORKFLOW.md src/baton_harness/resources/ruleset.main.json src/baton_harness/resources/ruleset.feature.json src/baton_harness/resources/ruleset.compare-keys.json src/baton_harness/resources/ruleset.compare-keys.app.json`

Expected: every referenced resource and lock is committed.

- [ ] **Step 8: Commit**

```bash
git add .github/workflows/ci.yml README.md docs/system-setup.md tests/test_dependency_lock_contract.py
git commit -m "ci(#360): enforce frozen wheel verification"
```

- [ ] **Step 9: Confirm branch readiness without pushing**

Run: `git status --short --branch`

Run: `git log --oneline main..HEAD`

Expected: a clean #360 branch containing the design and implementation commits. Verify live PR state before any later push, per project policy.
