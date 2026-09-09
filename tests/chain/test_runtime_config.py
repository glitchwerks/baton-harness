"""Runtime consumers honor canonical aliases before performing effects."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codereeve.chain import app_auth, cli, escalation
from codereeve.chain.app_private_key import resolve_app_private_key_config
from codereeve.chain.daemon import poll
from codereeve.chain.daemon.launch_gate import _resolve_app_id
from codereeve.chain.identity import Identity, env_for
from codereeve.chain.obs_config import load_obs_config
from codereeve.chain.registry import RepoConfig, load_registry
from codereeve.config_env import PRODUCT_ALIASES, AliasConflictError, AliasSpec
from codereeve.vendor.symphony.config import WorkflowConfig

_ALIASES = {alias.canonical: alias for alias in PRODUCT_ALIASES}
_MODES = ("canonical", "legacy", "identical")
_OBS_CASES = (
    ("RUNLOG_PATH", "runlog_path", "custom-log", Path("custom-log")),
    ("HEARTBEAT_FILE", "heartbeat_file", "custom-heart", Path("custom-heart")),
    (
        "REDISPATCH_COUNTS_PATH",
        "redispatch_counts_path",
        "counts",
        Path("counts"),
    ),
    (
        "FAILURE_COUNTS_PATH",
        "failure_counts_path",
        "failures",
        Path("failures"),
    ),
    ("REDISPATCH_WINDOW_TICKS", "redispatch_window_ticks", "17", 17),
    ("REDISPATCH_MAX", "redispatch_max", "7", 7),
    ("MAX_ISSUE_FAILURES", "max_issue_failures", "9", 9),
    ("HEARTBEAT_STALL_S", "heartbeat_stall_s", "27.5", 27.5),
    ("WORKER_PROGRESS_STALL_S", "worker_progress_stall_s", "37.5", 37.5),
    ("WORKTREE_GC", "worktree_gc", "reclaim", "reclaim"),
    (
        "HEARTBEAT_PING_URL",
        "heartbeat_ping_url",
        "https://monitor.test/key",
        "https://monitor.test/key",
    ),
)


def _env(values: dict[str, str], mode: str) -> dict[str, str]:
    """Spell independent fixture values through the declared alias catalog."""
    result = {}
    for key, value in values.items():
        alias = _ALIASES.get(key)
        if alias is None or mode != "legacy":
            result[key] = value
        if alias is not None and mode != "canonical":
            result[alias.legacy] = value
    return result


@pytest.mark.parametrize("mode", _MODES)
def test_codereeve_env_registry(mode: str, tmp_path: Path) -> None:
    """Canonical repository identity and root must reach registry loading."""
    values = _env(
        {
            "CODEREEVE_REPO_OWNER": "example",
            "CODEREEVE_REPO_NAME": "managed",
            "CODEREEVE_PROJECT_ROOT": str(tmp_path),
        },
        mode,
    )
    with patch.dict(os.environ, values, clear=True):
        assert load_registry() == [RepoConfig("example", "managed", tmp_path)]


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("suffix,field,value,expected", _OBS_CASES)
def test_codereeve_env_observability(
    mode: str,
    suffix: str,
    field: str,
    value: str,
    expected: object,
    tmp_path: Path,
) -> None:
    """Every observability option reaches its actual parsed config field."""
    values = _env({"CODEREEVE_" + suffix: value}, mode)
    values["CODEREEVE_PROJECT_ROOT"] = str(tmp_path)
    with patch.dict(os.environ, values, clear=True):
        assert getattr(load_obs_config(), field) == expected


@pytest.mark.parametrize("mode", _MODES)
def test_codereeve_env_app_provider(mode: str, tmp_path: Path) -> None:
    """The selected provider and private key path survive alias resolution."""
    key = tmp_path / "app.pem"
    values = _env(
        {
            "CODEREEVE_GITHUB_APP_KEY_PROVIDER": "file",
            "CODEREEVE_GITHUB_APP_PRIVATE_KEY_FILE": str(key),
            "CODEREEVE_GITHUB_APP_ID": "123",
        },
        mode,
    )
    assert resolve_app_private_key_config(values).file_path == key
    with patch.dict(os.environ, values, clear=True):
        assert _resolve_app_id() == "123"


@pytest.mark.parametrize("alias", PRODUCT_ALIASES, ids=lambda a: a.canonical)
def test_alias_conflict_before_direct_consumer_effects(
    alias: AliasSpec,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Any product conflict aborts direct consumers without leaking values."""
    values = {alias.canonical: "secret-one", alias.legacy: "secret-two"}
    with patch.dict(os.environ, values, clear=True):
        with pytest.raises(AliasConflictError):
            load_obs_config()
        with pytest.raises(AliasConflictError):
            load_registry()
        with patch.object(escalation, "_run") as network:
            with pytest.raises(AliasConflictError):
                escalation.escalate("owner", "repo", 1, "failure")
            network.assert_not_called()
    output = capsys.readouterr()
    assert "secret-one" not in output.out + output.err + caplog.text
    assert "secret-two" not in output.out + output.err + caplog.text


@pytest.mark.parametrize("entry", ["daemon", "app-auth", "bootstrap"])
def test_alias_conflict_before_entry_point_effects(
    entry: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Startup must reject conflicts before doctor, secret, or launch work."""
    values = {
        "CODEREEVE_GITHUB_APP_ID": "secret-one",
        "BH_GITHUB_APP_ID": "secret-two",
        "CODEREEVE_PROJECT_ROOT": str(tmp_path),
    }
    with (
        patch.dict(os.environ, values, clear=True),
        patch.object(cli, "_doctor_gate") as doctor_gate,
        patch.object(cli.bws_client, "fetch_secret") as fetch,
        patch.object(cli, "run_daemon", new_callable=AsyncMock) as daemon,
        patch.object(cli, "load_registry") as registry,
        patch.object(cli.os, "chdir") as chdir,
        patch.object(app_auth, "build_installation_token_provider") as mint,
    ):
        if entry == "daemon":
            assert cli.main(["--once"]) == 1
        elif entry == "app-auth":
            assert app_auth.main(["token"]) == 1
        else:
            with pytest.raises(AliasConflictError):
                cli.bootstrap_secrets()
        for effect in (doctor_gate, fetch, daemon, registry, chdir, mint):
            effect.assert_not_called()
    output = capsys.readouterr()
    assert "secret-one" not in output.out + output.err + caplog.text
    assert "secret-two" not in output.out + output.err + caplog.text


@pytest.mark.parametrize("legacy", [False, True])
def test_canonical_state_observability(tmp_path: Path, legacy: bool) -> None:
    """Fresh writes use canonical state; existing legacy state stays usable."""
    state = tmp_path / (".baton-harness" if legacy else ".codereeve")
    if legacy:
        state.mkdir()
    with patch.dict(
        os.environ, {"CODEREEVE_PROJECT_ROOT": str(tmp_path)}, clear=True
    ):
        obs = load_obs_config()
    assert obs.runlog_path == state / "runlog.jsonl"
    assert obs.heartbeat_file == state / "heartbeat"
    assert obs.redispatch_counts_path == state / "dispatch-counts.json"
    assert obs.failure_counts_path == state / "failure-counts.json"


def test_canonical_state_conflict_before_daemon_effects(
    tmp_path: Path,
) -> None:
    """Coexisting state roots abort even the direct async daemon entry."""
    from codereeve.paths import PathConflictError

    (tmp_path / ".codereeve").mkdir()
    (tmp_path / ".baton-harness").mkdir()
    with (
        patch.dict(
            os.environ, {"CODEREEVE_PROJECT_ROOT": str(tmp_path)}, clear=True
        ),
        patch.object(poll._daemon_mod, "RunLog") as runlog,
        patch.object(
            poll, "reconcile_startup", new_callable=AsyncMock
        ) as reconcile,
    ):
        with pytest.raises(PathConflictError):
            asyncio.run(
                poll.run_daemon(
                    WorkflowConfig(),
                    [RepoConfig("o", "r", tmp_path)],
                    once=True,
                )
            )
        runlog.assert_not_called()
        reconcile.assert_not_called()


@pytest.mark.parametrize(
    "value",
    [
        "https://user:password@test",
        "C:/secret/password.pem",
        "-----BEGIN PRIVATE KEY-----secret",
    ],
)
@pytest.mark.parametrize(
    "suffix",
    [
        "REDISPATCH_MAX",
        "REDISPATCH_WINDOW_TICKS",
        "MAX_ISSUE_FAILURES",
        "HEARTBEAT_STALL_S",
        "WORKER_PROGRESS_STALL_S",
        "WORKTREE_GC",
    ],
)
def test_codereeve_env_invalid_diagnostics_are_redacted(
    value: str,
    suffix: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed product values never enter configuration warning logs."""
    with patch.dict(
        os.environ,
        {
            "CODEREEVE_" + suffix: value,
            "CODEREEVE_PROJECT_ROOT": str(tmp_path),
        },
        clear=True,
    ):
        load_obs_config()
    assert "CODEREEVE_" + suffix in caplog.text
    assert value not in caplog.text


def test_codereeve_env_worker_filters_canonical_privilege() -> None:
    """Worker environments remove canonical key locators and webhooks too."""
    values = {
        "CODEREEVE_GITHUB_APP_KEY_PROVIDER": "file",
        "CODEREEVE_GITHUB_APP_PRIVATE_KEY_FILE": "/secret.pem",
        "CODEREEVE_HEARTBEAT_PING_URL": "https://secret",
        "CODEREEVE_VENV": "/venv",
        "CODEREEVE_FEATURE_BRANCH": "feature/work",
    }
    actual = env_for(Identity.WORKER, base_env=values)
    assert actual["CODEREEVE_VENV"] == "/venv"
    assert actual["CODEREEVE_FEATURE_BRANCH"] == "feature/work"
    for suffix in (
        "GITHUB_APP_KEY_PROVIDER",
        "GITHUB_APP_PRIVATE_KEY_FILE",
        "HEARTBEAT_PING_URL",
    ):
        assert "CODEREEVE_" + suffix not in actual


def _write_config(root: Path, extra: str = "") -> Path:
    """Write a valid canonical config without external credentials."""
    directory = root / ".codereeve"
    directory.mkdir(parents=True, exist_ok=True)
    config = directory / "config.env"
    config.write_text(
        "CODEREEVE_REPO_OWNER=example\n"
        "CODEREEVE_REPO_NAME=managed\n"
        "CODEREEVE_GITHUB_APP_ID=123\n"
        "CODEREEVE_GITHUB_APP_INSTALLATION_ID=456\n"
        "CODEREEVE_GITHUB_APP_KEY_PROVIDER=bws\n"
        "BWS_PEM_SECRET_ID=11111111-2222-3333-4444-555555555555\n" + extra,
        encoding="utf-8",
    )
    return config


def test_alias_conflict_from_config_precedes_doctor_effects(
    tmp_path: Path,
) -> None:
    """File/operator conflicts are rejected before even installation probes."""
    config = _write_config(tmp_path, "BH_SLACK_WEBHOOK_URL=secret-two\n")
    with (
        patch.dict(
            os.environ,
            {"CODEREEVE_SLACK_WEBHOOK_URL": "secret-one"},
            clear=True,
        ),
        patch.object(cli.doctor, "run_gate") as gate,
        patch.object(cli, "load_registry") as registry,
    ):
        assert cli.main(["--once", "--config", str(config)]) == 1
        gate.assert_not_called()
        registry.assert_not_called()


def test_canonical_state_cli_rejects_coexistence_before_doctor(
    tmp_path: Path,
) -> None:
    """The CLI rejects coexisting runtime roots before invoking any gate."""
    config = _write_config(tmp_path)
    (tmp_path / ".baton-harness").mkdir()
    with (
        patch.dict(os.environ, {}, clear=True),
        patch.object(cli.doctor, "run_gate") as gate,
        patch.object(cli.os, "chdir") as chdir,
    ):
        assert cli.main(["--once", "--config", str(config)]) == 1
        gate.assert_not_called()
        chdir.assert_not_called()


def test_codereeve_env_config_snapshot_reaches_daemon(tmp_path: Path) -> None:
    """File observability values and canonical report path reach launch."""
    config = _write_config(tmp_path, "CODEREEVE_REDISPATCH_MAX=11\n")
    with (
        patch.dict(os.environ, {}, clear=True),
        patch.object(cli.doctor, "run_gate"),
        patch.object(cli.os, "chdir"),
        patch.object(cli, "_assert_force_pr_not_merge_tripwire"),
        patch.object(cli, "bootstrap_secrets", return_value="ghs_test"),
        patch.object(cli, "validate_daemon_token"),
        patch.object(cli, "run_daemon", new_callable=AsyncMock) as daemon,
    ):
        assert cli.main(["--once", "--config", str(config)]) == 0
        assert load_obs_config().redispatch_max == 11
        assert daemon.call_args.kwargs["report_path"] == (
            tmp_path / ".codereeve" / "session-report.json"
        )
        assert daemon.call_args.kwargs["runtime_paths"].ruleset_baseline == (
            tmp_path / ".codereeve" / "ruleset-baseline.json"
        )


@pytest.mark.parametrize("mode", (*_MODES, "empty", "unset"))
def test_codereeve_env_worker_branch_and_venv(
    mode: str,
    tmp_path: Path,
) -> None:
    """A work unit publishes the fresh cut point and both branch spellings."""
    import sys

    from codereeve.chain.daemon import work_unit
    from codereeve.chain.recovery import RecoveryResult

    values = _env(
        {
            "CODEREEVE_VENV": str(tmp_path / "venv"),
            "CODEREEVE_FEATURE_BRANCH": "feature/old",
        },
        mode if mode in _MODES else "canonical",
    )
    if mode == "empty":
        values["CODEREEVE_VENV"] = ""
    if mode == "unset":
        values.pop("CODEREEVE_VENV")
    values["CHAIN_BASE_BRANCH"] = "old-cut"
    captured: dict[str, str] = {}

    def capture(*args: object, **kwargs: object) -> None:
        """Stop at the real issue-fetch boundary after export preparation."""
        captured.update(os.environ)
        raise SystemExit(77)

    with (
        patch.dict(os.environ, values, clear=True),
        patch.object(
            work_unit._daemon_mod, "fetch_blocked_by", return_value=[]
        ),
        patch.object(
            work_unit,
            "_setup_feature_branch",
            return_value=RecoveryResult(set(), set(), set(), set()),
        ),
        patch.object(work_unit, "Orchestrator"),
        patch.object(
            work_unit._daemon_mod,
            "_fetch_issue_labels",
            return_value={"agent-ready"},
        ),
        patch.object(work_unit.branches, "checkout_feature_branch"),
        patch.object(
            work_unit.branches, "record_cut_point", return_value="new-cut"
        ),
        patch.object(work_unit._daemon_mod, "_label_edit"),
        patch.object(
            work_unit._daemon_mod, "_fetch_issue_obj", side_effect=capture
        ),
        pytest.raises(SystemExit, match="77"),
    ):
        asyncio.run(
            work_unit._run_work_unit(
                WorkflowConfig(),
                RepoConfig("o", "r", tmp_path),
                "feature/new",
                "new",
                frozenset({1}),
            )
        )
    assert captured["CHAIN_BASE_BRANCH"] == "new-cut"
    assert captured["CODEREEVE_FEATURE_BRANCH"] == "feature/new"
    assert captured["BH_FEATURE_BRANCH"] == "feature/new"
    expected_venv = (
        str(Path(sys.executable).parent.parent)
        if mode in {"empty", "unset"}
        else str(tmp_path / "venv")
    )
    assert captured["CODEREEVE_VENV"] == expected_venv
    assert captured["BH_VENV"] == expected_venv


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("value", ["https://slack.test/token", ""])
def test_codereeve_env_slack(mode: str, value: str) -> None:
    """Slack receives the selected URL, and an empty URL disables it."""
    with (
        patch.dict(
            os.environ,
            _env({"CODEREEVE_SLACK_WEBHOOK_URL": value}, mode),
            clear=True,
        ),
        patch.object(escalation, "_run", return_value=MagicMock(returncode=0)),
        patch.object(escalation, "_post_slack") as slack,
    ):
        assert escalation.escalate("o", "r", 1, "message")
    if value:
        assert slack.call_args.args[0] == value
    else:
        slack.assert_not_called()


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("suffix,field,value,expected", _OBS_CASES)
def test_codereeve_env_empty_observability(
    mode: str,
    suffix: str,
    field: str,
    value: str,
    expected: object,
    tmp_path: Path,
) -> None:
    """Empty values keep the established parse/default rules for each field."""
    del value, expected
    defaults = {
        "redispatch_window_ticks": 10,
        "redispatch_max": 3,
        "max_issue_failures": 2,
        "heartbeat_stall_s": 7200.0,
        "worker_progress_stall_s": 1800.0,
        "worktree_gc": "detect",
        "heartbeat_ping_url": None,
    }
    values = _env({"CODEREEVE_" + suffix: ""}, mode)
    values["CODEREEVE_PROJECT_ROOT"] = str(tmp_path)
    with patch.dict(os.environ, values, clear=True):
        assert getattr(load_obs_config(), field) == defaults.get(
            field, Path("")
        )


@pytest.mark.parametrize("legacy", [False, True])
def test_canonical_state_ruleset_baseline(
    tmp_path: Path, legacy: bool
) -> None:
    """The comparator reads canonical or existing legacy baselines."""
    from codereeve.chain.ruleset_status import (
        RulesetStatus,
        check_ruleset_signals,
    )

    directory = tmp_path / (".bh" if legacy else ".codereeve")
    directory.mkdir()
    (directory / "ruleset-baseline.json").write_text(
        '{"o/r": {}}', encoding="utf-8"
    )
    with patch.dict(
        os.environ, {"CODEREEVE_PROJECT_ROOT": str(tmp_path)}, clear=True
    ):
        result = check_ruleset_signals(
            "o", "r", app_id="123", runner=MagicMock()
        )
    assert result.status is RulesetStatus.NOT_PROVISIONED
    assert "no baseline entry for ruleset" in result.detail


@pytest.mark.parametrize("legacy", [False, True])
def test_canonical_state_alive_marker(tmp_path: Path, legacy: bool) -> None:
    """Real startup reconciliation writes its marker in the selected root."""
    from codereeve.chain import reconcile

    state = tmp_path / (".baton-harness" if legacy else ".codereeve")
    if legacy:
        state.mkdir()
    credential = tmp_path / "credentials"
    credential.write_text("{}", encoding="utf-8")
    with (
        patch.dict(os.environ, {}, clear=True),
        patch.object(reconcile, "_OAUTH_CRED_PATH", credential),
        patch.object(reconcile, "validate_daemon_token"),
        patch.object(
            reconcile, "_get_git_credential_helpers", return_value=["helper"]
        ),
        patch.object(reconcile, "_list_claude_procs", return_value=[]),
    ):
        asyncio.run(
            reconcile.reconcile_startup(
                [RepoConfig("o", "r", tmp_path)],
                None,
                None,
                installation_token="ghs_test",
            )
        )
    assert (state / "daemon.alive").read_text(encoding="utf-8") == "alive"
    assert not (tmp_path / ".symphony").exists()


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("suffix", ["REPO_OWNER", "REPO_NAME", "PROJECT_ROOT"])
def test_codereeve_env_empty_registry(mode: str, suffix: str) -> None:
    """An explicitly empty required identity field remains a registry error."""
    values = {
        "CODEREEVE_REPO_OWNER": "o",
        "CODEREEVE_REPO_NAME": "r",
        "CODEREEVE_PROJECT_ROOT": "/root",
    }
    values["CODEREEVE_" + suffix] = ""
    with patch.dict(os.environ, _env(values, mode), clear=True):
        with pytest.raises(ValueError, match="Registry is not configured"):
            load_registry()


@pytest.mark.parametrize("mode", _MODES)
def test_codereeve_env_app_token_consumer(mode: str, tmp_path: Path) -> None:
    """App IDs and provider selection reach actual token provider creation."""
    values = _env(
        {
            "CODEREEVE_GITHUB_APP_ID": "123",
            "CODEREEVE_GITHUB_APP_INSTALLATION_ID": "456",
            "CODEREEVE_GITHUB_APP_KEY_PROVIDER": "file",
            "CODEREEVE_GITHUB_APP_PRIVATE_KEY_FILE": str(tmp_path / "key.pem"),
        },
        mode,
    )
    provider = MagicMock()
    provider.get_token.return_value = "ghs_test"
    with (
        patch.dict(os.environ, values, clear=True),
        patch.object(
            app_auth,
            "build_installation_token_provider",
            return_value=provider,
        ) as build,
    ):
        assert app_auth.main(["token"]) == 0
    assert build.call_args.args[0] == "123"
    assert build.call_args.args[2] == 456
    assert build.call_args.args[1].file_path == tmp_path / "key.pem"


@pytest.mark.parametrize("mode", _MODES)
def test_codereeve_env_empty_app_id(mode: str) -> None:
    """Empty App IDs cannot enable preflight launch."""
    with patch.dict(
        os.environ, _env({"CODEREEVE_GITHUB_APP_ID": ""}, mode), clear=True
    ):
        assert _resolve_app_id() is None


@pytest.mark.parametrize("alias", PRODUCT_ALIASES, ids=lambda a: a.canonical)
@pytest.mark.parametrize("value", ["", "literal"])
def test_codereeve_env_materialization(alias: AliasSpec, value: str) -> None:
    """Export identical aliases and preserve third-party values."""
    from codereeve.config_env import (
        apply_resolved_environment,
        runtime_environment,
    )

    snapshot = runtime_environment(
        {alias.canonical: value, "GH_TOKEN": "third-party"}
    )
    target = {alias.legacy: "stale"}
    apply_resolved_environment(snapshot, target)
    assert target[alias.canonical] == value
    assert target[alias.legacy] == value
    assert target["GH_TOKEN"] == "third-party"


def test_codereeve_env_source_snapshot_retains_provenance(
    tmp_path: Path,
) -> None:
    """Runtime handoff retains actual legacy file sources after validation."""
    from codereeve.chain.sandbox_config import resolve_config_sources
    from codereeve.paths import PathLayout

    path = _write_config(tmp_path, "BH_REDISPATCH_MAX=12\n")
    source = resolve_config_sources(
        path, {}, PathLayout.for_environment(tmp_path, {})
    )
    assert source.environment.legacy_uses == source.legacy_uses
    assert source.environment.values["CODEREEVE_REDISPATCH_MAX"] == "12"
    assert "BH_REDISPATCH_MAX" not in source.environment.values


def test_codereeve_env_bad_root_diagnostic_is_redacted(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Invalid root diagnostics name the canonical key without path secrets."""
    path = _write_config(tmp_path)
    root = tmp_path / "password-sentinel"
    with (
        patch.dict(
            os.environ, {"CODEREEVE_PROJECT_ROOT": str(root)}, clear=True
        ),
        patch.object(cli.doctor, "run_gate"),
    ):
        assert cli.main(["--once", "--config", str(path)]) == 1
    assert "password-sentinel" not in capsys.readouterr().err


@pytest.mark.parametrize("entry", ["bootstrap", "app-auth"])
def test_alias_conflict_still_scrubs_bootstrap_authority(entry: str) -> None:
    """Conflict exits retain the unconditional bootstrap-token scrub."""
    with (
        patch.dict(
            os.environ,
            {
                "CODEREEVE_GITHUB_APP_ID": "one",
                "BH_GITHUB_APP_ID": "two",
                "BWS_ACCESS_TOKEN": "bootstrap-secret",
            },
            clear=True,
        ),
        patch.object(cli.bws_client, "fetch_secret") as fetch,
    ):
        if entry == "bootstrap":
            with pytest.raises(AliasConflictError):
                cli.bootstrap_secrets()
        else:
            assert app_auth.main(["token"]) == 1
        assert "BWS_ACCESS_TOKEN" not in os.environ
        fetch.assert_not_called()


@pytest.mark.parametrize("entry", ["--once", "--doctor"])
@pytest.mark.parametrize(
    "failure", ["root-file", "ancestor-file", "state", "baseline"]
)
def test_canonical_state_path_failure_is_redacted_before_effects(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    entry: str,
    failure: str,
) -> None:
    """Every startup path failure is value-free and precedes all effects."""
    config = _write_config(tmp_path / "config-source")
    private_parent = tmp_path / "credential-one" / "credential-two"
    private_parent.parent.mkdir()
    root = private_parent / "repo"
    if failure == "ancestor-file":
        private_parent.write_text("sentinel", encoding="utf-8")
    elif failure == "root-file":
        private_parent.mkdir()
        root.write_text("sentinel", encoding="utf-8")
    else:
        (root / ".codereeve").mkdir(parents=True)
        if failure == "state":
            (root / ".baton-harness").mkdir()
        else:
            (root / ".bh").mkdir()
            for directory in (".codereeve", ".bh"):
                (root / directory / "ruleset-baseline.json").write_text(
                    "{}", encoding="utf-8"
                )
    with (
        patch.dict(
            os.environ, {"CODEREEVE_PROJECT_ROOT": str(root)}, clear=True
        ),
        patch.object(
            cli.doctor, "run_gate", side_effect=SystemExit("unexpected gate")
        ) as gate,
        patch.object(
            cli.doctor,
            "run_report",
            side_effect=SystemExit("unexpected report"),
        ) as doctor_run,
        patch.object(cli, "bootstrap_secrets") as bootstrap,
        patch.object(cli, "validate_daemon_token") as token,
        patch.object(cli, "load_registry") as registry,
        patch.object(cli.os, "chdir") as chdir,
        patch.object(cli, "run_daemon", new_callable=AsyncMock) as daemon,
        patch.object(Path, "mkdir") as mkdir,
        patch.object(Path, "write_text") as write,
    ):
        assert cli.main([entry, "--config", str(config)]) == 1
        for effect in (
            gate,
            doctor_run,
            bootstrap,
            token,
            registry,
            chdir,
            daemon,
            mkdir,
            write,
        ):
            effect.assert_not_called()
    captured = capsys.readouterr()
    output = captured.out + captured.err + caplog.text
    assert "credential-one" not in output
    assert "credential-two" not in output
    assert str(root) not in output
    assert (
        "ruleset baseline" if failure == "baseline" else "runtime state"
    ) in output
    assert (
        "ambiguous" if failure in {"state", "baseline"} else "unsafe"
    ) in output


def test_canonical_state_baseline_conflict_before_direct_daemon_effects(
    tmp_path: Path,
) -> None:
    """Direct daemon entry must reject ambiguous baselines before startup."""
    from codereeve.paths import PathConflictError

    for directory in (".codereeve", ".bh"):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / "ruleset-baseline.json").write_text(
            "{}", encoding="utf-8"
        )
    with (
        patch.dict(
            os.environ, {"CODEREEVE_PROJECT_ROOT": str(tmp_path)}, clear=True
        ),
        patch.object(
            poll._daemon_mod,
            "load_obs_config",
            side_effect=SystemExit("unexpected startup"),
        ) as obs,
        patch.object(poll._daemon_mod, "RunLog") as runlog,
        patch.object(
            poll, "reconcile_startup", new_callable=AsyncMock
        ) as reconcile,
        patch.object(
            poll._daemon_mod, "_poll_and_run", new_callable=AsyncMock
        ) as launch,
        patch.object(Path, "mkdir") as mkdir,
        patch.object(Path, "write_text") as write,
        pytest.raises(PathConflictError, match="ruleset baseline"),
    ):
        try:
            asyncio.run(
                poll.run_daemon(
                    WorkflowConfig(),
                    [RepoConfig("o", "r", tmp_path)],
                    once=True,
                )
            )
        finally:
            for effect in (obs, runlog, reconcile, launch, mkdir, write):
                effect.assert_not_called()


@pytest.mark.parametrize("legacy", [False, True])
def test_canonical_state_startup_preserves_baseline_for_comparator(
    tmp_path: Path, legacy: bool
) -> None:
    """The startup-selected baseline survives environment changes at launch."""
    from codereeve.chain.daemon.launch_gate import _should_launch_worker
    from codereeve.chain.ruleset_status import (
        RulesetCheckResult,
        RulesetStatus,
    )

    baseline = (
        tmp_path
        / (".bh" if legacy else ".codereeve")
        / "ruleset-baseline.json"
    )
    baseline.parent.mkdir()
    baseline.write_text('{"o/r": {}}', encoding="utf-8")
    with (
        patch.dict(
            os.environ, {"CODEREEVE_PROJECT_ROOT": str(tmp_path)}, clear=True
        ),
        patch.object(
            poll,
            "warn_if_async_escalation_unconfigured",
            side_effect=SystemExit("capture config"),
        ) as capture,
        pytest.raises(SystemExit, match="capture config"),
    ):
        asyncio.run(
            poll.run_daemon(
                WorkflowConfig(), [RepoConfig("o", "r", tmp_path)], once=True
            )
        )
    obs = capture.call_args.args[0]
    assert obs.ruleset_baseline_path == baseline
    with (
        patch.dict(
            os.environ,
            {"CODEREEVE_PROJECT_ROOT": str(tmp_path / "changed")},
            clear=True,
        ),
        patch.object(
            poll._daemon_mod,
            "check_ruleset_signals",
            return_value=RulesetCheckResult(RulesetStatus.MATCH, "matched"),
        ) as comparator,
    ):
        assert _should_launch_worker(
            1, "o", "r", app_id="123", runner=MagicMock(), obs=obs
        )
    assert comparator.call_args.kwargs["baseline_path"] == baseline
