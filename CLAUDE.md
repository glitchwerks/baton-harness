# baton-harness — project instructions

This harness drives the **Baton** orchestrator (`mraza007/baton`, package `symphony`) to run autonomous Claude Code agents against GitHub issues.

## Upstream dependency: Baton is currently DORMANT (as of 2026-06-06)

`mraza007/baton` is a single-author proof-of-concept — 3 commits in one Mar 17–27 2026 burst, nothing since, no releases, and **no external PRs ever merged**. Treat it as **unmaintained** until proven otherwise.

**Policy for Baton bugs we hit:**

- Bugs that affect the harness **may still be logged upstream** (best-effort) — e.g. `mraza007/baton#1` (`exclude_labels` not re-checked between continuation turns). A monthly remote routine monitors for any upstream response.
- **If too many such issues accumulate without an upstream response, consider forking `mraza007/baton` and fixing them ourselves** rather than waiting on a dormant repo.
- Prefer **harness-side mitigations** over blocking on an upstream fix.

Local tracker for the current upstream-dependent item: **#23** (terminal-block / `exclude_labels`-not-rechecked-between-turns). Upstream report: `mraza007/baton#1`.
