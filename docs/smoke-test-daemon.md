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

## Target-repo `.gitignore` requirement

The daemon writes orchestrator state to `.symphony/state.json` inside the managed target repo. If `.symphony/` is not in that repo's `.gitignore`, `gh pr create` (which the daemon invokes from that working directory) emits a `Warning: 1 uncommitted change` on every PR creation, and the state file appears as untracked clutter in `git status`.

`bin/run-daemon.sh` enforces this requirement at startup: it performs a preflight check (alongside the required-labels preflight) and aborts with "this repo is not ready for harness work" and a non-zero exit if `.symphony/` is not gitignored. The daemon will not start until the entry is present.

Add this line to the target repo's `.gitignore` before running the daemon:

```
.symphony/
```

If you are using `bin/init-sandbox.sh` to provision a throwaway sandbox, this is seeded automatically. For any real repo you point the daemon at, add the entry yourself and commit it before the first daemon run — the launcher will refuse to start otherwise.

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

---

## Running on a Linux server

### Credentials and auth on the server

The deployment model mandates OAuth/subscription auth for Claude — `ANTHROPIC_API_KEY` **must not be set** in the daemon's environment (`architecture-spec.md` §2, §5). The startup reconciliation sweep (G3b, `src/baton_harness/chain/reconcile.py`) checks for this at every daemon start and exits non-zero with a critical alert if the key is present. This is the most important thing to get right on a server:

- Mount the OAuth credentials volume at `/home/agent/.claude/` (or wherever the container user's home is). Do not supply an API key.
- `gh` must be authenticated via `GH_TOKEN` set to a fine-grained PAT (prefix `github_pat_`). See `architecture-spec.md` §2 and the README's GitHub token setup section.
- `git` must be configured with a user name and email in the daemon's environment.

Do not export `ANTHROPIC_API_KEY` — not in `.env` files, not in systemd `EnvironmentFile=`, not in the Docker entrypoint. Its presence at daemon startup is treated as a misconfiguration and causes an immediate hard abort.

### First run on the server

Follow the same `--once` safe-first-run approach described in the [Run it](#run-it) section above. Provision the sandbox and its labels first (see [Prerequisites](#prerequisites) and the `bin/init-sandbox.sh` automation). Then:

```bash
export BH_REPO_OWNER=<owner>
export BH_REPO_NAME=<repo>
export BH_PROJECT_ROOT=/path/to/local/sandbox/clone

bin/run-daemon.sh --once
```

This runs one poll-dispatch tick and exits, bounding blast radius.

### Process supervision (continuous mode)

For continuous operation, omit `--once`. The daemon polls indefinitely; stop it with SIGTERM (the handler in `src/baton_harness/chain/daemon.py` unlinks the `daemon.alive` marker and exits 0 cleanly) or Ctrl-C.

Two common supervision patterns are shown below. Both are illustrative starting points — adapt them to your environment.

#### systemd unit (recommended)

Create `/etc/systemd/system/bh-daemon.service`. The environment variables can also be placed in a separate `EnvironmentFile=` — keep the file root-readable only and make sure `ANTHROPIC_API_KEY` never appears in it.

```ini
[Unit]
Description=baton-harness daemon
After=network.target

[Service]
Type=simple
User=agent
Environment=BH_REPO_OWNER=<owner>
Environment=BH_REPO_NAME=<repo>
Environment=BH_PROJECT_ROOT=/path/to/sandbox/clone
Environment=GH_TOKEN=<fine-grained-pat>
ExecStart=/path/to/harness/.venv/bin/bh-daemon --workflow /path/to/harness/config/WORKFLOW.md
Restart=on-failure
RestartSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Key points:
- `Restart=on-failure` re-launches the daemon after an unexpected crash (SIGKILL, OOM). G2 at startup will detect and alert on the ungraceful exit.
- Do NOT add `KillSignal=SIGKILL` — the default `KillSignal=SIGTERM` lets the handler clear the `daemon.alive` marker before exit.
- Graceful shutdown: `systemctl stop bh-daemon` sends SIGTERM; the handler fires and exits 0.

Enable and start:

```bash
systemctl daemon-reload
systemctl enable --now bh-daemon
journalctl -u bh-daemon -f   # stream logs
```

#### tmux / nohup (lightweight alternative)

For a non-systemd environment or a quick deploy:

```bash
export BH_REPO_OWNER=<owner>
export BH_REPO_NAME=<repo>
export BH_PROJECT_ROOT=/path/to/sandbox/clone
export GH_TOKEN=<fine-grained-pat>

# In a persistent tmux session:
tmux new-session -d -s bh 'bin/run-daemon.sh >> /var/log/bh-daemon.log 2>&1'

# Or with nohup:
nohup bin/run-daemon.sh >> /var/log/bh-daemon.log 2>&1 &
```

Send SIGTERM to stop cleanly: `kill -TERM <pid>`.

---

## #40 recovery-path verification (`bin/verify-recovery.sh`)

Issue #40 (merged PR #107) added a startup reconciliation sweep to the daemon. `bin/verify-recovery.sh` exercises each gate in that sweep against a live sandbox to confirm the recovery behavior is intact after a deploy or server reboot.

### What it verifies and why it matters

At every startup the daemon runs four checks (`src/baton_harness/chain/reconcile.py`):

| Gate | What it checks | Fatal? |
|---|---|---|
| G3a | GitHub token is a valid fine-grained PAT | Yes — exits 1 |
| G3b | `ANTHROPIC_API_KEY` is NOT set | Yes — exits 1 |
| G2 | `daemon.alive` marker absent (no ungraceful prior exit) | No — critical alert, continues |
| G1 | No orphan `claude -p` processes from a prior crashed run | No — warn alert, continues |

The script also exercises graceful SIGTERM shutdown to confirm the `daemon.alive` marker is cleared cleanly, preventing a false-positive G2 alert on the next start.

### Prerequisites

> **Safety: the sandbox must have zero `agent-ready` issues before running this script.** Scenarios G2, G1, and SIGTERM start the daemon in continuous mode (or `--once` with no early-exit gate). Any open `agent-ready` issue could be dispatched during those scenarios. The script checks for this condition at startup and aborts if any are found. Close or re-label all `agent-ready` issues in the sandbox before proceeding.

**Platform:** Linux only. The script uses `pgrep`, `kill -TERM`, and `/proc`-based POSIX semantics. It does not run on Windows or Git-Bash dev hosts — run it on the server where the daemon is deployed.

**Environment requirements:**

```bash
export BH_REPO_OWNER=<owner>
export BH_REPO_NAME=<repo>
export BH_PROJECT_ROOT=/path/to/local/sandbox/clone
# GH_TOKEN or GITHUB_TOKEN must be set (fine-grained PAT; structural check only)
# ANTHROPIC_API_KEY must NOT be set (the script sets and unsets it for G3b; aborts if already present)
# bh-daemon must be on PATH
```

**`ANTHROPIC_API_KEY` must not be set in the caller's shell.** The script needs to set it temporarily for the G3b scenario. If it is already set, the script aborts before running any scenario.

Usage and options:

```bash
bin/verify-recovery.sh [--help|-h]
```

### Scenario table

Each scenario listed in the order the script runs them. "Alert text" refers to the substring grepped from daemon stderr (via the `escalate()` WARNING log line — see script header for observability note).

| Scenario | Gate exercised | Setup | Expected exit code | Expected alert text in output |
|---|---|---|---|---|
| G3b | `ANTHROPIC_API_KEY` set | Script sets `ANTHROPIC_API_KEY=dummy-value-for-test` inline | Non-zero (exit 1) | `ANTHROPIC_API_KEY must not be set` |
| G3a | Bogus `GH_TOKEN` | Script replaces `GH_TOKEN` with `ghp_BOGUS_TOKEN_FOR_TESTING` (classic-PAT prefix — rejected offline by `_auth.py` before any network call) | Non-zero (exit 1) | `Startup credential check failed` |
| G2 | Stale `daemon.alive` marker | Script pre-creates `.baton-harness/daemon.alive` before starting daemon `--once` | 0 (non-fatal) | `Prior daemon run ended ungracefully` |
| G1 | Orphan `claude -p` process | Script spawns `sleep 999` with argv containing `claude -p` so `pgrep -f` matches it | 0 (non-fatal) | `Orphan claude processes detected at startup` |
| SIGTERM | Graceful shutdown | Daemon starts in continuous mode; script waits for `daemon.alive` to appear, then sends SIGTERM | 0 (SystemExit(0) from handler) | Marker absent after exit |

Notes on specific scenarios:
- **G3b is the inverted gate**: the daemon refuses startup when `ANTHROPIC_API_KEY` IS set. This is the expected, correct behavior for OAuth/subscription deployment — the key's presence signals a misconfiguration.
- **G3a token format**: the script uses a `ghp_BOGUS_TOKEN_FOR_TESTING` value (classic PAT prefix `ghp_`) because `_auth.py` rejects classic-PAT-prefixed tokens immediately without a network call. A fine-grained PAT has prefix `github_pat_`; anything else is rejected. The inline substitution is deterministic and fast.
- **G2 marker path**: `$BH_PROJECT_ROOT/.baton-harness/daemon.alive` — pre-created by the script, then re-written by the daemon on startup (non-fatal path). The marker is cleaned up by the script in an EXIT trap.
- **G1 decoy**: the "orphan" process is `sleep 999` with its argv set to `sleep 999 claude -p`. No real Claude binary is invoked. The script reaps it immediately after the scenario.
- **SIGTERM exit code**: Python's SIGTERM handler in `daemon.py` calls `raise SystemExit(0)`, so the daemon exits 0 — not 143 (which would indicate the process was killed externally without the handler firing).

### Reading the output

The script prints a per-scenario `[PASS]` or `[FAIL]` line as each scenario completes, then a summary:

```
baton-harness: [PASS] G3b
baton-harness: [PASS] G3a
baton-harness: [PASS] G2
baton-harness: [PASS] G1
baton-harness: [PASS] SIGTERM
baton-harness: ==============================
baton-harness: Recovery verification summary
baton-harness: ==============================
baton-harness:   PASSED: 5
baton-harness:   FAILED: 0
baton-harness: RESULT: PASS
```

A `[FAIL]` line includes the reason. `FAILED` scenarios are listed again in the summary. The script exits non-zero if any scenario fails.

### When to run it

- After deploying the daemon to a new server for the first time.
- After a server reboot, before restarting the daemon in continuous mode.
- When validating the #40 recovery behavior after a harness upgrade.
- As part of a post-deploy smoke test alongside the `--once` dispatch check.
