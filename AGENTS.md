# CodeReeve — project instructions

**CodeReeve** runs autonomous Claude Code agents against GitHub issues. Its owned orchestration engine, `symphony`, was originally vendored from **Baton** (`mraza007/baton`).

## Upstream dependency: Baton is DORMANT → `symphony` is VENDORED (option c) [implemented]

`mraza007/baton` is a single-author proof-of-concept — 3 commits in one Mar 17–27 2026 burst, nothing since, no releases, and **no external PRs ever merged**. Treat it as **unmaintained**.

**Status [implemented, v1 serial — #27, phases P0–P3]:** the `symphony` package is vendored into `src/codereeve/vendor/symphony/`, and the always-on daemon (`src/codereeve/chain/`) calls `Orchestrator._run_worker(issue)` directly (option (c); see `docs/harness-design.md` §1 and §10). Vendoring — not forking, not external-dependency management — was the chosen response to the dormant upstream. CodeReeve maintains the orchestrator source; bugs in that engine are fixed within this repository.

**Policy for Baton bugs we hit [updated post-#224]:** the vendored tree under `src/codereeve/vendor/symphony/` is fully assimilated as owned code — linted and type-checked like any other module, no ruff/mypy exclusions. Fix bugs directly in place; a `patches/*.diff` file and `src/codereeve/vendor/symphony/VENDORING.md` entry are **no longer required** for new changes (that re-vendor-checklist procedure was retired in #224 — `src/codereeve/vendor/symphony/VENDORING.md` is now a provenance record, and `patches/` is a frozen historical archive of the pre-#224 patches). No upstream dependency; best-effort upstream reports (e.g. `mraza007/baton#1`) remain optional and are not load-bearing. The external-`baton` pilot launcher has been **retired** in favor of `bin/run-daemon.sh` and the `codereeve daemon` command.

The previously-tracked item **#23** (terminal-block / `exclude_labels` not re-checked between turns) is **resolved**: VP-2 adds the mid-turn `exclude_labels` re-check to the vendored `_run_worker` loop, so a mid-run `blocked` label is now terminal — retiring the `max_turns: 2` workaround (`config/WORKFLOW.md` now sets `max_turns: 8`). No upstream-blocked items remain.

## Prior art / design references

Two external systems inform CodeReeve's design. Draw on both when reasoning about orchestration, autonomy, and the label state machine.

- **`mraza007/baton`** (`symphony`) — the original orchestrator, now vendored. Source of the core poll-issue → run-agent → open-PR loop. Dormant upstream (see § Upstream dependency above).
- **[`nexu-io/looper`](https://github.com/nexu-io/looper)** — actively-maintained Go system with the same core idea (poll GitHub for labeled issues/PRs, run pluggable AI agents, produce PRs), but architecturally deeper: five agent roles (Coordinator → Planner → Reviewer ↔ Fixer → Worker), parallel goroutines, goal-based termination via a stdout result marker, optional auto-merge, and 11 ADRs. **Design reference, not a dependency** — borrow patterns, keep our Python stack and no-merge guardrails (the daemon opens a ready-for-review PR but never merges to `main`). Full comparison and the rationale for *not* adopting it wholesale: `docs/research/2026-06-21-looper-vs-baton-harness.md`. Active borrow-candidates tracked under milestone **Looper-inspired enhancements** (#139 goal-based termination, #140 automated review pass, #141 durable-authority discipline).

## CodeGraph

This repository is CodeGraph-enabled. Use the `mcp__codegraph__*` tools first for symbol discovery, architecture exploration, caller/callee tracing, and change-impact analysis. Start broad codebase investigations with `codegraph_context`.

Use `rg` and direct file reads for exact-text searches, configuration and other non-code content, or when the CodeGraph index is unavailable or stale. Do not initialize or rebuild a missing or stale index without explicit user authorization; report its status instead.

## Canonical product interfaces

Use `codereeve` for Python imports and extend the `codereeve` command tree for new operations. Application configuration uses `CODEREEVE_*` variables and `.codereeve` paths; third-party names remain unchanged. Legacy interfaces exist only for explicit compatibility and migration through 0.3.x, with removal in 0.4.0 (#390, #395).

## Repository work workflow

- Create or update a GitHub issue before starting work. A milestone is optional at issue creation and required before issue closure. Keep acceptance criteria to outcomes that must be complete before merge; do not include post-merge acceptance criteria.
- Start from current `main` and use a dedicated `<type>/<issue-number>-<slug>` branch, where type is `feature`, `bug`, `docs`, `chore`, or `refactor`. Large-feature sub-branches target the primary feature branch; the primary feature branch targets `main`.
- Run fast validation while working on a branch. Pull requests to `main` run the full integration suite and the PR-policy check.
- Every PR to `main` must include `Closes #N`, `Fixes #N`, or `Resolves #N`. The branch issue number must be among the closing issues, and every closing issue must have a milestone.
- Add the PR-only `needs-review` label to every feature PR and never to a docs-only PR. For bug, chore, and refactor PRs, add it when the change touches security, authorization, identity, secrets, permissions, persistence, concurrency, or another comparably high-risk boundary. CodeRabbit feedback is advisory.
- Never push or merge directly to `main`. Merge only after all required checks pass.
