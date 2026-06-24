---
title: "Slice 3b — branch ruleset + force-pr-not-merge PreToolUse hook"
touches:
  - bin/provision-ruleset.sh
  - config/ruleset.main.json
  - config/ruleset.feature.json
  - src/baton_harness/hooks/force_pr_not_merge.py
  - src/baton_harness/hooks/__init__.py
  - src/baton_harness/after_create.py
  - src/baton_harness/after_run.py
  - src/baton_harness/_cli.py
  - src/baton_harness/chain/ruleset_status.py
  - pyproject.toml
  - tests/test_force_pr_not_merge_hook.py
  - tests/test_provision_ruleset_idempotent.py
  - tests/test_after_create_drops_claude_settings.py
  - tests/test_after_run.py
  - tests/test_ruleset_status.py
  - tests/test_required_checks_match_ci_yml.py
  - tests/fixtures/ruleset.main.expected.json
  - tests/fixtures/ruleset.feature.expected.json
  - tests/fixtures/fake_gh/gh
  - config/WORKFLOW.md
  - docs/architecture-spec.md
  - README.md
skills_relevant:
  - python
  - hook-authoring
  - claude-github-tools:github-actions
  - claude-code-plugin-authoring
---

# Slice 3b — Merge boundary (branch ruleset + `force-pr-not-merge` hook) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the worker-identity → `main` merge path *deniable at the GitHub API layer* (the boundary) and *loudly stoppable at the worker tool layer* (defense-in-depth), so the harness's "harness merges, humans approve, workers never merge" invariant survives an attempted bypass.

**Architecture:** Two coordinated pieces — (A) a GitHub Repository Ruleset provisioned via `bin/provision-ruleset.sh` (idempotent, mirrors `bin/init-sandbox.sh`'s check-then-act pattern) that denies pushes/merges into `main` to anyone other than the daemon's App installation, plus a paired ruleset on `feature/**` that the daemon's App-installation identity bypasses so `chain/merge.py` keeps working; and (B) a Claude Code `PreToolUse` hook (`force-pr-not-merge`) installed into every worker worktree by `bh-after-create` via a generated `.claude/settings.json`. The hook matches `gh pr merge` and `gh api …/pulls/…/merge` Bash invocations, exits non-zero with a Slack-alertable stdout marker, AND drops a sentinel file under `.bh-state/` that `after_run` reads (the marker is for live-tail debugging; the sentinel is the load-bearing signal).

**Tech Stack:** Bash (provisioning script, hook entry point shim), Python 3.12 (hook logic, `bh-after-create` extension, `ruleset_status` module, tests via pytest), GitHub REST `/repos/{owner}/{repo}/rulesets` v3 API (`X-GitHub-Api-Version: 2022-11-28` — fetched 2026-06-23 via [GitHub Docs: Repository rulesets](https://docs.github.com/en/rest/repos/rules?apiVersion=2022-11-28)), Claude Code `PreToolUse` hooks ([Anthropic Docs: Hooks](https://docs.anthropic.com/en/docs/claude-code/hooks) — fetched 2026-06-23).

## Live-docs verification log (this revision pass)

The two API-shape claims that drive Tasks 2 and 3 were verified live during this revision pass via `WebFetch` against the GitHub Docs portal. Quotes are verbatim; URL + date are cited so a downstream verifier can re-fetch.

1. **`ruleset_id` parameter type — confirmed integer, not name.** Per [GET /repos/{owner}/{repo}/rulesets/{ruleset_id}](https://docs.github.com/en/rest/repos/rules?apiVersion=2022-11-28) (fetched 2026-06-23): "`ruleset_id` (integer) (required) The ID of the ruleset." Name-string lookup is not supported. Therefore B1's two-step pattern is mandatory: `GET /repos/{owner}/{repo}/rulesets` → filter list by `name` field → `GET /repos/.../rulesets/<numeric-id>` for the per-ID detail call.
2. **`actor_id` for `actor_type: Integration` — docs ambiguous.** Per the same page (fetched 2026-06-23), the docs state only: "`actor_id` (integer or null) The ID of the actor that can bypass a ruleset. Required for Integration, RepositoryRole, Team, and User actor types." The page does NOT disambiguate App ID vs Installation ID for `Integration`. We treat **App ID** as the canonical value (consistent with `chain/app_auth.py:L94-L125` using App ID as JWT `iss`, the canonical authenticated-as-App identifier) and add a runtime preflight in `bin/provision-ruleset.sh` that fetches `GET /app` and cross-checks `BH_GITHUB_APP_ID` before writing, plus a slice-3c live probe to detect a misclassification deterministically. See Task 2 Step 5.
3. **`actor_type: RepositoryRole` admin role ID — docs do NOT enumerate.** Same page, same fetch date: the docs state `RepositoryRole` is a valid `actor_type` value but do NOT provide a numeric mapping for Admin/Maintain/Write/Triage/Read. The widely-cited value `5 = Admin` (used by community ruleset templates) is **unverified by official docs as of 2026-06-23**. C6 resolution: include the admin bypass entry in `config/ruleset.main.json` with the value pinned to `5`, BUT add a runtime preflight + a clearly documented operator-override path (Task 2 Step 6) so a wrong default doesn't lock the admin out of `main`.

## Global Constraints

- **Boundary, not boundary alone.** Per `docs/architecture-spec.md:L155-L163` (the §3 hook table), `force-pr-not-merge` is Dial 1 / Layer 5 *defense-in-depth*. The **ruleset is the boundary**; the hook is the loud-stop signal. Both must ship in this slice — neither is optional.
- **Never block the legitimate daemon path.** `chain/merge.py:L455-L581` (`merge_issue_branch`) is the only legitimate merge — into `feature/<slug>` only, never `main` (guard at `chain/merge.py:L505-L510`). The ruleset config MUST permit the daemon-identity's `git merge --no-ff` into `feature/<slug>` and MUST block worker-identity merges into both `feature/<slug>` and `main`.
- **App ID vs Installation ID — two distinct integers, both required (B3 fix).** The harness needs BOTH because they encode different things and address different GitHub APIs:
  - **`BH_GITHUB_APP_ID`** — the App's numeric identifier returned from `GET /app` (visible at `https://github.com/settings/apps/<slug>` in the App settings URL and used as JWT `iss` in `chain/app_auth.py:build_app_jwt:L94-L125`). Used in the ruleset's `bypass_actors[].actor_id` for `actor_type: "Integration"`.
  - **`BH_GITHUB_APP_INSTALLATION_ID`** — the per-installation numeric identifier returned from `GET /repos/{owner}/{repo}/installation`. Used by `mint_installation_token` (`chain/app_auth.py:L133-L155`) to mint the installation access token.
  - **Both are required env vars for `bin/provision-ruleset.sh`** (the App ID for the bypass actor, the Installation ID is consumed by `app_auth.py` at runtime and is not directly written into the ruleset — it is loaded for cross-consistency only).
  - **Preflight enforcement:** `bin/provision-ruleset.sh` calls `gh api /app --jq .id` BEFORE writing the ruleset and refuses to proceed if `BH_GITHUB_APP_ID` does not match. This catches a mistaken Installation ID before it writes a non-functional ruleset.
- **Idempotency is mandatory and uses the list-then-by-ID API shape (B1 fix).** `bin/provision-ruleset.sh` MUST follow the `bin/init-sandbox.sh:L156-L194` check-then-act pattern, but the lookup is **two-step** because the GitHub Rulesets REST API `GET /repos/{owner}/{repo}/rulesets/{ruleset_id}` takes a numeric **ID** parameter, not a name string ([verified live 2026-06-23](https://docs.github.com/en/rest/repos/rules?apiVersion=2022-11-28) — verbatim: "`ruleset_id` (integer) (required) The ID of the ruleset"). Passing the string name returns 404 silently and would cause every re-run to POST a duplicate. The correct shape: (a) `GET /repos/{owner}/{repo}/rulesets` returns a list (each item has integer `id` and string `name` per the verified List endpoint response schema, fetched 2026-06-23) — filter by `name` to find the numeric `id`; (b) `GET /repos/{owner}/{repo}/rulesets/<id>` to fetch current state for diff; (c) `PUT /repos/{owner}/{repo}/rulesets/<id>` if drift, OR `POST /repos/{owner}/{repo}/rulesets` if no matching name was found. The same list-and-filter pattern is required in `ruleset_status.py` (Task 3).
- **Required-checks names are pinned.** The ruleset's `required_status_checks` rule MUST reference the exact job names hardcoded at `src/baton_harness/chain/merge.py:L105-L109` and produced by `.github/workflows/ci.yml:L16,L31,L43` — `Lint (ruff)`, `Type check (mypy)`, `Test (pytest)` (verified by Read at planning time 2026-06-23). Drift between this list and `merge.py` is a CI gate the Test plan enforces.
- **`config/WORKFLOW.md` hook category is unchanged.** Slice 3b's `force-pr-not-merge` is a **Claude Code `PreToolUse` hook** registered per-worktree via `.claude/settings.json` — it is NOT a Python-baton-hook in the `WORKFLOW.md:L13-L16` `after_create` / `before_run` / `after_run` category. The two categories MUST remain distinct in docs and code.
- **`bh-after-create` runs in the worktree as `$PWD`.** `src/baton_harness/after_create.py:L18-L26` documents the contract: cwd = freshly-created worktree directory. Slice 3b extends this hook to write `.claude/settings.json` at `$PWD/.claude/settings.json` before returning success.
- **Worker-tried-merge signalling is via a sentinel file (B2 fix).** `after_run.py:_classify()` (`src/baton_harness/after_run.py:L207-L323`, verified by Read at planning time 2026-06-23) takes NO arguments and does NOT receive worker stderr — it derives outcome entirely from `_run([...])` git/gh inspection. Therefore the hook signals "worker tried to merge" by writing a sentinel file `${PWD}/.bh-state/worker-tried-merge` (the `.bh-state/` directory parallels the existing `.symphony/state.json` convention at `chain/daemon.py:L738`, but lives under the worktree cwd because `after_run` runs there). `after_run._classify()` checks the sentinel as its FIRST step, before any git inspection — sentinel presence takes precedence over all other classifications. The stderr `BH_WORKER_TRIED_MERGE:` marker is retained as a live-tail debugging aid but is NOT load-bearing — only the sentinel drives behavior.
- **No live ruleset writes in CI.** Tests use a fake `gh` shim (per the `merge.py` `_run` pattern at `src/baton_harness/chain/merge.py:L173-L191`) — CI never touches the real sandbox repo's ruleset. The end-to-end smoke probe lives in slice 3c (out of scope here).
- **Slice 3c is out of scope.** The live merge-denial probe (worker container attempts `gh pr merge`, observes denial + escalation) is slice 3c. Slice 3b ships the mechanism + unit tests + provisioning script + signal file; slice 3c proves it works end-to-end against the sandbox.
- **#144 (preflight gate) consumes the signal, but is out of scope.** Slice 3b emits a readable signal — `src/baton_harness/chain/ruleset_status.py::ruleset_is_provisioned(owner, repo, gh_runner) -> RulesetStatus` returning one of `MATCH | DRIFT | ABSENT | ERROR` — that #144's daemon-startup preflight will call. Slice 3b adds the function + tests; #144 adds the call site.
- **Venv path is platform-resolved (C1 fix).** All Python invocations from bash scripts use a resolver mirroring `src/baton_harness/after_create.py:L99-L106`:
  ```bash
  _PYTHON="${HARNESS_DIR}/.venv/Scripts/python.exe"
  [[ ! -x "$_PYTHON" ]] && _PYTHON="${HARNESS_DIR}/.venv/bin/python"
  [[ ! -x "$_PYTHON" ]] && _PYTHON="python3"
  ```
- **`BH_VENV` is mandatory at worktree creation time (C4 fix).** If `BH_VENV` is unset when `bh-after-create` runs, it is a fatal misconfiguration — workers without the PreToolUse hook would silently lose defense-in-depth. `_write_claude_settings` returns non-zero and `err()` (not `log()`) when `BH_VENV` is absent.

---

## Open questions resolved in this plan

The brief listed 8 open questions; resolutions below are baked into the tasks. Each cites the source for the resolution.

1. **One ruleset or two? → Two.** Per the GitHub Rulesets API ([fetched 2026-06-23](https://docs.github.com/en/rest/repos/rules?apiVersion=2022-11-28)), a single ruleset has a single `bypass_actors` list. Because the daemon's App-installation identity needs **bypass on `feature/**` but NOT on `main`**, the cleanest representation is two rulesets — `harness-main-no-merge` (target: `~DEFAULT_BRANCH`; bypass list: admin-role only) and `harness-feature-daemon-only` (target: `refs/heads/feature/*`; bypass list: the App installation). One ruleset with branch-scoped bypass logic is not supported by the API. Trade-off: two rulesets is slightly more provisioning surface but matches the API's actual shape; collapsing them later (if GitHub adds branch-scoped bypass) is a one-line PUT change.
2. **Bypass actor identity. → The daemon's App + an explicit admin bypass on `main`.** The matrix is:
   | identity | `main` | `feature/<slug>` |
   |---|---|---|
   | Human repo admin (you) | allowed via explicit `RepositoryRole` admin bypass on the `harness-main-no-merge` ruleset (see C6 note below) | allowed (no rule restricts you on `feature/<slug>`) |
   | Daemon (App via `actor_type: Integration` + `BH_GITHUB_APP_ID`) | **denied** (never merges to `main`) | **allowed** (only legitimate `feature/<slug>` merger) |
   | Worker (PAT) | **denied** | **denied** |

   **C6 admin-lockout safeguard.** Active repository rulesets do NOT auto-bypass repository administrators — per the live-docs check above, the official Rulesets REST docs list `RepositoryRole` as a valid `actor_type` but do NOT enumerate which numeric `actor_id` is "Admin". To avoid locking the repo admin out of `main`, `config/ruleset.main.json` includes a `bypass_actors` entry of shape `{"actor_type": "RepositoryRole", "actor_id": 5, "bypass_mode": "always"}` — `5` is the community-cited Admin value, but it is **unverified by official docs**. To handle a wrong default safely:

   1. `bin/provision-ruleset.sh` exposes an env var `BH_ADMIN_ROLE_ID` (default `5`) that the operator can override before running provisioning if their org maps Admin to a different integer.
   2. The provisioning script logs the resolved `actor_id` it is about to write so the operator can verify before the ruleset becomes active.
   3. If a wrong value is written, the operator can revert via the GitHub Web UI (Settings → Rulesets) using their admin role, which is unaffected by classic branch-protection inheritance. (Per the GitHub Docs page on managing rulesets, the Settings UI for rulesets is gated by repo-admin permission, not by the ruleset itself — fetched 2026-06-23 via [Managing rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/creating-rulesets-for-a-repository).)

   The slice-3c live probe will exercise the admin-bypass path against the real sandbox and pin the correct integer in a follow-up issue if `5` turns out to be wrong.
3. **Provisioning surface. → Bash under `bin/provision-ruleset.sh` + checked-in JSON under `config/`.** Mirrors `bin/init-sandbox.sh`, reuses its check-then-act idiom, no new Python CLI to maintain. Auditability comes from the checked-in JSON config files (`config/ruleset.main.json`, `config/ruleset.feature.json`) — the script is a thin applier. Trade-off accepted: Python CLI would be more testable, but the script's logic is tiny (list rulesets → filter by name → diff → PUT/POST) and unit-testable via shim-injected `gh` (Task 2).
4. **Ruleset config source. → Checked-in JSON.** Two reasons: (a) auditability — reviewing a change to the ruleset diffs JSON, not embedded heredocs; (b) idempotency check trivially diffs API GET result against the file. Inline heredocs were rejected because they obscure the diff in code review.
5. **Hook script implementation. → Python (`src/baton_harness/hooks/force_pr_not_merge.py`) with a `bh-force-pr-not-merge` console-script entry point.** Rationale: matches the `bh-after-create` / `bh-before-run` / `bh-after-run` pattern (`pyproject.toml` already declares `[project.scripts]`), keeps test surface in pytest (not bats), and Claude Code hooks accept any executable. Bash was considered but loses pytest leverage. Registration: `bh-after-create` writes `.claude/settings.json` per Anthropic's hook-config schema ([fetched 2026-06-23](https://docs.anthropic.com/en/docs/claude-code/hooks)) pointing at `$BH_VENV/bin/bh-force-pr-not-merge`.
6. **Hook signalling. → Sentinel file + stderr marker (B2 fix).** The load-bearing signal is `${PWD}/.bh-state/worker-tried-merge` (an empty file whose mere presence indicates the hook fired). The stderr marker `BH_WORKER_TRIED_MERGE: …` is kept for live-tail debugging but is NOT consumed by `after_run`. Rationale: `after_run._classify()` runs in a separate process after the worker turn ends, derives all state from current git/gh inspection, and has no path to the worker's captured stderr. A sentinel file in a known location under the worktree cwd is the only reliable cross-process signal that matches the existing hook contract.
7. **#144 consumption contract. → `ruleset_is_provisioned(owner, repo, gh_runner) -> RulesetStatus`.** Implemented in `src/baton_harness/chain/ruleset_status.py` (new module). Returns enum: `MATCH` (both rulesets present & content-equal to checked-in JSON), `DRIFT` (rulesets present but config differs), `ABSENT` (one or both rulesets missing), `ERROR` (API call failed). #144 will call this at daemon startup; if status != `MATCH`, daemon refuses to start. Slice 3b implements the function + tests, NOT the daemon-startup wiring.
8. **CI verification of idempotency. → Shim-injected `gh`.** `tests/test_provision_ruleset_idempotent.py` invokes `bin/provision-ruleset.sh` with `PATH` prefixed by a fixture dir containing a fake `gh` that records calls to a log file and returns canned JSON. The shim simulates BOTH endpoint shapes (list endpoint + by-ID GET) per B1 fix. Three cases: empty (no rulesets) → 2 POSTs; identical (list returns matching names, by-ID GET returns canonical body) → 0 writes; drift (by-ID GET returns mutated body) → 1 PUT. Real-sandbox verification is slice 3c.

---

## File structure

### Created files (slice 3b new surface)

| File | Responsibility |
|---|---|
| `bin/provision-ruleset.sh` | Idempotent applier: lists rulesets, filters by name, GETs by id, PUTs on drift / POSTs on absence. |
| `config/ruleset.main.json` | Ruleset config targeting `~DEFAULT_BRANCH`. Admin-only bypass (RepositoryRole=5). Rules: required PR + required status checks (the three CI jobs). |
| `config/ruleset.feature.json` | Ruleset config targeting `refs/heads/feature/*`. Bypass: daemon App (id placeholder filled at provision time from `BH_GITHUB_APP_ID`). Rules: deletion + force-push protection only (no required-PR; daemon pushes already-merged refs directly — see one-line comment in the file). |
| `src/baton_harness/hooks/__init__.py` | Package marker — new sub-package for Claude Code hooks (distinct from Python-baton-hooks like `after_create.py`). One-line docstring explaining the distinction. |
| `src/baton_harness/hooks/force_pr_not_merge.py` | The hook logic: read Claude Code `PreToolUse` payload from stdin, regex-match against `Bash` tool calls, drop sentinel + exit non-zero on match. |
| `src/baton_harness/chain/ruleset_status.py` | `RulesetStatus` enum + `ruleset_is_provisioned()`. The contract #144 consumes. Uses list-then-by-id API shape (B1). |
| `tests/test_force_pr_not_merge_hook.py` | Pytest for the hook: positive matches, negatives, bypass attempts, sentinel-file assertion, URL-first regex case (C3). |
| `tests/test_provision_ruleset_idempotent.py` | Pytest for the bash provisioning script (subprocess + fake-`gh` PATH shim simulating list + by-id endpoints). |
| `tests/test_after_create_drops_claude_settings.py` | Pytest verifying `bh-after-create` writes a well-formed `.claude/settings.json`. |
| `tests/test_ruleset_status.py` | Pytest for `ruleset_is_provisioned` enum returns under MATCH/DRIFT/ABSENT/ERROR, including pre-existing-with-stale-id case (B1). |
| `tests/fixtures/ruleset.main.expected.json` | Canonical expected ruleset state for the GET-comparison test. |
| `tests/fixtures/ruleset.feature.expected.json` | Same, for the feature ruleset. |

### Modified files

| File | Change |
|---|---|
| `src/baton_harness/after_create.py` | After dependency install, write `$PWD/.claude/settings.json` with the `PreToolUse` registration pointing at `$BH_VENV/bin/bh-force-pr-not-merge`. `BH_VENV` absence is FATAL (C4). |
| `src/baton_harness/after_run.py` | Add `WORKER_TRIED_MERGE` to `RunOutcome`; `_classify()` checks `.bh-state/worker-tried-merge` sentinel as its FIRST step; `_reconcile_labels` routes the new outcome to `blocked` + emits escalation alert. |
| `src/baton_harness/_cli.py` | Add a small helper `claude_settings_json_for_worktree(venv_root: Path) -> dict` (kept here so `after_create` and tests share one source of truth for the JSON shape). |
| `pyproject.toml` | Add `bh-force-pr-not-merge = "baton_harness.hooks.force_pr_not_merge:main"` to `[project.scripts]`. |
| `config/WORKFLOW.md` | Add a note (after the existing `hooks:` block) clarifying that Claude Code `PreToolUse` hooks (slice 3b) are a separate category installed per-worktree by `bh-after-create`. |
| `docs/architecture-spec.md` | Update §3.5 hook table row for `force-pr-not-merge`: change "Action" cell. Add a one-line note under §6 noting the ruleset is the boundary, **referencing issue #157 and the merge PR — NOT the plan file path** (N2). |
| `README.md` | Add a "GitHub repository ruleset (sandbox setup)" sub-section under existing setup docs, documenting how to invoke `bin/provision-ruleset.sh` and the required env vars (`BH_REPO_OWNER`, `BH_REPO_NAME`, `BH_GITHUB_APP_ID`, `BH_GITHUB_APP_INSTALLATION_ID`) — both App vars are required (B3). |

---

## Sequencing (read this before starting tasks)

Order is locked because later tasks depend on earlier tasks' outputs:

1. **Task 1** — config schema + fixtures: write the canonical `ruleset.*.json` first; all later tasks consume these. Locks the data shape.
2. **Task 2** — `provision-ruleset.sh` with shim-injected `gh` tests (using list+by-id endpoint shape per B1): now we can produce the rulesets idempotently.
3. **Task 3** — `ruleset_status.py` (the #144 contract): reuses the config files from Task 1; uses the same list+by-id shape per B1.
4. **Task 4** — the hook script itself (`force_pr_not_merge.py`): independent of 1–3. Includes sentinel-file write (B2) and URL-first regex form (C3).
5. **Task 5** — `after_create` extension to drop `.claude/settings.json`: requires the entry-point name from Task 4. `BH_VENV` absence is fatal (C4).
6. **Task 6** — `after_run` extends `_classify()` to check the sentinel (B2): grounded in actual `after_run.py` shape.
7. **Task 7** — docs + WORKFLOW.md + README update (uses issue/PR refs, not plan path — N2).
8. **Task 8** — CI gate: `tests/test_required_checks_match_ci_yml.py` (uses `yaml.safe_load`, not regex — C2).

---

### Task 1: Ruleset config schema + fixtures

**Files:**
- Create: `config/ruleset.main.json`
- Create: `config/ruleset.feature.json`
- Create: `tests/fixtures/ruleset.main.expected.json`
- Create: `tests/fixtures/ruleset.feature.expected.json`

**Interfaces:**
- Consumes: nothing (entry task).
- Produces: two canonical ruleset JSON documents. Schema follows GitHub REST `POST /repos/{owner}/{repo}/rulesets` body shape ([fetched 2026-06-23](https://docs.github.com/en/rest/repos/rules?apiVersion=2022-11-28)). Two placeholders are substituted at apply time by `bin/provision-ruleset.sh`:
  - **`__BH_GITHUB_APP_ID__`** — the App ID for the daemon Integration bypass in `config/ruleset.feature.json`. Sourced from `$BH_GITHUB_APP_ID` and cross-checked against `GET /app` at apply time (see Task 2 Step 5 preflight).
  - **`__BH_ADMIN_ROLE_ID__`** — the numeric `RepositoryRole` id for the admin role in `config/ruleset.main.json`. Default `5` per § "Open questions resolved" item 2; operator may override via `BH_ADMIN_ROLE_ID` env var at provision time.

- [ ] **Step 1: Write `config/ruleset.main.json`**

The C6 admin-bypass entry is included. Defaults reflect the community-cited integer; the value is operator-overridable at provision time.

```json
{
  "name": "harness-main-no-merge",
  "target": "branch",
  "enforcement": "active",
  "bypass_actors": [
    {
      "actor_id": "__BH_ADMIN_ROLE_ID__",
      "actor_type": "RepositoryRole",
      "bypass_mode": "always"
    }
  ],
  "conditions": {
    "ref_name": {
      "include": ["~DEFAULT_BRANCH"],
      "exclude": []
    }
  },
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {
      "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 0,
        "dismiss_stale_reviews_on_push": false,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_review_thread_resolution": false
      }
    },
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": false,
        "required_status_checks": [
          {"context": "Lint (ruff)"},
          {"context": "Type check (mypy)"},
          {"context": "Test (pytest)"}
        ]
      }
    }
  ]
}
```

- [ ] **Step 2: Write `config/ruleset.feature.json`**

The feature ruleset deliberately omits `required_status_checks` — the legitimate daemon path is `git push <already-merged-ref>` to `feature/<slug>` (after a CI-green PR has been validated and merged into the feature branch elsewhere in the chain). A PR status-check gate would block that push because the daemon is pushing already-merged commits, not opening a PR. The comment in the JSON is informational only — JSON has no native comments, so we encode it as a synthetic `"_comment"` key that the diff-comparison logic in Task 2 and Task 3 ignores (it filters to a fixed `_COMPARE_KEYS` allowlist that does not include `_comment`):

```json
{
  "_comment": "required_status_checks intentionally omitted — daemon pushes already-merged feature/<slug> refs via git push; PR status-check gate does not apply to that path. See docs/architecture-spec.md §3 and src/baton_harness/chain/merge.py:merge_issue_branch.",
  "name": "harness-feature-daemon-only",
  "target": "branch",
  "enforcement": "active",
  "bypass_actors": [
    {
      "actor_id": "__BH_GITHUB_APP_ID__",
      "actor_type": "Integration",
      "bypass_mode": "always"
    }
  ],
  "conditions": {
    "ref_name": {
      "include": ["refs/heads/feature/*"],
      "exclude": []
    }
  },
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"}
  ]
}
```

- [ ] **Step 3: Copy both to `tests/fixtures/` as `*.expected.json`**

These are the byte-for-byte canonical states the idempotency tests will compare against. They duplicate `config/*.json` intentionally — keeping them in `tests/fixtures/` means the test detects accidental config drift even if someone changes `config/ruleset.main.json` without updating the fixture (the test compares fixture to API GET; the provisioning script compares `config/` to API GET).

```bash
cp config/ruleset.main.json tests/fixtures/ruleset.main.expected.json
cp config/ruleset.feature.json tests/fixtures/ruleset.feature.expected.json
```

- [ ] **Step 4: Commit**

```bash
git add config/ruleset.main.json config/ruleset.feature.json \
        tests/fixtures/ruleset.main.expected.json tests/fixtures/ruleset.feature.expected.json
git commit -m "feat(#157): add canonical ruleset configs (slice 3b task 1)"
```

---

### Task 2: `bin/provision-ruleset.sh` idempotent applier (list-then-by-id shape)

**Files:**
- Create: `bin/provision-ruleset.sh`
- Create: `tests/test_provision_ruleset_idempotent.py`
- Create: `tests/fixtures/fake_gh/gh` (executable shim used by the test)

**Interfaces:**
- Consumes: `config/ruleset.main.json`, `config/ruleset.feature.json` from Task 1.
- Produces: script that, given `BH_REPO_OWNER` / `BH_REPO_NAME` / `BH_GITHUB_APP_ID` / `BH_GITHUB_APP_INSTALLATION_ID`, idempotently brings the live rulesets into agreement with the JSON configs. Optional `BH_ADMIN_ROLE_ID` (default `5`). Exit codes: `0` on success-or-no-op, `1` on any drift it could not fix, `2` on missing env / invalid config / preflight mismatch.

- [ ] **Step 1: Write the failing test `tests/test_provision_ruleset_idempotent.py`**

Four cases — case 4 is new (B1: pre-existing ruleset with stale ID, proves the list-filter path).

```python
"""Slice 3b — bin/provision-ruleset.sh idempotency.

Drives the bash script with a fake gh on PATH that records every API call
to a log file and returns canned JSON for BOTH endpoints the script hits:

  - LIST: GET /repos/<owner>/<repo>/rulesets  (returns array of {id,name})
  - BY-ID: GET /repos/<owner>/<repo>/rulesets/<id>  (returns single object)

Five cases:

1. Empty state (LIST returns []) -> exactly 2 POSTs (one per ruleset name).
2. Identical state (LIST returns both names with ids 11/22; BY-ID GETs
   return the canonical bodies) -> zero writes (idempotent re-run).
3. Drift in feature ruleset (BY-ID GET returns mutated body) -> exactly
   1 PUT, targeting /rulesets/22.
4. Pre-existing ruleset with STALE numeric ID (LIST returns name with
   id=99; BY-ID GET on id=99 returns matching body) -> zero writes
   AND the call log shows the script GET-d /rulesets/99, not /rulesets/22.
   Proves the list-then-by-id path is used (NOT a name-string lookup).
5. Preflight App-ID mismatch (gh api /app returns id=111 but
   BH_GITHUB_APP_ID=222) -> exit 2, no writes.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parents[1]
SCRIPT = HARNESS / "bin" / "provision-ruleset.sh"
FAKE_GH_DIR = HARNESS / "tests" / "fixtures" / "fake_gh"


def _invoke(
    tmp_path: Path,
    canned_state_dir: Path,
    *,
    app_id: str = "111",
    preflight_app_id: str = "111",
    admin_role_id: str = "5",
) -> tuple[int, Path]:
    """Run the provisioning script with the fake gh on PATH.

    Returns (returncode, gh_call_log_path).
    """
    log_path = tmp_path / "gh_calls.jsonl"
    # The fake gh reads preflight_app_id to drive its /app response.
    (canned_state_dir / "app_id.txt").write_text(preflight_app_id)
    env = {
        **os.environ,
        "PATH": f"{FAKE_GH_DIR}{os.pathsep}{os.environ.get('PATH', '')}",
        "BH_REPO_OWNER": "fake-owner",
        "BH_REPO_NAME": "fake-repo",
        "BH_GITHUB_APP_ID": app_id,
        "BH_GITHUB_APP_INSTALLATION_ID": "999999",
        "BH_ADMIN_ROLE_ID": admin_role_id,
        "BH_FAKE_GH_LOG": str(log_path),
        "BH_FAKE_GH_CANNED_DIR": str(canned_state_dir),
    }
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )
    return proc.returncode, log_path


def _calls(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text().splitlines()
        if line.strip()
    ]


def _writes(calls: list[dict]) -> list[dict]:
    return [c for c in calls if c["method"] in ("POST", "PUT")]


def test_empty_state_creates_both_rulesets(tmp_path):
    canned = tmp_path / "canned"
    canned.mkdir()
    # LIST returns empty array.
    (canned / "list.body").write_text("[]")

    rc, log = _invoke(tmp_path, canned)
    assert rc == 0, f"script exited {rc}"
    writes = _writes(_calls(log))
    assert len(writes) == 2, f"expected 2 POSTs, got {writes}"
    assert all(c["method"] == "POST" for c in writes)
    assert {c["ruleset_name"] for c in writes} == {
        "harness-main-no-merge",
        "harness-feature-daemon-only",
    }


def test_identical_state_is_noop(tmp_path):
    canned = tmp_path / "canned"
    canned.mkdir()
    # LIST returns both rulesets with canonical numeric IDs.
    (canned / "list.body").write_text(json.dumps([
        {"id": 11, "name": "harness-main-no-merge"},
        {"id": 22, "name": "harness-feature-daemon-only"},
    ]))
    # BY-ID bodies are the canonical configs with placeholders rendered.
    main_body = json.loads((HARNESS / "config" / "ruleset.main.json").read_text())
    main_body["bypass_actors"][0]["actor_id"] = 5  # admin role
    (canned / "byid_11.body").write_text(json.dumps(main_body))

    feature_body = json.loads(
        (HARNESS / "config" / "ruleset.feature.json").read_text()
    )
    feature_body["bypass_actors"][0]["actor_id"] = 111  # app id
    (canned / "byid_22.body").write_text(json.dumps(feature_body))

    rc, log = _invoke(tmp_path, canned)
    assert rc == 0, f"script exited {rc}"
    assert _writes(_calls(log)) == [], "expected zero writes"


def test_drift_in_feature_triggers_single_put(tmp_path):
    canned = tmp_path / "canned"
    canned.mkdir()
    (canned / "list.body").write_text(json.dumps([
        {"id": 11, "name": "harness-main-no-merge"},
        {"id": 22, "name": "harness-feature-daemon-only"},
    ]))
    main_body = json.loads((HARNESS / "config" / "ruleset.main.json").read_text())
    main_body["bypass_actors"][0]["actor_id"] = 5
    (canned / "byid_11.body").write_text(json.dumps(main_body))

    feature_drifted = json.loads(
        (HARNESS / "config" / "ruleset.feature.json").read_text()
    )
    feature_drifted["bypass_actors"] = []  # mutated (workers could merge)
    (canned / "byid_22.body").write_text(json.dumps(feature_drifted))

    rc, log = _invoke(tmp_path, canned)
    assert rc == 0
    writes = _writes(_calls(log))
    assert len(writes) == 1
    assert writes[0]["method"] == "PUT"
    assert writes[0]["url"].endswith("/rulesets/22"), writes[0]["url"]


def test_preexisting_with_stale_id_uses_list_filter_path(tmp_path):
    """B1 regression: prove list-then-by-id, not name-string lookup."""
    canned = tmp_path / "canned"
    canned.mkdir()
    # Pre-existing rulesets with arbitrary IDs the script must discover.
    (canned / "list.body").write_text(json.dumps([
        {"id": 99, "name": "harness-main-no-merge"},
        {"id": 77, "name": "harness-feature-daemon-only"},
    ]))
    main_body = json.loads((HARNESS / "config" / "ruleset.main.json").read_text())
    main_body["bypass_actors"][0]["actor_id"] = 5
    (canned / "byid_99.body").write_text(json.dumps(main_body))
    feature_body = json.loads(
        (HARNESS / "config" / "ruleset.feature.json").read_text()
    )
    feature_body["bypass_actors"][0]["actor_id"] = 111
    (canned / "byid_77.body").write_text(json.dumps(feature_body))

    rc, log = _invoke(tmp_path, canned)
    assert rc == 0
    calls = _calls(log)
    # Zero writes (matched).
    assert _writes(calls) == []
    # Critically: the GET URLs must reference the DISCOVERED ids, not names.
    get_urls = [c["url"] for c in calls if c["method"] == "GET"]
    assert any(u.endswith("/rulesets/99") for u in get_urls), get_urls
    assert any(u.endswith("/rulesets/77") for u in get_urls), get_urls
    # And the GET URLs must NOT be by-name.
    assert not any("harness-main-no-merge" in u for u in get_urls if "/rulesets/" in u)
    assert not any("harness-feature-daemon-only" in u for u in get_urls if "/rulesets/" in u)


def test_preflight_app_id_mismatch_aborts(tmp_path):
    canned = tmp_path / "canned"
    canned.mkdir()
    (canned / "list.body").write_text("[]")
    rc, log = _invoke(
        tmp_path, canned, app_id="222", preflight_app_id="111"
    )
    assert rc == 2, f"expected exit 2 for mismatch, got {rc}"
    assert _writes(_calls(log)) == [], "must not write on preflight failure"
```

(Note: `test_empty_state_creates_both_rulesets` (N1 fix) docstring and assertions now consistently describe POST behavior — no "exactly 2 PUTs" wording.)

- [ ] **Step 2: Run test to verify it fails (script does not yet exist)**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_provision_ruleset_idempotent.py -v`
Expected: 5 FAILs with `bin/provision-ruleset.sh` not found or `bash` invocation errors.

- [ ] **Step 3: Write the fake `gh` shim at `tests/fixtures/fake_gh/gh`**

The shim simulates four endpoints: `GET /app` (preflight), `GET /repos/o/r/rulesets` (LIST), `GET /repos/o/r/rulesets/<id>` (BY-ID), and write endpoints. URL parsing is structural — it matches `/rulesets/<integer>` for by-id and `/rulesets` (no trailing segment) for list.

```bash
#!/usr/bin/env bash
# tests/fixtures/fake_gh/gh — recording shim for provision-ruleset.sh tests.
#
# Simulates the GitHub Rulesets REST API endpoints used by the script:
#   GET /app                                          (preflight)
#   GET /repos/<owner>/<repo>/rulesets                (LIST)
#   GET /repos/<owner>/<repo>/rulesets/<integer-id>   (BY-ID)
#   POST /repos/<owner>/<repo>/rulesets               (create)
#   PUT /repos/<owner>/<repo>/rulesets/<integer-id>   (update)
#
# Canned responses live in $BH_FAKE_GH_CANNED_DIR:
#   app_id.txt          — body for GET /app (just the integer)
#   list.body           — body for the LIST endpoint
#   byid_<id>.body      — body for GET /rulesets/<id>

set -euo pipefail

_log() {
  printf '%s\n' "$1" >> "${BH_FAKE_GH_LOG}"
}

# Default to GET; override on -X / --method.
_method="GET"
_args=("$@")
for i in "${!_args[@]}"; do
  case "${_args[$i]}" in
    --method|-X) _method="${_args[$((i+1))]}" ;;
  esac
done

# Find the URL path (first arg matching repos/* or "/app" or the bare "app").
_url=""
for a in "${_args[@]}"; do
  case "${a}" in
    repos/*|/app|app|/repos/*) _url="${a#/}"; break ;;
  esac
done

# Classify the request.
_endpoint=""
_ruleset_id=""
_ruleset_name=""

if [[ "${_url}" == "app" ]]; then
  _endpoint="get_app"
elif [[ "${_url}" =~ ^repos/[^/]+/[^/]+/rulesets/([0-9]+)$ ]]; then
  _endpoint="get_byid"
  _ruleset_id="${BASH_REMATCH[1]}"
elif [[ "${_url}" =~ ^repos/[^/]+/[^/]+/rulesets$ ]]; then
  if [[ "${_method}" == "POST" ]]; then
    _endpoint="post_create"
  else
    _endpoint="get_list"
  fi
fi

# On POST/PUT with --input, capture name from the body for the call log.
if [[ "${_method}" == "POST" || "${_method}" == "PUT" ]]; then
  _body="$(cat 2>/dev/null || true)"
  _ruleset_name="$(printf '%s' "${_body}" | python3 -c \
    'import json,sys; d=json.loads(sys.stdin.read() or "{}"); print(d.get("name",""))' \
    2>/dev/null || true)"
fi

_log "$(python3 -c "
import json
print(json.dumps({
    'method': '${_method}',
    'url': '${_url}',
    'endpoint': '${_endpoint}',
    'ruleset_id': '${_ruleset_id}',
    'ruleset_name': '${_ruleset_name}',
}))
")"

# Serve canned responses.
case "${_endpoint}" in
  get_app)
    printf '{"id":%s}\n' "$(cat "${BH_FAKE_GH_CANNED_DIR}/app_id.txt")"
    exit 0
    ;;
  get_list)
    cat "${BH_FAKE_GH_CANNED_DIR}/list.body"
    exit 0
    ;;
  get_byid)
    _body_file="${BH_FAKE_GH_CANNED_DIR}/byid_${_ruleset_id}.body"
    if [[ -f "${_body_file}" ]]; then
      cat "${_body_file}"
      exit 0
    fi
    echo "gh: Not Found (HTTP 404)" >&2
    exit 1
    ;;
  post_create|*)
    # Write echoes a success body with a fabricated id.
    printf '{"id":1,"name":"%s"}\n' "${_ruleset_name}"
    exit 0
    ;;
esac
```

Make it executable:
```bash
chmod +x tests/fixtures/fake_gh/gh
```

- [ ] **Step 4: Write `bin/provision-ruleset.sh` (list-then-by-id, preflight, platform-resolved Python)**

```bash
#!/usr/bin/env bash
# bin/provision-ruleset.sh — Idempotent ruleset applier (slice 3b).
#
# Brings the live GitHub Repository Rulesets into agreement with the
# checked-in JSON configs at config/ruleset.main.json and
# config/ruleset.feature.json. Mirrors the check-then-act pattern from
# bin/init-sandbox.sh.
#
# Two-step ID lookup (per GitHub Rulesets REST API):
#   1. GET /repos/<owner>/<repo>/rulesets  -> list of {id,name}; filter by name
#   2. GET /repos/<owner>/<repo>/rulesets/<id>  -> single ruleset detail
# (Name-string lookup at endpoint 2 returns 404 silently — must not be used.)
#
# Required environment variables:
#   BH_REPO_OWNER                    GitHub repository owner.
#   BH_REPO_NAME                     GitHub repository name.
#   BH_GITHUB_APP_ID                 Numeric App ID for ruleset bypass
#                                    (NOT the same as installation id).
#   BH_GITHUB_APP_INSTALLATION_ID    Required for app_auth.py at runtime.
#                                    Validated for presence only here.
#
# Optional environment variables:
#   BH_ADMIN_ROLE_ID                 Numeric RepositoryRole id for admin
#                                    bypass on main. Default: 5 (community-
#                                    cited; not officially documented).
#
# Exit codes:
#   0  success — rulesets match or were brought into agreement
#   1  drift could not be fixed
#   2  missing env / invalid config / preflight App-ID mismatch

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bin/provision-ruleset.sh [--help|-h]

Idempotently provisions the harness-main-no-merge and
harness-feature-daemon-only rulesets in the target sandbox repo.
EOF
}

if [[ "${1-}" == "--help" || "${1-}" == "-h" ]]; then
  usage
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ---------------------------------------------------------------------------
# Python resolver (C1) — mirrors after_create.py:L99-L106.
# ---------------------------------------------------------------------------
_PYTHON="${HARNESS_DIR}/.venv/Scripts/python.exe"
[[ ! -x "${_PYTHON}" ]] && _PYTHON="${HARNESS_DIR}/.venv/bin/python"
[[ ! -x "${_PYTHON}" ]] && _PYTHON="python3"

# ---------------------------------------------------------------------------
# Env validation.
# ---------------------------------------------------------------------------
_missing=()
for v in BH_REPO_OWNER BH_REPO_NAME BH_GITHUB_APP_ID BH_GITHUB_APP_INSTALLATION_ID; do
  if [[ -z "${!v:-}" ]]; then
    _missing+=("${v}")
  fi
done
if [[ ${#_missing[@]} -gt 0 ]]; then
  echo "provision-ruleset: missing env vars: ${_missing[*]}" >&2
  exit 2
fi

REPO_SLUG="${BH_REPO_OWNER}/${BH_REPO_NAME}"
ADMIN_ROLE_ID="${BH_ADMIN_ROLE_ID:-5}"

# ---------------------------------------------------------------------------
# Preflight: cross-check BH_GITHUB_APP_ID against GET /app (B3).
# ---------------------------------------------------------------------------
_live_app_id="$(gh api /app --jq .id)"
if [[ "${_live_app_id}" != "${BH_GITHUB_APP_ID}" ]]; then
  echo "provision-ruleset: PREFLIGHT FAILURE — BH_GITHUB_APP_ID=${BH_GITHUB_APP_ID}" >&2
  echo "  but 'gh api /app --jq .id' returned ${_live_app_id}." >&2
  echo "  BH_GITHUB_APP_ID must be the App ID from https://github.com/settings/apps/<slug>," >&2
  echo "  NOT the Installation ID. Aborting before writing ruleset." >&2
  exit 2
fi
echo "provision-ruleset: preflight OK — App ID ${BH_GITHUB_APP_ID} confirmed via GET /app"
echo "provision-ruleset: using admin RepositoryRole actor_id=${ADMIN_ROLE_ID}"

# ---------------------------------------------------------------------------
# Render each config (substitute placeholders).
# ---------------------------------------------------------------------------
_render_config() {
  local src="$1"
  sed -e "s|\"__BH_GITHUB_APP_ID__\"|${BH_GITHUB_APP_ID}|g" \
      -e "s|\"__BH_ADMIN_ROLE_ID__\"|${ADMIN_ROLE_ID}|g" \
      "${src}"
}

# ---------------------------------------------------------------------------
# List + filter helper: discover numeric id for a ruleset name.
# Echoes the numeric id, or empty string if not found.
# ---------------------------------------------------------------------------
_lookup_id() {
  local target_name="$1"
  gh api "repos/${REPO_SLUG}/rulesets" --jq \
    ".[] | select(.name == \"${target_name}\") | .id" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Apply one ruleset: list+filter, GET-by-id, compare, PUT/POST.
# ---------------------------------------------------------------------------
_apply_ruleset() {
  local name="$1"
  local desired_path="$2"
  local desired
  desired="$(_render_config "${desired_path}")"

  local existing_id
  existing_id="$(_lookup_id "${name}")"

  if [[ -z "${existing_id}" ]]; then
    echo "provision-ruleset: ${name} absent — POST-ing"
    printf '%s' "${desired}" | gh api \
      --method POST \
      "repos/${REPO_SLUG}/rulesets" \
      --input -
    return
  fi

  local current_body
  current_body="$(gh api "repos/${REPO_SLUG}/rulesets/${existing_id}" 2>/dev/null || true)"

  if "${_PYTHON}" -c '
import json, sys
desired = json.loads(sys.argv[1])
current = json.loads(sys.argv[2])
# Compare a fixed allowlist of structural keys; the synthetic _comment and
# server-managed fields (id, source, source_type, _links, node_id, *_at,
# current_user_can_bypass) are excluded.
keys = ["name","target","enforcement","bypass_actors","conditions","rules"]
sys.exit(0 if all(desired.get(k) == current.get(k) for k in keys) else 1)
' "${desired}" "${current_body}" 2>/dev/null; then
    echo "provision-ruleset: ${name} matches (id=${existing_id}) — no-op"
    return 0
  fi
  echo "provision-ruleset: ${name} drift detected (id=${existing_id}) — PUT-ing"
  printf '%s' "${desired}" | gh api \
    --method PUT \
    "repos/${REPO_SLUG}/rulesets/${existing_id}" \
    --input -
}

_apply_ruleset "harness-main-no-merge" "${HARNESS_DIR}/config/ruleset.main.json"
_apply_ruleset "harness-feature-daemon-only" "${HARNESS_DIR}/config/ruleset.feature.json"

echo "provision-ruleset: complete"
```

`chmod +x bin/provision-ruleset.sh`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_provision_ruleset_idempotent.py -v`
Expected: 5 PASS.

- [ ] **Step 6: Commit**

```bash
git add bin/provision-ruleset.sh tests/test_provision_ruleset_idempotent.py \
        tests/fixtures/fake_gh/gh
git commit -m "feat(#157): bin/provision-ruleset.sh + idempotency tests (slice 3b task 2)"
```

---

### Task 3: `ruleset_status.py` — the #144 contract (list-then-by-id shape, tightened error heuristic)

**Files:**
- Create: `src/baton_harness/chain/ruleset_status.py`
- Create: `tests/test_ruleset_status.py`

**Interfaces:**
- Consumes: an injected `runner: Callable[[list[str]], subprocess.CompletedProcess[str]]` that takes `gh` args and returns a CompletedProcess. Defaults to a thin wrapper around `subprocess.run`. #156 (when it lands) can swap the default to GhRunner.
- Produces: `RulesetStatus` enum + `ruleset_is_provisioned(owner: str, repo: str, *, app_id: str, runner: Callable | None = None, admin_role_id: int = 5) -> RulesetStatus`. The function uses the LIST endpoint to discover ids, then GET-by-id to fetch content — same shape as `provision-ruleset.sh`.

- [ ] **Step 1: Write the failing test `tests/test_ruleset_status.py`**

```python
"""Slice 3b — ruleset_status.ruleset_is_provisioned() unit tests.

Drives the function with a hand-rolled fake runner that returns canned
CompletedProcess objects for each gh call. Coverage:

  - MATCH  : LIST + both BY-ID GETs return canonical bodies
  - DRIFT  : both rulesets present but feature ruleset content differs
  - ABSENT : LIST is missing the feature ruleset name
  - ERROR  : LIST call returns non-zero with a 500-class error
  - tight  : a proxy-banner stderr that happens to contain "404"
             does NOT trigger ABSENT — only HTTP 404 / gh-Not-Found does (C5).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from baton_harness.chain.ruleset_status import (
    RulesetStatus,
    ruleset_is_provisioned,
)

HARNESS = Path(__file__).resolve().parents[1]
MAIN_CFG = HARNESS / "config" / "ruleset.main.json"
FEATURE_CFG = HARNESS / "config" / "ruleset.feature.json"


def _ok(body: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=body, stderr="")


def _notfound() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="gh: Not Found (HTTP 404)"
    )


def _http_500() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="HTTP 500: server error"
    )


def _proxy_404_banner() -> subprocess.CompletedProcess[str]:
    """A successful response whose body coincidentally mentions 404."""
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout="[]", stderr="X-Cache: 404-miss"
    )


def _make_runner(list_proc, byid_main_proc=None, byid_feature_proc=None):
    def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
        url = next((a for a in args if "rulesets" in a or "/app" in a), "")
        if url.endswith("/rulesets"):
            return list_proc
        if "/rulesets/11" in url:
            return byid_main_proc or _ok("{}")
        if "/rulesets/22" in url:
            return byid_feature_proc or _ok("{}")
        raise AssertionError(f"unexpected gh call: {args}")
    return _run


def _render_main():
    body = json.loads(MAIN_CFG.read_text())
    body["bypass_actors"][0]["actor_id"] = 5
    return body


def _render_feature(app_id=111):
    body = json.loads(FEATURE_CFG.read_text())
    body["bypass_actors"][0]["actor_id"] = app_id
    return body


def test_match():
    list_body = json.dumps([
        {"id": 11, "name": "harness-main-no-merge"},
        {"id": 22, "name": "harness-feature-daemon-only"},
    ])
    runner = _make_runner(
        _ok(list_body),
        _ok(json.dumps(_render_main())),
        _ok(json.dumps(_render_feature())),
    )
    assert ruleset_is_provisioned(
        "o", "r", app_id="111", runner=runner
    ) is RulesetStatus.MATCH


def test_drift():
    list_body = json.dumps([
        {"id": 11, "name": "harness-main-no-merge"},
        {"id": 22, "name": "harness-feature-daemon-only"},
    ])
    feature_drifted = _render_feature()
    feature_drifted["bypass_actors"] = []
    runner = _make_runner(
        _ok(list_body),
        _ok(json.dumps(_render_main())),
        _ok(json.dumps(feature_drifted)),
    )
    assert ruleset_is_provisioned(
        "o", "r", app_id="111", runner=runner
    ) is RulesetStatus.DRIFT


def test_absent_feature():
    list_body = json.dumps([
        {"id": 11, "name": "harness-main-no-merge"},
    ])
    runner = _make_runner(
        _ok(list_body),
        _ok(json.dumps(_render_main())),
    )
    assert ruleset_is_provisioned(
        "o", "r", app_id="111", runner=runner
    ) is RulesetStatus.ABSENT


def test_error_on_list_5xx():
    runner = _make_runner(_http_500())
    assert ruleset_is_provisioned(
        "o", "r", app_id="111", runner=runner
    ) is RulesetStatus.ERROR


def test_proxy_404_banner_does_not_false_positive(tmp_path):
    """C5 regression: a non-error response whose stderr happens to contain
    '404' (e.g. a proxy banner) must NOT be classified as ABSENT."""
    runner = _make_runner(_proxy_404_banner())
    # LIST returns ok with empty body -> ABSENT (both rulesets absent), but
    # NOT via the _is_not_found heuristic — via the empty list itself.
    # Re-check that returncode==0 path is treated as success, not error.
    result = ruleset_is_provisioned("o", "r", app_id="111", runner=runner)
    assert result is RulesetStatus.ABSENT  # because the LIST body is "[]"
    # If the heuristic were broad (the old `"404" in stderr`), this would
    # have returned ERROR. We assert it does NOT.
```

- [ ] **Step 2: Run test to verify it fails (module does not exist)**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_ruleset_status.py -v`
Expected: 5 FAILs with `ImportError: cannot import name 'RulesetStatus'`.

- [ ] **Step 3: Write `src/baton_harness/chain/ruleset_status.py`**

```python
"""Slice 3b — read-only ruleset status check for the #144 preflight gate.

Returns a four-state enum that a future daemon-startup gate will use to
decide whether the merge boundary is in place before processing any
issues. This module is INSPECTION-ONLY — it never mutates GitHub state.
The provisioning side lives in bin/provision-ruleset.sh.

API shape (same as bin/provision-ruleset.sh):
    1. GET /repos/<owner>/<repo>/rulesets  -> list of {id,name}
    2. Filter by name to discover numeric id for each expected ruleset.
    3. GET /repos/<owner>/<repo>/rulesets/<id>  -> single ruleset detail.

The Rulesets BY-ID endpoint takes a numeric id, NOT a name string
(verified against the live docs on 2026-06-23 — see plan task 3).
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from enum import Enum, auto
from pathlib import Path

_log = logging.getLogger(__name__)

# Compared keys — the subset of a ruleset object whose drift signals a
# real config divergence (timestamps, server-managed ids, the synthetic
# _comment key, and links are excluded on purpose).
_COMPARE_KEYS = (
    "name",
    "target",
    "enforcement",
    "bypass_actors",
    "conditions",
    "rules",
)

# Path resolution: this module lives at src/baton_harness/chain/ —
# config/ is three parents up from the module file (chain -> baton_harness
# -> src -> harness root).
_HARNESS_ROOT = Path(__file__).resolve().parents[3]
_MAIN_CFG = _HARNESS_ROOT / "config" / "ruleset.main.json"
_FEATURE_CFG = _HARNESS_ROOT / "config" / "ruleset.feature.json"

_MAIN_NAME = "harness-main-no-merge"
_FEATURE_NAME = "harness-feature-daemon-only"


class RulesetStatus(Enum):
    """The four states the #144 preflight gate consumes.

    Attributes:
        MATCH: Both rulesets are present and content-equal to the
            checked-in JSON (placeholders substituted).
        DRIFT: Both rulesets are present but at least one differs.
        ABSENT: One or both rulesets are missing.
        ERROR: A gh call failed with a non-404 error (network, auth, 5xx).
    """

    MATCH = auto()
    DRIFT = auto()
    ABSENT = auto()
    ERROR = auto()


def _default_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Default gh-runner: thin wrapper around ``subprocess.run``.

    Args:
        args: Args to pass to ``gh`` (NOT including ``gh`` itself).

    Returns:
        CompletedProcess with captured stdout/stderr.
    """
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _render_main_config(admin_role_id: int) -> dict:
    body = json.loads(_MAIN_CFG.read_text())
    body["bypass_actors"][0]["actor_id"] = admin_role_id
    return {k: v for k, v in body.items() if k != "_comment"}


def _render_feature_config(app_id: str) -> dict:
    body = json.loads(_FEATURE_CFG.read_text())
    # The placeholder is a JSON string in the file; replace with integer.
    body["bypass_actors"][0]["actor_id"] = int(app_id)
    return {k: v for k, v in body.items() if k != "_comment"}


def _is_not_found(proc: subprocess.CompletedProcess[str]) -> bool:
    """Tight 404 detector (C5).

    Only matches the exact stderr forms ``gh api`` itself emits — not the
    looser ``"404" in stderr`` heuristic, which false-positives on proxy
    headers and cache banners that happen to contain the digits 404.
    """
    if proc.returncode == 0:
        return False
    return "HTTP 404" in proc.stderr or "gh: Not Found" in proc.stderr


def _is_error(proc: subprocess.CompletedProcess[str]) -> bool:
    return proc.returncode != 0 and not _is_not_found(proc)


def _content_equal(desired: dict, current: dict) -> bool:
    # Strip the synthetic _comment from current as well, in case a future
    # operator manually copies it into the live ruleset.
    current_clean = {k: v for k, v in current.items() if k != "_comment"}
    return all(desired.get(k) == current_clean.get(k) for k in _COMPARE_KEYS)


def ruleset_is_provisioned(
    owner: str,
    repo: str,
    *,
    app_id: str,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    admin_role_id: int = 5,
) -> RulesetStatus:
    """Inspect both rulesets in the target repo and classify the state.

    Args:
        owner: Repository owner (org or user login).
        repo: Repository name (no owner prefix).
        app_id: Numeric GitHub App ID, used to substitute the placeholder
            in the feature ruleset before comparing. NOT the installation
            id.
        runner: Optional callable that takes a list of ``gh`` args and
            returns a CompletedProcess. Defaults to a thin
            ``subprocess.run(["gh", *args], …)`` wrapper. Tests inject
            a fake runner; #156 (when it lands) can swap in GhRunner.
        admin_role_id: Numeric RepositoryRole id for the admin bypass on
            main. Default 5 (community-cited; #157 plan flags it as
            unverified against official docs).

    Returns:
        ``RulesetStatus.MATCH`` if both rulesets are present and
        content-equal to ``config/ruleset.*.json``;
        ``RulesetStatus.DRIFT`` if both present but content differs;
        ``RulesetStatus.ABSENT`` if at least one is missing from the list;
        ``RulesetStatus.ERROR`` if any gh call returns a non-404 error.
    """
    run = runner or _default_runner

    list_proc = run(["api", f"repos/{owner}/{repo}/rulesets"])
    if _is_error(list_proc):
        return RulesetStatus.ERROR

    try:
        listed = json.loads(list_proc.stdout) if list_proc.stdout.strip() else []
    except json.JSONDecodeError as exc:
        _log.warning("ruleset LIST returned non-JSON body: %s", exc)
        return RulesetStatus.ERROR

    name_to_id = {item["name"]: item["id"] for item in listed if "name" in item}

    if _MAIN_NAME not in name_to_id or _FEATURE_NAME not in name_to_id:
        return RulesetStatus.ABSENT

    main_id = name_to_id[_MAIN_NAME]
    feature_id = name_to_id[_FEATURE_NAME]

    main_proc = run(["api", f"repos/{owner}/{repo}/rulesets/{main_id}"])
    feature_proc = run(["api", f"repos/{owner}/{repo}/rulesets/{feature_id}"])

    if _is_not_found(main_proc) or _is_not_found(feature_proc):
        return RulesetStatus.ABSENT
    if _is_error(main_proc) or _is_error(feature_proc):
        return RulesetStatus.ERROR

    try:
        current_main = json.loads(main_proc.stdout)
        current_feature = json.loads(feature_proc.stdout)
    except json.JSONDecodeError as exc:
        _log.warning("ruleset BY-ID returned non-JSON body: %s", exc)
        return RulesetStatus.ERROR

    desired_main = _render_main_config(admin_role_id)
    desired_feature = _render_feature_config(app_id)

    if _content_equal(desired_main, current_main) and _content_equal(
        desired_feature, current_feature
    ):
        return RulesetStatus.MATCH
    return RulesetStatus.DRIFT
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_ruleset_status.py -v`
Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/baton_harness/chain/ruleset_status.py tests/test_ruleset_status.py
git commit -m "feat(#157): ruleset_status read-only contract for #144 (slice 3b task 3)"
```

---

### Task 4: `force_pr_not_merge` PreToolUse hook (sentinel + tightened regex)

**Files:**
- Create: `src/baton_harness/hooks/__init__.py`
- Create: `src/baton_harness/hooks/force_pr_not_merge.py`
- Modify: `pyproject.toml` (add console-script entry)
- Create: `tests/test_force_pr_not_merge_hook.py`

**Interfaces:**
- Consumes: a Claude Code `PreToolUse` JSON payload from stdin. Per Anthropic's hook docs ([fetched 2026-06-23](https://docs.anthropic.com/en/docs/claude-code/hooks)), `PreToolUse` payloads contain at minimum `{"tool_name": str, "tool_input": dict}`.
- Produces:
  - On any merge-pattern match: exit code `2` (block) + stderr marker `BH_WORKER_TRIED_MERGE: …` + sentinel file `${PWD}/.bh-state/worker-tried-merge` (created in cwd, with the `.bh-state/` dir created if absent). The sentinel is the load-bearing signal consumed by `after_run` (B2); the stderr marker is for live-tail debugging.
  - On no match: exit code `0` (allow).

- [ ] **Step 1: Write the failing test `tests/test_force_pr_not_merge_hook.py`**

Three sets of patterns (block / allow / non-Bash), URL-first form (C3), and sentinel-file assertion.

```python
"""Slice 3b — force_pr_not_merge PreToolUse hook unit tests.

Drives main() with stdin payloads modelled on Claude Code's PreToolUse
schema. The hook MUST do two things on a merge-pattern match:

  1. Exit non-zero with stderr beginning ``BH_WORKER_TRIED_MERGE:``
  2. Drop a sentinel file at ``${PWD}/.bh-state/worker-tried-merge``

The sentinel is the load-bearing signal that ``after_run._classify()``
reads — the stderr marker is live-tail debugging only.
"""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from baton_harness.hooks.force_pr_not_merge import main as hook_main


@pytest.fixture
def in_tmp_cwd(tmp_path, monkeypatch):
    """Run the hook with cwd set to tmp_path so sentinel paths are isolated."""
    monkeypatch.chdir(tmp_path)
    yield tmp_path


def _run(payload: dict) -> tuple[int, str]:
    """Invoke main() with the given payload on stdin, capture (rc, stderr)."""
    stdin = io.StringIO(json.dumps(payload))
    stderr_buf = io.StringIO()
    stdout_buf = io.StringIO()
    with redirect_stderr(stderr_buf), redirect_stdout(stdout_buf):
        rc = hook_main(stdin=stdin)
    return rc, stderr_buf.getvalue()


@pytest.mark.parametrize(
    "command",
    [
        # Direct gh pr merge — all flag arrangements.
        "gh pr merge 42",
        "gh pr merge --auto 42",
        "gh pr merge 42 --squash",
        # Flag-first form.
        "gh api -X PUT repos/o/r/pulls/42/merge",
        "gh api --method PUT repos/o/r/pulls/42/merge",
        # URL-first form (C3 regression).
        "gh api repos/o/r/pulls/42/merge -X PUT",
        "gh api repos/o/r/pulls/42/merge --method PUT",
        # curl direct API.
        "curl -X PUT https://api.github.com/repos/o/r/pulls/42/merge",
        "curl https://api.github.com/repos/o/r/pulls/42/merge -X PUT",
        # Piped / chained.
        "something | gh pr merge 42",
        "gh pr merge 42 && echo ok",
    ],
)
def test_blocks_merge_attempts(in_tmp_cwd, command):
    rc, stderr = _run(
        {"tool_name": "Bash", "tool_input": {"command": command}}
    )
    assert rc != 0, f"expected block for: {command!r}"
    assert stderr.startswith("BH_WORKER_TRIED_MERGE:"), stderr
    # Sentinel must exist (load-bearing signal for after_run).
    sentinel = in_tmp_cwd / ".bh-state" / "worker-tried-merge"
    assert sentinel.exists(), f"sentinel missing for: {command!r}"


@pytest.mark.parametrize(
    "command",
    [
        "gh pr create --draft --base feature/x --title T --body B",
        "gh pr view 42",
        "gh pr list",
        "gh pr status",
        "gh issue comment 42 --body hi",
        "git push -u origin HEAD",
    ],
)
def test_allows_legitimate_commands(in_tmp_cwd, command):
    rc, stderr = _run(
        {"tool_name": "Bash", "tool_input": {"command": command}}
    )
    assert rc == 0, f"unexpectedly blocked: {command!r}\nstderr={stderr}"
    assert stderr == ""
    sentinel = in_tmp_cwd / ".bh-state" / "worker-tried-merge"
    assert not sentinel.exists()


@pytest.mark.parametrize("tool", ["Read", "Edit", "Write", "Grep"])
def test_passes_through_non_bash_tools(in_tmp_cwd, tool):
    rc, stderr = _run({"tool_name": tool, "tool_input": {"file_path": "/x"}})
    assert rc == 0
    assert stderr == ""
    sentinel = in_tmp_cwd / ".bh-state" / "worker-tried-merge"
    assert not sentinel.exists()


def test_malformed_stdin_is_safe_default_allow(in_tmp_cwd):
    """If we cannot parse stdin, do NOT block — fail-open. The ruleset is
    the boundary; a parser bug here should not stop legitimate work."""
    stdin = io.StringIO("not-json")
    stderr_buf = io.StringIO()
    with redirect_stderr(stderr_buf):
        rc = hook_main(stdin=stdin)
    assert rc == 0
    sentinel = in_tmp_cwd / ".bh-state" / "worker-tried-merge"
    assert not sentinel.exists()
```

- [ ] **Step 2: Run test to verify it fails (module does not exist)**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_force_pr_not_merge_hook.py -v`
Expected: ImportError, all fails.

- [ ] **Step 3: Write `src/baton_harness/hooks/__init__.py`**

```python
"""Claude Code hooks installed per-worktree by ``bh-after-create``.

DISTINCT from the Python-baton-hooks in ``baton_harness.after_create`` /
``baton_harness.before_run`` / ``baton_harness.after_run`` — those fire
in the daemon's worker turn loop. The hooks in THIS sub-package are
``PreToolUse`` hooks registered in ``.claude/settings.json`` and fired by
Claude Code itself before every tool invocation.

See ``docs/architecture-spec.md`` §3.5 hook table for the canonical list.
"""
```

- [ ] **Step 4: Write `src/baton_harness/hooks/force_pr_not_merge.py`**

Detection uses three independent substring checks for the `gh api …pulls/N/merge` family (C3) — `gh api` present, `pulls/<digits>/merge` present, AND a PUT method indicator present, in ANY ORDER. This catches the URL-first form `gh api repos/o/r/pulls/42/merge --method PUT` that the original anchored regex missed.

```python
"""Slice 3b — ``force-pr-not-merge`` Claude Code PreToolUse hook.

Reads a Claude Code PreToolUse payload from stdin and:

  - On a merge-pattern match: drops sentinel file
    ``${PWD}/.bh-state/worker-tried-merge`` AND exits 2 (block) with a
    ``BH_WORKER_TRIED_MERGE:`` stderr marker.
  - Otherwise: exits 0 (allow).

The sentinel file is the LOAD-BEARING signal — ``after_run._classify()``
inspects it as its first step. The stderr marker is live-tail debugging
only.

Defense-in-depth Layer 5 mechanism per ``docs/architecture-spec.md``
§3.5; the GitHub Repository Ruleset is the actual boundary.

Payload shape (per Anthropic Docs, fetched 2026-06-23):
    {"tool_name": "Bash", "tool_input": {"command": "<the command>"}}
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import TextIO

_SENTINEL_DIR = ".bh-state"
_SENTINEL_NAME = "worker-tried-merge"

# Cheap direct match: `gh pr merge` in any form.
_RE_GH_PR_MERGE = re.compile(r"\bgh\s+pr\s+merge\b")

# Three independent substring checks for the gh-api family. Composing them
# in any-order beats the previous single-regex that required flags BEFORE
# the URL (C3 — `gh api repos/o/r/pulls/42/merge --method PUT` evaded).
_RE_GH_API = re.compile(r"\bgh\s+api\b")
_RE_PULLS_MERGE = re.compile(r"\bpulls/\d+/merge\b")
_RE_PUT_METHOD = re.compile(r"(?:\s|^)(?:-X\s+PUT|--method\s+PUT)\b")

# curl direct API — same any-order composition.
_RE_CURL = re.compile(r"\bcurl\b")
_RE_CURL_PUT = re.compile(
    r"(?:\s|^)(?:-X\s+PUT|--request\s+PUT)\b"
)

#: Maximum command length echoed in the marker (avoid runaway stderr).
_MAX_ECHO_LEN: int = 200


def _sanitise(command: str) -> str:
    s = re.sub(r"[\x00-\x1f]+", " ", command)
    if len(s) > _MAX_ECHO_LEN:
        s = s[:_MAX_ECHO_LEN] + "…"
    return s


def _match(command: str) -> str | None:
    """Return a short label identifying the matched pattern, or None."""
    if _RE_GH_PR_MERGE.search(command):
        return "gh-pr-merge"
    if (
        _RE_GH_API.search(command)
        and _RE_PULLS_MERGE.search(command)
        and _RE_PUT_METHOD.search(command)
    ):
        return "gh-api-pulls-merge"
    if (
        _RE_CURL.search(command)
        and _RE_PULLS_MERGE.search(command)
        and _RE_CURL_PUT.search(command)
    ):
        return "curl-pulls-merge"
    return None


def _drop_sentinel() -> None:
    """Create ``${PWD}/.bh-state/worker-tried-merge`` as an empty file.

    Failure is swallowed (logged to stderr but not fatal) — exiting 2 is
    still the contract that blocks the tool call, sentinel is a belt-and-
    braces signal for the daemon's after_run hook.
    """
    try:
        sentinel_dir = Path.cwd() / _SENTINEL_DIR
        sentinel_dir.mkdir(parents=True, exist_ok=True)
        (sentinel_dir / _SENTINEL_NAME).touch()
    except OSError as exc:
        print(
            f"force-pr-not-merge: WARNING: could not write sentinel: {exc}",
            file=sys.stderr,
        )


def main(*, stdin: TextIO | None = None) -> int:
    """Read PreToolUse payload from stdin; block on merge attempts.

    Args:
        stdin: Optional stdin override (used by tests). Defaults to
            ``sys.stdin``.

    Returns:
        ``0`` to allow tool invocation; ``2`` to block. The non-zero
        return + stderr text is the contract Claude Code uses to block.
    """
    src = stdin if stdin is not None else sys.stdin
    raw = src.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        # Fail-open: a parser bug here must NOT block legitimate work.
        # The ruleset is the real boundary. Log to stderr for debugging.
        print(
            "force-pr-not-merge: WARNING: stdin was not JSON — allowing",
            file=sys.stderr,
        )
        return 0

    tool = payload.get("tool_name", "")
    if tool != "Bash":
        return 0

    command = (payload.get("tool_input") or {}).get("command", "")
    if not isinstance(command, str):
        return 0

    label = _match(command)
    if label is None:
        return 0

    _drop_sentinel()
    print(
        f"BH_WORKER_TRIED_MERGE: tool=Bash matched_pattern={label} "
        f"command={_sanitise(command)}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Add the console-script to `pyproject.toml`**

Locate the existing `[project.scripts]` table and append (preserve existing entries):

```toml
[project.scripts]
# … existing entries (bh-daemon, bh-after-create, bh-before-run, bh-after-run) …
bh-force-pr-not-merge = "baton_harness.hooks.force_pr_not_merge:main"
```

Reinstall in editable mode so the new entry is materialised in the venv:

```bash
uv pip install --python ./.venv/Scripts/python.exe -e ".[dev]"
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_force_pr_not_merge_hook.py -v`
Expected: all PASS (including the two new URL-first cases, the sentinel-file assertions, and the curl URL-first case).

- [ ] **Step 7: Commit**

```bash
git add src/baton_harness/hooks/__init__.py \
        src/baton_harness/hooks/force_pr_not_merge.py \
        tests/test_force_pr_not_merge_hook.py pyproject.toml
git commit -m "feat(#157): force-pr-not-merge PreToolUse hook (slice 3b task 4)"
```

---

### Task 5: `bh-after-create` drops `.claude/settings.json` (BH_VENV absence is fatal)

**Files:**
- Modify: `src/baton_harness/after_create.py`
- Modify: `src/baton_harness/_cli.py` (add `claude_settings_json_for_worktree` helper)
- Create: `tests/test_after_create_drops_claude_settings.py`

**Interfaces:**
- Consumes: `BH_VENV` env var (already exported by `bin/run-daemon.sh:L65-L66`), points at the venv root containing `Scripts/bh-force-pr-not-merge` or `bin/bh-force-pr-not-merge`. **`BH_VENV` MUST be set** — if absent, `_write_claude_settings` returns non-zero and `bh-after-create` aborts the worktree creation. The worker without this hook would silently lose defense-in-depth (C4 fix).
- Produces: `$PWD/.claude/settings.json` in every newly-created worktree, registering the hook.

- [ ] **Step 1: Write the failing test**

```python
"""Slice 3b — bh-after-create writes .claude/settings.json with the
force-pr-not-merge PreToolUse hook registered, pointing at
$BH_VENV/{Scripts,bin}/bh-force-pr-not-merge.

C4: BH_VENV absence is FATAL — _write_claude_settings returns non-zero.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from baton_harness._cli import claude_settings_json_for_worktree


def test_settings_dict_shape(tmp_path):
    venv = tmp_path / "venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "bh-force-pr-not-merge").touch()
    settings = claude_settings_json_for_worktree(venv)
    assert "hooks" in settings
    pre = settings["hooks"]["PreToolUse"]
    assert isinstance(pre, list) and len(pre) == 1
    entry = pre[0]
    assert entry["matcher"] == "Bash"
    cmd = entry["hooks"][0]["command"]
    assert cmd.endswith("bh-force-pr-not-merge") or cmd.endswith(
        "bh-force-pr-not-merge.exe"
    )
    assert ("Scripts" in cmd) or ("/bin/" in cmd)


def test_after_create_writes_settings_file(tmp_path, monkeypatch):
    from baton_harness.after_create import _write_claude_settings

    worktree = tmp_path / "wt"
    worktree.mkdir()
    venv = tmp_path / "venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "bh-force-pr-not-merge").touch()

    rc = _write_claude_settings(issue=42, cwd=worktree, venv_root=venv)
    assert rc == 0

    out = worktree / ".claude" / "settings.json"
    assert out.exists()
    payload = json.loads(out.read_text())
    pre = payload["hooks"]["PreToolUse"]
    assert pre[0]["matcher"] == "Bash"


def test_bh_venv_absent_is_fatal(tmp_path, monkeypatch):
    """C4: a misconfigured daemon (BH_VENV unset) must NOT silently ship
    workers without the hook — the bh-after-create call must fail loudly
    so the operator notices at first worktree creation."""
    from baton_harness.after_create import _write_claude_settings_if_configured

    worktree = tmp_path / "wt"
    worktree.mkdir()
    monkeypatch.delenv("BH_VENV", raising=False)

    rc = _write_claude_settings_if_configured(issue=42, cwd=worktree)
    assert rc != 0, "BH_VENV absent must return non-zero (C4)"
    assert not (worktree / ".claude").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_after_create_drops_claude_settings.py -v`
Expected: FAIL — `claude_settings_json_for_worktree`, `_write_claude_settings`, `_write_claude_settings_if_configured` do not exist.

- [ ] **Step 3: Add `claude_settings_json_for_worktree` to `src/baton_harness/_cli.py`**

Append to that module (confirm the import block includes `from pathlib import Path`; add it if not):

```python
def claude_settings_json_for_worktree(venv_root: Path) -> dict:
    """Build the .claude/settings.json payload for a per-worker worktree.

    Registers the force-pr-not-merge PreToolUse hook so any worker
    `gh pr merge` (or moral equivalent) is loudly stopped before the
    GitHub Ruleset would have denied it at the API.

    Args:
        venv_root: Absolute path to the venv that contains the
            ``bh-force-pr-not-merge`` console script. Both Windows
            (``Scripts/``) and POSIX (``bin/``) layouts are probed.

    Returns:
        A dict ready to ``json.dumps`` into ``.claude/settings.json``.
    """
    win = venv_root / "Scripts" / "bh-force-pr-not-merge"
    win_exe = win.with_suffix(".exe")
    posix = venv_root / "bin" / "bh-force-pr-not-merge"
    if posix.exists():
        cmd = str(posix)
    elif win_exe.exists():
        cmd = str(win_exe)
    elif win.exists() or (venv_root / "Scripts").exists():
        cmd = str(win)
    else:
        cmd = str(posix)

    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {"type": "command", "command": cmd}
                    ],
                }
            ]
        }
    }
```

- [ ] **Step 4: Extend `src/baton_harness/after_create.py`**

Add the new helpers near the bottom of the file (above the `main` entry, if present). The C4-fixed shape splits the "must-have-BH_VENV" guard into its own helper so the test can hit it cleanly:

```python
import json
import os
from pathlib import Path

from baton_harness._cli import claude_settings_json_for_worktree


def _write_claude_settings(issue: int, cwd: Path, venv_root: Path) -> int:
    """Drop a per-worktree .claude/settings.json registering the
    force-pr-not-merge PreToolUse hook.

    Args:
        issue: Issue number (used in log prefix).
        cwd: The freshly-created worktree directory.
        venv_root: Absolute path to the harness venv.

    Returns:
        ``0`` on success; non-zero on filesystem error.
    """
    settings = claude_settings_json_for_worktree(venv_root)
    out_dir = cwd / ".claude"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "settings.json"
    try:
        out_path.write_text(json.dumps(settings, indent=2) + "\n")
    except OSError as exc:
        err(_HOOK, issue, f"failed to write .claude/settings.json: {exc}")
        return 1
    log(_HOOK, issue, f"wrote {out_path}")
    return 0


def _write_claude_settings_if_configured(issue: int, cwd: Path) -> int:
    """Drop .claude/settings.json or FAIL LOUDLY if BH_VENV is absent (C4).

    Args:
        issue: Issue number (used in log prefix).
        cwd: The freshly-created worktree directory.

    Returns:
        ``0`` on success; non-zero on misconfiguration or write failure.
        Specifically: BH_VENV absent returns ``1`` — a worker without the
        force-pr-not-merge hook would silently lose defense-in-depth, and
        the operator MUST notice at first worktree creation rather than at
        first merge attempt.
    """
    venv_root_env = os.environ.get("BH_VENV")
    if not venv_root_env:
        err(
            _HOOK,
            issue,
            "BH_VENV not set — refusing to create worktree without the "
            "force-pr-not-merge PreToolUse hook. Set BH_VENV in the "
            "daemon environment (bin/run-daemon.sh:L65-L66 normally "
            "exports it) and re-run.",
        )
        return 1
    return _write_claude_settings(
        issue=issue, cwd=cwd, venv_root=Path(venv_root_env)
    )
```

Then locate the existing `def main()` (or equivalent CLI entry) in `after_create.py` and add — after the dependency-install step succeeds — a call:

```python
    # Slice 3b — install the force-pr-not-merge PreToolUse hook so any
    # worker-side `gh pr merge` is stopped before the ruleset would have
    # denied it at the API layer. BH_VENV absence is FATAL (C4) — a
    # silent skip would ship workers without defense-in-depth.
    rc_settings = _write_claude_settings_if_configured(
        issue=issue, cwd=Path.cwd()
    )
    if rc_settings != 0:
        return rc_settings
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_after_create_drops_claude_settings.py -v`
Expected: PASS (3 tests including BH_VENV-absent-is-fatal).

- [ ] **Step 6: Commit**

```bash
git add src/baton_harness/after_create.py src/baton_harness/_cli.py \
        tests/test_after_create_drops_claude_settings.py
git commit -m "feat(#157): bh-after-create drops .claude/settings.json (slice 3b task 5)"
```

---

### Task 6: `after_run` reads sentinel file in `_classify()` (B2 — grounded in real shape)

**Files:**
- Modify: `src/baton_harness/after_run.py` (extend `RunOutcome` + `_classify` + `_reconcile_labels`)
- Modify: `tests/test_after_run.py` (add the new case)

**Interfaces (grounded by Read of `after_run.py` at planning time 2026-06-23):**

The real shape of `after_run.py` (verified):

- `RunOutcome` enum at L107-L129 with members `UNCOMMITTED_CHANGES`, `NO_COMMITS`, `COMMITTED_NO_PR`, `PR_OPENED`, `TRANSIENT_ERROR`.
- `_classify() -> RunOutcome` at L207-L323 takes NO arguments; derives state from `_run(["git", ...])` and `_run(["gh", ...])` calls only. **Does NOT receive any worker stderr.**
- `_reconcile_labels(issue, outcome) -> int` at L378-L595 dispatches on outcome.
- `_run(cmd)` at L148-L167 is the single subprocess seam.

The sentinel file path is `Path.cwd() / ".bh-state" / "worker-tried-merge"` — the same path the hook writes (Task 4). `after_run` runs in the worktree per the docstring at L57-L59, so `Path.cwd()` is correct.

**Produces:** a new `RunOutcome.WORKER_TRIED_MERGE` terminal state. `_classify()` checks the sentinel as its FIRST step (before any git inspection), and presence ⇒ `WORKER_TRIED_MERGE` regardless of other state. `_reconcile_labels` routes the new outcome to remove `agent-ready` + add `blocked` and emit a critical-severity escalation via the existing `baton_harness.chain.escalation.alert` function.

**Verified `alert()` signature (read at planning time 2026-06-23):** `src/baton_harness/chain/escalation.py:L192-L201` defines `alert(owner: str, repo: str, issue: int | None, summary: str, *, severity: Literal["info", "warn", "critical"], runlog: RunLog | None = None, kind: str = "block") -> bool`. `owner` and `repo` are positional and required — there is no `source=` kwarg. Step 7 reads `owner`/`repo` from the daemon-exported env vars `BH_REPO_OWNER` and `BH_REPO_NAME` (validated by `bin/run-daemon.sh:L81-L96`, consumed by `src/baton_harness/chain/cli.py:L120-L132`; `bh-after-run` inherits them as a child of the daemon process — no new env-var contract is introduced by this slice).

**Import contract (no `as` alias).** Step 5 imports as `from baton_harness.chain.escalation import alert` (bare name). Step 7 calls `alert(...)`. Step 2 patches via `monkeypatch.setattr(after_run, "alert", ...)`. All three must agree on the attribute name `alert` in `after_run`'s module namespace, or the patch silently no-ops and the runtime call raises `NameError`.

- [ ] **Step 1: Read `src/baton_harness/after_run.py` and `tests/test_after_run.py` end-to-end**

(Already done at planning time; the shape facts above are baked in. Re-read at execution to confirm nothing has shifted.)

- [ ] **Step 2: Write the failing test in `tests/test_after_run.py`**

Append the new tests. The sentinel-first invariant means the test must work even when git state would otherwise produce a different classification:

```python
def test_classify_returns_worker_tried_merge_on_sentinel(tmp_path, monkeypatch):
    """B2: presence of .bh-state/worker-tried-merge under cwd is terminal."""
    from baton_harness import after_run

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".bh-state").mkdir()
    (tmp_path / ".bh-state" / "worker-tried-merge").touch()

    # Sentinel takes precedence regardless of git state — patch _run so a
    # call from _classify would fail loudly if reached.
    def _no_git_calls_expected(cmd):
        raise AssertionError(
            f"_classify must short-circuit on sentinel; got call: {cmd}"
        )

    monkeypatch.setattr(after_run, "_run", _no_git_calls_expected)
    assert after_run._classify() is after_run.RunOutcome.WORKER_TRIED_MERGE


def test_classify_ignores_sentinel_outside_cwd(tmp_path, monkeypatch):
    """Sibling-cwd sentinel must NOT affect classification."""
    from baton_harness import after_run

    sibling = tmp_path / "other-worktree"
    sibling.mkdir()
    (sibling / ".bh-state").mkdir()
    (sibling / ".bh-state" / "worker-tried-merge").touch()

    target = tmp_path / "this-worktree"
    target.mkdir()
    monkeypatch.chdir(target)

    # Patch the rest of _classify so the test doesn't actually need a git
    # repo — return a clean status, no commits ahead, no PR.
    import subprocess

    def _fake(cmd):
        if cmd[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(cmd, 0, "abc123\n", "")
        if cmd[:2] == ["git", "cherry"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")  # no '+'
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(after_run, "_run", _fake)
    # Without sentinel under cwd, classify should fall through to NO_COMMITS.
    assert after_run._classify() is after_run.RunOutcome.NO_COMMITS


def test_reconcile_labels_for_worker_tried_merge(monkeypatch):
    """WORKER_TRIED_MERGE routes to blocked + escalation alert.

    The alert() call must match the real signature
    `alert(owner, repo, issue, summary, *, severity, ...)` —
    owner/repo are positional, read from BH_REPO_OWNER/BH_REPO_NAME.
    """
    from baton_harness import after_run

    # The daemon exports BH_REPO_OWNER/BH_REPO_NAME (bin/run-daemon.sh:L81-L96,
    # chain/cli.py:L120-L132); bh-after-run inherits them. The test sets
    # them explicitly because pytest does not run under the daemon.
    monkeypatch.setenv("BH_REPO_OWNER", "test-owner")
    monkeypatch.setenv("BH_REPO_NAME", "test-repo")

    calls = []
    monkeypatch.setattr(
        after_run, "_current_labels", lambda issue: ["agent-ready"]
    )

    def _fake_run(cmd):
        calls.append(cmd)
        import subprocess
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(after_run, "_run", _fake_run)

    alerts = []
    # Capture positional AND keyword args — the real signature has
    # positional owner/repo/issue/summary then keyword-only severity.
    def _capture_alert(*args, **kwargs):
        alerts.append({"args": args, "kwargs": kwargs})
        return True

    monkeypatch.setattr(after_run, "alert", _capture_alert, raising=False)

    rc = after_run._reconcile_labels(42, after_run.RunOutcome.WORKER_TRIED_MERGE)
    assert rc == 0
    # agent-ready was removed, blocked was added.
    cmds = [" ".join(c) for c in calls]
    assert any("--remove-label agent-ready" in c for c in cmds), cmds
    assert any("--add-label blocked" in c for c in cmds), cmds
    # Escalation fired with positional owner/repo/issue/summary + severity kw.
    assert len(alerts) == 1, alerts
    call = alerts[0]
    assert call["args"][0] == "test-owner", call
    assert call["args"][1] == "test-repo", call
    assert call["args"][2] == 42, call
    assert "worker attempted to merge" in call["args"][3], call
    assert call["kwargs"].get("severity") == "critical", call
    # The buggy `source=` kwarg must NOT be present.
    assert "source" not in call["kwargs"], call


def test_reconcile_labels_for_worker_tried_merge_missing_env_is_best_effort(monkeypatch):
    """B2 defensive path: if BH_REPO_OWNER/BH_REPO_NAME are absent, the
    alert call still proceeds with empty positional args and is caught by
    the except guard — escalation is best-effort by the module's existing
    discipline (see the except Exception in Step 7)."""
    from baton_harness import after_run

    monkeypatch.delenv("BH_REPO_OWNER", raising=False)
    monkeypatch.delenv("BH_REPO_NAME", raising=False)

    monkeypatch.setattr(
        after_run, "_current_labels", lambda issue: ["agent-ready"]
    )

    def _fake_run(cmd):
        import subprocess
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(after_run, "_run", _fake_run)

    alerts = []
    monkeypatch.setattr(
        after_run, "alert",
        lambda *a, **kw: (alerts.append({"args": a, "kwargs": kw}) or True),
        raising=False,
    )

    rc = after_run._reconcile_labels(42, after_run.RunOutcome.WORKER_TRIED_MERGE)
    assert rc == 0
    # Alert still called — with empty-string positional owner/repo.
    assert len(alerts) == 1
    assert alerts[0]["args"][0] == ""
    assert alerts[0]["args"][1] == ""
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_after_run.py::test_classify_returns_worker_tried_merge_on_sentinel -v`
Expected: FAIL — `RunOutcome.WORKER_TRIED_MERGE` does not exist; sentinel is not checked.

- [ ] **Step 4: Extend `RunOutcome`**

In `src/baton_harness/after_run.py:L107-L129`, add a new member to the enum:

```python
class RunOutcome(enum.Enum):
    # ... existing members ...
    UNCOMMITTED_CHANGES = "uncommitted-changes"
    NO_COMMITS = "no-commits"
    COMMITTED_NO_PR = "committed-no-pr"
    PR_OPENED = "pr-opened"
    TRANSIENT_ERROR = "transient-error"
    WORKER_TRIED_MERGE = "worker-tried-merge"  # slice 3b
```

- [ ] **Step 5: Add the sentinel constants and import the escalation alert**

Near the top of `after_run.py` (after the existing constants block at L92-L99), add. **Important: import `alert` as the bare name (no `as` alias).** Step 7 calls the bare `alert(...)`, and the test in Step 2 patches via `monkeypatch.setattr(after_run, "alert", ...)` — all three sites must agree on the module-namespace attribute name. The `alert` callable's verified signature is `alert(owner: str, repo: str, issue: int | None, summary: str, *, severity, runlog=None, kind="block")` at `src/baton_harness/chain/escalation.py:L192-L201` (verified by Read 2026-06-23), so positional `owner`/`repo` are mandatory — Step 7 reads them from the daemon-exported env vars `BH_REPO_OWNER` and `BH_REPO_NAME` (already required by `bin/run-daemon.sh:L81-L96` and consumed by `src/baton_harness/chain/cli.py:L120-L132`; `bh-after-run` inherits the daemon process environment, so no new env-var contract is introduced).

```python
import os
from pathlib import Path

from baton_harness.chain.escalation import alert

# Slice 3b — sentinel file written by the force-pr-not-merge PreToolUse
# hook (src/baton_harness/hooks/force_pr_not_merge.py) when a worker
# attempts to merge a PR. _classify() checks this as its FIRST step.
#
# IMPORTANT: `alert` is imported as the bare name (no `as` alias) so
# `monkeypatch.setattr(after_run, "alert", ...)` in tests patches the
# same module-namespace attribute the Step 7 call site resolves.
_SENTINEL_DIR = ".bh-state"
_SENTINEL_NAME = "worker-tried-merge"
```

**CONCERN — documented tradeoff (not a code change).** If `_current_labels(issue)` returns `None` for some reason (transient gh error, race, etc.) inside the Priority-3 path that this branch falls through to, the `agent-ready → blocked` label transition may not land — the issue would stay `agent-ready` and could be redispatched before the operator acts. The escalation alert from Step 7 still fires (its `except Exception` guard is purely for `alert()` failures, not for label-application failures upstream of it), and the slice-3c live merge-denial probe is the deterministic safety net that catches a stuck-`agent-ready` state on the next worker turn. An operator paged by the critical alert should hold the issue manually if the redispatch race is observed in practice. We accept this rather than complicating the label path because (a) `_current_labels` returning `None` is itself a transient condition that the existing Priority-0 (TRANSIENT_ERROR) flow already handles for any other outcome, and (b) escalation paths in this module are uniformly best-effort per the existing `except Exception` discipline around `alert()` calls.

- [ ] **Step 6: Patch `_classify()` to check the sentinel FIRST**

Insert this block at the very TOP of `_classify()` (before the existing `git status` call at L237):

```python
def _classify() -> RunOutcome:
    # Slice 3b: sentinel check is FIRST — the force-pr-not-merge hook
    # writes ${PWD}/.bh-state/worker-tried-merge on a worker merge
    # attempt, and that outcome is terminal regardless of any other git
    # or gh state. See src/baton_harness/hooks/force_pr_not_merge.py.
    if (Path.cwd() / _SENTINEL_DIR / _SENTINEL_NAME).exists():
        return RunOutcome.WORKER_TRIED_MERGE

    # ... existing classification body unchanged ...
```

- [ ] **Step 7: Extend `_reconcile_labels()` with a Priority-0.5 branch**

Inserted BEFORE the existing Priority-0 (TRANSIENT_ERROR) check at L421. The new branch emits a critical escalation, then falls through to the existing Priority-3 path (remove `agent-ready` + add `blocked`).

The `alert(...)` call site MUST match the verified signature `alert(owner, repo, issue, summary, *, severity, runlog=None, kind="block")` at `src/baton_harness/chain/escalation.py:L192-L201`. `owner` and `repo` are POSITIONAL and REQUIRED — they are read from the daemon-exported env vars `BH_REPO_OWNER` and `BH_REPO_NAME` (validated by `bin/run-daemon.sh:L81-L96` and `src/baton_harness/chain/cli.py:L120-L132`; `bh-after-run` inherits the daemon environment). Empty-string fallback via `os.environ.get(..., "")` is the defensive path: if either env var is somehow empty when this branch runs, the call still proceeds (with empty positional args) and is caught by the existing `except Exception` guard around `alert()` — consistent with the module's best-effort escalation discipline. There is no `source=` kwarg in the real signature; it is dropped.

```python
    # Slice 3b — WORKER_TRIED_MERGE is terminal-blocked + critical alert.
    # Routes through the existing Priority-3 label flow (remove
    # agent-ready, add blocked) but ALSO emits a critical-severity
    # escalation so the operator is paged.
    if outcome == RunOutcome.WORKER_TRIED_MERGE:
        log(
            _HOOK,
            issue,
            "outcome=worker-tried-merge: force-pr-not-merge hook fired — "
            "emitting critical escalation, then applying blocked label.",
        )
        try:
            alert(
                os.environ.get("BH_REPO_OWNER", ""),
                os.environ.get("BH_REPO_NAME", ""),
                issue,
                "worker attempted to merge a PR — see .bh-state/worker-tried-merge sentinel",
                severity="critical",
            )
        except Exception as exc:  # noqa: BLE001 — escalation must not crash hook.
            err(_HOOK, issue, f"escalation alert failed: {exc}")
        # Fall through to Priority-3 label flow below.
```

(Then keep the existing `if outcome == RunOutcome.TRANSIENT_ERROR:` check, etc. The fall-through means the labels get the standard `blocked` treatment from the existing Priority-3 path at L544-L595.)

Why bare `alert` not `_escalation_alert`: Step 5 imports as `from baton_harness.chain.escalation import alert` (no `as` alias). The bare name `alert` lands in `after_run`'s module namespace; this call site resolves it; the Step 2 test patches via `monkeypatch.setattr(after_run, "alert", ...)`. Three-way consistent.

- [ ] **Step 8: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_after_run.py -v`
Expected: PASS — all existing tests plus the four new ones (sentinel-first short-circuit, sibling-cwd isolation, reconcile-labels happy path with positional owner/repo, reconcile-labels missing-env best-effort).

- [ ] **Step 9: Commit**

```bash
git add src/baton_harness/after_run.py tests/test_after_run.py
git commit -m "feat(#157): after_run reads worker-tried-merge sentinel (slice 3b task 6)"
```

---

### Task 7: Docs — `WORKFLOW.md`, `architecture-spec.md`, `README.md`

**Files:**
- Modify: `config/WORKFLOW.md`
- Modify: `docs/architecture-spec.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: every preceding task's surface.
- Produces: durable user-facing documentation. Per N2: durable references point to **issue #157 and PR #N** — NOT the plan file path, because plan files are deleted post-issue-close per CLAUDE.md lifecycle.

- [ ] **Step 1: Add a note to `config/WORKFLOW.md`**

After the existing `hooks:` block (currently at `config/WORKFLOW.md:L13-L16`), add a Markdown comment block AFTER the `---` closer, before the prompt body, so it does not break the YAML parser.

Exact addition (after `---` on line 17, before line 18):

```markdown
<!--
NOTE on hook categories (slice 3b — issue #157):
The `hooks:` block above lists the THREE Python-baton-hooks fired by the
daemon's worker turn loop (after_create / before_run / after_run).

There is a SECOND category — Claude Code PreToolUse hooks — installed
per-worktree by bh-after-create via a generated .claude/settings.json.
The current PreToolUse hook is `force-pr-not-merge`
(`src/baton_harness/hooks/force_pr_not_merge.py`); it is paired with the
branch ruleset provisioned via `bin/provision-ruleset.sh`. See
docs/architecture-spec.md §3.5 for the canonical list.
-->
```

- [ ] **Step 2: Update `docs/architecture-spec.md` §3.5 hook table**

The current `force-pr-not-merge` row at `docs/architecture-spec.md:L162` reads:

```
| `force-pr-not-merge` | PreToolUse, Bash matching `gh pr merge` | exit non-zero; merges are human-only |
```

Replace it with:

```
| `force-pr-not-merge` | PreToolUse, Bash matching `gh pr merge`, `gh api .*pulls/.*merge` (any-order), `curl …pulls/.*merge` (any-order) | exit non-zero; drop `.bh-state/worker-tried-merge` sentinel; emit `BH_WORKER_TRIED_MERGE` stderr marker; `after_run` reads the sentinel and escalates to `blocked` + Slack critical. **Defense-in-depth** — the GitHub Repository Ruleset (provisioned via `bin/provision-ruleset.sh`) is the boundary. |
```

Also add a one-sentence note at the end of §3.5 (after line 163). Per N2, reference issue/PR (durable) NOT the plan path (volatile):

```markdown
**Merge boundary (slice 3b):** worker-identity merges into `main` and `feature/<slug>` are denied by a GitHub Repository Ruleset. The daemon's GitHub App installation is the sole bypass actor on `feature/<slug>`; only the repo admin role bypasses on `main`. See issue #157 and PR #N for the design.
```

(At PR-creation time, replace `#N` with the actual PR number.)

- [ ] **Step 3: Update `README.md`**

Add a new sub-section near the existing sandbox-setup section (find the heading covering `bin/init-sandbox.sh` and add after it). Both `BH_GITHUB_APP_ID` and `BH_GITHUB_APP_INSTALLATION_ID` are required (B3):

```markdown
### GitHub repository ruleset (slice 3b — issue #157)

After running `bin/init-sandbox.sh`, provision the merge-boundary rulesets:

```bash
# Required: BOTH App identifiers (they are different integers).
#   BH_GITHUB_APP_ID is shown at https://github.com/settings/apps/<slug>
#     (it's also returned by `gh api /app --jq .id`).
#   BH_GITHUB_APP_INSTALLATION_ID is returned by
#     `gh api repos/<owner>/<repo>/installation --jq .id`.
export BH_REPO_OWNER=<owner>
export BH_REPO_NAME=<sandbox-repo>
export BH_GITHUB_APP_ID=<numeric-from-/app>
export BH_GITHUB_APP_INSTALLATION_ID=<numeric-from-/repos/.../installation>
# Optional: override the RepositoryRole admin actor_id (default 5).
# Only needed if your org has remapped role ids.
# export BH_ADMIN_ROLE_ID=5
bin/provision-ruleset.sh
```

This creates two rulesets:

- `harness-main-no-merge` — denies any push/merge into the default branch except by a repository admin (RepositoryRole bypass).
- `harness-feature-daemon-only` — denies pushes to `feature/*` branches except by the daemon's GitHub App installation (the legitimate per-issue merger).

The script is idempotent — safe to re-run. It uses the GitHub Rulesets REST API's list-then-by-id endpoint shape (per the API contract — `GET /rulesets/{ruleset_id}` requires a numeric id) and does a preflight cross-check that `BH_GITHUB_APP_ID` matches `GET /app`. See issue #157 and the merge PR for the design.
```

- [ ] **Step 4: Commit**

```bash
git add config/WORKFLOW.md docs/architecture-spec.md README.md
git commit -m "docs(#157): WORKFLOW.md / architecture-spec / README updates (slice 3b task 7)"
```

---

### Task 8: CI gate — required-checks names match `ci.yml` (yaml.safe_load — C2)

**Files:**
- Create: `tests/test_required_checks_match_ci_yml.py`

**Interfaces:**
- Consumes: `src/baton_harness/chain/merge.py:REQUIRED_CHECKS` (`L105-L109`, verified by Read 2026-06-23 — values: `Lint (ruff)`, `Test (pytest)`, `Type check (mypy)`) + `.github/workflows/ci.yml` job `name:` values (`L16, L31, L43`, verified) + `config/ruleset.main.json` `required_status_checks[].context` values.
- Produces: a CI-runnable test that fails loudly if any of the three drift apart. Uses `yaml.safe_load` to parse `ci.yml` and walks `data["jobs"].values()` to extract top-level job names — the previous regex `r"^\s{4}name:\s*(.+?)\s*$"` (C2) would also match step-level `name:` keys nested under `steps:` and produce false-positive failures.

- [ ] **Step 1: Confirm PyYAML is in `[dev]` extras**

`pyproject.toml` should already list `pyyaml` under `[project.optional-dependencies]` `dev` (PyYAML is a common test dep; if absent, add it now and `uv pip install --python ./.venv/Scripts/python.exe -e ".[dev]"`).

- [ ] **Step 2: Write the test**

```python
"""Slice 3b — three sources of truth for required-check names MUST agree.

If they drift, the merge boundary breaks silently:
- merge.py:REQUIRED_CHECKS — what the daemon waits for before merging.
- ci.yml top-level job names — what GitHub Actions actually reports.
- ruleset.main.json required_status_checks — what GitHub enforces on PRs to main.

This test reads all three and asserts the sets are equal. The ci.yml
parse uses yaml.safe_load and walks data["jobs"].values() to avoid the
step-level `name:` false-positives a flat regex would catch (C2).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from baton_harness.chain.merge import REQUIRED_CHECKS

HARNESS = Path(__file__).resolve().parents[1]


def _ci_yml_top_level_job_names() -> set[str]:
    """Extract top-level job names from .github/workflows/ci.yml.

    Walks ``data["jobs"].values()`` and yields the ``name`` field of each
    job — pinned to top-level jobs only so a step-level ``name:`` cannot
    sneak into the set.
    """
    text = (HARNESS / ".github" / "workflows" / "ci.yml").read_text()
    data = yaml.safe_load(text)
    return {
        job["name"]
        for job in data.get("jobs", {}).values()
        if isinstance(job, dict) and "name" in job
    }


def _ruleset_required_checks() -> set[str]:
    payload = json.loads(
        (HARNESS / "config" / "ruleset.main.json").read_text()
    )
    for rule in payload["rules"]:
        if rule["type"] == "required_status_checks":
            return {
                c["context"]
                for c in rule["parameters"]["required_status_checks"]
            }
    return set()


def test_required_checks_agree_across_sources():
    merge_set = set(REQUIRED_CHECKS)
    ci_set = _ci_yml_top_level_job_names()
    ruleset_set = _ruleset_required_checks()

    assert merge_set == ci_set, (
        f"merge.py REQUIRED_CHECKS {merge_set} differs from "
        f"ci.yml top-level job names {ci_set}"
    )
    assert merge_set == ruleset_set, (
        f"merge.py REQUIRED_CHECKS {merge_set} differs from "
        f"ruleset.main.json required_status_checks {ruleset_set}"
    )
```

- [ ] **Step 3: Run the test to verify it passes (against the current state Task 1 produced)**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_required_checks_match_ci_yml.py -v`
Expected: PASS — the three sets all equal `{"Lint (ruff)", "Type check (mypy)", "Test (pytest)"}`.

- [ ] **Step 4: Commit**

```bash
git add tests/test_required_checks_match_ci_yml.py
git commit -m "test(#157): pin required-check names across merge.py / ci.yml / ruleset (slice 3b task 8)"
```

---

## Acceptance criteria mapping (#133 / #157 "done when")

| Acceptance criterion | Delivered by |
|---|---|
| Worker-identity merges into `main` are denied at the GitHub API layer. | Tasks 1, 2 — `harness-main-no-merge` ruleset (admin-only bypass). |
| Worker-identity merges into `feature/<slug>` are denied at the GitHub API layer. | Tasks 1, 2 — `harness-feature-daemon-only` ruleset with daemon-only bypass. |
| Daemon's `chain/merge.py:merge_issue_branch` continues to merge into `feature/<slug>`. | Tasks 1, 2 — daemon App (App-ID) listed as Integration bypass actor. |
| Worker-side `gh pr merge` is loudly stopped and logged. | Tasks 4, 5 — `force-pr-not-merge` hook (any-order regex incl. URL-first) + `.claude/settings.json` drop. |
| Stoppage routes to escalation (`blocked` + Slack critical). | Tasks 4, 6 — sentinel file + `WORKER_TRIED_MERGE` outcome + escalation alert. |
| Provisioning is idempotent and reviewable. | Tasks 1, 2 — checked-in JSON + list-then-by-id script + 5 idempotency tests incl. stale-ID. |
| #144 (preflight gate) can consume a signal that the boundary is in place. | Task 3 — `ruleset_is_provisioned()` contract (same list-then-by-id shape). |
| Required-check names cannot silently drift. | Task 8 — three-way agreement test (yaml.safe_load, top-level only). |
| Docs reflect the new boundary + hook category + dual App identifiers. | Task 7 — `WORKFLOW.md`, `architecture-spec.md`, `README.md`. |

## Out of scope (explicit — do not implement)

- **Slice 3c**: live merge-denial probe against the real sandbox repo. Separate PR; the live probe will also confirm/falsify the `RepositoryRole` admin `actor_id=5` assumption and pin the correct integer if wrong.
- **#155**: real HTTP transport for `mint_installation_token`. Slice 3a stubs are sufficient for slice 3b's purposes.
- **#156**: `GhRunner` refactor + refresh-before-expiry. `ruleset_status.py` accepts an injected runner so #156 can swap in cleanly later.
- **#144**: preflight-gate daemon wiring. Slice 3b emits the signal; #144 adds the call at daemon startup.
- **Slack bot interactive UI** for the `WORKER_TRIED_MERGE` escalation. The `alert()` call reaches the existing webhook-escalation path; richer cards are deferred to the v2 Slack-bolt-bot work per `docs/architecture-spec.md:L297`.
