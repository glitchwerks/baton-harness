"""Fail-closed doctor and process heartbeat evidence tests."""

from __future__ import annotations

import importlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest

from codereeve.chain.doctor import CATALOG, Phase
from codereeve.service_cutover.model import CutoverError


def health_module() -> ModuleType:
    """Load the implementation after asserting the capability exists."""
    assert importlib.util.find_spec("codereeve.service_cutover.health")
    return importlib.import_module("codereeve.service_cutover.health")


def report(phases: tuple[Phase, ...]) -> dict[str, object]:
    """Build public catalog-shaped evidence independently of the validator."""
    checks = [
        {
            "id": c.check_id,
            "phase": c.phase.value,
            "severity": c.severity.value,
            "status": "pass",
            "title": "",
            "detail": "",
            "remediation": "",
        }
        for c in CATALOG
        if c.phase in phases
    ]
    return {
        "schema_version": 1,
        "selected_phases": [p.value for p in phases],
        "provenance": {
            "package_version": "0.3.0",
            "source_revision": "a" * 40,
            "lock_identity": "sha256:" + "b" * 64,
            "development": False,
        },
        "summary": {
            "pass": len(checks),
            "fail": 0,
            "warn": 0,
            "skip": 0,
            "critical_failures": 0,
        },
        "checks": checks,
    }


@pytest.mark.parametrize(
    "phases", [(Phase.INSTALLATION, Phase.CONFIGURATION), (Phase.LIVE,)]
)
def test_complete_doctor_evidence_accepts_catalog(
    phases: tuple[Phase, ...],
) -> None:
    """Catch rejection of a complete strict installed report."""
    result = health_module().validate_doctor(
        json.dumps(report(phases)), phases
    )
    assert result.source_revision == "a" * 40


@pytest.mark.parametrize(
    "mutation",
    [
        "empty",
        "missing",
        "duplicate",
        "skip",
        "phase",
        "severity",
        "unknown",
        "schema",
        "summary",
        "development",
        "provenance",
        "boolean-schema",
    ],
)
def test_doctor_refuses_incomplete_or_forged_evidence(mutation: str) -> None:
    """Catch zero-exit reports that omit or forge required evidence."""
    phases = (Phase.INSTALLATION, Phase.CONFIGURATION)
    raw = report(phases)
    checks = raw["checks"]
    assert isinstance(checks, list)
    if mutation == "empty":
        raw["selected_phases"] = []
    elif mutation == "missing":
        checks.pop()
    elif mutation == "duplicate":
        checks.append(checks[0])
    elif mutation in {"phase", "severity", "skip", "unknown"}:
        checks[0][
            {"skip": "status", "unknown": "id"}.get(mutation, mutation)
        ] = "skip" if mutation == "skip" else "invalid"
    elif mutation == "schema":
        raw["schema_version"] = 2
    elif mutation == "boolean-schema":
        raw["schema_version"] = True
    elif mutation == "summary":
        raw["summary"] = {}
    elif mutation == "development":
        assert isinstance(raw["provenance"], dict)
        raw["provenance"]["development"] = True
    else:
        raw["provenance"] = None
    with pytest.raises(CutoverError):
        health_module().validate_doctor(json.dumps(raw), phases)


@pytest.mark.parametrize(
    "raw", ["secret-token", "[]", '{"schema_version":1,"schema_version":1}']
)
def test_invalid_json_is_value_free(raw: str) -> None:
    """Catch raw subprocess contents escaping diagnostics."""
    with pytest.raises(CutoverError) as exc:
        health_module().validate_doctor(raw, (Phase.LIVE,))
    assert raw not in str(exc.value)
    assert exc.value.__cause__ is None


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "pid",
        "invocation",
        "missing",
        "stale",
        "future",
        "naive",
        "boolean-pid",
        "schema",
    ],
)
def test_heartbeat_is_bound_to_process_and_time(mutation: str | None) -> None:
    """Reject old, future, malformed, or different-process heartbeats."""
    raw = {
        "schema_version": 1,
        "pid": 51,
        "invocation_id": "a" * 32,
        "timestamp": "2026-09-08T12:00:05+00:00",
    }
    changes: dict[str, tuple[str, object]] = {
        "pid": ("pid", 52),
        "invocation": ("invocation_id", "b" * 32),
        "stale": ("timestamp", "2026-09-08T11:59:59+00:00"),
        "future": ("timestamp", "2026-09-08T12:00:20+00:00"),
        "naive": ("timestamp", "2026-09-08T12:00:05"),
        "boolean-pid": ("pid", True),
        "schema": ("schema_version", 2),
    }
    if mutation == "missing":
        raw.pop("invocation_id")
    elif mutation:
        key, value = changes[mutation]
        raw[key] = value
    args = dict(
        pid=51,
        invocation_id="a" * 32,
        started_at=datetime(2026, 9, 8, 12, tzinfo=timezone.utc),
        now=datetime(2026, 9, 8, 12, 0, 10, tzinfo=timezone.utc),
    )
    if mutation:
        with pytest.raises(CutoverError):
            health_module().validate_heartbeat(json.dumps(raw), **args)
    else:
        assert health_module().validate_heartbeat(json.dumps(raw), **args) == (
            datetime(2026, 9, 8, 12, 0, 5, tzinfo=timezone.utc)
        )


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "canonical",
        "home",
        "path",
        "project",
        "gate",
        "unreadable",
        "unwritable",
        "override",
    ],
)
def test_service_probe_checks_actual_access_and_effective_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str | None,
) -> None:
    """Reject caller context, legacy post-start paths, and denied access."""
    module = health_module()
    assert hasattr(module, "probe_service_context")
    home, project, environment = (
        tmp_path / "home",
        tmp_path / "project",
        tmp_path / "env",
    )
    home.mkdir()
    project.mkdir()
    state = project / ".baton-harness"
    state.mkdir()
    (project / ".bh").mkdir()
    config = project / ".bh/config.env"
    config.write_text(
        (
            "CODEREEVE_GITHUB_APP_KEY_PROVIDER=bws\nBH_REPO_"
            "OWNER=my-org\nBH_REPO_NAME=repo\nBH_GITHUB_APP_I"
            "D=123\nBH_GITHUB_APP_INSTALLATION_ID=456\nBWS_PE"
            "M_SECRET_ID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeee"
            "ee\n"
        ),
        encoding="utf-8",
    )
    values = {
        "HOME": home.as_posix(),
        "CODEREEVE_PROJECT_ROOT": project.as_posix(),
        "PATH": environment.as_posix()
        + "/bin:"
        + home.as_posix()
        + "/.local/bin:/usr/local/bin:/usr/bin:/bin",
    }
    if mutation in {"home", "path", "project", "gate"}:
        values[
            {
                "home": "HOME",
                "path": "PATH",
                "project": "CODEREEVE_PROJECT_ROOT",
                "gate": "CODEREEVE_CUTOVER_GATE",
            }[mutation]
        ] = "wrong-secret-value"
    if mutation == "override":
        values["CODEREEVE_HEARTBEAT_FILE"] = (
            project / "custom-heartbeat"
        ).as_posix()
    monkeypatch.setattr(os, "environ", values)
    monkeypatch.chdir(project)
    monkeypatch.setattr(module, "_service_identity", lambda _: 1001)
    if mutation == "unreadable":
        config.unlink()
    if mutation == "unwritable":
        monkeypatch.setattr(os, "access", lambda *_a, **_kw: False)
    args = (
        project.as_posix(),
        environment.as_posix(),
        "runner",
        home.as_posix(),
        "canonical" if mutation == "canonical" else "compatible",
        "",
    )
    if mutation and mutation != "override":
        with pytest.raises(CutoverError) as exc:
            module.probe_service_context(*args)
        assert "wrong-secret-value" not in str(exc.value)
    else:
        result = module.probe_service_context(*args)
        assert result["uid"] == 1001
        expected = (
            project / "custom-heartbeat.identity.json"
            if mutation == "override"
            else state / "heartbeat.identity.json"
        )
        assert result["heartbeat_path"] == expected.as_posix()
        assert expected.as_posix() in result["runtime_paths"]
        assert (state / "session-report.json").as_posix() in result[
            "runtime_paths"
        ]
