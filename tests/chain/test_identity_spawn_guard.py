"""Guard: every subprocess spawn in chain/ must pass an explicit env=.

Standalone by design (importable/runnable without the rest of the
identity-broker test suite) so it can double as a quick post-migration
sanity check.

Coverage:
- ``_find_unguarded_spawn_calls`` (the AST-walking helper this guard is
  built on) flags a bare
  ``subprocess.run``/``Popen``/``call``/``check_call``/``check_output``
  call that omits ``env=``.
- The helper does NOT flag a call that passes ``env=`` with a
  non-``None`` expression (a name, a call, a dict literal, ...).
- The helper DOES flag a call that passes ``env=None`` literally — that
  spells "inherit the ambient ``os.environ``", the exact bypass this
  guard exists to prevent, so a literal ``None`` is a violation, not
  compliance.
- The helper resolves subprocess access through import aliases: both
  ``import subprocess as sp; sp.run(...)`` and
  ``from subprocess import run; run(...)`` (with or without ``as``)
  are recognized, not just the literal ``subprocess.run(...)`` spelling.
- The helper honours the ``# identity: env-exempt`` trailing-comment
  marker as an explicit, per-call-site exemption for genuinely
  env-agnostic spawns (e.g. a liveness ``pgrep`` probe) — but only when
  the marker is an actual comment token on one of the call's source
  lines, never when the same text merely appears inside a string
  argument.
- The helper's failure output names the offending file and line number
  so a human/implementer can jump straight to the site.
- The helper fails closed: a missing or empty scan root raises rather
  than silently reporting zero violations.
- The real guard: walking the actual ``src/baton_harness/chain/``
  package must find zero unexempted spawn sites. This is the
  steady-state post-migration invariant — Phase 2 already migrated
  every pre-existing bare ``subprocess.run`` call site (e.g.
  ``branches.py``, ``cli.py``, ``reconcile.py``, ``ruleset_status.py``,
  ``sandbox_config.py``) to build their env via
  ``chain.identity.env_for`` (or otherwise pass an explicit ``env=``),
  so this test is expected to PASS and stay green; a future
  regression that reintroduces an un-``env=``'d spawn must trip it.

Exemption-mechanism contract pinned by this file:
- A spawn call is exempted only by an actual **comment token**
  carrying the literal marker ``# identity: env-exempt`` on one of the
  source lines the call spans (``node.lineno`` through
  ``node.end_lineno``). No separate allowlist file or config is
  required; the implementer marks genuinely env-agnostic call sites
  (e.g. a ``pgrep`` liveness probe with no credential surface)
  directly at the call site so the exemption is visible in code
  review. Critically, the marker text appearing inside a string
  literal argument (e.g. a command-line argument that happens to
  contain the marker words) is NOT a comment token and must NOT
  exempt the call — see
  ``test_marker_inside_string_argument_does_not_exempt``.
"""

from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

import pytest

_SPAWN_ATTRS = frozenset(
    {"run", "Popen", "call", "check_call", "check_output"}
)
_EXEMPT_MARKER = "# identity: env-exempt"


def _collect_comment_lines(source: str) -> dict[int, str]:
    """Map each source line number to its comment-token text, if any.

    Uses the ``tokenize`` module so only genuine ``#``-comment tokens
    are considered — a string literal that happens to contain the same
    characters is a different token type and is never included here.

    Args:
        source: The full source text of a Python file.

    Returns:
        A dict mapping 1-based line number to the comment token's
        string (including the leading ``#``) for every ``COMMENT``
        token found. Lines with no comment token are absent.
    """
    comments: dict[int, str] = {}
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            comments[tok.start[0]] = tok.string
    return comments


def _resolve_subprocess_aliases(
    tree: ast.Module,
) -> tuple[set[str], dict[str, str]]:
    """Resolve local names bound to the ``subprocess`` module or funcs.

    Handles ``import subprocess`` / ``import subprocess as <alias>``
    (module-level aliasing, used with attribute-style calls like
    ``sp.run(...)``) and ``from subprocess import <func>[, ...]`` /
    ``from subprocess import <func> as <alias>`` (name-level aliasing,
    used with bare calls like ``run(...)``).

    Args:
        tree: The parsed module AST to inspect for import statements.

    Returns:
        A 2-tuple of:
        - The set of local names bound to the ``subprocess`` module
          itself (e.g. ``{"subprocess", "sp"}``).
        - A dict mapping each local name bound via
          ``from subprocess import ...`` to the real spawn-function
          name it refers to (e.g. ``{"run": "run", "r": "run"}``).
    """
    module_aliases: set[str] = set()
    func_aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "subprocess":
                for alias in node.names:
                    if alias.name in _SPAWN_ATTRS:
                        func_aliases[alias.asname or alias.name] = alias.name
    return module_aliases, func_aliases


def _is_none_literal(value: ast.expr) -> bool:
    """Return whether an AST expression is the literal ``None``.

    Args:
        value: The expression node to inspect (e.g. a keyword
            argument's value).

    Returns:
        True if ``value`` is exactly the ``None`` constant. False for
        any other expression, including names/calls that might
        evaluate to ``None`` at runtime — this is a static, syntactic
        check only.
    """
    return isinstance(value, ast.Constant) and value.value is None


def _find_unguarded_spawn_calls(root: Path) -> list[str]:
    """Return ``"file:line"`` strings for spawn calls missing ``env=``.

    Walks every ``.py`` file directly under ``root`` (the chain package
    is flat, so this is intentionally non-recursive) looking for
    ``subprocess.run``/``Popen``/``call``/``check_call``/
    ``check_output`` calls — including via a module import alias
    (``import subprocess as sp``) or a ``from subprocess import ...``
    binding — that do not pass an explicit, non-``None`` ``env=``
    keyword argument. A call that passes ``env=None`` literally is
    treated the same as a missing ``env=``: it means "inherit the
    ambient environment", the exact bypass this guard exists to catch.
    A call is exempted only if a genuine comment token carrying the
    ``# identity: env-exempt`` marker appears on one of the lines the
    call's source spans.

    Args:
        root: Directory containing the ``.py`` files to scan. Must
            exist and contain at least one ``.py`` file.

    Returns:
        A list of ``"path:lineno"`` strings, one per unexempted spawn
        call missing ``env=``. Empty if every spawn call is compliant.

    Raises:
        FileNotFoundError: If ``root`` does not exist or is not a
            directory. A missing scan root must fail the guard, not
            silently report zero violations.
        ValueError: If ``root`` exists but contains no ``.py`` files.
            An empty scan root is equally suspicious (e.g. a path
            typo) and must fail the guard rather than pass it
            vacuously.
    """
    if not root.is_dir():
        raise FileNotFoundError(
            f"Spawn-guard scan root does not exist: {root}"
        )

    py_files = sorted(root.glob("*.py"))
    if not py_files:
        raise ValueError(
            f"Spawn-guard scan root contains no .py files to scan "
            f"(refusing to pass vacuously): {root}"
        )

    violations: list[str] = []
    for py_file in py_files:
        source = py_file.read_text(encoding="utf-8")
        comment_lines = _collect_comment_lines(source)
        tree = ast.parse(source, filename=str(py_file))
        module_aliases, func_aliases = _resolve_subprocess_aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                is_subprocess_spawn = (
                    func.attr in _SPAWN_ATTRS
                    and isinstance(func.value, ast.Name)
                    and func.value.id in module_aliases
                )
            elif isinstance(func, ast.Name):
                is_subprocess_spawn = func.id in func_aliases
            else:
                is_subprocess_spawn = False
            if not is_subprocess_spawn:
                continue
            env_kw = next(
                (kw for kw in node.keywords if kw.arg == "env"), None
            )
            if env_kw is not None and not _is_none_literal(env_kw.value):
                continue
            end_lineno = node.end_lineno or node.lineno
            is_exempted = any(
                _EXEMPT_MARKER in comment_lines.get(lineno, "")
                for lineno in range(node.lineno, end_lineno + 1)
            )
            if is_exempted:
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

    def test_env_none_literal_is_flagged(self, tmp_path: Path) -> None:
        """``env=None`` must be flagged, not treated as compliant.

        ``env=None`` means "inherit the ambient ``os.environ``" — the
        exact bypass this guard exists to prevent. Only the *presence*
        of the ``env=`` keyword used to be checked, so a literal
        ``None`` slipped through as if it were a real environment.
        """
        src = (
            "import subprocess\n\n\n"
            "def f():\n"
            "    subprocess.run(['ls'], env=None)\n"
        )
        (tmp_path / "fake_mod.py").write_text(src, encoding="utf-8")

        violations = _find_unguarded_spawn_calls(tmp_path)

        assert len(violations) == 1
        assert violations[0].endswith("fake_mod.py:5")

    def test_env_non_none_value_is_compliant(self, tmp_path: Path) -> None:
        """A non-``None`` ``env=`` expression (e.g. a name) must pass.

        Only a literal ``None`` is a violation — a name, call, or dict
        expression passed as ``env=`` is compliant regardless of what
        it evaluates to at runtime, since the guard is a static check.
        """
        src = (
            "import subprocess\n\n\n"
            "def f():\n"
            "    some_env = {'PATH': '/usr/bin'}\n"
            "    subprocess.run(['ls'], env=some_env)\n"
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

    def test_marker_inside_string_argument_does_not_exempt(
        self, tmp_path: Path
    ) -> None:
        """The marker text as a string-literal argument must NOT exempt.

        Regression for a substring-match bug: ``_EXEMPT_MARKER in
        line_text`` used to match the marker string when it appeared
        inside a command argument, falsely exempting a real
        un-``env=``'d spawn. Only an actual comment token counts.
        """
        src = (
            "import subprocess\n\n\n"
            "def f():\n"
            '    subprocess.run(["# identity: env-exempt"])\n'
        )
        (tmp_path / "fake_mod.py").write_text(src, encoding="utf-8")

        violations = _find_unguarded_spawn_calls(tmp_path)

        assert len(violations) == 1
        assert violations[0].endswith("fake_mod.py:5")

    def test_flags_popen_and_call_variants(self, tmp_path: Path) -> None:
        """Non-``run`` spawn functions are covered too.

        ``subprocess.Popen``, ``subprocess.call``,
        ``subprocess.check_call``, and ``subprocess.check_output`` —
        not just ``subprocess.run``.
        """
        src = (
            "import subprocess\n\n\n"
            "def f():\n"
            "    subprocess.Popen(['ls'])\n"
            "    subprocess.call(['ls'])\n"
            "    subprocess.check_call(['ls'])\n"
            "    subprocess.check_output(['ls'])\n"
        )
        (tmp_path / "fake_mod.py").write_text(src, encoding="utf-8")

        violations = _find_unguarded_spawn_calls(tmp_path)

        assert len(violations) == 4

    def test_resolves_module_import_alias(self, tmp_path: Path) -> None:
        """``import subprocess as sp; sp.run(...)`` must be resolved.

        The matcher must not be fooled by an aliased module import —
        ``sp.run(...)`` is exactly as much of an ambient-inheritance
        bypass risk as ``subprocess.run(...)``.
        """
        src = "import subprocess as sp\n\n\ndef f():\n    sp.run(['ls'])\n"
        (tmp_path / "fake_mod.py").write_text(src, encoding="utf-8")

        violations = _find_unguarded_spawn_calls(tmp_path)

        assert len(violations) == 1
        assert violations[0].endswith("fake_mod.py:5")

    def test_resolves_from_import(self, tmp_path: Path) -> None:
        """``from subprocess import run; run(...)`` must be resolved.

        A bare call to a name imported directly from ``subprocess``
        bypasses the ``subprocess.<attr>`` attribute-access pattern
        entirely, so it must be tracked through the ``from`` import
        binding instead.
        """
        src = "from subprocess import run\n\n\ndef f():\n    run(['ls'])\n"
        (tmp_path / "fake_mod.py").write_text(src, encoding="utf-8")

        violations = _find_unguarded_spawn_calls(tmp_path)

        assert len(violations) == 1
        assert violations[0].endswith("fake_mod.py:5")

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

    def test_missing_scan_root_fails_closed(self, tmp_path: Path) -> None:
        """A nonexistent scan root must raise, not pass vacuously.

        Regression: a nonexistent ``chain_root`` used to yield zero
        glob matches, so the guard "passed" without scanning anything.
        """
        missing_root = tmp_path / "does-not-exist"

        with pytest.raises(FileNotFoundError):
            _find_unguarded_spawn_calls(missing_root)

    def test_empty_scan_root_fails_closed(self, tmp_path: Path) -> None:
        """An existing-but-empty scan root must raise, not pass.

        An empty directory (e.g. from a path typo pointing one level
        too deep) is equally capable of vacuously "passing" the guard
        and must be rejected the same way as a missing root.
        """
        with pytest.raises(ValueError):
            _find_unguarded_spawn_calls(tmp_path)


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

        Covers ``subprocess.run``/``Popen``/``call``/``check_call``/
        ``check_output``, resolved through import aliases too, and
        rejects a literal ``env=None`` as equivalent to a missing
        ``env=``. This is the steady-state post-migration invariant:
        every real spawn site
        already builds its env via ``chain.identity.env_for`` (or
        otherwise passes an explicit ``env=``), or marks a genuinely
        env-agnostic call (e.g. a liveness ``pgrep`` probe) with a
        trailing ``# identity: env-exempt`` comment. This test must
        stay green; a future spawn added without ``env=`` is the
        regression it exists to catch.
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
