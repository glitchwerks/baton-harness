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
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BH_HARNESS_DIR = ".baton-harness"
_DEFAULT_RUNLOG_NAME = "runlog.jsonl"
_DEFAULT_HEARTBEAT_NAME = "heartbeat"
_DEFAULT_REDISPATCH_WINDOW_TICKS = 10
_DEFAULT_REDISPATCH_MAX = 3
_DEFAULT_HEARTBEAT_STALL_S = 7200.0


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
    """

    runlog_path: Path
    heartbeat_file: Path
    redispatch_window_ticks: int
    redispatch_max: int
    heartbeat_stall_s: float
    heartbeat_ping_url: str | None


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
        _default_runlog = (
            Path(project_root_raw) / _BH_HARNESS_DIR / _DEFAULT_RUNLOG_NAME
        )
        _default_heartbeat = (
            Path(project_root_raw) / _BH_HARNESS_DIR / _DEFAULT_HEARTBEAT_NAME
        )
    else:
        _default_runlog = Path(_BH_HARNESS_DIR) / _DEFAULT_RUNLOG_NAME
        _default_heartbeat = Path(_BH_HARNESS_DIR) / _DEFAULT_HEARTBEAT_NAME

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
    redispatch_window_ticks = int(
        os.environ.get(
            "BH_REDISPATCH_WINDOW_TICKS",
            str(_DEFAULT_REDISPATCH_WINDOW_TICKS),
        )
    )
    redispatch_max = int(
        os.environ.get("BH_REDISPATCH_MAX", str(_DEFAULT_REDISPATCH_MAX))
    )
    heartbeat_stall_s = float(
        os.environ.get("BH_HEARTBEAT_STALL_S", str(_DEFAULT_HEARTBEAT_STALL_S))
    )

    # Optional string field.
    heartbeat_ping_url = os.environ.get("BH_HEARTBEAT_PING_URL") or None

    return ObsConfig(
        runlog_path=runlog_path,
        heartbeat_file=heartbeat_file,
        redispatch_window_ticks=redispatch_window_ticks,
        redispatch_max=redispatch_max,
        heartbeat_stall_s=heartbeat_stall_s,
        heartbeat_ping_url=heartbeat_ping_url,
    )
