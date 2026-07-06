"""Tests for a ``required_checks`` WORKFLOW.md field — issue #225.

The CI merge gate's required-check set is hardcoded today
(``baton_harness.chain.merge.REQUIRED_CHECKS``) to the harness's OWN CI
job names.  Issue #225 adds an operator-facing override: a
``required_checks`` entry in ``config/WORKFLOW.md`` that, when set,
supplies the merge gate's required-check list instead of the hardcoded
default.

Seam choice (documented, not guessed):
    The router's briefing frames this as a ``RepoConfig``
    (``chain/registry.py``) field "parsed from ``config/WORKFLOW.md``".
    As of this writing ``RepoConfig.``/``load_registry()`` is sourced
    ENTIRELY from environment variables (``BH_REPO_OWNER`` /
    ``BH_REPO_NAME`` / ``BH_PROJECT_ROOT``) — it never reads
    ``WORKFLOW.md``.  The actual WORKFLOW.md front-matter parser is
    ``vendor.symphony.config.load_workflow``, which already produces the
    ``WorkflowConfig`` threaded into the daemon's per-work-unit runner
    (``daemon._run_work_unit(config: WorkflowConfig, repo_cfg:
    RepoConfig, ...)``) — the object actually in scope at both
    ``merge_issue_branch`` call sites.  These tests therefore pin
    ``required_checks`` on ``WorkflowConfig`` / ``load_workflow``, the
    seam that can actually reach the merge gate.  Flagged for the
    router/code-implementer to reconcile against the briefing's
    ``RepoConfig`` framing before implementation.

    The exact front-matter YAML location for ``required_checks:`` is
    likewise not pinned by the spec.  These tests assume a TOP-LEVEL key
    (a sibling of ``tracker:`` / ``polling:`` / ``agent:`` / ``hooks:``),
    reasoning that the required-check set is a repo/CI-level concern,
    not an agent-dispatch concern.  Flagged the same way.

Coverage:
- ``WorkflowConfig`` exposes a ``required_checks: list[str]`` field.
- The field defaults to an empty list (falsy — the "unset" sentinel)
  when not supplied, not ``None``.
- ``load_workflow`` parses a top-level ``required_checks:`` front-matter
  list onto ``WorkflowConfig.required_checks``.
- ``load_workflow`` on a WORKFLOW.md with no ``required_checks:`` key
  yields the empty-list default (not a ``KeyError``, not ``None``).
- ``load_workflow`` on a mixed-type ``required_checks:`` list (#229)
  drops non-string elements, keeping only the string check names.
"""

from __future__ import annotations

from pathlib import Path

from baton_harness.vendor.symphony.config import (
    WorkflowConfig,
    load_workflow,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_workflow(tmp_path: Path, content: str) -> str:
    """Write ``content`` to a ``WORKFLOW.md`` file under ``tmp_path``.

    Args:
        tmp_path: The pytest-provided temporary directory.
        content: The full WORKFLOW.md file contents (front matter + body).

    Returns:
        The absolute path to the written file, as a string (``load_workflow``
        takes a ``str`` path).
    """
    path = tmp_path / "WORKFLOW.md"
    path.write_text(content, encoding="utf-8")
    return str(path)


_FRONT_MATTER_NO_REQUIRED_CHECKS = (
    "---\n"
    "tracker:\n"
    "  kind: github\n"
    '  labels: ["agent-ready"]\n'
    "polling:\n"
    "  interval_ms: 30000\n"
    "agent:\n"
    "  max_concurrent: 1\n"
    "  max_turns: 5\n"
    "---\n"
    "Prompt body.\n"
)

_FRONT_MATTER_WITH_REQUIRED_CHECKS = (
    "---\n"
    "tracker:\n"
    "  kind: github\n"
    '  labels: ["agent-ready"]\n'
    "required_checks:\n"
    '  - "My CI"\n'
    '  - "Other Check"\n'
    "---\n"
    "Prompt body.\n"
)

_FRONT_MATTER_WITH_SCALAR_REQUIRED_CHECKS = (
    "---\n"
    "tracker:\n"
    "  kind: github\n"
    '  labels: ["agent-ready"]\n'
    'required_checks: "My CI"\n'
    "---\n"
    "Prompt body.\n"
)

_FRONT_MATTER_WITH_MIXED_TYPE_REQUIRED_CHECKS = (
    "---\n"
    "tracker:\n"
    "  kind: github\n"
    '  labels: ["agent-ready"]\n'
    "required_checks:\n"
    "  - 123\n"
    '  - "My CI"\n'
    "---\n"
    "Prompt body.\n"
)


# ---------------------------------------------------------------------------
# WorkflowConfig.required_checks field
# ---------------------------------------------------------------------------


class TestWorkflowConfigRequiredChecksField:
    """``WorkflowConfig`` must expose a ``required_checks: list[str]``."""

    def test_field_accepts_a_list_of_check_names(self) -> None:
        """Constructing ``WorkflowConfig(required_checks=[...])`` succeeds.

        MUST FAIL now: ``WorkflowConfig`` has no such field yet, so this
        raises ``TypeError: __init__() got an unexpected keyword argument
        'required_checks'``.
        """
        cfg = WorkflowConfig(required_checks=["My CI"])
        assert cfg.required_checks == ["My CI"]

    def test_default_is_empty_list_when_not_supplied(self) -> None:
        """Omitting ``required_checks`` yields ``[]``, not ``None``.

        An empty list is the "unset" sentinel the merge gate's fallback
        path is specified to check against (falsy) — this pins the
        empty-list shape specifically so ``not cfg.required_checks`` is
        the intended unset check, not an ``is None`` check.
        """
        cfg = WorkflowConfig()
        assert cfg.required_checks == []


# ---------------------------------------------------------------------------
# load_workflow() front-matter parsing
# ---------------------------------------------------------------------------


class TestLoadWorkflowRequiredChecksParsing:
    """``load_workflow()`` parses a ``required_checks:`` front-matter key."""

    def test_parses_required_checks_list_from_front_matter(
        self, tmp_path: Path
    ) -> None:
        """A top-level ``required_checks:`` list parses onto the config.

        MUST FAIL now: ``load_workflow`` has no code path that reads a
        ``required_checks`` key, so the parsed config's
        ``required_checks`` stays at whatever (currently nonexistent)
        default applies — this assertion cannot pass until both the
        field and the parse wiring exist.
        """
        path = _write_workflow(tmp_path, _FRONT_MATTER_WITH_REQUIRED_CHECKS)

        cfg = load_workflow(path)

        assert cfg.required_checks == ["My CI", "Other Check"]

    def test_absent_required_checks_key_yields_empty_default(
        self, tmp_path: Path
    ) -> None:
        """A WORKFLOW.md with no ``required_checks:`` key yields ``[]``.

        This is the "operator did not override" case the merge gate's
        fallback-to-``REQUIRED_CHECKS`` + warning behavior is keyed on.
        """
        path = _write_workflow(tmp_path, _FRONT_MATTER_NO_REQUIRED_CHECKS)

        cfg = load_workflow(path)

        assert cfg.required_checks == []

    def test_scalar_required_checks_falls_back_to_empty_default(
        self, tmp_path: Path
    ) -> None:
        """A non-list scalar ``required_checks:`` value parses to ``[]``.

        Regression test (CodeRabbit review, PR #228, VP-8): a front-matter
        typo like ``required_checks: "My CI"`` (a YAML string, not a list)
        is truthy and would otherwise pass straight through to the merge
        gate, which iterates it char-by-char -- silently reproducing the
        fail-closed "no matching jobs -> timeout" failure this feature
        exists to fix. ``load_workflow`` must reject any non-list scalar
        and fall back to the empty-list "unset" default instead.
        """
        path = _write_workflow(
            tmp_path, _FRONT_MATTER_WITH_SCALAR_REQUIRED_CHECKS
        )

        cfg = load_workflow(path)

        assert cfg.required_checks == []

    def test_mixed_type_list_drops_non_string_elements(
        self, tmp_path: Path
    ) -> None:
        """A mixed-type ``required_checks:`` list drops non-string items.

        Regression test, issue #229: ``load_workflow`` only guards the
        OUTER container type (``_raw if isinstance(_raw, list) else
        []``) -- a per-element type check is missing. A front-matter
        typo like ``required_checks: [123, "My CI"]`` currently passes
        the non-string ``123`` straight through, unfiltered, since the
        outer value is already a list. ``123`` can never match a real
        GitHub check name, silently reproducing the fail-closed "no
        matching jobs -> issue parks forever" symptom (#225) for a
        single mistyped element. ``load_workflow`` must filter the list
        to keep only ``str`` elements.

        MUST FAIL now: the current code keeps ``123`` unfiltered, so
        ``cfg.required_checks`` is ``[123, "My CI"]``, not ``["My CI"]``.
        """
        path = _write_workflow(
            tmp_path, _FRONT_MATTER_WITH_MIXED_TYPE_REQUIRED_CHECKS
        )

        cfg = load_workflow(path)

        assert cfg.required_checks == ["My CI"]
