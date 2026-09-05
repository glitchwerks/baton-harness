# Authentication

Single reference for every external service `bh-daemon` authenticates to: auth method, required permissions/scopes and why, and which harness module consumes the credential.

**Audience:** operators provisioning or troubleshooting a `bh-daemon` deployment, and developers changing auth-adjacent code. For the step-by-step provisioning walkthrough, see [docs/system-setup.md](system-setup.md) (machine-level) and [docs/repository-onboarding.md](repository-onboarding.md) (repo/sandbox-level) — this doc explains *what* each credential is and *why* it's required; those docs explain *how* to provision it. For the mechanics of how credentials are threaded into subprocess environments, see [docs/harness-design.md §12](harness-design.md#12-two-identity-subprocess-auth-model-identity-broker-implemented--issue-222) — that section is the authoritative home for the two-identity broker model and is not duplicated here.

| Authentication Method | Consumer | Method of Provision |
|---|---|---|
| [GitHub App installation token](#github-app-primary) | daemon push/labels/CI-reads/ruleset-bypass (`git`, `gh`) — `Identity.APP` | Selected App-key provider (`bws` or secured host file), minted to short-lived token at runtime |
| [GitHub fine-grained PAT](#github-fine-grained-pat-fallback) | required by the standard `bh-before-run` worker hook; never daemon authority | Vault or externally supplied `GH_TOKEN` |
| [Bitwarden Secrets Manager (`BWS_ACCESS_TOKEN`)](#bitwarden-secrets-manager) | conditional daemon startup / secret bootstrap (`app_auth.py`) | User Setup when any BWS-backed source is configured (this credential itself is never vaulted) |
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
- The private key PEM is loaded exactly once from the explicitly selected `bws` or `file`
  provider and held only in memory by Harness. A file is externally provisioned; Harness
  never generates, copies, enrolls, or rotates it.
- Before any installation-token API call, `build_installation_token_provider()` proves the
  loaded PEM can sign an RS256 App JWT. A missing, unsafe, unreadable, or malformed key
  fails startup before GitHub access.
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

### App private-key provider matrix

`BH_GITHUB_APP_KEY_PROVIDER` is required and accepts exactly `bws` or `file`. The selected
source and the unselected source are mutually exclusive:

| Provider | Required App-key setting | App-key BWS prerequisites |
|---|---|---|
| `bws` | `BWS_PEM_SECRET_ID=<uuid>` | `bws` on `PATH` and non-empty `BWS_ACCESS_TOKEN` |
| `file` | `BH_GITHUB_APP_PRIVATE_KEY_FILE=<absolute-path>` | None for the App key |

For `file`, the target must be an absolute-path, regular, non-symlink file with no group
or world permission bits (owner-only modes such as `0400` and `0600` are accepted). It
must be readable, unchanged across open, non-empty UTF-8 PEM text, and no larger than
1 MiB. The loader uses a single secured descriptor and rejects replacement races where
the platform exposes meaningful file identity.

Optional BWS consumers compose independently with either provider:

| Configuration | `bws` and `BWS_ACCESS_TOKEN` needed? |
|---|---|
| `bws` App key | Yes |
| `file` App key, no optional BWS secret IDs | No |
| `file` App key plus `BWS_GH_TOKEN_SECRET_ID` and/or `BWS_HEARTBEAT_PING_URL_SECRET_ID` | Yes, for those optional fetches |

Migration is fail-closed: existing deployments must add
`BH_GITHUB_APP_KEY_PROVIDER=bws` and retain `BWS_PEM_SECRET_ID`. File deployments set
`BH_GITHUB_APP_KEY_PROVIDER=file`, set `BH_GITHUB_APP_PRIVATE_KEY_FILE`, and remove
`BWS_PEM_SECRET_ID`; there is no implicit provider default.

**Provisioning:** [docs/repository-onboarding.md](repository-onboarding.md) walks through
App creation, obtaining the App ID and installation ID, and selecting either provider.

<a id="github-fine-grained-pat-fallback"></a>

## GitHub fine-grained PAT (required standard worker-hook credential)

**Auth method:** a fine-grained personal access token (prefix `github_pat_`),
supplied as `GH_TOKEN` to the daemon for the standard `bh-before-run` worker hook. The
PAT is required by that hook; only its BWS secret locator is optional. It does
**not** replace GitHub App provisioning: the daemon always requires App ID, installation
ID, and a private key from the selected `bws` or `file` provider. Mint the worker PAT
at <https://github.com/settings/personal-access-tokens/new> under a dedicated bot/machine
account — never a personal account.

The daemon threads this PAT by value only into `Orchestrator.hook_env` for the
`bh-before-run` subprocess. That hook validates the PAT, fetches the branch base, and
rebases locally. Daemon pushes, labels, pull requests, ruleset reads/bypass, and CI reads
continue to use the separately minted App installation token under `Identity.APP`.

**Required fine-grained PAT permissions:**

| Operation | Fine-grained permission |
|---|---|
| Fetch the remote branch base in `before_run` | Contents: Read |
| Baseline (granted automatically) | Metadata: Read |

Not granted: Contents write, Issues, Pull requests, Actions, Workflows, Administration,
Secrets, Checks, or any org-level scope. Those daemon operations remain App-authorized.

**Provisioning:**

- **Vault fetch:** set `BWS_GH_TOKEN_SECRET_ID` in `${BH_PROJECT_ROOT}/.bh/config.env`; `bootstrap_secrets()` fetches the PAT from Bitwarden at startup and retains it by value for the worker hook without writing it to `os.environ`.
- **Direct environment:** supply `GH_TOKEN` through external secret provisioning — an
  explicit env value wins over the vault fetch. This supplies only the worker hook; the
  GitHub App configuration remains mandatory.

For a BWS-free systemd deployment, externally provision
`/etc/bh-daemon/worker.env` (owner `root`, mode `0600`) with a `GH_TOKEN` assignment,
then reference it from a service drop-in with
`EnvironmentFile=/etc/bh-daemon/worker.env`. This supplies the required worker PAT
alongside the separately mounted App PEM. The installer does not create this file or
drop-in. Never put the token in a committed unit, `.bh/config.env`, or any repository
file. See the complete [BWS-free service example](smoke-test-daemon.md#manual--reference).

**Validated by:** `validate_github_token()` (`src/baton_harness/_auth.py`) — reads `GH_TOKEN`/`GITHUB_TOKEN`, rejects classic `ghp_` PATs and anything not prefixed `github_pat_`, then runs a live `gh api user` capability self-test (retried up to `_MAX_RETRIES` = 2 times on transient failures — rate-limits, gateway errors, DNS/TLS — before failing closed; permanent 401/403 failures raise immediately). This runs at the top of the `bh-before-run` hook (`src/baton_harness/before_run.py`), i.e. once per worker turn, not at daemon startup — a **different gate, on a different credential, in a different execution context** than `validate_daemon_token()` above. Known limitation: fine-grained PATs expose no scope-introspection API (unlike classic PATs' `X-OAuth-Scopes` response header), so this gate verifies token *type* and *reachability* only, not the exact granted permission set — that verification is the operator's responsibility at mint time.

## Bitwarden Secrets Manager

**Auth method:** `BWS_ACCESS_TOKEN`, an operator-supplied Bitwarden Secrets Manager
machine-account access token. It is needed when the App-key provider is `bws` or either
optional BWS secret locator is configured. The daemon completes all selected vault reads,
then removes `BWS_ACCESS_TOKEN` from `os.environ` in a `finally` block on every bootstrap
success or failure path. Worker subprocesses cannot inherit it.

**What it's used to fetch**, by Bitwarden Secrets UUID:

| Secret ID env var | Required? | Fetches |
|---|---|---|
| `BWS_PEM_SECRET_ID` | Required only for provider `bws` | The GitHub App's RSA private key (PEM) |
| `BWS_GH_TOKEN_SECRET_ID` | Optional | A fine-grained PAT — vault-fetched into `GH_TOKEN` when set and `GH_TOKEN` is not already in the environment (see [GitHub fine-grained PAT](#github-fine-grained-pat-fallback) above) |
| `BWS_HEARTBEAT_PING_URL_SECRET_ID` | Optional | The dead-man's-switch heartbeat ping URL — vault-fetched into `BH_HEARTBEAT_PING_URL` when set and that variable is not already in the environment. This is **not** the Slack webhook URL: `BH_HEARTBEAT_PING_URL` is a GET target polled once per heartbeat tick by `src/baton_harness/chain/daemon/poll.py`, distinct from `BH_SLACK_WEBHOOK_URL` (a POST target, see [Slack](#slack) below), which has no Bitwarden-vaulted form — it is supplied directly as an env var only. |

Bootstrap resolves and validates the complete provider matrix before mutating the
environment or making a GitHub request. It fetches configured optional PAT/heartbeat
secrets while the BWS token is available, loads the selected App key once, and proves
the PEM by signing a JWT. Its `finally` block scrubs the BWS token on every bootstrap
exit, before the first installation token is minted and validated. The installation
token remains a by-value credential and is never written to `os.environ`.

**Consuming code:** `src/baton_harness/chain/app_auth.py` (`bootstrap_secrets`,
`build_installation_token_provider`, `main`).

**Provisioning:** [docs/repository-onboarding.md §3](repository-onboarding.md#3-bws_access_token--export-only-when-bws-is-configured) covers conditionally exporting `BWS_ACCESS_TOKEN` for an interactive session, and the `/etc/bh-daemon/secrets.env` (mode `600`) pattern for a persistent BWS-backed deployment.

## Anthropic / Claude Code

**Auth method:** OAuth via a mounted `~/.claude/.credentials.json` volume — the subscription auth flow, established once by running `claude` interactively. `ANTHROPIC_API_KEY` is **explicitly forbidden**: the daemon refuses to start if it is set.

- Gate **G3b** (`src/baton_harness/chain/reconcile.py`) checks `ANTHROPIC_API_KEY` is absent at every daemon startup — fatal, critical alert, `sys.exit(1)` if present. A non-empty key means per-token billing is active, which contradicts the subscription-only cost model.
- Gate **G3c** (`src/baton_harness/chain/reconcile.py`) checks the OAuth credential file is present and readable — structural check only (presence + `open()` succeeds); contents are never read, decoded, or logged. Fatal if absent or unreadable.

**Consumed by:** the Claude Code worker subprocess turn-loop, under `Identity.WORKER` (see [docs/harness-design.md §12](harness-design.md#12-two-identity-subprocess-auth-model-identity-broker-implemented--issue-222)).

**Provisioning:** run `claude` once interactively after installation (`docs/system-setup.md §1`). On a server, `~/.claude/.credentials.json` must be an explicit credential-volume mount.

## `Identity.WORKER`

The unprivileged spawn identity used for the Claude Code worker subprocess and other
unprivileged chain spawns (git branch ops, sandboxed tool calls). It strips all privileged
GitHub tokens plus `BH_GITHUB_APP_KEY_PROVIDER`, `BH_GITHUB_APP_PRIVATE_KEY_FILE`,
`BWS_ACCESS_TOKEN`, `BWS_PEM_SECRET_ID`, `BWS_APP_ID`, `BWS_INSTALLATION_ID`,
`BWS_GH_TOKEN_SECRET_ID`, and `BWS_HEARTBEAT_PING_URL_SECRET_ID` unconditionally. The
identity is additive-safe: requesting `WORKER` can only remove credentials and secret
locators from a subprocess environment, never grant them.

The vendored Claude runner and every lifecycle hook apply this filter at their actual
spawn boundaries. Only `before_run` restores explicitly supplied worker PAT overrides
after filtering; other hooks and Claude receive neither ambient GitHub tokens nor that
PAT override.

The full two-identity broker model — `Identity.APP` vs. `Identity.WORKER`, the `env_for()` single resolution point, and the AST-walking spawn guard that enforces every `chain/` subprocess declares an explicit identity — is documented in **[docs/harness-design.md §12 "Two-identity subprocess auth model (identity broker)"](harness-design.md#12-two-identity-subprocess-auth-model-identity-broker-implemented--issue-222)** (issue #222). That section is the authoritative source; this doc does not duplicate its mechanics.

## Slack

**What's implemented today (v1):** a plain incoming webhook, nothing more. `BH_SLACK_WEBHOOK_URL` is an optional env var; when set, `src/baton_harness/chain/escalation.py` POSTs a plain-text message to it as a best-effort notification. There is **no bot auth, no OAuth scopes, no Socket Mode connection** — none of that exists in the current implementation.

- The GitHub issue comment is the durable record and is always attempted first; Slack is notification-only. Any Slack POST failure is logged at WARNING and does not affect the return value or block the durable record.
- When `BH_SLACK_WEBHOOK_URL` is unset, Slack is silently skipped at escalation time — not fatal. If it and `BH_HEARTBEAT_PING_URL` are *both* unset, the daemon logs a one-time startup WARNING (`src/baton_harness/chain/daemon/poll.py`) that async failure-signal escalation is entirely unconfigured.
- No Bitwarden-vaulted form exists for the webhook URL itself (see the [Bitwarden Secrets Manager](#bitwarden-secrets-manager) table above) — it is supplied directly as an env var.

**What's designed but deferred (v2, not implemented):** [docs/architecture-spec.md](architecture-spec.md) describes a two-way Slack Bolt bot over Socket Mode — bot/app tokens, OAuth scopes, an outbound WebSocket connection, Block Kit interactive decision cards, and thread-reply handling. **None of this exists yet.** A reader following `architecture-spec.md` alone would look for bot tokens and OAuth scopes that are not part of the current implementation; treat that document's Slack sections as the aspirational v2 design, not a description of what a deployment needs to configure today. See `architecture-spec.md` §10 item 2 for the tracked status of that gap.

## Related documents

- [docs/harness-design.md §12](harness-design.md#12-two-identity-subprocess-auth-model-identity-broker-implemented--issue-222) — the two-identity broker mechanics (authoritative; not duplicated here)
- [docs/system-setup.md](system-setup.md) — machine-level CLI setup and auth (`gh auth login`, `claude` OAuth, conditional `bws` install)
- [docs/repository-onboarding.md](repository-onboarding.md) — repo/sandbox-level provisioning walkthrough (GitHub App creation, key-provider selection, ruleset provisioning)
- [docs/smoke-test-daemon.md](smoke-test-daemon.md) — provisioning runbook; live permission-verification command
