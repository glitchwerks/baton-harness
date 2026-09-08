"""Tests for validated, side-effect-free systemd unit rendering."""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath
from typing import cast

import pytest

from codereeve.service_cutover import CutoverError, ServiceSpec, render_unit


def _path(value: str) -> Path:
    """Return a Linux target path without host-platform reinterpretation."""
    return cast(Path, PurePosixPath(value))


def _spec(**overrides: object) -> ServiceSpec:
    """Build a canonical service specification for one test."""
    values: dict[str, object] = {
        "project_root": _path("/srv/managed project"),
        "environment": _path("/opt/codereeve environment"),
        "run_user": "runner",
        "workflow": _path("/srv/managed project/WORKFLOW.md"),
        "secrets": _path("/etc/codereeve/secrets.env"),
        "home": _path("/home/runner home"),
    }
    values.update(overrides)
    return ServiceSpec(**values)  # type: ignore[arg-type]


def test_render_unit_emits_canonical_service_contract() -> None:
    """Catch legacy identity, executable, ordering, or cgroup regressions."""
    text = render_unit(_spec())

    assert text == (
        "[Unit]\n"
        "Description=CodeReeve daemon\n"
        "After=network.target bh-daemon.service\n"
        "Conflicts=bh-daemon.service\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "User=runner\n"
        "WorkingDirectory=/srv/managed project\n"
        'Environment="CODEREEVE_PROJECT_ROOT=/srv/managed project"\n'
        'Environment="HOME=/home/runner home"\n'
        'Environment="PATH=/opt/codereeve environment/bin:'
        '/usr/local/bin:/usr/bin:/bin"\n'
        "EnvironmentFile=/etc/codereeve/secrets.env\n"
        'ExecStart=":/opt/codereeve environment/bin/codereeve" '
        '"daemon" "--workflow" "/srv/managed project/WORKFLOW.md"\n'
        "KillMode=control-group\n"
        "TimeoutStopSec=120\n"
        "Restart=on-failure\n"
        "RestartSec=15\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    assert "bh-daemon --workflow" not in text


def test_render_unit_omits_optional_files_when_absent() -> None:
    """Catch accidental empty workflow, secrets, or gate directives."""
    text = render_unit(_spec(workflow=None, secrets=None))

    exec_start = next(
        line for line in text.splitlines() if line.startswith("ExecStart=")
    )
    assert exec_start == (
        'ExecStart=":/opt/codereeve environment/bin/codereeve" "daemon"'
    )
    assert "EnvironmentFile=" not in text
    assert "CODEREEVE_CUTOVER_GATE" not in text


def test_render_unit_uses_directive_specific_systemd_escaping() -> None:
    """Catch scalar/token parsing and expansion-context regressions."""
    spec = _spec(
        project_root=_path('/srv/managed "root" 100% $cash'),
        environment=_path('/opt/env "quoted" 25% $money'),
        workflow=_path('/srv/flow \\ file 50% "$draft".md'),
        secrets=_path('/etc/secret \\ file 75% "$token".env'),
        home=_path('/home/back \\ "quoted" 10% $owner'),
    )
    gate = _path('/run/gate \\ "receipt" 5% $pid.json')

    text = render_unit(spec, gate=gate)

    assert 'WorkingDirectory=/srv/managed "root" 100%% $cash' in text
    assert (
        'Environment="HOME=/home/back \\\\ \\"quoted\\" 10%% $owner"' in text
    )
    assert 'EnvironmentFile=/etc/secret \\ file 75%% "$token".env' in text
    assert (
        'ExecStart=":/opt/env \\"quoted\\" 25%% $money/bin/codereeve" '
        '"daemon" "--workflow" '
        '"/srv/flow \\\\ file 50%% \\"$draft\\".md"' in text
    )
    assert (
        'Environment="CODEREEVE_CUTOVER_GATE=/run/gate \\\\ '
        '\\"receipt\\" 5%% $pid.json"' in text
    )


@pytest.mark.parametrize(
    "field",
    ["project_root", "environment", "workflow", "secrets", "home"],
)
def test_service_spec_rejects_relative_paths(field: str) -> None:
    """Catch relative paths that would depend on systemd's process context."""
    with pytest.raises(CutoverError, match="service path must be absolute"):
        _spec(**{field: _path("relative/path")})


@pytest.mark.parametrize("field", ["project_root", "environment", "home"])
def test_service_spec_rejects_missing_required_paths(field: str) -> None:
    """Catch invalid specifications reaching later backend consumers."""
    with pytest.raises(CutoverError, match="service path must be absolute"):
        _spec(**{field: None})


@pytest.mark.parametrize(
    "bad",
    ["bad\npath", "bad\rpath", "bad\x00path", "bad\tpath"],
)
def test_service_spec_rejects_path_line_injection(bad: str) -> None:
    """Catch paths that could add unit directives or truncate data."""
    with pytest.raises(
        CutoverError, match="service path contains control data"
    ):
        _spec(project_root=_path(f"/srv/{bad}"))


@pytest.mark.parametrize(
    "bad_user",
    [
        "",
        "two users",
        "root\nExecStart=/bin/false",
        "runner%u",
        "$USER",
        "a/b",
    ],
)
def test_service_spec_rejects_user_injection(bad_user: str) -> None:
    """Catch User directive injection and systemd substitutions."""
    with pytest.raises(CutoverError, match="service user is invalid") as exc:
        _spec(run_user=bad_user)
    if bad_user:
        assert bad_user not in str(exc.value)


@pytest.mark.parametrize("bad_user", [None, 7])
def test_service_spec_rejects_non_string_user(bad_user: object) -> None:
    """Catch raw regex TypeError escaping the fixed diagnostic contract."""
    with pytest.raises(CutoverError, match="service user is invalid"):
        _spec(run_user=bad_user)


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), float("inf")])
def test_service_spec_rejects_non_positive_or_non_finite_timeout(
    timeout: float,
) -> None:
    """Catch unbounded or immediately expired stop verification windows."""
    with pytest.raises(CutoverError, match="service timeout is invalid"):
        _spec(timeout_s=timeout)


@pytest.mark.parametrize("bad", [_path("relative/gate"), _path("/bad\ngate")])
def test_render_unit_rejects_invalid_gate_path(bad: Path) -> None:
    """Catch optional gate injection before any unit text is returned."""
    with pytest.raises(CutoverError, match="service (path|gate)"):
        render_unit(_spec(), gate=bad)


def test_render_unit_rejects_windows_target_path() -> None:
    """Catch accidentally emitting a Windows drive path into a Linux unit."""
    with pytest.raises(CutoverError, match="service path must be absolute"):
        _spec(environment=Path("C:/codereeve-env"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_root", "/srv/trailing-space "),
        ("project_root", "/srv/trailing-backslash\\"),
        ("secrets", "/etc/codereeve/*.env"),
        ("secrets", "/etc/codereeve/secret?.env"),
        ("secrets", "/etc/codereeve/secret[12].env"),
    ],
)
def test_render_unit_rejects_unrepresentable_raw_scalar_path(
    field: str, value: str
) -> None:
    """Catch silent target changes in raw scalar systemd directives."""
    with pytest.raises(CutoverError, match="service path cannot be rendered"):
        render_unit(_spec(**{field: _path(value)}))


def test_render_unit_has_no_process_or_filesystem_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch attempts to activate services or inspect/write supplied paths."""

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("pure rendering attempted an external effect")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)

    text = render_unit(_spec(), gate=_path("/run/cutover-gate.json"))

    assert text.startswith("[Unit]\nDescription=CodeReeve daemon\n")
