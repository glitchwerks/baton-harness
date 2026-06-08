"""symphony/log.py — Structured terminal logging."""
from __future__ import annotations

import logging
import sys
from datetime import datetime


class SymphonyFormatter(logging.Formatter):
    COLORS = {
        "START": "\033[32m",   # green
        "DONE": "\033[36m",    # cyan
        "FAIL": "\033[31m",    # red
        "POLL": "\033[34m",    # blue
        "RUN": "\033[33m",     # yellow
        "CLOSE": "\033[36m",   # cyan
        "CLEAN": "\033[90m",   # grey
        "RELEASE": "\033[90m", # grey
        "IDLE": "\033[90m",    # grey
        "RECONCILE": "\033[35m",  # magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now().strftime("%H:%M:%S")
        msg = record.getMessage()

        # Colorize known prefixes
        for prefix, color in self.COLORS.items():
            if msg.startswith(prefix):
                msg = f"{color}{msg}{self.RESET}"
                break

        if record.levelno >= logging.ERROR and not any(msg.startswith(p) for p in self.COLORS):
            msg = f"\033[31m{msg}{self.RESET}"

        return f"[{ts}] {msg}"


def setup_logging(verbose: bool = False) -> None:
    logger = logging.getLogger("symphony")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(SymphonyFormatter())
    logger.addHandler(handler)
