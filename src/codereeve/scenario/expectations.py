"""Declarative expectations for supported daemon scenarios."""

from __future__ import annotations

from typing import Any

EXPECTATIONS: dict[str, dict[str, Any]] = {
    "block-ambiguity": {
        "issue": {
            "outcome": "parked",
            "park_kind": "block",
            "park_reason_present": True,
            "escalations_include": [{"kind": "block"}],
        },
    },
    "clean-implement": {
        "issue": {"outcome": ["merged", "pr_open"], "pr_present": True},
    },
    "ci-fail": {
        "issue": {
            "merge_gate": {"outcome": "CI_FAILED"},
            "outcome_not": "merged",
        },
    },
    "terminal-block": {
        "issues_len": 1,
        "issue": {"outcome": "skipped_blocked"},
    },
    "recovery": {
        "startup": {"findings_include_gates": ["G2"]},
    },
}
