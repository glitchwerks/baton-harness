"""Emit validated configuration as UTF-8 NUL records for Bash callers."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from codereeve.config_env import (
    PRODUCT_ALIASES,
    AliasConflictError,
    ConfigSyntaxError,
    EnvLayer,
    parse_env_file,
    resolve_environment,
)
from codereeve.paths import (
    PathConflictError,
    PathLayout,
    select_compatible_file,
    select_runtime_paths,
    validate_safe_file_path,
)


def _read_layer(path: Path) -> EnvLayer:
    """Read an optional safe file into a literal assignment layer."""
    values = {}
    if validate_safe_file_path(path, label="configuration"):
        for item in parse_env_file(path):
            if item.key.startswith("_codereeve_"):
                raise ConfigSyntaxError(str(path), item.line)
            values[item.key] = item.value
    return EnvLayer(str(path), values)


def main(argv: Sequence[str] | None = None) -> int:
    """Validate the complete config chain before writing any output.

    Args:
        argv: Optional command arguments, otherwise process arguments.

    Returns:
        Zero on success, one on a config failure, two for invalid arguments.
    """
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--format", choices=("nul",), required=True)
    parser.add_argument("--host", type=Path)
    parser.add_argument("--managed", type=Path)
    args = parser.parse_args(argv)
    try:
        operator = EnvLayer("environment", dict(os.environ))
        layout = PathLayout.for_environment(Path.cwd(), operator.values)
        host_path = (
            args.host
            or select_compatible_file(
                layout.canonical_host,
                layout.legacy_host,
                label="host configuration",
            ).path
        )
        host = _read_layer(host_path)
        # Only the project root is needed to discover the managed layer.
        # Compare every other alias after all layers are available.
        root_alias = tuple(
            alias
            for alias in PRODUCT_ALIASES
            if alias.canonical == "CODEREEVE_PROJECT_ROOT"
        )
        bootstrap = resolve_environment((operator, host), aliases=root_alias)
        project = bootstrap.values.get("CODEREEVE_PROJECT_ROOT")
        managed_path = args.managed
        state_directory = None
        if project:
            layout = PathLayout.for_environment(Path(project), operator.values)
            selected = select_compatible_file(
                layout.canonical_config,
                layout.legacy_config,
                label="managed configuration",
            ).path
            if managed_path is None:
                managed_path = selected
            state_directory = select_runtime_paths(
                Path(project), operator.values
            ).state_directory
        managed = (
            _read_layer(managed_path)
            if managed_path
            else EnvLayer("managed", {})
        )
        resolved = resolve_environment((operator, managed, host))
        keys = set(host.values) | set(managed.values)
        for alias in PRODUCT_ALIASES:
            keys.update((alias.canonical, alias.legacy))
        payload = b"".join(
            key.encode("utf-8")
            + b"\0"
            + resolved.values[key].encode("utf-8")
            + b"\0"
            for key in sorted(keys)
            if key in resolved.values
        )
        if state_directory is not None:
            payload += (
                b"_codereeve_state_directory\0"
                + state_directory.as_posix().encode("utf-8")
                + b"\0"
            )
    except ConfigSyntaxError as exc:
        print(f"{exc.source}:{exc.line}", file=sys.stderr)
        return 1
    except AliasConflictError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except PathConflictError as exc:
        print(exc.safe_diagnostic, file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        print("unable to read configuration", file=sys.stderr)
        return 1
    if resolved.values.get("CODEREEVE_DEBUG_CONFIG") == "1":
        for label, path in (
            ("host.env", host_path),
            ("config.env", managed_path),
        ):
            state = (
                "found" if path is not None and path.is_file() else "not found"
            )
            print(f"codereeve: config-debug: {label} {state}", file=sys.stderr)
    sys.stdout.buffer.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
