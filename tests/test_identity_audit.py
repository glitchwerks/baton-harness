"""Exact legacy identity exceptions must not hide new or stale references."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.fast


def _audit() -> ModuleType:
    """Load the standalone repository audit."""
    path = ROOT / "scripts/audit_identity.py"
    assert path.exists(), "identity audit implementation is missing"
    spec = importlib.util.spec_from_file_location("audit_identity", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _entry(**updates: object) -> dict[str, object]:
    """Return one exact compatibility allowance."""
    result: dict[str, object] = {
        "path": "legacy.txt",
        "text": "Use bh-daemon temporarily.",
        "count": 1,
        "category": "compatibility",
        "reason": "Retained command through 0.3.x; removed in 0.4.",
        "kind": "text",
    }
    result.update(updates)
    return result


@pytest.mark.parametrize(
    "token",
    [
        "baton-harness",
        "baton_harness",
        "Baton",
        "BH_TOKEN",
        "BH_*",
        "BH_",
        "bh-*",
        "__BH_GITHUB_APP_ID__",
        "_BH_LOCAL",
        "BATON_HARNESS_DIR",
        ".bh",
        "bh-daemon",
    ],
)
def test_legacy_scanner(token: str) -> None:
    """Legacy families and embedded placeholders cannot escape scanning."""
    assert _audit().tokens(token)


def test_unapproved_reference_does_not_echo_runtime_values() -> None:
    """Diagnostics identify the token without disclosing surrounding data."""
    errors = _audit().audit({"x.env": "BH_TOKEN=secret-value"}, [])
    assert errors and "BH_TOKEN" in errors[0]
    assert "secret-value" not in str(errors)


@pytest.mark.parametrize(
    "text",
    [
        "Use bh-daemon differently.",
        "Use bh-daemon temporarily.\nUse bh-daemon temporarily.",
        "CodeReeve only.",
    ],
)
def test_changed_duplicated_or_stale_occurrence(text: str) -> None:
    """Exact text and count freeze the exception, not its whole file."""
    assert _audit().audit({"legacy.txt": text}, [_entry()])


@pytest.mark.parametrize(
    "updates",
    [
        {"category": "active"},
        {"reason": ""},
        {"count": 0},
        {"count": True},
        {"path": "*.txt"},
        {"unexpected": "value"},
    ],
)
def test_catalog_schema_rejects_invalid_allowances(
    updates: dict[str, object],
) -> None:
    """Malformed entries must fail closed."""
    assert _audit().audit({}, [_entry(**updates)])


def test_duplicate_allowance_rejected() -> None:
    """Repeated allowances must not double-authorize a reference."""
    assert _audit().audit({}, [_entry(), _entry()])


def test_exact_allowance_does_not_cover_new_branding() -> None:
    """An approved compatibility line does not bless another old name."""
    audit = _audit().audit
    assert not audit({"legacy.txt": "Use bh-daemon temporarily."}, [_entry()])
    assert audit(
        {"legacy.txt": "Use bh-daemon temporarily.\nBaton"}, [_entry()]
    )
    assert not audit({"x.txt": "generic harness and Symphony"}, [])


def test_legacy_filename_requires_separate_allowance() -> None:
    """Tracked paths are audited even when content is canonical or binary."""
    assert _audit().audit({"bh-old.bin": None}, [])
    allowance = _entry(path="bh-old.bin", text="bh-old.bin", kind="path")
    assert not _audit().audit({"bh-old.bin": None}, [allowance])


def test_tracked_repository_identity() -> None:
    """Audit actual tracked source with the reviewed exception catalog."""
    module = _audit()
    assert module.main([str(ROOT)]) == 0


def test_only_tracked_utf8_source_is_audited(tmp_path: Path) -> None:
    """Ignored and untracked runtime data cannot become audit input."""
    subprocess.run(
        ["git", "init", str(tmp_path)], check=True, capture_output=True
    )
    (tmp_path / ".gitignore").write_bytes(b"runtime/\n")
    (tmp_path / "tracked.txt").write_text("CodeReeve", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"\xff\x00")
    (tmp_path / "untracked.txt").write_text(
        "private runtime", encoding="utf-8"
    )
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime/private.env").write_text("private", encoding="utf-8")
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "add",
            ".gitignore",
            "tracked.txt",
            "binary.bin",
        ],
        check=True,
        capture_output=True,
    )
    assert _audit().tracked_files(tmp_path) == {
        ".gitignore": "runtime/\n",
        "tracked.txt": "CodeReeve",
        "binary.bin": None,
    }


def test_catalog_text_is_explicitly_excluded() -> None:
    """Catalog examples do not need self-allowances."""
    module = _audit()
    assert not module.audit({module.CATALOG: "Baton"}, [])
