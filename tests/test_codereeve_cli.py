"""Tests for the unified CodeReeve command router."""

import pytest

import codereeve
from codereeve import cli


@pytest.mark.parametrize(
    ("argv", "handler", "forwarded"),
    [
        (["daemon", "--once"], "daemon", ["--once"]),
        (["doctor", "--strict"], "doctor", ["--strict"]),
        (["provenance"], "provenance", []),
        (["migrate", "--check"], "migrate", ["--check"]),
        (["verify", "--python", "3.13"], "verify", ["--python", "3.13"]),
        (["hook", "after-create"], "hook_after_create", []),
        (["hook", "before-run"], "hook_before_run", []),
        (["hook", "after-run"], "hook_after_run", []),
        (
            ["hook", "force-pr-not-merge"],
            "hook_force_pr_not_merge",
            [],
        ),
    ],
)
def test_routes_to_exact_handler(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    handler: str,
    forwarded: list[str],
) -> None:
    """Each public command forwards only its remaining arguments."""
    calls: list[list[str]] = []

    def fake(args: list[str]) -> int:
        calls.append(args)
        return 17

    monkeypatch.setitem(cli.HANDLERS, handler, fake)
    assert cli.main(argv) == 17
    assert calls == [forwarded]


def test_unknown_command_is_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unknown top-level command exits with a usage error."""
    assert cli.main(["unknown"]) == 2
    assert "usage: codereeve" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["migrate"],
        ["migrate", "--check", "--apply"],
        ["migrate", "--check", "--format=json"],
        ["migrate", "--apply", "--format", "yaml"],
        ["migrate", "--checks"],
    ],
)
def test_migrate_rejects_noncanonical_grammar(argv: list[str]) -> None:
    """The unified router preserves the migration command's closed grammar."""
    assert cli.main(argv) == 2


@pytest.mark.parametrize(
    "alias",
    ["after_create", "before_run", "after_run", "force_pr_not_merge"],
)
def test_rejects_underscore_hook_alias_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    alias: str,
) -> None:
    """Underscore aliases cannot dispatch lifecycle hook handlers."""
    calls: list[list[str]] = []

    def fake(args: list[str]) -> int:
        calls.append(args)
        return 17

    for handler in (
        "hook_after_create",
        "hook_before_run",
        "hook_after_run",
        "hook_force_pr_not_merge",
    ):
        monkeypatch.setitem(cli.HANDLERS, handler, fake)

    assert cli.main(["hook", alias]) == 2
    assert calls == []


@pytest.mark.parametrize(
    "alias",
    ["force-pr_not-merge", "force_pr-not_merge"],
)
def test_rejects_mixed_separator_hook_alias_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    alias: str,
) -> None:
    """Mixed hyphen/underscore aliases cannot dispatch hook handlers."""
    calls: list[list[str]] = []

    def fake(args: list[str]) -> int:
        calls.append(args)
        return 17

    monkeypatch.setitem(cli.HANDLERS, "hook_force_pr_not_merge", fake)

    assert cli.main(["hook", alias]) == 2
    assert calls == []


def test_doctor_preserves_strict_failure_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The router returns the doctor's strict failure status unchanged."""
    monkeypatch.setitem(cli.HANDLERS, "doctor", lambda _args: 1)
    assert cli.main(["doctor", "--strict"]) == 1


def test_version_prints_canonical_distribution_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The canonical version flag identifies the CodeReeve command."""
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out == f"codereeve {codereeve.__version__}\n"


def test_help_lists_every_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Top-level help exposes every supported canonical command."""
    assert cli.main(["--help"]) == 0
    output = capsys.readouterr().out
    for command in (
        "daemon",
        "doctor",
        "provenance",
        "migrate",
        "hook after-create",
        "hook before-run",
        "hook after-run",
        "hook force-pr-not-merge",
        "verify",
    ):
        assert command in output


def test_absent_command_is_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Calling CodeReeve without a command exits with usage status 2."""
    assert cli.main([]) == 2
    assert "usage: codereeve" in capsys.readouterr().err
