---
title: GitHub App private-key provider selection
status: proposed
issue: 359
---

# GitHub App private-key provider selection

## Decision summary

Harness will continue to require GitHub App authentication while allowing an explicit,
fail-closed choice of private-key provider. Configuration selects exactly one of `bws`
or `file`; there is no GitHub-App-free mode and no private-key-content environment
variable. This implements the security and deployment outcomes tracked by #359.

The provider selector is scoped only to the GitHub App PEM. A deployment using the
`file` App-key provider may still configure `BWS_GH_TOKEN_SECRET_ID` or
`BWS_HEARTBEAT_PING_URL_SECRET_ID`; those independent consumers conditionally require
the BWS CLI and `BWS_ACCESS_TOKEN`. This is the approved clarification to #359 and
preserves the existing optional secret uses documented in
`docs/authentication.md:L95-L109`.

## Current state

The App authentication implementation currently accepts only a Bitwarden secret ID.
Both startup entry points pop `BWS_ACCESS_TOKEN`, fetch `BWS_PEM_SECRET_ID`, and retain
the resulting PEM in an `InstallationTokenProvider`
(`src/baton_harness/chain/app_auth.py:L354-L459`). The daemon wrapper fetches the
optional PAT and heartbeat URL before building that provider because the provider
currently consumes the ambient BWS token (`src/baton_harness/chain/cli.py:L59-L174`).

Sandbox configuration requires `BWS_PEM_SECRET_ID`, exports it into `os.environ`, and
models it as a required dataclass field
(`src/baton_harness/chain/sandbox_config.py:L60-L77`,
`src/baton_harness/chain/sandbox_config.py:L307-L345`). Doctor likewise treats the BWS
binary and access token as unconditional critical prerequisites
(`src/baton_harness/chain/doctor.py:L959-L1049`). Worker environment filtering currently
removes only the three GitHub token variables
(`src/baton_harness/chain/identity.py:L13-L59`).

The maintained shell and documentation surfaces encode the same coupling:

- sandbox initialization always prompts for `BWS_PEM_SECRET_ID`
  (`bin/init-sandbox.sh:L903-L937`);
- the service example always requires and persists `BWS_ACCESS_TOKEN`
  (`bin/install-daemon-service.sh:L316-L345`,
  `bin/install-daemon-service.sh:L444-L464`);
- ruleset provisioning delegates JWT and installation-token creation to
  `baton_harness.chain.app_auth` (`bin/provision-ruleset.sh:L148-L165`,
  `bin/provision-ruleset.sh:L224-L240`);
- runtime prerequisites describe BWS as mandatory for the App PEM
  (`README.md:L201-L229`).

## Configuration contract

Add these sandbox configuration keys:

| Key | Meaning |
|---|---|
| `BH_GITHUB_APP_KEY_PROVIDER` | Required selector; exactly `bws` or `file` |
| `BWS_PEM_SECRET_ID` | App PEM secret UUID; required only for provider `bws` |
| `BH_GITHUB_APP_PRIVATE_KEY_FILE` | Absolute App PEM file path; required only for provider `file` |

The base GitHub App settings remain mandatory:
`BH_GITHUB_APP_ID` and `BH_GITHUB_APP_INSTALLATION_ID`. Existing optional
`BWS_GH_TOKEN_SECRET_ID` and `BWS_HEARTBEAT_PING_URL_SECRET_ID` settings remain
provider-independent. (#359; `src/baton_harness/chain/sandbox_config.py:L60-L77`)

Validation follows this matrix:

| Selector | `BWS_PEM_SECRET_ID` | `BH_GITHUB_APP_PRIVATE_KEY_FILE` | Result |
|---|---:|---:|---|
| missing or empty | any | any | reject: explicit provider required |
| unknown | any | any | reject: unsupported provider |
| `bws` | present, valid UUID | absent | accept |
| `bws` | absent or invalid | absent | reject: selected source missing/invalid |
| `bws` | any | present | reject: conflicting App-key sources |
| `file` | absent | present, absolute | accept configuration shape |
| `file` | absent | absent or relative | reject: selected source missing/invalid |
| `file` | present | any | reject: conflicting App-key sources |

There is deliberately no implicit `bws` default. Existing deployments must add
`BH_GITHUB_APP_KEY_PROVIDER=bws`; silently inferring a provider from legacy keys would
violate #359's missing/ambiguous-provider rejection requirement.

Environment overrides retain the existing non-empty-value-wins behavior, but validation
occurs on the completely resolved key set before `os.environ` is mutated. Once accepted,
the selected source keys are exported for daemon startup and the unselected source key
is removed. The selector and file path are non-secret configuration; PEM contents never
enter configuration or environment variables. (#359;
`src/baton_harness/chain/sandbox_config.py:L206-L229`,
`src/baton_harness/chain/sandbox_config.py:L290-L345`)

## Components

### Provider configuration and loading

Add `src/baton_harness/chain/app_private_key.py` as the single provider-policy module.
It owns:

- an `AppPrivateKeyProvider` enum containing `BWS` and `FILE`;
- an immutable `AppPrivateKeyConfig` discriminated by that enum;
- `AppPrivateKeyConfigError` and `AppPrivateKeyLoadError` exceptions that do not
  depend on `app_auth.py`;
- `resolve_app_private_key_config(values)` for the validation matrix above;
- `requires_bws(app_key_config, values)` for conditional prerequisite evaluation;
- `load_app_private_key(config, *, bws_access_token, fetch_secret)` for one-time PEM
  retrieval.

The resolver is pure: it accepts a mapping, returns a validated object, and performs no
filesystem, environment, BWS, or GitHub operations. `sandbox_config.py`, `doctor.py`,
`app_auth.py`, and shell-facing setup behavior must consume this same contract rather
than reproduce the matrix. This directly addresses the duplicated coupling described in
#359.

The loader performs exactly one selected-source read. For `bws`, it calls the injected
`fetch_secret` once with the validated UUID and caller-supplied access token. For `file`,
it uses the secure file procedure below and never invokes BWS.

### Sandbox configuration

Extend `SandboxConfig` with `github_app_key_provider`, optional
`bws_pem_secret_id`, and optional `github_app_private_key_file` fields. Make the selector
part of the required base key set, while provider resolution decides which source key is
required. Continue to validate optional BWS secret IDs independently. The repo identity
probe and derived App/installation IDs remain unchanged
(`src/baton_harness/chain/sandbox_config.py:L307-L345`).

### Authentication bootstrap

Change `build_installation_token_provider` to accept an `AppPrivateKeyConfig` instead of
an unconditional Bitwarden secret ID. It loads the PEM once, proves it can sign an App
JWT, then stores it only in the existing refreshable `InstallationTokenProvider`. Token
minting, caching, refresh margins, and by-value installation-token handling remain
unchanged (`src/baton_harness/chain/app_auth.py:L120-L151`,
`src/baton_harness/chain/app_auth.py:L241-L305`,
`src/baton_harness/chain/app_auth.py:L462-L514`).

Both the daemon wrapper and `python -m baton_harness.chain.app_auth {jwt|token}` use the
same resolver and loader. The shell-facing command changes its required variables by
provider but retains its current safe exit-code and output discipline
(`src/baton_harness/chain/app_auth.py:L592-L682`).

### Doctor and prerequisite selection

Doctor evaluates BWS need as:

```text
App provider is bws
OR BWS_GH_TOKEN_SECRET_ID is configured
OR BWS_HEARTBEAT_PING_URL_SECRET_ID is configured
```

`CLI_BWS` and `ENV_BWS_ACCESS_TOKEN` remain stable check identifiers. When the expression
is true, their current critical behavior remains; otherwise they return `PASS` with a
safe "not required" detail. The provider matrix is validated by the configuration check.
The opt-in PEM access check becomes provider-aware: it either fetches from BWS or securely
reads the configured file, then reports only whether usable key material was obtained.
No key, token, provider output, or credential-bearing URL appears in doctor output.
(#359; `src/baton_harness/chain/doctor.py:L518-L543`,
`src/baton_harness/chain/doctor.py:L905-L956`)

### Worker environment isolation

Extend the `Identity.WORKER` denylist to include:

- `BH_GITHUB_APP_KEY_PROVIDER`;
- `BH_GITHUB_APP_PRIVATE_KEY_FILE`;
- `BWS_ACCESS_TOKEN`;
- `BWS_PEM_SECRET_ID`;
- `BWS_APP_ID` and `BWS_INSTALLATION_ID`.

Also strip the optional BWS secret IDs because workers do not need daemon-side secret
locators. This strengthens the existing additive-safe worker identity model without
changing `Identity.APP` token injection (`src/baton_harness/chain/identity.py:L13-L59`;
`docs/harness-design.md:L443-L460`).

## Bootstrap data flow

Daemon startup performs these ordered steps:

1. Parse and resolve sandbox configuration, including the App-key provider matrix.
2. Determine whether any selected feature requires BWS.
3. Run provider-aware doctor checks before touching secrets.
4. If BWS is needed, read `BWS_ACCESS_TOKEN` without logging it and fetch configured
   optional PAT/heartbeat secrets plus the App PEM when provider `bws` is selected.
5. If provider `file` is selected, read the App PEM once through the secure file loader;
   optional BWS secret fetches, when configured, remain independent.
6. In a `finally` block, remove `BWS_ACCESS_TOKEN` from `os.environ` on every success or
   failure path after bootstrap begins.
7. Prove the PEM can sign an RS256 App JWT before making an installation-token API call.
8. Construct the refreshable provider, validate its first installation token, and enter
   the daemon loop using the existing by-value token path.

This ordering preserves the current need to finish optional vault fetches before the
ambient BWS token is consumed (`src/baton_harness/chain/cli.py:L130-L174`) while closing
the file-provider path that would otherwise leave an unused BWS token ambient.

## Secure file-provider procedure

The file source must be absolute. The loader:

1. calls `os.lstat` and rejects a symbolic link;
2. opens read-only with non-inheriting and no-follow flags where supported;
3. calls `os.fstat` on the open descriptor;
4. rejects anything other than a regular file;
5. rejects any group or world permission bit (`st_mode & 0o077 != 0`), allowing owner-only
   modes such as `0400` and `0600`;
6. compares pre-open and post-open file identity where the platform exposes meaningful
   device/inode values, rejecting replacement races;
7. reads the contents during that single open with a bounded maximum size, decodes UTF-8,
   closes the descriptor in a `finally` block, and rejects empty content;
8. passes the PEM to `build_app_jwt` and discards the probe JWT.

Python documents `os.open` as returning a non-inheritable descriptor, `os.fstat` as
reporting on the opened descriptor, `os.lstat` as not following symbolic links, and
`O_NOFOLLOW` as platform-dependent. The implementation therefore combines all three
checks rather than relying on a path-only precheck or assuming one flag exists everywhere.
https://docs.python.org/3.10/library/os.html (fetched 2026-09-05)

The maximum accepted PEM size is 1 MiB. This is a defensive input bound, not a format
assumption; exceeding it fails closed before JWT signing. No code path accepts PEM
contents from an environment variable or command-line argument. (#359)

## BWS independence and scrubbing

`requires_bws` is true when the App PEM uses BWS or either optional BWS secret locator is
configured. Therefore:

- file App key with no optional BWS secrets needs neither `bws` nor `BWS_ACCESS_TOKEN`;
- file App key plus an optional BWS locator needs both prerequisites and may perform only
  those selected vault fetches;
- BWS App key retains the current immediate bootstrap-token scrubbing and in-memory PEM
  behavior.

The daemon bootstrap owns access-token lifetime across all vault reads. It captures the
token once, performs only configured fetches, and removes it in `finally`. The App PEM,
optional PAT, and heartbeat URL keep their current destinations; the installation token
continues to be passed by value and never written to the ambient environment
(`src/baton_harness/chain/app_auth.py:L14-L25`,
`src/baton_harness/chain/cli.py:L141-L174`).

## Shell and deployment examples

`bin/init-sandbox.sh` prompts for the selector and only its selected source. It writes an
explicit provider line and never writes PEM contents. `bin/provision-ruleset.sh` continues
to delegate JWT/token generation to the Python auth command, so it inherits provider
selection without duplicating private-key logic (`bin/provision-ruleset.sh:L148-L165`,
`bin/provision-ruleset.sh:L224-L240`).

`bin/install-daemon-service.sh` requires and writes `BWS_ACCESS_TOKEN` only when
`requires_bws` is true. For a fully BWS-free file deployment it does not create an empty
secrets file merely to satisfy the old layout. It accepts a key path that external
infrastructure has already secured or mounted; it does not copy, enroll, or generate the
private key. This retains the deployment ownership boundary established by #359 and
#361.

Documentation includes a non-authoritative systemd drop-in example using
`LoadCredential=` and an environment override pointing
`BH_GITHUB_APP_PRIVATE_KEY_FILE` at the service credential directory. systemd documents
credentials as a service-facing mechanism under `systemd.exec`; Harness consumes only
the resulting path and does not own host credential provisioning.
https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#Credentials
(fetched 2026-09-05)

Update `README.md`, `docs/authentication.md`, `docs/repository-onboarding.md`,
`docs/system-setup.md`, and `docs/smoke-test-daemon.md` so all operational paths describe
the selector, provider-specific requirements, optional-BWS composition, and migration
from legacy implicit BWS configuration. This documentation update is required because
the change affects prerequisites, environment variables, setup, and service examples.
(#359)

## Failure behavior

The provider module raises `AppPrivateKeyConfigError` or `AppPrivateKeyLoadError` without
importing `app_auth.py`. Sandbox configuration translates configuration errors to
`SandboxConfigError`; authentication startup translates configuration and load errors to
`AppAuthError`. All messages provide secret-safe remediation. Configuration errors
identify the selector or source key. Runtime errors identify the selected provider and
failure class without including the PEM, token, raw BWS output, or credential contents.

The following fail before any GitHub API call:

- missing, empty, unknown, or conflicting provider configuration;
- missing BWS prerequisites when any BWS-backed item is configured;
- missing, relative, unreadable, symbolic-link, non-regular, replaced, over-permissive,
  oversized, empty, non-UTF-8, or malformed file key;
- empty or malformed PEM returned by BWS.

`BWS_ACCESS_TOKEN` removal is guaranteed once secret bootstrap begins, including provider
resolution, BWS, filesystem, and JWT-signing failures. Sandbox-configuration failures
that occur earlier terminate startup before any worker can spawn. The selected PEM
remains in memory only for the lifetime of the refreshable installation-token provider.
(#359)

## Test strategy

Implementation follows red-green-refactor. Each behavior is introduced by a focused
failing test before production code changes.

### Provider unit tests

Add `tests/chain/test_app_private_key.py` covering:

- every selector/source matrix row;
- provider-independent optional BWS composition;
- one BWS fetch with the correct secret locator and token;
- successful owner-only file reads;
- relative, missing, symlinked, non-regular, replaced, group/world-accessible, unreadable,
  oversized, empty, and non-UTF-8 files;
- no BWS call in file-only mode and no file call in BWS mode.

### Integration and regression tests

Update:

- `tests/chain/test_sandbox_config.py` for parsing, environment overrides, conflicts, and
  mutation-after-validation behavior;
- `tests/test_app_auth.py` for provider-aware JWT/token CLI behavior, malformed PEM
  rejection, and existing BWS/token-refresh regressions;
- `tests/chain/test_cli_bootstrap_vault.py` for optional BWS secrets in file mode and
  unconditional access-token scrubbing on failure;
- `tests/chain/test_doctor.py` and `tests/chain/test_cli_doctor_gate.py` for conditional
  BWS checks and provider-aware PEM access;
- `tests/chain/test_identity.py`, `tests/chain/test_identity_spawn_guard.py`, and
  `tests/chain/test_bootstrap_hook_env_no_leak.py` for worker-environment isolation;
- the existing `tests/test_init_sandbox_*.py`, `tests/test_install_daemon_*.py`, and
  `tests/test_provision_ruleset_*.py` suites for shell behavior and delegation.

Run the targeted suites after each TDD slice, then the full pytest, ruff, and strict mypy
commands defined by the repository before completion. The new worktree currently has no
project-local `.venv`; create it with `uv venv .venv` and install `.[dev]` before the first
Python test, following the repository interpreter policy.

## Migration

This is an intentional fail-closed configuration migration:

```text
Existing BWS deployment:
  add BH_GITHUB_APP_KEY_PROVIDER=bws
  retain BWS_PEM_SECRET_ID

File deployment:
  set BH_GITHUB_APP_KEY_PROVIDER=file
  set BH_GITHUB_APP_PRIVATE_KEY_FILE to an absolute owner-only PEM path
  remove BWS_PEM_SECRET_ID
```

Optional BWS locators may remain in either deployment and independently trigger BWS
prerequisites. Setup, doctor, ruleset provisioning, and daemon startup must all report the
same resolved requirement. (#359)

## Non-goals

- Making the GitHub App optional.
- Accepting PEM contents through environment variables, CLI arguments, or repository
  configuration.
- Adding providers beyond `bws` and `file`.
- Generating, rotating, copying, or enrolling App private keys.
- Provisioning systemd credentials or choosing production host paths.
- Changing GitHub App IDs, installation IDs, permissions, token refresh, or the
  operator/App/worker authority separation.
- Changing how the optional PAT or heartbeat URL is consumed after retrieval.

These exclusions keep #359 focused on App-key provider selection and preserve the
existing GitHub App protocol and subprocess identity boundaries.

## Acceptance mapping

| #359 outcome | Design owner |
|---|---|
| Exactly one recognized provider | `app_private_key.resolve_app_private_key_config` |
| Existing BWS behavior | provider loader plus bootstrap regression tests |
| Secure one-time file read and JWT proof | file loader plus `build_app_jwt` probe |
| No worker leakage | `identity.env_for(Identity.WORKER)` denylist and spawn tests |
| Conditional BWS prerequisites | `requires_bws`, doctor, setup, and service example |
| Consistent operational surfaces | shared resolver plus shell delegation and docs |
| App protocol unchanged | existing `InstallationTokenProvider` and App identity path |
| Failure coverage | provider, integration, shell, and environment-leak suites |
