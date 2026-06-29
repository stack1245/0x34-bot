from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiosqlite

from services.base import BaseService, ServiceError
from utils.database import Database
from utils.datetime import now_utc_iso


STATUS_OPEN = "open"
STATUS_CLOSED = "closed"
PARTICIPANT_PENDING = "pending"
PARTICIPANT_ACCEPTED = "accepted"
PARTICIPANT_REJECTED = "rejected"
PARTICIPANT_OWNER = "owner"


@dataclass(frozen=True)
class CreateRecruitmentRequest:
    guild_id: int
    channel_id: int
    message_id: int
    author_id: int
    title: str
    target: str
    max_members: int
    thread_id: int | None = None


class RecruitmentService(BaseService):
    """Recruitment domain operations with owner invariants enforced at the DB boundary."""

    def __init__(self, database: Database) -> None:
        super().__init__()
        self.database = database

    async def create_recruitment(self, request: CreateRecruitmentRequest) -> int:
        """Create a recruitment and atomically register its author as the owner participant."""
        timestamp = now_utc_iso()
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
                    STATUS_OPEN,
                    request.thread_id,
                    timestamp,
                ),
            )
            recruitment_id = int(cursor.lastrowid)
            await self._upsert_owner_participant(connection, recruitment_id, request.author_id, timestamp)
        return recruitment_id

    async def update_thread_id(self, recruitment_id: int, thread_id: int) -> None:
        await self.database.execute(
            "UPDATE recruitments SET thread_id = ? WHERE id = ?",
            (thread_id, recruitment_id),
        )

    async def close_recruitment(self, recruitment_id: int) -> None:
        await self.database.execute(
            "UPDATE recruitments SET status = ?, closed_at = ? WHERE id = ?",
            (STATUS_CLOSED, now_utc_iso(), recruitment_id),
        )

    async def update_recruitment_details(
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

    async def get_recruitment_by_message_id(self, message_id: int) -> dict[str, Any] | None:
        row = await self.database.fetch_one(
            "SELECT * FROM recruitments WHERE message_id = ?",
            (message_id,),
        )
        return await self._hydrate_recruitment(row)

    async def get_recruitment_by_id(self, recruitment_id: int) -> dict[str, Any] | None:
        row = await self.database.fetch_one(
            "SELECT * FROM recruitments WHERE id = ?",
            (recruitment_id,),
        )
        return await self._hydrate_recruitment(row)

    async def get_participant(self, recruitment_id: int, user_id: int) -> dict[str, Any] | None:
        recruitment = await self.get_recruitment_by_id(recruitment_id)
        if recruitment is None:
            return None
        for participant in recruitment["participants"]:
            if int(participant["user_id"]) == int(user_id):
                return participant
        return None

    async def save_participant_status(
        self,
        recruitment_id: int,
        user_id: int,
        status: str,
        *,
        application_reason: str | None = None,
        rejection_reason: str | None = None,
    ) -> None:
        """Upsert a participant while preventing the recruitment author from losing owner status."""
        recruitment = await self.database.fetch_one(
            "SELECT author_id, created_at FROM recruitments WHERE id = ?",
            (recruitment_id,),
        )
        if recruitment is None:
            raise ServiceError(f"Recruitment not found: {recruitment_id}")

        effective_status = status
        if int(recruitment["author_id"]) == int(user_id):
            effective_status = PARTICIPANT_OWNER
            application_reason = None
            rejection_reason = None

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
            (recruitment_id, user_id, effective_status, application_reason, rejection_reason, now_utc_iso()),
        )

    async def is_owner(self, recruitment_id: int, user_id: int) -> bool:
        participant = await self.get_participant(recruitment_id, user_id)
        return participant is not None and participant["status"] == PARTICIPANT_OWNER

    async def get_participant_user_ids(self, recruitment_id: int, status: str) -> list[int]:
        recruitment = await self.get_recruitment_by_id(recruitment_id)
        if recruitment is None:
            return []
        return [int(row["user_id"]) for row in recruitment["participants"] if row["status"] == status]

    async def get_confirmed_participants(self, recruitment_id: int) -> list[dict[str, Any]]:
        recruitment = await self.database.fetch_one(
            "SELECT author_id, created_at FROM recruitments WHERE id = ?",
            (recruitment_id,),
        )
        if recruitment is None:
            return []

        await self.ensure_owner_participant(
            recruitment_id,
            int(recruitment["author_id"]),
            str(recruitment["created_at"]),
        )
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
            (recruitment_id, PARTICIPANT_OWNER, PARTICIPANT_ACCEPTED),
        )
        return [dict(row) for row in rows]

    async def get_confirmed_participant_user_ids(self, recruitment_id: int) -> list[int]:
        return [int(row["user_id"]) for row in await self.get_confirmed_participants(recruitment_id)]

    async def get_pending_participant_count(self, recruitment_id: int) -> int:
        row = await self.database.fetch_one(
            """
            SELECT COUNT(*) AS pending_count
            FROM recruitment_participants
            WHERE recruitment_id = ? AND status = ?
            """,
            (recruitment_id, PARTICIPANT_PENDING),
        )
        if row is None:
            return 0
        return int(row["pending_count"])

    async def get_pending_participants(self, recruitment_id: int) -> list[dict[str, Any]]:
        recruitment = await self.get_recruitment_by_id(recruitment_id)
        if recruitment is None:
            return []
        pending_rows = [row for row in recruitment["participants"] if row["status"] == PARTICIPANT_PENDING]
        return pending_rows[:25]

    async def _hydrate_recruitment(self, row: aiosqlite.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None

        recruitment = dict(row)
        await self.ensure_owner_participant(
            int(recruitment["id"]),
            int(recruitment["author_id"]),
            str(recruitment["created_at"]),
        )
        recruitment["participants"] = await self._fetch_participants(int(recruitment["id"]))
        return recruitment

    async def ensure_owner_participant(self, recruitment_id: int, author_id: int, updated_at: str | None = None) -> None:
        connection = self.database._require_connection()
        await self._upsert_owner_participant(connection, recruitment_id, author_id, updated_at or now_utc_iso())
        await connection.commit()

    async def _upsert_owner_participant(
        self,
        connection: aiosqlite.Connection,
        recruitment_id: int,
        author_id: int,
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
            (recruitment_id, author_id, PARTICIPANT_OWNER, updated_at),
        )

    async def _fetch_participants(self, recruitment_id: int) -> list[dict[str, Any]]:
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
        return [dict(row) for row in rows]