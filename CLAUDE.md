# baton-harness — project instructions

This harness drives the **Baton** orchestrator (`mraza007/baton`, package `symphony`) to run autonomous Claude Code agents against GitHub issues.

## Upstream dependency: Baton is DORMANT → decision is to VENDOR `symphony` (option c)

`mraza007/baton` is a single-author proof-of-concept — 3 commits in one Mar 17–27 2026 burst, nothing since, no releases, and **no external PRs ever merged**. Treat it as **unmaintained**.

**Direction [decided — not yet built]:** vendor the `symphony` package into `src/baton_harness/vendor/symphony/` and call `Orchestrator._run_worker(issue)` directly (option (c); see `docs/harness-design.md` §1). Vendoring — not forking, not external-dependency management — is the chosen response to the dormant upstream. Once vendored, the harness is the de facto maintainer of the orchestrator source and Baton bugs become harness-internal fixes.

**Policy for Baton bugs we hit:**

- **Current pilot [implemented]** still installs Baton as an external dependency; prefer **harness-side mitigations** for any bug hit before vendoring lands.
- **Post-vendoring [decided — not yet built]**, fix bugs directly in the vendored source — no upstream dependency. Best-effort upstream reports (e.g. `mraza007/baton#1`) remain optional and are no longer load-bearing.

The previously-tracked upstream-dependent item **#23** (terminal-block / `exclude_labels` not re-checked between turns) is **closed**: the `max_turns: 2` workaround merged (PR #26), and the root-cause fix is a ~10-line change in the vendored `_run_worker` turn loop post-vendoring. No upstream-blocked items remain.
