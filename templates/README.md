# templates/

Source templates for files that must be committed to each project repo.

Populated by issue #5:
- `CLAUDE.md.template` — the Jinja2/source template for each project's
  committed `CLAUDE.md`. Because CLAUDE.md is irreducibly project-local
  (Claude Code discovers it from the worktree; spike finding F11), the live
  file is committed to the project repo. This template is the harness-owned
  source it is generated from.

For the pilot, the CLAUDE.md is produced via manual copy from the template
(harness-design.md §8: "Manual is fine for one project").
