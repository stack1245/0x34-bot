from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict

RecruitmentStatus = Literal["open", "closed"]
ParticipantStatus = Literal["pending", "accepted", "rejected", "owner"]

STATUS_OPEN: RecruitmentStatus = "open"
STATUS_CLOSED: RecruitmentStatus = "closed"
PARTICIPANT_PENDING: ParticipantStatus = "pending"
PARTICIPANT_ACCEPTED: ParticipantStatus = "accepted"
PARTICIPANT_REJECTED: ParticipantStatus = "rejected"
PARTICIPANT_OWNER: ParticipantStatus = "owner"


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


class RecruitmentParticipant(TypedDict):
    user_id: int
    status: ParticipantStatus
    application_reason: str | None
    rejection_reason: str | None
    updated_at: str


class RecruitmentRow(TypedDict):
    id: int
    guild_id: int
    channel_id: int
    message_id: int
    author_id: int
    title: str
    target: str
    max_members: int
    status: RecruitmentStatus
    thread_id: int | None
    created_at: str
    closed_at: str | None


class RecruitmentRecord(RecruitmentRow):
    participants: list[RecruitmentParticipant]


class RecruitmentAuthorContext(TypedDict):
    author_id: int
    created_at: str
