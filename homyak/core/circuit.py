"""Простой async circuit breaker (для защиты от лежащей Ollama)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class CircuitOpen(Exception):
    """Цепь разомкнута — сервис считается недоступным, вызов не делается."""


class CircuitBreaker:
    def __init__(self, fail_max: int = 5, reset_timeout: float = 30.0) -> None:
        self._fail_max = fail_max
        self._reset = reset_timeout
        self._fails = 0
        self._opened_at: float | None = None
        self._lock = asyncio.Lock()

    async def call(self, fn: Callable[..., Awaitable[T]], *args, **kwargs) -> T:
        async with self._lock:
            if self._opened_at is not None:
                if time.monotonic() - self._opened_at < self._reset:
                    raise CircuitOpen("circuit open")
                self._opened_at = None  # half-open: пробуем один вызов

        try:
            result = await fn(*args, **kwargs)
        except Exception:
            async with self._lock:
                self._fails += 1
                if self._fails >= self._fail_max:
                    self._opened_at = time.monotonic()
            raise
        else:
            async with self._lock:
                self._fails = 0
                self._opened_at = None
            return result
