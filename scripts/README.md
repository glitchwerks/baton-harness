# scripts/

Hook scripts called by Baton during the lifecycle of each agent run.

Populated by issues #2 and #3:
- `after-create.sh` — per-worktree dependency install (issue #2)
- `before-run.sh` — branch sync onto main before the agent runs (issue #3)
- `after-run.sh` — outcome classification and label reconciliation (issue #3)

Each script is standalone and independently testable. The hook scripts derive
issue context from `basename "$PWD"` (the worktree directory name) since Baton
does not pass env-var context to hooks (spike finding F2).

The harness root path is available via `$BATON_HARNESS_DIR`, exported by
`bin/run.sh` at launch time (harness-design.md §8 design decision).
