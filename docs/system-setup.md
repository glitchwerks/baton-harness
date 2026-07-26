# bh-daemon: system setup

This is the first of two setup walkthroughs for bringing up `bh-daemon` on a machine that
has never run it before — a fresh VM, a new laptop, or a freshly provisioned server. This
doc covers **machine-level** setup: installing the required CLIs and creating the Python
virtual environment. It answers "what do I run, in what order, and how do I know each step
worked" for an operator with nothing installed yet.

Once this doc is complete, continue to
[docs/repository-onboarding.md](repository-onboarding.md) for the repo/sandbox-level
steps — provisioning a throwaway sandbox repo, the Bitwarden access token, branch-protection
rulesets, the daemon preflight check, and the first run.

Neither doc replaces [docs/smoke-test-daemon.md](smoke-test-daemon.md), which is the
authoritative runbook for the daemon's environment-variable resolution chain, the required
GitHub App permission table, DAG dependency wiring for a multi-issue smoke test, CI-gate
behavior, and systemd deployment. Together these two docs get you from a bare machine to a
passing preflight check and a first `--once` run; they link out to `smoke-test-daemon.md`
for anything beyond that instead of duplicating it.

## 1. Prerequisites — have these in hand before you start

These are the CLIs `bin/setup-env.sh` (step 2 below) needs — some it installs for you
(marked "auto"), one you must install yourself first.

**Software (auto-installable on Linux/macOS by `bin/setup-env.sh`, see step 2):**

- [`uv`](https://docs.astral.sh/uv/) — not auto-installed; install it yourself first
  (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- `gh` (GitHub CLI; authenticate after setup with `gh auth login`)
- `bws` (Bitwarden Secrets CLI)
- `claude` (Claude Code CLI; authenticate interactively after installation via
  subscription/OAuth by running `claude` once — the daemon uses OAuth, not an API key)
- `git`, configured with a user name and email

**OS:** Linux or macOS with bash. (Some later-stage server scripts — `bin/verify-recovery.sh`,
`bin/verify-block-escalation.sh` — are Linux-only, but everything in this walkthrough runs
on macOS too.)

The repo/sandbox-level prerequisites — a throwaway GitHub repo, a GitHub App, its PEM, and
the Bitwarden access token — are covered in
[docs/repository-onboarding.md §1](repository-onboarding.md).

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

## Next: prepare a repo for daemon runs

Once `bh-daemon --help` runs inside the venv, this machine's tooling is ready. Continue to
[docs/repository-onboarding.md](repository-onboarding.md) to provision a sandbox repo and
run the daemon for the first time.
