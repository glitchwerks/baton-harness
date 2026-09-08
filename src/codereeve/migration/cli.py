"""Exact, secret-safe command surface for CodeReeve state migration."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from codereeve.config_env import (
    PRODUCT_ALIASES,
    AliasConflictError,
    ConfigSyntaxError,
    EnvLayer,
    parse_env_file,
    resolve_environment,
    runtime_environment,
)
from codereeve.migration.inventory import inventory_migration
from codereeve.migration.model import MigrationContext, MigrationReport
from codereeve.migration.transaction import (
    AppliedMigration,
    MigrationError,
    RestorationResult,
    apply_migration,
)
from codereeve.paths import (
    PathConflictError,
    PathLayout,
    select_compatible_file,
    validate_safe_file_path,
)
from codereeve.redact import redact_secrets

ContextFactory = Callable[[], MigrationContext]

HELP = """usage: codereeve migrate (--check | --apply) [--format text|json]

options:
  --check         inspect migration state: current (0), ready (2), blocked (1)
  --apply         apply a ready migration under verified writer quiescence
  --format        render text (default) or schema-versioned json
  -h, --help      show this help"""


def _read_layer(path: Path) -> EnvLayer:
    """Read one optional safe configuration file without executing it."""
    values: dict[str, str] = {}
    if validate_safe_file_path(path, label="host configuration"):
        for assignment in parse_env_file(path):
            if assignment.key.startswith("_codereeve_"):
                raise ConfigSyntaxError(str(path), assignment.line)
            values[assignment.key] = assignment.value
    return EnvLayer(str(path), values)


def default_context() -> MigrationContext:
    """Build one conflict-checked migration context from the environment.

    Returns:
        An immutable context rooted at the configured project or current
        directory, with operator environment as the highest-precedence layer.

    Raises:
        AliasConflictError: If an operator alias pair differs exactly.
    """
    resolved = runtime_environment(os.environ)
    operator = EnvLayer("environment", resolved.values)
    initial_layout = PathLayout.for_environment(Path.cwd(), resolved.values)
    host_path = select_compatible_file(
        initial_layout.canonical_host,
        initial_layout.legacy_host,
        label="host configuration",
    ).path
    host = _read_layer(host_path)
    root_alias = tuple(
        alias
        for alias in PRODUCT_ALIASES
        if alias.canonical == "CODEREEVE_PROJECT_ROOT"
    )
    bootstrap = resolve_environment(
        (operator, host), aliases=root_alias, export_legacy=False
    )
    configured_root = bootstrap.values.get("CODEREEVE_PROJECT_ROOT")
    project = (
        Path(configured_root) if configured_root is not None else Path.cwd()
    ).absolute()
    return MigrationContext(
        PathLayout.for_environment(project, resolved.values),
        layers=(operator,),
    )


def _apply(context: MigrationContext) -> AppliedMigration:
    """Call the transaction API through a private portable test seam."""
    return apply_migration(context)


def _usage_error(message: str) -> int:
    """Print a closed-grammar error and return conventional usage status."""
    print(f"codereeve migrate: {message}", file=sys.stderr)
    print(HELP, file=sys.stderr)
    return 2


def _parse(argv: Sequence[str]) -> tuple[str, str] | None:
    """Parse only canonical tokens without aliases or equals forms."""
    args = list(argv)
    if len(args) not in {1, 3} or args[0] not in {"--check", "--apply"}:
        return None
    if len(args) == 1:
        return args[0], "text"
    if args[1] != "--format" or args[2] not in {"text", "json"}:
        return None
    return args[0], args[2]


def _write(payload: dict[str, object], output_format: str) -> None:
    """Write deterministic JSON or its complete text representation."""
    if output_format == "json":
        print(json.dumps(payload, sort_keys=True))
        return
    for key, value in payload.items():
        if isinstance(value, list):
            if not value:
                print(f"{key}: []")
                continue
            item_key = key[:-1] if key.endswith("paths") else "-"
            if isinstance(value[0], dict):
                print(f"{key}:")
            for item in value:
                if isinstance(item, dict):
                    fields = " | ".join(
                        f"{field}={','.join(map(str, entry))}"
                        if isinstance(entry, list)
                        else f"{field}={entry}"
                        for field, entry in item.items()
                    )
                    print(f"- {fields}")
                else:
                    print(f"{item_key}: {item}")
        elif isinstance(value, dict):
            print(f"{key}:")
            for field, entry in value.items():
                print(f"  {field}: {entry}")
        else:
            print(f"{key}: {value}")


def _report_payload(report: MigrationReport) -> dict[str, object]:
    """Return the shared schema-v1 inventory representation."""
    return report.as_dict()


def _success_payload(result: AppliedMigration) -> dict[str, object]:
    """Return durable paths needed for verification and manual restoration."""
    manifest = redact_secrets(str(result.manifest_path))
    journal = redact_secrets(
        str(result.manifest_path.with_name("journal.jsonl"))
    )
    backups = [redact_secrets(str(path)) for path in result.backups]
    return {
        "schema_version": 1,
        "status": "applied",
        "exit_code": 0,
        "manifest_path": manifest,
        "journal_path": journal,
        "backup_paths": backups,
        "manual_restoration_paths": [manifest, journal, *backups],
    }


def _restoration_payload(
    restoration: RestorationResult | None,
) -> dict[str, object] | None:
    """Return structured persisted recovery evidence when one exists."""
    if restoration is None:
        return None
    manifest = redact_secrets(str(restoration.manifest_path))
    return {
        "status": restoration.status.value,
        "manifest_path": manifest,
        "journal_path": redact_secrets(
            str(restoration.manifest_path.with_name("journal.jsonl"))
        ),
        "diagnostic": redact_secrets(restoration.diagnostic),
    }


def _failure_payload(
    diagnostic: str,
    restoration: RestorationResult | None = None,
) -> dict[str, object]:
    """Return a blocked result without inventing restoration evidence."""
    return {
        "schema_version": 1,
        "status": "blocked",
        "exit_code": 1,
        "diagnostic": redact_secrets(diagnostic),
        "restoration": _restoration_payload(restoration),
    }


def _write_failure(
    diagnostic: str,
    output_format: str,
    restoration: RestorationResult | None = None,
) -> int:
    """Render one failure while preserving distinct evidence states."""
    payload = _failure_payload(diagnostic, restoration)
    if output_format == "text":
        print("status: blocked")
        print("exit_code: 1")
        print(f"diagnostic: {payload['diagnostic']}")
        if restoration is None:
            print("restoration_status: unavailable")
        else:
            evidence = payload["restoration"]
            assert isinstance(evidence, dict)
            print(f"restoration_status: {evidence['status']}")
            print(f"manifest_path: {evidence['manifest_path']}")
            print(f"journal_path: {evidence['journal_path']}")
            if evidence["diagnostic"]:
                print(f"restoration_diagnostic: {evidence['diagnostic']}")
    else:
        _write(payload, output_format)
    return 1


def main(
    argv: Sequence[str],
    *,
    context_factory: ContextFactory = default_context,
) -> int:
    """Check or apply one migration using the exact public grammar.

    Args:
        argv: Arguments after ``codereeve migrate``.
        context_factory: Shared conflict-checked context boundary.

    Returns:
        The report status, zero after a completed apply, one on refusal or
        failure, or two for invalid command syntax.
    """
    args = list(argv)
    if args in (["--help"], ["-h"]):
        print(HELP)
        return 0
    parsed = _parse(args)
    if parsed is None:
        return _usage_error("invalid arguments")
    mode, output_format = parsed
    try:
        context = context_factory()
        if mode == "--check":
            report = inventory_migration(context)
            _write(_report_payload(report), output_format)
            return report.exit_code
        result = _apply(context)
        _write(_success_payload(result), output_format)
        return 0
    except MigrationError as exc:
        return _write_failure(str(exc), output_format, exc.restoration)
    except AliasConflictError as exc:
        return _write_failure(str(exc), output_format)
    except PathConflictError as exc:
        return _write_failure(exc.safe_diagnostic, output_format)
    except (ConfigSyntaxError, OSError, UnicodeError):
        return _write_failure(
            "migration configuration or filesystem is unavailable",
            output_format,
        )
    except Exception:
        return _write_failure(
            "migration failed safely; inspect retained evidence",
            output_format,
        )
