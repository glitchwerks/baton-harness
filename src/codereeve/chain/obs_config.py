"""Observability configuration for the baton-harness daemon.

Reads ``CODEREEVE_*`` environment variables and constructs an ``ObsConfig``
frozen dataclass.  This module reads ``CODEREEVE_*`` env vars directly and does
NOT touch the vendored ``WorkflowConfig``
(``src/codereeve/vendor/symphony/config.py``) — putting obs config
there would be clobbered on re-vendor.

Environment variables
---------------------
CODEREEVE_PROJECT_ROOT : str, optional
    Absolute path to the project root.  Used to derive default values
    for ``CODEREEVE_RUNLOG_PATH`` and ``CODEREEVE_HEARTBEAT_FILE`` when those
    variables are unset.  When unset AND the corresponding path
    variable is also unset, the path defaults are CWD-relative (e.g.
    ``Path(".codereeve/runlog.jsonl")``).

CODEREEVE_RUNLOG_PATH : str, optional
    Absolute path for the JSONL run-record log file.
    Default: ``${CODEREEVE_PROJECT_ROOT}/.codereeve/runlog.jsonl``
    (or CWD-relative ``.codereeve/runlog.jsonl`` when
    ``CODEREEVE_PROJECT_ROOT`` is unset).

CODEREEVE_HEARTBEAT_FILE : str, optional
    Absolute path for the heartbeat file.
    Default: ``${CODEREEVE_PROJECT_ROOT}/.codereeve/heartbeat``
    (or CWD-relative ``.codereeve/heartbeat`` when
    ``CODEREEVE_PROJECT_ROOT`` is unset).

CODEREEVE_REDISPATCH_WINDOW_TICKS : int, optional
    Number of poll ticks that form the re-dispatch eligibility window.
    Default: ``10``.

CODEREEVE_REDISPATCH_MAX : int, optional
    Maximum number of re-dispatches allowed per issue within the window.
    Default: ``3``.

CODEREEVE_HEARTBEAT_STALL_S : float, optional
    Seconds after which the absence of a heartbeat update is treated as
    a stall condition.  Default: ``7200.0`` (two hours).

CODEREEVE_HEARTBEAT_PING_URL : str, optional
    URL to ping on each heartbeat write (e.g. an uptime-monitor
    webhook).  Default: ``None`` (pinging disabled).

CODEREEVE_REDISPATCH_COUNTS_PATH : str, optional
    Absolute path for the durable re-dispatch tally JSON file.
    Default: ``${CODEREEVE_PROJECT_ROOT}/.codereeve/dispatch-counts.json``
    (or CWD-relative ``.codereeve/dispatch-counts.json`` when
    ``CODEREEVE_PROJECT_ROOT`` is unset).

CODEREEVE_MAX_ISSUE_FAILURES : int, optional
    Maximum number of consecutive charged failures allowed per issue.
    Default: ``2``.

CODEREEVE_FAILURE_COUNTS_PATH : str, optional
    Absolute path for the durable issue-failure tally JSON file.
    Default: ``${CODEREEVE_PROJECT_ROOT}/.codereeve/failure-counts.json``
    (or CWD-relative ``.codereeve/failure-counts.json`` when
    ``CODEREEVE_PROJECT_ROOT`` is unset).

CODEREEVE_WORKTREE_GC : str, optional
    Worktree orphan-GC mode.  Accepted values: ``detect`` (default),
    ``reclaim``.  ``detect`` logs orphans but never removes them (safe
    default, IS-5 detect-first).  ``reclaim`` additionally calls
    ``cleanup_worktree`` for confirmed orphans.  Any unrecognised value
    logs a WARNING and falls back to ``detect``.

CODEREEVE_WORKER_PROGRESS_STALL_S : float, optional
    Seconds without a turn-progress signal during the worker-active
    phase before a progress-stall alert is fired.  Default: ``1800.0``
    (6× the 300 s per-turn timeout at ``config.py:L31``; see
    ``max_retry_backoff_ms``).  Any non-numeric value logs a WARNING
    and falls back to the default (never-raise contract).
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import Literal

from codereeve.config_env import runtime_environment
from codereeve.paths import runtime_state_directory

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_RUNLOG_NAME = "runlog.jsonl"
_DEFAULT_HEARTBEAT_NAME = "heartbeat"
_DEFAULT_DISPATCH_COUNTS_NAME = "dispatch-counts.json"
_DEFAULT_FAILURE_COUNTS_NAME = "failure-counts.json"
_DEFAULT_REDISPATCH_WINDOW_TICKS = 10
_DEFAULT_REDISPATCH_MAX = 3
_DEFAULT_MAX_ISSUE_FAILURES = 2
_DEFAULT_HEARTBEAT_STALL_S = 7200.0
_DEFAULT_WORKTREE_GC: Literal["detect", "reclaim"] = "detect"
_VALID_WORKTREE_GC = frozenset({"detect", "reclaim"})
# 1800 s = 6× the 300 s per-turn timeout (max_retry_backoff_ms / config.py:L31)
_DEFAULT_WORKER_PROGRESS_STALL_S = 1800.0


# ---------------------------------------------------------------------------
# ObsConfig dataclass
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ObsConfig:
    """Frozen observability configuration for the daemon.

    All fields are set once at load time from environment variables and
    are immutable thereafter.

    Attributes:
        runlog_path: Path to the JSONL run-record log file.
        heartbeat_file: Path to the heartbeat file updated each tick.
        redispatch_window_ticks: Tick window for re-dispatch eligibility.
        redispatch_max: Max re-dispatches per issue in the window.
        heartbeat_stall_s: Seconds without a heartbeat before stall is
            declared.
        heartbeat_ping_url: Optional URL pinged on each heartbeat write.
        redispatch_counts_path: Path to the durable re-dispatch tally
            JSON file used for loop detection.
        max_issue_failures: Maximum consecutive charged failures per issue.
        failure_counts_path: Path to the durable issue-failure tally JSON
            file.
        worktree_gc: Worktree orphan-GC mode.  ``"detect"`` (default)
            logs orphans only; ``"reclaim"`` enables opt-in cleanup.
        worker_progress_stall_s: Seconds without a turn-progress signal
            (worker-active phase only) before a progress-stall alert
            fires.  Default ``1800.0`` s (6× the 300 s per-turn timeout
            at ``config.py:L31``; see ``max_retry_backoff_ms``).
    """

    runlog_path: Path
    heartbeat_file: Path
    redispatch_window_ticks: int
    redispatch_max: int
    heartbeat_stall_s: float
    heartbeat_ping_url: str | None
    redispatch_counts_path: Path
    worktree_gc: Literal["detect", "reclaim"] = "detect"
    worker_progress_stall_s: float = 1800.0
    max_issue_failures: int = _DEFAULT_MAX_ISSUE_FAILURES
    failure_counts_path: Path = dataclasses.field(
        default_factory=lambda: (
            runtime_state_directory(Path("."), {})
            / _DEFAULT_FAILURE_COUNTS_NAME
        )
    )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def load_obs_config() -> ObsConfig:
    """Load observability configuration from environment variables.

    Reads ``CODEREEVE_*`` environment variables and returns an ``ObsConfig``
    instance populated with resolved values. Conflicting aliases or state
    directories raise before defaults are consumed. When
    ``CODEREEVE_PROJECT_ROOT`` is unset and no path-specific override is
    provided, path fields fall back to CWD-relative defaults.

    An explicitly-set path variable always wins over the
    ``CODEREEVE_PROJECT_ROOT``-derived default.

    Returns:
        A fully-populated ``ObsConfig`` instance.
    """
    values = runtime_environment().values
    state = runtime_state_directory(
        Path(values.get("CODEREEVE_PROJECT_ROOT", ".")), values
    )
    _default_runlog = state / _DEFAULT_RUNLOG_NAME
    _default_heartbeat = state / _DEFAULT_HEARTBEAT_NAME
    _default_dispatch_counts = state / _DEFAULT_DISPATCH_COUNTS_NAME
    _default_failure_counts = state / _DEFAULT_FAILURE_COUNTS_NAME

    # Explicit path overrides always win over derived defaults.
    runlog_raw = values.get("CODEREEVE_RUNLOG_PATH")
    runlog_path = (
        Path(runlog_raw) if runlog_raw is not None else _default_runlog
    )

    heartbeat_raw = values.get("CODEREEVE_HEARTBEAT_FILE")
    heartbeat_file = (
        Path(heartbeat_raw)
        if heartbeat_raw is not None
        else _default_heartbeat
    )

    # Numeric fields: parse from env or use defaults.
    # Each parse is guarded: a non-numeric value logs a WARNING and falls
    # back to the documented default so this function NEVER raises.
    _rdw_raw = values.get("CODEREEVE_REDISPATCH_WINDOW_TICKS")
    if _rdw_raw is not None:
        try:
            redispatch_window_ticks = int(_rdw_raw)
        except ValueError:
            _log.warning(
                "load_obs_config: CODEREEVE_REDISPATCH_WINDOW_TICKS is not a "
                "valid integer; using default %d",
                _DEFAULT_REDISPATCH_WINDOW_TICKS,
            )
            redispatch_window_ticks = _DEFAULT_REDISPATCH_WINDOW_TICKS
    else:
        redispatch_window_ticks = _DEFAULT_REDISPATCH_WINDOW_TICKS

    _rdm_raw = values.get("CODEREEVE_REDISPATCH_MAX")
    if _rdm_raw is not None:
        try:
            redispatch_max = int(_rdm_raw)
        except ValueError:
            _log.warning(
                "load_obs_config: CODEREEVE_REDISPATCH_MAX is not a valid "
                "integer; using default %d",
                _DEFAULT_REDISPATCH_MAX,
            )
            redispatch_max = _DEFAULT_REDISPATCH_MAX
    else:
        redispatch_max = _DEFAULT_REDISPATCH_MAX

    _mif_raw = values.get("CODEREEVE_MAX_ISSUE_FAILURES")
    if _mif_raw is not None:
        try:
            max_issue_failures = int(_mif_raw)
        except ValueError:
            _log.warning(
                "load_obs_config: CODEREEVE_MAX_ISSUE_FAILURES is not a valid "
                "integer; using default %d",
                _DEFAULT_MAX_ISSUE_FAILURES,
            )
            max_issue_failures = _DEFAULT_MAX_ISSUE_FAILURES
    else:
        max_issue_failures = _DEFAULT_MAX_ISSUE_FAILURES

    _hbs_raw = values.get("CODEREEVE_HEARTBEAT_STALL_S")
    if _hbs_raw is not None:
        try:
            heartbeat_stall_s = float(_hbs_raw)
        except ValueError:
            _log.warning(
                "load_obs_config: CODEREEVE_HEARTBEAT_STALL_S is not a valid "
                "float; using default %.1f",
                _DEFAULT_HEARTBEAT_STALL_S,
            )
            heartbeat_stall_s = _DEFAULT_HEARTBEAT_STALL_S
    else:
        heartbeat_stall_s = _DEFAULT_HEARTBEAT_STALL_S

    # Optional string field.
    heartbeat_ping_url = values.get("CODEREEVE_HEARTBEAT_PING_URL") or None

    # Durable re-dispatch tally path (env override wins; else derived).
    _rdc_raw = values.get("CODEREEVE_REDISPATCH_COUNTS_PATH")
    redispatch_counts_path = (
        Path(_rdc_raw) if _rdc_raw is not None else _default_dispatch_counts
    )

    # Durable issue-failure tally path (env override wins; else derived).
    _fc_raw = values.get("CODEREEVE_FAILURE_COUNTS_PATH")
    failure_counts_path = (
        Path(_fc_raw) if _fc_raw is not None else _default_failure_counts
    )

    # Worktree orphan-GC mode (detect | reclaim).  Unrecognised values log
    # a WARNING and fall back to "detect" (consistent with the never-raise
    # contract and the guarded-parse pattern used for numeric fields above).
    _wgc_raw = values.get("CODEREEVE_WORKTREE_GC")
    if _wgc_raw is not None:
        if _wgc_raw in _VALID_WORKTREE_GC:
            worktree_gc: Literal["detect", "reclaim"] = _wgc_raw  # type: ignore[assignment]
        else:
            _log.warning(
                "load_obs_config: CODEREEVE_WORKTREE_GC is not a valid value"
                " (expected 'detect' or 'reclaim'); using default 'detect'",
            )
            worktree_gc = _DEFAULT_WORKTREE_GC
    else:
        worktree_gc = _DEFAULT_WORKTREE_GC

    # Worker-active threshold: CODEREEVE_WORKER_PROGRESS_STALL_S.
    # Default 1800.0 s = 6× the 300 s per-turn timeout (max_retry_backoff_ms /
    # config.py:L31).  Guarded parse: never raises; malformed value → WARNING.
    _wps_raw = values.get("CODEREEVE_WORKER_PROGRESS_STALL_S")
    if _wps_raw is not None:
        try:
            worker_progress_stall_s = float(_wps_raw)
        except ValueError:
            _log.warning(
                "load_obs_config: CODEREEVE_WORKER_PROGRESS_STALL_S is not a"
                " valid float; using default %.1f",
                _DEFAULT_WORKER_PROGRESS_STALL_S,
            )
            worker_progress_stall_s = _DEFAULT_WORKER_PROGRESS_STALL_S
    else:
        worker_progress_stall_s = _DEFAULT_WORKER_PROGRESS_STALL_S

    return ObsConfig(
        runlog_path=runlog_path,
        heartbeat_file=heartbeat_file,
        redispatch_window_ticks=redispatch_window_ticks,
        redispatch_max=redispatch_max,
        heartbeat_stall_s=heartbeat_stall_s,
        heartbeat_ping_url=heartbeat_ping_url,
        redispatch_counts_path=redispatch_counts_path,
        max_issue_failures=max_issue_failures,
        failure_counts_path=failure_counts_path,
        worktree_gc=worktree_gc,
        worker_progress_stall_s=worker_progress_stall_s,
    )
