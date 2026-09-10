"""Deterministic, secret-safe renderers for preflight check results."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from codereeve.chain.doctor import (
    CheckResult,
    CheckStatus,
    DoctorContext,
    Phase,
    Severity,
)
from codereeve.provenance import Provenance
from codereeve.redact import redact_secrets

_ERROR_MESSAGE = "doctor report could not be safely rendered"
_SECRET_ENV_NAMES = frozenset(
    {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "BWS_ACCESS_TOKEN",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CODEREEVE_HEARTBEAT_PING_URL",
        "BH_HEARTBEAT_PING_URL",
    }
)


class ReportRenderingError(RuntimeError):
    """Raised without diagnostic payloads when rendering is unsafe."""


@dataclass(frozen=True)
class ReportSummary:
    """Immutable counts of outcomes and critical failures.

    Attributes:
        passed: Passing checks.
        failed: Failing checks of either severity.
        warned: Checks that reported a warning.
        skipped: Checks that were skipped.
        critical_failures: Failing critical checks only.
    """

    passed: int
    failed: int
    warned: int
    skipped: int
    critical_failures: int


def summarize(results: Sequence[CheckResult]) -> ReportSummary:
    """Count outcomes without conflating warnings with critical failures.

    Args:
        results: Collected checks in catalog order.

    Returns:
        Immutable report counters.
    """
    return ReportSummary(
        passed=sum(r.status == CheckStatus.PASS for r in results),
        failed=sum(r.status == CheckStatus.FAIL for r in results),
        warned=sum(r.status == CheckStatus.WARN for r in results),
        skipped=sum(r.status == CheckStatus.SKIP for r in results),
        critical_failures=sum(
            r.status == CheckStatus.FAIL and r.severity == Severity.CRITICAL
            for r in results
        ),
    )


def build_document(
    results: Sequence[CheckResult],
    phases: Sequence[Phase],
    provenance: Provenance | None,
) -> dict[str, object]:
    """Build the ordered schema from already-redacted check results.

    Args:
        results: Safe results in catalog order.
        phases: Selected phases in execution order.
        provenance: Validated installed provenance, or unavailable.

    Returns:
        JSON-compatible schema-version-1 document.
    """
    summary = summarize(results)
    return {
        "schema_version": 1,
        "provenance": (
            {
                "package_version": provenance.package_version,
                "source_revision": provenance.source_revision,
                "lock_identity": provenance.lock_identity,
                "development": provenance.development,
            }
            if provenance is not None
            else None
        ),
        "selected_phases": [phase.value for phase in phases],
        "summary": {
            "pass": summary.passed,
            "fail": summary.failed,
            "warn": summary.warned,
            "skip": summary.skipped,
            "critical_failures": summary.critical_failures,
        },
        "checks": [
            {
                "id": result.check_id,
                "phase": result.phase.value,
                "status": result.status.value,
                "severity": result.severity.value,
                "title": result.title,
                "detail": result.detail,
                "remediation": result.remediation,
            }
            for result in results
        ],
    }


def secret_values_from_context(ctx: DoctorContext) -> tuple[str, ...]:
    """Collect known ambient credentials and the token held by value.

    Args:
        ctx: Context snapshot taken before credential environment scrub.

    Returns:
        Non-empty exact credential values, never paths or secret IDs.
    """
    return tuple(
        dict.fromkeys(
            value
            for value in (
                *(ctx.env.get(name, "") for name in sorted(_SECRET_ENV_NAMES)),
                ctx.installation_token,
            )
            if value
        )
    )


def redact_results(
    results: Sequence[CheckResult], secret_values: Iterable[str]
) -> tuple[CheckResult, ...]:
    """Validate metadata and redact each display field without mutation.

    Args:
        results: Original checks in catalog order.
        secret_values: Exact secrets; one-shot iterables are supported.

    Returns:
        Fresh check results containing only redacted display fields.

    Raises:
        ReportRenderingError: If validation or redaction fails.
    """
    try:
        values = tuple(secret_values)
        if any(type(value) is not str for value in values):
            raise ValueError
        safe = []
        for result in results:
            if (
                type(result.check_id) is not str
                or not re.fullmatch(r"[A-Z][A-Z0-9_]*", result.check_id)
                or not isinstance(result.phase, Phase)
                or not isinstance(result.status, CheckStatus)
                or not isinstance(result.severity, Severity)
            ):
                raise ValueError
            fields = {}
            for name in ("title", "detail", "remediation"):
                value = getattr(result, name)
                if type(value) is not str:
                    raise ValueError
                fields[name] = redact_secrets(
                    value, extra_values=values, strict=True
                )
            safe.append(
                replace(
                    result,
                    title=fields["title"],
                    detail=fields["detail"],
                    remediation=fields["remediation"],
                )
            )
        return tuple(safe)
    except Exception:  # noqa: BLE001
        raise ReportRenderingError(_ERROR_MESSAGE) from None


def render_json(
    results: Sequence[CheckResult],
    phases: Sequence[Phase],
    provenance: Provenance | None,
    *,
    secret_values: Iterable[str] = (),
) -> str:
    """Render one complete JSON document followed by one newline.

    Args:
        results: Checks in catalog order.
        phases: Selected phases in execution order.
        provenance: Validated installed provenance, or unavailable.
        secret_values: Known exact credential values to remove.

    Returns:
        The complete serialized report, suitable for stdout.

    Raises:
        ReportRenderingError: If validation, redaction, or encoding fails.
    """
    try:
        document = build_document(
            redact_results(results, secret_values), phases, provenance
        )
        return json.dumps(document, separators=(",", ":")) + "\n"
    except Exception:  # noqa: BLE001
        raise ReportRenderingError(_ERROR_MESSAGE) from None


def render_text(
    results: Sequence[CheckResult], *, secret_values: Iterable[str] = ()
) -> str:
    """Render actionable check details for a human reader.

    Args:
        results: Checks in catalog order.
        secret_values: Known exact credential values to remove.

    Returns:
        A complete report with statuses, details, and remediation.

    Raises:
        ReportRenderingError: If validation or redaction fails.
    """
    try:
        safe = redact_results(results, secret_values)
        lines: list[str] = []
        for result in safe:
            lines.extend(
                (
                    f"[{result.status.value}] {result.check_id} "
                    f"({result.phase.value}, {result.severity.value}): "
                    f"{result.title}",
                    f"  {result.detail}",
                    f"  Remediation: {result.remediation}",
                )
            )
        return "\n".join(lines) + "\n"
    except Exception:  # noqa: BLE001
        raise ReportRenderingError(_ERROR_MESSAGE) from None
