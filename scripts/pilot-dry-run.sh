#!/usr/bin/env bash
# pilot-dry-run.sh — baton-harness pilot validation (tracks baton-harness#6)
#
# Approach A handoff: this script does the FULL bootstrap + preflight, then
# guides the three measurements in issue #6 against cbeaulieu-gt/promptsmith:
#   T1  — absolute -w path works
#   SA  — Scenario A: one clean issue (promptsmith#2) runs end-to-end to a draft PR
#   T2  — block cost: ambiguous issue (promptsmith#18) blocks; does retry burn max_turns?
#
# RUN MODEL — you need TWO terminals on the Linux host:
#   Terminal 1: runs `baton` in the foreground (it's a long-running poller).
#   Terminal 2: applies `agent-ready` labels and snapshots state on cue.
#
# Logs are captured under $WORKDIR/logs/ for hand-off back to Claude, which
# will interpret them and write the findings note.
#
# Usage:
#   ./pilot-dry-run.sh bootstrap     # clone + install + preflight (idempotent)
#   ./pilot-dry-run.sh run           # Terminal 1: start baton (T1 happens here), logs to file
#   ./pilot-dry-run.sh label-sa      # Terminal 2: label promptsmith#2 agent-ready (Scenario A)
#   ./pilot-dry-run.sh label-t2      # Terminal 2: label promptsmith#18 agent-ready (T2)
#   ./pilot-dry-run.sh snapshot      # Terminal 2: capture issue/PR state into logs/
#   ./pilot-dry-run.sh turns         # count agent invocations in the baton log (T2 measure)
#
# Override defaults with env vars, e.g.:
#   WORKDIR=$HOME/pilot ./pilot-dry-run.sh bootstrap

set -euo pipefail

# --------------------------------------------------------------------------
# Config — override via env
# --------------------------------------------------------------------------
WORKDIR="${WORKDIR:-$HOME/baton-pilot}"
HARNESS_REPO="${HARNESS_REPO:-glitchwerks/baton-harness}"   # private
PROJECT_REPO="${PROJECT_REPO:-cbeaulieu-gt/promptsmith}"
BATON_REPO="${BATON_REPO:-mraza007/baton}"

HARNESS_DIR="$WORKDIR/baton-harness"
PROJECT_DIR="$WORKDIR/promptsmith"
BATON_DIR="$WORKDIR/baton"
LOG_DIR="$WORKDIR/logs"
BATON_LOG="$LOG_DIR/baton.log"
VENV="$WORKDIR/.venv"   # baton + bh-* installed here; auto-activated below

SA_ISSUE=2     # promptsmith#2  — Scenario A (clean: Project scaffolding)
T2_ISSUE=18    # promptsmith#18 — T2 (ambiguous: render formatting)

say()  { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ok:\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# Activate the pilot venv if it exists, so baton + bh-* + their hook
# subprocesses all resolve from $VENV/bin. Load-bearing for hook firing.
activate_venv() {
  if [[ -f "$VENV/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "$VENV/bin/activate"
  fi
}

clone_or_update() {
  local slug="$1" dir="$2"
  if [[ -d "$dir/.git" ]]; then
    ok "exists: $dir (fetching)"
    git -C "$dir" fetch --quiet origin || warn "fetch failed for $dir"
  else
    say "cloning $slug -> $dir"
    gh repo clone "$slug" "$dir" || die "clone failed: $slug (private repo? gh auth?)"
  fi
}

# --------------------------------------------------------------------------
# bootstrap
# --------------------------------------------------------------------------
bootstrap() {
  say "Preflight"
  command -v git    >/dev/null || die "git not found"
  command -v gh     >/dev/null || die "gh not found"
  command -v python3>/dev/null || die "python3 not found"
  command -v claude >/dev/null || die "claude CLI not found (install + log in interactively first)"
  python3 -c 'import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)' \
    || die "python >= 3.11 required"
  gh auth status >/dev/null 2>&1 || die "gh not authenticated (run: gh auth login)"
  ok "git / gh / python3.11+ / claude present; gh authenticated"
  # claude login is interactive-only; we can't assert it headlessly. Flag it.
  warn "Confirm 'claude' is logged in (run 'claude' once interactively if unsure) — cannot verify headlessly."

  mkdir -p "$WORKDIR" "$LOG_DIR"

  say "Creating dedicated venv at $VENV"
  if [[ ! -d "$VENV" ]]; then
    if command -v uv >/dev/null; then uv venv "$VENV"; else python3 -m venv "$VENV"; fi
  fi
  activate_venv
  [[ -n "${VIRTUAL_ENV:-}" ]] || die "venv activation failed ($VENV)"
  ok "venv active: $VIRTUAL_ENV"
  # Inside an active venv, 'uv pip' and plain 'pip' both target it.
  local PIP="pip"; command -v uv >/dev/null && PIP="uv pip"

  clone_or_update "$BATON_REPO"   "$BATON_DIR"
  clone_or_update "$HARNESS_REPO" "$HARNESS_DIR"
  clone_or_update "$PROJECT_REPO" "$PROJECT_DIR"

  say "Installing Baton (editable) into venv"
  ( cd "$BATON_DIR" && $PIP install -e . ) || die "baton install failed"
  command -v baton >/dev/null || die "baton not on PATH after install"
  ok "baton: $(command -v baton)"

  say "Installing baton-harness (editable) — provides bh-* console scripts"
  ( cd "$HARNESS_DIR" && $PIP install -e . ) || die "harness install failed"
  for s in bh-after-create bh-before-run bh-after-run; do
    command -v "$s" >/dev/null || die "$s not on PATH (harness install incomplete)"
  done
  ok "bh-after-create / bh-before-run / bh-after-run on PATH"

  say "Bootstrap complete"
  cat <<EOF

Next:
  Terminal 1:  $0 run          # starts baton (T1 is validated the moment it launches)
  Terminal 2:  $0 label-sa     # once baton is polling, kick off Scenario A
               $0 snapshot     # after the PR opens, capture state
               $0 label-t2     # then kick off the T2 ambiguous run
               $0 turns        # measure whether retry burned max_turns

Repos under: $WORKDIR
Logs under:  $LOG_DIR
Venv:        $VENV  (each subcommand auto-activates it; no manual 'source' needed)
EOF
}

# --------------------------------------------------------------------------
# run  (Terminal 1) — T1 is exercised here
# --------------------------------------------------------------------------
run() {
  activate_venv
  # NOTE: bin/run.sh was deleted in P3 (replaced by bin/run-daemon.sh).
  # This 'run' subcommand targeted the retired external-baton / baton-start
  # invocation path (T1/T2 in the original pilot plan).  The new daemon no
  # longer accepts a project-path positional argument; it reads repo
  # coordinates from BH_REPO_OWNER / BH_REPO_NAME / BH_PROJECT_ROOT env vars.
  # Use bin/run-daemon.sh directly instead.
  die "The 'run' subcommand is retired (bin/run.sh was deleted in P3). " \
      "Set BH_REPO_OWNER, BH_REPO_NAME, BH_PROJECT_ROOT and run: " \
      "$HARNESS_DIR/bin/run-daemon.sh"
}

# --------------------------------------------------------------------------
# label helpers (Terminal 2)
# --------------------------------------------------------------------------
label_sa() {
  say "Scenario A — labelling promptsmith#$SA_ISSUE agent-ready"
  gh issue edit "$SA_ISSUE" -R "$PROJECT_REPO" --add-label agent-ready
  ok "labelled. Watch Terminal 1: expect worktree create -> rebase -> implement -> draft PR -> label reconcile."
  echo "  When the PR appears, run: $0 snapshot"
}

label_t2() {
  say "T2 — labelling promptsmith#$T2_ISSUE agent-ready"
  gh issue edit "$T2_ISSUE" -R "$PROJECT_REPO" --add-label agent-ready
  ok "labelled. Expect the agent to post a clarifying comment, add 'blocked', and STOP."
  echo "  After it settles, run: $0 turns   and   $0 snapshot"
}

# --------------------------------------------------------------------------
# snapshot (Terminal 2) — capture observable state for Claude
# --------------------------------------------------------------------------
snapshot() {
  mkdir -p "$LOG_DIR"
  local stamp; stamp="$(date +%Y%m%d-%H%M%S)"
  local out="$LOG_DIR/snapshot-$stamp.txt"
  say "Snapshot -> $out"
  {
    echo "### issue #$SA_ISSUE (Scenario A)";  gh issue view "$SA_ISSUE" -R "$PROJECT_REPO" --json number,state,labels,comments
    echo; echo "### issue #$T2_ISSUE (T2)";    gh issue view "$T2_ISSUE" -R "$PROJECT_REPO" --json number,state,labels,comments
    echo; echo "### open PRs";                 gh pr list -R "$PROJECT_REPO" --state all --json number,title,isDraft,headRefName,state,body --limit 20
  } > "$out" 2>&1
  ok "captured. Send $out (and $BATON_LOG) back to Claude."
}

# --------------------------------------------------------------------------
# turns (Terminal 2) — T2 measurement helper
# --------------------------------------------------------------------------
turns() {
  [[ -f "$BATON_LOG" ]] || die "no baton log at $BATON_LOG"
  say "T2 — counting agent invocations / turns in $BATON_LOG"
  echo "  max_turns in WORKFLOW.md is 3. If the agent was re-invoked ~3x for #$T2_ISSUE"
  echo "  AFTER it added 'blocked', the retry burned max_turns. If it ran once and stopped,"
  echo "  exclude_labels short-circuited the retry. (Heuristic grep — eyeball the log too.)"
  echo
  echo "  lines mentioning issue #$T2_ISSUE:"
  grep -niE "issue.*#?$T2_ISSUE|#$T2_ISSUE" "$BATON_LOG" || echo "  (none — check the log manually)"
  echo
  echo "  candidate turn/invocation markers:"
  grep -niE "turn|invoc|attempt|dispatch|claude .*-p|max_turns|blocked|exclude" "$BATON_LOG" || echo "  (none matched — eyeball $BATON_LOG)"
}

# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------
case "${1:-help}" in
  bootstrap) bootstrap ;;
  run)       run ;;
  label-sa)  label_sa ;;
  label-t2)  label_t2 ;;
  snapshot)  snapshot ;;
  turns)     turns ;;
  *) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//' ;;  # print the header as help
esac
