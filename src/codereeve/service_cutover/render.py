"""Pure rendering of the canonical CodeReeve systemd service unit."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from .model import CutoverError, ServiceSpec, _validated_path_text

_SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin"


def _quote_systemd_token(value: str) -> str:
    """Quote one systemd token while preserving its literal value.

    Args:
        value: One directive value or command argument.

    Returns:
        A double-quoted systemd token with C escapes and specifiers escaped.

    Raises:
        CutoverError: If control data could change the unit structure.
    """
    if any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise CutoverError("service value contains control data")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("%", "%%")
    return f'"{escaped}"'


def _raw_scalar_path(value: str, *, reject_glob: bool = False) -> str:
    """Serialize a path consumed as an unquoted scalar by systemd.

    Args:
        value: Validated absolute POSIX path.
        reject_glob: Whether systemd would interpret glob metacharacters.

    Returns:
        A raw scalar with percent specifiers escaped.

    Raises:
        CutoverError: If systemd cannot preserve the literal path spelling.
    """
    if value[-1].isspace() or value.endswith("\\"):
        raise CutoverError("service path cannot be rendered")
    if reject_glob and any(character in value for character in "*?["):
        raise CutoverError("service path cannot be rendered")
    return value.replace("%", "%%")


def _timeout_text(timeout_s: float) -> str:
    """Render a validated timeout without scientific notation.

    Args:
        timeout_s: Positive finite duration from a service specification.

    Returns:
        A systemd-compatible decimal number of seconds.
    """
    text = format(Decimal(str(timeout_s)), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def render_unit(spec: ServiceSpec, *, gate: Path | None = None) -> str:
    """Render the canonical service unit without external effects.

    Args:
        spec: Validated immutable service inputs.
        gate: Optional absolute startup-readiness receipt path.

    Returns:
        Complete canonical systemd unit text ending in one newline.

    Raises:
        CutoverError: If the optional gate is not a safe absolute path.
    """
    project_root = _validated_path_text(spec.project_root)
    environment = _validated_path_text(spec.environment)
    home = _validated_path_text(spec.home)
    workflow = (
        _validated_path_text(spec.workflow)
        if spec.workflow is not None
        else None
    )
    secrets = (
        _validated_path_text(spec.secrets)
        if spec.secrets is not None
        else None
    )
    gate_text = _validated_path_text(gate) if gate is not None else None

    lines = [
        "[Unit]",
        "Description=CodeReeve daemon",
        "After=network.target bh-daemon.service",
        "Conflicts=bh-daemon.service",
        "",
        "[Service]",
        "Type=simple",
        f"User={spec.run_user}",
        f"WorkingDirectory={_raw_scalar_path(project_root)}",
        "Environment="
        + _quote_systemd_token(f"CODEREEVE_PROJECT_ROOT={project_root}"),
        f"Environment={_quote_systemd_token(f'HOME={home}')}",
        "Environment="
        + _quote_systemd_token(f"PATH={environment}/bin:{_SYSTEM_PATH}"),
    ]
    if gate_text is not None:
        lines.append(
            "Environment="
            + _quote_systemd_token(f"CODEREEVE_CUTOVER_GATE={gate_text}")
        )
    if secrets is not None:
        lines.append(
            "EnvironmentFile=" + _raw_scalar_path(secrets, reject_glob=True)
        )

    command = [f":{environment}/bin/codereeve", "daemon"]
    if workflow is not None:
        command.extend(("--workflow", workflow))
    lines.extend(
        (
            "ExecStart="
            + " ".join(_quote_systemd_token(argument) for argument in command),
            "KillMode=control-group",
            f"TimeoutStopSec={_timeout_text(spec.timeout_s)}",
            "Restart=on-failure",
            "RestartSec=15",
            "StandardOutput=journal",
            "StandardError=journal",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
        )
    )
    return "\n".join(lines) + "\n"
