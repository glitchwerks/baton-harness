"""Unit tests for baton_harness.chain.cli (``bh-daemon`` entry point).

Coverage:
- ``--once`` path: daemon invoked with ``once=True``.
- Registry unset (missing env vars) → clean error message + exit 1.
- Default ``--workflow`` resolves to ``config/WORKFLOW.md`` relative to
  the repo root.
- ``--poll-interval`` override is threaded through.
- ``os.chdir`` is called with the managed repo root before ``run_daemon``.
- Workflow path is resolved to absolute BEFORE the ``os.chdir`` call.
- (slice 3a) ``bootstrap_secrets`` is called after chdir and before
  ``asyncio.run``.
- (slice 3a) Returned installation token is validated via
  ``validate_daemon_token``.
- (slice 3a) Non-installation token from bootstrap causes exit non-zero
  without entering ``asyncio.run``.
- (slice 3a) ``BWS_ACCESS_TOKEN`` is removed from environ by bootstrap.
- (slice 3a) Installation token is never written to environ after startup.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from baton_harness.chain.cli import main

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
# Registry unset → clean error + exit 1
# ---------------------------------------------------------------------------


def test_main_registry_unset_exits_1() -> None:
    """Missing registry env vars produce a clean error and return 1."""
    env_backup = {
        k: os.environ.pop(k, None)
        for k in ("BH_REPO_OWNER", "BH_REPO_NAME", "BH_PROJECT_ROOT")
    }
    try:
        # We still need a workflow file to exist; patch load_workflow to
        # avoid a file-system dependency.
        with (
            patch(
                "baton_harness.chain.cli.load_workflow",
                return_value=MagicMock(),
            ),
            patch(
                "baton_harness.chain.cli.load_registry",
                side_effect=ValueError(
                    "Registry is not configured. "
                    "Set BH_REPO_OWNER, BH_REPO_NAME, and BH_PROJECT_ROOT"
                ),
            ),
        ):
            result = _run_main("--once")
    finally:
        for k, v in env_backup.items():
            if v is not None:
                os.environ[k] = v

    assert result == 1, f"Expected exit 1, got {result}"


# ---------------------------------------------------------------------------
# --once path
# ---------------------------------------------------------------------------


def test_main_once_calls_run_daemon_with_once_true() -> None:
    """--once flag passes once=True to run_daemon."""
    called_kwargs: dict = {}

    async def fake_run_daemon(*args: object, **kwargs: object) -> None:
        called_kwargs.update(kwargs)

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
            return_value=[MagicMock()],
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
    assert called_kwargs.get("once") is True


def test_main_poll_interval_override() -> None:
    """--poll-interval is passed to run_daemon."""
    called_kwargs: dict = {}

    async def fake_run_daemon(*args: object, **kwargs: object) -> None:
        called_kwargs.update(kwargs)

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
            return_value=[MagicMock()],
        ),
        patch(
            "baton_harness.chain.cli.run_daemon",
            side_effect=fake_run_daemon,
        ),
        patch("baton_harness.chain.cli.os.chdir"),
        patch("baton_harness.chain.cli.os.path.isdir", return_value=True),
    ):
        result = _run_main("--once", "--poll-interval", "5")

    assert result == 0
    assert called_kwargs.get("poll_interval_s") == 5.0


# ---------------------------------------------------------------------------
# chdir into managed repo before run_daemon (FIX A)
# ---------------------------------------------------------------------------


def test_main_chdirs_into_project_root_before_run_daemon() -> None:
    """CLI must chdir into the managed repo before calling run_daemon.

    The vendored GitHubTracker calls ``gh`` without ``--repo``, so those
    calls resolve against the process cwd.  The daemon MUST set cwd to
    ``BH_PROJECT_ROOT`` before the event loop starts.
    """
    project_root = Path("/fake/project/root")
    chdir_calls: list[object] = []
    run_daemon_called_after_chdir = False

    async def fake_run_daemon(*args: object, **kwargs: object) -> None:
        nonlocal run_daemon_called_after_chdir
        # At this point chdir must already have been called.
        run_daemon_called_after_chdir = bool(chdir_calls)

    fake_repo_cfg = MagicMock()
    fake_repo_cfg.project_root = project_root

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
            side_effect=lambda p: chdir_calls.append(p),
        ),
        patch("baton_harness.chain.cli.os.path.isdir", return_value=True),
    ):
        result = _run_main("--once")

    assert result == 0
    assert chdir_calls, "os.chdir must be called before run_daemon"
    assert chdir_calls[0] == project_root, (
        f"Expected chdir({project_root!r}), got chdir({chdir_calls[0]!r})"
    )
    assert run_daemon_called_after_chdir, (
        "run_daemon was called before os.chdir"
    )


def test_main_invalid_project_root_exits_1_no_traceback(
    tmp_path: Path,
) -> None:
    """Non-existent BH_PROJECT_ROOT → clean exit 1, no raised exception.

    Regression guard for the uncaught ``FileNotFoundError`` from
    ``os.chdir`` when ``BH_PROJECT_ROOT`` points at a path that doesn't
    exist.
    """
    nonexistent_root = tmp_path / "does_not_exist"
    # Confirm the path really does not exist.
    assert not nonexistent_root.exists()

    fake_repo_cfg = MagicMock()
    fake_repo_cfg.project_root = nonexistent_root

    # main() must return 1 without raising any exception.
    with (
        patch(
            "baton_harness.chain.cli.load_workflow",
            return_value=MagicMock(),
        ),
        patch(
            "baton_harness.chain.cli.load_registry",
            return_value=[fake_repo_cfg],
        ),
    ):
        result = main(["--once"])

    assert result == 1, (
        f"Expected exit 1 for invalid project root, got {result}"
    )


# ---------------------------------------------------------------------------
# Workflow path absolute before chdir
# ---------------------------------------------------------------------------


def test_main_workflow_path_resolved_absolute_before_chdir() -> None:
    """Workflow path must be absolute before chdir so it survives cwd change.

    If a relative ``--workflow`` path were passed without resolving it first,
    the ``chdir`` into the managed repo would break config loading.
    """
    project_root = Path("/fake/project/root")

    # Track the sequence: when was load_workflow called relative to chdir?
    call_sequence: list[str] = []

    def record_load_workflow(path: str) -> MagicMock:
        call_sequence.append("load_workflow")
        assert Path(path).is_absolute(), (
            f"Workflow path must be absolute before load_workflow is"
            f" called, got: {path!r}"
        )
        return MagicMock()

    fake_repo_cfg = MagicMock()
    fake_repo_cfg.project_root = project_root

    async def fake_run_daemon(*args: object, **kwargs: object) -> None:
        pass

    with (
        patch(
            "baton_harness.chain.cli.bootstrap_secrets",
            return_value="ghs_TESTTOKEN_xxxxxxx",
        ),
        patch("baton_harness.chain.cli.validate_daemon_token"),
        patch(
            "baton_harness.chain.cli.load_workflow",
            side_effect=record_load_workflow,
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
        # Pass a relative path to simulate operator usage.
        result = _run_main("--once", "--workflow", "config/WORKFLOW.md")

    assert result == 0
    assert "load_workflow" in call_sequence


# ---------------------------------------------------------------------------
# Slice 3a — daemon startup App-token wiring (RED tests)
#
# Patch paths: code-writer must import bootstrap_secrets into cli.py so
# the symbol is patchable at:
#   baton_harness.chain.cli.bootstrap_secrets
# Fallback: also patchable at source:
#   baton_harness.chain.app_auth.bootstrap_secrets
# Tests patch both so they survive either import style.
# ---------------------------------------------------------------------------


class TestDaemonStartupAuthWiring:
    """Slice 3a: bootstrap_secrets + validate_daemon_token wired into main().

    These tests are RED until cli.py calls bootstrap_secrets() after the
    chdir step and validate_daemon_token() on the result.
    """

    def test_main_calls_bootstrap_secrets_after_cwd_validation(
        self,
    ) -> None:
        """bootstrap_secrets is called after chdir and before asyncio.run.

        Records the call-order of chdir and bootstrap_secrets via a shared
        sequence list.  chdir MUST appear before bootstrap_secrets.
        """
        project_root = Path("/fake/project/root")
        call_order: list[str] = []

        # Sentinel bootstrap that records its position in the sequence.
        def fake_bootstrap(**kwargs: object) -> str:
            call_order.append("bootstrap_secrets")
            return "ghs_TESTTOKEN_sentinel"

        fake_repo_cfg = MagicMock()
        fake_repo_cfg.project_root = project_root

        async def fake_run_daemon(*args: object, **kwargs: object) -> None:
            pass

        # Patch bootstrap_secrets at both possible import locations so the
        # test survives either import style the code-writer chooses.
        with (
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
                side_effect=lambda p: call_order.append("chdir"),
            ),
            patch(
                "baton_harness.chain.cli.os.path.isdir",
                return_value=True,
            ),
            # Primary patch location (code-writer imports into cli.py).
            patch(
                "baton_harness.chain.cli.bootstrap_secrets",
                side_effect=fake_bootstrap,
            ),
        ):
            result = _run_main("--once")

        assert result == 0, f"Expected exit 0, got {result}"
        assert "bootstrap_secrets" in call_order, (
            "bootstrap_secrets was never called during main()"
        )
        chdir_pos = (
            call_order.index("chdir")
            if "chdir" in call_order
            else len(call_order)
        )
        bootstrap_pos = call_order.index("bootstrap_secrets")
        assert chdir_pos < bootstrap_pos, (
            f"chdir must precede bootstrap_secrets; order was {call_order!r}"
        )

    def test_main_validates_minted_token_with_validate_daemon_token(
        self,
    ) -> None:
        """validate_daemon_token is called with the bootstrap token.

        bootstrap_secrets returns a sentinel ghs_ token;
        validate_daemon_token must be called with exactly that value,
        and main() must return 0.
        """
        project_root = Path("/fake/project/root")
        _sentinel_token = "ghs_TESTTOKEN_sentinel_abc123"
        validated_with: list[str] = []

        def fake_bootstrap(**kwargs: object) -> str:
            return _sentinel_token

        def fake_validate(token: str) -> None:
            validated_with.append(token)
            # ghs_ is valid — do not raise.

        fake_repo_cfg = MagicMock()
        fake_repo_cfg.project_root = project_root

        async def fake_run_daemon(*args: object, **kwargs: object) -> None:
            pass

        with (
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
            patch(
                "baton_harness.chain.cli.os.path.isdir",
                return_value=True,
            ),
            patch(
                "baton_harness.chain.cli.bootstrap_secrets",
                side_effect=fake_bootstrap,
            ),
            patch(
                "baton_harness.chain.cli.validate_daemon_token",
                side_effect=fake_validate,
            ),
        ):
            result = _run_main("--once")

        assert result == 0, f"Expected exit 0, got {result}"
        assert validated_with, "validate_daemon_token was never called"
        assert validated_with[0] == _sentinel_token, (
            f"Expected validate_daemon_token({_sentinel_token!r}), "
            f"got validate_daemon_token({validated_with[0]!r})"
        )

    def test_main_rejects_non_installation_token_from_bootstrap(
        self,
    ) -> None:
        """A non-ghs_ token from bootstrap causes main() to return non-zero.

        bootstrap_secrets returns a fine-grained PAT; validate_daemon_token
        raises (it already does for non-ghs_); main() must return non-zero
        WITHOUT entering asyncio.run.
        """
        project_root = Path("/fake/project/root")
        asyncio_run_called = False

        def fake_bootstrap(**kwargs: object) -> str:
            # Return a worker-PAT, not an installation token.
            return "github_pat_xyz_not_an_installation_token"

        fake_repo_cfg = MagicMock()
        fake_repo_cfg.project_root = project_root

        async def sentinel_run_daemon(*args: object, **kwargs: object) -> None:
            nonlocal asyncio_run_called
            asyncio_run_called = True

        with (
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
                side_effect=sentinel_run_daemon,
            ),
            patch("baton_harness.chain.cli.os.chdir"),
            patch(
                "baton_harness.chain.cli.os.path.isdir",
                return_value=True,
            ),
            patch(
                "baton_harness.chain.cli.bootstrap_secrets",
                side_effect=fake_bootstrap,
            ),
            # Use the REAL validate_daemon_token — it rejects github_pat_.
            # Patch only its import location in cli.py so the real logic runs.
        ):
            real_validate = __import__(
                "baton_harness._auth", fromlist=["validate_daemon_token"]
            ).validate_daemon_token

            with patch(
                "baton_harness.chain.cli.validate_daemon_token",
                side_effect=real_validate,
            ):
                result = _run_main("--once")

        assert result != 0, (
            "Expected non-zero exit when bootstrap returns a PAT,"
            f" got {result}"
        )
        assert not asyncio_run_called, (
            "asyncio.run (run_daemon) must NOT be called when token "
            "validation fails"
        )

    def test_main_bws_access_token_popped_from_environ_before_daemon_starts(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """BWS_ACCESS_TOKEN is removed from environ before daemon starts.

        Set BWS_ACCESS_TOKEN in environ; run main() with bootstrap
        stubbed to pop it (matching the spec: bootstrap pops
        BWS_ACCESS_TOKEN as its first operation).  After bootstrap,
        the token must be absent.

        Environ state is captured at the point bootstrap_secrets would
        have run, not at asyncio.run, to avoid requiring the full loop.
        """
        sentinel_bws = "0.sentinel-bws-access-token-for-test"
        monkeypatch.setenv("BWS_ACCESS_TOKEN", sentinel_bws)

        project_root = Path("/fake/project/root")
        environ_after_bootstrap: dict[str, str] = {}

        def fake_bootstrap(**kwargs: object) -> str:
            # Simulate the contract: pop BWS_ACCESS_TOKEN immediately.
            os.environ.pop("BWS_ACCESS_TOKEN", None)
            # Capture environ right after the pop.
            environ_after_bootstrap.update(os.environ)
            return "ghs_TESTTOKEN_from_bootstrap"

        fake_repo_cfg = MagicMock()
        fake_repo_cfg.project_root = project_root

        async def fake_run_daemon(*args: object, **kwargs: object) -> None:
            pass

        with (
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
            patch(
                "baton_harness.chain.cli.os.path.isdir",
                return_value=True,
            ),
            patch(
                "baton_harness.chain.cli.bootstrap_secrets",
                side_effect=fake_bootstrap,
            ),
            patch("baton_harness.chain.cli.validate_daemon_token"),
        ):
            result = _run_main("--once")

        assert result == 0, f"Expected exit 0, got {result}"
        assert "BWS_ACCESS_TOKEN" not in environ_after_bootstrap, (
            "BWS_ACCESS_TOKEN must be removed from os.environ by "
            "bootstrap_secrets before any other operation"
        )
        # Also assert it's absent from current environ (no accidental restore).
        assert "BWS_ACCESS_TOKEN" not in os.environ, (
            "BWS_ACCESS_TOKEN must not be present in os.environ after main()"
        )

    def test_main_installation_token_never_written_to_environ(
        self,
    ) -> None:
        """The installation token is never written to os.environ.

        After main() setup, neither GH_TOKEN, GITHUB_TOKEN, nor any environ
        key contains the sentinel ghs_ token value.  This is the
        env-discipline invariant: the token is passed by value to the daemon,
        never stored in the process environment.
        """
        sentinel_token = "ghs_SENTINEL_NEVER_IN_ENVIRON_xyz999"
        project_root = Path("/fake/project/root")
        environ_snapshot: dict[str, str] = {}

        def fake_bootstrap(**kwargs: object) -> str:
            return sentinel_token

        fake_repo_cfg = MagicMock()
        fake_repo_cfg.project_root = project_root

        async def fake_run_daemon(*args: object, **kwargs: object) -> None:
            # Capture environ at daemon entry to catch any write that
            # happened during startup.
            environ_snapshot.update(os.environ)

        with (
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
            patch(
                "baton_harness.chain.cli.os.path.isdir",
                return_value=True,
            ),
            patch(
                "baton_harness.chain.cli.bootstrap_secrets",
                side_effect=fake_bootstrap,
            ),
            patch("baton_harness.chain.cli.validate_daemon_token"),
        ):
            result = _run_main("--once")

        assert result == 0, f"Expected exit 0, got {result}"
        # Verify the sentinel token value is not in any environ key.
        leaked_keys = [
            k for k, v in environ_snapshot.items() if sentinel_token in v
        ]
        assert not leaked_keys, (
            f"Installation token was written to environ key(s): "
            f"{leaked_keys!r} — env-discipline invariant violated"
        )
        # Specifically check the canonical token keys.
        for key in ("GH_TOKEN", "GITHUB_TOKEN"):
            val = environ_snapshot.get(key, "")
            assert sentinel_token not in val, (
                f"Installation token found in os.environ[{key!r}]"
            )

    def test_main_accepts_refreshable_provider_from_bootstrap(
        self,
    ) -> None:
        """main() validates one token but passes provider to run_daemon."""
        project_root = Path("/fake/project/root")
        validated_with: list[str] = []
        run_daemon_kwargs: dict[str, object] = {}

        class FakeProvider:
            """Minimal token provider used to model refreshable auth."""

            def __init__(self) -> None:
                self.calls = 0

            def get_token(self) -> str:
                self.calls += 1
                return f"ghs_PROVIDER_TOKEN_{self.calls}"

        provider = FakeProvider()

        def fake_bootstrap(**kwargs: object) -> FakeProvider:
            return provider

        def fake_validate(token: str) -> None:
            validated_with.append(token)

        async def fake_run_daemon(*args: object, **kwargs: object) -> None:
            run_daemon_kwargs.update(kwargs)

        fake_repo_cfg = MagicMock()
        fake_repo_cfg.project_root = project_root

        with (
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
            patch(
                "baton_harness.chain.cli.os.path.isdir",
                return_value=True,
            ),
            patch(
                "baton_harness.chain.cli.bootstrap_secrets",
                side_effect=fake_bootstrap,
            ),
            patch(
                "baton_harness.chain.cli.validate_daemon_token",
                side_effect=fake_validate,
            ),
        ):
            result = _run_main("--once")

        assert result == 0, f"Expected exit 0, got {result}"
        assert validated_with == ["ghs_PROVIDER_TOKEN_1"], (
            "main() must validate the resolved provider token before daemon "
            f"startup; got {validated_with!r}"
        )
        assert run_daemon_kwargs.get("installation_token") is provider, (
            "run_daemon must receive the provider object so downstream gh "
            "calls can refresh tokens per call"
        )


# ---------------------------------------------------------------------------
# Gap 1 — duplicate reconcile_startup + token threading (codex 3347f83 P2)
# ---------------------------------------------------------------------------


class TestCliGap1DuplicateReconcileAndTokenThreading:
    """Codex gap: cli.py must NOT call reconcile_startup directly.

    Gap 1A: cli.main() must not call reconcile_startup itself — only
        run_daemon should call it (via its own internal startup sweep).
    Gap 1B: cli.main() must pass installation_token= to run_daemon so
        run_daemon can thread it into its reconcile_startup call.
    """

    def test_cli_main_does_not_call_reconcile_startup_directly(
        self,
    ) -> None:
        """cli.main() must NOT call reconcile_startup through ANY path.

        Both the reconcile-module symbol and the daemon-module re-export
        are patched as spies.  After main() returns, BOTH spies must have
        zero calls — the startup sweep must travel through run_daemon
        exclusively and must not be reachable by routing through any
        module alias (closing the indirection escape hatch used by the
        prior sub-agent via _daemon_mod.reconcile_startup).
        """
        direct_calls_via_reconcile_mod: list[object] = []
        direct_calls_via_daemon_mod: list[object] = []

        async def _spy_reconcile(*args: object, **kwargs: object) -> None:
            direct_calls_via_reconcile_mod.append((args, kwargs))

        async def _spy_daemon_reconcile(
            *args: object, **kwargs: object
        ) -> None:
            direct_calls_via_daemon_mod.append((args, kwargs))

        async def _noop_run_daemon(*args: object, **kwargs: object) -> None:
            pass

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
                return_value=[MagicMock()],
            ),
            # Spy on the reconcile module symbol directly — catches any
            # call routed through baton_harness.chain.reconcile.
            patch(
                "baton_harness.chain.reconcile.reconcile_startup",
                side_effect=_spy_reconcile,
            ),
            # Spy on the daemon module re-export — catches the prior
            # sub-agent's indirection via _daemon_mod.reconcile_startup.
            patch(
                "baton_harness.chain.daemon.reconcile_startup",
                side_effect=_spy_daemon_reconcile,
            ),
            patch(
                "baton_harness.chain.cli.run_daemon",
                side_effect=_noop_run_daemon,
            ),
            patch("baton_harness.chain.cli.os.chdir"),
            patch("baton_harness.chain.cli.os.path.isdir", return_value=True),
        ):
            result = _run_main("--once")

        assert result == 0, f"Expected exit 0, got {result}"
        assert not direct_calls_via_reconcile_mod, (
            "cli.main() called reconcile_startup via reconcile module "
            f"({len(direct_calls_via_reconcile_mod)} time(s)) — it must "
            "NOT; only run_daemon should invoke the startup sweep."
        )
        assert not direct_calls_via_daemon_mod, (
            "cli.main() called reconcile_startup via daemon module "
            f"re-export ({len(direct_calls_via_daemon_mod)} time(s)) — "
            "re-routing through _daemon_mod is still a direct call; "
            "only run_daemon should invoke the startup sweep."
        )

    def test_cli_main_passes_installation_token_to_run_daemon(
        self,
    ) -> None:
        """cli.main() must forward the minted token to run_daemon.

        Patches bootstrap_secrets to return a sentinel token.  Patches
        run_daemon as an async spy.  After main() returns, asserts that
        run_daemon was called with
        installation_token="ghs_TESTTOKEN_xxxxxxx".

        Currently FAILS if cli.py does not pass installation_token= to
        run_daemon (the kwarg would be absent or empty string).
        """
        sentinel = "ghs_TESTTOKEN_xxxxxxx"
        run_daemon_kwargs: dict[str, object] = {}

        async def _capture_run_daemon(*args: object, **kwargs: object) -> None:
            run_daemon_kwargs.update(kwargs)

        with (
            patch(
                "baton_harness.chain.cli.bootstrap_secrets",
                return_value=sentinel,
            ),
            patch("baton_harness.chain.cli.validate_daemon_token"),
            patch(
                "baton_harness.chain.cli.load_workflow",
                return_value=MagicMock(),
            ),
            patch(
                "baton_harness.chain.cli.load_registry",
                return_value=[MagicMock()],
            ),
            patch(
                "baton_harness.chain.cli.run_daemon",
                side_effect=_capture_run_daemon,
            ),
            patch("baton_harness.chain.cli.os.chdir"),
            patch("baton_harness.chain.cli.os.path.isdir", return_value=True),
            patch(
                "baton_harness.chain.reconcile.reconcile_startup",
                new_callable=AsyncMock,
            ),
        ):
            result = _run_main("--once")

        assert result == 0, f"Expected exit 0, got {result}"
        assert run_daemon_kwargs.get("installation_token") == sentinel, (
            "run_daemon must be called with "
            f"installation_token={sentinel!r}; "
            "got installation_token="
            f"{run_daemon_kwargs.get('installation_token')!r}"
        )


class TestForcePrNotMergeStartupSelfTest:
    """Startup self-test wiring for the force-pr-not-merge tripwire."""

    def test_main_runs_tripwire_self_test_before_bootstrap(
        self,
    ) -> None:
        """main() runs the hook self-test after chdir and before bootstrap."""
        project_root = Path("/fake/project/root")
        call_order: list[str] = []

        fake_repo_cfg = MagicMock()
        fake_repo_cfg.project_root = project_root

        def fake_self_test() -> None:
            call_order.append("self-test")

        def fake_bootstrap(**kwargs: object) -> str:
            call_order.append("bootstrap")
            return "ghs_TESTTOKEN_sentinel"

        async def fake_run_daemon(*args: object, **kwargs: object) -> None:
            call_order.append("run-daemon")

        with (
            patch(
                "baton_harness.chain.cli.load_workflow",
                return_value=MagicMock(),
            ),
            patch(
                "baton_harness.chain.cli.load_registry",
                return_value=[fake_repo_cfg],
            ),
            patch(
                "baton_harness.chain.cli.os.chdir",
                side_effect=lambda p: call_order.append("chdir"),
            ),
            patch(
                "baton_harness.chain.cli.os.path.isdir",
                return_value=True,
            ),
            patch(
                "baton_harness.chain.cli._assert_force_pr_not_merge_tripwire",
                side_effect=fake_self_test,
            ),
            patch(
                "baton_harness.chain.cli.bootstrap_secrets",
                side_effect=fake_bootstrap,
            ),
            patch("baton_harness.chain.cli.validate_daemon_token"),
            patch(
                "baton_harness.chain.cli.run_daemon",
                side_effect=fake_run_daemon,
            ),
        ):
            result = _run_main("--once")

        assert result == 0
        assert call_order.index("chdir") < call_order.index("self-test"), (
            f"chdir must happen before the hook self-test; got {call_order!r}"
        )
        assert call_order.index("self-test") < call_order.index("bootstrap"), (
            "force-pr-not-merge self-test must fail fast before token "
            f"bootstrap; got {call_order!r}"
        )

    def test_main_exits_when_tripwire_self_test_fails(self) -> None:
        """A failing hook self-test stops startup before bootstrap/run."""
        project_root = Path("/fake/project/root")
        bootstrap_called = False
        run_daemon_called = False

        fake_repo_cfg = MagicMock()
        fake_repo_cfg.project_root = project_root

        def fake_bootstrap(**kwargs: object) -> str:
            nonlocal bootstrap_called
            bootstrap_called = True
            return "ghs_TESTTOKEN_sentinel"

        async def fake_run_daemon(*args: object, **kwargs: object) -> None:
            nonlocal run_daemon_called
            run_daemon_called = True

        with (
            patch(
                "baton_harness.chain.cli.load_workflow",
                return_value=MagicMock(),
            ),
            patch(
                "baton_harness.chain.cli.load_registry",
                return_value=[fake_repo_cfg],
            ),
            patch("baton_harness.chain.cli.os.chdir"),
            patch(
                "baton_harness.chain.cli.os.path.isdir",
                return_value=True,
            ),
            patch(
                "baton_harness.chain.cli._assert_force_pr_not_merge_tripwire",
                side_effect=RuntimeError("hook parser drifted"),
            ),
            patch(
                "baton_harness.chain.cli.bootstrap_secrets",
                side_effect=fake_bootstrap,
            ),
            patch("baton_harness.chain.cli.validate_daemon_token"),
            patch(
                "baton_harness.chain.cli.run_daemon",
                side_effect=fake_run_daemon,
            ),
        ):
            result = _run_main("--once")

        assert result == 1
        assert not bootstrap_called, (
            "bootstrap must not run after a failing startup self-test"
        )
        assert not run_daemon_called, (
            "run_daemon must not run after a failing startup self-test"
        )
