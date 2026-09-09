"""Canonical CodeReeve identity contracts for shell tools and resources."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = Path("C:/Program Files/Git/usr/bin/bash.exe")


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("bin/run-daemon.sh", "Launches the CodeReeve daemon"),
        ("bin/setup-env.sh", "installs the CodeReeve package"),
        ("bin/init-sandbox.sh", "CodeReeve daemon smoke test"),
    ],
)
def test_shell_help_uses_canonical_product_name(
    script: str, expected: str
) -> None:
    """Help output presents the canonical CodeReeve product name."""
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(
        [str(BASH.parent), os.environ.get("PATH", "")]
    )
    proc = subprocess.run(
        [str(BASH), str(ROOT / script), "--help"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert expected in proc.stdout
    assert "baton-harness" not in proc.stdout.lower()


def test_packaged_workflow_exactly_matches_canonical_source() -> None:
    """The packaged workflow mirrors its canonical source byte for byte."""
    source = (ROOT / "config/WORKFLOW.md").read_bytes()
    packaged = (ROOT / "src/codereeve/resources/WORKFLOW.md").read_bytes()

    assert packaged == source
    assert b"src/codereeve/hooks/force_pr_not_merge.py" in source
    assert b"Python-CodeReeve hooks" in source


@pytest.mark.parametrize("name", ["ruleset.main.json", "ruleset.feature.json"])
def test_ruleset_templates_use_canonical_placeholders(name: str) -> None:
    """Ruleset templates expose canonical internal placeholder names."""
    source = ROOT / "config" / name
    packaged = ROOT / "src/codereeve/resources" / name
    body = source.read_text(encoding="utf-8")

    assert packaged.read_bytes() == source.read_bytes()
    assert "__BH_" not in body
    assert "__CODEREEVE_" in body
    json.loads(body)


def test_conflicting_bootstrap_venvs_fail_before_interpreter_effect(
    tmp_path: Path,
) -> None:
    """Conflicting venv aliases fail before any configured Python can run."""
    marker = tmp_path / "python-ran"
    legacy_venv = tmp_path / "legacy"
    scripts = legacy_venv / "Scripts"
    scripts.mkdir(parents=True)
    fake_python = scripts / "python.exe"
    fake_python.write_text(
        f"#!/usr/bin/env bash\nprintf ran > '{marker.as_posix()}'\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    env = dict(os.environ)
    env.update(
        {
            "CODEREEVE_VENV": str(tmp_path / "canonical"),
            "BH_VENV": str(legacy_venv),
        }
    )

    proc = subprocess.run(
        [str(BASH), str(ROOT / "bin/lib/load-config.sh")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert proc.returncode != 0
    assert "codereeve: conflicting environment variables" in proc.stderr
    assert not marker.exists()
