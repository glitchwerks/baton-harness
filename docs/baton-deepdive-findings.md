# Baton upstream deep-dive — architecture decision findings
**Date:** 2026-06-06  
**Scope:** issue #27 build-vs-fork architecture decision  
**Source:** full read of `I:/ai/claude/baton-upstream/symphony/*.py`, harness hooks, and chain spec

---

## Q1 — Shape of entrypoints. How would we call them?

### 1.1 CLI wire-up

`pyproject.toml:L16`:
```toml
[project.scripts]
baton = "symphony.cli:main"
```

`symphony/cli.py:L15-L19` — `main` is a Click group. The `start` subcommand
(`cli.py:L25-L70`) is:

```python
@main.command()
@click.option("--workflow", "-w", default="WORKFLOW.md")
@click.option("--verbose", "-v", is_flag=True)
def start(workflow: str, verbose: bool):
```

Wire-up trace:
1. Reads `config/WORKFLOW.md` via `load_workflow(workflow_path)` → `WorkflowConfig`
2. Builds `Orchestrator(config, project_root, state_path, workflow_path)`
3. Creates a new event loop (`asyncio.new_event_loop()`) and runs `orch.run()`
4. `orch.run()` is the infinite poll loop — never returns until SIGINT/SIGTERM

### 1.2 Is there a clean "run ONE issue" callable?

**Yes, but with caveats.** The function is:

```python
# orchestrator.py:L102-L182
async def _run_worker(self, issue: Issue) -> str:
    wt = await self.workspace.ensure_worktree(issue.number, title=issue.title)
    if wt.created_now:
        ok = await run_hook("after_create", self.config.hook_after_create, cwd=wt.path, ...)
    ok = await run_hook("before_run", self.config.hook_before_run, cwd=wt.path, ...)
    issue_skills = parse_issue_skills(issue.body)
    for turn in range(1, self.config.max_turns + 1):
        result = await self.worker.run_turn(prompt=..., cwd=wt.path, ...)
        ...
    await run_hook("after_run", self.config.hook_after_run, cwd=wt.path, ...)
    pr_exists = await self.tracker.check_pr_exists(issue.number)
    return "pr_created" if pr_exists else "no_pr"
```

**Signature summary:** `async def _run_worker(self, issue: Issue) -> str`

The `issue` parameter is a `symphony.tracker.Issue` dataclass
(`tracker.py:L17-L59`) — a plain `@dataclass` with fields `number`, `title`,
`state`, `body`, `url`, `labels`, `assignees`.

**Can it be called without starting the poller?** Technically yes — it is an
`async` method on `Orchestrator`. Nothing inside it requires the `run()` loop to
be active. However:

**Global state / context the path requires** (all mandatory):

| Dependency | Where it comes from | Callable import-path |
|---|---|---|
| `WorkflowConfig` | `symphony.config.load_workflow(path)` | `symphony.config` |
| `WorkspaceManager` | `Orchestrator.__init__` sets `self.workspace` | `symphony.workspace.WorkspaceManager` |
| `GitHubTracker` | `Orchestrator.__init__` sets `self.tracker` | `symphony.tracker.GitHubTracker` |
| `Worker` | `Orchestrator.__init__` sets `self.worker` | `symphony.worker.Worker` |
| `OrchestratorState` | `Orchestrator.__init__` sets `self.state` | `symphony.state.OrchestratorState` |
| asyncio event loop | caller must be `async` or call via `asyncio.run()` | stdlib |
| `gh` CLI on PATH | `GitHubTracker` uses `asyncio.create_subprocess_exec("gh", ...)` | external |
| `claude` CLI on PATH | `Worker.run_turn` uses `asyncio.create_subprocess_exec(config.agent_command, ...)` | external |

There is no config singleton or import-time side effect — `Orchestrator.__init__`
is a plain constructor. An external caller could do:

```python
import asyncio
from symphony.config import load_workflow
from symphony.orchestrator import Orchestrator
from symphony.tracker import Issue

config = load_workflow("path/to/WORKFLOW.md")
orch = Orchestrator(config, project_root="/path/to/repo",
                   state_path="/path/to/.symphony/state.json")
issue = Issue(number=42, title="...", state="open", body="...", url="...",
              labels=["agent-ready"])
result = asyncio.run(orch._run_worker(issue))
```

**Practical blockers to calling it this way from the chain driver:**

1. `_run_worker` is name-mangled by Python convention (single underscore, not
   technically private, but signals "internal"). More importantly:
2. The `OrchestratorState` (`self.state`) is mutated inside `_run_worker` to
   track `turn` (`orchestrator.py:L129-L131`). Calling it directly bypasses the
   `_dispatch` bookkeeping that initialises `IssueState` in `self.state.running`
   before `_run_worker` fires. This means the turn-update at `L129` will silently
   no-op (key miss in `self.state.running`), but that is not fatal — it is just
   the `baton status` command that would break, not the actual run.
3. The `_on_worker_done` callback is wired in `_dispatch` (`L60`) — it will NOT
   be called if you invoke `_run_worker` directly. That callback handles retry
   scheduling and state persistence. You would need to implement equivalent
   outcome-handling in the caller.
4. The `after_run` hook is called **at the end of every run** (both on success
   and failure paths — `orchestrator.py:L155-L158` and `L172-L175`). If we
   import `_run_worker`, the harness `after_run` will still fire (it is invoked
   via `run_hook` inside `_run_worker`), and label reconciliation will happen as
   normal — which is what we want.

**Verdict:** A callable `_run_worker` seam exists. It is importable and async-runnable
without starting the poller. The caveats are: (a) you must initialise `OrchestratorState`
with the right `IssueState` entry first (or accept that turn tracking in state.json is
broken), and (b) you must handle the `_on_worker_done` equivalent yourself (retry
scheduling, state persistence). The seam is real but **not designed for external
consumption** — it is a private implementation detail of the dispatch loop.

### 1.3 How hooks are invoked

All three hooks are invoked via `symphony.hooks.run_hook`:

```python
# hooks.py:L10-L44
async def run_hook(name: str, script: str | None, cwd: str, timeout_ms: int = 60000) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "bash", "-lc", script, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
```

- Hook value is a **shell string** from `WorkflowConfig` (e.g.
  `config.hook_after_create`). Baton runs it via `bash -lc <script>`.
- **`after_create`** — once per issue, only when `wt.created_now` is True
  (`orchestrator.py:L107`). Never called on a re-run against an existing worktree.
- **`before_run`** — once per outer `_run_worker` call, before the turn loop
  (`orchestrator.py:L115-L119`). NOT called once-per-turn.
- **`after_run`** — called at the END of `_run_worker`, in two places:
  - On turn failure (`orchestrator.py:L155-L158`, "best effort")
  - After the turn loop exits normally (`orchestrator.py:L172-L175`)
  So `after_run` is **once per outer run** (per `_run_worker` call), not
  once per turn. This matches the harness design intent (outcome classification
  is meaningful once, at the end of a run, not per-turn).
- **cwd** for all hooks: `wt.path` — the worktree directory. Baton passes NO
  environment variables to hooks (spike finding F2 confirmed by reading the
  `run_hook` call — only `cwd` is passed, no `env=` kwarg).
- **args:** hooks receive no positional arguments (they are `bash -lc` strings,
  not scripts with argv). The issue number is not passed; hooks must infer it
  from `basename(cwd)` as the harness `_cli.py:resolve_issue_number` does.

### 1.4 Worktree creation and naming

`symphony/workspace.py`:

```python
def branch_name(self, issue_number: int, title: str) -> str:
    slug = slugify(title)          # workspace.py:L42-L48
    if slug:
        return f"baton/{slug}-{issue_number}"
    return f"baton/issue-{issue_number}"

def worktree_path(self, issue_number: int) -> str:
    return os.path.join(self.worktrees_dir, str(issue_number))
    # => <project_root>/.symphony/worktrees/<issue_number>
```

Worktree path is `<project_root>/.symphony/worktrees/<integer>` — a bare integer.
Branch is `baton/<slug>-<issue_number>` (or `baton/issue-<N>` if no slug).
Creation (`workspace.py:L72-L98`): `git worktree add -b <branch> <path> HEAD`; on
failure, tries `git worktree add <path> <branch>` (branch already exists case).

### 1.5 Claude subprocess spawn (the worker loop body)

`symphony/worker.py:L74-L167`, specifically `Worker.run_turn`:

```python
async def run_turn(self, prompt: str, cwd: str,
                   issue_skills: list[str] | None = None,
                   timeout_ms: int = 3600000) -> WorkerResult:
    args = [self.config.agent_command, "-p", prompt, "--output-format", "json"]
    # optionally adds: --permission-mode acceptEdits / --dangerously-skip-permissions
    # optionally adds: --mcp-config <tmp_json_file>
    proc = await asyncio.create_subprocess_exec(*args, cwd=cwd, stdout=PIPE, stderr=PIPE)
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
```

Key observations:
- `claude -p <prompt> --output-format json` is the invocation. Output parsed as
  JSON; `parsed.get("result")` extracted.
- **Skills from issue body** (`## Skills` section, `tracker.py:L78-L95`) are
  parsed and used to build an MCP config written to a temp JSON file, passed as
  `--mcp-config`. Skills do NOT translate to `--skill` CLI flags.
- `timeout_ms` in the orchestrator call is `self.config.max_retry_backoff_ms`
  (default 300 000 ms = 5 min) — this is the OUTER turn timeout, not a per-turn
  limit (a bug noted in the code: the field name is misleading).

---

## Q2 — How much does our current #27 plan duplicate Baton?

### 2.1 Module-by-module table

| Chain module | Equivalent in Baton? | Verdict |
|---|---|---|
| `chain/gh_deps.py` — reads `blocked_by`/`blocking` via `gh api` | No. `GitHubTracker` (`tracker.py`) only does `gh issue list`, `gh issue view`, `gh pr list`. No dependency API calls exist. | **Net-new** |
| `chain/dag.py` — builds `{issue: [blockers]}`, cycle detection via `graphlib` | No. Baton has no graph or dependency model. | **Net-new** |
| `chain/scheduler.py` — `graphlib.TopologicalSorter` wrapper, ready frontier, mark_done/failed, sub-tree halt | No. Baton's "scheduler" is a simple slot check (`_should_dispatch`) with no ordering. | **Net-new** |
| `chain/branches.py` — create feature branch off main; per-issue branch off feature branch | **Partial overlap.** `WorkspaceManager.ensure_worktree` (`workspace.py:L72-L98`) does `git worktree add -b baton/<slug>-<N> ... HEAD` — always off HEAD (main). The chain needs branches off a _feature branch_, not main/HEAD. Naming convention also differs (`baton/<slug>-<N>` vs harness `<prefix>-<N>[-slug]`). Baton has the worktree + branch mechanics; we need a different base ref and naming. | **Partial overlap — must rewrite the base-ref logic** |
| `chain/merge.py` — `git merge --no-ff` into feature branch; CI gate per-issue PR | No. Baton never merges anything. After a PR is found, it releases the claim and moves on (`orchestrator.py:L86-L87`). No merge, no CI query. | **Net-new** |
| `chain/driver.py` — orchestration loop: promote → wait → CI-gate → merge → mark_done | **Conceptually overlaps `orchestrator.py`'s `_run_worker` + `run` loop**, but the chain loop is cross-issue, not per-issue. Baton's loop picks up `agent-ready` issues; the chain driver promotes them one-at-a-time and gates on merge. The cross-issue DAG traversal, serial promotion, wait-for-terminal-label, and merge step are all absent in Baton. | **Net-new at the cross-issue layer** |
| `chain/cli.py` — `bh-chain` argparse entrypoint | No equivalent. `baton start` is the only entrypoint; no "run one chain" concept. | **Net-new** |
| `_cli.py` (edits) — shared `run()` helper | Baton has `workspace.run_cmd` (`workspace.py:L26-L39`) and `tracker.run_gh` (`tracker.py:L62-L75`). Harness has per-hook `_run` functions. Consolidating these is a refactor we'd want anyway. | **Refactor of existing** |
| `bin/run-chain.sh` — launcher with label preflight | No equivalent. `bin/run.sh` is harness-side already. | **Net-new (but small)** |
| `config/WORKFLOW.chain.md` — Baton config variant for chain | Reuses Baton's existing WORKFLOW.md format — only a config file, not new code. | **Config-file, not code** |
| Shims to `before_run.py` / `after_run.py` | The current hooks exist and run correctly. The chain requires `before_run` to rebase onto the feature branch, not main. This is a parameterization change, not new code. | **Modification of existing** |

### 2.2 What Baton already provides that we'd otherwise rebuild

| Capability | Baton module | Location |
|---|---|---|
| Git worktree creation/reuse/cleanup | `WorkspaceManager` | `workspace.py:L51-L117` |
| Branch naming (slugify + issue number) | `WorkspaceManager.branch_name` | `workspace.py:L66-L70` |
| `claude -p` subprocess spawn with JSON output parsing, timeout, MCP config | `Worker.run_turn` | `worker.py:L74-L167` |
| Per-issue multi-turn loop with `max_turns` budget | `_run_worker` turns loop | `orchestrator.py:L127-L169` |
| Retry/backoff scheduling | `OrchestratorState.schedule_retry` + `_backoff_delay` | `orchestrator.py:L98-L100`, `state.py:L73-L85` |
| GitHub tracker (`gh issue list`, `gh issue view`, `gh pr list`) | `GitHubTracker` | `tracker.py:L98-L179` |
| Config loading from WORKFLOW.md front matter (YAML) | `load_workflow` | `config.py:L89-L129` |
| Hook invocation (`bash -lc`, timeout, stdout/stderr) | `run_hook` | `hooks.py:L10-L44` |
| Issue skills parsing from body (`## Skills` section) | `parse_issue_skills` | `tracker.py:L78-L95` |

Everything in the right column above would need to be independently rebuilt if
we chose option (c) (standalone driver, no Baton dependency).

### 2.3 Which plan elements are coordination-seam artifacts?

The chain spec describes these as explicit coordination machinery:

**"Wait for terminal label on N (`agent-done` or `blocked`)"** (`driver.py` step 3c):
This is the primary seam artifact. Because we treat Baton as a black box, the
chain driver must poll GitHub labels to discover when Baton has finished an issue.
If we called `_run_worker` directly and awaited it, the `asyncio.Task` would
resolve with `"pr_created"` or `"no_pr"` — no polling loop needed. The label
poll loop exists **only** because Baton is a subprocess daemon and we observe
it via side effects.

**`config/WORKFLOW.chain.md`** (`spec:L302-L306`): Described as potentially
needed "if the poller needs different tracker labels during a chain." This file
only exists to tame Baton's flat poller so it does not pick up issues we have
not promoted yet. If we called `_run_worker` directly, the poller does not run,
and this config is unnecessary.

**The C1 single-writer lock / "promote exactly one, then wait"** (`spec:L396-L403`):
The serial promotion design (and the associated lock machinery from OQ-8) is
required because the chain driver and Baton's poller could otherwise race on
`agent-ready`. If the chain called `_run_worker` directly, it controls the
execution directly — the race disappears.

**The `after_run` PR-base shim / `before_run` parameterization** (`spec:L344-L349`):
These are required because Baton calls `before_run` with `cwd=wt.path` and no
other context. Parameterizing rebase target is only needed because the hook is
a shell string invoked by Baton without arguments. If we called `_run_worker`
directly and modified `before_run.py` to accept an env variable, we could set
`CHAIN_BASE_BRANCH` in `run_hook`'s `env=` kwarg — but that kwarg does not
currently exist in `run_hook` (`hooks.py:L10-L44`). So even calling directly,
you'd need to extend `run_hook`.

**Verdict:** the terminal-label polling loop, `WORKFLOW.chain.md`, and the C1
lock are all artifacts of the black-box wrapper model. The `before_run`
parameterization problem is structural — it exists regardless of whether we
call via labels or directly, because `run_hook` passes no env to hooks.

---

## Q3 — Should we integrate/vendor parts of Baton instead of wrapping it?

### 3.1 Option evaluation

**Option (a) — wrap as black box (current plan)**  
The chain driver promotes via labels; Baton polls and runs; driver waits for
terminal label. Coordination via GitHub labels + git state.

Costs:
- Label polling loop in `driver.py` (30–60 s resolution per tick)
- `config/WORKFLOW.chain.md` to constrain Baton's poller
- C1 lock machinery (OQ-8) to prevent poller racing the driver
- `before_run` rebase-target problem still requires a solution (env set by the
  driver is not passable through `run_hook` without modifying Baton or the hook)
- Two simultaneous processes (`bh-chain` driver + `baton start` daemon) must be
  kept alive together

Benefits:
- Baton is a complete subprocess — zero import coupling
- Existing hooks fire unchanged (except `before_run` base-ref fix)

**Option (b) — fork Baton and extend its loop**  
Add chain-awareness (DAG, dependency API, merge) into `orchestrator.py`.

Costs:
- We own the fork indefinitely. Baton is dormant; "maintenance" = all of us.
- The most complex new logic (DAG traversal, CI gating, merge) is threaded into
  an existing async loop that was not designed for cross-issue coordination.
- Testing becomes harder: the run loop is not designed to be unit-tested (no
  dependency injection for the subprocess calls beyond what `Worker` already
  isolates).

Benefits:
- No coordination seam; the loop has direct access to per-issue state

**Option (c) — vendor/import Baton as a library**  
Import `symphony.*` directly from a vendored copy (or an editable install of
the upstream), call `Orchestrator._run_worker(issue)` from the chain driver,
bypass the poller.

**Size/cleanliness of the vendored path:**

The modules involved are small:
- `config.py` — 130 lines, zero runtime deps except PyYAML + Jinja2
- `orchestrator.py` — 316 lines
- `worker.py` — 168 lines
- `workspace.py` — 118 lines
- `hooks.py` — 45 lines
- `tracker.py` — 179 lines
- `state.py` — 119 lines
- `prompt.py` — 41 lines

Total: ~1120 lines. The runtime deps are `click`, `pyyaml`, `jinja2`,
`watchfiles` — manageable, though `watchfiles` is unused if we do not use the
polling loop.

**License:** MIT (`LICENSE:L1`) — permissive. Vendoring is explicitly allowed with
attribution. There is no copyleft constraint.

**The #23 fix under option (c):**

Issue #23 is the "terminal-block / `exclude_labels` not re-checked between turns"
bug. In Baton's source, `exclude_labels` is only applied in `fetch_candidates`
(`tracker.py:L128-L134`), which is called in `_tick` → `_handle_retries`, NOT
inside `_run_worker`'s turn loop. This means once an issue is dispatched, if the
agent adds `blocked` mid-run, Baton continues the turn loop regardless.

The fix under option (c): inside `_run_worker`'s turn loop
(`orchestrator.py:L127-L169`), add an `exclude_labels` re-check after each
`self.tracker.fetch_issue_state()` call (which already happens at `L162-L169`).
`fetch_issue_state` currently only checks `state == "open"`; adding a labels
fetch (`gh issue view --json labels`) and checking against `exclude_labels`
would close the #23 gap. This is a ~10-line change if we own the source.

Under option (a), the fix requires either (i) modifying the hook to close the
issue (preventing Baton re-dispatch, a side-channel hack) or (ii) waiting on
upstream. Under option (b)/(c) it is a direct 10-line edit.

### 3.2 The decisive factor

The chain spec's option (a) rationale (`spec:L87-L139`) is correct in its
premises but underweights one structural cost: **the `run_hook` function passes
no env to hooks** (`hooks.py:L22-L27`). The `before_run` rebase-target
parameterization problem (`spec:L344-L349`, OQ-6) cannot be solved cleanly under
option (a) without either:
- Modifying the target project's `WORKFLOW.md` hook line to read an env file
  (brittle, project-specific)
- Or modifying Baton's `run_hook` to accept env (requires the fork anyway)

Under option (c) (vendored import), this is solved by adding an `env=` parameter
to `run_hook` and passing `{"CHAIN_BASE_BRANCH": feature_branch}` from the chain
driver before calling `_run_worker`. That is a ~5-line change to a 45-line
module we would own.

### 3.3 Recommendation

**Option (c) — vendor Baton as a local library package.**

**Single most decisive reason:** the coordination seam (label polling, C1 lock,
`WORKFLOW.chain.md`) is not a safety feature — it is pure overhead created by
treating a 1120-line MIT-licensed codebase as an opaque subprocess. Option (c)
eliminates the polling loop, the C1 race, and the `before_run` env-passing
problem by giving the chain driver a direct `await orch._run_worker(issue)` call.
The upstream is frozen (3 commits, no external PRs ever merged, nothing since
2026-03-27), so the "fork maintenance cost" argument evaporates: we are already
the de facto maintainers the moment we depend on it.

**What "vendor" means concretely:**
- Copy `symphony/` into `src/baton_harness/vendor/symphony/` (or a sibling
  package), or add it as an editable local dependency in `pyproject.toml`.
- Make two targeted changes to the vendored copy:
  1. Add `env: dict | None = None` param to `run_hook` (`hooks.py`) and pass it
     through to `asyncio.create_subprocess_exec` — solves OQ-6 permanently.
  2. Re-check `exclude_labels` inside the turn loop after `fetch_issue_state` —
     fixes #23.
- The chain driver imports `from baton_harness.vendor.symphony.orchestrator import Orchestrator`
  (or similar) and calls `asyncio.run(orch._run_worker(issue))`.
- The polling daemon (`baton start`) is no longer needed during chain runs — the
  chain driver owns the issue lifecycle directly.

**What we explicitly do NOT get by going to option (c):**
- The `baton status` command (less useful for chain runs anyway — the chain has
  its own progress model)
- The multi-concurrent dispatch (the chain is serial by design in v1)
- The retry/backoff scheduling in `OrchestratorState` — but the chain driver can
  reuse `OrchestratorState.schedule_retry` / `_backoff_delay` by importing them,
  or implement a simpler chain-level retry policy

**When to reconsider:**
If the user's operational posture strongly prefers "one long-running daemon
process" over "a driver subprocess that exits when the chain is done," option (a)
has ergonomic value. The label-polling loop and lock overhead are real costs but
not fatal. The `before_run` env problem would need to be addressed by reading an
env file the driver writes to the worktree — ugly but workable.

---

## Appendix: key file:line index

| Fact | Source |
|---|---|
| CLI entrypoint | `pyproject.toml:L16`, `symphony/cli.py:L15-L70` |
| `_run_worker` signature | `symphony/orchestrator.py:L102` |
| Turn loop | `symphony/orchestrator.py:L127-L169` |
| Hook invocation (after_create once-per-create) | `orchestrator.py:L107-L113` |
| Hook invocation (before_run once-per-run) | `orchestrator.py:L115-L119` |
| Hook invocation (after_run once-per-run, two sites) | `orchestrator.py:L155-L158`, `L172-L175` |
| No env passed to hooks | `hooks.py:L22-L27` (no `env=` kwarg) |
| Worktree path: `<root>/.symphony/worktrees/<N>` | `workspace.py:L57-L64` |
| Branch naming: `baton/<slug>-<N>` | `workspace.py:L66-L70` |
| Claude subprocess invocation | `worker.py:L99-L106` |
| `exclude_labels` applied only in fetch_candidates (not mid-turn) | `tracker.py:L128-L134` |
| MIT license | `LICENSE:L1-L21` |
| Baton runtime deps | `pyproject.toml:L8-L13` |
| `_on_worker_done` callback (not called if `_run_worker` called directly) | `orchestrator.py:L64-L96` |

---

*Promoted from session scratch (`.tmp/baton-deepdive-findings.md`) into the repo on 2026-06-07 so the dependency-chain-orchestration spec's citations resolve durably. See PR #30 / issue #27.*
