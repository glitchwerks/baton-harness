# Contributing

This guide describes the repository workflow for all changes.

## Issue first

Create or identify a GitHub issue before starting work. A milestone is optional when the
issue is created, but it is required before the issue closes. Define acceptance criteria
as outcomes that must be complete before merge; do not use them to describe post-merge
work.

## Branches

Create a dedicated branch from the current `main` using
`<type>/<issue-number>-<slug>`. The allowed types are `feature`, `bug`, `docs`, `chore`,
and `refactor`. For a large feature with multiple work streams, sub-branch pull requests
target the primary feature branch; the primary feature pull request targets `main`.

## Validation

Run fast validation on the work branch while iterating. Before integrating a pull request
to `main`, run the full integration validation suite; it is the boundary for the complete
quality gate.

## Pull requests

Open a pull request for every change that reaches `main`; never push or merge directly to
`main`. Include an explicit `Closes #N`, `Fixes #N`, or `Resolves #N` directive. The issue
number in the branch name must be one of the issues named by those directives, and every
issue being closed must have a milestone.

## Review

Apply the `needs-review` PR label to every feature pull request and never to a docs-only
pull request. For bug, chore, and refactor pull requests, evaluate whether the change
touches security, authorization, identity, secrets, permissions, persistence, or
concurrency before deciding whether to apply the label. CodeRabbit feedback is advisory.

## Merge

Merge only after all required checks pass. Acceptance criteria are pre-merge requirements;
do not leave post-merge acceptance criteria. Never merge directly to `main`.

Sources: #365; [approved repository workflow standards design](superpowers/specs/2026-08-30-repository-workflow-standards-design.md).
