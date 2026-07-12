# bh-daemon: first-run smoke-test guide

This guide walks through the first live run of `bh-daemon`. The daemon has only ever been exercised by mocked unit tests; this is the procedure for its first real dispatch.

## What the daemon does

`bh-daemon` is an always-on poll loop that watches a GitHub repository for issues labelled `agent-ready`. It groups them into work units: a milestone becomes a single work unit whose issues are ordered by their `blocked_by` dependency edges into a DAG; an un-milestoned issue becomes its own N=1 unit. The daemon processes one work unit and one issue at a time (serial v1). For each issue it checks out a `feature/<slug>` branch, runs a Claude Code agent, waits for CI to pass on the agent's PR, then merges that PR into the feature branch with `--no-ff`. When all issues in a work unit are done it opens a single **draft** `feature/<slug> → main` PR. The daemon never merges to `main` — a human does that.

### State persistence

As of issue #106 (PR #166), `OrchestratorState` persists atomically to disk and reloads on startup. The state file lives at `$BH_PROJECT_ROOT/.symphony/state.json` (see §"Target-repo `.gitignore` requirement" below for why this path must be gitignored). Both `retry_queue` and running-issue state survive daemon restart or crash. A killed daemon that restarts picks up where it left off — it is **not** a clean slate. On a first smoke test this rarely matters; on subsequent runs after a forced kill, account for any state accumulated by the prior run.

---

## WARNING: safety first

**`bh-daemon` spawns real `claude -p --dangerously-skip-permissions` processes** that write code, commit, push branches, and open GitHub PRs autonomously. The `permission_mode: bypassPermissions` setting in `config/WORKFLOW.md` means every file-system and shell operation the agent attempts is permitted without confirmation.

Before running:

1. **Use a throwaway sandbox repo** — not a real project. Create a fresh, disposable GitHub repository with no production code in it.
2. **Always start with `--once`** — this runs exactly one poll-dispatch tick and exits, bounding the blast radius for a first run.
3. **Read through this guide completely** before invoking anything.

---

## Prerequisites

> **Fast path:** `bin/setup-env.sh` automates the venv creation, package install, and per-host config steps below (run it from the harness repo root; pass `--help` for details). When prompted, supply the absolute path to your local sandbox clone — the script writes it to `~/.config/baton-harness/host.env` (mode 600) so `bin/run-daemon.sh` picks it up automatically on every subsequent launch. Re-running the script when `host.env` already exists reports "delete it and re-run to reset" rather than re-prompting. Pass `BH_SETUP_NO_PROMPT=1` to skip the prompt in non-interactive contexts such as CI. `bin/init-sandbox.sh` automates the sandbox label creation, trigger-issue creation, `hello-feature` DAG, stub CI workflow, and `.bh/config.env` creation — run it after `bin/setup-env.sh` (pass `--help` for its safety warning and full option list). The manual steps below remain as the explainer and for partial or custom setups.

- `claude` CLI on `PATH` and authenticated (subscription auth — run `claude` once interactively to confirm). `bin/setup-env.sh` offers to auto-install via the official native installer when running interactively; auth is operator-supplied after install.
- `gh` CLI authenticated (`gh auth status`). `bin/setup-env.sh` offers to auto-install v2.62.0 (pinned, checksum-verified) when running interactively; `gh auth login` is operator-supplied after install.
- `git` configured with a user name and email.
- `bws` (Bitwarden Secrets CLI) on `PATH` — required for the App-auth bootstrap that mints the GitHub App installation token before the daemon starts. `bin/setup-env.sh` offers to auto-install v2.1.0 when running interactively. Install per the [Bitwarden Secrets Manager CLI docs](https://bitwarden.com/help/secrets-manager-cli/); verify with `bws --version`. Without it, the daemon fails immediately at startup with a subprocess error.
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

The daemon's environment is assembled from three sources in order, with explicit shell exports as an escape hatch for any layer. This section describes each source and what it supplies.

### Sandbox-committed constants — `.bh/config.env` in the sandbox repo

`${BH_PROJECT_ROOT}/.bh/config.env` is a plain `KEY=VAL` file committed in the **sandbox** repo (not the harness fork). This inverts the old model — per-deployment identity now lives alongside the code the daemon manages rather than in a file that had to be edited in the harness checkout on every new deploy.

`bin/run-daemon.sh` sources `host.env` for `BH_PROJECT_ROOT`, then reads `.bh/config.env` for the repo slug to run its label and `.symphony/`-gitignore preflights. `bh-daemon` itself then authoritatively parses and validates `.bh/config.env` via `sandbox_config.read_and_validate` before the registry loads.

`bin/init-sandbox.sh` writes this file interactively at provision time. To create it by hand, add the following to `${BH_PROJECT_ROOT}/.bh/config.env`:

```
BH_REPO_OWNER=<org-or-user>
BH_REPO_NAME=<repo>
BH_GITHUB_APP_ID=<numeric>
BH_GITHUB_APP_INSTALLATION_ID=<numeric>
BWS_PEM_SECRET_ID=<uuid>
BWS_GH_TOKEN_SECRET_ID=<uuid>       # optional
BWS_HEARTBEAT_PING_URL_SECRET_ID=<uuid>  # optional
```

| Variable | Purpose |
|---|---|
| `BH_REPO_OWNER` | GitHub org or user login for the target (managed) repo |
| `BH_REPO_NAME` | Repository name, without owner prefix |
| `BH_GITHUB_APP_ID` | Numeric GitHub App ID; also validated by `bin/provision-ruleset.sh` and read by `_resolve_app_id()` in `daemon.py` (fail-closed if absent) |
| `BH_GITHUB_APP_INSTALLATION_ID` | Numeric GitHub App installation ID; required by `bin/provision-ruleset.sh` |
| `BWS_PEM_SECRET_ID` | Bitwarden Secrets UUID of the RSA PEM private key for the GitHub App (required) |
| `BWS_GH_TOKEN_SECRET_ID` | Bitwarden Secrets UUID for a GitHub fine-grained PAT. When set and `GH_TOKEN` is absent, `bootstrap_secrets()` fetches the PAT at startup. Leave empty to supply `GH_TOKEN` directly (backward-compat). |
| `BWS_HEARTBEAT_PING_URL_SECRET_ID` | Bitwarden Secrets UUID for the Slack webhook URL. When set and `BH_HEARTBEAT_PING_URL` is absent, the URL is vault-fetched at startup. Leave empty to supply the URL directly or to omit it. |

`BWS_APP_ID` and `BWS_INSTALLATION_ID` are **derived** by the parser from `BH_GITHUB_APP_ID` and `BH_GITHUB_APP_INSTALLATION_ID` — do not set them. Missing or malformed values produce per-key errors with line numbers and cause an immediate exit.

> **Note — missing file is silently skipped by the daemon binary:** `bh-daemon` guards its `.bh/config.env` parse with an `os.path.isfile` check (`cli.py`); if the file is absent the daemon skips validation and starts with whatever is already in the environment. `bin/run-daemon.sh` is the component that hard-checks the file exists before launching — so operators who write a custom systemd `ExecStart=` that bypasses the launcher should ensure `.bh/config.env` is present, or the daemon will start without sandbox config validation.

### Per-host config — set by `bin/setup-env.sh`

`bin/setup-env.sh` prompts for `BH_PROJECT_ROOT` (the absolute path to the local clone of the managed repo) and writes it to `~/.config/baton-harness/host.env` (mode 600, directory mode 700). `bin/run-daemon.sh` sources this file at startup. The XDG base directory convention is honoured: the file path follows `${XDG_CONFIG_HOME:-${HOME}/.config}/baton-harness/host.env`.

| Variable | How it is set | Purpose |
|---|---|---|
| `BH_PROJECT_ROOT` | Written by `bin/setup-env.sh` to `host.env` | Absolute path to the local clone of the managed (sandbox) repo |

To reset the per-host config, delete `~/.config/baton-harness/host.env` and re-run `bin/setup-env.sh`. In non-interactive contexts (CI, cron), pass `BH_SETUP_NO_PROMPT=1` to skip the prompt entirely; the operator is then responsible for supplying `BH_PROJECT_ROOT` via another source.

### Operator-supplied secret — the one thing you set on each host

`BWS_ACCESS_TOKEN` is the Bitwarden machine-account access token. It is the only value that must reach the daemon from outside the repo. Provide it in a root-readable-only file (mode 600) and never commit it.

`bootstrap_secrets()` pops this token from `os.environ` as its first operation after vault-fetching any optional secrets (`GH_TOKEN`, `BH_HEARTBEAT_PING_URL`). After that point the token is gone from the process environment — it is never re-added.

**Vault-fetched at startup (no operator action required when the `BWS_*_SECRET_ID` is declared in `.bh/config.env`):**

- `GH_TOKEN` — the GitHub fine-grained PAT used by `gh` CLI calls. If `BWS_GH_TOKEN_SECRET_ID` is set in `.bh/config.env` and `GH_TOKEN` is not already in the environment, `bootstrap_secrets()` fetches it from the vault and writes it to `os.environ`. If `GH_TOKEN` is already set (shell export, CI env), the vault is not called — operator override wins.
- `BH_HEARTBEAT_PING_URL` — the Slack webhook URL for dead-man's-switch pings and per-launch preflight alerts (#144). Same skip logic: vault-fetch only when `BWS_HEARTBEAT_PING_URL_SECRET_ID` is declared and the URL is not already in the environment. If neither source supplies the URL, no alerts are sent — preflight refusals log to daemon stderr only.

Vault errors propagate as `BwsClientError` — fail-closed, never swallowed.

**Ordering invariant:** all vault fetches happen before `build_installation_token_provider()` is called. That function pops `BWS_ACCESS_TOKEN` from `os.environ` as its first operation, so any fetch attempted after it would receive an empty token and fail.

### Auto-derived (do not set)

`bin/run-daemon.sh` derives and exports these two variables automatically:

| Variable | How it is set | Purpose |
|---|---|---|
| `BATON_HARNESS_DIR` | Derived from the script's own location | Harness repo root; available to hook scripts |
| `BH_VENV` | Derived from the `bh-daemon` binary location | Hooks self-activate via `. "$BH_VENV/bin/activate"` |

### Operator override

Any variable can be exported in the shell before invoking `bin/run-daemon.sh`; explicit env values win over `.bh/config.env`, `host.env`, and vault-fetch, in that order. This is the escape hatch for one-off testing or CI environments where the standard sourcing chain is unavailable.

### Fresh host bringup — the four-step sequence

```bash
# 1. Run the setup script — creates the venv, installs the package,
#    and prompts for BH_PROJECT_ROOT (writes ~/.config/baton-harness/host.env).
bin/setup-env.sh

# 2. Provision the sandbox repo. bin/init-sandbox.sh reads BH_REPO_OWNER,
#    BH_REPO_NAME, and BH_PROJECT_ROOT from the environment — it does NOT
#    prompt for them. Export all three before running the script.
#    It then prompts interactively for the 5 App/vault identity values
#    (BH_GITHUB_APP_ID, BH_GITHUB_APP_INSTALLATION_ID, BWS_PEM_SECRET_ID,
#    BWS_GH_TOKEN_SECRET_ID, BWS_HEARTBEAT_PING_URL_SECRET_ID) and writes
#    them to ${BH_PROJECT_ROOT}/.bh/config.env.
export BH_REPO_OWNER=<owner>
export BH_REPO_NAME=<repo>
export BH_PROJECT_ROOT=<abs-path-to-local-sandbox-clone>
bin/init-sandbox.sh

# 3. Drop the single bootstrap secret in a root-readable-only file.
echo "BWS_ACCESS_TOKEN=<token>" | sudo tee /etc/bh-daemon/secrets.env
sudo chmod 600 /etc/bh-daemon/secrets.env

# 4. Run one poll tick to confirm everything starts cleanly.
BWS_ACCESS_TOKEN="$(sudo cat /etc/bh-daemon/secrets.env | grep BWS_ACCESS_TOKEN | cut -d= -f2-)" \
  bin/run-daemon.sh --once
```

---

## Ruleset provisioning (required before first run)

Before starting the daemon for the first time, provision the two branch-protection rulesets in the sandbox repo. The per-launch preflight gate (added in #144) calls `ruleset_is_provisioned()` before every worker dispatch and returns `RulesetStatus.ABSENT` on a fresh repo — causing every issue to be parked with "preflight refused — branch protection missing or misconfigured; worker not launched". The only way to create the rulesets is to run `bin/provision-ruleset.sh`.

**Prerequisites for this step:** the GitHub App must be installed on the sandbox repo, `BH_REPO_OWNER`, `BH_REPO_NAME`, `BH_GITHUB_APP_ID`, and `BH_GITHUB_APP_INSTALLATION_ID` must be present in `${BH_PROJECT_ROOT}/.bh/config.env` (or exported in the caller's shell), and `gh` must be authenticated as the harness App (or with a PAT that has `administration: write` on the repo).

```bash
bin/provision-ruleset.sh
```

The script is idempotent — safe to re-run if rulesets drift from the checked-in configs at `config/ruleset.main.json` and `config/ruleset.feature.json`. Exit codes: `0` = success (rulesets match or were corrected); `1` = drift could not be fixed; `2` = missing env vars or preflight App-ID mismatch.

The script provisions two rulesets:
- `harness-main-no-merge` — prevents direct merges to `main`, enforcing the draft-PR-only flow
- `harness-feature-daemon-only` — restricts pushes to `feature/*` branches to the harness App only

After provisioning, verify with:

```bash
gh api repos/<owner>/<repo>/rulesets --jq '.[].name'
```

You should see both ruleset names in the output.

---

## Required GitHub App permissions

The harness GitHub App must have the following permissions on the target repository. Configure these on the GitHub App settings page before installing the App on the sandbox repo. An operator can verify the live permission set with:

```bash
gh api /repos/<owner>/<repo>/installation --jq '.permissions'
```

| Permission | Level | Why required |
|---|---|---|
| `contents` | `write` | Push `feature/*` and `baton/*` branches; read repo files |
| `pull_requests` | `write` | Create draft PRs; post review comments |
| `issues` | `write` | Label transitions (`agent-ready` / `agent-in-progress` / `blocked` / `agent-done` / `agent-merged`); post escalation comments |
| `actions` or `checks` | `read` | Poll CI check-runs for the merge gate |
| `administration` | `read` | #144 preflight reads rulesets via `GET /repos/.../rulesets` (`ruleset_status.py:L356–361` returns `RulesetStatus.ERROR` on non-2xx, refusing every launch) |
| `administration` | `write` | `bin/provision-ruleset.sh` POSTs and PUTs rulesets |
| `metadata` | `read` | Always required by GitHub for any App installation |

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

With the environment variables set, labels created, and rulesets provisioned:

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

- Mount the OAuth credentials volume at `/home/agent/.claude/` (or wherever the container user's home is). Do not supply an API key. The G3c startup gate (`reconcile.py:L176–199`) checks that `~/.claude/.credentials.json` is present and readable before the daemon enters its poll loop — an absent or unreadable credential file causes an immediate exit 1 with "OAuth credential file absent or unreadable". Mounting the OAuth volume satisfies G3c.
- `gh` must have a valid GitHub fine-grained PAT available as `GH_TOKEN`. With `BWS_GH_TOKEN_SECRET_ID` declared in `.bh/config.env`, `bootstrap_secrets()` vault-fetches the PAT automatically at startup — the operator does not need to paste it into the systemd `EnvironmentFile=`. If you prefer to supply `GH_TOKEN` directly (for example in CI or during initial setup), export it in the shell or the `EnvironmentFile=` before invoking `bin/run-daemon.sh`; an explicit value always wins over the vault fetch.
- `git` must be configured with a user name and email in the daemon's environment.

Do not export `ANTHROPIC_API_KEY` — not in `.env` files, not in systemd `EnvironmentFile=`, not in the Docker entrypoint. Its presence at daemon startup is treated as a misconfiguration and causes an immediate hard abort.

### First run on the server

Follow the same `--once` safe-first-run approach described in the [Run it](#run-it) section above. Provision the sandbox and its labels first (see [Prerequisites](#prerequisites) and the `bin/init-sandbox.sh` automation). Run `bin/provision-ruleset.sh` to provision the branch-protection rulesets (see [Ruleset provisioning](#ruleset-provisioning-required-before-first-run)). Then:

```bash
# ${BH_PROJECT_ROOT}/.bh/config.env carries BH_REPO_OWNER, BH_REPO_NAME,
# BH_GITHUB_APP_*, BWS_PEM_SECRET_ID, and optionally BWS_GH_TOKEN_SECRET_ID.
# bin/setup-env.sh wrote BH_PROJECT_ROOT to ~/.config/baton-harness/host.env.
# Supply only the bootstrap secret:
export BWS_ACCESS_TOKEN=<bitwarden-machine-account-token>

bin/run-daemon.sh --once
```

This runs one poll-dispatch tick and exits, bounding blast radius.

### Process supervision (continuous mode)

For continuous operation, omit `--once`. The daemon polls indefinitely; stop it with SIGTERM (the handler in `src/baton_harness/chain/daemon.py` unlinks the `daemon.alive` marker and exits 0 cleanly) or Ctrl-C.

Two common supervision patterns are shown below. Both are illustrative starting points — adapt them to your environment.

#### systemd unit (recommended)

The recommended way to install the `bh-daemon` systemd unit is `bin/install-daemon-service.sh` (#208). It auto-detects `HARNESS_DIR` (the script's own repo root), `RUN_USER` (`${SUDO_USER:-$(whoami)}`), and `BH_PROJECT_ROOT` (via `~/.config/baton-harness/host.env` / `.bh/config.env`, same resolution `bin/run-daemon.sh` uses), prints a summary, and asks a single `[y/N]` confirm before writing `/etc/bh-daemon/secrets.env` (mode `600`) and `/etc/systemd/system/bh-daemon.service`, running `daemon-reload` + `enable --now`, and printing `systemctl status` plus the last 20 journal lines:

```bash
export BWS_ACCESS_TOKEN=<bitwarden-machine-account-token>   # or let it prompt silently
bin/install-daemon-service.sh
```

Useful flags:

| Flag | Effect |
|---|---|
| `--print-unit` | Render the unit + `secrets.env` to stdout; no privileged writes, no `systemctl` calls — dry-run preview |
| `--no-start` | Write the unit + `secrets.env` and run `daemon-reload` only; don't `enable`/start the service |
| `--harness-dir PATH` | Override the auto-detected harness repo root |
| `--project-root PATH` | Override the auto-detected `BH_PROJECT_ROOT` |
| `--user NAME` | Override the auto-detected systemd `User=` |
| `--help` / `-h` | Show usage |

Same safety behavior as the manual path below: the script refuses to run if `ANTHROPIC_API_KEY` is set in the calling environment, and backs up an existing unit or `secrets.env` (timestamped) before overwriting either. For non-interactive installs (CI, provisioning scripts), set `BH_SETUP_NO_PROMPT=1`; the script then fails closed with a clear error if `BH_PROJECT_ROOT` or `BWS_ACCESS_TOKEN` cannot be resolved without a prompt, rather than hanging on `read`.

After it finishes, the script reminds you to run `bin/provision-ruleset.sh` once against the target repo — it does **not** run provisioning itself, and without a captured `.bh/ruleset-baseline.json` the preflight gate (issue #206) parks every issue as `NOT_PROVISIONED`.

##### Manual / reference

`bin/install-daemon-service.sh` writes exactly the unit and secrets file shown below — use this if you want to see (or hand-edit) precisely what gets written, or are installing on a host where the script can't run.

`${BH_PROJECT_ROOT}/.bh/config.env` (read at daemon startup by `sandbox_config.read_and_validate`) supplies all other per-deployment constants (`BH_REPO_OWNER`, `BH_REPO_NAME`, `BH_GITHUB_APP_*`). Because the unit's `ExecStart=` invokes the `bh-daemon` binary directly (not the `bin/run-daemon.sh` wrapper that sources `~/.config/baton-harness/host.env`), the unit carries `BH_PROJECT_ROOT` as an explicit `Environment=` line. The `EnvironmentFile=` therefore needs only `BWS_ACCESS_TOKEN` — the single bootstrap secret. Keep that file root-readable only (`chmod 600`). Make sure `ANTHROPIC_API_KEY` never appears in either the unit or the `EnvironmentFile=`.

```ini
[Unit]
Description=baton-harness daemon
After=network.target

[Service]
Type=simple
User=agent
Environment=BH_PROJECT_ROOT=/path/to/sandbox/clone
EnvironmentFile=/etc/bh-daemon/secrets.env
ExecStart=/path/to/harness/.venv/bin/bh-daemon --workflow /path/to/harness/config/WORKFLOW.md
Restart=on-failure
RestartSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Where `/etc/bh-daemon/secrets.env` (mode `600`, owner `root`) contains:

```
BWS_ACCESS_TOKEN=<bitwarden-machine-account-token>
```

`GH_TOKEN` and `BH_HEARTBEAT_PING_URL` are vault-fetched at startup when their `BWS_*_SECRET_ID` values are declared in `.bh/config.env`. If you need to override either for a specific deploy (for example to use a different PAT in staging), add the override to `secrets.env` — an explicit env value wins over the vault fetch.

Key points:
- `Restart=on-failure` re-launches the daemon after an unexpected crash (SIGKILL, OOM). G2 at startup will detect and alert on the ungraceful exit.
- Do NOT add `KillSignal=SIGKILL` — the default `KillSignal=SIGTERM` lets the handler clear the `daemon.alive` marker before exit.
- Graceful shutdown: `systemctl stop bh-daemon` sends SIGTERM; the handler fires and exits 0.

Enable and start (this is what `bin/install-daemon-service.sh` does for you):

```bash
systemctl daemon-reload
systemctl enable --now bh-daemon
journalctl -u bh-daemon -f   # stream logs
```

#### tmux / nohup (lightweight alternative)

For a non-systemd environment or a quick deploy, only the bootstrap secret needs to be exported — all other required vars come from `${BH_PROJECT_ROOT}/.bh/config.env` and `~/.config/baton-harness/host.env`:

```bash
export BWS_ACCESS_TOKEN=<bitwarden-machine-account-token>

# In a persistent tmux session:
tmux new-session -d -s bh 'bin/run-daemon.sh >> /var/log/bh-daemon.log 2>&1'

# Or with nohup:
nohup bin/run-daemon.sh >> /var/log/bh-daemon.log 2>&1 &
```

If `bin/setup-env.sh` was not run on this host (and therefore `host.env` does not exist), add `BH_PROJECT_ROOT` as a shell export before the launcher call. All other per-deployment vars are read from `${BH_PROJECT_ROOT}/.bh/config.env` at daemon startup.

Send SIGTERM to stop cleanly: `kill -TERM <pid>`.

---

## #40 recovery-path verification (`bin/verify-recovery.sh`)

Issue #40 (merged PR #107) added a startup reconciliation sweep to the daemon. `bin/verify-recovery.sh` exercises each gate in that sweep against a live sandbox to confirm the recovery behavior is intact after a deploy or server reboot.

### What it verifies and why it matters

At every startup the daemon runs five checks (`src/baton_harness/chain/reconcile.py`):

| Gate | What it checks | Fatal? |
|---|---|---|
| G3a | GitHub token is a valid installation token | Yes — exits 1 |
| G3b | `ANTHROPIC_API_KEY` is NOT set | Yes — exits 1 |
| G3c | `~/.claude/.credentials.json` is present and readable | Yes — exits 1 |
| G2 | `daemon.alive` marker absent (no ungraceful prior exit) | No — critical alert, continues |
| G1 | No orphan `claude -p` processes from a prior crashed run | No — warn alert, continues |

The script also exercises graceful SIGTERM shutdown to confirm the `daemon.alive` marker is cleared cleanly, preventing a false-positive G2 alert on the next start.

G3c connects directly to the OAuth credential volume mount described in §"Credentials and auth on the server": mounting the volume at `~/.claude/` satisfies G3c; an absent mount causes exit 1 at startup before any poll occurs.

### Prerequisites

> **Safety: the sandbox must have zero `agent-ready` issues before running this script.** Scenarios G2, G1, and SIGTERM start the daemon in continuous mode (or `--once` with no early-exit gate). Any open `agent-ready` issue could be dispatched during those scenarios. The script checks for this condition at startup and aborts if any are found. Close or re-label all `agent-ready` issues in the sandbox before proceeding.

**Platform:** Linux only. The script uses `pgrep`, `kill -TERM`, and `/proc`-based POSIX semantics. It does not run on Windows or Git-Bash dev hosts — run it on the server where the daemon is deployed.

**Environment requirements:**

`BH_REPO_OWNER`, `BH_REPO_NAME`, and `BH_PROJECT_ROOT` must be available — either via `${BH_PROJECT_ROOT}/.bh/config.env` + `~/.config/baton-harness/host.env` (read automatically at startup), or exported in the caller's shell. Additionally:

- `GH_TOKEN` or `GITHUB_TOKEN` must be set (fine-grained PAT; structural check only). With `BWS_GH_TOKEN_SECRET_ID` declared in `.bh/config.env` and `BWS_ACCESS_TOKEN` in the environment, the token is vault-fetched automatically. Otherwise export it directly.
- `ANTHROPIC_API_KEY` must NOT be set (the script sets and unsets it temporarily for the G3b scenario; it aborts if the key is already present in the caller's shell).
- `bh-daemon` must be on `PATH`.

**`~/.claude/.credentials.json` must be present and readable.** If the OAuth credential file is absent when the script is invoked, the G3c preflight at the top of `verify-recovery.sh` (L255–277) prints `RESULT: SKIPPED` and exits 0 — all five scenarios are silently skipped rather than run. This is intentional: with G3c absent, every daemon-startup scenario would immediately exit 1 at the G3c gate, producing five misleading `[FAIL]` lines. On a CI system without the OAuth volume mounted, `RESULT: SKIPPED` is the expected output. To exercise the full scenario suite, ensure the credential file is present before running the script.

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
| G3a | Bogus `GH_TOKEN` | Script replaces `GH_TOKEN` with `ghp_BOGUS_TOKEN_FOR_TESTING`. Because no `BWS_ACCESS_TOKEN` is set in the test environment, `bootstrap_secrets()` fails first with an empty-token error, causing `validate_daemon_token` to fire via the credential-validation path. The daemon exits non-zero either way. | Non-zero (exit 1) | `Startup credential check failed` |
| G2 | Stale `daemon.alive` marker | Script pre-creates `.baton-harness/daemon.alive` before starting daemon `--once` | 0 (non-fatal) | `Prior daemon run ended ungracefully` |
| G1 | Orphan `claude -p` process | Script spawns `sleep 999` with argv containing `claude -p` so `pgrep -f` matches it | 0 (non-fatal) | `Orphan claude processes detected at startup` |
| SIGTERM | Graceful shutdown | Daemon starts in continuous mode; script waits for `daemon.alive` to appear, then sends SIGTERM | 0 (SystemExit(0) from handler) | Marker absent after exit |

Notes on specific scenarios:
- **G3b is the inverted gate**: the daemon refuses startup when `ANTHROPIC_API_KEY` IS set. This is the expected, correct behavior for OAuth/subscription deployment — the key's presence signals a misconfiguration.
- **G3a actual failure path**: the scenario supplies a `ghp_`-prefixed bogus token and no `BWS_*` vars. `bootstrap_secrets()` runs first and raises `BwsClientError("access_token is empty or None")` before any token format check. The failure propagates as "Startup credential check failed" either way. The important assertion is that the daemon exits non-zero; the exact failure point (BWS vs. token format) is an implementation detail of the test environment.
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

If the OAuth credential file is absent, you will see instead:

```
baton-harness: G3c preflight: OAuth creds absent at /home/agent/.claude/.credentials.json — skipping all daemon-startup scenarios
baton-harness: RESULT: SKIPPED
```

This is not a failure — it is the script correctly detecting that the G3c gate would cause every scenario to fail. Mount the OAuth credential volume and re-run to exercise the full suite.

### When to run it

- After deploying the daemon to a new server for the first time.
- After a server reboot, before restarting the daemon in continuous mode.
- When validating the #40 recovery behavior after a harness upgrade.
- As part of a post-deploy smoke test alongside the `--once` dispatch check.

---

## #239 block-escalation verification (`bin/verify-block-escalation.sh`)

Issue #239 exercises the WORKFLOW.md "Confidence / block rule" end to end: an agent that judges an issue's acceptance criteria genuinely ambiguous is expected to stop, post a clarifying question, and self-block rather than guess. `bin/verify-block-escalation.sh` confirms that when this happens, the daemon's park-and-escalate chain actually fires against a live sandbox.

### What it verifies and why it matters

Unlike `bin/verify-recovery.sh`, this script is **not** a decoy-only harness — it dispatches one real `claude -p` agent turn against one seeded issue. It seeds a genuinely un-defaultable `agent-ready` issue: cache capacity is fixed at exactly one entry with no overflow storage, yet LRU eviction is required when full, `set("a", 1)`, `get("a")`, and `set("b", 2)` must succeed with `get("b")` returning `2`, and any read entry must never be evicted. Once `"a"` has been read, no single reasonable implementation can satisfy all of these requirements simultaneously. It then runs a single `bh-daemon --once` poll tick, and asserts that the full chain described in the WORKFLOW.md confidence/block rule (`config/WORKFLOW.md` §"Confidence / block rule") completed:

1. the agent posts a clarifying question as an issue comment instead of guessing,
2. the agent adds the `blocked` label to signal it cannot proceed,
3. the daemon's post-turn label re-read (`src/baton_harness/chain/daemon.py`) sees `blocked` and takes the park path (`kind="block"`),
4. `escalation.escalate()` (`src/baton_harness/chain/escalation.py`) posts a durable GitHub comment and, when configured, attempts a best-effort Slack ping.

**Where to find the agent's actual question — read this before you go looking for it in Slack.** The clarifying question the agent wrote lands **only** on the GitHub issue comment thread. If `BH_SLACK_WEBHOOK_URL` is configured, the Slack message that fires is the *daemon's* own park summary — a fixed string like `"Issue #N parked: blocked label set."` — not the agent's question text. Slack tells you *that* an issue parked; the GitHub issue comment tells you *why*. The script's own assertions reflect this split: it asserts a GitHub comment exists (assertion 4) but only asserts that a Slack POST was *attempted* (assertion 5) — it cannot inspect delivered Slack content at all.

Two related signals are logged by the daemon but not locally assertable by this script: the runlog JSONL `escalation` event (written to `obs.runlog_path`), and the literal content actually delivered to Slack. Check those manually if you need to confirm delivery beyond "a POST was attempted."

### Prerequisites

> **Safety: the sandbox must have zero open `agent-ready` issues before running this script.** The script seeds exactly one ambiguous issue and runs a single `--once` poll tick; if other `agent-ready` issues already exist, that tick could dispatch the wrong one instead of (or in addition to) the seeded issue. The script checks this at startup and aborts if any are found.

**Platform:** the script has no `/proc` or `pgrep` dependency (more portable than `verify-recovery.sh`), but it **does** spawn a real agent turn that commits and comments — do not run it against a repo you are not prepared to have a real agent touch.

**Environment requirements:**

- `BH_REPO_OWNER`, `BH_REPO_NAME`, `BH_PROJECT_ROOT` — via `.bh/config.env` + `~/.config/baton-harness/host.env`, or exported directly.
- `bh-daemon` must be on `PATH`.
- `BH_PROJECT_ROOT` must be a git repository.
- `GH_TOKEN` or `GITHUB_TOKEN` must be set (fine-grained PAT; structural check only).
- `ANTHROPIC_API_KEY` must **not** be set (G3b — OAuth/subscription deployment).
- `~/.claude/.credentials.json` must be present and readable (G3c).
- The `agent-ready`, `agent-in-progress`, and `blocked` labels must exist in the target repo (the script does not check `agent-done` or `agent-merged`, since it never expects the seeded issue to complete normally).

**`~/.claude/.credentials.json` must be present and readable.** As with `verify-recovery.sh`, if the OAuth credential file is absent, the G3c preflight prints `RESULT: SKIPPED` and exits 0 before seeding any issue — this avoids a misleading `[FAIL]` on every assertion when the daemon would exit 1 at startup regardless of the block-escalation behavior under test.

**Optional:** set `BH_SLACK_WEBHOOK_URL` to also exercise the Slack-attempt assertion; leave it unset to skip that assertion cleanly. Set `BH_VERIFY_BLOCK_TIMEOUT_SECS` to override the default 600-second timeout on the daemon's single poll tick (real model latency for reasoning through the ambiguity, posting a comment, and adding the label can take several minutes).

Usage and options:

```bash
bin/verify-block-escalation.sh [--help|-h]
```

### Assertion table

The script runs one scenario (not a suite of scenarios like `verify-recovery.sh`) and reports one `[PASS]`/`[FAIL]`/`[SKIPPED]` line per assertion, all prefixed `BLOCK-`:

| Assertion | What it checks | Conditional? |
|---|---|---|
| `BLOCK-label-present` | The seeded issue carries the `blocked` label after the run | No |
| `BLOCK-in-progress-cleared` | The seeded issue does NOT carry `agent-in-progress` after the run | No |
| `BLOCK-escalation-logged` | Captured daemon stdout/stderr contains the specific SUCCESS message `escalate: GitHub comment posted on issue #<n> (kind=block)` | No |
| `BLOCK-comment-posted` | The issue has at least TWO comments post-run — the agent's clarifying question AND the daemon's escalation park-summary comment | No |
| `BLOCK-slack-attempted` | Daemon output contains a SINGLE line with `escalate: Slack ...`, `issue #<n>`, and `kind=block` | Yes — only runs if `BH_SLACK_WEBHOOK_URL` is set; otherwise reported `[SKIPPED]`, not `[FAIL]` |

Notes on specific assertions:
- **`BLOCK-escalation-logged` requires the SUCCESS log line specifically.** `escalation.escalate()` logs `"escalate: GitHub comment posted on issue #N (kind=block)"` at INFO only after the GitHub comment is posted. If only the WARNING failure-path text is present, the assertion fails instead of treating a real GitHub-comment-post failure as an acceptable outcome.
- **`BLOCK-comment-posted` requires >=2 comments, not >=1.** `escalation.escalate()` posts its own GitHub comment (the daemon's park summary, e.g. "Issue #N parked: blocked label set.") as the durable record. That comment alone would satisfy a bare `>=1` check even if the agent never posted its clarifying question, so the assertion requires both comments to be present.
- **`BLOCK-slack-attempted` cannot see delivered content**, only that the daemon logged a POST attempt (success or failure), matched with the same single-line, bounded-issue-number `grep -E` as `BLOCK-escalation-logged`. It intentionally does not — and cannot — assert on what the Slack message says.
- A `gh issue view` failure while re-fetching labels for the first two assertions is reported as its own failure (`BLOCK-labels-fetch`) rather than silently short-circuiting the rest of the assertions.

### Reading the output

A passing run looks like:

```text
baton-harness: --- Assertions: block escalation chain for #142 ---
baton-harness: [PASS] BLOCK-label-present
baton-harness: [PASS] BLOCK-in-progress-cleared
baton-harness: [PASS] BLOCK-escalation-logged
baton-harness: [PASS] BLOCK-comment-posted
baton-harness: [SKIPPED] BLOCK-slack-attempted — BH_SLACK_WEBHOOK_URL not set — Slack channel not exercised
baton-harness: ==============================
baton-harness: Block escalation verification summary
baton-harness: ==============================
baton-harness:   PASSED:  4
baton-harness:   FAILED:  0
baton-harness:   SKIPPED: 1
baton-harness: RESULT: PASS
```

With `BH_SLACK_WEBHOOK_URL` set, the fifth line becomes a `[PASS]`/`[FAIL]` instead of `[SKIPPED]`, and the summary's `PASSED`/`SKIPPED` counts shift accordingly.

If the OAuth credential file is absent, the entire scenario is skipped before any issue is seeded:

```text
baton-harness: G3c preflight: OAuth creds absent at /home/agent/.claude/.credentials.json — skipping the block-escalation scenario
baton-harness: RESULT: SKIPPED
```

This mirrors `verify-recovery.sh`'s G3c handling — it is not a failure, it is the script correctly detecting that the daemon would exit 1 before ever polling, which would otherwise produce five misleading `[FAIL]` lines instead of one clear `SKIPPED`.

The EXIT trap performs **best-effort** cleanup: it closes the seeded issue (with the same cleanup comment) and then removes the `agent-ready` / `agent-in-progress` / `blocked` / `agent-done` labels. It can act only if `_ISSUE_NUM` was successfully parsed from `gh issue create` output; after an `ORPHAN ISSUE WARNING`, it has no issue number and cannot clean up. Close happens *before* label removal (not after) so a label PATCH can't shift the issue's state out from under the close call. Each `gh` cleanup call is independently non-fatal: a transient failure is logged as a `warning:` line but does not stop the trap or script. After any orphan-issue warning or cleanup `warning:` line, manually verify the seeded issue in the sandbox repo and close or de-label it if the trap did not.

**On assertion failure, the daemon log is preserved, not deleted.** If any assertion fails — even when the daemon itself exited 0 — the summary dumps the last 40 lines of daemon output to stderr and copies the full captured output to a stable, announced path: `${BH_PROJECT_ROOT}/verify-block-escalation-daemon-<issue-number>.log`. Only on a clean run (zero failed assertions) does cleanup delete the temporary capture file.

### When to run it

- As part of pre-release smoke testing, alongside the positive-path dispatch check (#168) and `bin/verify-recovery.sh`'s startup-recovery gates.
- When validating the #239 self-block escalation chain after a change to `config/WORKFLOW.md`'s confidence/block rule, `src/baton_harness/chain/daemon.py`'s park path, or `src/baton_harness/chain/escalation.py`.
- Before enabling `BH_SLACK_WEBHOOK_URL` in a new deployment, to confirm the Slack-attempt path fires as expected.
