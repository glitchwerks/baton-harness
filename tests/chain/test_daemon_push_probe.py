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

CodeRabbit correctness fixes on PR #253 (tracked as issue #223 follow-ups)
------------------------------------------------------------------------

Four corrections to the contract above, pinned by this revision of the
file:

1. ``_probe_worker_push_denied`` no longer returns a bare ``bool``. It
   returns a ``ProbeResult`` (a frozen dataclass the code-writer defines
   at module scope in ``daemon.py``, alongside a ``ProbeDenialReason``
   enum)::

       class ProbeDenialReason(Enum):
           ACCEPTED = "accepted"              # push was ACCEPTED (breach)
           UNRECOGNIZED = "unrecognized"       # rejected, unrecognized signal
           TRANSPORT_ERROR = "transport_error"  # raised exception on push
           TIMEOUT = "timeout"                  # push/cleanup timed out
           CLEANUP_FAILED = "cleanup_failed"    # accepted push, cleanup
                                                 # delete did not succeed

       @dataclass(frozen=True)
       class ProbeResult:
           denied: bool                          # True only = safe/DENIED
           reason: ProbeDenialReason | None = None  # None iff denied
           detail: str = ""                      # human-readable detail

   ``result.denied is True`` replaces the old ``result is True`` (safe,
   push-protection intact; ``result.reason`` is ``None``).
   ``result.denied is False`` replaces the old ``result is False``, and
   ``result.reason`` names WHY: ``ACCEPTED``, ``UNRECOGNIZED``,
   ``TRANSPORT_ERROR``, ``TIMEOUT``, or ``CLEANUP_FAILED``. Tests A2/A3/
   A4a/A4b below are updated in place to assert on ``.denied``/``.reason``
   instead of raw booleans, and B6/B7/B8 supply ``ProbeResult`` instances
   wherever they previously supplied a bare ``bool`` to the patched
   ``_probe_worker_push_denied``. The refusal alert built in
   ``_should_launch_worker`` must include the reason (see the updated B7
   assertions).

2. Fail-closed for a non-git ``repo_root`` (C10 below): when
   ``_launch_one_issue`` resolves a ``repo_root`` with no ``.git`` entry,
   the behavioral probe cannot run — launch must be refused outright,
   never routed through the comparator-only gate (where a bare MATCH
   would otherwise authorize launch with no behavioral check at all).

3. A stalled probe push/cleanup must not block daemon startup forever
   (C12/C13 below): ``_run`` must be given a timeout for both the push
   and the cleanup delete, and a ``subprocess.TimeoutExpired`` must be
   distinguished from a generic transport error via
   ``ProbeDenialReason.TIMEOUT``.

4. A nonzero-returncode cleanup delete must not be silently swallowed
   (C11 below): it must be logged/escalated and surfaced via
   ``ProbeDenialReason.CLEANUP_FAILED``, distinct from the plain
   ``ACCEPTED`` reason used when cleanup itself succeeds.

Because a corrected ``_run`` may now be called with an additional
``timeout=`` keyword, every hand-written ``_run`` stub/spy in this file
accepts ``**kwargs`` (or an explicit ``timeout`` capture) so a compliant
implementation does not trip a spurious ``TypeError`` in an unrelated,
already-pinned test.

CodeRabbit correctness fixes on PR #253, round 2
-------------------------------------------------

Finding #6 (Data Integrity, Major): ``_probe_worker_push_denied`` only
attempted cleanup (``git push origin --delete <probe_ref>``) on the
ACCEPTED-push path. A TIMEOUT, TRANSPORT_ERROR, or UNRECOGNIZED-rejection
outcome does NOT prove the remote rejected the push — the throwaway ref
may have been created before the client lost the result, leaving durable
residue on origin.  The fix (pinned by the C14-C18 tests below): attempt
an IDEMPOTENT cleanup delete for every outcome EXCEPT a confirmed denial
(``denied=True``), while preserving the ORIGINAL refusal reason — a
cleanup attempt must never overwrite ``TIMEOUT``/``TRANSPORT_ERROR``/
``UNRECOGNIZED`` with the unrelated ``CLEANUP_FAILED`` reason, which
stays reserved for the accepted-push cleanup-failure case (see C11
above). A cleanup failure encountered on one of these indeterminate
paths must be escalated (logged at ERROR) and reflected in the result's
``detail`` text without discarding the original ``reason``.

(Finding #5 — bounding the diagnostic comparator runner with a timeout —
is pinned separately in ``tests/chain/test_daemon_preflight.py`` and
``tests/test_ruleset_status.py``, since it concerns
``_build_preflight_runner``/``check_ruleset_signals``, not the push
probe defined in this file.)
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


def _get_probe_result_types(daemon_mod: Any) -> tuple[Any, Any]:  # noqa: ANN401
    """Fetch the corrected result-type contract (CodeRabbit finding #2).

    Args:
        daemon_mod: The imported ``baton_harness.chain.daemon`` module.

    Returns:
        A ``(ProbeResult, ProbeDenialReason)`` tuple.
    """
    result_cls = getattr(daemon_mod, "ProbeResult", None)
    reason_enum = getattr(daemon_mod, "ProbeDenialReason", None)
    assert result_cls is not None and reason_enum is not None, (
        "chain/daemon.py must define module-level `ProbeResult` (a "
        "frozen dataclass with `denied: bool`, "
        "`reason: ProbeDenialReason | None = None`, `detail: str = ''`) "
        "and `ProbeDenialReason` (an Enum with ACCEPTED, UNRECOGNIZED, "
        "TRANSPORT_ERROR, TIMEOUT, and CLEANUP_FAILED members) — see "
        "this test file's module docstring for the full contract "
        "(CodeRabbit finding #2 on PR #253)"
    )
    return result_cls, reason_enum


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
        cmd: list[str],
        env: dict[str, str] | None = None,
        **_kwargs: Any,  # noqa: ANN401 (e.g. a future `timeout=` kwarg)
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
        cmd: list[str],
        env: dict[str, str] | None = None,
        **_kwargs: Any,  # noqa: ANN401 (e.g. a future `timeout=` kwarg)
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
        # env_for is bound locally inside daemon.push_probe (#273, Phase
        # 6a step 2) -- patching daemon_mod.env_for would not affect
        # this submodule's own import-time binding, so patch it where
        # it's actually looked up.
        patch.object(
            daemon_mod.push_probe, "env_for", side_effect=_spy_env_for
        ),
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
    _get_probe_result_types(daemon_mod)  # fail fast if the type is missing

    with patch.object(
        daemon_mod,
        "_run",
        side_effect=_run_side_effect(returncode=1, stderr=_DENIAL_STDERR),
    ):
        result = probe_fn(tmp_path)

    assert result.denied is True, (
        "A rejected push (non-zero exit) carrying a recognizable denial "
        "signal (GH006 / 'protected branch hook declined') must be "
        f"reported as DENIED (push protection intact); got "
        f"result.denied={result.denied!r}"
    )
    assert result.reason is None, (
        "A DENIED (safe) ProbeResult must carry no not-safe reason "
        f"(reason is only set for a not-safe outcome); got "
        f"{result.reason!r}"
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
    _, ProbeDenialReason = _get_probe_result_types(daemon_mod)  # noqa: N806

    run_calls: list[list[str]] = []

    with patch.object(
        daemon_mod,
        "_run",
        side_effect=_run_side_effect(returncode=0, calls=run_calls),
    ):
        result = probe_fn(tmp_path)

    assert result.denied is False, (
        "An ACCEPTED push (returncode 0) means the push-protection "
        "boundary was BREACHED — the probe must report NOT denied; "
        f"got result.denied={result.denied!r}"
    )
    assert result.reason == ProbeDenialReason.ACCEPTED, (
        "An accepted push whose cleanup delete ALSO succeeded must carry "
        f"the plain ACCEPTED reason; got {result.reason!r}"
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
    _, ProbeDenialReason = _get_probe_result_types(daemon_mod)  # noqa: N806

    with patch.object(
        daemon_mod,
        "_run",
        side_effect=_run_side_effect(
            returncode=128, stderr=_UNRECOGNIZED_STDERR
        ),
    ):
        result = probe_fn(tmp_path)

    assert result.denied is False, (
        "A non-zero exit WITHOUT a recognizable denial signal (e.g. a "
        "transport/network failure) is UNPROVEN, not a confirmed "
        f"denial — the probe must fail closed; got "
        f"result.denied={result.denied!r}"
    )
    assert result.reason == ProbeDenialReason.UNRECOGNIZED, (
        "An unrecognized non-zero exit must carry the UNRECOGNIZED "
        f"reason, distinct from a raised transport exception; got "
        f"{result.reason!r}"
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
    _, ProbeDenialReason = _get_probe_result_types(daemon_mod)  # noqa: N806

    def _raising_run(
        cmd: list[str],
        env: dict[str, str] | None = None,
        **_kwargs: Any,  # noqa: ANN401 (e.g. a future `timeout=` kwarg)
    ) -> subprocess.CompletedProcess[str]:
        raise OSError("network unreachable")

    with patch.object(daemon_mod, "_run", side_effect=_raising_run):
        result = probe_fn(tmp_path)  # must NOT raise

    assert result.denied is False, (
        "A raised transport/subprocess exception must be treated as "
        f"unproven ⇒ NOT denied (fail-closed); got "
        f"result.denied={result.denied!r}"
    )
    assert result.reason == ProbeDenialReason.TRANSPORT_ERROR, (
        "A raised OSError from the push subprocess call must carry the "
        f"TRANSPORT_ERROR reason, distinct from TIMEOUT; got "
        f"{result.reason!r}"
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
        cmd: list[str],
        env: dict[str, str] | None = None,
        **_kwargs: Any,  # noqa: ANN401 (e.g. a future `timeout=` kwarg)
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
    ProbeResult, _ = _get_probe_result_types(daemon_mod)  # noqa: N806

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
            daemon_mod,
            "_probe_worker_push_denied",
            return_value=ProbeResult(denied=True),
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

_ACCEPTED_PUSH_DETAIL = (
    "probe push to feature/__bh-probe-abc123 was ACCEPTED (returncode=0) "
    "— push-protection boundary breached"
)


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
    ProbeResult, ProbeDenialReason = _get_probe_result_types(daemon_mod)  # noqa: N806

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
            daemon_mod,
            "_probe_worker_push_denied",
            return_value=ProbeResult(
                denied=False,
                reason=ProbeDenialReason.ACCEPTED,
                detail=_ACCEPTED_PUSH_DETAIL,
            ),
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
    alert_message = slack_calls[0][1]
    assert (
        _ACCEPTED_PUSH_DETAIL in alert_message
        or ProbeDenialReason.ACCEPTED.name in alert_message
    ), (
        "The refusal alert must include the probe's failure reason "
        "(CodeRabbit finding #2) — either the ProbeResult.detail text or "
        f"the ProbeDenialReason name — not just a generic refusal "
        f"message; got {alert_message!r}"
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
    ProbeResult, ProbeDenialReason = _get_probe_result_types(daemon_mod)  # noqa: N806

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

    probe_result = ProbeResult(
        denied=probe_denied,
        reason=None if probe_denied else ProbeDenialReason.ACCEPTED,
    )

    with (
        patch.object(
            daemon_mod, "check_ruleset_signals", side_effect=_fake_check
        ),
        patch.object(
            daemon_mod,
            "_probe_worker_push_denied",
            return_value=probe_result,
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


# ---------------------------------------------------------------------------
# C10 — Fix #1: non-git repo_root must refuse launch outright, never
# routed through the comparator-only gate (CodeRabbit finding, line 410)
# ---------------------------------------------------------------------------


def test_launch_refuses_when_repo_root_has_no_git_dir_despite_comparator_match(
    tmp_path: Path,
) -> None:
    """A non-git ``repo_root`` must fail closed, even on comparator MATCH.

    Before the fix: ``_launch_one_issue`` resolves a ``repo_root`` with no
    ``.git`` entry, sets the module-level active-probe-repo-root to
    ``None``, and that routes the decision into the legacy
    comparator-only gate — where a bare ``RulesetStatus.MATCH`` alone
    authorizes launch with NO behavioral push-denial check at all.

    After the fix: a non-git ``repo_root`` means the decisive behavioral
    probe cannot run, so launch must be refused outright — the comparator
    must not, by itself, ever authorize a launch it could not verify
    behaviorally.

    ``tmp_path`` is a plain directory with no ``.git`` subdirectory,
    exactly the "resolved but not a git worktree" case the finding
    describes.

    Args:
        tmp_path: Pytest tmp_path fixture; deliberately NOT a git
            worktree (used directly as ``repo_root``).
    """
    from unittest.mock import AsyncMock, MagicMock

    import baton_harness.chain.daemon as daemon_mod
    from baton_harness.chain.ruleset_status import (
        RulesetCheckResult,
        RulesetStatus,
    )

    obs = _make_obs(tmp_path)
    mock_orch = MagicMock()
    mock_orch._run_worker = AsyncMock(return_value="pr_created")
    mock_issue = MagicMock()
    mock_issue.number = _ISSUE

    def _forbidden_probe(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise AssertionError(
            "the behavioral push-denial probe must not be invoked "
            "against a non-git repo_root — there is no git worktree to "
            "push from; the fix is to refuse launch outright rather "
            "than fall back to the comparator-only gate"
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
            daemon_mod,
            "_probe_worker_push_denied",
            side_effect=_forbidden_probe,
        ),
        patch.object(daemon_mod, "post_slack_alert", return_value=True),
        patch.object(daemon_mod, "_label_edit", return_value=None),
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
                repo_root=tmp_path,
            )
        )

    mock_orch._run_worker.assert_not_called()
    assert result is None, (
        "A repo_root with no .git directory means the decisive "
        "behavioral probe cannot run — launch must be refused (fail "
        "closed) even though the comparator reported MATCH; got "
        f"{result!r}"
    )
    assert comment_calls, (
        "A blocking comment must be posted when launch is refused "
        "because the repo_root is not a git worktree"
    )


# ---------------------------------------------------------------------------
# C11 — Fix #4: a nonzero-returncode cleanup delete must be surfaced as a
# distinct CLEANUP_FAILED reason, not silently swallowed (CodeRabbit
# finding, line 623)
# ---------------------------------------------------------------------------


def test_probe_reports_cleanup_failed_when_delete_returns_nonzero(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed cleanup delete (nonzero exit, no exception) must not vanish.

    Before the fix: only a RAISED exception from the cleanup delete is
    caught; a normal nonzero returncode is silently ignored, leaving the
    worker-created probe ref on origin (durable residue) with no trace in
    the result or the logs.

    After the fix: the cleanup returncode is checked. A nonzero exit is
    logged/escalated at ERROR level and surfaced via the distinct
    ``ProbeDenialReason.CLEANUP_FAILED`` — never collapsed into the plain
    ``ACCEPTED`` reason used when cleanup itself succeeds (see A3, which
    still expects ``ACCEPTED`` when cleanup returns 0).

    Args:
        tmp_path: Pytest tmp_path fixture; used as a stand-in repo_root.
        caplog: Pytest log-capture fixture.
    """
    import baton_harness.chain.daemon as daemon_mod

    probe_fn = _get_probe_fn(daemon_mod)
    _, ProbeDenialReason = _get_probe_result_types(daemon_mod)  # noqa: N806

    # First _run call = the probe push (accepted, rc=0). Second call =
    # the cleanup delete, which fails with a nonzero returncode but does
    # NOT raise.
    responses = [
        subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        ),
        subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=(
                "error: unable to delete "
                "'refs/heads/feature/__bh-probe-abc123': remote ref "
                "does not exist"
            ),
        ),
    ]
    run_calls: list[list[str]] = []

    def _sequenced_run(
        cmd: list[str],
        env: dict[str, str] | None = None,
        **_kwargs: Any,  # noqa: ANN401 (e.g. a future `timeout=` kwarg)
    ) -> subprocess.CompletedProcess[str]:
        run_calls.append(list(cmd))
        return responses[len(run_calls) - 1]

    with (
        patch.object(daemon_mod, "_run", side_effect=_sequenced_run),
        caplog.at_level(logging.ERROR),
    ):
        result = probe_fn(tmp_path)

    assert result.denied is False, (
        "An accepted push is never safe, regardless of the cleanup "
        f"outcome; got result.denied={result.denied!r}"
    )
    assert result.reason == ProbeDenialReason.CLEANUP_FAILED, (
        "A cleanup delete that returns a nonzero exit code (no "
        "exception raised) must be surfaced as its own distinct "
        "CLEANUP_FAILED reason — not swallowed, and not collapsed into "
        f"the generic ACCEPTED reason; got {result.reason!r}"
    )
    assert len(run_calls) >= 2, (
        "the cleanup delete must still be attempted even though the "
        f"probe already knows the push was accepted; calls: {run_calls!r}"
    )
    assert any(r.levelno >= logging.ERROR for r in caplog.records), (
        "A failed cleanup delete must be logged/escalated at ERROR "
        "level, not silently ignored; previously only a RAISED "
        f"exception was caught. records: "
        f"{[r.getMessage() for r in caplog.records]!r}"
    )


# ---------------------------------------------------------------------------
# C12/C13 — Fix #3: a stalled probe push/cleanup must not block daemon
# startup forever (CodeRabbit finding, line 603)
# ---------------------------------------------------------------------------


def test_probe_fails_closed_with_timeout_reason_when_push_times_out(
    tmp_path: Path,
) -> None:
    """A stalled push (``subprocess.TimeoutExpired``) fails closed as TIMEOUT.

    Distinct from the generic ``TRANSPORT_ERROR`` reason used for other
    raised exceptions (see A4b) — an operator debugging a daemon that
    never finishes starting up needs to see "timeout", not a generic
    transport failure, so a stuck credential prompt is diagnosable at a
    glance.

    Args:
        tmp_path: Pytest tmp_path fixture; used as a stand-in repo_root.
    """
    import baton_harness.chain.daemon as daemon_mod

    probe_fn = _get_probe_fn(daemon_mod)
    _, ProbeDenialReason = _get_probe_result_types(daemon_mod)  # noqa: N806

    def _timing_out_run(
        cmd: list[str],
        env: dict[str, str] | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            cmd=cmd, timeout=kwargs.get("timeout", 30)
        )

    with patch.object(daemon_mod, "_run", side_effect=_timing_out_run):
        result = probe_fn(tmp_path)  # must NOT raise

    assert result.denied is False, (
        "A timed-out probe push is unproven — never treated as a "
        f"confirmed denial; got result.denied={result.denied!r}"
    )
    assert result.reason == ProbeDenialReason.TIMEOUT, (
        "A subprocess.TimeoutExpired raised from the probe push must be "
        "reported as the distinct TIMEOUT reason, not the generic "
        f"TRANSPORT_ERROR reason used for other exceptions; got "
        f"{result.reason!r}"
    )


def test_probe_push_and_cleanup_are_each_given_a_positive_timeout(
    tmp_path: Path,
) -> None:
    """Both the probe push and its cleanup delete must request a deadline.

    Without a deadline threaded down to the subprocess call, a stalled
    git/credential prompt can block daemon startup indefinitely — merely
    catching ``subprocess.TimeoutExpired`` defensively (see the previous
    test) is not sufficient if no timeout is ever actually requested.

    Args:
        tmp_path: Pytest tmp_path fixture; used as a stand-in repo_root.
    """
    import baton_harness.chain.daemon as daemon_mod

    probe_fn = _get_probe_fn(daemon_mod)

    call_kwargs: list[dict[str, Any]] = []

    def _spy_run(
        cmd: list[str],
        env: dict[str, str] | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> subprocess.CompletedProcess[str]:
        call_kwargs.append(kwargs)
        # returncode=0 (accepted) so a cleanup delete call is also made.
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    with patch.object(daemon_mod, "_run", side_effect=_spy_run):
        probe_fn(tmp_path)

    assert len(call_kwargs) >= 2, (
        "expected both a push call and a cleanup delete call to _run; "
        f"got {call_kwargs!r}"
    )
    for i, kwargs in enumerate(call_kwargs):
        timeout_value = kwargs.get("timeout")
        call_label = "push" if i == 0 else "cleanup delete"
        assert (
            isinstance(timeout_value, (int, float))
            and not isinstance(timeout_value, bool)
            and timeout_value > 0
        ), (
            f"_run call #{i + 1} ({call_label}) must be given a positive "
            "`timeout=` kwarg so a stalled git/credential prompt cannot "
            f"block daemon startup indefinitely; got kwargs={kwargs!r}"
        )


# ---------------------------------------------------------------------------
# C14-C18 — Finding #6 (PR #253 round 2): reconcile the probe ref after
# indeterminate outcomes. Cleanup must be attempted for every outcome
# EXCEPT a confirmed denial, and the ORIGINAL refusal reason must survive
# the cleanup attempt regardless of whether cleanup itself succeeds,
# fails, or raises.
# ---------------------------------------------------------------------------


def _first_call_raises_then_succeeds(
    *,
    exc: BaseException,
    cleanup_returncode: int = 0,
    cleanup_stderr: str = "",
    calls: list[list[str]] | None = None,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Build a ``daemon_mod._run`` stub: first call raises, rest succeed.

    Simulates an indeterminate push outcome (a raised timeout or
    transport exception) followed by a normal cleanup-delete attempt, so
    tests can assert the cleanup call actually happens after the first
    call fails.

    Args:
        exc: Exception instance to raise on the FIRST invocation only.
        cleanup_returncode: Return code for every call after the first.
        cleanup_stderr: stderr text for every call after the first.
        calls: Optional list that each invoked ``cmd`` is appended onto,
            for spying on call count/order/content.

    Returns:
        A callable matching ``daemon_mod._run(cmd, env=None, **kwargs)``.
    """
    call_count = 0

    def _side_effect(
        cmd: list[str],
        env: dict[str, str] | None = None,
        **_kwargs: Any,  # noqa: ANN401 (e.g. a `timeout=` kwarg)
    ) -> subprocess.CompletedProcess[str]:
        nonlocal call_count
        call_count += 1
        if calls is not None:
            calls.append(list(cmd))
        if call_count == 1:
            raise exc
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=cleanup_returncode,
            stdout="",
            stderr=cleanup_stderr,
        )

    return _side_effect


def test_probe_attempts_cleanup_on_timeout_and_preserves_timeout_reason(
    tmp_path: Path,
) -> None:
    """A timed-out push must still attempt cleanup; reason stays TIMEOUT.

    CodeRabbit finding #6 (PR #253 round 2): before the fix, only the
    ACCEPTED-push path attempted a cleanup delete. A TIMEOUT outcome does
    NOT prove the remote rejected the push — the throwaway ref may have
    been created before the client lost the result — so a cleanup delete
    must be attempted here too, and the ORIGINAL TIMEOUT reason must be
    preserved (never overwritten by the cleanup attempt's own outcome).

    Args:
        tmp_path: Pytest tmp_path fixture; used as a stand-in repo_root.
    """
    import baton_harness.chain.daemon as daemon_mod

    probe_fn = _get_probe_fn(daemon_mod)
    _, ProbeDenialReason = _get_probe_result_types(daemon_mod)  # noqa: N806

    run_calls: list[list[str]] = []
    timeout_exc = subprocess.TimeoutExpired(cmd=["git", "push"], timeout=30)

    with patch.object(
        daemon_mod,
        "_run",
        side_effect=_first_call_raises_then_succeeds(
            exc=timeout_exc, calls=run_calls
        ),
    ):
        result = probe_fn(tmp_path)

    assert result.denied is False, (
        "a timed-out push is unproven, never a confirmed denial; got "
        f"result.denied={result.denied!r}"
    )
    assert result.reason == ProbeDenialReason.TIMEOUT, (
        "the ORIGINAL TIMEOUT reason must be preserved even though a "
        f"cleanup attempt followed it; got {result.reason!r}"
    )
    assert len(run_calls) >= 2, (
        "a TIMEOUT push outcome does not prove the remote rejected the "
        "push — the probe ref may have been created before the client "
        "lost the result, so a cleanup delete must still be attempted; "
        f"only saw calls: {run_calls!r}"
    )


def test_probe_attempts_cleanup_on_transport_error_and_preserves_reason(
    tmp_path: Path,
) -> None:
    """A raised transport exception must still attempt cleanup.

    Reason stays TRANSPORT_ERROR — the same "unproven, not confirmed
    denial" rationale as the TIMEOUT case above.

    Args:
        tmp_path: Pytest tmp_path fixture; used as a stand-in repo_root.
    """
    import baton_harness.chain.daemon as daemon_mod

    probe_fn = _get_probe_fn(daemon_mod)
    _, ProbeDenialReason = _get_probe_result_types(daemon_mod)  # noqa: N806

    run_calls: list[list[str]] = []
    transport_exc = OSError("network unreachable")

    with patch.object(
        daemon_mod,
        "_run",
        side_effect=_first_call_raises_then_succeeds(
            exc=transport_exc, calls=run_calls
        ),
    ):
        result = probe_fn(tmp_path)

    assert result.denied is False, (
        "a raised transport exception is unproven, never a confirmed "
        f"denial; got result.denied={result.denied!r}"
    )
    assert result.reason == ProbeDenialReason.TRANSPORT_ERROR, (
        "the ORIGINAL TRANSPORT_ERROR reason must be preserved even "
        f"though a cleanup attempt followed it; got {result.reason!r}"
    )
    assert len(run_calls) >= 2, (
        "a TRANSPORT_ERROR push outcome does not prove the remote "
        "rejected the push — a cleanup delete must still be attempted; "
        f"only saw calls: {run_calls!r}"
    )


def test_probe_attempts_cleanup_on_unrecognized_rejection_and_preserves_reason(
    tmp_path: Path,
) -> None:
    """An unrecognized nonzero rejection must still attempt cleanup.

    Reason stays UNRECOGNIZED — a rejection with no known denial signal
    is unproven, same as the raised-exception cases above.

    Args:
        tmp_path: Pytest tmp_path fixture; used as a stand-in repo_root.
    """
    import baton_harness.chain.daemon as daemon_mod

    probe_fn = _get_probe_fn(daemon_mod)
    _, ProbeDenialReason = _get_probe_result_types(daemon_mod)  # noqa: N806

    run_calls: list[list[str]] = []
    responses = [
        subprocess.CompletedProcess(
            args=[], returncode=128, stdout="", stderr=_UNRECOGNIZED_STDERR
        ),
        subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        ),
    ]

    def _sequenced_run(
        cmd: list[str],
        env: dict[str, str] | None = None,
        **_kwargs: Any,  # noqa: ANN401 (e.g. a `timeout=` kwarg)
    ) -> subprocess.CompletedProcess[str]:
        run_calls.append(list(cmd))
        return responses[len(run_calls) - 1]

    with patch.object(daemon_mod, "_run", side_effect=_sequenced_run):
        result = probe_fn(tmp_path)

    assert result.denied is False, (
        "an unrecognized nonzero rejection is unproven, never a "
        f"confirmed denial; got result.denied={result.denied!r}"
    )
    assert result.reason == ProbeDenialReason.UNRECOGNIZED, (
        "the ORIGINAL UNRECOGNIZED reason must be preserved even though "
        f"a cleanup attempt followed it; got {result.reason!r}"
    )
    assert len(run_calls) >= 2, (
        "an UNRECOGNIZED push outcome does not prove the remote "
        "rejected the push — a cleanup delete must still be attempted; "
        f"only saw calls: {run_calls!r}"
    )


def test_probe_does_not_attempt_cleanup_on_confirmed_denial(
    tmp_path: Path,
) -> None:
    """A CONFIRMED denial must NOT attempt any cleanup delete.

    A recognized denial signal proves the probe ref was never created on
    origin (the push was rejected before a ref could land), so there is
    nothing to clean up — attempting a delete here would be a wasted
    (and potentially confusing) extra call.

    Args:
        tmp_path: Pytest tmp_path fixture; used as a stand-in repo_root.
    """
    import baton_harness.chain.daemon as daemon_mod

    probe_fn = _get_probe_fn(daemon_mod)

    run_calls: list[list[str]] = []

    with patch.object(
        daemon_mod,
        "_run",
        side_effect=_run_side_effect(
            returncode=1, stderr=_DENIAL_STDERR, calls=run_calls
        ),
    ):
        result = probe_fn(tmp_path)

    assert result.denied is True, (
        f"a recognized denial signal must report denied=True; got "
        f"result.denied={result.denied!r}"
    )
    assert len(run_calls) == 1, (
        "a confirmed denial proves the probe ref was never created on "
        "origin — no cleanup delete should be attempted; calls: "
        f"{run_calls!r}"
    )


def test_probe_escalates_cleanup_failure_on_timeout_but_keeps_reason(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A FAILED cleanup on an indeterminate path must escalate, not hide.

    When the TIMEOUT path's cleanup delete ALSO fails (nonzero exit, a
    genuine failure — not an idempotent "ref does not exist" outcome),
    the failure must be logged at ERROR level and reflected in the
    result's ``detail`` text — but the reason must stay the ORIGINAL
    ``TIMEOUT``, never overwritten by ``CLEANUP_FAILED`` (that reason is
    reserved for the accepted-push cleanup-failure case pinned by C11
    above — a distinct scenario from residue left by an indeterminate
    outcome).

    Args:
        tmp_path: Pytest tmp_path fixture; used as a stand-in repo_root.
        caplog: Pytest log-capture fixture.
    """
    import baton_harness.chain.daemon as daemon_mod

    probe_fn = _get_probe_fn(daemon_mod)
    _, ProbeDenialReason = _get_probe_result_types(daemon_mod)  # noqa: N806

    run_calls: list[list[str]] = []
    timeout_exc = subprocess.TimeoutExpired(cmd=["git", "push"], timeout=30)

    with (
        patch.object(
            daemon_mod,
            "_run",
            side_effect=_first_call_raises_then_succeeds(
                exc=timeout_exc,
                cleanup_returncode=1,
                cleanup_stderr="remote: Permission denied (publickey)",
                calls=run_calls,
            ),
        ),
        caplog.at_level(logging.ERROR),
    ):
        result = probe_fn(tmp_path)

    assert result.reason == ProbeDenialReason.TIMEOUT, (
        "a FAILED cleanup delete on an indeterminate (TIMEOUT) outcome "
        "must never overwrite the ORIGINAL refusal reason — "
        "CLEANUP_FAILED is reserved for the accepted-push cleanup-fail "
        f"case (see C11); got {result.reason!r}"
    )
    assert len(run_calls) >= 2, (
        "the cleanup delete must still be attempted on the TIMEOUT path "
        f"even though it goes on to fail; calls: {run_calls!r}"
    )
    assert "cleanup" in (result.detail or "").lower(), (
        "the cleanup failure must be surfaced in the result detail "
        "without discarding the original TIMEOUT reason; got detail="
        f"{result.detail!r}"
    )
    assert any(r.levelno >= logging.ERROR for r in caplog.records), (
        "a cleanup-delete failure following an indeterminate probe "
        "outcome must be logged/escalated at ERROR level — durable "
        "residue left on origin is a data-integrity concern, not "
        "something to silently swallow; records: "
        f"{[r.getMessage() for r in caplog.records]!r}"
    )
