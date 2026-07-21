---
title: Module refactor proposal — reusability & maintainability (#268)
touches:
  - src/baton_harness/after_create.py
  - src/baton_harness/before_run.py
  - src/baton_harness/after_run.py
  - src/baton_harness/_auth.py
  - src/baton_harness/chain/branches.py
  - src/baton_harness/chain/merge.py
  - src/baton_harness/chain/escalation.py
  - src/baton_harness/chain/gh_deps.py
  - src/baton_harness/chain/recovery.py
  - src/baton_harness/chain/ruleset_status.py
  - src/baton_harness/chain/labels.py
  - src/baton_harness/chain/daemon.py
  - src/baton_harness/chain/subproc.py (new)
  - src/baton_harness/chain/gh_api.py (new)
  - src/baton_harness/chain/label_ops.py (new)
  - src/baton_harness/chain/daemon/ (new package)
  - tests/**
skills_relevant:
  - python
  - refactoring-discipline
---

# Module Refactor Proposal — baton-harness (#268)

**Status:** proposal / scoping only. No code changes and no child issues are
created by this document. Tracking issue: **glitchwerks/baton-harness#268**.
**Review history:** `project-reviewer` (Sonnet) pass 2026-07-20 found 1
BLOCKING + 4 CONCERN + 3 NIT (logger-name fix applied, §3.2). Adversarial
`codex-reviewer` escalation pass 2026-07-20 (user-opted, per
`architectural-review-for-plans`) found 4 further BLOCKING + 2 MAJOR + 3 minor
— folded into a revision (Phases 1, 4, 5, 6 and §5 Risks rewritten; §6 Q3
resolved, Q6 added). `project-reviewer` **re-review** 2026-07-20 found 1
further BLOCKING (Phase 6a cluster misassignment — 4 probe-context globals
moved to `launch_gate.py`, §3.2/Phase 6) + 2 CONCERN (additional named
spy-patch seams folded into the "alert fan-out" list; Phase 5 drop
independently re-verified sound) + 1 NIT (Q6 collapsed with the 6a fix as a
single pre-condition) — all folded into this current text.
project-reviewer's verdict: **Phases 1–4 ready for child-issue creation now;
Phase 6 waits until Q6 (façade vs. multi-target patching) is answered by the
user.** **User ratification 2026-07-21:** Q1, Q2, and Q6 are now resolved
(§6) — Phase 6a's façade-vs-multi-target and cluster-reassignment
pre-conditions are both satisfied. Phases 1–6 (all of 6a–6e) are now ready
for child-issue creation. Q4 (Phase 7 scope) and Q5 (Phase 3 in/out) remain
open and unaffected.

**Deliverable type:** phased refactoring plan. Each numbered phase is intended
to become one **child issue** and one small, independently-mergeable,
behavior-preserving PR.

## Behavior-preservation contract (governs this entire plan)

This is a **refactor, not a redesign**. Per the `refactoring-discipline`
skill: *"Every change must leave the external behavior of the code identical.
The observable contract — return values, side effects, error conditions,
public API signatures — must not change."* Consequences that bind every phase
below:

- **Existing tests are the contract.** No phase may change a test so it passes
  "for the wrong reason." Where a phase mechanically repoints a `mock.patch`
  target (Phase 4, where a site's helper moves into `label_ops.py`, and
  Phase 6, where daemon internals move into submodules — corrected 2026-07-20,
  this is not Phase-6-only), that is a test-mechanics change, not a
  contract change — production behavior stays identical and the assertions are
  untouched.
- **Entry points must keep resolving.** The five `module:main` console scripts
  in `pyproject.toml:46-51` (`bh-after-create` → `after_create:main`,
  `bh-before-run` → `before_run:main`, `bh-after-run` → `after_run:main`,
  `bh-daemon` → `chain.cli:main`, `bh-force-pr-not-merge` →
  `hooks.force_pr_not_merge:main`) are the packaged public surface. Any phase
  touching an entry module must keep its `main` importable at the **same dotted
  path**. "Entry point unchanged" is an explicit acceptance check on Phases 2
  and 6.
- **Bugs found are flagged, not fixed** (Discipline 4). Anything noticed in
  passing goes to the Observations list, never into a refactor diff.
- **Vendored code is out of scope.** `src/baton_harness/vendor/symphony/**`
  received a full assimilation under #224 (see `CLAUDE.md § Upstream
  dependency`); no structural split is proposed there.

> **Line-range caveat.** All `file:line` citations below were taken from an
> Explore pass on branch `main` (HEAD `6883364`) plus targeted re-reads on
> 2026-07-20. Treat line numbers as **approximate seam markers** — the code
> drifts. The implementer of each phase must re-verify exact ranges at PR time
> before extracting.

---

## 1. Audit summary

The package (`src/baton_harness/`, 31 non-vendor files, ~12,975 lines per the
Explore pass) has a small, healthy top layer (hooks + auth + CLI) and a large
`chain/` subpackage whose central module has accreted well past a maintainable
size. Three structural problems dominate.

### 1.1 God-modules (oversized, low-cohesion)

| Lines | File | Role | Primary concern |
|------:|------|------|-----------------|
| ~3426 | `chain/daemon.py` | Always-on serial daemon: poll loop + per-DAG work-unit runner | **Main target** — one module holds subprocess helpers, git-push probing, GitHub-API helpers, the ~1000-line `_run_work_unit`, and the outer poll loop |
| ~1046 | `chain/ruleset_status.py` | Read-only ruleset preflight gate (#144) | Follow-on candidate |
| ~800 | `chain/merge.py` | CI-gated `--no-ff` merge into feature branch | Follow-on candidate; note `REQUIRED_CHECKS` coupling (§5) |
| ~728 | `chain/recovery.py` | Crash/unblock recovery reconstruction | Follow-on candidate |
| ~713 | `after_run.py` | Hook: outcome classification + label reconciliation | Follow-on candidate |
| ~534 | `chain/heartbeat.py` | Thread-based heartbeat + stall detection | Follow-on candidate |

`daemon.py` is confirmed the dependency hub — it imports 19 intra-package
modules (Explore map) and its module docstring (`chain/daemon.py:1-39`)
describes at least five distinct responsibilities (poll, DAG loop, recovery,
CI-gated merge delegation, escalation). Internally it is banded by `# ---`
comment zones into: types/config, launch-gate decision, single-issue launch,
subprocess/push-probe helpers, GitHub-API helpers, the `_run_work_unit`
work-unit runner, and the daemon loop (Explore map seam markers; `_run_gh` at
`chain/daemon.py:686`, `_run_ci_gate` at `:1406`, `_run_work_unit` at `:1625`
with a `# noqa: C901 (acceptable complexity)` marker verified in-file).

### 1.2 Duplication — a subprocess runner reimplemented per module

A private `_run(...)`-shaped subprocess helper is independently defined in
**eleven** non-vendor sites (corrected 2026-07-20 — an earlier pass undercounted
this as "nine plus a tenth variant"; the Codex escalation review caught a missed
site):

- `after_create.py:48`, `before_run.py:57`, `after_run.py:167`, `_auth.py:121`,
  `chain/branches.py:49`, `chain/daemon.py:649`, `chain/escalation.py:42`,
  `chain/gh_deps.py:59`, `chain/recovery.py:76`, `chain/merge.py:183`
  (all verified via grep on 2026-07-20), plus
  `chain/ruleset_status.py:276` (`_default_runner`) and
  `before_run.py:82` (`_run_capture` — a second, distinct wrapper in the same
  file as `before_run._run`, previously omitted from this inventory).

**Critical nuance — the copies are not identical.** Three representative
bodies:

- `after_create._run` (`after_create.py:48-66`) — **streams** stdout/stderr to
  the terminal (no `capture_output`), signature `(cmd)`, no `env`.
- `branches._run` (`chain/branches.py:49-74`) — `capture_output=True,
  text=True`, signature `(cmd, env=None)`, and **defaults `env` to
  `env_for(Identity.WORKER)`** when `None`.
- `merge._run` (`chain/merge.py:183-209`) — `capture_output=True, text=True`,
  signature `(cmd, env=None)`, but a `None` env means **inherit `os.environ`
  unchanged**.

So the duplication is real, but a naive "collapse to one `_run`" would silently
change behavior (stream-vs-capture, and two different `env=None` semantics).
The extraction must be **parameterized**, and each call site's current defaults
must be preserved (see §4, Phases 1–2). This is the single biggest reuse
opportunity in the codebase, and also the one most able to introduce a
behavior regression if done carelessly.

**The module-local `_run` is also the test patch seam.** `daemon._run_gh`
(`chain/daemon.py:686-712`) documents this explicitly: *"Many test doubles
patch `_run` with a `cmd`-only signature"* — so tests do
`mock.patch("...<module>._run")`. Any consolidation must **keep a
module-local `_run` symbol** at each site (as a thin wrapper) or it breaks
every test that patches it. This constraint shapes Phases 1–2 (keep local
wrappers → zero test churn) versus Phase 6 (moving symbols → mechanical patch
repointing).

Related, more diffuse duplication:
- **`gh api` call sites scattered across ~11 modules** with no shared wrapper
  (Explore map).
- **Label fetch/edit duplicated in three places:** `daemon._label_edit` /
  `_fetch_issue_labels` (`chain/daemon.py:1083`, `:1184`),
  `after_run._current_labels` / `_reconcile_labels`, and `recovery._fetch_labels`
  / `_fetch_issue_state_and_labels` (Explore map).
- **Authed-env assembly in three places:** `app_auth.gh_env`
  (`chain/app_auth.py:476`, canonical), `identity.env_for`
  (`chain/identity.py:25`), and `daemon._authed_git_push`
  (`chain/daemon.py:715`).

### 1.3 Coupling signals & existing shared infra

- Leaf/pure modules that are safe stable dependencies: `dag.py`, `registry.py`,
  `labels.py`, `runlog.py`, `scheduler.py`, `bws_client.py`, `redispatch.py`,
  `obs_config.py`, `alert_post.py`, `_auth.py`, `_cli.py` (Explore map).
- Most-imported shared modules: `app_auth.py` (8 importers), `runlog.py` (5),
  `escalation.py` (4) (Explore map). These are the established "build on, don't
  duplicate" substrate.
- **No `utils.py` and no shared subprocess/gh-runner module exists** (Explore
  map + grep) — this is the gap Phases 1–3 fill.
- `chain/labels.py` (read in full, `chain/labels.py:1-129`) is **deliberately
  pure**: constants (`LABEL_*`, `STATE_LABELS`) plus pure checkers
  (`assert_single_state`, `target_state_from_observed`), with a documented
  import-direction rule (`chain/labels.py:7-9`): *"chain modules may import from
  this module; hooks import FROM this module. This module must never import from
  hooks."* **Map-vs-code correction:** the Explore map suggested `labels.py` as
  "the natural home" for the duplicated label-fetch helpers. That is declined
  here — injecting `gh api` I/O and a subprocess dependency into `labels.py`
  would break its purity and its import-direction contract. §4 Phase 4 proposes
  a **separate `label_ops.py`** I/O module instead and keeps `labels.py` pure.

---

## 2. Guiding principles

Short and concrete — the north star for every phase:

1. **Behavior first.** Observable behavior is frozen. Tests are the contract.
   (`refactoring-discipline` Discipline 1–2.)
2. **Extract shared seams before splitting god-modules.** De-duplication
   (Phases 1–4) creates the low-dependency utilities that the `daemon.py` split
   (Phase 6) then leans on, so the split moves *less* code and each moved
   cluster is cleaner.
3. **Keep shared utilities dependency-free.** The subprocess util imports only
   the stdlib. Module-specific concerns (`env_for(WORKER)` defaults, `gh_env`
   tokens) stay in each caller's thin wrapper — a util that imports `identity`
   or `app_auth` would reintroduce the import cycles this refactor must avoid.
4. **Preserve the test patch seam.** Every module keeps a local `_run` symbol;
   util extraction changes zero test files. (§1.2.)
5. **One concern per PR** (`refactoring-discipline` Discipline 3). No phase
   mixes a behavior change or bug fix into the diff.
6. **Cohesion over line-count.** The goal is modules that each name one
   responsibility, not uniformly small files. (`simplicity-first` framing:
   splitting is a deliberate choice justified by the god-module's size, not a
   reflexive minimize.)

New modules follow the repo's existing standards: `line-length = 79`, `mypy
--strict`, Google-style docstrings, `from __future__ import annotations`
(`pyproject.toml:60-95`).

---

## 3. Target structure

### 3.1 New shared utilities (Phases 1–4)

```
src/baton_harness/chain/
  subproc.py      NEW  dependency-free subprocess runner: run_cmd(cmd, *,
                       capture, text, env) -> CompletedProcess. Home of the
                       Windows cp1252 encoding="utf-8" guard (once, not 11x).
  gh_api.py       NEW  thin `gh api` wrapper built on subproc.run_cmd
                       (optional, Phase 3).
  label_ops.py    NEW  GitHub-API label fetch/edit I/O; imports subproc +
                       labels (pure). labels.py stays pure.
```

### 3.2 `daemon.py` → `daemon/` package (Phase 6)

Before:

```
src/baton_harness/chain/
  daemon.py            ~3426 lines — everything
```

After (one submodule per internal seam already banded in the file and already
mirrored by the test-file clusters):

```
src/baton_harness/chain/daemon/
  __init__.py          6a step 1: receives the ENTIRE daemon.py body verbatim
                       (the atomic module→package conversion — see Phase 6
                       below, corrected 2026-07-20). Then shrinks as each
                       later sub-PR (6a step 2, 6b-6e) extracts its cluster,
                       ending as a thin re-export of the public surface
                       (run_daemon, reconstruct, anything chain/cli.py
                       imports) so `baton_harness.chain.daemon` keeps
                       resolving.
  push_probe.py        ProbeDenialReason, ProbeResult, _authed_git_push,
                       _attempt_probe_ref_cleanup, _probe_worker_push_denied,
                       _PUSH_DENIAL_SIGNALS, _PROBE_PUSH_TIMEOUT_SECONDS
                       (6a step 2)
  gh_api_helpers.py    _slugify, _find_issue_pr, _fetch_issue_obj,
                       _fetch_full_milestone_members,
                       _effective_required_checks, _run_ci_gate, _open_pr
                       (* label helpers land in label_ops.py in Phase 4;
                        what remains here is issue/PR/CI-gate API glue)
  launch_gate.py       _should_launch_worker, _build_preflight_runner,
                       _resolve_app_id, _launch_one_issue, reconstruct entry,
                       plus _active_probe_repo_root, _NonGitRepoRootSentinel,
                       _NON_GIT_REPO_ROOT, _COMPARATOR_TIMEOUT_SECONDS
                       (moved here 2026-07-20 — see Phase 6 6a for why these
                        must not live in push_probe.py despite their
                        daemon.py line range)
  work_unit.py         _run_work_unit (+ any cleanly-extracted step helpers)
  poll.py              run_daemon, _poll_and_run, _select_work_unit,
                       warn_if_async_escalation_unconfigured
```

**Why `__init__.py` receives the full body first (2026-07-20 correction):**
Python cannot have both `chain/daemon.py` and `chain/daemon/__init__.py` at
the same import location, so the module→package rename cannot happen
incrementally file-by-file — it is one atomic move, validated on its own,
before any cluster is extracted. See Phase 6 for the full sequencing and the
separate patch-lookup fix required at every extraction step.

**Public-path preservation.** `bh-daemon` resolves to `chain.cli:main`
(`pyproject.toml:50`), and `chain/cli.py` imports the daemon symbols it needs
(`app_auth`, daemon entry). Converting `daemon.py` into a `daemon/` package
keeps `from baton_harness.chain import daemon` working; `__init__.py` re-exports
what `cli.py` references. **Caveat that drives the sub-phasing:** a re-export in
`__init__.py` does **not** make `mock.patch("...chain.daemon._run")` affect a
caller that now lives in `daemon.work_unit` and binds its own module's `_run`
(the "patch where it's looked up" rule). So each Phase-6 sub-PR that moves a
cluster must repoint that cluster's test patches to the new submodule path.
Production behavior is unchanged; only the patch target string moves.

**Logger-name convention (fixes project-reviewer BLOCKING finding, 2026-07-20).**
`project-reviewer` flagged that a naive `logging.getLogger(__name__)` in each
new submodule would change the emitted logger name from
`baton_harness.chain.daemon` to `baton_harness.chain.daemon.<submodule>` —
altering production log-aggregation output and breaking four `r.name ==
"baton_harness.chain.daemon"` / `caplog.at_level(logger=...)` assertions in
`test_daemon.py`. **Decision: every submodule under `chain/daemon/` acquires
its logger with the hard-coded original string, not `__name__`:**

```python
logger = logging.getLogger("baton_harness.chain.daemon")
```

This is a one-line, explicit deviation from the `__name__` idiom, called out in
each submodule with a short comment (`# hard-coded: preserves pre-split logger
name, see #268`) so a future contributor doesn't "fix" it back to `__name__`.
`__init__.py` does **not** need to define or re-export a logger — each
submodule owns its own call to `getLogger` with the fixed string, so there is
no import-order or re-export subtlety. This keeps every log record's logger
name, and all four `test_daemon.py` assertions, byte-identical pre- and
post-split. Add to Phase 6's behavior-preservation check: **grep every new
submodule for `getLogger(` and confirm each uses the fixed string, not
`__name__`.**

`ruleset_status.py`, `merge.py`, `recovery.py`, `after_run.py`, `heartbeat.py`
are **not** restructured in this plan's core phases — they are follow-on
candidates (§4 Phase 7), each its own future issue.

---

## 4. Phased plan

Ordered by risk/leverage: dependency-free util extractions first (they
de-duplicate and create the seams the god-module split reuses), then the I/O
consolidations, then the high-line-count `daemon.py` split. Each phase = one
child issue = one PR.

### Phase 1 — `subproc.run_cmd` util + migrate the capture-family `_run`s

- **Extracts:** a new `chain/subproc.py` with a single dependency-free
  `run_cmd(cmd, *, capture=..., text=..., env=None, timeout=None, check=True)`
  returning `subprocess.CompletedProcess`, holding the `encoding="utf-8"` guard
  once. **Corrected 2026-07-20 (Codex escalation, BLOCKING):** the signature
  originally proposed here — `run_cmd(cmd, *, capture, text, env)` — dropped
  `timeout`, which `daemon._run` currently exposes and forwards
  (`daemon.py:649`), and which the push probe relies on with a positive value
  (`daemon.py:900`), pinned by an assertion in `test_daemon_push_probe.py:1260`.
  `timeout` and `check` are now first-class `run_cmd` params, not an omission
  callers have to work around.
- **Migrates (thin local wrappers delegating to `run_cmd`):** the
  `capture_output=True, text=True` family whose bodies are near-identical —
  `branches._run` (`branches.py:49`), `merge._run` (`merge.py:183`),
  `escalation._run` (`escalation.py:42`), `gh_deps._run` (`gh_deps.py:59`),
  `recovery._run` (`recovery.py:76`), `daemon._run` (`daemon.py:649`) plus
  `daemon._run_gh` (`daemon.py:686`).
- **Preserve exactly:** each wrapper keeps its own `env=None` semantics —
  `branches` defaults `None` → `env_for(Identity.WORKER)`; `merge`/`daemon`
  leave `None` → inherit `os.environ`. These defaults stay in the **wrapper**,
  not the util.
- **Known keyword-shape differences across all migration targets (Phases 1–2),
  documented 2026-07-20 so `run_cmd`'s parameter set is designed against the
  real call sites, not just the capture-family):**
  - `after_create._run` (Phase 2) passes `check=False` but omits
    `capture_output`/`text` entirely (`after_create.py:62`).
  - `after_run._run` (Phase 2) captures output but does not explicitly pass
    `check=False` (`after_run.py:181`).
  - `ruleset_status._default_runner` (Phase 2) prepends the literal `"gh"`
    argv element and always supplies worker env — it is not a drop-in
    `_run`-shape at all (`ruleset_status.py:276`).
  - `run_cmd`'s parameters must have explicit, testable defaults that
    reproduce each of these shapes via the wrapper, not via `run_cmd` special-
    casing per caller.
- **Files changed:** `subproc.py` (new) + the six modules above. **No test
  files change** — the local `_run`/`_run_gh` symbols survive as patch targets.
- **Behavior-preservation check:** full suite green with zero test edits. Add
  unit tests for `subproc.run_cmd` itself (new module needs coverage,
  including a `timeout` pass-through test). **Add characterization tests per
  migrated wrapper** (not just a suite-green pass — most existing tests patch
  the module-local `_run` and so never exercise `run_cmd`'s actual
  `subprocess.run(...)` call args): assert each wrapper's call into `run_cmd`
  — and `run_cmd`'s resulting `subprocess.run(...)` invocation — matches the
  pre-migration call byte-for-byte (argv, `capture_output`, `text`, `env`,
  `check`, `timeout`).
- **Risk:** low-medium (raised from low 2026-07-20). The divergent `env=None`
  semantics are one trap; the missing `timeout` parameter and the
  keyword-shape differences above are a second, since a full-suite pass alone
  does not prove exact `subprocess.run` argument parity when tests patch
  `_run` directly.

### Phase 2 — migrate the stream-family `_run`s

- **Migrates:** the modules whose `_run` **streams** to the terminal or has a
  distinct signature — `after_create._run` (`after_create.py:48`, no capture),
  `before_run._run` (`before_run.py:57`), `after_run._run`
  (`after_run.py:167`), `_auth._run` (`_auth.py:121`), and
  `ruleset_status._default_runner` (`ruleset_status.py:276`). These call
  `run_cmd(..., capture=False)` (or the ruleset variant's shape) via a local
  wrapper.
- **Files changed:** those five modules. Test files unchanged (local symbols
  preserved).
- **Behavior-preservation check:** suite green, zero test edits. **Entry-point
  check:** `after_create.main`, `before_run.main`, `after_run.main` still
  import and run (these are `bh-*` console scripts, `pyproject.toml:47-49`).
- **Risk:** low-medium — kept separate from Phase 1 precisely because the
  stream-vs-capture behavior differs, so the PR's preservation argument is
  uniform within the phase.

### Phase 3 — `gh_api` wrapper (optional)

- **Extracts:** a `chain/gh_api.py` thin helper over `subproc.run_cmd` for the
  scattered `gh api` invocations, so the JSON-decode + error-shape handling
  lives once.
- **Files changed:** `gh_api.py` (new) + incremental adoption at call sites (can
  itself be split per-module if the diff is large).
- **Behavior-preservation check:** per adopted call site, the emitted `gh`
  argv and error handling are byte-identical; suite green.
- **Risk:** medium and **lower-certainty** — `gh api` call sites are diffuse and
  may vary (flags, `--jq`, error tolerance). Recommended only after Phase 1
  proves the wrapper pattern. Could be deferred without blocking Phase 6.

### Phase 4 — `label_ops.py` I/O module (keep `labels.py` pure)

**Rewritten 2026-07-20 (Codex escalation, MAJOR).** The three label-fetch sites
are **not** "possibly-different" — they are confirmed concretely incompatible
in signature and return type. Phase 4 does not merge them into one function;
it moves each into `label_ops.py` as three distinct, separately-named
functions, each preserving its own site's exact contract.

- **Extracts:** a new `chain/label_ops.py` with three distinct functions (not
  a single merged helper), each a straight relocation of one existing site's
  logic:
  - `fetch_daemon_labels(...) -> set[str] | None` — from `daemon._label_edit` /
    `_fetch_issue_labels` (`daemon.py:1083`, `:1184`). Lowercase; takes
    repo/token params; failure means `None`.
  - `fetch_after_run_labels(...) -> list[str] | None` — from
    `after_run._current_labels` (`after_run.py:357`). Case-preserving; uses
    ambient repository (no repo/token params); emits hook errors on failure
    rather than returning a sentinel.
  - `fetch_recovery_labels(...) -> set[str]` — from `recovery._fetch_labels` /
    `_fetch_issue_state_and_labels` (`recovery.py:208`). Lowercase; takes
    repo/token params; failure means an **empty set**, not `None`.
  - `after_run._reconcile_labels` (`after_run.py:404`) is **not** included in
    this extraction — it is a state-machine operation with ordered mutations
    and integer failure results, not label I/O. It stays in `after_run.py`,
    calling the new `fetch_after_run_labels` for its read step.
- **Files changed:** `label_ops.py` (new), `daemon.py`, `after_run.py`,
  `recovery.py`. **Patch repointing is expected, not just possible** — e.g.
  `test_daemon.py:5605` calls `_fetch_issue_labels` directly, not via
  `mock.patch`, and must be updated to import the relocated
  `fetch_daemon_labels`.
- **Behavior-preservation check:** each of the three functions is a
  byte-for-byte relocation, not a merge — there is no equivalence proof to
  construct because no unification is attempted. Guarded by
  `test_after_run.py`, `test_recovery.py`, `test_daemon_park_label_clear.py`,
  plus the direct call site at `test_daemon.py:5605`.
- **Risk:** medium — three distinct functions relocating cleanly is lower risk
  than the previously-proposed merge, but the direct (non-patch) call site
  means this phase touches test code beyond simple `mock.patch` repointing.

### Phase 5 — dropped, already resolved in the current code (2026-07-20)

**Removed as a phase per the Codex escalation review.** The review checked the
live code directly: `app_auth.gh_env` already delegates to
`identity.env_for(Identity.APP, ...)` (`app_auth.py:476`), and
`daemon._authed_git_push` already calls `gh_env` (`daemon.py:744`). There are
not three independent authed-env assemblers left to reconcile — the "review
and possibly unify" question this phase asked is already answered by the
code as it exists today. No child issue is cut for this. Recorded as an audit
finding, not a phase — see Non-goals below and former Open Question Q3
(resolved, removed from §6). **Independently re-verified 2026-07-20
(project-reviewer re-review):** confirmed `app_auth.gh_env`
(`app_auth.py:509-511`) calls `env_for(Identity.APP, installation_token=...)`
via a deferred import, and `daemon._authed_git_push` (`daemon.py:744`) calls
`gh_env`. Two independent reviews now agree the drop is sound.

### Phase 6 — split `daemon.py` into a `daemon/` package

**Substantially rewritten 2026-07-20 (Codex escalation, 3 BLOCKING findings).**
The prior version of this phase understated three things: the package
conversion cannot happen incrementally as originally sequenced; the
`mock.patch("...chain.daemon.X")` re-export scheme does not actually intercept
calls from code that has moved into a submodule; and the test-repoint surface
is far larger than "one or two files." All three are addressed below before
this phase is cut as a child issue.

**6a is now the atomic module→package conversion, not just the first
extraction.** Python cannot have both `chain/daemon.py` and
`chain/daemon/__init__.py` coexist — the package conversion is a single
atomic step, not something that can be deferred to 6e while earlier sub-PRs
still talk about "`daemon.py` (shrinks)." 6a must, in one PR:

1. Move the entire current `daemon.py` body into `chain/daemon/__init__.py`
   verbatim (no extraction yet) — this alone is the module→package rename and
   must be validated in isolation (imports, `bh-daemon --once`, full suite)
   before any cluster is extracted.
2. Only then extract the `push_probe.py` cluster: `ProbeDenialReason`,
   `ProbeResult`, `_authed_git_push`, `_attempt_probe_ref_cleanup`,
   `_probe_worker_push_denied`, `_PUSH_DENIAL_SIGNALS`,
   `_PROBE_PUSH_TIMEOUT_SECONDS` (`daemon.py:715-1051`) — these ARE
   probe-owned and their only users are inside this cluster.
   **Corrected 2026-07-20 (project-reviewer re-review, BLOCKING):** an
   earlier version of this plan assigned `_active_probe_repo_root`,
   `_NonGitRepoRootSentinel`, `_NON_GIT_REPO_ROOT`, and
   `_COMPARATOR_TIMEOUT_SECONDS` to `push_probe.py` based on their physical
   line range in `daemon.py` (`:125-206`). That was wrong: their actual
   users are exclusively in the `launch_gate.py` cluster —
   `_active_probe_repo_root` is written via `global` by `_launch_one_issue`
   (`daemon.py:546`) and read by `_should_launch_worker` (`daemon.py:278`);
   `_NonGitRepoRootSentinel` is type-checked in `_should_launch_worker`
   (`:280`); `_NON_GIT_REPO_ROOT` is assigned in `_launch_one_issue`
   (`:554`); `_COMPARATOR_TIMEOUT_SECONDS` (`:143`) is used only in
   `_build_preflight_runner` (`:433`, `:466`). Python's `global` statement
   is module-local — if `_active_probe_repo_root` lived in `push_probe.py`
   while `_launch_one_issue`/`_should_launch_worker` live in
   `launch_gate.py`, the two functions would silently mutate/read two
   *different* module-level names, making the #223 probe-context gate
   permanently inert with no loud failure signal. **These four names move
   to the 6c / `launch_gate.py` cluster instead** (see 6c below) — this also
   removes the prior "avoid a parent-package import cycle" rationale, which
   was backwards: both dependencies are one-directional
   `launch_gate → push_probe` and were never cyclic.
- Guard: `test_daemon_push_probe.py`.

**Patch-lookup fix (applies to every sub-phase 6a–6e).** A re-export in
`__init__.py` does not make `mock.patch("...chain.daemon.X")` affect a caller
that now lives in a submodule and imports `X` locally (`from .push_probe
import _probe_worker_push_denied`) — patching the package-level re-export
does not touch the submodule's own local binding. This breaks **all four**
suite-wide autouse fixtures, not a hypothetical edge case:
- `tests/conftest.py:53` patches `_resolve_app_id`.
- `tests/conftest.py:86` patches `check_ruleset_signals`.
- `tests/conftest.py:123` patches `_probe_worker_push_denied` and imports
  `ProbeResult` from the package path.
- `tests/chain/conftest.py:13` patches `daemon.reconcile_startup` — previously
  unflagged; after `run_daemon` moves to `poll.py` in 6e, this fixture stops
  intercepting the real call at `daemon.py:2759`, which can raise `SystemExit`
  on invalid credentials in the test environment.

Left unpatched, `check_ruleset_signals` makes live `gh api` calls
(`tests/conftest.py:64` documents this explicitly), and
`_probe_worker_push_denied` can execute a real authenticated `git push`
(`daemon.py:888`) — a transport failure returns `ProbeResult(denied=False)`
(`daemon.py:928`, causing launch refusal) while a real recognized denial
returns `denied=True` (`daemon.py:1024`), so some tests could pass having
silently performed live network I/O rather than failing loudly.

**Fix, required before any 6b–6e sub-PR is cut:** each Phase-6 sub-PR must
choose and document one of two concrete strategies per moved symbol — not
"repoint the mock.patch target" as a blanket instruction:
1. **Stable module-local façade wrappers** — the destination submodule keeps
   a thin function that autouse fixtures patch at *that* submodule's path, and
   the package `__init__.py` re-export calls through the façade so both the
   old (package-level) and new (submodule-level) patch targets stay valid
   during the migration window; or
2. **Explicit multi-target patching** — every affected fixture/test is
   updated to patch (or is redesigned to patch) the actual submodule location
   the moved code now lives at, verified per test file, not assumed from the
   `__init__.py` surface.
Whichever is chosen, it is a **substantive test-harness migration**, not a
mechanical string replacement — treat it as its own reviewed diff per
sub-PR, not a one-line patch-string edit.

- **6b — `gh_api_helpers.py`:** `_slugify`, `_find_issue_pr`, `_fetch_issue_obj`,
  `_fetch_full_milestone_members`, `_effective_required_checks`, `_run_ci_gate`,
  `_open_pr` (`daemon.py:1057-1619`; label helpers already gone via Phase 4).
  **Also update `merge.py:24` and `merge.py:110`**, which reference
  `chain.daemon._effective_required_checks` in comments/docs — these go stale
  (not broken at runtime) once the symbol moves; fix in the same PR so the
  refactor doesn't knowingly leave cross-reference drift.
  Guards: `test_daemon_preflight.py`, `test_daemon.py`.
- **6c — `launch_gate.py`:** `_should_launch_worker` (`daemon.py:206`),
  `_build_preflight_runner`, `_resolve_app_id`, `_launch_one_issue`,
  `reconstruct` entry (`daemon.py:206-648`). **Guard-test list corrected
  2026-07-20:** `test_worker_disallowed.py` and `test_identity_spawn_guard.py`
  — originally listed as 6c guards — do **not** exercise launch-gate behavior;
  the former tests vendored `Worker._build_claude_args`, the latter is an
  AST-wide subprocess-env guard unrelated to this cluster. The real guard is
  `test_daemon_preflight.py` plus the `_launch_one_issue` integration tests in
  `test_daemon_push_probe.py` starting at line 638 (that file spans both the
  6a and 6c clusters — split its assertions accordingly, don't assume
  file-per-cluster is clean here).
- **6d — `work_unit.py`:** `_run_work_unit` (`daemon.py:1625`, the `# noqa: C901`
  ~1000-line async function). **Highest risk** (§5). Guards: `test_daemon.py`,
  `test_daemon_orphan_scan.py`, `test_daemon_park_label_clear.py`.
- **6e — `poll.py`:** `run_daemon`, `_poll_and_run`, `_select_work_unit`,
  `warn_if_async_escalation_unconfigured` (`daemon.py:2643-3426`).
  `__init__.py`'s public-surface re-export was already established in 6a (see
  above) — 6e no longer needs to "create" `__init__.py`, only update its
  re-export list as `poll.py`'s symbols move out. **Guard-test correction
  2026-07-20:** `test_worktree_gc.py` — originally listed as a 6e guard —
  actually imports and tests `recovery.py` (`test_worktree_gc.py:71`), not
  `daemon.poll`; it is not a guard for this cluster. Guard: `test_daemon.py`.
- **`alert` fan-out (applies across 6b–6e, flagged 2026-07-20, BLOCKING for
  the "mechanical repoint" framing specifically).** `alert` is currently one
  imported binding (`daemon.py:67`) but its call sites land in **all four**
  destination submodules: `launch_gate.py` (`daemon.py:580`),
  `gh_api_helpers.py` (`daemon.py:1503`), `work_unit.py` (`daemon.py:1709`),
  `poll.py` (`daemon.py:2851`). `test_daemon.py` has **77** references to
  `daemon.alert`, including a shared end-to-end fixture that patches one
  `daemon._run`, one `daemon.merge_issue_branch`, and one `daemon.alert`
  together (`test_daemon.py:163`) — after the split that fixture's target
  exists in four different modules, not one, so a single patch cannot cover
  it. The same fan-out applies to `_run`/`_run_gh` (push-probe calls `_run` at
  `daemon.py:900`; work-unit calls it at `daemon.py:2609`; GitHub helpers and
  polling call `_run_gh` at `daemon.py:1110` and `daemon.py:2957`). There are
  also **direct, non-patch** test accesses that a re-export alone doesn't
  preserve: `daemon_mod._probe_worker_push_denied` /
  `daemon_mod.ProbeResult` at `test_daemon_push_probe.py:234`;
  `daemon_mod._should_launch_worker` beginning at `test_daemon_preflight.py:163`;
  `daemon_mod._run_work_unit` at `test_daemon.py:9594`. Each moved symbol with
  fan-out call sites or direct test access needs its own entry in the
  per-sub-PR patch/access matrix required below — this is why "one or two
  test files" (previous estimate) is replaced with a full dependency matrix.
  **Additional named examples (project-reviewer re-review, 2026-07-20,
  CONCERN — these fail loud via a broken assertion rather than silent live
  I/O like the four autouse fixtures, but the matrix must still catch
  them):** `daemon_mod.env_for` (`test_daemon_push_probe.py:362`, binding
  moves to `push_probe.py` in 6a); `daemon_mod.post_slack_alert`
  (`test_daemon_push_probe.py:817,945,1030,1134`, binding moves to
  `launch_gate.py` in 6c); `daemon_mod.RunLog` (`test_daemon.py:3792,5361`,
  binding moves to `work_unit.py` in 6d). Also: the `test_daemon.py:163`
  end-to-end fixture mixes patches that survive the split
  (`chain.recovery.reconstruct`, `chain.branches.*` — already source-module
  paths, unaffected) with patches that break (`chain.daemon.merge_issue_branch`,
  `.alert`, `daemon_mod._run` — daemon-bound names); the correct per-sub-PR
  instruction is "repoint the daemon-bound targets **within** the fixture,"
  not "repoint the fixture" wholesale.
- **Files changed per sub-PR:** the new submodule + `daemon/__init__.py`
  (after 6a) + **every test file identified by an explicit dependency-by-
  caller matrix for that sub-PR's moved symbols** — not an estimated "one or
  two." Each sub-PR's PR description must include that matrix (symbol → every
  test file that patches or directly accesses it → chosen fix strategy from
  the two options above).
- **Behavior-preservation check per sub-PR:** production behavior identical;
  every patch target and direct test access identified in that sub-PR's
  dependency matrix is verified working (not just "the string moved");
  cluster's guard tests green (using the corrected guard-test lists above);
  **entry-point check** — `chain.cli:main` (`bh-daemon`) still imports and
  `--once` runs; **logger-name check** — every `getLogger(` call in the new
  submodule uses the hard-coded `"baton_harness.chain.daemon"` string (§3.2),
  not `__name__`.
- **Risk:** medium-high (6a, because it is now an atomic package conversion,
  not a simple first extraction) to high (6d, unchanged) to medium (6b, 6c,
  6e) — all raised from the prior "medium" baseline given the patch-lookup and
  fan-out findings above apply across every sub-phase.

### Phase 7 — other god-modules (follow-on; each its own future issue)

Deferred beyond this plan's core; listed so they are tracked, not lost:
`ruleset_status.py` (~1046), `merge.py` (~800), `recovery.py` (~728),
`after_run.py` (~713), `heartbeat.py` (~534). Each is smaller and less central
than `daemon.py` and should be scoped individually after the daemon split
validates the pattern. **`merge.py` carries a coupling constraint** — see §5.

---

## 5. Risks & non-goals

### Risks

- **Divergent `_run` semantics (Phases 1–2).** The copies differ in
  stream-vs-capture and in two `env=None` meanings (§1.2). Mitigation:
  parameterized util; per-wrapper default preservation; zero-test-churn as the
  pass/fail signal.
- **Import cycles.** Shared utils must stay stdlib-only. A `subproc`/`gh_api`
  that imports `identity` or `app_auth` would create cycles with the daemon
  and hook layers. Mitigation: module-specific env/token logic stays in
  callers' wrappers (Principle 3).
- **Test patch seam (Phases 1–4 vs 6).** Util phases keep local `_run` symbols
  → zero test edits. The daemon split **moves** symbols → each sub-PR must
  repoint that cluster's `mock.patch` targets. Framed as mechanical, not a
  contract change, but it is the line separating low-churn phases from the
  god-module split.
- **`_run_work_unit` shared mutable locals (Phase 6d).** The ~1000-line async
  function (`daemon.py:1625`, `# noqa: C901`) has Steps 0–3 that thread locals
  (DAG, scheduler, feature-branch state, push results) across step boundaries.
  Extract-method is only behavior-preserving if an extracted step does not
  read/write a local a later step depends on. Mitigation: extract only cleanly
  separable steps first (Step 1 branch setup, Step 3 completion-push/PR-body);
  treat the Step-2 inner serial worker loop as a possibly-leave-intact core
  (see Open Question Q1). Do **not** pre-commit to fully dissolving the inner
  loop.
- **`merge.REQUIRED_CHECKS` import-path coupling (Phase 7).**
  `test_required_checks_match_ci_yml.py` couples `merge.REQUIRED_CHECKS` to
  `ci.yml`. Any `merge.py` split must keep `REQUIRED_CHECKS` importable at its
  current dotted path, or the test + CI coupling moves in the same PR.
- **Entry-point resolution.** Splitting an entry module (`after_run`,
  `chain.cli` via daemon imports) risks breaking a `module:main` script.
  Mitigation: explicit entry-point check on Phases 2 and 6.
- **Patch-lookup semantics across Phase 6 (added 2026-07-20, was BLOCKING).**
  `mock.patch("...chain.daemon.X")` only replaces the name bound in
  `__init__.py`; a caller that has moved into a submodule and imports `X`
  locally is untouched by that patch. This breaks all four suite-wide autouse
  fixtures (three in `tests/conftest.py`, one in `tests/chain/conftest.py`
  patching `reconcile_startup`) unless each sub-PR explicitly applies one of
  the two fix strategies in Phase 6 (façade wrappers or multi-target
  patching). Left unaddressed, the failure mode is not a loud test failure —
  it's a silently-unpatched fixture making a real `gh api` call or a real
  `git push` during CI.
- **Atomic package-conversion risk (added 2026-07-20, was BLOCKING).** 6a's
  first step — moving the entire `daemon.py` body into
  `chain/daemon/__init__.py` verbatim before any extraction — is a large,
  single-commit rename with no partial-progress checkpoint. Mitigation: land
  and validate that step alone (imports, `bh-daemon --once`, full suite) as
  its own reviewed commit within the 6a PR, before the `push_probe.py`
  extraction commit, so a revert of just the extraction is possible if it
  goes wrong.
- **`alert`/`_run`/`_run_gh` fan-out across Phase 6 submodules (added
  2026-07-20, was BLOCKING).** Several symbols (`alert`, `_run`, `_run_gh`)
  are called from multiple destination submodules, and several are accessed
  directly in tests rather than via `mock.patch`. "One or two test files"
  understated this; every sub-PR now requires an explicit dependency-by-
  caller matrix (Phase 6 above) rather than an estimate.
- **Guard-test list accuracy (added 2026-07-20, was MAJOR).** Three
  previously-named Phase 6 guard tests do not test the code they were
  assigned to guard (`test_worker_disallowed.py`, `test_identity_spawn_guard.py`
  for 6c; `test_worktree_gc.py` for 6e). Relying on a named guard test that
  doesn't actually cover the moved code would let a regression through
  silently. Corrected lists are in Phase 6 above; re-verify guard coverage at
  PR time regardless, since the code will have drifted further by then.

### Non-goals (explicitly out of scope)

- **Any behavior change or bug fix.** Bugs noticed in passing are flagged in
  Observations, never fixed in a refactor PR (`refactoring-discipline`
  Discipline 3–4).
- **Redesign** of the daemon's concurrency model, label state machine,
  recovery protocol, or CI-gate logic. Structure only.
- **Vendored `symphony/` restructuring** (#224 assimilation is complete;
  `CLAUDE.md § Upstream dependency`).
- **Forcing authed-env unification** across three different token models
  (Phase 5 is a review that may correctly conclude "leave as-is").
- **Adding new abstractions beyond the four named modules** (`subproc`,
  `gh_api`, `label_ops`, `daemon/` package) without surfacing them as an open
  question first.

---

## 6. Open questions (need the user's decision)

- **Q1 — RESOLVED 2026-07-21, no longer open.** Was: "How aggressively to
  split `_run_work_unit` (Phase 6d)?" Conservative default: extract only the
  cleanly-separable steps (branch setup, completion-push/PR-body) and leave
  the Step-2 inner serial worker loop intact as the function core.
  Aggressive: fully dissolve the inner loop into named step functions. The
  user ratified the plan's own recommended default — **conservative** —
  since the aggressive path raises the shared-locals regression risk (§5).
  No further action needed.
- **Q2 — RESOLVED 2026-07-21, no longer open.** Was: "`daemon/` package vs
  sibling modules?" This plan proposes a `daemon/` package with
  `__init__.py` re-exports to preserve the `baton_harness.chain.daemon`
  import path. Alternative: sibling modules (`daemon_push_probe.py`, etc.)
  at `chain/` level, which avoids the package/re-export step but pollutes
  the `chain/` namespace and still requires patch repointing. The user
  ratified the plan's own recommended default — the **package** approach —
  which §3.2's Phase 6 text already assumes throughout (6a is written as
  the package conversion). No further action needed.
- **Q3 — RESOLVED 2026-07-20, no longer open.** Was: "Is Phase 5 (authed-env)
  worth doing?" The Codex escalation review checked the live code and found
  `app_auth.gh_env` already delegates to `identity.env_for`, and
  `daemon._authed_git_push` already calls `gh_env` — there are not three
  independent assemblers left to unify. Phase 5 is dropped (see §4 Phase 5);
  no user decision needed.
- **Q4 — Scope of Phase 7?** Should the other god-modules
  (`ruleset_status`, `merge`, `recovery`, `after_run`, `heartbeat`) be tracked
  as child issues now, or deferred until the `daemon.py` split lands and
  validates the approach?
- **Q5 — RESOLVED 2026-07-21, no longer open.** Was: "Phase 3 (`gh_api`
  wrapper) — in or out?" The user initially said **in** (tracked as issue
  #271). A follow-up call-site survey of every `gh api` invocation in
  `src/baton_harness/` (see #271 for the full inventory) found no clean
  extraction target: `_auth.py`, `chain/gh_deps.py`, and `chain/merge.py` are
  each guarded by an existing `mock.patch.object(<mod>, "_run", ...)` test
  seam (10+ patch sites in some files), so adopting a shared `gh_api.py`
  wrapper there means bypassing or rewriting that seam — a test-behavior
  change, not a mechanical extraction; `chain/sandbox_config.py` and
  `chain/ruleset_status.py` are already dependency-injected (`run=`/`runner=`
  params) with no JSON-decode or error-shape duplication left to centralize.
  The one site with genuine duplication worth extracting
  (`gh_deps._paginate`) would still require repointing `test_gh_deps.py` off
  its `_run` mock. Shipping `gh_api.py` unused would be dead code, failing
  `simplicity-first` and this plan's own non-goal against speculative
  abstractions (§5). Presented with this finding, the user chose **won't-do**
  over forcing either the seam-bypass or a `gh_deps`-only path. **Final
  decision: won't-do, closed as #271.** No further action needed.
- **Q6 — RESOLVED 2026-07-21, no longer open.** Was: "Phase 6 patch-lookup
  fix: façade wrappers or multi-target patching?" Every Phase-6 sub-PR must
  fix the `mock.patch` lookup problem (see §4 Phase 6) by either (a) keeping
  stable module-local façade wrappers that both old and new patch targets
  can hit during the migration, or (b) redesigning affected fixtures/tests
  to patch the actual new submodule location directly. The user ratified
  the plan's own recommendation: **multi-target patching** for the four
  suite-wide autouse fixtures specifically (`tests/conftest.py:53`, `:86`,
  `:123`, `tests/chain/conftest.py:13` — worth getting exactly right once),
  and **stable module-local façade wrappers** for lower-traffic
  direct-access sites. This was the other pre-condition (alongside the 6a
  cluster-reassignment fix, §4 Phase 6 6a step 2) for a correct 6a PR scope
  — both are now satisfied, so Phase 6a is ready to be scoped/cut as a
  child issue like the rest of Phase 6. No further action needed.

---

## Observations (flagged, not actioned — `refactoring-discipline` Discipline 4)

- **Test-coverage note:** `daemon.py` is covered by 5+ test files keyed to its
  internal clusters (`test_daemon*.py`), and the cluster names already align
  with the proposed seams — good. Before Phase 6d specifically, confirm
  `_run_work_unit`'s Step-2 inner loop has assertion-level coverage, not just
  happy-path, since that is the extraction with the least margin for silent
  behavior drift.
- **`daemon._run_gh` (`daemon.py:686-712`)** exists purely to work around test
  doubles that patch `_run` with a `cmd`-only signature. After Phase 1 it may be
  simplifiable, but simplifying it is itself a (small) behavior-adjacent change —
  keep it out of the util-extraction PRs.
- Not investigated for correctness: whether the three label-fetch sites (Phase
  4) are truly semantically equivalent. That equivalence proof is Phase 4's
  gate, not a claim this audit makes.
