"""Regression guard for bin/install-daemon-service.sh root resolution (#352).

Issue #352 item 4 is regression-only for this script: it already prompts
for ``BH_PROJECT_ROOT`` (interactively, via its own ``_bh_interactive``
TTY check) only when unresolved through ``bin/lib/load-config.sh``'s
chain, and writes no env/config file of its own that the reuse-vs-
overwrite feature applies to. This suite pins the one half of that
contract observable without a real tty.

Confirmed via a black-box run of the real script (isolated
HOME/XDG_CONFIG_HOME, ``BH_SETUP_NO_PROMPT=1``, no ``--project-root``):
it exits 1 with a ``BH_PROJECT_ROOT could not be resolved`` error on
stderr -- this must be unaffected by the reuse-vs-overwrite refactor
elsewhere in the harness's setup scripts.

Not covered here (explicitly, not silently dropped): the ordering
guarantee that the ``BH_PROJECT_ROOT`` prompt occurs before the
``BWS_ACCESS_TOKEN`` prompt requires driving the script through a real
interactive session over a pty (see ``tests/_bh_pty.py``), and this
suite intentionally does not guess the exact wording of the
``BWS_ACCESS_TOKEN`` prompt to assert an ordering against it -- that
text was never independently confirmed via a black-box run (unlike the
non-interactive message below, which was). That ordering guarantee is a
known test gap; see the return summary for this agent's task.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[1]
SCRIPT = HARNESS / "bin" / "install-daemon-service.sh"

_GIT_BASH = Path("C:/Program Files/Git/usr/bin/bash.exe")
if sys.platform == "win32" and _GIT_BASH.exists():
    _BASH = str(_GIT_BASH)
else:
    _BASH = "bash"
_BASH_BIN_DIR = str(Path(_BASH).parent) if Path(_BASH).exists() else ""


def test_non_interactive_unresolved_root_fails_closed(tmp_path: Path) -> None:
    """Unresolved BH_PROJECT_ROOT + non-interactive must fail as today.

    Isolates HOME/XDG_CONFIG_HOME so no real operator host.env on this
    machine can supply BH_PROJECT_ROOT and mask the fatal path under
    test.
    """
    home = tmp_path / "home"
    home.mkdir()
    xdg_config_home = tmp_path / "xdg_config"
    xdg_config_home.mkdir()

    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("BH_PROJECT_ROOT", "BH_SETUP_NO_PROMPT")
    }
    env["PATH"] = os.pathsep.join(
        part for part in [_BASH_BIN_DIR, env.get("PATH", "")] if part
    )
    env["HOME"] = home.as_posix()
    env["XDG_CONFIG_HOME"] = xdg_config_home.as_posix()
    env["BH_SETUP_NO_PROMPT"] = "1"

    proc = subprocess.run(
        [_BASH, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=30,
    )

    assert proc.returncode == 1, (
        f"expected exit 1 when BH_PROJECT_ROOT cannot be resolved "
        f"non-interactively; got rc={proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "BH_PROJECT_ROOT could not be resolved" in proc.stderr, (
        "the existing fail-closed error for an unresolved "
        "BH_PROJECT_ROOT in a non-interactive session must be "
        f"unaffected by the #352 refactor; stderr was:\n{proc.stderr!r}"
    )
