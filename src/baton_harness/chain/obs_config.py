"""Observability configuration for the baton-harness daemon.

Reads ``BH_*`` environment variables and constructs an ``ObsConfig``
frozen dataclass.  This module reads ``BH_*`` env vars directly and does
NOT touch the vendored ``WorkflowConfig``
(``src/baton_harness/vendor/symphony/config.py``) — putting obs config
there would be clobbered on re-vendor.

Environment variables
---------------------
BH_PROJECT_ROOT : str, optional
    Absolute path to the project root.  Used to derive default values
    for ``BH_RUNLOG_PATH`` and ``BH_HEARTBEAT_FILE`` when those
    variables are unset.  When unset AND the corresponding path
    variable is also unset, the path defaults are CWD-relative (e.g.
    ``Path(".baton-harness/runlog.jsonl")``).

BH_RUNLOG_PATH : str, optional
    Absolute path for the JSONL run-record log file.
    Default: ``${BH_PROJECT_ROOT}/.baton-harness/runlog.jsonl``
    (or CWD-relative ``.baton-harness/runlog.jsonl`` when
    ``BH_PROJECT_ROOT`` is unset).

BH_HEARTBEAT_FILE : str, optional
    Absolute path for the heartbeat file.
    Default: ``${BH_PROJECT_ROOT}/.baton-harness/heartbeat``
    (or CWD-relative ``.baton-harness/heartbeat`` when
    ``BH_PROJECT_ROOT`` is unset).

BH_REDISPATCH_WINDOW_TICKS : int, optional
    Number of poll ticks that form the re-dispatch eligibility window.
    Default: ``10``.

BH_REDISPATCH_MAX : int, optional
    Maximum number of re-dispatches allowed per issue within the window.
    Default: ``3``.

BH_HEARTBEAT_STALL_S : float, optional
    Seconds after which the absence of a heartbeat update is treated as
    a stall condition.  Default: ``7200.0`` (two hours).

BH_HEARTBEAT_PING_URL : str, optional
    URL to ping on each heartbeat write (e.g. an uptime-monitor
    webhook).  Default: ``None`` (pinging disabled).

BH_REDISPATCH_COUNTS_PATH : str, optional
    Absolute path for the durable re-dispatch tally JSON file.
    Default: ``${BH_PROJECT_ROOT}/.baton-harness/dispatch-counts.json``
    (or CWD-relative ``.baton-harness/dispatch-counts.json`` when
    ``BH_PROJECT_ROOT`` is unset).

BH_WORKTREE_GC : str, optional
    Worktree orphan-GC mode.  Accepted values: ``detect`` (default),
    ``reclaim``.  ``detect`` logs orphans but never removes them (safe
    default, IS-5 detect-first).  ``reclaim`` additionally calls
    ``cleanup_worktree`` for confirmed orphans.  Any unrecognised value
    logs a WARNING and falls back to ``detect``.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from pathlib import Path
from typing import Literal

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BH_HARNESS_DIR = ".baton-harness"
_DEFAULT_RUNLOG_NAME = "runlog.jsonl"
_DEFAULT_HEARTBEAT_NAME = "heartbeat"
_DEFAULT_DISPATCH_COUNTS_NAME = "dispatch-counts.json"
_DEFAULT_REDISPATCH_WINDOW_TICKS = 10
_DEFAULT_REDISPATCH_MAX = 3
_DEFAULT_HEARTBEAT_STALL_S = 7200.0
_DEFAULT_WORKTREE_GC: Literal["detect", "reclaim"] = "detect"
_VALID_WORKTREE_GC = frozenset({"detect", "reclaim"})


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
        worktree_gc: Worktree orphan-GC mode.  ``"detect"`` (default)
            logs orphans only; ``"reclaim"`` enables opt-in cleanup.
    """

    runlog_path: Path
    heartbeat_file: Path
    redispatch_window_ticks: int
    redispatch_max: int
    heartbeat_stall_s: float
    heartbeat_ping_url: str | None
    redispatch_counts_path: Path
    worktree_gc: Literal["detect", "reclaim"] = "detect"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def load_obs_config() -> ObsConfig:
    """Load observability configuration from environment variables.

    Reads ``BH_*`` environment variables and returns an ``ObsConfig``
    instance populated with resolved values.  This function NEVER raises
    — when ``BH_PROJECT_ROOT`` is unset and no path-specific override is
    provided, path fields fall back to CWD-relative defaults.

    An explicitly-set path variable always wins over the
    ``BH_PROJECT_ROOT``-derived default.

    Returns:
        A fully-populated ``ObsConfig`` instance.
    """
    project_root_raw = os.environ.get("BH_PROJECT_ROOT")

    # Derive CWD-relative or project-root-relative defaults.
    if project_root_raw is not None:
        _root = Path(project_root_raw)
        _default_runlog = _root / _BH_HARNESS_DIR / _DEFAULT_RUNLOG_NAME
        _default_heartbeat = _root / _BH_HARNESS_DIR / _DEFAULT_HEARTBEAT_NAME
        _default_dispatch_counts = (
            _root / _BH_HARNESS_DIR / _DEFAULT_DISPATCH_COUNTS_NAME
        )
    else:
        _default_runlog = Path(_BH_HARNESS_DIR) / _DEFAULT_RUNLOG_NAME
        _default_heartbeat = Path(_BH_HARNESS_DIR) / _DEFAULT_HEARTBEAT_NAME
        _default_dispatch_counts = (
            Path(_BH_HARNESS_DIR) / _DEFAULT_DISPATCH_COUNTS_NAME
        )

    # Explicit path overrides always win over derived defaults.
    runlog_raw = os.environ.get("BH_RUNLOG_PATH")
    runlog_path = (
        Path(runlog_raw) if runlog_raw is not None else _default_runlog
    )

    heartbeat_raw = os.environ.get("BH_HEARTBEAT_FILE")
    heartbeat_file = (
        Path(heartbeat_raw)
        if heartbeat_raw is not None
        else _default_heartbeat
    )

    # Numeric fields: parse from env or use defaults.
    # Each parse is guarded: a non-numeric value logs a WARNING and falls
    # back to the documented default so this function NEVER raises.
    _rdw_raw = os.environ.get("BH_REDISPATCH_WINDOW_TICKS")
    if _rdw_raw is not None:
        try:
            redispatch_window_ticks = int(_rdw_raw)
        except ValueError:
            _log.warning(
                "load_obs_config: BH_REDISPATCH_WINDOW_TICKS=%r is not a "
                "valid integer; using default %d",
                _rdw_raw,
                _DEFAULT_REDISPATCH_WINDOW_TICKS,
            )
            redispatch_window_ticks = _DEFAULT_REDISPATCH_WINDOW_TICKS
    else:
        redispatch_window_ticks = _DEFAULT_REDISPATCH_WINDOW_TICKS

    _rdm_raw = os.environ.get("BH_REDISPATCH_MAX")
    if _rdm_raw is not None:
        try:
            redispatch_max = int(_rdm_raw)
        except ValueError:
            _log.warning(
                "load_obs_config: BH_REDISPATCH_MAX=%r is not a valid "
                "integer; using default %d",
                _rdm_raw,
                _DEFAULT_REDISPATCH_MAX,
            )
            redispatch_max = _DEFAULT_REDISPATCH_MAX
    else:
        redispatch_max = _DEFAULT_REDISPATCH_MAX

    _hbs_raw = os.environ.get("BH_HEARTBEAT_STALL_S")
    if _hbs_raw is not None:
        try:
            heartbeat_stall_s = float(_hbs_raw)
        except ValueError:
            _log.warning(
                "load_obs_config: BH_HEARTBEAT_STALL_S=%r is not a valid "
                "float; using default %.1f",
                _hbs_raw,
                _DEFAULT_HEARTBEAT_STALL_S,
            )
            heartbeat_stall_s = _DEFAULT_HEARTBEAT_STALL_S
    else:
        heartbeat_stall_s = _DEFAULT_HEARTBEAT_STALL_S

    # Optional string field.
    heartbeat_ping_url = os.environ.get("BH_HEARTBEAT_PING_URL") or None

    # Durable re-dispatch tally path (env override wins; else derived).
    _rdc_raw = os.environ.get("BH_REDISPATCH_COUNTS_PATH")
    redispatch_counts_path = (
        Path(_rdc_raw) if _rdc_raw is not None else _default_dispatch_counts
    )

    # Worktree orphan-GC mode (detect | reclaim).  Unrecognised values log
    # a WARNING and fall back to "detect" (consistent with the never-raise
    # contract and the guarded-parse pattern used for numeric fields above).
    _wgc_raw = os.environ.get("BH_WORKTREE_GC")
    if _wgc_raw is not None:
        if _wgc_raw in _VALID_WORKTREE_GC:
            worktree_gc: Literal["detect", "reclaim"] = _wgc_raw  # type: ignore[assignment]
        else:
            _log.warning(
                "load_obs_config: BH_WORKTREE_GC=%r is not a valid value"
                " (expected 'detect' or 'reclaim'); using default 'detect'",
                _wgc_raw,
            )
            worktree_gc = _DEFAULT_WORKTREE_GC
    else:
        worktree_gc = _DEFAULT_WORKTREE_GC

    return ObsConfig(
        runlog_path=runlog_path,
        heartbeat_file=heartbeat_file,
        redispatch_window_ticks=redispatch_window_ticks,
        redispatch_max=redispatch_max,
        heartbeat_stall_s=heartbeat_stall_s,
        heartbeat_ping_url=heartbeat_ping_url,
        redispatch_counts_path=redispatch_counts_path,
        worktree_gc=worktree_gc,
    )
