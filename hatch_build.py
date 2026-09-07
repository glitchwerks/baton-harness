"""Hatch hooks that create and validate build provenance records."""

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from hatchling.metadata.plugin.interface import MetadataHookInterface
from packaging.version import InvalidVersion, Version

DEVELOPMENT_VERSION = "0.1.0.dev0"
REVISION_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
RECORD_PATH = Path("src/baton_harness/build_provenance.json")
RECORD_KEYS = frozenset(
    {
        "schema_version",
        "package_version",
        "source_revision",
        "lock_identity",
        "development",
    }
)


class BuildProvenanceError(ValueError):
    """Raised when build identity inputs cannot prove artifact provenance."""


@dataclass(frozen=True)
class BuildProvenance:
    """The complete, versioned provenance identity embedded in an artifact."""

    schema_version: int
    package_version: str
    source_revision: str
    lock_identity: str
    development: bool

    def as_dict(self) -> dict[str, object]:
        """Return the record representation used in JSON artifacts."""
        return asdict(self)


def lock_identity(lock_path: Path) -> str:
    """Return the SHA-256 identity of the lock file's exact bytes."""
    try:
        content = lock_path.read_bytes()
    except OSError as exc:
        raise BuildProvenanceError(
            f"cannot read lock file: {lock_path}"
        ) from exc
    return f"sha256:{sha256(content).hexdigest()}"


def resolve_build_provenance(
    root: Path,
    env: Mapping[str, str],
    *,
    read_head: Callable[[Path], str],
) -> BuildProvenance:
    """Validate explicit build assertions and return their identity."""
    development_value = env.get("BH_BUILD_DEVELOPMENT")
    if development_value not in (None, "1"):
        raise BuildProvenanceError("BH_BUILD_DEVELOPMENT must be exactly '1'")

    development = development_value == "1"
    version_assertion = env.get("BH_BUILD_VERSION")
    revision_assertion = env.get("BH_BUILD_SOURCE_REVISION")
    if development and (
        version_assertion is not None or revision_assertion is not None
    ):
        raise BuildProvenanceError(
            "development builds cannot include standard identity assertions"
        )

    if development:
        package_version = DEVELOPMENT_VERSION
        source_revision = _validate_revision(read_head(root), "Git HEAD")
    else:
        package_version = _validate_version(version_assertion)
        source_revision = _validate_revision(
            revision_assertion, "BH_BUILD_SOURCE_REVISION"
        )
        if _is_checkout(root):
            actual_head = _validate_revision(read_head(root), "Git HEAD")
            if actual_head != source_revision:
                raise BuildProvenanceError(
                    "BH_BUILD_SOURCE_REVISION does not match Git HEAD"
                )

    identity = BuildProvenance(
        schema_version=1,
        package_version=package_version,
        source_revision=source_revision,
        lock_identity=lock_identity(root / "uv.lock"),
        development=development,
    )
    _validate_carried_record(root / RECORD_PATH, identity)
    return identity


def _validate_version(value: str | None) -> str:
    """Require and validate a PEP 440 version assertion."""
    if not value:
        raise BuildProvenanceError("BH_BUILD_VERSION is required")
    try:
        normalized = str(Version(value))
    except InvalidVersion as exc:
        raise BuildProvenanceError(
            "BH_BUILD_VERSION must be a PEP 440 version"
        ) from exc
    return normalized


def _validate_revision(value: str | None, name: str) -> str:
    """Normalize and validate a full forty-character Git revision."""
    if not value or not REVISION_PATTERN.fullmatch(value):
        raise BuildProvenanceError(
            f"{name} must be exactly 40 hexadecimal characters"
        )
    return value.lower()


def _is_checkout(root: Path) -> bool:
    """Return whether the root has a Git directory or worktree marker."""
    return (root / ".git").exists()


def _validate_carried_record(path: Path, identity: BuildProvenance) -> None:
    """Require a generated sdist record to exactly match the build identity."""
    if not path.exists():
        return
    try:
        carried = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildProvenanceError(
            f"cannot read carried provenance record: {path}"
        ) from exc
    if not isinstance(carried, dict) or set(carried) != RECORD_KEYS:
        raise BuildProvenanceError(
            "carried provenance record has an invalid schema"
        )
    if (
        type(carried["schema_version"]) is not int
        or type(carried["package_version"]) is not str
        or type(carried["source_revision"]) is not str
        or type(carried["lock_identity"]) is not str
        or type(carried["development"]) is not bool
    ):
        raise BuildProvenanceError(
            "carried provenance record has invalid field types"
        )
    if carried != identity.as_dict():
        raise BuildProvenanceError(
            "carried provenance record does not match build identity"
        )


def _read_head(root: Path) -> str:
    """Read the checked-out Git revision for a build hook invocation."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BuildProvenanceError("cannot read Git HEAD") from exc
    return result.stdout.strip()


class CustomMetadataHook(MetadataHookInterface):
    """Set dynamic package metadata from the validated build identity."""

    def update(self, metadata: dict[str, Any]) -> None:
        """Set only the dynamic project version."""
        identity = resolve_build_provenance(
            Path(self.root),
            os_environ(),
            read_head=_read_head,
        )
        metadata["version"] = identity.package_version


class CustomBuildHook(BuildHookInterface[Any]):
    """Embed the validated provenance record in every artifact type."""

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        """Generate and force-include the provenance record for this build."""
        del version
        identity = resolve_build_provenance(
            Path(self.root),
            os_environ(),
            read_head=_read_head,
        )
        directory = Path(self.directory)
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix="build-provenance-",
            suffix=".json",
            delete=False,
        ) as temporary:
            json.dump(identity.as_dict(), temporary, indent=2, sort_keys=True)
            temporary.write("\n")
            self._provenance_path = Path(temporary.name)

        source = str(self._provenance_path)
        destination = (
            "src/baton_harness/build_provenance.json"
            if self.target_name == "sdist"
            else "baton_harness/build_provenance.json"
        )
        build_data.setdefault("force_include", {})[source] = destination
        if self.target_name == "wheel":
            build_data.setdefault("force_include_editable", {})[source] = (
                destination
            )

    def finalize(
        self,
        version: str,
        build_data: dict[str, Any],
        artifact_path: str,
    ) -> None:
        """Delete only the temporary record created by this hook instance."""
        del version, build_data, artifact_path
        temporary = getattr(self, "_provenance_path", None)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def os_environ() -> Mapping[str, str]:
    """Return the current process environment as a read-only mapping view."""
    return os.environ
