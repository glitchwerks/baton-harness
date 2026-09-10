# CodeReeve: repository onboarding

Assumes [docs/system-setup.md](system-setup.md) is already complete on this machine.

This is the second of two setup walkthroughs for bringing up `codereeve daemon` on a machine that
has never run it before. This doc covers **repo/sandbox-level** setup: provisioning a
throwaway sandbox repository, selecting an App-key provider, provisioning
branch-protection rulesets, running the daemon's preflight check, and the first `--once`
run. See [docs/system-setup.md](system-setup.md) for how this doc relates to
[docs/smoke-test-daemon.md](smoke-test-daemon.md), the authoritative runbook this doc links
out to rather than duplicating. For what each credential below *is* and why it's required,
see [docs/authentication.md](authentication.md) — this doc covers the *how* (provisioning
steps and verification commands) only.

**Read the safety warning before running anything below.** `codereeve daemon` spawns real
`claude -p --dangerously-skip-permissions` processes that write code, commit, push
branches, and open GitHub PRs autonomously. Every step in this walkthrough targets a
throwaway sandbox repository — never a real project. See
[docs/smoke-test-daemon.md §"WARNING: safety first"](smoke-test-daemon.md#warning-safety-first).

## 1. Prerequisites — have these in hand before you start

This walkthrough uses canonical CodeReeve names. Legacy `BH_*`, `.bh/`, and
`bh-*` spellings remain accepted only through 0.3.x for migration and are removed in 0.4.

Some of these can be created automatically by the scripts below; the rest you must obtain
from GitHub and your selected key store before you begin, or a later step will stall
waiting for a value only you can supply. (For the machine-level CLI prerequisites — `uv`,
`gh`, conditional `bws`, `claude`, `git` — see
[docs/system-setup.md §1](system-setup.md).)

**Accounts and values you must obtain yourself (no script creates these):**

- A **throwaway sandbox GitHub repository**, created and cloned locally. The local clone
  path becomes `CODEREEVE_PROJECT_ROOT`. Never point this walkthrough at a real project.
- A **GitHub App** created and installed on that sandbox repo, with the permissions listed
  in [docs/authentication.md § GitHub App](authentication.md#github-app-primary).
  From it you need two numbers: the **App ID** (from the App's settings page) and the
  **installation ID** (`gh api /repos/<owner>/<repo>/installation --jq .id`).
- The App's **RSA private key (PEM)** and one of these explicit provider choices:
  - `bws`: upload the PEM to Bitwarden Secrets Manager and note its UUID
    (`BWS_PEM_SECRET_ID`).
  - `file`: externally provision the PEM at an absolute host path and note that path
    (`CODEREEVE_GITHUB_APP_PRIVATE_KEY_FILE`). The file must be regular, non-symlink,
    owner-only (`0400` or `0600`), readable UTF-8 PEM, non-empty, and at most 1 MiB.
- A **GitHub fine-grained PAT** for the standard `codereeve hook before-run` hook (see
  [docs/authentication.md § GitHub fine-grained PAT](authentication.md#github-fine-grained-pat-fallback)
  for the exact permission table — narrower than the App's). Optionally upload it to
  Bitwarden Secrets Manager as a secret (`BWS_GH_TOKEN_SECRET_ID`). If you skip this, supply
  `GH_TOKEN` directly instead.
- A **Bitwarden Secrets Manager machine-account access token** (`BWS_ACCESS_TOKEN`) only
  when you select the `bws` App-key provider or configure either optional BWS secret ID.
  This token is never stored in the repo or `.codereeve/config.env`. See
  [docs/authentication.md § Bitwarden Secrets Manager](authentication.md#bitwarden-secrets-manager)
  for what it's used to fetch.

Do not proceed to step 2 without the GitHub App and its two IDs, plus either the PEM's BWS
UUID or secured absolute file path. `bin/init-sandbox.sh` prompts for the selector and
only the selected source; it does not read, copy, or enroll PEM contents.

The two selector choices are explicit:

```bash
# Legacy BWS deployment migration: add this line and retain BWS_PEM_SECRET_ID.
CODEREEVE_GITHUB_APP_KEY_PROVIDER=bws

# New host-file deployment: use this line plus CODEREEVE_GITHUB_APP_PRIVATE_KEY_FILE.
CODEREEVE_GITHUB_APP_KEY_PROVIDER=file
```

Do not leave both `BWS_PEM_SECRET_ID` and `CODEREEVE_GITHUB_APP_PRIVATE_KEY_FILE` configured;
either conflict is rejected before environment mutation or GitHub access.

## 2. `bin/init-sandbox.sh` — provision the sandbox repo

This writes to a **live GitHub repository** — labels, issues, a pushed CI workflow file,
and a `.gitignore` entry. Point it only at the throwaway sandbox repo from step 1.

Export the three required variables and run it:

```bash
export CODEREEVE_REPO_OWNER=<owner>
export CODEREEVE_REPO_NAME=<sandbox-repo>
export CODEREEVE_PROJECT_ROOT=<abs-path-to-local-sandbox-clone>   # still required in this shell even if bin/setup-env.sh (docs/system-setup.md) already ran — that script only writes the value to host.env, which bin/init-sandbox.sh and bin/run-daemon.sh source internally but never export back into your shell
bin/init-sandbox.sh
```

What it does, in order:

1. Preflight (`gh auth status`, `git`, `CODEREEVE_PROJECT_ROOT` is a git repo)
2. Creates the six required harness labels, including the terminal
   `agent-failed` state (idempotent)
3. Creates a trivial trigger issue (`agent-ready`, no milestone)
4. Creates a `hello-feature` milestone with two DAG-ordered issues (B `blocked_by` A) plus
   a third issue exercising the body-marker dependency fallback
5. Writes a stub CI workflow (`.github/workflows/ci.yml`) and pushes it to the sandbox
   default branch (skipped if already identical)
6. **Prompts interactively** for the GitHub App ID, installation ID,
   `CODEREEVE_GITHUB_APP_KEY_PROVIDER` (`bws` or `file`), only that provider's source, and the two
   optional BWS secret UUIDs, then writes `${CODEREEVE_PROJECT_ROOT}/.codereeve/config.env`
7. Seeds `.symphony/` into the sandbox repo's `.gitignore` and pushes it (skipped if
   already present)

This script **hard-errors if run non-interactively** (`CODEREEVE_SETUP_NO_PROMPT=1` or no
attached terminal) at the `.codereeve/config.env`-writing step — it has no way to collect the
provider-specific values without a TTY. Run it interactively.

Issue and milestone creation are **not** idempotent — re-running against a repo that
already has them creates duplicates. Use a fresh sandbox repo (or clean it manually)
before repeating this step.

**Verify it worked:** the script prints a summary listing everything it created —
labels, the trigger issue URL, the milestone number, issues A/B/C, and confirmation that
`.codereeve/config.env` and the `.gitignore` entry were written. Spot-check the config file
(no secret values are printed by the script itself, since the UUIDs it writes are
Bitwarden secret *references*, not the secrets themselves):

```bash
cat "${CODEREEVE_PROJECT_ROOT}/.codereeve/config.env"
```

You should see `CODEREEVE_REPO_OWNER`, `CODEREEVE_REPO_NAME`, `CODEREEVE_GITHUB_APP_ID`,
`CODEREEVE_GITHUB_APP_INSTALLATION_ID`, `CODEREEVE_GITHUB_APP_KEY_PROVIDER`, exactly one of
`BWS_PEM_SECRET_ID` / `CODEREEVE_GITHUB_APP_PRIVATE_KEY_FILE`, and any optional
`BWS_*_SECRET_ID` UUIDs you supplied.

## 3. `BWS_ACCESS_TOKEN` — export only when BWS is configured

`BWS_ACCESS_TOKEN` is the operator-supplied Bitwarden machine-account token. Export it
when `CODEREEVE_GITHUB_APP_KEY_PROVIDER=bws`; `bin/provision-ruleset.sh` then uses it to load the
PEM before making any GitHub API call. Also export it for daemon startup when a file
provider configuration retains `BWS_GH_TOKEN_SECRET_ID` or
`BWS_HEARTBEAT_PING_URL_SECRET_ID`. A file provider with neither optional locator is
fully BWS-free and should leave the token unset. Use silent input so the token never lands
in shell history:

```bash
read -r -s BWS_ACCESS_TOKEN
export BWS_ACCESS_TOKEN
```

**Never print, log, or commit the actual token value.** This walkthrough only checks its
*presence* and *shape* (non-empty), never its content — the same discipline `codereeve daemon
--doctor` follows (see step 5).

For a first interactive BWS-backed run, the shell export is sufficient through steps
4–6. For a persistent/server deployment, use the conditional root-readable-only file
described in
[docs/smoke-test-daemon.md §"systemd unit (recommended)"](smoke-test-daemon.md#systemd-unit-recommended)
for `/etc/codereeve/secrets.env` (mode `600`), or use
`bin/install-daemon-service.sh`. The installer omits the file and `EnvironmentFile=` when
the resolved deployment is file-only.

## 4. `bin/provision-ruleset.sh` — branch-protection rulesets

Requires `CODEREEVE_GITHUB_APP_ID`, `CODEREEVE_GITHUB_APP_INSTALLATION_ID`, and a valid explicit key
provider — either already in `.codereeve/config.env` (written by step 2) or exported directly.
Provider `bws` also requires `BWS_ACCESS_TOKEN`; provider `file` loads the secured host
file without BWS.

```bash
bin/provision-ruleset.sh
```

What it does:

1. Loads the selected App key exactly once and proves it by minting a GitHub App JWT
   (`bws` needs `BWS_ACCESS_TOKEN` + `BWS_PEM_SECRET_ID`; `file` needs the absolute
   `CODEREEVE_GITHUB_APP_PRIVATE_KEY_FILE` path)
2. Cross-checks `CODEREEVE_GITHUB_APP_ID` against a live `GET /app` call — aborts if it doesn't
   match (a common mistake here is pasting the *installation* ID where the *App* ID goes)
3. Mints an installation token and validates the repo reports at least one admin
   collaborator before proceeding
4. Idempotently creates or updates two rulesets against the checked-in configs at
   `config/ruleset.main.json` and `config/ruleset.feature.json`:
   - `harness-main-no-merge` — blocks direct merges to `main` (the daemon opens a PR;
     only a human merges it)
   - `harness-feature-daemon-only` — restricts pushes to `feature/*` branches to the
     harness App only
5. **Pins a ruleset baseline** at `${CODEREEVE_PROJECT_ROOT}/.codereeve/ruleset-baseline.json` (ruleset
   ID + `updated_at` for each ruleset) — this is not optional bookkeeping: without it, the
   daemon's per-launch preflight parks every issue as `NOT_PROVISIONED`. The baseline
   capture only warns and skips (rather than failing the whole script) if
   `CODEREEVE_PROJECT_ROOT` is unset, so make sure it is exported.

Exit codes: `0` success (rulesets match or were corrected), `1` drift could not be fixed,
`2` missing env vars or an App-ID mismatch.

**Verify it worked:**

```bash
gh api repos/<owner>/<repo>/rulesets --jq '.[].name'
```

You should see both `harness-main-no-merge` and `harness-feature-daemon-only`. Also
confirm the baseline file exists:

```bash
cat "${CODEREEVE_PROJECT_ROOT}/.codereeve/ruleset-baseline.json"
```

## 5. `codereeve doctor --strict` — preflight before the first real run

`codereeve doctor` runs selected checks without starting the poll loop or exporting
resolved config into the process environment. It does not source
`~/.config/codereeve/host.env`. Supply `--config PATH` to select a local file,
or export `CODEREEVE_PROJECT_ROOT` to select `$CODEREEVE_PROJECT_ROOT/.codereeve/config.env`.
An explicit path does not need that environment variable merely to locate the
file; configuration checks still require a valid project root. Non-empty supported
environment overrides take precedence over file values. Missing or malformed
configuration is reported as a failed check.

```bash
codereeve --version
codereeve provenance
codereeve doctor --phase installation --format json --strict
codereeve doctor --phase configuration --config /path/to/config.env --strict
codereeve doctor --phase live --strict
```

Use the installed environment's `codereeve` executable when it is not on PATH.
`codereeve --version` reports installed distribution metadata. `codereeve provenance` emits the
validated packaged record, including `schema_version: 1`, `package_version`,
the exact `source_revision`, `lock_identity` (SHA-256 of the exact lock bytes),
and boolean `development`. Neither command needs config or credentials, and
runtime provenance never reads Git. Invalid provenance makes `codereeve provenance`
exit 1 with a safe stderr diagnostic.

| Phase | Checks and authority |
|---|---|
| `installation` | Offline, credential-free package provenance, imports, resources, entry points, workflow and force-PR tripwire |
| `configuration` | Local config, project root, provider shape, CLI presence and filesystem metadata; no credential use or network calls |
| `live` | Credential and remote checks, including App-key usability, GitHub auth/repository/rulesets/labels/admin, helper and OAuth usability |

Repeat `--phase` to choose multiple phases; duplicates are removed. Omission
selects installation, configuration, then live. Results follow selected-phase
order and catalog order within each phase. `--format text` is the default;
automation should request `--format json` and inspect schema version 1.

JSON stdout contains exactly one document with these fields:

| Field | Value |
|---|---|
| `schema_version` | Integer `1` |
| `provenance` | Object with `package_version`, `source_revision`, `lock_identity`, `development`; `null` if validation failed |
| `selected_phases` | Array of selected phase strings |
| `summary` | Integer counts `pass`, `fail`, `warn`, `skip`, `critical_failures` |
| `checks` | Ordered objects with `id`, `phase`, `status`, `severity`, `title`, `detail`, `remediation` |

Check IDs are stable. Status values are lowercase `pass`, `fail`, `warn`,
`skip`; severity values are `critical` or `warning`. Selecting installation
reports invalid provenance as a critical failed `PKG_PROVENANCE` check. Other
phase selections do not implicitly run installation to populate provenance.

Standalone doctor exits 0 despite findings unless `--strict` finds at least
one critical failed check, in which case it exits 1. Warning-severity failures
and warning/skip statuses do not trip strict. Usage errors exit 2. If rendering
or redaction cannot safely complete, the command exits 1 even in advisory mode,
prints a fixed diagnostic to stderr, and emits no partial report to stdout.
Human-readable report fields are redacted in both formats; logs and diagnostics
use stderr.

In file-only mode, `CLI_BWS` (configuration) and `ENV_BWS_ACCESS_TOKEN` (live)
pass as not required. With either optional BWS locator configured, both BWS
prerequisites become active. Live checks need the corresponding credential
authority; use `--phase installation` for an offline package-only assessment.
Standalone live checks do not perform the daemon's secret bootstrap.

## 5a. `codereeve daemon --check-vault` — compatibility App-key dry-run

`--check-vault` retains its legacy option name but is provider-aware. It runs one check
from the live catalog: load the selected App key (`bws` fetch or secured file read) and
prove it can sign an App JWT, without making a GitHub request. On `PASS`, only the status
and title are printed; no provider, path, UUID, content, token, or byte count is reported.

```bash
export CODEREEVE_PROJECT_ROOT=<abs-path-to-local-sandbox-clone>   # if not already exported
# Only for provider bws:
# export BWS_ACCESS_TOKEN=<your-bitwarden-machine-account-token>

codereeve daemon --check-vault
```

```text
[PASS] App private key is usable
```

or, on failure:

```text
[FAIL] App private key is usable
       detail: file provider App private key is unusable.
       fix:    Verify the selected App private-key source and its credentials.
```

Exit code is `0` on `PASS`, `1` otherwise. `--check-vault` and `--doctor` are mutually
exclusive in effect: if both are passed, only the vault check runs (and `--strict` is
ignored) — pass `--check-vault` alone.

The same App-key check participates in the live catalog and daemon live gate.
The compatibility command selects only this check, even if `--phase` is also
supplied. It accepts `--config PATH` and `--format json`. Use it to isolate an
unreachable BWS secret or an unavailable, unsafe, or unusable host key file.

## 6. First daemon run — `bin/run-daemon.sh`

With the sandbox provisioned, rulesets in place, and doctor passing, run one bounded tick:

```bash
bin/run-daemon.sh --once
```

`bin/run-daemon.sh` derives the environment from `codereeve`'s path and reads
`~/.config/codereeve/host.env` through the shared literal-assignment parser before its preflights and `codereeve daemon`.

1. **Label preflight** — confirms all six required labels exist in the target repo
   (created by step 2); aborts with the exact `gh label create` fix commands if not.
2. **`.symphony/`-gitignore preflight** — confirms the exact `.symphony/` line is present
   in the target repo's `.gitignore` (seeded by step 2); aborts with "this repo is not
   ready for harness work" if not.

It then changes to `CODEREEVE_PROJECT_ROOT` and execs `codereeve daemon`. The daemon resolves
one config snapshot and runs the `installation` and `configuration` gates
before applying config or bootstrapping secrets. It collects and renders every
selected critical failure, then exits 1 if any failed. No failed gate proceeds
to the event loop. Missing config is a critical configuration failure even when
launching the daemon binary directly.

After bootstrap and installation-token validation, the same context supplies the
`live` gate before entering `run_daemon`. This gate includes the credential and
remote checks in step 5. The reconciliation sweep retains its native credential,
helper, alert and recovery checks but does not repeat the doctor gate.
`--once` runs one poll-dispatch tick after successful startup; omit it for
continuous polling (stop with Ctrl-C).

When the resolved configuration needs BWS, `bin/setup-env.sh` prints a non-fatal
setup-time notice if the access token is absent. Bootstrap requires that token;
`ENV_BWS_ACCESS_TOKEN` belongs to the live phase. For file-only configuration it
passes as not required. Bootstrap removes the token from ambient `os.environ`
on success or failure; the shared context retains the captured authority for
the subsequent live check.

For what a fully successful tick looks like in the logs, the CI-gate check-name
requirement that most often causes a first tick to park instead of merge, and how to seed
trigger issues, see
[docs/smoke-test-daemon.md §"Run it"](smoke-test-daemon.md#run-it) onward. For continuous
operation on a server (systemd unit, tmux/nohup), see
[docs/smoke-test-daemon.md §"Running on a Linux server"](smoke-test-daemon.md#running-on-a-linux-server).

## 7. Troubleshooting

Start with the failing step's own output — every script in this walkthrough prints an
`error:` (or `provision-ruleset:` / `codereeve:`-prefixed) line with a specific fix
when a preflight fails, rather than a bare stack trace. If the failure is inside the
daemon's startup preflight rather than one of the `bin/*.sh` scripts, `codereeve doctor`
(step 5) will name the exact check that fails, with a secret-safe `detail:` explaining
what it saw and a `fix:` explaining what to do about it — run it again after any fix to
confirm.

Common first-run stumbling points, in the order you are likely to hit them:

- **`init-sandbox.sh` hard-errors immediately at the config-write step.** You are running
  it non-interactively (no TTY, or `CODEREEVE_SETUP_NO_PROMPT=1`). It has no way to collect the
  App ID, installation ID, provider/source, and optional BWS UUIDs without prompting —
  run it in an interactive terminal.
- **`provision-ruleset.sh` fails at the JWT-minting step.** For `bws`,
  `BWS_ACCESS_TOKEN` is absent or `BWS_PEM_SECRET_ID` is unreadable. For `file`, check the
  absolute `CODEREEVE_GITHUB_APP_PRIVATE_KEY_FILE` path and its owner-only file contract.
- **`provision-ruleset.sh` aborts with an App-ID mismatch.** You pasted the installation
  ID where the App ID goes (or vice versa) into `.codereeve/config.env` during step 2. Fetch both
  again per the step-1 commands and correct the file.
- **`codereeve doctor` reports `CODEREEVE_PROJECT_ROOT` or a BWS prerequisite failure on a host
  you believe is correctly set up.** `--doctor` does not source `host.env` — supply `--config PATH` or export
  `CODEREEVE_PROJECT_ROOT` in the shell running doctor. Export `BWS_ACCESS_TOKEN` only
  when the resolved provider/optional-secret composition needs it (see step 5).
- **`run-daemon.sh` aborts on the label or `.gitignore` preflight.** Do not re-run
  `bin/init-sandbox.sh` to fix this — issue and milestone creation are not idempotent
  (step 2) and re-running it against an already-provisioned sandbox creates duplicates.
  Apply a targeted fix instead: for a missing label, the label preflight itself prints
  the exact `gh label create` command to run (step 2 lists the six required labels); for
  the `.gitignore` preflight, add the `.symphony/` line to the sandbox repo's `.gitignore`
  and push it by hand (step 2, item 7). If the sandbox is broken beyond these targeted
  fixes, provision a fresh sandbox repo instead.
- **A tick runs but the issue parks instead of merging.** This is almost always the
  CI-gate check-name requirement — see
  [docs/smoke-test-daemon.md §"CI-gate subtlety"](smoke-test-daemon.md#ci-gate-subtlety--required-check-names).

If none of the above matches, `docs/smoke-test-daemon.md` covers the full environment-
variable resolution chain and (for a server deployment) the `#40` recovery-path and
`#239` block-escalation verification scripts — see [docs/authentication.md](authentication.md)
for the GitHub App permission table — which exercise the daemon's startup gates and
self-block behavior end-to-end against a
live sandbox.
