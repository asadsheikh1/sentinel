"""Structured output: the JSON schema sent as Ollama's ``format``, and parsing the reply.

Ollama turns ``format`` into a grammar, so the model can only produce JSON of
that shape (constrained decoding). The reply is still validated with Pydantic,
which also enforces what a grammar cannot (lengths, patterns, cross-field
rules).
"""

import hashlib
import json
import re
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ValidationError

from sentinel.llm.types import Message


def json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The model's JSON schema with every local ``$ref`` inlined (no ``$defs``).

    Grammar converters handle inline schemas most reliably. Recursive models
    cannot be inlined and are refused.
    """
    schema = model.model_json_schema()
    defs: dict[str, Any] = schema.pop("$defs", {})

    def resolve(node: Any, seen: tuple[str, ...]) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.removeprefix("#/$defs/")
                if name in seen:
                    raise ValueError(f"recursive schema ({name}) cannot be used for output")
                target = resolve(defs[name], (*seen, name))
                rest = {k: resolve(v, seen) for k, v in node.items() if k != "$ref"}
                return {**target, **rest}
            return {k: resolve(v, seen) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v, seen) for v in node]
        return node

    resolved: dict[str, Any] = resolve(schema, ())
    return resolved


_THINK = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def clean_reply(content: str) -> str:
    """Drop an inline ``<think>`` block or a Markdown fence around the JSON, if any."""
    text = _THINK.sub("", content)
    fenced = _FENCE.match(text)
    return (fenced.group(1) if fenced else text).strip()


def parse_reply[T: BaseModel](schema: type[T], content: str) -> T:
    """Validate a reply. Raises :class:`pydantic.ValidationError` (or ``ValueError``)."""
    text = clean_reply(content)
    if not text:
        raise ValueError("the reply was empty")
    return schema.model_validate_json(text)


def describe_error(exc: Exception, limit: int = 1500) -> str:
    """A compact description of why a reply failed, for the retry prompt and the log."""
    if isinstance(exc, ValidationError):
        parts = []
        for err in exc.errors(include_url=False, include_input=False):
            where = ".".join(str(p) for p in err["loc"]) or "(root)"
            parts.append(f"{where}: {err['msg']}")
        text = "; ".join(parts)
    else:
        text = str(exc)
    return text[:limit]


def retry_feedback(error: str) -> Message:
    return {
        "role": "user",
        "content": (
            "Your previous reply did not match the required JSON schema: "
            f"{error}\nReply again with only one JSON object that matches the schema."
        ),
    }


def prompt_sha256(
    model: str, messages: Sequence[Message], fmt: dict[str, Any] | None, think: bool | None
) -> str:
    """A stable hash of what the model was asked (the prompt itself is not logged)."""
    canonical = json.dumps(
        {"model": model, "messages": list(messages), "format": fmt, "think": think},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
