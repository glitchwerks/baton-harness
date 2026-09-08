# baton-harness

A reusable policy and tooling layer for autonomous Claude Code agent runs. The harness
owns the lifecycle hook modules, per-project workflow config, context templates, and the
always-on daemon. The orchestration engine (`symphony`, from
[mraza007/baton](https://github.com/mraza007/baton)) is vendored into the package and
called directly as a library.

**Current state [implemented, #27]:** the `symphony` package is vendored at
`src/codereeve/vendor/symphony/` and the always-on daemon (`src/codereeve/chain/`)
calls `Orchestrator._run_worker(issue)` directly — no subprocess, no `baton start`. The
daemon is the entry point; `bin/run-daemon.sh` is the launcher. See
[docs/harness-design.md §1 and §10](docs/harness-design.md) for the design rationale.

## What this is

The harness owns everything *shareable* across projects: the Python hook modules that run
before and after each agent turn, per-project workflow config, a CLAUDE.md template, and
the always-on daemon. Each target project carries only its own committed `CLAUDE.md` and
CI workflow.

The hooks and daemon ship in the `codereeve` Python distribution and package. Installing
it provides one canonical `codereeve` executable with daemon, doctor, provenance, hook,
and verification subcommands. The former `bh-*` console scripts remain temporary
compatibility shims for the 0.2 and 0.3 release lines only.

The orchestration engine (`symphony`) is vendored into the package rather than installed
as an external dependency. Upstream `mraza007/baton` is dormant (3 commits, no releases,
no external PRs ever merged). The harness is the de facto maintainer of the vendored
source; `src/codereeve/vendor/symphony/` is linted and type-checked as owned code
(issue #224) and Baton bugs are fixed directly in it, the same as any other module.
`patches/` holds a frozen historical record of pre-#224 patches; it is not required for
new changes.

## Integration model [implemented]

The daemon runs against a target GitHub repository. Repo identity and App IDs live in
`${BH_PROJECT_ROOT}/.bh/config.env` (committed in the sandbox repo); the shell only
needs `BH_PROJECT_ROOT`. The `baton start -w` external-process model from the spike is
retired.

```bash
export BH_PROJECT_ROOT=/path/to/local/clone

bin/run-daemon.sh --once   # one poll-dispatch tick, then exit
```

The daemon polls the target repo for `agent-ready` issues, groups them into dependency-
ordered DAGs (milestones) or N=1 single-issue work units, calls `Orchestrator._run_worker(issue)`
directly for each DAG-ready issue, CI-gates each agent's PR, and opens a single
ready-for-review `feature/<slug> → main` PR when all issues in a work unit are done. It
never merges to `main`.

For a full walkthrough, see [docs/smoke-test-daemon.md](docs/smoke-test-daemon.md).

## Repo structure

```
baton-harness/
├── README.md
├── pyproject.toml               # package metadata, dev dependencies, ruff/mypy config
├── bin/
│   ├── run-daemon.sh            # launcher: validates env vars + labels, starts codereeve daemon
│   ├── setup-env.sh             # idempotent dev-env bootstrap: uv venv + editable install + optional bws check
│   ├── init-sandbox.sh          # provision a throwaway sandbox repo for a first smoke test
│   ├── provision-ruleset.sh     # create/repair the two branch-protection rulesets (required before first run)
│   ├── verify-recovery.sh       # exercise the five startup-reconciliation gates against a live sandbox
│   └── probe-merge-denial.sh    # assert all merge-bypass vectors are denied against a live sandbox PR
├── patches/                     # frozen historical record of pre-#224 vendor patches (not required for new changes — see patches/README.md)
│   ├── VP-1-run-hook-env.diff   # thread env= through run_hook (before_run base-ref fix)
│   ├── VP-2-exclude-labels-recheck.diff  # mid-turn blocked check — makes block terminal
│   └── mypy-strict-remediation.diff      # superseded by #224; original vendor-wide mypy exclusion
├── scripts/
│   └── pilot-dry-run.sh         # manual dry-run helper (development use)
├── src/
│   └── codereeve/               # canonical installable Python package
│       ├── __init__.py          # __version__
│       ├── _cli.py              # shared log/err helpers and issue-number resolver
│       ├── after_create.py      # codereeve hook after-create implementation
│       ├── before_run.py        # codereeve hook before-run implementation
│       ├── after_run.py         # codereeve hook after-run implementation
│       ├── chain/               # always-on daemon (issue #27, P0–P3)
│       │   ├── cli.py           # codereeve daemon implementation
│       │   ├── daemon.py        # poll loop, work-unit selection, top-level orchestration
│       │   ├── dag.py           # DAG construction (graphlib.TopologicalSorter)
│       │   ├── scheduler.py     # ready-frontier tracking (done/parked/dispatched)
│       │   ├── branches.py      # feature/<slug> branch creation and lifetime
│       │   ├── merge.py         # CI-gated --no-ff merge; REQUIRED_CHECKS constant
│       │   ├── escalation.py    # Slack webhook + GitHub issue comment escalation
│       │   ├── recovery.py      # crash recovery: reconstruct done/parked on start
│       │   ├── registry.py      # repo-registry (one entry in v1; seam for multi-repo)
│       │   └── gh_deps.py       # GitHub dependency API (blocked_by edges)
│       └── vendor/              # vendored symphony orchestrator (mraza007/baton)
│           └── symphony/
│               ├── VENDORING.md # provenance record: upstream SHA, license, historical patch annotations
│               └── ...          # orchestrator.py, worker.py, hooks.py, etc.
├── tests/                       # pytest suite
│   ├── test_smoke.py
│   ├── test_cli.py
│   ├── test_after_create.py
│   ├── test_after_run.py
│   ├── test_before_run.py
│   ├── test_hook_env_awareness.py
│   ├── chain/                   # daemon component tests
│   └── vendor/                  # vendored-patch regression tests
├── config/
│   └── WORKFLOW.md              # generic harness config + agent prompt (max_turns: 8)
├── templates/
│   └── CLAUDE.md.template       # source for each project's committed CLAUDE.md
└── docs/                        # design docs, research
    ├── harness-design.md        # architecture, vendoring decision, daemon design (§10), decision records, constraints
    ├── smoke-test-daemon.md     # first-run walkthrough, env vars, required labels
    └── ...
```

## Contributing

See [the contribution workflow](docs/contributing.md). Start from an issue, work on a
dedicated branch, and use a pull request to integrate changes into `main`.

## Python development

### Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (required for locked development and
  production verification)
- [`prek`](https://github.com/j178/prek#installation) for automated local pre-commit checks
- `git` (required by `bin/init-sandbox.sh` for sandbox repo operations)

### Setup

`bin/setup-env.sh` wraps these locked development steps (idempotent; pass
`--help` for details):

```bash
# Create the virtual environment, then install the project editably with
# the exact runtime and development dependencies recorded in uv.lock.
uv venv .venv
CODEREEVE_BUILD_DEVELOPMENT=1 uv sync --locked --extra dev
```

### Running the quality gate

`prek` (installed via `bin/setup-env.sh`, or separately with `pip install prek` or
`cargo install prek`) runs ruff check, ruff format --check, mypy src, and shellcheck
automatically on `git commit` via `.pre-commit-config.yaml`. The commands below mirror
what CI and the pre-commit hook run, for manual CI-parity reference or to run everything
without committing. Pytest remains CI/manual-only. All commands must be clean before
pushing:

```bash
# Lint
.venv/Scripts/python.exe -m ruff check .         # Windows
.venv/bin/python        -m ruff check .         # macOS/Linux

# Format check
.venv/Scripts/python.exe -m ruff format --check .
.venv/bin/python        -m ruff format --check .

# Type check
.venv/Scripts/python.exe -m mypy src
.venv/bin/python        -m mypy src

# Tests
.venv/Scripts/python.exe -m pytest               # Windows
.venv/bin/python        -m pytest               # macOS/Linux

# Frozen wheel and Python 3.10/3.13 installation proof
.venv/Scripts/codereeve.exe verify                # Windows
.venv/bin/codereeve verify                        # macOS/Linux
```

Note: `src/codereeve/vendor/symphony/` is **not** excluded from these checks. Issue `#224`
assimilated the vendored symphony tree as owned code — it is linted and type-checked
(`strict = true`) identically to the rest of `src/codereeve/`. `patches/` and
`src/codereeve/vendor/symphony/VENDORING.md` hold a historical record of the tree's
provenance and pre-#224 patches, not an active exclusion or re-vendor procedure.

### Production wheel verification

Development uses the editable environment above. Production installation is
non-editable and contains runtime dependencies only. Before deployment, run
`codereeve verify` from the repository's locked development environment.
The command:

1. rejects a missing or stale `uv.lock` and resource-mirror drift;
2. exports the locked runtime-only and development dependency graphs;
3. constrains the wheel build backend to the hashed development resolution;
4. builds an sdist and wheel, then inspects the wheel's resources and commands;
5. creates clean Python 3.10 and 3.13 environments outside the checkout;
6. syncs only locked runtime dependencies, installs the wheel with `--no-deps`,
   and rejects editable metadata or any development-only distribution; and
7. loads all packaged defaults and executes all installed console wrappers.

Use repeated `--python VERSION` arguments for a focused diagnostic run. CI and
release validation use the default 3.10 and 3.13 endpoints. The command only
reports drift; it never updates the lock or repairs resource mirrors.

Standard builds require an explicit PEP 440 `CODEREEVE_BUILD_VERSION` and a
40-hex-character `CODEREEVE_BUILD_SOURCE_REVISION`. In a checkout the revision must
match HEAD. Production
pins an immutable release tag (for example `v1.0.0`); release automation may derive
both assertions from that tag, but the hook validates explicit values, not tag names.
Editable installs opt in with `CODEREEVE_BUILD_DEVELOPMENT=1`, use stable `0.2.0.dev0`,
and record the exact commit at build time in `source_revision`.

The old build-variable names are accepted only as temporary 0.2/0.3 compatibility
surfaces and are removed in 0.4.0:

| Canonical variable | Temporary compatibility variable |
|---|---|
| `CODEREEVE_BUILD_VERSION` | `BH_BUILD_VERSION` |
| `CODEREEVE_BUILD_SOURCE_REVISION` | `BH_BUILD_SOURCE_REVISION` |
| `CODEREEVE_BUILD_DEVELOPMENT` | `BH_BUILD_DEVELOPMENT` |

Do not set both names for one value; use the canonical `CODEREEVE_BUILD_*` names in new
automation.

To reproduce the non-editable installation manually from the repository root,
use the following Bash commands. Set `RUNTIME_PYTHON` to the path for the host
platform as shown. Replace the illustrative version and revision with the selected
release identity; unset any ambient development-build flags first:

```bash
unset CODEREEVE_BUILD_DEVELOPMENT BH_BUILD_DEVELOPMENT
SOURCE_REVISION="$(git rev-parse HEAD)"
mkdir -p .tmp
uv lock --check
uv export --locked --no-emit-project --format requirements.txt \
  --output-file .tmp/runtime-requirements.txt
uv export --locked --extra dev --no-emit-project --format requirements.txt \
  --output-file .tmp/build-requirements.txt
CODEREEVE_BUILD_VERSION=1.0.0 \
CODEREEVE_BUILD_SOURCE_REVISION="$SOURCE_REVISION" \
uv build --build-constraints .tmp/build-requirements.txt --require-hashes \
  --sdist --wheel --out-dir .tmp/dist
uv venv .tmp/runtime-venv --python 3.13

RUNTIME_PYTHON=.tmp/runtime-venv/Scripts/python.exe  # Windows Git Bash
# RUNTIME_PYTHON=.tmp/runtime-venv/bin/python        # macOS/Linux
uv pip sync --python "$RUNTIME_PYTHON" .tmp/runtime-requirements.txt
uv pip install --python "$RUNTIME_PYTHON" --no-deps \
  .tmp/dist/codereeve-*.whl
uv pip check --python "$RUNTIME_PYTHON"
```

The build produces `codereeve-<version>-py3-none-any.whl` and
`codereeve-<version>.tar.gz`.

This staging environment contains the locked runtime closure and the built wheel;
it does not select the `dev` extra. Run `codereeve verify` before promoting the
wheel to a deployment environment. Its installed smoke processes run with empty
temporary home/config directories and only execution-essential environment values.

### Supported 0.2 upgrade and rollback

Build or obtain the 0.2 wheel before touching the active environment. Stage it in a
separate `.venv-codereeve`, install and verify it there, and only then switch the service
configuration to the new executable:

```bash
# Build with the release's exact tag-derived version and 40-hex revision, or
# place an independently obtained codereeve-0.2.0-py3-none-any.whl in dist/.
SOURCE_REVISION="$(git rev-parse HEAD)"
CODEREEVE_BUILD_VERSION=0.2.0 \
CODEREEVE_BUILD_SOURCE_REVISION="$SOURCE_REVISION" \
uv build --wheel --out-dir dist

uv venv .venv-codereeve
uv pip install --python .venv-codereeve/bin/python \
  dist/codereeve-0.2.0-py3-none-any.whl
.venv-codereeve/bin/codereeve verify --installed
```

On Windows, use `.venv-codereeve/Scripts/python.exe` and
`.venv-codereeve/Scripts/codereeve.exe verify --installed`. Change the existing service
configuration only after verification succeeds. Keep the original `.venv` completely
unchanged until the rollback window closes; rollback consists of pointing the service
configuration back to that original environment. Service and path renaming is outside
this upgrade step and remains tracked separately.

### Provenance and preflight

```bash
codereeve --version
codereeve provenance
codereeve doctor --phase installation --format json --strict
codereeve doctor --phase configuration --config /path/to/config.env --strict
codereeve doctor --phase live --strict
```

`codereeve --version` prints the installed distribution version; `codereeve provenance`
prints the validated packaged identity JSON without consulting Git or credentials.
Installation checks are offline and credential-free; configuration checks are
local-only; live checks may use credentials and network access. Repeat `--phase`
to select multiple phases; omission runs all three. Text is the default format.
`--config` selects an explicit file; otherwise selection uses
`$BH_PROJECT_ROOT/.bh/config.env`, with non-empty environment overrides.
An explicit `<root>/.bh/config.env` infers the project root when `BH_PROJECT_ROOT`
is unset or empty; daemon startup applies that root after the readiness gate.

Doctor is advisory (exit 0) unless `--strict` finds a critical failure (exit 1).
Unsafe report rendering exits 1 even without strict; usage errors exit 2. Daemon
startup always gates critical failures. `--check-vault` remains a live-check
compatibility command and exits 0 only on PASS. See
[operator preflight and JSON schema](docs/repository-onboarding.md#5-bh-daemon---doctor--strict--preflight-before-the-first-real-run)
for report fields and startup ordering.

### Filesystem migration

CodeReeve 0.2 inventories and migrates these configuration and state locations.
Managed config, baseline, and runtime state become one verified `.codereeve/`
publication; an existing canonical destination blocks rather than being merged
(#393; `src/codereeve/migration/inventory.py:L114-L207`).

| Scope | Temporary legacy source | Canonical destination |
|---|---|---|
| Managed config | `<project>/.bh/config.env` | `<project>/.codereeve/config.env` |
| Ruleset baseline | `<project>/.bh/ruleset-baseline.json` | `<project>/.codereeve/ruleset-baseline.json` |
| Runtime state | `<project>/.baton-harness/` | `<project>/.codereeve/` |
| Host config | `~/.config/baton-harness/host.env` | `~/.config/codereeve/host.env` |
| System secrets | `/etc/bh-daemon/secrets.env` | `/etc/codereeve/secrets.env` |
| Service unit, inventory only | `/etc/systemd/system/bh-daemon.service` | `/etc/systemd/system/codereeve.service` |

For each spelling, source precedence is operator environment, managed config,
host config, then system secrets. Unset is absent and empty is explicitly set.
Resolve both spellings independently, then compare the selected values exactly;
different canonical and temporary legacy values block without printing either
value. `BATON_HARNESS_DIR` pairs with `CODEREEVE_ROOT`, while third-party names
remain unchanged (#393; `src/codereeve/config_env.py:L158-L193`,
`src/codereeve/config_env.py:L280-L318`). `.symphony/` remains unchanged (#393;
`src/codereeve/paths.py:L59-L135`).

Run the read-only inventory before scheduling an apply:

```bash
codereeve migrate --check
codereeve migrate --check --format json
```

Text and schema-version-1 JSON carry the same deterministic report (#393;
`src/codereeve/migration/model.py:L148-L213`).

| Status | Exit code | Meaning |
|---|---:|---|
| `current` | 0 | No legacy migration is needed. |
| `ready` | 2 | A plan exists; apply still needs fresh quiescence authority. |
| `blocked` | 1 | A conflict, unsafe path, active writer, or incomplete transaction blocks apply. |

Apply accepts only ready data migrations and refuses current or blocked state.
Every writer must be stopped and held stopped by the authoritative coordinator
tracked in #394 before the transaction can proceed (#393, #394;
`src/codereeve/migration/transaction.py:L82-L108`).

```bash
codereeve migrate --apply
codereeve migrate --apply --format json
```

Apply creates verified private staging, unique backups, a checksummed manifest,
and `journal.jsonl` before publishing canonical state. Successful output names
the manifest, journal, each backup, and all manual-restoration paths. The
manifest carries `transaction_id`, `actions`, and `entries`; retain it, the
journal, backups, and private stages through 0.3.x (#393;
`src/codereeve/migration/transaction.py:L588-L743`,
`src/codereeve/migration/journal.py:L150-L186`).

Caught failures attempt restoration in reverse order. A complete result has
revalidated every original and absent canonical publication; incomplete or
unavailable evidence stays blocked and cannot authorize restart. Manual
restoration must use the exact manifest paths, reverse completed publications,
then reverse completed backups while verifying the manifest `entries` against
each `actions` source. Keep all evidence and writers stopped (#393, #394;
`src/codereeve/migration/transaction.py:L768-L920`).

There is no standalone restore command. #394 calls the restoration API with the
returned manifest path and owns service activation and rollback. Roll back
configuration and state before that coordinator activates the canonical
service; this migration never changes either service unit (#393, #394;
`src/codereeve/migration/transaction.py:L922-L958`).

Standalone apply currently refuses because its default operation cannot prove
authoritative coordinator quiescence. Real Windows apply is also blocked before
mutation because directory durability is unsupported; tests use a portable
injected storage capability rather than a public bypass flag (#393, #394;
`src/codereeve/migration/journal.py:L44-L65`). See the
[migration runbook](docs/codereeve-migration.md) for recovery evidence and the
manual reverse procedure.

### Unified command convention

The `codereeve` executable is the canonical command surface installed by
`pyproject.toml`:

| Command | Purpose |
|---|---|
| `codereeve daemon` | Run the always-on daemon; accepts the existing daemon options |
| `codereeve doctor` | Run installation, configuration, or live preflight checks |
| `codereeve migrate --check` | Inventory legacy and canonical migration state without mutation |
| `codereeve migrate --apply` | Apply only a ready migration under coordinator authority |
| `codereeve provenance` | Print validated packaged build provenance |
| `codereeve hook after-create` | Run the post-worktree-creation lifecycle hook |
| `codereeve hook before-run` | Run the pre-agent-turn lifecycle hook |
| `codereeve hook after-run` | Run the post-agent-turn lifecycle hook |
| `codereeve hook force-pr-not-merge` | Enforce the worker no-merge boundary |
| `codereeve verify` | Verify frozen wheel and installation foundations |

After `CODEREEVE_BUILD_DEVELOPMENT=1 uv sync --locked --extra dev`, `codereeve` is on
`PATH` inside the editable development venv. WORKFLOW.md hook lines use:

```yaml
hooks:
  after_create: codereeve hook after-create
  before_run:   codereeve hook before-run
  after_run:    codereeve hook after-run
```

The six legacy scripts remain temporary compatibility shims in 0.2 and 0.3 and are
removed in 0.4.0:

| Temporary legacy script | Canonical replacement |
|---|---|
| `bh-daemon` | `codereeve daemon` |
| `bh-after-create` | `codereeve hook after-create` |
| `bh-before-run` | `codereeve hook before-run` |
| `bh-after-run` | `codereeve hook after-run` |
| `bh-force-pr-not-merge` | `codereeve hook force-pr-not-merge` |
| `bh-verify-foundation` | `codereeve verify` |

The hooks derive the issue number from `ISSUE_NUMBER` (threaded by the vendored `run_hook
env=` patch VP-1). As a fallback, `basename($PWD)` is used — the worktree directory name
must contain the issue number. Both `feat-10-python-scaffold` and bare `10` (symphony's
own `.symphony/worktrees/<N>` convention) are valid. The prefix-optional regex accepts any
of: `<prefix>-<issue>`, `<prefix>-<issue>-<slug>`, or bare `<issue>`.

## Prerequisites (runtime)

For what each credential is, why it's required, and which module consumes it, see
[docs/authentication.md](docs/authentication.md). The list below is the operational
checklist — what to install and export before a first run.

- **`claude` CLI** on `PATH` and authenticated (run `claude` once interactively; the
  worker processes use OAuth, not an API key). `bin/setup-env.sh` checks for `claude` and,
  when running interactively on Linux/macOS, offers to auto-install via the official native
  installer. In non-interactive or CI contexts (`BH_SETUP_NO_PROMPT=1`) it exits 1 with a
  link to the [setup docs](https://docs.claude.com/en/docs/claude-code/setup).
- **`gh` CLI** authenticated (`gh auth login`, verify with `gh auth status`). `bin/setup-env.sh`
  checks for `gh` and, when running interactively on Linux/macOS, offers to auto-install
  v2.62.0 to `~/.local/bin`.
- `git` configured with user name and email
- **`bws` (Bitwarden Secrets CLI)** on `PATH` only when the selected App-key provider is
  `bws`, or when `BWS_GH_TOKEN_SECRET_ID` or `BWS_HEARTBEAT_PING_URL_SECRET_ID` is set.
  `bin/setup-env.sh` checks for `bws` and offers to auto-install v2.1.0 to
  `~/.local/bin`. Declining or running non-interactively without it prints conditional
  guidance and continues without downloading; verify with `bws --version` when your
  deployment uses BWS.
- **`BWS_ACCESS_TOKEN`** only for those same BWS-backed configurations. Provide this
  operator-supplied machine-account token in a root-readable-only file and never commit it.
  `bin/install-daemon-service.sh` writes `/etc/bh-daemon/secrets.env` (mode `600`) and adds
  `EnvironmentFile=` only when the resolved configuration needs BWS; file-only deployments
  create neither. See [docs/smoke-test-daemon.md §"systemd unit
  (recommended)"](docs/smoke-test-daemon.md).
- **GitHub App** created, installed on the target repo, with the required permissions
  configured **before** first run (table in [docs/authentication.md](docs/authentication.md)).
  `bin/run-daemon.sh` reads the App IDs from `${BH_PROJECT_ROOT}/.bh/config.env`;
  `bin/provision-ruleset.sh` uses them to create the branch-protection rulesets.
- **`~/.claude/.credentials.json` present and readable** — the OAuth credential file the
  worker processes use. Checked at every daemon start (fatal if absent or unreadable). On a
  server this must be an explicit credential-volume mount.
- **`ANTHROPIC_API_KEY` must NOT be set** — checked at every daemon start (fatal, critical
  alert if present). Do not set this variable in shell profiles, `.env` files, systemd
  `EnvironmentFile=`, or Docker entrypoints.
- **OS:** Linux/macOS, bash. The server deployment scripts (`bin/verify-recovery.sh` in
  particular) are Linux-only.
- `config/WORKFLOW.md` present in this repo (already committed — see `config/`)
- The target project repo cloned locally (`BH_PROJECT_ROOT`)
- The target project repo must have all six harness labels (see
  [Required GitHub labels](#required-github-labels) below)
- The target project repo must have `.symphony/` in its `.gitignore` — `bin/run-daemon.sh`
  enforces this with a preflight check and aborts ("this repo is not ready for harness work")
  if the entry is absent; without it, `gh pr create` warns about an uncommitted change and
  the state file pollutes the tree (see
  [docs/smoke-test-daemon.md](docs/smoke-test-daemon.md) for details; `bin/init-sandbox.sh`
  seeds this automatically for throwaway sandboxes)

The separate `baton` package is **not required** — `symphony` is vendored inside the
`codereeve` package. Use `CODEREEVE_BUILD_DEVELOPMENT=1 uv sync --locked --extra dev`
for an editable local development installation.

### GitHub App private-key provider

Select exactly one provider in `${BH_PROJECT_ROOT}/.bh/config.env`. Existing BWS
deployments must add the selector explicitly; there is no legacy default:

```bash
# Existing BWS deployment
export BH_GITHUB_APP_KEY_PROVIDER=bws
export BWS_PEM_SECRET_ID=<uuid>
```

```bash
# BWS-free file deployment
export BH_GITHUB_APP_KEY_PROVIDER=file
export BH_GITHUB_APP_PRIVATE_KEY_FILE=/run/credentials/bh-daemon/app.pem
```

`BWS_ACCESS_TOKEN` is a shell/service bootstrap secret, not provider configuration. Never
store it in `${BH_PROJECT_ROOT}/.bh/config.env`. For an interactive BWS-backed run, export
it separately in the caller's shell:

```bash
read -r -s BWS_ACCESS_TOKEN
export BWS_ACCESS_TOKEN
```

For `file`, the path must be absolute and the target must be a regular, non-symlink file
with owner-only permissions (`0400` or `0600`). Its contents must be a non-empty UTF-8 PEM
of at most 1 MiB. Harness reads the file once and proves it can sign an App JWT before any
GitHub request; it does not generate, copy, enroll, or rotate the key.

| App-key provider | Optional BWS PAT/heartbeat IDs | `bws` and `BWS_ACCESS_TOKEN` |
|---|---|---|
| `bws` | Either | Required |
| `file` | Neither configured | Not required |
| `file` | Either configured | Required for those optional fetches |

## Required GitHub labels

The target project repo must have all six harness labels before running
`bin/run-daemon.sh`. The launcher runs a preflight check and exits non-zero with
actionable instructions if any are absent. It does not auto-create labels — label creation
is an operator action.

| Label | Purpose |
|---|---|
| `agent-ready` | Issue is eligible for an agent run |
| `agent-in-progress` | Agent is actively running against this issue |
| `agent-done` | Agent has opened a PR (human reviews) |
| `agent-failed` | Charged failure budget exhausted; human triage required |
| `agent-merged` | Per-issue branch merged into the feature branch by the daemon (CI-gated) |
| `blocked` | Agent needs human input; sub-tree is parked |

To create all six labels, see the exact `gh label create` commands in
[docs/smoke-test-daemon.md §"Required labels"](docs/smoke-test-daemon.md) (kept in sync
with the runbook; duplicating them here risks drift). The commands are also printed by
`bin/run-daemon.sh` when it detects a missing label. `bin/init-sandbox.sh` creates all
six automatically when provisioning a throwaway sandbox.

To retry an `agent-failed` issue after triage, remove `agent-failed` and add
`agent-ready`. Do not leave both labels applied.

## GitHub token: least-privilege setup

**Primary safeguard — use a dedicated bot/machine account.** Run the harness
under a GitHub account that holds no memberships, no organization roles, and no
permissions beyond what the harness requires. Structural least-privilege — the
account literally cannot perform destructive actions — is the real security
boundary. No software check substitutes for it.

Auth method, required permissions, the optional worker-hook PAT's narrower permission
surface relative to the mandatory GitHub App, and the validation gates are documented in
[docs/authentication.md](docs/authentication.md) — this section is a pointer,
not a restatement. Quick reference for exporting the optional hook PAT directly
(bypassing the Bitwarden vault fetch):

```bash
export GH_TOKEN=github_pat_<your-token>
export BH_PROJECT_ROOT=/path/to/local/clone
bin/run-daemon.sh
```

This PAT does not replace the App ID, installation ID, or selected App private-key
provider required in `${BH_PROJECT_ROOT}/.bh/config.env`.

## Safety and guardrails

`codereeve daemon` spawns real `claude -p --dangerously-skip-permissions` processes that write
code, commit, push branches, and open GitHub PRs autonomously. Before running:

- **Use a throwaway sandbox repo** — never a real project. See [Prerequisites (runtime)](#prerequisites-runtime).
- **Always start with `--once`** for a first run — one poll-dispatch tick, then exit.
- **No-merge boundary** — the daemon opens `feature/<slug> → main` PRs ready for review
  and never merges to `main`. A human reviews and merges.
- **`ANTHROPIC_API_KEY` must not be set** — OAuth/subscription auth only; the key's presence
  triggers an immediate abort at startup.
- See [docs/smoke-test-daemon.md §"WARNING: safety first"](docs/smoke-test-daemon.md) for
  the full pre-run checklist.

## Usage

### Running the daemon

`codereeve daemon` is the always-on poll loop that watches a GitHub repo for `agent-ready`
issues, runs Claude Code agents against them in dependency order, CI-gates each agent's
PR, and opens a ready-for-review `feature/<slug> → main` PR when a work unit completes.
It never merges to `main`.

**Required shell variable:**

| Variable | Description |
|---|---|
| `BH_PROJECT_ROOT` | Absolute path to the local clone of the target repo |

Repo identity (`BH_REPO_OWNER`, `BH_REPO_NAME`), GitHub App IDs, the explicit App-key
provider and its selected source, and optional vault secret IDs are read from
`${BH_PROJECT_ROOT}/.bh/config.env` at startup. `bin/init-sandbox.sh` writes that file
interactively at provision time and prompts to overwrite or reuse it on a re-run.

**Quickstart (one tick, then exit):**

```bash
export BH_PROJECT_ROOT=/path/to/local/clone

bin/run-daemon.sh --once
```

The `--once` flag runs exactly one poll-dispatch tick then exits — safe for a first run.

**Startup reconciliation sweep (as of #40):** before entering the poll loop, the daemon runs a one-time reconciliation sweep:

- **Fatal credential validation (gates G3a–G3d).** GitHub token, `ANTHROPIC_API_KEY` absence, OAuth credential-volume readability, and git credential-helper presence are each checked and fatal on failure. See [docs/authentication.md](docs/authentication.md) for what each gate validates and why. Previously, bad credentials caused every worker dispatch to fail silently.
- **Ungraceful-prior-exit detection.** A `.baton-harness/daemon.alive` marker is written at startup and removed on graceful shutdown. If the marker is present at boot, the prior run ended ungracefully (likely OOM-kill) — a critical alert fires. This is the only tractable notification for an uncatchable SIGKILL: the harness reports it on the next boot.
- **Orphan `claude` process sweep.** A `pgrep`-based scan detects any `claude -p` processes left over from a crashed prior run. Matches emit a warn alert with the PID list. Detection only — no auto-kill in v1.

**Continuous operation:**

```bash
bin/run-daemon.sh   # polls continuously; stop with Ctrl-C
```

**Using the canonical console script directly** (after the editable install above):

```bash
codereeve daemon --once
codereeve daemon           # continuous
```

**Additional variables set automatically by the launcher:**

| Variable | How it is set | Purpose |
|---|---|---|
| `BATON_HARNESS_DIR` | Derived from the script's own location | Harness repo root; available to hook scripts |
| `BH_VENV` | Derived from the temporary compatibility `bh-daemon` binary location | Hooks self-activate the venv |

**Optional:**

| Variable | Default | Purpose |
|---|---|---|
| `BH_SLACK_WEBHOOK_URL` | (unset) | If set, escalation notices are POSTed to Slack. If unset, Slack is skipped silently and the GitHub issue comment is the only durable escalation record. |
| `BH_HEARTBEAT_PING_URL` | (unset) | Healthchecks.io-style ping URL. When set, the daemon GETs this URL once per heartbeat tick (nominally every 30 s; actual interval is 30 s plus ping latency, as the ping runs synchronously last in each tick) so an external dead-man's-switch service can alarm if pings stop. Unset = no external ping; local heartbeat file is still written. See [docs/harness-design.md §11](docs/harness-design.md) for setup and threshold guidance. |
| `BH_HEARTBEAT_FILE` | `${BH_PROJECT_ROOT}/.baton-harness/heartbeat` | Path for the local liveness file written on each heartbeat tick. Override to direct the file to a location convenient for your monitoring setup. |
| `BH_WORKER_PROGRESS_STALL_S` | `1800` | Seconds without a turn-progress signal during the worker-active phase (fresh dispatch) before a progress-stall alert fires. 1800 s is 6× the 300 s per-turn timeout. Non-numeric value logs a WARNING and falls back to the default. |
| `BH_WORKTREE_GC` | `detect` | Worktree orphan-GC mode. `detect` logs orphaned worktrees without removing them (safe default). `reclaim` additionally removes confirmed orphans. Unrecognised value logs a WARNING and falls back to `detect`. |
| `BH_MAX_ISSUE_FAILURES` | `2` | Consecutive charged worker failures allowed before the issue becomes `agent-failed`. This is lower than `BH_REDISPATCH_MAX` because it counts consumed worker runs, while redispatch counts crash-recovery attempts. |
| `BH_FAILURE_COUNTS_PATH` | `${BH_PROJECT_ROOT}/.baton-harness/failure-counts.json` | Durable charged-failure counts and one-time alert state. |

`bin/init-sandbox.sh` provisions a throwaway sandbox repo for a smoke test. Its
`--scenario <name>` flag (or `BH_SCENARIO`) selects `hello` (the existing default),
`terminal-block` (one dual-labeled no-dispatch issue), or `recovery` (no issues); shared
setup still creates the labels, stub CI workflow, and `${BH_PROJECT_ROOT}/.bh/config.env`.

`bin/run-daemon.sh` now requires only `BH_PROJECT_ROOT` in the shell. Repo identity,
GitHub App IDs, the App-key provider/source, and optional vault secret IDs are read at
daemon startup from `${BH_PROJECT_ROOT}/.bh/config.env`.

For the full first-run walkthrough — sandbox setup, trigger-issue creation, DAG dependency
wiring, CI-gate behaviour, and expected log output — see
[docs/smoke-test-daemon.md](docs/smoke-test-daemon.md).

### First run — quick start

**Bringing up `codereeve daemon` on a machine that has never run it before?** See
[docs/system-setup.md](docs/system-setup.md) for machine-level setup (CLIs, the Python
venv), then [docs/repository-onboarding.md](docs/repository-onboarding.md) for the
repo/sandbox-level walkthrough — prerequisites, each `bin/*.sh` step with its verification
command, the `codereeve doctor --strict` preflight, the provider-aware
`--check-vault` App-key dry-run, and troubleshooting. The summary below assumes the CLIs
are already installed and is a quick reference, not a walkthrough.

The four-step bringup sequence from [docs/smoke-test-daemon.md §"Fresh host bringup"](docs/smoke-test-daemon.md):

```bash
# Step 1 — create the venv, install the package, and record BH_PROJECT_ROOT.
#   Requires uv, gh, and claude; checks optional bws and offers to auto-install it.
#   Missing/declined bws prints conditional guidance and does not stop setup.
#   Writes BH_PROJECT_ROOT to ~/.config/baton-harness/host.env (mode 600),
#   prompting to overwrite or reuse an existing file on a re-run.
bin/setup-env.sh

# Step 2 — provision the throwaway sandbox repo.
#   Reads BH_REPO_OWNER, BH_REPO_NAME, BH_PROJECT_ROOT from the environment.
#   Prompts for the App IDs, bws/file selector, the selected key source, and
#   optional BWS secret IDs, then writes ${BH_PROJECT_ROOT}/.bh/config.env.
#   Optional: --scenario hello|terminal-block|recovery (or BH_SCENARIO).
export BH_REPO_OWNER=<owner>
export BH_REPO_NAME=<repo>
export BH_PROJECT_ROOT=<abs-path-to-local-sandbox-clone>
bin/init-sandbox.sh

# Step 3 — provision branch-protection rulesets (required before first run).
#   Reads the App IDs and selected key provider from .bh/config.env. Provider bws
#   needs the bootstrap token here; file mode does not. Optional BWS IDs need it
#   later when the daemon starts.
# read -r -s BWS_ACCESS_TOKEN
# export BWS_ACCESS_TOKEN
bin/provision-ruleset.sh

# Step 4 — run one bounded tick. A file-only deployment needs no BWS environment.
bin/run-daemon.sh --once
```

Step 4 above is the bounded, single-tick smoke test. For continuous operation, install the
compatibility-named `bh-daemon` systemd unit with `bin/install-daemon-service.sh`; it
creates a secrets file
only when BWS is needed — see
[docs/smoke-test-daemon.md §"systemd unit (recommended)"](docs/smoke-test-daemon.md) for the
one-command invocation, flags, and the manual/reference unit it generates.

The runbook at [docs/smoke-test-daemon.md](docs/smoke-test-daemon.md) has the full
walkthrough — expected log output, CI-gate subtleties, DAG dependency wiring, cleanup, and
server deployment patterns. Read it before running.

### GitHub repository ruleset (required before first run — issue #157)

**Provision the merge-boundary rulesets before starting the daemon for the first time.**
The per-launch preflight gate checks `ruleset_is_provisioned()` before every worker
dispatch and parks every issue with "preflight refused — branch protection missing or
misconfigured" when rulesets are absent. The only way to create them is to run
`bin/provision-ruleset.sh`.

Run after `bin/init-sandbox.sh` (the App IDs it wrote to `.bh/config.env` are read here):

```bash
# Required: BOTH App identifiers (they are different integers).
#   BH_GITHUB_APP_ID is shown at https://github.com/settings/apps/<slug>
#     (also returned by `gh api /app --jq .id`).
#   BH_GITHUB_APP_INSTALLATION_ID is returned by
#     `gh api /repos/<owner>/<repo>/installation --jq .id`.
export BH_REPO_OWNER=<owner>
export BH_REPO_NAME=<sandbox-repo>
export BH_GITHUB_APP_ID=<numeric-from-/app>
export BH_GITHUB_APP_INSTALLATION_ID=<numeric-from-/repos/.../installation>
# Select exactly one App private-key source (normally already in .bh/config.env).
# BWS:
export BH_GITHUB_APP_KEY_PROVIDER=bws
export BWS_PEM_SECRET_ID=<bws-secret-id-for-app-private-key>
export BWS_ACCESS_TOKEN=<bws-access-token>
# Or file (unset BWS_PEM_SECRET_ID; no BWS token unless an optional BWS ID is set):
# export BH_GITHUB_APP_KEY_PROVIDER=file
# export BH_GITHUB_APP_PRIVATE_KEY_FILE=/run/credentials/bh-daemon/app.pem
# Optional: override the RepositoryRole admin actor_id (default 5).
# Only needed if your org has remapped role ids.
# export BH_ADMIN_ROLE_ID=5
bin/provision-ruleset.sh
```

This creates two rulesets:

- `harness-main-no-merge` — denies any push/merge into the default branch except by a
  repository admin (RepositoryRole bypass).
- `harness-feature-daemon-only` — denies pushes to `feature/*` branches except by the
  daemon's GitHub App installation (the legitimate per-issue merger).

The script is idempotent — safe to re-run. It uses the GitHub Rulesets REST API's
list-then-by-id endpoint shape (per the [API contract](https://docs.github.com/en/rest/repos/rules?apiVersion=2022-11-28)
— `GET /rulesets/{ruleset_id}` requires a numeric id) and runs a preflight cross-check
that `BH_GITHUB_APP_ID` matches `GET /app`. It also validates the admin bypass
assumption before writing rulesets: the repo must report at least one admin
collaborator via `GET /repos/<owner>/<repo>/collaborators?permission=admin`, and any
non-default `BH_ADMIN_ROLE_ID` override must be confirmed through the org custom
repository-roles API. See issue #157 and the merge PR for the design.

## CLAUDE.md for the pilot project

Each target project must have a committed `CLAUDE.md` so Claude Code can discover
repo-local context. The harness owns the source template at
`templates/CLAUDE.md.template`; the live file is committed to the project repo (it is
irreducibly project-local — Claude Code discovers it from the worktree).

**For the pilot (one project):** copy the template manually, fill in the
`<!-- FILL: ... -->` markers, and commit the result as `CLAUDE.md` in the project repo. A
generate step is not warranted for a single project; add one when project #2 appears.

```bash
cp /path/to/baton-harness/templates/CLAUDE.md.template /path/to/project/CLAUDE.md
# edit CLAUDE.md to fill in all markers, then:
git -C /path/to/project add CLAUDE.md && git -C /path/to/project commit -m "Add CLAUDE.md from harness template"
```

## Design documentation

See `docs/` for the full design:

- `docs/harness-design.md` — architecture, vendoring decision (§1), daemon design (§10), component descriptions, decision records (D1/D2, spike findings), project constraints
- `docs/smoke-test-daemon.md` — first-run walkthrough, env vars, required labels, CI-gate subtleties
- `docs/architecture-spec.md` — overall system architecture
