"""Audit tracked CodeReeve source against exact legacy identity exceptions."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

CATALOG = "tests/data/identity-allowlist.json"
LEGACY = re.compile(
    r"baton[-_]harness|(?<![a-z0-9])baton(?![a-z0-9])"
    r"|(?<![a-z0-9])bh_[a-z0-9_*]*"
    r"|(?<![a-z0-9])bh-[a-z0-9_*-]*|\.bh(?![a-z0-9])",
    re.IGNORECASE,
)
CATEGORIES = {"compatibility", "migration", "historical", "provenance"}
FIELDS = {"path", "text", "count", "category", "reason", "kind"}


def tokens(text: str) -> list[str]:
    """Return legacy identity tokens without their surrounding values."""
    return LEGACY.findall(text)


def audit(
    files: Mapping[str, str | None],
    catalog: object,
) -> list[str]:
    """Compare tracked paths and text lines with a closed exact catalog.

    Args:
        files: Relative tracked paths and decoded text, or None for binary.
        catalog: Parsed JSON list of exact occurrence exceptions.

    Returns:
        Safe diagnostics containing locations and a fixed identity label.
    """
    errors: list[str] = []
    if not isinstance(catalog, list):
        return ["catalog: expected an array"]
    allowed: dict[tuple[str, str, str], int] = {}
    for number, entry in enumerate(catalog, 1):
        location = f"catalog entry {number}"
        if not isinstance(entry, dict) or set(entry) != FIELDS:
            errors.append(f"{location}: invalid fields")
            continue
        if (
            any(not isinstance(entry[key], str) for key in FIELDS - {"count"})
            or type(entry["count"]) is not int
            or entry["count"] < 1
            or entry["category"] not in CATEGORIES
            or entry["kind"] not in {"path", "text"}
            or not entry["reason"].strip()
            or not entry["text"]
            or "\n" in entry["text"]
            or "\r" in entry["text"]
            or not entry["path"]
            or any(char in entry["path"] for char in "*?[]")
            or entry["path"].startswith("/")
            or ".." in entry["path"].split("/")
            or entry["path"] == CATALOG
        ):
            errors.append(f"{location}: invalid allowance")
            continue
        key = (entry["path"], entry["kind"], entry["text"])
        if key in allowed:
            errors.append(f"{location}: duplicate allowance")
        allowed[key] = entry["count"]
    found: Counter[tuple[str, str, str]] = Counter()
    locations: dict[tuple[str, str, str], str] = {}
    for path, content in files.items():
        if path == CATALOG:
            continue
        references = [("path", path, "path")]
        if content is not None:
            references.extend(
                ("text", line, str(number))
                for number, line in enumerate(content.splitlines(), 1)
            )
        for kind, text, line in references:
            matched = tokens(text)
            if not matched:
                continue
            key = (path, kind, text)
            found[key] += 1
            locations[key] = f"{path}:{line}: legacy identity"
    for key, count in found.items():
        if count != allowed.get(key, 0):
            errors.append(
                f"{locations[key]}: unapproved occurrence count {count}"
            )
    for key in allowed.keys() - found.keys():
        errors.append(f"{key[0]}:{key[1]}: stale allowance")
    return errors


def tracked_files(root: Path) -> dict[str, str | None]:
    """Read only Git-tracked files; runtime and ignored data stay outside."""
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    files: dict[str, str | None] = {}
    for path in result.stdout.decode("utf-8").split("\0"):
        if not path or path == CATALOG:
            continue
        data = (root / path).read_bytes()
        try:
            files[path] = None if b"\0" in data else data.decode("utf-8")
        except UnicodeDecodeError:
            files[path] = None
    return files


def main(argv: Sequence[str] | None = None) -> int:
    """Run the audit and return a nonzero status for any rejected reference."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        catalog = json.loads((args.root / CATALOG).read_text(encoding="utf-8"))
        errors = audit(tracked_files(args.root), catalog)
    except (OSError, ValueError, subprocess.CalledProcessError):
        print("identity audit: unable to read tracked source or catalog")
        return 1
    for error in errors:
        print(error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
