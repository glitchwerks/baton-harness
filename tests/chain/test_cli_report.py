"""Unit tests for the ``--report`` CLI flag (issue #303, Phase 3 of #243).

Coverage:
- ``--report /absolute/path.json`` is forwarded to ``run_daemon`` as
  ``report_path=`` with the exact absolute path given.
- ``--report relative/path.json`` is resolved to absolute BEFORE the
  daemon's ``os.chdir`` into the managed repo root -- i.e. relative to
  the original process cwd, not the post-chdir cwd. Mirrors the
  pre-chdir resolution convention already pinned for ``--workflow`` by
  ``test_cli.py::test_main_workflow_path_resolved_absolute_before_chdir``.
- ``--report`` omitted entirely defaults to
  ``${BH_PROJECT_ROOT}/.baton-harness/session-report.json``, still
  forwarded as an absolute ``report_path=``.
- The new default ``report_path=`` kwarg coexists with the other
  kwargs ``run_daemon`` already receives (``once``, ``poll_interval_s``)
  without disrupting them -- a forward-compatibility regression guard
  for the existing ``test_cli.py`` assertions on those kwargs.

Per the plan's resolved design decision (#243 plan Sec.8, decision 3),
the daemon ALWAYS writes a report -- ``--report`` only overrides the
write location. There is therefore no "report disabled" case to test.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from baton_harness.chain.cli import main

# ---------------------------------------------------------------------------
# Autouse fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _auto_patch_pre_bootstrap_gate() -> Iterator[None]:
    """No-op ``doctor.run_gate`` for daemon-path tests that don't test it.

    Mirrors the identically-named fixture in ``test_cli.py`` -- see that
    file's docstring for why this must not live in the shared
    ``chain/conftest.py`` autouse set.
    """
    with patch("baton_harness.chain.doctor.run_gate", return_value=None):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_main(*args: str) -> int:
    """Run ``main`` with the given argv and return the exit code.

    Args:
        *args: Command-line arguments to pass to ``main``.

    Returns:
        The integer exit code returned by ``main``.
    """
    return main(list(args))


# ---------------------------------------------------------------------------
# --report with an absolute path
# ---------------------------------------------------------------------------


def test_report_absolute_path_forwarded_to_run_daemon(tmp_path: Path) -> None:
    """An absolute ``--report`` path is forwarded to run_daemon unchanged.

    Once made absolute (it already is), the resolved path passed to
    ``run_daemon(..., report_path=...)`` must match exactly.
    """
    report_path_arg = tmp_path / "reports" / "out.json"
    fake_repo_cfg = MagicMock()
    fake_repo_cfg.project_root = tmp_path / "project_root"

    run_daemon_kwargs: dict[str, object] = {}

    async def fake_run_daemon(*args: object, **kwargs: object) -> None:
        run_daemon_kwargs.update(kwargs)

    with (
        patch(
            "baton_harness.chain.cli.bootstrap_secrets",
            return_value="ghs_TESTTOKEN_xxxxxxx",
        ),
        patch("baton_harness.chain.cli.validate_daemon_token"),
        patch(
            "baton_harness.chain.cli.load_workflow",
            return_value=MagicMock(),
        ),
        patch(
            "baton_harness.chain.cli.load_registry",
            return_value=[fake_repo_cfg],
        ),
        patch(
            "baton_harness.chain.cli.run_daemon",
            side_effect=fake_run_daemon,
        ),
        patch("baton_harness.chain.cli.os.chdir"),
        patch("baton_harness.chain.cli.os.path.isdir", return_value=True),
    ):
        result = _run_main("--once", "--report", str(report_path_arg))

    assert result == 0, f"Expected exit 0, got {result}"
    forwarded = run_daemon_kwargs.get("report_path")
    assert forwarded == report_path_arg.resolve(), (
        f"Expected report_path={report_path_arg.resolve()!r}, "
        f"got {forwarded!r}"
    )
    assert forwarded is not None and Path(forwarded).is_absolute(), (
        f"report_path must be absolute; got {forwarded!r}"
    )


# ---------------------------------------------------------------------------
# --report with a relative path: resolved BEFORE chdir
# ---------------------------------------------------------------------------


def test_report_relative_path_resolved_before_chdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative ``--report`` path resolves against the ORIGINAL cwd.

    This is the trickiest case: ``--report relative/path.json`` must be
    made absolute before the daemon's ``os.chdir(project_root)`` call, so
    it must resolve relative to the cwd the process was launched from --
    not relative to the managed-repo root the daemon chdirs into.

    A real ``os.chdir`` is wired through the patched
    ``baton_harness.chain.cli.os.chdir`` (rather than a no-op mock, as
    other tests use) so the process cwd genuinely changes partway through
    ``main()``, letting this test distinguish "resolved before chdir"
    from "resolved after chdir" -- the two would only produce the same
    absolute path by coincidence.
    """
    original_cwd = tmp_path / "original_cwd"
    original_cwd.mkdir()
    project_root = tmp_path / "project_root"
    project_root.mkdir()
    monkeypatch.chdir(original_cwd)

    # Capture the real os.chdir BEFORE it gets patched below, so the
    # side_effect can perform a genuine chdir without recursing into the
    # patched mock.
    _real_chdir = os.chdir

    def _do_real_chdir(path: object) -> None:
        _real_chdir(path)

    fake_repo_cfg = MagicMock()
    fake_repo_cfg.project_root = project_root

    run_daemon_kwargs: dict[str, object] = {}

    async def fake_run_daemon(*args: object, **kwargs: object) -> None:
        run_daemon_kwargs.update(kwargs)

    relative_report = Path("relative") / "report.json"

    with (
        patch(
            "baton_harness.chain.cli.bootstrap_secrets",
            return_value="ghs_TESTTOKEN_xxxxxxx",
        ),
        patch("baton_harness.chain.cli.validate_daemon_token"),
        patch(
            "baton_harness.chain.cli.load_workflow",
            return_value=MagicMock(),
        ),
        patch(
            "baton_harness.chain.cli.load_registry",
            return_value=[fake_repo_cfg],
        ),
        patch(
            "baton_harness.chain.cli.run_daemon",
            side_effect=fake_run_daemon,
        ),
        patch(
            "baton_harness.chain.cli.os.chdir",
            side_effect=_do_real_chdir,
        ),
        patch("baton_harness.chain.cli.os.path.isdir", return_value=True),
    ):
        result = _run_main("--once", "--report", str(relative_report))

    assert result == 0, f"Expected exit 0, got {result}"
    expected = (original_cwd / relative_report).resolve()
    forwarded = run_daemon_kwargs.get("report_path")
    assert forwarded == expected, (
        f"Expected report_path resolved against the ORIGINAL cwd "
        f"({original_cwd!r}) to give {expected!r}; got {forwarded!r}. "
        "If this instead matches "
        f"{(project_root / relative_report).resolve()!r}, the path was "
        "resolved AFTER chdir into the managed repo -- resolution must "
        "happen before chdir, mirroring the --workflow convention."
    )


# ---------------------------------------------------------------------------
# --report omitted: default path under BH_PROJECT_ROOT
# ---------------------------------------------------------------------------


def test_report_omitted_defaults_to_bh_project_root_session_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting ``--report`` uses the ``BH_PROJECT_ROOT`` default path.

    Defaults to ``${BH_PROJECT_ROOT}/.baton-harness/session-report.json``.
    ``BH_PROJECT_ROOT`` is set to the same directory used as the
    registry's ``project_root`` so the assertion holds regardless of
    whether the implementer sources the default from the env var
    directly or from the loaded registry entry -- both must agree.
    """
    project_root = tmp_path / "project_root"
    project_root.mkdir()
    monkeypatch.setenv("BH_PROJECT_ROOT", str(project_root))

    fake_repo_cfg = MagicMock()
    fake_repo_cfg.project_root = project_root

    run_daemon_kwargs: dict[str, object] = {}

    async def fake_run_daemon(*args: object, **kwargs: object) -> None:
        run_daemon_kwargs.update(kwargs)

    with (
        patch(
            "baton_harness.chain.cli.bootstrap_secrets",
            return_value="ghs_TESTTOKEN_xxxxxxx",
        ),
        patch("baton_harness.chain.cli.validate_daemon_token"),
        patch(
            "baton_harness.chain.cli.load_workflow",
            return_value=MagicMock(),
        ),
        patch(
            "baton_harness.chain.cli.load_registry",
            return_value=[fake_repo_cfg],
        ),
        patch(
            "baton_harness.chain.cli.run_daemon",
            side_effect=fake_run_daemon,
        ),
        patch("baton_harness.chain.cli.os.chdir"),
        patch("baton_harness.chain.cli.os.path.isdir", return_value=True),
    ):
        result = _run_main("--once")

    assert result == 0, f"Expected exit 0, got {result}"
    expected = project_root / ".baton-harness" / "session-report.json"
    forwarded = run_daemon_kwargs.get("report_path")
    assert forwarded == expected, (
        f"Expected default report_path={expected!r}, got {forwarded!r}"
    )
    assert forwarded is not None and Path(forwarded).is_absolute(), (
        f"Default report_path must be absolute; got {forwarded!r}"
    )


# ---------------------------------------------------------------------------
# report_path coexists with other forwarded run_daemon kwargs
# ---------------------------------------------------------------------------


def test_report_forwarded_alongside_existing_run_daemon_kwargs(
    tmp_path: Path,
) -> None:
    """report_path= joins once= and poll_interval_s= without disrupting them.

    Regression guard: adding the ``--report`` flag must not change how
    ``--once`` or ``--poll-interval`` are threaded through to
    ``run_daemon``, matching the existing assertions in
    ``test_cli.py::test_main_once_calls_run_daemon_with_once_true`` and
    ``test_cli.py::test_main_poll_interval_override``.
    """
    report_path_arg = tmp_path / "combo-report.json"
    fake_repo_cfg = MagicMock()
    fake_repo_cfg.project_root = tmp_path / "project_root"

    run_daemon_kwargs: dict[str, object] = {}

    async def fake_run_daemon(*args: object, **kwargs: object) -> None:
        run_daemon_kwargs.update(kwargs)

    with (
        patch(
            "baton_harness.chain.cli.bootstrap_secrets",
            return_value="ghs_TESTTOKEN_xxxxxxx",
        ),
        patch("baton_harness.chain.cli.validate_daemon_token"),
        patch(
            "baton_harness.chain.cli.load_workflow",
            return_value=MagicMock(),
        ),
        patch(
            "baton_harness.chain.cli.load_registry",
            return_value=[fake_repo_cfg],
        ),
        patch(
            "baton_harness.chain.cli.run_daemon",
            side_effect=fake_run_daemon,
        ),
        patch("baton_harness.chain.cli.os.chdir"),
        patch("baton_harness.chain.cli.os.path.isdir", return_value=True),
    ):
        result = _run_main(
            "--once",
            "--poll-interval",
            "5",
            "--report",
            str(report_path_arg),
        )

    assert result == 0, f"Expected exit 0, got {result}"
    assert run_daemon_kwargs.get("once") is True
    assert run_daemon_kwargs.get("poll_interval_s") == 5.0
    assert run_daemon_kwargs.get("report_path") == report_path_arg.resolve()
