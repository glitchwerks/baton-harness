# Authentication

Single reference for every external service `bh-daemon` authenticates to: auth method, required permissions/scopes and why, and which harness module consumes the credential.

**Audience:** operators provisioning or troubleshooting a `bh-daemon` deployment, and developers changing auth-adjacent code. For the step-by-step provisioning walkthrough, see [docs/system-setup.md](system-setup.md) (machine-level) and [docs/repository-onboarding.md](repository-onboarding.md) (repo/sandbox-level) — this doc explains *what* each credential is and *why* it's required; those docs explain *how* to provision it. For the mechanics of how credentials are threaded into subprocess environments, see [docs/harness-design.md §12](harness-design.md#12-two-identity-subprocess-auth-model-identity-broker-implemented--issue-222) — that section is the authoritative home for the two-identity broker model and is not duplicated here.

| Authentication Method | Consumer | Method of Provision |
|---|---|---|
| [GitHub App installation token](#github-app-primary) | daemon push/labels/CI-reads/ruleset-bypass (`git`, `gh`) — `Identity.APP` | Vault (Bitwarden PEM, minted to short-lived token at runtime) |
| [GitHub fine-grained PAT](#github-fine-grained-pat-fallback) | daemon fallback path, CI reads via Actions API not Checks, no ruleset bypass (`git`, `gh`) — `Identity.APP` | Vault (preferred) or User Setup (direct `export GH_TOKEN=...`) |
| [Bitwarden Secrets Manager (`BWS_ACCESS_TOKEN`)](#bitwarden-secrets-manager) | daemon startup / secret bootstrap (`app_auth.py`) | User Setup (operator exports manually — this credential itself is never vaulted) |
| [Anthropic / Claude Code OAuth](#anthropic--claude-code) | Claude Code worker subprocess — `Identity.WORKER` | Web auth (interactive `claude` login, produces `~/.claude/.credentials.json`) |
| [Slack webhook (`BH_SLACK_WEBHOOK_URL`)](#slack) | daemon escalation notifications (`escalation.py`) | User Setup (plain env var, no vault form exists for it) |

## Operator `gh auth login` (not a runtime credential)

The `gh auth login` session from [docs/system-setup.md §1](system-setup.md#1-prerequisites--have-these-in-hand-before-you-start) is the operator's own personal GitHub identity — separate from the five `bh-daemon` runtime credentials in the table above, and not one of the credentials `bh-daemon` provisions or requires at runtime. It backs one-time, operator-driven setup actions only:

- `gh auth setup-git` (git credential helper), run by `bin/init-sandbox.sh`
- Label, issue, and milestone creation in `bin/init-sandbox.sh` (`gh label create`, `gh issue create`, `gh api .../dependencies/blocked_by`)
- Reading the GitHub App's installation ID (`gh api /repos/<owner>/<repo>/installation --jq .id`), per [docs/repository-onboarding.md](repository-onboarding.md)
- Verifying rulesets after `bin/provision-ruleset.sh` runs (`gh api repos/<owner>/<repo>/rulesets --jq '.[].name'`), per [docs/repository-onboarding.md](repository-onboarding.md)

## GitHub App (primary)

**Auth method:** App ID + installation ID + RSA private key (PEM) → RS256-signed JWT → short-lived GitHub App installation access token (prefix `ghs_`).

- `build_app_jwt` (`src/baton_harness/chain/app_auth.py`) signs a JWT with a 9-minute TTL and a 60-second `iat` backdate for clock skew.
- `mint_installation_token` exchanges that JWT for an installation token via the GitHub REST API, with retry on transient 5xx responses.
- The private key PEM is never stored on disk by the harness — it is vault-fetched from Bitwarden Secrets Manager at daemon startup (see [Bitwarden Secrets Manager](#bitwarden-secrets-manager) below) and held only in memory.
- The installation token is **never written to `os.environ`** — `bootstrap_secrets()` and `build_installation_token_provider()` return it by value; callers pass it explicitly into the one subprocess invocation that needs it and discard it afterward (env-discipline invariant documented in `app_auth.py`'s module docstring).

**Consumed via:** `Identity.APP` (`src/baton_harness/chain/identity.py`) and `gh_env()` / `app_auth.py`, for daemon-privileged operations only:

- Feature-branch push (`_authed_git_push`, `src/baton_harness/chain/daemon/push_probe.py`)
- Ruleset preflight `gh api` reads (the daemon's launch-gate)
- Label transitions and CI-status reads
- The App is also the sole bypass actor on the `feature/*` branch-protection ruleset — a fine-grained PAT cannot hold that role (see [GitHub fine-grained PAT](#github-fine-grained-pat-fallback) below for how this narrows the fallback path)

**Validated by:** `validate_daemon_token()` (`src/baton_harness/_auth.py`) — a type-gate that rejects anything not prefixed `ghs_`. Runs as gate **G3a** at daemon startup (`src/baton_harness/chain/reconcile.py`, fatal on failure) and again in `src/baton_harness/chain/cli.py`.

**Required permissions** (configure on the GitHub App's settings page before installing it on the target repo; verify the live grant with `gh api /repos/<owner>/<repo>/installation --jq '.permissions'`):

| Permission | Level | Why required |
|---|---|---|
| `contents` | `write` | Push `feature/*` and `baton/*` branches; read repo files |
| `pull_requests` | `write` | Create PRs; post review comments |
| `issues` | `write` | Label transitions (`agent-ready` / `agent-in-progress` / `blocked` / `agent-done` / `agent-merged`); post escalation comments |
| `actions` or `checks` | `read` | Poll CI check-runs for the merge gate |
| `administration` | `read` | Preflight reads rulesets via `GET /repos/.../rulesets` (`ruleset_status.py`, issue #144); refuses every launch on a non-2xx response |
| `administration` | `write` | `bin/provision-ruleset.sh` POSTs and PUTs rulesets |
| `metadata` | `read` | Always required by GitHub for any App installation |

**Residual risk — `administration: write` is repo-scoped, not ruleset-scoped:** GitHub's permission model has no way to grant "write this ruleset only" — `administration: write` authorizes POST/PUT **and DELETE** (`DELETE /repos/{owner}/{repo}/rulesets/{ruleset_id}`) on *every* ruleset in the repo, including `harness-main-no-merge`, the ruleset enforcing the daemon's core "never merges to `main`" invariant ([docs/harness-design.md §10](harness-design.md#10-always-on-daemon-dependency-ordered-work-units-implemented-v1-serial)). A compromised App token could therefore not just modify but **outright delete** `harness-main-no-merge`, removing the invariant's enforcement entirely — that deletion case is in scope of this residual risk, not just create/update. This is a **GitHub platform limitation, not a gap in this harness's design** — no finer-grained permission exists to request instead.

The existing mitigations are code-level, not permission-level (see [docs/harness-design.md §12](harness-design.md#12-two-identity-subprocess-auth-model-identity-broker-implemented--issue-222) for the broker mechanics; not re-derived here):

- `Identity.APP` — the identity actually carrying this token — is resolved only at fixed daemon call sites (`_authed_git_push`, the ruleset preflight `gh api` reads, label transitions, and CI-status reads) and by the operator-run `bin/provision-ruleset.sh`; the Claude Code worker subprocess never resolves `Identity.APP`.
- `Identity.WORKER` — what the LLM agent runs under — strips all three privileged env keys (`GH_TOKEN`, `GITHUB_TOKEN`, `GH_INSTALLATION_TOKEN`) unconditionally (`identity.py`). The identity is additive-safe by construction: requesting `WORKER` can only remove credentials, never grant them.
- `tests/chain/test_identity_spawn_guard.py` AST-walks every subprocess spawn under `src/baton_harness/chain/` and fails closed on any spawn that omits an explicit identity — no call site can silently inherit an ambient token.
- Net effect: the prompt-injectable, untrusted part of the system (the LLM worker) never holds the App token and never calls `gh api` on rulesets. Only fixed, reviewed script/daemon code does, and that code only ever touches the two rulesets defined in checked-in JSON (`config/ruleset.main.json`, `config/ruleset.feature.json`) — never an arbitrary or agent-supplied ruleset name.

**Decision: risk accepted**, following the same posture as [D1](harness-design.md#d1--tos-posture-risk-accepted-revisit-at-terms-changes) — this is a *monitored assumption*, not a closed gate. What would further close it (future hardening, not a current gap or action item): credential rotation discipline for the App's private key, and audit-log alerting on ruleset-modification events (GitHub's audit log API or an equivalent webhook).

**Provisioning:** [docs/repository-onboarding.md](repository-onboarding.md) walks through App creation, obtaining the App ID and installation ID, and uploading the PEM to Bitwarden.

## GitHub fine-grained PAT (fallback)

**Auth method:** a fine-grained personal access token (prefix `github_pat_`), exported directly as `GH_TOKEN` (or `GITHUB_TOKEN`) instead of provisioning a GitHub App + Bitwarden PEM. Mint at <https://github.com/settings/personal-access-tokens/new> under a dedicated bot/machine account — never a personal account.

**This is a narrower credential than the GitHub App, not an equivalent one — for two repo-specific reasons, not a blanket GitHub-wide restriction on what fine-grained PATs can hold.** This harness's PAT is not granted `checks`, so merge-gate CI status is read from the Actions API instead. Separately, the ruleset bypass actor for `feature/*` pushes must be the GitHub App itself — a fine-grained PAT cannot hold that role no matter what permissions it's granted (issue #220; see harness-design.md §12, which documents the bypass-actor requirement this constraint derives from). Note that fine-grained PATs *can* in general carry `administration`, including for ruleset read/write endpoints — this harness simply doesn't grant it to the PAT, both because it's unneeded (the App handles the ruleset preflight read) and because the bypass-actor constraint above rules out the PAT path for feature-branch pushes regardless. Consequences:

- CI status is read from the Actions API (`repos/{owner}/{repo}/actions/runs` + `.../jobs`, `src/baton_harness/chain/merge.py`) instead of the Checks API — the merge gate polls with `Actions: read`, not `checks` (#121).
- The PAT cannot be the ruleset bypass actor for `feature/*` pushes — that role requires `Identity.APP` (issue #220). The `administration` preflight read is also performed via `Identity.APP` in this harness, though that is a scoping choice here, not a GitHub-imposed limitation on the PAT.

**Required fine-grained PAT permissions:**

| Operation | Fine-grained permission |
|---|---|
| Clone repo, push feature branches | Contents: Read & write |
| Read issue body, edit labels, post comments | Issues: Read & write |
| `gh pr list` / `gh pr create` | Pull requests: Read & write |
| CI merge gate (read workflow-run/job conclusions) | Actions: Read |
| Baseline (granted automatically) | Metadata: Read |

Not granted: Workflows, Administration, Secrets, Checks (App-only), any org-level scope. `Commit statuses: Read` is a useful diagnostic supplement for `gh pr checks` but not required.

**Provisioning:**

- **Vault fetch (preferred when using the PAT path):** set `BWS_GH_TOKEN_SECRET_ID` in `${BH_PROJECT_ROOT}/.bh/config.env`; `bootstrap_secrets()` fetches the PAT from Bitwarden at startup and writes it to `GH_TOKEN`.
- **Direct export (override / fallback):** `export GH_TOKEN=github_pat_<token>` — an explicit env value always wins over the vault fetch. This is the path described in [docs/repository-onboarding.md §1](repository-onboarding.md#1-prerequisites--have-these-in-hand-before-you-start) as the alternative to provisioning a GitHub App.

**Validated by:** `validate_github_token()` (`src/baton_harness/_auth.py`) — reads `GH_TOKEN`/`GITHUB_TOKEN`, rejects classic `ghp_` PATs and anything not prefixed `github_pat_`, then runs a live `gh api user` capability self-test (retried up to `_MAX_RETRIES` = 2 times on transient failures — rate-limits, gateway errors, DNS/TLS — before failing closed; permanent 401/403 failures raise immediately). This runs at the top of the `bh-before-run` hook (`src/baton_harness/before_run.py`), i.e. once per worker turn, not at daemon startup — a **different gate, on a different credential, in a different execution context** than `validate_daemon_token()` above. Known limitation: fine-grained PATs expose no scope-introspection API (unlike classic PATs' `X-OAuth-Scopes` response header), so this gate verifies token *type* and *reachability* only, not the exact granted permission set — that verification is the operator's responsibility at mint time.

## Bitwarden Secrets Manager

**Auth method:** `BWS_ACCESS_TOKEN`, an operator-supplied Bitwarden Secrets Manager machine-account access token. This is the one secret an operator personally carries — the harness never writes it to a file it manages, and `bootstrap_secrets()` / `build_installation_token_provider()` (`src/baton_harness/chain/app_auth.py`) **pop it from `os.environ` immediately** after reading it, so worker subprocesses (which inherit a copy of the daemon's environment before the `Identity.WORKER` filter is applied) cannot read it.

**What it's used to fetch**, by Bitwarden Secrets UUID:

| Secret ID env var | Required? | Fetches |
|---|---|---|
| `BWS_PEM_SECRET_ID` | Required | The GitHub App's RSA private key (PEM) |
| `BWS_GH_TOKEN_SECRET_ID` | Optional | A fine-grained PAT — vault-fetched into `GH_TOKEN` when set and `GH_TOKEN` is not already in the environment (see [GitHub fine-grained PAT](#github-fine-grained-pat-fallback) above) |
| `BWS_HEARTBEAT_PING_URL_SECRET_ID` | Optional | The dead-man's-switch heartbeat ping URL — vault-fetched into `BH_HEARTBEAT_PING_URL` when set and that variable is not already in the environment. This is **not** the Slack webhook URL: `BH_HEARTBEAT_PING_URL` is a GET target polled once per heartbeat tick by `src/baton_harness/chain/daemon/poll.py`, distinct from `BH_SLACK_WEBHOOK_URL` (a POST target, see [Slack](#slack) below), which has no Bitwarden-vaulted form — it is supplied directly as an env var only. |

**Consuming code:** `src/baton_harness/chain/app_auth.py` (`bootstrap_secrets`, `build_installation_token_provider`, `main`).

**Provisioning:** [docs/repository-onboarding.md §3](repository-onboarding.md#3-bws_access_token--export-it-now-before-provisioning-rulesets) covers exporting `BWS_ACCESS_TOKEN` for an interactive session, and the `/etc/bh-daemon/secrets.env` (mode `600`) pattern for a persistent server deployment.

## Anthropic / Claude Code

**Auth method:** OAuth via a mounted `~/.claude/.credentials.json` volume — the subscription auth flow, established once by running `claude` interactively. `ANTHROPIC_API_KEY` is **explicitly forbidden**: the daemon refuses to start if it is set.

- Gate **G3b** (`src/baton_harness/chain/reconcile.py`) checks `ANTHROPIC_API_KEY` is absent at every daemon startup — fatal, critical alert, `sys.exit(1)` if present. A non-empty key means per-token billing is active, which contradicts the subscription-only cost model.
- Gate **G3c** (`src/baton_harness/chain/reconcile.py`) checks the OAuth credential file is present and readable — structural check only (presence + `open()` succeeds); contents are never read, decoded, or logged. Fatal if absent or unreadable.

**Consumed by:** the Claude Code worker subprocess turn-loop, under `Identity.WORKER` (see [docs/harness-design.md §12](harness-design.md#12-two-identity-subprocess-auth-model-identity-broker-implemented--issue-222)).

**Provisioning:** run `claude` once interactively after installation (`docs/system-setup.md §1`). On a server, `~/.claude/.credentials.json` must be an explicit credential-volume mount.

## `Identity.WORKER`

The unprivileged spawn identity used for the Claude Code worker subprocess and other unprivileged chain spawns (git branch ops, sandboxed tool calls). All three privileged GitHub credential keys (`GH_TOKEN`, `GITHUB_TOKEN`, `GH_INSTALLATION_TOKEN`) are stripped unconditionally — the identity is deliberately additive-safe: requesting `WORKER` can only remove credentials from a subprocess's environment, never grant them.

The full two-identity broker model — `Identity.APP` vs. `Identity.WORKER`, the `env_for()` single resolution point, and the AST-walking spawn guard that enforces every `chain/` subprocess declares an explicit identity — is documented in **[docs/harness-design.md §12 "Two-identity subprocess auth model (identity broker)"](harness-design.md#12-two-identity-subprocess-auth-model-identity-broker-implemented--issue-222)** (issue #222). That section is the authoritative source; this doc does not duplicate its mechanics.

## Slack

**What's implemented today (v1):** a plain incoming webhook, nothing more. `BH_SLACK_WEBHOOK_URL` is an optional env var; when set, `src/baton_harness/chain/escalation.py` POSTs a plain-text message to it as a best-effort notification. There is **no bot auth, no OAuth scopes, no Socket Mode connection** — none of that exists in the current implementation.

- The GitHub issue comment is the durable record and is always attempted first; Slack is notification-only. Any Slack POST failure is logged at WARNING and does not affect the return value or block the durable record.
- When `BH_SLACK_WEBHOOK_URL` is unset, Slack is silently skipped at escalation time — not fatal. If it and `BH_HEARTBEAT_PING_URL` are *both* unset, the daemon logs a one-time startup WARNING (`src/baton_harness/chain/daemon/poll.py`) that async failure-signal escalation is entirely unconfigured.
- No Bitwarden-vaulted form exists for the webhook URL itself (see the [Bitwarden Secrets Manager](#bitwarden-secrets-manager) table above) — it is supplied directly as an env var.

**What's designed but deferred (v2, not implemented):** [docs/architecture-spec.md](architecture-spec.md) describes a two-way Slack Bolt bot over Socket Mode — bot/app tokens, OAuth scopes, an outbound WebSocket connection, Block Kit interactive decision cards, and thread-reply handling. **None of this exists yet.** A reader following `architecture-spec.md` alone would look for bot tokens and OAuth scopes that are not part of the current implementation; treat that document's Slack sections as the aspirational v2 design, not a description of what a deployment needs to configure today. See `architecture-spec.md` §10 item 2 for the tracked status of that gap.

## Related documents

- [docs/harness-design.md §12](harness-design.md#12-two-identity-subprocess-auth-model-identity-broker-implemented--issue-222) — the two-identity broker mechanics (authoritative; not duplicated here)
- [docs/system-setup.md](system-setup.md) — machine-level CLI setup and auth (`gh auth login`, `claude` OAuth, `bws` install)
- [docs/repository-onboarding.md](repository-onboarding.md) — repo/sandbox-level provisioning walkthrough (GitHub App creation, Bitwarden export, ruleset provisioning)
- [docs/smoke-test-daemon.md](smoke-test-daemon.md) — provisioning runbook; live permission-verification command
