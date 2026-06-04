# baton-harness

A reusable policy and tooling layer that drives [Baton](https://github.com/mraza007/baton)
for autonomous Claude Code agent runs. Baton is an upstream dependency — this repo
wraps it with project-specific config, lifecycle hook scripts, context templates, and a
launcher (decision D2: own repo, not a Baton fork).

## What this is

The harness owns everything *shareable* across projects: the hook scripts that run before
and after each agent turn, per-project workflow config (passed to Baton via `-w`), a
CLAUDE.md template, and the `bin/run.sh` launcher. Each target project carries only its
own committed `CLAUDE.md` and CI workflow.

## Integration model

Baton runs project-local (from the project repo directory) but its config lives here and
is pointed at via `baton start -w <absolute-path>`:

```
cd <project-repo> && baton start -w /path/to/baton-harness/config/<project>/WORKFLOW.md
```

`bin/run.sh` encapsulates this invocation so it isn't retyped. It also exports
`BATON_HARNESS_DIR` so hook scripts can resolve `scripts/` without hardcoding paths.

## Repo structure

```
baton-harness/
├── README.md
├── bin/
│   └── run.sh                   # launcher: resolve harness root, cd into project, baton start -w <config>
├── scripts/                     # lifecycle hook scripts (populated by issues #2/#3)
│   ├── after-create.sh          # per-worktree dependency install
│   ├── before-run.sh            # branch sync onto main
│   └── after-run.sh             # outcome classification + label reconciliation
├── config/
│   └── <project-name>/
│       └── WORKFLOW.md          # per-project Baton config + agent prompt (issue #5)
├── templates/
│   └── CLAUDE.md.template       # source for each project's committed CLAUDE.md (issue #5)
└── docs/                        # design docs, spike findings
    ├── harness-design.md
    └── spike-findings.md
```

## Prerequisites

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
