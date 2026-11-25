"""The LLM gateway: every model call in SENTINEL goes through here (ADR-0002).

``generate_structured`` asks for JSON that matches a Pydantic schema (Ollama's
``format``: constrained decoding), validates the reply, and on a schema
violation asks again with the validation errors as feedback (at most
``schema_retries`` more times). Every attempt is logged to ``llm_calls`` and
traced as an OpenTelemetry span (GenAI conventions; no prompt text in spans).
"""

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel, ValidationError

from sentinel.config import Settings
from sentinel.db.ids import uuid7
from sentinel.llm.errors import LLMEmptyReplyError, LLMError, LLMSchemaError
from sentinel.llm.recorder import CallRecord, CallSink, NullSink
from sentinel.llm.schema import describe_error, json_schema, parse_reply, prompt_sha256
from sentinel.llm.schema import retry_feedback as feedback_message
from sentinel.llm.types import Backend, ChatOptions, ChatRequest, EmbedRequest, Message

tracer = trace.get_tracer("sentinel.llm")

PROVIDER = "ollama"

# Thinking per task (AI design §4.1): on where reasoning pays, off for prose.
DEFAULT_THINKING: dict[str, bool] = {
    "rca": True,
    "code_investigation": True,
    "patch": True,
    "report": False,
}


@dataclass(frozen=True)
class TaskConfig:
    model: str
    think: bool
    num_ctx: int


class LLMGateway:
    def __init__(self, backend: Backend, settings: Settings, sink: CallSink | None = None) -> None:
        self.backend = backend
        self.settings = settings
        self.sink: CallSink = sink or NullSink()

    def task_config(self, task: str) -> TaskConfig:
        s = self.settings
        think = s.llm_task_thinking.get(task, DEFAULT_THINKING.get(task, False))
        return TaskConfig(
            model=s.llm_task_models.get(task, s.llm_model), think=think, num_ctx=s.llm_num_ctx
        )

    async def generate_structured[T: BaseModel](
        self,
        task: str,
        schema: type[T],
        messages: Sequence[Message],
        *,
        model: str | None = None,
        think: bool | None = None,
        num_ctx: int | None = None,
        max_retries: int | None = None,
        prompt_version: str = "",
        investigation_id: uuid.UUID | None = None,
    ) -> T:
        """Return a validated ``schema`` instance, or raise :class:`LLMError`."""
        cfg = self.task_config(task)
        s = self.settings
        options = ChatOptions(
            num_ctx=num_ctx or cfg.num_ctx,
            temperature=s.llm_temperature,
            seed=s.llm_seed,
            num_predict=s.llm_max_output_tokens,
        )
        fmt = json_schema(schema)
        conversation: list[Message] = list(messages)
        retries = s.llm_schema_retries if max_retries is None else max_retries
        request_id = uuid7()
        last_error = ""
        for attempt in range(1, retries + 2):
            request = ChatRequest(
                task=task,
                model=model or cfg.model,
                messages=tuple(conversation),
                options=options,
                format=fmt,
                think=cfg.think if think is None else think,
                keep_alive=s.llm_keep_alive,
            )
            content, record = await self._attempt(
                request, request_id, attempt, prompt_version, investigation_id
            )
            try:
                value = parse_reply(schema, content)
            except (ValidationError, ValueError) as exc:
                last_error = describe_error(exc)
                await self._schema_failure(record, last_error)
                conversation = [
                    *conversation,
                    {"role": "assistant", "content": content[:4000]},
                    feedback_message(last_error),
                ]
                continue
            await self.sink.record(record)
            return value
        raise LLMSchemaError(task, retries + 1, last_error)

    async def embed(
        self, texts: Sequence[str], *, task: str = "embed", model: str | None = None
    ) -> list[list[float]]:
        request = EmbedRequest(
            task=task,
            model=model or self.settings.llm_embed_model,
            inputs=tuple(texts),
            keep_alive=self.settings.llm_keep_alive,
        )
        record = CallRecord(
            request_id=uuid7(),
            attempt=1,
            operation="embed",
            task=task,
            model=request.model,
            prompt_version="",
            prompt_sha256=prompt_sha256(
                request.model, [{"role": "user", "content": t} for t in texts], None, None
            ),
            thinking=False,
            latency_ms=0,
            success=False,
        )
        started = time.perf_counter()
        with tracer.start_as_current_span(f"embeddings {request.model}") as span:
            span.set_attributes(
                {
                    "gen_ai.operation.name": "embeddings",
                    "gen_ai.provider.name": PROVIDER,
                    "gen_ai.request.model": request.model,
                    "sentinel.llm.task": task,
                    "sentinel.llm.inputs": len(texts),
                }
            )
            try:
                result = await self.backend.embed(request)
            except LLMError as exc:
                record.latency_ms = _elapsed_ms(started)
                _fail(span, record, exc)
                await self.sink.record(record)
                raise
            record.latency_ms = result.latency_ms or _elapsed_ms(started)
            record.success = True
            record.input_tokens = result.input_tokens
            record.load_ms = result.load_ms
            if result.input_tokens is not None:
                span.set_attribute("gen_ai.usage.input_tokens", result.input_tokens)
        await self.sink.record(record)
        return result.embeddings

    # ------------------------------------------------------------- internals
    async def _attempt(
        self,
        request: ChatRequest,
        request_id: uuid.UUID,
        attempt: int,
        prompt_version: str,
        investigation_id: uuid.UUID | None,
    ) -> tuple[str, CallRecord]:
        """One request. A runtime failure is recorded and raised; a reply is returned
        with its (not yet written) record, which the caller marks valid or not."""
        record = CallRecord(
            request_id=request_id,
            attempt=attempt,
            operation="chat",
            task=request.task,
            model=request.model,
            prompt_version=prompt_version,
            prompt_sha256=prompt_sha256(
                request.model, request.messages, request.format, request.think
            ),
            thinking=bool(request.think),
            num_ctx=request.options.num_ctx,
            latency_ms=0,
            success=False,
            investigation_id=investigation_id,
            request_payload=_payload(request) if self.settings.llm_store_payloads else None,
        )
        started = time.perf_counter()
        with tracer.start_as_current_span(f"chat {request.model}") as span:
            span.set_attributes(
                {
                    "gen_ai.operation.name": "chat",
                    "gen_ai.provider.name": PROVIDER,
                    "gen_ai.request.model": request.model,
                    "gen_ai.request.temperature": request.options.temperature,
                    "gen_ai.request.seed": request.options.seed,
                    "gen_ai.request.max_tokens": request.options.num_predict,
                    "gen_ai.output.type": "json",
                    "sentinel.llm.task": request.task,
                    "sentinel.llm.attempt": attempt,
                    "sentinel.llm.thinking": bool(request.think),
                    "sentinel.llm.num_ctx": request.options.num_ctx,
                    "sentinel.llm.prompt_version": prompt_version,
                }
            )
            try:
                result = await self.backend.chat(request)
                if not result.content.strip():
                    raise LLMEmptyReplyError(
                        f"no answer (done_reason={result.done_reason}, "
                        f"{result.output_tokens} tokens, thinking only)"
                    )
            except LLMError as exc:
                record.latency_ms = _elapsed_ms(started)
                _fail(span, record, exc)
                await self.sink.record(record)
                raise
            record.success = True
            record.latency_ms = result.latency_ms or _elapsed_ms(started)
            record.ttft_ms = result.ttft_ms
            record.load_ms = result.load_ms
            record.input_tokens = result.input_tokens
            record.output_tokens = result.output_tokens
            record.response = result.content if self.settings.llm_store_payloads else None
            record.thinking_text = result.thinking if self.settings.llm_store_payloads else None
            span.set_attribute("gen_ai.response.model", result.model)
            if result.input_tokens is not None:
                span.set_attribute("gen_ai.usage.input_tokens", result.input_tokens)
            if result.output_tokens is not None:
                span.set_attribute("gen_ai.usage.output_tokens", result.output_tokens)
            if result.done_reason:
                span.set_attribute("gen_ai.response.finish_reasons", [result.done_reason])
            if result.ttft_ms is not None:
                span.set_attribute("sentinel.llm.ttft_ms", result.ttft_ms)
        return result.content, record

    async def _schema_failure(self, record: CallRecord, error: str) -> None:
        """Record the attempt whose reply arrived but did not validate."""
        record.success = False
        record.error_kind = "schema"
        record.error = error
        trace.get_current_span().add_event(
            "sentinel.llm.schema_violation", {"attempt": record.attempt, "error": error[:500]}
        )
        await self.sink.record(record)


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _fail(span: trace.Span, record: CallRecord, exc: LLMError) -> None:
    record.success = False
    record.error_kind = exc.kind
    record.error = str(exc)
    span.set_attribute("error.type", type(exc).__name__)
    span.set_status(Status(StatusCode.ERROR, exc.kind))


def _payload(request: ChatRequest) -> dict[str, Any]:
    return {
        "model": request.model,
        "messages": [dict(m) for m in request.messages],
        "think": request.think,
        "options": request.options.as_ollama(),
    }
