# Baton System-Prompt Injection — Feasibility Research

**Date:** 2026-06-05
**Researcher:** Claude Code (researcher sub-agent)
**Question:** Can we inject autonomous-mode policy text as a SYSTEM-LEVEL prompt into every `claude` invocation that the Baton orchestrator fires, using only WORKFLOW.md config — or does it require an upstream code change to `mraza007/baton`?

---

## Feasibility Verdict

**Upstream change required.** The Claude Code CLI provides a documented `--append-system-prompt` flag that appends text at system-prompt authority — above CLAUDE.md's user/project tier. However, the Baton `Worker._build_claude_args()` function in `symphony/worker.py` hard-codes a fixed argument list (`[agent_command, "-p", prompt, "--output-format", "json"]`) and only conditionally appends `--permission-mode` / `--dangerously-skip-permissions`. It does not pass `agent_command` through as an argument vector, nor does it read any WORKFLOW.md key that would translate to extra CLI flags. The `agent.command` config key is treated purely as the executable binary name (a single string, not a list). Injecting `--append-system-prompt` therefore requires a two-line upstream change to `_build_claude_args()` and a new `system_prompt_append` field on `WorkflowConfig`.

---

## Question 1 — Claude Code `--append-system-prompt` Flag

### Finding 1a: Flag names confirmed

Both flags exist and are documented on the current CLI reference page.

| Flag | Behavior |
|------|----------|
| `--system-prompt` | Replaces the entire default system prompt with custom text |
| `--system-prompt-file` | Replaces with content from a file |
| `--append-system-prompt` | Appends custom text to the end of the default system prompt |
| `--append-system-prompt-file` | Appends file contents to the default system prompt |

Quoted verbatim from the docs:
> "`--system-prompt` and `--system-prompt-file` are mutually exclusive. The append flags can be combined with either replacement flag."

Source: https://code.claude.com/docs/en/cli-reference (fetched 2026-06-05), "System prompt flags" section.

### Finding 1b: System-prompt authority vs. CLAUDE.md tier

The Agent SDK documentation makes the instruction-tier distinction explicit:

> "CLAUDE.md doesn't change the system prompt itself: the SDK injects its content into the conversation as project context."

And:

> "CLAUDE.md files give Claude persistent project context and instructions. The SDK injects their content into the conversation, not into the system prompt, so they work with any system prompt configuration."

Source: https://code.claude.com/docs/en/agent-sdk/modifying-system-prompts (fetched 2026-06-05), "CLAUDE.md files for project-level instructions" section.

**Implication for the hierarchy question:** Text injected via `--append-system-prompt` lands in the system prompt. Text from CLAUDE.md lands in the conversation (user-tier context). In LLM API terms, system-prompt instructions carry more weight than user-turn instructions. The docs explicitly note this asymmetry when discussing `excludeDynamicSections`:

> "Instructions in the user message carry marginally less weight than the same text in the system prompt, so Claude may rely on them less strongly."

So `--append-system-prompt` sits at a higher-authority tier than CLAUDE.md project instructions.

### Finding 1c: Composition with `-p` (print/headless mode)

The CLI reference confirms both flags work in headless mode:

> "All four work in both interactive and non-interactive modes."

Source: https://code.claude.com/docs/en/cli-reference (fetched 2026-06-05), "System prompt flags" table header note.

The example in the flag table uses inline text: `claude --append-system-prompt "Always use TypeScript"`. The docs also provide `--append-system-prompt-file` for longer policy text (a file path instead of an inline string). Both compose freely with `-p "<prompt>"`.

### Finding 1d: Append vs. replace — choice for this use case

The docs recommend `--append-system-prompt` (not `--system-prompt`) when:

> "Use an append flag when Claude should remain a coding assistant that also follows your extra rules: per-invocation instructions, output formatting, or domain context for a `-p` script. Appending preserves the default tool guidance, safety instructions, and coding conventions, so you only supply what differs."

For autonomous-mode policy injection (scope boundaries, block rules, closing steps), `--append-system-prompt` is the correct flag — it preserves the coding-agent identity while adding enforcement text at system-prompt authority.

---

## Question 2 — How Baton Constructs the `claude` Subprocess

All source files read from `mraza007/baton` at commit SHA `08da776b0547d8563a1942e92883fd523a157b29` (orchestrator.py), `4317c5ec4b8518431a1e12c9e52f2bd76f2d2a20` (worker.py), `61cb4ae4dd57b32062f5060199bdba3958184fef` (config.py).

### Finding 2a: Subprocess construction — the exact call site

`symphony/worker.py`, `Worker._build_claude_args()` (lines ~55–72), builds the argument list:

```python
args = [
    self.config.agent_command,
    "-p", prompt,
    "--output-format", "json",
]

if self.config.permission_mode:
    mode = self.config.permission_mode
    if mode == "acceptEdits":
        args.extend(["--permission-mode", "acceptEdits"])
    elif mode == "bypassPermissions":
        args.extend(["--dangerously-skip-permissions"])
```

The subprocess is then launched via `asyncio.create_subprocess_exec(*args, cwd=cwd, ...)`.

Permalink: https://github.com/mraza007/baton/blob/4317c5ec4b8518431a1e12c9e52f2bd76f2d2a20/symphony/worker.py

### Finding 2b: How the prompt body is passed

The rendered WORKFLOW.md prompt body is passed as the inline argument to `-p`:

```python
args = [
    self.config.agent_command,
    "-p", prompt,      # ← prompt string inlined as argv element
    ...
]
```

It is NOT passed via stdin or a temp file. The `render_prompt()` call in `orchestrator.py` returns a string, which is handed directly to `_build_claude_args(prompt=...)`, which places it as the literal second argument after `-p`. For multi-turn continuation turns, the prompt is a hardcoded continuation string built inline in `_run_worker()`.

### Finding 2c: How WORKFLOW.md config keys map to the invocation

`symphony/config.py` (`load_workflow()`, SHA `61cb4ae4dd57b32062f5060199bdba3958184fef`) reads the YAML front matter and populates `WorkflowConfig`. The keys that affect the invocation are:

| WORKFLOW.md key | `WorkflowConfig` field | Effect on `claude` args |
|---|---|---|
| `agent.command` | `agent_command: str = "claude"` | Used as `args[0]` — the binary name only |
| `agent.permission_mode` | `permission_mode: str` | Appends `--permission-mode acceptEdits` or `--dangerously-skip-permissions` |
| `agent.max_turns` | `max_turns: int` | Controls the Python-side loop counter; NOT passed as `--max-turns` to `claude` |
| `agent.mcp_servers` | `mcp_servers: list` | Serialized to a temp JSON file; appended as `--mcp-config <path>` |

**`agent.command` is a single string, not a list.** It feeds directly into `args[0]`:

```python
agent_command: str = "claude"   # config.py WorkflowConfig default
# ...
args = [
    self.config.agent_command,   # always args[0], the binary name
    "-p", prompt,
    ...
]
```

There is no shlex-splitting of `agent_command`, no argument vector interpretation, and no extra-args field. Setting `agent.command: "claude --append-system-prompt 'text'"` would cause `create_subprocess_exec` to look for a binary literally named `claude --append-system-prompt 'text'` — it would fail with `FileNotFoundError`.

### Finding 2d: THE KEY QUESTION — config-only path blocked

There is no config path in the current Baton source that would place `--append-system-prompt` on the `claude` argv. Specifically:

1. `agent.command` is a single string, not a list — cannot embed flags.
2. `_build_claude_args()` has no branch for extra CLI flags beyond `permission_mode`.
3. `WorkflowConfig` has no `system_prompt_append` or `extra_args` field.
4. The `skills` field on `WorkflowConfig` is loaded but **never used** in `_build_claude_args()` (the `issue_skills` parameter is accepted by `_build_claude_args()` but the function body doesn't act on it — the loop in `_run_worker` passes `issue_skills` but they have no argv effect in the current code).

### Finding 2e: Minimal upstream patch required

**File:** `symphony/worker.py`
**Function:** `Worker._build_claude_args()`
**File:** `symphony/config.py`
**Class:** `WorkflowConfig`

**Shape of the minimal change (two locations):**

In `symphony/config.py`, add one field to `WorkflowConfig`:

```python
# Add to WorkflowConfig dataclass
system_prompt_append: str | None = None
```

And in `load_workflow()`, read it from the YAML:

```python
system_prompt_append=_get(fm, "agent", "system_prompt_append"),
```

In `symphony/worker.py`, add one conditional block to `_build_claude_args()` after the existing permission-mode block:

```python
if self.config.system_prompt_append:
    args.extend(["--append-system-prompt", self.config.system_prompt_append])
```

After this change, the WORKFLOW.md author sets:

```yaml
agent:
  system_prompt_append: |
    AUTONOMOUS POLICY: ...scope boundaries... block rules... closing steps...
```

For longer policy text, a parallel `system_prompt_append_file` → `--append-system-prompt-file` variant follows the same pattern.

---

## No Prior Art Found

None. This is a targeted feasibility question against two specific source artifacts. The gap is not "no prior art" — it is a confirmed absence of the feature in the current Baton source.

---

## Sources Cited

| Artifact | URL / Path | Version |
|---|---|---|
| Claude Code CLI reference | https://code.claude.com/docs/en/cli-reference | fetched 2026-06-05 |
| Claude Code Agent SDK — Modifying system prompts | https://code.claude.com/docs/en/agent-sdk/modifying-system-prompts | fetched 2026-06-05 |
| `mraza007/baton` `symphony/worker.py` | https://github.com/mraza007/baton/blob/4317c5ec4b8518431a1e12c9e52f2bd76f2d2a20/symphony/worker.py | SHA 4317c5e |
| `mraza007/baton` `symphony/config.py` | https://github.com/mraza007/baton/blob/61cb4ae4dd57b32062f5060199bdba3958184fef/symphony/config.py | SHA 61cb4ae |
| `mraza007/baton` `symphony/orchestrator.py` | https://github.com/mraza007/baton/blob/08da776b0547d8563a1942e92883fd523a157b29/symphony/orchestrator.py | SHA 08da776 |
| `mraza007/baton` `symphony/prompt.py` | https://github.com/mraza007/baton/blob/6e965e0228e0e6fda0cfed134b69f13560107e5c/symphony/prompt.py | SHA 6e965e0 |

---

## Recommended Handoff

- **`project-planner`** — the minimal patch shape is fully specified (two files, one new config field, one conditional `args.extend` call). Planner should scope a D2 upstream-contribution work item: fork or PR against `mraza007/baton`, add `system_prompt_append` to `WorkflowConfig` + `_build_claude_args()`, then update the local `baton-harness` WORKFLOW.md to set the policy text. No design work remains — only implementation and the decision of whether to upstream-contribute or maintain a local fork/patch.
- **`user`** — one decision before implementation: whether to contribute upstream (PR to `mraza007/baton`) or carry a local monkey-patch/wrapper. The upstream PR is two files, ~6 lines; the local option could be a thin shell wrapper around `claude` that injects `--append-system-prompt`.

## Open Questions

- The `--append-system-prompt-file` variant is also available and likely preferable for multi-paragraph policy text. The patch shape above shows the inline string variant; an `_file` variant would need a temp-file write step analogous to how `_build_mcp_config()` already writes a temp JSON file. No doc ambiguity — both flag variants are confirmed supported.
- `mraza007/baton` appears to be a personal/small repo. Upstream PR acceptance is uncertain. The local-patch path (wrapper script or forked `worker.py`) is the lower-risk execution path if turnaround matters.
