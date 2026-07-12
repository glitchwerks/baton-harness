"""Root-level conftest: autouse fixtures that apply to every test module.

The branch-protection preflight gate (``_should_launch_worker``) was wired
into ``_run_work_unit`` as part of issue #144.  Two module-level helpers
gate every worker dispatch:

* ``_resolve_app_id()`` — reads ``BH_GITHUB_APP_ID`` from the environment.
  Integration tests that do not set this env var would hit fail-closed
  parking before any worker is dispatched.

* Inside ``_should_launch_worker``, ``check_ruleset_signals`` (the #206
  App-token-safe replacement for ``ruleset_is_provisioned`` — the daemon
  gate's only caller of either symbol) makes live ``gh api`` calls.
  Integration tests that do not mock the ruleset layer would hit
  ``ERROR`` (or ``NOT_PROVISIONED``, absent a pinned baseline) and park
  every issue.

The three autouse fixtures below provide safe defaults so that
integration-level daemon tests (``test_daemon.py``, etc.) remain
unaffected by the new gate layer.

Preflight-specific unit tests (``test_daemon_preflight.py``,
``test_daemon_push_probe.py``) patch these symbols inside each
individual test body via ``patch.object``; those inner patches take
precedence and these autouse fixtures have no effect on them.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

# Sentinel app-id used by the autouse resolve patch below.  Not a real
# GitHub App ID; used only so _run_work_unit does not park on missing env var.
_TEST_APP_ID = "0"


@pytest.fixture(autouse=True)
def _auto_patch_resolve_app_id() -> None:  # type: ignore[return]
    """Return a sentinel app-id so _run_work_unit does not park on missing env.

    ``_resolve_app_id()`` reads ``BH_GITHUB_APP_ID`` from the process
    environment.  Integration tests do not set this variable; without this
    autouse the daemon would log CRITICAL and park every issue before
    dispatching a worker.

    Preflight tests 7–8 call ``_launch_one_issue`` directly, passing
    ``_APP_ID`` explicitly — they bypass ``_resolve_app_id`` entirely, so
    this autouse has no effect on them.  Any test that explicitly patches
    ``_resolve_app_id`` overrides this via the innermost-patch rule.
    """
    with patch(
        "baton_harness.chain.daemon._resolve_app_id",
        return_value=_TEST_APP_ID,
    ):
        yield


@pytest.fixture(autouse=True)
def _auto_patch_ruleset_check_daemon() -> None:  # type: ignore[return]
    """Return MATCH for tests that do not exercise the ruleset check.

    ``check_ruleset_signals`` (imported into ``daemon`` module scope) is the
    #206 hard-swap replacement for ``ruleset_is_provisioned`` at the daemon
    gate's call site — ``ruleset_is_provisioned`` has no remaining caller in
    ``daemon.py`` (it stays in ``ruleset_status.py`` for the provisioning-
    side verifier only), so patching it here would no longer intercept
    anything.  ``check_ruleset_signals`` makes live ``gh api`` calls and
    also reads a pinned baseline file; integration tests do not mock the
    ruleset layer or provide a baseline, so without this autouse the daemon
    would detect ``ERROR``/``NOT_PROVISIONED`` and park every issue before
    dispatching a worker.

    Preflight tests use their own explicit
    ``patch.object(daemon_mod, "check_ruleset_signals", …)`` inside each
    test body; those inner patches take precedence over this autouse and
    the tests call the REAL ``_should_launch_worker`` function directly
    (not the autouse mock), so the correct per-test status flows through.
    """
    from baton_harness.chain.ruleset_status import (
        RulesetCheckResult,
        RulesetStatus,
    )

    with patch(
        "baton_harness.chain.daemon.check_ruleset_signals",
        return_value=RulesetCheckResult(status=RulesetStatus.MATCH),
    ):
        yield


@pytest.fixture(autouse=True)
def _auto_patch_push_probe_daemon(
    request: pytest.FixtureRequest,
) -> None:  # type: ignore[return]
    """Return a DENIED (safe) probe result for tests that don't exercise it.

    ``_probe_worker_push_denied`` (issue #223 decisive behavioral gate,
    demoted ``check_ruleset_signals`` to diagnostic-only) attempts a real
    git push authenticated as the worker identity. Integration tests do
    not set up a real git remote or worker credentials, so without this
    autouse the probe would either raise (transport error) or hang, and
    ``_should_launch_worker`` would fail closed and park every issue
    before dispatching a worker.

    Preflight tests use their own explicit
    ``patch.object(daemon_mod, "_probe_worker_push_denied", …)`` inside
    each test body; those inner patches take precedence (same pattern as
    ``_auto_patch_ruleset_check_daemon`` above).

    ``tests/chain/test_daemon_push_probe.py`` is excluded outright rather
    than relying on inner-patch precedence: several of its tests fetch
    ``_probe_worker_push_denied`` itself via ``getattr`` and invoke it
    directly to pin its own internals (only patching the lower-level
    ``_run`` seam) — those tests need the REAL function, not this
    autouse's mock, so this fixture is a no-op for that module.
    """
    if request.module.__name__.endswith(".test_daemon_push_probe"):
        yield
        return

    from baton_harness.chain.daemon import ProbeResult

    with patch(
        "baton_harness.chain.daemon._probe_worker_push_denied",
        return_value=ProbeResult(denied=True),
    ):
        yield
