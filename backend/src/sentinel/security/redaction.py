"""Secret redaction and text sanitising for anything SENTINEL stores or shows a model.

Telemetry is untrusted: it can contain credentials and attacker-written text.
Every log line, span attribute and other excerpt passes through ``clean`` before
it is stored as evidence or put into a prompt. ``clean``:

1. strips ANSI escape sequences and control characters (keeps newline and tab),
2. replaces secrets with ``[REDACTED:<kind>]``,
3. truncates.

Detection uses known secret formats first, then ``key=value`` patterns,
credentials in URLs, emails and card numbers (Luhn-checked), and finally a
high-entropy check for unknown token formats. Trace IDs, span IDs, UUIDs and
commit SHAs are never redacted: investigations need them.

Redaction is idempotent: redacting already-redacted text changes nothing.
"""

import math
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

PLACEHOLDER = "[REDACTED:{kind}]"
_ALREADY = r"(?!\[REDACTED)"

# CSI/OSC/other escape sequences, then the remaining C0/C1 controls except \t and \n.
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

_SENSITIVE_KEY = (
    r"password|passwd|pwd|pass|secret|client[_-]?secret|api[_-]?key|apikey|access[_-]?key"
    r"|secret[_-]?key|private[_-]?key|auth[_-]?token|access[_-]?token|refresh[_-]?token"
    r"|session[_-]?token|token|credentials?|aws_secret_access_key|authorization|cookie"
)
SENSITIVE_KEY_RE = re.compile(rf"(?i)^(?:.*[._-])?(?:{_SENSITIVE_KEY})$")


@dataclass(frozen=True)
class _Rule:
    kind: str
    pattern: re.Pattern[str]
    # Number of leading groups kept verbatim (e.g. "password=" in "password=hunter2").
    keep_groups: int = 0
    check: Callable[[str], bool] | None = None


def _luhn(digits: str) -> bool:
    nums = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(nums) <= 19:
        return False
    total = 0
    for i, n in enumerate(reversed(nums)):
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


_RULES: tuple[_Rule, ...] = (
    _Rule(
        "private_key",
        re.compile(
            r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"
            r"[\s\S]*?-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----"
        ),
    ),
    _Rule("aws_access_key", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")),
    _Rule(
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})\b"),
    ),
    _Rule("sentinel_token", re.compile(r"\b(?:sk_live|sit)_[a-z0-9]{8}_[A-Za-z0-9_-]{20,}")),
    _Rule("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    _Rule("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    _Rule("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    _Rule("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    _Rule(
        "auth_header",
        re.compile(rf"(?i)\b((?:bearer|basic|token)\s+){_ALREADY}[A-Za-z0-9._~+/=-]{{8,}}"),
        keep_groups=1,
    ),
    _Rule(
        "url_credentials",
        re.compile(rf"([a-z][a-z0-9+.-]*://[^\s:/@]+:){_ALREADY}([^\s@/]+)(?=@)", re.IGNORECASE),
        keep_groups=1,
    ),
    _Rule(
        "key_value",
        re.compile(
            rf"(?i)((?<![A-Za-z0-9])[\"']?(?:[A-Za-z0-9]+[._-])*(?:{_SENSITIVE_KEY})[\"']?"
            rf"\s*[:=]\s*[\"']?){_ALREADY}([^\s\"',;&}}\]<>]{{3,}})"
        ),
        keep_groups=1,
    ),
    _Rule(
        "email",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"),
    ),
    _Rule("card_number", re.compile(r"\b\d(?:[ -]?\d){12,18}\b"), check=_luhn),
)

# Values investigations need, never treated as secrets by the entropy check.
_ALLOWED_SHAPES = (
    re.compile(
        r"[0-9a-f]{16}|[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64}", re.IGNORECASE
    ),  # span/trace/SHA
    re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE),
    re.compile(r"[0-9]+"),
)
# "=" only as trailing base64 padding, so "key=value" splits into its parts.
_CANDIDATE = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{24,}={0,2}(?![A-Za-z0-9+/_=-])")
ENTROPY_THRESHOLD = 4.0


def shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = Counter(value)
    n = len(value)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def _looks_random(token: str, allow: frozenset[str]) -> bool:
    if token in allow or token.startswith("[REDACTED"):
        return False
    if any(shape.fullmatch(token) for shape in _ALLOWED_SHAPES):
        return False
    if "/" in token and not re.search(r"[+=]", token):
        return False  # a path, not base64
    has_upper = any(c.isupper() for c in token)
    has_lower = any(c.islower() for c in token)
    has_digit = any(c.isdigit() for c in token)
    if (has_upper + has_lower + has_digit) < 3:
        return False
    return shannon_entropy(token) >= ENTROPY_THRESHOLD


@dataclass
class RedactionResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def redacted(self) -> bool:
        return bool(self.counts)


def strip_control(text: str) -> str:
    """Remove ANSI escape sequences and control characters (newline and tab are kept)."""
    return _CONTROL.sub("", _ANSI.sub("", text))


def redact_text(text: str, *, allow: Iterable[str] = ()) -> RedactionResult:
    allowed = frozenset(allow)
    counts: Counter[str] = Counter()

    for rule in _RULES:

        def replace(m: re.Match[str], rule: _Rule = rule) -> str:
            secret = m.group(0)
            if rule.check is not None and not rule.check(secret):
                return secret
            kept = "".join(m.group(i) or "" for i in range(1, rule.keep_groups + 1))
            if secret[len(kept) :] in allowed:
                return secret
            counts[rule.kind] += 1
            return kept + PLACEHOLDER.format(kind=rule.kind)

        text = rule.pattern.sub(replace, text)

    def entropy(m: re.Match[str]) -> str:
        if not _looks_random(m.group(0), allowed):
            return m.group(0)
        counts["high_entropy"] += 1
        return PLACEHOLDER.format(kind="high_entropy")

    text = _CANDIDATE.sub(entropy, text)
    return RedactionResult(text, dict(counts))


def truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    marker = f"…[truncated {len(text) - max_chars} chars]"
    return text[: max(0, max_chars - len(marker))] + marker


def clean(text: str, *, max_chars: int | None = 2000, allow: Iterable[str] = ()) -> str:
    """Sanitise, redact and truncate one untrusted string."""
    result = redact_text(strip_control(text), allow=allow).text
    return truncate(result, max_chars) if max_chars is not None else result


def clean_value(value: Any, *, max_chars: int | None = 2000) -> Any:
    """``clean`` applied through dicts and lists. Values under sensitive keys are dropped."""
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            k = strip_control(str(key))
            out[k] = (
                PLACEHOLDER.format(kind="sensitive_key")
                if SENSITIVE_KEY_RE.match(k)
                else clean_value(item, max_chars=max_chars)
            )
        return out
    if isinstance(value, list | tuple):
        return [clean_value(item, max_chars=max_chars) for item in value]
    if isinstance(value, str):
        return clean(value, max_chars=max_chars)
    return value
