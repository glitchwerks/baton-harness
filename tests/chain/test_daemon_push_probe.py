"""Tests for the #223 lightweight push-denial probe (Option A).

This test file pins the contract for a new **behavioral** launch gate that
the code-writer must implement in ``daemon.py``, and for the demotion of
the existing config-diff comparator (``check_ruleset_signals``) from
decisive to diagnostic-only.

Proposed seam (code-writer adopts this name/signature in Phase 2)::

    def _probe_worker_push_denied(repo_root: Path) -> bool:
        ...

Contract for ``_probe_worker_push_denied``:

1. Attempts a git push to a UNIQUE throwaway ref under ``feature/`` (e.g.
   ``feature/__bh-probe-<random>``), authenticated with the **WORKER**
   identity (``env_for(Identity.WORKER)``) — never ``_authed_git_push``,
   never an App installation token / credential helper. Uses the existing
   ``daemon._run(cmd, env=None)`` subprocess seam (the same one
   ``_authed_git_push`` and friends already share).
2. Returns ``True`` ("denied" — push-protection boundary intact) when the
   push is rejected with a non-zero returncode AND a recognizable denial
   signal in stderr (``403``, ``protected``, ``declined``,
   ``refusing to allow``, ``GH006``, etc).
3. Returns ``False`` ("NOT denied" — boundary BREACHED, the dangerous
   case) when the push is ACCEPTED (returncode 0). In that case the probe
   must also attempt to DELETE the now-existing probe ref from origin
   (cleanup), via a second call through the same subprocess seam.
4. Returns ``False`` (fail-closed — unproven, never treated as "safe by
   default") for any INDETERMINATE outcome: a non-zero exit without a
   recognizable denial signal, or a raised transport/subprocess
   exception. The probe must not let such an exception propagate.
5. Uses a DISTINCT throwaway ref on every invocation (no ref reuse across
   calls), so concurrent daemons / retries can't collide.

Gate demotion (pinned via ``_launch_one_issue`` — the existing, stable
entry point; internal layout of ``_should_launch_worker`` is not pinned):

- ``check_ruleset_signals`` keeps running and is still logged
  (diagnostic), but no longer decides launch on its own.
- ``_probe_worker_push_denied`` becomes the DECISIVE signal:
  denied ⇒ launch proceeds even on a comparator DRIFT/ABSENT/ERROR/
  NOT_PROVISIONED result; NOT denied ⇒ refuse even on a comparator MATCH.
- On refusal, the existing park semantics (Slack alert, restored
  ``agent-ready``, cleared ``agent-in-progress``, blocking "preflight
  refused" comment) still apply — this file re-pins those assertions
  under the new decisive signal so a #223 regression can't silently
  disable them.

Since ``daemon_mod._probe_worker_push_denied`` and ``daemon_mod._run`` /
``daemon_mod.env_for`` are patched at MODULE scope, these integration
tests are agnostic to whether the demotion/demotion wiring physically
lives inside ``_should_launch_worker`` or elsewhere in the call chain —
only the observable behavior of ``_launch_one_issue`` is pinned.

Coverage:
- A1: probe pushes using WORKER identity (env_for(Identity.WORKER)), via
  the shared ``_run`` seam, to a ``feature/…probe…`` ref — never
  ``_authed_git_push``.
- A2: a rejected push with a recognizable denial signal ⇒ True (denied).
- A3: an accepted push (returncode 0) ⇒ False (NOT denied) AND a cleanup
  delete of the same ref is attempted.
- A4a: a non-zero exit with an UNRECOGNIZED stderr ⇒ False (fail-closed).
- A4b: a raised exception from the subprocess seam ⇒ False (fail-closed),
  and the probe itself must not raise.
- A5: two invocations use two distinct probe refs.
- B6: probe denied + comparator DRIFT ⇒ launch proceeds; comparator
  result is still logged as a diagnostic WARNING.
- B7: probe NOT denied + comparator MATCH ⇒ launch refused; Slack alert,
  blocking comment, and label restoration semantics still fire.
- B8: comparator (``check_ruleset_signals``) is still called exactly
  once per launch decision, regardless of the probe's outcome — proving
  it is demoted, not removed.
- B9: an unexpected exception raised BY the probe (defense in depth,
  distinct from A4b's probe-internal handling) does not crash the launch
  decision and still results in a fail-closed refusal + park.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from baton_harness.chain.obs_config import ObsConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_OWNER = "glitchwerks"
_REPO = "baton-harness"
_ISSUE = 42
_APP_ID = "111"
_TOKEN = "ghs_TESTTOKEN"
_WEBHOOK = "https://hooks.slack.com/services/T00/B00/secret"

# A denial signal a real GitHub push-protection rejection carries.
_DENIAL_STDERR = (
    "remote: error: GH006: Protected branch update failed for "
    "refs/heads/feature/__bh-probe-abc123\n"
    "remote: Cannot push to this protected branch\n"
    "To github.com:glitchwerks/baton-harness.git\n"
    " ! [remote rejected] feature/__bh-probe-abc123 -> "
    "feature/__bh-probe-abc123 (protected branch hook declined)\n"
    "error: failed to push some refs to "
    "'github.com:glitchwerks/baton-harness.git'"
)

# A non-zero exit that carries NO recognizable denial vocabulary — must
# be treated as indeterminate, not as proof of denial.
_UNRECOGNIZED_STDERR = (
    "fatal: unable to access "
    "'https://github.com/glitchwerks/baton-harness.git/': "
    "Could not resolve host: github.com"
)

_PROBE_REF_RE = re.compile(r"feature/\S*probe\S*", re.IGNORECASE)


def _make_obs(tmp_path: Path, *, ping_url: str | None = _WEBHOOK) -> ObsConfig:
    """Build a minimal ObsConfig for push-probe tests.

    Args:
        tmp_path: Pytest tmp_path fixture; used to generate required paths.
        ping_url: Value for ``heartbeat_ping_url``; use ``None`` to test
            the no-URL path.

    Returns:
        A populated ObsConfig with the heartbeat_ping_url set accordingly.
    """
    return ObsConfig(
        runlog_path=tmp_path / "runlog.jsonl",
        heartbeat_file=tmp_path / "heartbeat",
        redispatch_window_ticks=10,
        redispatch_max=3,
        heartbeat_stall_s=7200.0,
        heartbeat_ping_url=ping_url,
        redispatch_counts_path=tmp_path / "dispatch-counts.json",
    )


def _get_probe_fn(daemon_mod: Any) -> Any:  # noqa: ANN401
    """Fetch the #223 probe seam, failing with a clear reason if absent.

    Args:
        daemon_mod: The imported ``baton_harness.chain.daemon`` module.

    Returns:
        The ``_probe_worker_push_denied`` callable.
    """
    probe_fn = getattr(daemon_mod, "_probe_worker_push_denied", None)
    assert probe_fn is not None, (
        "chain/daemon.py must define a module-level "
        "`_probe_worker_push_denied(repo_root: Path) -> bool` seam "
        "(issue #223) — see this test file's module docstring for the "
        "full contract"
    )
    return probe_fn


def _run_side_effect(
    *,
    returncode: int,
    stderr: str = "",
    stdout: str = "",
    calls: list[list[str]] | None = None,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Build a ``daemon_mod._run`` stub returning a fixed result.

    Args:
        returncode: Exit code to report on every invocation.
        stderr: stderr text to report on every invocation.
        stdout: stdout text to report on every invocation.
        calls: Optional list that each invoked ``cmd`` is appended onto,
            for spying on call count/order/content.

    Returns:
        A callable matching ``daemon_mod._run(cmd, env=None)``.
    """

    def _side_effect(
        cmd: list[str], env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        if calls is not None:
            calls.append(list(cmd))
        return subprocess.CompletedProcess(
            args=cmd, returncode=returncode, stdout=stdout, stderr=stderr
        )

    return _side_effect


# ---------------------------------------------------------------------------
# A1 — probe pushes using the WORKER identity via the shared `_run` seam
# ---------------------------------------------------------------------------


def test_probe_pushes_using_worker_identity_via_run_seam(
    tmp_path: Path,
) -> None:
    """The probe pushes with env_for(Identity.WORKER) via `_run`.

    Never via ``_authed_git_push`` (the App-token push helper) and never
    with a bare ``None``/ambient env.

    Args:
        tmp_path: Pytest tmp_path fixture; used as a stand-in repo_root.
    """
    import baton_harness.chain.daemon as daemon_mod

    probe_fn = _get_probe_fn(daemon_mod)

    sentinel_env = {"__BH_TEST_SENTINEL_WORKER_ENV__": "1"}
    captured_identity: list[Any] = []

    def _spy_env_for(identity: Any, **kwargs: Any) -> dict[str, str]:  # noqa: ANN401
        captured_identity.append(identity)
        return dict(sentinel_env)

    run_calls: list[list[str]] = []
    run_envs: list[dict[str, str] | None] = []

    def _spy_run(
        cmd: list[str], env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        run_calls.append(list(cmd))
        run_envs.append(env)
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout="", stderr=_DENIAL_STDERR
        )

    def _forbidden_authed_push(
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError(
            "_probe_worker_push_denied must not call _authed_git_push — "
            "it must push using the WORKER identity, never the App "
            "installation token"
        )

    with (
        patch.object(daemon_mod, "env_for", side_effect=_spy_env_for),
        patch.object(daemon_mod, "_run", side_effect=_spy_run),
        patch.object(
            daemon_mod, "_authed_git_push", side_effect=_forbidden_authed_push
        ),
    ):
        probe_fn(tmp_path)

    assert captured_identity, (
        "the probe must call env_for(...) to build its subprocess env"
    )
    assert captured_identity[0] is daemon_mod.Identity.WORKER, (
        "the probe must request env_for(Identity.WORKER), not any other "
        f"identity; got {captured_identity[0]!r}"
    )
    assert run_calls, "the probe must invoke the `_run` subprocess seam"
    joined = " ".join(run_calls[0])
    assert "git" in run_calls[0] and "push" in run_calls[0], (
        f"the probe's first subprocess call must be a git push; got "
        f"{run_calls[0]!r}"
    )
    assert _PROBE_REF_RE.search(joined), (
        "the probe's push command must target a "
        f"'feature/…probe…' ref; got {run_calls[0]!r}"
    )
    assert run_envs[0] == sentinel_env, (
        "the probe must pass the env_for(Identity.WORKER) result "
        f"through to `_run`; got env={run_envs[0]!r}"
    )
    for forbidden_key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_INSTALLATION_TOKEN"):
        assert forbidden_key not in run_envs[0], (
            f"probe push env must never carry {forbidden_key!r} "
            "(App-token identity leaking into a worker-identity probe)"
        )


# ---------------------------------------------------------------------------
# A2 — rejected push with a recognizable denial signal -> True (denied)
# ---------------------------------------------------------------------------


def test_probe_returns_denied_on_recognizable_rejection(
    tmp_path: Path,
) -> None:
    """A rejected push carrying a recognizable denial signal ⇒ denied=True.

    Args:
        tmp_path: Pytest tmp_path fixture; used as a stand-in repo_root.
    """
    import baton_harness.chain.daemon as daemon_mod

    probe_fn = _get_probe_fn(daemon_mod)

    with patch.object(
        daemon_mod,
        "_run",
        side_effect=_run_side_effect(returncode=1, stderr=_DENIAL_STDERR),
    ):
        result = probe_fn(tmp_path)

    assert result is True, (
        "A rejected push (non-zero exit) carrying a recognizable denial "
        "signal (GH006 / 'protected branch hook declined') must be "
        f"reported as DENIED (push protection intact); got {result!r}"
    )


# ---------------------------------------------------------------------------
# A3 — accepted push (returncode 0) -> False, and cleanup delete attempted
# ---------------------------------------------------------------------------


def test_probe_returns_not_denied_on_accepted_push_and_cleans_up(
    tmp_path: Path,
) -> None:
    """An ACCEPTED push (returncode 0) means the boundary was BREACHED.

    The probe must report ``False`` (NOT denied) and must also attempt to
    delete the now-existing probe ref from origin.

    Args:
        tmp_path: Pytest tmp_path fixture; used as a stand-in repo_root.
    """
    import baton_harness.chain.daemon as daemon_mod

    probe_fn = _get_probe_fn(daemon_mod)

    run_calls: list[list[str]] = []

    with patch.object(
        daemon_mod,
        "_run",
        side_effect=_run_side_effect(returncode=0, calls=run_calls),
    ):
        result = probe_fn(tmp_path)

    assert result is False, (
        "An ACCEPTED push (returncode 0) means the push-protection "
        "boundary was BREACHED — the probe must report NOT denied; "
        f"got {result!r}"
    )
    assert len(run_calls) >= 2, (
        "An accepted probe push must be followed by a cleanup delete of "
        f"the now-existing probe ref; only saw calls: {run_calls!r}"
    )
    push_joined = " ".join(run_calls[0])
    push_ref_match = _PROBE_REF_RE.search(push_joined)
    assert push_ref_match, (
        f"could not find a probe ref in the push command: {run_calls[0]!r}"
    )
    probe_ref = push_ref_match.group(0)
    cleanup_cmd = run_calls[1]
    cleanup_joined = " ".join(cleanup_cmd)
    assert "--delete" in cleanup_cmd or f":{probe_ref}" in cleanup_joined, (
        "The cleanup call must delete the SAME probe ref that was just "
        f"pushed (via `--delete <ref>` or a `:<ref>` refspec); got "
        f"{cleanup_cmd!r} (expected ref {probe_ref!r})"
    )


# ---------------------------------------------------------------------------
# A4a — indeterminate: non-zero exit WITHOUT a recognizable denial signal
# ---------------------------------------------------------------------------


def test_probe_fails_closed_on_unrecognized_nonzero_exit(
    tmp_path: Path,
) -> None:
    """A non-zero exit with NO recognizable denial signal is unproven.

    It must not be treated as a confirmed denial; the probe must
    fail-closed and report ``False`` (NOT denied).

    Args:
        tmp_path: Pytest tmp_path fixture; used as a stand-in repo_root.
    """
    import baton_harness.chain.daemon as daemon_mod

    probe_fn = _get_probe_fn(daemon_mod)

    with patch.object(
        daemon_mod,
        "_run",
        side_effect=_run_side_effect(
            returncode=128, stderr=_UNRECOGNIZED_STDERR
        ),
    ):
        result = probe_fn(tmp_path)

    assert result is False, (
        "A non-zero exit WITHOUT a recognizable denial signal (e.g. a "
        "transport/network failure) is UNPROVEN, not a confirmed "
        f"denial — the probe must fail closed; got {result!r}"
    )


# ---------------------------------------------------------------------------
# A4b — indeterminate: a raised exception must fail-closed, not propagate
# ---------------------------------------------------------------------------


def test_probe_fails_closed_when_subprocess_seam_raises(
    tmp_path: Path,
) -> None:
    """A raised transport/subprocess exception must fail-closed.

    The probe itself must swallow the exception (not propagate it) and
    report ``False`` (NOT denied — unproven).

    Args:
        tmp_path: Pytest tmp_path fixture; used as a stand-in repo_root.
    """
    import baton_harness.chain.daemon as daemon_mod

    probe_fn = _get_probe_fn(daemon_mod)

    def _raising_run(
        cmd: list[str], env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        raise OSError("network unreachable")

    with patch.object(daemon_mod, "_run", side_effect=_raising_run):
        result = probe_fn(tmp_path)  # must NOT raise

    assert result is False, (
        "A raised transport/subprocess exception must be treated as "
        f"unproven ⇒ NOT denied (fail-closed); got {result!r}"
    )


# ---------------------------------------------------------------------------
# A5 — two invocations must use two distinct probe refs
# ---------------------------------------------------------------------------


def test_probe_uses_distinct_refs_across_invocations(
    tmp_path: Path,
) -> None:
    """Two probe invocations must target DISTINCT throwaway refs.

    Prevents concurrent daemons / retries from colliding on the same ref.

    Args:
        tmp_path: Pytest tmp_path fixture; used as a stand-in repo_root.
    """
    import baton_harness.chain.daemon as daemon_mod

    probe_fn = _get_probe_fn(daemon_mod)

    seen_refs: list[str] = []

    def _spy_run(
        cmd: list[str], env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        match = _PROBE_REF_RE.search(" ".join(cmd))
        if match:
            seen_refs.append(match.group(0))
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout="", stderr=_DENIAL_STDERR
        )

    with patch.object(daemon_mod, "_run", side_effect=_spy_run):
        probe_fn(tmp_path)
        probe_fn(tmp_path)

    assert len(seen_refs) >= 2, (
        f"expected a probe ref captured on each invocation; got {seen_refs!r}"
    )
    assert seen_refs[0] != seen_refs[1], (
        "Two probe invocations must target DISTINCT refs so concurrent "
        f"daemons/retries don't collide; got the same ref twice: "
        f"{seen_refs!r}"
    )


# ---------------------------------------------------------------------------
# B6 — probe denied + comparator DRIFT -> launch proceeds; diagnostic
# WARNING still logged for the non-MATCH comparator result
# ---------------------------------------------------------------------------

_DRIFT_DETAIL = (
    "DRIFT (harness-feature-daemon-only): current_user_can_bypass differs "
    "— expected 'always', live 'never'"
)


def test_launch_proceeds_when_probe_denies_despite_comparator_drift(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Probe denial overrides a comparator DRIFT result — launch proceeds.

    Proves the config-diff comparator (``check_ruleset_signals``) is now
    diagnostic-only: even a DRIFT verdict must not block the launch when
    the behavioral probe confirms push-protection is intact. The DRIFT
    result must still be logged as a diagnostic WARNING.

    Args:
        tmp_path: Pytest tmp_path fixture.
        caplog: Pytest log-capture fixture.
    """
    from unittest.mock import AsyncMock, MagicMock

    import baton_harness.chain.daemon as daemon_mod
    from baton_harness.chain.ruleset_status import (
        RulesetCheckResult,
        RulesetStatus,
    )

    _get_probe_fn(daemon_mod)  # fail fast with a clear reason if missing

    obs = _make_obs(tmp_path)
    mock_orch = MagicMock()
    mock_orch._run_worker = AsyncMock(return_value="pr_created")
    mock_issue = MagicMock()
    mock_issue.number = _ISSUE

    check_calls: list[Any] = []

    def _fake_check(
        owner: str,
        repo: str,
        *,
        app_id: str,
        runner: Any,  # noqa: ANN401
    ) -> RulesetCheckResult:
        check_calls.append((owner, repo, app_id))
        return RulesetCheckResult(
            status=RulesetStatus.DRIFT, detail=_DRIFT_DETAIL
        )

    with (
        patch.object(
            daemon_mod, "check_ruleset_signals", side_effect=_fake_check
        ),
        patch.object(
            daemon_mod, "_probe_worker_push_denied", return_value=True
        ),
        patch.object(mock_orch, "_run_worker", new=mock_orch._run_worker),
        caplog.at_level(logging.WARNING),
    ):
        result = asyncio.run(
            daemon_mod._launch_one_issue(  # type: ignore[attr-defined]
                mock_orch,
                mock_issue,
                _OWNER,
                _REPO,
                _APP_ID,
                _TOKEN,
                obs,
            )
        )

    mock_orch._run_worker.assert_called_once_with(mock_issue)
    assert result == "pr_created", (
        "Launch must PROCEED (the behavioral probe denied the push) even "
        "though the config-diff comparator reported DRIFT — the "
        f"comparator is now diagnostic-only; got {result!r}"
    )
    assert len(check_calls) == 1, (
        "check_ruleset_signals must still run exactly once (demoted to "
        f"diagnostic, not removed); got {len(check_calls)} calls"
    )
    assert any(
        r.levelno >= logging.WARNING and "DRIFT" in r.getMessage()
        for r in caplog.records
    ), (
        "A diagnostic WARNING naming the non-MATCH comparator result "
        f"(DRIFT) must be logged even though it did not block launch; "
        f"records: {[r.getMessage() for r in caplog.records]!r}"
    )


# ---------------------------------------------------------------------------
# B7 — probe NOT denied + comparator MATCH -> refuse; park semantics fire
# ---------------------------------------------------------------------------


def test_launch_refuses_when_probe_accepts_despite_comparator_match(
    tmp_path: Path,
) -> None:
    """Probe non-denial overrides a comparator MATCH result — refuses launch.

    Proves behavioral truth overrides "config looks fine": even a MATCH
    verdict must not allow launch when the behavioral probe shows a
    worker-identity push was ACCEPTED (protection breached). The existing
    park semantics (Slack alert, restored agent-ready label, cleared
    agent-in-progress, "preflight refused" comment) must still fire.

    Args:
        tmp_path: Pytest tmp_path fixture.
    """
    from unittest.mock import AsyncMock, MagicMock

    import baton_harness.chain.daemon as daemon_mod
    from baton_harness.chain.ruleset_status import (
        RulesetCheckResult,
        RulesetStatus,
    )

    _get_probe_fn(daemon_mod)

    obs = _make_obs(tmp_path)
    mock_orch = MagicMock()
    mock_orch._run_worker = AsyncMock(return_value="pr_created")
    mock_issue = MagicMock()
    mock_issue.number = _ISSUE

    slack_calls: list[tuple[str, str]] = []

    def _fake_post(url: str, message: str, **kwargs: Any) -> bool:  # noqa: ANN401
        slack_calls.append((url, message))
        return True

    label_edit_calls: list[dict[str, Any]] = []

    def _record_label_edit(
        owner: str,
        repo: str,
        number: int,
        *,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        label_edit_calls.append(
            {"add": list(add or []), "remove": list(remove or [])}
        )

    comment_calls: list[str] = []

    def _record_alert(
        owner: str,
        repo: str,
        issue: int,
        summary: str,
        **kwargs: Any,  # noqa: ANN401
    ) -> bool:
        comment_calls.append(summary)
        return True

    with (
        patch.object(
            daemon_mod,
            "check_ruleset_signals",
            return_value=RulesetCheckResult(status=RulesetStatus.MATCH),
        ),
        patch.object(
            daemon_mod, "_probe_worker_push_denied", return_value=False
        ),
        patch.object(daemon_mod, "post_slack_alert", side_effect=_fake_post),
        patch.object(
            daemon_mod, "_label_edit", side_effect=_record_label_edit
        ),
        patch.object(daemon_mod, "alert", side_effect=_record_alert),
        patch.object(mock_orch, "_run_worker", new=mock_orch._run_worker),
    ):
        result = asyncio.run(
            daemon_mod._launch_one_issue(  # type: ignore[attr-defined]
                mock_orch,
                mock_issue,
                _OWNER,
                _REPO,
                _APP_ID,
                _TOKEN,
                obs,
            )
        )

    mock_orch._run_worker.assert_not_called()
    assert result is None, (
        "Launch must be REFUSED — the behavioral push-denial probe "
        "reported NOT denied (accepted push) even though the comparator "
        f"reported MATCH; got {result!r}"
    )
    assert slack_calls, (
        "The refuse path must still fire the Slack alert even when the "
        "comparator says MATCH — behavioral truth overrides a clean "
        "config diff"
    )
    assert comment_calls and any(
        "preflight refused" in body for body in comment_calls
    ), (
        "A blocking 'preflight refused' comment must be posted on "
        f"refusal; got {comment_calls!r}"
    )
    net_removed = sum(
        1 for c in label_edit_calls if "agent-ready" in c["remove"]
    )
    net_added = sum(1 for c in label_edit_calls if "agent-ready" in c["add"])
    assert net_removed == 0 or net_added >= net_removed, (
        "agent-ready must not be net-removed on refusal; "
        f"label_edit calls: {label_edit_calls!r}"
    )
    net_ip_removed = sum(
        1 for c in label_edit_calls if "agent-in-progress" in c["remove"]
    )
    net_ip_added = sum(
        1 for c in label_edit_calls if "agent-in-progress" in c["add"]
    )
    assert net_ip_added == 0 or net_ip_removed >= net_ip_added, (
        "agent-in-progress must be cleared on refusal; "
        f"label_edit calls: {label_edit_calls!r}"
    )


# ---------------------------------------------------------------------------
# B8 — comparator still called exactly once, regardless of probe outcome
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("probe_denied", [True, False])
def test_comparator_called_exactly_once_regardless_of_probe_outcome(
    tmp_path: Path,
    probe_denied: bool,
) -> None:
    """check_ruleset_signals runs exactly once per launch, either outcome.

    Proves the comparator is demoted (still consulted for diagnostics),
    never removed and never called a second time to re-derive detail.

    Args:
        tmp_path: Pytest tmp_path fixture.
        probe_denied: The behavioral probe's outcome to drive this run.
    """
    from unittest.mock import AsyncMock, MagicMock

    import baton_harness.chain.daemon as daemon_mod
    from baton_harness.chain.ruleset_status import (
        RulesetCheckResult,
        RulesetStatus,
    )

    _get_probe_fn(daemon_mod)

    obs = _make_obs(tmp_path)
    mock_orch = MagicMock()
    mock_orch._run_worker = AsyncMock(return_value="pr_created")
    mock_issue = MagicMock()
    mock_issue.number = _ISSUE

    check_calls: list[Any] = []

    def _fake_check(
        owner: str,
        repo: str,
        *,
        app_id: str,
        runner: Any,  # noqa: ANN401
    ) -> RulesetCheckResult:
        check_calls.append(1)
        return RulesetCheckResult(status=RulesetStatus.MATCH)

    with (
        patch.object(
            daemon_mod, "check_ruleset_signals", side_effect=_fake_check
        ),
        patch.object(
            daemon_mod,
            "_probe_worker_push_denied",
            return_value=probe_denied,
        ),
        patch.object(daemon_mod, "post_slack_alert", return_value=True),
        patch.object(daemon_mod, "_label_edit", return_value=None),
        patch.object(daemon_mod, "alert", return_value=True),
        patch.object(mock_orch, "_run_worker", new=mock_orch._run_worker),
    ):
        asyncio.run(
            daemon_mod._launch_one_issue(  # type: ignore[attr-defined]
                mock_orch,
                mock_issue,
                _OWNER,
                _REPO,
                _APP_ID,
                _TOKEN,
                obs,
            )
        )

    assert len(check_calls) == 1, (
        "check_ruleset_signals (the demoted diagnostic comparator) must "
        "still be called exactly once per launch decision regardless of "
        f"the probe's outcome; got {len(check_calls)} calls "
        f"(probe_denied={probe_denied!r})"
    )


# ---------------------------------------------------------------------------
# B9 — an unexpected exception from the probe itself fails closed (defense
# in depth at the composite-gate level, distinct from A4b's probe-internal
# handling of a subprocess-seam exception)
# ---------------------------------------------------------------------------


def test_launch_refuses_and_parks_when_probe_itself_raises_unexpectedly(
    tmp_path: Path,
) -> None:
    """An exception raised BY the probe must not crash the launch decision.

    Even if ``_probe_worker_push_denied`` itself raises (bypassing its
    own internal fail-closed handling), the composite launch decision
    must not propagate the exception and must still refuse + park —
    never treat an indeterminate/crashed probe as "safe to launch".

    Args:
        tmp_path: Pytest tmp_path fixture.
    """
    from unittest.mock import AsyncMock, MagicMock

    import baton_harness.chain.daemon as daemon_mod
    from baton_harness.chain.ruleset_status import (
        RulesetCheckResult,
        RulesetStatus,
    )

    _get_probe_fn(daemon_mod)

    obs = _make_obs(tmp_path)
    mock_orch = MagicMock()
    mock_orch._run_worker = AsyncMock(return_value="pr_created")
    mock_issue = MagicMock()
    mock_issue.number = _ISSUE

    comment_calls: list[str] = []

    def _record_alert(
        owner: str,
        repo: str,
        issue: int,
        summary: str,
        **kwargs: Any,  # noqa: ANN401
    ) -> bool:
        comment_calls.append(summary)
        return True

    def _raising_probe(*args: Any, **kwargs: Any) -> bool:  # noqa: ANN401
        raise RuntimeError("push probe blew up unexpectedly")

    with (
        patch.object(
            daemon_mod,
            "check_ruleset_signals",
            return_value=RulesetCheckResult(status=RulesetStatus.MATCH),
        ),
        patch.object(
            daemon_mod, "_probe_worker_push_denied", side_effect=_raising_probe
        ),
        patch.object(daemon_mod, "post_slack_alert", return_value=True),
        patch.object(daemon_mod, "_label_edit", return_value=None),
        patch.object(daemon_mod, "alert", side_effect=_record_alert),
        patch.object(mock_orch, "_run_worker", new=mock_orch._run_worker),
    ):
        # Must NOT raise — the daemon must continue cleanly and refuse.
        result = asyncio.run(
            daemon_mod._launch_one_issue(  # type: ignore[attr-defined]
                mock_orch,
                mock_issue,
                _OWNER,
                _REPO,
                _APP_ID,
                _TOKEN,
                obs,
            )
        )

    mock_orch._run_worker.assert_not_called()
    assert result is None, (
        "An unexpected exception from the probe must fail-closed (refuse "
        f"+ park), never be treated as safe-to-launch; got {result!r}"
    )
    assert comment_calls, (
        "A blocking comment must still be posted when the probe itself "
        "raises unexpectedly"
    )
