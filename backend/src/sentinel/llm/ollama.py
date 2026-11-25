"""Ollama HTTP client: ``/api/chat`` (streamed, for time to first token) and ``/api/embed``.

Timeouts:

- connect: the runtime must accept the connection quickly;
- stall: the longest wait for the next streamed line (this covers loading a
  model from disk before the first token);
- total: one whole request, however steadily it streams.

A request that could not connect, got a 5xx, or timed out is retried
``transport_retries`` times with a short backoff. A 4xx (unknown model, bad
request) is not retried.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from sentinel.llm.errors import LLMError, LLMHTTPError, LLMTimeoutError, LLMUnavailableError
from sentinel.llm.types import ChatRequest, ChatResult, EmbedRequest, EmbedResult

_NS_PER_MS = 1_000_000
_MAX_ERROR_CHARS = 500


def _ms(ns: object) -> int | None:
    return int(ns) // _NS_PER_MS if isinstance(ns, int) else None


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        *,
        connect_timeout_s: float = 5.0,
        stall_timeout_s: float = 180.0,
        total_timeout_s: float = 900.0,
        transport_retries: int = 1,
        backoff_s: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.total_timeout_s = total_timeout_s
        self.transport_retries = transport_retries
        self.backoff_s = backoff_s
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(
                connect=connect_timeout_s, read=stall_timeout_s, write=30.0, pool=30.0
            ),
            transport=transport,
            trust_env=False,  # never through a proxy: the runtime is local
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ chat
    async def chat(self, request: ChatRequest) -> ChatResult:
        return await self._with_retries(lambda: self._chat_once(request))

    async def _chat_once(self, request: ChatRequest) -> ChatResult:
        started = time.perf_counter()
        content: list[str] = []
        thinking: list[str] = []
        ttft_ms: int | None = None
        first_content_ms: int | None = None
        final: dict[str, Any] = {}
        async with self._post_stream("/api/chat", request.body(stream=True)) as lines:
            async for line in lines:
                try:
                    chunk = json.loads(line)
                except ValueError as exc:
                    raise LLMUnavailableError("the runtime sent a malformed stream line") from exc
                if "error" in chunk:
                    raise LLMUnavailableError(
                        f"runtime error: {str(chunk['error'])[:_MAX_ERROR_CHARS]}"
                    )
                message = chunk.get("message") or {}
                piece = message.get("content") or ""
                thought = message.get("thinking") or ""
                now_ms = int((time.perf_counter() - started) * 1000)
                if (piece or thought) and ttft_ms is None:
                    ttft_ms = now_ms
                if piece and first_content_ms is None:
                    first_content_ms = now_ms
                content.append(piece)
                thinking.append(thought)
                if chunk.get("done"):
                    final = chunk
                    break
        if not final:
            raise LLMUnavailableError("the stream ended before the reply was done")
        return ChatResult(
            model=str(final.get("model") or request.model),
            content="".join(content),
            thinking="".join(thinking),
            input_tokens=final.get("prompt_eval_count"),
            output_tokens=final.get("eval_count"),
            latency_ms=int((time.perf_counter() - started) * 1000),
            ttft_ms=ttft_ms,
            first_content_ms=first_content_ms,
            load_ms=_ms(final.get("load_duration")),
            prompt_eval_ms=_ms(final.get("prompt_eval_duration")),
            eval_ms=_ms(final.get("eval_duration")),
            done_reason=final.get("done_reason"),
        )

    # ----------------------------------------------------------------- embed
    async def embed(self, request: EmbedRequest) -> EmbedResult:
        return await self._with_retries(lambda: self._embed_once(request))

    async def _embed_once(self, request: EmbedRequest) -> EmbedResult:
        body: dict[str, Any] = {"model": request.model, "input": list(request.inputs)}
        if request.keep_alive is not None:
            body["keep_alive"] = request.keep_alive
        started = time.perf_counter()
        data = await self._post_json("/api/embed", body)
        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(request.inputs):
            raise LLMUnavailableError("the runtime returned no embeddings")
        return EmbedResult(
            model=str(data.get("model") or request.model),
            embeddings=embeddings,
            input_tokens=data.get("prompt_eval_count"),
            latency_ms=int((time.perf_counter() - started) * 1000),
            load_ms=_ms(data.get("load_duration")),
        )

    # ------------------------------------------------------------ inspection
    async def version(self) -> str:
        return str((await self._get_json("/api/version")).get("version", ""))

    async def loaded(self) -> list[dict[str, Any]]:
        """Models in memory now, with ``size`` and ``size_vram`` in bytes (``/api/ps``)."""
        models = (await self._get_json("/api/ps")).get("models") or []
        return [m for m in models if isinstance(m, dict)]

    async def show(self, model: str) -> dict[str, Any]:
        return await self._post_json("/api/show", {"model": model})

    async def unload(self, model: str) -> None:
        """Free a chat model's memory now (``keep_alive: 0``)."""
        await self._post_json("/api/generate", {"model": model, "keep_alive": 0})

    # ------------------------------------------------------------- transport
    async def _with_retries[R](self, attempt: Any) -> R:
        tries = self.transport_retries + 1
        for n in range(tries):
            try:
                async with asyncio.timeout(self.total_timeout_s):
                    result: R = await attempt()
                    return result
            except TimeoutError as exc:
                error: LLMError = LLMTimeoutError(
                    f"no complete reply in {self.total_timeout_s:g} s"
                )
                error.__cause__ = exc
            except LLMError as exc:
                error = exc
            if not error.retryable or n == tries - 1:
                raise error
            await asyncio.sleep(self.backoff_s * (2**n))
        raise AssertionError("unreachable")  # pragma: no cover

    @asynccontextmanager
    async def _post_stream(self, path: str, body: dict[str, Any]) -> AsyncIterator[Any]:
        try:
            async with self._client.stream("POST", path, json=body) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise _status_error(response)
                yield (line async for line in response.aiter_lines() if line.strip())
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"{type(exc).__name__} from {path}") from exc
        except httpx.TransportError as exc:
            raise LLMUnavailableError(f"{type(exc).__name__} from {path}") from exc

    async def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", path, body)

    async def _get_json(self, path: str) -> dict[str, Any]:
        return await self._request("GET", path, None)

    async def _request(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, json=body)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"{type(exc).__name__} from {path}") from exc
        except httpx.TransportError as exc:
            raise LLMUnavailableError(f"{type(exc).__name__} from {path}") from exc
        if response.status_code >= 400:
            raise _status_error(response)
        data = response.json()
        return data if isinstance(data, dict) else {}


def _status_error(response: httpx.Response) -> LLMError:
    try:
        message = str(response.json().get("error", ""))
    except ValueError:
        message = response.text
    message = message[:_MAX_ERROR_CHARS]
    if response.status_code >= 500:
        return LLMUnavailableError(f"HTTP {response.status_code}: {message}")
    return LLMHTTPError(response.status_code, message)
