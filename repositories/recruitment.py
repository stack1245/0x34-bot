from __future__ import annotations

from typing import Protocol, cast, runtime_checkable

import aiosqlite

from services.recruitment_models import (
    CreateRecruitmentRequest,
    ParticipantStatus,
    RecruitmentAuthorContext,
    RecruitmentParticipant,
    RecruitmentRow,
    RecruitmentStatus,
)
from utils.database import Database


@runtime_checkable
class RecruitmentRepositoryProtocol(Protocol):
    """모집 도메인이 의존하는 저장소 계약입니다."""

    async def create_with_owner(
        self,
        request: CreateRecruitmentRequest,
        *,
        status: RecruitmentStatus,
        owner_status: ParticipantStatus,
        timestamp: str,
    ) -> int: ...

    async def update_thread_id(self, recruitment_id: int, thread_id: int) -> None: ...

    async def set_status(
        self, recruitment_id: int, status: RecruitmentStatus, closed_at: str | None
    ) -> None: ...

    async def update_details(
        self,
        *,
        recruitment_id: int,
        guild_id: int,
        title: str,
        target: str,
        max_members: int,
    ) -> None: ...

    async def fetch_by_message_id(self, message_id: int) -> RecruitmentRow | None: ...

    async def fetch_by_id(self, recruitment_id: int) -> RecruitmentRow | None: ...

    async def fetch_author_context(
        self, recruitment_id: int
    ) -> RecruitmentAuthorContext | None: ...

    async def upsert_participant_status(
        self,
        *,
        recruitment_id: int,
        user_id: int,
        status: ParticipantStatus,
        application_reason: str | None,
        rejection_reason: str | None,
        updated_at: str,
    ) -> None: ...

    async def ensure_owner_participant(
        self, recruitment_id: int, author_id: int, updated_at: str
    ) -> None: ...

    async def fetch_participants(
        self, recruitment_id: int
    ) -> list[RecruitmentParticipant]: ...

    async def fetch_confirmed_participants(
        self,
        recruitment_id: int,
        owner_status: ParticipantStatus,
        accepted_status: ParticipantStatus,
    ) -> list[RecruitmentParticipant]: ...

    async def count_participants_by_status(
        self, recruitment_id: int, status: ParticipantStatus
    ) -> int: ...


class SQLiteRecruitmentRepository:
    """SQLite로 모집 데이터를 저장하고 행 변환을 담당합니다."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def create_with_owner(
        self,
        request: CreateRecruitmentRequest,
        *,
        status: RecruitmentStatus,
        owner_status: ParticipantStatus,
        timestamp: str,
    ) -> int:
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO recruitments (guild_id, channel_id, message_id, author_id, title, target, max_members, status, thread_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.guild_id,
                    request.channel_id,
                    request.message_id,
                    request.author_id,
                    request.title,
                    request.target,
                    request.max_members,
                    status,
                    request.thread_id,
                    timestamp,
                ),
            )
            recruitment_id = int(cursor.lastrowid)
            await self._upsert_owner_participant(
                connection, recruitment_id, request.author_id, owner_status, timestamp
            )
        return recruitment_id

    async def update_thread_id(self, recruitment_id: int, thread_id: int) -> None:
        await self.database.execute(
            "UPDATE recruitments SET thread_id = ? WHERE id = ?",
            (thread_id, recruitment_id),
        )

    async def set_status(
        self, recruitment_id: int, status: RecruitmentStatus, closed_at: str | None
    ) -> None:
        await self.database.execute(
            "UPDATE recruitments SET status = ?, closed_at = ? WHERE id = ?",
            (status, closed_at, recruitment_id),
        )

    async def update_details(
        self,
        *,
        recruitment_id: int,
        guild_id: int,
        title: str,
        target: str,
        max_members: int,
    ) -> None:
        await self.database.execute(
            """
            UPDATE recruitments
            SET title = ?, target = ?, max_members = ?
            WHERE id = ? AND guild_id = ?
            """,
            (title, target, max_members, recruitment_id, guild_id),
        )

    async def fetch_by_message_id(self, message_id: int) -> RecruitmentRow | None:
        row = await self.database.fetch_one(
            "SELECT * FROM recruitments WHERE message_id = ?",
            (message_id,),
        )
        return self._recruitment_from_row(row)

    async def fetch_by_id(self, recruitment_id: int) -> RecruitmentRow | None:
        row = await self.database.fetch_one(
            "SELECT * FROM recruitments WHERE id = ?",
            (recruitment_id,),
        )
        return self._recruitment_from_row(row)

    async def fetch_author_context(
        self, recruitment_id: int
    ) -> RecruitmentAuthorContext | None:
        row = await self.database.fetch_one(
            "SELECT author_id, created_at FROM recruitments WHERE id = ?",
            (recruitment_id,),
        )
        if row is None:
            return None
        return {
            "author_id": int(row["author_id"]),
            "created_at": str(row["created_at"]),
        }

    async def upsert_participant_status(
        self,
        *,
        recruitment_id: int,
        user_id: int,
        status: ParticipantStatus,
        application_reason: str | None,
        rejection_reason: str | None,
        updated_at: str,
    ) -> None:
        await self.database.execute(
            """
            INSERT INTO recruitment_participants (recruitment_id, user_id, status, application_reason, rejection_reason, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(recruitment_id, user_id)
            DO UPDATE SET
                status = excluded.status,
                application_reason = COALESCE(excluded.application_reason, recruitment_participants.application_reason),
                rejection_reason = COALESCE(excluded.rejection_reason, recruitment_participants.rejection_reason),
                updated_at = excluded.updated_at
            """,
            (
                recruitment_id,
                user_id,
                status,
                application_reason,
                rejection_reason,
                updated_at,
            ),
        )

    async def ensure_owner_participant(
        self, recruitment_id: int, author_id: int, updated_at: str
    ) -> None:
        connection = self.database._require_connection()
        await self._upsert_owner_participant(
            connection, recruitment_id, author_id, "owner", updated_at
        )
        await connection.commit()

    async def fetch_participants(
        self, recruitment_id: int
    ) -> list[RecruitmentParticipant]:
        rows = await self.database.fetch_all(
            """
            SELECT user_id, status, application_reason, rejection_reason, updated_at
            FROM recruitment_participants
            WHERE recruitment_id = ?
            ORDER BY
                CASE status
                    WHEN 'owner' THEN 0
                    WHEN 'accepted' THEN 1
                    WHEN 'pending' THEN 2
                    WHEN 'rejected' THEN 3
                    ELSE 4
                END,
                updated_at ASC,
                user_id ASC
            """,
            (recruitment_id,),
        )
        return [self._participant_from_row(row) for row in rows]

    async def fetch_confirmed_participants(
        self,
        recruitment_id: int,
        owner_status: ParticipantStatus,
        accepted_status: ParticipantStatus,
    ) -> list[RecruitmentParticipant]:
        rows = await self.database.fetch_all(
            """
            SELECT user_id, status, application_reason, rejection_reason, updated_at
            FROM recruitment_participants
            WHERE recruitment_id = ? AND status IN (?, ?)
            ORDER BY
                CASE status
                    WHEN 'owner' THEN 0
                    WHEN 'accepted' THEN 1
                    ELSE 2
                END,
                updated_at ASC,
                user_id ASC
            """,
            (recruitment_id, owner_status, accepted_status),
        )
        return [self._participant_from_row(row) for row in rows]

    async def count_participants_by_status(
        self, recruitment_id: int, status: ParticipantStatus
    ) -> int:
        row = await self.database.fetch_one(
            """
            SELECT COUNT(*) AS participant_count
            FROM recruitment_participants
            WHERE recruitment_id = ? AND status = ?
            """,
            (recruitment_id, status),
        )
        if row is None:
            return 0
        return int(row["participant_count"])

    async def _upsert_owner_participant(
        self,
        connection: aiosqlite.Connection,
        recruitment_id: int,
        author_id: int,
        owner_status: ParticipantStatus,
        updated_at: str,
    ) -> None:
        await connection.execute(
            """
            INSERT INTO recruitment_participants (recruitment_id, user_id, status, application_reason, rejection_reason, updated_at)
            VALUES (?, ?, ?, NULL, NULL, ?)
            ON CONFLICT(recruitment_id, user_id)
            DO UPDATE SET
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (recruitment_id, author_id, owner_status, updated_at),
        )

    def _recruitment_from_row(self, row: aiosqlite.Row | None) -> RecruitmentRow | None:
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "guild_id": int(row["guild_id"]),
            "channel_id": int(row["channel_id"]),
            "message_id": int(row["message_id"]),
            "author_id": int(row["author_id"]),
            "title": str(row["title"]),
            "target": str(row["target"]),
            "max_members": int(row["max_members"]),
            "status": cast(RecruitmentStatus, str(row["status"])),
            "thread_id": None if row["thread_id"] is None else int(row["thread_id"]),
            "created_at": str(row["created_at"]),
            "closed_at": None if row["closed_at"] is None else str(row["closed_at"]),
        }

    def _participant_from_row(self, row: aiosqlite.Row) -> RecruitmentParticipant:
        return {
            "user_id": int(row["user_id"]),
            "status": cast(ParticipantStatus, str(row["status"])),
            "application_reason": (
                None
                if row["application_reason"] is None
                else str(row["application_reason"])
            ),
            "rejection_reason": (
                None
                if row["rejection_reason"] is None
                else str(row["rejection_reason"])
            ),
            "updated_at": str(row["updated_at"]),
        }
