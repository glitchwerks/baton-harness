"""Strict, value-free validation of service verification evidence."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from datetime import datetime, timedelta
from pathlib import Path

from codereeve.chain.doctor import CATALOG, CheckStatus, Phase, Severity
from codereeve.provenance import Provenance, validate_provenance

from .model import CutoverError, service_path


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON keys rather than silently overwriting evidence."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def evidence_document(text: str) -> dict[str, object]:
    """Decode a bounded JSON object without leaking untrusted input.

    Args:
        text: Captured verification output, potentially containing secrets.

    Returns:
        An object with unique keys.

    Raises:
        CutoverError: If the evidence is malformed or too large.
    """
    try:
        if len(text) > 1_048_576:
            raise ValueError
        raw = json.loads(text, object_pairs_hook=_unique_object)
        if not isinstance(raw, dict):
            raise ValueError
        return raw
    except (ValueError, TypeError, RecursionError):
        raise CutoverError("verification evidence is invalid") from None


def validate_doctor(text: str, phases: tuple[Phase, ...]) -> Provenance:
    """Require the complete public catalog and passing critical checks.

    Args:
        text: JSON emitted by the installed strict doctor command.
        phases: Exact requested phase sequence.

    Returns:
        Validated non-development artifact provenance.

    Raises:
        CutoverError: If metadata, coverage, counts, or critical checks fail.
    """
    try:
        raw = evidence_document(text)
        if (
            type(raw.get("schema_version")) is not int
            or raw["schema_version"] != 1
            or not phases
            or raw.get("selected_phases") != [p.value for p in phases]
        ):
            raise ValueError
        expected = {c.check_id: c for c in CATALOG if c.phase in phases}
        checks = raw.get("checks")
        if not isinstance(checks, list) or len(checks) != len(expected):
            raise ValueError
        seen: set[str] = set()
        counts = {s.value: 0 for s in CheckStatus}
        for result in checks:
            if not isinstance(result, dict):
                raise ValueError
            check_id = result.get("id")
            if not isinstance(check_id, str) or check_id in seen:
                raise ValueError
            check = expected[check_id]
            status = CheckStatus(result["status"])
            if (
                result.get("phase") != check.phase.value
                or result.get("severity") != check.severity.value
                or (
                    check.severity == Severity.CRITICAL
                    and status != CheckStatus.PASS
                )
            ):
                raise ValueError
            seen.add(check_id)
            counts[status.value] += 1
        counts["critical_failures"] = 0
        summary = raw.get("summary")
        if (
            not isinstance(summary, dict)
            or summary != counts
            or any(type(v) is not int for v in summary.values())
        ):
            raise ValueError
        provenance = raw.get("provenance")
        if not isinstance(provenance, dict):
            raise ValueError
        version = provenance.get("package_version")
        if not isinstance(version, str) or not version:
            raise ValueError
        record = validate_provenance(
            {"schema_version": 1, **provenance}, version
        )
        if record.development:
            raise ValueError
        return record
    except (ValueError, TypeError, KeyError):
        raise CutoverError("strict doctor evidence failed") from None


def validate_heartbeat(
    text: str,
    *,
    pid: int,
    invocation_id: str,
    started_at: datetime,
    now: datetime,
    future_tolerance_s: float = 2.0,
    max_age_s: float = 60.0,
) -> datetime:
    """Bind a recent heartbeat to one systemd runtime cycle and PID.

    Args:
        text: Structured heartbeat sidecar contents.
        pid: Freshly observed main process identifier.
        invocation_id: Freshly observed systemd invocation identity.
        started_at: Earliest acceptable timestamp for this start operation.
        now: Current wall clock, with timezone.
        future_tolerance_s: Maximum tolerated clock skew into the future.
        max_age_s: Maximum heartbeat age independent of start time.

    Returns:
        The validated timezone-aware heartbeat timestamp.

    Raises:
        CutoverError: If identity, schema, or freshness is unproven.
    """
    try:
        raw = evidence_document(text)
        if (
            set(raw) != {"schema_version", "pid", "invocation_id", "timestamp"}
            or type(raw["schema_version"]) is not int
            or raw["schema_version"] != 1
            or type(raw["pid"]) is not int
            or raw["pid"] != pid
            or pid <= 0
            or not re.fullmatch(r"[0-9a-f]{32}", invocation_id)
            or raw["invocation_id"] != invocation_id
            or not isinstance(raw["timestamp"], str)
        ):
            raise ValueError
        timestamp = datetime.fromisoformat(raw["timestamp"])
        if (
            timestamp.tzinfo is None
            or started_at.tzinfo is None
            or now.tzinfo is None
            or timestamp < started_at
            or timestamp < now - timedelta(seconds=max_age_s)
            or timestamp > now + timedelta(seconds=future_tolerance_s)
        ):
            raise ValueError
        return timestamp
    except (TypeError, ValueError, OverflowError):
        raise CutoverError("fresh process heartbeat is unproven") from None


def _service_identity(user: str) -> int:
    """Require execution as the requested non-root service account."""
    if sys.platform == "win32":
        raise CutoverError("Linux service identity required")
    import pwd

    uid = int(pwd.getpwnam(user).pw_uid)
    if uid <= 0 or os.geteuid() != uid or os.getuid() != uid:
        raise CutoverError("service identity is unproven")
    return uid


def _writable_path(path: Path) -> None:
    """Require safe paths writable by the current account."""
    from codereeve.paths import validate_safe_file_path

    validate_safe_file_path(path, label="service state")
    if path.exists() and not os.access(path, os.R_OK | os.W_OK):
        raise ValueError
    parent = path.parent
    while not parent.exists():
        parent = parent.parent
    if not os.access(parent, os.W_OK | os.X_OK):
        raise ValueError


def probe_service_context(
    project: str,
    environment: str,
    user: str,
    home: str,
    selection: str,
    workflow: str,
) -> dict[str, object]:
    """Check config and state access inside the actual service job.

    Args:
        project: Expected managed root and current working directory.
        environment: Separate candidate environment used in PATH.
        user: Expected non-root system account.
        home: Expected HOME used for per-host configuration.
        selection: Compatible pre-stop or canonical post-start paths.
        workflow: Optional explicit workflow, empty for packaged default.

    Returns:
        Non-secret path/identity evidence; no configuration values or output.

    Raises:
        CutoverError: If effective identity, config, or state access differs.
    """
    from codereeve.chain import sandbox_config
    from codereeve.chain.cli import _doctor_context, _workflow_path
    from codereeve.chain.obs_config import load_obs_config
    from codereeve.config_env import (
        apply_resolved_environment,
        runtime_environment,
    )
    from codereeve.vendor.symphony.config import load_workflow

    try:
        uid = _service_identity(user)
        expected = {
            "HOME": home,
            "CODEREEVE_PROJECT_ROOT": project,
            "PATH": service_path(environment, home),
        }
        if (
            selection not in {"compatible", "canonical"}
            or Path.cwd() != Path(project)
            or any(
                os.environ.get(key) != value for key, value in expected.items()
            )
            or "CODEREEVE_CUTOVER_GATE" in os.environ
        ):
            raise ValueError
        context = _doctor_context(None)
        if (
            context.config is None
            or context.config_error
            or context.runtime_paths is None
            or context.config_path is None
        ):
            raise ValueError
        if (
            any(
                context.env.get(key) != value
                for key, value in expected.items()
            )
            or "CODEREEVE_CUTOVER_GATE" in context.env
        ):
            raise ValueError
        if selection == "canonical" and (
            context.runtime_paths.state_directory
            != Path(project) / ".codereeve"
            or context.config_path != Path(project) / ".codereeve/config.env"
        ):
            raise ValueError
        # Match daemon environment resolution before observability loads.
        apply_resolved_environment(
            runtime_environment(context.env), os.environ
        )
        sandbox_config.apply_config(context.config, os.environ)
        with _workflow_path(workflow or None) as workflow_path:
            load_workflow(str(workflow_path))
        obs = load_obs_config()
        state = context.runtime_paths.state_directory
        pending = [state] if state.exists() else []
        while pending:
            current = pending.pop()
            if not os.access(current, os.R_OK | os.W_OK | os.X_OK):
                raise ValueError
            with os.scandir(current) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(Path(entry.path))
                    elif stat.S_ISREG(info.st_mode):
                        _writable_path(Path(entry.path))
                    else:
                        raise ValueError
        paths = (
            obs.heartbeat_file,
            obs.runlog_path,
            obs.redispatch_counts_path,
            obs.failure_counts_path,
            state / "session-report.json",
        )
        for path in paths:
            _writable_path(path)
        sidecar = Path(str(obs.heartbeat_file) + ".identity.json")
        _writable_path(sidecar)
        return {
            "schema_version": 1,
            "uid": uid,
            "heartbeat_path": sidecar.absolute().as_posix(),
            "runtime_paths": sorted(
                {p.absolute().as_posix() for p in (*paths, sidecar)}
            ),
            "state_path": state.absolute().as_posix(),
            "config_path": context.config_path.absolute().as_posix(),
        }
    except Exception:
        raise CutoverError(
            "service configuration or state access is unproven"
        ) from None
