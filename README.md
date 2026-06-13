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
source; Baton bugs are fixed directly in `src/baton_harness/vendor/symphony/` and
recorded as patches in `patches/`.

## Integration model [implemented]

The daemon runs against a target GitHub repository, driven entirely by environment
variables. The `baton start -w` external-process model from the spike is retired.

```bash
export BH_REPO_OWNER=<owner>
export BH_REPO_NAME=<repo>
export BH_PROJECT_ROOT=/path/to/local/clone

bin/run-daemon.sh --once   # one poll-dispatch tick, then exit
```

The daemon polls the target repo for `agent-ready` issues, groups them into dependency-
ordered DAGs (milestones) or N=1 single-issue work units, calls `Orchestrator._run_worker(issue)`
directly for each DAG-ready issue, CI-gates each agent's PR, and opens a single draft
`feature/<slug> → main` PR when all issues in a work unit are done. It never merges to
`main`.

For a full walkthrough, see [docs/smoke-test-daemon.md](docs/smoke-test-daemon.md).

## Repo structure

```
baton-harness/
├── README.md
├── pyproject.toml               # package metadata, dev dependencies, ruff/mypy config
├── bin/
│   ├── run-daemon.sh            # launcher: validates env vars + labels, starts bh-daemon
│   ├── setup-env.sh             # idempotent dev-env bootstrap: uv venv + editable install
│   └── init-sandbox.sh          # provision a throwaway sandbox repo for a first smoke test
├── patches/                     # vendor patches (diff format, # VENDOR-PATCH markers)
│   ├── VP-1-run-hook-env.diff   # thread env= through run_hook (before_run base-ref fix)
│   ├── VP-2-exclude-labels-recheck.diff  # mid-turn blocked check — makes block terminal
│   └── mypy-strict-remediation.diff      # type annotation fixes in vendored source
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
│               ├── VENDORING.md # re-vendor checklist and patch record
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
└── docs/                        # design docs, spike findings, research
    ├── harness-design.md        # architecture, vendoring decision, daemon design (§10)
    ├── smoke-test-daemon.md     # first-run walkthrough, env vars, required labels
    ├── spike-findings.md
    └── ...
```

## Python development

### Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

### Setup

`bin/setup-env.sh` wraps these two steps (idempotent; pass `--help` for details):

```bash
# Create and populate the virtual environment
uv venv .venv
uv pip install -e ".[dev]"
```

### Running the quality gate

These three commands mirror the CI checks exactly — all must be clean before
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

Note: ruff and mypy exclude `src/baton_harness/vendor/` — the vendored symphony source is
not owned by this project and is not linted or strictly typed. Vendor patches are tracked
in `patches/` and `src/baton_harness/vendor/symphony/VENDORING.md`.

### Hook entry-point convention

The three lifecycle hooks and the daemon are installed as console scripts by `pyproject.toml`:

| Script | Entry point | Module |
|---|---|---|
| `bh-after-create` | `baton_harness.after_create:main` | `src/baton_harness/after_create.py` |
| `bh-before-run` | `baton_harness.before_run:main` | `src/baton_harness/before_run.py` |
| `bh-after-run` | `baton_harness.after_run:main` | `src/baton_harness/after_run.py` |
| `bh-daemon` | `baton_harness.chain.cli:main` | `src/baton_harness/chain/cli.py` |

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

- `claude` CLI on `PATH` and authenticated (subscription auth)
- `gh` CLI authenticated (`gh auth status`)
- `git` configured with user name and email
- `config/WORKFLOW.md` present in this repo (already committed — see `config/`)
- The target project repo cloned locally (`BH_PROJECT_ROOT`)
- The target project repo must have all five harness state labels (see
  [Required GitHub labels](#required-github-labels) below)

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

To create all five labels in the target repo:

```bash
gh label create "agent-ready"        -R <owner>/<repo> --color 0075ca
gh label create "agent-done"         -R <owner>/<repo> --color 0e8a16
gh label create "blocked"            -R <owner>/<repo> --color e4e669
gh label create "agent-in-progress"  -R <owner>/<repo> --color d93f0b
gh label create "agent-merged"       -R <owner>/<repo> --color 5319e7
```

The exact `gh label create` commands are also printed by `bin/run-daemon.sh` when it
detects a missing label.

## GitHub token: least-privilege setup

**Primary safeguard — use a dedicated bot/machine account.** Run the harness
under a GitHub account that holds no memberships, no organization roles, and no
permissions beyond what the table below requires. Structural least-privilege —
the account literally cannot perform destructive actions — is the real security
boundary. No software check substitutes for it.

**Defense-in-depth gate (issue #35).** The `validate_github_token()` check in
`src/baton_harness/_auth.py` runs at the top of the `before_run` hook and
rejects missing tokens, classic `ghp_` PATs, and tokens that fail a live
`gh api user` capability probe. This reduces the chance an over-scoped or
wrong-type token is used by accident. It is not a safety guarantee — it is a
fast-fail layer that sits in front of the real enforcement, which is the bot
account's permission configuration.

### Required fine-grained PAT permissions

Mint the token at <https://github.com/settings/personal-access-tokens/new>.
The harness requires a fine-grained PAT (prefix `github_pat_`) — classic PATs
(`ghp_`) are explicitly rejected because their account-wide scopes are
incompatible with per-repo least privilege.

| Operation | Fine-grained permission |
|---|---|
| Clone repo, push feature branches | Contents: Read & write |
| Read issue body, edit labels, post comments | Issues: Read & write |
| `gh pr list` / `gh pr create` | Pull requests: Read & write |
| Baseline (granted automatically) | Metadata: Read |

Additional settings:

- **Repository access:** Only select repositories → the pilot repo(s). Do not
  grant organization-wide access.
- **Expiry:** set the shortest expiry you can operationally manage; rotate on
  schedule.
- **Explicitly not granted:** Workflows, Administration, Secrets, any
  org-level scope.

Export the token as `GH_TOKEN` before running the harness (the gate also
accepts `GITHUB_TOKEN` as a fallback, consistent with standard CI conventions):

```bash
export GH_TOKEN=github_pat_<your-token>
bin/run.sh /path/to/project
```

### Known limitation — fine-grained PAT scope introspection

GitHub exposes a classic PAT's granted scopes via the `X-OAuth-Scopes` response
header. Fine-grained PATs provide no equivalent introspection endpoint as of
mid-2026. The gate therefore validates token *type* and *reachability* — it
cannot verify that the exact permission set above was granted. That verification
is the operator's responsibility at token-mint time.

### Known limitation — persistent transient GitHub API failures fail-closed

The capability self-test in the `before_run` auth gate calls `gh api user`
to confirm the token is reachable. If the GitHub API is experiencing a
sustained outage, rate-limit storm, or network-level failure (DNS, TLS, gateway
errors), the self-test will retry a small number of times
(`_auth._MAX_RETRIES`, currently 2) and then fail-closed — blocking the run
before any git work is attempted.

**This is intentional:** an agent run against an unreachable GitHub API would
produce useless or misleading output.

**Recovery:** wait for GitHub to recover (check
<https://www.githubstatus.com/>), then re-run the harness or restart the
daemon. The `TokenValidationError` message will indicate a
transient/network condition so it is distinguishable from a genuine bad-token
failure.

## Usage

### Running the daemon

`bh-daemon` is the always-on poll loop that watches a GitHub repo for `agent-ready`
issues, runs Claude Code agents against them in dependency order, CI-gates each agent's
PR, and opens a draft `feature/<slug> → main` PR when a work unit completes. It never
merges to `main`.

**Required environment variables:**

| Variable | Description |
|---|---|
| `BH_REPO_OWNER` | GitHub org or user login owning the target repo |
| `BH_REPO_NAME` | Target repository name (no owner prefix) |
| `BH_PROJECT_ROOT` | Absolute path to the local clone of the target repo |

**Quickstart (one tick, then exit):**

```bash
export BH_REPO_OWNER=<owner>
export BH_REPO_NAME=<repo>
export BH_PROJECT_ROOT=/path/to/local/clone

bin/run-daemon.sh --once
```

The `--once` flag runs exactly one poll-dispatch tick then exits — safe for a first run.

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

`bin/init-sandbox.sh` provisions a throwaway sandbox repo for a first smoke test — it
creates the required labels, a trivial trigger issue, a `hello-feature` DAG milestone, and
the stub CI workflow in one step (pass `--help` for the safety warning and required env
vars).

For the full first-run walkthrough — sandbox setup, trigger-issue creation, DAG dependency
wiring, CI-gate behaviour, and expected log output — see
[docs/smoke-test-daemon.md](docs/smoke-test-daemon.md).

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

- `docs/harness-design.md` — architecture, vendoring decision (§1), daemon design (§10), component descriptions
- `docs/smoke-test-daemon.md` — first-run walkthrough, env vars, required labels, CI-gate subtleties
- `docs/spike-findings.md` — empirical findings from the smoke-test spike that ground the design decisions
- `docs/architecture-spec.md` — overall system architecture
- `docs/pilot-validation-findings.md` — pilot dry-run findings
