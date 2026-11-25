"""Errors raised by the LLM gateway. ``kind`` is what ``llm_calls.error_kind`` stores."""


class LLMError(Exception):
    kind = "http"
    retryable = False


class LLMUnavailableError(LLMError):
    """The runtime could not be reached (connection refused, reset, 5xx)."""

    kind = "unavailable"
    retryable = True


class LLMTimeoutError(LLMError):
    """No token for the stall timeout, or the whole request took too long."""

    kind = "timeout"
    retryable = True


class LLMHTTPError(LLMError):
    """The runtime refused the request (unknown model, bad request): not retried."""

    kind = "http"

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class LLMEmptyReplyError(LLMError):
    kind = "empty"


class LLMSchemaError(LLMError):
    """Every attempt's reply failed validation against the schema."""

    kind = "schema"

    def __init__(self, task: str, attempts: int, last_error: str) -> None:
        super().__init__(f"{task}: no valid reply in {attempts} attempt(s): {last_error}")
        self.task = task
        self.attempts = attempts
        self.last_error = last_error
