# Merge-Denial Probe — Operator Runbook

**Slice:** 3c — issue #160  
**Purpose:** Demonstrate that the `harness-main-no-merge` ruleset (provisioned in
slice 3b, PR #158) actually blocks a worker-identity token from merging any PR,
regardless of which bypass vector the worker uses.

---

## Overview

The probe (`bin/probe-merge-denial.sh`) attempts seven known bypass vectors
against a live sandbox PR and asserts each is DENIED:

| Vec | Description | Expected denial source |
|-----|-------------|------------------------|
| 1   | `gh pr merge <N>` | hook (exit 2) + sentinel |
| 2   | `gh api -X PUT repos/…/pulls/<N>/merge` | hook + ruleset 403 |
| 3   | `gh api repos/…/pulls/<N>/merge -X PUT` | hook + ruleset 403 |
| 4   | `gh api --method=PUT repos/…/pulls/<N>/merge` | hook + ruleset 403 |
| 5   | `curl -X PUT … /pulls/<N>/merge` | ruleset 403 (no hook coverage) |
| 6   | `curl --request=PUT … /pulls/<N>/merge` | ruleset 403 (no hook coverage) |
| 7   | `python -c "urllib.request PUT /pulls/<N>/merge"` | ruleset 403 (no hook) |

Vectors 1–4 go through the `force-pr-not-merge` PreToolUse hook AND the
GitHub Repository Ruleset (defense-in-depth). Vectors 5–7 bypass the hook
entirely (raw HTTP, not `gh`/Bash) but must be blocked by the ruleset alone.

---

## Prerequisites

### 1. Sandbox repo

Create a dedicated sandbox repo (e.g. `yourorg/harness-sandbox`) with:

- A `main` branch
- Branch protection configured by running `bin/provision-ruleset.sh`
  (see [README § sandbox-repo setup](../README.md#sandbox-repo-setup))

The ruleset named `harness-main-no-merge` must be active on `main`.

### 2. Worker-identity token

The probe token must be a **fine-grained PAT** scoped to the sandbox repo
with `Contents: Read` and `Pull requests: Read/Write` but **without** merge
bypass in the ruleset. The worker identity must NOT be in `bypass_actors` for
the `harness-main-no-merge` ruleset.

Store the token in a file (NEVER as an env-var value):

```bash
echo "github_pat_<...>" > /path/to/worker-token.txt
chmod 600 /path/to/worker-token.txt
```

### 3. Open PR against main

Create an open (non-draft) PR in the sandbox repo targeting `main`. Note its
PR number.

### 4. Hook binary (for vectors 1–4)

The `bh-force-pr-not-merge` entry point must be installed in the harness venv:

```bash
uv pip install -e ".[dev]"
# Verify:
.venv/Scripts/bh-force-pr-not-merge.exe --help   # Windows
.venv/bin/bh-force-pr-not-merge --help            # Linux/macOS
```

---

## Environment variable setup

```bash
export BH_PROBE_SANDBOX_REPO="yourorg/harness-sandbox"
export BH_PROBE_PR_NUMBER="42"
export BH_PROBE_WORKER_TOKEN_PATH="/path/to/worker-token.txt"
```

Optional:

```bash
# Print commands without running them (useful for validating arg construction)
export BH_PROBE_DRY_RUN=1

# Override hook script path (auto-detected from harness venv by default)
export BH_PROBE_HOOK_SCRIPT="/custom/path/bh-force-pr-not-merge"
```

---

## Dry-run example

Validate argument construction without touching the live API:

```bash
export BH_PROBE_SANDBOX_REPO="yourorg/harness-sandbox"
export BH_PROBE_PR_NUMBER="42"
export BH_PROBE_WORKER_TOKEN_PATH="/path/to/worker-token.txt"
export BH_PROBE_DRY_RUN=1

bash bin/probe-merge-denial.sh
```

Expected output snippet:

```
============================================================
  baton-harness merge-denial probe  (slice 3c, #160)
============================================================
  Sandbox repo : yourorg/harness-sandbox
  PR number    : 42
  Token path   : /path/to/worker-token.txt (len=93)
  Dry-run      : 1
  API URL      : https://api.github.com/repos/yourorg/harness-sandbox/pulls/42/merge
============================================================

=== Vector 1: gh pr merge (obvious path) ===
  [DRY-RUN] hook payload: {"tool_name":"Bash","tool_input":{"command":"gh pr merge 42"}}
  PASS — hook exit=2, sentinel present, marker in stderr

... (7 vectors) ...

  RESULT: PASS — 7/7 vectors denied as expected
```

---

## Full-run example

Against a live sandbox repo:

```bash
export BH_PROBE_SANDBOX_REPO="yourorg/harness-sandbox"
export BH_PROBE_PR_NUMBER="42"
export BH_PROBE_WORKER_TOKEN_PATH="/path/to/worker-token.txt"

bash bin/probe-merge-denial.sh
```

### Interpreting the output

Each vector prints either:

```
  PASS — hook exit=2, sentinel present, marker in stderr
```

or

```
  FAIL — exit_ok=False http_ok=False
```

The final summary table shows all 7 vectors and exits 0 (all pass) or 1 (any
fail).

A PASS means the boundary is confirmed. A FAIL means something unexpected
succeeded and requires investigation (see Troubleshooting below).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `missing required env vars` | Forgot to export one or more env vars | Export all three: `BH_PROBE_SANDBOX_REPO`, `BH_PROBE_PR_NUMBER`, `BH_PROBE_WORKER_TOKEN_PATH` |
| `token file not found` | Wrong path in `BH_PROBE_WORKER_TOKEN_PATH` | Verify file exists: `ls -la "$BH_PROBE_WORKER_TOKEN_PATH"` |
| Vector 1–4 hook exit=0 (no sentinel) | `bh-force-pr-not-merge` not installed or wrong path | Run `uv pip install -e ".[dev]"` in the harness root; check `BH_PROBE_HOOK_SCRIPT` |
| Vectors 5–7 return 200 / no 403 | Ruleset not provisioned or token is in bypass_actors | Run `bin/provision-ruleset.sh`; verify token identity is not an App actor |
| `403` but no `harness-main-no-merge` in body | Different ruleset blocking the merge | GitHub's 403 body varies — may need to add the ruleset name check for your repo's actual response body |
| Vector 5–7 FAIL even with ruleset active | Token still has merge permission via App bypass | Check `bypass_actors` in the ruleset config; the worker PAT must not be in the bypass list |
| `bh-force-pr-not-merge.exe not found` on Windows | Entry point not compiled / PATH issue | Use `BH_PROBE_HOOK_SCRIPT=".venv/Scripts/bh-force-pr-not-merge.exe"` explicitly |

---

## CI integration note

Running this probe in CI is **deferred** — it requires:

1. A sandbox repo pre-provisioned with the `harness-main-no-merge` ruleset.
2. A worker-identity token stored as an Actions secret (secret-management
   in CI is a separate operational concern).
3. An open PR in the sandbox repo at the time the probe runs.

These preconditions are suitable for a release-validation workflow but not
for per-PR CI. A future enhancement tracked under
[glitchwerks/baton-harness #249](https://github.com/glitchwerks/baton-harness/issues/249)
covers wiring this probe into release-validation once a sandbox-credential
management strategy is established for the Actions runner. (Previously
tracked under #133, which is closed and did not cover this residual; #249
supersedes it.)

---

## Files

| Path | Description |
|------|-------------|
| `bin/probe-merge-denial.sh` | Main probe script |
| `scripts/probe_assert.py` | Python assertion helper (called by probe) |
| `tests/test_probe_merge_denial.py` | Unit tests for assertion logic |
| `docs/probe-merge-denial.md` | This runbook |

---

## Related

- `bin/provision-ruleset.sh` — idempotent ruleset provisioner (slice 3b)
- `src/baton_harness/hooks/force_pr_not_merge.py` — the hook the probe tests
- `docs/architecture-spec.md` §3.5 — defense-in-depth layer description
- Issue [#160](https://github.com/glitchwerks/baton-harness/issues/160) — slice 3c spec
- Issue [#133](https://github.com/glitchwerks/baton-harness/issues/133) — parent feature
