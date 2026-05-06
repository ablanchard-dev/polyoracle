import logging
from collections.abc import Callable
from time import monotonic, sleep
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self, min_interval_seconds: float = 0.25) -> None:
        self.min_interval_seconds = min_interval_seconds
        self._last_request = 0.0

    def wait(self) -> None:
        elapsed = monotonic() - self._last_request
        if elapsed < self.min_interval_seconds:
            sleep(self.min_interval_seconds - elapsed)
        self._last_request = monotonic()


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5) -> None:
        self.failure_threshold = failure_threshold
        self.failures = 0
        self.open = False

    def call(self, fn: Callable[[], Any]) -> Any:
        if self.open:
            raise RuntimeError("Circuit breaker is open")
        try:
            result = fn()
            self.failures = 0
            return result
        except Exception:
            self.failures += 1
            if self.failures >= self.failure_threshold:
                self.open = True
            raise


class PolymarketHttpClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.rate_limiter = RateLimiter()
        self.circuit_breaker = CircuitBreaker()

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | list[Any]:
        def request() -> dict[str, Any] | list[Any]:
            self.rate_limiter.wait()
            url = f"{self.base_url}/{path.lstrip('/')}"
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(url, params=params)
                response.raise_for_status()
                return response.json()

        return self.circuit_breaker.call(request)
