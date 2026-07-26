# bh-daemon: repository onboarding

Assumes [docs/system-setup.md](system-setup.md) is already complete on this machine.

This is the second of two setup walkthroughs for bringing up `bh-daemon` on a machine that
has never run it before. This doc covers **repo/sandbox-level** setup: provisioning a
throwaway sandbox repository, exporting the Bitwarden access token, provisioning
branch-protection rulesets, running the daemon's preflight check, and the first `--once`
run. See [docs/system-setup.md](system-setup.md) for how this doc relates to
[docs/smoke-test-daemon.md](smoke-test-daemon.md), the authoritative runbook this doc links
out to rather than duplicating.

**Read the safety warning before running anything below.** `bh-daemon` spawns real
`claude -p --dangerously-skip-permissions` processes that write code, commit, push
branches, and open GitHub PRs autonomously. Every step in this walkthrough targets a
throwaway sandbox repository — never a real project. See
[docs/smoke-test-daemon.md §"WARNING: safety first"](smoke-test-daemon.md#warning-safety-first).

## 1. Prerequisites — have these in hand before you start

Some of these can be created automatically by the scripts below; the rest you must obtain
from GitHub or Bitwarden yourself before you begin, or a later step will stall waiting for
a value only you can supply. (For the machine-level CLI prerequisites — `uv`, `gh`, `bws`,
`claude`, `git` — see [docs/system-setup.md §1](system-setup.md).)

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

Do not proceed to step 2 without the GitHub App and its two IDs, and the PEM's Bitwarden
secret UUID in hand — step 2 (`bin/init-sandbox.sh`) prompts for them interactively and
has no way to look them up for you.

## 2. `bin/init-sandbox.sh` — provision the sandbox repo

This writes to a **live GitHub repository** — labels, issues, a pushed CI workflow file,
and a `.gitignore` entry. Point it only at the throwaway sandbox repo from step 1.

Export the three required variables and run it:

```bash
export BH_REPO_OWNER=<owner>
export BH_REPO_NAME=<sandbox-repo>
export BH_PROJECT_ROOT=<abs-path-to-local-sandbox-clone>   # still required in this shell even if bin/setup-env.sh (docs/system-setup.md) already ran — that script only writes the value to host.env, which bin/init-sandbox.sh and bin/run-daemon.sh source internally but never export back into your shell
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

## 3. `BWS_ACCESS_TOKEN` — export it now, before provisioning rulesets

`BWS_ACCESS_TOKEN` is the single operator-supplied Bitwarden machine-account token. It is
needed starting with the **next** step (`bin/provision-ruleset.sh`), not just at daemon
launch time — that script mints a GitHub App JWT via
`python -m baton_harness.chain.app_auth jwt`, which vault-fetches the App's PEM using
`BWS_ACCESS_TOKEN` and `BWS_PEM_SECRET_ID` before it can call any GitHub API
(`src/baton_harness/chain/app_auth.py`, `main()`). Export it in this shell session now,
using silent input so the token never lands in your shell history:

```bash
read -r -s BWS_ACCESS_TOKEN
export BWS_ACCESS_TOKEN
```

**Never print, log, or commit the actual token value.** This walkthrough only checks its
*presence* and *shape* (non-empty), never its content — the same discipline `bh-daemon
--doctor` follows (see step 5).

For a first interactive run, exporting it in the shell is sufficient — it stays in this
session's environment through steps 4–6. For a persistent/server deployment, drop it in a
root-readable-only file instead of a shell export; see
[docs/smoke-test-daemon.md §"systemd unit (recommended)"](smoke-test-daemon.md#systemd-unit-recommended)
for the canonical `/etc/bh-daemon/secrets.env` (mode `600`) pattern, or
`bin/install-daemon-service.sh` to write it for you.

## 4. `bin/provision-ruleset.sh` — branch-protection rulesets

Requires `BWS_ACCESS_TOKEN` exported (step 3) and `BH_GITHUB_APP_ID` /
`BH_GITHUB_APP_INSTALLATION_ID` available — either already in `.bh/config.env` (written
by step 2) or exported directly.

```bash
bin/provision-ruleset.sh
```

What it does:

1. Mints a GitHub App JWT (needs `BWS_ACCESS_TOKEN` + `BWS_PEM_SECRET_ID`, per step 3)
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

## 5. `bh-daemon --doctor` / `--strict` — preflight before the first real run

`bh-daemon --doctor` runs the full preflight check catalog (20 checks) and exits without
starting the poll loop. **It reads only the ambient shell environment** — it does not
source `~/.config/baton-harness/host.env` (that sourcing only happens inside
`bin/run-daemon.sh`), and it does not load `${BH_PROJECT_ROOT}/.bh/config.env` into the
process environment either (that only happens later, in the daemon's normal startup path,
which the `--doctor`/`--check-vault` branch returns before reaching). So a bare `bh-daemon
--doctor` invoked in a genuinely fresh shell will report failures for `BH_PROJECT_ROOT` and
`BWS_ACCESS_TOKEN`, and also for the GitHub-API checks added below (`RULESET_MAIN`,
`RULESET_FEATURE`, `LABELS_PRESENT`, `GH_REPO_ADMIN` read `BH_REPO_OWNER`/`BH_REPO_NAME`
directly from the environment; the two ruleset checks also need `BH_GITHUB_APP_ID`) — even
on a correctly-provisioned host, unless you export all of these first. In this walkthrough's
normal flow they are already exported from step 2, so this mainly bites when running
`--doctor` in a fresh shell days later:

```bash
export BH_PROJECT_ROOT=<abs-path-to-local-sandbox-clone>   # if not already exported
export BWS_ACCESS_TOKEN=<your-bitwarden-machine-account-token>   # if not already exported
export BH_REPO_OWNER=<owner>                # if not already exported (step 2)
export BH_REPO_NAME=<sandbox-repo>          # if not already exported (step 2)
export BH_GITHUB_APP_ID=<app-id>            # if not already exported; only needed for the
                                             # two ruleset checks below

bh-daemon --doctor --strict
```

(`.venv/bin/bh-daemon` or `.venv/Scripts/bh-daemon` if the venv is not activated on
`PATH`.)

`--strict` makes the command exit non-zero if any **CRITICAL**-severity check ends in
`FAIL`; without it, `--doctor` always exits 0 regardless of findings, so scripting a
preflight gate around this command requires `--strict`. A `WARNING`-severity check never
trips `--strict`'s exit code, even on `FAIL`/`WARN`.

Each check prints one `[STATUS] Title` line; `FAIL` and `WARN` results additionally print
`detail:` and `fix:` lines. `PASS` and `SKIP` print only the header line. On a
correctly-provisioned host with `gh` authenticated against the target repo (and all the
env vars from the block above exported), a run looks like this. `GH_REPO_ADMIN` is the one
line here that can legitimately show `WARN` instead of `PASS` even on an otherwise-healthy
repo — GitHub's collaborators API does not always surface an implicit organization-owner's
admin rights — and a `WARN` there never trips `--strict`'s exit code (it is
WARNING-severity):

```text
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
[PASS] Main branch ruleset provisioned
[PASS] Feature branch ruleset provisioned
[PASS] Required repository labels present
[PASS] Repository admin collaborator present
[PASS] GitHub CLI authentication valid
[PASS] Claude OAuth credential file readable
```

A `FAIL` (here, `BWS_ACCESS_TOKEN` forgotten in a fresh shell) looks like this — the
detail line never reports the token's value, only that it is absent:

```text
[FAIL] BWS access token present
       detail: BWS_ACCESS_TOKEN is unset or empty.
       fix:    Set BWS_ACCESS_TOKEN to a non-empty access token.
```

The 20-check catalog (`src/baton_harness/chain/doctor.py`) covers two groups. The first 14
are local/CLI/config checks that can run before any GitHub App token exists: the four CLIs
(`gh`, `bws`, `claude` — CRITICAL; `uv` — WARNING); `BH_PROJECT_ROOT` validity; presence of
`~/.config/baton-harness/host.env` (WARNING) and `${BH_PROJECT_ROOT}/.bh/config.env`
(CRITICAL); shape-validation of that config file's required keys and optional secret IDs;
presence of `BWS_ACCESS_TOKEN`; the exact `.symphony/` `.gitignore` entry; absence of
`ANTHROPIC_API_KEY`; the force-PR-not-merge startup self-test; and a configured git
credential helper. The remaining 6 need a live GitHub API call and so only make sense once
repo identity is known: both branch-protection rulesets provisioned (step 4); all five
required harness labels present; a repository admin collaborator (WARNING — informational);
GitHub CLI authentication validity; and the Claude OAuth credential file's readability. It
never inspects or prints secret *values* — only presence, shape, and byte length where a
length is diagnostic (as in the `BWS_ACCESS_TOKEN` example above).

`--doctor` running the full 20-check catalog standalone is not the same as either of the
daemon's two real startup gates — it lets you see every check's `[STATUS]` ahead of time,
including the ones a real launch would skip. Five checks (`CRED_ANTHROPIC_UNSET`,
`FORCE_PR_TRIPWIRE`, `GIT_CRED_HELPER` in the first group; `GH_AUTH`, `CRED_OAUTH_VOLUME` in
the second) are marked `daemon_native` in the catalog: at a real daemon launch these are
instead covered by equivalent native startup code (the G3b/G3d checks and the force-PR
tripwire before secrets bootstrap; G3a/G3c after), not by re-running the catalog check
itself, so `--doctor` is the only way to see their `[STATUS]` output ahead of a run. See
step 6 for exactly where each of the daemon's two gates (`PRE_BOOTSTRAP`, `POST_BOOTSTRAP`)
sits relative to secret bootstrap and the native G-checks.

## 5a. `bh-daemon --check-vault` — opt-in live vault dry-run

`--check-vault` runs a single check outside the main catalog: a live `bws` fetch of the PEM
secret named by `BWS_PEM_SECRET_ID` in `.bh/config.env`, reporting only whether the fetch
returned a non-empty value. On `PASS`, only the status and check title are printed; no size
is reported. The secret's actual content is never printed, whether the fetch succeeds,
comes back empty, or the `bws` call itself fails (a bad token or an unreachable secret
surfaces as a `FAIL` with the underlying error message — e.g. `bws` exit status and stderr
— never the fetched value, which was never returned in that case):

```bash
export BH_PROJECT_ROOT=<abs-path-to-local-sandbox-clone>   # if not already exported
export BWS_ACCESS_TOKEN=<your-bitwarden-machine-account-token>   # if not already exported

bh-daemon --check-vault
```

```text
[PASS] Vault PEM secret fetch succeeds
```

or, on failure:

```text
[FAIL] Vault PEM secret fetch succeeds
       detail: PEM secret fetch returned an empty value.
       fix:    Verify BWS_PEM_SECRET_ID in .bh/config.env and set a valid BWS_ACCESS_TOKEN.
```

Exit code is `0` on `PASS`, `1` otherwise. `--check-vault` and `--doctor` are mutually
exclusive in effect: if both are passed, only the vault check runs (and `--strict` is
ignored) — pass `--check-vault` alone.

This check is deliberately **excluded** from both `--doctor`/`--strict`'s catalog and the
daemon's two startup gates (step 6) — it is redundant there, since `bootstrap_secrets`
performs the same PEM fetch seconds later on every real daemon launch. It exists as a
standalone, opt-in diagnostic for the one failure mode the other checks can't distinguish:
`BWS_PEM_SECRET_ID` present and shaped like a UUID (`CFG_OPTIONAL_SECRET_IDS`, step 5) but
not pointing at a readable secret. Run it only when you need to isolate that case — it is
the one doctor-adjacent check that touches the vault.

## 6. First daemon run — `bin/run-daemon.sh`

With the sandbox provisioned, rulesets in place, and doctor passing, run one bounded tick:

```bash
bin/run-daemon.sh --once
```

`bin/run-daemon.sh` derives the venv location from `bh-daemon`'s own path, sources
`~/.config/baton-harness/host.env` for `BH_PROJECT_ROOT`, then runs its own two preflights
before ever invoking `bh-daemon`:

1. **Label preflight** — confirms all five required labels exist in the target repo
   (created by step 2); aborts with the exact `gh label create` fix commands if not.
2. **`.symphony/`-gitignore preflight** — confirms the exact `.symphony/` line is present
   in the target repo's `.gitignore` (seeded by step 2); aborts with "this repo is not
   ready for harness work" if not.

It then `cd`s into `BH_PROJECT_ROOT` and execs `bh-daemon`, which runs the CRITICAL,
non-`daemon_native` subset of the step-5 doctor catalog as a hard gate
(`doctor.run_gate`, `PRE_BOOTSTRAP` phase) before bootstrapping any secret. A failing
check here prints `Preflight check <ID> failed: <detail> Fix: <fix>` to stderr and aborts
before any git or GitHub Actions work begins. `--once` runs exactly one poll-dispatch tick
then exits; this is the safe default for a first run. Omit `--once` for continuous polling
(stop with Ctrl-C).

Once secrets are bootstrapped, the daemon's startup sweep (`reconcile_startup`) runs a
**second** doctor gate — `doctor.run_gate`, `POST_BOOTSTRAP` phase — covering the checks
that need a live GitHub API call and so can't run before the App token exists:
`RULESET_MAIN`, `RULESET_FEATURE`, and `LABELS_PRESENT` (all CRITICAL; `GH_REPO_ADMIN` is
WARNING and never aborts). This runs after the native G3a–G3d credential checks (GitHub
token, `ANTHROPIC_API_KEY` absence, OAuth credential volume, git credential helper) and
before the G2 ungraceful-prior-exit marker check — so a misprovisioned ruleset or a missing
harness label aborts startup once credentials are already validated, but still before the
daemon starts polling for issues. A failing check here additionally fires a critical
escalation alert ("Post-bootstrap doctor gate failed a critical readiness check.") before
exiting non-zero. A `PRE_BOOTSTRAP` failure (step 6, above) aborts the same way — stderr
message, exit 1 — but does not fire an escalation alert; it happens earlier in startup,
before `bootstrap_secrets()` has run.

`BWS_ACCESS_TOKEN` has two independent checkpoints across the daemon's lifecycle, not one:
`bin/setup-env.sh` prints a non-fatal setup-time notice if it's absent when the venv is
first created, while `ENV_BWS_ACCESS_TOKEN` (step 5, `PRE_BOOTSTRAP`) is the fatal boot-time
gate that actually blocks a launch. The two are complementary, not redundant — the first
catches the problem early during machine setup; the second is the hard stop that guarantees
the daemon never starts without it.

For what a fully successful tick looks like in the logs, the CI-gate check-name
requirement that most often causes a first tick to park instead of merge, and how to seed
trigger issues, see
[docs/smoke-test-daemon.md §"Run it"](smoke-test-daemon.md#run-it) onward. For continuous
operation on a server (systemd unit, tmux/nohup), see
[docs/smoke-test-daemon.md §"Running on a Linux server"](smoke-test-daemon.md#running-on-a-linux-server).

## 7. Troubleshooting

Start with the failing step's own output — every script in this walkthrough prints an
`error:` (or `provision-ruleset:` / `baton-harness:`-prefixed) line with a specific fix
when a preflight fails, rather than a bare stack trace. If the failure is inside the
daemon's startup preflight rather than one of the `bin/*.sh` scripts, `bh-daemon --doctor`
(step 5) will name the exact check that fails, with a secret-safe `detail:` explaining
what it saw and a `fix:` explaining what to do about it — run it again after any fix to
confirm.

Common first-run stumbling points, in the order you are likely to hit them:

- **`init-sandbox.sh` hard-errors immediately at the config-write step.** You are running
  it non-interactively (no TTY, or `BH_SETUP_NO_PROMPT=1`). It has no way to collect the
  App ID, installation ID, and three Bitwarden UUIDs without prompting — run it in an
  interactive terminal.
- **`provision-ruleset.sh` fails at the JWT-minting step.** `BWS_ACCESS_TOKEN` is not
  exported in this shell (step 3), or `BWS_PEM_SECRET_ID` in `.bh/config.env` does not
  point at a real Bitwarden secret.
- **`provision-ruleset.sh` aborts with an App-ID mismatch.** You pasted the installation
  ID where the App ID goes (or vice versa) into `.bh/config.env` during step 2. Fetch both
  again per the step-1 commands and correct the file.
- **`bh-daemon --doctor` reports `BH_PROJECT_ROOT`/`BWS_ACCESS_TOKEN` failures on a host
  you believe is correctly set up.** `--doctor` does not source `host.env` — export both
  directly in the shell you're running `--doctor` from (see step 5).
- **`run-daemon.sh` aborts on the label or `.gitignore` preflight.** Do not re-run
  `bin/init-sandbox.sh` to fix this — issue and milestone creation are not idempotent
  (step 2) and re-running it against an already-provisioned sandbox creates duplicates.
  Apply a targeted fix instead: for a missing label, the label preflight itself prints
  the exact `gh label create` command to run (step 2 lists the five required labels); for
  the `.gitignore` preflight, add the `.symphony/` line to the sandbox repo's `.gitignore`
  and push it by hand (step 2, item 7). If the sandbox is broken beyond these targeted
  fixes, provision a fresh sandbox repo instead.
- **A tick runs but the issue parks instead of merging.** This is almost always the
  CI-gate check-name requirement — see
  [docs/smoke-test-daemon.md §"CI-gate subtlety"](smoke-test-daemon.md#ci-gate-subtlety--required-check-names).

If none of the above matches, `docs/smoke-test-daemon.md` covers the full environment-
variable resolution chain, the required GitHub App permission table, and (for a server
deployment) the `#40` recovery-path and `#239` block-escalation verification scripts,
which exercise the daemon's startup gates and self-block behavior end-to-end against a
live sandbox.
