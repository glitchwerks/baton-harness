# GitHub App Private-Key Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow every GitHub App authentication path to select either Bitwarden Secrets Manager or a securely read host file for the App private key, without making BWS mandatory for file-only deployments.

**Architecture:** A new `chain.app_private_key` module owns provider resolution, conditional BWS selection, secure one-time key loading, and a secret-safe shell query. Sandbox loading, authentication, doctor, and maintained shell scripts consume that shared policy; the existing `InstallationTokenProvider` continues to own refreshable installation-token behavior. The daemon bootstrap owns the lifetime of `BWS_ACCESS_TOKEN` across all configured vault reads and removes it in `finally` on every path. (`docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L94-L197`)

**Tech Stack:** Python 3.10+, PyJWT with cryptography, pytest, strict mypy, Ruff, Bash, and systemd examples. (`pyproject.toml:L9-L30`, `pyproject.toml:L60-L99`)

**Spec:** `docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md`

## Global Constraints

- GitHub App authentication remains mandatory; the only providers are the exact lowercase values `bws` and `file`, with no implicit default and no PEM-content environment variable. (`docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L9-L14`, `docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L69-L84`)
- `BH_GITHUB_APP_KEY_PROVIDER` selects only the App PEM source. Optional `BWS_GH_TOKEN_SECRET_ID` and `BWS_HEARTBEAT_PING_URL_SECRET_ID` remain valid with either provider and independently make BWS necessary. (`docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L16-L21`, `docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L225-L241`)
- Provider `bws` requires one valid `BWS_PEM_SECRET_ID` and forbids `BH_GITHUB_APP_PRIVATE_KEY_FILE`; provider `file` requires one absolute `BH_GITHUB_APP_PRIVATE_KEY_FILE` and forbids `BWS_PEM_SECRET_ID`. (`docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L54-L80`)
- Resolve all non-empty environment overrides and validate the complete provider configuration before mutating `os.environ`; export only the selected source and remove the unselected source. (`docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L86-L92`)
- The file loader accepts only a non-symlink regular file with no group/world permission bits, reads at most 1 MiB through one open descriptor, compares pre/post-open identity where meaningful, decodes UTF-8, and rejects empty content. (`docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L199-L223`)
- Every loaded key must successfully sign a throwaway RS256 App JWT before any GitHub API call; key material, BWS tokens, raw vault output, and credential-bearing URLs never appear in errors or logs. (`docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L129-L142`, `docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L273-L294`)
- Once bootstrap begins, remove `BWS_ACCESS_TOKEN` from `os.environ` in `finally`, including file-only, provider-resolution, vault, file, and JWT-signing failures. (`docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L178-L197`, `docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L290-L294`)
- Preserve installation-token minting, caching, refresh margins, by-value propagation, and the App/worker authority boundary. (`docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L129-L137`, `docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L354-L367`)
- Host creation, private-key generation, copying, enrollment, rotation, and production path selection remain external. Maintained scripts consume an already secured/mounted key path. (`docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L243-L264`, `docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L354-L364`)
- Before the first test, create the worktree-local environment with `uv venv .venv` and `uv pip install --python .venv/Scripts/python.exe -e ".[dev]"`; thereafter invoke Python only as `.venv/Scripts/python.exe`. The worktree had no local environment when the design was written. (`docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L330-L333`; `README.md:L121-L164`)

---

### Task 1: Shared Provider Policy and Secure Loader

**Files:**

- Create: `src/baton_harness/chain/app_private_key.py`
- Create: `tests/chain/test_app_private_key.py`

**Interfaces:**

- Consumes: `Mapping[str, str]`, an injected `SecretFetcher`, and standard `os`/`stat` file-descriptor operations.
- Produces: `AppPrivateKeyProvider`, frozen `AppPrivateKeyConfig`, `AppPrivateKeyConfigError`, `AppPrivateKeyLoadError`, `resolve_app_private_key_config(values: Mapping[str, str]) -> AppPrivateKeyConfig`, `requires_bws(config: AppPrivateKeyConfig, values: Mapping[str, str]) -> bool`, `load_app_private_key(config: AppPrivateKeyConfig, *, bws_access_token: str, fetch_secret: SecretFetcher) -> str`, and `main(argv: list[str]) -> int` for the secret-safe `requires-bws` shell query.

**Source basis:** The provider module is the single policy owner, must remain independent of `app_auth`, and must implement the full selector matrix and secure file procedure. (`docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L94-L118`, `docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L199-L223`, `docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L273-L288`)

- [ ] **Step 1: Create the worktree-local development environment**

Run:

```bash
uv venv .venv
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"
```

Expected: both commands exit 0 and `.venv/Scripts/python.exe` exists.

- [ ] **Step 2: Write failing resolver and BWS-composition tests**

Create `tests/chain/test_app_private_key.py` with constants for canonical UUIDs and a parametrized matrix. Use exact assertions so missing, unknown, malformed, and conflicting inputs cannot collapse into one permissive path:

```python
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from baton_harness.chain.app_private_key import (
    AppPrivateKeyConfigError,
    AppPrivateKeyLoadError,
    AppPrivateKeyProvider,
    load_app_private_key,
    requires_bws,
    resolve_app_private_key_config,
)

_PEM_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_OPTIONAL_ID = "11111111-2222-3333-4444-555555555555"


@pytest.mark.parametrize(
    ("values", "match"),
    [
        ({}, "BH_GITHUB_APP_KEY_PROVIDER"),
        ({"BH_GITHUB_APP_KEY_PROVIDER": "vault"}, "unsupported"),
        ({"BH_GITHUB_APP_KEY_PROVIDER": "bws"}, "BWS_PEM_SECRET_ID"),
        (
            {
                "BH_GITHUB_APP_KEY_PROVIDER": "bws",
                "BWS_PEM_SECRET_ID": "not-a-uuid",
            },
            "BWS_PEM_SECRET_ID",
        ),
        (
            {
                "BH_GITHUB_APP_KEY_PROVIDER": "bws",
                "BWS_PEM_SECRET_ID": _PEM_ID,
                "BH_GITHUB_APP_PRIVATE_KEY_FILE": "/run/key.pem",
            },
            "conflicting",
        ),
        ({"BH_GITHUB_APP_KEY_PROVIDER": "file"}, "PRIVATE_KEY_FILE"),
        (
            {
                "BH_GITHUB_APP_KEY_PROVIDER": "file",
                "BH_GITHUB_APP_PRIVATE_KEY_FILE": "relative.pem",
            },
            "absolute",
        ),
        (
            {
                "BH_GITHUB_APP_KEY_PROVIDER": "file",
                "BH_GITHUB_APP_PRIVATE_KEY_FILE": "/run/key.pem",
                "BWS_PEM_SECRET_ID": _PEM_ID,
            },
            "conflicting",
        ),
    ],
)
def test_invalid_provider_matrix_rejected(
    values: dict[str, str], match: str
) -> None:
    with pytest.raises(AppPrivateKeyConfigError, match=match):
        resolve_app_private_key_config(values)


def test_bws_and_file_configs_are_discriminated() -> None:
    bws = resolve_app_private_key_config(
        {
            "BH_GITHUB_APP_KEY_PROVIDER": "bws",
            "BWS_PEM_SECRET_ID": _PEM_ID,
        }
    )
    file = resolve_app_private_key_config(
        {
            "BH_GITHUB_APP_KEY_PROVIDER": "file",
            "BH_GITHUB_APP_PRIVATE_KEY_FILE": "/run/key.pem",
        }
    )
    assert bws.provider is AppPrivateKeyProvider.BWS
    assert bws.bws_secret_id == _PEM_ID
    assert bws.file_path is None
    assert file.provider is AppPrivateKeyProvider.FILE
    assert file.file_path == Path("/run/key.pem")
    assert file.bws_secret_id is None


@pytest.mark.parametrize(
    ("provider_values", "optional_values", "expected"),
    [
        ({"BH_GITHUB_APP_KEY_PROVIDER": "bws", "BWS_PEM_SECRET_ID": _PEM_ID}, {}, True),
        ({"BH_GITHUB_APP_KEY_PROVIDER": "file", "BH_GITHUB_APP_PRIVATE_KEY_FILE": "/run/key.pem"}, {}, False),
        ({"BH_GITHUB_APP_KEY_PROVIDER": "file", "BH_GITHUB_APP_PRIVATE_KEY_FILE": "/run/key.pem"}, {"BWS_GH_TOKEN_SECRET_ID": _OPTIONAL_ID}, True),
        ({"BH_GITHUB_APP_KEY_PROVIDER": "file", "BH_GITHUB_APP_PRIVATE_KEY_FILE": "/run/key.pem"}, {"BWS_HEARTBEAT_PING_URL_SECRET_ID": _OPTIONAL_ID}, True),
    ],
)
def test_requires_bws_composes_provider_and_optional_consumers(
    provider_values: dict[str, str],
    optional_values: dict[str, str],
    expected: bool,
) -> None:
    config = resolve_app_private_key_config(provider_values)
    assert requires_bws(config, optional_values) is expected
```

- [ ] **Step 3: Write failing loader, race, permission, and shell-query tests**

Add tests with these exact names and assertions:

- `test_bws_loader_fetches_once_with_explicit_access_token`: result equals the mock PEM and the fetcher receives the selected UUID/token exactly once.
- `test_bws_loader_rejects_empty_access_token_without_fetch`: raises `AppPrivateKeyLoadError` matching `access token`, with `fetch.assert_not_called()`.
- `test_bws_loader_rejects_empty_secret`: raises `AppPrivateKeyLoadError` matching `empty` when the fetcher returns `""`.
- `test_file_loader_reads_owner_only_regular_file_once`: returns the exact UTF-8 file contents from a `0600` file.
- `test_file_loader_never_calls_bws`: succeeds from the file and asserts `fetch.assert_not_called()`.
- `test_file_loader_rejects_missing_path`: raises `AppPrivateKeyLoadError` matching `unavailable`.
- `test_file_loader_rejects_symlink`: creates a symlink to a `0600` target and raises with text matching `symbolic link`.
- `test_file_loader_rejects_directory`: passes an owner-only directory and raises with text matching `regular file`.
- `test_file_loader_rejects_group_or_world_bits`: parameterizes `0o004`, `0o020`, `0o040`, and `0o077`; every case raises with text matching `permissions`.
- `test_file_loader_accepts_0400_and_0600`: parameterizes those two modes and returns the exact contents.
- `test_file_loader_rejects_permission_error`: patches `os.open` to raise `PermissionError` and asserts the safe error excludes the path and OS exception text.
- `test_file_loader_rejects_replacement_race`: changes the nonzero `(st_dev, st_ino)` returned by `os.fstat`, raises with text matching `changed`, and verifies `os.close` receives the descriptor.
- `test_file_loader_rejects_more_than_one_mib`: writes 1,048,577 bytes and raises with text matching `maximum size`.
- `test_file_loader_rejects_empty_file`: writes zero bytes and raises with text matching `empty`.
- `test_file_loader_rejects_non_utf8`: writes `b"\xff\xfe"` and raises with text matching `UTF-8`.
- `test_requires_bws_cli_prints_only_boolean`: seeds valid file-only environment values, calls `main(["requires-bws"])`, and asserts exit 0, stdout `false\n`, and empty stderr.
- `test_requires_bws_cli_reports_safe_config_error`: seeds an unknown provider plus sentinel secret values, asserts exit 2, and proves neither sentinel appears in captured output.

Use this common file helper and concrete checks for the high-risk cases:

```python
def _file_config(path: Path):
    return resolve_app_private_key_config(
        {
            "BH_GITHUB_APP_KEY_PROVIDER": "file",
            "BH_GITHUB_APP_PRIVATE_KEY_FILE": str(path.resolve()),
        }
    )


def test_file_loader_reads_owner_only_regular_file_once(tmp_path: Path) -> None:
    key = tmp_path / "app.pem"
    key.write_text("private-key", encoding="utf-8")
    key.chmod(0o600)
    fetch = Mock()
    assert load_app_private_key(
        _file_config(key), bws_access_token="", fetch_secret=fetch
    ) == "private-key"
    fetch.assert_not_called()


def test_bws_loader_fetches_once_with_explicit_access_token() -> None:
    fetch = Mock(return_value="private-key")
    config = resolve_app_private_key_config(
        {"BH_GITHUB_APP_KEY_PROVIDER": "bws", "BWS_PEM_SECRET_ID": _PEM_ID}
    )
    assert load_app_private_key(
        config, bws_access_token="machine-token", fetch_secret=fetch
    ) == "private-key"
    fetch.assert_called_once_with(_PEM_ID, access_token="machine-token")


def test_file_loader_rejects_more_than_one_mib(tmp_path: Path) -> None:
    key = tmp_path / "app.pem"
    key.write_bytes(b"x" * (1024 * 1024 + 1))
    key.chmod(0o600)
    with pytest.raises(AppPrivateKeyLoadError, match="maximum size"):
        load_app_private_key(
            _file_config(key), bws_access_token="", fetch_secret=Mock()
        )
```

Patch `os.open` to raise `PermissionError` for the unreadable test. Patch `os.fstat` to return a `stat_result` whose nonzero `(st_dev, st_ino)` differs from `os.lstat` for the replacement test; assert `AppPrivateKeyLoadError` and assert the descriptor was closed. On platforms where symlink creation is unavailable, use `pytest.skip` only around `symlink_to`, not around the loader assertion.

- [ ] **Step 4: Run the provider tests to verify they fail**

Run:

```bash
.venv/Scripts/python.exe -m pytest tests/chain/test_app_private_key.py -v
```

Expected: collection fails because `baton_harness.chain.app_private_key` does not exist.

- [ ] **Step 5: Implement the pure resolver, secure loader, and shell query**

Create `app_private_key.py` with this public shape and keep helpers private:

```python
class AppPrivateKeyProvider(str, Enum):
    BWS = "bws"
    FILE = "file"


@dataclass(frozen=True)
class AppPrivateKeyConfig:
    provider: AppPrivateKeyProvider
    bws_secret_id: str | None = None
    file_path: Path | None = None


class SecretFetcher(Protocol):
    def __call__(
        self, secret_id: str, *, access_token: str
    ) -> str:
        raise NotImplementedError


def requires_bws(
    config: AppPrivateKeyConfig,
    values: Mapping[str, str],
) -> bool:
    return config.provider is AppPrivateKeyProvider.BWS or any(
        values.get(key, "")
        for key in (
            "BWS_GH_TOKEN_SECRET_ID",
            "BWS_HEARTBEAT_PING_URL_SECRET_ID",
        )
    )


```

Implement file reading with `os.lstat`, `stat.S_ISLNK`, `os.open` using `O_RDONLY | O_CLOEXEC | O_NOFOLLOW` when the latter flags exist, `os.fstat`, `stat.S_ISREG`, `st_mode & 0o077`, meaningful device/inode comparison, a loop of `os.read` calls capped at `1_048_577` bytes, UTF-8 decode, and `os.close` in `finally`. Normalize all `OSError`, empty, oversize, and decode failures to secret-safe `AppPrivateKeyLoadError` messages that name only the provider and failure class. For the BWS path, reject an empty access token before invoking the fetcher and translate fetch failures without embedding exception text.

Implement `main(["requires-bws"])` by resolving `os.environ`, printing exactly `true` or `false`, and returning 0. Invalid usage returns 2; provider configuration errors return 2 with a safe `app_private_key:` stderr line. This command is the maintained shell seam that prevents Bash from reimplementing provider/BWS composition.

- [ ] **Step 6: Run targeted quality checks and commit**

Run:

```bash
.venv/Scripts/python.exe -m pytest tests/chain/test_app_private_key.py -v
.venv/Scripts/python.exe -m ruff check src/baton_harness/chain/app_private_key.py tests/chain/test_app_private_key.py
.venv/Scripts/python.exe -m mypy src/baton_harness/chain/app_private_key.py
git add src/baton_harness/chain/app_private_key.py tests/chain/test_app_private_key.py
git commit -m "feat(#359): add App private-key providers"
```

Expected: tests, Ruff, and mypy pass; the commit succeeds.

---

### Task 2: Provider-Aware Sandbox Configuration

**Files:**

- Modify: `src/baton_harness/chain/sandbox_config.py:59-77`
- Modify: `src/baton_harness/chain/sandbox_config.py:108-131`
- Modify: `src/baton_harness/chain/sandbox_config.py:173-198`
- Modify: `src/baton_harness/chain/sandbox_config.py:232-345`
- Modify: `tests/chain/test_sandbox_config.py`

**Interfaces:**

- Consumes: `resolve_app_private_key_config` from Task 1 and the existing `resolve_overridable_keys` environment precedence helper.
- Produces: `SandboxConfig.github_app_key_provider: AppPrivateKeyProvider`, `SandboxConfig.bws_pem_secret_id: str | None`, and `SandboxConfig.github_app_private_key_file: Path | None`; a validated selected source exported to daemon environment.

**Source basis:** Sandbox config currently requires and exports BWS unconditionally; it must instead preserve existing non-empty environment precedence, validate before mutation, and translate provider errors to `SandboxConfigError`. (`src/baton_harness/chain/sandbox_config.py:L59-L77`, `src/baton_harness/chain/sandbox_config.py:L206-L345`; `docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L120-L127`, `docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L273-L280`)

- [ ] **Step 1: Update shared valid-config fixtures and write failing provider integration tests**

Add `BH_GITHUB_APP_KEY_PROVIDER=bws` to the canonical valid config fixture and retain its existing BWS PEM UUID. Add focused tests with these exact names and outcomes:

- `test_file_provider_loads_absolute_path_and_removes_bws_source`: returns `AppPrivateKeyProvider.FILE` plus the resolved `Path`, exports the selector/path, and removes an ambient `BWS_PEM_SECRET_ID`.
- `test_bws_provider_removes_file_source`: returns `AppPrivateKeyProvider.BWS` plus the selected UUID, exports the selector/UUID, and removes an ambient file path.
- `test_provider_and_sources_honor_nonempty_environment_override`: environment values replace different file values as one resolved provider tuple.
- `test_empty_environment_override_falls_back_to_file_value`: empty selector/source environment values do not erase valid file settings.
- `test_missing_or_unknown_provider_fails_before_repo_probe`: parameterizes `""`, `"vault"`, `"BWS"`, and `"FILE"`, asserts `SandboxConfigError`, and asserts the repo-probe mock is untouched.
- `test_conflicting_bws_and_file_sources_fail_before_environment_mutation`: both source keys cause `SandboxConfigError` and leave the environment snapshot unchanged.
- `test_relative_file_source_fails_before_environment_mutation`: a relative path fails and leaves the environment snapshot unchanged.
- `test_optional_bws_secret_ids_remain_valid_in_file_mode`: both canonical optional UUIDs survive parsing and appear on the returned `SandboxConfig`.

For both “before mutation” tests, snapshot the relevant environment mapping, pass a `run` mock, assert `run.assert_not_called()`, and assert no provider/source environment value changed after `SandboxConfigError`.

- [ ] **Step 2: Run the focused sandbox tests to verify they fail**

Run:

```bash
.venv/Scripts/python.exe -m pytest tests/chain/test_sandbox_config.py -k "provider or source or optional_bws" -v
```

Expected: failures show the selector/path are not parsed or represented and BWS remains unconditional.

- [ ] **Step 3: Integrate the shared resolver into `read_and_validate`**

Make `_REQUIRED_KEYS` contain only repository/App identity plus `BH_GITHUB_APP_KEY_PROVIDER`. Add both provider source keys to `_ENV_OVERRIDABLE_KEYS`. After resolving and validating base/optional values, call:

```python
try:
    app_key_config = resolve_app_private_key_config(resolved)
except AppPrivateKeyConfigError as exc:
    raise SandboxConfigError(str(exc)) from exc
```

Do this before the repo probe and before any `os.environ` assignment. After successful validation, export `BH_GITHUB_APP_KEY_PROVIDER`, export exactly the selected source, and call `os.environ.pop("BWS_PEM_SECRET_ID", None)` in file mode or `os.environ.pop("BH_GITHUB_APP_PRIVATE_KEY_FILE", None)` in BWS mode. Return the selected enum/path/optional UUID fields in `SandboxConfig`; preserve the derived `BWS_APP_ID` and `BWS_INSTALLATION_ID` compatibility exports and the existing repository probe.

- [ ] **Step 4: Run the complete sandbox-config suite and static checks**

Run:

```bash
.venv/Scripts/python.exe -m pytest tests/chain/test_sandbox_config.py -v
.venv/Scripts/python.exe -m ruff check src/baton_harness/chain/sandbox_config.py tests/chain/test_sandbox_config.py
.venv/Scripts/python.exe -m mypy src/baton_harness/chain/sandbox_config.py
```

Expected: all existing precedence/parser/repo-probe regressions and new provider tests pass.

- [ ] **Step 5: Commit the sandbox configuration slice**

Run:

```bash
git add src/baton_harness/chain/sandbox_config.py tests/chain/test_sandbox_config.py
git commit -m "feat(#359): resolve App key provider in sandbox config"
```

Expected: commit succeeds with only the two listed files.

---

### Task 3: Authentication and Daemon Bootstrap

**Files:**

- Modify: `src/baton_harness/chain/app_auth.py:14-25`
- Modify: `src/baton_harness/chain/app_auth.py:354-459`
- Modify: `src/baton_harness/chain/app_auth.py:592-682`
- Modify: `src/baton_harness/chain/cli.py:59-174`
- Modify: `tests/test_app_auth.py`
- Modify: `tests/chain/test_cli_bootstrap_vault.py`
- Modify: `tests/chain/test_bootstrap_hook_env_no_leak.py`

**Interfaces:**

- Consumes: Task 1’s resolver/loader/config types, `bws_client.fetch_secret`, existing `build_app_jwt`, and existing `InstallationTokenProvider`.
- Produces: `build_installation_token_provider(app_id: str, app_key_config: AppPrivateKeyConfig, installation_id: int, *, bws_access_token: str, fetch_secret: SecretFetcher) -> InstallationTokenProvider`; provider-aware `app_auth.main`; daemon `cli.bootstrap_secrets` with unconditional bootstrap-token scrubbing.

**Source basis:** The existing provider retains PEM material for refresh and the daemon fetches optional BWS values before constructing it; the new flow must preserve those destinations while moving token ownership to one `try/finally`. (`src/baton_harness/chain/app_auth.py:L241-L315`, `src/baton_harness/chain/cli.py:L130-L174`; `docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L129-L142`, `docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L178-L197`, `docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L225-L241`)

- [ ] **Step 1: Write failing provider-aware authentication tests**

Update BWS fixtures with `BH_GITHUB_APP_KEY_PROVIDER=bws`. Add a file fixture that writes a generated RSA PEM to an absolute `0600` path. Add these exact tests to `tests/test_app_auth.py`:

- `test_build_provider_bws_loads_once_and_proves_jwt_before_return`: uses a real generated RSA PEM, asserts one BWS fetch, and verifies the returned provider can mint through the mocked HTTP transport.
- `test_build_provider_file_never_calls_bws`: reads the `0600` fixture and asserts the BWS fetch mock is untouched.
- `test_build_provider_rejects_malformed_pem_before_github_call`: supplies non-key text, asserts `AppAuthError`, and asserts `_github_http_post` is untouched.
- `test_cli_jwt_file_provider_prints_valid_jwt_without_bws`: unsets `BWS_ACCESS_TOKEN`, decodes stdout with the generated public key, and asserts exit 0/empty stderr.
- `test_cli_token_file_provider_mints_without_bws`: unsets BWS, stubs `_github_http_post`, and asserts stdout is exactly the installation token.
- `test_cli_bws_provider_requires_access_token`: valid selector/UUID without a token returns exit 2 and names only `BWS_ACCESS_TOKEN`.
- `test_cli_file_provider_does_not_require_access_token`: valid file configuration without a token reaches signing successfully.
- `test_cli_scrubs_ambient_bws_token_on_success_and_failure`: parameterizes `jwt`/`token` and successful/invalid PEM paths; `BWS_ACCESS_TOKEN` is absent after every call.
- `test_cli_errors_never_include_pem_or_access_token`: sentinel PEM/token strings are absent from combined stdout/stderr.

Patch `_github_http_post` with a mock and assert it is not called for malformed PEM. Decode successful JWT output with the generated public key, retaining the existing CLI output contract.

- [ ] **Step 2: Write failing daemon composition and scrubbing tests**

In `tests/chain/test_cli_bootstrap_vault.py`, add a valid file-provider environment helper and these exact cases:

- `test_file_only_bootstrap_never_calls_bws`: generated `0600` PEM, no optional locators/token, successful provider return, and `fetch_secret.assert_not_called()`.
- `test_file_provider_with_optional_pat_fetches_only_pat_and_key_from_file`: one fetch for the PAT UUID, no PEM UUID fetch, and `_BOOTSTRAPPED_GH_TOKEN` holds the PAT by value.
- `test_file_provider_with_optional_heartbeat_fetches_only_heartbeat_and_key_from_file`: one fetch for the heartbeat UUID and the existing heartbeat destination is preserved.
- `test_file_provider_with_optional_bws_secret_requires_access_token`: missing access token raises before the optional fetch and provider construction.
- `test_bootstrap_scrubs_bws_access_token_for_every_failure_stage`: parameterizes `resolve`, `optional_fetch`, `key_load`, and `jwt_probe`; each injected failure leaves the ambient token absent.
- `test_bootstrap_never_writes_installation_token_to_environment`: returned sentinel token is absent from every `os.environ` value.

Update `tests/chain/test_bootstrap_hook_env_no_leak.py` so the fixture includes the explicit BWS provider and assert the expanded App-key/BWS variables do not appear in any worker-facing environment produced after bootstrap.

- [ ] **Step 3: Run the new authentication/bootstrap tests to verify they fail**

Run:

```bash
.venv/Scripts/python.exe -m pytest tests/test_app_auth.py tests/chain/test_cli_bootstrap_vault.py tests/chain/test_bootstrap_hook_env_no_leak.py -k "provider or scrubs or optional or malformed or never_calls" -v
```

Expected: file-provider cases fail and current signatures still require `BWS_PEM_SECRET_ID`/ambient BWS token.

- [ ] **Step 4: Refactor provider construction around explicit values**

Change `build_installation_token_provider` to load once and prove the PEM before storing it:

```python
def build_installation_token_provider(
    app_id: str,
    app_key_config: AppPrivateKeyConfig,
    installation_id: int,
    *,
    bws_access_token: str,
    fetch_secret: SecretFetcher,
) -> InstallationTokenProvider:
    try:
        private_key_pem = load_app_private_key(
            app_key_config,
            bws_access_token=bws_access_token,
            fetch_secret=fetch_secret,
        )
    except AppPrivateKeyLoadError as exc:
        raise AppAuthError(str(exc)) from exc
    try:
        build_app_jwt(app_id, private_key_pem, now=int(time.time()))
    except Exception as exc:
        raise AppAuthError(
            f"{app_key_config.provider.value} App private key could not sign "
            "an RS256 JWT"
        ) from exc
    return InstallationTokenProvider(
        app_id=app_id,
        private_key_pem=private_key_pem,
        installation_id=installation_id,
        http_post=_github_http_post,
    )
```

The loader’s custom exception text is safe by Task 1’s contract; the JWT clause identifies only provider and failure class. Adapt the legacy `app_auth.bootstrap_secrets` test seam to accept `AppPrivateKeyConfig`, use an explicit access-token value, and scrub the ambient token in `finally` while preserving its `(token, expires_at)` return contract.

- [ ] **Step 5: Make daemon bootstrap own all BWS reads and scrubbing**

Capture the access token once, then resolve the App-key config, calculate `requires_bws`, and execute all selected reads inside one `try/finally` so even resolver failures scrub the token:

```python
access_token = os.environ.get("BWS_ACCESS_TOKEN", "")
try:
    app_key_config = resolve_app_private_key_config(os.environ)
    if requires_bws(app_key_config, os.environ) and not access_token:
        raise AppAuthError(
            "BWS_ACCESS_TOKEN is required by the selected secret configuration"
        )
    # Fetch only configured PAT/heartbeat values, preserving operator overrides.
    # Build the App provider with app_key_config and the explicit access_token.
finally:
    os.environ.pop("BWS_ACCESS_TOKEN", None)
```

Keep `_BOOTSTRAPPED_GH_TOKEN` by value, keep the heartbeat URL’s existing destination, and never write an installation token into `os.environ`. Translate provider errors to `AppAuthError` with safe remediation.

- [ ] **Step 6: Make `app_auth {jwt|token}` provider-aware**

Require `BH_GITHUB_APP_ID` and `BH_GITHUB_APP_KEY_PROVIDER` in both modes and the installation ID only in token mode. Resolve the selected source with Task 1, require `BWS_ACCESS_TOKEN` only for the BWS App-key provider, load and prove the PEM, and always pop an ambient BWS token in `finally`. Preserve exit 2 for usage/configuration and exit 1 for load/sign/mint failures; stdout contains only the successful JWT/token.

- [ ] **Step 7: Run all auth/bootstrap regressions and commit**

Run:

```bash
.venv/Scripts/python.exe -m pytest tests/test_app_auth.py tests/chain/test_cli_bootstrap_vault.py tests/chain/test_bootstrap_hook_env_no_leak.py tests/chain/test_cli.py -v
.venv/Scripts/python.exe -m ruff check src/baton_harness/chain/app_auth.py src/baton_harness/chain/cli.py tests/test_app_auth.py tests/chain/test_cli_bootstrap_vault.py tests/chain/test_bootstrap_hook_env_no_leak.py
.venv/Scripts/python.exe -m mypy src/baton_harness/chain/app_auth.py src/baton_harness/chain/cli.py
git add src/baton_harness/chain/app_auth.py src/baton_harness/chain/cli.py tests/test_app_auth.py tests/chain/test_cli_bootstrap_vault.py tests/chain/test_bootstrap_hook_env_no_leak.py
git commit -m "feat(#359): bootstrap App keys from selected provider"
```

Expected: targeted suites and static checks pass; existing token-refresh and hook-PAT regressions remain green.

---

### Task 4: Conditional Doctor Prerequisites and Key Probe

**Files:**

- Modify: `src/baton_harness/chain/doctor.py:14-41`
- Modify: `src/baton_harness/chain/doctor.py:283-300`
- Modify: `src/baton_harness/chain/doctor.py:430-543`
- Modify: `src/baton_harness/chain/doctor.py:905-956`
- Modify: `src/baton_harness/chain/doctor.py:959-1049`
- Modify: `tests/chain/test_doctor.py`
- Modify: `tests/chain/test_cli_doctor_gate.py`

**Interfaces:**

- Consumes: Task 1 resolver, `requires_bws`, secure loader, and Task 2 environment precedence behavior.
- Produces: stable `CLI_BWS`, `ENV_BWS_ACCESS_TOKEN`, and `VAULT_PEM_DRYRUN` check IDs with provider-aware outcomes and secret-safe detail.

**Source basis:** Doctor currently hard-codes BWS as critical and fetches only a vault PEM; the approved behavior makes those prerequisites conditional while retaining identifiers and a provider-aware opt-in key probe. (`src/baton_harness/chain/doctor.py:L283-L300`, `src/baton_harness/chain/doctor.py:L518-L543`, `src/baton_harness/chain/doctor.py:L905-L1049`; `docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L144-L161`)

- [ ] **Step 1: Write failing doctor matrix tests**

Add exact test cases:

- `test_cli_bws_and_access_token_follow_composed_requirement`: parameterize `(bws, no optional ID, required)`, `(file, no optional ID, not required)`, `(file, PAT ID, required)`, and `(file, heartbeat ID, required)`; assert both check statuses for each row.
- `test_file_only_missing_bws_binary_and_token_are_pass_not_required`: `which("bws")` returns `None`, token is absent, and both checks return `PASS` with `not required` detail.
- `test_invalid_provider_matrix_fails_cfg_required_keys`: conflicting source values return `FAIL` and do not invoke a secret fetch.
- `test_bws_key_probe_fetches_without_exposing_secret`: one fetch with UUID/token returns `PASS`, while detail contains neither value nor a character count.
- `test_file_key_probe_reads_secure_file_without_bws`: a generated `0600` file returns `PASS` and leaves the fetch mock untouched.
- `test_file_key_probe_rejects_insecure_mode_safely`: a `0644` file returns `FAIL` without including its contents.
- `test_key_probe_output_excludes_pem_token_and_credential_url`: seed all three sentinels and prove none appears in text or JSON doctor rendering.
- `test_cli_doctor_gate_allows_file_only_host_without_bws`: the pre-bootstrap gate advances when the only formerly failing prerequisites are missing BWS binary/token.
- `test_cli_doctor_gate_blocks_file_provider_optional_bws_without_token`: an optional PAT UUID makes the missing token critical and prevents bootstrap.

Keep the existing catalog-ID assertions so stable IDs cannot be accidentally renamed.

- [ ] **Step 2: Run focused doctor tests to verify they fail**

Run:

```bash
.venv/Scripts/python.exe -m pytest tests/chain/test_doctor.py tests/chain/test_cli_doctor_gate.py -k "bws or provider or key_probe or file_only" -v
```

Expected: file-only cases still fail on unconditional BWS checks and the PEM dry-run cannot read a file provider.

- [ ] **Step 3: Centralize doctor’s resolved provider context**

Add a private helper that parses `.bh/config.env`, overlays `ctx.env` using `sandbox_config.resolve_overridable_keys`, invokes `resolve_app_private_key_config`, and returns both config and resolved values. Use it in `_check_required_keys`, `_check_cli_bws`, `_check_bws_access_token`, and `_check_vault_dryrun`; do not retain a second provider matrix in `doctor.py`.

When BWS is not required, `CLI_BWS` and `ENV_BWS_ACCESS_TOKEN` return `PASS`/`CRITICAL` with detail `BWS is not required by the resolved secret configuration.` When required, preserve their current binary/token checks. For `VAULT_PEM_DRYRUN`, call `load_app_private_key` with the resolved provider and report only `App private key loaded successfully.` or a safe provider-class failure; do not report character counts.

- [ ] **Step 4: Run doctor suites and static checks**

Run:

```bash
.venv/Scripts/python.exe -m pytest tests/chain/test_doctor.py tests/chain/test_cli_doctor_gate.py -v
.venv/Scripts/python.exe -m ruff check src/baton_harness/chain/doctor.py tests/chain/test_doctor.py tests/chain/test_cli_doctor_gate.py
.venv/Scripts/python.exe -m mypy src/baton_harness/chain/doctor.py
```

Expected: all doctor catalog, phase, JSON/text rendering, gate, and new provider cases pass.

- [ ] **Step 5: Commit the doctor slice**

Run:

```bash
git add src/baton_harness/chain/doctor.py tests/chain/test_doctor.py tests/chain/test_cli_doctor_gate.py
git commit -m "feat(#359): make BWS doctor checks conditional"
```

Expected: commit succeeds with only doctor implementation/tests.

---

### Task 5: Worker Environment Isolation

**Files:**

- Modify: `src/baton_harness/chain/identity.py:13-59`
- Modify: `tests/chain/test_identity.py`
- Modify: `tests/chain/test_identity_spawn_guard.py`
- Modify: `tests/chain/test_bootstrap_hook_env_no_leak.py`

**Interfaces:**

- Consumes: the existing `Identity.APP`/`Identity.WORKER` environment broker.
- Produces: a worker denylist covering GitHub App provider configuration and every daemon-side BWS credential/locator, without changing App token injection.

**Source basis:** Worker filtering is currently limited to three GitHub token keys; the approved design adds the provider selector/path, access token, App PEM locator, compatibility IDs, and optional BWS locators. (`src/baton_harness/chain/identity.py:L13-L59`; `docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L163-L176`)

- [ ] **Step 1: Write the failing denylist regression**

Add a single parametrized test over this exact set:

```python
_DAEMON_ONLY_KEYS = {
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_INSTALLATION_TOKEN",
    "BH_GITHUB_APP_KEY_PROVIDER",
    "BH_GITHUB_APP_PRIVATE_KEY_FILE",
    "BWS_ACCESS_TOKEN",
    "BWS_PEM_SECRET_ID",
    "BWS_APP_ID",
    "BWS_INSTALLATION_ID",
    "BWS_GH_TOKEN_SECRET_ID",
    "BWS_HEARTBEAT_PING_URL_SECRET_ID",
}


@pytest.mark.parametrize("key", sorted(_DAEMON_ONLY_KEYS))
def test_worker_strips_every_daemon_only_auth_key(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    monkeypatch.setenv(key, f"sentinel-{key}")
    assert key not in env_for(Identity.WORKER)
```

Extend the bootstrap leak test to seed all keys at once and assert no seeded value reaches the worker environment. Keep the spawn guard’s real-package scan green.

- [ ] **Step 2: Run identity tests to verify they fail**

Run:

```bash
.venv/Scripts/python.exe -m pytest tests/chain/test_identity.py tests/chain/test_identity_spawn_guard.py tests/chain/test_bootstrap_hook_env_no_leak.py -v
```

Expected: new cases fail for App/BWS keys not in `_PRIVILEGED_ENV_KEYS`; existing App identity cases remain green.

- [ ] **Step 3: Expand the centralized privileged-key set**

Add the exact keys above to `_PRIVILEGED_ENV_KEYS`. Do not alter the `Identity.APP` branch or the by-value worker-token filtering. Update the module docstring to state that the set contains daemon-only authentication configuration as well as minted tokens.

- [ ] **Step 4: Re-run identity suites and commit**

Run:

```bash
.venv/Scripts/python.exe -m pytest tests/chain/test_identity.py tests/chain/test_identity_spawn_guard.py tests/chain/test_bootstrap_hook_env_no_leak.py -v
.venv/Scripts/python.exe -m ruff check src/baton_harness/chain/identity.py tests/chain/test_identity.py tests/chain/test_identity_spawn_guard.py tests/chain/test_bootstrap_hook_env_no_leak.py
.venv/Scripts/python.exe -m mypy src/baton_harness/chain/identity.py
git add src/baton_harness/chain/identity.py tests/chain/test_identity.py tests/chain/test_identity_spawn_guard.py tests/chain/test_bootstrap_hook_env_no_leak.py
git commit -m "fix(#359): isolate App key configuration from workers"
```

Expected: tests/static checks pass and the commit succeeds.

---

### Task 6: Provider-Aware Setup and Service Scripts

**Files:**

- Modify: `bin/init-sandbox.sh:903-944`
- Modify: `bin/install-daemon-service.sh:1-88`
- Modify: `bin/install-daemon-service.sh:240-345`
- Modify: `bin/install-daemon-service.sh:351-411`
- Modify: `bin/install-daemon-service.sh:430-493`
- Modify: `bin/provision-ruleset.sh:13-32`
- Modify: `tests/test_init_sandbox_config_reuse_prompt.py`
- Modify: `tests/test_install_daemon_secrets_reuse_prompt.py`
- Modify: `tests/test_provision_ruleset_app_auth.py`

**Interfaces:**

- Consumes: `python -m baton_harness.chain.app_private_key requires-bws` from Task 1, the shared shell config loader, and provider-aware `app_auth {jwt|token}` from Task 3.
- Produces: interactive provider-specific sandbox config, a systemd unit that omits `EnvironmentFile` when no bootstrap secret is needed, and unchanged ruleset delegation across both providers.

**Source basis:** Initialization currently prompts only for BWS; the installer always prompts/writes an access-token file; ruleset provisioning already delegates JWT/token generation to Python and should continue doing so. (`bin/init-sandbox.sh:L903-L937`, `bin/install-daemon-service.sh:L316-L369`, `bin/install-daemon-service.sh:L444-L493`, `bin/provision-ruleset.sh:L148-L165`, `bin/provision-ruleset.sh:L224-L240`; `docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L243-L264`)

- [ ] **Step 1: Write failing sandbox initialization tests**

Extend the pseudo-TTY harness to cover these prompt/output contracts:

- `test_new_bws_config_writes_explicit_provider_and_only_bws_source`: answer `bws`, then assert the selector/UUID lines exist and the file-source line does not.
- `test_new_file_config_writes_explicit_provider_and_only_absolute_file_source`: answer `file` plus `/run/credentials/app.pem`, then assert the selector/path lines exist and the BWS PEM line does not.
- `test_file_provider_reprompts_relative_path`: answer `relative.pem` then an absolute path; assert only the latter is persisted.
- `test_unknown_provider_reprompts_without_writing_config`: answer `vault` then `file`; assert the error is safe and only the valid selection is written.
- `test_generated_config_never_contains_pem_contents`: include a PEM-header sentinel in the pseudo-TTY input stream after all expected answers and prove it never appears in the config.

Assert BWS mode writes `BH_GITHUB_APP_KEY_PROVIDER=bws` and `BWS_PEM_SECRET_ID`, with no `BH_GITHUB_APP_PRIVATE_KEY_FILE` line. Assert file mode writes `BH_GITHUB_APP_KEY_PROVIDER=file` and the absolute file path, with no `BWS_PEM_SECRET_ID` line. Preserve overwrite/reuse behavior.

- [ ] **Step 2: Write failing service-installer and ruleset tests**

Add service tests:

- `test_file_only_print_unit_needs_no_bws_token_or_secrets_file`: no token is supplied; output has neither a secrets preview nor `EnvironmentFile=` and exits 0.
- `test_file_provider_optional_bws_secret_still_requires_token`: configure a PAT UUID without a token and assert the non-interactive installer fails before rendering.
- `test_bws_provider_still_renders_redacted_environment_file`: output contains `<redacted>`, never the token sentinel, and retains `EnvironmentFile=`.
- `test_file_only_install_does_not_create_or_overwrite_secrets_file`: seed a sentinel secrets file, run the fake-sudo install harness, and prove byte-for-byte preservation.
- `test_invalid_provider_fails_before_prompt_or_privileged_write`: an unknown selector yields nonzero status and an empty fake-sudo call log.

Add ruleset tests that exercise the real `app_auth` fallback in both provider modes, using a generated `0600` PEM file for file mode and a mocked BWS command for BWS mode. Assert JWT and installation-token acquisition still occur before any ruleset mutation and that neither PEM nor BWS token appears in stdout/stderr.

- [ ] **Step 3: Run the shell-facing tests to verify they fail**

Run:

```bash
.venv/Scripts/python.exe -m pytest tests/test_init_sandbox_config_reuse_prompt.py tests/test_install_daemon_secrets_reuse_prompt.py tests/test_provision_ruleset_app_auth.py -v
```

Expected: provider-specific prompt/install cases fail against the BWS-only scripts.

- [ ] **Step 4: Update `init-sandbox.sh` provider prompts**

Prompt for `BH_GITHUB_APP_KEY_PROVIDER (bws/file)` in a loop. In the `bws` branch, prompt only for the PEM UUID; in the `file` branch, prompt until the value begins with `/` (the target host is POSIX) and never read or copy file contents. Prompt for optional PAT/heartbeat secret IDs after either branch. Render the selector and only the selected source into `.bh/config.env`, preserving the existing file mode/reuse behavior.

- [ ] **Step 5: Make service installation query shared BWS policy**

After sourcing sandbox config, resolve the worktree interpreter from `${HARNESS_DIR}/.venv/bin/python` or `${HARNESS_DIR}/.venv/Scripts/python.exe` and run:

```bash
if ! _bh_requires_bws="$("${_BH_PYTHON}" -m baton_harness.chain.app_private_key requires-bws)"; then
    echo "baton-harness: error: invalid App private-key configuration" >&2
    exit 1
fi
case "${_bh_requires_bws}" in
    true) _BH_REQUIRES_BWS=1 ;;
    false) _BH_REQUIRES_BWS=0 ;;
    *) echo "baton-harness: error: could not resolve BWS requirement" >&2; exit 1 ;;
esac
```

Quote the command path in the actual implementation. Only execute the token reuse/prompt/write flow when `_BH_REQUIRES_BWS=1`. When false, do not create, read, back up, preview, or overwrite `BH_DAEMON_SECRETS_PATH`, and omit `EnvironmentFile=` from `_render_unit`. Update the summary/help text to say BWS is conditional. Continue to include the configured PATH; its presence is harmless and avoids changing unrelated systemd path behavior.

- [ ] **Step 6: Keep ruleset provisioning delegated to Python**

Update only required-variable/help commentary and test fixtures for the explicit selector. Do not add provider branching or file reads to `provision-ruleset.sh`; the existing two `python -m baton_harness.chain.app_auth` calls are the policy boundary.

- [ ] **Step 7: Run all maintained shell test families and syntax checks**

Run:

```bash
.venv/Scripts/python.exe -m pytest tests/test_init_sandbox_*.py tests/test_install_daemon_*.py tests/test_provision_ruleset_*.py -v
bash -n bin/init-sandbox.sh
bash -n bin/install-daemon-service.sh
bash -n bin/provision-ruleset.sh
```

Expected: all shell tests and Bash syntax checks pass.

- [ ] **Step 8: Commit the shell surface**

Run:

```bash
git add bin/init-sandbox.sh bin/install-daemon-service.sh bin/provision-ruleset.sh tests/test_init_sandbox_config_reuse_prompt.py tests/test_install_daemon_secrets_reuse_prompt.py tests/test_provision_ruleset_app_auth.py
git commit -m "feat(#359): make setup scripts provider-aware"
```

Expected: commit succeeds with scripts and their black-box tests.

---

### Task 7: Operator Documentation, Migration, and Full Verification

**Files:**

- Modify: `README.md:121-164`
- Modify: `README.md:201-229`
- Modify: `README.md:420-440`
- Modify: `docs/authentication.md:1-130`
- Modify: `docs/repository-onboarding.md`
- Modify: `docs/system-setup.md`
- Modify: `docs/smoke-test-daemon.md`

**Interfaces:**

- Consumes: all implemented configuration names and operational behavior from Tasks 1-6.
- Produces: one consistent operator contract for BWS, host-file, mixed optional-BWS, migration, doctor, setup, ruleset provisioning, and systemd credential-path examples.

**Source basis:** Runtime/setup documentation currently describes BWS as mandatory and must be updated whenever prerequisites, environment variables, setup, or service examples change. The design requires all five operational documents plus README to explain provider selection and migration. (`README.md:L201-L229`; `docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L243-L271`, `docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L335-L352`)

- [ ] **Step 1: Update the README configuration and quick-start paths**

Document these exact examples:

```bash
# Existing BWS deployment
export BH_GITHUB_APP_KEY_PROVIDER=bws
export BWS_PEM_SECRET_ID=<uuid>
export BWS_ACCESS_TOKEN=<machine-account-token>

# BWS-free file deployment
export BH_GITHUB_APP_KEY_PROVIDER=file
export BH_GITHUB_APP_PRIVATE_KEY_FILE=/run/credentials/bh-daemon/app.pem
```

State that the file must be absolute, regular, non-symlink, owner-only (`0400` or `0600`), UTF-8 PEM, non-empty, and at most 1 MiB. Add a composition table showing that optional PAT/heartbeat BWS IDs require `bws` and `BWS_ACCESS_TOKEN` even in file mode. Remove unconditional BWS language from prerequisite and service-install examples.

- [ ] **Step 2: Update authentication and onboarding documentation**

In `docs/authentication.md`, describe the provider matrix, bootstrap ordering, JWT proof, token scrubbing, and worker denylist without showing secret values. In `docs/repository-onboarding.md`, show both selector choices and the legacy migration line `BH_GITHUB_APP_KEY_PROVIDER=bws`. Keep GitHub App IDs/permissions unchanged.

- [ ] **Step 3: Update host setup and smoke-test documentation**

In `docs/system-setup.md` and `docs/smoke-test-daemon.md`, make BWS conditional and add a non-authoritative systemd example:

```ini
[Service]
LoadCredential=app.pem:/externally/provisioned/github-app.pem
Environment=BH_GITHUB_APP_KEY_PROVIDER=file
Environment=BH_GITHUB_APP_PRIVATE_KEY_FILE=%d/app.pem
```

Label the example as an external host-provisioning option, not Harness-managed key enrollment, and cite `https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#Credentials` with retrieval date 2026-09-05. Include doctor expectations for file-only (`CLI_BWS` and `ENV_BWS_ACCESS_TOKEN` pass as “not required”) and mixed file+BWS configurations.

- [ ] **Step 4: Verify documentation names and stale mandatory-BWS claims**

Run:

```bash
rg -n "BH_GITHUB_APP_KEY_PROVIDER|BH_GITHUB_APP_PRIVATE_KEY_FILE|BWS_PEM_SECRET_ID|BWS_ACCESS_TOKEN" README.md docs/authentication.md docs/repository-onboarding.md docs/system-setup.md docs/smoke-test-daemon.md
rg -n "BWS.*required|required.*BWS|always.*BWS|mandatory.*BWS" README.md docs/authentication.md docs/repository-onboarding.md docs/system-setup.md docs/smoke-test-daemon.md
```

Expected: every new variable appears where operators need it; remaining “BWS required” statements are explicitly scoped to the BWS App-key provider or optional BWS consumers.

- [ ] **Step 5: Run the full automated verification suite**

Run:

```bash
.venv/Scripts/python.exe -m pytest
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m ruff format --check .
.venv/Scripts/python.exe -m mypy src
bash -n bin/init-sandbox.sh
bash -n bin/install-daemon-service.sh
bash -n bin/provision-ruleset.sh
git diff main...HEAD --check
```

Expected: every command exits 0. Do not claim completion from targeted suites alone; these are the repository’s documented full checks. (`README.md:L143-L164`, `pyproject.toml:L60-L99`)

- [ ] **Step 6: Audit referenced artifact persistence**

Run:

```bash
git ls-tree HEAD -- docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md
git ls-tree HEAD -- src/baton_harness/chain/app_private_key.py
git diff main...HEAD --stat
```

Expected: both referenced files appear in `HEAD`, and the diff stat contains every implementation/test/documentation deliverable named by this plan.

- [ ] **Step 7: Commit the documentation and verification-complete slice**

Run:

```bash
git add README.md docs/authentication.md docs/repository-onboarding.md docs/system-setup.md docs/smoke-test-daemon.md
git commit -m "docs(#359): document App key provider deployment"
git status --short
```

Expected: the documentation commit succeeds and `git status --short` is empty.

---

## Acceptance Checklist

- [ ] Missing, empty, unknown, or conflicting provider configuration fails before environment mutation or GitHub access. (`docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L69-L92`, #359)
- [ ] BWS provider behavior remains functional and fetches the PEM exactly once. (`docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L116-L118`, #359)
- [ ] File-only operation requires neither the BWS binary nor `BWS_ACCESS_TOKEN`; optional BWS locators independently restore both prerequisites. (`docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L225-L241`, #359)
- [ ] The file provider rejects relative, missing, symlink, non-regular, replaced, over-permissive, unreadable, oversized, empty, non-UTF-8, and malformed key material before GitHub access. (`docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L199-L223`, `docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L282-L288`, #359)
- [ ] `BWS_ACCESS_TOKEN` is absent after every bootstrap success/failure path and no App-key/BWS configuration reaches worker environments. (`docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L163-L197`, #359)
- [ ] Doctor, init, service install, ruleset provisioning, daemon startup, README, and operator docs agree on the same provider contract. (`docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L243-L271`, `docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L369-L380`, #359)
- [ ] Existing installation-token refresh and by-value authority behavior remains green under the full regression suite. (`src/baton_harness/chain/app_auth.py:L241-L315`, `docs/superpowers/specs/2026-09-05-github-app-key-provider-design.md:L129-L137`)
