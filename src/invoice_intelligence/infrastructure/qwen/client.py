"""Shared Qwen OpenAI-compatible client with bounded remote-call reliability."""

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, TypeVar
from urllib.parse import urlsplit, urlunsplit

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)
from pydantic import BaseModel

from invoice_intelligence.application.ports.governance import (
    RemoteCallObservation,
    RetrievalTelemetryContext,
)

T = TypeVar("T")

_LOGGER = logging.getLogger(__name__)
_MAX_ERROR_MESSAGE_LENGTH = 1024
_SENSITIVE_ERROR_TEXT = re.compile(
    r"(?i)(?:api[-_ ]?key|authorization|bearer|token|password|secret)\s*[:=]\s*[^\s,;]+"
)
_INLINE_DATA = re.compile(r"(?i)data:(?:image|application)/[^,\s]+,[^\s]+")


class QwenRemoteError(Exception):
    """Base failure that never contains a remote Prompt or payload."""


class QwenRemoteAccessError(QwenRemoteError):
    """Non-retryable authentication or authorization failure."""


class QwenRemoteRequestError(QwenRemoteError):
    """Non-retryable request or Schema failure."""


class QwenRemoteUnavailableError(QwenRemoteError):
    """Retryable remote failure exhausted the configured budget."""


class _RateLimiter:
    def __init__(self, requests_per_minute: int) -> None:
        self._capacity = float(requests_per_minute)
        self._tokens = float(requests_per_minute)
        self._refill_per_second = requests_per_minute / 60.0
        self._updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = max(0.0, now - self._updated_at)
                self._tokens = min(
                    self._capacity,
                    self._tokens + elapsed * self._refill_per_second,
                )
                self._updated_at = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait_seconds = (1.0 - self._tokens) / self._refill_per_second
            await asyncio.sleep(wait_seconds)


class _CircuitBreaker:
    def __init__(self, failure_threshold: int, recovery_seconds: float) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_seconds = recovery_seconds
        self._failure_count = 0
        self._open_until = 0.0
        self._lock = asyncio.Lock()

    async def before_request(self) -> None:
        async with self._lock:
            if time.monotonic() < self._open_until:
                raise QwenRemoteUnavailableError("Qwen remote circuit is open")
            if self._open_until:
                self._failure_count = 0
                self._open_until = 0.0

    async def record_success(self) -> None:
        async with self._lock:
            self._failure_count = 0
            self._open_until = 0.0

    async def record_transient_failure(self) -> None:
        async with self._lock:
            self._failure_count += 1
            if self._failure_count >= self._failure_threshold:
                self._open_until = time.monotonic() + self._recovery_seconds


class QwenRemoteClient:
    """Invoke officially documented compatible endpoints without leaking SDK types."""

    def __init__(
        self,
        *,
        api_key: str,
        compatible_base_url: str | None,
        rerank_base_url: str | None,
        timeout_seconds: float,
        max_retries: int,
        max_concurrency: int,
        requests_per_minute: int,
        backoff_base_seconds: float,
        backoff_max_seconds: float,
        circuit_failure_threshold: int,
        circuit_recovery_seconds: float,
        audit_enabled: bool,
        telemetry: RetrievalTelemetryContext | None = None,
        model_costs_per_million: Mapping[str, tuple[float, float]] | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Qwen API key must not be empty")
        if max_retries < 0:
            raise ValueError("Qwen max_retries must not be negative")
        if max_concurrency <= 0 or requests_per_minute <= 0:
            raise ValueError("Qwen concurrency and rate limit must be greater than zero")
        if backoff_base_seconds <= 0 or backoff_max_seconds < backoff_base_seconds:
            raise ValueError("Qwen retry backoff configuration is invalid")
        self._compatible = (
            AsyncOpenAI(
                api_key=api_key,
                base_url=compatible_base_url,
                timeout=timeout_seconds,
                max_retries=0,
            )
            if compatible_base_url is not None
            else None
        )
        self._rerank = (
            AsyncOpenAI(
                api_key=api_key,
                base_url=rerank_base_url,
                timeout=timeout_seconds,
                max_retries=0,
            )
            if rerank_base_url is not None
            else None
        )
        self._rerank_base_url = rerank_base_url
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._rate_limiter = _RateLimiter(requests_per_minute)
        self._circuit = _CircuitBreaker(
            circuit_failure_threshold,
            circuit_recovery_seconds,
        )
        self._audit_enabled = audit_enabled
        self._telemetry = telemetry
        self._model_costs = dict(model_costs_per_million or {})
        if any(
            input_rate < 0 or output_rate < 0
            for input_rate, output_rate in self._model_costs.values()
        ):
            raise ValueError("Qwen model cost rates must not be negative")

    async def aclose(self) -> None:
        """Close provider clients owned by this composition root."""

        clients = tuple(
            client
            for client in (self._compatible, self._rerank)
            if client is not None
        )
        for client in clients:
            await client.close()

    async def parse_chat(
        self,
        *,
        operation: str,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        response_format: type[BaseModel],
    ) -> Any:
        """Call Chat Completions JSON Schema mode with thinking disabled."""

        client = self._compatible
        if client is None:
            raise QwenRemoteRequestError("Qwen compatible endpoint is not configured")
        return await self._execute(
            operation,
            model,
            lambda: client.chat.completions.parse(
                model=model,
                messages=list(messages),
                response_format=response_format,
                extra_body={"enable_thinking": False},
            ),
        )

    async def create_chat(
        self,
        *,
        operation: str,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        response_format: Mapping[str, Any],
        trace_id: str | None = None,
    ) -> Any:
        """Call Chat Completions with a provider-compatible JSON Schema envelope."""

        client = self._compatible
        if client is None:
            raise QwenRemoteRequestError("Qwen compatible endpoint is not configured")
        return await self._execute(
            operation,
            model,
            lambda: client.chat.completions.create(
                model=model,
                messages=list(messages),
                response_format=dict(response_format),
                extra_body={"enable_thinking": False},
            ),
            trace_id=trace_id,
        )

    async def create_embeddings(
        self,
        *,
        model: str,
        texts: Sequence[str],
        dimensions: int,
    ) -> Any:
        """Call the documented OpenAI-compatible Embeddings endpoint."""

        client = self._compatible
        if client is None:
            raise QwenRemoteRequestError("Qwen compatible endpoint is not configured")
        return await self._execute(
            "dense_embedding",
            model,
            lambda: client.embeddings.create(
                model=model,
                input=list(texts),
                dimensions=dimensions,
            ),
        )

    async def rerank(self, *, model: str, body: Mapping[str, Any]) -> object:
        """Call the model-specific documented rerank endpoint."""

        client = self._rerank
        if client is None:
            raise QwenRemoteRequestError("Qwen rerank endpoint is not configured")
        path = "/reranks"
        if model == "qwen3.7-text-rerank":
            parsed = urlsplit(self._rerank_base_url or "")
            path = urlunsplit(
                (
                    parsed.scheme,
                    parsed.netloc,
                    "/api/v1/services/rerank/text-rerank/text-rerank",
                    "",
                    "",
                )
            )
        return await self._execute(
            "rerank",
            model,
            lambda: client.post(path, body=dict(body), cast_to=object),
        )

    async def _execute(
        self,
        operation: str,
        model: str,
        request: Callable[[], Awaitable[T]],
        *,
        trace_id: str | None = None,
    ) -> T:
        for attempt in range(self._max_retries + 1):
            circuit_started_at = time.monotonic()
            try:
                await self._circuit.before_request()
            except QwenRemoteUnavailableError as exc:
                self._audit(
                    operation,
                    model,
                    circuit_started_at,
                    "circuit_open",
                    exc,
                    trace_id=trace_id,
                )
                raise
            await self._rate_limiter.acquire()
            started_at = time.monotonic()
            try:
                async with self._semaphore:
                    response = await request()
            except (AuthenticationError, PermissionDeniedError) as exc:
                self._audit(
                    operation,
                    model,
                    started_at,
                    "access_error",
                    exc,
                    trace_id=trace_id,
                )
                raise QwenRemoteAccessError("Qwen access was denied") from exc
            except (BadRequestError, NotFoundError, UnprocessableEntityError) as exc:
                self._audit(
                    operation,
                    model,
                    started_at,
                    "request_error",
                    exc,
                    trace_id=trace_id,
                )
                raise QwenRemoteRequestError("Qwen rejected the request") from exc
            except Exception as exc:
                if not self._is_transient(exc):
                    self._audit(
                        operation,
                        model,
                        started_at,
                        "request_error",
                        exc,
                        trace_id=trace_id,
                    )
                    raise QwenRemoteRequestError("Qwen request failed") from exc
                await self._circuit.record_transient_failure()
                self._audit(
                    operation,
                    model,
                    started_at,
                    "transient_error",
                    exc,
                    trace_id=trace_id,
                )
                if attempt >= self._max_retries:
                    raise QwenRemoteUnavailableError(
                        "Qwen temporary failure exhausted retry budget"
                    ) from exc
                delay = min(
                    self._backoff_max_seconds,
                    self._backoff_base_seconds * (2**attempt),
                )
                await asyncio.sleep(delay)
                continue
            await self._circuit.record_success()
            self._audit(
                operation,
                model,
                started_at,
                "success",
                response=response,
                trace_id=trace_id,
            )
            return response
        raise QwenRemoteUnavailableError("Qwen request exhausted retry budget")

    @staticmethod
    def _is_transient(error: Exception) -> bool:
        if isinstance(
            error,
            (APIConnectionError, APITimeoutError, InternalServerError, RateLimitError),
        ):
            return True
        if isinstance(error, APIStatusError):
            return error.status_code in {408, 409, 429} or error.status_code >= 500
        return False

    def _audit(
        self,
        operation: str,
        model: str,
        started_at: float,
        outcome: str,
        error: Exception | None = None,
        response: object | None = None,
        *,
        trace_id: str | None = None,
    ) -> None:
        latency_ms = round((time.monotonic() - started_at) * 1000, 2)
        input_tokens, output_tokens = self._usage(response)
        estimated_cost = self._estimate_cost(model, input_tokens, output_tokens)
        if self._telemetry is not None:
            self._telemetry.record_remote_call(
                RemoteCallObservation(
                    operation=operation,
                    model=model,
                    outcome=outcome,
                    latency_ms=latency_ms,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    estimated_cost=estimated_cost,
                )
            )
        if not self._audit_enabled:
            return
        error_details = self._error_details(error)
        _LOGGER.info(
            "qwen_remote_request",
            extra={
                "provider": "qwen",
                "trace_id": trace_id,
                "operation": operation,
                "model": model,
                "outcome": outcome,
                "latency_ms": latency_ms,
                "request_id": self._request_id(response),
                "error_type": type(error).__name__ if error is not None else None,
                **error_details,
            },
        )

    @staticmethod
    def _error_details(error: Exception | None) -> dict[str, object]:
        """Extract bounded provider diagnostics without logging request payloads."""

        if error is None:
            return {}
        status_code = getattr(error, "status_code", None)
        body = getattr(error, "body", None)
        if isinstance(body, str):
            try:
                decoded = json.loads(body)
            except (TypeError, ValueError):
                decoded = None
            if isinstance(decoded, Mapping):
                body = decoded
        if isinstance(body, Mapping) and isinstance(body.get("error"), Mapping):
            body = body["error"]
        if isinstance(body, Mapping):
            code = QwenRemoteClient._safe_error_value(body.get("code"))
            message = QwenRemoteClient._safe_error_value(body.get("message"))
            error_type = QwenRemoteClient._safe_error_value(body.get("type"))
            param = QwenRemoteClient._safe_error_value(body.get("param"))
        else:
            code = None
            message = QwenRemoteClient._safe_error_value(body)
            error_type = None
            param = None
        if message is None:
            message = QwenRemoteClient._safe_error_value(getattr(error, "message", None))
        details: dict[str, object] = {}
        if isinstance(status_code, int):
            details["remote_status_code"] = status_code
        for name, value in (
            ("remote_error_code", code),
            ("remote_error_message", message),
            ("remote_error_type", error_type),
            ("remote_error_param", param),
        ):
            if value is not None:
                details[name] = value
        response = getattr(error, "response", None)
        headers = getattr(response, "headers", None)
        if headers is not None:
            request_id = headers.get("x-request-id") or headers.get("request-id")
            if request_id:
                details["remote_request_id"] = QwenRemoteClient._safe_error_value(
                    request_id
                )
        return details

    @staticmethod
    def _safe_error_value(value: object) -> str | None:
        """Keep only a short, redacted scalar from an upstream error envelope."""

        if value is None or isinstance(value, (Mapping, list, tuple, set)):
            return None
        text = str(value).replace("\r", " ").replace("\n", " ").strip()
        if not text:
            return None
        text = _INLINE_DATA.sub("[inline-data-redacted]", text)
        text = _SENSITIVE_ERROR_TEXT.sub("[sensitive]=[redacted]", text)
        return text[:_MAX_ERROR_MESSAGE_LENGTH]

    def _estimate_cost(
        self,
        model: str,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> float | None:
        rates = self._model_costs.get(model)
        if rates is None or (input_tokens is None and output_tokens is None):
            return None
        input_rate, output_rate = rates
        return (
            (input_tokens or 0) * input_rate
            + (output_tokens or 0) * output_rate
        ) / 1_000_000

    @staticmethod
    def _usage(response: object | None) -> tuple[int | None, int | None]:
        if response is None:
            return None, None
        if isinstance(response, Mapping):
            usage = response.get("usage")
        else:
            usage = getattr(response, "usage", None)
        if usage is None:
            return None, None

        def value(*names: str) -> int | None:
            for name in names:
                if isinstance(usage, Mapping):
                    candidate = usage.get(name)
                else:
                    candidate = getattr(usage, name, None)
                if isinstance(candidate, int) and candidate >= 0:
                    return candidate
            return None

        return value("input_tokens", "prompt_tokens"), value(
            "output_tokens",
            "completion_tokens",
        )

    @staticmethod
    def _request_id(response: object | None) -> str | None:
        if response is None:
            return None
        if isinstance(response, Mapping):
            value = response.get("id") or response.get("request_id")
        else:
            value = (
                getattr(response, "_request_id", None)
                or getattr(response, "id", None)
                or getattr(response, "request_id", None)
            )
        return str(value) if value else None
