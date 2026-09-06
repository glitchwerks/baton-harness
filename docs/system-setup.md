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
5. Installs the package with dev extras: `uv pip install -e ".[dev]"`
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

# host.env was written (only if you answered the prompt)
cat ~/.config/baton-harness/host.env
```

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

Once `bh-daemon --help` runs inside the venv, this machine's tooling is ready. Continue to
[docs/repository-onboarding.md](repository-onboarding.md) to provision a sandbox repo and
run the daemon for the first time.
