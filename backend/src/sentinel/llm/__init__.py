"""LLM gateway: the only way SENTINEL talks to a model (local Ollama; ADR-0002)."""

from sentinel.config import Settings
from sentinel.llm.errors import (
    LLMEmptyReplyError,
    LLMError,
    LLMHTTPError,
    LLMSchemaError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from sentinel.llm.gateway import LLMGateway, TaskConfig
from sentinel.llm.ollama import OllamaClient
from sentinel.llm.recorder import CallRecord, CallSink, DbCallSink, MemorySink, prune_payloads
from sentinel.llm.types import Message


def ollama_client(settings: Settings) -> OllamaClient:
    return OllamaClient(
        settings.ollama_url,
        connect_timeout_s=settings.llm_connect_timeout_s,
        stall_timeout_s=settings.llm_stall_timeout_s,
        total_timeout_s=settings.llm_timeout_s,
        transport_retries=settings.llm_transport_retries,
    )


__all__ = [
    "CallRecord",
    "CallSink",
    "DbCallSink",
    "LLMEmptyReplyError",
    "LLMError",
    "LLMGateway",
    "LLMHTTPError",
    "LLMSchemaError",
    "LLMTimeoutError",
    "LLMUnavailableError",
    "MemorySink",
    "Message",
    "OllamaClient",
    "TaskConfig",
    "ollama_client",
    "prune_payloads",
]
