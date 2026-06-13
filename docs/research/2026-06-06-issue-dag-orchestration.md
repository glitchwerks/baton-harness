## Idea

Design a dependency-chain orchestration layer in baton-harness that reads a GitHub issue DAG, works each ready issue on its own branch, merges completed branches into a shared feature branch, and unblocks dependents — all without merging to `main`.

## Requirements

1. Read an issue dependency graph from GitHub's native API (not a convention baked into issue body text).
2. Topologically sort issues and identify which are ready (all blockers closed/done).
3. Create per-issue branches off a shared feature branch, not off `main`.
4. Merge completed per-issue branches into the feature branch (CI-gated), not into `main`.
5. After each merge, unblock dependent issues (flip their readiness state) and queue them.
6. Repeat until no more issues are workable; leave feature branch for human review.
7. Operate within the existing Python 3.11 / `uv` / `gh` CLI / `mcp__github__*` toolchain.
8. Must not require preview or waitlisted GitHub features.

## Search axes used

- **Direct synonyms**: GitHub issue dependencies API, `blocked_by`, `blocking`, issue dependency graph, sub-issues, task lists
- **Problem-shape synonyms**: DAG-based task orchestration, topological sort scheduling, dependency-ordered issue execution, ready-queue with blocking
- **Adjacent domains**: stacked PRs / stacked diffs (Graphite, Aviator, spr, gh-stack), data pipeline orchestrators (Airflow DAG patterns), multi-agent coding orchestrators (ComposioHQ/agent-orchestrator, continuous-claude)
- **Vendor-specific phrasing**: GitHub Projects v2 fields, `sub_issues` REST endpoint, `addBlockedBy` GraphQL mutation, `mcp__github__*` MCP tool gap for relationships
- **Negative axes**: cross-repo dependencies (confirmed not supported), Projects v2 as the dependency source of truth (confirmed overkill — dependency data lives on the issue itself), squash-merge for integration branch (confirmed harmful for stacked branches)

---

## Shortlist (ranked by expected value)

### 1. GitHub Issue Dependencies REST API — native `blocked_by` / `blocking` graph, GA since Aug 21 2025

- **URL:** https://docs.github.com/en/rest/issues/issue-dependencies?apiVersion=2026-03-10 (fetched 2026-06-06)
- **Changelog GA announcement:** https://github.blog/changelog/2025-08-21-dependencies-on-issues/ (fetched 2026-06-06)
- **Relevance:** addresses requirements 1, 2, 5. The REST API is the canonical programmatic surface for reading and mutating the dependency graph.
- **Maturity:** GA as of 2025-08-21. Public preview ran from 2025-07-10 to 2025-08-21. Standard REST, no special accept header required beyond `X-GitHub-Api-Version: 2026-03-10`.
- **Worth borrowing:**
  - `GET /repos/{owner}/{repo}/issues/{issue_number}/dependencies/blocked_by` — returns array of Issue objects that block this issue. Paginated (default 30, max 100).
  - `GET /repos/{owner}/{repo}/issues/{issue_number}/dependencies/blocking` — returns array of Issue objects this issue is blocking.
  - `POST /repos/{owner}/{repo}/issues/{issue_number}/dependencies/blocked_by` with body `{"issue_id": <int>}` — adds a blocker. Returns 201 on success, 422 on validation failure.
  - `DELETE /repos/{owner}/{repo}/issues/{issue_number}/dependencies/blocked_by/{issue_id}` — removes a blocker.
  - GraphQL equivalents: `addBlockedBy` and `removeBlockedBy` mutations (confirmed GA by community discussion https://github.com/orgs/community/discussions/165749, fetched 2026-06-06).
  - Issue search filter `is:blocked` and `blocked-by:<number>` for cheap pre-flight queries.
- **What to avoid:**
  - Cross-repository dependencies: confirmed same-repo only for non-enterprise users. Enterprise Managed Users (EMU) face FORBIDDEN errors even within their org. Since baton-harness operates on a single repo, this is not a blocking constraint for the current use case but rules out any future multi-repo extension.
  - Secondary rate limiting on `POST` and `DELETE` operations if programmatically bulk-setting dependencies — space them out.
  - The `mcp__github__*` MCP server does **not** yet expose issue relationships: issue #950 on `github/github-mcp-server` (https://github.com/github/github-mcp-server/issues/950, fetched 2026-06-06) is open with a draft PR (#1927) but unmerged as of 2026-06-06. Use `gh` CLI REST calls or direct HTTP for dependency reads in the harness.
- **Lift effort:** drop-in — standard REST calls, no library needed beyond `requests` or `gh api`.

---

### 2. GitHub Sub-Issues REST API — parent/child hierarchy, GA

- **URL:** https://docs.github.com/en/rest/issues/sub-issues (fetched 2026-06-06)
- **Relevance:** addresses requirement 1 (alternative DAG encoding). Sub-issues give a parent/child tree; `blocked_by`/`blocking` gives a peer dependency graph. These are orthogonal mechanisms.
- **Maturity:** GA. REST API available at `X-GitHub-Api-Version: 2026-03-10`. Up to 50 sub-issues per parent (one source says 100; the REST doc says 50 — treat 50 as the safe limit). Up to 8 levels of nesting.
- **Worth borrowing:**
  - `GET /repos/{owner}/{repo}/issues/{issue_number}/sub_issues` — returns children of a parent issue.
  - `GET /repos/{owner}/{repo}/issues/{issue_number}/parent` — returns the parent of a sub-issue. Useful for walking the tree upward.
  - The `sub_issues_summary` field on every Issue response object gives completion progress without traversal.
  - Pattern: use sub-issues to group all issues belonging to a feature under a single parent (the "feature epic"), and use `blocked_by`/`blocking` for the execution-order dependency graph within that group. These two mechanisms compose cleanly.
- **What to avoid:** Sub-issues encode hierarchy (grouping), not execution order. Do not rely on sub-issue ordering as a proxy for dependency ordering — use `blocked_by` for that.
- **Lift effort:** drop-in — same REST pattern as the dependency API.

---

### 3. Python `graphlib.TopologicalSorter` — stdlib DAG scheduler with ready-queue API

- **URL:** https://docs.python.org/3/library/graphlib.html (fetched 2026-06-06)
- **Relevance:** addresses requirement 2. This is the mechanism for computing which issues are ready to work given the dependency graph fetched from the GitHub API.
- **Maturity:** stdlib since Python 3.9 (well within the project's Python 3.11 requirement). No external dependency.
- **Worth borrowing:**
  - `TopologicalSorter(graph)` where `graph` is `{node: [predecessors]}` — construct from the GitHub `blocked_by` edges.
  - `.prepare()` then `.get_ready()` returns the set of nodes with all predecessors done — maps directly to "which issues have no open blockers."
  - `.done(node)` marks a node complete, which makes its dependents eligible for `get_ready()` on the next call — maps to "issue merged into feature branch, unblock dependents."
  - `.is_active()` as the loop termination condition.
  - Cycle detection is built in: raises `CycleError` if the graph has a cycle, which is a useful guard against misconfigured dependency chains.
- **What to avoid:** `TopologicalSorter` does not handle partial failures — if an issue fails mid-chain, the caller must decide which downstream nodes to skip. The stdlib class itself will still report them as ready. The harness must track a separate "failed" set and filter `get_ready()` output against it.
- **Lift effort:** drop-in (stdlib, no install).

---

### 4. Graphite — stacked PR tooling with sequential merge queue, merge-commit preferred for integration branches

- **URL:** https://graphite.com/guides/stacked-diffs (fetched 2026-06-06); merge strategy guide: https://graphite.com/blog/pull-request-merge-strategy (fetched 2026-06-06)
- **Relevance:** addresses requirements 3, 4, 5. Graphite is the most mature production tool for the "merge into integration branch sequentially" pattern.
- **Maturity:** Production SaaS + CLI. Free tier available without waitlist. Active development. MIT-licensed CLI.
- **Worth borrowing:**
  - The **bottom-up merge rule**: always merge the lowest PR in a stack first. Attempting to merge out of order creates ghost diffs. The harness must enforce the same constraint — merge per-issue branches in dependency order.
  - The **`gt sync` + `gt submit` restack pattern**: after any branch merges into the integration branch, all branches that depended on it must be rebased. The harness should run `git rebase --onto <feature-branch> <old-base>` on each dependent issue branch after an issue branch merges.
  - The **merge-commit-preferred finding**: Graphite's own documentation and the broader stacked-PR community confirm that squash merging an integration branch creates git history divergence that forces `--onto` rebasing of all dependent branches. Using standard merge commits (not squash) for the feature branch merges keeps dependent branches' histories clean and avoids phantom diffs.
  - The merge queue documentation describes gating each merge on CI passing before the next branch becomes eligible — directly applicable to the harness's step 5 sequence.
- **What to avoid:** Graphite is a human-workflow tool with a CLI designed for interactive use. The harness should not attempt to drive the Graphite CLI programmatically — borrow the *pattern*, not the tool. The SaaS merge queue requires paying for the Graphite service.
- **Lift effort:** adapt-pattern (pattern is well-documented; reimplement the rebase logic natively in the harness using `git` CLI calls).

---

### 5. ComposioHQ/agent-orchestrator — agent-per-issue orchestrator with worktree isolation

- **URL:** https://github.com/ComposioHQ/agent-orchestrator (fetched 2026-06-06; v0.9.2, last commit May 23 2026)
- **Relevance:** addresses requirements 3, 4 (partially). Shows a working pattern for spawning one autonomous agent per GitHub issue in an isolated git worktree.
- **Maturity:** 7.4k stars, MIT, TypeScript, v0.9.2, actively maintained as of May 2026.
- **Worth borrowing:**
  - The **one-worktree-per-issue isolation pattern**: each agent gets its own `git worktree` with its own branch. This is the same worktree convention the baton-harness already uses. Confirms the pattern at scale.
  - The **agent-agnostic interface**: the orchestrator abstracts over Claude Code, Codex, Aider, etc. If baton-harness ever needs to swap agents, a similar interface boundary is worth designing in.
  - The **reactive CI loop**: the orchestrator monitors CI status and routes failures back to the agent — the harness's post-merge CI gating is a simplified version of this pattern.
- **What to avoid:** This tool does not implement dependency ordering between issues — it spawns agents in parallel without sequencing. For baton-harness, issues must be serialized by dependency order. Also TypeScript, so no direct code reuse from a Python harness.
- **Lift effort:** study-only — borrow the worktree pattern and CI feedback loop concept; do not attempt to drive or wrap this tool.

---

### 6. `ejoffe/spr` — commit-per-PR stacked pull request tool

- **URL:** https://github.com/ejoffe/spr (fetched via search 2026-06-06)
- **Relevance:** addresses requirement 4 (branch sequencing). SPR maps each commit on a single branch to a separate PR, merges from the top of the stack down, and closes intermediate PRs. Different topology from what baton-harness needs (baton has separate branches, not commits-on-one-branch).
- **Maturity:** Established Go CLI, long-lived project, CLI-driven.
- **Worth borrowing:** The "find top mergeable PR in the stack, merge it, close intermediates" logic — the harness needs a similar "find topologically-next mergeable issue branch" query. SPR's approach of combining all commits up to the mergeable point is relevant if the harness ever adopts a linear-commit model.
- **What to avoid:** SPR's commit-stack model does not match the harness's branch-per-issue model. The merge direction (stack top → trunk) is also inverted relative to what Graphite recommends (stack bottom → trunk). Do not adopt SPR's merge direction.
- **Lift effort:** study-only.

---

### 7. `github/gh-stack` — GitHub's native stacked PR CLI extension (private preview)

- **URL:** https://github.com/github/gh-stack (fetched 2026-06-06; v0.0.5, May 26 2026)
- **Relevance:** addresses requirements 3, 4. JSON output (`gh stack view --json`) and exit codes (0-8) make it scriptable. The `--auto` flag exists for non-interactive operation.
- **Maturity:** **Private preview as of 2026-06-06.** Requires waitlist enrollment. Repository-level feature flag. MIT license.
- **Worth borrowing:** The `gh stack link` command creates stack relationships without requiring local tracking — relevant if the harness manages branches externally. The exit-code contract is useful for scripted merge decisions.
- **What to avoid:** **Do not build on this in the harness now** — private preview means the feature flag is not enabled by default and is not production-safe. Revisit when GA. Also, gh-stack targets merging into `main`, not into an integration/feature branch; the `--onto` behavior for non-`main` targets is not documented.
- **Lift effort:** study-only until GA; then potentially drop-in for the branch creation/tracking layer.

---

### 8. `Dependent Issues` GitHub Action — body-text dependency detection with labeling

- **URL:** https://github.com/marketplace/actions/dependent-issues (fetched 2026-06-06; v1.5.2, 170 stars, 7 contributors)
- **Relevance:** addresses requirements 2, 5 (partially). This Action reads body text for "depends on"/"blocked by" keywords and applies labels. It is the *prior approach* before the GitHub native dependency API existed.
- **Maturity:** Active independent project, not GitHub-official.
- **Worth borrowing:** The label-as-gate pattern: the action marks an issue with a `dependent` label when its dependencies are open, and removes the label when they close. The harness's `agent-ready` label is structurally identical — this Action proves the pattern works at GitHub's event model level and could be composed with the native API to auto-flip the `agent-ready` label after a dependency closes.
- **What to avoid:** This Action reads from issue *body text*, not the native GitHub dependency API. It was a workaround for the old lack of API support. Now that the native API is GA, body-text parsing is the wrong source of truth. Do not use this as the primary DAG data source.
- **Lift effort:** adapt-pattern — the label-flip-on-dependency-close webhook pattern is worth reusing; do not use the body-text parsing.

---

## No prior art found

- **CI-gated merge into a non-`main` feature branch, then unblocking dependent issues automatically.** Searched: Graphite docs, Aviator docs, gh-stack docs, agent-orchestrator, continuous-claude. All existing stacked-PR tools target `main` (or `trunk`) as the merge destination. The pattern of merging a dependent chain sequentially into a *shared integration branch* (not trunk), then leaving that branch for human review, appears to be novel. The harness must implement this layer from scratch. The closest analogy is Graphite's stack-aware merge queue, but Graphite's target is always trunk.
- **Automatic re-application of `agent-ready` label via the GitHub dependency API webhook.** Searched: GitHub Actions marketplace, GitHub webhooks docs, community discussions. No existing Action or tool reads the native `blocked_by`/`blocking` API webhooks to flip a readiness label. The `Dependent Issues` Action does the label-flip but from body-text, not the API. Webhook events for dependency creation/removal (`issues` event with `dependency_added`/`dependency_removed` action types — if they exist) need to be verified against the webhook payload docs before designing this.
- **Dependency ordering enforcement across baton/symphony's issue loop.** Baton has no dependency awareness at all. This must be implemented entirely in the harness, upstream of any Baton call.

---

## Recommended handoff

- `project-planner` — use candidates 1, 2, and 3 as the foundation: the REST dependency API for the DAG source of truth, the sub-issues API for feature grouping, and `graphlib.TopologicalSorter` for ready-queue scheduling. The planner should design the orchestration loop around these three drop-in components. Candidates 4 (Graphite pattern) and 5 (ComposioHQ worktree pattern) inform the branch-management and CI-gating sections of the plan — adapt the merge-commit-not-squash finding and the one-worktree-per-issue pattern directly.
- `user` — two open questions require a decision before planning can finalize (see Open Questions below).

---

## Open questions

1. **Do the GitHub `issues` webhooks fire events for dependency creation/removal?** The changelog GA announcement does not enumerate new webhook action types. The harness's "unblock dependent issues" step (requirement 5/7) could be event-driven (webhook) or poll-driven (`GET .../blocking` after each merge). The poll-driven approach works today and requires no new webhook infrastructure; the event-driven approach is cleaner but unverified. Someone should check the GitHub webhook payload docs for `dependency_added` / `dependency_removed` action types on the `issues` event.

2. **Which issues constitute "the DAG" — are they identified by label, milestone, sub-issue parent, or all three?** The research confirms all three mechanisms are available (sub-issues for grouping, `blocked_by` for ordering, labels for readiness gating). The planner needs the user to decide: should the harness scope the DAG to a specific parent issue's sub-issues, to a milestone, to a label (e.g. `feature-X`), or to some combination? This decision determines what the harness queries first.

3. **Cross-repo dependency scope is same-repo only (confirmed limitation).** No action required now since baton-harness operates on one repo. Worth noting for documentation so the team does not design a cross-repo use case assuming the API supports it.

4. **`github/gh-stack` is in private preview.** The harness should not depend on it. If it reaches GA before feature #27 is implemented, it is worth re-evaluating as a drop-in branch-stack layer for the `gh stack link` command.

5. **GitHub MCP server (`mcp__github__*`) does not expose dependency relationships.** PR #1927 on `github/github-mcp-server` is open but unmerged. Until that PR merges, any harness code that needs to read `blocked_by`/`blocking` must use `gh api` REST calls directly, not the MCP tool. This is a minor tactical note, not a blocker.
