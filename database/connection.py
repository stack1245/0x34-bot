from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from core.config import DATA_DIR

logger = logging.getLogger(__name__)
SETTINGS_DB_PATH = DATA_DIR / "settings.db"
SCHEDULES_DB_PATH = DATA_DIR / "schedules.db"
TEAMS_DB_PATH = DATA_DIR / "teams.db"


@dataclass(frozen=True)
class GuildSettings:
    guild_id: int
    contest_channel_id: int | None
    team_contest_channel_id: int | None
    schedule_channel_id: int | None
    schedule_message_id: int | None


def initialize_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Initializing SQLite databases. settings=%s schedules=%s teams=%s",
        SETTINGS_DB_PATH,
        SCHEDULES_DB_PATH,
        TEAMS_DB_PATH,
    )

    with _settings_connection() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                contest_channel_id INTEGER,
                team_contest_channel_id INTEGER,
                schedule_channel_id INTEGER,
                schedule_message_id INTEGER
            )
            """)

        settings_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(guild_settings)"
            ).fetchall()
        }

        if "team_contest_channel_id" not in settings_columns:
            logger.info(
                "Migrating guild_settings: adding team_contest_channel_id column."
            )
            connection.execute(
                "ALTER TABLE guild_settings ADD COLUMN team_contest_channel_id INTEGER"
            )

        if "schedule_channel_id" not in settings_columns:
            logger.info("Migrating guild_settings: adding schedule_channel_id column.")
            connection.execute(
                "ALTER TABLE guild_settings ADD COLUMN schedule_channel_id INTEGER"
            )

        if "schedule_message_id" not in settings_columns:
            logger.info("Migrating guild_settings: adding schedule_message_id column.")
            connection.execute(
                "ALTER TABLE guild_settings ADD COLUMN schedule_message_id INTEGER"
            )

        connection.execute("""
            UPDATE guild_settings
            SET contest_channel_id = -1
            WHERE contest_channel_id IS NULL
            """)

    with _schedules_connection() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS guild_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                start_timestamp INTEGER NULL,
                end_timestamp INTEGER NOT NULL
            )
            """)

        schedule_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(guild_schedules)"
            ).fetchall()
        }

        if "start_timestamp" not in schedule_columns:
            logger.info("Migrating guild_schedules: adding start_timestamp column.")
            connection.execute(
                "ALTER TABLE guild_schedules ADD COLUMN start_timestamp INTEGER NULL"
            )

        if "end_timestamp" not in schedule_columns:
            logger.info("Migrating guild_schedules: adding end_timestamp column.")
            connection.execute(
                "ALTER TABLE guild_schedules ADD COLUMN end_timestamp INTEGER"
            )

        if "target_timestamp" in schedule_columns:
            logger.info(
                "Migrating guild_schedules data from target_timestamp to end_timestamp."
            )
            connection.execute("""
                UPDATE guild_schedules
                SET end_timestamp = COALESCE(end_timestamp, target_timestamp)
                WHERE target_timestamp IS NOT NULL
                """)

        connection.execute("""
            UPDATE guild_schedules
            SET start_timestamp = NULL
            WHERE start_timestamp IS NULL
            """)

    with _teams_connection() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS guild_teams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                thread_id INTEGER NOT NULL,
                leader_id INTEGER NOT NULL
            )
            """)

    logger.info(
        "Database schemas ensured for guild_settings, guild_schedules and guild_teams tables."
    )


def init_db() -> None:
    initialize_database()


def add_schedule(
    guild_id: int,
    title: str,
    start_ts: Optional[int],
    end_ts: int,
) -> None:
    logger.info(
        "Adding schedule. guild_id=%s title=%s start_timestamp=%s end_timestamp=%s",
        guild_id,
        title,
        start_ts,
        end_ts,
    )
    try:
        with _schedules_connection() as connection:
            connection.execute(
                """
                INSERT INTO guild_schedules (
                    guild_id,
                    title,
                    start_timestamp,
                    end_timestamp
                )
                VALUES (?, ?, ?, ?)
                """,
                (guild_id, title, start_ts, end_ts),
            )
    except Exception:
        logger.exception(
            "Failed to add schedule. guild_id=%s title=%s start_timestamp=%s end_timestamp=%s",
            guild_id,
            title,
            start_ts,
            end_ts,
        )
        raise


def get_schedules(guild_id: int) -> list[tuple[int, int, str, Optional[int], int]]:
    logger.debug("Fetching active schedules. guild_id=%s", guild_id)
    now_timestamp = int(time.time())
    try:
        with _schedules_connection() as connection:
            rows = connection.execute(
                """
                SELECT id, guild_id, title, start_timestamp, end_timestamp
                FROM guild_schedules
                WHERE guild_id = ?
                  AND end_timestamp >= ?
                ORDER BY end_timestamp ASC
                """,
                (guild_id, now_timestamp),
            ).fetchall()
    except Exception:
        logger.exception("Failed to fetch schedules. guild_id=%s", guild_id)
        raise

    return [
        (
            int(row["id"]),
            int(row["guild_id"]),
            str(row["title"]),
            int(row["start_timestamp"]) if row["start_timestamp"] is not None else None,
            int(row["end_timestamp"]),
        )
        for row in rows
    ]


def delete_schedule(guild_id: int, schedule_id: int) -> bool:
    logger.info(
        "Deleting schedule. guild_id=%s schedule_id=%s",
        guild_id,
        schedule_id,
    )
    try:
        with _schedules_connection() as connection:
            cursor = connection.execute(
                "DELETE FROM guild_schedules WHERE guild_id = ? AND id = ?",
                (guild_id, schedule_id),
            )
    except Exception:
        logger.exception(
            "Failed to delete schedule. guild_id=%s schedule_id=%s",
            guild_id,
            schedule_id,
        )
        raise

    deleted = cursor.rowcount > 0
    logger.info(
        "Schedule delete result. guild_id=%s schedule_id=%s deleted=%s",
        guild_id,
        schedule_id,
        deleted,
    )
    return deleted


def get_guild_settings(guild_id: int) -> GuildSettings:
    logger.debug("Fetching guild settings. guild_id=%s", guild_id)
    with _settings_connection() as connection:
        row = connection.execute(
            """
            SELECT
                guild_id,
                contest_channel_id,
                team_contest_channel_id,
                schedule_channel_id,
                schedule_message_id
            FROM guild_settings
            WHERE guild_id = ?
            """,
            (guild_id,),
        ).fetchone()

    if row is None:
        logger.debug("No guild settings stored. guild_id=%s", guild_id)
        return GuildSettings(
            guild_id=guild_id,
            contest_channel_id=None,
            team_contest_channel_id=None,
            schedule_channel_id=None,
            schedule_message_id=None,
        )

    contest_channel_id_raw = row["contest_channel_id"]
    contest_channel_id = (
        int(contest_channel_id_raw)
        if contest_channel_id_raw is not None and int(contest_channel_id_raw) > 0
        else None
    )
    team_contest_channel_id_raw = row["team_contest_channel_id"]
    team_contest_channel_id = (
        int(team_contest_channel_id_raw)
        if team_contest_channel_id_raw is not None
        and int(team_contest_channel_id_raw) > 0
        else None
    )
    schedule_channel_id = (
        int(row["schedule_channel_id"])
        if row["schedule_channel_id"] is not None
        else None
    )
    schedule_message_id = (
        int(row["schedule_message_id"])
        if row["schedule_message_id"] is not None
        else None
    )

    return GuildSettings(
        guild_id=int(row["guild_id"]),
        contest_channel_id=contest_channel_id,
        team_contest_channel_id=team_contest_channel_id,
        schedule_channel_id=schedule_channel_id,
        schedule_message_id=schedule_message_id,
    )


def get_contest_channel_id(guild_id: int) -> int | None:
    return get_guild_settings(guild_id).contest_channel_id


def set_contest_channel_id(guild_id: int, channel_id: int) -> None:
    logger.info(
        "Persisting contest channel. guild_id=%s channel_id=%s",
        guild_id,
        channel_id,
    )
    with _settings_connection() as connection:
        connection.execute(
            """
            INSERT INTO guild_settings (guild_id, contest_channel_id)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE
            SET contest_channel_id = excluded.contest_channel_id
            """,
            (guild_id, channel_id),
        )


def clear_contest_channel_id(guild_id: int) -> None:
    logger.info("Clearing contest channel. guild_id=%s", guild_id)
    with _settings_connection() as connection:
        connection.execute(
            """
            INSERT INTO guild_settings (guild_id, contest_channel_id)
            VALUES (?, -1)
            ON CONFLICT(guild_id) DO UPDATE
            SET contest_channel_id = -1
            """,
            (guild_id,),
        )


def get_team_contest_channel_id(guild_id: int) -> Optional[int]:
    return get_guild_settings(guild_id).team_contest_channel_id


def set_team_contest_channel_id(guild_id: int, channel_id: int) -> None:
    logger.info(
        "Persisting team contest channel. guild_id=%s channel_id=%s",
        guild_id,
        channel_id,
    )
    with _settings_connection() as connection:
        connection.execute(
            """
            INSERT INTO guild_settings (guild_id, team_contest_channel_id)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE
            SET team_contest_channel_id = excluded.team_contest_channel_id
            """,
            (guild_id, channel_id),
        )


def reset_team_contest_channel(guild_id: int) -> None:
    logger.info("Resetting team contest channel. guild_id=%s", guild_id)
    with _settings_connection() as connection:
        connection.execute(
            """
            INSERT INTO guild_settings (guild_id, team_contest_channel_id)
            VALUES (?, NULL)
            ON CONFLICT(guild_id) DO UPDATE
            SET team_contest_channel_id = NULL
            """,
            (guild_id,),
        )


def add_team_leader(guild_id: int, thread_id: int, leader_id: int) -> None:
    logger.info(
        "Persisting team leader mapping. guild_id=%s thread_id=%s leader_id=%s",
        guild_id,
        thread_id,
        leader_id,
    )
    with _teams_connection() as connection:
        connection.execute(
            """
            INSERT INTO guild_teams (guild_id, thread_id, leader_id)
            VALUES (?, ?, ?)
            """,
            (guild_id, thread_id, leader_id),
        )


def is_team_leader_of_thread(guild_id: int, thread_id: int, leader_id: int) -> bool:
    with _teams_connection() as connection:
        row = connection.execute(
            """
            SELECT 1
            FROM guild_teams
            WHERE guild_id = ?
              AND thread_id = ?
              AND leader_id = ?
            LIMIT 1
            """,
            (guild_id, thread_id, leader_id),
        ).fetchone()

    return row is not None


def get_schedule_channel_id(guild_id: int) -> Optional[int]:
    return get_guild_settings(guild_id).schedule_channel_id


def set_schedule_channel_id(guild_id: int, channel_id: int) -> None:
    logger.info(
        "Persisting schedule dashboard channel. guild_id=%s channel_id=%s",
        guild_id,
        channel_id,
    )
    with _settings_connection() as connection:
        connection.execute(
            """
            INSERT INTO guild_settings (guild_id, contest_channel_id, schedule_channel_id)
            VALUES (?, COALESCE((SELECT contest_channel_id FROM guild_settings WHERE guild_id = ?), -1), ?)
            ON CONFLICT(guild_id) DO UPDATE
            SET schedule_channel_id = excluded.schedule_channel_id
            """,
            (guild_id, guild_id, channel_id),
        )


def reset_schedule_channel(guild_id: int) -> None:
    logger.info("Resetting schedule dashboard mapping. guild_id=%s", guild_id)
    with _settings_connection() as connection:
        connection.execute(
            """
            INSERT INTO guild_settings (guild_id, contest_channel_id, schedule_channel_id, schedule_message_id)
            VALUES (?, COALESCE((SELECT contest_channel_id FROM guild_settings WHERE guild_id = ?), -1), NULL, NULL)
            ON CONFLICT(guild_id) DO UPDATE
            SET schedule_channel_id = NULL,
                schedule_message_id = NULL
            """,
            (guild_id, guild_id),
        )


def get_schedule_message_id(guild_id: int) -> Optional[int]:
    return get_guild_settings(guild_id).schedule_message_id


def set_schedule_message_id(guild_id: int, message_id: Optional[int]) -> None:
    logger.info(
        "Persisting schedule dashboard message id. guild_id=%s message_id=%s",
        guild_id,
        message_id,
    )
    with _settings_connection() as connection:
        connection.execute(
            """
            INSERT INTO guild_settings (guild_id, contest_channel_id, schedule_message_id)
            VALUES (?, COALESCE((SELECT contest_channel_id FROM guild_settings WHERE guild_id = ?), -1), ?)
            ON CONFLICT(guild_id) DO UPDATE
            SET schedule_message_id = excluded.schedule_message_id
            """,
            (guild_id, guild_id, message_id),
        )


@contextmanager
def _connection(database_path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=3000")

    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        logger.exception("Database transaction failed.")
        raise
    finally:
        connection.close()


@contextmanager
def _settings_connection() -> Iterator[sqlite3.Connection]:
    with _connection(SETTINGS_DB_PATH) as connection:
        yield connection


@contextmanager
def _schedules_connection() -> Iterator[sqlite3.Connection]:
    with _connection(SCHEDULES_DB_PATH) as connection:
        yield connection


@contextmanager
def _teams_connection() -> Iterator[sqlite3.Connection]:
    with _connection(TEAMS_DB_PATH) as connection:
        yield connection


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    with _settings_connection() as connection:
        yield connection
