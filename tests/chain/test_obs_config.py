"""Unit tests for codereeve.chain.obs_config.

Tests the ``ObsConfig`` dataclass and ``load_obs_config`` factory.
All environment variable injection uses ``monkeypatch`` so tests are
hermetic and restore the original environment on teardown.

Coverage:
- Default field values are derived from ``CODEREEVE_PROJECT_ROOT`` when path-
  specific variables are unset.
- Explicit overrides for every ``CODEREEVE_*`` variable win over the derived
  default (int/float parsing included).
- ``load_obs_config()`` does NOT raise when ``CODEREEVE_PROJECT_ROOT`` is
  unset, falling back to CWD-relative paths.
- ``ObsConfig`` is a frozen dataclass (attribute mutation raises
  ``FrozenInstanceError``).
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path

import pytest

from codereeve.chain.obs_config import ObsConfig, load_obs_config

# ---------------------------------------------------------------------------
# Environment variable names (mirrors the contract exactly)
# ---------------------------------------------------------------------------

_CODEREEVE_PROJECT_ROOT = "CODEREEVE_PROJECT_ROOT"
_CODEREEVE_RUNLOG_PATH = "CODEREEVE_RUNLOG_PATH"
_CODEREEVE_HEARTBEAT_FILE = "CODEREEVE_HEARTBEAT_FILE"
_CODEREEVE_REDISPATCH_WINDOW_TICKS = "CODEREEVE_REDISPATCH_WINDOW_TICKS"
_CODEREEVE_REDISPATCH_MAX = "CODEREEVE_REDISPATCH_MAX"
_CODEREEVE_HEARTBEAT_STALL_S = "CODEREEVE_HEARTBEAT_STALL_S"
_CODEREEVE_HEARTBEAT_PING_URL = "CODEREEVE_HEARTBEAT_PING_URL"
_CODEREEVE_REDISPATCH_COUNTS_PATH = "CODEREEVE_REDISPATCH_COUNTS_PATH"
_CODEREEVE_WORKTREE_GC = "CODEREEVE_WORKTREE_GC"

_ALL_OBS_VARS = (
    _CODEREEVE_PROJECT_ROOT,
    _CODEREEVE_RUNLOG_PATH,
    _CODEREEVE_HEARTBEAT_FILE,
    _CODEREEVE_REDISPATCH_WINDOW_TICKS,
    _CODEREEVE_REDISPATCH_MAX,
    _CODEREEVE_HEARTBEAT_STALL_S,
    _CODEREEVE_HEARTBEAT_PING_URL,
    _CODEREEVE_REDISPATCH_COUNTS_PATH,
    _CODEREEVE_WORKTREE_GC,
)


# ---------------------------------------------------------------------------
# Helper: clear all CODEREEVE_* obs vars via monkeypatch
# ---------------------------------------------------------------------------


def _clear_all_obs_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all CODEREEVE_* observability environment variables."""
    for var in _ALL_OBS_VARS:
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Defaults from CODEREEVE_PROJECT_ROOT
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_state_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep CWD-relative default tests independent of local runtime state."""
    monkeypatch.chdir(tmp_path)


def test_load_obs_config_defaults_from_project_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Derive path defaults from the project root."""
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_CODEREEVE_PROJECT_ROOT, "/some/root")

    cfg = load_obs_config()

    assert cfg.runlog_path == Path("/some/root/.codereeve/runlog.jsonl")
    assert cfg.heartbeat_file == Path("/some/root/.codereeve/heartbeat")
    assert cfg.redispatch_window_ticks == 10
    assert cfg.redispatch_max == 3
    assert cfg.heartbeat_stall_s == 7200.0
    assert cfg.heartbeat_ping_url is None


def test_load_obs_config_default_int_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default redispatch fields are int, not str."""
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_CODEREEVE_PROJECT_ROOT, "/some/root")

    cfg = load_obs_config()

    assert isinstance(cfg.redispatch_window_ticks, int)
    assert isinstance(cfg.redispatch_max, int)


def test_load_obs_config_default_float_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default heartbeat_stall_s is float, not str or int."""
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_CODEREEVE_PROJECT_ROOT, "/some/root")

    cfg = load_obs_config()

    assert isinstance(cfg.heartbeat_stall_s, float)


# ---------------------------------------------------------------------------
# Explicit overrides win over derived defaults
# ---------------------------------------------------------------------------


def test_load_obs_config_explicit_overrides_win(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicitly set CODEREEVE_* vars override all derived defaults."""
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_CODEREEVE_PROJECT_ROOT, "/some/root")
    monkeypatch.setenv(_CODEREEVE_RUNLOG_PATH, "/custom/path/run.jsonl")
    monkeypatch.setenv(_CODEREEVE_HEARTBEAT_FILE, "/custom/path/hb")
    monkeypatch.setenv(_CODEREEVE_REDISPATCH_WINDOW_TICKS, "20")
    monkeypatch.setenv(_CODEREEVE_REDISPATCH_MAX, "5")
    monkeypatch.setenv(_CODEREEVE_HEARTBEAT_STALL_S, "3600.5")
    monkeypatch.setenv(
        _CODEREEVE_HEARTBEAT_PING_URL, "https://ping.example.com/"
    )

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
    """CODEREEVE_REDISPATCH_* env vars are parsed to int (not left as str)."""
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_CODEREEVE_PROJECT_ROOT, "/r")
    monkeypatch.setenv(_CODEREEVE_REDISPATCH_WINDOW_TICKS, "15")
    monkeypatch.setenv(_CODEREEVE_REDISPATCH_MAX, "7")

    cfg = load_obs_config()

    assert isinstance(cfg.redispatch_window_ticks, int)
    assert isinstance(cfg.redispatch_max, int)
    assert cfg.redispatch_window_ticks == 15
    assert cfg.redispatch_max == 7


def test_load_obs_config_parsed_float_type_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CODEREEVE_HEARTBEAT_STALL_S is parsed to float (not left as str)."""
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_CODEREEVE_PROJECT_ROOT, "/r")
    monkeypatch.setenv(_CODEREEVE_HEARTBEAT_STALL_S, "900.0")

    cfg = load_obs_config()

    assert isinstance(cfg.heartbeat_stall_s, float)
    assert cfg.heartbeat_stall_s == 900.0


def test_load_obs_config_explicit_runlog_path_without_project_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit runlog path wins even when project root is unset."""
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_CODEREEVE_RUNLOG_PATH, "/explicit/runlog.jsonl")

    cfg = load_obs_config()

    assert cfg.runlog_path == Path("/explicit/runlog.jsonl")


def test_load_obs_config_explicit_heartbeat_file_without_project_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit heartbeat file wins even when project root is unset."""
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_CODEREEVE_HEARTBEAT_FILE, "/explicit/heartbeat")

    cfg = load_obs_config()

    assert cfg.heartbeat_file == Path("/explicit/heartbeat")


# ---------------------------------------------------------------------------
# CODEREEVE_PROJECT_ROOT-unset tolerance
# ---------------------------------------------------------------------------


def test_load_obs_config_does_not_raise_without_project_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_obs_config() must NOT raise when project root is unset."""
    _clear_all_obs_vars(monkeypatch)

    # Must not raise.
    cfg = load_obs_config()

    assert cfg is not None


def test_load_obs_config_cwd_relative_defaults_without_project_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without project root, paths fall back to CWD-relative defaults."""
    _clear_all_obs_vars(monkeypatch)

    cfg = load_obs_config()

    assert cfg.runlog_path == Path(".codereeve/runlog.jsonl")
    assert cfg.heartbeat_file == Path(".codereeve/heartbeat")


def test_load_obs_config_numeric_defaults_without_project_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Numeric defaults are correct when CODEREEVE_PROJECT_ROOT is unset."""
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
    monkeypatch.setenv(_CODEREEVE_PROJECT_ROOT, "/r")
    cfg = load_obs_config()

    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.redispatch_max = 99  # type: ignore[misc]


def test_obs_config_field_types_are_correct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ObsConfig fields have the correct types after construction."""
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_CODEREEVE_PROJECT_ROOT, "/r")
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
    """Non-numeric redispatch * values fall back to documented defaults.

    Regression test: ``load_obs_config()`` must NEVER raise on malformed
    integer env vars — the function contract guarantees it never raises.

    Args:
        monkeypatch: pytest fixture for hermetic env-var injection.
        caplog: pytest fixture to assert a WARNING was logged.
    """
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_CODEREEVE_PROJECT_ROOT, "/r")
    monkeypatch.setenv(_CODEREEVE_REDISPATCH_MAX, "nope")
    monkeypatch.setenv(_CODEREEVE_REDISPATCH_WINDOW_TICKS, "also-bad")

    with caplog.at_level(logging.WARNING):
        cfg = load_obs_config()

    # Must not raise — result must equal the documented defaults.
    assert cfg.redispatch_max == 3, (
        f"Expected default 3 for malformed CODEREEVE_REDISPATCH_MAX; got "
        f"{cfg.redispatch_max!r}"
    )
    assert cfg.redispatch_window_ticks == 10, (
        f"Expected default 10 for malformed "
        f"CODEREEVE_REDISPATCH_WINDOW_TICKS; "
        f"got {cfg.redispatch_window_ticks!r}"
    )
    # A WARNING must have been logged for each malformed var.
    warning_text = caplog.text
    assert "CODEREEVE_REDISPATCH_MAX" in warning_text, (
        "Expected a WARNING mentioning "
        "CODEREEVE_REDISPATCH_MAX in the log output"
    )
    assert "CODEREEVE_REDISPATCH_WINDOW_TICKS" in warning_text, (
        "Expected a WARNING mentioning "
        "CODEREEVE_REDISPATCH_WINDOW_TICKS in the "
        "log output"
    )


def test_load_obs_config_malformed_float_uses_default(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-numeric heartbeat stall s falls back to the documented default.

    Regression test: ``load_obs_config()`` must NEVER raise on a malformed
    float env var.

    Args:
        monkeypatch: pytest fixture for hermetic env-var injection.
        caplog: pytest fixture to assert a WARNING was logged.
    """
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_CODEREEVE_PROJECT_ROOT, "/r")
    monkeypatch.setenv(_CODEREEVE_HEARTBEAT_STALL_S, "not-a-float")

    with caplog.at_level(logging.WARNING):
        cfg = load_obs_config()

    # Must not raise — result must equal the documented default.
    assert cfg.heartbeat_stall_s == 7200.0, (
        f"Expected default 7200.0 for malformed CODEREEVE_HEARTBEAT_STALL_S; "
        f"got {cfg.heartbeat_stall_s!r}"
    )
    # A WARNING must have been logged for the malformed var.
    assert "CODEREEVE_HEARTBEAT_STALL_S" in caplog.text, (
        "Expected a WARNING mentioning "
        "CODEREEVE_HEARTBEAT_STALL_S in the log output"
    )


# ---------------------------------------------------------------------------
# redispatch_counts_path field (new for #77)
# ---------------------------------------------------------------------------


def test_load_obs_config_redispatch_counts_path_default_from_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default redispatch_counts_path is derived from CODEREEVE_PROJECT_ROOT.

    The path must be
    ``${CODEREEVE_PROJECT_ROOT}/.codereeve/dispatch-counts.json``
    when CODEREEVE_REDISPATCH_COUNTS_PATH is unset.

    Args:
        monkeypatch: pytest fixture for hermetic env-var injection.
    """
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_CODEREEVE_PROJECT_ROOT, "/some/root")

    cfg = load_obs_config()

    assert cfg.redispatch_counts_path == Path(
        "/some/root/.codereeve/dispatch-counts.json"
    )


def test_load_obs_config_redispatch_counts_path_cwd_relative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without CODEREEVE_PROJECT_ROOT, redispatch_counts_path is CWD-relative.

    Must be ``.codereeve/dispatch-counts.json`` (mirrors runlog_path
    behaviour when CODEREEVE_PROJECT_ROOT is unset).

    Args:
        monkeypatch: pytest fixture for hermetic env-var injection.
    """
    _clear_all_obs_vars(monkeypatch)

    cfg = load_obs_config()

    assert cfg.redispatch_counts_path == Path(
        ".codereeve/dispatch-counts.json"
    )


def test_load_obs_config_redispatch_counts_path_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CODEREEVE_REDISPATCH_COUNTS_PATH overrides the derived default.

    Args:
        monkeypatch: pytest fixture for hermetic env-var injection.
    """
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_CODEREEVE_PROJECT_ROOT, "/some/root")
    monkeypatch.setenv(
        _CODEREEVE_REDISPATCH_COUNTS_PATH, "/custom/path/dispatch-counts.json"
    )

    cfg = load_obs_config()

    assert cfg.redispatch_counts_path == Path(
        "/custom/path/dispatch-counts.json"
    )


def test_load_obs_config_redispatch_counts_path_override_no_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redispatch counts path wins even when project root is unset.

    Args:
        monkeypatch: pytest fixture for hermetic env-var injection.
    """
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(
        _CODEREEVE_REDISPATCH_COUNTS_PATH, "/explicit/dispatch-counts.json"
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
    monkeypatch.setenv(_CODEREEVE_PROJECT_ROOT, "/r")

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
    """CODEREEVE_WORKTREE_GC unset → worktree_gc defaults to 'detect'.

    The default must be the conservative detect-only mode (IS-5: detect,
    not destroy). Destructive reclaim is opt-in only.

    Args:
        monkeypatch: pytest fixture for hermetic env-var injection.
    """
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_CODEREEVE_PROJECT_ROOT, "/r")

    cfg = load_obs_config()

    assert cfg.worktree_gc == "detect", (
        f"Expected worktree_gc='detect' when CODEREEVE_WORKTREE_GC is unset; "
        f"got {cfg.worktree_gc!r}"
    )


def test_load_obs_config_worktree_gc_reclaim_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CODEREEVE_WORKTREE_GC=reclaim → worktree_gc is 'reclaim'.

    The 'reclaim' value must be parsed and stored as the literal string
    'reclaim', enabling the opt-in destructive GC path.

    Args:
        monkeypatch: pytest fixture for hermetic env-var injection.
    """
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_CODEREEVE_PROJECT_ROOT, "/r")
    monkeypatch.setenv(_CODEREEVE_WORKTREE_GC, "reclaim")

    cfg = load_obs_config()

    assert cfg.worktree_gc == "reclaim", (
        f"Expected worktree_gc='reclaim' when CODEREEVE_WORKTREE_GC=reclaim; "
        f"got {cfg.worktree_gc!r}"
    )


def test_load_obs_config_worktree_gc_garbage_warns_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CODEREEVE_WORKTREE_GC=<invalid> warns and falls back to 'detect'.

    An unrecognised value (not 'detect' or 'reclaim') must:
    - NOT raise (consistent with the never-raise contract).
    - Fall back to the safe default 'detect'.
    - Log a WARNING mentioning CODEREEVE_WORKTREE_GC (consistent with the
      guarded-parse pattern used for malformed int/float env vars).

    Args:
        monkeypatch: pytest fixture for hermetic env-var injection.
        caplog: pytest fixture to assert a WARNING was logged.
    """
    _clear_all_obs_vars(monkeypatch)
    monkeypatch.setenv(_CODEREEVE_PROJECT_ROOT, "/r")
    monkeypatch.setenv(_CODEREEVE_WORKTREE_GC, "destroy-everything")

    with caplog.at_level(logging.WARNING):
        cfg = load_obs_config()

    assert cfg.worktree_gc == "detect", (
        f"Expected fallback worktree_gc='detect' for invalid "
        f"CODEREEVE_WORKTREE_GC; got {cfg.worktree_gc!r}"
    )
    assert "CODEREEVE_WORKTREE_GC" in caplog.text, (
        "Expected a WARNING mentioning "
        "CODEREEVE_WORKTREE_GC for the invalid value"
    )


# ---------------------------------------------------------------------------
# worker_progress_stall_s field (new for #33 P2)
# ---------------------------------------------------------------------------

_CODEREEVE_WORKER_PROGRESS_STALL_S = "CODEREEVE_WORKER_PROGRESS_STALL_S"

# Add to all-vars cleanup so monkeypatch isolation is complete.
_ALL_OBS_VARS_P2 = _ALL_OBS_VARS + (_CODEREEVE_WORKER_PROGRESS_STALL_S,)


def _clear_all_obs_vars_p2(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all * observability env vars including the P2 addition."""
    for var in _ALL_OBS_VARS_P2:
        monkeypatch.delenv(var, raising=False)


def test_load_obs_config_worker_progress_stall_s_default_is_1800(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker progress stall s unset → worker_progress_stall_s == 1800.0.

    The default is 1800.0 s (6× the 300 s per-turn timeout at config.py:L31).
    This pins the verified default from OQ-2 resolution.

    Args:
        monkeypatch: pytest fixture for hermetic env-var injection.
    """
    _clear_all_obs_vars_p2(monkeypatch)
    monkeypatch.setenv(_CODEREEVE_PROJECT_ROOT, "/r")

    cfg = load_obs_config()

    assert cfg.worker_progress_stall_s == 1800.0, (  # type: ignore[attr-defined]
        f"Expected worker_progress_stall_s=1800.0 when "
        f"CODEREEVE_WORKER_PROGRESS_STALL_S is unset; got "
        f"{cfg.worker_progress_stall_s!r}"  # type: ignore[attr-defined]
    )


def test_load_obs_config_worker_progress_stall_s_valid_env_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid CODEREEVE_WORKER_PROGRESS_STALL_S is parsed to float.

    A well-formed numeric string must be parsed to the float value.

    Args:
        monkeypatch: pytest fixture for hermetic env-var injection.
    """
    _clear_all_obs_vars_p2(monkeypatch)
    monkeypatch.setenv(_CODEREEVE_PROJECT_ROOT, "/r")
    monkeypatch.setenv(_CODEREEVE_WORKER_PROGRESS_STALL_S, "900.0")

    cfg = load_obs_config()

    assert cfg.worker_progress_stall_s == 900.0, (  # type: ignore[attr-defined]
        f"Expected worker_progress_stall_s=900.0; got "
        f"{cfg.worker_progress_stall_s!r}"  # type: ignore[attr-defined]
    )
    assert isinstance(cfg.worker_progress_stall_s, float), (  # type: ignore[attr-defined]
        f"worker_progress_stall_s must be float, not "
        f"{type(cfg.worker_progress_stall_s)!r}"  # type: ignore[attr-defined]
    )


def test_load_obs_config_worker_progress_stall_s_garbage_warns_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Worker progress stall s=<garbage> warns and falls back to 1800.0.

    Consistent with the guarded-parse pattern used for all other numeric
    env vars:
    - Must NOT raise (never-raise contract).
    - Falls back to the documented default 1800.0.
    - Logs a WARNING mentioning CODEREEVE_WORKER_PROGRESS_STALL_S.

    Args:
        monkeypatch: pytest fixture for hermetic env-var injection.
        caplog: pytest fixture to assert a WARNING was logged.
    """
    _clear_all_obs_vars_p2(monkeypatch)
    monkeypatch.setenv(_CODEREEVE_PROJECT_ROOT, "/r")
    monkeypatch.setenv(_CODEREEVE_WORKER_PROGRESS_STALL_S, "not-a-number")

    with caplog.at_level(logging.WARNING):
        cfg = load_obs_config()

    assert cfg.worker_progress_stall_s == 1800.0, (  # type: ignore[attr-defined]
        f"Expected fallback 1800.0 for garbage "
        f"CODEREEVE_WORKER_PROGRESS_STALL_S; "
        f"got {cfg.worker_progress_stall_s!r}"  # type: ignore[attr-defined]
    )
    assert "CODEREEVE_WORKER_PROGRESS_STALL_S" in caplog.text, (
        "Expected a WARNING mentioning "
        "CODEREEVE_WORKER_PROGRESS_STALL_S for the "
        "garbage value"
    )
