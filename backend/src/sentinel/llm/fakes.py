"""Test doubles for the gateway (AI design §4.1).

- :func:`FakeLLM`: scripted replies per task, for unit and workflow tests. The
  replies go through the real gateway, so validation, schema retries and call
  logging behave as in production.
- :func:`ReplayLLM`: replays replies recorded from a real model, keyed by the
  exact request (model, messages, schema, options), for deterministic
  regression tests. Record a cassette with :class:`ReplayBackend` in
  ``record`` mode around a real :class:`OllamaClient`.
"""

import hashlib
import json
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from sentinel.config import Settings
from sentinel.llm.errors import LLMError
from sentinel.llm.gateway import LLMGateway
from sentinel.llm.recorder import MemorySink
from sentinel.llm.types import Backend, ChatRequest, ChatResult, EmbedRequest, EmbedResult

# A scripted reply: a model instance or dict (sent as its JSON), raw text (sent
# as is, e.g. to exercise a schema retry), an error to raise, or a function of the request.
Reply = BaseModel | Mapping[str, Any] | str | LLMError | Callable[[ChatRequest], Any]

EMBED_DIM = 768


class FakeBackend:
    def __init__(self, script: Mapping[str, Iterable[Reply]] | None = None) -> None:
        self.replies: dict[str, deque[Reply]] = defaultdict(deque)
        for task, replies in (script or {}).items():
            self.replies[task].extend(replies)
        self.requests: list[ChatRequest] = []
        self.embed_requests: list[EmbedRequest] = []

    def add(self, task: str, *replies: Reply) -> None:
        self.replies[task].extend(replies)

    async def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        queue = self.replies.get(request.task)
        if not queue:
            raise AssertionError(f"FakeLLM: no scripted reply left for task {request.task!r}")
        reply = queue.popleft()
        if callable(reply) and not isinstance(reply, BaseModel):
            reply = reply(request)
        if isinstance(reply, LLMError):
            raise reply
        content = _text(reply)
        return ChatResult(
            model=request.model,
            content=content,
            input_tokens=sum(len(m["content"]) // 4 for m in request.messages),
            output_tokens=max(1, len(content) // 4),
            latency_ms=1,
            ttft_ms=1,
            first_content_ms=1,
        )

    async def embed(self, request: EmbedRequest) -> EmbedResult:
        """Deterministic unit vectors from a hash of each text (same text, same vector)."""
        self.embed_requests.append(request)
        return EmbedResult(
            model=request.model,
            embeddings=[_hash_vector(t) for t in request.inputs],
            input_tokens=sum(len(t) // 4 for t in request.inputs),
            latency_ms=1,
        )


def FakeLLM(  # noqa: N802 - reads as a class at call sites
    script: Mapping[str, Iterable[Reply]] | None = None, settings: Settings | None = None
) -> LLMGateway:
    """A gateway over scripted replies. ``.backend`` has the requests, ``.sink.calls`` the log."""
    return LLMGateway(FakeBackend(script), settings or Settings(), MemorySink())


class ReplayMissError(LookupError):
    """The request is not in the cassette (the prompt, schema or options changed)."""


class ReplayBackend:
    def __init__(
        self,
        cassette: Path,
        *,
        mode: Literal["replay", "record"] = "replay",
        inner: Backend | None = None,
    ) -> None:
        if mode == "record" and inner is None:
            raise ValueError("record mode needs the real backend to record from")
        self.cassette = cassette
        self.mode = mode
        self.inner = inner
        self.entries: dict[str, dict[str, Any]] = {}
        if cassette.exists():
            for line in cassette.read_text().splitlines():
                if line.strip():
                    entry = json.loads(line)
                    self.entries[entry["key"]] = entry

    async def chat(self, request: ChatRequest) -> ChatResult:
        key = request_key(request)
        if key in self.entries:
            return ChatResult(**self.entries[key]["result"])
        if self.mode == "replay" or self.inner is None:
            raise ReplayMissError(f"no recorded reply for task {request.task!r} ({key[:12]})")
        result = await self.inner.chat(request)
        entry = {"key": key, "task": request.task, "result": _result_dict(result)}
        self.entries[key] = entry
        self.cassette.parent.mkdir(parents=True, exist_ok=True)
        with self.cassette.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return result

    async def embed(self, request: EmbedRequest) -> EmbedResult:
        if self.inner is not None and self.mode == "record":
            return await self.inner.embed(request)
        return await FakeBackend().embed(request)


def ReplayLLM(cassette: Path, settings: Settings | None = None) -> LLMGateway:  # noqa: N802
    return LLMGateway(ReplayBackend(cassette), settings or Settings(), MemorySink())


def request_key(request: ChatRequest) -> str:
    canonical = json.dumps(
        {
            "model": request.model,
            "messages": [dict(m) for m in request.messages],
            "format": request.format,
            "think": request.think,
            "options": request.options.as_ollama(),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _text(reply: Any) -> str:
    if isinstance(reply, BaseModel):
        return reply.model_dump_json()
    if isinstance(reply, Mapping):
        return json.dumps(dict(reply))
    return str(reply)


def _result_dict(result: ChatResult) -> dict[str, Any]:
    return {
        "model": result.model,
        "content": result.content,
        "thinking": result.thinking,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "latency_ms": result.latency_ms,
        "ttft_ms": result.ttft_ms,
        "first_content_ms": result.first_content_ms,
        "done_reason": result.done_reason,
    }


def _hash_vector(text: str, dim: int = EMBED_DIM) -> list[float]:
    raw = b""
    counter = 0
    while len(raw) < dim:
        raw += hashlib.sha256(f"{counter}:{text}".encode()).digest()
        counter += 1
    values = [(b - 127.5) / 127.5 for b in raw[:dim]]
    norm = sum(v * v for v in values) ** 0.5 or 1.0
    return [v / norm for v in values]
