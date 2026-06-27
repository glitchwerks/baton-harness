"""Root-level conftest: autouse fixtures that apply to every test module.

The branch-protection preflight gate (``_should_launch_worker``) was wired
into ``_run_work_unit`` as part of issue #144.  Two module-level helpers
gate every worker dispatch:

* ``_resolve_app_id()`` — reads ``BH_GITHUB_APP_ID`` from the environment.
  Integration tests that do not set this env var would hit fail-closed
  parking before any worker is dispatched.

* Inside ``_should_launch_worker``, ``ruleset_is_provisioned`` makes live
  ``gh api`` calls.  Integration tests that do not mock the ruleset layer
  would hit ``ERROR`` status and park every issue.

The two autouse fixtures below provide safe defaults so that integration-
level daemon tests (``test_daemon.py``, etc.) remain unaffected by the
new gate layer.

Preflight-specific unit tests (``test_daemon_preflight.py``) patch these
symbols inside each individual test body via ``patch.object``; those inner
patches take precedence and these autouse fixtures have no effect on them.
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
def _auto_patch_ruleset_is_provisioned_daemon() -> None:  # type: ignore[return]
    """Return MATCH for tests that do not exercise the ruleset check.

    ``ruleset_is_provisioned`` (imported into ``daemon`` module scope) makes
    live ``gh api`` calls.  Integration tests do not mock the ruleset layer;
    without this autouse the daemon would detect ``ERROR`` and park every
    issue before dispatching a worker.

    Preflight tests 1–6 use their own explicit
    ``patch.object(daemon_mod, "ruleset_is_provisioned", …)`` inside each
    test body; those inner patches take precedence over this autouse and the
    tests call the REAL ``_should_launch_worker`` function directly (not the
    autouse mock), so the correct per-test status flows through.
    """
    from baton_harness.chain.ruleset_status import RulesetStatus

    with patch(
        "baton_harness.chain.daemon.ruleset_is_provisioned",
        return_value=RulesetStatus.MATCH,
    ):
        yield
