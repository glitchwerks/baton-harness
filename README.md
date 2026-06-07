# baton-harness

A reusable policy and tooling layer for autonomous Claude Code agent runs. The harness
owns the lifecycle hook modules, per-project workflow config, context templates, and the
launcher. The orchestration engine (`symphony`, from [mraza007/baton](https://github.com/mraza007/baton))
is vendored into the package and called directly as a library.

**Current pilot state [implemented]:** the harness drives Baton as an external process
(`baton start -w <config>`). The vendored-library model is the decided target (#27) and
is not yet built — see [docs/harness-design.md §1](docs/harness-design.md) for the
decision and rationale.

## What this is

The harness owns everything *shareable* across projects: the Python hook modules that run
before and after each agent turn, per-project workflow config, a CLAUDE.md template, and
the `bin/run.sh` launcher. Each target project carries only its own committed `CLAUDE.md`
and CI workflow.

The hooks are shipped as a proper Python package (`baton_harness`) with console entry
points (`bh-after-create`, `bh-before-run`, `bh-after-run`) so they are on `PATH` after
`pip install` and can be wired directly into WORKFLOW.md hook lines without path
gymnastics.

## Integration model [implemented]

The harness runs project-local (from the project repo directory). Its config lives here
and is pointed at via `baton start -w <absolute-path>`:

```
cd <project-repo> && baton start -w /path/to/baton-harness/config/WORKFLOW.md
```

`bin/run.sh` encapsulates this invocation so it isn't retyped. It also exports
`BATON_HARNESS_DIR` so hook scripts can locate the harness root without hardcoding paths.

## Repo structure

```
baton-harness/
├── README.md
├── pyproject.toml               # package metadata, dev dependencies, ruff/mypy config
├── bin/
│   └── run.sh                   # launcher (shell): resolve harness root, baton start -w <config> [implemented; retireable post-vendoring]
├── src/
│   └── baton_harness/           # installable Python package (issue #10)
│       ├── __init__.py          # __version__
│       ├── _cli.py              # shared log/err helpers and issue-number resolver
│       ├── after_create.py      # bh-after-create hook entry point (implemented in #2)
│       ├── before_run.py        # bh-before-run hook entry point (implemented in #2)
│       └── after_run.py         # bh-after-run hook entry point (implemented in #3)
├── tests/                       # pytest suite
│   ├── test_smoke.py            # package import + version checks
│   └── test_cli.py              # unit tests for _cli helpers
├── config/
│   └── WORKFLOW.md              # generic Baton config + agent prompt
├── templates/
│   └── CLAUDE.md.template       # source for each project's committed CLAUDE.md (issue #5)
└── docs/                        # design docs, spike findings
    ├── harness-design.md
    └── spike-findings.md
```

## Python development

### Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

### Setup

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

### Hook entry-point convention

The three lifecycle hooks are installed as console scripts by `pyproject.toml`:

| Script | Entry point | Module |
|---|---|---|
| `bh-after-create` | `baton_harness.after_create:main` | `src/baton_harness/after_create.py` |
| `bh-before-run` | `baton_harness.before_run:main` | `src/baton_harness/before_run.py` |
| `bh-after-run` | `baton_harness.after_run:main` | `src/baton_harness/after_run.py` |

After `uv pip install -e ".[dev]"`, these commands are on `PATH` inside the
venv.  Issue #5 (WORKFLOW.md authoring) should wire them exactly as shown in
the WORKFLOW.md hook section:

```yaml
# Example WORKFLOW.md hook lines (issue #5)
hooks:
  after_create: bh-after-create
  before_run:   bh-before-run
  after_run:    bh-after-run
```

The hooks derive the issue number from `basename($PWD)`. The worktree
directory name must contain the issue number, but the prefix is optional:
both `feat-10-python-scaffold` and bare `10` (Baton's own `.symphony/worktrees/<N>`
convention) are valid (PR #20). The prefix-optional regex accepts any of:
`<prefix>-<issue>`, `<prefix>-<issue>-<slug>`, or bare `<issue>`.

## Prerequisites (runtime)

**Current pilot [implemented]:**
- [Baton](https://github.com/mraza007/baton) (`pip install baton`) installed and on `$PATH` — required for the external-process model
- `config/WORKFLOW.md` present in this repo (already committed — see `config/`)
- The target project repo checked out locally
- The target project repo must have all three harness state labels (see
  [Required GitHub labels](#required-github-labels) below)

**Post-vendoring [decided — not yet built]:** the separate `baton` pip install is no longer required — `symphony/` is included in the `baton_harness` package.

## Required GitHub labels

The target project repo **must** have all three harness state labels before
running `bin/run.sh`. The `after_run` hook uses `gh issue edit --add-label`
and `--remove-label` to reconcile these labels after each agent turn; if any
are missing, `gh` will error and reconciliation breaks — which in the worst
case causes an unbounded agent re-dispatch loop (pilot finding, issue #21).

`bin/run.sh` runs a preflight check for all three labels and exits non-zero
with actionable instructions if any are absent. It **does not auto-create
labels** — label creation is an operator action.

| Label | Purpose |
|---|---|
| `agent-ready` | Issue is eligible for an agent run |
| `agent-done` | Agent has opened a PR (human reviews) |
| `blocked` | Agent needs human input mid-run |

To create a missing label in the target repo:

```bash
gh -C /path/to/target-repo label create "agent-ready" --color "#0075ca"
gh -C /path/to/target-repo label create "agent-done"  --color "#0e8a16"
gh -C /path/to/target-repo label create "blocked"     --color "#e4e669"
```

Or follow the exact `gh label create` commands printed by `bin/run.sh` when it
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

## Usage

```
bin/run.sh <project-repo-path>
```

**Arguments:**

| Argument | Description |
|---|---|
| `project-repo-path` | Path to the target project repo (Baton runs inside it) |

**Example:**

```bash
bin/run.sh /home/chris/projects/my-api
```

**Help:**

```bash
bin/run.sh --help
```

**Exported environment variable:**

| Variable | Value |
|---|---|
| `BATON_HARNESS_DIR` | Absolute path to this harness repo root — available to all hook scripts |

## CLAUDE.md for the pilot project

Each target project must have a committed `CLAUDE.md` so Claude Code can
discover repo-local context. The harness owns the source template at
`templates/CLAUDE.md.template`; the live file is committed to the project repo
(it is irreducibly project-local — Claude Code discovers it from the worktree).

**For the pilot (one project):** copy the template manually, fill in the
`<!-- FILL: ... -->` markers, and commit the result as `CLAUDE.md` in the
project repo. A generate step is not warranted for a single project; add one
when project #2 appears.

```bash
cp /path/to/baton-harness/templates/CLAUDE.md.template /path/to/project/CLAUDE.md
# edit CLAUDE.md to fill in all markers, then:
git -C /path/to/project add CLAUDE.md && git -C /path/to/project commit -m "Add CLAUDE.md from harness template"
```

## Design documentation

See `docs/` for the full design:

- `docs/harness-design.md` — architecture, integration model, component descriptions, open questions
- `docs/spike-findings.md` — empirical findings from the smoke-test spike that ground the design decisions
