"""Tests for issue #32 — defensive hardening of after_run.py.

Pins the behavioral contract for:

1. Guarded ``json.loads`` in ``_classify``: non-JSON ``gh pr list`` output
   is transient, never terminal, and never tears ``agent-ready``.
2. Returncode-checked ``_classify``: a failed ``gh pr list`` (non-zero /
   empty stdout) is a transient error, NOT ``COMMITTED_NO_PR``.
3. Bounded retry with recovery: transient failures followed by a valid ``gh
   pr list`` response classify as ``PR_OPENED``.
4. Guarded ``_current_labels`` parse: non-JSON ``gh issue view`` output does
   not crash the run.

All subprocess calls are patched through the module-local ``_run`` seam —
the single patchable symbol used by every existing test in
``test_after_run.py``.  No real git or gh is invoked.

Retry contract (for Phase 2 implementer):
    - Minimum 3 attempts before giving up on transient ``gh pr list``
      failures (non-zero returncode OR non-JSON stdout).
    - Between attempts the implementation must call ``time.sleep`` (or a
      wrapper that delegates to it).  Tests patch ``time.sleep`` via
      ``unittest.mock.patch`` on ``after_run.time.sleep``.  If the
      implementer introduces a different sleep symbol, they must update this
      seam or provide an equivalent patchable alias in the module.
    - After exhausting retries, ``main()`` must return non-zero AND must NOT
      issue ``gh issue edit --remove-label agent-ready``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from baton_harness import after_run
from baton_harness.after_run import RunOutcome, _classify, _current_labels

# ---------------------------------------------------------------------------
# Re-use the same helper the existing test suite uses
# ---------------------------------------------------------------------------


def _completed(
    stdout: str = "", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """Build a fake CompletedProcess for mock return values.

    Args:
        stdout: Simulated standard output.
        returncode: Simulated process return code.
        stderr: Simulated standard error.

    Returns:
        A ``subprocess.CompletedProcess`` with the given fields.
    """
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ---------------------------------------------------------------------------
# Shared side-effect factory for classify's git prefix calls
# (git status, git rev-parse base, git cherry, git rev-parse branch)
# ---------------------------------------------------------------------------

_GIT_PREFIX_CLEAN = [
    _completed(stdout=""),  # git status — clean
    _completed(stdout="abc123\n"),  # git rev-parse base SHA
    _completed(stdout="+ deadbeef\n"),  # git cherry — 1 ahead
    _completed(stdout="feat-32-guard\n"),  # git rev-parse (branch name)
]

# Valid non-empty PR JSON (simulates a real open PR)
_PR_JSON_OPEN = json.dumps([{"number": 32}])

# Non-JSON gh output (rate-limit banner, HTML, auth expiry, etc.)
_NON_JSON_BANNERS = [
    "<!DOCTYPE html><html>...rate limited...</html>",
    "error: HTTP 429: too many requests",
    "You are not logged in. Run gh auth login to authenticate.",
    "rate limit exceeded; retry after 2026-06-15T00:00:00Z",
]


# ---------------------------------------------------------------------------
# Contract 1: non-JSON gh pr list → transient, NOT terminal, no agent-ready
# removal
# ---------------------------------------------------------------------------


class TestClassifyGuardedJsonLoadsNonJson:
    """Non-JSON ``gh pr list`` stdout is a transient error, not terminal.

    Load-bearing: this is the unbounded-redispatch-loop guard.  If the
    implementer allows an uncaught ``JSONDecodeError`` to propagate, or
    misclassifies the response as ``COMMITTED_NO_PR``, the issue loses
    ``agent-ready`` and the daemon retries it immediately — causing a loop.
    """

    def test_non_json_stdout_does_not_raise_json_decode_error(self) -> None:
        """Non-JSON gh pr list stdout must NOT propagate JSONDecodeError.

        Current code: ``json.loads(pr_result.stdout)`` at L226 raises
        ``JSONDecodeError`` on any non-JSON string.  After the fix this
        must be caught and treated as transient.
        """
        with patch("baton_harness.after_run._run") as mock_run:
            # Every gh pr list attempt returns a non-JSON banner
            mock_run.side_effect = (
                list(_GIT_PREFIX_CLEAN)
                + [_completed(stdout=_NON_JSON_BANNERS[0])] * 5
            )
            with patch(
                "baton_harness.after_run.time",
                create=True,
            ) as mock_time:
                mock_time.sleep = MagicMock()
                # Must not raise; may return any non-PR_OPENED / non-terminal
                # outcome or raise a controlled exception — but NOT
                # JSONDecodeError
                try:
                    _classify()
                except json.JSONDecodeError as exc:
                    pytest.fail(
                        f"_classify raised JSONDecodeError on non-JSON "
                        f"gh output: {exc}"
                    )

    def test_non_json_stdout_is_not_classified_as_committed_no_pr(
        self,
    ) -> None:
        """Non-JSON gh pr list must NOT yield COMMITTED_NO_PR.

        ``COMMITTED_NO_PR`` triggers ``agent-ready`` removal.  A transient
        API response banner must not be misread as "no PR found".
        """
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = (
                list(_GIT_PREFIX_CLEAN)
                + [_completed(stdout=_NON_JSON_BANNERS[1])] * 5
            )
            with patch(
                "baton_harness.after_run.time",
                create=True,
            ) as mock_time:
                mock_time.sleep = MagicMock()
                try:
                    result = _classify()
                    # If _classify returns (no exception), must not be
                    # COMMITTED_NO_PR
                    assert result != RunOutcome.COMMITTED_NO_PR, (
                        "Non-JSON gh pr list must not be classified as "
                        "COMMITTED_NO_PR — that would trigger agent-ready "
                        "removal"
                    )
                except json.JSONDecodeError:
                    pytest.fail(
                        "JSONDecodeError propagated; must be caught internally"
                    )

    def test_non_json_stdout_is_not_classified_as_pr_opened(self) -> None:
        """Non-JSON gh pr list must NOT yield PR_OPENED.

        The implementation must not parse garbage as a truthy list of PRs.
        """
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = (
                list(_GIT_PREFIX_CLEAN)
                + [_completed(stdout=_NON_JSON_BANNERS[2])] * 5
            )
            with patch(
                "baton_harness.after_run.time",
                create=True,
            ) as mock_time:
                mock_time.sleep = MagicMock()
                try:
                    result = _classify()
                    assert result != RunOutcome.PR_OPENED, (
                        "Non-JSON gh pr list must not be classified as "
                        "PR_OPENED"
                    )
                except json.JSONDecodeError:
                    pytest.fail(
                        "JSONDecodeError propagated; must be caught internally"
                    )


class TestMainGuardedJsonLoadsNoAgentReadyRemoval:
    """Non-JSON gh pr list → main() non-zero, no agent-ready removal.

    This is the core safety contract: a transient gh API failure must never
    cause the daemon to remove ``agent-ready`` and schedule a re-dispatch.
    """

    def test_non_json_pr_list_does_not_remove_agent_ready(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main() must not issue --remove-label agent-ready on non-JSON gh.

        Arrange: all gh pr list calls return a rate-limit HTML banner.
        Assert: no ``gh issue edit --remove-label agent-ready`` call is made.
        """
        worktree = tmp_path / "feat-32-guard"
        worktree.mkdir()
        monkeypatch.chdir(worktree)
        monkeypatch.delenv("CHAIN_BASE_BRANCH", raising=False)

        with patch("baton_harness.after_run._run") as mock_run:
            # Provide enough responses: git prefix + repeated non-JSON for
            # all retry attempts
            mock_run.side_effect = (
                list(_GIT_PREFIX_CLEAN)
                + [
                    _completed(stdout=_NON_JSON_BANNERS[0]),
                    _completed(stdout=_NON_JSON_BANNERS[0]),
                    _completed(stdout=_NON_JSON_BANNERS[0]),
                    _completed(stdout=_NON_JSON_BANNERS[0]),
                    _completed(stdout=_NON_JSON_BANNERS[0]),
                ]
            )
            with patch(
                "baton_harness.after_run.time", create=True
            ) as _mt:
                _mt.sleep = MagicMock()
                result = after_run.main()

        # main() must be non-zero — transient failure, not success
        assert result != 0, (
            "main() must return non-zero when gh pr list returns non-JSON "
            "on every attempt"
        )

        # No --remove-label agent-ready must have been called
        all_cmd_lists = [c[0][0] for c in mock_run.call_args_list]
        remove_agent_ready_calls = [
            cmd
            for cmd in all_cmd_lists
            if "--remove-label" in cmd and "agent-ready" in cmd
        ]
        assert remove_agent_ready_calls == [], (
            "gh issue edit --remove-label agent-ready must NOT be called when "
            "gh pr list returns non-JSON (transient, not terminal): "
            f"got calls: {remove_agent_ready_calls}"
        )

    def test_non_json_pr_list_main_returns_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main() returns non-zero exit code on persistent non-JSON failure.

        A transient gh failure must surface as a hook failure so Baton or the
        daemon can record the error rather than treating it as success.
        """
        worktree = tmp_path / "feat-32-banner"
        worktree.mkdir()
        monkeypatch.chdir(worktree)
        monkeypatch.delenv("CHAIN_BASE_BRANCH", raising=False)

        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = (
                list(_GIT_PREFIX_CLEAN)
                + [_completed(stdout=banner) for banner in _NON_JSON_BANNERS]
                + [_completed(stdout=_NON_JSON_BANNERS[0])]
            )
            with patch(
                "baton_harness.after_run.time", create=True
            ) as _mt:
                _mt.sleep = MagicMock()
                result = after_run.main()

        assert result != 0, (
            "main() must return non-zero when gh pr list is persistently "
            "non-JSON"
        )


# ---------------------------------------------------------------------------
# Contract 2: failed gh pr list (non-zero returncode, empty stdout)
# is NOT "no PR"
# ---------------------------------------------------------------------------


class TestClassifyReturncodeChecked:
    """Failed ``gh pr list`` (non-zero returncode) is transient, not terminal.

    Current code does not check ``pr_result.returncode`` before calling
    ``json.loads(pr_result.stdout)``.  When returncode != 0 and stdout is
    empty, ``json.loads("")`` raises ``JSONDecodeError``.  Even if stdout
    happened to be ``"[]"`` on a non-zero exit, that must not be treated as
    "no open PR" (which would produce ``COMMITTED_NO_PR`` and trigger removal
    of ``agent-ready``).
    """

    def test_nonzero_returncode_empty_stdout_is_not_committed_no_pr(
        self,
    ) -> None:
        """Non-zero returncode + empty stdout: must not be COMMITTED_NO_PR.

        ``COMMITTED_NO_PR`` falsely signals "no PR exists, re-run" and
        would cause a duplicate dispatch.
        """
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = (
                list(_GIT_PREFIX_CLEAN)
                + [_completed(returncode=1, stdout="")] * 5
            )
            with patch(
                "baton_harness.after_run.time", create=True
            ) as _mt:
                _mt.sleep = MagicMock()
                try:
                    result = _classify()
                    assert result != RunOutcome.COMMITTED_NO_PR, (
                        "gh pr list returncode=1, stdout='' must not be "
                        "classified as COMMITTED_NO_PR"
                    )
                except json.JSONDecodeError:
                    pytest.fail(
                        "JSONDecodeError from empty stdout on non-zero exit; "
                        "returncode must be checked before json.loads"
                    )

    def test_nonzero_returncode_empty_stdout_does_not_remove_agent_ready(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Gh pr list failure: main() returns non-zero, no agent-ready removal.

        Ensures the returncode guard propagates all the way through main().
        """
        worktree = tmp_path / "feat-32-rc-check"
        worktree.mkdir()
        monkeypatch.chdir(worktree)
        monkeypatch.delenv("CHAIN_BASE_BRANCH", raising=False)

        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = (
                list(_GIT_PREFIX_CLEAN)
                + [_completed(returncode=1, stdout="")] * 5
            )
            with patch(
                "baton_harness.after_run.time", create=True
            ) as _mt:
                _mt.sleep = MagicMock()
                result = after_run.main()

        assert result != 0, (
            "main() must return non-zero when gh pr list fails with "
            "returncode=1"
        )
        all_cmd_lists = [c[0][0] for c in mock_run.call_args_list]
        remove_calls = [
            cmd
            for cmd in all_cmd_lists
            if "--remove-label" in cmd and "agent-ready" in cmd
        ]
        assert remove_calls == [], (
            "--remove-label agent-ready must not be called when gh pr list "
            "fails with returncode=1"
        )

    def test_nonzero_returncode_with_non_json_stderr_not_committed_no_pr(
        self,
    ) -> None:
        """Non-zero returncode with stderr error text: not COMMITTED_NO_PR.

        A gh auth / network error appears in stderr with a non-zero code; the
        empty stdout must not be misread as an empty PR list.
        """
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = list(_GIT_PREFIX_CLEAN) + [
                _completed(
                    returncode=1,
                    stdout="",
                    stderr="error: authentication required",
                )
            ] * 5
            with patch(
                "baton_harness.after_run.time", create=True
            ) as _mt:
                _mt.sleep = MagicMock()
                try:
                    result = _classify()
                    assert result != RunOutcome.COMMITTED_NO_PR
                except json.JSONDecodeError:
                    pytest.fail("JSONDecodeError on non-zero + empty stdout")


# ---------------------------------------------------------------------------
# Contract 3: bounded retry with recovery → success classification
# ---------------------------------------------------------------------------


class TestClassifyRetryWithRecovery:
    """Transient failures followed by a valid response → PR_OPENED.

    The retry mechanism must:
    - Attempt gh pr list at least 3 times.
    - Call ``time.sleep`` (or ``after_run.time.sleep``) between attempts.
    - Classify from the first successful attempt.
    """

    def test_transient_non_json_then_success_classifies_pr_opened(
        self,
    ) -> None:
        """First 2 gh pr list calls non-JSON, 3rd valid → PR_OPENED.

        The retry must exhaust transient failures and succeed on the valid
        third attempt.
        """
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = (
                list(_GIT_PREFIX_CLEAN)
                + [
                    _completed(stdout=_NON_JSON_BANNERS[0]),  # attempt 1 fail
                    _completed(stdout=_NON_JSON_BANNERS[1]),  # attempt 2 fail
                    _completed(stdout=_PR_JSON_OPEN),  # attempt 3 success
                ]
            )
            with patch(
                "baton_harness.after_run.time", create=True
            ) as _mt:
                _mt.sleep = MagicMock()
                result = _classify()

        assert result == RunOutcome.PR_OPENED, (
            "After 2 transient failures, a valid PR JSON on attempt 3 must "
            f"classify as PR_OPENED; got {result!r}"
        )

    def test_transient_nonzero_then_success_classifies_pr_opened(
        self,
    ) -> None:
        """First 2 gh pr list calls non-zero, 3rd valid → PR_OPENED."""
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = (
                list(_GIT_PREFIX_CLEAN)
                + [
                    _completed(returncode=1, stdout=""),  # attempt 1 fail
                    _completed(returncode=1, stdout=""),  # attempt 2 fail
                    _completed(stdout=_PR_JSON_OPEN),  # attempt 3 success
                ]
            )
            with patch(
                "baton_harness.after_run.time", create=True
            ) as _mt:
                _mt.sleep = MagicMock()
                result = _classify()

        assert result == RunOutcome.PR_OPENED, (
            "After 2 returncode=1 failures, a valid PR JSON on attempt 3 "
            f"must classify as PR_OPENED; got {result!r}"
        )

    def test_retry_calls_sleep_between_attempts(self) -> None:
        """time.sleep is called between retry attempts (backoff is active).

        The implementer is free to choose any backoff shape (fixed / linear /
        exponential) but MUST call ``time.sleep`` or an alias that delegates
        to it.  This test patches ``after_run.time.sleep`` — the implementer
        must import ``time`` into ``after_run`` and call ``time.sleep`` (or a
        module-level alias) so this patch point is valid.
        """
        sleep_mock = MagicMock()
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = (
                list(_GIT_PREFIX_CLEAN)
                + [
                    _completed(stdout=_NON_JSON_BANNERS[0]),  # attempt 1 fail
                    _completed(stdout=_PR_JSON_OPEN),  # attempt 2 success
                ]
            )
            with patch(
                "baton_harness.after_run.time", create=True
            ) as mock_time:
                mock_time.sleep = sleep_mock
                _classify()

        # At least one sleep call must have occurred (retry backoff)
        assert sleep_mock.call_count >= 1, (
            "time.sleep must be called at least once between retry attempts; "
            f"got {sleep_mock.call_count} calls"
        )

    def test_retry_count_at_least_3_before_giving_up(self) -> None:
        """Gh pr list is called at least 3 times before giving up.

        Pins the minimum retry count.  The implementer may exceed 3
        attempts but must not give up before the 3rd.
        """
        pr_list_call_count = 0

        def _tracking_side_effect(
            cmd: list[str],
        ) -> subprocess.CompletedProcess[str]:
            nonlocal pr_list_call_count
            if "gh" in cmd and "pr" in cmd and "list" in cmd:
                pr_list_call_count += 1
                return _completed(stdout=_NON_JSON_BANNERS[0])
            # Return appropriate defaults for other commands
            if "status" in cmd:
                return _completed(stdout="")
            if "rev-parse" in cmd and "--abbrev-ref" not in cmd:
                return _completed(stdout="abc123\n")
            if "cherry" in cmd:
                return _completed(stdout="+ deadbeef\n")
            if "--abbrev-ref" in cmd:
                return _completed(stdout="feat-32-guard\n")
            return _completed(stdout="")

        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = _tracking_side_effect
            with patch(
                "baton_harness.after_run.time", create=True
            ) as mock_time:
                mock_time.sleep = MagicMock()
                try:
                    _classify()
                except Exception:
                    pass  # We care about call count, not the exception

        assert pr_list_call_count >= 3, (
            f"gh pr list must be called at least 3 times before giving up; "
            f"got {pr_list_call_count} calls"
        )

    def test_success_on_first_attempt_no_sleep_needed(self) -> None:
        """No transient failure → classify succeeds without sleep.

        A clean first-attempt success must not incur unnecessary delay.
        This is the non-regression test for the happy path: the retry
        wrapper must only sleep when there is actually a transient failure.
        """
        sleep_mock = MagicMock()
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = (
                list(_GIT_PREFIX_CLEAN) + [_completed(stdout=_PR_JSON_OPEN)]
            )
            with patch(
                "baton_harness.after_run.time", create=True
            ) as mock_time:
                mock_time.sleep = sleep_mock
                result = _classify()

        assert result == RunOutcome.PR_OPENED
        assert sleep_mock.call_count == 0, (
            "time.sleep must not be called when the first gh pr list attempt "
            f"succeeds; got {sleep_mock.call_count} calls"
        )


# ---------------------------------------------------------------------------
# Contract 4 (optional, forward-spec): guarded _current_labels parse
# ---------------------------------------------------------------------------


class TestCurrentLabelsGuardedParse:
    """Non-JSON ``gh issue view`` stdout does not crash the run.

    This is marked as a forward-spec: the current implementation has an
    unguarded ``json.loads(result.stdout)`` at L251.  The test asserts the
    observable effect (no uncaught exception), leaving the recovery
    mechanism (return empty list, treat as transient, etc.) to the
    implementer.

    FORWARD-SPEC: exact recovery behavior is implementation-defined.
    Constraint: must not propagate ``JSONDecodeError`` or ``KeyError`` to
    the caller.
    """

    def test_non_json_gh_issue_view_does_not_raise(self) -> None:
        """Non-JSON gh issue view stdout must not propagate JSONDecodeError.

        Current code raises ``JSONDecodeError`` immediately.  After the fix
        this must be caught.
        """
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.return_value = _completed(
                stdout="<!DOCTYPE html>rate limited</html>"
            )
            try:
                _current_labels(42)
            except json.JSONDecodeError as exc:
                pytest.fail(
                    f"_current_labels raised JSONDecodeError on non-JSON "
                    f"gh issue view output: {exc}"
                )

    def test_nonzero_returncode_gh_issue_view_does_not_raise(self) -> None:
        """Non-zero returncode from gh issue view must not raise.

        The caller (_reconcile_labels) must be able to handle the degraded
        response (empty list, or similar) without an uncaught exception.
        """
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.return_value = _completed(
                returncode=1, stdout="", stderr="auth error"
            )
            try:
                result = _current_labels(42)
                # Forward-spec: graceful degradation yields some value, not
                # an exception. The exact value is implementation-defined;
                # a list (possibly empty) is the expected shape.
                assert isinstance(result, list), (
                    "_current_labels must return a list on degraded input; "
                    f"got {type(result)!r}"
                )
            except (json.JSONDecodeError, KeyError) as exc:
                pytest.fail(
                    f"_current_labels raised {type(exc).__name__} on "
                    f"non-zero returncode: {exc}"
                )
