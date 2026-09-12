"""Sandbox prompts reject malformed App identifiers before config writes."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _prompt(tmp_path: Path, answers: str) -> subprocess.CompletedProcess[str]:
    """Execute the production prompt function with piped terminal answers."""
    source = (ROOT / "bin/init-sandbox.sh").read_text(encoding="utf-8")
    function = source.split(
        "_codereeve_prompt_and_write_sandbox_config() {", 1
    )[1]
    function = function.split("\n}\n", 1)[0]
    # Only bypass the terminal-presence gate; all reads and writes stay real.
    function = function.replace(" || ! -t 0 || ! -t 1", "")
    script = (
        "export PATH=/usr/bin:/bin:$PATH\n"
        "_codereeve_prompt_and_write_sandbox_config() {" + function + "\n}\n"
    )
    script += (
        "if ! _codereeve_prompt_and_write_sandbox_config; then exit 1; fi\n"
    )
    env = dict(os.environ)
    env.update(
        CODEREEVE_SETUP_NO_PROMPT="0",
        CODEREEVE_PROJECT_ROOT=tmp_path.as_posix(),
        CODEREEVE_REPO_OWNER="example",
        CODEREEVE_REPO_NAME="sandbox",
    )
    bash = (
        "C:/Program Files/Git/usr/bin/bash.exe"
        if sys.platform == "win32"
        else "bash"
    )
    result = subprocess.run(
        [bash, "-c", script],
        input=answers.encode(),
        capture_output=True,
        env=env,
        cwd=tmp_path,
        check=False,
    )
    return subprocess.CompletedProcess(
        result.args,
        result.returncode,
        result.stdout.decode(),
        result.stderr.decode(),
    )


@pytest.mark.parametrize("invalid", ["", "0", "-1", "abc", "1.5", "01"])
@pytest.mark.parametrize("field", ["APP_ID", "APP_INSTALLATION_ID"])
def test_invalid_id_reprompts_before_writing(
    tmp_path: Path,
    invalid: str,
    field: str,
) -> None:
    """Invalid identifiers must be replaced before a config is written."""
    ids = (
        f"{invalid}\n123\n456\n"
        if field == "APP_ID"
        else f"123\n{invalid}\n456\n"
    )
    result = _prompt(tmp_path, ids + "file\n/secured/key.pem\n\n\n")
    assert result.returncode == 0, result.stderr
    assert (
        f"CODEREEVE_GITHUB_{field} must be a positive integer" in result.stderr
    )
    config = (tmp_path / ".codereeve/config.env").read_text()
    assert "export CODEREEVE_GITHUB_APP_ID=123\n" in config
    assert "export CODEREEVE_GITHUB_APP_INSTALLATION_ID=456\n" in config


@pytest.mark.parametrize(
    "answers", ["", "123\n", "invalid\n", "123\ninvalid\n"]
)
def test_id_prompt_eof_preserves_existing_config(
    tmp_path: Path, answers: str
) -> None:
    """EOF at either identifier must fail without overwriting config."""
    config = tmp_path / ".codereeve/config.env"
    config.parent.mkdir()
    config.write_text("existing config\n")
    result = _prompt(tmp_path, answers)
    assert result.returncode != 0
    assert config.read_text() == "existing config\n"
