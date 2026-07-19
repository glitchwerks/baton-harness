# baton-harness — project instructions

This harness drives the **Baton** orchestrator (`mraza007/baton`, package `symphony`) to run autonomous Claude Code agents against GitHub issues.

## Upstream dependency: Baton is DORMANT → `symphony` is VENDORED (option c) [implemented]

`mraza007/baton` is a single-author proof-of-concept — 3 commits in one Mar 17–27 2026 burst, nothing since, no releases, and **no external PRs ever merged**. Treat it as **unmaintained**.

**Status [implemented, v1 serial — #27, phases P0–P3]:** the `symphony` package is vendored into `src/baton_harness/vendor/symphony/`, and the always-on daemon (`src/baton_harness/chain/`) calls `Orchestrator._run_worker(issue)` directly (option (c); see `docs/harness-design.md` §1 and §10). Vendoring — not forking, not external-dependency management — was the chosen response to the dormant upstream. The harness is now the de facto maintainer of the orchestrator source, and Baton bugs are harness-internal fixes.

**Policy for Baton bugs we hit [updated post-#224]:** the vendored tree under `src/baton_harness/vendor/symphony/` is fully assimilated as owned code — linted and type-checked like any other module, no ruff/mypy exclusions. Fix bugs directly in place; a `patches/*.diff` file and `VENDORING.md` entry are **no longer required** for new changes (that re-vendor-checklist procedure was retired in #224 — `VENDORING.md` is now a provenance record, and `patches/` is a frozen historical archive of the pre-#224 patches). No upstream dependency; best-effort upstream reports (e.g. `mraza007/baton#1`) remain optional and are not load-bearing. The external-`baton` pilot launcher (`bin/run.sh` / `baton start -w`) has been **retired** in favor of `bin/run-daemon.sh` and the `bh-daemon` entry point.

The previously-tracked item **#23** (terminal-block / `exclude_labels` not re-checked between turns) is **resolved**: VP-2 adds the mid-turn `exclude_labels` re-check to the vendored `_run_worker` loop, so a mid-run `blocked` label is now terminal — retiring the `max_turns: 2` workaround (`config/WORKFLOW.md` now sets `max_turns: 8`). No upstream-blocked items remain.

## Prior art / design references

Two external systems inform this harness's design. Draw on both when reasoning about orchestration, autonomy, and the label state machine.

- **`mraza007/baton`** (`symphony`) — the original orchestrator, now vendored. Source of the core poll-issue → run-agent → open-PR loop. Dormant upstream (see § Upstream dependency above).
- **[`nexu-io/looper`](https://github.com/nexu-io/looper)** — actively-maintained Go system with the same core idea (poll GitHub for labeled issues/PRs, run pluggable AI agents, produce PRs), but architecturally deeper: five agent roles (Coordinator → Planner → Reviewer ↔ Fixer → Worker), parallel goroutines, goal-based termination via a stdout result marker, optional auto-merge, and 11 ADRs. **Design reference, not a dependency** — borrow patterns, keep our Python stack and no-merge guardrails (the daemon opens a ready-for-review PR but never merges to `main`). Full comparison and the rationale for *not* adopting it wholesale: `docs/research/2026-06-21-looper-vs-baton-harness.md`. Active borrow-candidates tracked under milestone **Looper-inspired enhancements** (#139 goal-based termination, #140 automated review pass, #141 durable-authority discipline).
