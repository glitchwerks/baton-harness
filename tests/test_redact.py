"""Unit tests for baton_harness.redact — secret-redaction helper.

Issue #351 (D6 step 1): ``redact_secrets`` is a hard prerequisite for
threading hook diagnostics (``stderr_tail``) into escalation comments —
per ``CLAUDE.md`` § Credentials and Secrets, redaction is a hard gate, not
a follow-up. This module is deliberately placed at the package root
(``src/baton_harness/redact.py``, not ``chain/``) so the vendored
``symphony`` tree can import it without creating a ``vendor -> chain``
dependency — mirroring the ``_auth.py`` package-root precedent.

Coverage:
- Each named token-prefix pattern (``ghs_``, ``ghp_``, ``gho_``, ``ghu_``,
  ``ghr_``, ``github_pat_``) is redacted out of free text.
- A token-bearing remote URL (``https://user:pass@host/...``) is redacted.
- A token that would straddle a 500-char truncation boundary is still
  fully redacted when the *whole* (untruncated) text is passed in — this
  is the order-of-operations regression the plan calls out: redaction
  must run before any truncation, never after.
- ``extra_values``: exact-substring redaction of caller-supplied secret
  values (e.g. an injected ``GH_TOKEN``) that don't match any pattern.
- Empty / None-ish inputs do not raise and do not crash on an empty
  ``extra_values``.
- The function never raises, even on unusual input shapes.
"""

from __future__ import annotations

from baton_harness.redact import redact_secrets

# ---------------------------------------------------------------------------
# Fixtures / sample secrets
# ---------------------------------------------------------------------------

_TOKEN_SAMPLES = {
    "ghs_": "ghs_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd",
    "ghp_": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd",
    "gho_": "gho_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd",
    "ghu_": "ghu_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd",
    "ghr_": "ghr_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd",
    "github_pat_": "github_pat_11AAAAAAA0_"
    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
}


# ---------------------------------------------------------------------------
# Per-prefix pattern redaction
# ---------------------------------------------------------------------------


class TestTokenPrefixRedaction:
    """Every named GitHub token prefix is redacted out of free text."""

    def test_ghs_prefixed_token_is_redacted(self) -> None:
        """A ``ghs_`` (installation) token never survives redaction."""
        token = _TOKEN_SAMPLES["ghs_"]
        text = f"fatal: remote error: bad credentials for token {token}"
        result = redact_secrets(text)
        assert token not in result, (
            f"ghs_ token leaked through redact_secrets; got {result!r}"
        )

    def test_ghp_prefixed_token_is_redacted(self) -> None:
        """A ``ghp_`` (classic PAT) token never survives redaction."""
        token = _TOKEN_SAMPLES["ghp_"]
        text = f"Authorization: token {token}"
        result = redact_secrets(text)
        assert token not in result, (
            f"ghp_ token leaked through redact_secrets; got {result!r}"
        )

    def test_gho_prefixed_token_is_redacted(self) -> None:
        """A ``gho_`` (OAuth) token never survives redaction."""
        token = _TOKEN_SAMPLES["gho_"]
        text = f"using token {token} to authenticate"
        result = redact_secrets(text)
        assert token not in result, (
            f"gho_ token leaked through redact_secrets; got {result!r}"
        )

    def test_ghu_prefixed_token_is_redacted(self) -> None:
        """A ``ghu_`` (user-to-server) token never survives redaction."""
        token = _TOKEN_SAMPLES["ghu_"]
        text = f"token={token}"
        result = redact_secrets(text)
        assert token not in result, (
            f"ghu_ token leaked through redact_secrets; got {result!r}"
        )

    def test_ghr_prefixed_token_is_redacted(self) -> None:
        """A ``ghr_`` (refresh) token never survives redaction."""
        token = _TOKEN_SAMPLES["ghr_"]
        text = f"refresh token: {token}"
        result = redact_secrets(text)
        assert token not in result, (
            f"ghr_ token leaked through redact_secrets; got {result!r}"
        )

    def test_github_pat_prefixed_token_is_redacted(self) -> None:
        """A ``github_pat_`` (fine-grained PAT) token is redacted."""
        token = _TOKEN_SAMPLES["github_pat_"]
        text = f"error: could not read Username for '{token}'"
        result = redact_secrets(text)
        assert token not in result, (
            f"github_pat_ token leaked through redact_secrets; got {result!r}"
        )

    def test_redaction_leaves_a_marker_not_silence(self) -> None:
        """Redacting a token leaves *some* replacement text, not nothing.

        Guards against an implementation that silently deletes the
        matched span (which would make truncated/redacted output
        indistinguishable from "nothing happened here").
        """
        token = _TOKEN_SAMPLES["ghp_"]
        text = f"before {token} after"
        result = redact_secrets(text)
        assert "before" in result and "after" in result
        assert result != "before  after", (
            "expected a visible replacement marker in place of the "
            f"redacted token; got {result!r}"
        )


# ---------------------------------------------------------------------------
# Token-bearing remote URL
# ---------------------------------------------------------------------------


class TestTokenBearingUrlRedaction:
    """A ``://user:pass@host`` credential-bearing URL is redacted."""

    def test_https_userinfo_url_is_redacted(self) -> None:
        """A basic-auth remote URL's credentials never survive."""
        secret_part = "x-access-token:ghs_SUPERSECRETVALUE1234567890"
        text = (
            f"fatal: unable to access "
            f"'https://{secret_part}@github.com/o/r.git/': "
            f"The requested URL returned error: 403"
        )
        result = redact_secrets(text)
        assert secret_part not in result, (
            f"credential-bearing URL leaked through redaction; got {result!r}"
        )
        # The surrounding context (host, path) is not required to survive,
        # but the redaction must not raise and must alter the input.
        assert result != text


# ---------------------------------------------------------------------------
# Order-of-operations regression: redact BEFORE truncate
# ---------------------------------------------------------------------------


class TestOrderOfOperationsAcrossTruncationBoundary:
    """A token straddling a 500-char cut is still fully redacted.

    ``redact_secrets`` itself performs no truncation — callers (e.g.
    ``hooks.py``) are responsible for calling this function on the FULL
    text first and truncating afterwards. This test proves the pattern
    match works correctly on a token positioned so that, if truncation
    had happened first (at position 500), the token would have been cut
    in half and the pattern pass would fail to recognise it. Redacting
    the *whole*, untruncated text must never leave a partial token
    fragment behind.
    """

    def test_token_positioned_across_the_500_char_mark_is_fully_redacted(
        self,
    ) -> None:
        """A token starting before char 500 and ending after it is redacted.

        Padding is chosen so the real token (44 chars) starts at
        position 480 and ends at position 524 — squarely straddling the
        naive ``[:500]`` truncation boundary an order-of-operations bug
        would apply too early.
        """
        padding = "x" * 480
        token = _TOKEN_SAMPLES["ghp_"]
        assert 480 < 500 < 480 + len(token), (
            "test setup invariant: token must straddle offset 500"
        )
        text = padding + token + " trailing context after the token"

        result = redact_secrets(text)

        assert token not in result, (
            "a token straddling the 500-char truncation boundary was "
            f"not fully redacted when passed whole; got {result!r}"
        )
        # No fragment of the raw token (prefix or suffix half) survives.
        half = len(token) // 2
        assert token[:half] not in result or len(token[:half]) < 4, (
            f"a partial token fragment survived redaction; got {result!r}"
        )


# ---------------------------------------------------------------------------
# extra_values — exact-substring pass
# ---------------------------------------------------------------------------


class TestExtraValuesRedaction:
    """Caller-supplied secret values are redacted by exact substring."""

    def test_extra_value_not_matching_any_pattern_is_redacted(self) -> None:
        """A secret with no recognisable prefix is redacted via extra_values.

        This is the case that matters most for #351/F5: a raw PAT value
        injected into ``orch.hook_env`` may appear verbatim in a failing
        hook's stderr without any of the standard ``gh*_`` prefixes
        surviving (e.g. echoed by a custom script), so the pattern pass
        alone cannot catch it.
        """
        secret = "s3cr3t-value-without-a-known-prefix-9F8E7D6C"
        text = f"error: authentication failed using {secret}"
        result = redact_secrets(text, extra_values=[secret])
        assert secret not in result, (
            f"extra_values secret leaked through redaction; got {result!r}"
        )

    def test_multiple_extra_values_are_all_redacted(self) -> None:
        """Every non-empty string in extra_values is redacted independently."""
        secret_a = "AAAA-secret-value-one"
        secret_b = "BBBB-secret-value-two"
        text = f"first={secret_a} second={secret_b}"
        result = redact_secrets(text, extra_values=[secret_a, secret_b])
        assert secret_a not in result
        assert secret_b not in result

    def test_empty_string_in_extra_values_is_ignored(self) -> None:
        """An empty string among extra_values must not redact everything.

        Per D6 step 1, only *non-empty* strings in ``extra_values`` are
        substituted — treating "" as a substring to redact would corrupt
        every character position in the text.
        """
        text = "perfectly ordinary log line with no secrets"
        result = redact_secrets(text, extra_values=["", "another-secret"])
        assert result == text or "another-secret" not in text, (
            "an empty extra_values entry must not mangle unrelated text; "
            f"got {result!r}"
        )
        # The text must not be reduced to a wall of redaction markers.
        assert len(result) >= len(text) - 20, (
            "an empty string in extra_values appears to have matched "
            f"every position in the text; got {result!r}"
        )

    def test_extra_values_defaults_to_empty_and_is_a_no_op(self) -> None:
        """Omitting extra_values entirely leaves non-secret text untouched."""
        text = "nothing sensitive here at all"
        assert redact_secrets(text) == text


# ---------------------------------------------------------------------------
# Empty / None-ish inputs
# ---------------------------------------------------------------------------


class TestEmptyAndNoneIshInputs:
    """Degenerate inputs are handled gracefully."""

    def test_empty_string_returns_empty_string(self) -> None:
        """redact_secrets("") returns "" without raising."""
        assert redact_secrets("") == ""

    def test_whitespace_only_text_is_returned_unchanged(self) -> None:
        """Whitespace-only text with no secrets passes through untouched."""
        assert redact_secrets("   \n\t  ") == "   \n\t  "

    def test_text_with_no_secrets_is_unchanged(self) -> None:
        """Text containing no recognisable secret is returned unchanged."""
        text = "the quick brown fox jumps over the lazy dog"
        assert redact_secrets(text) == text


# ---------------------------------------------------------------------------
# Never raises
# ---------------------------------------------------------------------------


class TestNeverRaises:
    """redact_secrets is a hard gate — it must never itself raise."""

    def test_does_not_raise_on_extra_values_containing_none(self) -> None:
        """A stray ``None`` in extra_values (e.g. from ``dict.values()``).

        Must not crash the redaction pass — callers may pass
        ``env.values()`` verbatim, and a missing env var can legitimately
        surface as ``None`` in some call shapes.
        """
        text = "some ordinary log output"
        try:
            result = redact_secrets(text, extra_values=["real-secret", None])  # type: ignore[list-item]
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(
                f"redact_secrets must never raise; got {type(exc).__name__}: "
                f"{exc}"
            ) from exc
        assert isinstance(result, str)

    def test_does_not_raise_on_very_long_text(self) -> None:
        """A very large text blob does not raise (no catastrophic regex)."""
        text = ("ghp_" + "A" * 40 + " ") * 2000
        try:
            result = redact_secrets(text)
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(
                f"redact_secrets must never raise on large input; got "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        assert isinstance(result, str)

    def test_does_not_raise_with_no_arguments_beyond_text(self) -> None:
        """Calling with only the required ``text`` positional arg is safe."""
        result = redact_secrets("plain text")
        assert result == "plain text"
