from __future__ import annotations

import json
import logging
import re

from services.ai import AIProvider, AIRequest
from utils.ai_input import (
    CONVERSATIONAL_INPUT_INSTRUCTION,
    prepare_conversational_source_text,
    trim_text as _trim_text,
)
from utils.datetime import get_current_time_context

DEFAULT_AI_RECRUITMENT_CAPACITY = 4
MAX_AI_TITLE_LENGTH = 50
MAX_EMBED_DESCRIPTION_LENGTH = 3900
MAX_RECRUITMENT_SOURCE_TEXT_LENGTH = 12000
SCRAPING_ERROR_MESSAGE = "웹페이지 내용을 불러오지 못했습니다. 사이트 링크 대신 상세 텍스트를 직접 입력해 주세요."
GEMINI_RATE_LIMIT_MESSAGE = (
    "⚠️ 봇이 너무 많은 요청을 처리하고 있습니다. 1분 뒤에 다시 시도해 주세요."
)


GEMINI_SYSTEM_PROMPT = """
주어진 해커톤/대회 웹사이트 텍스트를 분석하여 다음 JSON 스키마에 맞게 결과를 반환해라.
{
    "title": "이모지를 포함한 50자 이내의 모집 제목",
    "description": "마크다운을 활용한 대회 일정, 참가 자격, 주제, 혜택 요약글",
    "max_members": "본문에 명시된 최대 팀원 수 (정수형). 명시되어 있지 않으면 4로 설정"
}
텍스트에 없는 내용은 추측하지 말고, 확인할 수 없는 항목은 "공개된 정보 없음"이라고 적어라.
응답은 반드시 JSON 객체 하나로만 작성해라.
""".strip()


def build_recruitment_system_prompt() -> str:
    """현재 한국 시간과 모집글 JSON 규칙을 Gemini 시스템 프롬프트에 주입합니다."""
    return f"""
{get_current_time_context()}
위 제공된 '현재 시간'을 기준으로 날짜를 계산해라. 본문에 연도가 생략되어 있다면 무조건 현재 연도를 사용하고, 절대로 지나간 과거 연도로 작성하지 마라.
{CONVERSATIONAL_INPUT_INSTRUCTION}

{GEMINI_SYSTEM_PROMPT}
""".strip()


def _strip_json_code_fence(value: str) -> str:
    """Gemini 응답이 코드블록으로 감싸져 있어도 JSON 본문만 꺼냅니다."""
    text = value.strip()
    fence_match = re.search(
        r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL
    )
    if fence_match is None:
        return text
    return fence_match.group(1).strip()


def _clean_title(value: str) -> str:
    """AI가 붙일 수 있는 제목 마크다운과 접두어를 정리합니다."""
    title = value.strip()
    title = re.sub(r"^#+\s*", "", title)
    title = re.sub(
        r"^\*{0,2}(제목|title)\*{0,2}\s*[:：]\s*", "", title, flags=re.IGNORECASE
    )
    return _trim_text(title, MAX_AI_TITLE_LENGTH)


def _parse_max_members(value: object) -> int:
    """AI가 반환한 정원 값을 음수가 아닌 정수로 보정합니다."""
    try:
        if isinstance(value, bool):
            raise ValueError("bool is not a valid max_members")
        if isinstance(value, int):
            parsed = value
        elif isinstance(value, float):
            parsed = int(value)
        elif isinstance(value, str):
            match = re.search(r"\d+", value)
            if match is None:
                raise ValueError("no integer in max_members string")
            parsed = int(match.group(0))
        else:
            raise ValueError("unsupported max_members type")
    except (TypeError, ValueError):
        return DEFAULT_AI_RECRUITMENT_CAPACITY

    if parsed < 0:
        return DEFAULT_AI_RECRUITMENT_CAPACITY
    return parsed


def parse_gemini_recruitment(
    raw_text: str, fallback_source: str
) -> tuple[str, str, int]:
    """Gemini 응답을 모집 Embed에 필요한 제목, 설명, 정원으로 변환합니다."""
    cleaned = _strip_json_code_fence(raw_text)
    title = ""
    description = ""
    max_members = DEFAULT_AI_RECRUITMENT_CAPACITY

    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        object_match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if object_match is not None:
            try:
                payload = json.loads(object_match.group(0))
            except json.JSONDecodeError:
                payload = None
        else:
            payload = None

    if isinstance(payload, dict):
        title = str(payload.get("title", ""))
        description = str(payload.get("description", ""))
        max_members = _parse_max_members(
            payload.get("max_members", DEFAULT_AI_RECRUITMENT_CAPACITY)
        )

    if not title or not description:
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        if lines:
            title = title or lines[0]
            description = description or "\n".join(lines[1:])

    title = _clean_title(title) or "🚀 Team 0x34 모집"
    description = description.strip() or f"**대상 정보**\n- {fallback_source}"
    return title, _trim_text(description, MAX_EMBED_DESCRIPTION_LENGTH), max_members


class RecruitmentCopyService:
    """AI 모집글 생성과 입력 전처리를 담당합니다."""

    def __init__(
        self, ai_provider: AIProvider, *, logger: logging.Logger | None = None
    ) -> None:
        self.ai_provider = ai_provider
        self.logger = logger or logging.getLogger(__name__)

    async def generate_copy_text(self, source_text: str) -> str:
        response = await self.ai_provider.generate(
            AIRequest(
                system_instruction=build_recruitment_system_prompt(),
                response_mime_type="application/json",
                temperature=0.4,
                prompt=(
                    "다음은 사용자가 자유롭게 제공한 대화형 입력과 URL 크롤링 내용을 합친 원문입니다. "
                    "사용자의 요청 의도와 어조를 유지하면서 모집글을 작성해라: "
                    f"\n\n{source_text}\n\n"
                    "이 텍스트 내용만을 엄격하게 바탕으로, 없는 내용을 지어내지 말고 다음 규칙에 따라 모집글을 작성해라."
                ),
            )
        )
        return response.text

    async def generate_copy(self, source_text: str) -> tuple[str, str, int]:
        raw_text = await self.generate_copy_text(source_text)
        return parse_gemini_recruitment(raw_text, source_text)

    async def prepare_source_text(self, target_info: str) -> str:
        return await prepare_conversational_source_text(
            target_info,
            max_length=MAX_RECRUITMENT_SOURCE_TEXT_LENGTH,
            logger=self.logger,
        )
