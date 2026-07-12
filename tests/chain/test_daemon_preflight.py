"""Tests for the daemon per-launch preflight wiring.

This test file pins the contract for a small, testable seam that the
code-writer must implement at the worker-launch site in ``daemon.py``.

Proposed seam (code-writer adopts this name in Phase 2):

    _should_launch_worker(
        issue_number: int,
        owner: str,
        repo: str,
        *,
        app_id: str,
        runner: Callable[[list[str]], subprocess.CompletedProcess[str]],
        obs: ObsConfig,
    ) -> bool

#206 hard swap: ``_should_launch_worker`` calls ``check_ruleset_signals``
(NOT ``ruleset_is_provisioned`` — that call site is being removed) and:
  1. Returns ``True`` only when the returned ``RulesetCheckResult.status``
     is ``RulesetStatus.MATCH``.
  2. On any non-MATCH result (``DRIFT``, ``ABSENT``, ``ERROR``, or
     ``NOT_PROVISIONED``):
     - Returns ``False`` (refuse to launch).
     - Calls ``post_slack_alert(obs.heartbeat_ping_url, <message>)`` when
       ``obs.heartbeat_ping_url`` is not None.
     - Does NOT crash if ``post_slack_alert`` raises (fail-closed, not
       fail-open).
     - Builds the alert message from the SINGLE ``RulesetCheckResult``
       already returned by ``check_ruleset_signals`` — the failed-checks
       text is the result's ``.detail`` — never a second call to
       ``check_ruleset_signals`` and never a call to
       ``ruleset_is_provisioned`` (the old ``_drift_detail_message``
       double-GET helper is deleted).
  3. When ``obs.heartbeat_ping_url`` is None, skips the POST entirely
     but still refuses to launch (returns False for non-MATCH).

The alert message body for Charge 5 must satisfy:
  ``"baton-harness refusing to launch worker"`` is a substring, AND
  the failed-checks description is present (e.g. ``"Failed checks:"``).

All external calls (``check_ruleset_signals``, ``post_slack_alert``) are
mocked.  Tests work with a minimal hand-constructed ObsConfig rather than
importing the full daemon start-up machinery.

Coverage:
- MATCH → _should_launch_worker returns True; no alert sent; no parking.
- DRIFT → returns False; alert carries the result's ``.detail`` verbatim
  (proving a single ``check_ruleset_signals`` call, not a second GET).
- ABSENT → returns False; alert body carries the result's ``.detail``
  (missing ruleset(s)).
- ERROR → returns False (fail-closed); alert carries the result's
  ``.detail`` (error path).
- NOT_PROVISIONED → returns False (fail-closed — a missing baseline must
  park, not launch); alert names the fix (``bin/provision-ruleset.sh``)
  and is textually distinct from the DRIFT message.
- Regression guard: the gate never calls ``ruleset_is_provisioned`` (it
  stays in the module for the provisioning-side verifier, but the daemon
  gate must not call it — catches an accidental re-introduction).
- Alert POST failure does NOT crash launch decision loop.
- No ``BH_HEARTBEAT_PING_URL`` configured (obs.heartbeat_ping_url is None)
  → returns False on DRIFT but no POST attempted; a warning is logged.

Additional coverage added for codex-review issues (PR #167, cef91ce5aa):

P1 — _build_preflight_runner seam: when _launch_one_issue is called with
     an installation_token, the runner it builds via _build_preflight_runner
     passes env=gh_env(installation_token) to subprocess.run so that
     ruleset gh api calls authenticate as the App, not ambient credentials.

P2a — issue visibility on preflight refusal: when preflight returns False,
     the issue's agent-ready label must be visible again after
     _launch_one_issue returns (either never removed, or restored) AND a
     blocking comment with "preflight refused" and the RulesetStatus reason
     must be posted to the issue.  agent-in-progress must NOT be left set.

See also test_alert_post.py for P2b (webhook URL secret not logged).

CodeRabbit correctness fixes on PR #253, round 2
-------------------------------------------------

Finding #5 (Stability, Major): the ``_runner`` closure
``_build_preflight_runner`` returns previously called ``subprocess.run``
with NO ``timeout=`` at all, so a stalled ``gh api`` ruleset call could
hang daemon launch indefinitely. The fix, pinned by the P3/P4 tests
below:

- P3: the runner passes a POSITIVE ``timeout=`` kwarg through to
  ``subprocess.run``.
- P4: a ``subprocess.TimeoutExpired`` surfaced while running the
  (diagnostic-only, #223-demoted) comparator must not propagate out of
  ``_should_launch_worker`` uncaught — it must degrade to a diagnostic
  error and the launch decision must still complete (fail-closed), never
  crash or hang. See also
  ``test_check_ruleset_signals_degrades_to_error_when_runner_times_out``
  in ``tests/test_ruleset_status.py``, which pins the same requirement
  one layer down at ``check_ruleset_signals`` itself.
"""

from __future__ import annotations

import asyncio
import logging
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


def _make_obs(
    tmp_path: Path,
    *,
    ping_url: str | None = _WEBHOOK,
) -> ObsConfig:
    """Build a minimal ObsConfig for preflight tests.

    Args:
        tmp_path: Pytest tmp_path fixture; used to generate required paths.
        ping_url: Value for ``heartbeat_ping_url``; use ``None`` to
            test the no-URL path.

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


def _fake_runner(args: list[str]) -> Any:  # noqa: ANN401
    """Stub runner (never called in these integration seam tests)."""
    raise AssertionError(
        "_fake_runner must not be called in preflight unit tests "
        "(check_ruleset_signals is mocked)"
    )


# ---------------------------------------------------------------------------
# Test 1 — MATCH → proceed (returns True; no alert)
# ---------------------------------------------------------------------------


def test_should_launch_worker_returns_true_on_match(
    tmp_path: Path,
) -> None:
    """_should_launch_worker returns True when check_ruleset_signals → MATCH.

    No Slack alert must be sent; the launch is not refused.

    Args:
        tmp_path: Pytest tmp_path fixture.
    """
    import baton_harness.chain.daemon as daemon_mod
    from baton_harness.chain.ruleset_status import (
        RulesetCheckResult,
        RulesetStatus,
    )

    obs = _make_obs(tmp_path)
    post_calls: list[tuple[str, str]] = []

    def _fake_post(url: str, message: str, **kwargs: Any) -> bool:  # noqa: ANN401
        post_calls.append((url, message))
        return True

    with (
        patch.object(
            daemon_mod,
            "check_ruleset_signals",
            return_value=RulesetCheckResult(status=RulesetStatus.MATCH),
        ),
        patch(
            "baton_harness.chain.daemon.post_slack_alert",
            side_effect=_fake_post,
        ),
    ):
        result = daemon_mod._should_launch_worker(  # type: ignore[attr-defined]
            _ISSUE,
            _OWNER,
            _REPO,
            app_id=_APP_ID,
            runner=_fake_runner,
            obs=obs,
        )

    assert result is True, "_should_launch_worker must return True on MATCH"
    assert not post_calls, (
        "No Slack alert must be sent when preflight passes (MATCH)"
    )


# ---------------------------------------------------------------------------
# Test 2 — DRIFT → refuse + alert carrying the result's .detail verbatim
# ---------------------------------------------------------------------------

_DRIFT_DETAIL = (
    "DRIFT (harness-feature-daemon-only): current_user_can_bypass differs "
    "— expected 'always', live 'never'"
)


def test_should_launch_worker_refuses_and_alerts_on_drift(
    tmp_path: Path,
) -> None:
    """_should_launch_worker returns False on DRIFT and POSTs a Slack alert.

    The alert body must contain:
    - ``"baton-harness refusing to launch worker"``
    - ``"Failed checks:"``
    - The exact ``.detail`` string from the ``RulesetCheckResult`` returned
      by ``check_ruleset_signals`` — proving the gate builds the alert from
      the single result it already has, rather than making a second
      ``check_ruleset_signals`` call to re-derive detail text.

    Args:
        tmp_path: Pytest tmp_path fixture.
    """
    import baton_harness.chain.daemon as daemon_mod
    from baton_harness.chain.ruleset_status import (
        RulesetCheckResult,
        RulesetStatus,
    )

    obs = _make_obs(tmp_path)
    post_calls: list[tuple[str, str]] = []

    def _fake_post(url: str, message: str, **kwargs: Any) -> bool:  # noqa: ANN401
        post_calls.append((url, message))
        return True

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
            daemon_mod,
            "check_ruleset_signals",
            side_effect=_fake_check,
        ),
        patch(
            "baton_harness.chain.daemon.post_slack_alert",
            side_effect=_fake_post,
        ),
    ):
        result = daemon_mod._should_launch_worker(  # type: ignore[attr-defined]
            _ISSUE,
            _OWNER,
            _REPO,
            app_id=_APP_ID,
            runner=_fake_runner,
            obs=obs,
        )

    assert result is False, "_should_launch_worker must return False on DRIFT"
    assert len(check_calls) == 1, (
        "check_ruleset_signals must be called exactly ONCE per preflight "
        f"decision (no second call to re-derive detail); got {check_calls!r}"
    )
    assert post_calls, "A Slack alert must be posted on DRIFT"
    url_posted, message_posted = post_calls[0]
    assert url_posted == _WEBHOOK, (
        f"Alert must POST to obs.heartbeat_ping_url {_WEBHOOK!r}; "
        f"got {url_posted!r}"
    )
    assert "baton-harness refusing to launch worker" in message_posted, (
        f"Alert body must contain the refusal phrase; got {message_posted!r}"
    )
    assert "Failed checks:" in message_posted, (
        f"Alert body must contain 'Failed checks:'; got {message_posted!r}"
    )
    assert _DRIFT_DETAIL in message_posted, (
        "Alert body must carry the RulesetCheckResult.detail text verbatim "
        f"(single-call contract); got {message_posted!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — ABSENT → refuse + alert mentioning missing ruleset(s)
# ---------------------------------------------------------------------------

_ABSENT_DETAIL = (
    "ruleset 'harness-feature-daemon-only' (id=456) not found (404)"
)


def test_should_launch_worker_refuses_and_alerts_on_absent(
    tmp_path: Path,
) -> None:
    """_should_launch_worker returns False on ABSENT and POSTs a Slack alert.

    The alert body must contain the refusal phrase, 'Failed checks:', and
    the result's ``.detail`` naming the missing ruleset.

    Args:
        tmp_path: Pytest tmp_path fixture.
    """
    import baton_harness.chain.daemon as daemon_mod
    from baton_harness.chain.ruleset_status import (
        RulesetCheckResult,
        RulesetStatus,
    )

    obs = _make_obs(tmp_path)
    post_calls: list[tuple[str, str]] = []

    def _fake_post(url: str, message: str, **kwargs: Any) -> bool:  # noqa: ANN401
        post_calls.append((url, message))
        return True

    with (
        patch.object(
            daemon_mod,
            "check_ruleset_signals",
            return_value=RulesetCheckResult(
                status=RulesetStatus.ABSENT, detail=_ABSENT_DETAIL
            ),
        ),
        patch(
            "baton_harness.chain.daemon.post_slack_alert",
            side_effect=_fake_post,
        ),
    ):
        result = daemon_mod._should_launch_worker(  # type: ignore[attr-defined]
            _ISSUE,
            _OWNER,
            _REPO,
            app_id=_APP_ID,
            runner=_fake_runner,
            obs=obs,
        )

    assert result is False, "_should_launch_worker must return False on ABSENT"
    assert post_calls, "A Slack alert must be posted on ABSENT"
    _, message_posted = post_calls[0]
    assert "baton-harness refusing to launch worker" in message_posted, (
        f"Alert body must contain the refusal phrase; got {message_posted!r}"
    )
    assert "Failed checks:" in message_posted, (
        f"Alert body must mention 'Failed checks:'; got {message_posted!r}"
    )
    assert _ABSENT_DETAIL in message_posted, (
        "Alert body must carry the RulesetCheckResult.detail text naming "
        f"the missing ruleset; got {message_posted!r}"
    )


# ---------------------------------------------------------------------------
# Test 4 — ERROR → refuse + alert (fail-closed)
# ---------------------------------------------------------------------------

_ERROR_DETAIL = (
    "ruleset 'harness-main-no-merge' (id=123) GET failed (http=500)"
)


def test_should_launch_worker_refuses_and_alerts_on_error_fail_closed(
    tmp_path: Path,
) -> None:
    """_should_launch_worker returns False on ERROR (fail-closed contract).

    Even when the ruleset check itself fails with an error (e.g. network
    outage), the daemon must refuse to launch rather than proceeding
    without branch-protection.  The Slack alert must still be attempted
    and carry the result's ``.detail``.

    Args:
        tmp_path: Pytest tmp_path fixture.
    """
    import baton_harness.chain.daemon as daemon_mod
    from baton_harness.chain.ruleset_status import (
        RulesetCheckResult,
        RulesetStatus,
    )

    obs = _make_obs(tmp_path)
    post_calls: list[tuple[str, str]] = []

    def _fake_post(url: str, message: str, **kwargs: Any) -> bool:  # noqa: ANN401
        post_calls.append((url, message))
        return True

    with (
        patch.object(
            daemon_mod,
            "check_ruleset_signals",
            return_value=RulesetCheckResult(
                status=RulesetStatus.ERROR, detail=_ERROR_DETAIL
            ),
        ),
        patch(
            "baton_harness.chain.daemon.post_slack_alert",
            side_effect=_fake_post,
        ),
    ):
        result = daemon_mod._should_launch_worker(  # type: ignore[attr-defined]
            _ISSUE,
            _OWNER,
            _REPO,
            app_id=_APP_ID,
            runner=_fake_runner,
            obs=obs,
        )

    assert result is False, (
        "_should_launch_worker must return False on ERROR (fail-closed)"
    )
    assert post_calls, "A Slack alert must be attempted on ERROR"
    _, message_posted = post_calls[0]
    assert "baton-harness refusing to launch worker" in message_posted, (
        f"Alert body must contain the refusal phrase; got {message_posted!r}"
    )
    assert _ERROR_DETAIL in message_posted, (
        "Alert body must carry the RulesetCheckResult.detail text for the "
        f"error path; got {message_posted!r}"
    )


# ---------------------------------------------------------------------------
# Test 4b — NOT_PROVISIONED → refuse + alert naming the fix (fail-closed,
# strict — a missing baseline must park, distinct from DRIFT)
# ---------------------------------------------------------------------------

_NOT_PROVISIONED_DETAIL = (
    "no ruleset baseline pinned for glitchwerks/baton-harness at "
    ".bh/ruleset-baseline.json; run bin/provision-ruleset.sh first"
)


def test_should_launch_worker_refuses_and_alerts_on_not_provisioned(
    tmp_path: Path,
) -> None:
    """_should_launch_worker returns False on NOT_PROVISIONED (fail-closed).

    NOT_PROVISIONED means no ruleset baseline has ever been pinned for
    this repo — ``check_ruleset_signals`` cannot safely assert "no drift"
    with nothing to compare against, so the gate must refuse to launch
    (park) rather than treat an un-pinned baseline as safe-by-default.

    The alert must:
    - Contain the refusal phrase and ``"Failed checks:"``.
    - Name the fix — carry ``"bin/provision-ruleset.sh"`` (or the result's
      ``.detail``, which already names it) so an operator knows exactly
      what to run.
    - Be textually distinct from the DRIFT alert body (this is "never
      provisioned", not "drifted from a known-good state" — the two must
      not read the same to an operator).

    Args:
        tmp_path: Pytest tmp_path fixture.
    """
    import baton_harness.chain.daemon as daemon_mod
    from baton_harness.chain.ruleset_status import (
        RulesetCheckResult,
        RulesetStatus,
    )

    obs = _make_obs(tmp_path)
    post_calls: list[tuple[str, str]] = []

    def _fake_post(url: str, message: str, **kwargs: Any) -> bool:  # noqa: ANN401
        post_calls.append((url, message))
        return True

    with (
        patch.object(
            daemon_mod,
            "check_ruleset_signals",
            return_value=RulesetCheckResult(
                status=RulesetStatus.NOT_PROVISIONED,
                detail=_NOT_PROVISIONED_DETAIL,
            ),
        ),
        patch(
            "baton_harness.chain.daemon.post_slack_alert",
            side_effect=_fake_post,
        ),
    ):
        result = daemon_mod._should_launch_worker(  # type: ignore[attr-defined]
            _ISSUE,
            _OWNER,
            _REPO,
            app_id=_APP_ID,
            runner=_fake_runner,
            obs=obs,
        )

    assert result is False, (
        "_should_launch_worker must return False on NOT_PROVISIONED — a "
        "missing baseline must park the worker, never launch it "
        "(strict fail-closed)"
    )
    assert post_calls, "A Slack alert must be posted on NOT_PROVISIONED"
    _, message_posted = post_calls[0]
    assert "baton-harness refusing to launch worker" in message_posted, (
        f"Alert body must contain the refusal phrase; got {message_posted!r}"
    )
    assert "Failed checks:" in message_posted, (
        f"Alert body must mention 'Failed checks:'; got {message_posted!r}"
    )
    assert "bin/provision-ruleset.sh" in message_posted, (
        "Alert body must name the fix (bin/provision-ruleset.sh) so an "
        f"operator knows what to run; got {message_posted!r}"
    )
    # Distinctness from DRIFT: the NOT_PROVISIONED message must not read
    # as a DRIFT message. DRIFT detail text is keyed on a structural/
    # bypass/updated_at signal comparison (see _DRIFT_DETAIL and
    # _ruleset_signal_drift); none of that vocabulary belongs in a
    # "never provisioned" message, and the DRIFT detail string used
    # elsewhere in this module must not appear here.
    assert _DRIFT_DETAIL not in message_posted, (
        "NOT_PROVISIONED alert must not contain DRIFT detail text; got "
        f"{message_posted!r}"
    )
    assert "current_user_can_bypass" not in message_posted, (
        "NOT_PROVISIONED alert must be distinct from a DRIFT alert — it "
        "must not carry DRIFT's signal-comparison vocabulary; got "
        f"{message_posted!r}"
    )


# ---------------------------------------------------------------------------
# Test 5 — Alert POST failure does NOT crash the launch decision
# ---------------------------------------------------------------------------


def test_alert_post_failure_does_not_crash_launch_decision(
    tmp_path: Path,
) -> None:
    """Launch refusal is clean even when post_slack_alert raises.

    post_slack_alert's own fire-and-forget contract guarantees it does not
    raise, but the daemon seam must also be resilient in case the helper
    contract is violated.  The launch refusal must complete without raising.

    Args:
        tmp_path: Pytest tmp_path fixture.
    """
    import baton_harness.chain.daemon as daemon_mod
    from baton_harness.chain.ruleset_status import (
        RulesetCheckResult,
        RulesetStatus,
    )

    obs = _make_obs(tmp_path)

    with (
        patch.object(
            daemon_mod,
            "check_ruleset_signals",
            return_value=RulesetCheckResult(
                status=RulesetStatus.DRIFT, detail=_DRIFT_DETAIL
            ),
        ),
        patch(
            "baton_harness.chain.daemon.post_slack_alert",
            side_effect=RuntimeError("post helper itself raised"),
        ),
    ):
        # Must NOT raise — the daemon must continue cleanly.
        result = daemon_mod._should_launch_worker(  # type: ignore[attr-defined]
            _ISSUE,
            _OWNER,
            _REPO,
            app_id=_APP_ID,
            runner=_fake_runner,
            obs=obs,
        )

    assert result is False, (
        "_should_launch_worker must return False on DRIFT even if "
        "post_slack_alert raises"
    )


# ---------------------------------------------------------------------------
# Test 6 — No heartbeat_ping_url → no POST; warn logged; still refuses
# ---------------------------------------------------------------------------


def test_no_ping_url_configured_skips_post_but_still_refuses(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When obs.heartbeat_ping_url is None, no POST is attempted.

    The launch refusal must still fire (returns False for DRIFT).  A
    warning must be logged so operators know no Slack alert was sent.

    Args:
        tmp_path: Pytest tmp_path fixture.
        caplog: Pytest log-capture fixture.
    """
    import baton_harness.chain.daemon as daemon_mod
    from baton_harness.chain.ruleset_status import (
        RulesetCheckResult,
        RulesetStatus,
    )

    obs = _make_obs(tmp_path, ping_url=None)  # no webhook configured
    post_calls: list[Any] = []

    def _fake_post(url: str, message: str, **kwargs: Any) -> bool:  # noqa: ANN401
        post_calls.append((url, message))
        return True

    with (
        patch.object(
            daemon_mod,
            "check_ruleset_signals",
            return_value=RulesetCheckResult(
                status=RulesetStatus.DRIFT, detail=_DRIFT_DETAIL
            ),
        ),
        patch(
            "baton_harness.chain.daemon.post_slack_alert",
            side_effect=_fake_post,
        ),
        caplog.at_level(logging.WARNING),
    ):
        result = daemon_mod._should_launch_worker(  # type: ignore[attr-defined]
            _ISSUE,
            _OWNER,
            _REPO,
            app_id=_APP_ID,
            runner=_fake_runner,
            obs=obs,
        )

    assert result is False, (
        "Launch must still be refused when no ping URL is configured"
    )
    assert not post_calls, (
        "post_slack_alert must NOT be called when "
        "obs.heartbeat_ping_url is None"
    )
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "A WARNING must be logged when a preflight alert cannot be sent "
        "because no heartbeat_ping_url is configured; "
        f"records: {[r.message for r in caplog.records]!r}"
    )


# ---------------------------------------------------------------------------
# Regression guard — the gate never calls ruleset_is_provisioned (#206 hard
# swap). ``ruleset_is_provisioned`` stays in ruleset_status.py for the
# provisioning-side verifier, but the daemon gate must exclusively consult
# ``check_ruleset_signals``. Patching both and asserting the old symbol is
# untouched catches an accidental re-introduction of the old call site.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        "MATCH",
        "DRIFT",
        "ABSENT",
        "ERROR",
        "NOT_PROVISIONED",
    ],
)
def test_should_launch_worker_never_calls_ruleset_is_provisioned(
    tmp_path: Path,
    status: str,
) -> None:
    """_should_launch_worker must not call ruleset_is_provisioned at all.

    ``ruleset_is_provisioned`` is patched to raise on any call, for every
    ``RulesetStatus`` value ``check_ruleset_signals`` can report — proving
    the gate's decision is driven solely by ``check_ruleset_signals``
    across the whole status space, not just for the MATCH happy path.

    Args:
        tmp_path: Pytest tmp_path fixture.
        status: The ``RulesetStatus`` name to drive through
            ``check_ruleset_signals`` for this parametrized run.
    """
    import baton_harness.chain.daemon as daemon_mod
    from baton_harness.chain.ruleset_status import (
        RulesetCheckResult,
        RulesetStatus,
    )

    obs = _make_obs(tmp_path)

    def _forbidden_ruleset_is_provisioned(
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> RulesetStatus:
        raise AssertionError(
            "_should_launch_worker must not call ruleset_is_provisioned — "
            "the #206 hard swap replaces it with check_ruleset_signals"
        )

    with (
        patch.object(
            daemon_mod,
            "check_ruleset_signals",
            return_value=RulesetCheckResult(
                status=RulesetStatus[status], detail="detail text"
            ),
        ),
        patch.object(
            daemon_mod,
            "ruleset_is_provisioned",
            side_effect=_forbidden_ruleset_is_provisioned,
        ) as mock_legacy,
        patch(
            "baton_harness.chain.daemon.post_slack_alert",
            return_value=True,
        ),
    ):
        daemon_mod._should_launch_worker(  # type: ignore[attr-defined]
            _ISSUE,
            _OWNER,
            _REPO,
            app_id=_APP_ID,
            runner=_fake_runner,
            obs=obs,
        )

    mock_legacy.assert_not_called()


# ---------------------------------------------------------------------------
# Test 7 — launch loop calls _should_launch_worker BEFORE _run_worker
# ---------------------------------------------------------------------------


def test_daemon_launch_loop_calls_should_launch_worker_before_run_worker(
    tmp_path: Path,
) -> None:
    """Launch loop consults _should_launch_worker before _run_worker.

    Code-writer must extract a module-level async helper with the updated
    signature (extended by P1 — installation_token parameter)::

        async def _launch_one_issue(
            orch: Orchestrator,
            issue_obj: object,
            owner: str,
            repo: str,
            app_id: str,
            installation_token: str,
            runner: Callable[[list[str]], subprocess.CompletedProcess[str]],
            obs: ObsConfig,
        ) -> str | None

    **Signature change (P1):** ``installation_token`` is now threaded
    through so the helper can call ``_build_preflight_runner(token)``
    instead of using the ambient ``_default_gh_runner``.

    The helper's contract:

    1. ``runner = _build_preflight_runner(installation_token)``
    2. ``preflight = _should_launch_worker(issue_number, owner, repo,
       app_id=app_id, runner=runner, obs=obs)``
    3. If not preflight: skip ``_run_worker``; return ``None``.
    4. Otherwise: ``return await orch._run_worker(issue_obj)``

    The existing dispatch loop near L1373 of daemon.py calls
    ``_launch_one_issue`` in place of the bare ``await
    orch._run_worker(issue_obj)``.  ``_run_work_unit`` and its callers
    must thread ``app_id`` (from ``RepoConfig.app_id`` or
    ``config.app_id``), ``installation_token``, and ``obs`` down to
    ``_launch_one_issue``.

    This test patches both ``_should_launch_worker`` and
    ``Orchestrator._run_worker`` at module scope, then drives
    ``_launch_one_issue`` directly via ``asyncio.run()``.  The test
    asserts:

    - ``_should_launch_worker`` was called with positional args
      ``(issue_number, owner, repo)`` and keyword args
      ``app_id=_APP_ID``.
    - ``_run_worker`` was called exactly once (after the preflight).
    - Return value threads through from ``_run_worker``.

    Args:
        tmp_path: Pytest tmp_path fixture.
    """
    from unittest.mock import AsyncMock, MagicMock

    import baton_harness.chain.daemon as daemon_mod

    obs = _make_obs(tmp_path)

    # Build a minimal mock Orchestrator with an async _run_worker.
    mock_orch = MagicMock()
    mock_orch._run_worker = AsyncMock(return_value="pr_created")

    # issue_obj only needs to carry .number for the preflight call.
    mock_issue = MagicMock()
    mock_issue.number = _ISSUE

    with (
        patch.object(
            daemon_mod,
            "_should_launch_worker",
            return_value=True,
        ) as mock_preflight,
        patch.object(
            mock_orch,
            "_run_worker",
            new=mock_orch._run_worker,
        ),
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

    # Preflight must have been called with the right positional args.
    mock_preflight.assert_called_once()
    preflight_args, preflight_kwargs = mock_preflight.call_args
    assert preflight_args == (_ISSUE, _OWNER, _REPO), (
        f"_should_launch_worker must receive (issue_number, owner, repo); "
        f"got positional args {preflight_args!r}"
    )
    assert preflight_kwargs.get("app_id") == _APP_ID, (
        f"_should_launch_worker must receive app_id={_APP_ID!r}; "
        f"got {preflight_kwargs!r}"
    )

    # _run_worker must have been called once (launch allowed).
    mock_orch._run_worker.assert_called_once_with(mock_issue)

    # Return value threads through from _run_worker.
    assert result == "pr_created", (
        f"_launch_one_issue must return the _run_worker result; got {result!r}"
    )


# ---------------------------------------------------------------------------
# Test 8 — launch loop skips _run_worker when preflight refuses
# ---------------------------------------------------------------------------


def test_daemon_launch_loop_skips_run_worker_when_preflight_refuses(
    tmp_path: Path,
) -> None:
    """Launch loop skips _run_worker and returns None when preflight refuses.

    When ``_should_launch_worker`` returns ``False``, ``_launch_one_issue``
    must:

    - NOT call ``orch._run_worker``.
    - Return ``None`` (signals refused/parked to the calling loop).
    - Not raise — control must return cleanly to the caller.

    The calling loop in ``_run_work_unit`` is responsible for parking the
    issue (``sched.mark_parked(n)`` + ``parked_reasons[n] = "preflight
    refused"``) and continuing to the next issue.  ``_launch_one_issue``
    itself only returns ``None``; it does not touch ``sched``.

    See the seam contract in
    ``test_daemon_launch_loop_calls_should_launch_worker_before_run_worker``
    for the full helper signature (including the ``installation_token``
    parameter added by P1) and threading requirements.

    Args:
        tmp_path: Pytest tmp_path fixture.
    """
    from unittest.mock import AsyncMock, MagicMock

    import baton_harness.chain.daemon as daemon_mod

    obs = _make_obs(tmp_path)

    mock_orch = MagicMock()
    mock_orch._run_worker = AsyncMock(return_value="pr_created")

    mock_issue = MagicMock()
    mock_issue.number = _ISSUE

    with (
        patch.object(
            daemon_mod,
            "_should_launch_worker",
            return_value=False,
        ),
        patch.object(
            mock_orch,
            "_run_worker",
            new=mock_orch._run_worker,
        ),
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

    # _run_worker must NOT have been called.
    mock_orch._run_worker.assert_not_called()

    # _launch_one_issue must return None (refused / skip signal to caller).
    assert result is None, (
        "_launch_one_issue must return None when preflight refuses; "
        f"got {result!r}"
    )


# ---------------------------------------------------------------------------
# P1 — _build_preflight_runner seam uses installation token in subprocess env
# ---------------------------------------------------------------------------


def test_build_preflight_runner_injects_gh_token_into_subprocess_env(
    tmp_path: Path,
) -> None:
    """_build_preflight_runner(token) produces a runner that passes token env.

    Code-writer must add a module-level factory at ``chain/daemon.py``::

        def _build_preflight_runner(
            installation_token: str,
        ) -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
            ...

    The returned callable, when invoked with a list of args, must call
    ``subprocess.run`` with ``env=`` containing at least
    ``GH_TOKEN=installation_token``.  This is obtained via
    ``chain.app_auth.gh_env(installation_token)``.

    Without this fix, the bare ``_default_gh_runner`` passes no env
    override, so ruleset ``gh api`` calls authenticate as nobody / the
    wrong user in deployments without an ambient GH_TOKEN, and every
    worker launch is refused with ``ERROR``.

    Test mechanism: patch ``chain.daemon.subprocess.run`` and assert that
    the ``env`` kwarg passed to it contains ``GH_TOKEN=_TOKEN``.  The
    runner factory is driven directly (not through _launch_one_issue) so
    the seam is pinned independently.

    Args:
        tmp_path: Pytest tmp_path fixture (unused; kept for fixture arity
            consistency across this module).
    """
    import subprocess as _subprocess

    import baton_harness.chain.daemon as daemon_mod

    captured_env: list[dict[str, str] | None] = []

    def _spy_run(
        args: list[str],
        **kwargs: Any,  # noqa: ANN401
    ) -> _subprocess.CompletedProcess[str]:
        captured_env.append(kwargs.get("env"))
        return _subprocess.CompletedProcess(
            args=args, returncode=0, stdout="", stderr=""
        )

    # _build_preflight_runner must exist at module scope in daemon.py.
    runner_factory = getattr(
        daemon_mod,
        "_build_preflight_runner",
        None,
    )
    assert runner_factory is not None, (
        "_build_preflight_runner must be defined at module scope in "
        "chain/daemon.py (P1 seam)"
    )

    runner = runner_factory(_TOKEN)

    with patch(
        "baton_harness.chain.daemon.subprocess.run", side_effect=_spy_run
    ):
        runner(["gh", "api", "repos/o/r/rulesets"])

    assert captured_env, "subprocess.run must be called by the runner"
    env_used = captured_env[0]
    assert env_used is not None, (
        "runner must pass env= to subprocess.run (not None); "
        "without an env override, ambient GH_TOKEN is used instead of the "
        "App installation token"
    )
    assert env_used.get("GH_TOKEN") == _TOKEN, (
        f"env['GH_TOKEN'] must equal the installation token {_TOKEN!r}; "
        f"got {env_used.get('GH_TOKEN')!r}.  "
        "Use chain.app_auth.gh_env(installation_token) to build the env."
    )


def test_launch_one_issue_uses_build_preflight_runner_not_default_runner(
    tmp_path: Path,
) -> None:
    """_launch_one_issue builds its runner via _build_preflight_runner(token).

    When _launch_one_issue is called with an installation_token, it must
    call _build_preflight_runner(installation_token) to obtain the runner
    it passes to _should_launch_worker — NOT the bare _default_gh_runner.

    This pins the call-site wiring: even if _build_preflight_runner exists,
    the fix is only effective if _launch_one_issue actually calls it.

    Mechanism: patch _build_preflight_runner to return a sentinel callable
    and assert that _should_launch_worker is called with that sentinel as
    its runner kwarg.

    Args:
        tmp_path: Pytest tmp_path fixture.
    """
    from unittest.mock import AsyncMock, MagicMock

    import baton_harness.chain.daemon as daemon_mod

    obs = _make_obs(tmp_path)

    mock_orch = MagicMock()
    mock_orch._run_worker = AsyncMock(return_value="pr_created")

    mock_issue = MagicMock()
    mock_issue.number = _ISSUE

    sentinel_runner = MagicMock(name="sentinel_runner")
    captured_runner: list[Any] = []

    def _capture_preflight(
        issue_number: int,
        owner: str,
        repo: str,
        *,
        app_id: str,
        runner: Any,  # noqa: ANN401
        obs: Any,  # noqa: ANN401
    ) -> bool:
        captured_runner.append(runner)
        return True  # allow launch so _run_worker is reachable

    with (
        patch.object(
            daemon_mod,
            "_build_preflight_runner",
            return_value=sentinel_runner,
        ) as mock_factory,
        patch.object(
            daemon_mod,
            "_should_launch_worker",
            side_effect=_capture_preflight,
        ),
        patch.object(
            mock_orch,
            "_run_worker",
            new=mock_orch._run_worker,
        ),
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

    # _build_preflight_runner must have been called with the token.
    mock_factory.assert_called_once_with(_TOKEN)

    # _should_launch_worker must have received the sentinel runner.
    assert captured_runner, "_should_launch_worker was not called"
    assert captured_runner[0] is sentinel_runner, (
        "_launch_one_issue must pass the runner from "
        "_build_preflight_runner(_TOKEN) to _should_launch_worker, "
        f"not _default_gh_runner; got {captured_runner[0]!r}"
    )


# ---------------------------------------------------------------------------
# P2a — issue visibility restored on preflight refusal
# ---------------------------------------------------------------------------


def test_preflight_refusal_restores_agent_ready_label(
    tmp_path: Path,
) -> None:
    """agent-ready must be visible after preflight refuses the launch.

    When _should_launch_worker returns False (DRIFT / ABSENT / ERROR), the
    issue must still carry ``agent-ready`` so future polls can pick it up
    (the protection might be restored later).

    Two acceptable implementations:
    1. Run preflight BEFORE the agent-ready → agent-in-progress transition
       (preferred — label is never removed).
    2. Run preflight after the transition and then restore agent-ready on
       refusal.

    This test drives ``_launch_one_issue`` and patches the label-edit
    primitive (``baton_harness.chain.daemon._label_edit``) to record calls.
    It then asserts that no net removal of agent-ready occurs: either
    agent-ready is never touched, OR a subsequent add=["agent-ready"] call
    is made before the function returns.

    NOTE: ``_launch_one_issue`` owns only the preflight + dispatch step.
    The label-transition and park logic live in the surrounding
    ``_run_work_unit`` loop.  If the code-writer chooses option 1 (run
    preflight first), ``_launch_one_issue`` may return None without ever
    touching labels — and the loop must not remove agent-ready before it
    consults preflight.  This test pins the seam on ``_launch_one_issue``
    itself; a separate integration-level test may be needed for the full
    loop path.  Code-writer may satisfy this assertion by having
    ``_launch_one_issue`` call ``_label_edit(add=['agent-ready'])`` on
    None return (option 2), or by never removing it in the first place
    (option 1 — in that case this test passes trivially).

    Args:
        tmp_path: Pytest tmp_path fixture.
    """
    from unittest.mock import AsyncMock, MagicMock

    import baton_harness.chain.daemon as daemon_mod

    obs = _make_obs(tmp_path)

    mock_orch = MagicMock()
    mock_orch._run_worker = AsyncMock(return_value="pr_created")

    mock_issue = MagicMock()
    mock_issue.number = _ISSUE

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

    with (
        patch.object(
            daemon_mod,
            "_should_launch_worker",
            return_value=False,
        ),
        patch.object(
            daemon_mod,
            "_label_edit",
            side_effect=_record_label_edit,
        ),
        patch.object(
            mock_orch,
            "_run_worker",
            new=mock_orch._run_worker,
        ),
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

    assert result is None, "Preflight refusal must return None"

    # Compute net label state for agent-ready.
    net_removed = sum(
        1 for c in label_edit_calls if "agent-ready" in c["remove"]
    )
    net_added = sum(1 for c in label_edit_calls if "agent-ready" in c["add"])
    # Either agent-ready was never touched (net_removed == 0), OR it was
    # removed and then restored (net_added >= net_removed).
    assert net_removed == 0 or net_added >= net_removed, (
        "agent-ready must not be net-removed on preflight refusal; "
        f"label_edit calls: {label_edit_calls!r}.  "
        "Restore it with add=['agent-ready'] or run preflight before the "
        "label transition."
    )

    # agent-in-progress must NOT be left set.
    net_ip_removed = sum(
        1 for c in label_edit_calls if "agent-in-progress" in c["remove"]
    )
    net_ip_added = sum(
        1 for c in label_edit_calls if "agent-in-progress" in c["add"]
    )
    # If agent-in-progress was ever added, it must also be removed.
    assert net_ip_added == 0 or net_ip_removed >= net_ip_added, (
        "agent-in-progress must be cleared on preflight refusal; "
        f"label_edit calls: {label_edit_calls!r}"
    )


def test_preflight_refusal_posts_blocking_comment_with_reason(
    tmp_path: Path,
) -> None:
    """A blocking comment is posted to the issue on preflight refusal.

    When _should_launch_worker returns False, _launch_one_issue must post
    a comment to the GitHub issue containing:
    - The phrase ``"preflight refused"``
    - The RulesetStatus reason (surfaced by _should_launch_worker's return
      value or an out-param — code-writer's choice of mechanism).

    The comment machinery is pinned by patching
    ``baton_harness.chain.daemon.escalate`` (the existing blocking-comment
    primitive used elsewhere in daemon.py) OR any equivalent comment-post
    call the code-writer chooses.  The test asserts at least one call with
    a body containing ``"preflight refused"``.

    Alternative satisfaction: if the code-writer threads the RulesetStatus
    through _should_launch_worker's return value (e.g. returns the status
    object instead of bool, or raises a typed exception), the test accepts
    any comment containing ``"preflight refused"``.

    The daemon imports ``alert`` from ``baton_harness.chain.escalation``
    (as ``from baton_harness.chain.escalation import alert``).  The test
    patches ``baton_harness.chain.daemon.alert`` — the name as bound in
    the daemon module's namespace — so any call site in _launch_one_issue
    that uses the locally-bound ``alert(...)`` is captured.

    Args:
        tmp_path: Pytest tmp_path fixture.
    """
    from unittest.mock import AsyncMock, MagicMock

    import baton_harness.chain.daemon as daemon_mod

    obs = _make_obs(tmp_path)

    mock_orch = MagicMock()
    mock_orch._run_worker = AsyncMock(return_value="pr_created")

    mock_issue = MagicMock()
    mock_issue.number = _ISSUE

    comment_calls: list[str] = []  # bodies of comments posted

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
            "_should_launch_worker",
            return_value=False,
        ),
        patch.object(
            daemon_mod,
            "alert",
            side_effect=_record_alert,
        ),
        patch.object(
            mock_orch,
            "_run_worker",
            new=mock_orch._run_worker,
        ),
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

    assert result is None, "Preflight refusal must return None"
    assert comment_calls, (
        "A blocking comment must be posted to the issue when preflight "
        "refuses.  Call alert(owner, repo, issue, <msg>) from "
        "_launch_one_issue where <msg> contains 'preflight refused'."
    )
    assert any("preflight refused" in body for body in comment_calls), (
        "At least one alert/comment must contain 'preflight refused'; "
        f"got: {comment_calls!r}"
    )


# ---------------------------------------------------------------------------
# P3 — CodeRabbit PR #253 round 2, finding #5: the comparator runner built
# by _build_preflight_runner must bound its subprocess.run call with a
# positive timeout= so a stalled `gh api` call cannot hang daemon launch
# indefinitely.
# ---------------------------------------------------------------------------


def test_build_preflight_runner_passes_positive_timeout_to_subprocess_run(
    tmp_path: Path,
) -> None:
    """The runner _build_preflight_runner returns must bound its gh call.

    CodeRabbit finding #5 (PR #253 round 2): ``_build_preflight_runner``'s
    ``_runner`` closure previously called ``subprocess.run(...)`` with no
    ``timeout=`` at all, so a stalled ``gh api`` ruleset call could hang
    daemon launch forever. This pins that the runner now passes a
    POSITIVE numeric ``timeout=`` kwarg through to ``subprocess.run``.

    Test mechanism mirrors
    ``test_build_preflight_runner_injects_gh_token_into_subprocess_env``
    above (P1): patch ``chain.daemon.subprocess.run`` and assert on the
    captured kwargs, driving the runner factory directly rather than
    through ``_launch_one_issue``.

    Args:
        tmp_path: Pytest tmp_path fixture (unused; kept for fixture arity
            consistency across this module).
    """
    import subprocess as _subprocess

    import baton_harness.chain.daemon as daemon_mod

    captured_kwargs: list[dict[str, Any]] = []

    def _spy_run(
        args: list[str],
        **kwargs: Any,  # noqa: ANN401
    ) -> _subprocess.CompletedProcess[str]:
        captured_kwargs.append(kwargs)
        return _subprocess.CompletedProcess(
            args=args, returncode=0, stdout="", stderr=""
        )

    runner_factory = getattr(daemon_mod, "_build_preflight_runner", None)
    assert runner_factory is not None, (
        "_build_preflight_runner must be defined at module scope in "
        "chain/daemon.py (P1 seam)"
    )

    runner = runner_factory(_TOKEN)

    with patch(
        "baton_harness.chain.daemon.subprocess.run", side_effect=_spy_run
    ):
        runner(["gh", "api", "repos/o/r/rulesets"])

    assert captured_kwargs, "subprocess.run must be called by the runner"
    timeout_value = captured_kwargs[0].get("timeout")
    assert (
        isinstance(timeout_value, (int, float))
        and not isinstance(timeout_value, bool)
        and timeout_value > 0
    ), (
        "the comparator runner must pass a POSITIVE `timeout=` kwarg to "
        "subprocess.run so a stalled `gh api` call cannot hang daemon "
        f"launch indefinitely; got kwargs={captured_kwargs[0]!r}"
    )


# ---------------------------------------------------------------------------
# P4 — CodeRabbit PR #253 round 2, finding #5: a subprocess.TimeoutExpired
# raised while running the (diagnostic-only) comparator must degrade to a
# diagnostic error result rather than propagating out of the launch
# decision -- the comparator's own failure must never crash/hang launch.
# ---------------------------------------------------------------------------


def test_should_launch_worker_fails_closed_when_comparator_raises_timeout(
    tmp_path: Path,
) -> None:
    """A TimeoutExpired from the comparator must not crash launch decision.

    CodeRabbit finding #5 (PR #253 round 2): even with a timeout bound
    on the runner (P3 above), a resulting ``subprocess.TimeoutExpired``
    must not propagate out of ``_should_launch_worker`` uncaught — the
    comparator is diagnostic-only (#223 demotion), so its own failure
    must degrade to an error-detail result and the launch decision must
    still complete (fail-closed, exactly like the existing ERROR-status
    path pinned by ``test_should_launch_worker_refuses_and_alerts_on_
    error_fail_closed`` above).

    Args:
        tmp_path: Pytest tmp_path fixture.
    """
    import subprocess as _subprocess

    import baton_harness.chain.daemon as daemon_mod

    obs = _make_obs(tmp_path)
    post_calls: list[tuple[str, str]] = []

    def _fake_post(url: str, message: str, **kwargs: Any) -> bool:  # noqa: ANN401
        post_calls.append((url, message))
        return True

    def _raising_check(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise _subprocess.TimeoutExpired(cmd=["gh", "api"], timeout=30)

    with (
        patch.object(
            daemon_mod, "check_ruleset_signals", side_effect=_raising_check
        ),
        patch(
            "baton_harness.chain.daemon.post_slack_alert",
            side_effect=_fake_post,
        ),
    ):
        # Must NOT raise -- a stalled comparator call must degrade, not
        # crash or hang the launch decision.
        result = daemon_mod._should_launch_worker(  # type: ignore[attr-defined]
            _ISSUE,
            _OWNER,
            _REPO,
            app_id=_APP_ID,
            runner=_fake_runner,
            obs=obs,
        )

    assert result is False, (
        "a comparator that times out is unproven -- _should_launch_worker "
        f"must fail closed (refuse), not raise or treat it as safe; got "
        f"{result!r}"
    )
    assert post_calls, (
        "A Slack alert must still be attempted when the comparator "
        "degrades to an error (a diagnostic-only failure must not "
        "silently swallow the refusal notification)"
    )
