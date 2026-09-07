# CodeReeve Configuration and State Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make CodeReeve configuration, environment variables, and state paths canonical in 0.2.0 while providing a reversible, fail-closed `codereeve migrate --check/--apply` path for existing installations.

**Architecture:** A pure `config_env` module owns literal assignment parsing and old/new environment resolution; a pure `paths` module owns canonical/legacy filesystem selection. Runtime Python and shell entry points consume those modules before effects. A migration package separates read-only inventory from quiescence/locking, durable journaling, staged publication, and restoration so #394 can coordinate service cutover without duplicating filesystem logic.

**Tech Stack:** Python 3.10+, Bash, `argparse`, dataclasses, JSON/JSONL, `hashlib`, `os.replace`, `fcntl`/`msvcrt` advisory locks, pytest, Ruff, mypy, ShellCheck when available.

**Spec:** `docs/superpowers/specs/2026-09-07-codereeve-rename-design.md`

## Global Constraints

- Canonical names are `CODEREEVE_*`; `BATON_HARNESS_DIR` maps to `CODEREEVE_ROOT`; legacy aliases remain functional through 0.3.x. Exact comparison is the default and diagnostics must never include either conflicting value. (`docs/superpowers/specs/2026-09-07-codereeve-rename-design.md:L140-L166`, #393)
- Canonical paths are `<project>/.codereeve/config.env`, `<project>/.codereeve/`, `~/.config/codereeve/host.env`, and `/etc/codereeve/secrets.env`. `.symphony/` remains untouched. (`docs/superpowers/specs/2026-09-07-codereeve-rename-design.md:L66-L79`, `docs/superpowers/specs/2026-09-07-codereeve-rename-design.md:L203-L214`)
- Third-party names including `BWS_*`, `GH_TOKEN`, and `ANTHROPIC_API_KEY` remain unchanged. (`docs/superpowers/specs/2026-09-07-codereeve-rename-design.md:L86-L90`, #393)
- `migrate --check` is read-only and returns `current`/0, `ready`/2, or `blocked`/1 in text and stable JSON. (`docs/superpowers/specs/2026-09-07-codereeve-rename-design.md:L168-L183`)
- Apply must inventory before mutation, refuse pre-existing canonical destinations or target collisions, stage and verify the whole destination, preserve timestamped backups, journal every boundary durably, and restore completed operations in reverse after failures. (`docs/superpowers/specs/2026-09-07-codereeve-rename-design.md:L185-L201`, #393)
- Empty means explicitly set; unset means absent. Resolve precedence separately for each spelling, then compare the resolved canonical and legacy values exactly. No key has a normalization rule in 0.2.0. (#393)
- Product-owned `.bh/ruleset-baseline.json` is treated as a second legacy state input and migrates to `.codereeve/ruleset-baseline.json`; leaving it behind would contradict the canonical state root because the current reader/writer stores it under `.bh`. (`src/codereeve/chain/ruleset_status.py:L937-L955`, #393)
- Service rendering, activation, and rollback remain in #394. This issue inventories the legacy/canonical unit and exports quiescence/restoration primitives, but does not activate, stop, disable, or rewrite a service. (#393, #394)
- Use non-blocking advisory locks: POSIX uses `fcntl.flock(..., LOCK_EX | LOCK_NB)` and Windows uses `msvcrt.locking(..., LK_NBLCK, 1)`. [Python `fcntl` documentation](https://docs.python.org/3.10/library/fcntl.html) and [Python `msvcrt` documentation](https://docs.python.org/3.10/library/msvcrt.html) (fetched 2026-09-07).
- Use `os.replace` only after proving source and destination staging are on the same filesystem; otherwise report a blocked plan and manual recovery guidance. [Python `os.replace` documentation](https://docs.python.org/3.10/library/os.html) (fetched 2026-09-07) and `docs/superpowers/specs/2026-09-07-codereeve-rename-design.md:L190-L195`.
- Preserve CRLF working-tree convention and existing behavior outside identity/configuration/state migration. Do not rename Symphony, alter orchestration semantics, or implement 0.3/0.4 warning/removal policy. (`AGENTS.md`, #393)

---

## File Structure

- `src/codereeve/config_env.py`: assignment grammar, product alias catalog, layered resolver, secret-safe errors, config rewriting.
- `src/codereeve/paths.py`: canonical/legacy path layout and ambiguity-safe selectors.
- `src/codereeve/config_bridge.py`: private NUL-record bridge used by Bash callers; no public console script.
- `src/codereeve/migration/model.py`: stable report/action/finding/manifest dataclasses and JSON schema.
- `src/codereeve/migration/inventory.py`: read-only environment/path/type/collision inventory.
- `src/codereeve/migration/lease.py`: cross-platform non-blocking writer/migration lease.
- `src/codereeve/migration/journal.py`: fsynced manifest and append-only progress journal.
- `src/codereeve/migration/transaction.py`: staging, verification, publication, rollback, and interruption recovery.
- `src/codereeve/migration/cli.py`: exact `--check`/`--apply` grammar, rendering, and exit codes.
- Existing runtime modules consume canonical keys/paths through the shared APIs; compatibility logic remains centralized.
- Existing Bash entry points call `config_bridge` through `bin/lib/load-config.sh`; no env file is sourced or evaluated.

---

### Task 1: Literal assignment parser and environment alias resolver

**Files:**
- Create: `src/codereeve/config_env.py`
- Create: `tests/test_config_env.py`
- Modify: `hatch_build.py`
- Modify: `tests/test_build_provenance.py`

**Interfaces:**
- Produces: `Assignment`, `EnvLayer`, `AliasSpec`, `ResolvedEnvironment`, `ConfigSyntaxError`, `AliasConflictError`.
- Produces: `parse_env_text(text: str, *, source: str) -> tuple[Assignment, ...]`.
- Produces: `parse_env_file(path: Path) -> tuple[Assignment, ...]`.
- Produces: `resolve_environment(layers: Sequence[EnvLayer], *, aliases: Sequence[AliasSpec] = PRODUCT_ALIASES, export_legacy: bool = True) -> ResolvedEnvironment`.
- Produces: `rewrite_assignments(assignments: Sequence[Assignment], *, path_values: Mapping[str, tuple[str, str]]) -> str`.
- Consumes: no effectful services; this task is pure except explicit file reads in `parse_env_file`.

- [ ] **Step 1: Write parser tests for the complete accepted grammar**

```python
def test_parse_env_text_accepts_literals_quotes_export_and_comments() -> None:
    parsed = parse_env_text(
        "export CODEREEVE_REPO_OWNER='glitchwerks' # owner\n"
        'CODEREEVE_REPO_NAME="sandbox"\n'
        "BWS_PEM_SECRET_ID=11111111-2222-3333-4444-555555555555\n",
        source="config.env",
    )
    assert [(item.key, item.value, item.line) for item in parsed] == [
        ("CODEREEVE_REPO_OWNER", "glitchwerks", 1),
        ("CODEREEVE_REPO_NAME", "sandbox", 2),
        ("BWS_PEM_SECRET_ID", "11111111-2222-3333-4444-555555555555", 3),
    ]
```

Cover blank lines, leading/trailing whitespace, `#` inside quotes, empty quoted and unquoted values, escaped quote/backslash in the matching quote mode, CRLF, final line without newline, duplicate same-spelling assignments with last-assignment precedence, and NUL rejection. (`bin/lib/load-config.sh:L38-L47`, #393)

- [ ] **Step 2: Write non-execution and redaction tests**

Parameterize `$()`, backticks, `${name}`, `$name`, `;`, `&&`, `||`, redirection, line continuation, unmatched quotes, multiple assignments on one line, invalid names, and `export` without an assignment. Assert `ConfigSyntaxError` contains source and line number but not the input value, and prove a command-substitution sentinel file is never created. (#393)

- [ ] **Step 3: Run the parser tests and verify RED**

Run: `./.venv/Scripts/python.exe -m pytest -q tests/test_config_env.py -k 'parse or syntax or execute'`

Expected: collection fails because `codereeve.config_env` does not exist.

- [ ] **Step 4: Implement the parser without `eval`, `source`, interpolation, or `shlex` execution semantics**

Use a character scanner that recognizes one assignment per physical line. Single-quoted content is literal; double-quoted content permits only `\\` and `\"`; unquoted content stops before an unquoted whitespace-plus-`#` comment. Reject expansion/control tokens before returning any assignments. Preserve `raw` and `line` on each `Assignment` so migration can rewrite only recognized keys.

- [ ] **Step 5: Write resolution tests for every pair state and source precedence**

```python
@pytest.mark.parametrize(
    ("operator", "managed", "expected"),
    [
        ({"CODEREEVE_PROJECT_ROOT": "/new"}, {}, "/new"),
        ({"BH_PROJECT_ROOT": "/old"}, {}, "/old"),
        (
            {"CODEREEVE_PROJECT_ROOT": "/same", "BH_PROJECT_ROOT": "/same"},
            {},
            "/same",
        ),
        ({}, {"BH_PROJECT_ROOT": "/file"}, "/file"),
    ],
)
def test_resolve_environment_pair_matrix(
    operator: dict[str, str], managed: dict[str, str], expected: str
) -> None:
    result = resolve_environment(
        [EnvLayer("operator", operator), EnvLayer("managed", managed)]
    )
    assert result.values["CODEREEVE_PROJECT_ROOT"] == expected
```

Also test: canonical/legacy mismatch across the same and different layers; identical pair; canonical unset versus empty; legacy unset versus empty; higher-priority same-spelling override; third-party pass-through; exactly one legacy-use record; and errors naming keys/sources without values. (`docs/superpowers/specs/2026-09-07-codereeve-rename-design.md:L151-L166`, #393)

- [ ] **Step 6: Implement the exhaustive alias catalog and layered resolver**

Catalog build aliases plus every externally read product variable:

```python
PRODUCT_ALIASES = (
    AliasSpec("CODEREEVE_ADMIN_ROLE_ID", "BH_ADMIN_ROLE_ID"),
    AliasSpec("CODEREEVE_APP_AUTH_JWT_CMD", "BH_APP_AUTH_JWT_CMD"),
    AliasSpec("CODEREEVE_APP_AUTH_TOKEN_CMD", "BH_APP_AUTH_TOKEN_CMD"),
    AliasSpec("CODEREEVE_BUILD_DEVELOPMENT", "BH_BUILD_DEVELOPMENT"),
    AliasSpec("CODEREEVE_BUILD_SOURCE_REVISION", "BH_BUILD_SOURCE_REVISION"),
    AliasSpec("CODEREEVE_BUILD_VERSION", "BH_BUILD_VERSION"),
    AliasSpec("CODEREEVE_DEBUG_CONFIG", "BH_DEBUG_CONFIG"),
    AliasSpec("CODEREEVE_FAILURE_COUNTS_PATH", "BH_FAILURE_COUNTS_PATH"),
    AliasSpec("CODEREEVE_FEATURE_BRANCH", "BH_FEATURE_BRANCH"),
    AliasSpec("CODEREEVE_GITHUB_APP_ID", "BH_GITHUB_APP_ID"),
    AliasSpec("CODEREEVE_GITHUB_APP_INSTALLATION_ID", "BH_GITHUB_APP_INSTALLATION_ID"),
    AliasSpec("CODEREEVE_GITHUB_APP_KEY_PROVIDER", "BH_GITHUB_APP_KEY_PROVIDER"),
    AliasSpec("CODEREEVE_GITHUB_APP_PRIVATE_KEY_FILE", "BH_GITHUB_APP_PRIVATE_KEY_FILE"),
    AliasSpec("CODEREEVE_HEARTBEAT_FILE", "BH_HEARTBEAT_FILE"),
    AliasSpec("CODEREEVE_HEARTBEAT_PING_URL", "BH_HEARTBEAT_PING_URL"),
    AliasSpec("CODEREEVE_HEARTBEAT_STALL_S", "BH_HEARTBEAT_STALL_S"),
    AliasSpec("CODEREEVE_MAX_ISSUE_FAILURES", "BH_MAX_ISSUE_FAILURES"),
    AliasSpec("CODEREEVE_PROBE_DRY_RUN", "BH_PROBE_DRY_RUN"),
    AliasSpec("CODEREEVE_PROBE_HOOK_SCRIPT", "BH_PROBE_HOOK_SCRIPT"),
    AliasSpec("CODEREEVE_PROBE_PR_NUMBER", "BH_PROBE_PR_NUMBER"),
    AliasSpec("CODEREEVE_PROBE_SANDBOX_REPO", "BH_PROBE_SANDBOX_REPO"),
    AliasSpec("CODEREEVE_PROBE_WORKER_TOKEN_PATH", "BH_PROBE_WORKER_TOKEN_PATH"),
    AliasSpec("CODEREEVE_PROJECT_ROOT", "BH_PROJECT_ROOT"),
    AliasSpec("CODEREEVE_REDISPATCH_COUNTS_PATH", "BH_REDISPATCH_COUNTS_PATH"),
    AliasSpec("CODEREEVE_REDISPATCH_MAX", "BH_REDISPATCH_MAX"),
    AliasSpec("CODEREEVE_REDISPATCH_WINDOW_TICKS", "BH_REDISPATCH_WINDOW_TICKS"),
    AliasSpec("CODEREEVE_REPO_NAME", "BH_REPO_NAME"),
    AliasSpec("CODEREEVE_REPO_OWNER", "BH_REPO_OWNER"),
    AliasSpec("CODEREEVE_RUNLOG_PATH", "BH_RUNLOG_PATH"),
    AliasSpec("CODEREEVE_SCENARIO", "BH_SCENARIO"),
    AliasSpec("CODEREEVE_SETUP_NO_PROMPT", "BH_SETUP_NO_PROMPT"),
    AliasSpec("CODEREEVE_SLACK_WEBHOOK_URL", "BH_SLACK_WEBHOOK_URL"),
    AliasSpec("CODEREEVE_VENV", "BH_VENV"),
    AliasSpec("CODEREEVE_VERIFY_BLOCK_TIMEOUT_SECS", "BH_VERIFY_BLOCK_TIMEOUT_SECS"),
    AliasSpec("CODEREEVE_WORKER_PROGRESS_STALL_S", "BH_WORKER_PROGRESS_STALL_S"),
    AliasSpec("CODEREEVE_WORKTREE_GC", "BH_WORKTREE_GC"),
    AliasSpec("CODEREEVE_ROOT", "BATON_HARNESS_DIR"),
)
```

Keep third-party keys out of this catalog. Resolve each spelling by layer priority, compare spellings exactly, emit canonical keys, and optionally materialize legacy aliases with the identical selected value for 0.2 compatibility.

- [ ] **Step 7: Consolidate build alias handling on the shared pair primitive**

Change `hatch_build.py` to call a dependency-free pair helper from `config_env.py` without importing runtime-only modules. Preserve the #392 build error messages and secret-safe conflict behavior. Extend `tests/test_build_provenance.py` to prove the three existing build aliases remain exact and conflict-safe. (`hatch_build.py:L72-L136`, #392)

- [ ] **Step 8: Implement and test recognized config rewriting**

`rewrite_assignments` must canonicalize recognized product keys, retain third-party keys/comments/order, collapse an identical old/new pair to one canonical assignment, reject conflicts, and rewrite a path value only when it exactly equals a documented legacy default. Custom paths remain byte-for-byte values. (#393)

- [ ] **Step 9: Run focused tests and commit**

Run:

```powershell
./.venv/Scripts/python.exe -m pytest -q tests/test_config_env.py tests/test_build_provenance.py
./.venv/Scripts/python.exe -m ruff check src/codereeve/config_env.py tests/test_config_env.py hatch_build.py tests/test_build_provenance.py
./.venv/Scripts/python.exe -m mypy src/codereeve/config_env.py
```

Commit: `feat(#393): add canonical environment resolution`

---

### Task 2: Canonical path layout and configuration selection

**Files:**
- Create: `src/codereeve/paths.py`
- Create: `tests/test_paths.py`
- Modify: `src/codereeve/chain/sandbox_config.py`
- Modify: `src/codereeve/chain/doctor.py`
- Modify: `tests/chain/test_sandbox_config.py`
- Modify: `tests/chain/test_doctor.py`

**Interfaces:**
- Consumes: `resolve_environment`, `parse_env_file`, and the exact alias catalog from Task 1.
- Produces: `PathLayout.for_environment(project_root: Path, env: Mapping[str, str], *, home: Path | None = None, etc_root: Path = Path('/etc')) -> PathLayout`.
- Produces: `select_compatible_file(canonical: Path, legacy: Path, *, label: str) -> SelectedPath` and `select_compatible_directory(...) -> SelectedPath`.
- Produces: `sandbox_config.resolve_config_sources(explicit: Path | None, env: Mapping[str, str], layout: PathLayout) -> ResolvedSandboxConfig`.

- [ ] **Step 1: Write the path-layout and coexistence tests**

```python
def test_layout_uses_canonical_codereeve_paths(tmp_path: Path) -> None:
    layout = PathLayout.for_environment(
        tmp_path / "project",
        {"XDG_CONFIG_HOME": str(tmp_path / "xdg")},
        etc_root=tmp_path / "etc",
    )
    assert layout.canonical_state == tmp_path / "project" / ".codereeve"
    assert layout.canonical_config == layout.canonical_state / "config.env"
    assert layout.canonical_host == tmp_path / "xdg" / "codereeve" / "host.env"
    assert layout.canonical_secrets == tmp_path / "etc" / "codereeve" / "secrets.env"
    assert layout.symphony_state == tmp_path / "project" / ".symphony"
```

Cover HOME fallback, XDG override, legacy state/config/host/secrets/unit paths, neither-present canonical selection, canonical-only, legacy-only, identical file contents at both paths still blocked, unsafe symlink, and `.symphony` exclusion. (`docs/superpowers/specs/2026-09-07-codereeve-rename-design.md:L66-L79`, #393)

- [ ] **Step 2: Run path tests and verify RED**

Run: `./.venv/Scripts/python.exe -m pytest -q tests/test_paths.py`

Expected: collection fails because `codereeve.paths` does not exist.

- [ ] **Step 3: Implement immutable layouts and ambiguity-safe selectors**

Selectors use `lstat`, never follow symlinks, return canonical when neither path exists, and raise `PathConflictError` when both paths exist or an existing path has an unsupported type. They do not create, move, chmod, or delete anything.

- [ ] **Step 4: Refactor sandbox config parsing onto Task 1 without changing validation**

Replace `_LINE_RE` parsing with `parse_env_file`. Change required/overridable product keys to canonical spellings, keep `BWS_*` spellings unchanged, and return a `SandboxConfig` whose fields remain semantic rather than name-prefixed. Ensure invalid-value errors identify key/source/line but never print the value. (`src/codereeve/chain/sandbox_config.py:L56-L93`, `src/codereeve/chain/sandbox_config.py:L278-L376`, #358)

- [ ] **Step 5: Implement canonical-first configuration selection with compatibility fallback**

An explicit `--config` remains authoritative after syntax/type validation. Without it, resolve operator plus host config first to obtain `CODEREEVE_PROJECT_ROOT`; then select `.codereeve/config.env` or legacy `.bh/config.env`. Both existing is an error. Legacy-only works through the shared resolver and records compatibility use. (`src/codereeve/chain/sandbox_config.py:L252-L275`, `src/codereeve/chain/doctor.py:L219-L253`, #393)

- [ ] **Step 6: Update doctor context and configuration checks**

Doctor uses the same `ResolvedSandboxConfig` as daemon startup, reports canonical remediation, accepts legacy-only state during 0.2, blocks coexistence, and keeps installation/configuration/live phases non-mutating and secret-safe. (`src/codereeve/chain/doctor.py:L200-L253`, #358, #393)

- [ ] **Step 7: Run focused tests and commit**

Run:

```powershell
./.venv/Scripts/python.exe -m pytest -q tests/test_paths.py tests/chain/test_sandbox_config.py tests/chain/test_doctor.py tests/chain/test_cli_doctor_gate.py
./.venv/Scripts/python.exe -m ruff check src/codereeve/paths.py src/codereeve/chain/sandbox_config.py src/codereeve/chain/doctor.py tests/test_paths.py tests/chain/test_sandbox_config.py tests/chain/test_doctor.py
./.venv/Scripts/python.exe -m mypy src/codereeve/paths.py src/codereeve/chain/sandbox_config.py src/codereeve/chain/doctor.py
```

Commit: `feat(#393): select canonical CodeReeve configuration paths`

---

### Task 3: Python runtime environment and state cutover

**Files:**
- Modify: `src/codereeve/chain/registry.py`
- Modify: `src/codereeve/chain/obs_config.py`
- Modify: `src/codereeve/chain/cli.py`
- Modify: `src/codereeve/chain/app_auth.py`
- Modify: `src/codereeve/chain/app_private_key.py`
- Modify: `src/codereeve/chain/identity.py`
- Modify: `src/codereeve/chain/daemon/launch_gate.py`
- Modify: `src/codereeve/chain/daemon/poll.py`
- Modify: `src/codereeve/chain/daemon/work_unit.py`
- Modify: `src/codereeve/chain/escalation.py`
- Modify: `src/codereeve/chain/reconcile.py`
- Modify: `src/codereeve/chain/ruleset_status.py`
- Modify: affected tests under `tests/chain/`

**Interfaces:**
- Consumes: Task 1 `ResolvedEnvironment`; Task 2 `PathLayout` and selectors.
- Produces: runtime code that reads canonical names and canonical state while accepting centralized legacy resolution.
- Produces: `apply_resolved_environment(resolved: ResolvedEnvironment, target: MutableMapping[str, str]) -> None` as the sole compatibility materialization boundary.

- [ ] **Step 1: Write effect-order regression tests**

For real daemon startup and direct module entry points, set conflicting canonical/legacy values containing distinct secret sentinels. Patch network, secret-fetch, `os.chdir`, state writes, and daemon launch; assert return 1/raised `AliasConflictError`, zero effect calls, and neither sentinel in stdout/stderr/logs. (#393)

- [ ] **Step 2: Write canonical-only, legacy-only, identical-pair, and empty-value tests for each consumer family**

Cover registry identity/root, App IDs/provider/file path, observability numeric/string/path options, Slack/heartbeat, worker venv/feature branch, ruleset baseline, session report, and daemon-alive marker. Use parameterized tests over the alias catalog rather than one-off assertions. (`src/codereeve/chain/registry.py:L44-L83`, `src/codereeve/chain/obs_config.py:L161-L330`, #393)

- [ ] **Step 3: Run the new runtime tests and verify RED**

Run: `./.venv/Scripts/python.exe -m pytest -q tests/chain -k 'codereeve_env or alias_conflict or canonical_state'`

Expected: failures show direct `BH_*` reads and `.baton-harness`/`.bh` defaults.

- [ ] **Step 4: Resolve once before daemon effects and migrate Python consumers to canonical keys**

At the daemon command boundary, resolve operator/config layers before doctor live checks, secret fetch, registry load, `chdir`, or daemon launch. Materialize compatibility aliases only after a conflict-free snapshot exists. Update internal consumers to request `CODEREEVE_*` keys; keep vendored Symphony names unchanged. (`src/codereeve/chain/cli.py:L437-L465`, #393)

- [ ] **Step 5: Route every CodeReeve-owned state default through `PathLayout`**

Fresh runs write `.codereeve/runlog.jsonl`, `heartbeat`, `dispatch-counts.json`, `failure-counts.json`, `daemon.alive`, `session-report.json`, and `ruleset-baseline.json`. Legacy-only layouts remain readable/writable until migration; coexistence fails before daemon launch. The Symphony worktree root remains `.symphony/`. (`src/codereeve/chain/obs_config.py:L175-L204`, `src/codereeve/chain/daemon/poll.py:L302-L307`, `src/codereeve/chain/ruleset_status.py:L937-L955`)

- [ ] **Step 6: Remove value-bearing configuration diagnostics**

Warnings for invalid product variables identify the canonical key and fallback/default, never `%r` the supplied value. Extend redaction tests to include URLs, file paths containing credentials, PEM-looking values, and distinct conflict sentinels. (`src/codereeve/chain/obs_config.py:L214-L320`, #358, #393)

- [ ] **Step 7: Run the affected Python suite and commit**

Run:

```powershell
./.venv/Scripts/python.exe -m pytest -q tests/chain tests/test_app_auth.py tests/test_auth.py tests/test_hook_env_awareness.py tests/test_ruleset_status.py
./.venv/Scripts/python.exe -m ruff check src/codereeve tests/chain tests/test_app_auth.py tests/test_auth.py tests/test_hook_env_awareness.py tests/test_ruleset_status.py
./.venv/Scripts/python.exe -m mypy src/codereeve
```

Commit: `feat(#393): use canonical runtime environment and state`

---

### Task 4: Non-executing shell configuration bridge

**Files:**
- Create: `src/codereeve/config_bridge.py`
- Create: `tests/test_config_bridge.py`
- Modify: `bin/lib/load-config.sh`
- Modify: `bin/setup-env.sh`
- Modify: `bin/run-daemon.sh`
- Modify: `bin/init-sandbox.sh`
- Modify: `bin/provision-ruleset.sh`
- Modify: `bin/probe-merge-denial.sh`
- Modify: `bin/verify-recovery.sh`
- Modify: `bin/verify-block-escalation.sh`
- Modify: `config/WORKFLOW.md`
- Modify: `src/codereeve/resources/WORKFLOW.md`
- Modify: existing shell-facing tests under `tests/`

**Interfaces:**
- Consumes: Task 1 parser/resolver and Task 2 path selection.
- Produces: `python -m codereeve.config_bridge --format nul --host PATH --managed PATH`, emitting alternating UTF-8 NUL-delimited key/value records only after complete validation.
- Produces: `_codereeve_load_config` Bash function that returns nonzero on parser/resolver/path failure and never sources caller-controlled input.

- [ ] **Step 1: Write bridge byte-protocol and failure tests**

Assert the module emits `key\0value\0` records for canonical and compatibility aliases, emits nothing on any error, returns 1 for unsafe syntax/conflict/coexistence, preserves empty values, and keeps third-party keys unchanged. Test Windows and POSIX venv interpreter layouts by injecting the bridge command. (#393)

- [ ] **Step 2: Write real Bash non-execution tests**

Create host/managed fixtures containing `$(touch sentinel)`, backticks, redirection, and chained commands. Source `bin/lib/load-config.sh` in a subprocess, assert nonzero, assert no sentinel exists, and assert stderr contains only source path plus line number. Existing operator-env-wins tests must be rewritten to cover canonical/legacy exact-pair semantics. (`bin/lib/load-config.sh:L32-L95`, #393)

- [ ] **Step 3: Run bridge and shell-loader tests and verify RED**

Run:

```powershell
./.venv/Scripts/python.exe -m pytest -q tests/test_config_bridge.py tests/test_load_config_debug.py tests/test_load_config_export_visibility.py
```

Expected: bridge import fails and the malicious fixture executes under the old `source` implementation.

- [ ] **Step 4: Implement the NUL bridge and replace direct sourcing**

The Bash loader writes bridge output to a mode-600 `mktemp` file, checks the bridge exit status, then reads alternating NUL records with `read -r -d ''` and exports using `export "${key}=${value}"`. It deletes the temporary file on every return path. It never uses `eval`, command substitution to hold NUL bytes, or `source` on host/managed files.

- [ ] **Step 5: Canonicalize shell entry-point environment names**

Use `CODEREEVE_ROOT`, `CODEREEVE_VENV`, `CODEREEVE_PROJECT_ROOT`, and other catalog names internally. Resolve and export legacy aliases only through the bridge. Rename non-interface shell temporaries such as `BH_DAEMON_BIN` to `_codereeve_daemon_bin`; do not add them to the public alias catalog. Keep `BWS_*`, `GH_TOKEN`, and `ANTHROPIC_API_KEY` unchanged. (#393)

- [ ] **Step 6: Canonicalize shell-owned config/state paths except the #394 installer**

Fresh setup writes `~/.config/codereeve/host.env` and `<project>/.codereeve/config.env`; rule baseline writes `.codereeve/ruleset-baseline.json`; workflow hooks use canonical CLI/variables. Legacy-only loading still works. Do not change `bin/install-daemon-service.sh`, service units, activation, or `/etc` selection in this task. (#393, #394)

- [ ] **Step 7: Run shell-facing tests and static checks**

Run:

```powershell
./.venv/Scripts/python.exe -m pytest -q tests/test_config_bridge.py tests/test_load_config_debug.py tests/test_load_config_export_visibility.py tests/test_load_config_reuse_prompt_helper.py tests/test_run_daemon_repo_owner_env.py tests/test_setup_env_bws_optional.py tests/test_provision_ruleset_app_auth.py tests/test_provision_ruleset_idempotent.py
./.venv/Scripts/python.exe -m ruff check src/codereeve/config_bridge.py tests/test_config_bridge.py
./.venv/Scripts/python.exe -m mypy src/codereeve/config_bridge.py
```

If `shellcheck` is available, run it on every modified `.sh` file; otherwise record unavailability without installing it.

- [ ] **Step 8: Commit**

Commit: `feat(#393): resolve shell configuration without execution`

---

### Task 5: Read-only migration inventory and stable report

**Files:**
- Create: `src/codereeve/migration/__init__.py`
- Create: `src/codereeve/migration/model.py`
- Create: `src/codereeve/migration/inventory.py`
- Create: `tests/test_migration_inventory.py`
- Modify: `src/codereeve/redact.py`
- Modify: `tests/test_redact.py`

**Interfaces:**
- Produces: `MigrationStatus(str, Enum)` with `CURRENT`, `READY`, `BLOCKED`.
- Produces: immutable `MigrationAction`, `MigrationFinding`, `MigrationReport`, and `MigrationContext`.
- Produces: `inventory_migration(context: MigrationContext) -> MigrationReport` with no filesystem mutations.
- Produces: `MigrationReport.as_dict() -> dict[str, object]` schema version 1 and `exit_code` property.

- [ ] **Step 1: Write a mutation-spy contract for `inventory_migration`**

Patch `mkdir`, `open` write modes, `rename`, `replace`, `unlink`, `chmod`, secret fetch, subprocess/network, and service mutation seams to raise. Exercise fresh/current, legacy-ready, and blocked fixtures; assert none of the mutation/effect seams is called. (`docs/superpowers/specs/2026-09-07-codereeve-rename-design.md:L172-L183`, #393)

- [ ] **Step 2: Write the complete status matrix**

Cover:

- no legacy and no canonical state (`current`, 0);
- valid canonical-only (`current`, 0);
- each legacy source alone and all together (`ready`, 2);
- canonical/legacy coexistence at config, state, host, secrets, or service (`blocked`, 1);
- unsafe symlink/FIFO/socket/device/unreadable path (`blocked`, 1);
- duplicate target filename across `.bh` inputs and `.baton-harness/` (`blocked`, 1);
- canonical/legacy environment conflict (`blocked`, 1);
- stale/incomplete journal or held migration lease (`blocked`, 1 with recovery code);
- `.symphony/` present with otherwise current/ready states and never listed as an action.

The report must expose stable finding/action codes, scopes, and paths but no parsed values. (#393)

- [ ] **Step 3: Run inventory tests and verify RED**

Run: `./.venv/Scripts/python.exe -m pytest -q tests/test_migration_inventory.py`

Expected: collection fails because the migration package does not exist.

- [ ] **Step 4: Implement immutable schema-v1 report models**

Use only JSON primitives in `as_dict()`, sort actions/findings deterministically by scope and path, and keep status/exit mapping in the model. Text and JSON rendering in Task 8 must consume this same object.

- [ ] **Step 5: Implement lstat-only recursive inventory**

Reject symlinks and non-regular file/directory types before reading; do not follow directory links. Inventory `.bh/config.env`, `.bh/ruleset-baseline.json`, `.baton-harness/*`, host config, system secrets, and service unit. Mark the unit as `external_service_cutover` for #394 rather than an apply action. Verify target names are unique before status `ready`.

- [ ] **Step 6: Extend redaction to migration reports**

Feed every rendered detail through the existing redaction boundary and add sentinels for URLs, quoted values, PEM text, tokens, and command output. Paths may be reported only after masking credential-like URL/userinfo fragments. (#358, #393)

- [ ] **Step 7: Run focused tests and commit**

Run:

```powershell
./.venv/Scripts/python.exe -m pytest -q tests/test_migration_inventory.py tests/test_redact.py
./.venv/Scripts/python.exe -m ruff check src/codereeve/migration src/codereeve/redact.py tests/test_migration_inventory.py tests/test_redact.py
./.venv/Scripts/python.exe -m mypy src/codereeve/migration
```

Commit: `feat(#393): inventory CodeReeve migrations safely`

---

### Task 6: Writer quiescence lease and durable transaction journal

**Files:**
- Create: `src/codereeve/migration/lease.py`
- Create: `src/codereeve/migration/journal.py`
- Create: `tests/test_migration_lease.py`
- Create: `tests/test_migration_journal.py`
- Modify: `src/codereeve/chain/daemon/poll.py`
- Modify: `src/codereeve/chain/ruleset_status.py`
- Modify: daemon and ruleset tests

**Interfaces:**
- Produces: `WriterLease.acquire(path: Path, *, purpose: str) -> WriterLease`, a context manager holding a non-blocking exclusive OS lock.
- Produces: `MigrationJournal.create(transaction_root: Path, report: MigrationReport, now: datetime) -> MigrationJournal`.
- Produces: `journal.record(event: JournalEvent) -> None`, fsyncing the append before returning.
- Produces: `load_incomplete_journal(path: Path) -> RecoveryState` and `verify_manifest(path: Path) -> Manifest`.
- Consumes: Task 5 planned actions and paths; contains no move/copy logic.

- [ ] **Step 1: Write same-process and child-process lease contention tests**

Acquire the project writer lease in one process, prove daemon/second migration acquisition fails immediately without state changes, release it, then prove acquisition succeeds. Parameterize Windows and POSIX implementation seams. A lock diagnostic names path/purpose only. (#393)

- [ ] **Step 2: Write daemon lifetime lease tests**

Assert daemon startup acquires the writer lease before `daemon.alive`, heartbeat, runlog, or any state write; conflict returns before network/secret/launch effects; graceful and exceptional exits release the lease. Ruleset baseline publication must use the same lease helper. (#393)

- [ ] **Step 3: Run lease tests and verify RED**

Run: `./.venv/Scripts/python.exe -m pytest -q tests/test_migration_lease.py tests/chain/test_daemon.py tests/test_ruleset_status.py`

Expected: lease module import fails and existing writers do not acquire it.

- [ ] **Step 4: Implement cross-platform non-blocking lock backends**

Lock one byte of `<project>/.codereeve-migration.lock`; create the file mode 600, retain the descriptor for the context lifetime, and do not treat file existence alone as an active lease. Use injected backend seams for deterministic tests. Do not unlink a lock file owned by another process.

- [ ] **Step 5: Write journal durability and secrecy tests**

After each planned/before/after/rollback event, reopen the JSONL journal from a second descriptor and assert the event is parseable. Inject write, file-fsync, directory-fsync, partial-line, checksum, and permission failures. Manifest/journal must contain paths, modes, hashes, and operation IDs but no file contents or environment values. (#393)

- [ ] **Step 6: Implement private transaction directories, manifest, and append-only journal**

Create `<project>/.codereeve-migration/<UTC timestamp>-<nonce>/` mode 700, `manifest.json` mode 600 via write-temp/fsync/`os.replace`/parent-fsync, and `journal.jsonl` mode 600 via one append seam. Reject existing transaction IDs. `load_incomplete_journal` ignores no corruption: malformed/truncated events block automatic mutation and return manual recovery evidence.

- [ ] **Step 7: Run focused tests and commit**

Run:

```powershell
./.venv/Scripts/python.exe -m pytest -q tests/test_migration_lease.py tests/test_migration_journal.py tests/chain/test_daemon.py tests/test_ruleset_status.py
./.venv/Scripts/python.exe -m ruff check src/codereeve/migration/lease.py src/codereeve/migration/journal.py tests/test_migration_lease.py tests/test_migration_journal.py
./.venv/Scripts/python.exe -m mypy src/codereeve/migration/lease.py src/codereeve/migration/journal.py
```

Commit: `feat(#393): journal migration under a writer lease`

---

### Task 7: Transactional apply and interruption restoration

**Files:**
- Create: `src/codereeve/migration/transaction.py`
- Create: `tests/test_migration_transaction.py`
- Modify: `src/codereeve/migration/inventory.py`
- Modify: `src/codereeve/migration/__init__.py`

**Interfaces:**
- Consumes: Task 5 `MigrationReport`; Task 6 lease/journal.
- Produces: `apply_migration(context: MigrationContext, *, operations: FileOperations = REAL_FILE_OPERATIONS) -> AppliedMigration`.
- Produces: `restore_migration(manifest_path: Path, *, operations: FileOperations = REAL_FILE_OPERATIONS) -> RestorationResult` for #394.
- Produces: `verify_staged_migration(stage: Path, manifest: Manifest) -> None`.

- [ ] **Step 1: Write a successful managed-repository transaction test**

Fixture `.bh/config.env`, `.bh/ruleset-baseline.json`, and `.baton-harness/{runlog.jsonl,heartbeat}`. Assert apply:

1. takes the migration lease;
2. creates a private stage;
3. rewrites recognized product keys/default paths but preserves custom paths, comments, third-party variables, and empty values;
4. verifies canonical config with compatibility disabled;
5. renames each source to a timestamped sibling backup;
6. publishes one complete `.codereeve/` directory;
7. verifies hashes/modes/content;
8. retains manifest, journal, and every backup.

No intermediate canonical directory may be observable through the publication seam. (#393)

- [ ] **Step 2: Write host and system-secret transaction tests**

Use injected HOME/XDG/`etc_root` fixtures. Assert converted host and secrets files are staged on their destination filesystem, published only after verification, retain mode no broader than the source and at most 600, and leave timestamped sibling backups. Third-party secret assignments remain unchanged and never appear in reports/journals. (`docs/superpowers/specs/2026-09-07-codereeve-rename-design.md:L208-L210`, #393)

- [ ] **Step 3: Write collision, filesystem, permission, and quiescence RED tests**

Block before mutation for pre-existing canonical paths, duplicate target names, symlinks, unreadable files, different-device stage/destination, active daemon lease, unverified service state, and an incomplete prior transaction. Assert transaction-created stage paths are not mistaken for pre-existing canonical state. (#393)

- [ ] **Step 4: Write an injected-failure matrix for every mutation boundary**

Parameterize failures before/after stage file copy, config rewrite, hash verification, each source-to-backup rename, each canonical publish, file fsync, directory fsync, and final verification. For caught failures, assert reverse restoration, no partial canonical result, originals restored, backups/journal retained, and secret-safe failure output. (#393)

- [ ] **Step 5: Write interruption-recovery tests from persisted journals**

Construct journal prefixes representing process death after each durable event, then call `restore_migration` in a fresh process. Assert idempotent reverse recovery, hash verification before overwriting, refusal when external changes make restoration unsafe, and an explicit incomplete-recovery result that #394 can use to block restart. (#393, #394)

- [ ] **Step 6: Run transaction tests and verify RED**

Run: `./.venv/Scripts/python.exe -m pytest -q tests/test_migration_transaction.py`

Expected: collection fails because `transaction.py` does not exist.

- [ ] **Step 7: Implement stage/copy/verify/backup/publish with one injected operations seam**

Use `lstat` before every read/move, `O_NOFOLLOW` where available, streaming SHA-256, explicit mode preservation bounded to owner-readable/writeable confidentiality, file and directory fsync, same-device checks, and `os.replace` publication. Do not recursively merge a canonical directory. Journal `before` before each mutation and `after` only after fsync/verification.

- [ ] **Step 8: Implement reverse restoration and public coordinator primitives**

Restore completed operations in reverse journal order. Never replace a path whose current hash/type differs from the manifest. Return structured `COMPLETE`, `INCOMPLETE`, or `NOT_NEEDED` evidence; do not restart services or invoke systemctl. Export `inventory_migration`, `apply_migration`, and `restore_migration` from the package.

- [ ] **Step 9: Run focused tests and commit**

Run:

```powershell
./.venv/Scripts/python.exe -m pytest -q tests/test_migration_inventory.py tests/test_migration_lease.py tests/test_migration_journal.py tests/test_migration_transaction.py
./.venv/Scripts/python.exe -m ruff check src/codereeve/migration tests/test_migration_inventory.py tests/test_migration_lease.py tests/test_migration_journal.py tests/test_migration_transaction.py
./.venv/Scripts/python.exe -m mypy src/codereeve/migration
```

Commit: `feat(#393): apply and restore CodeReeve migrations`

---

### Task 8: `codereeve migrate` CLI and operator documentation

**Files:**
- Create: `src/codereeve/migration/cli.py`
- Create: `tests/test_migration_cli.py`
- Create: `docs/codereeve-migration.md`
- Modify: `src/codereeve/cli.py`
- Modify: `tests/test_codereeve_cli.py`
- Modify: `README.md`
- Modify: `tests/test_repository_workflow_contract.py`

**Interfaces:**
- Consumes: Tasks 5-7 inventory/apply/restore APIs.
- Produces: exact public grammar `codereeve migrate (--check | --apply) [--format text|json]`.
- Produces: `migration.cli.main(argv: Sequence[str], *, context_factory: ContextFactory = default_context) -> int`.
- Does not expose restoration as a standalone public command in 0.2; #394 imports `restore_migration` using the manifest path returned by apply.

- [ ] **Step 1: Write exact router and argument-grammar tests**

```python
@pytest.mark.parametrize(
    "argv",
    [
        ["migrate"],
        ["migrate", "--check", "--apply"],
        ["migrate", "--check", "--format=json"],
        ["migrate", "--apply", "--format", "yaml"],
        ["migrate", "--checks"],
    ],
)
def test_migrate_rejects_noncanonical_grammar(argv: list[str]) -> None:
    assert codereeve_main(argv) == 2
```

Use the accepted form `--format json`; reject abbreviations and equals-form aliases to keep the closed grammar consistent with the unified CLI. Assert help lists exactly check/apply/status semantics. (#392, #393)

- [ ] **Step 2: Write text/JSON/exit-code integration tests**

For current, ready, and every blocked class, assert text and JSON represent the same `MigrationReport`, JSON has schema version 1 and deterministic ordering, and exit codes are 0/2/1. Check mode leaves a filesystem snapshot and effect spy unchanged. Apply refuses anything except `ready`. (#393)

- [ ] **Step 3: Write CLI apply/recovery evidence tests**

Assert successful apply prints manifest/journal/backup/manual-restoration paths; caught failure reports completed rollback; interrupted/incomplete restoration reports `blocked` without claiming success. Neither text nor JSON may contain fixture secret values. #394 must be able to import the restoration API and use the returned manifest path.

- [ ] **Step 4: Run CLI tests and verify RED**

Run: `./.venv/Scripts/python.exe -m pytest -q tests/test_migration_cli.py tests/test_codereeve_cli.py`

Expected: `migrate` is rejected by the current command router.

- [ ] **Step 5: Implement the exact CLI route and renderers**

Add `migrate` to `HELP` and `HANDLERS`. The command creates one context from already conflict-checked environment/layout data, invokes check or apply once, renders the returned object, and returns its declared exit code. Catch only expected migration/config/OS failures and render secret-safe findings; unexpected exceptions return 1 with a generic safe message.

- [ ] **Step 6: Document staged check, apply, and restoration**

`docs/codereeve-migration.md` and README must include:

- canonical/legacy source table and exact precedence;
- unset versus empty and exact pair comparison;
- `--check` statuses/exit codes in text and JSON;
- daemon/writer quiescence prerequisite;
- `--apply` staging, backups, journal, and retained artifacts;
- manual restoration in reverse order using paths from the manifest;
- interruption/incomplete-recovery behavior;
- rollback before #394 service activation;
- `.symphony/` exclusion;
- examples that use only canonical commands/variables except inside a clearly labeled compatibility table.

Every durable factual claim cites the approved spec, #393/#394, a repo file line range, or an official URL with retrieval date. (`AGENTS.md`)

- [ ] **Step 7: Enforce documentation behavior with repository tests**

Assert documented commands appear in CLI help, status/exit-code tables match model constants, manual restoration names manifest fields that exist, no legacy CLI invocation appears outside compatibility sections, and every referenced local file is committed. (`tests/test_repository_workflow_contract.py`, `AGENTS.md`)

- [ ] **Step 8: Run focused tests and commit**

Run:

```powershell
./.venv/Scripts/python.exe -m pytest -q tests/test_migration_cli.py tests/test_codereeve_cli.py tests/test_repository_workflow_contract.py
./.venv/Scripts/python.exe -m ruff check src/codereeve/cli.py src/codereeve/migration/cli.py tests/test_migration_cli.py tests/test_codereeve_cli.py tests/test_repository_workflow_contract.py
./.venv/Scripts/python.exe -m mypy src/codereeve/cli.py src/codereeve/migration/cli.py
```

Commit: `docs(#393): document reversible CodeReeve migration`

---

### Task 9: Repository-wide compatibility contracts and final verification

**Files:**
- Create: `tests/test_codereeve_config_contract.py`
- Modify: `tests/test_repository_workflow_contract.py`
- Modify: files found by the contract tests only when they violate #393 scope

**Interfaces:**
- Consumes: every Task 1-8 public interface.
- Produces: permanent repository contracts that prevent unregistered `BH_*` environment reads, direct env-file execution, new legacy state writes, Symphony renames, and undocumented migration schema drift.

- [ ] **Step 1: Write the product-variable catalog coverage test**

Scan tracked Python/Bash/workflow/template files for actual environment reads/exports, not comments or local `_codereeve_*` variables. Every `BH_*`/`BATON_HARNESS_DIR` interface occurrence must map to `PRODUCT_ALIASES` or be in an explicit narrow exception set for compatibility diagnostics/fixtures. Every canonical product variable must either be consumed or documented. Third-party names must not appear in `PRODUCT_ALIASES`. (#393)

- [ ] **Step 2: Write the legacy path and non-execution contract tests**

Allow `.bh`, `.baton-harness`, legacy host/secrets paths only in migration, compatibility selectors, labeled compatibility docs, and tests. Reject direct `source`/`.`/`eval` of `host.env` or `config.env`. Reject new writes to legacy state outside compatibility selection. Assert `.symphony` remains unchanged in code/config/templates. (#393)

- [ ] **Step 3: Run contracts and fix only demonstrated violations**

Run: `./.venv/Scripts/python.exe -m pytest -q tests/test_codereeve_config_contract.py tests/test_repository_workflow_contract.py`

For each failure, either use the shared canonical interface or add a documented compatibility exception with an expiry of 0.4.0. Do not modify the #394 systemd installer beyond compatibility inventory assertions.

- [ ] **Step 4: Run the complete fast/static validation set**

Run:

```powershell
./.venv/Scripts/python.exe -m pytest -q -m fast
./.venv/Scripts/python.exe -m ruff check .
./.venv/Scripts/python.exe -m mypy src
git diff --check
```

Feed raw committed LF blobs to pinned `ruff format --check --stdin-filename` so CRLF working-tree conversion cannot hide a new formatter failure.

- [ ] **Step 5: Run complete behavioral and artifact verification**

Run the full suite with a worktree-local basetemp, then rerun `TestRegisterGitExcludeOnLinkedWorktree::test_non_git_directory_returns_rc_no_git` under a verified non-Git OS temp root. Run `codereeve verify --python 3.10 --python 3.13` with an isolated `UV_CACHE_DIR`. Inspect a fresh wheel and sdist for canonical identity, provenance, migration modules/docs, exact compatibility scripts, and absence of transaction/test/workspace directories.

- [ ] **Step 6: Audit persisted artifacts and issue acceptance criteria**

Run `git diff feature/396-codereeve-0-2-cutover...HEAD --stat`, reconcile every claimed deliverable, and verify every path cited by a committed doc/script with `git ls-tree HEAD -- <path>`. Walk every #393 acceptance checkbox and record the exact test/file evidence. Confirm no #394 service activation, #395 repository-doc identity, or 0.3/0.4 lifecycle work leaked into this branch.

- [ ] **Step 7: Commit the contract/audit changes**

Commit: `test(#393): enforce CodeReeve configuration migration contracts`

---

## Self-Review Checklist

- Every #393 acceptance item maps to at least one task: alias/parser semantics (1-4), canonical paths/runtime (2-4), read-only status (5/8), quiescence/journal (6), transactional apply/recovery (7), CLI/docs (8), and exhaustive contracts/verification (9).
- #394 owns service mutation; this plan supplies only inventory, lease, manifest, apply, and restoration primitives.
- The plan defines every cross-task type/function before a consumer uses it.
- There are no placeholder implementation steps; every test cycle names a command, expected failure, implementation boundary, and commit.
- Durable decision claims cite the approved spec, current repository lines, GitHub issues, or official Python documentation with retrieval date.
