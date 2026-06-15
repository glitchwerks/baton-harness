"""Tests for issue #32 — defensive hardening of after_run.py.

Pins the behavioral contract for:

1. Guarded ``json.loads`` in ``_classify``: non-JSON ``gh pr list`` output
   is transient, never terminal, and never tears ``agent-ready``.
2. Returncode-checked ``_classify``: a failed ``gh pr list`` (non-zero /
   empty stdout) is a transient error, NOT ``COMMITTED_NO_PR``.
3. Bounded retry with recovery: transient failures followed by a valid ``gh
   pr list`` response classify as ``PR_OPENED``.
4. Guarded ``_current_labels`` parse: a labels-fetch failure must cause
   ``_reconcile_labels`` to abort with ZERO label mutations and a non-zero
   ``main()`` result.  The single-state-invariant must not be violated.
5. Returncode-checked ``git cherry``: a non-zero ``git cherry`` (empty
   stdout) must NOT classify as ``NO_COMMITS``; it is treated as a
   transient error (non-terminal, no ``agent-ready`` removal).

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

Single-state-invariant contract (for Phase 2 implementer — MAJOR 2):
    - ``_current_labels`` failure (non-JSON stdout OR non-zero returncode)
      must cause ``_reconcile_labels`` to issue ZERO ``gh issue edit`` calls
      (no ``--remove-label`` and no ``--add-label``) and ``main()`` must
      return non-zero.
    - The mechanism (sentinel return, controlled exception, etc.) is the
      implementer's choice; the tests assert observable behaviour only.

git cherry returncode contract (for Phase 2 implementer — MAJOR 1):
    - A non-zero ``git cherry`` exit (e.g. bad base SHA, detached HEAD)
      must NOT classify as ``NO_COMMITS`` (which would falsely signal "no
      work done" and trigger ``agent-ready`` removal via the blocked path).
    - The result must be treated as a transient error: non-terminal, with
      no ``agent-ready`` removal, and ``main()`` returns non-zero.
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
            mock_run.side_effect = list(_GIT_PREFIX_CLEAN) + [
                _completed(stdout=_NON_JSON_BANNERS[0]),
                _completed(stdout=_NON_JSON_BANNERS[0]),
                _completed(stdout=_NON_JSON_BANNERS[0]),
                _completed(stdout=_NON_JSON_BANNERS[0]),
                _completed(stdout=_NON_JSON_BANNERS[0]),
            ]
            with patch("baton_harness.after_run.time", create=True) as _mt:
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
            with patch("baton_harness.after_run.time", create=True) as _mt:
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
            with patch("baton_harness.after_run.time", create=True) as _mt:
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
            with patch("baton_harness.after_run.time", create=True) as _mt:
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
            mock_run.side_effect = (
                list(_GIT_PREFIX_CLEAN)
                + [
                    _completed(
                        returncode=1,
                        stdout="",
                        stderr="error: authentication required",
                    )
                ]
                * 5
            )
            with patch("baton_harness.after_run.time", create=True) as _mt:
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
            mock_run.side_effect = list(_GIT_PREFIX_CLEAN) + [
                _completed(stdout=_NON_JSON_BANNERS[0]),  # attempt 1 fail
                _completed(stdout=_NON_JSON_BANNERS[1]),  # attempt 2 fail
                _completed(stdout=_PR_JSON_OPEN),  # attempt 3 success
            ]
            with patch("baton_harness.after_run.time", create=True) as _mt:
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
            mock_run.side_effect = list(_GIT_PREFIX_CLEAN) + [
                _completed(returncode=1, stdout=""),  # attempt 1 fail
                _completed(returncode=1, stdout=""),  # attempt 2 fail
                _completed(stdout=_PR_JSON_OPEN),  # attempt 3 success
            ]
            with patch("baton_harness.after_run.time", create=True) as _mt:
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
            mock_run.side_effect = list(_GIT_PREFIX_CLEAN) + [
                _completed(stdout=_NON_JSON_BANNERS[0]),  # attempt 1 fail
                _completed(stdout=_PR_JSON_OPEN),  # attempt 2 success
            ]
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
            mock_run.side_effect = list(_GIT_PREFIX_CLEAN) + [
                _completed(stdout=_PR_JSON_OPEN)
            ]
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
# Contract 4: _current_labels failure → _reconcile_labels aborts,
# zero label mutations, main() non-zero  (MAJOR 2 — single-state invariant)
# ---------------------------------------------------------------------------


class TestCurrentLabelsGuardedParse:
    """Labels-fetch failure → zero label mutations + non-zero main().

    MAJOR 2 (Codex review): the current implementation returns ``[]`` on
    both non-JSON and non-zero-returncode ``gh issue view``.  That empty
    list is misread by ``_reconcile_labels`` as "issue has no labels":
      - The fast-path ``blocked`` label check is skipped.
      - Priority 3 falls through to remove ``agent-ready`` and add
        ``blocked`` — but ``agent-ready`` WAS on the issue; the removal
        never happened, leaving BOTH labels present (violates single-state
        invariant).

    Correct contract:
        A ``_current_labels`` failure must cause ``_reconcile_labels`` to
        abort with ZERO ``gh issue edit`` calls.  ``main()`` must return
        non-zero.  The mechanism is the implementer's choice.

    These tests assert OBSERVABLE behaviour only (zero mutations + non-zero
    main()); they do NOT constrain the internal return type of
    ``_current_labels``.

    The tests in this class that assert zero label mutations + non-zero
    main() are FORWARD-SPEC (RED until Phase 2 implementation lands) —
    the current code returns ``[]`` and allows mutations to proceed.
    The no-crash tests are also FORWARD-SPEC for the same reason.
    """

    # --- No-crash guards (assert _current_labels itself does not raise) ---

    def test_non_json_gh_issue_view_does_not_raise(self) -> None:
        """Non-JSON gh issue view stdout must not propagate JSONDecodeError.

        Asserts the internal function does not raise; the observable effect
        on the label-mutation contract is asserted in the main() tests below.
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

        The no-crash contract: ``_current_labels`` must not propagate
        ``JSONDecodeError`` or ``KeyError``.  Observable mutation contract
        is asserted in the main() tests below.
        """
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.return_value = _completed(
                returncode=1, stdout="", stderr="auth error"
            )
            try:
                _current_labels(42)
            except (json.JSONDecodeError, KeyError) as exc:
                pytest.fail(
                    f"_current_labels raised {type(exc).__name__} on "
                    f"non-zero returncode: {exc}"
                )

    # --- Observable-contract guards (FORWARD-SPEC — RED until Phase 2) ---
    # These assert the load-bearing single-state-invariant behaviour:
    # a labels-fetch failure must produce ZERO label mutations and non-zero
    # main(), regardless of what outcome _classify() would have returned.

    def test_non_json_labels_fetch_zero_label_mutations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-JSON gh issue view → zero gh issue edit calls issued.

        Arrange: git prefix clean with commits ahead + open PR (would
        normally trigger remove-agent-ready + add-agent-done), but
        gh issue view returns a non-JSON rate-limit banner.
        Assert: NO ``gh issue edit`` calls of any kind.

        FORWARD-SPEC: RED until Phase 2 — current code proceeds with
        mutations using the degraded ``[]`` label list.
        """
        worktree = tmp_path / "feat-32-labels-guard-nonjson"
        worktree.mkdir()
        monkeypatch.chdir(worktree)
        monkeypatch.delenv("CHAIN_BASE_BRANCH", raising=False)

        with patch("baton_harness.after_run._run") as mock_run:
            # _classify: git status, git rev-parse, git cherry (ahead),
            #            git rev-parse --abbrev-ref (branch), gh pr list (ok)
            # _current_labels: gh issue view → non-JSON
            # Extra completeds absorb any gh issue edit calls that current
            # (unfixed) code makes — so mock doesn't StopIterate before we
            # reach the assertion; the assertion itself catches the violation.
            mock_run.side_effect = [
                _completed(stdout=""),  # git status — clean
                _completed(stdout="abc123\n"),  # git rev-parse base SHA
                _completed(stdout="+ deadbeef\n"),  # git cherry — 1 ahead
                _completed(stdout="feat-32-guard\n"),  # branch name
                _completed(stdout=_PR_JSON_OPEN),  # gh pr list — has PR
                # gh issue view — non-JSON (rate-limit banner)
                _completed(stdout="<!DOCTYPE html>rate limited</html>"),
                # Absorb any spurious gh issue edit calls from unfixed code:
                _completed(stdout=""),
                _completed(stdout=""),
            ]
            with patch("baton_harness.after_run.time", create=True) as _mt:
                _mt.sleep = MagicMock()
                result = after_run.main()

        # Non-zero: labels-fetch failure must surface as hook failure.
        assert result != 0, (
            "main() must return non-zero when gh issue view returns non-JSON"
        )

        # Zero label mutations: no gh issue edit must be called at all.
        all_cmd_lists = [c[0][0] for c in mock_run.call_args_list]
        issue_edit_calls = [
            cmd for cmd in all_cmd_lists if "issue" in cmd and "edit" in cmd
        ]
        assert issue_edit_calls == [], (
            "gh issue edit must NOT be called when gh issue view returns "
            "non-JSON — zero label mutations required to preserve the "
            "single-state invariant. "
            f"Got calls: {issue_edit_calls}"
        )

    def test_nonzero_labels_fetch_zero_label_mutations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-zero gh issue view returncode → zero gh issue edit calls.

        Arrange: git prefix clean with commits ahead + open PR (would
        normally trigger remove-agent-ready + add-agent-done), but
        gh issue view returns returncode=1 with empty stdout.
        Assert: NO ``gh issue edit`` calls of any kind.

        FORWARD-SPEC: RED until Phase 2 — current code proceeds with
        mutations using the degraded ``[]`` label list.
        """
        worktree = tmp_path / "feat-32-labels-guard-rc"
        worktree.mkdir()
        monkeypatch.chdir(worktree)
        monkeypatch.delenv("CHAIN_BASE_BRANCH", raising=False)

        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout=""),  # git status — clean
                _completed(stdout="abc123\n"),  # git rev-parse base SHA
                _completed(stdout="+ deadbeef\n"),  # git cherry — 1 ahead
                _completed(stdout="feat-32-guard\n"),  # branch name
                _completed(stdout=_PR_JSON_OPEN),  # gh pr list — has PR
                # gh issue view — non-zero returncode
                _completed(returncode=1, stdout="", stderr="auth error"),
                # Absorb any spurious gh issue edit calls from unfixed code:
                _completed(stdout=""),
                _completed(stdout=""),
            ]
            with patch("baton_harness.after_run.time", create=True) as _mt:
                _mt.sleep = MagicMock()
                result = after_run.main()

        assert result != 0, (
            "main() must return non-zero when gh issue view fails with "
            "returncode=1"
        )

        all_cmd_lists = [c[0][0] for c in mock_run.call_args_list]
        issue_edit_calls = [
            cmd for cmd in all_cmd_lists if "issue" in cmd and "edit" in cmd
        ]
        assert issue_edit_calls == [], (
            "gh issue edit must NOT be called when gh issue view fails "
            "with returncode=1 — zero label mutations required. "
            f"Got calls: {issue_edit_calls}"
        )

    def test_non_json_labels_fetch_no_remove_label(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-JSON gh issue view: no --remove-label call emitted.

        Specifically checks the --remove-label vector, which would be the
        most likely mutation to occur (Priority 3 path, or PR path both
        start with remove-agent-ready).

        FORWARD-SPEC: RED until Phase 2.
        """
        worktree = tmp_path / "feat-32-labels-no-remove"
        worktree.mkdir()
        monkeypatch.chdir(worktree)
        monkeypatch.delenv("CHAIN_BASE_BRANCH", raising=False)

        with patch("baton_harness.after_run._run") as mock_run:
            # Use NO_COMMITS path (no commits ahead) so _reconcile_labels
            # is called for a non-TRANSIENT_ERROR outcome — the current
            # Priority 3 path would normally remove agent-ready + add blocked.
            # Absorb spurious label-mutation calls from unfixed code so the
            # assertion (not StopIteration) is the failure signal.
            mock_run.side_effect = [
                _completed(stdout=""),  # git status — clean
                _completed(stdout="abc123\n"),  # git rev-parse base SHA
                _completed(stdout=""),  # git cherry — nothing ahead
                # gh issue view — non-JSON
                _completed(stdout="error: HTTP 429: too many requests"),
                # Absorb any gh issue edit calls from unfixed code:
                _completed(stdout=""),
                _completed(stdout=""),
            ]
            with patch("baton_harness.after_run.time", create=True) as _mt:
                _mt.sleep = MagicMock()
                result = after_run.main()

        assert result != 0, (
            "main() must return non-zero when gh issue view is non-JSON "
            "(even on a NO_COMMITS outcome)"
        )

        all_cmd_lists = [c[0][0] for c in mock_run.call_args_list]
        remove_calls = [
            cmd for cmd in all_cmd_lists if "--remove-label" in cmd
        ]
        assert remove_calls == [], (
            "--remove-label must NOT be called when gh issue view returns "
            "non-JSON — zero mutations required to guard single-state "
            f"invariant. Got: {remove_calls}"
        )

    def test_non_json_labels_fetch_no_add_label(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-JSON gh issue view: no --add-label call emitted.

        FORWARD-SPEC: RED until Phase 2.
        """
        worktree = tmp_path / "feat-32-labels-no-add"
        worktree.mkdir()
        monkeypatch.chdir(worktree)
        monkeypatch.delenv("CHAIN_BASE_BRANCH", raising=False)

        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout=""),  # git status — clean
                _completed(stdout="abc123\n"),  # git rev-parse base SHA
                _completed(stdout=""),  # git cherry — nothing ahead
                # gh issue view — non-JSON
                _completed(
                    stdout="rate limit exceeded; retry after 00:00:00Z"
                ),
                # Absorb any gh issue edit calls from unfixed code:
                _completed(stdout=""),
                _completed(stdout=""),
            ]
            with patch("baton_harness.after_run.time", create=True) as _mt:
                _mt.sleep = MagicMock()
                result = after_run.main()

        assert result != 0, (
            "main() must return non-zero when gh issue view returns non-JSON"
        )

        all_cmd_lists = [c[0][0] for c in mock_run.call_args_list]
        add_calls = [cmd for cmd in all_cmd_lists if "--add-label" in cmd]
        assert add_calls == [], (
            "--add-label must NOT be called when gh issue view returns "
            "non-JSON — zero mutations required. "
            f"Got: {add_calls}"
        )


# ---------------------------------------------------------------------------
# Contract 5: git cherry returncode must be checked  (MAJOR 1)
# ---------------------------------------------------------------------------


class TestClassifyGitCherryReturncodeChecked:
    """Non-zero ``git cherry`` exit is transient, NOT ``NO_COMMITS``.

    MAJOR 1 (Codex review): AC for issue #32 states "_classify checks
    returncode before trusting git cherry / gh pr list."  The ``gh pr list``
    side is already hardened.  The ``git cherry`` side is not: a non-zero
    exit (e.g. detached HEAD, bad base SHA, git error) produces empty stdout,
    ``ahead_commits`` is ``[]``, and ``NO_COMMITS`` is returned — a
    misclassification that triggers ``agent-ready`` removal and ``blocked``
    label addition.

    Correct contract:
        A non-zero ``git cherry`` returncode must NOT yield ``NO_COMMITS``.
        The outcome must be treated as transient (non-terminal).  ``main()``
        must return non-zero.  No ``agent-ready`` removal or ``blocked``
        addition may occur.

    All tests in this class are FORWARD-SPEC (RED until Phase 2) because the
    current implementation does not check ``cherry.returncode``.

    Side-effect sequence note:
        ``_classify`` issues these ``_run`` calls in order:
          1. ``git status`` (Step 1 — uncommitted check)
          2. ``git rev-parse <base>`` (inside ``_resolve_base_sha``)
          3. ``git cherry <base_sha> HEAD`` (Step 2 — commits-ahead check)
          4. ``git rev-parse --abbrev-ref HEAD`` (``_current_branch``)
          5. ``gh pr list ...`` (Step 3, repeated up to MAX_ATTEMPTS)
        A non-zero cherry at position 3 must short-circuit before positions
        4 and 5.
    """

    def test_nonzero_cherry_is_not_no_commits(self) -> None:
        """Git cherry non-zero + empty stdout must NOT yield NO_COMMITS.

        ``NO_COMMITS`` would trigger the blocked path (remove agent-ready +
        add blocked).  A failed cherry must be treated as non-terminal.

        FORWARD-SPEC: RED until Phase 2 — current code returns NO_COMMITS.
        """
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout=""),  # git status — clean
                _completed(stdout="abc123\n"),  # git rev-parse base SHA
                # git cherry — non-zero (e.g. bad ref, detached HEAD)
                _completed(returncode=128, stdout="", stderr="bad object"),
                # Provide extras so _current_labels / subsequent calls don't
                # StopIterate before we reach the assertion.
                _completed(stdout=""),
                _completed(stdout=""),
            ]
            result = _classify()
            assert result != RunOutcome.NO_COMMITS, (
                "git cherry returncode=128 with empty stdout must NOT "
                "be classified as NO_COMMITS — that would remove "
                "agent-ready and add blocked incorrectly. "
                f"Got: {result!r}"
            )

    def test_nonzero_cherry_does_not_remove_agent_ready(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-zero git cherry: main() non-zero, no agent-ready removal.

        The load-bearing safety test: a git error must not cause the hook to
        remove ``agent-ready`` (which would make the issue ineligible for
        retry and lose the work).

        FORWARD-SPEC: RED until Phase 2.
        """
        worktree = tmp_path / "feat-32-cherry-rc"
        worktree.mkdir()
        monkeypatch.chdir(worktree)
        monkeypatch.delenv("CHAIN_BASE_BRANCH", raising=False)

        with patch("baton_harness.after_run._run") as mock_run:
            # Provide extra completeds to absorb the gh issue view call and
            # any label-mutation calls that unfixed code makes via the
            # NO_COMMITS / _current_labels path; the assertion catches the
            # violation rather than a StopIteration from the mock.
            mock_run.side_effect = [
                _completed(stdout=""),  # git status — clean
                _completed(stdout="abc123\n"),  # git rev-parse base SHA
                # git cherry — non-zero (fatal: bad object)
                _completed(returncode=128, stdout="", stderr="bad object"),
                # Absorb gh issue view + label mutations from unfixed code:
                _completed(stdout='{"labels":[]}'),
                _completed(stdout=""),
                _completed(stdout=""),
            ]
            with patch("baton_harness.after_run.time", create=True) as _mt:
                _mt.sleep = MagicMock()
                result = after_run.main()

        assert result != 0, (
            "main() must return non-zero when git cherry fails with "
            "returncode=128"
        )

        all_cmd_lists = [c[0][0] for c in mock_run.call_args_list]
        remove_agent_ready = [
            cmd
            for cmd in all_cmd_lists
            if "--remove-label" in cmd and "agent-ready" in cmd
        ]
        assert remove_agent_ready == [], (
            "--remove-label agent-ready must NOT be called when git cherry "
            "returns non-zero — issue must stay eligible for future run. "
            f"Got: {remove_agent_ready}"
        )

    def test_nonzero_cherry_no_label_mutations_at_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-zero git cherry: no gh issue edit calls of any kind.

        Confirms both the remove-label and add-label vectors are blocked.

        FORWARD-SPEC: RED until Phase 2.
        """
        worktree = tmp_path / "feat-32-cherry-no-mutations"
        worktree.mkdir()
        monkeypatch.chdir(worktree)
        monkeypatch.delenv("CHAIN_BASE_BRANCH", raising=False)

        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout=""),  # git status — clean
                _completed(stdout="abc123\n"),  # git rev-parse base SHA
                _completed(returncode=128, stdout="", stderr="bad object"),
                # Absorb gh issue view + label mutations from unfixed code:
                _completed(stdout='{"labels":[]}'),
                _completed(stdout=""),
                _completed(stdout=""),
            ]
            with patch("baton_harness.after_run.time", create=True) as _mt:
                _mt.sleep = MagicMock()
                after_run.main()

        all_cmd_lists = [c[0][0] for c in mock_run.call_args_list]
        issue_edit_calls = [
            cmd for cmd in all_cmd_lists if "issue" in cmd and "edit" in cmd
        ]
        assert issue_edit_calls == [], (
            "gh issue edit must NOT be called when git cherry returns "
            "non-zero (any returncode != 0). "
            f"Got: {issue_edit_calls}"
        )

    def test_nonzero_cherry_returncode_1_is_also_guarded(self) -> None:
        """returncode=1 from git cherry must not yield NO_COMMITS.

        Covers returncode=1 (e.g. git exits 1 on some errors); the
        guard must apply to any non-zero exit, not just fatal-level 128.

        FORWARD-SPEC: RED until Phase 2.
        """
        with patch("baton_harness.after_run._run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout=""),  # git status — clean
                _completed(stdout="abc123\n"),  # git rev-parse base SHA
                _completed(returncode=1, stdout="", stderr="error"),
                # Extras so downstream calls don't StopIterate.
                _completed(stdout=""),
                _completed(stdout=""),
            ]
            result = _classify()
            assert result != RunOutcome.NO_COMMITS, (
                "git cherry returncode=1 must not be classified as "
                f"NO_COMMITS; got {result!r}"
            )
