"""Command-line contracts for read-only and reversible migrations."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from codereeve.config_env import EnvLayer
from codereeve.migration import (
    AppliedMigration,
    EvidenceState,
    MigrationContext,
    MigrationError,
    MigrationEvidence,
    RestorationResult,
    RestorationStatus,
    cli,
    restore_migration,
)
from codereeve.paths import PathLayout


def put(path: Path, text: str = "") -> None:
    """Create a regular migration fixture before invoking the CLI."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def context_for(tmp_path: Path) -> MigrationContext:
    """Build an isolated context whose host and system roots are temporary."""
    return MigrationContext(
        PathLayout.for_environment(
            tmp_path / "project",
            {},
            home=tmp_path / "home",
            etc_root=tmp_path / "etc",
        )
    )


def factory_for(context: MigrationContext) -> Callable[[], MigrationContext]:
    """Return a context factory compatible with the public CLI seam."""
    return lambda: context


def snapshot(root: Path) -> dict[str, bytes | None]:
    """Capture path types and file bytes without following links."""
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): (
            path.read_bytes() if path.is_file() else None
        )
        for path in sorted(root.rglob("*"))
    }


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--check", "--apply"],
        ["--check", "--format=json"],
        ["--apply", "--format", "yaml"],
        ["--checks"],
        ["--apply", "--format", "json", "extra"],
    ],
)
def test_rejects_noncanonical_grammar(argv: list[str]) -> None:
    """Only the documented closed token grammar may dispatch migration."""
    assert (
        cli.main(argv, context_factory=lambda: pytest.fail("dispatched")) == 2
    )


def test_help_names_modes_formats_and_statuses(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Migration help exposes the exact mode, format, and status contract."""
    assert cli.main(["--help"]) == 0
    output = capsys.readouterr().out
    assert (
        "codereeve migrate (--check | --apply) [--format text|json]" in output
    )
    assert "--check" in output
    assert "--apply" in output
    assert "current (0)" in output
    assert "ready (2)" in output
    assert "blocked (1)" in output
    assert "restore" not in output


@pytest.mark.parametrize(
    ("kind", "expected_status", "expected_exit"),
    [
        ("current", "current", 0),
        ("ready", "ready", 2),
        ("coexistence", "blocked", 1),
        ("collision", "blocked", 1),
        ("syntax", "blocked", 1),
        ("journal", "blocked", 1),
        ("environment", "blocked", 1),
        ("service", "blocked", 1),
        ("writer", "blocked", 1),
        ("lease", "blocked", 1),
        ("unsafe", "blocked", 1),
    ],
)
def test_check_text_and_json_share_status_actions_findings_and_exit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    expected_status: str,
    expected_exit: int,
) -> None:
    """Every local inventory class has equivalent text and schema-v1 JSON."""
    context = context_for(tmp_path)
    layout = context.layout
    if kind != "current":
        put(layout.legacy_config, "BH_REPO_OWNER=owner\n")
    if kind == "coexistence":
        put(layout.canonical_config)
    elif kind == "collision":
        put(layout.legacy_state / "config.env")
    elif kind == "syntax":
        put(layout.legacy_config, "BH_REPO_OWNER=$(private-command)\n")
    elif kind == "journal":
        put(
            layout.canonical_state.parent
            / ".codereeve-migration"
            / "incomplete"
            / "journal.jsonl"
        )
    elif kind == "environment":
        put(layout.legacy_config, "BH_REPO_OWNER=private-old\n")
        context = replace(
            context,
            layers=(
                EnvLayer(
                    "environment",
                    {"CODEREEVE_REPO_OWNER": "private-new"},
                ),
            ),
        )
    elif kind == "service":
        put(layout.legacy_unit)
    elif kind == "writer":
        context = replace(
            context,
            evidence=MigrationEvidence(writers=EvidenceState.BLOCKED),
        )
    elif kind == "lease":
        context = replace(
            context,
            evidence=MigrationEvidence(lease=EvidenceState.BLOCKED),
        )
    elif kind == "unsafe":
        layout.legacy_config.unlink()
        layout.legacy_config.mkdir()

    before = snapshot(tmp_path)
    factory_calls = 0

    def create_context() -> MigrationContext:
        """Count construction while returning the isolated real context."""
        nonlocal factory_calls
        factory_calls += 1
        return context

    monkeypatch.setattr(
        cli,
        "_apply",
        lambda _context: pytest.fail("check dispatched apply"),
    )

    text_exit = cli.main(["--check"], context_factory=create_context)
    text_output = capsys.readouterr().out
    json_exit = cli.main(
        ["--check", "--format", "json"], context_factory=create_context
    )
    json_output = capsys.readouterr().out
    payload = json.loads(json_output)

    assert (text_exit, json_exit) == (expected_exit, expected_exit)
    assert payload["schema_version"] == 1
    assert payload["status"] == expected_status
    assert payload["exit_code"] == expected_exit
    assert f"status: {payload['status']}" in text_output
    assert f"exit_code: {payload['exit_code']}" in text_output
    for action in payload["actions"]:
        assert action["code"] in text_output
        assert action["source"] in text_output
        assert action["destination"] in text_output
    for finding in payload["findings"]:
        assert finding["code"] in text_output
        assert finding["detail"] in text_output
        for path in finding["paths"]:
            assert path in text_output
    assert json_output == json.dumps(payload, sort_keys=True) + "\n"
    assert factory_calls == 2
    assert snapshot(tmp_path) == before
    assert "private-old" not in text_output + json_output
    assert "private-new" not in text_output + json_output


@pytest.mark.parametrize("output_format", ["text", "json"])
def test_apply_refuses_current_and_blocked_without_effects(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    output_format: str,
) -> None:
    """Apply reports blocked unless the transaction itself proves readiness."""
    for kind in ("current", "blocked"):
        context = context_for(tmp_path / kind)
        if kind == "blocked":
            put(context.layout.legacy_config)
            put(context.layout.canonical_config)
        before = snapshot(tmp_path / kind)
        assert (
            cli.main(
                ["--apply", "--format", output_format],
                context_factory=factory_for(context),
            )
            == 1
        )
        output = capsys.readouterr().out
        if output_format == "json":
            assert json.loads(output)["status"] == "blocked"
        else:
            assert "status: blocked" in output
        assert snapshot(tmp_path / kind) == before


@pytest.mark.parametrize("output_format", ["text", "json"])
def test_default_apply_refuses_without_authoritative_coordinator(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    output_format: str,
) -> None:
    """A fresh standalone apply cannot bypass coordinator quiescence proof."""
    context = context_for(tmp_path)
    put(context.layout.legacy_config, "BH_REPO_OWNER=owner\n")
    before = snapshot(tmp_path)

    assert (
        cli.main(
            ["--apply", "--format", output_format],
            context_factory=factory_for(context),
        )
        == 1
    )

    output = capsys.readouterr().out
    assert "blocked" in output
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("output_format", ["text", "json"])
def test_successful_apply_reports_all_recovery_evidence_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    output_format: str,
) -> None:
    """Successful output carries every retained path needed by #394."""
    context = context_for(tmp_path)
    transaction = tmp_path / "project" / ".codereeve-migration" / "txn"
    result = AppliedMigration(
        transaction / "manifest.json",
        (
            tmp_path / "project" / ".bh.codereeve-backup-txn",
            tmp_path / "home" / "host.env.codereeve-backup-txn",
        ),
    )
    calls: list[MigrationContext] = []

    def apply_once(value: MigrationContext) -> AppliedMigration:
        """Return durable evidence from the internal transaction wrapper."""
        calls.append(value)
        return result

    monkeypatch.setattr(cli, "_apply", apply_once)
    assert (
        cli.main(
            ["--apply", "--format", output_format],
            context_factory=factory_for(context),
        )
        == 0
    )
    output = capsys.readouterr().out

    if output_format == "json":
        payload = json.loads(output)
        assert payload["schema_version"] == 1
        assert payload["status"] == "applied"
        assert payload["exit_code"] == 0
        assert payload["manual_restoration_paths"] == [
            str(result.manifest_path),
            str(result.manifest_path.with_name("journal.jsonl")),
            *(str(path) for path in result.backups),
        ]
    else:
        for path in (
            result.manifest_path,
            result.manifest_path.with_name("journal.jsonl"),
            *result.backups,
        ):
            assert str(path) in output
        assert "manual_restoration_path:" in output
    assert calls == [context]


@pytest.mark.parametrize(
    ("restoration", "expected_status"),
    [
        (
            RestorationResult(
                RestorationStatus.COMPLETE, Path("safe/manifest.json")
            ),
            "complete",
        ),
        (
            RestorationResult(
                RestorationStatus.INCOMPLETE,
                Path("safe/manifest.json"),
                "manual recovery required",
            ),
            "incomplete",
        ),
        (
            RestorationResult(
                RestorationStatus.NOT_NEEDED,
                Path("safe/manifest.json"),
                "already-restored state freshly revalidated",
            ),
            "not_needed",
        ),
        (None, None),
    ],
)
@pytest.mark.parametrize("output_format", ["text", "json"])
def test_apply_failure_reports_only_accurate_safe_recovery_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    restoration: RestorationResult | None,
    expected_status: str | None,
    output_format: str,
) -> None:
    """Failure output distinguishes all recovery evidence states."""
    secret = "fixture-super-secret"

    def fail(_context: MigrationContext) -> AppliedMigration:
        """Raise the public safe failure while hiding private cause text."""
        try:
            raise OSError(secret)
        except OSError:
            raise MigrationError(
                "migration failed; inspect restoration evidence", restoration
            ) from None

    monkeypatch.setattr(cli, "_apply", fail)
    assert (
        cli.main(
            ["--apply", "--format", output_format],
            context_factory=factory_for(context_for(tmp_path)),
        )
        == 1
    )
    output = capsys.readouterr().out
    assert secret not in output
    assert "applied" not in output
    if output_format == "json":
        payload = json.loads(output)
        assert payload["schema_version"] == 1
        assert payload["status"] == "blocked"
        assert payload["exit_code"] == 1
        if restoration is None:
            assert payload["restoration"] is None
        else:
            assert payload["restoration"]["status"] == expected_status
            assert payload["restoration"]["manifest_path"] == str(
                restoration.manifest_path
            )
    elif restoration is None:
        assert "status: blocked" in output
        assert "restoration_status: unavailable" in output
    else:
        assert "status: blocked" in output
        assert f"restoration_status: {expected_status}" in output


def test_unexpected_apply_failure_is_generic_and_secret_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unexpected exceptions cannot expose their type or message."""
    secret = "opaque-private-value"

    def fail(_context: MigrationContext) -> AppliedMigration:
        """Represent an unexpected implementation failure."""
        raise RuntimeError(secret)

    monkeypatch.setattr(cli, "_apply", fail)
    assert (
        cli.main(
            ["--apply", "--format", "json"],
            context_factory=factory_for(context_for(tmp_path)),
        )
        == 1
    )
    output = capsys.readouterr().out
    assert secret not in output
    assert "RuntimeError" not in output
    assert json.loads(output)["diagnostic"] == (
        "migration failed safely; inspect retained evidence"
    )


def test_default_context_uses_exact_operator_project_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared factory resolves the project pair exactly once."""
    monkeypatch.setenv("CODEREEVE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("BH_PROJECT_ROOT", str(tmp_path))
    context = cli.default_context()
    assert context.layout.canonical_state == tmp_path / ".codereeve"
    assert context.layers[0].source == "environment"


@pytest.mark.parametrize(
    ("canonical", "source"),
    [
        ("CODEREEVE_REPO_OWNER", "managed"),
        ("CODEREEVE_REPO_OWNER", "host"),
        ("CODEREEVE_PROJECT_ROOT", "managed"),
        ("CODEREEVE_PROJECT_ROOT", "host"),
    ],
)
def test_operator_legacy_conflict_with_lower_canonical_blocks_before_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    canonical: str,
    source: str,
) -> None:
    """Raw operator spellings survive bootstrap for layered comparison."""
    project = tmp_path / "operator-project"
    lower_project = tmp_path / "lower-project"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for key in (
        "CODEREEVE_PROJECT_ROOT",
        "BH_PROJECT_ROOT",
        "CODEREEVE_REPO_OWNER",
        "BH_REPO_OWNER",
    ):
        monkeypatch.delenv(key, raising=False)
    if canonical == "CODEREEVE_PROJECT_ROOT":
        monkeypatch.setenv("BH_PROJECT_ROOT", str(project))
        lower_value = str(lower_project)
    else:
        monkeypatch.setenv("CODEREEVE_PROJECT_ROOT", str(project))
        monkeypatch.setenv("BH_REPO_OWNER", "operator-private")
        lower_value = "lower-private"
    layout = PathLayout.for_environment(
        project,
        {"XDG_CONFIG_HOME": str(tmp_path / "config")},
    )
    target = (
        layout.legacy_config if source == "managed" else layout.canonical_host
    )
    put(target, f"{canonical}={lower_value}\n")
    before = snapshot(tmp_path)

    assert cli.main(["--check", "--format", "json"]) == 1
    check_payload = json.loads(capsys.readouterr().out)
    assert check_payload["status"] == "blocked"
    assert cli.main(["--apply", "--format", "json"]) == 1
    apply_payload = json.loads(capsys.readouterr().out)

    assert apply_payload["status"] == "blocked"
    assert apply_payload["exit_code"] == 1
    rendered = json.dumps((check_payload, apply_payload))
    assert "operator-private" not in rendered
    assert "lower-private" not in rendered
    assert snapshot(tmp_path) == before


def test_identical_operator_legacy_and_host_canonical_pair_is_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An identical cross-source project pair remains valid and raw."""
    project = tmp_path / "project"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("CODEREEVE_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("BH_PROJECT_ROOT", str(project))
    host = tmp_path / "config" / "codereeve" / "host.env"
    put(host, f"CODEREEVE_PROJECT_ROOT={project}\n")

    context = cli.default_context()

    assert "BH_PROJECT_ROOT" in context.layers[0].values
    assert "CODEREEVE_PROJECT_ROOT" not in context.layers[0].values
    assert cli.main(["--check", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "current"


def test_operator_legacy_spelling_retains_precedence_over_managed_legacy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Preserving raw spellings does not change within-spelling precedence."""
    project = tmp_path / "project"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CODEREEVE_PROJECT_ROOT", str(project))
    monkeypatch.delenv("CODEREEVE_REPO_OWNER", raising=False)
    monkeypatch.setenv("BH_REPO_OWNER", "operator-private")
    layout = PathLayout.for_environment(project, {})
    put(layout.legacy_config, "BH_REPO_OWNER=managed-private\n")

    assert cli.main(["--check", "--format", "json"]) == 2
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "ready"
    assert not any(
        finding["code"] == "environment_conflict"
        for finding in payload["findings"]
    )
    assert "operator-private" not in json.dumps(payload)
    assert "managed-private" not in json.dumps(payload)


def test_default_context_bootstraps_project_from_selected_host_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Host config supplies the project when operator environment omits it."""
    project = tmp_path / "managed-project"
    current = tmp_path / "unrelated-directory"
    current.mkdir()
    monkeypatch.chdir(current)
    monkeypatch.delenv("CODEREEVE_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("BH_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    host = tmp_path / "config" / "baton-harness" / "host.env"
    put(host, f"BH_PROJECT_ROOT={project}\n")

    context = cli.default_context()

    assert context.layout.canonical_state == project / ".codereeve"


def test_restoration_api_remains_importable_for_coordinator() -> None:
    """The CLI addition leaves #394's restoration API available."""
    assert callable(restore_migration)
