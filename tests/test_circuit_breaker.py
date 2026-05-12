"""Tests for the circuit breaker state machine."""
import asyncio

import pytest

from app.transport.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState


@pytest.fixture
def cb():
    return CircuitBreaker(
        "test-server",
        failure_threshold=3,
        recovery_timeout=0.1,   # short for testing
        probe_successes=2,
    )


async def test_starts_closed(cb):
    assert cb.state == CircuitState.CLOSED


async def test_opens_after_threshold(cb):
    for _ in range(3):
        await cb.on_failure()
    assert cb.state == CircuitState.OPEN


async def test_open_rejects_calls(cb):
    for _ in range(3):
        await cb.on_failure()
    with pytest.raises(CircuitOpenError):
        await cb.before_call()


async def test_transitions_to_half_open_after_timeout(cb):
    for _ in range(3):
        await cb.on_failure()
    await asyncio.sleep(0.15)
    await cb.before_call()   # should NOT raise — transitions to HALF_OPEN
    assert cb.state == CircuitState.HALF_OPEN


async def test_closes_after_probe_successes(cb):
    for _ in range(3):
        await cb.on_failure()
    await asyncio.sleep(0.15)
    await cb.before_call()   # transition to HALF_OPEN
    await cb.on_success()
    await cb.on_success()
    assert cb.state == CircuitState.CLOSED


async def test_reopens_on_probe_failure(cb):
    for _ in range(3):
        await cb.on_failure()
    await asyncio.sleep(0.15)
    await cb.before_call()   # HALF_OPEN
    await cb.on_failure()    # probe failed → back to OPEN
    assert cb.state == CircuitState.OPEN


async def test_success_resets_failure_count(cb):
    await cb.on_failure()
    await cb.on_failure()
    await cb.on_success()    # resets counter
    # Should need threshold failures again to open.
    await cb.on_failure()
    await cb.on_failure()
    assert cb.state == CircuitState.CLOSED
    await cb.on_failure()
    assert cb.state == CircuitState.OPEN
