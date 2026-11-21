"""Redaction corpus (100% of seeded secrets removed), false positives, and properties."""

import string
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from sentinel.security.redaction import (
    clean,
    clean_value,
    redact_text,
    shannon_entropy,
    strip_control,
    truncate,
)

CORPUS = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "fixtures" / "redaction" / "corpus.yaml").read_text()
)


@pytest.mark.parametrize("case", CORPUS["secrets"], ids=lambda c: c["kind"])
def test_every_seeded_secret_is_removed(case: dict[str, Any]) -> None:
    result = redact_text(case["text"])
    assert case["secret"] not in result.text
    assert "[REDACTED:" in result.text
    assert result.redacted


def test_corpus_covers_the_required_kinds() -> None:
    """docs/2-architecture/6-security.md: AWS keys, GitHub tokens, JWTs, URL passwords,
    private keys, emails."""
    kinds = " ".join(c["kind"] for c in CORPUS["secrets"])
    for required in ("aws", "github", "jwt", "url", "private key", "email"):
        assert required in kinds


def test_corpus_score_is_100_percent() -> None:
    leaked = [c["kind"] for c in CORPUS["secrets"] if c["secret"] in redact_text(c["text"]).text]
    assert leaked == []
    assert len(CORPUS["secrets"]) >= 30


@pytest.mark.parametrize("line", CORPUS["benign"])
def test_benign_lines_are_left_alone(line: str) -> None:
    result = redact_text(line)
    assert result.text == line, result.counts


def test_allowlist_keeps_a_known_value() -> None:
    text = "build id Zx8Qp2LmN4vR7tY1wK9sD3fG6hJ0aB5c"
    assert "[REDACTED" in redact_text(text).text
    assert redact_text(text, allow={"Zx8Qp2LmN4vR7tY1wK9sD3fG6hJ0aB5c"}).text == text


def test_luhn_check_avoids_redacting_ordinary_numbers() -> None:
    assert redact_text("order 1234567890123456 shipped").text == "order 1234567890123456 shipped"
    assert "[REDACTED:card_number]" in redact_text("card 4111111111111111").text


def test_counts_by_kind() -> None:
    result = redact_text("a@example.com b@example.com AKIAIOSFODNN7EXAMPLE")
    assert result.counts == {"email": 2, "aws_access_key": 1}


# --------------------------------------------------------------- sanitising


def test_ansi_and_control_characters_are_stripped() -> None:
    raw = "\x1b[31;1mERROR\x1b[0m\x07 disk\x00 full\x1b]0;title\x07\r\n\tnext"
    assert strip_control(raw) == "ERROR disk full\n\tnext"


def test_truncate_marks_the_cut() -> None:
    text = "x" * 100
    out = truncate(text, 40)
    assert len(out) <= 40
    assert out.endswith("chars]")
    assert truncate("short", 40) == "short"


def test_clean_value_walks_nested_structures() -> None:
    data = {
        "user": "alice@example.com",
        "nested": [{"api_key": "anything"}, "Bearer opaque-token-123456"],
        "http.request.header.authorization": "Basic abc",
        "count": 3,
    }
    out = clean_value(data)
    assert out["user"] == "[REDACTED:email]"
    assert out["nested"][0]["api_key"] == "[REDACTED:sensitive_key]"
    assert "opaque-token-123456" not in out["nested"][1]
    assert out["http.request.header.authorization"] == "[REDACTED:sensitive_key]"
    assert out["count"] == 3


def test_entropy() -> None:
    assert shannon_entropy("") == 0
    assert shannon_entropy("aaaa") == 0
    assert shannon_entropy("abcd") == 2


# --------------------------------------------------------------- properties

printable = st.text(alphabet=string.printable, max_size=200)
SECRETS = [c["secret"] for c in CORPUS["secrets"] if "\n" not in c["text"]]


@settings(max_examples=300)
@given(printable)
def test_redaction_is_idempotent(text: str) -> None:
    once = redact_text(text).text
    assert redact_text(once).text == once


@settings(max_examples=300)
@given(st.text(max_size=300))
def test_clean_never_returns_control_characters(text: str) -> None:
    out = clean(text, max_chars=None)
    assert not any(ord(c) < 32 and c not in "\n\t" for c in out)
    assert "\x1b" not in out


@settings(max_examples=300)
@given(
    prefix=st.text(alphabet=string.ascii_letters + " .,:;()[]", max_size=40),
    suffix=st.text(alphabet=string.ascii_letters + " .,:;()[]", max_size=40),
    secret=st.sampled_from(SECRETS),
)
def test_a_secret_is_removed_wherever_it_appears(prefix: str, suffix: str, secret: str) -> None:
    case = next(c for c in CORPUS["secrets"] if c["secret"] == secret)
    text = f"{prefix} {case['text']} {suffix}"
    assert secret not in redact_text(text).text


@settings(max_examples=200)
@given(st.text(max_size=500), st.integers(min_value=40, max_value=400))
def test_clean_respects_the_length_cap(text: str, cap: int) -> None:
    assert len(clean(text, max_chars=cap)) <= cap
