"""LLM gateway (Step 15): the Ollama client against a mock server, the gateway, the fakes."""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel, Field, ValidationError

from sentinel.config import Settings
from sentinel.llm import (
    LLMEmptyReplyError,
    LLMGateway,
    LLMHTTPError,
    LLMSchemaError,
    LLMTimeoutError,
    LLMUnavailableError,
    MemorySink,
    OllamaClient,
    ollama_client,
)
from sentinel.llm.fakes import (
    EMBED_DIM,
    FakeBackend,
    FakeLLM,
    ReplayBackend,
    ReplayLLM,
    ReplayMissError,
)
from sentinel.llm.schema import clean_reply, describe_error, json_schema, parse_reply, prompt_sha256
from sentinel.llm.types import ChatOptions, ChatRequest, EmbedRequest, Message

# --------------------------------------------------------------- schemas used here


class Hypothesis(BaseModel):
    title: str = Field(min_length=3)
    category: Literal["code_change", "config_change", "unknown"]
    evidence_ids: list[str]


class Rca(BaseModel):
    hypotheses: list[Hypothesis] = Field(min_length=1, max_length=5)


class Node(BaseModel):
    children: list["Node"] = []


GOOD = {"hypotheses": [{"title": "Bad deploy", "category": "code_change", "evidence_ids": ["E-1"]}]}
MESSAGES: list[Message] = [{"role": "user", "content": "Why did checkout fail?"}]

_exporter = InMemorySpanExporter()


@pytest.fixture(scope="module", autouse=True)
def _tracing() -> None:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_exporter))
    trace.set_tracer_provider(provider)


@pytest.fixture
def spans() -> InMemorySpanExporter:
    _exporter.clear()
    return _exporter


def settings(**overrides: Any) -> Settings:
    return Settings(**overrides)


# ------------------------------------------------------------------- schema helpers


def test_json_schema_inlines_refs() -> None:
    schema = json_schema(Rca)
    assert "$defs" not in schema
    assert "$ref" not in json.dumps(schema)
    item = schema["properties"]["hypotheses"]["items"]
    assert item["properties"]["category"]["enum"] == ["code_change", "config_change", "unknown"]


def test_json_schema_refuses_recursive_models() -> None:
    with pytest.raises(ValueError, match="recursive"):
        json_schema(Node)


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps(GOOD),
        f"<think>\nhmm\n</think>\n{json.dumps(GOOD)}",
        f"```json\n{json.dumps(GOOD)}\n```",
        f"  {json.dumps(GOOD)}\n",
    ],
)
def test_parse_reply_accepts_wrapped_json(raw: str) -> None:
    assert parse_reply(Rca, raw).hypotheses[0].evidence_ids == ["E-1"]


def test_parse_reply_rejects_empty_and_invalid() -> None:
    with pytest.raises(ValueError, match="empty"):
        parse_reply(Rca, "<think>only thoughts</think>")
    with pytest.raises(ValidationError) as info:
        parse_reply(Rca, '{"hypotheses": [{"title": "x", "category": "aliens"}]}')
    text = describe_error(info.value)
    assert "hypotheses.0.title" in text
    assert "hypotheses.0.category" in text
    assert "hypotheses.0.evidence_ids" in text
    assert "aliens" not in text  # inputs are not echoed into the log


def test_clean_reply_leaves_plain_text() -> None:
    assert clean_reply('{"a": 1}') == '{"a": 1}'


def test_prompt_hash_is_stable_and_sensitive() -> None:
    a = prompt_sha256("m", MESSAGES, {"type": "object"}, True)
    assert a == prompt_sha256("m", list(MESSAGES), {"type": "object"}, True)
    assert a != prompt_sha256("m", MESSAGES, {"type": "object"}, False)
    assert a != prompt_sha256("other", MESSAGES, {"type": "object"}, True)
    assert len(a) == 64


# --------------------------------------------------------------- Ollama, mock server


def stream_lines(*chunks: dict[str, Any]) -> bytes:
    return "".join(json.dumps(c) + "\n" for c in chunks).encode()


def chat_stream(content: str = json.dumps(GOOD), thinking: str = "") -> bytes:
    chunks: list[dict[str, Any]] = []
    if thinking:
        chunks.append({"message": {"role": "assistant", "content": "", "thinking": thinking}})
    half = len(content) // 2
    chunks += [
        {"message": {"role": "assistant", "content": content[:half]}, "done": False},
        {"message": {"role": "assistant", "content": content[half:]}, "done": False},
        {
            "model": "qwen3:4b",
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 120,
            "prompt_eval_duration": 400_000_000,
            "eval_count": 40,
            "eval_duration": 2_000_000_000,
            "load_duration": 1_500_000_000,
        },
    ]
    return stream_lines(*chunks)


class MockOllama:
    """A scripted Ollama: each handler answers one request, in order."""

    def __init__(self, *handlers: Callable[[httpx.Request], httpx.Response]) -> None:
        self.handlers = list(handlers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handlers.pop(0)(request)

    def client(self, **kwargs: Any) -> OllamaClient:
        kwargs.setdefault("backoff_s", 0)
        return OllamaClient("http://ollama.test", transport=httpx.MockTransport(self), **kwargs)

    def body(self, n: int = 0) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(self.requests[n].content)
        return result


def ok(content: bytes) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _: httpx.Response(200, content=content)


def status(code: int, error: str = "boom") -> Callable[[httpx.Request], httpx.Response]:
    return lambda _: httpx.Response(code, json={"error": error})


def raises(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_: httpx.Request) -> httpx.Response:
        raise exc

    return handler


def chat_request(**overrides: Any) -> ChatRequest:
    base: dict[str, Any] = {
        "task": "rca",
        "model": "qwen3:4b",
        "messages": MESSAGES,
        "options": ChatOptions(num_ctx=16384, temperature=0.1, seed=42, num_predict=512),
        "format": json_schema(Rca),
        "think": True,
        "keep_alive": "30m",
    }
    return ChatRequest(**{**base, **overrides})


async def test_chat_streams_and_measures() -> None:
    server = MockOllama(ok(chat_stream(thinking="Let me look at the deploy.")))
    result = await server.client().chat(chat_request())

    assert parse_reply(Rca, result.content).hypotheses[0].title == "Bad deploy"
    assert result.thinking == "Let me look at the deploy."
    assert (result.input_tokens, result.output_tokens) == (120, 40)
    assert (result.load_ms, result.prompt_eval_ms, result.eval_ms) == (1500, 400, 2000)
    assert result.tokens_per_s == pytest.approx(20.0)
    assert result.prompt_tokens_per_s == pytest.approx(300.0)
    assert result.ttft_ms is not None
    assert result.first_content_ms is not None
    assert result.ttft_ms <= result.first_content_ms
    assert result.done_reason == "stop"

    request = server.requests[0]
    assert request.url.path == "/api/chat"
    body = server.body()
    assert body["stream"] is True
    assert body["think"] is True
    assert body["keep_alive"] == "30m"
    assert body["options"] == {"num_ctx": 16384, "temperature": 0.1, "seed": 42, "num_predict": 512}
    assert body["format"]["properties"]["hypotheses"]["type"] == "array"


async def test_think_is_omitted_when_unset() -> None:
    server = MockOllama(ok(chat_stream()))
    await server.client().chat(chat_request(think=None, format=None, keep_alive=None))
    body = server.body()
    assert "think" not in body
    assert "format" not in body
    assert "keep_alive" not in body


async def test_unknown_model_is_not_retried() -> None:
    server = MockOllama(status(404, "model 'nope' not found"))
    with pytest.raises(LLMHTTPError, match="not found") as info:
        await server.client(transport_retries=3).chat(chat_request(model="nope"))
    assert info.value.status == 404
    assert len(server.requests) == 1


async def test_server_error_is_retried_then_succeeds() -> None:
    server = MockOllama(status(503), ok(chat_stream()))
    result = await server.client(transport_retries=1).chat(chat_request())
    assert result.output_tokens == 40
    assert len(server.requests) == 2


async def test_connection_refused_gives_up_after_retries() -> None:
    server = MockOllama(*[raises(httpx.ConnectError("refused"))] * 3)
    with pytest.raises(LLMUnavailableError, match="ConnectError"):
        await server.client(transport_retries=2).chat(chat_request())
    assert len(server.requests) == 3


async def test_stalled_stream_times_out() -> None:
    server = MockOllama(raises(httpx.ReadTimeout("stalled")))
    with pytest.raises(LLMTimeoutError, match="ReadTimeout"):
        await server.client(transport_retries=0).chat(chat_request())


async def test_whole_request_deadline() -> None:
    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, content=chat_stream())

    client = OllamaClient(
        "http://ollama.test",
        transport=httpx.MockTransport(slow),
        total_timeout_s=0.05,
        transport_retries=0,
    )
    with pytest.raises(LLMTimeoutError, match="no complete reply"):
        await client.chat(chat_request())
    await client.aclose()


async def test_error_inside_the_stream() -> None:
    server = MockOllama(ok(stream_lines({"error": "out of memory"})))
    with pytest.raises(LLMUnavailableError, match="out of memory"):
        await server.client(transport_retries=0).chat(chat_request())


async def test_stream_that_never_finishes() -> None:
    server = MockOllama(ok(stream_lines({"message": {"content": "{"}, "done": False})))
    with pytest.raises(LLMUnavailableError, match="ended before"):
        await server.client(transport_retries=0).chat(chat_request())


async def test_malformed_stream_line() -> None:
    server = MockOllama(ok(b"not json\n"))
    with pytest.raises(LLMUnavailableError, match="malformed"):
        await server.client(transport_retries=0).chat(chat_request())


async def test_embed() -> None:
    reply = {
        "model": "nomic-embed-text:v1.5",
        "embeddings": [[0.1, 0.2], [0.3, 0.4]],
        "prompt_eval_count": 8,
        "load_duration": 2_000_000,
    }
    server = MockOllama(ok(json.dumps(reply).encode()))
    result = await server.client().embed(
        EmbedRequest(task="memory", model="nomic-embed-text:v1.5", inputs=["a", "b"])
    )
    assert result.embeddings == [[0.1, 0.2], [0.3, 0.4]]
    assert (result.input_tokens, result.load_ms) == (8, 2)
    assert server.requests[0].url.path == "/api/embed"
    assert server.body() == {"model": "nomic-embed-text:v1.5", "input": ["a", "b"]}


async def test_embed_count_mismatch() -> None:
    server = MockOllama(ok(json.dumps({"embeddings": [[0.1]]}).encode()))
    with pytest.raises(LLMUnavailableError, match="no embeddings"):
        await server.client(transport_retries=0).embed(
            EmbedRequest(task="memory", model="m", inputs=["a", "b"])
        )


async def test_inspection_endpoints() -> None:
    server = MockOllama(
        ok(json.dumps({"version": "0.40.16"}).encode()),
        ok(json.dumps({"models": [{"name": "qwen3:4b", "size": 5, "size_vram": 5}]}).encode()),
        ok(json.dumps({"details": {"quantization_level": "Q4_K_M"}}).encode()),
        ok(json.dumps({"done": True}).encode()),
    )
    client = server.client()
    assert await client.version() == "0.40.16"
    assert (await client.loaded())[0]["name"] == "qwen3:4b"
    assert (await client.show("qwen3:4b"))["details"]["quantization_level"] == "Q4_K_M"
    await client.unload("qwen3:4b")
    assert server.body(3) == {"model": "qwen3:4b", "keep_alive": 0}


def test_client_from_settings() -> None:
    client = ollama_client(settings(ollama_url="http://host.containers.internal:11434/"))
    assert client.base_url == "http://host.containers.internal:11434"


# ------------------------------------------------------------------------- gateway


async def test_structured_reply_is_validated_and_logged(spans: InMemorySpanExporter) -> None:
    llm = FakeLLM({"rca": [GOOD]})
    result = await llm.generate_structured("rca", Rca, MESSAGES, prompt_version="rca-v1")
    assert result.hypotheses[0].category == "code_change"

    (call,) = llm.sink.calls  # type: ignore[attr-defined]
    assert call.success
    assert call.attempt == 1
    assert call.task == "rca"
    assert call.model == "qwen3:14b"
    assert call.thinking is True
    assert call.num_ctx == 16384
    assert call.prompt_version == "rca-v1"
    assert len(call.prompt_sha256) == 64
    assert call.request_payload is None  # payload storage is off by default

    (span,) = spans.get_finished_spans()
    assert span.name == "chat qwen3:14b"
    attrs = dict(span.attributes or {})
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.provider.name"] == "ollama"
    assert attrs["gen_ai.request.temperature"] == 0.1
    assert attrs["sentinel.llm.task"] == "rca"
    assert "gen_ai.usage.output_tokens" in attrs
    # No prompt or reply text in spans.
    assert not any("checkout" in str(v) or "Bad deploy" in str(v) for v in attrs.values())


async def test_schema_violation_is_retried_with_feedback() -> None:
    llm = FakeLLM({"rca": ['{"hypotheses": []}', GOOD]})
    result = await llm.generate_structured("rca", Rca, MESSAGES)
    assert result.hypotheses[0].title == "Bad deploy"

    first, second = llm.sink.calls  # type: ignore[attr-defined]
    assert (first.attempt, first.success, first.error_kind) == (1, False, "schema")
    assert "hypotheses" in (first.error or "")
    assert (second.attempt, second.success) == (2, True)
    assert first.request_id == second.request_id

    retry = llm.backend.requests[1]  # type: ignore[attr-defined]
    assert [m["role"] for m in retry.messages] == ["user", "assistant", "user"]
    assert retry.messages[1]["content"] == '{"hypotheses": []}'
    assert "did not match the required JSON schema" in retry.messages[2]["content"]


async def test_gives_up_after_the_retry_budget() -> None:
    llm = FakeLLM({"rca": ["{}", "not json", '{"hypotheses": [{}]}']})
    with pytest.raises(LLMSchemaError) as info:
        await llm.generate_structured("rca", Rca, MESSAGES)
    assert info.value.attempts == 3
    assert [c.error_kind for c in llm.sink.calls] == ["schema"] * 3  # type: ignore[attr-defined]


async def test_retry_budget_can_be_set_per_call() -> None:
    llm = FakeLLM({"rca": ["{}"]})
    with pytest.raises(LLMSchemaError):
        await llm.generate_structured("rca", Rca, MESSAGES, max_retries=0)
    assert len(llm.sink.calls) == 1  # type: ignore[attr-defined]


async def test_runtime_failure_is_logged_and_raised(spans: InMemorySpanExporter) -> None:
    llm = FakeLLM({"rca": [LLMUnavailableError("connection refused")]})
    with pytest.raises(LLMUnavailableError):
        await llm.generate_structured("rca", Rca, MESSAGES)
    (call,) = llm.sink.calls  # type: ignore[attr-defined]
    assert (call.success, call.error_kind) == (False, "unavailable")
    (span,) = spans.get_finished_spans()
    assert span.status.status_code == trace.StatusCode.ERROR
    assert (span.attributes or {})["error.type"] == "LLMUnavailableError"


async def test_thinking_only_reply_is_an_empty_reply() -> None:
    llm = FakeLLM({"report": [""]})
    with pytest.raises(LLMEmptyReplyError):
        await llm.generate_structured("report", Rca, MESSAGES)
    assert llm.sink.calls[0].error_kind == "empty"  # type: ignore[attr-defined]


def test_task_configuration() -> None:
    gateway = LLMGateway(FakeBackend(), settings())
    assert gateway.task_config("rca").think is True
    assert gateway.task_config("report").think is False
    assert gateway.task_config("anything_else").think is False
    assert gateway.task_config("rca").model == "qwen3:14b"

    tuned = LLMGateway(
        FakeBackend(),
        settings(
            llm_model="qwen3:8b",
            llm_task_models={"patch": "qwen2.5-coder:14b"},
            llm_task_thinking={"rca": False},
        ),
    )
    assert tuned.task_config("rca") == tuned.task_config("report")
    assert tuned.task_config("rca").model == "qwen3:8b"
    assert tuned.task_config("patch").model == "qwen2.5-coder:14b"


def test_task_overrides_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTINEL_LLM_TASK_MODELS", '{"report": "qwen3:8b"}')
    monkeypatch.setenv("SENTINEL_LLM_TASK_THINKING", '{"report": true}')
    s = Settings()
    assert s.llm_task_models == {"report": "qwen3:8b"}
    assert s.llm_task_thinking == {"report": True}


def test_keep_alive_is_validated() -> None:
    assert settings(llm_keep_alive="-1").llm_keep_alive == "-1"
    with pytest.raises(ValidationError):
        settings(llm_keep_alive="forever")


async def test_request_carries_the_task_settings() -> None:
    llm = FakeLLM({"report": [GOOD]}, settings(llm_seed=7, llm_num_ctx=8192))
    await llm.generate_structured("report", Rca, MESSAGES, model="qwen3:4b", think=True)
    request = llm.backend.requests[0]  # type: ignore[attr-defined]
    assert request.model == "qwen3:4b"
    assert request.think is True
    assert request.options.seed == 7
    assert request.options.num_ctx == 8192
    assert request.options.temperature == 0.1
    assert request.format == json_schema(Rca)


async def test_payloads_only_when_enabled() -> None:
    llm = FakeLLM({"rca": [GOOD]}, settings(llm_store_payloads=True))
    await llm.generate_structured("rca", Rca, MESSAGES)
    (call,) = llm.sink.calls  # type: ignore[attr-defined]
    assert call.request_payload is not None
    assert call.request_payload["messages"] == MESSAGES
    assert call.response == json.dumps(GOOD)


async def test_concurrent_calls_keep_their_own_records() -> None:
    llm = FakeLLM({"rca": [GOOD] * 5})
    await asyncio.gather(*(llm.generate_structured("rca", Rca, MESSAGES) for _ in range(5)))
    calls = llm.sink.calls  # type: ignore[attr-defined]
    assert len({c.request_id for c in calls}) == 5
    assert all(c.success for c in calls)


async def test_embeddings(spans: InMemorySpanExporter) -> None:
    llm = FakeLLM()
    a, b, a_again = await llm.embed(
        ["disk full on db-1", "KeyError 'currency'", "disk full on db-1"]
    )
    assert len(a) == EMBED_DIM
    assert a == a_again
    assert a != b
    assert sum(v * v for v in a) == pytest.approx(1.0)
    (call,) = llm.sink.calls  # type: ignore[attr-defined]
    assert (call.operation, call.model, call.success) == ("embed", "nomic-embed-text:v1.5", True)
    assert spans.get_finished_spans()[0].name == "embeddings nomic-embed-text:v1.5"


async def test_embedding_failure_is_logged() -> None:
    class Down(FakeBackend):
        async def embed(self, request: EmbedRequest) -> Any:
            raise LLMTimeoutError("slow")

    sink = MemorySink()
    gateway = LLMGateway(Down(), settings(), sink)
    with pytest.raises(LLMTimeoutError):
        await gateway.embed(["x"])
    assert sink.calls[0].error_kind == "timeout"


async def test_fake_llm_complains_when_the_script_runs_out() -> None:
    with pytest.raises(AssertionError, match="no scripted reply"):
        await FakeLLM().generate_structured("rca", Rca, MESSAGES)


async def test_fake_llm_accepts_models_and_functions() -> None:
    llm = FakeLLM({"rca": [Rca.model_validate(GOOD), lambda request: GOOD]})
    assert (await llm.generate_structured("rca", Rca, MESSAGES)).hypotheses
    assert (await llm.generate_structured("rca", Rca, MESSAGES)).hypotheses


# -------------------------------------------------------------------------- replay


async def test_record_then_replay(tmp_path: Path) -> None:
    cassette = tmp_path / "rca.jsonl"
    inner = FakeBackend({"rca": [GOOD]})
    recorder = LLMGateway(ReplayBackend(cassette, mode="record", inner=inner), settings())
    recorded = await recorder.generate_structured("rca", Rca, MESSAGES)

    replay = ReplayLLM(cassette)
    assert await replay.generate_structured("rca", Rca, MESSAGES) == recorded
    assert len(cassette.read_text().splitlines()) == 1

    # The same request again is served from the cassette, not the inner backend.
    assert await recorder.generate_structured("rca", Rca, MESSAGES) == recorded


async def test_replay_miss_when_the_prompt_changes(tmp_path: Path) -> None:
    cassette = tmp_path / "rca.jsonl"
    inner = FakeBackend({"rca": [GOOD]})
    await LLMGateway(
        ReplayBackend(cassette, mode="record", inner=inner), settings()
    ).generate_structured("rca", Rca, MESSAGES)
    replay = ReplayLLM(cassette)
    with pytest.raises(ReplayMissError):
        await replay.generate_structured("rca", Rca, [{"role": "user", "content": "changed"}])
    with pytest.raises(ReplayMissError):
        await replay.generate_structured("rca", Rca, MESSAGES, think=False)


def test_record_mode_needs_a_real_backend(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="record mode"):
        ReplayBackend(tmp_path / "x.jsonl", mode="record")


async def test_replay_embeddings_are_deterministic(tmp_path: Path) -> None:
    replay = ReplayLLM(tmp_path / "none.jsonl")
    assert await replay.embed(["a"]) == await replay.embed(["a"])
