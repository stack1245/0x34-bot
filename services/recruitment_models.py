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
    """Immutable command payload for creating a recruitment post.

    Args:
        guild_id: Discord guild snowflake where the recruitment belongs.
        channel_id: Discord channel snowflake containing the public recruitment message.
        message_id: Discord message snowflake for the public recruitment embed.
        author_id: Discord user snowflake of the recruitment owner.
        title: Public recruitment title.
        target: Public recruitment body or target description.
        max_members: Maximum confirmed participant count; 0 means unlimited.
        thread_id: Optional private workspace thread snowflake.
    """

    guild_id: int
    channel_id: int
    message_id: int
    author_id: int
    title: str
    target: str
    max_members: int
    thread_id: int | None = None


class RecruitmentParticipant(TypedDict):
    """Persisted participant projection exposed to services and Discord UI.

    Keys:
        user_id: Discord user snowflake.
        status: Participant lifecycle state.
        application_reason: Applicant-provided reason, if any.
        rejection_reason: Owner-provided or system rejection reason, if any.
        updated_at: UTC ISO timestamp for the latest participant mutation.
    """

    user_id: int
    status: ParticipantStatus
    application_reason: str | None
    rejection_reason: str | None
    updated_at: str


class RecruitmentRow(TypedDict):
    """Recruitment table projection without hydrated participants.

    Keys mirror the SQLite recruitments table and preserve optional Discord thread and close metadata.
    """

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
    """Hydrated recruitment aggregate consumed by higher-level services and cogs.

    Keys:
        participants: Ordered participant projections for this recruitment.
    """

    participants: list[RecruitmentParticipant]


class RecruitmentAuthorContext(TypedDict):
    """Minimal owner context required to enforce owner participant invariants.

    Keys:
        author_id: Discord user snowflake of the recruitment owner.
        created_at: UTC ISO timestamp used for owner participant ordering.
    """

    author_id: int
    created_at: str
