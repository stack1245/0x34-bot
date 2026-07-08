from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Generic, TypeVar

T = TypeVar("T")


class ServiceError(RuntimeError):
    """서비스 계층 예외의 기준 타입입니다."""


@dataclass(frozen=True)
class ServiceResult(Generic[T]):
    """서비스 작업의 성공/실패를 명시적으로 담는 결과 타입입니다."""

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
    """서비스 공통 로거와 안전 실행 헬퍼를 제공합니다."""

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
