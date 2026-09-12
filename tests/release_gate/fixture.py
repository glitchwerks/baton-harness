"""Load and materialize the durable CodeReeve upgrade fixture."""

from __future__ import annotations

import json
import re
import stat
from pathlib import Path, PurePosixPath

from codereeve.migration.lease import WriterLease, probe_writer_lease
from codereeve.migration.model import EvidenceState, MigrationEvidence
from codereeve.migration.transaction import FileOperations

_TOP_LEVEL_KEYS = {
    "schema_version",
    "legacy_source_revision",
    "files",
    "canonical_assertions",
}
_FILE_KEYS = {"path", "content", "permissions"}
_REVISION_PATTERN = re.compile(r"[0-9a-f]{40}")


class PortableFixtureOperations(FileOperations):
    """Provide portable test evidence without claiming OS durability."""

    def verify_quiescence(
        self, project: Path, lease: WriterLease
    ) -> MigrationEvidence:
        """Attest synthetic writers are stopped under a real held lease.

        Args:
            project: Disposable synthetic project root.
            lease: Actual migration writer lease held by the transaction.

        Returns:
            Clear synthetic writer and service evidence.
        """
        assert lease.path == project / ".codereeve-migration.lock"
        assert probe_writer_lease(lease.path) is EvidenceState.BLOCKED
        return MigrationEvidence(
            writers=EvidenceState.CLEAR,
            service=EvidenceState.CLEAR,
        )

    def sync_directory(self, path: Path) -> None:
        """Exercise directory boundaries without making durability claims.

        Args:
            path: Directory whose durability seam is being exercised.
        """
        self.boundary("directory_fsync", "before", path)
        self.boundary("directory_fsync", "after", path)


def load_fixture(path: Path) -> dict[str, object]:
    """Load a fixture after validating its complete public schema.

    Args:
        path: JSON fixture to read as UTF-8.

    Returns:
        A validated mutable JSON object for isolated test customization.

    Raises:
        ValueError: If the JSON or fixture schema is invalid.
        OSError: If the fixture cannot be read.
    """
    try:
        fixture = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid upgrade fixture JSON") from exc
    _validate_fixture(fixture)
    return fixture


def materialize_fixture(root: Path, fixture: dict[str, object]) -> None:
    """Write validated fixture files beneath a fresh disposable root.

    Args:
        root: New disposable root which will contain every written file.
        fixture: Fixture object returned by :func:`load_fixture`.

    Raises:
        ValueError: If the root exists, has a linked parent, or the fixture is
            invalid.
        OSError: If validated content cannot be written.
    """
    _validate_fixture(fixture)
    if root.exists() or root.is_symlink():
        raise ValueError("fixture destination must not exist")
    _reject_symlink_ancestors(root)

    files = fixture["files"]
    assert isinstance(files, list)
    root.mkdir()
    for entry in files:
        assert isinstance(entry, dict)
        relative = PurePosixPath(entry["path"])
        destination = root.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(entry["content"].encode("utf-8"))
        destination.chmod(entry["permissions"])


def _validate_fixture(fixture: object) -> None:
    """Validate the entire fixture graph without filesystem writes."""
    if not isinstance(fixture, dict) or set(fixture) != _TOP_LEVEL_KEYS:
        raise ValueError("invalid upgrade fixture schema")
    if fixture["schema_version"] != 1:
        raise ValueError("unsupported upgrade fixture schema version")
    revision = fixture["legacy_source_revision"]
    if not isinstance(revision, str) or not _REVISION_PATTERN.fullmatch(
        revision
    ):
        raise ValueError("invalid legacy source revision")
    files = fixture["files"]
    assertions = fixture["canonical_assertions"]
    if not isinstance(files, list) or not files:
        raise ValueError("upgrade fixture files must be a nonempty list")
    if not isinstance(assertions, dict) or not assertions:
        raise ValueError("canonical assertions must be a nonempty object")

    paths: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != _FILE_KEYS:
            raise ValueError("invalid upgrade fixture file entry")
        path = entry["path"]
        _validate_relative_path(path)
        if path in paths:
            raise ValueError("duplicate upgrade fixture path")
        paths.add(path)
        content = entry["content"]
        permissions = entry["permissions"]
        if not isinstance(content, str):
            raise ValueError("fixture file content must be text")
        content.encode("utf-8")
        if (
            not isinstance(permissions, int)
            or isinstance(permissions, bool)
            or permissions < 0
            or permissions > 0o777
        ):
            raise ValueError("fixture permissions must be a file mode")

    for path, expected in assertions.items():
        _validate_relative_path(path)
        if not isinstance(expected, dict) or not expected:
            raise ValueError("canonical assertion must be a nonempty object")
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in expected.items()
        ):
            raise ValueError("canonical assertion values must be strings")


def _validate_relative_path(path: object) -> None:
    """Require one normalized relative POSIX path without placeholders."""
    if not isinstance(path, str) or not path or "\\" in path:
        raise ValueError("fixture path must be relative POSIX syntax")
    relative = PurePosixPath(path)
    if (
        relative.is_absolute()
        or path.startswith("//")
        or re.match(r"^[A-Za-z]:", path)
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.as_posix() != path
    ):
        raise ValueError("fixture path escapes its disposable root")


def _reject_symlink_ancestors(path: Path) -> None:
    """Reject any existing linked parent without resolving the destination."""
    for ancestor in path.parents:
        try:
            mode = ancestor.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError("fixture destination has a symlink parent")
