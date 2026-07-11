"""Guard: every subprocess spawn in chain/ must pass an explicit env=.

Standalone by design (importable/runnable without the rest of the
identity-broker test suite) so it can double as a quick pre-migration
sanity check.

Coverage:
- ``_find_unguarded_spawn_calls`` (the AST-walking helper this guard is
  built on) flags a bare ``subprocess.run``/``Popen``/``call`` call that
  omits ``env=``.
- The helper does NOT flag a call that already passes ``env=``.
- The helper honours the ``# identity: env-exempt`` trailing-comment
  marker as an explicit, per-call-site exemption for genuinely
  env-agnostic spawns (e.g. a liveness ``pgrep`` probe).
- The helper's failure output names the offending file and line number
  so a human/implementer can jump straight to the site.
- The real guard: walking the actual
  ``src/baton_harness/chain/`` package must find zero unexempted
  spawn sites. This is expected to FAIL until Phase 2 migrates the
  pre-existing bare ``subprocess.run`` call sites (e.g.
  ``branches.py``, ``cli.py``, ``reconcile.py``, ``ruleset_status.py``,
  ``sandbox_config.py``) to build their env via
  ``chain.identity.env_for`` (or otherwise pass an explicit ``env=``).

Exemption-mechanism contract pinned by this file:
- A spawn call is exempted by appending the literal comment
  ``# identity: env-exempt`` on the same source line as the
  ``subprocess.run(``/``Popen(``/``call(`` call. No separate allowlist
  file or config is required; the implementer marks genuinely
  env-agnostic call sites (e.g. a ``pgrep`` liveness probe with no
  credential surface) directly at the call site so the exemption is
  visible in code review.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SPAWN_ATTRS = frozenset({"run", "Popen", "call"})
_EXEMPT_MARKER = "# identity: env-exempt"


def _find_unguarded_spawn_calls(root: Path) -> list[str]:
    """Return ``"file:line"`` strings for spawn calls missing ``env=``.

    Walks every ``.py`` file directly under ``root`` (the chain package
    is flat, so this is intentionally non-recursive) looking for
    ``subprocess.run``/``subprocess.Popen``/``subprocess.call`` calls
    that do not pass an explicit ``env=`` keyword argument. A call is
    exempted if its source line contains the ``# identity: env-exempt``
    marker comment.

    Args:
        root: Directory containing the ``.py`` files to scan.

    Returns:
        A list of ``"path:lineno"`` strings, one per unexempted spawn
        call missing ``env=``. Empty if every spawn call is compliant.
    """
    violations: list[str] = []
    for py_file in sorted(root.glob("*.py")):
        source = py_file.read_text(encoding="utf-8")
        lines = source.splitlines()
        tree = ast.parse(source, filename=str(py_file))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_subprocess_spawn = (
                isinstance(func, ast.Attribute)
                and func.attr in _SPAWN_ATTRS
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
            )
            if not is_subprocess_spawn:
                continue
            if any(kw.arg == "env" for kw in node.keywords):
                continue
            line_text = (
                lines[node.lineno - 1] if node.lineno <= len(lines) else ""
            )
            if _EXEMPT_MARKER in line_text:
                continue
            violations.append(f"{py_file}:{node.lineno}")
    return violations


# ---------------------------------------------------------------------------
# Helper unit tests (synthetic sources — these pass regardless of
# migration state; they pin the guard logic itself)
# ---------------------------------------------------------------------------


class TestFindUnguardedSpawnCallsHelper:
    """Unit tests for the AST-walking helper against synthetic sources."""

    def test_flags_call_missing_env(self, tmp_path: Path) -> None:
        """A bare ``subprocess.run`` with no ``env=`` must be flagged."""
        src = "import subprocess\n\n\ndef f():\n    subprocess.run(['ls'])\n"
        (tmp_path / "fake_mod.py").write_text(src, encoding="utf-8")

        violations = _find_unguarded_spawn_calls(tmp_path)

        assert len(violations) == 1
        assert violations[0].endswith("fake_mod.py:5")

    def test_ignores_call_with_explicit_env(self, tmp_path: Path) -> None:
        """A ``subprocess.run`` call that passes ``env=`` must pass."""
        src = (
            "import subprocess\n\n\n"
            "def f():\n"
            "    subprocess.run(['ls'], env={})\n"
        )
        (tmp_path / "fake_mod.py").write_text(src, encoding="utf-8")

        assert _find_unguarded_spawn_calls(tmp_path) == []

    def test_respects_exemption_marker_comment(self, tmp_path: Path) -> None:
        """The exemption marker must suppress a violation.

        Applies to that ``# identity: env-exempt``-marked call site
        only.
        """
        src = (
            "import subprocess\n\n\n"
            "def f():\n"
            "    subprocess.run(['ls'])  # identity: env-exempt\n"
        )
        (tmp_path / "fake_mod.py").write_text(src, encoding="utf-8")

        assert _find_unguarded_spawn_calls(tmp_path) == []

    def test_flags_popen_and_call_variants(self, tmp_path: Path) -> None:
        """Non-``run`` spawn functions are covered too.

        ``subprocess.Popen`` and ``subprocess.call``, not just
        ``subprocess.run``.
        """
        src = (
            "import subprocess\n\n\n"
            "def f():\n"
            "    subprocess.Popen(['ls'])\n"
            "    subprocess.call(['ls'])\n"
        )
        (tmp_path / "fake_mod.py").write_text(src, encoding="utf-8")

        violations = _find_unguarded_spawn_calls(tmp_path)

        assert len(violations) == 2

    def test_ignores_non_subprocess_calls(self, tmp_path: Path) -> None:
        """Non-``subprocess`` calls named ``run``/``call`` must pass.

        Only calls whose receiver is literally named ``subprocess``
        count as spawn calls.
        """
        src = (
            "import subprocess\n\n\n"
            "class Runner:\n"
            "    def run(self, cmd):\n"
            "        return None\n\n\n"
            "def f():\n"
            "    Runner().run(['ls'])\n"
        )
        (tmp_path / "fake_mod.py").write_text(src, encoding="utf-8")

        assert _find_unguarded_spawn_calls(tmp_path) == []


# ---------------------------------------------------------------------------
# The real guard
# ---------------------------------------------------------------------------


class TestChainPackageSpawnGuard:
    """Every real spawn site in ``chain/`` must pass an explicit env.

    Covers the actual ``src/baton_harness/chain/`` package, not a
    synthetic fixture.
    """

    def test_no_unexempted_spawn_sites_in_chain_package(self) -> None:
        """No un-exempted spawn call in ``chain/`` may omit ``env=``.

        Covers ``subprocess.run``/``Popen``/``call``. Expected to FAIL
        until Phase 2 migrates the pre-existing bare
        call sites to build their env via ``chain.identity.env_for``
        (or otherwise pass an explicit ``env=``), and marks any
        genuinely env-agnostic call (e.g. a liveness ``pgrep`` probe)
        with a trailing ``# identity: env-exempt`` comment.
        """
        chain_root = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "baton_harness"
            / "chain"
        )

        violations = _find_unguarded_spawn_calls(chain_root)

        assert not violations, (
            "Found subprocess spawn(s) in chain/ without an explicit "
            "`env=` kwarg. Build the env via "
            "`chain.identity.env_for(...)` (or otherwise pass an "
            "explicit `env=`), or mark a genuinely env-agnostic call "
            "with a trailing `# identity: env-exempt` comment. "
            "Offending site(s): " + ", ".join(violations)
        )
