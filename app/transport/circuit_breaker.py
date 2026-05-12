"""Per-upstream circuit breaker (CLOSED → OPEN → HALF_OPEN → CLOSED).

State transitions:
  CLOSED   → OPEN      : `failure_threshold` consecutive failures
  OPEN     → HALF_OPEN : `recovery_timeout` seconds have elapsed
  HALF_OPEN→ CLOSED    : `probe_successes` consecutive successes in HALF_OPEN
  HALF_OPEN→ OPEN      : any failure while probing

Config shape in GatewayConfig.circuit_breaker:
  circuit_breaker:
    enabled: true
    failure_threshold: 5       # failures before opening
    recovery_timeout: 30.0     # seconds to wait before probing
    probe_successes: 2         # consecutive successes to close
"""
from __future__ import annotations

import asyncio
import time
from enum import Enum, auto
from typing import Any


class CircuitState(Enum):
    CLOSED = auto()
    OPEN = auto()
    HALF_OPEN = auto()


class CircuitOpenError(Exception):
    def __init__(self, server_id: str, retry_after: float) -> None:
        super().__init__(f"circuit open for server '{server_id}'")
        self.server_id = server_id
        self.retry_after = retry_after


class CircuitBreaker:
    """One circuit breaker per upstream server."""

    def __init__(
        self,
        server_id: str,
        *,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        probe_successes: int = 2,
    ) -> None:
        self.server_id = server_id
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.probe_successes = probe_successes
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._probe_ok = 0
        self._opened_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    async def before_call(self) -> None:
        """Called before every upstream request. Raises if circuit is OPEN."""
        async with self._lock:
            if self._state == CircuitState.OPEN:
                elapsed = time.monotonic() - (self._opened_at or 0)
                remaining = self.recovery_timeout - elapsed
                if remaining > 0:
                    raise CircuitOpenError(self.server_id, round(remaining, 1))
                # Transition to HALF_OPEN to allow one probe.
                self._state = CircuitState.HALF_OPEN
                self._probe_ok = 0

    async def on_success(self) -> None:
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._probe_ok += 1
                if self._probe_ok >= self.probe_successes:
                    self._state = CircuitState.CLOSED
                    self._failures = 0
                    self._opened_at = None
            elif self._state == CircuitState.CLOSED:
                self._failures = 0

    async def on_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._state == CircuitState.HALF_OPEN:
                # Probe failed — back to OPEN.
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
            elif self._state == CircuitState.CLOSED:
                if self._failures >= self.failure_threshold:
                    self._state = CircuitState.OPEN
                    self._opened_at = time.monotonic()

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self._state.name,
            "failures": self._failures,
            "probe_ok": self._probe_ok,
            "opened_at": self._opened_at,
        }


class CircuitBreakerRegistry:
    """Manages one CircuitBreaker per server_id."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        probe_successes: int = 2,
    ) -> None:
        self.enabled = enabled
        self._cfg = dict(
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
            probe_successes=probe_successes,
        )
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, server_id: str) -> CircuitBreaker:
        if server_id not in self._breakers:
            self._breakers[server_id] = CircuitBreaker(server_id, **self._cfg)
        return self._breakers[server_id]

    def all_states(self) -> dict[str, dict]:
        return {sid: cb.to_dict() for sid, cb in self._breakers.items()}
