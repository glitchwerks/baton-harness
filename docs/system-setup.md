# bh-daemon: system setup

This is the first of two setup walkthroughs for bringing up `bh-daemon` on a machine that
has never run it before — a fresh VM, a new laptop, or a freshly provisioned server. This
doc covers **machine-level** setup: installing the required CLIs and creating the Python
virtual environment. It answers "what do I run, in what order, and how do I know each step
worked" for an operator with nothing installed yet.

Once this doc is complete, continue to
[docs/repository-onboarding.md](repository-onboarding.md) for the repo/sandbox-level
steps — provisioning a throwaway sandbox repo, selecting the App-key provider, branch-protection
rulesets, the daemon preflight check, and the first run.

Neither doc replaces [docs/smoke-test-daemon.md](smoke-test-daemon.md), which is the
authoritative runbook for the daemon's environment-variable resolution chain, DAG dependency
wiring for a multi-issue smoke test, CI-gate behavior, and systemd deployment. Together these
two docs get you from a bare machine to a passing preflight check and a first `--once` run;
they link out to `smoke-test-daemon.md` for anything beyond that instead of duplicating it.
For what each credential these walkthroughs provision *is* and why it's required, see
[docs/authentication.md](authentication.md).

## 1. Prerequisites — have these in hand before you start

These are the CLIs `bin/setup-env.sh` (step 2 below) checks — it requires `uv`, `gh`, and
`claude`, while `bws` is conditional on the later provider selection.

**Software (auto-installable on Linux/macOS by `bin/setup-env.sh`, see step 2):**

- [`uv`](https://docs.astral.sh/uv/) — not auto-installed; install it yourself first
  (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- `gh` (GitHub CLI; authenticate after setup with `gh auth login` — this is your personal
  GitHub identity, separate from `bh-daemon`'s runtime credentials; see
  [docs/authentication.md § Operator `gh auth login`](authentication.md#operator-gh-auth-login-not-a-runtime-credential))
- `bws` (Bitwarden Secrets CLI) when the App-key provider is `bws`, or either optional
  BWS PAT/heartbeat secret locator is configured. It is not a daemon runtime prerequisite
  for a file-only deployment.
- `claude` (Claude Code CLI; authenticate interactively after installation via
  subscription/OAuth by running `claude` once — the daemon uses OAuth, not an API key)
- `git`, configured with a user name and email

**OS:** Linux or macOS with bash. (Some later-stage server scripts — `bin/verify-recovery.sh`,
`bin/verify-block-escalation.sh` — are Linux-only, but everything in this walkthrough runs
on macOS too.)

The repo/sandbox-level prerequisites — a throwaway GitHub repo, a GitHub App, its PEM
provider, and a Bitwarden access token only when BWS is configured — are covered in
[docs/repository-onboarding.md §1](repository-onboarding.md).

## 2. `bin/setup-env.sh` — Python environment and CLI checks

From the harness repo root:

```bash
bin/setup-env.sh
```

What it does, in order:

1. Checks `uv` is on `PATH` (fails with an install hint if not — this is the one tool it
   does not offer to auto-install)
2. Checks optional `bws` on `PATH` and offers the pinned, checksum-verified v2.1.0 install
   in an interactive Linux/macOS terminal. Declining, input EOF, non-interactive mode, or
   `BH_SETUP_NO_PROMPT=1` prints the conditional manual-install guidance and continues
   without a network call; provider selection happens later in `bin/init-sandbox.sh`.
3. Requires `gh` and `claude` on `PATH`; in an interactive Linux/macOS terminal it offers
   to install them (`gh` v2.62.0 with checksum verification; the official installer for
   `claude`). Missing either in a non-interactive context or with `BH_SETUP_NO_PROMPT=1`
   exits 1 with its manual-install link and makes no network call.
4. Creates `.venv` (skipped if already present — safe to re-run)
5. Syncs the package editably with the exact runtime and development
   dependencies from `uv.lock`: `BH_BUILD_DEVELOPMENT=1 uv sync --locked --extra dev`
6. Verifies `bh-daemon` is reachable inside the venv
7. Prints the venv-activation hint
8. In an interactive terminal, prompts for `BH_PROJECT_ROOT` (the absolute path to your
   local sandbox clone) and writes it to `~/.config/baton-harness/host.env` (mode 600) —
   `bin/run-daemon.sh` sources this automatically on every later launch
9. Checks whether `BWS_ACCESS_TOKEN` is already set and prints a non-fatal notice if not.
   The notice is relevant only when the selected provider or optional secret locators use
   BWS; the setup script itself never consumes the token.

**Verify it worked:**

```bash
# bh-daemon is on PATH inside the venv
.venv/Scripts/bh-daemon --help   # Windows Git Bash
.venv/bin/bh-daemon --help       # macOS/Linux

# Verify a runtime-only, non-editable wheel on Python 3.10 and 3.13
.venv/Scripts/bh-verify-foundation.exe   # Windows Git Bash
.venv/bin/bh-verify-foundation           # macOS/Linux

# host.env was written (only if you answered the prompt)
cat ~/.config/baton-harness/host.env
```

### Editable development versus immutable production

`bin/setup-env.sh` creates a locked but editable development environment with
`BH_BUILD_DEVELOPMENT=1 uv sync --locked --extra dev`. Source changes are immediately visible and the
quality tools are installed. The editable version stays `0.1.0.dev0`; the build-time
exact commit is recorded in `source_revision`.
Production consumers pin immutable release tags and install verified standard
artifacts. Release automation may derive `BH_BUILD_VERSION` (PEP 440) and
`BH_BUILD_SOURCE_REVISION` (40 hex characters) from a selected tag. Standard builds
without both assertions fail closed.

Production installation is non-editable and contains runtime dependencies only.
`bh-verify-foundation` is the executable production-installation reference: it
checks `uv.lock` without modifying it, compares canonical package resources with
their temporary `config/` mirrors, exports the locked runtime and development
closures, and constrains the build backend to the hashed development resolution.
It then builds the sdist and wheel, installs only the runtime closure plus the
wheel into clean Python 3.10 and 3.13 environments outside the checkout, runs
`uv pip check`, loads every packaged resource, rejects development-only packages,
and executes every installed console wrapper.

CI runs the same command with both default interpreters. For a focused diagnostic
run, pass one or more `--python VERSION` arguments. The command fails closed on
stale dependency metadata or packaging drift and never rewrites the lock or
resource mirrors.

To stage the equivalent non-editable runtime installation manually, run these
Bash commands from the repository root. Replace the illustrative version and
40-hex revision below with the selected immutable release tag's version and exact
commit (which must match HEAD in a checkout). The hook validates these explicit
assertions, not the tag name. Unset the development flag for a standard build:

```bash
unset BH_BUILD_DEVELOPMENT
mkdir -p .tmp
uv lock --check
uv export --locked --no-emit-project --format requirements.txt \
  --output-file .tmp/runtime-requirements.txt
uv export --locked --extra dev --no-emit-project --format requirements.txt \
  --output-file .tmp/build-requirements.txt
BH_BUILD_VERSION=1.0.0 \
BH_BUILD_SOURCE_REVISION=0123456789abcdef0123456789abcdef01234567 \
uv build --build-constraints .tmp/build-requirements.txt --require-hashes \
  --sdist --wheel --out-dir .tmp/dist
uv venv .tmp/runtime-venv --python 3.13

RUNTIME_PYTHON=.tmp/runtime-venv/Scripts/python.exe  # Windows Git Bash
# RUNTIME_PYTHON=.tmp/runtime-venv/bin/python        # macOS/Linux
uv pip sync --python "$RUNTIME_PYTHON" .tmp/runtime-requirements.txt
uv pip install --python "$RUNTIME_PYTHON" --no-deps \
  .tmp/dist/baton_harness-*.whl
uv pip check --python "$RUNTIME_PYTHON"
```

The `dev` export constrains the build backend; it is not synced into the runtime
environment. Run `bh-verify-foundation` before promoting the wheel.

Check the installed artifact and selected operational phases:

```bash
bh-daemon --version
bh-daemon --provenance
bh-daemon --doctor --phase installation --format json --strict
bh-daemon --doctor --phase configuration --config /path/to/config.env --strict
bh-daemon --doctor --phase live --strict
```

Installation is offline and credential-free; configuration is local-only; live may
use credentials and network calls. Repeat `--phase` as needed; omission runs all
three. `--format` defaults to text. `--config` overrides the default
`$BH_PROJECT_ROOT/.bh/config.env`; non-empty environment overrides still apply.
Doctor findings are advisory (exit 0) unless `--strict` finds a critical failure
(exit 1). Unsafe rendering also exits 1; usage errors exit 2. Daemon startup always
fails closed on critical results. `--check-vault` selects only the live App-key
check and exits 0 only on PASS. See the
[operator JSON contract](repository-onboarding.md#5-bh-daemon---doctor--strict--preflight-before-the-first-real-run)
for schema version 1 fields and config semantics.

If `gh`, `bws`, or `claude` were auto-installed to `~/.local/bin` and are not yet visible
to `command -v`, add `export PATH="$HOME/.local/bin:$PATH"` to your shell rc and re-run.
After installing `gh` or `claude`, authenticate them separately (`gh auth login`; run
`claude` once interactively) — `setup-env.sh` only installs the binaries, not credentials.

### Optional external systemd credential path

For a file-provider server, host provisioning may place the PEM outside Harness and use
systemd credentials to expose a service-private path. This non-authoritative example is
an external host-provisioning option, not Harness-managed key generation, copying,
enrollment, or rotation:

```ini
[Service]
LoadCredential=app.pem:/externally/provisioned/github-app.pem
Environment=BH_GITHUB_APP_KEY_PROVIDER=file
Environment=BH_GITHUB_APP_PRIVATE_KEY_FILE=%d/app.pem
```

systemd expands `%d` to the service credential directory. See the official
[systemd.exec credentials documentation](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#Credentials)
(fetched 2026-09-05). Harness consumes only the resulting absolute path; your host
provisioner remains responsible for placing and securing the source PEM.

## Next: prepare a repo for daemon runs

## Production systemd cutover

The editable `.venv` above is a development environment. Production systemd
installation uses a separately installed release wheel, normally
`<harness>/.venv-codereeve`, and the canonical `codereeve.service` unit. Render
the unit first, install without activation with `--no-start`, and recover an
interrupted transaction with `--recover JOURNAL`. The installer never modifies
the legacy environment and retains recovery journals and backups through 0.3.x.

Automatic activation is Linux-only and requires root systemd and complete
cgroup-v2/process visibility while the daemon runs as a dedicated non-root
account. Follow [the service cutover runbook](codereeve-service-cutover.md) for
the exact install, doctor, recovery, and disposable acceptance commands. The
Windows tests do not claim live activation.

Once `bh-daemon --help` runs inside the venv, this machine's tooling is ready. Continue to
[docs/repository-onboarding.md](repository-onboarding.md) to provision a sandbox repo and
run the daemon for the first time.
