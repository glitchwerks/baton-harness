"""Immutable inputs and outcomes for transactional service cutover."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath

_RUN_USER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")


class CutoverError(RuntimeError):
    """Report a service-cutover failure using a fixed, value-free message."""


def _validated_path_text(path: Path) -> str:
    """Return one validated absolute Linux target path.

    Args:
        path: Path intended for the target systemd host.

    Returns:
        The POSIX spelling of the validated path.

    Raises:
        CutoverError: If the value is not an absolute POSIX path or contains
            data that could alter the unit structure.
    """
    if not isinstance(path, PurePath):
        raise CutoverError("service path must be absolute")
    text = path.as_posix()
    if not PurePosixPath(text).is_absolute():
        raise CutoverError("service path must be absolute")
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise CutoverError("service path contains control data")
    return text


@dataclass(frozen=True)
class ServiceSpec:
    """Validated immutable inputs used to render and manage the service.

    Attributes:
        project_root: Absolute managed-repository root on the service host.
        environment: Absolute separate CodeReeve installation environment.
        run_user: Literal system user that owns the service process.
        workflow: Optional absolute workflow file passed to the daemon.
        secrets: Optional absolute environment file loaded by systemd.
        home: Absolute home directory exported to the service process.
        timeout_s: Positive finite service-operation timeout in seconds.
    """

    project_root: Path
    environment: Path
    run_user: str
    workflow: Path | None
    secrets: Path | None
    home: Path
    timeout_s: float = 120.0

    def __post_init__(self) -> None:
        """Validate all values before a renderer or backend can use them.

        Raises:
            CutoverError: If a path, user, or timeout is unsafe or invalid.
        """
        for required_path in (
            self.project_root,
            self.environment,
            self.home,
        ):
            _validated_path_text(required_path)
        for optional_path in (self.workflow, self.secrets):
            if optional_path is not None:
                _validated_path_text(optional_path)
        valid_user = isinstance(
            self.run_user, str
        ) and _RUN_USER_PATTERN.fullmatch(self.run_user)
        if not valid_user:
            raise CutoverError("service user is invalid")
        try:
            timeout_is_valid = (
                not isinstance(self.timeout_s, bool)
                and math.isfinite(self.timeout_s)
                and self.timeout_s > 0
            )
        except (TypeError, OverflowError):
            timeout_is_valid = False
        if not timeout_is_valid:
            raise CutoverError("service timeout is invalid")


@dataclass(frozen=True)
class CutoverResult:
    """Immutable terminal outcome returned by a cutover coordinator.

    Attributes:
        status: Stable terminal status name.
        journal_path: Durable journal path when a transaction created one.
        recovery: Safe recovery guidance when operator action is required.
    """

    status: str
    journal_path: Path | None
    recovery: str | None
