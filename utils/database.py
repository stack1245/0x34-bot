from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import aiosqlite


SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schedules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        starts_at TEXT NOT NULL,
        body TEXT NOT NULL,
        created_by INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        event_id INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tournaments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        platform TEXT NOT NULL,
        starts_at TEXT NOT NULL,
        link TEXT NOT NULL,
        notice_channel_id INTEGER NOT NULL,
        created_by INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS recruitments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        message_id INTEGER UNIQUE NOT NULL,
        author_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        target TEXT NOT NULL,
        max_members INTEGER NOT NULL,
        status TEXT NOT NULL,
        thread_id INTEGER,
        created_at TEXT NOT NULL,
        closed_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS recruitment_votes (
        recruitment_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        state TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (recruitment_id, user_id),
        FOREIGN KEY (recruitment_id) REFERENCES recruitments(id) ON DELETE CASCADE
    )
    """,
)


class Database:
    """aiosqlite 연결을 감싸서 Cog들이 같은 방식으로 DB를 쓰게 하는 작은 헬퍼입니다."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.connection: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """SQLite 파일을 열고 필요한 테이블을 준비합니다."""
        if self.connection is not None:
            return

        if self.path.parent != Path(""):
            self.path.parent.mkdir(parents=True, exist_ok=True)

        self.connection = await aiosqlite.connect(self.path)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute("PRAGMA foreign_keys = ON")

        for statement in SCHEMA:
            await self.connection.execute(statement)
        await self._run_migrations()
        await self.connection.commit()

    async def _run_migrations(self) -> None:
        """이미 만들어진 SQLite DB에도 새 컬럼을 안전하게 추가합니다."""
        connection = self._require_connection()
        cursor = await connection.execute("PRAGMA table_info(recruitments)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "thread_id" not in columns:
            await connection.execute("ALTER TABLE recruitments ADD COLUMN thread_id INTEGER")

    async def close(self) -> None:
        """봇 종료 시 DB 연결을 닫습니다."""
        if self.connection is None:
            return
        await self.connection.close()
        self.connection = None

    def _require_connection(self) -> aiosqlite.Connection:
        """초기화 전에 DB를 쓰는 실수를 빠르게 발견하게 합니다."""
        if self.connection is None:
            raise RuntimeError("Database.connect()가 먼저 호출되어야 합니다.")
        return self.connection

    async def execute(self, query: str, params: Iterable[Any] = ()) -> aiosqlite.Cursor:
        """INSERT, UPDATE, DELETE처럼 결과 행이 중요하지 않은 쿼리를 실행합니다."""
        connection = self._require_connection()
        cursor = await connection.execute(query, tuple(params))
        await connection.commit()
        return cursor

    async def fetch_one(self, query: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        """하나의 행만 필요한 SELECT 쿼리에 사용합니다."""
        connection = self._require_connection()
        cursor = await connection.execute(query, tuple(params))
        return await cursor.fetchone()

    async def fetch_all(self, query: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        """여러 행이 필요한 SELECT 쿼리에 사용합니다."""
        connection = self._require_connection()
        cursor = await connection.execute(query, tuple(params))
        rows = await cursor.fetchall()
        return list(rows)