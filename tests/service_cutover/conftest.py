"""Deterministic external-command fixtures for service cutover tests."""

from __future__ import annotations

import importlib
import subprocess
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import cast

import pytest

from codereeve.service_cutover.model import ServiceSpec


@pytest.fixture
def spec() -> ServiceSpec:
    """Return target POSIX paths without consulting Windows host services."""

    def path(value: str) -> Path:
        return cast(Path, PurePosixPath(value))

    return ServiceSpec(
        path("/srv/project"),
        path("/opt/new"),
        "runner",
        None,
        path("/etc/codereeve/secrets.env"),
        path("/home/runner"),
        timeout_s=2,
    )


def properties(
    name: str, *, active: bool = False, absent: bool = False
) -> dict[str, str]:
    """Supply complete systemctl properties, including empty values."""
    return {
        "Id": name,
        "LoadState": "not-found" if absent else "loaded",
        "ActiveState": "active" if active else "inactive",
        "SubState": "running" if active else "dead",
        "UnitFileState": "" if absent else "enabled",
        "MainPID": "51" if active else "0",
        "InvocationID": "a" * 32 if active else "",
        "ControlGroup": "/system.slice/" + name if active else "",
        "FragmentPath": "" if absent else "/etc/systemd/system/" + name,
        "DropInPaths": "",
        "KillMode": "" if absent else "control-group",
        "User": "" if absent else "runner",
        "Job": "",
        "ExecStart": ""
        if absent
        else (
            "{ path=/opt/old/bin/bh-daemon ; argv[]=/opt/old/bin/bh-daemon ; "
            "ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; "
            "pid=0 ; code=(null) ; status=0/0 }"
        ),
    }


class FakeRunner:
    """Respond only to supported commands while recording bounded argv."""

    def __init__(self) -> None:
        """Initialize two known units and explicit command result seams."""
        self.units = {
            "bh-daemon.service": properties("bh-daemon.service", active=True),
            "codereeve.service": properties("codereeve.service", absent=True),
        }
        self.calls: list[tuple[str, ...]] = []
        self.timeouts: list[float] = []
        self.failure: Exception | None = None
        self.transient_outputs: list[str] = []
        self.after_stop: Callable[[], None] = lambda: None
        self.after_show: Callable[[], None] = lambda: None
        self.after_start: Callable[[], None] = lambda: None
        self.corrupt_show: str | None = None
        self.returncode = 0
        self.fail_token: str | None = None

    def __call__(
        self, argv: tuple[str, ...], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        """Simulate systemd effects only inside the fixture."""
        self.calls.append(argv)
        self.timeouts.append(timeout)
        if self.failure:
            raise self.failure
        output = ""
        if argv[0] == "systemctl":
            if "show" in argv:
                name = argv[-1]
                values = self.units.get(name, properties(name, absent=True))
                output = (
                    self.corrupt_show
                    if self.corrupt_show is not None
                    else (
                        "\n".join(f"{k}={v}" for k, v in values.items()) + "\n"
                    )
                )
                self.after_show()
            elif "stop" in argv:
                name = argv[-1]
                if name in self.units:
                    self.units[name].update(
                        ActiveState="inactive",
                        SubState="dead",
                        MainPID="0",
                        ControlGroup="",
                    )
                self.after_stop()
            elif "start" in argv:
                self.units[argv[-1]].update(
                    ActiveState="active",
                    SubState="running",
                    MainPID="51",
                    InvocationID="a" * 32,
                    ControlGroup="/system.slice/" + argv[-1],
                )
                self.after_start()
        elif argv[0] == "systemd-run":
            output = self.transient_outputs.pop(0)
        elif argv[0] != "systemd-analyze":
            raise AssertionError("unexpected command")
        return subprocess.CompletedProcess(
            argv,
            1 if self.fail_token in argv else self.returncode,
            output,
            "secret-token",
        )


@pytest.fixture
def runner() -> FakeRunner:
    """Supply a fresh strict command double."""
    return FakeRunner()


@pytest.fixture
def backend(tmp_path: Path, runner: FakeRunner) -> object:
    """Create real filesystem inspection with only host authority injected."""
    assert importlib.util.find_spec("codereeve.service_cutover.systemd")
    module = importlib.import_module("codereeve.service_cutover.systemd")
    (tmp_path / "cgroup").mkdir()
    (tmp_path / "proc").mkdir()
    legacy_installation(tmp_path / "host")
    return module.SystemdBackend(
        runner=runner,
        cgroup_root=tmp_path / "cgroup",
        proc_root=tmp_path / "proc",
        platform_check=lambda: None,
        uid_for_user=lambda _: 1001,
        trusted_unit=lambda _: None,
        timeout_s=2,
        temporary_root=tmp_path,
        filesystem_root=tmp_path / "host",
    )


def legacy_installation(
    root: Path, *, shebang: str = "/opt/old/bin/python"
) -> Path:
    """Write a disposable legacy console-script and virtualenv marker."""
    environment = root / "opt/old"
    (environment / "bin").mkdir(parents=True, exist_ok=True)
    script = environment / "bin/bh-daemon"
    script.write_text(
        f"#!{shebang}\nimport sys\n"
        "from baton_harness.chain.cli import main\n"
        "if __name__ == '__main__':\n"
        "    if sys.argv[0].endswith('-script.pyw'):\n"
        "        sys.argv[0] = sys.argv[0][:-11]\n"
        "    elif sys.argv[0].endswith('.exe'):\n"
        "        sys.argv[0] = sys.argv[0][:-4]\n"
        "    sys.exit(main())\n",
        encoding="utf-8",
        newline="\n",
    )
    (environment / "bin/python").write_bytes(b"\x7fELFdisposable interpreter")
    (environment / "pyvenv.cfg").write_text(
        "home = /usr/bin\ninclude-system-site-packages = false\n",
        encoding="utf-8",
    )
    return script
