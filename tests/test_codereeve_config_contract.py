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
import yaml

from codereeve.config_env import PRODUCT_ALIASES
from codereeve.paths import PathLayout

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.fast

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
    (
        "src/codereeve/migration/transaction.py",
        "_converted",
        "/etc/bh-daemon/secrets.env",
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
    assert not missing, sorted(missing)
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
        for command, target in _execution_targets(_execution_text(path))
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
            if function in {
                "get",
                "getenv",
                "pop",
                "setdefault",
                "putenv",
                "unsetenv",
            }:
                candidates = node.args[:1]
            elif function in {"resolve_alias_pair", "_compat_value"}:
                candidates = node.args[1:3]
            elif function == "update":
                for argument in node.args:
                    if isinstance(argument, ast.Dict):
                        candidates.extend(key for key in argument.keys if key)
                names.update(item.arg for item in node.keywords if item.arg)
        elif isinstance(node, ast.AugAssign) and isinstance(
            node.value, ast.Dict
        ):
            candidates = [key for key in node.value.keys if key]
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
        for export in re.finditer(
            r"(?:^|[;\s])(?:export|declare\s+-[a-zA-Z]*x[a-zA-Z]*)\s+([^;]+)",
            visible,
        ):
            names.update(
                re.findall(r"(?:^|\s)([A-Z][A-Z_0-9]*)(?=\s|=|$)", export[1])
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
                    "&",
                    "|",
                    "|&",
                    "(",
                    ")",
                    "{",
                    "then",
                    "do",
                    "!",
                    "if",
                    "elif",
                    "while",
                    "until",
                    "else",
                    "time",
                    "coproc",
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


@pytest.mark.parametrize(
    "statement",
    [
        'os.environ.update({"BH_UNREGISTERED": "value"})',
        'os.environ.update(BH_UNREGISTERED="value")',
        'os.putenv("BH_UNREGISTERED", "value")',
        'os.environ |= {"BH_UNREGISTERED": "value"}',
    ],
)
def test_python_scan_tracks_environment_mutations(statement: str) -> None:
    """Mutation APIs cannot export names outside the product catalog."""
    assert _python_interfaces(statement) == {"BH_UNREGISTERED"}


@pytest.mark.parametrize(
    "statement",
    [
        "BH_UNREGISTERED=value\nexport BH_UNREGISTERED",
        "export BH_UNREGISTERED BH_SECOND=value",
        "declare -x BH_UNREGISTERED=value",
        "declare -rx BH_UNREGISTERED",
    ],
)
def test_shell_scan_tracks_split_and_declared_exports(statement: str) -> None:
    """An assignment followed by a bare export remains an interface."""
    assert "BH_UNREGISTERED" in _shell_interfaces(statement)


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
        ("true & source host.env", ("source", "host.env")),
        ("printf x | source config.env", ("source", "config.env")),
        ("printf x |& source config.env", ("source", "config.env")),
        ("load() { source host.env; }", ("source", "host.env")),
        ("while source config.env; do true; done", ("source", "config.env")),
        ("until . config.env; do true; done", (".", "config.env")),
        (
            "if false; then true; elif source host.env; then true; fi",
            ("source", "host.env"),
        ),
        ("case x in x) eval contents;; esac", ("eval", "contents")),
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
        for line in _shell_legacy_lines(path.read_text(encoding="utf-8"))
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


def _open_is_read_only(
    call: ast.Call, literals: dict[str, ast.Constant], *, path_method: bool
) -> bool:
    """Permit proven read modes; unknown modes may mutate legacy paths."""
    low_level = ast.unparse(call.func) == "os.open"
    if any(item.arg is None for item in call.keywords) or any(
        isinstance(item, ast.Starred) for item in call.args
    ):
        return False
    mode = next(
        (
            item.value
            for item in call.keywords
            if item.arg == ("flags" if low_level else "mode")
        ),
        None,
    )
    index = 0 if path_method else 1
    if mode is None and len(call.args) > index:
        mode = call.args[index]
    if mode is None:
        return not low_level  # Builtin and Path.open default to read mode.
    if isinstance(mode, ast.Name):
        mode = literals.get(mode.id)
    if not low_level:
        return isinstance(mode, ast.Constant) and mode.value in {
            "r",
            "rt",
            "rb",
            "tr",
            "br",
        }
    # O_RDONLY is zero; these extra flags do not request filesystem writes.
    pending = [mode]
    while pending:
        flag = pending.pop()
        if isinstance(flag, ast.BinOp) and isinstance(flag.op, ast.BitOr):
            pending.extend((flag.left, flag.right))
        elif isinstance(flag, ast.Constant) and flag.value == 0:
            continue
        elif isinstance(flag, ast.Attribute) and ast.unparse(flag) in {
            "os.O_RDONLY",
            "os.O_CLOEXEC",
            "os.O_NOFOLLOW",
            "os.O_DIRECTORY",
            "os.O_NONBLOCK",
            "os.O_BINARY",
            "os.O_TEXT",
        }:
            continue
        else:
            return False
    return True


def _derived_legacy_writes(
    text: str, *, attribute_prefix: str = "legacy_"
) -> set[tuple[str, str]]:
    """Find common mutations of paths derived from legacy layout fields."""
    found: set[tuple[str, str]] = set()
    tree = ast.parse(text)
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        aliases: set[str] = set()
        literals: dict[str, ast.Constant] = {}

        def legacy(expression: ast.AST, known: set[str] = aliases) -> bool:
            """Recognize direct layout attributes and simple assigned paths."""
            return any(
                (
                    isinstance(node, ast.Attribute)
                    and node.attr.startswith(attribute_prefix)
                )
                or (isinstance(node, ast.Name) and node.id in known)
                for node in ast.walk(expression)
            )

        # Walk source order, including statements inside conditional blocks.
        for node in sorted(
            ast.walk(function), key=lambda item: getattr(item, "lineno", 0)
        ):
            if (
                isinstance(node, (ast.Assign, ast.AnnAssign))
                and node.value is not None
            ):
                targets = (
                    node.targets
                    if isinstance(node, ast.Assign)
                    else [node.target]
                )
                for target in targets:
                    if isinstance(target, ast.Name):
                        value = node.value
                        if isinstance(value, ast.Name):
                            value = literals.get(value.id)
                        if isinstance(value, ast.Constant):
                            literals[target.id] = value
                        else:
                            literals.pop(target.id, None)
                        if legacy(node.value):
                            aliases.add(target.id)
                        else:
                            aliases.discard(target.id)
            if isinstance(node, ast.AugAssign) and isinstance(
                node.target, ast.Name
            ):
                literals.pop(node.target.id, None)
            if not isinstance(node, ast.Call):
                continue
            name = ast.unparse(node.func).split(".")[-1]
            if name in {
                "write_text",
                "write_bytes",
                "mkdir",
                "makedirs",
                "touch",
                "unlink",
                "rmdir",
                "rename",
                "replace",
                "remove",
                "rmtree",
                "move",
                "copy",
                "copy2",
                "copyfile",
            }:
                if legacy(node):
                    found.add((function.name, name))
            elif name == "open" and legacy(node):
                if not _open_is_read_only(
                    node,
                    literals,
                    path_method=isinstance(node.func, ast.Attribute)
                    and legacy(node.func.value),
                ):
                    found.add((function.name, name))
    return found


@pytest.mark.parametrize(
    "statement",
    [
        '(layout.legacy_state / "new.json").write_text("data")',
        'path = layout.legacy_state / "new.json"\npath.write_bytes(b"data")',
        'path = layout.legacy_host\nopen(path, "w")',
        'path = layout.legacy_host\npath.open("w")',
        "layout.legacy_state.mkdir()",
        "os.makedirs(layout.legacy_state)",
        "os.replace(layout.legacy_state, layout.canonical_state)",
    ],
)
def test_legacy_layout_mutation_is_not_a_selector(statement: str) -> None:
    """Using a shared legacy path property must not hide a new mutation."""
    text = "def new_writer():\n" + "\n".join(
        "    " + line for line in statement.splitlines()
    )
    assert _derived_legacy_writes(text)
    assert not _derived_legacy_writes(
        "def read_only():\n    return layout.legacy_state.exists()\n"
    )


@pytest.mark.parametrize(
    "statement, writes",
    [
        ('mode = "w"\nopen(layout.legacy_host, mode)', True),
        ('mode = "r"\nmode = input()\nopen(layout.legacy_host, mode)', True),
        ('mode = "r"\nmode += "+"\nopen(layout.legacy_host, mode)', True),
        ("open(layout.legacy_host, get_mode())", True),
        ("layout.legacy_host.open(mode=unknown)", True),
        ("open(layout.legacy_host, **options)", True),
        ("os.open(layout.legacy_host, os.O_WRONLY)", True),
        ("os.open(layout.legacy_host, os.O_RDWR)", True),
        ("os.open(layout.legacy_host, os.O_RDONLY | os.O_CREAT)", True),
        ("os.open(layout.legacy_host, flags=os.O_RDONLY | os.O_TRUNC)", True),
        ("os.open(layout.legacy_host, os.O_RDONLY | os.O_APPEND)", True),
        ("os.open(layout.legacy_host, unknown_flags)", True),
        ("open(layout.legacy_host)", False),
        ("layout.legacy_host.open()", False),
        ('open(layout.legacy_host, "rb")', False),
        ('mode = "r"\ncopy = mode\nopen(layout.legacy_host, copy)', False),
        ('mode = "rb"\nlayout.legacy_host.open(mode=mode)', False),
        ("os.open(layout.legacy_host, os.O_RDONLY)", False),
        ("os.open(layout.legacy_host, os.O_RDONLY | os.O_CLOEXEC)", False),
        ("os.open(layout.legacy_host, 0)", False),
    ],
)
def test_legacy_open_modes_require_proven_read_only(
    statement: str, writes: bool
) -> None:
    """Literal aliases and low-level flags cannot hide legacy file writes."""
    text = "def access():\n" + "\n".join(
        "    " + line for line in statement.splitlines()
    )
    assert bool(_derived_legacy_writes(text)) is writes


def _shell_legacy_lines(
    text: str,
    *,
    pattern: str = (
        r"(?:\.bh|\.baton-harness)/|/etc/bh-daemon/"
        r"|baton-harness/host.env"
    ),
) -> set[str]:
    """Identify active legacy paths, preserving echo redirection sinks."""
    found = set()
    for line in _shell_code(text):
        line = line.strip()
        if line.startswith("echo "):
            lexer = shlex.shlex(line, posix=False, punctuation_chars=">;&")
            lexer.whitespace_split = True
            try:
                tokens = list(lexer)
            except ValueError:
                continue
            destinations = [
                tokens[index + 1]
                for index, token in enumerate(tokens[:-1])
                if token in {">", ">>"}
            ]
            if not any(re.search(pattern, target) for target in destinations):
                continue
        if re.search(pattern, line):
            lexer = shlex.shlex(line, posix=False)
            lexer.whitespace_split = True
            found.add(" ".join(lexer))
    return found


def test_derived_legacy_mutations_have_exact_owners() -> None:
    """Shared layout properties cannot introduce an unreviewed writer."""
    actual = {
        (path.relative_to(ROOT).as_posix(), owner, operation)
        for path in _production_files()
        if path.suffix == ".py"
        for owner, operation in _derived_legacy_writes(
            path.read_text(encoding="utf-8")
        )
    }
    assert not actual, sorted(actual)


def test_echo_redirection_is_a_legacy_write() -> None:
    """Echoing guidance and redirecting bytes to state are distinct."""
    write = 'echo data > "${BH_PROJECT_ROOT}/.bh/new.json"'
    assert _shell_legacy_lines(write) == {write}
    assert not _shell_legacy_lines(
        'echo "use .bh/config.env for compatibility"'
    )
    assert not _shell_legacy_lines('echo "use .bh/config.env" >&2')


def _execution_text(path: Path) -> str:
    """Select actual workflow commands, excluding YAML descriptive prose."""
    if path.suffix not in {".yml", ".yaml"}:
        return _interface_text(path)
    commands: list[str] = []
    pending = [
        yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    ]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if isinstance(node.get("run"), str):
                commands.append(node["run"])
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return "\n".join(commands)


def test_workflow_execution_excludes_descriptive_prose(tmp_path: Path) -> None:
    """A description's punctuation cannot become a Bash command boundary."""
    path = tmp_path / "action.yml"
    path.write_text(
        "description: (test) . Each\nsteps:\n  - run: source config.env\n",
        encoding="utf-8",
    )
    assert _execution_targets(_execution_text(path)) == {
        ("source", "config.env")
    }


def _assert_symphony_state_writer(text: str) -> None:
    """Pin the active daemon state expression and Orchestrator argument."""
    (function,) = [
        node
        for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_run_work_unit"
    ]
    expressions = [
        node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "state_path"
            for target in node.targets
        )
    ]
    assert len(expressions) == 1
    expected = ast.parse(
        'str(repo_root / ".symphony" / "state.json")', mode="eval"
    ).body
    assert ast.dump(expressions[0]) == ast.dump(expected)
    (constructor,) = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Orchestrator"
    ]
    (state_argument,) = [
        item.value for item in constructor.keywords if item.arg == "state_path"
    ]
    assert (
        isinstance(state_argument, ast.Name)
        and state_argument.id == "state_path"
    )


def test_symphony_contract_checks_actual_writer() -> None:
    """Changing or diverting the real daemon destination fails the guard."""
    source = (ROOT / "src/codereeve/chain/daemon/work_unit.py").read_text(
        encoding="utf-8"
    )
    _assert_symphony_state_writer(source)
    for mutation in (
        source.replace(
            'repo_root / ".symphony" / "state.json"',
            'repo_root / ".codereeve" / "state.json"',
        ),
        source.replace("state_path=state_path,", 'state_path="other.json",'),
    ):
        assert mutation != source
        with pytest.raises(AssertionError):
            _assert_symphony_state_writer(mutation)


def _symphony_literals(text: str) -> set[tuple[str, str]]:
    """Inventory active Symphony path literals by exact function owner."""
    tree = ast.parse(text)
    parents = {
        child: node
        for node in ast.walk(tree)
        for child in ast.iter_child_nodes(node)
    }
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(
            node.value, str
        ):
            continue
        if not re.match(r"\.symphony(?:/|$)", node.value) or any(
            c.isspace() for c in node.value
        ):
            continue
        owner: ast.AST = node
        while owner in parents and not isinstance(
            owner, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            owner = parents[owner]
        found.add((getattr(owner, "name", "<module>"), node.value))
    return found


def test_symphony_path_inventory_rejects_new_moves() -> None:
    """New Symphony destinations and renames cannot hide in another module."""
    assert _symphony_literals(
        'def move():\n    (root / ".symphony").rename(root / ".codereeve")\n'
    ) == {("move", ".symphony")}


def test_symphony_paths_have_exact_active_owners() -> None:
    """Keep all production Symphony literals under their reviewed owners."""
    expected = {
        ("src/codereeve/paths.py", "for_environment", ".symphony"),
        (
            "src/codereeve/chain/doctor.py",
            "_check_gitignore_symphony",
            ".symphony/",
        ),
        (
            "src/codereeve/vendor/symphony/workspace.py",
            "__init__",
            ".symphony",
        ),
        (
            "src/codereeve/chain/daemon/work_unit.py",
            "_run_work_unit",
            ".symphony",
        ),
    }
    actual = {
        (path.relative_to(ROOT).as_posix(), owner, literal)
        for path in _production_files()
        if path.suffix == ".py"
        for owner, literal in _symphony_literals(
            path.read_text(encoding="utf-8")
        )
    }
    assert actual == expected, sorted(actual ^ expected)


def test_symphony_property_mutation_is_rejected() -> None:
    """Shared layout paths cannot be used to move Symphony into CodeReeve."""
    assert _derived_legacy_writes(
        "def move():\n    layout.symphony_state.rename(other)\n",
        attribute_prefix="symphony_",
    ) == {("move", "rename")}


def test_symphony_operations_have_no_new_mutation_sinks() -> None:
    """New move/rename/write calls on Symphony layout properties fail."""
    actual = {
        (path.relative_to(ROOT).as_posix(), owner, operation)
        for path in _production_files()
        if path.suffix == ".py"
        for owner, operation in _derived_legacy_writes(
            path.read_text(encoding="utf-8"), attribute_prefix="symphony_"
        )
    }
    assert not actual, sorted(actual)


def test_symphony_shell_operations_are_the_existing_gitignore_contract() -> (
    None
):
    """Shell/config/template surfaces may only inspect or seed gitignore."""
    expected = {
        (
            "bin/init-sandbox.sh",
            "printf '.symphony/\\n' > \"${GITIGNORE_FILE}\"",
        ),
        (
            "bin/init-sandbox.sh",
            "elif grep -qxF '.symphony/' \"${GITIGNORE_FILE}\" ; then",
        ),
        (
            "bin/init-sandbox.sh",
            "printf '%s\\n' '.symphony/' >> \"${GITIGNORE_FILE}\"",
        ),
        (
            "bin/init-sandbox.sh",
            'git -C "${CODEREEVE_PROJECT_ROOT}" commit -m "chore: '
            'gitignore .symphony/ daemon state" -- .gitignore',
        ),
        (
            "bin/run-daemon.sh",
            'if [[ ! -f "${CODEREEVE_PROJECT_ROOT}/.gitignore" ]] '
            "|| ! grep -qxF '.symphony/' "
            '"${CODEREEVE_PROJECT_ROOT}/.gitignore" ; then',
        ),
    }
    actual = {
        (path.relative_to(ROOT).as_posix(), line)
        for path in _production_files()
        if path.suffix != ".py"
        for line in _shell_legacy_lines(
            _execution_text(path), pattern=r"\.symphony(?=/|$|[\s\"'])"
        )
    }
    assert actual == expected, sorted(actual ^ expected)
