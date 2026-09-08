"""Tests for canonical and compatibility CodeReeve filesystem paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from codereeve.paths import (
    PathConflictError,
    PathLayout,
    select_compatible_directory,
    select_compatible_file,
)


def test_layout_uses_canonical_codereeve_paths(tmp_path: Path) -> None:
    """The layout exposes every 0.2 canonical location."""
    layout = PathLayout.for_environment(
        tmp_path / "project",
        {"XDG_CONFIG_HOME": str(tmp_path / "xdg")},
        etc_root=tmp_path / "etc",
    )

    assert layout.canonical_state == tmp_path / "project" / ".codereeve"
    assert layout.canonical_config == layout.canonical_state / "config.env"
    assert layout.canonical_host == tmp_path / "xdg" / "codereeve" / "host.env"
    assert (
        layout.canonical_secrets
        == tmp_path / "etc" / "codereeve" / "secrets.env"
    )
    assert layout.symphony_state == tmp_path / "project" / ".symphony"


def test_layout_uses_home_when_xdg_config_home_is_unset(
    tmp_path: Path,
) -> None:
    """The host configuration falls back to the supplied home directory."""
    layout = PathLayout.for_environment(
        tmp_path / "project",
        {},
        home=tmp_path / "home",
        etc_root=tmp_path / "etc",
    )

    assert layout.canonical_host == (
        tmp_path / "home" / ".config" / "codereeve" / "host.env"
    )


def test_layout_exposes_the_full_legacy_catalog(tmp_path: Path) -> None:
    """Legacy paths are explicit inputs to migration-safe selection."""
    layout = PathLayout.for_environment(
        tmp_path / "project",
        {},
        home=tmp_path / "home",
        etc_root=tmp_path / "etc",
    )

    assert layout.legacy_state == tmp_path / "project" / ".baton-harness"
    assert layout.legacy_config == tmp_path / "project" / ".bh" / "config.env"
    assert layout.legacy_host == (
        tmp_path / "home" / ".config" / "baton-harness" / "host.env"
    )
    assert layout.legacy_secrets == (
        tmp_path / "etc" / "bh-daemon" / "secrets.env"
    )
    assert layout.legacy_unit == (
        tmp_path / "etc" / "systemd" / "system" / "bh-daemon.service"
    )
    assert layout.canonical_unit == (
        tmp_path / "etc" / "systemd" / "system" / "codereeve.service"
    )


def test_file_selection_prefers_canonical_when_neither_path_exists(
    tmp_path: Path,
) -> None:
    """A fresh installation selects the canonical file path."""
    canonical = tmp_path / "canonical.env"
    selected = select_compatible_file(
        canonical, tmp_path / "legacy.env", label="config"
    )

    assert selected.path == canonical
    assert selected.uses_legacy is False


@pytest.mark.parametrize("selected_name", ["canonical.env", "legacy.env"])
def test_file_selection_accepts_exactly_one_regular_file(
    tmp_path: Path, selected_name: str
) -> None:
    """Either single compatible file remains usable during the transition."""
    canonical = tmp_path / "canonical.env"
    legacy = tmp_path / "legacy.env"
    (canonical if selected_name == canonical.name else legacy).write_text(
        "KEY=value\n", encoding="utf-8"
    )

    selected = select_compatible_file(canonical, legacy, label="config")

    assert selected.path.name == selected_name
    assert selected.uses_legacy is (selected_name == legacy.name)


def test_file_selection_blocks_identical_canonical_and_legacy_files(
    tmp_path: Path,
) -> None:
    """Coexisting compatibility paths remain ambiguous even when identical."""
    canonical = tmp_path / "canonical.env"
    legacy = tmp_path / "legacy.env"
    canonical.write_text("KEY=value\n", encoding="utf-8")
    legacy.write_text("KEY=value\n", encoding="utf-8")

    with pytest.raises(PathConflictError) as exc_info:
        select_compatible_file(canonical, legacy, label="config")

    assert str(canonical) in str(exc_info.value)
    assert str(legacy) in str(exc_info.value)
    assert "value" not in str(exc_info.value)


def test_file_selection_rejects_symlink_without_following_it(
    tmp_path: Path,
) -> None:
    """A symlink is unsafe even when it targets a regular file."""
    target = tmp_path / "target.env"
    target.write_text("KEY=value\n", encoding="utf-8")
    canonical = tmp_path / "canonical.env"
    try:
        canonical.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(PathConflictError, match="canonical"):
        select_compatible_file(
            canonical, tmp_path / "legacy.env", label="config"
        )


def test_file_selection_rejects_symlinked_parent_directory(
    tmp_path: Path,
) -> None:
    """A final file is unsafe when its state-directory parent is linked."""
    target_state = tmp_path / "target-state"
    target_state.mkdir()
    (target_state / "config.env").write_text("KEY=value\n", encoding="utf-8")
    canonical = tmp_path / ".codereeve" / "config.env"
    try:
        canonical.parent.symlink_to(target_state, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(PathConflictError, match="config"):
        select_compatible_file(
            canonical, tmp_path / ".bh" / "config.env", label="config"
        )


def test_file_selection_rejects_linked_parent_without_final_file(
    tmp_path: Path,
) -> None:
    """A missing final file cannot hide an unsafe linked state directory."""
    target_state = tmp_path / "target-state"
    target_state.mkdir()
    canonical = tmp_path / ".codereeve" / "config.env"
    try:
        canonical.parent.symlink_to(target_state, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(PathConflictError, match="config"):
        select_compatible_file(
            canonical, tmp_path / ".bh" / "config.env", label="config"
        )


def test_file_selection_rejects_linked_grandparent_with_missing_parent(
    tmp_path: Path,
) -> None:
    """A missing child cannot hide an unsafe linked grandparent directory."""
    target_root = tmp_path / "target-root"
    target_root.mkdir()
    linked_root = tmp_path / "linked-root"
    try:
        linked_root.symlink_to(target_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(PathConflictError, match="config"):
        select_compatible_file(
            linked_root / "missing" / "config.env",
            tmp_path / ".bh" / "config.env",
            label="config",
        )


def test_directory_selection_rejects_linked_grandparent_with_missing_parent(
    tmp_path: Path,
) -> None:
    """Directory selection also inspects existing linked grandparents."""
    target_root = tmp_path / "target-root"
    target_root.mkdir()
    linked_root = tmp_path / "linked-root"
    try:
        linked_root.symlink_to(target_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(PathConflictError, match="state"):
        select_compatible_directory(
            linked_root / "missing" / ".codereeve",
            tmp_path / ".baton-harness",
            label="state",
        )


def test_directory_selection_does_not_consider_symphony_state(
    tmp_path: Path,
) -> None:
    """Symphony-owned state never participates in CodeReeve selection."""
    canonical = tmp_path / ".codereeve"
    legacy = tmp_path / ".baton-harness"
    symphony = tmp_path / ".symphony"
    symphony.mkdir()

    selected = select_compatible_directory(canonical, legacy, label="state")

    assert selected.path == canonical
    assert not selected.uses_legacy
    assert symphony.is_dir()


def test_directory_selection_rejects_non_directory(tmp_path: Path) -> None:
    """A state-file replacement fails closed rather than being followed."""
    canonical = tmp_path / ".codereeve"
    canonical.write_text("not a directory", encoding="utf-8")

    with pytest.raises(PathConflictError, match="state"):
        select_compatible_directory(
            canonical, tmp_path / ".baton-harness", label="state"
        )
