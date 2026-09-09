"""Tests for the private CodeReeve service installer command surface."""

from __future__ import annotations

import importlib
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest

from codereeve.chain.app_private_key import AppPrivateKeyProvider
from codereeve.chain.sandbox_config import SandboxConfig
from codereeve.config_env import ResolvedEnvironment
from codereeve.service_cutover.model import (
    CutoverError,
    CutoverResult,
    FreshSecrets,
    ServiceSpec,
)


def _spec() -> ServiceSpec:
    """Return a complete Linux service selection for CLI dispatch tests."""

    def path(value: str) -> Path:
        return cast(Path, PurePosixPath(value))

    return ServiceSpec(
        project_root=path("/srv/project"),
        environment=path("/opt/codereeve"),
        run_user="codereeve",
        workflow=path("/srv/harness/config/WORKFLOW.md"),
        secrets=path("/etc/codereeve/secrets.env"),
        home=path("/home/codereeve"),
    )


def test_print_unit_dispatches_only_pure_render(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Print mode renders the unit without invoking installation or cutover."""
    try:
        module = importlib.import_module("codereeve.service_cutover.cli")
    except ModuleNotFoundError:
        pytest.fail("service cutover CLI is not implemented")
    calls: list[str] = []
    spec = _spec()
    monkeypatch.setattr(
        module, "_installation_inputs", lambda _args, _env: (spec, None)
    )
    monkeypatch.setattr(
        module,
        "render_only",
        lambda selected: calls.append("render") or "rendered-unit\n",
    )
    monkeypatch.setattr(
        module,
        "install_only",
        lambda *_args, **_kwargs: calls.append("install"),
    )
    monkeypatch.setattr(
        module,
        "cutover",
        lambda *_args, **_kwargs: calls.append("cutover"),
    )

    assert module.main(["--print-unit"], environment={}) == 0

    output = capsys.readouterr()
    assert output.out == "rendered-unit\n"
    assert output.err == ""
    assert calls == ["render"]


def test_no_start_reports_reversible_installation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No-start returns success only for the coordinator's installed result."""
    module = importlib.import_module("codereeve.service_cutover.cli")
    journal = Path("/srv/project/.codereeve-cutover/tx/journal.jsonl")
    monkeypatch.setattr(
        module,
        "_installation_inputs",
        lambda _args, _env: (_spec(), None),
    )
    monkeypatch.setattr(
        module,
        "install_only",
        lambda _spec, **_kwargs: CutoverResult("installed", journal, None),
    )
    monkeypatch.setattr(
        module,
        "cutover",
        lambda *_args, **_kwargs: pytest.fail("cutover must not run"),
    )

    assert module.main(["--no-start"], environment={}) == 0

    output = capsys.readouterr()
    assert "status: installed" in output.out
    assert str(journal) in output.out
    assert output.err == ""


@pytest.mark.parametrize(
    ("status", "recovery", "expected"),
    [
        ("committed", None, 0),
        ("failed", "complete", 1),
        ("incomplete", "incomplete", 1),
    ],
)
def test_cutover_status_controls_process_exit(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    recovery: str | None,
    expected: int,
) -> None:
    """Activation succeeds only for the coordinator's committed status."""
    module = importlib.import_module("codereeve.service_cutover.cli")
    secret = FreshSecrets(b"BWS_ACCESS_TOKEN=fixture-token\n")
    monkeypatch.setattr(
        module,
        "_installation_inputs",
        lambda _args, _env: (_spec(), secret),
    )
    observed: list[FreshSecrets | None] = []
    monkeypatch.setattr(
        module,
        "cutover",
        lambda _spec, *, fresh_secrets: (
            observed.append(fresh_secrets)
            or CutoverResult(status, Path("/private/journal.jsonl"), recovery)
        ),
    )

    assert module.main([], environment={}) == expected
    assert observed == [secret]


def test_recover_uses_only_the_explicit_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery bypasses fresh installation and returns incomplete nonzero."""
    module = importlib.import_module("codereeve.service_cutover.cli")
    journal = Path("/private/recovery/journal.jsonl")
    monkeypatch.setattr(
        module,
        "_installation_inputs",
        lambda *_args: pytest.fail("recovery must not resolve installation"),
    )
    monkeypatch.setattr(
        module,
        "recover",
        lambda selected: CutoverResult("incomplete", selected, "incomplete"),
    )

    assert module.main(["--recover", str(journal)], environment={}) == 1


def test_cutover_error_is_value_safe_and_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Coordinator refusal reports a redacted diagnostic and exits nonzero."""
    module = importlib.import_module("codereeve.service_cutover.cli")
    sentinel = "secret-sentinel-never-print"
    monkeypatch.setattr(
        module,
        "_installation_inputs",
        lambda _args, _env: (_spec(), None),
    )
    monkeypatch.setattr(
        module,
        "cutover",
        Mock(side_effect=CutoverError("service selection is unsupported")),
    )

    assert module.main([], environment={"BWS_ACCESS_TOKEN": sentinel}) == 1
    output = capsys.readouterr()
    assert "service selection is unsupported" in output.err
    assert sentinel not in output.out + output.err


def test_fresh_bws_token_is_encoded_only_for_coordinator() -> None:
    """A supported literal BWS token becomes bounded ephemeral bytes."""
    module = importlib.import_module("codereeve.service_cutover.cli")

    secret = module._fresh_secrets(
        {"BWS_ACCESS_TOKEN": "fixture:token+value="},
        required=True,
        selected_path=Path("/etc/codereeve/secrets.env"),
        render_only=False,
        path_exists=lambda _path: False,
        interactive=lambda: False,
    )

    assert secret == FreshSecrets(b"BWS_ACCESS_TOKEN=fixture:token+value=\n")


@pytest.mark.parametrize(
    "no_prompt_name",
    ["CODEREEVE_SETUP_NO_PROMPT", "BH_SETUP_NO_PROMPT"],
)
def test_no_prompt_missing_token_fails_before_prompt_or_coordinator(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_prompt_name: str,
) -> None:
    """Both no-prompt names refuse a missing token without any effects."""
    module = importlib.import_module("codereeve.service_cutover.cli")
    environment = {no_prompt_name: "1"}

    monkeypatch.setattr(module, "_is_interactive", lambda: True)
    monkeypatch.setattr(
        module.getpass,
        "getpass",
        lambda _prompt: pytest.fail("no-prompt mode must not request a token"),
    )

    def installation_inputs(
        _args: object, values: dict[str, str]
    ) -> tuple[ServiceSpec, FreshSecrets | None]:
        fresh = module._fresh_secrets(
            values,
            required=True,
            selected_path=Path("/etc/codereeve/secrets.env"),
            render_only=False,
            path_exists=lambda _path: False,
            interactive=lambda: True,
        )
        return _spec(), fresh

    monkeypatch.setattr(module, "_installation_inputs", installation_inputs)
    monkeypatch.setattr(
        module,
        "cutover",
        lambda *_args, **_kwargs: pytest.fail(
            "coordinator must not run without a required token"
        ),
    )

    assert module.main([], environment=environment) == 1
    output = capsys.readouterr()
    assert "BWS_ACCESS_TOKEN is required" in output.err
    assert "status:" not in output.out


def test_file_provider_never_reads_or_creates_fresh_secrets() -> None:
    """A configuration without BWS passes no secret content downstream."""
    module = importlib.import_module("codereeve.service_cutover.cli")

    assert (
        module._fresh_secrets(
            {"BWS_ACCESS_TOKEN": "unused-sentinel"},
            required=False,
            selected_path=None,
            render_only=False,
            path_exists=lambda _path: pytest.fail("must not inspect a path"),
            interactive=lambda: False,
        )
        is None
    )


def test_unsupported_bws_token_fails_before_coordinator_effects() -> None:
    """Tokens outside the conservative assignment grammar fail value-free."""
    module = importlib.import_module("codereeve.service_cutover.cli")
    sentinel = "unsupported token with spaces"

    with pytest.raises(CutoverError) as caught:
        module._fresh_secrets(
            {"BWS_ACCESS_TOKEN": sentinel},
            required=True,
            selected_path=Path("/etc/codereeve/secrets.env"),
            render_only=False,
            path_exists=lambda _path: False,
            interactive=lambda: False,
        )

    assert sentinel not in str(caught.value)


def test_print_unit_never_resolves_fresh_secret_bytes() -> None:
    """Render mode needs only the path and never reads a bootstrap token."""
    module = importlib.import_module("codereeve.service_cutover.cli")

    assert (
        module._fresh_secrets(
            {},
            required=True,
            selected_path=Path("/etc/codereeve/secrets.env"),
            render_only=True,
            path_exists=lambda _path: pytest.fail("must not inspect a path"),
            interactive=lambda: pytest.fail("must not prompt"),
        )
        is None
    )


def test_installation_inputs_use_shared_config_and_separate_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI selections retain overrides and provider policy in ServiceSpec."""
    module = importlib.import_module("codereeve.service_cutover.cli")
    captured: list[dict[str, str]] = []
    final_values = {
        "CODEREEVE_PROJECT_ROOT": "/srv/project",
        "CODEREEVE_DAEMON_SECRETS_PATH": "/etc/codereeve/secrets.env",
        "BWS_ACCESS_TOKEN": "fixture-token",
    }
    configured = SimpleNamespace(
        config=SandboxConfig(
            "owner",
            "repo",
            "1",
            "2",
            AppPrivateKeyProvider.BWS,
            "11111111-1111-1111-1111-111111111111",
            None,
        ),
        environment=ResolvedEnvironment(final_values, ()),
    )

    def resolve(
        _explicit: object, values: dict[str, str], _layout: object
    ) -> object:
        captured.append(dict(values))
        return configured

    monkeypatch.setattr(module, "resolve_config_sources", resolve)
    monkeypatch.setattr(
        module, "_account_home", lambda _user: Path("/home/runner")
    )
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda path: path.name == "WORKFLOW.md",
    )
    args = module._parser().parse_args(
        [
            "--harness-dir",
            "/srv/harness",
            "--project-root",
            "/srv/project",
            "--environment",
            "/opt/codereeve",
            "--user",
            "runner",
        ]
    )

    spec, secret = module._installation_inputs(args, final_values)

    assert spec.project_root.as_posix() == "/srv/project"
    assert spec.environment.as_posix() == "/opt/codereeve"
    assert spec.run_user == "runner"
    assert spec.workflow is not None
    assert spec.workflow.as_posix() == "/srv/harness/config/WORKFLOW.md"
    assert spec.secrets is not None
    assert spec.secrets.as_posix() == "/etc/codereeve/secrets.env"
    assert spec.home.as_posix() == "/home/runner"
    assert secret == FreshSecrets(b"BWS_ACCESS_TOKEN=fixture-token\n")
    assert captured[0]["CODEREEVE_PROJECT_ROOT"] == "/srv/project"


def test_alias_conflict_fails_before_coordinator_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonical and compatibility values cannot disagree before effects."""
    module = importlib.import_module("codereeve.service_cutover.cli")
    monkeypatch.setattr(
        module,
        "cutover",
        lambda *_args, **_kwargs: pytest.fail("coordinator must not run"),
    )

    status = module.main(
        [],
        environment={
            "CODEREEVE_PROJECT_ROOT": "/srv/one",
            "BH_PROJECT_ROOT": "/srv/two",
        },
    )

    assert status == 1


def test_interactive_cancellation_has_no_coordinator_effect(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Declining the single safe prompt exits without installation."""
    module = importlib.import_module("codereeve.service_cutover.cli")
    monkeypatch.setattr(
        module,
        "_installation_inputs",
        lambda _args, _env: (_spec(), None),
    )
    monkeypatch.setattr(module, "_confirm", lambda _env: False)
    monkeypatch.setattr(
        module,
        "cutover",
        lambda *_args, **_kwargs: pytest.fail("coordinator must not run"),
    )

    assert module.main([], environment={}) == 0
    assert "status: cancelled" in capsys.readouterr().out
