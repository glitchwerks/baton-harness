# Runtime Provenance and Machine-Readable Preflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship verifiable build/runtime provenance plus phase-selectable, secret-safe
doctor JSON without requiring a live checkout or credentials for installation checks.

**Architecture:** An in-repository Hatch metadata/build hook validates explicit build
identity and injects one immutable JSON record. Runtime code validates that record
against installed distribution metadata. Doctor keeps one catalog and execution
engine, with a pure config resolver, a separate report renderer, and three public
phases shared by standalone and daemon startup paths.

**Tech Stack:** Python 3.10+, Hatchling custom hooks, uv universal lock,
`importlib.metadata`/`importlib.resources`, argparse, dataclasses, JSON, pytest, Ruff,
and mypy.

**Spec:**
`docs/superpowers/specs/2026-09-06-runtime-provenance-preflight-design.md`

## Global Constraints

- Keep Python 3.10 as the floor and add no runtime dependency
  (`pyproject.toml:L9-L31`; design spec:L323-L337).
- Standard builds require `BH_BUILD_VERSION` and a 40-hex-character
  `BH_BUILD_SOURCE_REVISION`; editable builds require
  `BH_BUILD_DEVELOPMENT=1` and use stable version `0.1.0.dev0`
  (design spec:L67-L110).
- Record exact `uv.lock` bytes as `sha256:` followed by 64 lowercase hex characters,
  and keep exact commit
  identity in `source_revision`, never in the editable version
  (design spec:L84-L108).
- Production releases may derive explicit identity from immutable release tags;
  development checkouts do not depend on a moving tag (design spec:L102-L110).
- Runtime provenance never reads `.git`; missing, malformed, unsupported, or
  version-inconsistent records fail closed (design spec:L153-L167).
- Installation checks remain offline and credential-free. Configuration checks are
  local-only. Credential or network activity belongs to `live`
  (design spec:L169-L186).
- Standalone doctor is advisory unless `--strict`; daemon startup is fail-closed on
  critical results (design spec:L200-L225).
- Use the project interpreter `./.venv/Scripts/python.exe`. Give pytest a repo-local
  `I:` basetemp because the shell-script tests cannot use a Git-Bash-converted
  `C:`-based `HOME` (baseline evidence from this branch: 1,780 passed, 22 skipped).
- Follow red-green-refactor for every production change and commit each independently
  reviewable task (#358; design spec:L296-L321).

---

### Task 1: Build-Time Provenance Authority

**Files:**

- Create: `hatch_build.py`
- Create: `tests/test_build_provenance.py`
- Modify: `pyproject.toml:1-12`
- Modify: `pyproject.toml:59-60`
- Modify: `uv.lock:74-76`

**Interfaces:**

- Produces: `BuildProvenanceError`, immutable `BuildProvenance`,
  `lock_identity(Path) -> str`, and
  `resolve_build_provenance(root: Path, env: Mapping[str, str], *,
  read_head: Callable[[Path], str]) -> BuildProvenance`.
- Produces: Hatch-discovered `CustomMetadataHook` and `CustomBuildHook` classes.
- Produces: generated `src/baton_harness/build_provenance.json` in sdists and
  `baton_harness/build_provenance.json` in standard/editable wheels.
- Consumes: exact `uv.lock` bytes and explicit build environment described in the
  global constraints (design spec:L67-L151).

- [ ] **Step 1: Write failing unit tests for identity resolution.**

Add tests with a fixed revision and lock payload; assert the entire dataclass so every
schema field is pinned:

```python
REVISION = "0123456789abcdef0123456789abcdef01234567"


def test_standard_identity_requires_and_preserves_assertions(
    tmp_path: Path,
) -> None:
    (tmp_path / "uv.lock").write_bytes(b"locked\n")
    identity = resolve_build_provenance(
        tmp_path,
        {
            "BH_BUILD_VERSION": "1.2.3",
            "BH_BUILD_SOURCE_REVISION": REVISION.upper(),
        },
        read_head=lambda _root: REVISION,
    )
    assert identity == BuildProvenance(
        schema_version=1,
        package_version="1.2.3",
        source_revision=REVISION,
        lock_identity=f"sha256:{sha256(b'locked\n').hexdigest()}",
        development=False,
    )


def test_development_identity_uses_stable_version_and_exact_head(
    tmp_path: Path,
) -> None:
    (tmp_path / "uv.lock").write_bytes(b"locked\n")
    identity = resolve_build_provenance(
        tmp_path,
        {"BH_BUILD_DEVELOPMENT": "1"},
        read_head=lambda _root: REVISION,
    )
    assert identity.package_version == "0.1.0.dev0"
    assert identity.source_revision == REVISION
    assert identity.development is True
```

Add parametrized failures for absent assertions, non-PEP-440 version, short/non-hex
revision, missing lock, non-`1` development flag, Git mismatch, and standard inputs
combined with the development flag. Add a carried-record test that writes
`src/baton_harness/build_provenance.json` with one changed field and expects
`BuildProvenanceError`.

- [ ] **Step 2: Run the new tests and verify the missing module fails.**

Run:

```bash
./.venv/Scripts/python.exe -m pytest -q tests/test_build_provenance.py --basetemp='I:/ai/claude/baton-harness/.tmp/pytest-358-build-red'
```

Expected: collection fails because `hatch_build` does not exist.

- [ ] **Step 3: Implement the pure identity functions and strict record comparison.**

Use this public shape and keep all subprocess access behind the injected `read_head`
seam:

```python
DEVELOPMENT_VERSION = "0.1.0.dev0"
REVISION_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")


@dataclass(frozen=True)
class BuildProvenance:
    schema_version: int
    package_version: str
    source_revision: str
    lock_identity: str
    development: bool

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def lock_identity(lock_path: Path) -> str:
    try:
        content = lock_path.read_bytes()
    except OSError as exc:
        raise BuildProvenanceError(f"cannot read lock file: {lock_path}") from exc
    return f"sha256:{sha256(content).hexdigest()}"
```

Validate versions with `packaging.version.Version`, which is available in the isolated
Hatch build environment. Detect a checkout by `.git` file or directory; when present,
require the injected/read `HEAD` to equal the standard assertion. If the carried JSON
exists, require exactly the five schema keys and exact equality with `as_dict()`.

- [ ] **Step 4: Add the Hatch metadata and build hooks.**

Configure dynamic metadata and both custom hooks:

```toml
[project]
name = "baton-harness"
dynamic = ["version"]

[tool.hatch.metadata.hooks.custom]
path = "hatch_build.py"

[tool.hatch.build.hooks.custom]
path = "hatch_build.py"
```

`CustomMetadataHook.update()` must set only `metadata["version"]` from the resolved
identity. `CustomBuildHook.initialize()` must serialize sorted, indented JSON with a
trailing newline to a build-owned temporary file, then populate `force_include` for
sdist/standard wheel and `force_include_editable` for editable wheel. Its `finalize()`
must delete only the exact temporary file it created. Hatch documents these hook data
keys and editable override behavior (design spec:L111-L127).

- [ ] **Step 5: Refresh the stable editable lock and environment.**

Run:

```bash
BH_BUILD_DEVELOPMENT=1 uv lock
BH_BUILD_DEVELOPMENT=1 uv sync --locked --extra dev
BH_BUILD_DEVELOPMENT=1 uv lock --check
```

Expected: `uv.lock` records `0.1.0.dev0`, sync succeeds, and the final lock check makes
no change.

- [ ] **Step 6: Run focused tests and commit.**

Run:

```bash
./.venv/Scripts/python.exe -m pytest -q tests/test_build_provenance.py --basetemp='I:/ai/claude/baton-harness/.tmp/pytest-358-build-green'
```

Expected: all build-provenance tests pass.

Commit:

```bash
git add hatch_build.py pyproject.toml uv.lock tests/test_build_provenance.py
git commit -m "feat(#358): embed validated build provenance"
```

### Task 2: Runtime Provenance and Early CLI Identity

**Files:**

- Create: `src/baton_harness/provenance.py`
- Create: `tests/test_provenance.py`
- Modify: `src/baton_harness/__init__.py:1-16`
- Modify: `src/baton_harness/chain/cli.py:190-261`
- Modify: `tests/chain/test_cli.py`

**Interfaces:**

- Consumes: generated `build_provenance.json` and installed distribution metadata.
- Produces: `ProvenanceError`, immutable `Provenance`,
  `validate_provenance(object, installed_version: str) -> Provenance`, and
  `load_provenance() -> Provenance`.
- Produces: `bh-daemon --version` and JSON-only `bh-daemon --provenance` early exits
  (design spec:L153-L167).

- [ ] **Step 1: Write failing schema and CLI tests.**

Pin exact-key rejection and installed-version equality:

```python
def test_validate_provenance_requires_exact_schema() -> None:
    raw = {
        "schema_version": 1,
        "package_version": "1.2.3",
        "source_revision": REVISION,
        "lock_identity": f"sha256:{'a' * 64}",
        "development": False,
        "unexpected": True,
    }
    with pytest.raises(ProvenanceError, match="unexpected fields"):
        validate_provenance(raw, installed_version="1.2.3")


def test_validate_provenance_rejects_distribution_mismatch() -> None:
    raw = valid_record(package_version="1.2.3")
    with pytest.raises(ProvenanceError, match="installed version"):
        validate_provenance(raw, installed_version="1.2.4")
```

Add tests for invalid JSON/root type/schema version/field types/hex formats/missing
resource. In `tests/chain/test_cli.py`, patch config, bootstrap, and daemon seams to
raise if called; assert `--version` and `--provenance` still exit successfully, and
assert malformed provenance returns 1 with no traceback or config access.

Define the test helper before those cases:

```python
def valid_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": 1,
        "package_version": "1.2.3",
        "source_revision": REVISION,
        "lock_identity": f"sha256:{'a' * 64}",
        "development": False,
    }
    record.update(overrides)
    return record
```

- [ ] **Step 2: Run the focused tests and verify they fail.**

Run:

```bash
./.venv/Scripts/python.exe -m pytest -q tests/test_provenance.py tests/chain/test_cli.py --basetemp='I:/ai/claude/baton-harness/.tmp/pytest-358-runtime-red'
```

Expected: failures identify the absent module and CLI flags.

- [ ] **Step 3: Implement runtime loading and validation.**

Use a frozen dataclass and validate before construction:

```python
@dataclass(frozen=True)
class Provenance:
    schema_version: int
    package_version: str
    source_revision: str
    lock_identity: str
    development: bool

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def load_provenance() -> Provenance:
    try:
        text = resources.files("baton_harness").joinpath(
            "build_provenance.json"
        ).read_text(encoding="utf-8")
        raw = json.loads(text)
        installed = metadata.version("baton-harness")
    except (OSError, UnicodeError, json.JSONDecodeError,
            metadata.PackageNotFoundError) as exc:
        raise ProvenanceError("runtime provenance is unavailable") from exc
    return validate_provenance(raw, installed_version=installed)
```

Use fixed lowercase-hex patterns, reject booleans where integers are expected, require
exact keys, and expose no `.git` or subprocess path from this module.

- [ ] **Step 4: Replace the duplicate package version and add early CLI options.**

Set `baton_harness.__version__` from `importlib.metadata.version("baton-harness")`.
Add argparse `action="version"` for `--version`; handle `--provenance` immediately
after parsing by validating, then printing `json.dumps(record.as_dict(), sort_keys=True)`.
Catch `ProvenanceError`, write one fixed-prefix diagnostic to stderr, and return 1.

- [ ] **Step 5: Verify both unit and real editable behavior.**

Run:

```bash
./.venv/Scripts/python.exe -m pytest -q tests/test_provenance.py tests/chain/test_cli.py --basetemp='I:/ai/claude/baton-harness/.tmp/pytest-358-runtime-green'
./.venv/Scripts/bh-daemon.exe --version
./.venv/Scripts/bh-daemon.exe --provenance
```

Expected: tests pass; version is `0.1.0.dev0`; provenance is valid JSON with
`development: true` and the branch `HEAD` revision.

- [ ] **Step 6: Commit runtime provenance.**

```bash
git add src/baton_harness/__init__.py src/baton_harness/provenance.py src/baton_harness/chain/cli.py tests/test_provenance.py tests/chain/test_cli.py
git commit -m "feat(#358): expose runtime provenance"
```

### Task 3: Pure Shared Sandbox Config Resolution

**Files:**

- Modify: `src/baton_harness/chain/sandbox_config.py:102-380`
- Modify: `tests/chain/test_sandbox_config.py`

**Interfaces:**

- Produces: frozen `SandboxConfig`,
  `select_config_path(explicit: str | None, env: Mapping[str, str]) -> Path`,
  `resolve_config(path: Path, env: Mapping[str, str]) -> SandboxConfig`,
  `apply_config(config: SandboxConfig, env: MutableMapping[str, str]) -> None`, and
  `validate_repository(config: SandboxConfig, run: RunFn) -> None`.
- Retains: `read_and_validate()` as a compatibility composition of resolve, live
  repository validation, and explicit application until call sites migrate.
- Consumes: existing non-empty environment precedence in
  `resolve_overridable_keys()` (`src/baton_harness/chain/sandbox_config.py:L227-L250`;
  design spec:L200-L216).

- [ ] **Step 1: Add failing purity and path-selection tests.**

```python
def test_resolve_config_is_non_mutating_and_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_env(tmp_path, _VALID_ENV_CONTENT)
    ambient = {"BH_REPO_OWNER": "override-owner"}
    before = dict(os.environ)
    config = resolve_config(path, ambient)
    assert config.repo_owner == "override-owner"
    assert dict(os.environ) == before


def test_explicit_config_path_does_not_require_project_root(
    tmp_path: Path,
) -> None:
    explicit = tmp_path / "custom.env"
    assert select_config_path(str(explicit), {}) == explicit.resolve()
```

Add tests proving `resolve_config` never invokes a runner, repository validation is a
separate call, `apply_config` writes derived BWS compatibility keys, and provider
switches remove the unselected source key from the supplied mutable mapping.

- [ ] **Step 2: Run the config tests and verify the new imports fail.**

```bash
./.venv/Scripts/python.exe -m pytest -q tests/chain/test_sandbox_config.py --basetemp='I:/ai/claude/baton-harness/.tmp/pytest-358-config-red'
```

Expected: failures report missing `select_config_path`, `resolve_config`, and
`apply_config`.

- [ ] **Step 3: Extract parsing/resolution without changing validation rules.**

Make `SandboxConfig` frozen. Move file parsing, overrides, required-key checks, value
checks, and provider resolution into `resolve_config`. Return before any subprocess or
environment write. Implement path selection exactly as:

```python
def select_config_path(
    explicit: str | None,
    env: Mapping[str, str],
) -> Path:
    if explicit:
        return Path(explicit).resolve()
    project_root = env.get("BH_PROJECT_ROOT", "")
    if not project_root:
        raise SandboxConfigError(
            "BH_PROJECT_ROOT is required when --config is not supplied"
        )
    return (Path(project_root) / ".bh" / "config.env").resolve()
```

- [ ] **Step 4: Isolate live validation and explicit environment application.**

`validate_repository` runs only `gh api repos/{owner}/{repo} --jq .id` and raises the
existing stable error on non-zero. `apply_config` receives the target mapping instead
of reaching into `os.environ`; set/remove the same keys currently handled at
`sandbox_config.py:L351-L375`. Keep `read_and_validate` behavior by composing all three
functions with `os.environ`.

- [ ] **Step 5: Run config and existing doctor tests.**

```bash
./.venv/Scripts/python.exe -m pytest -q tests/chain/test_sandbox_config.py tests/chain/test_doctor.py --basetemp='I:/ai/claude/baton-harness/.tmp/pytest-358-config-green'
```

Expected: all tests pass with legacy behavior preserved through the wrapper.

- [ ] **Step 6: Commit the config boundary.**

```bash
git add src/baton_harness/chain/sandbox_config.py tests/chain/test_sandbox_config.py
git commit -m "refactor(#358): separate config resolution from effects"
```

### Task 4: Three-Phase Doctor Domain and Offline Installation Checks

**Files:**

- Modify: `src/baton_harness/chain/doctor.py:24-1300`
- Modify: `src/baton_harness/chain/cli.py:416-443`
- Modify: `src/baton_harness/chain/reconcile.py:288-331`
- Modify: `tests/chain/test_doctor.py`
- Modify: `tests/test_auth.py`
- Modify: `tests/chain/test_reconcile.py`
- Modify: `tests/chain/test_reconcile_git_credential_helper.py`
- Modify: `tests/chain/test_reconcile_oauth_cred.py`

**Interfaces:**

- Produces: string enums `Phase.INSTALLATION`, `Phase.CONFIGURATION`, `Phase.LIVE`;
  `CheckResult` with `check_id`, `phase`, `title`, `severity`, `status`, `detail`, and
  `remediation`; and `DoctorGateError(results)`.
- Produces:
  `create_context(*, env: Mapping[str, str], config_path: Path | None = None,
  home_dir: str | None = None, installation_token: str = "",
  which: WhichFn, runner: RunnerFn, run: RunFn,
  fetch_secret: FetchSecretFn) -> DoctorContext`,
  `run_report(ctx, phases=None, checks=None) -> list[CheckResult]`, and
  `run_gate(ctx, phases, checks=None) -> list[CheckResult]`.
- Consumes: `load_provenance`, packaged resources/workflow loader, installed entry
  point metadata, and the pure config APIs from Task 3.
- Source: public phases and offline/live boundaries are fixed by design
  spec:L169-L198; test coverage is fixed by design spec:L296-L321.

- [ ] **Step 1: Rewrite model tests first.**

Replace the old two-phase assertions with exact public values:

```python
def test_phase_enum_has_stable_machine_values() -> None:
    assert {phase.value for phase in Phase} == {
        "installation",
        "configuration",
        "live",
    }


def test_run_gate_collects_every_critical_failure() -> None:
    def critical_failure(check_id: str) -> CheckFn:
        def run(ctx: DoctorContext) -> CheckResult:
            del ctx
            return CheckResult(
                check_id=check_id,
                phase=Phase.INSTALLATION,
                title="failure",
                severity=Severity.CRITICAL,
                status=CheckStatus.FAIL,
                detail="failed",
                remediation="repair it",
            )

        return run

    checks = (
        _make_check(
            "A", phase=Phase.INSTALLATION, fn=critical_failure("A")
        ),
        _make_check(
            "B", phase=Phase.INSTALLATION, fn=critical_failure("B")
        ),
    )
    with pytest.raises(DoctorGateError) as captured:
        run_gate(_make_ctx(), (Phase.INSTALLATION,), checks=checks)
    assert [item.check_id for item in captured.value.results] == ["A", "B"]
```

Add selection tests for one, repeated, and default-all phases; prove unselected check
functions are never called. Update old phase references in auth/reconcile fixtures to
the new public names without changing those tests' operational assertions.

- [ ] **Step 2: Add failing installation-phase tests.**

Add one test per stable check ID: `PKG_PROVENANCE`, `PKG_IMPORTS`,
`PKG_ENTRY_POINTS`, `PKG_RESOURCES`, `PKG_WORKFLOW`, and `FORCE_PR_TRIPWIRE`. In the
all-pass test, remove `GH_TOKEN`, `BWS_ACCESS_TOKEN`, `ANTHROPIC_API_KEY`, and Claude
OAuth variables, install runners that raise on `gh`/`bws`/`claude`, and assert every
installation result passes.

```python
def test_installation_phase_never_uses_live_tools() -> None:
    def forbidden_runner(command: list[str]) -> CompletedProcess[str]:
        raise AssertionError(f"live command used: {command}")

    results = run_report(
        make_context(runner=forbidden_runner, env={}),
        (Phase.INSTALLATION,),
    )
    assert all(result.status is CheckStatus.PASS for result in results)
```

- [ ] **Step 3: Run doctor tests and verify phase/check failures.**

```bash
./.venv/Scripts/python.exe -m pytest -q tests/chain/test_doctor.py --basetemp='I:/ai/claude/baton-harness/.tmp/pytest-358-doctor-red'
```

Expected: failures identify old enum members, result shape, and absent installation
checks.

- [ ] **Step 4: Implement the domain model and context factory.**

Use string-valued enums and an aggregated gate exception:

```python
class Phase(str, Enum):
    INSTALLATION = "installation"
    CONFIGURATION = "configuration"
    LIVE = "live"


class DoctorGateError(RuntimeError):
    def __init__(self, results: Sequence[CheckResult]) -> None:
        super().__init__("critical preflight checks failed")
        self.results = tuple(results)
```

`run_report` normalizes omitted phases to all three in enum order, preserves catalog
order, and turns check exceptions into redacted-ready failed results. `run_gate` calls
`run_report` once, raises after collection when any critical failure exists, and
otherwise returns the complete result list.

Update the interim daemon call sites to pass `(Phase.CONFIGURATION,)` from `cli.py`
and `(Phase.LIVE,)` from `reconcile.py`; Task 6 will replace the former with the
combined installation/configuration gate and move the latter before event-loop entry.

- [ ] **Step 5: Rephase the catalog and implement offline checks.**

Use this exact catalog ownership:

- `installation`: `PKG_PROVENANCE`, `PKG_IMPORTS`, `PKG_ENTRY_POINTS`,
  `PKG_RESOURCES`, `PKG_WORKFLOW`, `FORCE_PR_TRIPWIRE`;
- `configuration`: `CLI_GH`, `CLI_BWS`, `CLI_CLAUDE`, `CLI_UV`,
  `ENV_PROJECT_ROOT`, `ENV_HOST_ENV`, `CFG_CONFIG_ENV`, `CFG_REQUIRED_KEYS`,
  `CFG_OPTIONAL_SECRET_IDS`, `ENV_BWS_ACCESS_TOKEN`, `GITIGNORE_SYMPHONY`,
  `CRED_ANTHROPIC_UNSET`;
- `live`: `GIT_CRED_HELPER`, `RULESET_MAIN`, `RULESET_FEATURE`, `LABELS_PRESENT`,
  `GH_REPO_ADMIN`, `GH_AUTH`, `CRED_OAUTH_VOLUME`, `VAULT_PEM_DRYRUN`.

The import check imports `baton_harness`, `baton_harness.chain.cli`, and
`baton_harness.vendor.symphony.config`. Entry-point validation requires the six names
at `pyproject.toml:L47-L53`. Resource validation reads every `RESOURCE_NAMES` member.
Workflow validation calls `load_workflow` through `_workflow_path(None)`. Reuse the
existing tripwire function, which already operates in a temporary directory
(`src/baton_harness/chain/cli.py:L165-L187`).

- [ ] **Step 6: Run the doctor-domain regression set.**

```bash
./.venv/Scripts/python.exe -m pytest -q tests/chain/test_doctor.py tests/test_auth.py tests/chain/test_reconcile.py tests/chain/test_reconcile_git_credential_helper.py tests/chain/test_reconcile_oauth_cred.py --basetemp='I:/ai/claude/baton-harness/.tmp/pytest-358-doctor-green'
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit the doctor domain.**

```bash
git add src/baton_harness/chain/doctor.py src/baton_harness/chain/cli.py src/baton_harness/chain/reconcile.py tests/chain/test_doctor.py tests/test_auth.py tests/chain/test_reconcile.py tests/chain/test_reconcile_git_credential_helper.py tests/chain/test_reconcile_oauth_cred.py
git commit -m "feat(#358): add phase-selectable doctor checks"
```

### Task 5: Secret-Safe Text and JSON Reports

**Files:**

- Create: `src/baton_harness/chain/doctor_report.py`
- Create: `tests/chain/test_doctor_report.py`
- Modify: `src/baton_harness/redact.py:8-42`
- Modify: `tests/test_redact.py`

**Interfaces:**

- Produces: immutable `ReportSummary`, `summarize(results) -> ReportSummary`,
  `build_document(results: Sequence[CheckResult], phases: Sequence[Phase],
  provenance: Provenance | None) -> dict[str, object]`,
  `secret_values_from_context(ctx: DoctorContext) -> tuple[str, ...]`,
  `redact_results(results: Sequence[CheckResult],
  secret_values: Iterable[str]) -> tuple[CheckResult, ...]`, and
  `render_json(results: Sequence[CheckResult], phases: Sequence[Phase],
  provenance: Provenance | None, *, secret_values: Iterable[str] = ()) -> str`.
- Produces: `render_text(results: Sequence[CheckResult], *,
  secret_values: Iterable[str] = ()) -> str`.
- Consumes: Task 2 `Provenance | None`, Task 4 results/phases, and exact known secret
  values from `DoctorContext.env`.
- Source: JSON schema, null-provenance failure behavior, deterministic ordering, and
  redaction boundary are fixed by design spec:L227-L278.

- [ ] **Step 1: Add failing redaction cases.**

```python
@pytest.mark.parametrize(
    "secret",
    [
        "-----BEGIN PRIVATE KEY-----\nmaterial\n-----END PRIVATE KEY-----",
        "https://user:password@example.test/path",
        "https://example.test/?access_token=oauth-secret",
        '{"refresh_token":"refresh-secret"}',
    ],
)
def test_extended_credentials_are_redacted(secret: str) -> None:
    rendered = redact_secrets(f"failure: {secret}")
    assert "secret" not in rendered
    assert "material" not in rendered
    assert "password" not in rendered
```

Retain all existing GitHub-token, exact-value, exception, and fail-closed tests.

- [ ] **Step 2: Add failing report-schema tests.**

Build two results in catalog order and assert the complete parsed document: schema
version, provenance mapping/null, selected phase values, exact summary counters, and
check keys `id`, `phase`, `status`, `severity`, `title`, `detail`, `remediation`.
Assert JSON output ends with one newline and contains no token, PEM, OAuth value, or
credential URL. Assert text output carries the same statuses/details/remediation.

- [ ] **Step 3: Run renderer/redactor tests and verify failures.**

```bash
./.venv/Scripts/python.exe -m pytest -q tests/test_redact.py tests/chain/test_doctor_report.py --basetemp='I:/ai/claude/baton-harness/.tmp/pytest-358-report-red'
```

Expected: new credential patterns and report module are absent.

- [ ] **Step 4: Extend fail-closed redaction.**

Add compiled patterns for complete PEM blocks, sensitive URL query values, and common
OAuth/API JSON or assignment fields. Apply structural patterns before exact values;
retain the existing catch-all behavior that returns only `«redacted»` if substitution
fails (`src/baton_harness/redact.py:L25-L41`). Never log or interpolate the supplied
exact secret list.

- [ ] **Step 5: Implement deterministic report construction and rendering.**

Build dictionaries in the exact schema order shown in design spec:L227-L260. Normalize
enum values through `.value`, map internal `check_id` to JSON `id`, and map
`remediation` directly. Redact `title`, `detail`, and `remediation` separately before
serialization. If redaction or `json.dumps` fails, raise `ReportRenderingError` and do
not return partial content.

```python
def render_json(
    results: Sequence[CheckResult],
    phases: Sequence[Phase],
    provenance: Provenance | None,
    *,
    secret_values: Iterable[str] = (),
) -> str:
    document = build_document(
        redact_results(results, secret_values),
        phases,
        provenance,
    )
    return f"{json.dumps(document, separators=(',', ':'))}\n"
```

- [ ] **Step 6: Run tests and commit.**

```bash
./.venv/Scripts/python.exe -m pytest -q tests/test_redact.py tests/chain/test_doctor_report.py --basetemp='I:/ai/claude/baton-harness/.tmp/pytest-358-report-green'
git add src/baton_harness/redact.py src/baton_harness/chain/doctor_report.py tests/test_redact.py tests/chain/test_doctor_report.py
git commit -m "feat(#358): render secret-safe doctor reports"
```

### Task 6: Standalone and Daemon CLI Integration

**Files:**

- Modify: `src/baton_harness/chain/cli.py:190-507`
- Modify: `src/baton_harness/chain/reconcile.py:288-331`
- Modify: `tests/chain/test_cli_doctor_gate.py`
- Modify: `tests/chain/test_cli.py`
- Modify: `tests/chain/test_reconcile.py`
- Modify: `tests/chain/test_reconcile_git_credential_helper.py`
- Modify: `tests/chain/test_reconcile_oauth_cred.py`
- Modify: `tests/test_auth.py`

**Interfaces:**

- Consumes: Task 3 config APIs, Task 4 context/report/gate APIs, and Task 5 renderers.
- Produces: repeatable `--phase`, `--format {text,json}`, and `--config PATH`.
- Preserves: `--doctor`, `--strict`, and `--check-vault` compatibility; advisory
  standalone and fail-closed daemon semantics (design spec:L187-L225;
  design spec:L323-L331).

- [ ] **Step 1: Add failing option, output, and early-return tests.**

Assert these command shapes:

```python
assert _run_main("--doctor", "--phase", "installation") == 0
assert _run_main(
    "--doctor",
    "--phase", "configuration",
    "--phase", "live",
    "--format", "json",
    "--config", str(config_path),
) == 0
```

Parse JSON stdout and assert stderr contains no log noise. Verify omitted `--phase`
passes all phases in order. Verify advisory and strict exit codes with both critical
and warning-only results. Verify `--check-vault` selects only `VAULT_PEM_DRYRUN` and
returns 1 unless it passes.

- [ ] **Step 2: Add failing daemon ordering and config-sharing tests.**

Record calls and require this order:

```python
assert events == [
    "resolve_config",
    "installation_configuration_gate",
    "apply_config",
    "bootstrap_secrets",
    "live_gate",
    "run_daemon",
]
```

Assert critical pre-bootstrap results prevent config application/bootstrap; critical
live results prevent the event loop. Assert the exact `--config` path reaches both the
resolver and context factory. Preserve current token-validation ordering around the
live gate (`src/baton_harness/chain/reconcile.py:L260-L331`).

- [ ] **Step 3: Run CLI/reconcile tests and verify failures.**

```bash
./.venv/Scripts/python.exe -m pytest -q tests/chain/test_cli_doctor_gate.py tests/chain/test_cli.py tests/chain/test_reconcile.py tests/chain/test_reconcile_git_credential_helper.py tests/chain/test_reconcile_oauth_cred.py tests/test_auth.py --basetemp='I:/ai/claude/baton-harness/.tmp/pytest-358-cli-red'
```

Expected: failures identify absent options and old gate phases.

- [ ] **Step 4: Implement standalone doctor selection/rendering.**

Use `action="append"`, `choices=tuple(phase.value for phase in Phase)`, and normalize
omission to `tuple(Phase)`. Create one context through `doctor.create_context`. Run one
report, load provenance without executing an installation check implicitly, render to
stdout, and compute `--strict` from selected critical failures. On report-rendering
failure, write one fixed diagnostic to stderr and return 1.

- [ ] **Step 5: Replace daemon config and gates with shared APIs.**

Resolve explicit/default config without mutation. Run installation and configuration
through one aggregated gate, render all critical failures to stderr, then apply the
resolved config and bootstrap secrets. After CLI token validation, construct a live
context through the same factory and run `Phase.LIVE` before calling `run_daemon`.
Remove the old doctor gate from reconciliation while preserving its native token,
credential-helper, OAuth, alert, and startup-order guards. Do not let a gate exception
escape as a traceback.

- [ ] **Step 6: Run the integration regression set.**

```bash
./.venv/Scripts/python.exe -m pytest -q tests/chain/test_cli_doctor_gate.py tests/chain/test_cli.py tests/chain/test_reconcile.py tests/chain/test_reconcile_git_credential_helper.py tests/chain/test_reconcile_oauth_cred.py tests/test_auth.py --basetemp='I:/ai/claude/baton-harness/.tmp/pytest-358-cli-green'
```

Expected: all selected tests pass and no old phase enum name remains in production
code.

- [ ] **Step 7: Commit CLI integration.**

```bash
git add src/baton_harness/chain/cli.py src/baton_harness/chain/reconcile.py tests/chain/test_cli_doctor_gate.py tests/chain/test_cli.py tests/chain/test_reconcile.py tests/chain/test_reconcile_git_credential_helper.py tests/chain/test_reconcile_oauth_cred.py tests/test_auth.py
git commit -m "feat(#358): share doctor gates with daemon startup"
```

### Task 7: Source-Archive and Installed Foundation Verification

**Files:**

- Modify: `src/baton_harness/verify_foundation.py:28-620`
- Modify: `tests/test_verify_foundation.py`

**Interfaces:**

- Produces: `inspect_provenance_archive(path: Path,
  expected: Mapping[str, object]) -> None` and source-archive
  rebuild validation with no `.git` directory.
- Consumes: standard identity `0.0.0+foundation` plus actual repository `HEAD`, exact
  lock digest, Task 2 runtime commands, and installation-only doctor JSON.
- Source: required archive/install verification is fixed by design spec:L279-L294 and
  #358.

- [ ] **Step 1: Extend fake artifacts and write failing archive tests.**

Teach `_write_wheel` and `_RecordingRunner` to emit Core Metadata and provenance.
Create a minimal `.tar.gz` sdist containing `pyproject.toml`, `hatch_build.py`,
`uv.lock`, and `src/baton_harness/build_provenance.json`. Add tests for absent record,
malformed JSON, metadata-version mismatch, lock-digest mismatch, source-revision
mismatch, and a source tree containing `.git`.

- [ ] **Step 2: Add failing command-sequence and installed-smoke assertions.**

Require the standard build call environment to contain:

```python
assert build_env["BH_BUILD_VERSION"] == "0.0.0+foundation"
assert build_env["BH_BUILD_SOURCE_REVISION"] == repository_head
assert "BH_BUILD_DEVELOPMENT" not in build_env
```

Require a second wheel build whose `cwd` is the extracted sdist and assert
`(cwd / ".git").exists()` is false. Extend safe smokes with `bh-daemon --version`,
`bh-daemon --provenance`, and
`bh-daemon --doctor --phase installation --format json --strict`; parse both JSON
outputs.

- [ ] **Step 3: Run verifier tests and verify failures.**

```bash
./.venv/Scripts/python.exe -m pytest -q tests/test_verify_foundation.py --basetemp='I:/ai/claude/baton-harness/.tmp/pytest-358-foundation-red'
```

Expected: failures identify missing archive inspection and smoke commands.

- [ ] **Step 4: Implement archive and installed checks.**

Read `.whl` with `ZipFile` and `.tar.gz` with `tarfile.open`; require exactly one
canonical provenance member. Validate it with the same five-field shape, compare wheel
`METADATA` version, and compare expected identity. Extract only after rejecting
absolute paths and `..` traversal components. Assert the extracted root has no `.git`,
then invoke the second standard wheel build with the same identity environment.

- [ ] **Step 5: Run unit verification and one real foundation pass.**

```bash
./.venv/Scripts/python.exe -m pytest -q tests/test_verify_foundation.py --basetemp='I:/ai/claude/baton-harness/.tmp/pytest-358-foundation-green'
./.venv/Scripts/bh-verify-foundation.exe --python 3.13
```

Expected: unit tests pass; the real command builds the sdist/wheel, rebuilds without
`.git`, installs outside the checkout, and passes provenance/doctor smokes.

- [ ] **Step 6: Commit foundation verification.**

```bash
git add src/baton_harness/verify_foundation.py tests/test_verify_foundation.py
git commit -m "test(#358): verify frozen provenance outside git"
```

### Task 8: Editable Setup and Operator Documentation

**Files:**

- Modify: `.github/actions/setup/action.yml:20-24`
- Modify: `bin/setup-env.sh:441-446`
- Modify: `tests/test_required_checks_match_ci_yml.py`
- Modify: `README.md`
- Modify: `docs/system-setup.md`
- Modify: `docs/repository-onboarding.md`
- Modify: `docs/smoke-test-daemon.md`

**Interfaces:**

- Produces: every maintained editable sync sets `BH_BUILD_DEVELOPMENT=1`.
- Produces: operator docs for immutable release tags, explicit standard build identity,
  provenance commands, phase/format/config selection, JSON schema, and exit behavior.
- Source: documentation and compatibility requirements are fixed by design
  spec:L323-L337; current stale doctor/bootstrap names occur in
  `docs/repository-onboarding.md:L304-L387` and `docs/smoke-test-daemon.md:L154-L154`.

- [ ] **Step 1: Update the setup-contract test first.**

Require the composite sync step to retain its exact command while adding the explicit
environment:

```python
assert sync_step["run"] == "uv sync --python 3.10 --locked --extra dev"
assert sync_step["env"] == {"BH_BUILD_DEVELOPMENT": "1"}
```

Add a setup-script assertion that the sync command begins with
`BH_BUILD_DEVELOPMENT=1`.

- [ ] **Step 2: Run setup-contract tests and verify they fail.**

```bash
./.venv/Scripts/python.exe -m pytest -q tests/test_required_checks_match_ci_yml.py tests/test_repository_workflow_contract.py --basetemp='I:/ai/claude/baton-harness/.tmp/pytest-358-docs-red'
```

Expected: CI/setup script do not yet set the development flag.

- [ ] **Step 3: Apply the editable flag to maintained setup paths.**

Add `env: {BH_BUILD_DEVELOPMENT: "1"}` to the composite action step. Change the shell
sync to:

```bash
BH_BUILD_DEVELOPMENT=1 uv sync --project "${BATON_HARNESS_DIR}" --locked --extra dev
```

Do not change line-ending policy or introduce a second lock file.

- [ ] **Step 4: Update build/install and doctor documentation.**

Document these exact operator examples:

```bash
BH_BUILD_VERSION=1.0.0 \
BH_BUILD_SOURCE_REVISION=0123456789abcdef0123456789abcdef01234567 \
uv build --build-constraints .tmp/build-requirements.txt --require-hashes

bh-daemon --version
bh-daemon --provenance
bh-daemon --doctor --phase installation --format json --strict
bh-daemon --doctor --phase configuration --config /path/to/config.env --strict
bh-daemon --doctor --phase live --strict
```

Explain that a release workflow may derive the two standard assertions from an
immutable tag, the hook validates explicit values rather than tag names, editable
work uses `0.1.0.dev0`, and the exact commit is always `source_revision`. Replace all
operator-facing `PRE_BOOTSTRAP`/`POST_BOOTSTRAP` descriptions with the three public
phases. Preserve `--check-vault` as the live-check compatibility command.

- [ ] **Step 5: Run contract tests and stale-text searches.**

```bash
./.venv/Scripts/python.exe -m pytest -q tests/test_required_checks_match_ci_yml.py tests/test_repository_workflow_contract.py --basetemp='I:/ai/claude/baton-harness/.tmp/pytest-358-docs-green'
rg -n 'PRE_BOOTSTRAP|POST_BOOTSTRAP' README.md docs src tests
rg -n 'uv sync' README.md docs bin .github
```

Expected: tests pass; old phase names remain only in durable historical documents or
test history that explicitly describes the retired API; every maintained editable
sync sets the development flag.

- [ ] **Step 6: Commit setup and docs.**

```bash
git add .github/actions/setup/action.yml bin/setup-env.sh tests/test_required_checks_match_ci_yml.py README.md docs/system-setup.md docs/repository-onboarding.md docs/smoke-test-daemon.md
git commit -m "docs(#358): document provenance and preflight phases"
```

### Task 9: Full Verification, Persistence Audit, and Pull Request

**Files:**

- Verify: all files changed by Tasks 1-8
- Update only if checks expose a defect: the exact source/test/doc responsible for
  that defect

**Interfaces:**

- Consumes: the complete #358 branch.
- Produces: a review-ready feature PR targeting `main`, labeled `needs-review`, whose
  body includes `Closes #358` and the required Codex attribution.
- Source: #358 acceptance criteria; artifact persistence requires every path named by
  committed code/docs to exist in `HEAD` or already in `main`.

- [ ] **Step 1: Verify lock, formatting, lint, and typing.**

```bash
BH_BUILD_DEVELOPMENT=1 uv lock --check
./.venv/Scripts/python.exe -m ruff format --check .
./.venv/Scripts/python.exe -m ruff check .
./.venv/Scripts/python.exe -m mypy src tests hatch_build.py
git diff --check
```

Expected: every command exits 0 and uses Ruff `0.15.20` from the project environment
(`pyproject.toml:L24-L30`).

- [ ] **Step 2: Run the complete test suite.**

```bash
./.venv/Scripts/python.exe -m pytest -q --basetemp='I:/ai/claude/baton-harness/.tmp/pytest-358-final'
```

Expected: all tests pass; compare skips with the 22-skip baseline and investigate any
new skip before proceeding.

- [ ] **Step 3: Run the full two-interpreter foundation verifier.**

```bash
./.venv/Scripts/bh-verify-foundation.exe
```

Expected: Python 3.10 and 3.13 source-archive, wheel, non-editable install,
provenance, and installation-doctor checks pass.

- [ ] **Step 4: Audit deliverables and referenced artifacts.**

```bash
git diff main...HEAD --stat
git status --short
git ls-tree HEAD -- hatch_build.py uv.lock src/baton_harness/provenance.py docs/superpowers/specs/2026-09-06-runtime-provenance-preflight-design.md docs/superpowers/plans/2026-09-06-runtime-provenance-preflight.md
git log --oneline main..HEAD
```

Expected: every claimed source/doc is committed; archive inspection from Task 7 proves
the generated provenance member is present in built artifacts; no unrelated file is
present.

- [ ] **Step 5: Commit any verification-only corrections, then re-run their failing
  command.**

Use a scoped message matching the corrected boundary:

After confirming `git status --short` contains only the intended correction, stage
tracked corrections and commit them:

```bash
git add -u
git commit -m "fix(#358): correct provenance verification"
```

Skip this step when Step 1-4 require no correction; never create an empty commit.

- [ ] **Step 6: Review the branch and create the PR.**

Run the `superpowers:requesting-code-review` skill, address valid findings, then fetch
current `main` and rebase only if required by repository policy. Before pushing, verify
there is no merged/closed PR for this branch. Create a PR targeting `main` with
summary, verification evidence, `Closes #358`, and this final line:

```text
> 🤖 _Generated by Codex on behalf of @cbeaulieu-gt_
```

Add the `needs-review` label because this is a feature PR. Do not merge it; hand the
review-ready PR to the user.
