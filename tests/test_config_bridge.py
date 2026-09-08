"""Exercise the shell bridge protocol and non-execution boundary."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _bridge(
    tmp_path: Path, text: str, **values: str
) -> subprocess.CompletedProcess[bytes]:
    """Run the real module with isolated host and managed sources."""
    host = tmp_path / "host.env"
    host.write_text(text, encoding="utf-8")
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("BH_", "CODEREEVE_"))
    }
    env.pop("BATON_HARNESS_DIR", None)
    env.update(values)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "codereeve.config_bridge",
            "--format",
            "nul",
            "--host",
            str(host),
            "--managed",
            str(tmp_path / "managed.env"),
        ],
        env=env,
        capture_output=True,
    )


def test_bridge_emits_aliases_empty_and_third_party(tmp_path: Path) -> None:
    """Resolved values survive the byte protocol intact."""
    proc = _bridge(
        tmp_path, 'BH_REPO_OWNER="héllo world"\nBWS_PEM_SECRET_ID=\n'
    )
    assert proc.returncode == 0, proc.stderr
    assert b"CODEREEVE_REPO_OWNER\0h\xc3\xa9llo world\0" in proc.stdout
    assert b"BH_REPO_OWNER\0h\xc3\xa9llo world\0" in proc.stdout
    assert b"BWS_PEM_SECRET_ID\0\0" in proc.stdout


@pytest.mark.parametrize(
    "unsafe",
    [
        "$(touch sentinel)",
        "`touch sentinel`",
        "x > sentinel",
        "x; touch sentinel",
    ],
)
def test_bridge_error_emits_no_partial_records(
    tmp_path: Path, unsafe: str
) -> None:
    """Unsafe input suppresses even records parsed before the bad line."""
    proc = _bridge(tmp_path, f"GH_TOKEN=private\nBAD={unsafe}\n")
    assert proc.returncode == 1
    assert proc.stdout == b""
    assert proc.stderr.decode().strip() == f"{tmp_path / 'host.env'}:2"


def test_bridge_cross_spelling_conflict_is_not_override(
    tmp_path: Path,
) -> None:
    """Conflicting canonical and legacy values fail closed."""
    proc = _bridge(
        tmp_path,
        "BH_REPO_OWNER=secret-file\n",
        CODEREEVE_REPO_OWNER="secret-env",
    )
    assert proc.returncode == 1
    assert proc.stdout == b""
    assert b"secret" not in proc.stderr
    assert b"CODEREEVE_REPO_OWNER" in proc.stderr


def test_bridge_rejects_coexisting_host_paths(tmp_path: Path) -> None:
    """Default host discovery refuses canonical and legacy files together."""
    for name in ("codereeve", "baton-harness"):
        folder = tmp_path / "xdg" / name
        folder.mkdir(parents=True)
        (folder / "host.env").write_text("", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "codereeve.config_bridge", "--format", "nul"],
        env={**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "xdg")},
        capture_output=True,
    )
    assert proc.returncode == 1
    assert proc.stdout == b""


@pytest.mark.parametrize(
    "unsafe",
    [
        "$(touch sentinel)",
        "`touch sentinel`",
        "x > sentinel",
        "x; touch sentinel",
    ],
)
@pytest.mark.parametrize("layer", ["host", "managed"])
def test_shell_loader_never_executes_config(
    tmp_path: Path, unsafe: str, layer: str
) -> None:
    """Bash rejects shell code without creating its sentinel."""
    from tests.test_load_config_export_visibility import _BASH, _BASH_BIN_DIR

    host = tmp_path / "xdg" / "baton-harness" / "host.env"
    host.parent.mkdir(parents=True)
    project = tmp_path / "project"
    bad = host if layer == "host" else project / ".codereeve" / "config.env"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(f"BAD={unsafe}\n", encoding="utf-8")
    env = dict(os.environ, XDG_CONFIG_HOME=host.parent.parent.as_posix())
    env["CODEREEVE_PROJECT_ROOT"] = project.as_posix()
    env["PATH"] = os.pathsep.join([_BASH_BIN_DIR, env.get("PATH", "")])
    proc = subprocess.run(
        [
            _BASH,
            "-c",
            'source "$1"',
            "bash",
            (ROOT / "bin/lib/load-config.sh").as_posix(),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert not (tmp_path / "sentinel").exists()
    assert proc.stderr.strip().replace("\\", "/") == f"{bad.as_posix()}:1"


def test_bridge_reserves_shell_internal_names(tmp_path: Path) -> None:
    """Config cannot overwrite the loader's temporary output filename."""
    proc = _bridge(tmp_path, "_codereeve_records=important-file\n")
    assert proc.returncode == 1
    assert proc.stdout == b""


def test_bridge_carries_legacy_runtime_state_selection(tmp_path: Path) -> None:
    """Shell recovery probes observe the same state directory as the daemon."""
    project = tmp_path / "project"
    state = project / ".baton-harness"
    state.mkdir(parents=True)
    proc = _bridge(tmp_path, "", CODEREEVE_PROJECT_ROOT=project.as_posix())
    assert proc.returncode == 0, proc.stderr
    expected = (
        b"_codereeve_state_directory\0" + state.as_posix().encode() + b"\0"
    )
    assert expected in proc.stdout


@pytest.mark.parametrize("layout", ["Scripts/python.exe", "bin/python"])
def test_loader_uses_selected_venv_interpreter(
    tmp_path: Path, layout: str
) -> None:
    """Both supported venv layouts invoke the bridge with intact arguments."""
    from tests.test_load_config_export_visibility import _BASH, _BASH_BIN_DIR

    # Executable scripts work under Bash in either venv layout, including
    # the .exe filename on Git Bash; the wrapper invokes real Python.
    interpreter = tmp_path / "venv" / layout
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text(
        "#!/usr/bin/env bash\n"
        f'exec "{Path(sys.executable).as_posix()}" "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    interpreter.chmod(0o755)
    env = dict(
        os.environ,
        CODEREEVE_VENV=interpreter.parent.parent.as_posix(),
        XDG_CONFIG_HOME=(tmp_path / "xdg").as_posix(),
    )
    env["PATH"] = os.pathsep.join([_BASH_BIN_DIR, env.get("PATH", "")])
    env["BH_REPO_OWNER"] = "same"
    proc = subprocess.run(
        [
            _BASH,
            "-c",
            'source "$1" && printf "%s" "$CODEREEVE_REPO_OWNER"',
            "bash",
            (ROOT / "bin/lib/load-config.sh").as_posix(),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "same"


@pytest.mark.parametrize(
    "canonical,legacy,expected",
    [
        ("same", "same", 0),
        ("", "", 0),
        ("", "other", 1),
        ("same", "different", 1),
    ],
)
def test_bridge_exact_pair_after_layer_precedence(
    tmp_path: Path, canonical: str, legacy: str, expected: int
) -> None:
    """Both spellings resolve independently, preserving empties."""
    (tmp_path / "managed.env").write_text(
        f"BH_REPO_OWNER={legacy}\n", encoding="utf-8"
    )
    proc = _bridge(
        tmp_path, "CODEREEVE_REPO_OWNER=host\n", CODEREEVE_REPO_OWNER=canonical
    )
    assert proc.returncode == expected
    if expected == 0:
        assert (
            b"CODEREEVE_REPO_OWNER\0" + canonical.encode() + b"\0"
            in proc.stdout
        )
    else:
        assert proc.stdout == b""
