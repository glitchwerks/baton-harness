"""Unit tests for the merge-denial probe assertion helper (slice 3c, #160).

Tests the ``scripts.probe_assert`` module that ``bin/probe-merge-denial.sh``
calls via ``python -m scripts.probe_assert``.  All 7 bypass vectors are
covered at the assertion-logic level; no live API calls are made.

Approach B was chosen over Approach A (fake_gh subprocess shim) because:
- The extractable logic is the *response-parsing and assertion functions*,
  not the argument-construction that varies by vector.
- Testing via pure Python is ~10x faster than launching subprocesses and
  requires no additional fixture files.
- The fake_gh shim pattern is already exercised by
  ``test_provision_ruleset_idempotent.py``; duplicating it here adds
  infrastructure cost without new coverage.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Import the helper as a module.  ``scripts/`` is not a package (no
# __init__.py), so we insert the repo root onto sys.path and import directly.
_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import scripts.probe_assert as pa  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_stdout(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:  # type: ignore[type-arg]
    """Capture stdout from a probe_assert call and parse the JSON line.

    Args:
        capsys: pytest capsys fixture.

    Returns:
        Parsed dict with ``ok`` and ``reason`` keys.
    """
    out, _ = capsys.readouterr()
    return json.loads(out.strip())


# ---------------------------------------------------------------------------
# check_exit_code
# ---------------------------------------------------------------------------


class TestCheckExitCode:
    """Tests for ``check_exit_code``."""

    def test_nonzero_actual_returns_pass(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Any non-zero exit code is a denial (PASS)."""
        rc = pa.check_exit_code(2, 2)
        assert rc == 0
        result = _parse_stdout(capsys)
        assert result["ok"] is True
        assert "denied" in str(result["reason"]).lower()

    def test_zero_actual_returns_fail(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Exit code 0 means merge succeeded — a probe FAIL."""
        rc = pa.check_exit_code(2, 0)
        assert rc == 1
        result = _parse_stdout(capsys)
        assert result["ok"] is False
        assert "0" in str(result["reason"])

    def test_any_nonzero_counts_as_denied(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Exit code 1 (curl denied) is also a PASS even if expected=2."""
        rc = pa.check_exit_code(2, 1)
        assert rc == 0
        result = _parse_stdout(capsys)
        assert result["ok"] is True

    def test_exit_code_127_is_denied(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Exit code 127 (command not found) counts as denied."""
        rc = pa.check_exit_code(1, 127)
        assert rc == 0
        result = _parse_stdout(capsys)
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# check_http_403
# ---------------------------------------------------------------------------


class TestCheckHttp403:
    """Tests for ``check_http_403``."""

    def test_both_markers_present_returns_pass(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Response containing 403 and ruleset name → PASS."""
        body = (
            "gh: PUT https://api.github.com/repos/o/r/pulls/1/merge: "
            "403 Forbidden\n"
            '{"message":"Required status check \\"harness-main-no-merge\\" '
            'is expected."}'
        )
        rc = pa.check_http_403(body)
        assert rc == 0
        result = _parse_stdout(capsys)
        assert result["ok"] is True

    def test_missing_403_returns_fail(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Response without 403 → FAIL."""
        body = '{"message":"harness-main-no-merge but status 200 somehow"}'
        rc = pa.check_http_403(body)
        assert rc == 1
        result = _parse_stdout(capsys)
        assert result["ok"] is False
        assert "403" in str(result["reason"])

    def test_missing_ruleset_name_returns_fail(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Response with 403 but no ruleset name → FAIL."""
        body = "gh: 403 some other reason"
        rc = pa.check_http_403(body)
        assert rc == 1
        result = _parse_stdout(capsys)
        assert result["ok"] is False
        assert "harness-main-no-merge" in str(result["reason"])

    def test_empty_body_returns_fail(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Empty response body → FAIL."""
        rc = pa.check_http_403("")
        assert rc == 1
        result = _parse_stdout(capsys)
        assert result["ok"] is False

    def test_200_success_body_returns_fail(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A 200 merge-succeeded body (unexpected success) → FAIL."""
        body = (
            '{"sha":"abc123","merged":true,'
            '"message":"Pull Request successfully merged"}'
        )
        rc = pa.check_http_403(body)
        assert rc == 1
        result = _parse_stdout(capsys)
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# check_sentinel
# ---------------------------------------------------------------------------


class TestCheckSentinel:
    """Tests for ``check_sentinel``."""

    def test_sentinel_present_returns_pass(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Sentinel file exists → PASS."""
        state_dir = tmp_path / ".bh-state"
        state_dir.mkdir()
        (state_dir / "worker-tried-merge").touch()
        rc = pa.check_sentinel(str(state_dir))
        assert rc == 0
        result = _parse_stdout(capsys)
        assert result["ok"] is True

    def test_sentinel_missing_returns_fail(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Sentinel file absent → FAIL."""
        state_dir = tmp_path / ".bh-state"
        state_dir.mkdir()
        rc = pa.check_sentinel(str(state_dir))
        assert rc == 1
        result = _parse_stdout(capsys)
        assert result["ok"] is False
        assert "NOT found" in str(result["reason"])

    def test_state_dir_missing_entirely_returns_fail(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """If .bh-state dir doesn't exist, sentinel is absent → FAIL."""
        state_dir = tmp_path / ".bh-state"
        # Do NOT create the dir.
        rc = pa.check_sentinel(str(state_dir))
        assert rc == 1
        result = _parse_stdout(capsys)
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# check_stderr_marker
# ---------------------------------------------------------------------------


class TestCheckStderrMarker:
    """Tests for ``check_stderr_marker``."""

    def test_marker_present_returns_pass(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """BH_WORKER_TRIED_MERGE: in stderr → PASS."""
        stderr = (
            "BH_WORKER_TRIED_MERGE: tool=Bash "
            "matched_pattern=gh-pr-merge command=gh pr merge 42"
        )
        rc = pa.check_stderr_marker(stderr)
        assert rc == 0
        result = _parse_stdout(capsys)
        assert result["ok"] is True

    def test_marker_absent_returns_fail(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No marker in stderr → FAIL."""
        rc = pa.check_stderr_marker("some unrelated output")
        assert rc == 1
        result = _parse_stdout(capsys)
        assert result["ok"] is False
        assert "NOT found" in str(result["reason"])

    def test_empty_stderr_returns_fail(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Empty stderr → FAIL."""
        rc = pa.check_stderr_marker("")
        assert rc == 1
        result = _parse_stdout(capsys)
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# summarise
# ---------------------------------------------------------------------------


class TestSummarise:
    """Tests for ``summarise``."""

    def test_all_pass_returns_ok(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """7/7 passing → PASS (exit 0)."""
        rc = pa.summarise(7, 7, 0)
        assert rc == 0
        result = _parse_stdout(capsys)
        assert result["ok"] is True
        assert "7/7" in str(result["reason"])

    def test_any_fail_returns_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """1 fail → FAIL (exit 1)."""
        rc = pa.summarise(7, 6, 1)
        assert rc == 1
        result = _parse_stdout(capsys)
        assert result["ok"] is False
        assert "FAIL" in str(result["reason"]).upper() or "fail" in str(
            result["reason"]
        )

    def test_all_fail_returns_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """7/7 failing → FAIL (exit 1)."""
        rc = pa.summarise(7, 0, 7)
        assert rc == 1
        result = _parse_stdout(capsys)
        assert result["ok"] is False

    def test_partial_pass_is_fail(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """3 vectors PASS, 4 FAIL → overall FAIL."""
        rc = pa.summarise(7, 3, 4)
        assert rc == 1
        result = _parse_stdout(capsys)
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# CLI dispatch (main function)
# ---------------------------------------------------------------------------


class TestCLIDispatch:
    """Tests for the ``main()`` CLI entry point."""

    def test_cli_check_exit_code_denied(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """CLI: check_exit_code with denied command (exit!=0) → exit 0."""
        rc = pa.main(["check_exit_code", "2", "2"])
        assert rc == 0
        result = _parse_stdout(capsys)
        assert result["ok"] is True

    def test_cli_check_http_403_passes(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """CLI: check_http_403 with valid denial body → exit 0."""
        body = "403 harness-main-no-merge"
        rc = pa.main(["check_http_403", body])
        assert rc == 0
        result = _parse_stdout(capsys)
        assert result["ok"] is True

    def test_cli_summarise_all_pass(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """CLI: summarise 7 7 0 → exit 0."""
        rc = pa.main(["summarise", "7", "7", "0"])
        assert rc == 0
        result = _parse_stdout(capsys)
        assert result["ok"] is True
