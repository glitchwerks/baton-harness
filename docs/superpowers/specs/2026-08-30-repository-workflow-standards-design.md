# Repository Workflow Standards Design

**Status:** Approved conversational design; awaiting written-spec review

**Issue:** #365

**Date:** 2026-08-30

## Purpose

Standardize how work is proposed, branched, validated, reviewed, and merged in this
repository. The durable policy covers issue forms, the pull-request template, repository
labels, branch naming, fast branch validation, full integration validation, review-bot
selection, and the required PR-policy check. The approved requirements are recorded in
#365.

This design does not add public-contributor process, approval-count requirements,
automatic merging, or automatic reopening of manually closed issues. Those are explicit
non-goals in #365.

## Current State

- The single `CI` workflow runs for pushes and pull requests targeting `main` and
  `feature/**`; it exposes four independent jobs named `Lint (ruff)`, `Type check
  (mypy)`, `Test (pytest)`, and `Lint (shellcheck)`
  (`.github/workflows/ci.yml:L1-L62`).
- A contract test currently requires the CI job names, daemon merge wait list, and
  checked-in target-sandbox ruleset to match exactly
  (`tests/test_required_checks_match_ci_yml.py:L1-L12`,
  `tests/test_required_checks_match_ci_yml.py:L87-L112`).
- Pytest currently has no `fast` marker (`pyproject.toml:L97-L99`).
- The repository has no issue-form or pull-request-template files; `.github/` contains
  only the shared setup action and CI workflow (repository tree at commit `ade7809`).
- The harness has a separate operational state-label vocabulary in code, including
  `agent-ready`, `agent-done`, `blocked`, and `agent-failed`
  (`src/baton_harness/chain/labels.py:L20-L40`), while the operator documentation also
  defines the runtime labels needed in a target repository (`README.md:L247-L266`).
- The live repository rulesets presently require pull requests to the default branch and
  the four existing CI checks, with no required approving reviews
  ([default ruleset](https://api.github.com/repos/glitchwerks/baton-harness/rulesets/15682536),
  [CI ruleset](https://api.github.com/repos/glitchwerks/baton-harness/rulesets/20236089),
  fetched 2026-08-30).

## Workflow Model

Every work item uses a dedicated branch and reaches `main` through a pull request. Branch
names use `<type>/<issue-number>-<slug>`, where `<type>` is one of `feature`, `bug`,
`docs`, `chore`, or `refactor`. This provides GitHub's slash-delimited hierarchical branch
grouping while making the work item's issue number machine-readable. #365 is the authority
for the convention and allowed types.

Large, multi-stream features retain the existing primary/sub-branch model: sub-branch PRs
may target the primary feature branch, while the primary feature PR targets `main`. The
new integration and PR-policy gates apply to PRs whose base is `main`; lightweight branch
validation applies before that integration boundary. This boundary is required by #365.

```text
issue #N
   |
   +-- <type>/N-slug -- fast validation
             |
             +-- PR to main -- PR policy + full CI + optional CodeRabbit
                                      |
                                      +-- merge closes issue
```

## Issue Forms

Three YAML issue forms will be added under `.github/ISSUE_TEMPLATE/`:

1. Bug report, automatically labeled `bug`.
2. Feature request, automatically labeled `enhancement`.
3. General work item, with no automatic type label.

Each form asks for a bounded problem statement, proposed outcome or reproduction details,
and pre-merge acceptance criteria. Form guidance states that acceptance criteria must be
satisfied before merge and must not describe post-merge work. CI intentionally does not
parse acceptance-criteria prose or checkbox state; this keeps the enforcement boundary on
objective repository metadata, as approved in #365.

`.github/ISSUE_TEMPLATE/config.yml` disables blank issues and directs security reports to
a private reporting route. GitHub supports configured issue templates and form routing in
the repository `.github/ISSUE_TEMPLATE` directory
([GitHub issue-template documentation](https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/about-issue-and-pull-request-templates?ref=froilan-irizarry-rivera),
fetched 2026-08-30).

Milestones remain optional when an issue is created. They are required before an issue is
closed through a merged PR. Manual closure follows the same rule as instruction, but no bot
will automatically reopen a manually closed issue. #365 is the authority for this split.

## Pull-Request Template

`.github/PULL_REQUEST_TEMPLATE.md` will require:

- one or more explicit closing directives (`Closes #N`, `Fixes #N`, or `Resolves #N`);
- a concise summary and test evidence;
- documentation impact;
- review classification and reasoning;
- confirmation that acceptance criteria are pre-merge outcomes.

The closing directive is mandatory rather than a plain issue mention because the merge is
the issue-closure boundary established in #365. GitHub discovers a default PR template
from the repository `.github` directory
([GitHub PR-template documentation](https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/creating-a-pull-request-template-for-your-repository?apiVersion=2022-11-28),
fetched 2026-08-30).

## Validation Architecture

### Fast branch validation

A new fast-validation workflow runs on the supported non-main work branches. It performs:

- Ruff lint and format checks;
- ShellCheck;
- selected pytest tests marked `fast`.

`pyproject.toml` will register the `fast` marker. The initial fast set will cover smoke,
CLI, and workflow-contract tests, as required by #365. Tests that exercise the new policy
implementation will be included so invalid metadata fails before the integration PR.

### Main integration validation

The existing `ci.yml` becomes the full integration workflow for PRs to `main`. It retains
the four existing check names and continues to run full Ruff, ShellCheck, mypy, and pytest.
Preserving those names avoids breaking the current three-source contract documented by
`tests/test_required_checks_match_ci_yml.py:L1-L12` and the live CI ruleset cited above.

GitHub Actions supports branch filters on `push` and `pull_request` events; the workflows
will use those filters to make the base-branch boundary explicit
([GitHub workflow-trigger documentation](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow),
fetched 2026-08-30).

## PR Policy Check

A separate `PR policy` workflow runs for pull requests to `main`. A small Python policy
module receives normalized PR metadata from the workflow and returns actionable failures.
Keeping policy logic in Python permits focused unit tests without requiring live GitHub
events; #365 requires both the check and automated coverage.

The check fails when any of these conditions is true:

1. The head branch does not match
   `^(feature|bug|docs|chore|refactor)/[0-9]+-[a-z0-9]+(?:-[a-z0-9]+)*$`.
2. The PR body has no explicit `Closes`, `Fixes`, or `Resolves` directive for an issue in
   this repository.
3. The issue number encoded in the branch name is not among those closing directives.
4. Any issue named by a closing directive has no milestone.

The workflow receives read-only pull-request and issue access and does not mutate PRs,
issues, labels, or milestones. It reports every detected policy violation in one run so the
author can correct them together. These enforcement rules and the read-only posture are
approved in #365.

After the workflow has produced its check on the implementation PR, the live repository
CI ruleset will add `PR policy` as a required status check. GitHub rulesets can require
named status checks before the protected ref is updated
([GitHub ruleset documentation](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets?ref=jscarle.dev),
fetched 2026-08-30). The checked-in `config/ruleset.main.json` remains unchanged because it
configures target-sandbox daemon behavior, as evidenced by the current contract test's
description and path (`tests/test_required_checks_match_ci_yml.py:L57-L79`).

## Review Selection

CodeRabbit review is opt-in by the PR-only label `needs-review`:

- feature PRs always receive `needs-review`;
- docs-only PRs never receive it;
- bug PRs receive it when the investigation finds a high-risk boundary such as security,
  authorization, identity, secrets, permissions, persistence, or concurrency;
- chore and refactor PRs use the same risk-based judgment.

The submitter records that classification and reasoning in the PR template. Review remains
advisory and is not a required merge check. Both the rubric and advisory status are
approved in #365.

`.coderabbit.yaml` will disable automatic reviews globally and configure
`needs-review` as the opt-in label. CodeRabbit documents that label-triggered reviews can
be enabled while automatic review is disabled
([CodeRabbit auto-review configuration](https://docs.coderabbit.ai/configuration/auto-review),
fetched 2026-08-30). No issue receives `needs-review`; the investigation establishes the
actual PR scope before the label decision, per #365.

## Repository Labels

The user-facing label set will be:

- work type: `bug`, `enhancement`, `documentation`;
- workflow: `needs-decision`, `needs-review`;
- triage/resolution: `duplicate`, `invalid`, `question`, `wontfix`;
- retained project label: `operator-ergonomics`.

`good first issue` and `help wanted` will be removed. No `type:*`, `area:*`, or other
public-contribution labels will be introduced. The operational daemon labels remain
untouched because they participate in the state model shown in
`src/baton_harness/chain/labels.py:L20-L40`. The complete repository-label decision is
recorded in #365.

## Documentation and Agent Instructions

`docs/contributing.md` will explain the human workflow, branch convention, validation
boundary, milestone closure requirement, closing-directive requirement, and review rubric.
`AGENTS.md` and `CLAUDE.md` will codify the same rules for agents. Their policy statements
will be kept equivalent so either instruction entry point leads to the same behavior, as
required by #365.

The README will be updated only if the implementation changes commands or development
steps exposed to a new developer. The operational-label section remains intact because it
documents target-repository runtime prerequisites (`README.md:L247-L266`).

## Testing and Verification

Implementation is complete only after all of the following pass:

1. Unit tests for valid and invalid branch names, closing-directive parsing, branch/issue
   matching, multiple closing issues, and missing milestones.
2. Contract tests for issue forms, the PR template, workflow triggers, stable full-CI check
   names, the registered `fast` marker, and CodeRabbit opt-in configuration.
3. Fast validation locally, followed by the complete existing test suite and static checks.
4. A GitHub pull request showing the four existing full-CI checks plus a successful
   `PR policy` check on the actual head commit.
5. A live repository audit confirming the intended label set and required-check ruleset.

The PR body will use `Closes #365`; issue #365 must have a milestone before merge. These
verification criteria restate #365 and preserve the existing named-check contract at
`tests/test_required_checks_match_ci_yml.py:L87-L112`.

## Rollout Order

1. Add tests and the policy implementation.
2. Add templates, CodeRabbit configuration, and the split validation workflows.
3. Update agent and human documentation.
4. Open the implementation PR, label it `needs-review`, and verify the new check runs.
5. Add `PR policy` to the live CI ruleset only after GitHub has observed that check name.
6. Reconcile live repository labels, removing only the two retired contribution labels.
7. Merge after all required checks succeed; the PR closes #365.

This order prevents the ruleset from requiring a check that GitHub has not yet observed,
while keeping all live mutations tied to the reviewed implementation. The required end
state and mutation scope are defined by #365; GitHub's named-status-check behavior is
documented in the ruleset source cited above.
