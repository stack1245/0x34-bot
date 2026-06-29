from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

import google.generativeai as genai


@dataclass(frozen=True)
class AIRequest:
    """Provider-neutral AI generation request."""

    prompt: str
    system_instruction: str
    response_mime_type: str = "text/plain"
    temperature: float = 0.2


@dataclass(frozen=True)
class AIResponse:
    """Provider-neutral AI generation response."""

    text: str
    model: str


class AIProvider(Protocol):
    """Contract implemented by Gemini, future Gemini Pro, or other providers."""

    async def generate(self, request: AIRequest) -> AIResponse:
        ...


class GeminiProvider:
    """Thin Gemini adapter kept outside cogs so model changes happen in one place."""

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
        return AIResponse(text=str(getattr(response, "text", "") or ""), model=self.model_name)

    async def generate(self, request: AIRequest) -> AIResponse:
        return await asyncio.to_thread(self._generate_sync, request)


class DisabledAIProvider:
    """Provider used when no API key is configured."""

    async def generate(self, request: AIRequest) -> AIResponse:
        raise RuntimeError("GEMINI_API_KEY가 설정되어 있지 않습니다. .env 또는 Railway Variables에 추가해 주세요.")


def build_ai_provider(*, api_key: str | None, model_name: str) -> AIProvider:
    if api_key is None:
        return DisabledAIProvider()
    return GeminiProvider(api_key=api_key, model_name=model_name)
