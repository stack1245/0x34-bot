"""DB 접근 계약과 구현체를 담는 저장소 계층입니다."""

from repositories.recruitment import (
    RecruitmentRepositoryProtocol,
    SQLiteRecruitmentRepository,
)

__all__ = ["RecruitmentRepositoryProtocol", "SQLiteRecruitmentRepository"]
