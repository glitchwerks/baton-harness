# baton-harness

A reusable policy and tooling layer for autonomous Claude Code agent runs. The harness
owns the lifecycle hook modules, per-project workflow config, context templates, and the
always-on daemon. The orchestration engine (`symphony`, from
[mraza007/baton](https://github.com/mraza007/baton)) is vendored into the package and
called directly as a library.

**Current state [implemented, #27]:** the `symphony` package is vendored at
`src/baton_harness/vendor/symphony/` and the always-on daemon (`src/baton_harness/chain/`)
calls `Orchestrator._run_worker(issue)` directly — no subprocess, no `baton start`. The
daemon is the entry point; `bin/run-daemon.sh` is the launcher. See
[docs/harness-design.md §1 and §10](docs/harness-design.md) for the design rationale.

## What this is

The harness owns everything *shareable* across projects: the Python hook modules that run
before and after each agent turn, per-project workflow config, a CLAUDE.md template, and
the `bh-daemon` always-on daemon. Each target project carries only its own committed
`CLAUDE.md` and CI workflow.

The hooks and daemon are shipped as a proper Python package (`baton_harness`) with console
entry points (`bh-after-create`, `bh-before-run`, `bh-after-run`, `bh-daemon`) so they
are on `PATH` after `pip install` and can be wired directly into WORKFLOW.md hook lines
without path gymnastics.

The orchestration engine (`symphony`) is vendored into the package rather than installed
as an external dependency. Upstream `mraza007/baton` is dormant (3 commits, no releases,
no external PRs ever merged). The harness is the de facto maintainer of the vendored
source; `src/baton_harness/vendor/symphony/` is linted and type-checked as owned code
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
│   ├── run-daemon.sh            # launcher: validates env vars + labels, starts bh-daemon
│   ├── setup-env.sh             # idempotent dev-env bootstrap: uv venv + editable install + bws check
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
│   └── baton_harness/           # installable Python package
│       ├── __init__.py          # __version__
│       ├── _cli.py              # shared log/err helpers and issue-number resolver
│       ├── after_create.py      # bh-after-create hook entry point
│       ├── before_run.py        # bh-before-run hook entry point
│       ├── after_run.py         # bh-after-run hook entry point
│       ├── chain/               # always-on daemon (issue #27, P0–P3)
│       │   ├── cli.py           # bh-daemon entry point
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
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- [`prek`](https://github.com/j178/prek#installation) for automated local pre-commit checks
- `git` (required by `bin/init-sandbox.sh` for sandbox repo operations)

### Setup

`bin/setup-env.sh` wraps these two steps (idempotent; pass `--help` for details):

```bash
# Create and populate the virtual environment
uv venv .venv
uv pip install -e ".[dev]"
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
```

Note: `src/baton_harness/vendor/symphony/` is **not** excluded from these checks. Issue `#224`
assimilated the vendored symphony tree as owned code — it is linted and type-checked
(`strict = true`) identically to the rest of `src/baton_harness/`. `patches/` and
`src/baton_harness/vendor/symphony/VENDORING.md` hold a historical record of the tree's
provenance and pre-#224 patches, not an active exclusion or re-vendor procedure.

### Hook entry-point convention

The three lifecycle hooks and the daemon are installed as console scripts by `pyproject.toml`:

| Script | Entry point | Module |
|---|---|---|
| `bh-after-create` | `baton_harness.after_create:main` | `src/baton_harness/after_create.py` |
| `bh-before-run` | `baton_harness.before_run:main` | `src/baton_harness/before_run.py` |
| `bh-after-run` | `baton_harness.after_run:main` | `src/baton_harness/after_run.py` |
| `bh-daemon` | `baton_harness.chain.cli:main` | `src/baton_harness/chain/cli.py` |
| `bh-force-pr-not-merge` | `baton_harness.hooks.force_pr_not_merge:main` | `src/baton_harness/hooks/force_pr_not_merge.py` |

After `uv pip install -e ".[dev]"`, these commands are on `PATH` inside the venv.
WORKFLOW.md hook lines wire them as:

```yaml
hooks:
  after_create: bh-after-create
  before_run:   bh-before-run
  after_run:    bh-after-run
```

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
- **`bws` (Bitwarden Secrets CLI)** on `PATH` — required for vault-fetching the GitHub App PEM
  key and optional secrets at daemon startup. `bin/setup-env.sh` checks for `bws` and offers
  to auto-install v2.1.0 to `~/.local/bin` when running interactively. Verify with
  `bws --version`. Without it the daemon fails immediately at startup.
- **`BWS_ACCESS_TOKEN`** exported — the operator-supplied Bitwarden machine-account access
  token. Provide it in a root-readable-only file and never commit it. The canonical server
  path is `/etc/bh-daemon/secrets.env` (mode `600`); `bin/install-daemon-service.sh` writes
  this file for you as part of the systemd install — see
  [docs/smoke-test-daemon.md §"systemd unit (recommended)"](docs/smoke-test-daemon.md) for the
  `EnvironmentFile=` pattern it produces.
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
- The target project repo must have all five harness state labels (see
  [Required GitHub labels](#required-github-labels) below)
- The target project repo must have `.symphony/` in its `.gitignore` — `bin/run-daemon.sh`
  enforces this with a preflight check and aborts ("this repo is not ready for harness work")
  if the entry is absent; without it, `gh pr create` warns about an uncommitted change and
  the state file pollutes the tree (see
  [docs/smoke-test-daemon.md](docs/smoke-test-daemon.md) for details; `bin/init-sandbox.sh`
  seeds this automatically for throwaway sandboxes)

The separate `baton` pip install is **not required** — `symphony` is vendored inside the
`baton_harness` package. Only `uv pip install -e .` (or `pip install -e .`) is needed.

## Required GitHub labels

The target project repo must have all five harness state labels before running
`bin/run-daemon.sh`. The launcher runs a preflight check and exits non-zero with
actionable instructions if any are absent. It does not auto-create labels — label creation
is an operator action.

| Label | Purpose |
|---|---|
| `agent-ready` | Issue is eligible for an agent run |
| `agent-in-progress` | Agent is actively running against this issue |
| `agent-done` | Agent has opened a PR (human reviews) |
| `agent-merged` | Per-issue branch merged into the feature branch by the daemon (CI-gated) |
| `blocked` | Agent needs human input; sub-tree is parked |

To create all five labels, see the exact `gh label create` commands in
[docs/smoke-test-daemon.md §"Required labels"](docs/smoke-test-daemon.md) (kept in sync
with the runbook; duplicating them here risks drift). The commands are also printed by
`bin/run-daemon.sh` when it detects a missing label. `bin/init-sandbox.sh` creates all
five automatically when provisioning a throwaway sandbox.

## GitHub token: least-privilege setup

**Primary safeguard — use a dedicated bot/machine account.** Run the harness
under a GitHub account that holds no memberships, no organization roles, and no
permissions beyond what the harness requires. Structural least-privilege — the
account literally cannot perform destructive actions — is the real security
boundary. No software check substitutes for it.

Auth method, required permissions, the fallback PAT's narrower permission
surface relative to the GitHub App, and the validation gates are documented in
[docs/authentication.md](docs/authentication.md) — this section is a pointer,
not a restatement. Quick reference for exporting the fallback PAT directly
(bypassing the Bitwarden vault fetch):

```bash
export GH_TOKEN=github_pat_<your-token>
export BH_PROJECT_ROOT=/path/to/local/clone
bin/run-daemon.sh
```

## Safety and guardrails

`bh-daemon` spawns real `claude -p --dangerously-skip-permissions` processes that write
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

`bh-daemon` is the always-on poll loop that watches a GitHub repo for `agent-ready`
issues, runs Claude Code agents against them in dependency order, CI-gates each agent's
PR, and opens a ready-for-review `feature/<slug> → main` PR when a work unit completes.
It never merges to `main`.

**Required shell variable:**

| Variable | Description |
|---|---|
| `BH_PROJECT_ROOT` | Absolute path to the local clone of the target repo |

Repo identity (`BH_REPO_OWNER`, `BH_REPO_NAME`), GitHub App IDs, and vault secret IDs are read from `${BH_PROJECT_ROOT}/.bh/config.env` at startup — the operator does not export them by hand. `bin/init-sandbox.sh` writes that file interactively at provision time and prompts to overwrite or reuse it on a re-run.

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

**Using the console script directly** (after `uv pip install -e .`):

```bash
bh-daemon --once
bh-daemon           # continuous
```

**Additional variables set automatically by the launcher:**

| Variable | How it is set | Purpose |
|---|---|---|
| `BATON_HARNESS_DIR` | Derived from the script's own location | Harness repo root; available to hook scripts |
| `BH_VENV` | Derived from the `bh-daemon` binary location | Hooks self-activate the venv |

**Optional:**

| Variable | Default | Purpose |
|---|---|---|
| `BH_SLACK_WEBHOOK_URL` | (unset) | If set, escalation notices are POSTed to Slack. If unset, Slack is skipped silently and the GitHub issue comment is the only durable escalation record. |
| `BH_HEARTBEAT_PING_URL` | (unset) | Healthchecks.io-style ping URL. When set, the daemon GETs this URL once per heartbeat tick (nominally every 30 s; actual interval is 30 s plus ping latency, as the ping runs synchronously last in each tick) so an external dead-man's-switch service can alarm if pings stop. Unset = no external ping; local heartbeat file is still written. See [docs/harness-design.md §11](docs/harness-design.md) for setup and threshold guidance. |
| `BH_HEARTBEAT_FILE` | `${BH_PROJECT_ROOT}/.baton-harness/heartbeat` | Path for the local liveness file written on each heartbeat tick. Override to direct the file to a location convenient for your monitoring setup. |
| `BH_WORKER_PROGRESS_STALL_S` | `1800` | Seconds without a turn-progress signal during the worker-active phase (fresh dispatch) before a progress-stall alert fires. 1800 s is 6× the 300 s per-turn timeout. Non-numeric value logs a WARNING and falls back to the default. |
| `BH_WORKTREE_GC` | `detect` | Worktree orphan-GC mode. `detect` logs orphaned worktrees without removing them (safe default). `reclaim` additionally removes confirmed orphans. Unrecognised value logs a WARNING and falls back to `detect`. |

`bin/init-sandbox.sh` provisions a throwaway sandbox repo for a smoke test. Its
`--scenario <name>` flag (or `BH_SCENARIO`) selects `hello` (the existing default),
`terminal-block` (one dual-labeled no-dispatch issue), or `recovery` (no issues); shared
setup still creates the labels, stub CI workflow, and `${BH_PROJECT_ROOT}/.bh/config.env`.

`bin/run-daemon.sh` now requires only `BH_PROJECT_ROOT` in the shell. Repo identity,
GitHub App IDs, and vault secret IDs are read at daemon startup from
`${BH_PROJECT_ROOT}/.bh/config.env`.

For the full first-run walkthrough — sandbox setup, trigger-issue creation, DAG dependency
wiring, CI-gate behaviour, and expected log output — see
[docs/smoke-test-daemon.md](docs/smoke-test-daemon.md).

### First run — quick start

**Bringing up `bh-daemon` on a machine that has never run it before?** See
[docs/system-setup.md](docs/system-setup.md) for machine-level setup (CLIs, the Python
venv), then [docs/repository-onboarding.md](docs/repository-onboarding.md) for the
repo/sandbox-level walkthrough — prerequisites, each `bin/*.sh` step with its verification
command, the `bh-daemon --doctor --strict` preflight, the opt-in `--check-vault` live vault
dry-run, and troubleshooting. The summary below assumes the CLIs are already installed and
is a quick reference, not a walkthrough.

The four-step bringup sequence from [docs/smoke-test-daemon.md §"Fresh host bringup"](docs/smoke-test-daemon.md):

```bash
# Step 1 — create the venv, install the package, and record BH_PROJECT_ROOT.
#   Checks for bws; offers to auto-install on Linux/macOS when interactive.
#   Writes BH_PROJECT_ROOT to ~/.config/baton-harness/host.env (mode 600),
#   prompting to overwrite or reuse an existing file on a re-run.
bin/setup-env.sh

# Step 2 — provision the throwaway sandbox repo.
#   Reads BH_REPO_OWNER, BH_REPO_NAME, BH_PROJECT_ROOT from the environment.
#   Prompts interactively for the 5 App/vault identity values and writes
#   ${BH_PROJECT_ROOT}/.bh/config.env; re-runs prompt to overwrite or reuse it.
#   Optional: --scenario hello|terminal-block|recovery (or BH_SCENARIO).
export BH_REPO_OWNER=<owner>
export BH_REPO_NAME=<repo>
export BH_PROJECT_ROOT=<abs-path-to-local-sandbox-clone>
bin/init-sandbox.sh

# Step 3 — provision branch-protection rulesets (required before first run).
#   Reads App IDs from ${BH_PROJECT_ROOT}/.bh/config.env. Requires BWS_ACCESS_TOKEN
#   already exported in this shell (it mints a GitHub App JWT via a vault fetch) —
#   see docs/repository-onboarding.md §3 for the secure (silent-input) export pattern
#   and for why this must come before step 4 below.
read -r -s BWS_ACCESS_TOKEN
export BWS_ACCESS_TOKEN
bin/provision-ruleset.sh

# Step 4 — drop the bootstrap secret and run one tick.
#   Uses the $BWS_ACCESS_TOKEN already exported in step 3 — never re-typed or echoed.
sudo install -m 600 /dev/null /etc/bh-daemon/secrets.env
printf 'BWS_ACCESS_TOKEN=%s\n' "$BWS_ACCESS_TOKEN" |
  sudo tee /etc/bh-daemon/secrets.env >/dev/null
bin/run-daemon.sh --once
```

Step 4 above is the bounded, single-tick smoke test. For continuous operation, install the
`bh-daemon` systemd unit with `bin/install-daemon-service.sh` instead of writing
`secrets.env` by hand — see
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
# Required: the harness App's private-key secret ID (in .bh/config.env) and
# the Bitwarden Secrets Manager access token (exported in the shell) used to
# fetch it — both consumed by `app_auth.py` when it mints the App JWT.
export BWS_PEM_SECRET_ID=<bws-secret-id-for-app-private-key>
export BWS_ACCESS_TOKEN=<bws-access-token>
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
