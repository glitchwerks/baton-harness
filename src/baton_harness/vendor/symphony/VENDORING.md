# Vendored symphony package

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

## Applied patches

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

### Vendoring-mechanics patches (not VP patches; no separate diff files)

These are **structural edits required for re-packaging** — they change
no runtime behavior; they merely make the absolute `symphony.*` imports
resolve correctly under the `baton_harness.vendor.symphony` namespace.

- **Absolute → relative imports** (`orchestrator.py`, `cli.py`, `prompt.py`,
  `worker.py`): all `from symphony.X import Y` statements converted to
  `from .X import Y`. Marker: `# VENDOR-PATCH: relative import for vendoring`.

### mypy strict-remediation

- **File:** `pyproject.toml` (`[[tool.mypy.overrides]]` block)
- **Patch file:** `patches/mypy-strict-remediation.diff` (relative to repo root)
- **Description:** Adds `ignore_errors = true` for `baton_harness.vendor.*`
  so `mypy src` does not type-check the vendored third-party tree. Full
  annotation of the vendored source is deferred. Kept as a separate patch
  file from the behaviour patches (VP-*) per issue #42.

## Re-vendor checklist

When re-vendoring at a new upstream SHA, apply these steps in order:

1. For each module in the `symphony/` package
   (`__init__`, `cli`, `config`, `hooks`, `log`, `orchestrator`, `prompt`,
   `state`, `tracker`, `worker`, `workspace`), fetch the new version:
   ```bash
   gh api "repos/mraza007/baton/contents/symphony/<module>.py?ref=<NEW_SHA>" \
     --jq .content | base64 -d > src/baton_harness/vendor/symphony/<module>.py
   ```
2. Fetch the upstream `LICENSE`:
   ```bash
   gh api "repos/mraza007/baton/contents/LICENSE?ref=<NEW_SHA>" \
     --jq .content | base64 -d > src/baton_harness/vendor/symphony/LICENSE
   ```
3. Re-apply each patch from `patches/` using `git apply` or `patch -p1`:
   ```bash
   git apply patches/VP-1-run-hook-env.diff
   git apply patches/VP-2-exclude-labels-recheck.diff
   ```
4. Re-apply the relative-import vendoring-mechanics patches manually (they
   are not in a diff file because they only depend on the module names, which
   are stable). See the "Vendoring-mechanics patches" section above for the
   full list of files and markers.
5. Confirm every `# VENDOR-PATCH` marker landed:
   ```bash
   grep -rn "VENDOR-PATCH" src/baton_harness/vendor/
   ```
   Expected output must include at minimum:
   - `VP-1` in `hooks.py`
   - `VP-2` in `orchestrator.py`
   - `relative import for vendoring` in `orchestrator.py`, `cli.py`,
     `prompt.py`, `worker.py`
6. Update the **Pinned SHA** and **Vendor date** fields at the top of this
   file.
7. Re-run the full CI suite:
   ```bash
   .venv/Scripts/python.exe -m ruff check .
   .venv/Scripts/python.exe -m ruff format --check .
   .venv/Scripts/python.exe -m mypy src
   .venv/Scripts/python.exe -m pytest -q
   ```
   All checks must be green before the re-vendor is considered complete.
