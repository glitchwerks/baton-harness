# config/example-project/

Placeholder for the pilot project's Baton configuration directory.

Populated by issue #5:
- `WORKFLOW.md` — the Baton workflow config for the pilot project, including:
  - Hook wiring (absolute paths into `scripts/`)
  - Agent prompt with mechanical closing steps (spike finding F4)
  - `permission_mode: bypassPermissions` (spike finding F11)
  - `max_turns` setting (sized conservatively per harness-design.md §6)
  - Tracker labels and concurrency settings

Replace the `example-project` directory name with the actual pilot project name
when creating the real config (issue #5).
