# baton-harness — project instructions

This harness drives the **Baton** orchestrator (`mraza007/baton`, package `symphony`) to run autonomous Claude Code agents against GitHub issues.

## Upstream dependency: Baton is DORMANT → `symphony` is VENDORED (option c) [implemented]

`mraza007/baton` is a single-author proof-of-concept — 3 commits in one Mar 17–27 2026 burst, nothing since, no releases, and **no external PRs ever merged**. Treat it as **unmaintained**.

**Status [implemented, v1 serial — #27, phases P0–P3]:** the `symphony` package is vendored into `src/baton_harness/vendor/symphony/`, and the always-on daemon (`src/baton_harness/chain/`) calls `Orchestrator._run_worker(issue)` directly (option (c); see `docs/harness-design.md` §1 and §10). Vendoring — not forking, not external-dependency management — was the chosen response to the dormant upstream. The harness is now the de facto maintainer of the orchestrator source, and Baton bugs are harness-internal fixes.

**Policy for Baton bugs we hit:** fix them directly in the vendored source under `src/baton_harness/vendor/symphony/`, recorded as a tracked patch in `patches/` with `# VENDOR-PATCH` markers and a `VENDORING.md` entry (see its re-vendor checklist). No upstream dependency; best-effort upstream reports (e.g. `mraza007/baton#1`) remain optional and are not load-bearing. The external-`baton` pilot launcher (`bin/run.sh` / `baton start -w`) has been **retired** in favor of `bin/run-daemon.sh` and the `bh-daemon` entry point.

The previously-tracked item **#23** (terminal-block / `exclude_labels` not re-checked between turns) is **resolved**: VP-2 adds the mid-turn `exclude_labels` re-check to the vendored `_run_worker` loop, so a mid-run `blocked` label is now terminal — retiring the `max_turns: 2` workaround (`config/WORKFLOW.md` now sets `max_turns: 8`). No upstream-blocked items remain.
