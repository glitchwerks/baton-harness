# bh-daemon: new-machine setup walkthrough

This is a start-to-finish walkthrough for bringing up `bh-daemon` on a machine that has
never run it before — a fresh VM, a new laptop, or a freshly provisioned server. It
answers "what do I run, in what order, and how do I know each step worked" for an
operator with nothing installed yet.

This doc does not replace [docs/smoke-test-daemon.md](smoke-test-daemon.md), which is the
authoritative runbook for the daemon's environment-variable resolution chain, the required
GitHub App permission table, DAG dependency wiring for a multi-issue smoke test, CI-gate
behavior, and systemd deployment. This doc gets you from a bare machine to a passing
preflight check and a first `--once` run; it links out to `smoke-test-daemon.md` for
anything beyond that instead of duplicating it.

**Read the safety warning before running anything below.** `bh-daemon` spawns real
`claude -p --dangerously-skip-permissions` processes that write code, commit, push
branches, and open GitHub PRs autonomously. Every step in this walkthrough targets a
throwaway sandbox repository — never a real project. See
[docs/smoke-test-daemon.md §"WARNING: safety first"](smoke-test-daemon.md#warning-safety-first).

## 1. Prerequisites — have these in hand before you start

Some of these can be created automatically by the scripts below (marked "auto"); the rest
you must obtain from GitHub or Bitwarden yourself before you begin, or a later step will
stall waiting for a value only you can supply.

**Software (auto-installable on Linux/macOS by `bin/setup-env.sh`, see step 2):**

- [`uv`](https://docs.astral.sh/uv/) — not auto-installed; install it yourself first
  (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- `gh` (GitHub CLI), authenticated (`gh auth login`)
- `bws` (Bitwarden Secrets CLI)
- `claude` (Claude Code CLI), authenticated via subscription/OAuth (run `claude` once
  interactively after install — the daemon uses OAuth, not an API key)
- `git`, configured with a user name and email

**Accounts and values you must obtain yourself (no script creates these):**

- A **throwaway sandbox GitHub repository**, created and cloned locally. The local clone
  path becomes `BH_PROJECT_ROOT`. Never point this walkthrough at a real project.
- A **GitHub App** created and installed on that sandbox repo, with the permissions listed
  in [docs/smoke-test-daemon.md §"Required GitHub App permissions"](smoke-test-daemon.md#required-github-app-permissions).
  From it you need two numbers: the **App ID** (from the App's settings page) and the
  **installation ID** (`gh api /repos/<owner>/<repo>/installation --jq .id`).
- The App's **RSA private key (PEM)**, uploaded to Bitwarden Secrets Manager as a secret —
  note that secret's UUID (`BWS_PEM_SECRET_ID`).
- Optionally, a **GitHub fine-grained PAT** (see
  [README §"GitHub token: least-privilege setup"](../README.md#github-token-least-privilege-setup)
  for the exact permission table) uploaded to Bitwarden Secrets Manager as a secret
  (`BWS_GH_TOKEN_SECRET_ID`). If you skip this, you must export `GH_TOKEN` directly instead.
- A **Bitwarden Secrets Manager machine-account access token** (`BWS_ACCESS_TOKEN`). This
  is the one secret you personally carry and export — it is never stored in any file this
  walkthrough writes to the repo or to `.bh/config.env`.
- **OS:** Linux or macOS with bash. (Some later-stage server scripts — `bin/verify-recovery.sh`,
  `bin/verify-block-escalation.sh` — are Linux-only, but everything in this walkthrough
  runs on macOS too.)

Do not proceed to step 2 without the GitHub App and its two IDs, and the PEM's Bitwarden
secret UUID in hand — step 3 (`bin/init-sandbox.sh`) prompts for them interactively and
has no way to look them up for you.

## 2. `bin/setup-env.sh` — Python environment and CLI checks

From the harness repo root:

```bash
bin/setup-env.sh
```

What it does, in order:

1. Checks `uv` is on `PATH` (fails with an install hint if not — this is the one tool it
   does not offer to auto-install)
2. Checks `bws`, `gh`, and `claude` are on `PATH`; in an interactive terminal on
   Linux/macOS it offers to auto-install each (pinned, checksum-verified versions for
   `bws`/`gh`; the official installer for `claude`) to `~/.local/bin`. In a non-interactive
   context (or with `BH_SETUP_NO_PROMPT=1`), it exits 1 with a link to the manual install
   page instead of silently reaching the network.
3. Creates `.venv` (skipped if already present — safe to re-run)
4. Installs the package with dev extras: `uv pip install -e ".[dev]"`
5. Verifies `bh-daemon` is reachable inside the venv
6. Prints the venv-activation hint
7. In an interactive terminal, prompts for `BH_PROJECT_ROOT` (the absolute path to your
   local sandbox clone) and writes it to `~/.config/baton-harness/host.env` (mode 600) —
   `bin/run-daemon.sh` sources this automatically on every later launch
8. Checks whether `BWS_ACCESS_TOKEN` is already set and prints a non-fatal notice if not
   (this script never needs it — only later steps do)

**Verify it worked:**

```bash
# bh-daemon is on PATH inside the venv
.venv/Scripts/bh-daemon --help   # Windows Git Bash
.venv/bin/bh-daemon --help       # macOS/Linux

# host.env was written (only if you answered the prompt)
cat ~/.config/baton-harness/host.env
```

If `gh`, `bws`, or `claude` were auto-installed to `~/.local/bin` and are not yet visible
to `command -v`, add `export PATH="$HOME/.local/bin:$PATH"` to your shell rc and re-run.
After installing `gh` or `claude`, authenticate them separately (`gh auth login`; run
`claude` once interactively) — `setup-env.sh` only installs the binaries, not credentials.

## 3. `bin/init-sandbox.sh` — provision the sandbox repo

This writes to a **live GitHub repository** — labels, issues, a pushed CI workflow file,
and a `.gitignore` entry. Point it only at the throwaway sandbox repo from step 1.

Export the three required variables and run it:

```bash
export BH_REPO_OWNER=<owner>
export BH_REPO_NAME=<sandbox-repo>
export BH_PROJECT_ROOT=<abs-path-to-local-sandbox-clone>   # already set if step 2 wrote host.env
bin/init-sandbox.sh
```

What it does, in order:

1. Preflight (`gh auth status`, `git`, `BH_PROJECT_ROOT` is a git repo)
2. Creates the five required harness labels (idempotent)
3. Creates a trivial trigger issue (`agent-ready`, no milestone)
4. Creates a `hello-feature` milestone with two DAG-ordered issues (B `blocked_by` A) plus
   a third issue exercising the body-marker dependency fallback
5. Writes a stub CI workflow (`.github/workflows/ci.yml`) and pushes it to the sandbox
   default branch (skipped if already identical)
6. **Prompts interactively** for the GitHub App ID, installation ID, and the three
   Bitwarden secret UUIDs from step 1, then writes `${BH_PROJECT_ROOT}/.bh/config.env`
7. Seeds `.symphony/` into the sandbox repo's `.gitignore` and pushes it (skipped if
   already present)

This script **hard-errors if run non-interactively** (`BH_SETUP_NO_PROMPT=1` or no
attached terminal) at the `.bh/config.env`-writing step — it has no way to collect the
five prompted values without a TTY. Run it interactively.

Issue and milestone creation are **not** idempotent — re-running against a repo that
already has them creates duplicates. Use a fresh sandbox repo (or clean it manually)
before repeating this step.

**Verify it worked:** the script prints a summary listing everything it created —
labels, the trigger issue URL, the milestone number, issues A/B/C, and confirmation that
`.bh/config.env` and the `.gitignore` entry were written. Spot-check the config file
(no secret values are printed by the script itself, since the UUIDs it writes are
Bitwarden secret *references*, not the secrets themselves):

```bash
cat "${BH_PROJECT_ROOT}/.bh/config.env"
```

You should see `BH_REPO_OWNER`, `BH_REPO_NAME`, `BH_GITHUB_APP_ID`,
`BH_GITHUB_APP_INSTALLATION_ID`, and the `BWS_*_SECRET_ID` UUIDs you supplied.

## 4. `BWS_ACCESS_TOKEN` — export it now, before provisioning rulesets

`BWS_ACCESS_TOKEN` is the single operator-supplied Bitwarden machine-account token. It is
needed starting with the **next** step (`bin/provision-ruleset.sh`), not just at daemon
launch time — that script mints a GitHub App JWT via
`python -m baton_harness.chain.app_auth jwt`, which vault-fetches the App's PEM using
`BWS_ACCESS_TOKEN` and `BWS_PEM_SECRET_ID` before it can call any GitHub API
(`src/baton_harness/chain/app_auth.py`, `main()`). Export it in this shell session now:

```bash
export BWS_ACCESS_TOKEN=<your-bitwarden-machine-account-token>
```

**Never print, log, or commit the actual token value.** This walkthrough only checks its
*presence* and *shape* (non-empty), never its content — the same discipline `bh-daemon
--doctor` follows (see step 6).

For a first interactive run, exporting it in the shell is sufficient — it stays in this
session's environment through steps 5–7. For a persistent/server deployment, drop it in a
root-readable-only file instead of a shell export; see
[docs/smoke-test-daemon.md §"systemd unit (recommended)"](smoke-test-daemon.md#systemd-unit-recommended)
for the canonical `/etc/bh-daemon/secrets.env` (mode `600`) pattern, or
`bin/install-daemon-service.sh` to write it for you.

## 5. `bin/provision-ruleset.sh` — branch-protection rulesets

Requires `BWS_ACCESS_TOKEN` exported (step 4) and `BH_GITHUB_APP_ID` /
`BH_GITHUB_APP_INSTALLATION_ID` available — either already in `.bh/config.env` (written
by step 3) or exported directly.

```bash
bin/provision-ruleset.sh
```

What it does:

1. Mints a GitHub App JWT (needs `BWS_ACCESS_TOKEN` + `BWS_PEM_SECRET_ID`, per step 4)
2. Cross-checks `BH_GITHUB_APP_ID` against a live `GET /app` call — aborts if it doesn't
   match (a common mistake here is pasting the *installation* ID where the *App* ID goes)
3. Mints an installation token and validates the repo reports at least one admin
   collaborator before proceeding
4. Idempotently creates or updates two rulesets against the checked-in configs at
   `config/ruleset.main.json` and `config/ruleset.feature.json`:
   - `harness-main-no-merge` — blocks direct merges to `main` (the daemon opens a PR;
     only a human merges it)
   - `harness-feature-daemon-only` — restricts pushes to `feature/*` branches to the
     harness App only
5. **Pins a ruleset baseline** at `${BH_PROJECT_ROOT}/.bh/ruleset-baseline.json` (ruleset
   ID + `updated_at` for each ruleset) — this is not optional bookkeeping: without it, the
   daemon's per-launch preflight parks every issue as `NOT_PROVISIONED`. The baseline
   capture only warns and skips (rather than failing the whole script) if
   `BH_PROJECT_ROOT` is unset, so make sure it is exported.

Exit codes: `0` success (rulesets match or were corrected), `1` drift could not be fixed,
`2` missing env vars or an App-ID mismatch.

**Verify it worked:**

```bash
gh api repos/<owner>/<repo>/rulesets --jq '.[].name'
```

You should see both `harness-main-no-merge` and `harness-feature-daemon-only`. Also
confirm the baseline file exists:

```bash
cat "${BH_PROJECT_ROOT}/.bh/ruleset-baseline.json"
```

## 6. `bh-daemon --doctor` / `--strict` — preflight before the first real run

`bh-daemon --doctor` runs the full preflight check catalog and exits without starting the
poll loop. **It reads only the ambient shell environment** — it does not source
`~/.config/baton-harness/host.env` (that sourcing only happens inside `bin/run-daemon.sh`).
So a bare `bh-daemon --doctor` invoked in a fresh shell will report failures for
`BH_PROJECT_ROOT` and `BWS_ACCESS_TOKEN` even on a correctly-provisioned host, unless you
export them first:

```bash
export BH_PROJECT_ROOT=<abs-path-to-local-sandbox-clone>   # if not already exported
export BWS_ACCESS_TOKEN=<your-bitwarden-machine-account-token>   # if not already exported

bh-daemon --doctor --strict
```

(`.venv/bin/bh-daemon` or `.venv/Scripts/bh-daemon` if the venv is not activated on
`PATH`.)

`--strict` makes the command exit non-zero if any **CRITICAL**-severity check ends in
`FAIL`; without it, `--doctor` always exits 0 regardless of findings, so scripting a
preflight gate around this command requires `--strict`. A `WARNING`-severity check never
trips `--strict`'s exit code, even on `FAIL`/`WARN`.

Each check prints one `[STATUS] Title` line; `FAIL` and `WARN` results additionally print
`detail:` and `fix:` lines. `PASS` and `SKIP` print only the header line. A fully-passing
run on a correctly-provisioned host looks like this:

```
[PASS] GitHub CLI available
[PASS] Bitwarden Secrets CLI available
[PASS] Claude CLI available
[PASS] uv package manager available
[PASS] Project root is valid
[PASS] Host environment file present
[PASS] Sandbox config file present
[PASS] Required sandbox config keys valid
[PASS] Optional secret IDs valid
[PASS] BWS access token present
[PASS] Symphony state is gitignored
[PASS] Anthropic API key is unset
[PASS] Force-PR-not-merge tripwire passes
[PASS] Git credential helper configured
```

A `FAIL` (here, `BWS_ACCESS_TOKEN` forgotten in a fresh shell) looks like this — the
detail line never reports the token's value, only that it is absent:

```
[FAIL] BWS access token present
       detail: BWS_ACCESS_TOKEN is unset or empty.
       fix:    Set BWS_ACCESS_TOKEN to a non-empty access token.
```

The 14-check catalog (`src/baton_harness/chain/doctor.py`) covers: the four CLIs (`gh`,
`bws`, `claude` — CRITICAL; `uv` — WARNING); `BH_PROJECT_ROOT` validity; presence of
`~/.config/baton-harness/host.env` (WARNING) and `${BH_PROJECT_ROOT}/.bh/config.env`
(CRITICAL); shape-validation of that config file's required keys and optional secret IDs;
presence of `BWS_ACCESS_TOKEN`; the exact `.symphony/` `.gitignore` entry; absence of
`ANTHROPIC_API_KEY`; the force-PR-not-merge startup self-test; and a configured git
credential helper. It never inspects or prints secret *values* — only presence, shape, and
byte length where a length is diagnostic (as in the `BWS_ACCESS_TOKEN` example above).

The CRITICAL, non-`daemon_native` subset of this same catalog runs automatically as a hard
gate at every real daemon startup, before any secret is bootstrapped (see step 7) —
`--doctor` lets you run the *full* catalog standalone, ahead of time, without also trying
to bootstrap secrets or start polling. Three checks (`CRED_ANTHROPIC_UNSET`,
`FORCE_PR_TRIPWIRE`, `GIT_CRED_HELPER`) are marked `daemon_native` in the catalog and are
covered by other native daemon startup code instead of the gate, so `--doctor` is the only
way to see their `[STATUS]` output ahead of a real run.

## 7. First daemon run — `bin/run-daemon.sh`

With the sandbox provisioned, rulesets in place, and doctor passing, run one bounded tick:

```bash
bin/run-daemon.sh --once
```

`bin/run-daemon.sh` derives the venv location from `bh-daemon`'s own path, sources
`~/.config/baton-harness/host.env` for `BH_PROJECT_ROOT`, then runs its own two preflights
before ever invoking `bh-daemon`:

1. **Label preflight** — confirms all five required labels exist in the target repo
   (created by step 3); aborts with the exact `gh label create` fix commands if not.
2. **`.symphony/`-gitignore preflight** — confirms the exact `.symphony/` line is present
   in the target repo's `.gitignore` (seeded by step 3); aborts with "this repo is not
   ready for harness work" if not.

It then `cd`s into `BH_PROJECT_ROOT` and execs `bh-daemon`, which runs the CRITICAL,
non-`daemon_native` subset of the step-6 doctor catalog as a hard gate
(`doctor.run_gate`, `PRE_BOOTSTRAP` phase) before bootstrapping any secret. A failing
check here prints `Preflight check <ID> failed: <detail> Fix: <fix>` to stderr and aborts
before any git or GitHub Actions work begins. `--once` runs exactly one poll-dispatch tick
then exits; this is the safe default for a first run. Omit `--once` for continuous polling
(stop with Ctrl-C).

For what a fully successful tick looks like in the logs, the CI-gate check-name
requirement that most often causes a first tick to park instead of merge, and how to seed
trigger issues, see
[docs/smoke-test-daemon.md §"Run it"](smoke-test-daemon.md#run-it) onward. For continuous
operation on a server (systemd unit, tmux/nohup), see
[docs/smoke-test-daemon.md §"Running on a Linux server"](smoke-test-daemon.md#running-on-a-linux-server).

## 8. Troubleshooting

Start with the failing step's own output — every script in this walkthrough prints an
`error:` (or `provision-ruleset:` / `baton-harness:`-prefixed) line with a specific fix
when a preflight fails, rather than a bare stack trace. If the failure is inside the
daemon's startup preflight rather than one of the `bin/*.sh` scripts, `bh-daemon --doctor`
(step 6) will name the exact check that fails, with a secret-safe `detail:` explaining
what it saw and a `fix:` explaining what to do about it — run it again after any fix to
confirm.

Common first-run stumbling points, in the order you are likely to hit them:

- **`init-sandbox.sh` hard-errors immediately at the config-write step.** You are running
  it non-interactively (no TTY, or `BH_SETUP_NO_PROMPT=1`). It has no way to collect the
  App ID, installation ID, and three Bitwarden UUIDs without prompting — run it in an
  interactive terminal.
- **`provision-ruleset.sh` fails at the JWT-minting step.** `BWS_ACCESS_TOKEN` is not
  exported in this shell (step 4), or `BWS_PEM_SECRET_ID` in `.bh/config.env` does not
  point at a real Bitwarden secret.
- **`provision-ruleset.sh` aborts with an App-ID mismatch.** You pasted the installation
  ID where the App ID goes (or vice versa) into `.bh/config.env` during step 3. Fetch both
  again per the step-1 commands and correct the file.
- **`bh-daemon --doctor` reports `BH_PROJECT_ROOT`/`BWS_ACCESS_TOKEN` failures on a host
  you believe is correctly set up.** `--doctor` does not source `host.env` — export both
  directly in the shell you're running `--doctor` from (see step 6).
- **`run-daemon.sh` aborts on the label or `.gitignore` preflight.** Re-run
  `bin/init-sandbox.sh` against the same sandbox, or fix the specific label /
  `.gitignore` line it names by hand.
- **A tick runs but the issue parks instead of merging.** This is almost always the
  CI-gate check-name requirement — see
  [docs/smoke-test-daemon.md §"CI-gate subtlety"](smoke-test-daemon.md#ci-gate-subtlety--required-check-names).

If none of the above matches, `docs/smoke-test-daemon.md` covers the full environment-
variable resolution chain, the required GitHub App permission table, and (for a server
deployment) the `#40` recovery-path and `#239` block-escalation verification scripts,
which exercise the daemon's startup gates and self-block behavior end-to-end against a
live sandbox.
