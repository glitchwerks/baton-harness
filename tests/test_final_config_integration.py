"""Cross-entrypoint regressions from the final #393 integration review."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codereeve.chain import doctor
from codereeve.chain.daemon.poll import run_daemon
from codereeve.chain.obs_config import load_obs_config
from codereeve.chain.registry import RepoConfig
from codereeve.chain.ruleset_status import publish_baseline
from codereeve.chain.sandbox_config import resolve_config_sources
from codereeve.config_env import AliasConflictError, EnvLayer
from codereeve.migration.cli import main
from codereeve.migration.inventory import inventory_migration
from codereeve.migration.lease import WriterLease, probe_writer_lease
from codereeve.migration.model import (
    EvidenceState,
    MigrationContext,
    MigrationEvidence,
)
from codereeve.migration.transaction import (
    FileOperations,
    apply_migration,
    restore_migration,
)
from codereeve.paths import PathConflictError, PathLayout, select_runtime_paths
from codereeve.vendor.symphony.config import WorkflowConfig

ROOT = Path(__file__).resolve().parents[1]
CONFIG = (
    "CODEREEVE_REPO_OWNER=managed-owner\n"
    "CODEREEVE_REPO_NAME=repo\n"
    "CODEREEVE_GITHUB_APP_ID=123\n"
    "CODEREEVE_GITHUB_APP_INSTALLATION_ID=456\n"
    "CODEREEVE_GITHUB_APP_KEY_PROVIDER=bws\n"
    "BWS_PEM_SECRET_ID=11111111-1111-1111-1111-111111111111\n"
)


def put(path: Path, content: str) -> None:
    """Create isolated fixture content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def snapshot(root: Path) -> dict[str, bytes | None]:
    """Observe the complete fixture tree without creating anything."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        if path.is_file()
        else None
        for path in root.rglob("*")
    }


@pytest.mark.parametrize("operator", [{}, {"BH_WORKTREE_GC": "detect"}])
def test_managed_precedence_after_root_discovery(
    tmp_path: Path, operator: dict[str, str]
) -> None:
    """Managed same-spelling overrides precede full pair comparison."""
    layout = PathLayout.for_environment(tmp_path, {}, home=tmp_path / "home")
    put(layout.canonical_config, CONFIG + "CODEREEVE_WORKTREE_GC=detect\n")
    put(
        layout.canonical_host,
        "CODEREEVE_REPO_OWNER=host-owner\nCODEREEVE_WORKTREE_GC=reclaim\n",
    )
    result = resolve_config_sources(
        None, {"CODEREEVE_PROJECT_ROOT": str(tmp_path), **operator}, layout
    )
    assert result.config.repo_owner == "managed-owner"
    assert result.environment.values["CODEREEVE_WORKTREE_GC"] == "detect"
    environment = {
        **os.environ,
        "CODEREEVE_PROJECT_ROOT": str(tmp_path),
        **operator,
    }
    bridge = subprocess.run(
        [
            sys.executable,
            "-m",
            "codereeve.config_bridge",
            "--format",
            "nul",
            "--host",
            str(layout.canonical_host),
        ],
        env=environment,
        capture_output=True,
    )
    assert bridge.returncode == 0, bridge.stderr
    parts = bridge.stdout.decode().split("\0")[:-1]
    values = dict(zip(parts[::2], parts[1::2], strict=True))
    assert values["CODEREEVE_REPO_OWNER"] == result.config.repo_owner
    assert values["CODEREEVE_WORKTREE_GC"] == "detect"
    report = inventory_migration(
        MigrationContext(layout, layers=(EnvLayer("operator", operator),))
    )
    assert not any(f.blocking for f in report.findings), report.findings


def test_empty_runtime_values_survive_context_materialization(
    tmp_path: Path,
) -> None:
    """An empty safety option or webhook cannot inherit a file destination."""
    path = tmp_path / ".codereeve" / "config.env"
    put(
        path,
        CONFIG + "CODEREEVE_WORKTREE_GC=reclaim\n"
        "CODEREEVE_SLACK_WEBHOOK_URL=https://example.test/private\n"
        "CODEREEVE_HEARTBEAT_PING_URL=https://example.test/ping\n",
    )
    environment = {
        "CODEREEVE_PROJECT_ROOT": str(tmp_path),
        "CODEREEVE_WORKTREE_GC": "",
        "CODEREEVE_SLACK_WEBHOOK_URL": "",
        "CODEREEVE_HEARTBEAT_PING_URL": "",
    }
    context = doctor.create_context(
        config_path=path,
        env=environment,
        which=MagicMock(),
        runner=MagicMock(),
        run=MagicMock(),
        fetch_secret=MagicMock(),
    )
    with patch.dict(os.environ, context.env, clear=True):
        assert load_obs_config().worktree_gc == "detect"
        assert load_obs_config().heartbeat_ping_url is None
        assert os.environ["CODEREEVE_SLACK_WEBHOOK_URL"] == ""


def test_empty_runtime_pair_does_not_enter_sandbox_fallback(
    tmp_path: Path,
) -> None:
    """Sandbox fallback cannot reintroduce a resolved runtime conflict."""
    path = tmp_path / "config.env"
    put(
        path,
        CONFIG + "CODEREEVE_WORKTREE_GC=reclaim\nBH_WORKTREE_GC=detect\n",
    )
    result = resolve_config_sources(
        path,
        {"CODEREEVE_WORKTREE_GC": "", "BH_WORKTREE_GC": ""},
        PathLayout.for_environment(tmp_path, {}),
    )
    assert result.environment.values["CODEREEVE_WORKTREE_GC"] == ""


class PortableOperations(FileOperations):
    """Inject directory durability and independent fixture shutdown proof."""

    def sync_directory(self, path: Path) -> None:
        """Model supported flushing without claiming Windows durability."""

    def verify_quiescence(
        self, project: Path, lease: WriterLease
    ) -> MigrationEvidence:
        """Verify the real lease while attesting stopped fixture writers."""
        assert probe_writer_lease(lease.path) is EvidenceState.BLOCKED
        return MigrationEvidence(
            writers=EvidenceState.CLEAR, service=EvidenceState.CLEAR
        )


@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize("damage", [None, "corrupt", "incomplete"])
def test_public_check_probes_retained_journals(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    restored: bool,
    damage: str | None,
) -> None:
    """CHECK uses fresh terminal evidence and leaves recovery files intact."""
    layout = PathLayout.for_environment(
        tmp_path, {}, home=tmp_path / "home", etc_root=tmp_path / "etc"
    )
    put(layout.legacy_config, "BH_REPO_OWNER=owner\n")
    context = MigrationContext(layout)
    operations = PortableOperations()
    applied = apply_migration(context, operations=operations)
    if restored:
        restore_migration(applied.manifest_path, operations=operations)
    journal = applied.manifest_path.with_name("journal.jsonl")
    if damage == "corrupt":
        journal.write_text("corrupt\n", encoding="utf-8")
    elif damage == "incomplete":
        lines = journal.read_bytes().splitlines(keepends=True)
        journal.write_bytes(b"".join(lines[:-1]))
    before = snapshot(tmp_path)
    expected = 1 if damage else 2 if restored else 0
    assert (
        main(["--check", "--format", "json"], context_factory=lambda: context)
        == expected
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == (
        "blocked" if damage else "ready" if restored else "current"
    )
    assert snapshot(tmp_path) == before
    # A valid journal must never erase independently supplied writer evidence.
    if not damage:
        blocked = replace(
            context, evidence=MigrationEvidence(writers=EvidenceState.BLOCKED)
        )
        assert main(["--check"], context_factory=lambda: blocked) == 1


@pytest.mark.parametrize("kind", ["state", "baseline", "both", "canonical"])
def test_baseline_publication_keeps_compatible_layout(
    tmp_path: Path, kind: str
) -> None:
    """A refresh retains a usable state/baseline selection under the lease."""
    if kind in {"state", "both"}:
        put(tmp_path / ".baton-harness" / "heartbeat", "alive")
    if kind in {"baseline", "both", "canonical"}:
        directory = ".codereeve" if kind == "canonical" else ".bh"
        put(tmp_path / directory / "ruleset-baseline.json", '{"old/repo": {}}')
    before = select_runtime_paths(tmp_path, {})
    publish_baseline(tmp_path, "o/r", {"main": {"ruleset_id": 1}})
    after = select_runtime_paths(tmp_path, {})
    assert after == before
    assert (
        json.loads(after.ruleset_baseline.read_text())["o/r"]["main"][
            "ruleset_id"
        ]
        == 1
    )
    if kind != "canonical":
        assert not (tmp_path / ".codereeve").exists()


def test_provisioner_reports_selected_legacy_baseline(tmp_path: Path) -> None:
    """The real capture function reports the actual compatibility path."""
    baseline = tmp_path / ".bh" / "ruleset-baseline.json"
    put(baseline, "{}")
    text = (ROOT / "bin/provision-ruleset.sh").read_text(encoding="utf-8")
    capture = (
        "_capture_baseline() {" + text.split("_capture_baseline() {", 1)[1]
    )
    env = {**os.environ, "CODEREEVE_PROJECT_ROOT": tmp_path.as_posix()}
    prefix = (
        "set -euo pipefail\n"
        "_lookup_id() { echo 1; }\n"
        "gh() { printf '%s\\n' '{\"updated_at\": \"today\"}'; }\n"
        "REPO_SLUG=o/r\n_INSTALL_TOKEN=fixture\n"
        f"_PYTHON='{Path(sys.executable).as_posix()}'\n"
    )
    bash = (
        "C:/Program Files/Git/usr/bin/bash.exe" if os.name == "nt" else "bash"
    )
    result = subprocess.run(
        [bash, "-c", prefix + capture],
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert f"baseline pinned at {baseline.as_posix()}" in result.stdout
    assert select_runtime_paths(tmp_path, {}).ruleset_baseline == baseline


@pytest.mark.parametrize("entry", ["baseline", "daemon"])
@pytest.mark.parametrize("conflict", ["alias", "state", "baseline"])
def test_direct_entry_rejection_preserves_complete_tree(
    tmp_path: Path, entry: str, conflict: str
) -> None:
    """Pure validation rejects conflicts before even creating a lease file."""
    values: dict[str, str] = {}
    if conflict == "alias":
        values = {"CODEREEVE_REPO_OWNER": "new", "BH_REPO_OWNER": "old"}
    elif conflict == "state":
        (tmp_path / ".codereeve").mkdir()
        (tmp_path / ".baton-harness").mkdir()
    else:
        for name in (".bh", ".codereeve"):
            put(tmp_path / name / "ruleset-baseline.json", "{}")
    before = snapshot(tmp_path)
    with patch.dict(os.environ, values, clear=True):
        with pytest.raises((AliasConflictError, PathConflictError)):
            if entry == "baseline":
                publish_baseline(tmp_path, "o/r", {})
            else:
                asyncio.run(
                    run_daemon(
                        WorkflowConfig(), [RepoConfig("o", "r", tmp_path)]
                    )
                )
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("legacy", ["BH_SCENARIO", "BH_SETUP_NO_PROMPT"])
@pytest.mark.parametrize("conflict", [False, True])
@pytest.mark.parametrize("override", [False, True])
def test_bootstrap_alias_before_script_consumption(
    tmp_path: Path, legacy: str, conflict: bool, override: bool
) -> None:
    """Execute the actual early script prefix without package Python."""
    scenario = legacy == "BH_SCENARIO"
    filename = "init-sandbox.sh" if scenario else "setup-env.sh"
    text = (ROOT / "bin" / filename).read_text(encoding="utf-8")
    marker = "# Print safety banner" if scenario else "# Preflight: uv"
    prefix = text.split(marker)[0]
    key = "CODEREEVE_" + legacy.removeprefix("BH_")
    value = "ci-fail" if scenario else "1"
    env = {**os.environ, legacy: value}
    env.pop(key, None)
    if conflict:
        env[key] = "private-divergent"
    # Set BASH_SOURCE through a real script location so its helper is found.
    script = tmp_path / filename
    prefix = prefix.replace(
        '$(dirname "${BASH_SOURCE[0]}")', ROOT.joinpath("bin").as_posix()
    )
    variable = "SCENARIO" if scenario else key
    script.write_text(
        prefix + f'\nprintf "selected=%s\\n" "${{{variable}:-}}"\n',
        encoding="utf-8",
        newline="\n",
    )
    bash = (
        "C:/Program Files/Git/usr/bin/bash.exe" if os.name == "nt" else "bash"
    )
    args = ["--scenario", "recovery"] if scenario and override else []
    result = subprocess.run(
        [bash, str(script), *args], env=env, text=True, capture_output=True
    )
    if conflict:
        assert result.returncode != 0
        assert "private-divergent" not in result.stdout + result.stderr
        assert key in result.stderr and legacy in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        expected = "recovery" if scenario and override else value
        assert f"selected={expected}" in result.stdout


@pytest.mark.parametrize("value", ["", " two words ", "$(touch sentinel)"])
def test_bootstrap_helper_keeps_values_literal(
    value: str, tmp_path: Path
) -> None:
    """Bootstrap aliases retain exact empty/whitespace/command-like values."""
    bash = (
        "C:/Program Files/Git/usr/bin/bash.exe" if os.name == "nt" else "bash"
    )
    helper = ROOT / "bin/lib/bootstrap-alias.sh"
    result = subprocess.run(
        [
            bash,
            "-c",
            'set -eu; source "$1"; '
            "_codereeve_bootstrap_alias CODEREEVE_SCENARIO BH_SCENARIO; "
            'printf "%s" "$CODEREEVE_SCENARIO"',
            "bash",
            helper.as_posix(),
        ],
        env={"BH_SCENARIO": value},
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == value
    assert snapshot(tmp_path) == {}
