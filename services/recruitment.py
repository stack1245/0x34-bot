from __future__ import annotations

from logging import Logger
from typing import cast

from repositories.recruitment import (
    RecruitmentRepositoryProtocol,
    SQLiteRecruitmentRepository,
)
from services.base import BaseService, ServiceError
from services.recruitment_models import (
    CreateRecruitmentRequest,
    PARTICIPANT_ACCEPTED,
    PARTICIPANT_OWNER,
    PARTICIPANT_PENDING,
    PARTICIPANT_REJECTED,
    STATUS_CLOSED,
    STATUS_OPEN,
    ParticipantStatus,
    RecruitmentRecord,
    RecruitmentRow,
)
from utils.database import Database
from utils.datetime import now_utc_iso

__all__ = [
    "CreateRecruitmentRequest",
    "RecruitmentError",
    "RecruitmentNotFoundError",
    "RecruitmentService",
    "PARTICIPANT_ACCEPTED",
    "PARTICIPANT_OWNER",
    "PARTICIPANT_PENDING",
    "PARTICIPANT_REJECTED",
    "STATUS_CLOSED",
    "STATUS_OPEN",
]


class RecruitmentError(ServiceError):
    """Base exception for recruitment domain failures."""


class RecruitmentNotFoundError(RecruitmentError):
    """Raised when a recruitment mutation targets a missing recruitment.

    Args:
        recruitment_id: Recruitment primary key that could not be found.
    """

    def __init__(self, recruitment_id: int) -> None:
        super().__init__(f"Recruitment not found: {recruitment_id}")
        self.recruitment_id = recruitment_id


class RecruitmentService(BaseService):
    """Recruitment domain service with repository-backed persistence.

    Args:
        data_source: SQLite database wrapper or object implementing the recruitment repository protocol.
        logger: Optional logger injected for service diagnostics.
    """

    def __init__(
        self,
        data_source: Database | RecruitmentRepositoryProtocol,
        *,
        logger: Logger | None = None,
    ) -> None:
        super().__init__(logger=logger)
        if isinstance(data_source, RecruitmentRepositoryProtocol):
            self.repository = data_source
        else:
            self.repository = SQLiteRecruitmentRepository(data_source)

    async def create_recruitment(self, request: CreateRecruitmentRequest) -> int:
        """Create a recruitment and atomically register its author as owner.

        Args:
            request: Immutable creation payload for the recruitment post.

        Returns:
            Newly created recruitment primary key.
        """
        timestamp = now_utc_iso()
        return await self.repository.create_with_owner(
            request,
            status=STATUS_OPEN,
            owner_status=PARTICIPANT_OWNER,
            timestamp=timestamp,
        )

    async def update_thread_id(self, recruitment_id: int, thread_id: int) -> None:
        """Attach a private workspace thread to a recruitment.

        Args:
            recruitment_id: Recruitment primary key.
            thread_id: Discord private thread snowflake.

        Returns:
            None.
        """
        await self.repository.update_thread_id(recruitment_id, thread_id)

    async def close_recruitment(self, recruitment_id: int) -> None:
        """Mark a recruitment as closed.

        Args:
            recruitment_id: Recruitment primary key.

        Returns:
            None.
        """
        await self.repository.set_status(recruitment_id, STATUS_CLOSED, now_utc_iso())

    async def reopen_recruitment(self, recruitment_id: int) -> None:
        """Reopen a closed recruitment.

        Args:
            recruitment_id: Recruitment primary key.

        Returns:
            None.
        """
        await self.repository.set_status(recruitment_id, STATUS_OPEN, None)

    async def update_recruitment_details(
        self,
        *,
        recruitment_id: int,
        guild_id: int,
        title: str,
        target: str,
        max_members: int,
    ) -> None:
        """Update editable recruitment fields.

        Args:
            recruitment_id: Recruitment primary key.
            guild_id: Discord guild snowflake used as an update boundary.
            title: Updated recruitment title.
            target: Updated recruitment body.
            max_members: Updated participant capacity; 0 means unlimited.

        Returns:
            None.
        """
        await self.repository.update_details(
            recruitment_id=recruitment_id,
            guild_id=guild_id,
            title=title,
            target=target,
            max_members=max_members,
        )

    async def get_recruitment_by_message_id(
        self, message_id: int
    ) -> RecruitmentRecord | None:
        """Fetch a hydrated recruitment by Discord message ID.

        Args:
            message_id: Discord message snowflake.

        Returns:
            Hydrated recruitment aggregate when found; otherwise None.
        """
        row = await self.repository.fetch_by_message_id(message_id)
        return await self._hydrate_recruitment(row)

    async def get_recruitment_by_id(
        self, recruitment_id: int
    ) -> RecruitmentRecord | None:
        """Fetch a hydrated recruitment by primary key.

        Args:
            recruitment_id: Recruitment primary key.

        Returns:
            Hydrated recruitment aggregate when found; otherwise None.
        """
        row = await self.repository.fetch_by_id(recruitment_id)
        return await self._hydrate_recruitment(row)

    async def get_participant(
        self, recruitment_id: int, user_id: int
    ) -> dict[str, object] | None:
        """Fetch a participant projection from a recruitment.

        Args:
            recruitment_id: Recruitment primary key.
            user_id: Discord user snowflake.

        Returns:
            Participant projection when found; otherwise None.
        """
        recruitment = await self.get_recruitment_by_id(recruitment_id)
        if recruitment is None:
            return None
        for participant in recruitment["participants"]:
            if int(participant["user_id"]) == int(user_id):
                return dict(participant)
        return None

    async def save_participant_status(
        self,
        recruitment_id: int,
        user_id: int,
        status: ParticipantStatus,
        *,
        application_reason: str | None = None,
        rejection_reason: str | None = None,
    ) -> None:
        """Persist a participant status while preserving the owner invariant.

        Args:
            recruitment_id: Recruitment primary key.
            user_id: Discord user snowflake.
            status: Desired participant lifecycle status.
            application_reason: Optional applicant-provided reason to store.
            rejection_reason: Optional owner/system rejection reason to store.

        Returns:
            None.

        Raises:
            RecruitmentNotFoundError: When the recruitment does not exist.
        """
        author_context = await self.repository.fetch_author_context(recruitment_id)
        if author_context is None:
            raise RecruitmentNotFoundError(recruitment_id)

        effective_status = status
        if int(author_context["author_id"]) == int(user_id):
            effective_status = PARTICIPANT_OWNER
            application_reason = None
            rejection_reason = None

        await self.repository.upsert_participant_status(
            recruitment_id=recruitment_id,
            user_id=user_id,
            status=effective_status,
            application_reason=application_reason,
            rejection_reason=rejection_reason,
            updated_at=now_utc_iso(),
        )

    async def is_owner(self, recruitment_id: int, user_id: int) -> bool:
        """Return whether a user owns the recruitment.

        Args:
            recruitment_id: Recruitment primary key.
            user_id: Discord user snowflake.

        Returns:
            True when the participant is the recruitment owner; otherwise False.
        """
        participant = await self.get_participant(recruitment_id, user_id)
        return participant is not None and participant["status"] == PARTICIPANT_OWNER

    async def get_participant_user_ids(
        self, recruitment_id: int, status: ParticipantStatus
    ) -> list[int]:
        """Fetch user IDs for participants in a specific state.

        Args:
            recruitment_id: Recruitment primary key.
            status: Participant status to filter by.

        Returns:
            Discord user snowflakes for matching participants.
        """
        recruitment = await self.get_recruitment_by_id(recruitment_id)
        if recruitment is None:
            return []
        return [
            int(row["user_id"])
            for row in recruitment["participants"]
            if row["status"] == status
        ]

    async def get_confirmed_participants(
        self, recruitment_id: int
    ) -> list[dict[str, object]]:
        """Fetch owner and accepted participants for display.

        Args:
            recruitment_id: Recruitment primary key.

        Returns:
            Ordered participant projections for owner and accepted users.
        """
        author_context = await self.repository.fetch_author_context(recruitment_id)
        if author_context is None:
            return []

        await self.ensure_owner_participant(
            recruitment_id,
            int(author_context["author_id"]),
            str(author_context["created_at"]),
        )
        participants = await self.repository.fetch_confirmed_participants(
            recruitment_id, PARTICIPANT_OWNER, PARTICIPANT_ACCEPTED
        )
        return [dict(row) for row in participants]

    async def get_confirmed_participant_user_ids(
        self, recruitment_id: int
    ) -> list[int]:
        """Fetch user IDs for confirmed recruitment participants.

        Args:
            recruitment_id: Recruitment primary key.

        Returns:
            Discord user snowflakes for owner and accepted participants.
        """
        return [
            int(row["user_id"])
            for row in await self.get_confirmed_participants(recruitment_id)
        ]

    async def get_pending_participant_count(self, recruitment_id: int) -> int:
        """Count pending applications for a recruitment.

        Args:
            recruitment_id: Recruitment primary key.

        Returns:
            Number of pending participant rows.
        """
        return await self.repository.count_participants_by_status(
            recruitment_id, PARTICIPANT_PENDING
        )

    async def get_pending_participants(
        self, recruitment_id: int
    ) -> list[dict[str, object]]:
        """Fetch pending application projections capped for Discord select menus.

        Args:
            recruitment_id: Recruitment primary key.

        Returns:
            Up to 25 pending participant projections.
        """
        recruitment = await self.get_recruitment_by_id(recruitment_id)
        if recruitment is None:
            return []
        pending_rows = [
            dict(row)
            for row in recruitment["participants"]
            if row["status"] == PARTICIPANT_PENDING
        ]
        return pending_rows[:25]

    async def ensure_owner_participant(
        self, recruitment_id: int, author_id: int, updated_at: str | None = None
    ) -> None:
        """Ensure the recruitment owner exists as an owner participant.

        Args:
            recruitment_id: Recruitment primary key.
            author_id: Discord user snowflake of the recruitment owner.
            updated_at: Optional UTC ISO timestamp for the owner participant row.

        Returns:
            None.
        """
        await self.repository.ensure_owner_participant(
            recruitment_id, author_id, updated_at or now_utc_iso()
        )

    async def _hydrate_recruitment(
        self, row: RecruitmentRow | None
    ) -> RecruitmentRecord | None:
        """Hydrate a recruitment row with invariant-checked participants.

        Args:
            row: Recruitment projection returned by the repository.

        Returns:
            Hydrated recruitment aggregate when a row is provided; otherwise None.
        """
        if row is None:
            return None

        await self.ensure_owner_participant(
            int(row["id"]),
            int(row["author_id"]),
            str(row["created_at"]),
        )
        record = cast(RecruitmentRecord, dict(row))
        record["participants"] = await self.repository.fetch_participants(
            int(row["id"])
        )
        return record
