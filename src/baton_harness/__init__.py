"""Temporary import compatibility for :mod:`codereeve`."""

import warnings

from codereeve import __version__

warnings.warn(
    "baton_harness is deprecated; use codereeve; removed in 0.4.0",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["__version__"]
