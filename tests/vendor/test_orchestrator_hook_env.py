"""Tests for issue #347: thread the vault-fetched GitHub PAT to hooks.

The PAT reaches the ``before_run`` hook subprocess via
``Orchestrator.hook_env``, without ever writing it into the daemon's
own ambient ``os.environ``.

Background (see #222 / #347 investigation): ``bootstrap_secrets()``
stopped writing the BWS-vault-fetched executor PAT into
``os.environ["GH_TOKEN"]``, so ``before_run.main()``'s
``validate_github_token()`` gate (which reads only ``os.environ``) now
always fails on a BWS-only setup. The chosen fix is option (b): set
``orch.hook_env = {"GH_TOKEN": pat, "GITHUB_TOKEN": pat}`` on the
``Orchestrator`` instance (mirroring the existing ``progress_cb``
attribute-injection precedent — see ``orchestrator.py``'s
``self.progress_cb = None`` in ``__init__`` and
``chain/daemon/work_unit.py``'s ``orch.progress_cb = _progress_cb``
post-construction assignment), and have ONLY the ``before_run``
``run_hook`` call site (``orchestrator.py`` ~L197-202) forward it as
``env=``. ``after_create`` (~L187-194) and ``after_run`` (~L269, ~L387)
must NOT receive the token.

Coverage:
- ``before_run``'s ``run_hook`` call receives ``env={"GH_TOKEN": ...,
  "GITHUB_TOKEN": ...}`` when ``orch.hook_env`` is set.
- ``after_create``'s ``run_hook`` call does NOT receive the PAT.
- ``after_run``'s ``run_hook`` call does NOT receive the PAT.
- Authored-green invariant guard: an ``Orchestrator`` constructed
  without ever touching ``hook_env`` (the pre-#347 / bare-construction
  shape used by every other vendor Orchestrator test) must not regress
  — ``before_run``'s ``env`` stays ``None``/absent and the run
  completes without an ``AttributeError``. This pins the ``__init__``
  side of the ``progress_cb`` precedent: ``self.hook_env`` must default
  to ``None`` rather than only being set when the daemon opts in.

Mock strategy follows ``tests/vendor/test_exclude_labels_recheck.py``:
``run_hook`` is patched at the module level it is imported into
(``baton_harness.vendor.symphony.orchestrator.run_hook``), and all
async calls are driven with ``asyncio.run`` (no pytest-asyncio dep).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from baton_harness.vendor.symphony.config import WorkflowConfig
from baton_harness.vendor.symphony.orchestrator import Orchestrator
from baton_harness.vendor.symphony.tracker import Issue

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FAKE_PAT = "github_pat_11FAKEFAKEFAKEFAKEFAKEFAKE_347TESTVALUE"

# ---------------------------------------------------------------------------
# Helpers (mirrors tests/vendor/test_exclude_labels_recheck.py)
# ---------------------------------------------------------------------------


def _minimal_config(max_turns: int = 1) -> WorkflowConfig:
    """Return a minimal WorkflowConfig with all three hooks configured.

    All three hooks (``hook_after_create``, ``hook_before_run``,
    ``hook_after_run``) are given non-empty scripts so that every
    ``run_hook`` call site actually fires during ``_run_worker`` (the
    real ``run_hook`` short-circuits on an empty/whitespace script, but
    since it is fully mocked here that guard is irrelevant — the
    non-empty values just document intent).

    Args:
        max_turns: Maximum turns per agent run.

    Returns:
        A ``WorkflowConfig`` suitable for driving ``_run_worker`` under
        test with every hook call site reachable.
    """
    return WorkflowConfig(
        prompt_template="Work on issue #{{ issue.number }}: {{ issue.title }}",
        tracker_labels=["agent-ready"],
        # Empty on purpose: keeps the VP-2 mid-turn exclude_labels
        # re-check (which calls run_gh, unrelated to this test) inert.
        tracker_exclude_labels=[],
        tracker_assignee=None,
        max_concurrent=1,
        max_turns=max_turns,
        hook_after_create="true",
        hook_before_run="true",
        hook_after_run="true",
        hook_timeout_ms=5000,
        poll_interval_ms=1000,
        max_retry_backoff_ms=10000,
    )


def _fake_issue(number: int = 1) -> Issue:
    """Return a minimal Issue dataclass."""
    return Issue(
        number=number,
        title="Test Issue",
        state="open",
        body="",
        url=f"https://github.com/o/r/issues/{number}",
        labels=[],
    )


def _make_orch(
    max_turns: int = 1,
    project_root: str = "/tmp/fake_root",
    state_path: str = "/tmp/fake_state.json",
) -> Orchestrator:
    """Create an Orchestrator with a minimal config, three hooks enabled."""
    config = _minimal_config(max_turns=max_turns)
    return Orchestrator(
        config=config,
        project_root=project_root,
        state_path=state_path,
    )


def _run_worker_capturing_hook_calls(
    orch: Orchestrator,
    issue: Issue,
    *,
    created_now: bool = True,
) -> list[tuple[str, dict[str, Any]]]:
    """Run ``orch._run_worker(issue)`` and capture every ``run_hook`` call.

    Args:
        orch: The Orchestrator under test.
        issue: The fake issue to run.
        created_now: Whether the fake worktree reports as freshly
            created, which gates whether ``after_create`` fires at all.
            Defaults to ``True`` so every hook call site is exercised.

    Returns:
        A list of ``(hook_name, kwargs)`` tuples, one per ``run_hook``
        invocation observed during the run, in call order.
    """
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_run_hook(  # noqa: ANN401
        name: str, script: object, **kwargs: object
    ) -> bool:
        calls.append((name, kwargs))
        return True

    fake_wt = MagicMock()
    fake_wt.created_now = created_now
    fake_wt.path = "/fake/wt"

    fake_turn_result = MagicMock()
    fake_turn_result.success = True
    fake_turn_result.error = None

    async def fake_run_turn(**kwargs: Any) -> Any:  # noqa: ANN401
        return fake_turn_result

    async def fake_fetch_issue_state(num: int) -> str:
        return "open"

    with (
        patch.object(
            orch.workspace,
            "ensure_worktree",
            new_callable=AsyncMock,
            return_value=fake_wt,
        ),
        patch.object(
            orch.worker,
            "run_turn",
            side_effect=fake_run_turn,
        ),
        patch.object(
            orch.tracker,
            "fetch_issue_state",
            side_effect=fake_fetch_issue_state,
        ),
        patch(
            "baton_harness.vendor.symphony.orchestrator.run_hook",
            side_effect=fake_run_hook,
        ),
        patch.object(
            orch.tracker,
            "check_pr_exists",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        import asyncio

        asyncio.run(orch._run_worker(issue))

    return calls


# ---------------------------------------------------------------------------
# Positive: before_run receives the PAT via orch.hook_env
# ---------------------------------------------------------------------------


class TestHookEnvWiredToBeforeRunOnly:
    """``orch.hook_env`` reaches ONLY the ``before_run`` run_hook call."""

    def test_before_run_hook_receives_pat_via_hook_env(self) -> None:
        """before_run's run_hook call gets env={"GH_TOKEN": pat, ...}.

        MUST FAIL today: ``_run_worker``'s before_run call site
        (orchestrator.py ~L197-202) does not pass ``env=`` at all, so
        ``env`` is absent from the captured kwargs regardless of
        ``orch.hook_env``.
        """
        orch = _make_orch()
        orch.hook_env = {"GH_TOKEN": _FAKE_PAT, "GITHUB_TOKEN": _FAKE_PAT}
        issue = _fake_issue()

        calls = _run_worker_capturing_hook_calls(orch, issue, created_now=True)

        before_run_calls = [kw for (name, kw) in calls if name == "before_run"]
        assert before_run_calls, (
            "before_run hook was never invoked (test setup issue)"
        )
        env = before_run_calls[0].get("env")
        assert env is not None, (
            "before_run's run_hook call did not receive an env= override "
            "— the vault-fetched PAT never reaches the before_run "
            "subprocess (issue #347)"
        )
        assert env.get("GH_TOKEN") == _FAKE_PAT, (
            f"expected before_run env GH_TOKEN={_FAKE_PAT!r}, "
            f"got {env.get('GH_TOKEN')!r}"
        )
        assert env.get("GITHUB_TOKEN") == _FAKE_PAT, (
            f"expected before_run env GITHUB_TOKEN={_FAKE_PAT!r}, "
            f"got {env.get('GITHUB_TOKEN')!r}"
        )

    def test_after_create_hook_does_not_receive_the_pat(self) -> None:
        """after_create's run_hook call must NOT receive the PAT.

        MUST FAIL today for the wrong reason if a naive fix threads
        ``orch.hook_env`` into every ``run_hook`` call site instead of
        only ``before_run``. Currently passes vacuously (no env= is
        passed anywhere yet); ``created_now=True`` ensures after_create
        actually fires so this assertion is not vacuous once the fix
        lands.
        """
        orch = _make_orch()
        orch.hook_env = {"GH_TOKEN": _FAKE_PAT, "GITHUB_TOKEN": _FAKE_PAT}
        issue = _fake_issue()

        calls = _run_worker_capturing_hook_calls(orch, issue, created_now=True)

        after_create_calls = [
            kw for (name, kw) in calls if name == "after_create"
        ]
        assert after_create_calls, (
            "after_create hook was never invoked (test setup issue —"
            " created_now=True should force it to fire)"
        )
        env = after_create_calls[0].get("env") or {}
        assert "GH_TOKEN" not in env, (
            "after_create must NOT receive the vault-fetched PAT — only "
            "before_run needs it for issue #347"
        )

    def test_after_run_hook_does_not_receive_the_pat(self) -> None:
        """after_run's run_hook call must NOT receive the PAT.

        after_run (orchestrator.py ~L387) fires unconditionally at the
        end of every ``_run_worker`` call, so this assertion is never
        vacuous.
        """
        orch = _make_orch()
        orch.hook_env = {"GH_TOKEN": _FAKE_PAT, "GITHUB_TOKEN": _FAKE_PAT}
        issue = _fake_issue()

        calls = _run_worker_capturing_hook_calls(orch, issue, created_now=True)

        after_run_calls = [kw for (name, kw) in calls if name == "after_run"]
        assert after_run_calls, (
            "after_run hook was never invoked (test setup issue)"
        )
        env = after_run_calls[0].get("env") or {}
        assert "GH_TOKEN" not in env, (
            "after_run must NOT receive the vault-fetched PAT — only "
            "before_run needs it for issue #347"
        )


# ---------------------------------------------------------------------------
# Authored-green guard: hook_env unset must not regress bare construction
# ---------------------------------------------------------------------------


class TestHookEnvUnsetDoesNotRegressBareConstruction:
    """An Orchestrator that never touches ``hook_env`` keeps working.

    Authored-green today: no code path reads ``self.hook_env`` yet, so
    ``before_run``'s ``env`` kwarg is simply absent and the run
    completes normally. This guard pins the ``progress_cb`` precedent
    for the fix: ``hook_env`` must be initialised to ``None`` in
    ``Orchestrator.__init__`` (an attribute-injection default, not a
    required constructor argument), so every pre-existing bare
    ``Orchestrator(config=..., project_root=..., state_path=...)``
    construction — see ``test_exclude_labels_recheck.py`` and
    ``test_pr_exists_early_exit.py``, neither of which ever sets
    ``hook_env`` — keeps working without an ``AttributeError``.
    """

    def test_bare_orchestrator_runs_before_run_with_no_env_override(
        self,
    ) -> None:
        """A bare Orchestrator (hook_env never set) does not error.

        Confirms before_run's ``env`` stays ``None``/absent when
        ``hook_env`` was never assigned — the pre-#347 behaviour must
        be preserved for callers that do not opt in.
        """
        orch = _make_orch()
        issue = _fake_issue()

        # Precondition: this Orchestrator never had hook_env touched.
        assert not hasattr(orch, "hook_env") or orch.hook_env is None, (
            "test setup issue: hook_env must be untouched/None for this "
            "guard to be meaningful"
        )

        calls = _run_worker_capturing_hook_calls(orch, issue, created_now=True)

        before_run_calls = [kw for (name, kw) in calls if name == "before_run"]
        assert before_run_calls, (
            "before_run hook was never invoked (test setup issue)"
        )
        env = before_run_calls[0].get("env")
        assert env is None, (
            "before_run's env kwarg must be None when hook_env was never "
            f"set on the Orchestrator; got {env!r}"
        )
