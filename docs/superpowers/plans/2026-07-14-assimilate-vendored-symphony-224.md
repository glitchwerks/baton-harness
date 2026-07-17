---
title: Assimilate vendored symphony tree — lint/type it as owned code (issue #224)
touches:
  - pyproject.toml
  - src/baton_harness/vendor/symphony/cli.py
  - src/baton_harness/vendor/symphony/log.py
  - src/baton_harness/vendor/symphony/__init__.py
  - src/baton_harness/vendor/symphony/config.py
  - src/baton_harness/vendor/symphony/prompt.py
  - src/baton_harness/vendor/symphony/state.py
  - src/baton_harness/vendor/symphony/tracker.py
  - src/baton_harness/vendor/symphony/hooks.py
  - src/baton_harness/vendor/symphony/workspace.py
  - src/baton_harness/vendor/symphony/worker.py
  - src/baton_harness/vendor/symphony/orchestrator.py
  - src/baton_harness/vendor/symphony/VENDORING.md
  - patches/
  - README.md
skills_relevant:
  - python
  - claude-github-tools:github-actions
---

# Assimilate vendored symphony tree — lint/type it as owned code (#224)

## Provenance note

This plan's code claims are grounded in `file:line` citations verified by reading
`main` at commit `30a3afe` (`git log` HEAD, 2026-07-14). The **issue #224 scope
statement and acceptance criteria** are taken from the router dispatch brief; no
`gh`/GitHub MCP tool was available in this planning session, so the issue body was
**not independently fetched** — verify against `gh issue view 224` before executing
if the brief and issue have diverged.

The **finding counts** (134 ruff / 19 mypy) come from the router's Explore map, which
measured them with the exclusions forced off *globally* (before the dead-code deletion
in Phase 1). They are **historical, pre-deletion estimates for sizing, not a budget**
— re-baseline after each phase (see § 6). This planning session had no Python/ruff/mypy
execution capability (sub-agent, no Bash tool), so per-file counts were not
independently re-measured; the implementer must run the per-file gate at each phase
start.

**Verified Phase-1 baseline (PR #259, post-deletion):** **116 ruff findings / 9 mypy
errors** across the **8 files with findings** (`cli.py`/`log.py` deleted; the two
`__init__.py` files carry no findings of their own beyond the 8 module files' counts).
This is the number later-phase batch sizing (§ 5) is actually built against — the
134/19 figures above are retained only as the pre-deletion historical estimate.

---

## 1. Problem & goal

The vendored `symphony` tree at `src/baton_harness/vendor/symphony/` is currently
excluded from both linters: `pyproject.toml:66-68` excludes
`src/baton_harness/vendor` from ruff, and `pyproject.toml:108-110` sets
`ignore_errors = true` for the `baton_harness.vendor.*` mypy module glob. The
project has since declared itself the de facto maintainer of this source
(`CLAUDE.md § Upstream dependency`; `VENDORING.md:11-18` rationale) — it is vendored,
not tracked upstream, and Baton bugs are fixed here directly (VP-1..VP-9, per
`VENDORING.md:28-159`).

The goal of #224 is to **finish assimilating the tree as owned code**: remove the
lint/type exclusions, fix (or justifiably suppress) the resulting findings, retire the
re-vendor machinery (`VENDORING.md` checklist, `patches/`), and let CI run the full
gate over the tree. CI already runs `ruff check .`, `ruff format --check .`,
`mypy src`, and `pytest` over everything (`.github/workflows/ci.yml:24-28,39-40,51-52`)
— so "CI runs the full gate over the tree" is satisfied purely by removing the config
exclusions. **No `ci.yml` change is required.**

This is milestone **1.0 release gate** work — not urgent-blocking — so a moderate,
multi-PR phased plan (keeping each PR reviewable and CI green throughout) is the
appropriate shape, not a single mega-PR. **Six PRs total** — Phases 1, 2, 3, 3b, 4, 5
(§ 5) — with **Phase 3b a standalone PR**, not a sub-phase folded into Phase 3's count;
see § 7's issue-decomposition bullet for the one-tracking-issue-per-PR mapping.

---

## 2. Key scope-shrinking finding: `cli.py` and `log.py` are dead code — delete them

The dispatch brief flagged an open question: is `cli.py` dead relative to the harness?
**It is, and so is `log.py`.** Both should be deleted rather than remediated. Evidence
(verified this session):

- **Nothing imports `vendor.symphony.cli`.** A repo-wide grep for
  `vendor.symphony.cli` returns only `chain/cli.py` and the `tests/chain/test_cli*.py`
  suites — all of which reference `baton_harness.chain.cli` (the harness's own daemon
  CLI, the `bh-daemon` entry point at `pyproject.toml:49`), **never**
  `baton_harness.vendor.symphony.cli`. The `[project.scripts]` block
  (`pyproject.toml:45-50`) is untouched by this deletion — `bh-daemon` maps to
  `chain.cli:main`, not the vendored `cli.py`.
- **`vendor/symphony/cli.py` implements the retired `baton start/status/stop` CLI**
  (`cli.py:15-121`) for the external-process launcher. That launcher (`bin/run.sh` /
  `baton start -w`) has been **retired** (`CLAUDE.md § Upstream dependency`;
  `docs/harness-design.md:106` "The original `bin/run.sh` … was deleted when
  `bin/run-daemon.sh` landed in P3"). The only `baton start` references remaining in
  the repo are prose describing this retired historical model
  (`docs/harness-design.md:47,50,57,169,529`; `README.md:37`) — no live invocation.
- **`log.py` is reachable only through `cli.py`.** `setup_logging` and
  `SymphonyFormatter` (`log.py:40-46,9-37`) are imported only at `cli.py:28`. The
  orchestrator gets its logger via `logging.getLogger("symphony")` directly
  (`orchestrator.py:16`), not from `log.py`. Deleting `cli.py` orphans `log.py`.
- **Deleting `log.py` has zero live-path effect.** The daemon configures logging via
  `logging.basicConfig(...)` at `chain/cli.py:226`; orchestrator's `"symphony"` logger
  propagates to the root handler that `basicConfig` installs. `setup_logging`'s
  colorized formatter was only ever applied in the (now-deleted) `cli.py` path.
- **No test imports `cli` or `log`.** A grep of `tests/**/*.py` for `.cli`/`.log`
  returns only `baton_harness.chain.cli` targets; the `tests/vendor/` regression suite
  imports `config`, `orchestrator`, `tracker`, `workspace`, `hooks`, `state` — never
  `cli` or `log`. So `tests/vendor/` stays green after deletion.

**Consequences that shrink scope:**
- The undeclared-`click` dependency concern disappears: `click` is imported only at
  `cli.py:10`, so deleting `cli.py` removes it — **do not add `click` to
  dependencies**.
- The ~7 mypy errors the Explore map attributed to `cli.py` (untyped decorators/
  functions) and all of `cli.py`/`log.py`'s ruff findings vanish, so the real 134/19
  workload is smaller. Re-baseline after Phase 1.

> ✅ **Decision D1 — RATIFIED by user, 2026-07-14: delete both `cli.py` and `log.py`.**
> No live imports found; `python -m ...cli` never worked (no `__main__` guard); this
> also removes the undeclared `click` dependency. Phase 1 is unblocked.

---

## 3. Finding inventory (from Explore map — treat as estimate)

**Historical, pre-deletion estimate** — the 134/19 figures below predate the
Phase 1 `cli.py`/`log.py` deletion. See § 1 provenance note for the **verified
Phase-1 baseline (116 ruff / 9 mypy, 8 files with findings, PR #259)**, which is the
number later-phase batch sizing (§ 5) is built against.

**Ruff (134 findings, exclusions off, pre-deletion):** dominant classes —
`E501` line-too-long (56), `D101`/`D102` undocumented public class/method (17 each),
`ANN204` missing dunder return type (10), `D107` undocumented `__init__` (10),
`B904` raise-without-from (5), plus smaller `ANN`/`I001`/`E741` counts. Rule set:
`E,W,F,I,B,UP,N,ANN,D`; line-length 79; `D203`/`D213` ignored; pydocstyle
convention google (`pyproject.toml:70-88`).

**Mypy (19 errors in 5 files, strict, override off, pre-deletion):**
- 2 are dependency/stub gaps: `yaml` untyped (→ add `types-PyYAML`); `click`
  unresolved in `cli.py:10` (→ **resolved by deleting `cli.py`**, not by adding click).
- 17 are genuine annotation/type debt: missing generic args (`dict`, `Task`) ×4,
  untyped functions/decorators in `cli.py` ×7 (→ mostly deleted with `cli.py`),
  `Any`-return leaks ×2, assignment/arg-type mismatches ×2, etc.

**Docstrings are the dominant hand-written cost:** `D101`+`D102`+`D107` ≈ **44 of the
134** findings.

> ✅ **Decision D2 — RATIFIED by user, 2026-07-14: write real google-style docstrings**
> for the ~44 `D101`/`D102`/`D107` findings, not per-line suppressions. This is
> ownership-consistent ("lint it as owned code") and this code is actively patched
> (VP-1..VP-9), so the docstrings pay for themselves in maintainability — it makes the
> code genuinely owned, not just quieted. No suppression fallback needed.

---

## 4. Sequencing strategy — narrow the exclusions per-file, leaves→root

**Why per-file, not per-error-class:** both exclusions are file/module-scoped
(`pyproject.toml:66-68` is a path list; `108-110` is a module glob). To keep CI green
throughout, we clean one file (or small batch) at a time and **remove that file from
both exclusion lists in the same PR** — so the real gate validates each file the
moment it is un-excluded, and no single "giant red" PR ever exists. Per-error-class
phasing can't partially lift a directory exclude and would leave the tree red between
PRs.

**Why leaves→root (dependency order, not size):** when a module is un-excluded, mypy
`strict` + `warn_return_any` (`pyproject.toml:97,100`) type-checks it **including the
types it consumes from still-suppressed imports**. `Any`-leaks from an unannotated
dependency surface as errors **in the module you are trying to clean**. Cleaning
leaves first minimizes these spurious cross-module errors.

The intra-package import DAG (verified — grep of `^from \.` across the tree):
- **Layer 0 (leaves, no intra-package deps):** `config.py`, `tracker.py`, `state.py`,
  `hooks.py`, `workspace.py`, plus both `__init__.py` files (docstring-only).
- **Layer 1:** `prompt.py` (→ `.tracker`, `prompt.py:8`), `worker.py`
  (→ `.config`, `worker.py:11`).
- **Layer 2 (root):** `orchestrator.py` imports config, hooks, prompt, state, tracker,
  worker, workspace (`orchestrator.py:8-14`).

So: **Layer 0 → Layer 1 → orchestrator.** In particular, clean `tracker` before/with
`prompt`, and `config` before/with `worker`.

**Per-PR commit hygiene (reviewability):** within each cleanup PR, isolate the
mechanical churn: **Commit A = `ruff format` only** (quote/whitespace normalization —
a large, skimmable diff). **Commit B = hand-written fixes** (E501 residuals that the
formatter won't break — long string literals, comments, URLs — plus ANN annotations,
docstrings, `B904` raise-from, `I001` import ordering). **Commit C = remove the
file(s) from the ruff `exclude` + mypy override lists.** `ruff format` will **not**
clear all 56 `E501`s — long literals/comments survive the formatter and need manual
breaking or a justified `noqa`.

The exclusion-narrowing mechanics:
- **Ruff:** change `exclude = ["src/baton_harness/vendor"]` to an explicit list of the
  still-dirty files, and delete entries as they are cleaned. (Directory-level
  granularity can't be lifted incrementally; file paths can.)
- **Mypy:** change the override `module = "baton_harness.vendor.*"` to an explicit list
  (`module` accepts a list), e.g. `module = ["baton_harness.vendor.symphony.orchestrator",
  "…worker", …]`, deleting entries as they are cleaned. Remove the whole override block
  once the list is empty.

**VP-marker format convention (from project-reviewer; established in Phase 1, before
Phase 2 cleanup begins):** `ruff format` reflows multi-line imports but cannot break
comments, so a long trailing `# VENDOR-PATCH` comment on an import line — as in
`orchestrator.py:8-14` — can exceed the 79-char line length once the formatter
re-wraps the import. Convention: move any such trailing marker to a standalone block
comment on the line directly above the import, instead of trailing it. This is
format-stable, `E501`-safe, and preserves the full marker text verbatim. State this
convention explicitly in the Phase 1 PR description so it is settled before any
cleanup PR touches an annotated import (Phase 2 onward).

---

## 5. Phased PR plan

**Six PRs total: Phases 1, 2, 3, 3b, 4, 5.** Phase 3b (§ below) is a standalone PR
split out of what would otherwise be Phase 3's Layer-1 modules — not a sub-step folded
into Phase 3's own PR — because `worker.py` needs to be isolated for security-focused
review (project-reviewer finding; see Phase 3b's rationale). Any reference elsewhere in
this plan (or in this PR's description) to "five phases" / "four phases remaining after
Phase 1" is stale against this six-PR decomposition.

Batch sizes below are **provisional** — the implementer must run the per-file gate at
each phase start (see § 6) and re-balance so each PR stays reviewable, **except**
`worker.py` (Phase 3b) and `orchestrator.py` (Phase 4), which are fixed as their own
PRs unconditionally (per project-reviewer — see those phases for rationale), not
subject to re-balancing.

### Phase 1 (PR #1) — Dead-code removal + exclusion scaffolding + stub dep

- Delete `src/baton_harness/vendor/symphony/cli.py` and
  `src/baton_harness/vendor/symphony/log.py` (per D1). Keep `LICENSE` and every other
  module.
- Add `types-PyYAML` to `[project.optional-dependencies] dev`
  (`pyproject.toml:25-29`) — the stub for `yaml`, needed once `config.py` is
  un-excluded. **Do not add `click`** (deleted with `cli.py`).
- Convert `[tool.ruff] exclude` (`pyproject.toml:66-68`) from the blanket directory to
  an explicit list of the **10 surviving files** — derived from
  `Glob src/baton_harness/vendor/**/*.py`, **not** a hand-list. The blanket directory
  exclude currently covers the whole `vendor/` tree, which includes the parent-package
  `src/baton_harness/vendor/__init__.py` **and** `vendor/symphony/__init__.py` — both
  easy to miss. Full survivor set (after the cli/log deletion): `vendor/__init__.py`,
  `vendor/symphony/__init__.py`, `config.py`, `hooks.py`, `orchestrator.py`,
  `prompt.py`, `state.py`, `tracker.py`, `worker.py`, `workspace.py`. **Any file
  omitted from this list is silently un-excluded** and immediately hits `ruff check .`
  / `mypy src` — e.g. a bare `vendor/__init__.py` with no package docstring trips
  `D104` and turns this "zero-findings scaffold" PR red. Enumerate all 10 so nothing
  leaks out. No findings fixed yet — everything still excluded. CI stays green.
- Convert the mypy override (`pyproject.toml:108-110`) from the `baton_harness.vendor.*`
  glob to an explicit module list covering the **same 10 files** (note the glob also
  matches the bare `baton_harness.vendor` package — include it).
- Update `VENDORING.md` minimally: remove `cli`/`log` from the re-vendor module
  enumeration (`VENDORING.md:184-190,224`) and the `# VENDOR-PATCH` grep expectations
  for `cli.py` (`VENDORING.md:211-225`), and add a line noting both were **deleted as
  dead code by #224**. (Full provenance rewrite happens in the final phase.)
- **State the VP-marker reflow convention in this PR's description** (see § 4): long
  trailing `# VENDOR-PATCH` comments on import lines move to a standalone block
  comment above the import once `ruff format` is applied to that file. This is a
  documentation-only note in Phase 1 (no annotated imports are touched yet) — it exists
  so the convention is settled before Phase 2 needs it.
- **Re-baseline:** run the full gate with the new per-file exclusion lists and record
  the true remaining per-file finding counts (see § 6). This is the number the batch
  sizing in Phases 2–4 is built against. **Verified (PR #259): 116 ruff / 9 mypy across
  the 8 files with findings** — see § 1 provenance note.
- Tests: `tests/vendor/` unaffected (no `cli`/`log` importers). Green expected.

### Phase 2 (PR #2) — Layer-0 leaves batch A: package `__init__`s + `config` + `tracker`

Clean both `__init__.py` files (docstring-only — each needs a package/module docstring
to clear `D104`/`D100`) plus the Layer-0 leaves `config.py` and `tracker.py`. Add the
`types-PyYAML`-dependent fixes for `config.py`'s `yaml` usage here. Commit A/B/C
per § 4. Remove these files from both exclusion lists. (`tracker` is cleaned here so
it is done before `prompt` consumes it in Phase 3b.)

> ⚠️ **`config.py` is not a cheap leaf (per project-reviewer).** `yaml.safe_load()`
> returns `Any` even with `types-PyYAML` installed; every function that reads a parsed
> YAML value and returns or assigns it will trigger `warn_return_any` under strict
> mypy (`pyproject.toml:97,100`). This may make `config.py` the hardest module in this
> phase, not the easiest as the original Explore-map sizing assumed. Run `config.py`
> through the per-file gate (§ 6) **before** batching it with the `__init__` files —
> if its mypy finding count exceeds 5, split it into its own commit (or its own PR)
> within Phase 2 rather than batching it.

### Phase 3 (PR #3) — Layer-0 leaves batch B: `state`, `hooks`, `workspace`

Clean the remaining Layer-0 leaves only: `state.py`, `hooks.py`, `workspace.py`.
`hooks.py` carries VP-1/VP-7, `state.py` carries VP-6, and `workspace.py` carries
VP-9 — **retain all `# VENDOR-PATCH` comments** (acceptance
criterion); annotating around them is fine. Commit A/B/C per § 4; remove these three
files from both exclusion lists.

> Per project-reviewer: the Layer-1 modules `prompt.py` and `worker.py` are **not**
> batched here — see Phase 3b. `worker.py` in particular carries an active
> merge-prevention control (VP-4) and needs isolated reviewability, not a "if it's too
> big" contingency.

### Phase 3b (PR #3b) — Layer-1: `prompt.py` + `worker.py`

Clean the Layer-1 modules: `prompt.py` (→ depends on the now-clean `tracker`) and
`worker.py` (→ depends on the now-clean `config`). This is its own PR, **unconditionally**
— not bundled with Phase 3 or gated on batch size. Rationale (per project-reviewer):
`worker.py` contains VP-4's `_MERGE_DENY_TOOLS` (`worker.py:15,82`), an active
merge-prevention control per `VENDORING.md:74-90` — not historical code — so it needs
to be isolated for security-focused reviewability rather than being folded into a
larger leaves batch. Bundle `prompt.py` alongside it since both are Layer 1 and both
are now un-blocked by Phase 2/3's leaf cleanup. Apply the VP-marker reflow convention
(§ 4) to `worker.py:11`'s trailing mechanical marker and `prompt.py:8`'s trailing
mechanical marker if `ruff format` pushes either past the line limit. Commit A/B/C;
remove both
files from both exclusion lists.

### Phase 4 (PR #4) — `orchestrator.py` solo

Largest module, top of the DAG, carries VP-2/VP-3/VP-5/VP-6 markers. By now all its
dependencies (Phases 2, 3, 3b) are annotated, minimizing `Any`-leaks. Solo PR —
highest review risk (the `_run_worker` turn loop is the harness's core control flow).
Apply the VP-marker reflow convention (§ 4) to `orchestrator.py:8-14`'s seven trailing
mechanical markers — this is the file the convention was written for. Commit A/B/C;
remove the last entries from both exclusion lists.

### Phase 5 (PR #5) — Retirement + docs

- Remove the now-empty ruff `exclude` block and the entire mypy override block from
  `pyproject.toml`. (Ruff's built-in default excludes still cover `.venv`,
  `.worktrees`, etc. — confirm none of the harness's own excludes were relying on the
  removed `exclude` list.)
- **`VENDORING.md`:** retire the "Re-vendor checklist" section (`VENDORING.md:180-235`);
  rewrite the file as a **provenance record** — upstream repo/SHA/date/license
  (`VENDORING.md:1-9`), the applied-patch list retained as **historical annotations**
  (VP-1..VP-9, `VENDORING.md:28-159`), an explicit note that `cli.py`/`log.py` were
  **deleted as dead code** (so the record matches the tree), and a note that #224
  assimilated the tree as owned code, **superseding the mypy-strict deferral**
  documented at `VENDORING.md:171-178`. State the new policy: changes to the tree no
  longer require `patches/` diff files.
- **`patches/` (Decision D3 — RATIFIED by user, 2026-07-14): freeze as historical
  record.** Keep the 9 existing diff files in place documenting what was patched and
  why (48 KB, cheap; useful evidence of what each VP-N changed). Add a
  `patches/README.md` marking the directory frozen/historical, and state that new
  `patches/*.diff` files are **no longer required** for future vendor-tree changes —
  that requirement is what's being retired, not the existing files. Keep
  `VENDORING.md`'s `patches/VP-N-*.diff` citations (they stay valid since the files
  aren't moving). Annotate `patches/mypy-strict-remediation.diff` as **superseded by
  #224**.
- **`README.md`:** update the vendoring section — `README.md:155-157` ("ruff and mypy
  exclude `src/baton_harness/vendor/` …") is now false and must say the tree is
  linted/typed as owned code; update the `patches/` and `VENDORING.md` descriptions
  (`README.md:67,70,92-93`) to reflect "historical record" rather than "re-vendor
  checklist."
- `# VENDOR-PATCH` markers: **no action** — retained as historical annotations per the
  acceptance criterion.
- **No `ci.yml` change** — the gate already runs over the whole tree
  (`ci.yml:24-28,39-40,51-52`); removing the config exclusions completes the coverage.

---

## 6. Verification (run at every phase start and before every PR)

Use the project venv (`.venv/Scripts/python.exe` on this Windows host; CI uses
`.venv/bin/python`). Per `CLAUDE.md § Python`, do not fall back to a global interpreter.

Per-file finding count (for sizing a batch). **Do not rely on bespoke config-override
flags** — ruff's `--config KEY=VALUE` array-override syntax and a `--config-file`
`/dev/null` path are both unverified here (and `/dev/null` does not exist on the
Windows host). Two robust options, in preference order:
1. **Passing the path may already work.** Ruff's `force-exclude` defaults to `false`,
   so an explicitly-passed excluded path is normally still checked:
   `.venv/Scripts/python.exe -m ruff check src/baton_harness/vendor/symphony/<file>.py --statistics`.
   Verify this actually reports findings once at execution start; if it silently
   reports nothing (excluded), fall back to option 2.
2. **Drop-and-revert (always works, mirrors the real gate):** temporarily remove the
   file from the `exclude`/override lists in the working tree, run the standard gate
   below, read the count, then `git restore pyproject.toml`. This is what each
   cleanup PR does permanently, so it measures the true number.

   > ⚠️ **Caveat: `git restore pyproject.toml` discards ALL uncommitted edits in that
   > file, not just the temporary exclusion-list change.** If `pyproject.toml` has any
   > other pending edit (e.g. a dependency bump being staged in the same working
   > session), `git restore` silently drops it too. Use a temporary copy
   > (`cp pyproject.toml pyproject.toml.bak` before editing, `cp pyproject.toml.bak
   > pyproject.toml` after) or a path-scoped stash (`git stash push --
   > pyproject.toml`, then `git stash pop` after measuring) instead, and explicitly
   > reapply any other pending `pyproject.toml` edits afterward.

Full gate (must be green before every PR merges — mirrors `ci.yml`):

```bash
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m ruff format --check .
.venv/Scripts/python.exe -m mypy src
.venv/Scripts/python.exe -m pytest
```

Acceptance-criteria checklist (verify at Phase 5 close):
- [ ] `vendor/symphony/` removed from ruff and mypy exclusions in `pyproject.toml`;
      all findings fixed or per-line suppressed with justification (per D2).
- [ ] `VENDORING.md` re-vendor checklist retired; file is a provenance record.
- [ ] `# VENDOR-PATCH` markers retained per the two-ledger tracking in § 8 (both
      counts scoped to `*.py`) — behavioral VP-1..VP-9 markers flat-invariant at 56 at
      every PR; mechanical "relative import for vendoring" markers flat-invariant at 9
      from Phase 1 onward (13 before Phase 1). No new `patches/` diffs required for
      future changes (policy stated in `VENDORING.md`).
- [ ] `patches/` removed or frozen (per D3); dangling citations handled accordingly.
- [ ] `README.md` vendoring section reflects ownership.
- [ ] CI runs the full gate over the tree; `tests/vendor/` regression tests green.

---

## 7. Decisions

Three decisions previously blocked execution; all three are now **RATIFIED by the
user, 2026-07-14** and Phase 1 is unblocked. One item remains genuinely open.

- **D1 — Delete `cli.py` + `log.py`? RATIFIED: delete both.** No live imports found;
  `python -m ...cli` never worked (no `__main__` guard); removes the undeclared `click`
  dependency. See § 2.
- **D2 — Docstrings: write or suppress? RATIFIED: write** real google-style docstrings
  for the ~44 findings, not per-line suppressions — makes the code genuinely owned, not
  just quieted. See § 3. Shapes Phases 2–4, including 3b (docstring-bearing modules
  land throughout).
- **D3 — `patches/`: freeze or remove? RATIFIED: freeze** as historical record. Keep
  the 9 diff files in place documenting what was patched and why; stop requiring new
  `patches/*.diff` files for future vendor-tree changes (that requirement is retired,
  not the files themselves). Affects Phase 5 and `VENDORING.md` citation handling.
- **Issue decomposition / milestone (still open):** recommend one tracking sub-issue
  per PR (Phases 1, 2, 3, 3b, 4, 5 — i.e. 6 PRs) under the existing **1.0 release gate**
  milestone, linked to parent #224. Per `CLAUDE.md § Issue Tracking`, creating issues is
  not permission to start — and this planning session has **no GitHub MCP tool
  loaded**, so no issues were created. Confirm the decomposition and I (or the router)
  can create them, or create them manually.

## 8. Risks / notes

- **`ruff format` diff volume:** the format pass (Commit A in each phase) will produce
  a large mechanical diff (`quote-style = "double"`, `line-ending = "lf"`,
  `pyproject.toml:90-93`). Isolating it in its own commit is what keeps the substantive
  fixes reviewable — do not squash A into B.
- **VP-marker preservation — track as two separate ledgers, not one combined number
  (per project-reviewer).** A single combined `grep -c VENDOR-PATCH` count can mask a
  real loss: a dropped behavioral marker could be offset by an incidental mechanical
  marker appearing elsewhere, and the count would look unchanged. Both ledgers are
  **flat invariants** — the same expected total at every PR, not a count that grows as
  files are un-excluded (un-excluding a file makes the linter check it; it does not
  change how many `# VENDOR-PATCH` comments exist in the tree). Scope both greps to
  `*.py` — `VENDORING.md` itself quotes several `# VENDOR-PATCH VP-N` markers in prose
  and would inflate an unscoped count, and it's also rewritten in Phases 1 and 5, so an
  unscoped count wouldn't even be stable. Counts verified this session against `main` @
  `30a3afe`:
  - **Ledger A — behavioral `VP-1`..`VP-9` markers**
    (`grep -rn --include=*.py "VENDOR-PATCH VP-[0-9]" src/baton_harness/vendor/symphony`):
    **56, invariant at every PR from before Phase 1 through Phase 5.** `cli.py`/`log.py`
    carry zero behavioral markers, so deleting them in Phase 1 does not move this
    number — zero loss tolerated, ever.
  - **Ledger B — mechanical "relative import for vendoring" markers**
    (`grep -rn --include=*.py "relative import for vendoring" src/baton_harness/vendor/symphony`):
    **13 before Phase 1, 9 from Phase 1 onward** (Phase 1 deletes `cli.py`'s 4 — the
    only decrease the plan allows). Invariant at 9 for every PR from Phase 1 through
    Phase 5; a drop below 9 after Phase 1 is a real loss, not an expected deletion.
  Run both greps before and after every PR and confirm each ledger equals its expected
  flat total for that stage (13/56 pre-Phase-1; 9/56 for every PR Phase 1 onward). For
  context on *which* file carries which markers — informational only, not part of the
  acceptance number, and provisional since batch composition may shift (e.g. the
  `config.py` split note in Phase 2): `state.py` carries 34× VP-6, `orchestrator.py`
  13× VP-2/VP-3/VP-5/VP-6, `hooks.py` 4× VP-1/VP-7, `worker.py` 2× VP-4, `config.py`
  2× VP-8, `workspace.py` 1× VP-9; mechanically, `orchestrator.py` carries 7,
  `worker.py` and `prompt.py` 1 each (post-Phase-1).
- **Behavioral no-op:** this is a lint/type/docs assimilation — no runtime behavior
  should change. The `tests/vendor/` regression suite is the guardrail; it must stay
  green at every phase. If a "fix" (e.g. a `B904` raise-from, an annotation-driven
  refactor) would alter behavior, stop — it is out of scope for #224.
- **mypy generic-args fixes:** the 4 "missing generic args (`dict`, `Task`)" errors may
  require importing `from __future__ import annotations` (already present per the file
  headers) and choosing correct parameterizations — verify against actual usage, don't
  blanket-`Any` them, or `warn_return_any` will just move the error.

---

## 9. Reviewer findings — incorporated

`project-reviewer` raised 4 CONCERN-level findings on the original draft of this plan.
All 4 are worked into the sections above; this is a pointer index, not a duplicate:

1. **VP-marker + `ruff-format` interaction** (long trailing markers on import lines may
   exceed line length after reflow) — convention established in § 4, applied explicitly
   in Phase 1 (§ 5) and called out again in Phases 3b and 4 where it actually bites.
2. **`worker.py` must be its own PR, unconditionally** (contains VP-4's active
   merge-prevention control, not historical code) — Phase 3 split into Phase 3
   (leaves only) + Phase 3b (`prompt.py` + `worker.py`), § 5; batch-sizing intro in § 5
   updated to exempt these two phases from re-balancing.
3. **VP-marker ledger tracked as two separate counts** (behavioral VP-1..VP-9 vs.
   mechanical "relative import for vendoring") — § 8, with the acceptance-criteria
   checklist in § 6 updated to name both as flat invariants (56 / 9), not a single
   combined or per-phase-growing number.
4. **`config.py` is not a cheap leaf** (`yaml.safe_load()` returns `Any`, triggering
   `warn_return_any` on every function that returns a parsed value) — noted inline in
   Phase 2's description (§ 5), with a per-file-gate/split-if->5-findings instruction.
