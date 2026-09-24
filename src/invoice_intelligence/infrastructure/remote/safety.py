"""Bounded concurrency, rate limiting, circuit breaking, and safe remote audit."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from invoice_intelligence.infrastructure.observability.metrics import get_metrics_registry

_LOGGER = logging.getLogger(__name__)


class RemoteCircuitOpenError(RuntimeError):
    """A remote call was rejected before sending sensitive input."""


class RemoteCallSafety:
    """Process-local safety gate for an SDK-backed remote Provider."""

    def __init__(
        self,
        *,
        max_concurrency: int,
        requests_per_minute: int,
        circuit_failure_threshold: int,
        circuit_recovery_seconds: float,
    ) -> None:
        if max_concurrency <= 0 or requests_per_minute <= 0:
            raise ValueError("Remote concurrency and rate limits must be positive")
        if circuit_failure_threshold <= 0 or circuit_recovery_seconds <= 0:
            raise ValueError("Remote circuit settings must be positive")
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._capacity = float(requests_per_minute)
        self._tokens = float(requests_per_minute)
        self._refill_per_second = requests_per_minute / 60.0
        self._rate_updated_at = time.monotonic()
        self._rate_lock = asyncio.Lock()
        self._failure_threshold = circuit_failure_threshold
        self._recovery_seconds = circuit_recovery_seconds
        self._failure_count = 0
        self._open_until = 0.0
        self._circuit_lock = asyncio.Lock()

    @asynccontextmanager
    async def call(
        self,
        *,
        provider: str,
        operation: str,
        model: str,
    ) -> AsyncIterator[None]:
        """Apply safety controls and emit metadata-only audit events."""

        started_at = time.monotonic()
        async with self._semaphore:
            await self._acquire_rate_token()
            if not await self._before_call():
                self._audit(provider, operation, model, started_at, "circuit_open")
                get_metrics_registry().record_provider(
                    provider=provider,
                    operation=operation,
                    outcome="circuit_open",
                )
                raise RemoteCircuitOpenError("Remote provider circuit is open")
            try:
                yield
            except Exception as exc:
                await self._record_failure()
                self._audit(
                    provider,
                    operation,
                    model,
                    started_at,
                    "failed",
                    error_type=type(exc).__name__,
                )
                get_metrics_registry().record_provider(
                    provider=provider,
                    operation=operation,
                    outcome="failed",
                )
                raise
            else:
                await self._record_success()
                self._audit(provider, operation, model, started_at, "success")
                get_metrics_registry().record_provider(
                    provider=provider,
                    operation=operation,
                    outcome="success",
                )

    async def _acquire_rate_token(self) -> None:
        while True:
            async with self._rate_lock:
                now = time.monotonic()
                elapsed = max(0.0, now - self._rate_updated_at)
                self._tokens = min(
                    self._capacity,
                    self._tokens + elapsed * self._refill_per_second,
                )
                self._rate_updated_at = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait_seconds = (1.0 - self._tokens) / self._refill_per_second
            await asyncio.sleep(wait_seconds)

    async def _before_call(self) -> bool:
        async with self._circuit_lock:
            now = time.monotonic()
            if now < self._open_until:
                return False
            if self._open_until:
                self._failure_count = 0
                self._open_until = 0.0
            return True

    async def _record_success(self) -> None:
        async with self._circuit_lock:
            self._failure_count = 0
            self._open_until = 0.0

    async def _record_failure(self) -> None:
        async with self._circuit_lock:
            self._failure_count += 1
            if self._failure_count >= self._failure_threshold:
                self._open_until = time.monotonic() + self._recovery_seconds

    @staticmethod
    def _audit(
        provider: str,
        operation: str,
        model: str,
        started_at: float,
        outcome: str,
        *,
        error_type: str | None = None,
    ) -> None:
        _LOGGER.info(
            "remote_provider_call",
            extra={
                "provider": provider,
                "operation": operation,
                "model": model,
                "outcome": outcome,
                "latency_ms": round((time.monotonic() - started_at) * 1000.0, 2),
                "error_type": error_type,
            },
        )
