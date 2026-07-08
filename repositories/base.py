from __future__ import annotations

from typing import Any, Iterable, Protocol

import aiosqlite


class Repository(Protocol):
    """저장소와 서비스가 공유하는 최소 비동기 DB 계약입니다."""

    async def execute(
        self, query: str, params: Iterable[Any] = ()
    ) -> aiosqlite.Cursor: ...

    async def fetch_one(
        self, query: str, params: Iterable[Any] = ()
    ) -> aiosqlite.Row | None: ...

    async def fetch_all(
        self, query: str, params: Iterable[Any] = ()
    ) -> list[aiosqlite.Row]: ...
