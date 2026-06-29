from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Generic, TypeVar


T = TypeVar("T")


class ServiceError(RuntimeError):
    """Base exception raised by service-layer operations."""


@dataclass(frozen=True)
class ServiceResult(Generic[T]):
    """Explicit success/failure envelope for service operations."""

    ok: bool
    value: T | None = None
    error: str | None = None

    @classmethod
    def success(cls, value: T | None = None) -> "ServiceResult[T]":
        return cls(ok=True, value=value)

    @classmethod
    def failure(cls, error: str) -> "ServiceResult[T]":
        return cls(ok=False, error=error)


class BaseService:
    """Common service base with a logger and graceful async execution helper."""

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger(self.__class__.__module__)

    async def run_safely(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        fallback: T | None = None,
        context: str = "service operation",
    ) -> ServiceResult[T]:
        try:
            return ServiceResult.success(await operation())
        except Exception as exc:
            self.logger.exception("%s failed: %s", context, exc)
            if fallback is not None:
                return ServiceResult.success(fallback)
            return ServiceResult.failure(str(exc))
