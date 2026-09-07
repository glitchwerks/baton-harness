"""Temporary Baton-era compatibility contracts for 0.2/0.3."""

import importlib
import sys
import warnings
from collections.abc import Callable

import pytest

import codereeve


def test_top_level_legacy_package_reexports_version() -> None:
    """The legacy package warns once and exposes the canonical version."""
    sys.modules.pop("baton_harness", None)
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        legacy = importlib.import_module("baton_harness")
    assert legacy.__version__ == codereeve.__version__
    assert legacy.__all__ == ["__version__"]
    assert len(captured) == 1
    assert "removed in 0.4.0" in str(captured[0].message)


@pytest.mark.parametrize(
    ("legacy_name", "canonical_name"),
    [
        ("after_create", "after_create"),
        ("before_run", "before_run"),
        ("after_run", "after_run"),
        ("chain.cli", "chain.cli"),
        (
            "hooks.force_pr_not_merge",
            "hooks.force_pr_not_merge",
        ),
        ("verify_foundation", "verify_foundation"),
    ],
)
def test_legacy_entry_module_is_canonical_function(
    legacy_name: str,
    canonical_name: str,
) -> None:
    """Every supported legacy module re-exports the canonical function."""
    legacy = importlib.import_module(f"baton_harness.{legacy_name}")
    canonical = importlib.import_module(f"codereeve.{canonical_name}")
    assert legacy.main is canonical.main
    assert legacy.__all__ == ["main"]


@pytest.mark.parametrize(
    ("adapter_name", "surface", "replacement", "arguments", "forwarded"),
    [
        (
            "daemon_main",
            "bh-daemon",
            "codereeve daemon",
            ["--once"],
            ["daemon", "--once"],
        ),
        (
            "after_create_main",
            "bh-after-create",
            "codereeve hook after-create",
            ["one"],
            ["hook", "after-create", "one"],
        ),
        (
            "before_run_main",
            "bh-before-run",
            "codereeve hook before-run",
            ["two"],
            ["hook", "before-run", "two"],
        ),
        (
            "after_run_main",
            "bh-after-run",
            "codereeve hook after-run",
            ["three"],
            ["hook", "after-run", "three"],
        ),
        (
            "force_pr_not_merge_main",
            "bh-force-pr-not-merge",
            "codereeve hook force-pr-not-merge",
            [],
            ["hook", "force-pr-not-merge"],
        ),
        (
            "verify_foundation_main",
            "bh-verify-foundation",
            "codereeve verify",
            ["--python", "3.13"],
            ["verify", "--python", "3.13"],
        ),
    ],
)
def test_legacy_console_adapter_forwards_and_warns_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    adapter_name: str,
    surface: str,
    replacement: str,
    arguments: list[str],
    forwarded: list[str],
) -> None:
    """Legacy scripts preserve routing while deduplicating their notice."""
    legacy_cli = importlib.import_module("codereeve.legacy_cli")
    calls: list[list[str]] = []

    def fake_main(argv: list[str]) -> int:
        calls.append(argv)
        return 17

    monkeypatch.setattr(legacy_cli.cli, "main", fake_main)
    monkeypatch.setattr(sys, "argv", [surface, *arguments])
    legacy_cli._WARNED.clear()
    adapter: Callable[[], int] = getattr(legacy_cli, adapter_name)

    assert adapter() == 17
    assert adapter() == 17
    assert calls == [forwarded, forwarded]
    assert capsys.readouterr().err.splitlines() == [
        f"{surface} is deprecated; use {replacement}; removed in 0.4.0"
    ]
