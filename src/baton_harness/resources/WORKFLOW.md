---
tracker:
  kind: github
  labels: ["agent-ready"]
  exclude_labels: ["blocked"]
polling:
  interval_ms: 30000
agent:
  max_concurrent: 2
  max_turns: 8
  command: claude
  permission_mode: bypassPermissions
hooks:
  after_create: . "$BH_VENV/bin/activate" && bh-after-create
  before_run: . "$BH_VENV/bin/activate" && bh-before-run
  after_run: . "$BH_VENV/bin/activate" && bh-after-run
---
<!--
NOTE on hook categories (slice 3b — issue #157):
The `hooks:` block above lists the THREE Python-baton-hooks fired by the
daemon's worker turn loop (after_create / before_run / after_run).

There is a SECOND category — Claude Code PreToolUse hooks — installed
per-worktree by bh-after-create via a generated .claude/settings.json.
The current PreToolUse hook is `force-pr-not-merge`
(`src/baton_harness/hooks/force_pr_not_merge.py`); it is paired with the
branch ruleset provisioned via `bin/provision-ruleset.sh`. See
docs/architecture-spec.md §3.5 for the canonical list.
-->
You are working on issue #{{ issue.number }}: {{ issue.title }}

{{ issue.body }}

---

## Confidence / block rule (read before doing anything)

If ANY acceptance criterion in this issue has more than one reasonable
interpretation, do NOT implement. Post a comment on the issue with your
specific question (quote the exact criterion that is ambiguous), add the
`blocked` label, and STOP immediately. Do not guess. Do not proceed with
the most-likely interpretation. The cost of a wrong implementation is higher
than the cost of one clarification round.

---

## Required closing steps

When your implementation is complete and all tests pass, execute the
following steps **in order**. These steps are REQUIRED — do not skip or
reorder them.

1. **Stage all changes.**
   Run `git add -A` (or stage files individually). Verify with `git status`
   that nothing is left unstaged. If `git status` shows unexpected files,
   investigate before continuing. Report if this step fails and STOP.

2. **Commit.**
   Run `git commit -m "<short description> — closes #{{ issue.number }}"`.
   The commit message must reference the issue number. Report if this step
   fails and STOP.

3. **Push.**
   Run `git push -u origin HEAD`. Report if this step fails and STOP.

4. **Open a draft PR.**
   Run:
   ```
   gh pr create --draft \
     --base "$BH_FEATURE_BRANCH" \
     --title "<short description>" \
     --body "Closes #{{ issue.number }}"
   ```
   The PR body MUST contain the plain-text `Closes #{{ issue.number }}` so
   GitHub auto-closes the issue on merge. Report if this step fails and STOP.

   **Do NOT merge any pull request.** Open a draft PR only; the harness owns
   all merges. Never run `gh pr merge` or any merge tool.

If any step above fails, report exactly which step failed and what the error
output was, then STOP. Do not attempt to work around a failing step silently.
