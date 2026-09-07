"""Schema, output parity, and secret-boundary tests for doctor reports."""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterator
from dataclasses import FrozenInstanceError, replace
from types import ModuleType
from typing import Literal, NoReturn, TypedDict

import pytest

from baton_harness.chain.doctor import (
    CheckResult,
    CheckStatus,
    DoctorContext,
    Phase,
    Severity,
)
from baton_harness.provenance import Provenance


class _HumanFields(TypedDict, total=False):
    """Typed display-field overrides for immutable check fixtures."""

    title: str
    detail: str
    remediation: str


@pytest.fixture
def report() -> ModuleType:
    """Load the feature lazily so missing implementation fails a test."""
    try:
        return importlib.import_module("baton_harness.chain.doctor_report")
    except ModuleNotFoundError:
        pytest.fail("doctor report implementation is missing")


@pytest.fixture
def results() -> tuple[CheckResult, ...]:
    """Supply literal catalog-ordered check outcomes."""
    return (
        CheckResult(
            "PKG_PROVENANCE",
            Phase.INSTALLATION,
            "Identity",
            Severity.CRITICAL,
            CheckStatus.PASS,
            "Validated",
            "Reinstall",
        ),
        CheckResult(
            "CFG_FILE",
            Phase.CONFIGURATION,
            "Config",
            Severity.CRITICAL,
            CheckStatus.FAIL,
            "Missing",
            "Create config",
        ),
    )


def test_complete_json_document(
    report: ModuleType, results: tuple[CheckResult, ...]
) -> None:
    """JSON preserves check order and the public schema exactly."""
    provenance = Provenance(1, "1.0.0", "a" * 40, "sha256:" + "b" * 64, False)
    phases = (Phase.INSTALLATION, Phase.CONFIGURATION)
    rendered = report.render_json(results, phases, provenance)
    expected = {
        "schema_version": 1,
        "provenance": {
            "package_version": "1.0.0",
            "source_revision": "a" * 40,
            "lock_identity": "sha256:" + "b" * 64,
            "development": False,
        },
        "selected_phases": ["installation", "configuration"],
        "summary": {
            "pass": 1,
            "fail": 1,
            "warn": 0,
            "skip": 0,
            "critical_failures": 1,
        },
        "checks": [
            {
                "id": "PKG_PROVENANCE",
                "phase": "installation",
                "status": "pass",
                "severity": "critical",
                "title": "Identity",
                "detail": "Validated",
                "remediation": "Reinstall",
            },
            {
                "id": "CFG_FILE",
                "phase": "configuration",
                "status": "fail",
                "severity": "critical",
                "title": "Config",
                "detail": "Missing",
                "remediation": "Create config",
            },
        ],
    }
    assert json.loads(rendered) == expected
    assert rendered == json.dumps(expected, separators=(",", ":")) + "\n"
    assert report.build_document(results, phases, provenance) == expected


def test_null_provenance_and_summary(
    report: ModuleType, results: tuple[CheckResult, ...]
) -> None:
    """Counts distinguish outcomes from critical failures."""
    outcomes = results + (
        replace(
            results[1], status=CheckStatus.FAIL, severity=Severity.WARNING
        ),
        replace(results[1], status=CheckStatus.WARN),
        replace(results[1], status=CheckStatus.SKIP),
    )
    document = json.loads(report.render_json(outcomes, (Phase.LIVE,), None))
    assert document["provenance"] is None
    assert document["selected_phases"] == ["live"]
    assert document["summary"] == {
        "pass": 1,
        "fail": 2,
        "warn": 1,
        "skip": 1,
        "critical_failures": 1,
    }
    summary = report.summarize(outcomes)
    assert summary.critical_failures == 1
    with pytest.raises(FrozenInstanceError):
        summary.critical_failures = 99
    assert json.loads(report.render_json((), (), None))["summary"] == {
        "pass": 0,
        "fail": 0,
        "warn": 0,
        "skip": 0,
        "critical_failures": 0,
    }


@pytest.mark.parametrize("field", ["title", "detail", "remediation"])
@pytest.mark.parametrize(
    "secret",
    [
        "ghp_abcdef123456",
        "exact opaque credential",
        "-----BEGIN PRIVATE KEY-----\nmaterial\n-----END PRIVATE KEY-----",
        "https://user:password@example.test/path",
        "https://:opaque-password@example.test/path",
        "https://opaque-credential@example.test/path",
        "https://example.test/?access_token=oauth-value",
        '{"refresh_token":"refresh-value"}',
    ],
)
def test_both_formats_redact_every_human_field(
    report: ModuleType,
    results: tuple[CheckResult, ...],
    field: Literal["title", "detail", "remediation"],
    secret: str,
) -> None:
    """Neither serializer can leak credentials through any display field."""
    overrides: _HumanFields = {}
    overrides[field] = f"before {secret} after"
    original = replace(results[0], **overrides)
    selected = (original, original)
    # A one-shot iterable must work across fields and multiple results.
    text = report.render_text(
        selected, secret_values=iter(["exact opaque credential"])
    )
    encoded = report.render_json(
        selected,
        (Phase.INSTALLATION,),
        None,
        secret_values=iter(["exact opaque credential"]),
    )
    decoded = json.loads(encoded)
    for output in (
        text,
        decoded["checks"][0][field],
        decoded["checks"][1][field],
    ):
        assert secret not in output
        for value in ("material", "password", "oauth-value", "refresh-value"):
            assert value not in output
        assert "«redacted»" in output
    assert getattr(original, field) == f"before {secret} after"


@pytest.mark.parametrize("field", ["title", "detail", "remediation"])
@pytest.mark.parametrize(
    ("secret", "values"),
    [
        ("opaque-token-long", ("opaque-token", "opaque-token-long")),
        ("opaque-token-long", ("opaque-token-long", "opaque-token")),
        ("abcdef", ("abcd", "cdef")),
        ("abcdef", ("cdef", "abcd")),
    ],
)
def test_report_redacts_overlapping_values_completely(
    report: ModuleType,
    results: tuple[CheckResult, ...],
    field: Literal["title", "detail", "remediation"],
    secret: str,
    values: tuple[str, ...],
) -> None:
    """All fields and repeated checks hide complete overlapping spans."""
    overrides: _HumanFields = {}
    overrides[field] = f"before {secret} after"
    original = replace(results[0], **overrides)
    selected = (original, original)
    text = report.render_text(selected, secret_values=iter(values))
    document = json.loads(
        report.render_json(
            selected,
            (Phase.INSTALLATION,),
            None,
            secret_values=iter(values),
        )
    )
    assert text.count("before «redacted» after") == 2
    for check in document["checks"]:
        assert check[field] == "before «redacted» after"


def test_text_contains_status_details_and_remediation(
    report: ModuleType, results: tuple[CheckResult, ...]
) -> None:
    """Human output carries the same actionable information as JSON."""
    rendered = report.render_text(results)
    for value in (
        "PKG_PROVENANCE",
        "CFG_FILE",
        "pass",
        "fail",
        "Identity",
        "Config",
        "Validated",
        "Missing",
        "Reinstall",
        "Create config",
    ):
        assert value in rendered
    assert rendered.index("PKG_PROVENANCE") < rendered.index("CFG_FILE")
    assert rendered.endswith("\n") and not rendered.endswith("\n\n")


def test_context_collects_credentials_only(report: ModuleType) -> None:
    """Exact inputs include ambient credentials and the token held by value."""

    def unused(*args: object, **kwargs: object) -> NoReturn:
        """Reject any secret collection side effect."""
        raise AssertionError("Secret collection must not probe anything")

    ctx = DoctorContext(
        "/project",
        "/home",
        {
            "GH_TOKEN": "gh-value",
            "GITHUB_TOKEN": "github-value",
            "BWS_ACCESS_TOKEN": "vault-value",
            "ANTHROPIC_API_KEY": "api-value",
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-value",
            "BH_HEARTBEAT_PING_URL": "ping-value",
            "PATH": "/bin",
            "BH_PROJECT_ROOT": "/project",
            "EMPTY_TOKEN": "",
            "BWS_PEM_SECRET_ID": "public-id",
            "BH_GITHUB_APP_PRIVATE_KEY_FILE": "/key.pem",
        },
        unused,
        unused,
        unused,
        unused,
        installation_token="install-value",
    )
    assert set(report.secret_values_from_context(ctx)) == {
        "gh-value",
        "github-value",
        "vault-value",
        "api-value",
        "oauth-value",
        "ping-value",
        "install-value",
    }


@pytest.mark.parametrize(
    "kind", ["redaction", "pattern", "json", "iterator", "invalid_field"]
)
def test_render_failure_is_fixed_and_never_partial(
    report: ModuleType,
    results: tuple[CheckResult, ...],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """Unexpected rendering failures expose only a safe boundary error."""

    def broken(*args: object, **kwargs: object) -> NoReturn:
        """Raise a sensitive failure from a rendering dependency."""
        raise RuntimeError("private exception secret")

    def broken_values() -> Iterator[str]:
        """Fail halfway through enumerating known secrets."""
        yield "first"
        raise RuntimeError("private exception secret")

    secrets: Iterator[str] = iter(())
    if kind == "redaction":
        monkeypatch.setattr(report, "redact_secrets", broken)
    elif kind == "pattern":
        from baton_harness import redact

        class BrokenPattern:
            """Simulate substitution failing inside the redaction helper."""

            sub = staticmethod(broken)

        monkeypatch.setattr(redact, "_TOKEN_PATTERN", BrokenPattern())
    elif kind == "json":
        monkeypatch.setattr(report.json, "dumps", broken)
    elif kind == "iterator":
        secrets = broken_values()
    else:
        results = (replace(results[0], title=123),)  # type: ignore[arg-type]
    with pytest.raises(report.ReportRenderingError) as error:
        report.render_json(
            results, (Phase.INSTALLATION,), None, secret_values=secrets
        )
    assert "secret" not in str(error.value)
    assert error.value.__suppress_context__
    if kind != "json":
        with pytest.raises(report.ReportRenderingError):
            report.render_text(
                results,
                secret_values=broken_values() if kind == "iterator" else (),
            )
