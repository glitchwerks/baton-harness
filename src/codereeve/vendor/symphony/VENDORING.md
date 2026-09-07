# Vendored symphony package

**This file is a provenance record**, not an active procedure. It documents
where the vendored `symphony` tree came from, what was patched and why
(historically), and what changed during the #224 assimilation. It is not a
re-vendor runbook — see "Policy: no active re-vendor procedure" below.

## Upstream

- **Repository:** https://github.com/mraza007/baton
- **Pinned SHA:** `7bb5fb73c08f31d897b7b64e85b3247a0292eebd`
- **Vendor date:** 2026-06-07
- **License:** MIT (see `LICENSE` in this directory; preserved verbatim as
  required by the MIT license terms)

## Rationale

`mraza007/baton` is a dormant single-author proof-of-concept (3 commits,
Mar 2026, no external PRs ever merged). The harness vendors the `symphony`
package so it can call `Orchestrator._run_worker(issue)` directly as a
library function, apply patches without upstream dependency, and remain
self-contained. See `CLAUDE.md § Upstream dependency` and
`docs/harness-design.md §1` for the full decision.

## Status (P3 — LIVE)

The vendored tree is **live** as of P3.  The custom always-on daemon
(`chain/daemon.py`) calls `Orchestrator._run_worker` directly.  VP-2 has
been applied (see below).  `bin/run.sh` has been deleted; `bin/run-daemon.sh`
is the new launcher.  The external `baton` dependency is no longer the active
orchestration path.

## Applied patches (historical annotations)

The entries below document what each patch changed in the vendored source and
why, at the time it was applied. As of issue #224, `src/baton_harness/vendor/symphony/`
is linted and type-checked as owned code — see "Policy: no active re-vendor
procedure" below — so these are a historical record of past changes, not a
description of an ongoing patch-and-track workflow. The corresponding diff
files remain in `patches/` (frozen, per Decision D3 — see `patches/README.md`)
as evidence of what each entry describes.

### VP-1 — `run_hook` gains `env=` parameter

- **File:** `hooks.py`
- **Patch file:** `patches/VP-1-run-hook-env.diff` (relative to repo root)
- **Description:** `run_hook` now accepts an optional `env: dict[str, str] | None`
  keyword argument. The override dict is **merged into `os.environ`** via
  `merged_env = {**os.environ, **(env or {})}` and passed to
  `asyncio.create_subprocess_exec(..., env=merged_env)`. This preserves
  `PATH`, `HOME`, and all other inherited environment variables so that
  `git` and `gh` remain resolvable inside the hook subprocess (CONCERN-1,
  issue #42). The default (`env=None`) is unchanged — passing no `env`
  argument uses `os.environ` exactly as before. Marker:
  `# VENDOR-PATCH VP-1: run_hook env= threading (merged into os.environ)`.

### VP-2 — exclude_labels re-check + running[N] guard

- **File:** `orchestrator.py`
- **Patch file:** `patches/VP-2-exclude-labels-recheck.diff` (relative to repo root)
- **Description:** After the `fetch_issue_state` / `current_state != "open"`
  check inside the `_run_worker` turn loop, adds a re-check of
  `self.tracker.exclude_labels` by fetching the issue's current labels via
  `run_gh(["issue", "view", ..., "--json", "labels"])`.  If any exclude label
  (e.g. `"blocked"`) is now present, the loop is terminated immediately — making
  a mid-run block terminal (closes the #23 root cause; retires the `max_turns: 2`
  workaround in `config/WORKFLOW.md`).  The fetch is best-effort (wrapped in
  `try/except`) so a label-API failure cannot crash the run.  Also confirms the
  existing `if issue.number in self.state.running:` guard at the turn-state
  mutation site (CONCERN-4 / VP-2 requirement already satisfied in the vendored
  source — documented with a `# VENDOR-PATCH VP-2` comment).  Marker:
  `# VENDOR-PATCH VP-2: ...`.

### VP-3 — per-turn progress callback

- **File:** `orchestrator.py`
- **Patch file:** `patches/VP-3-progress-callback.diff` (relative to repo root)
- **Description:** Adds an optional `progress_cb` attribute (default `None`)
  to `Orchestrator.__init__` via attribute injection — zero changes to any
  existing method signature.  Adds one guarded call at the turn-loop head
  (immediately after the `log.info(f"RUN  ...")` line) wrapped in
  `try/except` so a callback exception is logged and swallowed, never
  crashing the worker run.  The daemon injects a closure that calls
  `liveness_state.note_progress(now)` so the heartbeat monitor can detect
  a hung worker (P2 / IS-1, issue #33).  Marker:
  `# VENDOR-PATCH VP-3: per-turn progress callback (issue #33)`.

### VP-4 — _build_claude_args denies PR-merge tools

- **File:** `worker.py`
- **Patch file:** `patches/VP-4-worker-disallow-merge.diff` (relative to repo root)
- **Description:** Adds a module-level constant `_MERGE_DENY_TOOLS` containing
  the two deny tokens `"Bash(gh pr merge*)"` and
  `"mcp__github__merge_pull_request"`. `_build_claude_args` unconditionally
  appends `["--disallowed-tools", *_MERGE_DENY_TOOLS]` after the
  permission-mode block and before `return args`. The deny-list is therefore
  present regardless of `permission_mode` value (including `None`). Deny rules
  are honored even under `--dangerously-skip-permissions` — deny precedence is
  a hard constraint; skip-permissions only skips prompts. A `Bash(gh pr merge*)`
  deny rule is robust against compound/process-wrapper bypass per Claude Code's
  command-splitting semantics. Defense-in-depth alongside the no-merge
  prohibition added to `config/WORKFLOW.md` (issue #130). Marker:
  `# VENDOR-PATCH VP-4: always deny PR-merge tools (#130)`.

### VP-5 — mid-loop PR-exists early-exit in _run_worker

- **File:** `orchestrator.py`
- **Patch file:** `patches/VP-5-pr-exists-early-exit.diff` (relative to repo root)
- **Description:** Adds a guarded `check_pr_exists` call inside the `_run_worker`
  turn loop, inserted AFTER the existing closed-issue break and the VP-2
  `exclude_labels` break — so those higher-priority terminations still fire first.
  If `check_pr_exists` returns `True`, the loop breaks immediately
  (`"PR_EARLY"` log line) so the worker does not burn remaining turns firing
  useless "continue" prompts after a PR is already open (closes issue #137).
  Also wraps the existing post-loop `check_pr_exists` call in `try/except` so
  a transient gh failure no longer crashes the run — on exception, `pr_exists`
  is set to `False` and the daemon schedules a continuation retry.  Both sites
  use the best-effort swallow-and-continue pattern matching VP-2/VP-3.
  Marker: `# VENDOR-PATCH VP-5: mid-loop PR-exists early-exit (#137)`.

### VP-6 — `OrchestratorState.load()` — restore state on startup

- **Files:** `state.py`, `orchestrator.py`
- **Patch file:** `patches/VP-6-state-load-on-startup.diff` (relative to repo root)
- **Description:** Adds an `OrchestratorState.load(path)` method that restores
  `running`, `retry_queue`, and `claimed` from a previously-persisted
  `state.json`. A missing file is a no-op (first-ever startup); malformed
  JSON or an unreadable file logs a WARNING and leaves state empty (safe
  fresh-start rather than a crash). `last_event_at` is always `None` after
  load because that field is intentionally omitted from the persisted JSON.
  `Orchestrator.__init__` calls `self.state.load(state_path)` immediately
  after constructing `self.state`, so restore is transparent to callers.
  Also hardens `persist()` to write atomically — via a sibling `tempfile` +
  `os.replace` — so a crash or exception mid-write can no longer leave a
  partial/corrupt `state.json` (the file `load()` would otherwise have to
  tolerate). Closes issue #106 (merged via PR #166): the daemon's retry
  queue and running-issue state now survive a restart instead of resetting
  to empty on every boot. Marker: `# VENDOR-PATCH VP-6: ...`.

### VP-7 — `run_hook` drops the login-shell flag

- **File:** `hooks.py`
- **Patch file:** `patches/VP-7-hooks-non-login-shell.diff` (relative to repo root)
- **Description:** `run_hook`'s subprocess spawn changes from
  `asyncio.create_subprocess_exec("bash", "-lc", script, ...)` to
  `asyncio.create_subprocess_exec("bash", "-c", script, ...)`. The `-l`
  (login shell) flag forced the OS account's `/etc/profile` + `~/.bashrc`
  chain to run before the hook script itself executed, which could clobber
  daemon-injected environment variables (e.g. `GH_TOKEN`) ahead of the hook
  ever reading them (issue #215). Dropping `-l` makes the invocation a
  non-interactive, non-login shell; the VP-1 env-merge behaviour (env=
  overrides layered onto `os.environ`) is unaffected — it is applied to the
  `env=` kwarg regardless of the argv shell flags. Marker:
  `# VENDOR-PATCH VP-7: non-login shell ("-c", not "-lc") ...`.

### VP-8 — `WorkflowConfig.required_checks` operator override

- **File:** `config.py`
- **Patch file:** `patches/VP-8-required-checks-config.diff` (relative to repo root)
- **Description:** Adds a `required_checks: list[str]` field (default
  `field(default_factory=list)`, i.e. `[]`) to `WorkflowConfig`, and
  parses a top-level `required_checks:` WORKFLOW.md front-matter key
  (sibling of `tracker:`/`polling:`/`agent:`/`hooks:`) onto it in
  `load_workflow`; an absent key yields the `[]` default (no
  `KeyError`). The empty list is the "unset" sentinel the daemon's merge
  gate checks against: when set, the daemon threads `required_checks` to
  `merge_issue_branch(required=...)` instead of the hardcoded
  `baton_harness.chain.merge.REQUIRED_CHECKS` default; when unset, the
  gate falls back to `REQUIRED_CHECKS` and logs a one-time WARNING per
  daemon run (issue #225; see `_effective_required_checks` in
  `chain/daemon.py`, not part of the vendored tree). Marker:
  `# VENDOR-PATCH VP-8: ...`.

### VP-9 — `WorkspaceManager.__init__` normalizes `symphony_dir` to an absolute path

- **File:** `workspace.py`
- **Patch file:** `patches/VP-9-workspace-symphony-dir-abspath.diff` (relative to repo root)
- **Description:** `WorkspaceManager.__init__` now wraps the computed
  `symphony_dir` (whether caller-supplied or defaulted to
  `<project_root>/.symphony`) in `os.path.abspath(...)`, matching
  `project_root`'s existing `os.path.abspath` normalization on the
  preceding line. Previously a caller-supplied relative `symphony_dir`
  could leave `self.symphony_dir` relative, which the `worktree_path`
  escape check (`abs_path.startswith(os.path.abspath(self.symphony_dir))`)
  then resolved relative to the process's current working directory
  rather than the intended project root — a latent gap in that safety
  check. Addresses a CodeRabbit CHANGES_REQUESTED finding on PR #261.
  Marker: `# VENDOR-PATCH VP-9: normalize caller-provided symphony_dir to an absolute path.`

### Vendoring-mechanics patches (not VP patches; no separate diff files)

These are **structural edits required for re-packaging** — they change
no runtime behavior; they merely make the absolute `symphony.*` imports
resolve correctly under the `baton_harness.vendor.symphony` namespace.

- **Absolute → relative imports** (`orchestrator.py`, `prompt.py`,
  `worker.py`): all `from symphony.X import Y` statements converted to
  `from .X import Y`. Marker: `# VENDOR-PATCH: relative import for vendoring`.

### Deleted dead code (#224)

- **`cli.py` and `log.py` were deleted as dead code by #224.** Neither was
  reachable from the harness: `cli.py` implemented the retired `baton
  start/status/stop` CLI for the external-process launcher (`bin/run.sh`,
  itself already retired in favor of `bin/run-daemon.sh`), had no
  `__main__` guard so `python -m ...cli` never worked, and was the only
  importer of `log.py`'s `setup_logging`/`SymphonyFormatter`. Deleting
  `cli.py` also removed the undeclared `click` dependency. No tests
  imported either module.

### mypy strict-remediation — superseded by #224

- **File (historical):** `pyproject.toml` (`[[tool.mypy.overrides]]` block —
  this block **no longer exists** in `pyproject.toml`, see below)
- **Patch file:** `patches/mypy-strict-remediation.diff` (relative to repo root;
  **historical and frozen** — the diff content documents a state that no
  longer exists; do not re-apply it)
- **Description (original, pre-#224):** Added `ignore_errors = true` for the
  `baton_harness.vendor.*` module **glob**, so `mypy src` did not type-check
  the vendored third-party tree. Full annotation of the vendored source was
  deferred. Kept as a separate patch file from the behaviour patches (VP-*)
  per issue #42.
- **Superseded by #224:** the deferral is over. Issue #224 assimilated the
  entire vendored tree as owned code — every module is now fully annotated
  and type-checked under `strict = true`. The `[[tool.mypy.overrides]]` block
  described above (first narrowed to an explicit per-module list during #224
  Phase 1–4, then emptied as each module was cleaned) has been **removed
  outright** from `pyproject.toml`; there is no vendor-specific mypy override
  of any kind left. `mypy src` type-checks `src/baton_harness/vendor/symphony/`
  under the same `[tool.mypy]` config as the rest of the package. **A future
  re-vendor must not reintroduce a vendor override** — see "Policy" below.

## Policy: no active re-vendor procedure

As of issue #224, `src/baton_harness/vendor/symphony/` is linted (`ruff
check`, `ruff format --check`) and type-checked (`mypy src`, `strict = true`)
as owned code, identically to the rest of `src/baton_harness/`. The ruff
`exclude` entry and the mypy `[[tool.mypy.overrides]]` block that used to
carve this tree out were removed from `pyproject.toml` during #224.

Consequences for future changes to this tree:

- **No `patches/*.diff` file is required** for new changes to
  `src/baton_harness/vendor/symphony/`. Fix bugs, add features, or refactor
  directly, exactly as you would in any other owned module — normal code
  review is the record, not a diff artifact. (`patches/` itself is frozen as
  a historical record of the pre-#224 patches — see `patches/README.md` —
  not deleted, but also not a destination for new files.)
- **`# VENDOR-PATCH` markers already in the source are retained** as
  historical annotations of what was patched and why before #224; they are
  not removed and new changes are not required to add new markers in this
  style.
- **There is no re-vendor procedure to follow.** The "Applied patches"
  section above is retained as a historical record of what was changed
  relative to the pinned upstream SHA, for anyone who wants to understand the
  tree's history or, in the unlikely event of a future re-vendor from a new
  upstream SHA, wants a starting reference for what behavioral changes this
  fork carries. It is not a maintained runbook and should not be treated as
  one — if a re-vendor is ever undertaken, re-derive the approach from the
  current state of the tree and this history, and update the **Pinned SHA**
  and **Vendor date** fields above accordingly.
