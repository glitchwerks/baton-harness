"""Tests for literal CodeReeve environment configuration handling."""

from pathlib import Path

import pytest

from codereeve.config_env import (
    AliasConflictError,
    ConfigSyntaxError,
    EnvLayer,
    parse_env_file,
    parse_env_text,
    resolve_environment,
    rewrite_assignments,
)


def test_parse_env_text_accepts_literals_quotes_export_and_comments() -> None:
    """The accepted grammar preserves literal values and line locations."""
    parsed = parse_env_text(
        "export CODEREEVE_REPO_OWNER='glitchwerks' # owner\n"
        'CODEREEVE_REPO_NAME="sandbox"\n'
        "BWS_PEM_SECRET_ID=11111111-2222-3333-4444-555555555555\n",
        source="config.env",
    )
    assert [(item.key, item.value, item.line) for item in parsed] == [
        ("CODEREEVE_REPO_OWNER", "glitchwerks", 1),
        ("CODEREEVE_REPO_NAME", "sandbox", 2),
        ("BWS_PEM_SECRET_ID", "11111111-2222-3333-4444-555555555555", 3),
    ]


def test_parse_env_text_handles_whitespace_empty_values_and_crlf() -> None:
    """Blank lines, comments, whitespace, and empty literals are accepted."""
    parsed = parse_env_text(
        "\r\n"
        "  # comment\r\n"
        "  EMPTY=   \r\n"
        "EMPTY_WITH_COMMENT= # empty\r\n"
        "  QUOTED_EMPTY=''\r\n"
        'DOUBLE_EMPTY=""\r\n'
        "HASH='value # literal'\r\n"
        "exported=value\r\n"
        "FINAL=value",
        source="config.env",
    )
    assert [(item.key, item.value) for item in parsed] == [
        ("EMPTY", ""),
        ("EMPTY_WITH_COMMENT", ""),
        ("QUOTED_EMPTY", ""),
        ("DOUBLE_EMPTY", ""),
        ("HASH", "value # literal"),
        ("exported", "value"),
        ("FINAL", "value"),
    ]


def test_parse_env_text_unescapes_only_matching_quote_mode() -> None:
    """Matching quote and backslash escapes remain literal otherwise."""
    parsed = parse_env_text(
        "SINGLE='it\\'s\\\\literal'\n"
        'DOUBLE="a\\"quote\\\\slash"\n',
        source="config.env",
    )
    assert [(item.key, item.value) for item in parsed] == [
        ("SINGLE", "it's\\literal"),
        ("DOUBLE", 'a"quote\\slash'),
    ]


def test_parse_env_text_preserves_duplicate_assignments_for_last_precedence(
) -> None:
    """Repeated spellings retain their physical order for later precedence."""
    parsed = parse_env_text("VALUE=first\nVALUE=last\n", source="config.env")
    assert [(item.value, item.line) for item in parsed] == [
        ("first", 1),
        ("last", 2),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "VALUE=$(touch should-not-exist)\n",
        "VALUE=`touch should-not-exist`\n",
        "VALUE=${name}\n",
        "VALUE=$name\n",
        "VALUE=one; two\n",
        "VALUE=one && two\n",
        "VALUE=one || two\n",
        "VALUE=one > output\n",
        "VALUE=one \\\ntwo\n",
        "VALUE='unterminated\n",
        'VALUE="unterminated\n',
        "FIRST=one SECOND=two\n",
        "1INVALID=value\n",
        "export\n",
        "VALUE=bad\x00value\n",
    ],
)
def test_parse_env_text_rejects_syntax_without_exposing_value(
    text: str,
) -> None:
    """Malformed syntax raises a redacted source-location error."""
    with pytest.raises(ConfigSyntaxError) as caught:
        parse_env_text(text, source="private.env")

    message = str(caught.value)
    assert "private.env:1" in message
    assert text.strip() not in message
    assert "should-not-exist" not in message


def test_parse_env_text_never_evaluates_command_substitution(
    tmp_path: Path,
) -> None:
    """Parsing hostile input cannot create a command-substitution sentinel."""
    sentinel = tmp_path / "parser-execution-sentinel"
    with pytest.raises(ConfigSyntaxError):
        parse_env_text(
            f"VALUE=$(touch {sentinel.as_posix()})\n",
            source="private.env",
        )
    assert not sentinel.exists()


def test_parse_env_file_only_reads_the_explicit_path(tmp_path: Path) -> None:
    """File parsing returns literal assignments from the requested file."""
    path = tmp_path / "config.env"
    path.write_text("CODEREEVE_REPO_NAME=sandbox\n", encoding="utf-8")
    assert parse_env_file(path)[0].value == "sandbox"


@pytest.mark.parametrize(
    ("operator", "managed", "expected"),
    [
        ({"CODEREEVE_PROJECT_ROOT": "/new"}, {}, "/new"),
        ({"BH_PROJECT_ROOT": "/old"}, {}, "/old"),
        (
            {"CODEREEVE_PROJECT_ROOT": "/same", "BH_PROJECT_ROOT": "/same"},
            {},
            "/same",
        ),
        ({}, {"BH_PROJECT_ROOT": "/file"}, "/file"),
    ],
)
def test_resolve_environment_pair_matrix(
    operator: dict[str, str], managed: dict[str, str], expected: str
) -> None:
    """Canonical and legacy project roots follow layer priority safely."""
    result = resolve_environment(
        [EnvLayer("operator", operator), EnvLayer("managed", managed)]
    )
    assert result.values["CODEREEVE_PROJECT_ROOT"] == expected


@pytest.mark.parametrize(
    "layers",
    [
        [
            EnvLayer(
                "operator",
                {
                    "CODEREEVE_PROJECT_ROOT": "/new",
                    "BH_PROJECT_ROOT": "/old",
                },
            )
        ],
        [
            EnvLayer("operator", {"CODEREEVE_PROJECT_ROOT": "/new"}),
            EnvLayer("managed", {"BH_PROJECT_ROOT": "/old"}),
        ],
    ],
)
def test_resolve_environment_rejects_mismatched_aliases_without_values(
    layers: list[EnvLayer],
) -> None:
    """Any canonical/legacy disagreement is fail-closed and redacted."""
    with pytest.raises(AliasConflictError) as caught:
        resolve_environment(layers)
    message = str(caught.value)
    assert "CODEREEVE_PROJECT_ROOT" in message
    assert "BH_PROJECT_ROOT" in message
    assert "/new" not in message
    assert "/old" not in message


def test_resolve_environment_distinguishes_unset_from_empty() -> None:
    """Empty values are selected values rather than missing spellings."""
    canonical_empty = resolve_environment(
        [EnvLayer("operator", {"CODEREEVE_PROJECT_ROOT": ""})]
    )
    legacy_empty = resolve_environment(
        [EnvLayer("operator", {"BH_PROJECT_ROOT": ""})]
    )
    unset = resolve_environment([EnvLayer("operator", {})])
    assert canonical_empty.values["CODEREEVE_PROJECT_ROOT"] == ""
    assert legacy_empty.values["CODEREEVE_PROJECT_ROOT"] == ""
    assert "CODEREEVE_PROJECT_ROOT" not in unset.values


def test_resolve_environment_preserves_precedence_and_third_party_keys(
) -> None:
    """Higher layers override same spellings and pass through other keys."""
    result = resolve_environment(
        [
            EnvLayer(
                "operator",
                {
                    "CODEREEVE_REPO_NAME": "operator-repo",
                    "BWS_PEM_SECRET_ID": "operator-secret-id",
                },
            ),
            EnvLayer(
                "managed",
                {
                    "CODEREEVE_REPO_NAME": "managed-repo",
                    "GH_TOKEN": "managed-token",
                },
            ),
        ]
    )
    assert result.values["CODEREEVE_REPO_NAME"] == "operator-repo"
    assert result.values["BWS_PEM_SECRET_ID"] == "operator-secret-id"
    assert result.values["GH_TOKEN"] == "managed-token"


def test_resolve_environment_records_one_legacy_use_and_exports_alias(
) -> None:
    """A selected legacy spelling records once and supports export."""
    result = resolve_environment(
        [EnvLayer("managed", {"BH_PROJECT_ROOT": "/old"})]
    )
    assert result.values["CODEREEVE_PROJECT_ROOT"] == "/old"
    assert result.values["BH_PROJECT_ROOT"] == "/old"
    assert len(result.legacy_uses) == 1
    assert result.legacy_uses[0].legacy == "BH_PROJECT_ROOT"


def test_resolve_environment_can_omit_materialized_legacy_aliases() -> None:
    """Callers can receive canonical-only compatibility results."""
    result = resolve_environment(
        [EnvLayer("managed", {"BH_PROJECT_ROOT": "/old"})],
        export_legacy=False,
    )
    assert result.values == {"CODEREEVE_PROJECT_ROOT": "/old"}


def test_rewrite_assignments_canonicalizes_product_keys_and_preserves_others(
) -> None:
    """Rewriting retains third-party assignments, comments, and ordering."""
    assignments = parse_env_text(
        "BWS_PEM_SECRET_ID=uuid # keep\n"
        "BH_REPO_NAME=sandbox # legacy\n"
        "UNRELATED=value\n",
        source="config.env",
    )
    assert rewrite_assignments(assignments, path_values={}) == (
        "BWS_PEM_SECRET_ID=uuid # keep\n"
        "CODEREEVE_REPO_NAME=sandbox # legacy\n"
        "UNRELATED=value\n"
    )


def test_rewrite_assignments_rewrites_equal_pair_and_default_path(
) -> None:
    """Equal aliases collapse while custom path values remain exact."""
    assignments = parse_env_text(
        "BH_PROJECT_ROOT=/legacy/root\n"
        "CODEREEVE_PROJECT_ROOT=/legacy/root\n"
        "BH_RUNLOG_PATH=/custom/runlog.jsonl\n",
        source="config.env",
    )
    rewritten = rewrite_assignments(
        assignments,
        path_values={
            "CODEREEVE_PROJECT_ROOT": ("/legacy/root", "/codereeve/root"),
            "CODEREEVE_RUNLOG_PATH": (
                "/legacy/runlog.jsonl",
                "/codereeve/runlog.jsonl",
            ),
        },
    )
    assert rewritten == (
        "CODEREEVE_PROJECT_ROOT=/codereeve/root\n"
        "CODEREEVE_RUNLOG_PATH=/custom/runlog.jsonl\n"
    )


def test_rewrite_assignments_rejects_conflicting_alias_values_without_values(
) -> None:
    """Config migration does not rewrite an ambiguous alias pair."""
    assignments = parse_env_text(
        "BH_REPO_NAME=old\nCODEREEVE_REPO_NAME=new\n",
        source="config.env",
    )
    with pytest.raises(AliasConflictError) as caught:
        rewrite_assignments(assignments, path_values={})
    message = str(caught.value)
    assert "BH_REPO_NAME" in message
    assert "CODEREEVE_REPO_NAME" in message
    assert "old" not in message
    assert "new" not in message
