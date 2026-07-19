# patches/

**Frozen historical record.** This directory is no longer an active part of
the vendor-change workflow — see "Policy" below.

## What's here

Nine `VP-N-*.diff` files, each documenting one behavioral patch applied to
the vendored `symphony` package (`src/baton_harness/vendor/symphony/`)
before issue #224, plus `mypy-strict-remediation.diff`, which documents a
type-checking exclusion that #224 has since removed outright. Full
descriptions of what each patch changed and why are in
`src/baton_harness/vendor/symphony/VENDORING.md` under "Applied patches
(historical annotations)" — that is the canonical description; the `.diff`
files here are the literal patch content.

| File | Documents |
|---|---|
| `VP-1-run-hook-env.diff` | `run_hook` gains `env=` parameter |
| `VP-2-exclude-labels-recheck.diff` | `exclude_labels` re-check + `running[N]` guard |
| `VP-3-progress-callback.diff` | per-turn progress callback |
| `VP-4-worker-disallow-merge.diff` | `_build_claude_args` denies PR-merge tools |
| `VP-5-pr-exists-early-exit.diff` | mid-loop PR-exists early-exit in `_run_worker` |
| `VP-6-state-load-on-startup.diff` | `OrchestratorState.load()` — restore state on startup |
| `VP-7-hooks-non-login-shell.diff` | `run_hook` drops the login-shell flag |
| `VP-8-required-checks-config.diff` | `WorkflowConfig.required_checks` operator override |
| `VP-9-workspace-symphony-dir-abspath.diff` | `WorkspaceManager.__init__` normalizes `symphony_dir` to an absolute path |
| `mypy-strict-remediation.diff` | original vendor-wide mypy `ignore_errors` exclusion — **superseded by #224**, the exclusion no longer exists in `pyproject.toml` |

## Policy (Decision D3, ratified 2026-07-14)

Before issue #224, a change to the vendored `symphony` tree was expected to
produce a corresponding `patches/*.diff` file, tracked alongside a
`VENDORING.md` entry and `# VENDOR-PATCH` source markers, because the tree
was excluded from linting/type-checking and treated as third-party code
under best-effort management.

Issue #224 assimilated `src/baton_harness/vendor/symphony/` as owned code:
it is now linted and type-checked identically to the rest of
`src/baton_harness/`, with no vendor-specific exclusions in `pyproject.toml`.
As a result:

- **New `patches/*.diff` files are no longer required** for future changes
  to the vendored tree. Fix bugs, add features, or refactor directly, and
  rely on normal code review and commit history as the record — the same as
  for any other module in this codebase.
- **The 9 existing `VP-N-*.diff` files, and `mypy-strict-remediation.diff`,
  remain in place** as historical evidence of what each patch changed and
  why, relative to the pinned upstream SHA. They are not deleted and this
  directory is not repurposed for anything else.
- Existing citations to these files (in `VENDORING.md`, source comments, and
  elsewhere) remain valid — the files are not moving or being renamed.

See `src/baton_harness/vendor/symphony/VENDORING.md` for the full
provenance record, including upstream SHA, license, and per-patch
descriptions.
