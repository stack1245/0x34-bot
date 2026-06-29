from __future__ import annotations

from typing import Any, Iterable, Protocol

import aiosqlite


class Repository(Protocol):
    """Minimal async database contract used by repositories and services."""

    async def execute(self, query: str, params: Iterable[Any] = ()) -> aiosqlite.Cursor:
        ...

    async def fetch_one(self, query: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        ...

    async def fetch_all(self, query: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        ...
