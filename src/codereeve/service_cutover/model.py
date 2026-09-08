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


@dataclass(frozen=True)
class UnitState:
    """Exact observed system-service identity and activation metadata.

    Attributes:
        name: Manager-resolved unit name.
        load_state: Loaded, absent, or masked state.
        active_state: Observed runtime state.
        enabled_state: Persistent or runtime enablement/mask state.
        main_pid: Current main process, zero when absent.
        invocation_id: Runtime-cycle identity, empty when never started.
        control_group: Absolute cgroup v2 subtree, empty when inactive.
        fragment_path: Manager-selected unit file or mask target.
        dropin_paths: Complete manager-selected drop-in list.
        kill_mode: Effective service kill mode, including defaults.
        user: Effective configured service account.
        exec_start: Absolute executable selected by ExecStart.
        sub_state: Exact runtime substate.
        job: Pending manager job description, empty when none.
        exec_start_argv: Manager-rendered complete ExecStart argument string.
    """

    name: str
    load_state: str
    active_state: str
    enabled_state: str
    main_pid: int
    invocation_id: str
    control_group: str
    fragment_path: str
    dropin_paths: tuple[str, ...]
    kill_mode: str = ""
    user: str = ""
    exec_start: str = ""
    sub_state: str = ""
    job: str = ""
    exec_start_argv: str = ""


@dataclass(frozen=True)
class ServiceSnapshot:
    """Original manager states and explicitly owned files to retain privately.

    Raw command fields are never serialized by the recovery journal.
    Files must include selected units, owned drop-ins and environment inputs.
    """

    old: UnitState
    new: UnitState
    files: tuple[Path, ...] = ()
