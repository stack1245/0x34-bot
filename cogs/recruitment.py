from __future__ import annotations

import asyncio
import json
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands
import google.generativeai as genai

from utils.ai_input import (
    CONVERSATIONAL_INPUT_INSTRUCTION,
    ScrapingError,
    prepare_conversational_source_text,
    trim_text as _trim_text,
)
from utils.datetime import get_current_time_context, now_utc_iso
from utils.embeds import STOP_COLOR, SUCCESS_COLOR, WARNING_COLOR, base_embed, mention_list


STATE_JOIN = "join"
STATE_DECLINE = "decline"
STATE_WAIT = "wait"
STATUS_OPEN = "open"
STATUS_CLOSED = "closed"
DEFAULT_AI_RECRUITMENT_CAPACITY = 4
MAX_AI_TITLE_LENGTH = 50
MAX_EMBED_DESCRIPTION_LENGTH = 3900
MAX_RECRUITMENT_SOURCE_TEXT_LENGTH = 12000
SCRAPING_ERROR_MESSAGE = "웹페이지 내용을 불러오지 못했습니다. 사이트 링크 대신 상세 텍스트를 직접 입력해 주세요."


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
    """Gemini 모집 글 생성에 현재 한국 시간과 날짜 규칙을 주입합니다."""
    return f"""
{get_current_time_context()}
위 제공된 '현재 시간'을 기준으로 날짜를 계산해라. 본문에 연도가 생략되어 있다면 무조건 현재 연도를 사용하고, 절대로 지나간 과거 연도로 작성하지 마라.
{CONVERSATIONAL_INPUT_INSTRUCTION}

{GEMINI_SYSTEM_PROMPT}
""".strip()


def _strip_json_code_fence(value: str) -> str:
    """Gemini가 ```json 코드블록으로 감싸서 답해도 JSON만 꺼낼 수 있게 합니다."""
    text = value.strip()
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fence_match is None:
        return text
    return fence_match.group(1).strip()


def _clean_title(value: str) -> str:
    """Markdown 제목 기호나 `제목:` 접두어를 제거하고 50자 제한을 맞춥니다."""
    title = value.strip()
    title = re.sub(r"^#+\s*", "", title)
    title = re.sub(r"^\*{0,2}(제목|title)\*{0,2}\s*[:：]\s*", "", title, flags=re.IGNORECASE)
    return _trim_text(title, MAX_AI_TITLE_LENGTH)


def _parse_max_members(value: object) -> int:
    """Gemini JSON의 max_members를 안전하게 정수로 바꾸고 실패하면 기본값 4를 사용합니다."""
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


def parse_gemini_recruitment(raw_text: str, fallback_source: str) -> tuple[str, str, int]:
    """Gemini 응답을 모집 Embed에 넣을 제목과 본문으로 변환합니다.

    모델에는 JSON만 반환하라고 지시하지만, 실제 LLM 응답은 코드블록이나 일반 텍스트가 섞일 수 있습니다.
    그래서 json.loads를 먼저 시도하고, 실패하면 첫 줄을 제목, 나머지를 본문으로 쓰는 fallback을 둡니다.
    max_members는 JSON 파싱 실패나 값 오류가 있어도 기본값 4로 안전하게 보정합니다.
    """
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
        max_members = _parse_max_members(payload.get("max_members", DEFAULT_AI_RECRUITMENT_CAPACITY))

    if not title or not description:
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        if lines:
            title = title or lines[0]
            description = description or "\n".join(lines[1:])

    title = _clean_title(title) or "🚀 Team 0x34 모집"
    description = description.strip() or f"**대상 정보**\n- {fallback_source}"
    return title, _trim_text(description, MAX_EMBED_DESCRIPTION_LENGTH), max_members


class RecruitmentView(discord.ui.View):
    """모집 메시지 아래에 붙는 Persistent Button View입니다."""

    def __init__(self, cog: "RecruitmentCog") -> None:
        # timeout=None과 고정 custom_id를 쓰면 봇 재시작 후에도 버튼 이벤트를 받을 수 있습니다.
        super().__init__(timeout=None)
        self.cog = cog

    async def handle_vote(self, interaction: discord.Interaction, state: str) -> None:
        """참가/불참/대기 버튼의 공통 처리 로직입니다."""
        if interaction.message is None:
            await interaction.response.send_message("모집 메시지를 찾을 수 없습니다.", ephemeral=True)
            return

        recruitment = await self.cog.get_recruitment(interaction.message.id)
        if recruitment is None:
            await interaction.response.send_message("DB에서 모집 정보를 찾을 수 없습니다.", ephemeral=True)
            return
        if recruitment["status"] == STATUS_CLOSED:
            await interaction.response.send_message("이미 마감된 모집입니다.", ephemeral=True)
            return

        if state == STATE_JOIN and recruitment["max_members"] > 0:
            joined = await self.cog.get_vote_user_ids(recruitment["id"], STATE_JOIN)
            already_joined = interaction.user.id in joined
            if not already_joined and len(joined) >= recruitment["max_members"]:
                await interaction.response.send_message("정원이 찼습니다. 필요하면 [대기]를 눌러 주세요.", ephemeral=True)
                return

        await self.cog.bot.database.execute(
            """
            INSERT INTO recruitment_votes (recruitment_id, user_id, state, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(recruitment_id, user_id)
            DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at
            """,
            (recruitment["id"], interaction.user.id, state, now_utc_iso()),
        )

        thread_notice = await self.cog.sync_private_thread_membership(recruitment, interaction.user, state)
        embed = await self.cog.build_recruitment_embed(recruitment["message_id"])
        await interaction.message.edit(embed=embed, view=self)

        response = "응답이 반영되었습니다."
        if thread_notice:
            response += f"\n{thread_notice}"
        await interaction.response.send_message(response, ephemeral=True)

    @discord.ui.button(label="참가", style=discord.ButtonStyle.success, custom_id="0x34:recruitment:join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """참가 버튼을 눌렀을 때 호출됩니다."""
        await self.handle_vote(interaction, STATE_JOIN)

    @discord.ui.button(label="불참", style=discord.ButtonStyle.danger, custom_id="0x34:recruitment:decline")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """불참 버튼을 눌렀을 때 호출됩니다."""
        await self.handle_vote(interaction, STATE_DECLINE)

    @discord.ui.button(label="대기", style=discord.ButtonStyle.secondary, custom_id="0x34:recruitment:wait")
    async def wait(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """대기 버튼을 눌렀을 때 호출됩니다."""
        await self.handle_vote(interaction, STATE_WAIT)

    @discord.ui.button(label="모집 마감", style=discord.ButtonStyle.primary, custom_id="0x34:recruitment:close")
    async def close_recruitment(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """작성자가 모집을 마감하고 참가자 멘션 및 스레드 생성을 시도합니다."""
        if interaction.message is None:
            await interaction.response.send_message("모집 메시지를 찾을 수 없습니다.", ephemeral=True)
            return

        recruitment = await self.cog.get_recruitment(interaction.message.id)
        if recruitment is None:
            await interaction.response.send_message("DB에서 모집 정보를 찾을 수 없습니다.", ephemeral=True)
            return
        if interaction.user.id != recruitment["author_id"]:
            await interaction.response.send_message("모집 작성자만 마감할 수 있습니다.", ephemeral=True)
            return
        if recruitment["status"] == STATUS_CLOSED:
            await interaction.response.send_message("이미 마감된 모집입니다.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.bot.database.execute(
            "UPDATE recruitments SET status = ?, closed_at = ? WHERE id = ?",
            (STATUS_CLOSED, now_utc_iso(), recruitment["id"]),
        )

        embed = await self.cog.build_recruitment_embed(recruitment["message_id"])
        await interaction.message.edit(embed=embed, view=self)

        thread, thread_notice = await self.cog.ensure_private_workspace_thread(recruitment, interaction, interaction.message)
        joined = await self.cog.get_vote_user_ids(recruitment["id"], STATE_JOIN)
        mentions = " ".join(f"<@{user_id}>" for user_id in joined) or "참가자가 없습니다."

        if thread is None:
            await interaction.followup.send(
                "모집은 마감했지만 비공개 스레드를 사용할 수 없어 참가자 멘션은 공개 채널에 보내지 않았습니다.\n"
                f"{thread_notice or '채널 권한과 서버 부스트 레벨을 확인해 주세요.'}",
                ephemeral=True,
            )
            return

        for user_id in joined:
            await self.cog.add_user_to_private_thread(thread, discord.Object(id=user_id))

        await thread.send(f"모집이 마감되었습니다.\n참가 인원: {mentions}")
        await interaction.followup.send(f"모집을 마감하고 비공개 워크스페이스에 참가자를 안내했습니다: {thread.mention}", ephemeral=True)


class RecruitmentModal(discord.ui.Modal, title="팀원 모집"):
    """모집 Embed를 만들기 위한 정보를 입력받는 Modal입니다."""

    title_input = discord.ui.TextInput(label="모집 제목", placeholder="예: DEF CON Quals 팀원 모집", max_length=100)
    target_input = discord.ui.TextInput(
        label="대회/프로젝트 설명",
        placeholder="모집 목적, 기간, 필요한 역할 등을 적어 주세요.",
        style=discord.TextStyle.long,
        max_length=1000,
    )
    max_members_input = discord.ui.TextInput(label="정원", placeholder="예: 4", default="4", max_length=3)

    def __init__(self, cog: "RecruitmentCog") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """모집 정보를 DB에 만들고 버튼이 달린 Embed 메시지를 전송합니다."""
        if interaction.guild is None:
            await interaction.response.send_message("서버 안에서만 모집을 만들 수 있습니다.", ephemeral=True)
            return

        try:
            max_members = int(str(self.max_members_input.value).strip())
        except ValueError:
            await interaction.response.send_message("정원은 숫자로 입력해 주세요.", ephemeral=True)
            return
        if max_members < 0:
            await interaction.response.send_message("정원은 0 이상이어야 합니다. 0은 제한 없음입니다.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        message, _, thread_notice = await self.cog.post_recruitment_message(
            interaction,
            title=str(self.title_input.value),
            target=str(self.target_input.value),
            max_members=max_members,
            use_deferred_response=False,
        )
        response = f"모집 글을 만들었습니다: {message.jump_url}"
        if thread_notice:
            response += f"\n{thread_notice}"
        await interaction.followup.send(response, ephemeral=True)


class RecruitmentEditModal(discord.ui.Modal, title="모집 수정"):
    """기존 모집 데이터를 TextInput default로 채워서 여는 수정 Modal입니다."""

    def __init__(self, cog: "RecruitmentCog", row, source_message: discord.Message | None, user_id: int) -> None:
        super().__init__()
        self.cog = cog
        self.recruitment_id = int(row["id"])
        self.channel_id = int(row["channel_id"])
        self.message_id = int(row["message_id"])
        self.source_message = source_message
        self.user_id = user_id

        self.title_input = discord.ui.TextInput(
            label="모집 제목",
            placeholder="예: DEF CON Quals 팀원 모집",
            default=str(row["title"])[:100],
            max_length=100,
        )
        self.target_input = discord.ui.TextInput(
            label="대회/프로젝트 설명",
            placeholder="모집 목적, 기간, 필요한 역할 등을 적어 주세요.",
            default=_trim_text(str(row["target"]), MAX_EMBED_DESCRIPTION_LENGTH),
            style=discord.TextStyle.long,
            max_length=MAX_EMBED_DESCRIPTION_LENGTH,
        )
        self.max_members_input = discord.ui.TextInput(
            label="정원",
            placeholder="예: 4",
            default=str(row["max_members"])[:6],
            max_length=6,
        )
        self.add_item(self.title_input)
        self.add_item(self.target_input)
        self.add_item(self.max_members_input)

    async def disable_source_view(self) -> None:
        if self.source_message is None:
            return
        try:
            await self.source_message.edit(view=None)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("서버 안에서만 모집을 수정할 수 있습니다.", ephemeral=True)
            return
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("이 모집 수정 창은 명령어를 실행한 사람만 제출할 수 있습니다.", ephemeral=True)
            return

        try:
            max_members = int(str(self.max_members_input.value).strip())
        except ValueError:
            await interaction.response.send_message("정원은 숫자로 입력해 주세요.", ephemeral=True)
            return
        if max_members < 0:
            await interaction.response.send_message("정원은 0 이상이어야 합니다. 0은 제한 없음입니다.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await self.cog.bot.database.fetch_one(
            """
            SELECT * FROM recruitments
            WHERE id = ? AND guild_id = ?
            """,
            (self.recruitment_id, interaction.guild.id),
        )
        if row is None:
            await self.disable_source_view()
            await interaction.followup.send("수정할 모집 글을 찾을 수 없습니다.", ephemeral=True)
            return
        if row["author_id"] != interaction.user.id and row["status"] != STATUS_OPEN:
            await interaction.followup.send("모집 작성자이거나 현재 모집 중인 글만 수정할 수 있습니다.", ephemeral=True)
            return

        title = str(self.title_input.value).strip()
        target = _trim_text(str(self.target_input.value), MAX_EMBED_DESCRIPTION_LENGTH)

        # aiosqlite UPDATE를 먼저 실행해 DB를 최신 상태로 만든 뒤 Embed를 다시 빌드합니다.
        # Database.execute()는 내부에서 commit까지 수행하므로, 아래 build_recruitment_embed()는
        # 방금 저장한 title/target/max_members 값을 같은 DB 연결에서 바로 읽을 수 있습니다.
        await self.cog.bot.database.execute(
            """
            UPDATE recruitments
            SET title = ?, target = ?, max_members = ?
            WHERE id = ? AND guild_id = ?
            """,
            (title, target, max_members, self.recruitment_id, interaction.guild.id),
        )

        message_notice = await self.cog.edit_recruitment_message(self.channel_id, self.message_id)
        await self.disable_source_view()

        response = "✅ 성공적으로 수정되었습니다."
        if message_notice:
            response += f"\n{message_notice}"
        await interaction.followup.send(response, ephemeral=True)


class RecruitmentEditSelect(discord.ui.Select):
    """수정할 모집 글을 선택하는 드롭다운입니다."""

    def __init__(self, cog: "RecruitmentCog", rows: list) -> None:
        self.cog = cog
        options: list[discord.SelectOption] = []
        for row in rows:
            status_text = "모집 중" if row["status"] == STATUS_OPEN else "모집 마감"
            capacity_text = "제한 없음" if row["max_members"] == 0 else f"정원 {row['max_members']}명"
            options.append(
                discord.SelectOption(
                    label=str(row["title"])[:100],
                    description=f"{status_text} · {capacity_text}"[:100],
                    value=str(row["id"]),
                )
            )

        super().__init__(
            placeholder="수정할 모집 글을 선택하세요.",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.edit_message(content="서버 안에서만 모집을 수정할 수 있습니다.", embed=None, view=None)
            return

        recruitment_id = int(self.values[0])
        row = await self.cog.bot.database.fetch_one(
            """
            SELECT * FROM recruitments
            WHERE id = ? AND guild_id = ?
            """,
            (recruitment_id, interaction.guild.id),
        )
        if row is None:
            await interaction.response.edit_message(content="이미 삭제되었거나 찾을 수 없는 모집 글입니다.", embed=None, view=None)
            return
        if row["author_id"] != interaction.user.id and row["status"] != STATUS_OPEN:
            await interaction.response.send_message("모집 작성자이거나 현재 모집 중인 글만 수정할 수 있습니다.", ephemeral=True)
            return

        await interaction.response.send_modal(RecruitmentEditModal(self.cog, row, interaction.message, interaction.user.id))


class RecruitmentEditView(discord.ui.View):
    """모집 수정 Select를 담는 Ephemeral View입니다."""

    def __init__(self, cog: "RecruitmentCog", rows: list, user_id: int) -> None:
        super().__init__(timeout=120)
        self.user_id = user_id
        self.add_item(RecruitmentEditSelect(cog, rows))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("이 모집 수정 메뉴는 명령어를 실행한 사람만 사용할 수 있습니다.", ephemeral=True)
        return False


class RecruitmentCog(commands.Cog):
    """팀 빌딩과 참가 버튼 업데이트를 담당하는 Cog입니다."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.bot.add_view(RecruitmentView(self))

    async def resolve_recruitment_channel(self, interaction: discord.Interaction) -> discord.abc.Messageable:
        """환경 변수에 모집 채널이 있으면 그 채널을, 없으면 현재 채널을 사용합니다."""
        channel_id = self.bot.settings.recruitment_channel_id
        if interaction.guild is not None and channel_id is not None:
            channel = interaction.guild.get_channel(channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
            if isinstance(channel, discord.abc.Messageable):
                return channel

        if interaction.channel is None or not isinstance(interaction.channel, discord.abc.Messageable):
            raise RuntimeError("모집 글을 보낼 채널을 찾을 수 없습니다.")
        return interaction.channel

    async def post_recruitment_message(
        self,
        interaction: discord.Interaction,
        *,
        title: str,
        target: str,
        max_members: int,
        use_deferred_response: bool,
    ) -> tuple[discord.Message, bool, str | None]:
        """DB 저장과 버튼 달린 모집 메시지 생성을 한 곳에서 처리합니다.

        `/모집`과 `/모집생성`이 같은 테이블, 같은 Embed 빌더, 같은 Persistent View를 사용해야
        버튼 클릭 시 참가자 목록이 동일한 방식으로 업데이트됩니다.
        """
        if interaction.guild is None:
            raise RuntimeError("서버 안에서만 모집을 만들 수 있습니다.")

        channel = await self.resolve_recruitment_channel(interaction)
        placeholder = base_embed("모집을 준비하는 중입니다.")
        view = RecruitmentView(self)

        # /모집생성은 이미 공개 defer를 했으므로, 같은 채널이라면 그 deferred 응답 자체를 모집 메시지로 씁니다.
        # 이렇게 하면 Gemini를 기다리는 동안 Discord 3초 제한을 피하면서도 불필요한 안내 메시지를 만들지 않습니다.
        should_use_followup = (
            use_deferred_response
            and interaction.channel is not None
            and getattr(channel, "id", None) == interaction.channel.id
        )
        if should_use_followup:
            message = await interaction.followup.send(embed=placeholder, view=view, wait=True)
        else:
            message = await channel.send(embed=placeholder, view=view)

        cursor = await self.bot.database.execute(
            """
            INSERT INTO recruitments (guild_id, channel_id, message_id, author_id, title, target, max_members, status, thread_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interaction.guild.id,
                message.channel.id,
                message.id,
                interaction.user.id,
                title,
                target,
                max_members,
                STATUS_OPEN,
                None,
                now_utc_iso(),
            ),
        )

        thread, thread_notice = await self.create_private_workspace_thread(interaction, channel, title, message)
        if thread is not None:
            await self.bot.database.execute(
                "UPDATE recruitments SET thread_id = ? WHERE id = ?",
                (thread.id, cursor.lastrowid),
            )

        embed = await self.build_recruitment_embed(message.id)
        await message.edit(embed=embed, view=RecruitmentView(self))
        if thread_notice:
            logging.info("Private recruitment thread notice for message %s: %s", message.id, thread_notice)
        return message, should_use_followup, thread_notice

    async def create_private_workspace_thread(
        self,
        interaction: discord.Interaction,
        channel: discord.abc.Messageable,
        title: str,
        source_message: discord.Message,
    ) -> tuple[discord.Thread | None, str | None]:
        """모집 전용 비공개 스레드를 만들고 작성자를 즉시 초대합니다.

        비공개 스레드를 만들려면 봇이 해당 텍스트 채널에서 `Create Private Threads`와
        `Send Messages in Threads` 권한을 가져야 합니다. 서버 설정이나 채널 오버라이드에 따라
        멤버 초대/아카이브된 스레드 관리를 위해 `Manage Threads` 권한이 추가로 필요할 수 있습니다.
        또한 Discord 정책상 일부 서버 기능은 부스트 레벨 2 이상에서만 안정적으로 사용할 수 있습니다.
        """
        if not isinstance(channel, discord.TextChannel):
            return None, "비공개 스레드는 일반 텍스트 채널에서만 생성할 수 있습니다."

        thread_name = _trim_text(f"{title} 워크스페이스", 90)
        try:
            thread = await channel.create_thread(
                name=thread_name,
                type=discord.ChannelType.private_thread,
                auto_archive_duration=1440,
                invitable=False,
                reason="Team 0x34 private recruitment workspace",
            )
        except discord.Forbidden:
            return None, "봇에게 비공개 스레드 생성 권한이 없습니다. Create Private Threads, Send Messages in Threads, Manage Threads 권한을 확인해 주세요."
        except discord.HTTPException as exc:
            return None, f"비공개 스레드 생성에 실패했습니다. 서버 부스트 레벨 또는 Discord API 제한을 확인해 주세요: {exc.text}"

        author_notice = await self.add_user_to_private_thread(thread, interaction.user)
        intro_embed = base_embed(
            "Team 0x34 비공개 워크스페이스",
            "Team 0x34의 비공개 워크스페이스가 생성되었습니다.",
            color=SUCCESS_COLOR,
        )
        intro_embed.add_field(name="모집 글", value=source_message.jump_url, inline=False)
        intro_embed.add_field(name="접근 안내", value="작성자와 [참가]를 누른 팀원만 이 스레드에 초대됩니다.", inline=False)

        try:
            await thread.send(embed=intro_embed)
        except discord.HTTPException as exc:
            return thread, f"비공개 스레드는 만들었지만 안내 Embed 전송에 실패했습니다: {exc.text}"

        return thread, author_notice

    async def ensure_private_workspace_thread(
        self,
        recruitment,
        interaction: discord.Interaction,
        source_message: discord.Message,
    ) -> tuple[discord.Thread | None, str | None]:
        """기존 비공개 스레드를 찾고, 없으면 새로 만들어 DB에 저장합니다."""
        thread_id = recruitment["thread_id"]
        if thread_id is not None:
            thread = await self.fetch_private_thread(int(thread_id))
            if thread is not None:
                return thread, await self.add_user_to_private_thread(thread, interaction.user)

        thread, notice = await self.create_private_workspace_thread(interaction, source_message.channel, recruitment["title"], source_message)
        if thread is not None:
            await self.bot.database.execute(
                "UPDATE recruitments SET thread_id = ? WHERE id = ?",
                (thread.id, recruitment["id"]),
            )
        return thread, notice

    async def fetch_private_thread(self, thread_id: int) -> discord.Thread | None:
        """저장된 스레드 ID로 Thread 객체를 가져옵니다."""
        channel = self.bot.get_channel(thread_id)
        if isinstance(channel, discord.Thread):
            return channel

        try:
            fetched = await self.bot.fetch_channel(thread_id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return None
        if isinstance(fetched, discord.Thread):
            return fetched
        return None

    async def add_user_to_private_thread(self, thread: discord.Thread, user: discord.abc.Snowflake) -> str | None:
        """비공개 스레드에 사용자를 초대하고 실패 사유를 사용자에게 보여줄 문구로 반환합니다."""
        try:
            await thread.add_user(user)
        except discord.Forbidden:
            return "비공개 스레드 초대 권한이 없어 워크스페이스에 자동 초대하지 못했습니다. Manage Threads 권한을 확인해 주세요."
        except discord.HTTPException as exc:
            return f"비공개 스레드 초대에 실패했습니다: {exc.text}"
        return None

    async def remove_user_from_private_thread(self, thread: discord.Thread, user: discord.abc.Snowflake) -> str | None:
        """참가를 취소한 사용자가 비공개 워크스페이스에 계속 남지 않도록 제거합니다."""
        try:
            await thread.remove_user(user)
        except discord.Forbidden:
            return "비공개 스레드에서 사용자를 제거할 권한이 없습니다. Manage Threads 권한을 확인해 주세요."
        except discord.NotFound:
            return None
        except discord.HTTPException as exc:
            return f"비공개 스레드 멤버 제거에 실패했습니다: {exc.text}"
        return None

    async def sync_private_thread_membership(self, recruitment, user: discord.abc.Snowflake, state: str) -> str | None:
        """참가 버튼 상태와 비공개 스레드 멤버십을 맞춥니다."""
        thread_id = recruitment["thread_id"]
        if thread_id is None:
            return "비공개 워크스페이스가 아직 없어 스레드 초대는 건너뛰었습니다."

        thread = await self.fetch_private_thread(int(thread_id))
        if thread is None:
            return "저장된 비공개 워크스페이스를 찾을 수 없습니다. 작성자가 모집 마감 시 다시 생성할 수 있습니다."

        if state == STATE_JOIN:
            notice = await self.add_user_to_private_thread(thread, user)
            return notice or f"비공개 워크스페이스에 초대했습니다: {thread.mention}"

        if user.id == recruitment["author_id"]:
            return None
        return await self.remove_user_from_private_thread(thread, user)

    def _generate_recruitment_copy_sync(self, source_text: str) -> str:
        """Gemini SDK의 동기 API를 호출합니다.

        google-generativeai의 기본 호출은 네트워크 I/O가 끝날 때까지 현재 스레드를 붙잡습니다.
        이 함수는 아래 `generate_recruitment_copy`에서 `asyncio.to_thread`로 실행하므로
        Discord 이벤트 루프와 다른 버튼/명령어 처리를 막지 않습니다.
        """
        if self.bot.settings.gemini_api_key is None:
            raise RuntimeError("GEMINI_API_KEY가 설정되어 있지 않습니다. .env 또는 Railway Variables에 추가해 주세요.")

        genai.configure(api_key=self.bot.settings.gemini_api_key)
        model = genai.GenerativeModel(
            model_name=self.bot.settings.gemini_model,
            system_instruction=build_recruitment_system_prompt(),
        )
        response = model.generate_content(
            "다음은 사용자가 자유롭게 제공한 대화형 입력과 URL 크롤링 내용을 합친 원문입니다. "
            "사용자의 요청 의도와 어조를 유지하면서 모집글을 작성해라: "
            f"\n\n{source_text}\n\n"
            "이 텍스트 내용만을 엄격하게 바탕으로, 없는 내용을 지어내지 말고 다음 규칙에 따라 모집글을 작성해라.",
            generation_config={
                "temperature": 0.4,
                "response_mime_type": "application/json",
            },
        )
        return str(getattr(response, "text", "") or "")

    async def generate_recruitment_copy(self, source_text: str) -> tuple[str, str, int]:
        """Gemini 호출을 백그라운드 스레드로 넘기고, 응답을 Embed용 데이터로 파싱합니다."""
        raw_text = await asyncio.to_thread(self._generate_recruitment_copy_sync, source_text)
        return parse_gemini_recruitment(raw_text, source_text)

    async def prepare_recruitment_source_text(self, target_info: str) -> str:
        """입력 텍스트에 포함된 여러 URL을 동시에 크롤링하고 일반 텍스트와 병합합니다."""
        return await prepare_conversational_source_text(
            target_info,
            max_length=MAX_RECRUITMENT_SOURCE_TEXT_LENGTH,
            logger=logging.getLogger(__name__),
        )

    async def get_recruitment(self, message_id: int):
        """메시지 ID로 모집 레코드를 찾습니다."""
        return await self.bot.database.fetch_one(
            "SELECT * FROM recruitments WHERE message_id = ?",
            (message_id,),
        )

    async def edit_recruitment_message(self, channel_id: int, message_id: int) -> str | None:
        """DB에 저장된 채널/메시지 ID로 기존 모집 Embed 메시지를 찾아 수정합니다."""
        try:
            channel = self.bot.get_channel(int(channel_id))
            if channel is None:
                channel = await self.bot.fetch_channel(int(channel_id))
            if not hasattr(channel, "fetch_message"):
                return "저장된 채널에서 모집 메시지를 가져올 수 없어 Discord 메시지는 수정하지 못했습니다."

            # channel_id/message_id는 모집 생성 시 DB에 저장된 원본 메시지 위치입니다.
            # fetch_message()로 Discord에 이미 올라간 메시지를 다시 가져온 뒤 edit()해야
            # 참가/대기/불참 버튼이 붙은 실제 공개 모집글도 DB 수정 내용과 함께 갱신됩니다.
            message = await channel.fetch_message(int(message_id))
            embed = await self.build_recruitment_embed(int(message_id))
            await message.edit(embed=embed, view=RecruitmentView(self))
        except (TypeError, ValueError):
            return "저장된 채널 또는 메시지 ID가 올바르지 않아 Discord 메시지는 수정하지 못했습니다."
        except discord.NotFound:
            return "기존 모집 메시지를 찾을 수 없어 DB만 수정했습니다."
        except discord.Forbidden:
            return "모집 메시지를 수정할 권한이 없어 DB만 수정했습니다."
        except discord.HTTPException as exc:
            return f"모집 메시지 수정 중 오류가 발생해 DB만 수정했습니다: {exc.text}"
        return None

    async def get_vote_user_ids(self, recruitment_id: int, state: str) -> list[int]:
        """특정 상태에 투표한 유저 ID 목록을 가져옵니다."""
        rows = await self.bot.database.fetch_all(
            """
            SELECT user_id FROM recruitment_votes
            WHERE recruitment_id = ? AND state = ?
            ORDER BY updated_at ASC
            """,
            (recruitment_id, state),
        )
        return [int(row["user_id"]) for row in rows]

    async def build_recruitment_embed(self, message_id: int) -> discord.Embed:
        """현재 DB 상태를 읽어 모집 Embed를 다시 만듭니다."""
        recruitment = await self.get_recruitment(message_id)
        if recruitment is None:
            return base_embed("모집 정보를 찾을 수 없습니다.", color=STOP_COLOR)

        joined = await self.get_vote_user_ids(recruitment["id"], STATE_JOIN)
        declined = await self.get_vote_user_ids(recruitment["id"], STATE_DECLINE)
        waiting = await self.get_vote_user_ids(recruitment["id"], STATE_WAIT)

        status_text = "모집 중" if recruitment["status"] == STATUS_OPEN else "모집 마감"
        color = SUCCESS_COLOR if recruitment["status"] == STATUS_OPEN else WARNING_COLOR
        max_members = recruitment["max_members"]
        capacity = "제한 없음" if max_members == 0 else f"{len(joined)} / {max_members}"

        embed = base_embed(str(recruitment["title"]), str(recruitment["target"]), color=color)
        embed.add_field(name="상태", value=status_text, inline=True)
        embed.add_field(name="정원", value=capacity, inline=True)
        embed.add_field(name="작성자", value=f"<@{recruitment['author_id']}>", inline=True)
        if recruitment["thread_id"]:
            embed.add_field(name="비공개 워크스페이스", value=f"<#{recruitment['thread_id']}>", inline=False)
        embed.add_field(name="참가", value=mention_list(joined), inline=False)
        embed.add_field(name="대기", value=mention_list(waiting), inline=False)
        embed.add_field(name="불참", value=mention_list(declined), inline=False)
        return embed

    @app_commands.command(name="모집", description="팀원 모집 Embed와 참가 버튼을 생성합니다.")
    async def create_recruitment(self, interaction: discord.Interaction) -> None:
        """모집 생성 Modal을 엽니다."""
        await interaction.response.send_modal(RecruitmentModal(self))

    @app_commands.command(name="모집수정", description="드롭다운 메뉴와 Modal로 모집 글을 수정합니다.")
    async def edit_recruitment(self, interaction: discord.Interaction) -> None:
        """내가 작성했거나 현재 모집 중인 글 최대 25개를 Select Menu로 보여줍니다."""
        if interaction.guild is None:
            await interaction.response.send_message("서버 안에서만 모집을 수정할 수 있습니다.", ephemeral=True)
            return

        rows = await self.bot.database.fetch_all(
            """
            SELECT id, title, target, max_members, status, author_id, channel_id, message_id FROM recruitments
            WHERE guild_id = ? AND (author_id = ? OR status = ?)
            ORDER BY created_at DESC
            LIMIT 25
            """,
            (interaction.guild.id, interaction.user.id, STATUS_OPEN),
        )

        if not rows:
            await interaction.response.send_message("수정할 모집 글이 없습니다.", ephemeral=True)
            return

        embed = base_embed(
            "수정할 모집 글을 선택하세요",
            "내가 작성했거나 현재 모집 중인 글이 최대 25개까지 표시됩니다.",
            color=WARNING_COLOR,
        )
        await interaction.response.send_message(embed=embed, view=RecruitmentEditView(self, rows, interaction.user.id), ephemeral=True)

    @app_commands.command(name="모집생성", description="Gemini로 대회/해커톤 정보를 분석해 모집 글을 생성합니다.")
    @app_commands.describe(target_info="대회/해커톤 웹사이트 링크 또는 상세 텍스트")
    async def create_ai_recruitment(self, interaction: discord.Interaction, target_info: str) -> None:
        """Gemini가 만든 모집 글을 기존 모집 버튼 로직과 연결합니다."""
        # Gemini 응답은 3초를 넘길 수 있으므로 명령어가 들어오자마자 공개 defer를 보냅니다.
        # 이 줄이 늦게 실행되면 Discord가 "상호작용 실패"로 처리할 수 있습니다.
        await interaction.response.defer(ephemeral=False, thinking=True)

        if interaction.guild is None:
            await interaction.followup.send("서버 안에서만 모집을 만들 수 있습니다.", ephemeral=True)
            return

        target_info = target_info.strip()
        if not target_info:
            await interaction.followup.send("target_info에는 링크나 상세 텍스트를 입력해 주세요.", ephemeral=True)
            return

        try:
            source_text = await self.prepare_recruitment_source_text(target_info)
        except ScrapingError:
            await interaction.followup.send(SCRAPING_ERROR_MESSAGE, ephemeral=True)
            return

        try:
            title, description, max_members = await self.generate_recruitment_copy(source_text)
        except RuntimeError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            logging.exception("Gemini recruitment generation failed")
            await interaction.followup.send("Gemini API 호출 중 문제가 발생했습니다. API 키, 모델명, 할당량을 확인해 주세요.", ephemeral=True)
            return

        message, used_deferred_response, thread_notice = await self.post_recruitment_message(
            interaction,
            title=title,
            target=description,
            max_members=max_members,
            use_deferred_response=True,
        )

        if not used_deferred_response:
            await interaction.followup.send(f"Gemini가 모집 글을 만들었습니다: {message.jump_url}")
        if thread_notice:
            await interaction.followup.send(thread_notice, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """discord.py가 이 파일을 Cog로 로드할 때 호출하는 함수입니다."""
    await bot.add_cog(RecruitmentCog(bot))