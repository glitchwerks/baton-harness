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
import logging
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
_BH_REDISPATCH_COUNTS_PATH = "BH_REDISPATCH_COUNTS_PATH"
_BH_WORKTREE_GC = "BH_WORKTREE_GC"

_ALL_OBS_VARS = (
    _BH_PROJECT_ROOT,
    _BH_RUNLOG_PATH,
    _BH_HEARTBEAT_FILE,
    _BH_REDISPATCH_WINDOW_TICKS,
    _BH_REDISPATCH_MAX,
    _BH_HEARTBEAT_STALL_S,
    _BH_HEARTBEAT_PING_URL,
    _BH_REDISPATCH_COUNTS_PATH,
    _BH_WORKTREE_GC,
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


# ---------------------------------------------------------------------------
# Malformed numeric env var tolerance (regression for PR #80 review finding)
# ---------------------------------------------------------------------------


def test_load_obs_config_malformed_int_uses_default(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-numeric BH_REDISPATCH_* values fall back to documented defaults.

    Regression test: ``load_obs_config()`` must NEVER raise on malformed
    integer env vars — the function contract guarantees it never raises.

    Args:
        monkeypatch: pytest fixture for hermetic env-var injection.
        caplog: pytest fixture to assert a WARNING was logged.
    """
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_BH_PROJECT_ROOT, "/r")
    monkeypatch.setenv(_BH_REDISPATCH_MAX, "nope")
    monkeypatch.setenv(_BH_REDISPATCH_WINDOW_TICKS, "also-bad")

    with caplog.at_level(logging.WARNING):
        cfg = load_obs_config()

    # Must not raise — result must equal the documented defaults.
    assert cfg.redispatch_max == 3, (
        f"Expected default 3 for malformed BH_REDISPATCH_MAX; got "
        f"{cfg.redispatch_max!r}"
    )
    assert cfg.redispatch_window_ticks == 10, (
        f"Expected default 10 for malformed BH_REDISPATCH_WINDOW_TICKS; "
        f"got {cfg.redispatch_window_ticks!r}"
    )
    # A WARNING must have been logged for each malformed var.
    warning_text = caplog.text
    assert "BH_REDISPATCH_MAX" in warning_text, (
        "Expected a WARNING mentioning BH_REDISPATCH_MAX in the log output"
    )
    assert "BH_REDISPATCH_WINDOW_TICKS" in warning_text, (
        "Expected a WARNING mentioning BH_REDISPATCH_WINDOW_TICKS in the "
        "log output"
    )


def test_load_obs_config_malformed_float_uses_default(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-numeric BH_HEARTBEAT_STALL_S falls back to the documented default.

    Regression test: ``load_obs_config()`` must NEVER raise on a malformed
    float env var.

    Args:
        monkeypatch: pytest fixture for hermetic env-var injection.
        caplog: pytest fixture to assert a WARNING was logged.
    """
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_BH_PROJECT_ROOT, "/r")
    monkeypatch.setenv(_BH_HEARTBEAT_STALL_S, "not-a-float")

    with caplog.at_level(logging.WARNING):
        cfg = load_obs_config()

    # Must not raise — result must equal the documented default.
    assert cfg.heartbeat_stall_s == 7200.0, (
        f"Expected default 7200.0 for malformed BH_HEARTBEAT_STALL_S; "
        f"got {cfg.heartbeat_stall_s!r}"
    )
    # A WARNING must have been logged for the malformed var.
    assert "BH_HEARTBEAT_STALL_S" in caplog.text, (
        "Expected a WARNING mentioning BH_HEARTBEAT_STALL_S in the log output"
    )


# ---------------------------------------------------------------------------
# redispatch_counts_path field (new for #77)
# ---------------------------------------------------------------------------


def test_load_obs_config_redispatch_counts_path_default_from_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default redispatch_counts_path is derived from BH_PROJECT_ROOT.

    The path must be
    ``${BH_PROJECT_ROOT}/.baton-harness/dispatch-counts.json``
    when BH_REDISPATCH_COUNTS_PATH is unset.

    Args:
        monkeypatch: pytest fixture for hermetic env-var injection.
    """
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_BH_PROJECT_ROOT, "/some/root")

    cfg = load_obs_config()

    assert cfg.redispatch_counts_path == Path(
        "/some/root/.baton-harness/dispatch-counts.json"
    )


def test_load_obs_config_redispatch_counts_path_cwd_relative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without BH_PROJECT_ROOT, redispatch_counts_path is CWD-relative.

    Must be ``.baton-harness/dispatch-counts.json`` (mirrors runlog_path
    behaviour when BH_PROJECT_ROOT is unset).

    Args:
        monkeypatch: pytest fixture for hermetic env-var injection.
    """
    _clear_all_obs_vars(monkeypatch)

    cfg = load_obs_config()

    assert cfg.redispatch_counts_path == Path(
        ".baton-harness/dispatch-counts.json"
    )


def test_load_obs_config_redispatch_counts_path_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BH_REDISPATCH_COUNTS_PATH overrides the derived default.

    Args:
        monkeypatch: pytest fixture for hermetic env-var injection.
    """
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_BH_PROJECT_ROOT, "/some/root")
    monkeypatch.setenv(
        _BH_REDISPATCH_COUNTS_PATH, "/custom/path/dispatch-counts.json"
    )

    cfg = load_obs_config()

    assert cfg.redispatch_counts_path == Path(
        "/custom/path/dispatch-counts.json"
    )


def test_load_obs_config_redispatch_counts_path_override_no_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BH_REDISPATCH_COUNTS_PATH wins even when BH_PROJECT_ROOT is unset.

    Args:
        monkeypatch: pytest fixture for hermetic env-var injection.
    """
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(
        _BH_REDISPATCH_COUNTS_PATH, "/explicit/dispatch-counts.json"
    )

    cfg = load_obs_config()

    assert cfg.redispatch_counts_path == Path("/explicit/dispatch-counts.json")


def test_load_obs_config_redispatch_counts_path_is_path_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """redispatch_counts_path is a Path instance (not str).

    Args:
        monkeypatch: pytest fixture for hermetic env-var injection.
    """
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_BH_PROJECT_ROOT, "/r")

    cfg = load_obs_config()

    assert isinstance(cfg.redispatch_counts_path, Path), (
        f"Expected Path, got {type(cfg.redispatch_counts_path)!r}"
    )


def test_load_obs_config_still_never_raises_with_new_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_obs_config() does not raise even with the new field unset.

    Regression guard: adding redispatch_counts_path must not break the
    never-raise contract.

    Args:
        monkeypatch: pytest fixture for hermetic env-var injection.
    """
    _clear_all_obs_vars(monkeypatch)

    # Must not raise.
    cfg = load_obs_config()

    assert hasattr(cfg, "redispatch_counts_path")


# ---------------------------------------------------------------------------
# worktree_gc field (new for #33 P1)
# ---------------------------------------------------------------------------


def test_load_obs_config_worktree_gc_default_is_detect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BH_WORKTREE_GC unset → worktree_gc defaults to 'detect'.

    The default must be the conservative detect-only mode (IS-5: detect,
    not destroy). Destructive reclaim is opt-in only.

    Args:
        monkeypatch: pytest fixture for hermetic env-var injection.
    """
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_BH_PROJECT_ROOT, "/r")

    cfg = load_obs_config()

    assert cfg.worktree_gc == "detect", (
        f"Expected worktree_gc='detect' when BH_WORKTREE_GC is unset; "
        f"got {cfg.worktree_gc!r}"
    )


def test_load_obs_config_worktree_gc_reclaim_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BH_WORKTREE_GC=reclaim → worktree_gc is 'reclaim'.

    The 'reclaim' value must be parsed and stored as the literal string
    'reclaim', enabling the opt-in destructive GC path.

    Args:
        monkeypatch: pytest fixture for hermetic env-var injection.
    """
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_BH_PROJECT_ROOT, "/r")
    monkeypatch.setenv(_BH_WORKTREE_GC, "reclaim")

    cfg = load_obs_config()

    assert cfg.worktree_gc == "reclaim", (
        f"Expected worktree_gc='reclaim' when BH_WORKTREE_GC=reclaim; "
        f"got {cfg.worktree_gc!r}"
    )


def test_load_obs_config_worktree_gc_garbage_warns_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """BH_WORKTREE_GC=<invalid> warns and falls back to 'detect'.

    An unrecognised value (not 'detect' or 'reclaim') must:
    - NOT raise (consistent with the never-raise contract).
    - Fall back to the safe default 'detect'.
    - Log a WARNING mentioning BH_WORKTREE_GC (consistent with the
      guarded-parse pattern used for malformed int/float env vars).

    Args:
        monkeypatch: pytest fixture for hermetic env-var injection.
        caplog: pytest fixture to assert a WARNING was logged.
    """
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_BH_PROJECT_ROOT, "/r")
    monkeypatch.setenv(_BH_WORKTREE_GC, "destroy-everything")

    with caplog.at_level(logging.WARNING):
        cfg = load_obs_config()

    assert cfg.worktree_gc == "detect", (
        f"Expected fallback worktree_gc='detect' for invalid "
        f"BH_WORKTREE_GC; got {cfg.worktree_gc!r}"
    )
    assert "BH_WORKTREE_GC" in caplog.text, (
        "Expected a WARNING mentioning BH_WORKTREE_GC for the invalid value"
    )
