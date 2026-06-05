"""Baton harness — policy and tooling layer for autonomous agent runs.

This package provides the lifecycle hook entry points invoked by Baton
during each agent run cycle:

- ``after_create`` — per-worktree dependency setup after worktree creation.
- ``before_run`` — branch sync onto ``main`` before the agent executes.
- ``after_run`` — outcome classification and GitHub label reconciliation.

The shared CLI helpers (logging, issue-number resolution) live in
``_cli.py`` and are imported by every hook module.

See ``docs/harness-design.md`` for the architecture and integration model.
"""

__version__ = "0.1.0"
