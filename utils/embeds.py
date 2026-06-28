from __future__ import annotations

import discord


BRAND_COLOR = discord.Color.from_rgb(52, 152, 219)
SUCCESS_COLOR = discord.Color.from_rgb(46, 204, 113)
WARNING_COLOR = discord.Color.from_rgb(241, 196, 15)
STOP_COLOR = discord.Color.from_rgb(231, 76, 60)


def base_embed(title: str, description: str | None = None, *, color: discord.Color = BRAND_COLOR) -> discord.Embed:
    """Team 0x34 봇에서 공통으로 쓰는 Embed 기본 스타일입니다."""
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text="Team 0x34")
    return embed


def mention_list(user_ids: list[int]) -> str:
    """유저 ID 목록을 멘션 문자열로 바꾸고, 비어 있으면 보기 좋은 대체 문구를 반환합니다."""
    if not user_ids:
        return "아직 없음"
    return "\n".join(f"<@{user_id}>" for user_id in user_ids)