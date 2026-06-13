"""Unit tests for baton_harness.chain.obs_config.

Tests the ``ObsConfig`` dataclass and ``load_obs_config`` factory.
All environment variable injection uses ``monkeypatch`` so tests are
hermetic and restore the original environment on teardown.

Coverage:
- Default field values are derived from ``BH_PROJECT_ROOT`` when path-
  specific variables are unset.
- Explicit overrides for every ``BH_*`` variable win over the derived
  default (int/float parsing included).
- ``load_obs_config()`` does NOT raise when ``BH_PROJECT_ROOT`` is
  unset, falling back to CWD-relative paths.
- ``ObsConfig`` is a frozen dataclass (attribute mutation raises
  ``FrozenInstanceError``).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from baton_harness.chain.obs_config import ObsConfig, load_obs_config

# ---------------------------------------------------------------------------
# Environment variable names (mirrors the contract exactly)
# ---------------------------------------------------------------------------

_BH_PROJECT_ROOT = "BH_PROJECT_ROOT"
_BH_RUNLOG_PATH = "BH_RUNLOG_PATH"
_BH_HEARTBEAT_FILE = "BH_HEARTBEAT_FILE"
_BH_REDISPATCH_WINDOW_TICKS = "BH_REDISPATCH_WINDOW_TICKS"
_BH_REDISPATCH_MAX = "BH_REDISPATCH_MAX"
_BH_HEARTBEAT_STALL_S = "BH_HEARTBEAT_STALL_S"
_BH_HEARTBEAT_PING_URL = "BH_HEARTBEAT_PING_URL"

_ALL_OBS_VARS = (
    _BH_PROJECT_ROOT,
    _BH_RUNLOG_PATH,
    _BH_HEARTBEAT_FILE,
    _BH_REDISPATCH_WINDOW_TICKS,
    _BH_REDISPATCH_MAX,
    _BH_HEARTBEAT_STALL_S,
    _BH_HEARTBEAT_PING_URL,
)


# ---------------------------------------------------------------------------
# Helper: clear all BH_* obs vars via monkeypatch
# ---------------------------------------------------------------------------


def _clear_all_obs_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all BH_* observability environment variables."""
    for var in _ALL_OBS_VARS:
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Defaults from BH_PROJECT_ROOT
# ---------------------------------------------------------------------------


def test_load_obs_config_defaults_from_project_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path defaults derived from BH_PROJECT_ROOT when others are unset."""
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_BH_PROJECT_ROOT, "/some/root")

    cfg = load_obs_config()

    assert cfg.runlog_path == Path("/some/root/.baton-harness/runlog.jsonl")
    assert cfg.heartbeat_file == Path("/some/root/.baton-harness/heartbeat")
    assert cfg.redispatch_window_ticks == 10
    assert cfg.redispatch_max == 3
    assert cfg.heartbeat_stall_s == 7200.0
    assert cfg.heartbeat_ping_url is None


def test_load_obs_config_default_int_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default redispatch fields are int, not str."""
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_BH_PROJECT_ROOT, "/some/root")

    cfg = load_obs_config()

    assert isinstance(cfg.redispatch_window_ticks, int)
    assert isinstance(cfg.redispatch_max, int)


def test_load_obs_config_default_float_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default heartbeat_stall_s is float, not str or int."""
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_BH_PROJECT_ROOT, "/some/root")

    cfg = load_obs_config()

    assert isinstance(cfg.heartbeat_stall_s, float)


# ---------------------------------------------------------------------------
# Explicit overrides win over derived defaults
# ---------------------------------------------------------------------------


def test_load_obs_config_explicit_overrides_win(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicitly set BH_* vars override all derived defaults."""
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_BH_PROJECT_ROOT, "/some/root")
    monkeypatch.setenv(_BH_RUNLOG_PATH, "/custom/path/run.jsonl")
    monkeypatch.setenv(_BH_HEARTBEAT_FILE, "/custom/path/hb")
    monkeypatch.setenv(_BH_REDISPATCH_WINDOW_TICKS, "20")
    monkeypatch.setenv(_BH_REDISPATCH_MAX, "5")
    monkeypatch.setenv(_BH_HEARTBEAT_STALL_S, "3600.5")
    monkeypatch.setenv(_BH_HEARTBEAT_PING_URL, "https://ping.example.com/")

    cfg = load_obs_config()

    assert cfg.runlog_path == Path("/custom/path/run.jsonl")
    assert cfg.heartbeat_file == Path("/custom/path/hb")
    assert cfg.redispatch_window_ticks == 20
    assert cfg.redispatch_max == 5
    assert cfg.heartbeat_stall_s == 3600.5
    assert cfg.heartbeat_ping_url == "https://ping.example.com/"


def test_load_obs_config_parsed_int_types_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BH_REDISPATCH_* env vars are parsed to int (not left as str)."""
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_BH_PROJECT_ROOT, "/r")
    monkeypatch.setenv(_BH_REDISPATCH_WINDOW_TICKS, "15")
    monkeypatch.setenv(_BH_REDISPATCH_MAX, "7")

    cfg = load_obs_config()

    assert isinstance(cfg.redispatch_window_ticks, int)
    assert isinstance(cfg.redispatch_max, int)
    assert cfg.redispatch_window_ticks == 15
    assert cfg.redispatch_max == 7


def test_load_obs_config_parsed_float_type_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BH_HEARTBEAT_STALL_S is parsed to float (not left as str)."""
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_BH_PROJECT_ROOT, "/r")
    monkeypatch.setenv(_BH_HEARTBEAT_STALL_S, "900.0")

    cfg = load_obs_config()

    assert isinstance(cfg.heartbeat_stall_s, float)
    assert cfg.heartbeat_stall_s == 900.0


def test_load_obs_config_explicit_runlog_path_without_project_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit BH_RUNLOG_PATH wins even when BH_PROJECT_ROOT is unset."""
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_BH_RUNLOG_PATH, "/explicit/runlog.jsonl")

    cfg = load_obs_config()

    assert cfg.runlog_path == Path("/explicit/runlog.jsonl")


def test_load_obs_config_explicit_heartbeat_file_without_project_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit BH_HEARTBEAT_FILE wins even when BH_PROJECT_ROOT is unset."""
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_BH_HEARTBEAT_FILE, "/explicit/heartbeat")

    cfg = load_obs_config()

    assert cfg.heartbeat_file == Path("/explicit/heartbeat")


# ---------------------------------------------------------------------------
# BH_PROJECT_ROOT-unset tolerance
# ---------------------------------------------------------------------------


def test_load_obs_config_does_not_raise_without_project_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_obs_config() must NOT raise when BH_PROJECT_ROOT is unset."""
    _clear_all_obs_vars(monkeypatch)

    # Must not raise.
    cfg = load_obs_config()

    assert cfg is not None


def test_load_obs_config_cwd_relative_defaults_without_project_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without BH_PROJECT_ROOT, paths fall back to CWD-relative defaults."""
    _clear_all_obs_vars(monkeypatch)

    cfg = load_obs_config()

    assert cfg.runlog_path == Path(".baton-harness/runlog.jsonl")
    assert cfg.heartbeat_file == Path(".baton-harness/heartbeat")


def test_load_obs_config_numeric_defaults_without_project_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Numeric defaults are correct when BH_PROJECT_ROOT is unset."""
    _clear_all_obs_vars(monkeypatch)

    cfg = load_obs_config()

    assert cfg.redispatch_window_ticks == 10
    assert cfg.redispatch_max == 3
    assert cfg.heartbeat_stall_s == 7200.0
    assert cfg.heartbeat_ping_url is None


# ---------------------------------------------------------------------------
# ObsConfig is a frozen dataclass
# ---------------------------------------------------------------------------


def test_obs_config_is_dataclass() -> None:
    """ObsConfig is a dataclass."""
    assert dataclasses.is_dataclass(ObsConfig)


def test_obs_config_is_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting an attribute on ObsConfig raises FrozenInstanceError."""
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_BH_PROJECT_ROOT, "/r")
    cfg = load_obs_config()

    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.redispatch_max = 99  # type: ignore[misc]


def test_obs_config_field_types_are_correct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ObsConfig fields have the correct types after construction."""
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_BH_PROJECT_ROOT, "/r")
    cfg = load_obs_config()

    assert isinstance(cfg.runlog_path, Path)
    assert isinstance(cfg.heartbeat_file, Path)
    assert isinstance(cfg.redispatch_window_ticks, int)
    assert isinstance(cfg.redispatch_max, int)
    assert isinstance(cfg.heartbeat_stall_s, float)
    # heartbeat_ping_url is None or str — confirm it is not something else.
    assert cfg.heartbeat_ping_url is None or isinstance(
        cfg.heartbeat_ping_url, str
    )
