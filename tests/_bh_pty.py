"""Shared pty-based interactive-session driver for bh setup-script tests.

bash's own interactivity checks used across the harness's setup scripts
(the ``_bh_interactive``-style ``[[ -t 0 && -t 1 && ... ]]`` guard) test
whether fd 0 (stdin) *and* fd 1 (stdout) are real terminals. A plain
``subprocess.run`` with pipes never satisfies that -- pipes are never
ttys -- so any test that needs to exercise an *interactive* branch of
one of these scripts must attach a real pseudo-terminal to both fds.
Python's ``pty`` module only exists on POSIX; every test module that
imports this helper must skip on ``sys.platform == "win32"`` (matching
the existing ``@pytest.mark.skipif(sys.platform == "win32", ...)``
precedent in ``tests/test_provision_ruleset_missing_env_debug.py``).

This module is not itself a test file (no ``test_*`` functions) and is
not collected by pytest.
"""

from __future__ import annotations

import os
import select
import subprocess
import threading
import time


def run_interactive(
    argv: list[str],
    env: dict[str, str],
    *,
    input_text: str,
    timeout: float = 30.0,
) -> tuple[int, str, str]:
    """Run ``argv`` with stdin+stdout attached to a real pty.

    ``stderr`` is captured via a normal pipe, not the pty -- bash's own
    ``read -r -p PROMPT`` writes ``PROMPT`` to standard error regardless
    of whether stdin/stdout are terminals, so prompt-text assertions
    belong against the returned ``stderr`` string. Only fd 0 and fd 1
    need to be a tty to satisfy an ``-t 0 && -t 1`` interactivity check.

    Args:
        argv: Command and arguments to execute.
        env: Environment for the subprocess.
        input_text: Text pre-loaded into the pty's input queue before
            the process starts, consumed in order as the process's
            ``read`` calls occur. Must include trailing newlines for
            each expected answer.
        timeout: Maximum seconds to wait for the process to exit before
            killing it and raising ``TimeoutError``.

    Returns:
        A ``(returncode, pty_output, stderr)`` tuple. ``pty_output`` is
        whatever the process wrote to its controlling terminal (its own
        stdout, plus tty echo of the fed input) -- most assertions
        should target ``stderr`` instead, per the note above.

    Raises:
        TimeoutError: If the process has not exited within ``timeout``
            seconds. The process is killed before raising.
    """
    import pty  # POSIX only; caller module must skip on win32.

    master_fd, slave_fd = pty.openpty()
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(
            argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=subprocess.PIPE,
            env=env,
            close_fds=True,
        )
        os.close(slave_fd)
        slave_fd = -1
        os.write(master_fd, input_text.encode("utf-8"))

        stderr_holder: dict[str, bytes] = {}

        def _drain_stderr() -> None:
            assert proc is not None
            assert proc.stderr is not None
            stderr_holder["data"] = proc.stderr.read()

        stderr_thread = threading.Thread(target=_drain_stderr)
        stderr_thread.start()

        stdout_chunks: list[bytes] = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                proc.wait(timeout=5)
                raise TimeoutError(
                    f"interactive process did not exit within {timeout}s; "
                    f"partial pty output: {b''.join(stdout_chunks)!r}"
                )
            ready, _, _ = select.select(
                [master_fd], [], [], min(remaining, 0.2)
            )
            if master_fd in ready:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                stdout_chunks.append(chunk)
            if proc.poll() is not None:
                # Drain whatever is left buffered in the pty briefly.
                while True:
                    ready, _, _ = select.select([master_fd], [], [], 0.05)
                    if not ready:
                        break
                    try:
                        chunk = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    stdout_chunks.append(chunk)
                break

        stderr_thread.join(timeout=5)
        proc.wait(timeout=5)
        stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
        stderr = stderr_holder.get("data", b"").decode(
            "utf-8", errors="replace"
        )
        return proc.returncode, stdout, stderr
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        if slave_fd != -1:
            try:
                os.close(slave_fd)
            except OSError:
                pass
