"""Exercise the shell bridge protocol and non-execution boundary."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _shell_path(path: Path) -> str:
    """Represent a fixture path for Bash on Windows or POSIX."""
    value = path.as_posix()
    if sys.platform == "win32":
        return "/" + value[0].lower() + value[2:]
    return value


def _security_env(tmp_path: Path) -> dict[str, str]:
    """Isolate loader inputs and use the working Git Bash toolchain."""
    from tests.test_load_config_export_visibility import _BASH_BIN_DIR

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("BH_", "CODEREEVE_"))
    }
    env.pop("BATON_HARNESS_DIR", None)
    env["XDG_CONFIG_HOME"] = (tmp_path / "xdg").as_posix()
    env["PATH"] = os.pathsep.join([_BASH_BIN_DIR, env.get("PATH", "")])
    return env


def test_conflicting_venv_aliases_never_execute_interpreter(
    tmp_path: Path,
) -> None:
    """Reject divergent bootstrap aliases before invoking either venv."""
    from tests.test_load_config_export_visibility import _BASH

    sentinel = tmp_path / "executed"
    interpreter = tmp_path / "venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text(
        f'#!/bin/bash\n: > "{_shell_path(sentinel)}"\nexit 1\n',
        encoding="utf-8",
        newline="\n",
    )
    interpreter.chmod(0o755)
    env = _security_env(tmp_path)
    env["CODEREEVE_VENV"] = interpreter.parent.parent.as_posix()
    env["BH_VENV"] = "different-private-value"
    proc = subprocess.run(
        [
            _BASH,
            "-c",
            'source "$1"',
            "bash",
            (ROOT / "bin/lib/load-config.sh").as_posix(),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert not sentinel.exists()
    assert "CODEREEVE_VENV" in proc.stderr and "BH_VENV" in proc.stderr
    assert "different-private-value" not in proc.stderr


def test_cleanup_precedes_exporting_configured_path(tmp_path: Path) -> None:
    """A configured PATH cannot replace the loader's rm cleanup command."""
    from tests.test_load_config_export_visibility import _BASH

    sentinel = tmp_path / "executed"
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    fake_rm = fakebin / "rm"
    fake_rm.write_text(
        f'#!/bin/bash\n: > "{_shell_path(sentinel)}"\n',
        encoding="utf-8",
        newline="\n",
    )
    fake_rm.chmod(0o755)
    env = _security_env(tmp_path)
    host = tmp_path / "xdg" / "codereeve" / "host.env"
    host.parent.mkdir(parents=True)
    host.write_text(f"PATH={_shell_path(fakebin)}\n", encoding="utf-8")
    # Windows synthesizes PATH for a native child even after Bash unexports
    # it. Remove that operator layer so the real parser selects file PATH.
    interpreter = tmp_path / "venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text(
        "#!/bin/bash\n"
        f'exec "{Path(sys.executable).as_posix()}" -c '
        "'import os,sys; from codereeve.config_bridge import main; "
        'os.environ.pop("PATH",None); '
        'raise SystemExit(main(sys.argv[3:]))\' "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    interpreter.chmod(0o755)
    env["CODEREEVE_VENV"] = interpreter.parent.parent.as_posix()
    records = tmp_path / "records"
    records.mkdir()
    proc = subprocess.run(
        [
            _BASH,
            "-c",
            'export -n PATH; export TMPDIR="$2"; source "$1"',
            "bash",
            (ROOT / "bin/lib/load-config.sh").as_posix(),
            _shell_path(records),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert not sentinel.exists()
    assert list(records.iterdir()) == []


def test_truncated_protocol_never_exports_partial_config(
    tmp_path: Path,
) -> None:
    """Even a broken bridge process cannot publish an incomplete record set."""
    from tests.test_load_config_export_visibility import _BASH

    interpreter = tmp_path / "venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text(
        "#!/bin/bash\nprintf 'NEW_VALUE\\0changed\\0INCOMPLETE'\n",
        encoding="utf-8",
        newline="\n",
    )
    interpreter.chmod(0o755)
    env = _security_env(tmp_path)
    env["CODEREEVE_VENV"] = interpreter.parent.parent.as_posix()
    records = tmp_path / "records"
    records.mkdir()
    proc = subprocess.run(
        [
            _BASH,
            "-c",
            'export TMPDIR="$2"; source "$1"; '
            'status=$?; printf "%s" "${NEW_VALUE-unset}"; exit "$status"',
            "bash",
            (ROOT / "bin/lib/load-config.sh").as_posix(),
            _shell_path(records),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert proc.stdout == "unset"
    assert list(records.iterdir()) == []


def test_readonly_destination_never_leaves_partial_exports(
    tmp_path: Path,
) -> None:
    """Readonly destinations fail before changing any other variable."""
    from tests.test_load_config_export_visibility import _BASH

    env = _security_env(tmp_path)
    host = tmp_path / "xdg" / "codereeve" / "host.env"
    host.parent.mkdir(parents=True)
    host.write_text("AAA_NEW=changed\nZZZ_READONLY=new\n", encoding="utf-8")
    records = tmp_path / "records"
    records.mkdir()
    proc = subprocess.run(
        [
            _BASH,
            "-c",
            'export TMPDIR="$2"; readonly ZZZ_READONLY=old; '
            'source "$1"; status=$?; '
            'printf "%s:%s" "${AAA_NEW-unset}" "$ZZZ_READONLY"; '
            'exit "$status"',
            "bash",
            (ROOT / "bin/lib/load-config.sh").as_posix(),
            _shell_path(records),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert proc.stdout == "unset:old"
    assert list(records.iterdir()) == []


def test_cleanup_never_receives_configured_loader_environment(
    tmp_path: Path,
) -> None:
    """No cleanup command runs with a newly configured LD_PRELOAD."""
    from tests.test_load_config_export_visibility import _BASH

    env = _security_env(tmp_path)
    env.pop("LD_PRELOAD", None)
    host = tmp_path / "xdg" / "codereeve" / "host.env"
    host.parent.mkdir(parents=True)
    host.write_text("LD_PRELOAD=untrusted-library\n", encoding="utf-8")
    sentinel = tmp_path / "executed"
    records = tmp_path / "records"
    records.mkdir()
    proc = subprocess.run(
        [
            _BASH,
            "-c",
            'export TMPDIR="$2"; '
            'rm() { if [[ ${LD_PRELOAD+x} ]]; then : > "$sentinel"; fi; '
            '(unset LD_PRELOAD; command rm "$@"); }; '
            'sentinel="$3"; source "$1"',
            "bash",
            (ROOT / "bin/lib/load-config.sh").as_posix(),
            _shell_path(records),
            _shell_path(sentinel),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert not sentinel.exists()
    assert list(records.iterdir()) == []


@pytest.mark.parametrize(
    "config,diagnostic",
    [
        ("INVALID=$(touch sentinel)\n", "host.env:1"),
        ("CODEREEVE_REPO_OWNER=one\nBH_REPO_OWNER=two\n", "conflicting"),
    ],
)
def test_probe_stops_before_commands_on_invalid_configuration(
    tmp_path: Path,
    config: str,
    diagnostic: str,
) -> None:
    """The probe must propagate loader failure despite not using errexit."""
    from tests.test_load_config_export_visibility import _BASH

    env = _security_env(tmp_path)
    host = tmp_path / "xdg" / "codereeve" / "host.env"
    host.parent.mkdir(parents=True)
    host.write_text(config, encoding="utf-8")
    sentinel = tmp_path / "first-command"
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    first_command = fakebin / "wc"
    first_command.write_text(
        f'#!/bin/bash\n: > "{_shell_path(sentinel)}"\nprintf "12\\n"\n',
        encoding="utf-8",
        newline="\n",
    )
    first_command.chmod(0o755)
    env["PATH"] = os.pathsep.join([fakebin.as_posix(), env["PATH"]])
    token = tmp_path / "token"
    token.write_text("fixture-token", encoding="utf-8")
    env.update(
        CODEREEVE_PROBE_SANDBOX_REPO="owner/repo",
        CODEREEVE_PROBE_PR_NUMBER="1",
        CODEREEVE_PROBE_WORKER_TOKEN_PATH=token.as_posix(),
        CODEREEVE_PROBE_DRY_RUN="1",
    )
    proc = subprocess.run(
        [_BASH, (ROOT / "bin/probe-merge-denial.sh").as_posix()],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert not sentinel.exists()
    assert diagnostic in proc.stderr


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
