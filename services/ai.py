from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

import google.generativeai as genai


@dataclass(frozen=True)
class AIRequest:
    """AI 제공자와 무관한 생성 요청입니다."""

    prompt: str
    system_instruction: str
    response_mime_type: str = "text/plain"
    temperature: float = 0.2


@dataclass(frozen=True)
class AIResponse:
    """AI 제공자와 무관한 생성 응답입니다."""

    text: str
    model: str


class AIProvider(Protocol):
    """Gemini와 다른 AI 제공자가 구현하는 생성 계약입니다."""

    async def generate(self, request: AIRequest) -> AIResponse: ...


class GeminiProvider:
    """Gemini SDK 호출을 Cog 밖으로 격리하는 어댑터입니다."""

    def __init__(self, *, api_key: str, model_name: str) -> None:
        self.api_key = api_key
        self.model_name = model_name

    def _generate_sync(self, request: AIRequest) -> AIResponse:
        genai.configure(api_key=self.api_key)
        model = genai.GenerativeModel(
            model_name=self.model_name,
            system_instruction=request.system_instruction,
        )
        response = model.generate_content(
            request.prompt,
            generation_config={
                "temperature": request.temperature,
                "response_mime_type": request.response_mime_type,
            },
        )
        return AIResponse(
            text=str(getattr(response, "text", "") or ""), model=self.model_name
        )

    async def generate(self, request: AIRequest) -> AIResponse:
        return await asyncio.to_thread(self._generate_sync, request)


class DisabledAIProvider:
    """API 키가 없을 때 명확한 설정 오류를 내는 제공자입니다."""

    async def generate(self, request: AIRequest) -> AIResponse:
        raise RuntimeError(
            "GEMINI_API_KEY가 설정되어 있지 않습니다. .env 또는 Railway Variables에 추가해 주세요."
        )


def build_ai_provider(*, api_key: str | None, model_name: str) -> AIProvider:
    if api_key is None:
        return DisabledAIProvider()
    return GeminiProvider(api_key=api_key, model_name=model_name)
