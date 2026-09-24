"""PaddleX OCR serving adapter with bounded reliability and redacted diagnostics."""

from __future__ import annotations

import asyncio
import base64
import logging
import math
import time
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from invoice_intelligence.application.ports.observability import (
    OCRPageMetric,
    OCRProviderCallMetric,
    OCRTelemetry,
)
from invoice_intelligence.application.ports.validation import RawOCRProvider
from invoice_intelligence.domain.document import VisionImage
from invoice_intelligence.domain.extraction import (
    BoundingBox,
    OCRProviderStatus,
    RawOCRObservation,
    RawOCRResult,
)

_LOGGER = logging.getLogger(__name__)
_TRANSIENT_HTTP_STATUSES = frozenset({500, 502, 503, 504})


class _OCRResultPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prunedResult: dict[str, Any]


class _OCRInferPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ocrResults: list[_OCRResultPayload]


class _OCRResponsePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    logId: str = Field(min_length=1, max_length=128)
    errorCode: int
    errorMsg: str
    result: _OCRInferPayload | None = None


class _PrunedOCRPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rec_texts: list[str]
    rec_scores: list[float]
    rec_boxes: list[list[float]]

    @field_validator("rec_scores")
    @classmethod
    def _validate_scores(cls, values: list[float]) -> list[float]:
        if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in values):
            raise ValueError("rec_scores must be finite values from zero to one")
        return values

    @field_validator("rec_boxes")
    @classmethod
    def _validate_boxes(cls, values: list[list[float]]) -> list[list[float]]:
        for box in values:
            if len(box) != 4 or any(not math.isfinite(value) for value in box):
                raise ValueError("rec_boxes must contain four finite coordinates")
            left, top, right, bottom = box
            if left < 0 or top < 0 or right < left or bottom < top:
                raise ValueError("rec_boxes coordinates must be ordered and non-negative")
        return values


@dataclass(frozen=True, slots=True)
class _PageFailure:
    code: str
    status_code: int | None = None


@dataclass(frozen=True, slots=True)
class _PageResult:
    page_number: int
    latency_ms: float
    observations: tuple[RawOCRObservation, ...]
    failure: _PageFailure | None


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

    async def before_request(self) -> bool:
        async with self._lock:
            now = time.monotonic()
            if now < self._open_until:
                return False
            if self._open_until:
                self._failure_count = 0
                self._open_until = 0.0
            return True

    async def record_success(self) -> None:
        async with self._lock:
            self._failure_count = 0
            self._open_until = 0.0

    async def record_transient_failure(self) -> None:
        async with self._lock:
            self._failure_count += 1
            if self._failure_count >= self._failure_threshold:
                self._open_until = time.monotonic() + self._recovery_seconds


class PaddleXOCRHttpAdapter(RawOCRProvider):
    """Call the PaddleX ``/ocr`` endpoint and return raw, unbound OCR lines."""

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8188",
        endpoint_path: str = "/ocr",
        provider_name: str = "paddlex_ocr_http",
        provider_version: str = "paddlex-3.7.2",
        model_version: str = "PP-OCRv6_small_det+PP-OCRv6_small_rec",
        config_version: str = "ppocrv6-small-v1",
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 30.0,
        write_timeout_seconds: float = 30.0,
        pool_timeout_seconds: float = 5.0,
        max_retries: int = 2,
        max_concurrency: int = 4,
        requests_per_minute: int = 60,
        backoff_base_seconds: float = 0.5,
        backoff_max_seconds: float = 8.0,
        circuit_failure_threshold: int = 5,
        circuit_recovery_seconds: float = 30.0,
        max_input_bytes: int = 26_214_400,
        telemetry: OCRTelemetry | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("OCR base_url must be an absolute HTTP(S) URL")
        if not endpoint_path.startswith("/"):
            raise ValueError("OCR endpoint_path must start with '/'")
        for name, value in (
            ("provider_name", provider_name),
            ("provider_version", provider_version),
            ("model_version", model_version),
            ("config_version", config_version),
        ):
            if not value.strip() or value != value.strip():
                raise ValueError(f"{name} must be non-empty and normalized")
        if any(value <= 0 for value in (
            connect_timeout_seconds,
            read_timeout_seconds,
            write_timeout_seconds,
            pool_timeout_seconds,
            backoff_base_seconds,
            backoff_max_seconds,
            circuit_recovery_seconds,
        )) or backoff_max_seconds < backoff_base_seconds:
            raise ValueError("OCR timeout and backoff values are invalid")
        if max_retries < 0 or max_concurrency <= 0 or requests_per_minute <= 0:
            raise ValueError("OCR retry, concurrency and rate-limit values are invalid")
        if circuit_failure_threshold <= 0 or max_input_bytes <= 0:
            raise ValueError("OCR circuit threshold and max_input_bytes must be positive")

        self._url = f"{base_url.rstrip('/')}{endpoint_path}"
        self._provider_name = provider_name
        self._provider_version = provider_version
        self._model_version = model_version
        self._config_version = config_version
        self._timeouts = httpx.Timeout(
            connect=connect_timeout_seconds,
            read=read_timeout_seconds,
            write=write_timeout_seconds,
            pool=pool_timeout_seconds,
        )
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._max_input_bytes = max_input_bytes
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._rate_limiter = _RateLimiter(requests_per_minute)
        self._circuit = _CircuitBreaker(circuit_failure_threshold, circuit_recovery_seconds)
        self._client = httpx.AsyncClient(timeout=self._timeouts)
        self._telemetry = telemetry

    async def aclose(self) -> None:
        await self._client.aclose()

    async def observe_raw(self, images: Sequence[VisionImage]) -> RawOCRResult:
        """Observe each normalized page, degrading to an explicit unavailable result."""

        trace_id = str(uuid4())
        page_count = len(images)
        started_at = time.monotonic()
        if not images:
            elapsed_ms = round((time.monotonic() - started_at) * 1000, 2)
            self._record_telemetry(
                trace_id=trace_id,
                page_count=0,
                latency_ms=elapsed_ms,
                page_results=(),
                error_type="ocr_empty_input",
            )
            return RawOCRResult(
                status=OCRProviderStatus.UNAVAILABLE,
                anomalies=("ocr_empty_input",),
                trace_id=trace_id,
            )

        tasks = [self._observe_page(image, trace_id) for image in images]
        try:
            results = await asyncio.gather(*tasks)
        except Exception as exc:
            elapsed_ms = round((time.monotonic() - started_at) * 1000, 2)
            error_type = f"ocr_{type(exc).__name__.lower()}"
            self._log(
                trace_id=trace_id,
                page_count=page_count,
                latency_ms=elapsed_ms,
                status_code=None,
                error_type=error_type,
                candidate_count=0,
            )
            self._record_telemetry(
                trace_id=trace_id,
                page_count=page_count,
                latency_ms=elapsed_ms,
                page_results=(),
                error_type=error_type,
            )
            return RawOCRResult(
                status=OCRProviderStatus.UNAVAILABLE,
                anomalies=(error_type,),
                trace_id=trace_id,
            )
        observations: list[RawOCRObservation] = []
        failures: list[_PageFailure] = []
        for page_result in results:
            observations.extend(page_result.observations)
            if page_result.failure is not None:
                failures.append(page_result.failure)
        elapsed_ms = round((time.monotonic() - started_at) * 1000, 2)
        if failures:
            anomalies = tuple(dict.fromkeys(item.code for item in failures))
            self._log(
                trace_id=trace_id,
                page_count=page_count,
                latency_ms=elapsed_ms,
                status_code=next(
                    (item.status_code for item in failures if item.status_code is not None),
                    None,
                ),
                error_type=anomalies[0],
                candidate_count=len(observations),
            )
            self._record_telemetry(
                trace_id=trace_id,
                page_count=page_count,
                latency_ms=elapsed_ms,
                page_results=tuple(results),
                error_type=anomalies[0],
            )
            return RawOCRResult(
                status=OCRProviderStatus.UNAVAILABLE,
                observations=tuple(observations),
                anomalies=anomalies,
                trace_id=trace_id,
            )
        self._log(
            trace_id=trace_id,
            page_count=page_count,
            latency_ms=elapsed_ms,
            status_code=200,
            error_type=None,
            candidate_count=len(observations),
        )
        self._record_telemetry(
            trace_id=trace_id,
            page_count=page_count,
            latency_ms=elapsed_ms,
            page_results=tuple(results),
            error_type=None,
        )
        return RawOCRResult(
            status=OCRProviderStatus.AVAILABLE,
            observations=tuple(observations),
            trace_id=trace_id,
        )

    async def _observe_page(
        self,
        image: VisionImage,
        trace_id: str,
    ) -> _PageResult:
        started_at = time.monotonic()
        observations, failure = await self._observe_page_with_retries(image, trace_id)
        return _PageResult(
            page_number=image.page_number,
            latency_ms=round((time.monotonic() - started_at) * 1000, 2),
            observations=observations,
            failure=failure,
        )

    async def _observe_page_with_retries(
        self,
        image: VisionImage,
        trace_id: str,
    ) -> tuple[tuple[RawOCRObservation, ...], _PageFailure | None]:
        if len(image.content) > self._max_input_bytes:
            return (), _PageFailure("ocr_input_too_large")
        for attempt in range(self._max_retries + 1):
            if not await self._circuit.before_request():
                return (), _PageFailure("ocr_circuit_open")
            await self._rate_limiter.acquire()
            try:
                async with self._semaphore:
                    response = await self._post_page(image.content)
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                await self._circuit.record_transient_failure()
                if attempt >= self._max_retries:
                    return (), _PageFailure(f"ocr_{type(exc).__name__.lower()}")
                await self._backoff(attempt)
                continue
            except httpx.RequestError as exc:
                return (), _PageFailure(f"ocr_{type(exc).__name__.lower()}")

            status_code = response.status_code
            try:
                response_payload = _OCRResponsePayload.model_validate(response.json())
            except (ValueError, TypeError, ValidationError):
                response_payload = None
            if status_code in _TRANSIENT_HTTP_STATUSES or status_code == 429:
                await self._circuit.record_transient_failure()
                if attempt >= self._max_retries:
                    return (), _PageFailure(
                        f"ocr_http_{status_code}",
                        status_code=status_code,
                    )
                await self._backoff(attempt)
                continue
            if status_code < 200 or status_code >= 300:
                return (), _PageFailure(f"ocr_http_{status_code}", status_code=status_code)
            if response_payload is None:
                return (), _PageFailure("ocr_response_schema_error", status_code=status_code)
            try:
                observations = self._parse_response(
                    response_payload,
                    image.page_number,
                    trace_id,
                )
            except _ResponseSchemaError as exc:
                return (), _PageFailure(str(exc), status_code=status_code)
            except ValidationError:
                return (), _PageFailure("ocr_response_schema_error", status_code=status_code)
            except (ValueError, TypeError) as exc:
                return (), _PageFailure(
                    f"ocr_response_{type(exc).__name__.lower()}",
                    status_code=status_code,
                )
            await self._circuit.record_success()
            return observations, None
        return (), _PageFailure("ocr_retry_exhausted")

    async def _post_page(self, content: bytes) -> httpx.Response:
        encoded = base64.b64encode(content).decode("ascii")
        try:
            return await self._client.post(
                self._url,
                json={
                    "file": encoded,
                    "fileType": 1,
                    "useDocOrientationClassify": False,
                    "useDocUnwarping": False,
                    "useTextlineOrientation": False,
                    "textDetLimitSideLen": 640,
                    "textDetLimitType": "max",
                    "visualize": False,
                },
            )
        finally:
            encoded = ""

    async def _backoff(self, attempt: int) -> None:
        await asyncio.sleep(
            min(self._backoff_max_seconds, self._backoff_base_seconds * (2**attempt))
        )

    def _parse_response(
        self,
        response: _OCRResponsePayload,
        page_number: int,
        trace_id: str,
    ) -> tuple[RawOCRObservation, ...]:
        if response.errorCode != 0 or response.result is None:
            raise _ResponseSchemaError("ocr_provider_error_envelope")
        if len(response.result.ocrResults) != 1:
            raise _ResponseSchemaError("ocr_page_result_count_mismatch")
        parsed = _PrunedOCRPayload.model_validate(response.result.ocrResults[0].prunedResult)
        if not (
            len(parsed.rec_texts)
            == len(parsed.rec_scores)
            == len(parsed.rec_boxes)
        ):
            raise _ResponseSchemaError("ocr_array_length_mismatch")
        source_id = f"paddlex:{trace_id}:page:{page_number}"
        source_reference = f"/ocr#{response.logId}:page:{page_number}"
        return tuple(
            RawOCRObservation(
                source_id=source_id,
                provider_name=self._provider_name,
                provider_version=self._provider_version,
                model_version=self._model_version,
                page_number=page_number,
                observed_text=text,
                normalized_text=_normalize_text(text),
                bounding_box=_to_bounding_box(box),
                provider_score=score,
                source_reference=source_reference,
            )
            for text, score, box in zip(
                parsed.rec_texts,
                parsed.rec_scores,
                parsed.rec_boxes,
                strict=True,
            )
        )

    @staticmethod
    def _log(
        *,
        trace_id: str,
        page_count: int,
        latency_ms: float,
        status_code: int | None,
        error_type: str | None,
        candidate_count: int,
    ) -> None:
        _LOGGER.info(
            "paddlex_ocr_request",
            extra={
                "trace_id": trace_id,
                "page_count": page_count,
                "latency_ms": latency_ms,
                "status_code": status_code,
                "error_type": error_type,
                "candidate_count": candidate_count,
            },
        )

    def _record_telemetry(
        self,
        *,
        trace_id: str,
        page_count: int,
        latency_ms: float,
        page_results: tuple[_PageResult, ...],
        error_type: str | None,
    ) -> None:
        telemetry = self._telemetry
        if telemetry is None:
            return
        try:
            telemetry.record_provider_call(
                OCRProviderCallMetric(
                    trace_id=trace_id,
                    provider_name=self._provider_name,
                    provider_version=self._provider_version,
                    model_version=self._model_version,
                    config_version=self._config_version,
                    page_count=page_count,
                    latency_ms=latency_ms,
                    pages=tuple(
                        OCRPageMetric(
                            page_number=result.page_number,
                            latency_ms=result.latency_ms,
                            status_code=(
                                result.failure.status_code
                                if result.failure is not None
                                else 200
                            ),
                            outcome=self._metric_outcome(result.failure),
                            text_box_count=len(result.observations),
                        )
                        for result in page_results
                    ),
                    error_type=error_type,
                )
            )
        except Exception:
            return

    @staticmethod
    def _metric_outcome(failure: _PageFailure | None) -> str:
        if failure is None:
            return "success"
        code = failure.code
        if "timeout" in code:
            return "timeout"
        if code == "ocr_circuit_open":
            return "circuit_open"
        if code in {
            "ocr_response_schema_error",
            "ocr_array_length_mismatch",
            "ocr_page_result_count_mismatch",
        }:
            return "schema_error"
        return "other_error"


class _ResponseSchemaError(ValueError):
    """Response envelope or array contract violation; never retry."""


def _to_bounding_box(values: list[float]) -> BoundingBox:
    return (float(values[0]), float(values[1]), float(values[2]), float(values[3]))


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split())
