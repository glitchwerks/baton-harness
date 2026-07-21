"""Worker-identity push-denial probe cluster (#223, extracted by #273).

Extracted verbatim (Phase 6a step 2, #268) from ``chain/daemon.py``. This
module owns the behavioral push-protection probe: it attempts a
throwaway push authenticated as the WORKER identity and observes
whether the push is denied by branch protection.

The ``_run`` subprocess seam intentionally stays defined in
``daemon/__init__.py`` (it is shared by clusters not yet extracted) and
is reached here via ``_daemon_mod._run(...)`` — a live attribute lookup
on the parent package module, not a captured import-time binding — so
that ``mock.patch.object(daemon_mod, "_run", ...)`` in existing tests
continues to intercept calls made from this submodule (the "patch
where it's looked up" rule, plan §4 Phase 6 / issue #273).
"""

from __future__ import annotations

import logging
import subprocess
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import baton_harness.chain.daemon as _daemon_mod
from baton_harness.chain.app_auth import InstallationTokenSource, gh_env
from baton_harness.chain.identity import Identity, env_for

# Hard-coded: preserves the pre-split "baton_harness.chain.daemon" logger
# name (plan §3.2) rather than the submodule's own __name__, so log
# aggregation and the caplog(logger=...) assertions in test_daemon.py stay
# byte-identical pre- and post-split. See #268.
_log = logging.getLogger("baton_harness.chain.daemon")

_PUSH_DENIAL_SIGNALS: tuple[str, ...] = (
    "403",
    "protected",
    "declined",
    "refusing to allow",
    "gh006",
)

#: Timeout (seconds) applied to both the probe push and its cleanup
#: delete `_run` calls (CodeRabbit PR #253 finding C12/C13) so a stalled
#: git/credential prompt cannot block daemon startup indefinitely.
_PROBE_PUSH_TIMEOUT_SECONDS: float = 30.0


class ProbeDenialReason(Enum):
    """Why `_probe_worker_push_denied` reported a not-safe outcome.

    ``ACCEPTED`` and ``CLEANUP_FAILED`` are both raised from an accepted
    (breached) push; ``CLEANUP_FAILED`` is the more specific of the two
    and is used instead of ``ACCEPTED`` whenever the post-accept cleanup
    delete itself did not succeed (CodeRabbit PR #253 finding C11).
    """

    ACCEPTED = "accepted"
    UNRECOGNIZED = "unrecognized"
    TRANSPORT_ERROR = "transport_error"
    TIMEOUT = "timeout"
    CLEANUP_FAILED = "cleanup_failed"


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of a worker-identity push-denial probe.

    Attributes:
        denied: ``True`` only when the push was rejected with a
            recognizable protection signal (safe). ``False`` for every
            other outcome (accepted, indeterminate, timed out, or
            raised) — always fail-closed.
        reason: ``None`` when ``denied`` is ``True``; otherwise one of
            ``ProbeDenialReason`` naming why the outcome was not safe.
        detail: Human-readable detail for logs/alerts.
    """

    denied: bool
    reason: ProbeDenialReason | None = None
    detail: str = ""


def _authed_git_push(
    repo_root: Path,
    branch_name: str,
    installation_token: InstallationTokenSource,
) -> subprocess.CompletedProcess[str]:
    """Push ``branch_name`` to origin, authed as the App installation.

    A "daemon-only" feature ruleset (bypass actor = the App) rejects a
    push authenticated as the ambient user PAT, so the push must
    instead inject the installation token via an env var and override
    the git credential helper inline (issue #220) — the raw token is
    never placed in argv/URL or persisted to ``.git/config``, where it
    would be visible in process listings, shell history, or disk.

    Args:
        repo_root: Path to the git worktree to push from.
        branch_name: Name of the branch to push to origin.
        installation_token: GitHub App installation access token (or a
            refreshable provider).  Falsy values fall back to a bare
            ``git push`` for non-App (PAT-only) deploys.

    Returns:
        The ``subprocess.CompletedProcess`` from the push invocation.
    """
    if not installation_token:
        return _daemon_mod._run(
            ["git", "-C", str(repo_root), "push", "origin", branch_name]
        )

    push_env = gh_env(installation_token)
    return _daemon_mod._run(
        [
            "git",
            "-C",
            str(repo_root),
            "-c",
            "credential.https://github.com.helper=",
            "-c",
            "credential.https://github.com.helper=!f() { "
            "echo username=x-access-token; "
            'echo "password=$GH_INSTALLATION_TOKEN"; '
            "}; f",
            "push",
            "origin",
            branch_name,
        ],
        env=push_env,
    )


def _attempt_probe_ref_cleanup(
    repo_root: Path,
    probe_ref: str,
    worker_env: dict[str, str],
) -> str | None:
    """Attempt an idempotent cleanup delete of a throwaway probe ref.

    CodeRabbit PR #253 round 2, finding #6: before the fix, cleanup was
    only attempted on the ACCEPTED (breached) push path. The TIMEOUT,
    TRANSPORT_ERROR, and UNRECOGNIZED outcomes returned without any
    cleanup attempt — but the throwaway ref may have been created on
    origin before the client lost the result, leaving durable residue.

    Idempotent: a cleanup delete that fails because the ref was never
    created (git reports "does not exist") is treated as success — that
    is the expected outcome when the push never actually landed, not a
    failure to escalate.

    Args:
        repo_root: Repository root to run the cleanup delete from.
        probe_ref: The throwaway ``feature/`` ref to delete.
        worker_env: The worker-identity environment used for the
            original probe push, reused here for the cleanup delete.

    Returns:
        ``None`` on success (including the idempotent "ref does not
        exist" outcome). Otherwise a human-readable failure-detail
        string describing why the cleanup delete itself failed
        (timeout, raised exception, or a genuine nonzero exit).
    """
    cleanup_cmd = [
        "git",
        "-C",
        str(repo_root),
        "push",
        "origin",
        "--delete",
        probe_ref,
    ]
    remediation = (
        f"remediation: manually delete refs/heads/{probe_ref} on origin"
    )

    try:
        cleanup_result = _daemon_mod._run(
            cleanup_cmd,
            env=worker_env,
            timeout=_PROBE_PUSH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        detail = (
            f"cleanup delete for {probe_ref} timed out after "
            f"{_PROBE_PUSH_TIMEOUT_SECONDS}s, ref may remain on origin"
        )
        _log.error("daemon: %s (%s)", detail, remediation)
        return detail
    except Exception as exc:  # noqa: BLE001
        detail = f"cleanup delete for {probe_ref} raised: {exc}"
        _log.error("daemon: %s (%s)", detail, remediation)
        return detail

    if cleanup_result.returncode == 0:
        return None

    stderr_lower = (cleanup_result.stderr or "").lower()
    if "does not exist" in stderr_lower:
        # Idempotent: the ref was never created on origin (the push
        # never landed) -- this is the expected outcome, not a failure.
        return None

    detail = (
        f"cleanup delete for {probe_ref} exited "
        f"{cleanup_result.returncode}, ref may remain on origin: "
        f"{cleanup_result.stderr}"
    )
    _log.error("daemon: %s (%s)", detail, remediation)
    return detail


def _probe_worker_push_denied(repo_root: Path) -> ProbeResult:
    """Probe whether worker-identity pushes are correctly denied.

    Attempts a best-effort push of ``HEAD`` to a unique throwaway
    ``feature/`` ref using the worker identity. A recognized denial means
    the protection boundary is intact and launch is safe. Any accepted,
    indeterminate, timed-out, or exception outcome fails closed.

    Both the probe push and its cleanup delete are given a positive
    ``timeout=`` on the shared ``_run`` seam (CodeRabbit PR #253 finding
    C12/C13) so a stalled git/credential prompt cannot block daemon
    startup indefinitely; a ``subprocess.TimeoutExpired`` is reported via
    the distinct ``ProbeDenialReason.TIMEOUT`` rather than the generic
    ``TRANSPORT_ERROR``.

    When the push is ACCEPTED (returncode 0 — protection breached), the
    cleanup delete's outcome is itself checked: a nonzero returncode (no
    exception raised) is logged at ERROR level and surfaced via the
    distinct ``ProbeDenialReason.CLEANUP_FAILED`` rather than being
    silently swallowed and collapsed into the plain ``ACCEPTED`` reason
    (CodeRabbit PR #253 finding C11).

    Every OTHER non-denied outcome — ``TIMEOUT``, ``TRANSPORT_ERROR``,
    and ``UNRECOGNIZED`` — also attempts an idempotent cleanup delete
    via ``_attempt_probe_ref_cleanup`` (CodeRabbit PR #253 round 2,
    finding #6), since none of those outcomes prove the throwaway ref
    was never created on origin. The cleanup is a side effect only: the
    ORIGINAL refusal reason is always preserved, and a cleanup failure
    on these paths is logged at ERROR and appended to ``detail`` rather
    than escalated to ``CLEANUP_FAILED`` (that reason is reserved for
    the accepted-push cleanup failure above). A CONFIRMED denial is the
    only outcome that skips cleanup entirely — a recognized denial
    signal proves the ref was never created.

    Args:
        repo_root: Repository root to run the probe push from.

    Returns:
        A ``ProbeResult``. ``denied=True`` (``reason=None``) only when
        the push was rejected with a recognizable protection signal.
        ``denied=False`` for every other outcome, with ``reason`` naming
        why (``ACCEPTED``, ``CLEANUP_FAILED``, ``UNRECOGNIZED``,
        ``TRANSPORT_ERROR``, or ``TIMEOUT``).
    """
    probe_ref = f"feature/__bh-probe-{uuid.uuid4().hex[:12]}"
    worker_env = env_for(Identity.WORKER)
    push_cmd = [
        "git",
        "-C",
        str(repo_root),
        "push",
        "origin",
        f"HEAD:refs/heads/{probe_ref}",
    ]

    try:
        push_result = _daemon_mod._run(
            push_cmd, env=worker_env, timeout=_PROBE_PUSH_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        _log.warning(
            "daemon: push-denial probe push to %s timed out after %ss",
            probe_ref,
            _PROBE_PUSH_TIMEOUT_SECONDS,
        )
        # CodeRabbit PR #253 round 2, finding #6: a TIMEOUT does not
        # prove the remote rejected the push -- the throwaway ref may
        # have been created before the client lost the result, so
        # cleanup must still be attempted. The ORIGINAL TIMEOUT reason
        # is preserved regardless of the cleanup outcome.
        detail = (
            f"probe push to {probe_ref} timed out after "
            f"{_PROBE_PUSH_TIMEOUT_SECONDS}s"
        )
        cleanup_failure = _attempt_probe_ref_cleanup(
            repo_root, probe_ref, worker_env
        )
        if cleanup_failure is not None:
            detail = f"{detail}; {cleanup_failure}"
        return ProbeResult(
            denied=False,
            reason=ProbeDenialReason.TIMEOUT,
            detail=detail,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("daemon: push-denial probe transport failure: %s", exc)
        # Same rationale as the TIMEOUT branch above: a raised transport
        # exception does not prove the push was rejected -- attempt
        # cleanup, but preserve the ORIGINAL TRANSPORT_ERROR reason.
        detail = f"probe push to {probe_ref} raised: {exc}"
        cleanup_failure = _attempt_probe_ref_cleanup(
            repo_root, probe_ref, worker_env
        )
        if cleanup_failure is not None:
            detail = f"{detail}; {cleanup_failure}"
        return ProbeResult(
            denied=False,
            reason=ProbeDenialReason.TRANSPORT_ERROR,
            detail=detail,
        )

    if push_result.returncode == 0:
        cleanup_cmd = [
            "git",
            "-C",
            str(repo_root),
            "push",
            "origin",
            "--delete",
            probe_ref,
        ]
        accepted_prefix = (
            f"probe push to {probe_ref} was ACCEPTED (returncode=0) — "
            "push-protection boundary breached"
        )
        try:
            cleanup_result = _daemon_mod._run(
                cleanup_cmd,
                env=worker_env,
                timeout=_PROBE_PUSH_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            _log.error(
                "daemon: probe cleanup delete for %s timed out after "
                "%ss — probe ref may remain on origin (remediation: "
                "manually delete refs/heads/%s on origin)",
                probe_ref,
                _PROBE_PUSH_TIMEOUT_SECONDS,
                probe_ref,
            )
            return ProbeResult(
                denied=False,
                reason=ProbeDenialReason.CLEANUP_FAILED,
                detail=(
                    f"{accepted_prefix}; cleanup delete timed out after "
                    f"{_PROBE_PUSH_TIMEOUT_SECONDS}s, ref may remain on "
                    "origin"
                ),
            )
        except Exception as exc:  # noqa: BLE001
            _log.error(
                "daemon: probe cleanup delete failed for %s: %s "
                "(remediation: manually delete refs/heads/%s on origin)",
                probe_ref,
                exc,
                probe_ref,
            )
            return ProbeResult(
                denied=False,
                reason=ProbeDenialReason.CLEANUP_FAILED,
                detail=f"{accepted_prefix}; cleanup delete raised: {exc}",
            )

        if cleanup_result.returncode != 0:
            _log.error(
                "daemon: probe cleanup delete for %s exited %d — probe "
                "ref may remain on origin (remediation: manually delete "
                "refs/heads/%s on origin): %s",
                probe_ref,
                cleanup_result.returncode,
                probe_ref,
                cleanup_result.stderr,
            )
            return ProbeResult(
                denied=False,
                reason=ProbeDenialReason.CLEANUP_FAILED,
                detail=(
                    f"{accepted_prefix}; cleanup delete exited "
                    f"{cleanup_result.returncode}, ref may remain on "
                    f"origin (remediation: manually delete "
                    f"refs/heads/{probe_ref} on origin)"
                ),
            )

        return ProbeResult(
            denied=False,
            reason=ProbeDenialReason.ACCEPTED,
            detail=accepted_prefix,
        )

    stderr_lower = (push_result.stderr or "").lower()
    if any(signal in stderr_lower for signal in _PUSH_DENIAL_SIGNALS):
        # A CONFIRMED denial proves the probe ref was never created on
        # origin (the push was rejected before a ref could land) --
        # nothing to clean up, so no cleanup delete is attempted here.
        return ProbeResult(denied=True)

    # CodeRabbit PR #253 round 2, finding #6: an UNRECOGNIZED rejection
    # does not prove the remote actually rejected the push -- attempt
    # cleanup, but preserve the ORIGINAL UNRECOGNIZED reason regardless
    # of the cleanup outcome.
    detail = (
        f"probe push to {probe_ref} was rejected (returncode="
        f"{push_result.returncode}) without a recognizable denial "
        f"signal: {push_result.stderr!r}"
    )
    cleanup_failure = _attempt_probe_ref_cleanup(
        repo_root, probe_ref, worker_env
    )
    if cleanup_failure is not None:
        detail = f"{detail}; {cleanup_failure}"
    return ProbeResult(
        denied=False,
        reason=ProbeDenialReason.UNRECOGNIZED,
        detail=detail,
    )
