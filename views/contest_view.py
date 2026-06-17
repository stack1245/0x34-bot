from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlparse

import discord

from core.config import MAX_LINK_BUTTONS

logger = logging.getLogger(__name__)

MARKDOWN_LINK_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\[(?P<label>[^\]]+)\]\((?P<url>https?://[^\s)]+)\)",
    re.IGNORECASE,
)
RAW_URL_PATTERN: Final[re.Pattern[str]] = re.compile(r"https?://[^\s)]+", re.IGNORECASE)


@dataclass(frozen=True)
class ContestLink:
    label: str
    url: str


class ContestLinkView(discord.ui.View):
    def __init__(self, body_text: str) -> None:
        super().__init__(timeout=None)
        self.links = self.extract_links(body_text)
        self.has_link = bool(self.links)

        for link in self.links:
            self.add_item(discord.ui.Button(label=link.label, url=link.url))

        logger.debug("ContestLinkView initialized. buttons=%s", len(self.links))

    @classmethod
    def extract_links(cls, body_text: str) -> list[ContestLink]:
        normalized_text = body_text.strip()
        seen_urls: set[str] = set()
        collected_links: list[ContestLink] = []

        for match in MARKDOWN_LINK_PATTERN.finditer(normalized_text):
            label = cls._normalize_label(match.group("label"), len(collected_links) + 1)
            url = match.group("url").rstrip(",.;")
            if url in seen_urls:
                continue

            seen_urls.add(url)
            collected_links.append(ContestLink(label=label, url=url))

            if len(collected_links) >= MAX_LINK_BUTTONS:
                logger.info("Link extraction hit button limit with markdown links.")
                return collected_links

        markdown_free_text = MARKDOWN_LINK_PATTERN.sub("", normalized_text)
        for index, match in enumerate(
            RAW_URL_PATTERN.finditer(markdown_free_text), start=len(collected_links) + 1
        ):
            url = match.group(0).rstrip(",.;")
            if url in seen_urls:
                continue

            seen_urls.add(url)
            collected_links.append(
                ContestLink(label=cls._default_label(url, index), url=url)
            )

            if len(collected_links) >= MAX_LINK_BUTTONS:
                logger.info("Link extraction hit button limit with raw URLs.")
                break

        logger.debug(
            "Extracted links from contest body. count=%s", len(collected_links)
        )
        return collected_links

    @staticmethod
    def _normalize_label(label: str, index: int) -> str:
        compact_label = " ".join(label.split()).strip()
        if compact_label:
            return compact_label[:80]

        return f"대회 링크 {index}"

    @staticmethod
    def _default_label(url: str, index: int) -> str:
        parsed = urlparse(url)
        hostname = parsed.netloc.replace("www.", "").strip()
        if hostname:
            return f"대회 링크 | {hostname}"[:80]

        return f"대회 링크 {index}"
