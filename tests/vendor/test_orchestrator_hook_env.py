"""Tests for issue #347: thread the vault-fetched GitHub PAT to hooks.

Also carries #351's T3 coverage — hook diagnostics threading (D6 steps
2-3): ``run_hook`` returns a ``HookResult`` carrying the real
``returncode`` and a redacted ``stderr_tail`` on failure, the
``before_run`` ``RuntimeError`` embeds both, and the two best-effort
``after_run`` call sites (F4: orchestrator.py ~L274-279 and ~L387-397)
continue to swallow hook failures rather than propagate them. These
tests import ``baton_harness.vendor.symphony.hooks`` as a module (not
``from ... import HookResult``) so the pre-existing #347 tests above
keep collecting and passing independently of the new #351 symbol —
only the new tests below fail (cleanly, via ``AttributeError`` on
``hooks_mod.HookResult``) until T1 (redact.py) and T3 (hooks.py /
orchestrator.py) land.

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

import asyncio
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from baton_harness.vendor.symphony import hooks as hooks_mod
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
    ) -> hooks_mod.HookResult:
        calls.append((name, kwargs))
        return hooks_mod.HookResult(ok=True, returncode=0, stderr_tail="")

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

        ``_run_worker``'s before_run call site (orchestrator.py
        ~L197-202) forwards ``orch.hook_env`` as the ``env=`` kwarg on
        its ``run_hook`` call, so the vault-fetched PAT reaches the
        subprocess under both ``GH_TOKEN`` and ``GITHUB_TOKEN``.
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

        Guards against a naive fix that threads ``orch.hook_env`` into
        every ``run_hook`` call site instead of only ``before_run``.
        ``created_now=True`` ensures after_create actually fires so
        this assertion is not vacuous. Checks both ``GH_TOKEN`` and
        ``GITHUB_TOKEN`` — a fix that leaked the PAT via only one of
        the two keys must still be caught.
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
        assert "GITHUB_TOKEN" not in env, (
            "after_create must NOT receive the vault-fetched PAT under "
            "GITHUB_TOKEN either — only before_run needs it for #347"
        )

    def test_after_run_hook_does_not_receive_the_pat(self) -> None:
        """after_run's run_hook call must NOT receive the PAT.

        after_run (orchestrator.py ~L387) fires unconditionally at the
        end of every ``_run_worker`` call, so this assertion is never
        vacuous. Checks both ``GH_TOKEN`` and ``GITHUB_TOKEN`` — a fix
        that leaked the PAT via only one of the two keys must still be
        caught.
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
        assert "GITHUB_TOKEN" not in env, (
            "after_run must NOT receive the vault-fetched PAT under "
            "GITHUB_TOKEN either — only before_run needs it for #347"
        )


# ---------------------------------------------------------------------------
# Authored-green guard: hook_env unset must not regress bare construction
# ---------------------------------------------------------------------------


class TestHookEnvUnsetDoesNotRegressBareConstruction:
    """An Orchestrator that never touches ``hook_env`` keeps working.

    ``hook_env`` is initialised to ``None`` in ``Orchestrator.__init__``
    (an attribute-injection default, not a required constructor
    argument, mirroring the ``progress_cb`` precedent), so when a
    caller never assigns it, ``before_run``'s ``env`` kwarg stays
    absent and the run completes normally. This guard pins that
    default for every pre-existing bare
    ``Orchestrator(config=..., project_root=..., state_path=...)``
    construction — see ``test_exclude_labels_recheck.py`` and
    ``test_pr_exists_early_exit.py``, neither of which ever sets
    ``hook_env`` — so they keep working without an ``AttributeError``.
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


# ---------------------------------------------------------------------------
# #351 T3 — run_hook returns HookResult (D6 step 2)
# ---------------------------------------------------------------------------


class _FakeSubprocess:
    """Minimal asyncio.subprocess stub.

    Duplicated from ``tests/vendor/test_run_hook_env.py`` per this package's
    stated "duplicate helpers rather than cross-import between test files"
    convention.
    """

    def __init__(
        self,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> None:
        self.returncode = returncode
        self._communicate_result = (stdout, stderr)

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._communicate_result

    def kill(self) -> None:
        """No-op kill for the timeout path."""


def _run_sync(coro: object) -> object:
    """Run an async coroutine synchronously for test use."""
    return asyncio.run(coro)  # type: ignore[arg-type]


class TestRunHookReturnsHookResult:
    """``run_hook`` returns a ``HookResult`` (ok, returncode, stderr_tail)."""

    def test_run_hook_result_carries_real_returncode_on_failure(self) -> None:
        """A non-zero exit code is preserved on the returned HookResult."""

        async def fake_create_subprocess_exec(
            *args: object, **kwargs: object
        ) -> _FakeSubprocess:
            return _FakeSubprocess(returncode=17, stderr=b"boom")

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = _run_sync(
                hooks_mod.run_hook("before_run", "false", cwd="/tmp")
            )

        assert isinstance(result, hooks_mod.HookResult), (
            f"run_hook must return a HookResult on failure; got "
            f"{result!r} ({type(result).__name__})"
        )
        assert result.ok is False
        assert result.returncode == 17, (
            f"HookResult.returncode must carry the real exit code; got "
            f"{result.returncode!r}"
        )

    def test_run_hook_result_ok_true_on_success(self) -> None:
        """A zero exit code produces HookResult(ok=True, returncode=0, ...)."""

        async def fake_create_subprocess_exec(
            *args: object, **kwargs: object
        ) -> _FakeSubprocess:
            return _FakeSubprocess(returncode=0, stderr=b"")

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = _run_sync(
                hooks_mod.run_hook("before_run", "true", cwd="/tmp")
            )

        assert isinstance(result, hooks_mod.HookResult)
        assert result.ok is True
        assert result.returncode == 0


class TestRunHookStderrTailIsRedacted:
    """``HookResult.stderr_tail`` is redacted before it reaches a caller."""

    def test_stderr_tail_redacts_a_prefixed_token(self) -> None:
        """A ghp_-prefixed token in raw stderr is redacted in stderr_tail."""
        secret = "ghp_" + "A" * 40

        async def fake_create_subprocess_exec(
            *args: object, **kwargs: object
        ) -> _FakeSubprocess:
            return _FakeSubprocess(
                returncode=1,
                stderr=f"fatal: bad credentials: {secret}".encode(),
            )

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = _run_sync(
                hooks_mod.run_hook("before_run", "false", cwd="/tmp")
            )

        assert secret not in result.stderr_tail, (
            f"a raw token must never survive into stderr_tail; got "
            f"{result.stderr_tail!r}"
        )

    def test_gh_token_env_value_never_appears_in_stderr_tail(self) -> None:
        """A GH_TOKEN value passed via env= never appears in stderr_tail.

        Simulates a failing git/gh invocation that echoes the injected
        token verbatim (no gh*_ prefix survives the echo, e.g. it is
        embedded in a remote URL) — the value-based redaction pass
        (extra_values=env.values()) must catch what the pattern pass
        cannot (#351 F5).
        """
        injected_pat = "s3cr3t-injected-value-without-a-known-prefix"

        async def fake_create_subprocess_exec(
            *args: object, **kwargs: object
        ) -> _FakeSubprocess:
            return _FakeSubprocess(
                returncode=1,
                stderr=(
                    f"remote: Invalid credentials for {injected_pat}"
                ).encode(),
            )

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = _run_sync(
                hooks_mod.run_hook(
                    "before_run",
                    "false",
                    cwd="/tmp",
                    env={"GH_TOKEN": injected_pat},
                )
            )

        assert injected_pat not in result.stderr_tail, (
            "a GH_TOKEN value passed via env= must never appear in "
            f"stderr_tail; got {result.stderr_tail!r}"
        )


# ---------------------------------------------------------------------------
# #351 T3 — before_run's RuntimeError carries rc + tail (D6 step 3)
# ---------------------------------------------------------------------------


class _FakeHookResult(NamedTuple):
    """Local stand-in for hooks.HookResult.

    For tests that only need to control Orchestrator._run_worker's
    consumption of the return value (not run_hook's own construction of it —
    covered above).
    """

    ok: bool
    returncode: int | None
    stderr_tail: str


def _run_worker_expecting_runtime_error(
    orch: Orchestrator,
    issue: Issue,
    *,
    before_run_result: _FakeHookResult,
) -> Exception | None:
    """Run ``orch._run_worker(issue)`` and capture a raised RuntimeError.

    Args:
        orch: The Orchestrator under test.
        issue: The fake issue to run.
        before_run_result: The HookResult-shaped value the patched
            ``run_hook`` returns for the ``before_run`` call.

    Returns:
        The raised exception, or ``None`` if no exception propagated.
    """

    async def fake_run_hook(  # noqa: ANN401
        name: str, script: object, **kwargs: object
    ) -> _FakeHookResult:
        if name == "before_run":
            return before_run_result
        return _FakeHookResult(ok=True, returncode=0, stderr_tail="")

    fake_wt = MagicMock()
    fake_wt.created_now = True
    fake_wt.path = "/fake/wt"

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
            orch.tracker,
            "fetch_issue_state",
            side_effect=fake_fetch_issue_state,
        ),
        patch(
            "baton_harness.vendor.symphony.orchestrator.run_hook",
            side_effect=fake_run_hook,
        ),
    ):
        try:
            asyncio.run(orch._run_worker(issue))
        except RuntimeError as exc:  # noqa: BLE001
            return exc
    return None


class TestBeforeRunRuntimeErrorCarriesDiagnostics:
    """before_run's RuntimeError embeds the hook's rc and stderr_tail."""

    def test_runtime_error_message_contains_returncode_and_tail(
        self,
    ) -> None:
        """The raised RuntimeError names the returncode and stderr tail."""
        orch = _make_orch()
        issue = _fake_issue()
        failing = _FakeHookResult(
            ok=False, returncode=17, stderr_tail="boom xyz"
        )

        exc = _run_worker_expecting_runtime_error(
            orch, issue, before_run_result=failing
        )

        assert exc is not None, (
            "before_run hook failure must raise a RuntimeError, "
            "not silently continue"
        )
        message = str(exc)
        assert "17" in message, (
            f"RuntimeError message must carry the returncode (17); "
            f"got {message!r}"
        )
        assert "boom xyz" in message, (
            f"RuntimeError message must carry the stderr_tail; got {message!r}"
        )


# ---------------------------------------------------------------------------
# #351 T3 — after_run's two best-effort call sites swallow failures (F4)
# ---------------------------------------------------------------------------


class TestAfterRunSwallowsHookFailure:
    """after_run's run_hook call sites never propagate a hook failure.

    F4: orchestrator.py has two after_run call sites — a success-path
    one (~L387-397) and a failure-path one (~L274-279) — both of which
    discard run_hook's return value today and must continue to do so
    once it becomes a HookResult.
    """

    def test_after_run_is_invoked_on_the_success_path(self) -> None:
        """after_run fires on the success path via the shared harness.

        ``_run_worker_capturing_hook_calls``'s ``fake_run_hook`` always
        returns ``True`` (no failure), so this only confirms that
        ``after_run`` is invoked when the hook succeeds — it does not
        exercise failure-swallowing. Failure-swallowing behavior is
        covered separately by
        ``test_after_run_success_path_swallows_failing_hookresult``
        below, which injects a failing ``HookResult``.
        """
        orch = _make_orch()
        issue = _fake_issue()

        calls = _run_worker_capturing_hook_calls(orch, issue, created_now=True)

        after_run_calls = [kw for (name, kw) in calls if name == "after_run"]
        assert after_run_calls, (
            "after_run hook was never invoked (test setup issue)"
        )

    def test_after_run_success_path_swallows_failing_hookresult(
        self,
    ) -> None:
        """A HookResult(ok=False, ...) from after_run must not propagate."""
        orch = _make_orch()
        issue = _fake_issue()

        async def fake_run_hook(  # noqa: ANN401
            name: str, script: object, **kwargs: object
        ) -> _FakeHookResult:
            if name == "after_run":
                return _FakeHookResult(
                    ok=False, returncode=3, stderr_tail="after_run boom"
                )
            return _FakeHookResult(ok=True, returncode=0, stderr_tail="")

        fake_wt = MagicMock()
        fake_wt.created_now = True
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
            try:
                asyncio.run(orch._run_worker(issue))
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(
                    "after_run's success-path call site must swallow a "
                    f"failing HookResult, not raise; got "
                    f"{type(exc).__name__}: {exc}"
                ) from exc


# ---------------------------------------------------------------------------
# #351 reconciliation gap (T1) — BWS_ACCESS_TOKEN value-pass coverage
# ---------------------------------------------------------------------------
#
# ``run_hook`` calls ``redact_secrets(stderr_text,
# extra_values=(env or {}).values())`` at its stderr-redaction call
# site(s) (hooks.py ~L95, ~L128). The ``env`` dict there is whatever the
# caller passed via ``env=`` — in production, ``orch.hook_env``, which
# only ever holds ``GH_TOKEN``/``GITHUB_TOKEN`` (see
# ``TestHookEnvWiredToBeforeRunOnly`` above). ``BWS_ACCESS_TOKEN`` lives
# in the daemon's ambient ``os.environ`` and is never threaded through
# ``env=``, so a hook whose stderr echoes that value verbatim is NOT
# covered by the value pass today. The test below proves the gap against
# the real call site (not just that ``redact_secrets`` *can* redact a
# value if handed it — see ``tests/test_redact.py`` for that, already
# covered by ``TestExtraValuesRedaction``).


class TestBwsAccessTokenRedactedInStderrTail:
    """A ``BWS_ACCESS_TOKEN`` from os.environ never leaks into stderr_tail."""

    def test_bws_access_token_from_process_env_never_appears_in_stderr_tail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A BWS_ACCESS_TOKEN set only in os.environ is redacted.

        The sentinel deliberately has no recognisable ``gh*_`` prefix and
        is not shaped like a credential-bearing URL, so ``redact_secrets``'s
        pattern pass cannot catch it independently — only a value pass
        that includes ``os.environ["BWS_ACCESS_TOKEN"]`` in its
        ``extra_values`` would. ``env=`` is given a realistic
        ``hook_env``-shaped dict (``GH_TOKEN`` only, no
        ``BWS_ACCESS_TOKEN`` key) to mirror production, where the vault
        token is never threaded through ``env=`` at all — it is set via
        ``monkeypatch.setenv`` on the process environment instead.
        """
        sentinel = "bws-sentinel-no-known-prefix-A1B2C3D4"
        monkeypatch.setenv("BWS_ACCESS_TOKEN", sentinel)

        async def fake_create_subprocess_exec(
            *args: object, **kwargs: object
        ) -> _FakeSubprocess:
            return _FakeSubprocess(
                returncode=1,
                stderr=f"vault error: bad secret {sentinel}".encode(),
            )

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = _run_sync(
                hooks_mod.run_hook(
                    "before_run",
                    "false",
                    cwd="/tmp",
                    env={"GH_TOKEN": "unrelated-gh-token-value"},
                )
            )

        assert sentinel not in result.stderr_tail, (  # type: ignore[attr-defined]
            "BWS_ACCESS_TOKEN (set only in os.environ, never in env=) "
            f"leaked into stderr_tail; got {result.stderr_tail!r}"  # type: ignore[attr-defined]
        )

    def test_bws_access_token_rotated_mid_run_still_redacts_the_leaked_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A BWS_ACCESS_TOKEN that rotates mid-run must still be redacted.

        ``merged_env`` is built early in ``run_hook`` (before the
        subprocess is spawned) from the process's ``os.environ`` at
        that moment — this is the token value the subprocess actually
        runs with, and the value it could echo into stderr. The
        stderr-redaction call site(s) run AFTER ``await
        proc.communicate()`` and independently call
        ``os.environ.get("BWS_ACCESS_TOKEN", "")`` again at that later
        point. If the parent process's token value rotates in between
        subprocess spawn and this later read, the redaction pass
        scrubs the NEW value while the subprocess (and its echoed
        stderr) actually ran with the OLD one — so the token that
        genuinely leaked slips through unredacted.

        This test rotates the REAL ``os.environ["BWS_ACCESS_TOKEN"]``
        (via ``monkeypatch.setenv``) at the exact moment the mocked
        ``asyncio.create_subprocess_exec`` is invoked — the true spawn
        boundary. This is deliberately accessor-agnostic: whatever
        read constructs ``merged_env`` (dict-spread, ``.get()``,
        subscript — any of them) happens strictly before this point
        and observes the OLD value; any read of
        ``os.environ["BWS_ACCESS_TOKEN"]`` performed AFTER this point
        (i.e. after ``communicate()``, at the redaction call site)
        observes the NEW value regardless of which accessor it uses.
        A correct fix must capture the token's value once, at
        ``merged_env`` construction time, and reuse that captured
        value at the redaction call site(s) — merely swapping
        ``os.environ.get(...)`` for a different accessor at the same
        (late) call site would still fail this test. The fake
        subprocess's stderr echoes the OLD value, mirroring what a
        real credential leak during the hook's actual execution would
        look like.
        """
        monkeypatch.setenv("BWS_ACCESS_TOKEN", "old-token-value")

        async def fake_create_subprocess_exec(
            *args: object, **kwargs: object
        ) -> _FakeSubprocess:
            # The parent process's token rotates at the instant the
            # child is spawned. merged_env (built just before this
            # call, from whatever os.environ held a moment ago) has
            # already captured the OLD value by this point -- only a
            # *later* read of os.environ observes the NEW one.
            monkeypatch.setenv("BWS_ACCESS_TOKEN", "new-token-value")
            return _FakeSubprocess(
                returncode=1,
                stderr=b"vault error: bad secret old-token-value",
            )

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = _run_sync(
                hooks_mod.run_hook("before_run", "false", cwd="/tmp")
            )

        assert "old-token-value" not in result.stderr_tail, (  # type: ignore[attr-defined]
            "the token value LIVE during subprocess execution (the OLD, "
            "pre-rotation value actually echoed into stderr) must be the "
            "one that gets redacted — not whatever a fresh read of "
            "os.environ['BWS_ACCESS_TOKEN'] returns at the later, "
            "independent redaction call site after the token has "
            f"rotated; got stderr_tail={result.stderr_tail!r}"  # type: ignore[attr-defined]
        )
