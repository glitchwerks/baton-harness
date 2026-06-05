# baton-harness

A reusable policy and tooling layer that drives [Baton](https://github.com/mraza007/baton)
for autonomous Claude Code agent runs. Baton is an upstream dependency — this repo
wraps it with project-specific config, lifecycle hook scripts, context templates, and a
launcher (decision D2: own repo, not a Baton fork).

## What this is

The harness owns everything *shareable* across projects: the Python hook modules that run
before and after each agent turn, per-project workflow config (passed to Baton via `-w`),
a CLAUDE.md template, and the `bin/run.sh` launcher. Each target project carries only its
own committed `CLAUDE.md` and CI workflow.

The hooks are shipped as a proper Python package (`baton_harness`) with console entry
points (`bh-after-create`, `bh-before-run`, `bh-after-run`) so they are on `PATH` after
`pip install` and can be wired directly into WORKFLOW.md hook lines without path
gymnastics.

## Integration model

Baton runs project-local (from the project repo directory) but its config lives here and
is pointed at via `baton start -w <absolute-path>`:

```
cd <project-repo> && baton start -w /path/to/baton-harness/config/<project>/WORKFLOW.md
```

`bin/run.sh` encapsulates this invocation so it isn't retyped. It also exports
`BATON_HARNESS_DIR` so hook scripts can locate the harness root without hardcoding paths.

## Repo structure

```
baton-harness/
├── README.md
├── pyproject.toml               # package metadata, dev dependencies, ruff/mypy config
├── bin/
│   └── run.sh                   # launcher (shell): resolve harness root, baton start -w <config>
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
│   └── <project-name>/
│       └── WORKFLOW.md          # per-project Baton config + agent prompt (issue #5)
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

The hooks derive the issue number from `basename($PWD)` — the worktree
directory name must follow the `<prefix>-<issue>[-<slug>]` convention
(e.g. `feat-10-python-scaffold`) for issue-number resolution to work.

## Prerequisites (runtime)

- [Baton](https://github.com/mraza007/baton) installed and on `$PATH`
- A project config directory at `config/<project-name>/WORKFLOW.md` in this repo
- The target project repo checked out locally

## Usage

```
bin/run.sh <project-name> <project-repo-path>
```

**Arguments:**

| Argument | Description |
|---|---|
| `project-name` | Name of the config dir under `config/` — expects `config/<project-name>/WORKFLOW.md` |
| `project-repo-path` | Path to the target project repo (Baton runs inside it) |

**Example:**

```bash
bin/run.sh my-api /home/chris/projects/my-api
```

**Help:**

```bash
bin/run.sh --help
```

**Exported environment variable:**

| Variable | Value |
|---|---|
| `BATON_HARNESS_DIR` | Absolute path to this harness repo root — available to all hook scripts |

## Design documentation

See `docs/` for the full design:

- `docs/harness-design.md` — architecture, integration model, component descriptions, open questions
- `docs/spike-findings.md` — empirical findings from the smoke-test spike that ground the design decisions
