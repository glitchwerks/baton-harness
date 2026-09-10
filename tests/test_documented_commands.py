"""Validate maintained command examples through real parsers before effects.

Doctor gates, installer forms, and build-mode cleanup are checked here.
Other Bash runbook commands remain outside this bounded contract.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from codereeve import cli
from codereeve.service_cutover import cli as installer

ROOT = Path(__file__).resolve().parents[1]
DOCS = (
    "README.md",
    "docs/repository-onboarding.md",
    "docs/codereeve-service-cutover.md",
)
pytestmark = pytest.mark.fast


class ParsedError(Exception):
    """Stop immediately after successful real argument parsing."""

    def __init__(self, namespace: argparse.Namespace) -> None:
        """Retain parsed values without entering any effectful code."""
        self.namespace = namespace


def _examples(text: str) -> list[list[str]]:
    """Extract doctor and installer commands from maintained Bash fences."""
    commands = []
    for block in re.findall(r"```(?:bash|sh)\n(.*?)```", text, re.S):
        for line in block.replace("\\\n", " ").splitlines():
            if line.lstrip().startswith("#"):
                continue
            if not re.search(
                r"(?:codereeve doctor|install-daemon-service\.sh)", line
            ):
                continue
            words = shlex.split(line)
            positions = [
                i
                for i, word in enumerate(words)
                if Path(word).name == "install-daemon-service.sh"
                or (
                    Path(word).name == "codereeve"
                    and words[i + 1 : i + 2] == ["doctor"]
                )
            ]
            assert len(positions) == 1, "unsupported executable example"
            args = words[positions[0] :]
            assert not any(word in {"|", ";", "&&", "||"} for word in args), (
                "unsupported compound executable example"
            )
            commands.append(args)
    return commands


def _parse(
    command: Sequence[str], monkeypatch: pytest.MonkeyPatch
) -> argparse.Namespace:
    """Route an actual example, intercepting only after its parser succeeds."""
    original = argparse.ArgumentParser.parse_args

    def stop(
        parser: argparse.ArgumentParser,
        args: Sequence[str] | None = None,
        namespace: argparse.Namespace | None = None,
    ) -> argparse.Namespace:
        """Validate all flags before stopping effects."""
        result = original(parser, args, namespace)
        raise ParsedError(result)

    with monkeypatch.context() as patch:
        patch.setattr(argparse.ArgumentParser, "parse_args", stop)
        with pytest.raises(ParsedError) as parsed:
            if Path(command[0]).name == "codereeve":
                result = cli.main(command[1:])
            elif Path(command[0]).name == "install-daemon-service.sh":
                result = installer.main(command[1:])
            else:
                raise AssertionError("unsupported documented command")
            raise AssertionError(
                f"command exited before argument validation: {result}"
            )
    return parsed.value.namespace


def _doctor_gate(namespace: argparse.Namespace) -> None:
    """Require explicit phase and strictness for every documented gate."""
    assert namespace.doctor and namespace.strict and namespace.phase


@pytest.mark.parametrize("document", DOCS)
def test_documented_doctor_gates(
    document: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing flags and broken canonical routing fail on actual examples."""
    examples = _examples((ROOT / document).read_text(encoding="utf-8"))
    doctors = [c for c in examples if Path(c[0]).name == "codereeve"]
    expected_count = 5 if document == DOCS[2] else 3
    assert len(doctors) == expected_count, "missing or unsupported doctor gate"
    phases = set()
    for command in doctors:
        namespace = _parse(command, monkeypatch)
        _doctor_gate(namespace)
        phases.update(namespace.phase)
    assert phases == {"installation", "configuration", "live"}


def test_documented_installer_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validate all documented installer modes."""
    text = (ROOT / DOCS[2]).read_text(encoding="utf-8")
    examples = [
        c for c in _examples(text) if "install-daemon-service.sh" in c[0]
    ]
    assert len(examples) == 3
    parsed = [_parse(command, monkeypatch) for command in examples]
    assert sum(bool(args.print_unit) for args in parsed) == 1
    assert sum(bool(args.no_start) for args in parsed) == 1
    assert sum(bool(args.recover) for args in parsed) == 1
    assert all(args.environment == "/opt/codereeve" for args in parsed)


@pytest.mark.parametrize(
    "command",
    [
        "codereeve doctor --phase invalid --strict",
        "codereeve doctor --invalid-flag --strict",
        "install-daemon-service.sh --invalid-flag",
        "install-daemon-service.sh --recover",
    ],
)
def test_invalid_documented_flags_fail_before_effects(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject invalid flags through real argument parsing."""
    with pytest.raises(SystemExit) as error:
        _parse(shlex.split(command), monkeypatch)
    assert error.value.code == 2


@pytest.mark.parametrize("arguments", ["--strict", "--phase live"])
def test_missing_doctor_gate_is_rejected(
    arguments: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Argparse defaults cannot silently weaken a documented cutover gate."""
    namespace = _parse(
        shlex.split(f"codereeve doctor {arguments}"), monkeypatch
    )
    with pytest.raises(AssertionError):
        _doctor_gate(namespace)


def test_unsupported_extraction_is_visible() -> None:
    """Compound shell examples require explicit support."""
    with pytest.raises(AssertionError, match="unsupported compound"):
        _examples("```bash\ncodereeve doctor --strict && touch marker\n```")


def test_unknown_documented_command_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown canonical command cannot bypass parser interception."""
    with pytest.raises(AssertionError, match="before argument validation"):
        _parse(["codereeve", "unknown-command"], monkeypatch)


@pytest.mark.parametrize("document", ["README.md", "docs/system-setup.md"])
def test_standard_build_recipe_clears_both_development_switches(
    document: str,
) -> None:
    """Execute the documented unset with both development flags inherited."""
    from tests.test_shell_identity import _bash_executable

    text = (ROOT / document).read_text(encoding="utf-8")
    commands = re.findall(
        r"^unset CODEREEVE_BUILD_DEVELOPMENT[^\n]*$", text, re.M
    )
    assert len(commands) == 1
    words = shlex.split(commands[0])
    switches = {"CODEREEVE_BUILD_DEVELOPMENT", "BH_BUILD_DEVELOPMENT"}
    assert set(words[1:]) <= switches
    environment = dict(os.environ)
    environment.update(dict.fromkeys(switches, "1"))
    process = subprocess.run(
        [
            str(_bash_executable()),
            "--noprofile",
            "--norc",
            "-c",
            commands[0]
            + '\ntest -z "${CODEREEVE_BUILD_DEVELOPMENT+x}"'
            + ' && test -z "${BH_BUILD_DEVELOPMENT+x}"',
        ],
        env=environment,
        check=False,
        capture_output=True,
        timeout=15,
    )
    assert process.returncode == 0, "recipe retained a development switch"


@pytest.mark.parametrize("contract", ["predecessor", "alias"])
def test_smoke_migration_guidance_names_real_compatibility_inputs(
    contract: str,
) -> None:
    """Migration guidance names supported runtime compatibility inputs."""
    from codereeve.config_env import PRODUCT_ALIASES
    from codereeve.service_cutover.systemd import OLD_UNIT

    text = (ROOT / "docs/smoke-test-daemon.md").read_text(encoding="utf-8")
    predecessor = re.search(r"recoverable cutover from `([^`]+)`", text)
    if contract == "predecessor":
        assert predecessor and predecessor.group(1) == OLD_UNIT
        return
    alias = next(
        spec.legacy
        for spec in PRODUCT_ALIASES
        if spec.canonical == "CODEREEVE_SETUP_NO_PROMPT"
    )
    deprecated = re.search(r"\(the temporary\s+`([^`]+)` alias", text)
    assert deprecated and deprecated.group(1) == alias
