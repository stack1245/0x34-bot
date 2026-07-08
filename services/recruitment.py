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
    """모집 도메인 예외의 기준 타입입니다."""


class RecruitmentNotFoundError(RecruitmentError):
    """존재하지 않는 모집을 변경하려 할 때 발생합니다."""

    def __init__(self, recruitment_id: int) -> None:
        super().__init__(f"Recruitment not found: {recruitment_id}")
        self.recruitment_id = recruitment_id


class RecruitmentService(BaseService):
    """모집 도메인 규칙을 저장소 계약 위에서 실행합니다."""

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
        timestamp = now_utc_iso()
        return await self.repository.create_with_owner(
            request,
            status=STATUS_OPEN,
            owner_status=PARTICIPANT_OWNER,
            timestamp=timestamp,
        )

    async def update_thread_id(self, recruitment_id: int, thread_id: int) -> None:
        await self.repository.update_thread_id(recruitment_id, thread_id)

    async def close_recruitment(self, recruitment_id: int) -> None:
        await self.repository.set_status(recruitment_id, STATUS_CLOSED, now_utc_iso())

    async def reopen_recruitment(self, recruitment_id: int) -> None:
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
        row = await self.repository.fetch_by_message_id(message_id)
        return await self._hydrate_recruitment(row)

    async def get_recruitment_by_id(
        self, recruitment_id: int
    ) -> RecruitmentRecord | None:
        row = await self.repository.fetch_by_id(recruitment_id)
        return await self._hydrate_recruitment(row)

    async def get_participant(
        self, recruitment_id: int, user_id: int
    ) -> dict[str, object] | None:
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
        """작성자는 어떤 상태 변경 요청에서도 owner 상태를 유지합니다."""
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
        participant = await self.get_participant(recruitment_id, user_id)
        return participant is not None and participant["status"] == PARTICIPANT_OWNER

    async def get_participant_user_ids(
        self, recruitment_id: int, status: ParticipantStatus
    ) -> list[int]:
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
        return [
            int(row["user_id"])
            for row in await self.get_confirmed_participants(recruitment_id)
        ]

    async def get_pending_participant_count(self, recruitment_id: int) -> int:
        return await self.repository.count_participants_by_status(
            recruitment_id, PARTICIPANT_PENDING
        )

    async def get_pending_participants(
        self, recruitment_id: int
    ) -> list[dict[str, object]]:
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
        await self.repository.ensure_owner_participant(
            recruitment_id, author_id, updated_at or now_utc_iso()
        )

    async def _hydrate_recruitment(
        self, row: RecruitmentRow | None
    ) -> RecruitmentRecord | None:
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
