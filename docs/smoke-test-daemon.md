# bh-daemon: first-run smoke-test guide

This guide walks through the first live run of `bh-daemon`. The daemon has only ever been exercised by mocked unit tests; this is the procedure for its first real dispatch.

## What the daemon does

`bh-daemon` is an always-on poll loop that watches a GitHub repository for issues labelled `agent-ready`. It groups them into work units: a milestone becomes a single work unit whose issues are ordered by their `blocked_by` dependency edges into a DAG; an un-milestoned issue becomes its own N=1 unit. The daemon processes one work unit and one issue at a time (serial v1). For each issue it checks out a `feature/<slug>` branch, runs a Claude Code agent, waits for CI to pass on the agent's PR, then merges that PR into the feature branch with `--no-ff`. When all issues in a work unit are done it opens a single **draft** `feature/<slug> → main` PR. The daemon never merges to `main` — a human does that.

---

## WARNING: safety first

**`bh-daemon` spawns real `claude -p --dangerously-skip-permissions` processes** that write code, commit, push branches, and open GitHub PRs autonomously. The `permission_mode: bypassPermissions` setting in `config/WORKFLOW.md` means every file-system and shell operation the agent attempts is permitted without confirmation.

Before running:

1. **Use a throwaway sandbox repo** — not a real project. Create a fresh, disposable GitHub repository with no production code in it.
2. **Always start with `--once`** — this runs exactly one poll-dispatch tick and exits, bounding the blast radius for a first run.
3. **Read through this guide completely** before invoking anything.

---

## Prerequisites

> **Fast path:** `bin/setup-env.sh` automates the venv creation and package install steps below (run it from the harness repo root; pass `--help` for details). `bin/init-sandbox.sh` automates the sandbox label creation, trigger-issue creation, `hello-feature` DAG, and stub CI workflow — set `BH_REPO_OWNER`, `BH_REPO_NAME`, and `BH_PROJECT_ROOT` first, then run it (pass `--help` for its safety warning and full option list). The manual steps below remain as the explainer and for partial or custom setups.

- `claude` CLI on `PATH` and authenticated (subscription auth — run `claude` once interactively to confirm).
- `gh` CLI authenticated (`gh auth status`).
- `git` configured with a user name and email.
- The **sandbox repo cloned locally**. The local clone path becomes `BH_PROJECT_ROOT`.
- The harness package installed into a venv with `bh-daemon` on `PATH`:

```bash
# From the baton-harness repo root:
uv venv .venv
uv pip install -e .
# Activate so bh-daemon is on PATH for manual invocations:
source .venv/bin/activate   # Linux / macOS / Git Bash
# .venv\Scripts\activate     # Windows cmd / PowerShell
```

Confirm the entry point exists in `pyproject.toml` `[project.scripts]`:

```
bh-daemon = "baton_harness.chain.cli:main"
```

---

## Required labels

`bin/run-daemon.sh` runs a label preflight before starting the daemon and exits non-zero if any of the following five labels are absent from the target repo. Create them all in the sandbox repo before running:

```bash
gh label create "agent-ready"        -R <owner>/<repo> --color 0075ca
gh label create "agent-done"         -R <owner>/<repo> --color 0e8a16
gh label create "blocked"            -R <owner>/<repo> --color e4e669
gh label create "agent-in-progress"  -R <owner>/<repo> --color d93f0b
gh label create "agent-merged"       -R <owner>/<repo> --color 5319e7
```

Replace `<owner>/<repo>` with your sandbox repo slug (e.g. `alice/sandbox-baton`).

---

## Environment variables

Set these in your shell before running the launcher:

```bash
export BH_REPO_OWNER=<owner>          # GitHub org or user login
export BH_REPO_NAME=<repo>            # Repository name (no owner prefix)
export BH_PROJECT_ROOT=/path/to/local/clone   # Absolute path to local sandbox clone
```

`bin/run-daemon.sh` derives and exports two more automatically:

| Variable | How it is set | Purpose |
|---|---|---|
| `BATON_HARNESS_DIR` | Derived from the script's own location | Harness repo root; available to hook scripts |
| `BH_VENV` | Derived from the `bh-daemon` binary location | Hooks self-activate via `. "$BH_VENV/bin/activate"` |

One optional variable:

| Variable | Default | Purpose |
|---|---|---|
| `BH_SLACK_WEBHOOK_URL` | (unset) | If set, escalation notices are also POSTed to Slack. If unset, Slack is skipped silently and the GitHub issue comment is the only durable escalation record. |

Full sample export block for a sandbox:

```bash
export BH_REPO_OWNER=alice
export BH_REPO_NAME=sandbox-baton
export BH_PROJECT_ROOT=/home/alice/sandbox-baton
# Optional:
# export BH_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

---

## Create trigger issues in the sandbox

### (a) Trivial single issue — exercises the basic dispatch path

```bash
gh issue create \
  --repo <owner>/<repo> \
  --title "add a hello() function" \
  --body "Add a Python file with a hello() function that prints 'hello'." \
  --label agent-ready
```

This exercises: poll → select work unit → dispatch → agent run → PR creation → CI gate → merge or park → draft PR.

### (b) Optional: 2-issue milestone with a blocked_by edge — exercises DAG ordering

This exercises the topological scheduler. Issue A is a prerequisite; issue B is blocked by A.

**Step 1 — create the milestone:**

```bash
gh api repos/<owner>/<repo>/milestones \
  --method POST \
  -f title="hello-feature"
```

Note the `number` returned (e.g. `1`).

**Step 2 — create issues and assign them to the milestone:**

```bash
# Issue A (the prerequisite — no blocker)
gh issue create \
  --repo <owner>/<repo> \
  --title "add hello() function" \
  --body "Add hello.py with a hello() function." \
  --label agent-ready \
  --milestone 1

# Issue B (blocked by A)
gh issue create \
  --repo <owner>/<repo> \
  --title "add tests for hello()" \
  --body "Add pytest tests for the hello() function from the prior issue." \
  --label agent-ready \
  --milestone 1
```

Note the **issue numbers** returned (e.g. A = `#1`, B = `#2`).

**Step 3 — link the dependency (B is blocked_by A):**

The GitHub dependencies API uses each issue's **database ID** (not its number). Fetch the database IDs:

```bash
gh api repos/<owner>/<repo>/issues/1 --jq '.id'   # database ID of issue A
gh api repos/<owner>/<repo>/issues/2 --jq '.id'   # database ID of issue B
```

Then POST the dependency (B blocked_by A — use A's database ID in the body):

```bash
gh api repos/<owner>/<repo>/issues/2/dependencies/blocked_by \
  --method POST \
  -f issue_id=<database-id-of-issue-A>
```

A `201` response confirms the link. The daemon reads this via `GET repos/{owner}/{repo}/issues/{n}/dependencies/blocked_by` and builds the DAG; issue A will be dispatched before issue B.

---

## Run it

With the environment variables set and labels created:

```bash
bin/run-daemon.sh --once
```

The `--once` flag runs exactly one poll-dispatch tick then exits. This is the safe default for a first run.

To override the workflow config path:

```bash
bin/run-daemon.sh --once --workflow /path/to/config/WORKFLOW.md
```

The default config is `config/WORKFLOW.md` in the harness repo root. To poll continuously (normal operation) simply omit `--once`; stop with Ctrl-C.

### What success looks like in the logs

```
baton-harness: checking required labels in <owner>/<repo>...
baton-harness: all required labels present
baton-harness: harness=...
baton-harness: workflow=...
baton-harness: repo=<owner>/<repo> at <BH_PROJECT_ROOT>
baton-harness: starting bh-daemon...
INFO baton_harness.chain.daemon: bh-daemon: chdir to managed repo root: ...
INFO baton_harness.chain.daemon: poll tick: fetching agent-ready issues
INFO baton_harness.chain.daemon: selected work unit: ...
INFO baton_harness.chain.daemon: dispatching issue #N ...
INFO baton_harness.chain.daemon: agent run complete for issue #N
INFO baton_harness.chain.daemon: CI gate: polling check-runs for sha=...
INFO baton_harness.chain.daemon: merge outcome for issue #N: MERGED
INFO baton_harness.chain.daemon: work unit complete; opening draft PR feature/... → main
```

---

## CI-gate subtlety — required check names

**This is the most likely reason a smoke test parks rather than merges. Read this section before running.**

The CI gate's green predicate requires these three check names to be **present and passing** on the agent's PR head commit:

- `Lint (ruff)`
- `Test (pytest)`
- `Type check (mypy)`

These names are a module constant in `src/baton_harness/chain/merge.py` (`REQUIRED_CHECKS`). If a required check is absent from the check-runs response, `evaluate_ci` treats it as NOT-YET and keeps polling until the 30-minute hard timeout elapses, then returns `CiResult.TIMEOUT` → `MergeOutcome.CI_TIMEOUT`. There is no vacuous pass: zero matching checks is a timeout, not green.

**Practical consequence for a sandbox repo:**

| Sandbox CI setup | What happens |
|---|---|
| No CI workflow at all | Required checks never arrive → 30-minute wait → CI_TIMEOUT → issue parked |
| CI workflow but check names differ | Same as above — name match is exact |
| CI workflow with exactly those three check names | Full merge path exercised |

To smoke-test only the **dispatch → agent → PR-creation path** without waiting for the CI timeout, you can observe the agent's PR being opened on GitHub and stop the daemon (Ctrl-C) before the CI gate fires. The 30-minute timeout is the maximum wait; the daemon will log the CI_TIMEOUT and park the issue rather than block indefinitely.

To smoke-test the **full merge path**, add a GitHub Actions workflow to the sandbox repo that runs and names its jobs exactly `Lint (ruff)`, `Test (pytest)`, and `Type check (mypy)`. A workflow that simply exits 0 under those names is sufficient.

---

## Cleanup and teardown

- **`--once` mode**: the daemon exits on its own after one tick. No action needed.
- **Continuous mode**: send Ctrl-C. The daemon catches `KeyboardInterrupt` and exits cleanly.
- **Parked issues**: an issue that hit CI_TIMEOUT or a merge conflict carries the `blocked` label and an escalation comment on the GitHub issue. Inspect the daemon logs to see the exact outcome. Clear the `blocked` label and re-add `agent-ready` to re-queue an issue.
- **Branches created**: the agent creates `baton/<slug>-<N>` per-issue branches and a `feature/<slug>` integration branch. Delete them when done: `git -C $BH_PROJECT_ROOT branch -d <branch>` or via the GitHub UI.
