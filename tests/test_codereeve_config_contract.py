"""Executable configuration contracts for the 0.2/0.3 compatibility window.

Scan production Python, shell, workflow and configuration interfaces. Test
fixtures and historical prose are deliberately outside this executable scope.
Exceptions identify individual interfaces and expire with compatibility in
0.4.0; no source file is exempt from inspection.
"""

from __future__ import annotations

import ast
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from codereeve.config_env import PRODUCT_ALIASES
from codereeve.paths import PathLayout

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.fast

# #394 owns the service installer cutover. This single interface exception
# expires in 0.4.0; it does not exempt any other interface in the installer.
ENV_COMPATIBILITY = {
    ("bin/install-daemon-service.sh", "BH_DAEMON_SECRETS_PATH"),
}

# Read-only compatibility recognition and transactional old-default rewrite.
# Each exact (file, function, literal) exception expires in 0.4.0. An added
# literal in another function in these same modules is still a violation.
PATH_COMPATIBILITY = {
    ("src/codereeve/paths.py", "for_environment", ".baton-harness"),
    ("src/codereeve/paths.py", "for_environment", ".bh"),
    ("src/codereeve/paths.py", "for_environment", "baton-harness"),
    ("src/codereeve/paths.py", "for_environment", "bh-daemon"),
    ("src/codereeve/chain/doctor.py", "create_context", ".bh"),
    ("src/codereeve/chain/doctor.py", "_check_config_env", ".bh"),
    ("src/codereeve/chain/doctor.py", "_config_path", ".bh"),
    ("src/codereeve/chain/sandbox_config.py", "select_config_path", ".bh"),
    (
        "src/codereeve/migration/transaction.py",
        "_converted",
        ".baton-harness/",
    ),
}


def _production_files() -> list[Path]:
    """Return tracked executable/configuration surfaces, never Git history."""
    paths = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split("\0")
    return [
        ROOT / path
        for path in paths
        if path
        and (
            path.startswith(
                ("src/", "bin/", "config/", ".github/", "templates/")
            )
            or path == "hatch_build.py"
        )
        and Path(path).suffix
        in {
            ".py",
            ".sh",
            ".yml",
            ".yaml",
            ".env",
            ".md",
            ".template",
        }
    ]


def test_scan_includes_workflow_hooks_and_worker_template() -> None:
    """Distributed hooks and generated worker instructions are covered."""
    paths = {path.relative_to(ROOT).as_posix() for path in _production_files()}
    assert {"config/WORKFLOW.md", "templates/CLAUDE.md.template"} <= paths


def _interface_text(path: Path) -> str:
    """Read active code/config, or just frontmatter and fenced doc examples."""
    text = path.read_text(encoding="utf-8")
    if path.suffix not in {".md", ".template"}:
        return text
    pieces = re.findall(r"```[^\n]*\n(.*?)```", text, re.DOTALL)
    if text.startswith("---\n"):
        pieces.append(text.split("---\n", 2)[1])
    return "\n".join(pieces)


def test_document_scan_uses_frontmatter_and_examples(tmp_path: Path) -> None:
    """Historical prose does not become an executable interface occurrence."""
    path = tmp_path / "WORKFLOW.md"
    path.write_text(
        '---\nhooks:\n  before_run: echo "$BH_HOOK"\n---\n'
        "History $BH_HISTORY\n```bash\nexport BH_EXAMPLE=1\n```\n",
        encoding="utf-8",
    )
    assert _shell_interfaces(_interface_text(path)) == {
        "BH_HOOK",
        "BH_EXAMPLE",
    }


def test_all_product_environment_interfaces_are_registered() -> None:
    """Actual old/new environment names belong to the shared alias catalog."""
    registered = {
        name
        for pair in PRODUCT_ALIASES
        for name in (pair.canonical, pair.legacy)
    }
    missing: set[tuple[str, str]] = set()
    consumed: set[str] = set()
    for path in _production_files():
        text = _interface_text(path)
        names = (
            _python_interfaces(text)
            if path.suffix == ".py"
            else _shell_interfaces(text)
        )
        if path.suffix in {".yaml", ".yml", ".env"}:
            names.update(
                re.findall(
                    r"(?m)^\s*(?:export\s+)?([A-Z][A-Z_0-9]*)\s*[:=]", text
                )
            )
        consumed.update(names)
        missing.update(
            (path.relative_to(ROOT).as_posix(), name)
            for name in names
            if name.startswith(("BH_", "CODEREEVE_"))
            or name == "BATON_HARNESS_DIR"
            if name not in registered
        )
    assert missing == ENV_COMPATIBILITY, sorted(missing ^ ENV_COMPATIBILITY)
    docs = (ROOT / "README.md").read_text(encoding="utf-8") + (
        ROOT / "docs/codereeve-migration.md"
    ).read_text(encoding="utf-8")
    assert not {
        pair.canonical
        for pair in PRODUCT_ALIASES
        if pair.canonical not in consumed and pair.canonical not in docs
    }
    assert len(registered) == 2 * len(PRODUCT_ALIASES)
    assert not any(
        name.startswith(("BWS_", "GH_", "ANTHROPIC_")) for name in registered
    )
    assert ("CODEREEVE_ROOT", "BATON_HARNESS_DIR") in {
        (pair.canonical, pair.legacy) for pair in PRODUCT_ALIASES
    }


def test_shell_configuration_has_only_reviewed_execution_targets() -> None:
    """Only the loader and explicit authentication hooks execute."""
    allowed = {
        ("source", "${_codereeve_load_config}"),
        ("source", "${_BH_LOAD_CONFIG}"),
        ("eval", "${CODEREEVE_APP_AUTH_JWT_CMD}"),
        ("eval", "${CODEREEVE_APP_AUTH_TOKEN_CMD}"),
        (".", "$CODEREEVE_VENV/bin/activate"),
    }
    violations = {
        (path.relative_to(ROOT).as_posix(), command, target)
        for path in _production_files()
        if path.suffix != ".py"
        for command, target in _execution_targets(_interface_text(path))
        if (command, target) not in allowed
    }
    assert not violations, sorted(violations)


def test_legacy_path_literals_have_only_exact_compatibility_owners() -> None:
    """New legacy paths, including writers in allowed modules, are rejected."""
    actual = {
        (path.relative_to(ROOT).as_posix(), owner, literal)
        for path in _production_files()
        if path.suffix == ".py"
        for owner, literal in _legacy_literals(
            path.read_text(encoding="utf-8")
        )
    }
    assert actual == PATH_COMPATIBILITY, sorted(actual ^ PATH_COMPATIBILITY)


def test_canonical_layout_keeps_symphony_owned_state(tmp_path: Path) -> None:
    """CodeReeve paths never absorb the independently owned Symphony root."""
    layout = PathLayout.for_environment(tmp_path, {}, home=tmp_path)
    assert layout.canonical_state == tmp_path / ".codereeve"
    assert layout.canonical_config == tmp_path / ".codereeve/config.env"
    assert layout.canonical_host == tmp_path / ".config/codereeve/host.env"
    assert layout.symphony_state == tmp_path / ".symphony"
    for relative in ("bin/init-sandbox.sh", "bin/run-daemon.sh"):
        assert "'.symphony/'" in (ROOT / relative).read_text(encoding="utf-8")


def _python_interfaces(text: str) -> set[str]:
    """Find literal mapping reads/writes and shared alias resolver calls."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(text)):
        candidates: list[ast.expr] = []
        if isinstance(node, ast.Subscript):
            candidates = [node.slice]
        elif isinstance(node, ast.Call):
            function = ast.unparse(node.func).split(".")[-1]
            if function in {"get", "getenv", "pop", "setdefault"}:
                candidates = node.args[:1]
            elif function in {"resolve_alias_pair", "_compat_value"}:
                candidates = node.args[1:3]
        names.update(
            item.value
            for item in candidates
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        )
    return names


def _shell_interfaces(text: str) -> set[str]:
    """Find shell expansions, exports and persisted assignment interfaces."""
    names: set[str] = set()
    locals_: set[str] = set()
    for line in text.splitlines():
        # Keep double-quoted expansions; discard single-quoted literals and
        # comments. Include heredoc assignment interfaces emitted as config.
        visible = re.sub(
            r""""(?:\\.|[^"\\])*"|'[^']*'|#[^\n]*""",
            lambda match: match[0] if match[0].startswith('"') else "",
            line,
        )
        names.update(
            set(re.findall(r"\$\{?([A-Z][A-Z_0-9]*)", visible)) - locals_
        )
        names.update(
            re.findall(
                r"(?:\bexport\s+|\bEnvironment=)([A-Z][A-Z_0-9]*)=",
                visible,
            )
        )
        assignment = re.match(r"\s*([A-Z][A-Z_0-9]*)=(.*)$", visible)
        if assignment and not re.search(
            rf"\$\{{?{assignment[1]}\b", assignment[2]
        ):
            locals_.add(assignment[1])
    return names


def _execution_targets(text: str) -> set[tuple[str, str]]:
    """Find executable source/dot/eval operands, excluding quoted prose."""
    found: set[tuple[str, str]] = set()
    for line in _shell_code(text):
        # Inspect command substitutions before tokenizing outer quoting.
        for substitution in re.findall(r"\$\(([^()]*)\)", line):
            found.update(_execution_targets(substitution))
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";()&|<>")
        lexer.whitespace_split = True
        try:
            tokens = list(lexer)
        except ValueError:
            # Multiline quoted prose cannot start a shell execution command;
            # actual source/eval lines must remain lexically inspectable.
            assert not re.match(r"\s*(source|eval|\.)\s", line), line
            continue
        for index, (command, operand) in enumerate(
            zip(tokens, tokens[1:], strict=False)
        ):
            previous = tokens[index - 1] if index else ";"
            if command in {"source", ".", "eval"} and (
                previous
                in {
                    ";",
                    "&&",
                    "||",
                    "(",
                    "then",
                    "do",
                    "!",
                    "if",
                    "command",
                    "builtin",
                }
                or previous.endswith(":")
            ):
                found.add((command, operand))
    return found


def _legacy_literals(text: str) -> set[tuple[str, str]]:
    """Find legacy path literals with their Python function ownership."""
    tree = ast.parse(text)
    parents = {
        child: node
        for node in ast.walk(tree)
        for child in ast.iter_child_nodes(node)
    }
    found: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(
            node.value, str
        ):
            continue
        is_legacy_path = re.match(
            r"(?:\.bh|\.baton-harness)(?:/|$)|/etc/bh-daemon/|"
            r"(?:~/)?\.config/baton-harness/",
            node.value,
        )
        is_legacy_component = (
            node.value in {"baton-harness", "bh-daemon"}
            and isinstance(parents.get(node), ast.BinOp)
            and isinstance(parents[node].op, ast.Div)
        )
        if not is_legacy_path and not is_legacy_component:
            continue
        if any(character.isspace() for character in node.value):
            continue
        owner: ast.AST = node
        while owner in parents and not isinstance(
            owner, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            owner = parents[owner]
        name = getattr(owner, "name", "<module>")
        found.add((name, node.value))
    return found


def test_python_scan_excludes_comments_and_docstrings() -> None:
    """Mapping access and aliases are interfaces; prose is not."""
    text = '''
"""os.environ["BH_HISTORY"] was removed."""
# os.getenv("BH_COMMENT")
first = os.environ.get("BH_READ")
second = os.getenv("CODEREEVE_READ")
os.environ["BH_WRITE"] = "value"
value = environment.pop("BH_POP", None)
pair = resolve_alias_pair(values, "CODEREEVE_PAIR", "BH_PAIR")
build = _compat_value(values, "CODEREEVE_BUILD_PAIR", "BH_BUILD_PAIR")
message = "BH_DIAGNOSTIC"
'''
    assert _python_interfaces(text) == {
        "BH_READ",
        "CODEREEVE_READ",
        "BH_WRITE",
        "BH_POP",
        "CODEREEVE_PAIR",
        "BH_PAIR",
        "CODEREEVE_BUILD_PAIR",
        "BH_BUILD_PAIR",
    }


def test_shell_scan_tracks_interfaces_without_internal_locals() -> None:
    """Unknown exported variables fail while installer locals stay local."""
    text = """
# export BH_HISTORY=1
echo 'BH_PROSE is deprecated'
echo '$BH_QUOTED'
echo value # $BH_COMMENT
echo "$BH_EARLY_READ"
BH_EARLY_READ=overridden
BH_LOCAL=/a/local/binary
echo "${BH_LOCAL}"
_codereeve_private=1
export BH_EXPORT=1
value="${BH_READ:-default}"
value="$CODEREEVE_READ"
Environment=BH_SERVICE=value
"""
    assert _shell_interfaces(text) == {
        "BH_EXPORT",
        "BH_READ",
        "CODEREEVE_READ",
        "BH_SERVICE",
        "BH_EARLY_READ",
    }


@pytest.mark.parametrize(
    "command, expected",
    [
        ('source "${HOME}/host.env"', ("source", "${HOME}/host.env")),
        ('. "$config_file"', (".", "$config_file")),
        ('if value="$(eval "$contents")"; then', ("eval", "$contents")),
        ("true; source config.env", ("source", "config.env")),
    ],
)
def test_execution_scan_catches_direct_and_indirect_execution(
    command: str,
    expected: tuple[str, str],
) -> None:
    """Commands and substitutions cannot execute configuration text."""
    assert expected in _execution_targets(command)
    assert not _execution_targets(f"# {command}")
    assert not _execution_targets("echo 'source host.env'")


def test_legacy_scan_is_scoped_to_literal_and_function() -> None:
    """A new writer in a compatibility module cannot inherit an exception."""
    text = '''
"""Historic .baton-harness state."""
def permitted():
    return root / ".baton-harness"
def new_writer():
    return (root / ".bh" / "new.json").write_text("data")
def new_host_writer():
    return root / "baton-harness" / "host.env"
def new_secrets_writer():
    return Path("/etc/bh-daemon/secrets.env")
'''
    assert _legacy_literals(text) == {
        ("permitted", ".baton-harness"),
        ("new_writer", ".bh"),
        ("new_host_writer", "baton-harness"),
        ("new_secrets_writer", "/etc/bh-daemon/secrets.env"),
    }


def test_execution_scan_ignores_literal_help_heredoc() -> None:
    """Documented shell examples are data, not active source statements."""
    assert not _execution_targets("cat <<'HELP'\nsource host.env\nHELP\n")


def test_execution_scan_inspects_expanding_heredoc() -> None:
    """An unquoted heredoc can execute substitutions and must be checked."""
    assert ("source", "host.env") in _execution_targets(
        "cat <<HELP\n$(source host.env)\nHELP\n"
    )


def test_shell_legacy_paths_reject_new_writers() -> None:
    """Legacy default construction stays limited to #394 installer lines."""
    # Exact executable lines, not a whole installer exemption; expiry 0.4.0.
    allowed = {
        (
            "bin/install-daemon-service.sh",
            'if [[ ! -f "${BH_PROJECT_ROOT}/.bh/config.env" ]]; then',
        ),
        (
            "bin/install-daemon-service.sh",
            'BH_DAEMON_SECRETS_PATH="${BH_DAEMON_SECRETS_PATH:-/etc/bh-daemon/secrets.env}"',
        ),
    }
    actual = {
        (path.relative_to(ROOT).as_posix(), line.strip())
        for path in _production_files()
        if path.suffix == ".sh"
        for line in _shell_code(path.read_text(encoding="utf-8"))
        if not line.lstrip().startswith("echo ")
        and re.search(
            r"(?:\.bh|\.baton-harness)/|/etc/bh-daemon/|baton-harness/host.env",
            line,
        )
    }
    assert actual == allowed, sorted(actual ^ allowed)


def _shell_code(text: str) -> list[str]:
    """Select active shell lines while excluding comments and heredoc data."""
    lines: list[str] = []
    heredoc: str | None = None
    expands = False
    for line in text.splitlines():
        if heredoc is not None:
            if line.strip() == heredoc:
                heredoc = None
            elif expands:
                lines.extend(re.findall(r"\$\(([^()]*)\)", line))
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        delimiter = re.search(r"<<-?\s*(['\"]?)(\w+)\1", line)
        if delimiter:
            heredoc = delimiter[2]
            expands = not delimiter[1]
        lines.append(line)
    return lines


def test_shell_code_preserves_new_write_outside_help() -> None:
    """A write appended to an installer or loader is still inspected."""
    text = "# old\ncat <<'END'\nhistory\nEND\ntouch .bh/new.json\n"
    assert _shell_code(text) == ["cat <<'END'", "touch .bh/new.json"]
