"""What the gateway sends to a model runtime and what comes back.

A backend (the Ollama client, or a fake) turns a :class:`ChatRequest` into a
:class:`ChatResult`. ``task`` travels with the request so fakes can script
replies per task; it is never sent to the runtime.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypedDict


class Message(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class ChatOptions:
    num_ctx: int
    temperature: float
    seed: int
    num_predict: int

    def as_ollama(self) -> dict[str, Any]:
        return {
            "num_ctx": self.num_ctx,
            "temperature": self.temperature,
            "seed": self.seed,
            "num_predict": self.num_predict,
        }


@dataclass(frozen=True)
class ChatRequest:
    task: str
    model: str
    messages: Sequence[Message]
    options: ChatOptions
    format: dict[str, Any] | None = None  # JSON schema: constrained decoding
    think: bool | None = None  # None: the model's own default (not sent)
    keep_alive: str | None = None

    def body(self, *, stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [dict(m) for m in self.messages],
            "stream": stream,
            "options": self.options.as_ollama(),
        }
        if self.format is not None:
            body["format"] = self.format
        if self.think is not None:
            body["think"] = self.think
        if self.keep_alive is not None:
            body["keep_alive"] = self.keep_alive
        return body


@dataclass(frozen=True)
class ChatResult:
    model: str
    content: str
    thinking: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None  # thinking included
    latency_ms: int = 0
    ttft_ms: int | None = None  # first token of any kind (thinking or answer)
    first_content_ms: int | None = None  # first token of the answer
    load_ms: int | None = None
    prompt_eval_ms: int | None = None
    eval_ms: int | None = None
    done_reason: str | None = None

    @property
    def tokens_per_s(self) -> float | None:
        if not self.output_tokens or not self.eval_ms:
            return None
        return self.output_tokens / (self.eval_ms / 1000)

    @property
    def prompt_tokens_per_s(self) -> float | None:
        if not self.input_tokens or not self.prompt_eval_ms:
            return None
        return self.input_tokens / (self.prompt_eval_ms / 1000)


@dataclass(frozen=True)
class EmbedRequest:
    task: str
    model: str
    inputs: Sequence[str]
    keep_alive: str | None = None


@dataclass(frozen=True)
class EmbedResult:
    model: str
    embeddings: list[list[float]]
    input_tokens: int | None = None
    latency_ms: int = 0
    load_ms: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class Backend(Protocol):
    async def chat(self, request: ChatRequest) -> ChatResult: ...

    async def embed(self, request: EmbedRequest) -> EmbedResult: ...
