from __future__ import annotations

import asyncio
import logging
import re

import aiohttp
from bs4 import BeautifulSoup


MAX_SCRAPED_TEXT_LENGTH = 5000
URL_PATTERN = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
URL_TRAILING_PUNCTUATION = ".,;:!?)]}>'\""
CONVERSATIONAL_INPUT_INSTRUCTION = (
    "사용자가 제공한 정보는 정형화된 문서가 아니라 자유로운 대화나 구어체 텍스트일 수 있다. "
    "텍스트에 포함된 사용자의 맥락, 의도, 어조(예: '팀 0x34 화이팅', '빡세게 하실 분만')를 정확히 파악하여 "
    "결과물(Embed 제목, 본문, 요약 등)에 자연스럽게 녹여내라."
)


class ScrapingError(RuntimeError):
    """URL 크롤링 실패를 AI 생성 명령어의 사용자 안내로 바꾸기 위한 예외입니다."""


def trim_text(value: str, limit: int) -> str:
    """Discord와 Gemini 입력 제한을 넘지 않도록 긴 문자열을 안전하게 자릅니다."""
    text = value.strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3].rstrip()}..."


def extract_urls(value: str) -> list[str]:
    """대화형 target_info 안에 섞여 있는 모든 URL을 입력 순서대로 추출합니다."""
    urls: list[str] = []
    for match in re.findall(URL_PATTERN, value):
        url = match.rstrip(URL_TRAILING_PUNCTUATION)
        if url and url not in urls:
            urls.append(url)
    return urls


def remove_urls_for_plain_text(value: str, urls: list[str]) -> str:
    """모든 URL을 뺀 나머지 사용자 대화형 텍스트가 있는지 확인합니다."""
    text = value
    for url in urls:
        text = text.replace(url, " ")
    return re.sub(r"[\s.,;:!?()[\]{}'\"<>]+", " ", text).strip()


def replace_urls_with_scraped_text(value: str, scraped_by_url: dict[str, str], failed_urls: set[str], max_length: int) -> str:
    """원문 속 URL 위치에 크롤링한 텍스트를 끼워 넣고, URL 밖의 구어체 요청은 그대로 보존합니다."""
    pieces: list[str] = []
    cursor = 0

    for match in URL_PATTERN.finditer(value):
        raw_url = match.group(0)
        url = raw_url.rstrip(URL_TRAILING_PUNCTUATION)
        start = match.start()

        pieces.append(value[cursor:start])
        if url in scraped_by_url:
            pieces.append(f"\n\n[크롤링한 URL: {url}]\n{scraped_by_url[url]}\n\n")
        elif url in failed_urls:
            pieces.append(f"\n\n[크롤링 실패 URL: {url}]\n웹페이지 내용을 불러오지 못했습니다.\n\n")
        else:
            pieces.append(url)
        cursor = match.end()

    pieces.append(value[cursor:])
    merged_text = "".join(pieces)
    merged_text = re.sub(r"[ \t\r\f\v]+", " ", merged_text)
    merged_text = re.sub(r" *\n *", "\n", merged_text)
    merged_text = re.sub(r"\n{3,}", "\n\n", merged_text).strip()
    return trim_text(merged_text, max_length)


def normalize_scraped_text(value: str) -> str:
    """HTML에서 추출한 텍스트의 반복 공백과 줄바꿈을 줄여 토큰 낭비를 막습니다."""
    text = re.sub(r"\s+", " ", value).strip()
    return trim_text(text, MAX_SCRAPED_TEXT_LENGTH)


async def extract_text_from_url(url: str) -> str:
    """aiohttp와 BeautifulSoup으로 웹페이지의 본문 텍스트만 추출합니다."""
    timeout = aiohttp.ClientTimeout(total=10)
    headers = {
        "User-Agent": "Team0x34Bot/1.0 (+https://discord.com)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, allow_redirects=True) as response:
                if response.status >= 400:
                    raise ScrapingError(f"HTTP {response.status}")
                html = await response.text(errors="ignore")
    except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as exc:
        raise ScrapingError(str(exc)) from exc

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    scraped_text = normalize_scraped_text(soup.get_text(separator=" "))
    if not scraped_text:
        raise ScrapingError("empty page text")
    return scraped_text


async def prepare_conversational_source_text(target_info: str, *, max_length: int, logger: logging.Logger | None = None) -> str:
    """사용자의 구어체 원문과 원문 속 URL 크롤링 결과를 Gemini 입력용 텍스트로 합칩니다."""
    source_text = target_info.strip()
    urls = extract_urls(source_text)
    if not urls:
        return trim_text(source_text, max_length)

    scraping_results = await asyncio.gather(
        *(extract_text_from_url(url) for url in urls),
        return_exceptions=True,
    )

    scraped_by_url: dict[str, str] = {}
    failed_urls: set[str] = set()
    for url, result in zip(urls, scraping_results):
        if isinstance(result, Exception):
            failed_urls.add(url)
            if logger is not None:
                logger.info("Failed to scrape conversational URL %s: %s", url, result)
            continue
        scraped_by_url[url] = result

    if not scraped_by_url and not remove_urls_for_plain_text(source_text, urls):
        raise ScrapingError("all URLs failed")

    return replace_urls_with_scraped_text(source_text, scraped_by_url, failed_urls, max_length)