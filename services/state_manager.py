from __future__ import annotations

from dataclasses import dataclass

from utils.database import Database
from utils.datetime import now_utc_iso

from .base import BaseService


@dataclass(frozen=True)
class DashboardState:
    """Persisted Discord message pointer for a singleton dashboard."""

    name: str
    board_channel_id: int | None
    board_message_id: int | None
    updated_at: str


class StateManager(BaseService):
    """Repository-backed state manager for restart-safe singleton messages."""

    def __init__(self, database: Database) -> None:
        super().__init__()
        self.database = database

    async def get_dashboard_state(self, name: str) -> DashboardState | None:
        row = await self.database.fetch_one(
            """
            SELECT name, board_channel_id, board_message_id, updated_at
            FROM dashboard_state
            WHERE name = ?
            """,
            (name,),
        )
        if row is None:
            return None
        return DashboardState(
            name=str(row["name"]),
            board_channel_id=None if row["board_channel_id"] is None else int(row["board_channel_id"]),
            board_message_id=None if row["board_message_id"] is None else int(row["board_message_id"]),
            updated_at=str(row["updated_at"]),
        )

    async def save_dashboard_state(self, name: str, channel_id: int, message_id: int) -> None:
        await self.database.execute(
            """
            INSERT INTO dashboard_state (name, board_channel_id, board_message_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name)
            DO UPDATE SET
                board_channel_id = excluded.board_channel_id,
                board_message_id = excluded.board_message_id,
                updated_at = excluded.updated_at
            """,
            (name, channel_id, message_id, now_utc_iso()),
        )

    async def clear_dashboard_state(self, name: str) -> None:
        await self.database.execute("DELETE FROM dashboard_state WHERE name = ?", (name,))
