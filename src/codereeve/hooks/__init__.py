"""Claude Code hooks installed per-worktree by ``bh-after-create``.

DISTINCT from the Python-baton-hooks in ``codereeve.after_create`` /
``codereeve.before_run`` / ``codereeve.after_run`` — those fire
in the daemon's worker turn loop. The hooks in THIS sub-package are
``PreToolUse`` hooks registered in ``.claude/settings.json`` and fired by
Claude Code itself before every tool invocation.

See ``docs/architecture-spec.md`` §3.5 hook table for the canonical list.
"""
