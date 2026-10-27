"""Deterministic, thread-safe mock telecom providers for the SmartDialer prototype.

Idempotency is guaranteed only for the lifetime of each provider object. A real
provider must persist idempotency keys or support reconciliation across restarts.
"""
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock


@dataclass(frozen=True)
class ProviderResult:
    accepted: bool
    provider_call_id: str | None = None
    error: str | None = None


class TelecomProvider:
    name = "base"

    def initiate(self, call_id: int, idempotency_key: str) -> ProviderResult:
        raise NotImplementedError


class FastMockProvider(TelecomProvider):
    name = "mock-fast"

    def __init__(
        self,
        *,
        latency_seconds: float = 0.01,
        failure_rate: float = 0.0,
        random_value: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        self.latency_seconds = max(0.0, latency_seconds)
        self.failure_rate = max(0.0, min(1.0, failure_rate))
        self._random_value = random_value or random.random
        self._sleep = sleep or time.sleep
        self._accepted_keys: dict[str, ProviderResult] = {}
        self._lock = RLock()

    def initiate(self, call_id: int, idempotency_key: str) -> ProviderResult:
        if not idempotency_key:
            return ProviderResult(False, error="idempotency_key is required")
        if self.latency_seconds:
            self._sleep(self.latency_seconds)
        # Cache operations are synchronized so concurrent calls with the same
        # key cannot both create accepted results.
        with self._lock:
            if idempotency_key in self._accepted_keys:
                return self._accepted_keys[idempotency_key]
            if self.failure_rate > 0 and self._random_value() < self.failure_rate:
                return ProviderResult(False, error="simulated fast-provider failure")
            result = ProviderResult(True, f"fast-{call_id}")
            self._accepted_keys[idempotency_key] = result
            return result


class UnreliableMockProvider(TelecomProvider):
    name = "mock-unreliable"

    def __init__(
        self,
        random_value: Callable[[], float] | None = None,
        *,
        latency_seconds: float = 0.1,
        failure_rate: float = 0.25,
        sleep: Callable[[float], None] | None = None,
    ):
        self.latency_seconds = max(0.0, latency_seconds)
        self.failure_rate = max(0.0, min(1.0, failure_rate))
        self._random_value = random_value or random.random
        self._sleep = sleep or time.sleep
        self._accepted_keys: dict[str, ProviderResult] = {}
        self._lock = RLock()

    def initiate(self, call_id: int, idempotency_key: str) -> ProviderResult:
        if not idempotency_key:
            return ProviderResult(False, error="idempotency_key is required")
        if self.latency_seconds:
            self._sleep(self.latency_seconds)
        with self._lock:
            if idempotency_key in self._accepted_keys:
                return self._accepted_keys[idempotency_key]
            if self._random_value() < self.failure_rate:
                return ProviderResult(False, error="simulated provider timeout")
            result = ProviderResult(True, f"unreliable-{call_id}")
            self._accepted_keys[idempotency_key] = result
            return result


PROVIDERS = {
    "mock-fast": FastMockProvider(),
    "mock-unreliable": UnreliableMockProvider(),
}
