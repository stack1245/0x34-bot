from __future__ import annotations

import asyncio
import json
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands
import google.generativeai as genai

from utils.datetime import now_utc_iso
from utils.embeds import STOP_COLOR, SUCCESS_COLOR, WARNING_COLOR, base_embed, mention_list


STATE_JOIN = "join"
STATE_DECLINE = "decline"
STATE_WAIT = "wait"
STATUS_OPEN = "open"
STATUS_CLOSED = "closed"
DEFAULT_AI_RECRUITMENT_CAPACITY = 4
MAX_AI_TITLE_LENGTH = 50
MAX_EMBED_DESCRIPTION_LENGTH = 3900


GEMINI_SYSTEM_PROMPT = """
제공된 링크나 텍스트를 분석하여 해커톤/대회 모집 글을 작성해라.
1. 제목은 이모지를 포함해 50자 이내로 직관적으로 작성해라.
2. 본문은 대회 일정, 참가 자격, 주제, 혜택을 디스코드 마크다운(볼드, 불릿 등)을 활용해 깔끔하게 요약해라.
3. 응답은 반드시 JSON 객체 하나로만 작성해라. 형식은 {"title": "제목", "description": "본문"} 이다.
""".strip()


def _trim_text(value: str, limit: int) -> str:
    """Discord Embed 제한을 넘지 않도록 긴 문자열을 안전하게 자릅니다."""
    text = value.strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3].rstrip()}..."


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


def parse_gemini_recruitment(raw_text: str, fallback_source: str) -> tuple[str, str]:
    """Gemini 응답을 모집 Embed에 넣을 제목과 본문으로 변환합니다.

    모델에는 JSON만 반환하라고 지시하지만, 실제 LLM 응답은 코드블록이나 일반 텍스트가 섞일 수 있습니다.
    그래서 JSON 파싱을 먼저 시도하고, 실패하면 첫 줄을 제목, 나머지를 본문으로 쓰는 fallback을 둡니다.
    """
    cleaned = _strip_json_code_fence(raw_text)
    title = ""
    description = ""

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

    if not title or not description:
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        if lines:
            title = title or lines[0]
            description = description or "\n".join(lines[1:])

    title = _clean_title(title) or "🚀 Team 0x34 모집"
    description = description.strip() or f"**대상 정보**\n- {fallback_source}"
    return title, _trim_text(description, MAX_EMBED_DESCRIPTION_LENGTH)


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

        embed = await self.cog.build_recruitment_embed(recruitment["message_id"])
        await interaction.message.edit(embed=embed, view=self)
        await interaction.response.send_message("응답이 반영되었습니다.", ephemeral=True)

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

        joined = await self.cog.get_vote_user_ids(recruitment["id"], STATE_JOIN)
        waiting = await self.cog.get_vote_user_ids(recruitment["id"], STATE_WAIT)
        mentions = " ".join(f"<@{user_id}>" for user_id in [*joined, *waiting]) or "참가자가 없습니다."

        thread_message = f"모집이 마감되었습니다.\n참가/대기 인원: {mentions}"
        try:
            thread = await interaction.message.create_thread(name=f"{recruitment['title']} 준비", auto_archive_duration=1440)
            await thread.send(thread_message)
            await interaction.followup.send(f"모집을 마감하고 스레드를 만들었습니다: {thread.mention}", ephemeral=True)
        except discord.HTTPException:
            await interaction.channel.send(thread_message)  # type: ignore[union-attr]
            await interaction.followup.send("모집을 마감했습니다. 스레드 생성은 실패해 현재 채널에 멘션했습니다.", ephemeral=True)


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
        message, _ = await self.cog.post_recruitment_message(
            interaction,
            title=str(self.title_input.value),
            target=str(self.target_input.value),
            max_members=max_members,
            use_deferred_response=False,
        )
        await interaction.followup.send(f"모집 글을 만들었습니다: {message.jump_url}", ephemeral=True)


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
    ) -> tuple[discord.Message, bool]:
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

        await self.bot.database.execute(
            """
            INSERT INTO recruitments (guild_id, channel_id, message_id, author_id, title, target, max_members, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                now_utc_iso(),
            ),
        )

        embed = await self.build_recruitment_embed(message.id)
        await message.edit(embed=embed, view=RecruitmentView(self))
        return message, should_use_followup

    def _generate_recruitment_copy_sync(self, target_info: str) -> str:
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
            system_instruction=GEMINI_SYSTEM_PROMPT,
        )
        response = model.generate_content(
            f"Team 0x34 Discord 서버에 올릴 팀원 모집 글을 작성해 주세요.\n\n입력:\n{target_info}",
            generation_config={
                "temperature": 0.4,
                "response_mime_type": "application/json",
            },
        )
        return str(getattr(response, "text", "") or "")

    async def generate_recruitment_copy(self, target_info: str) -> tuple[str, str]:
        """Gemini 호출을 백그라운드 스레드로 넘기고, 응답을 Embed용 데이터로 파싱합니다."""
        raw_text = await asyncio.to_thread(self._generate_recruitment_copy_sync, target_info)
        return parse_gemini_recruitment(raw_text, target_info)

    async def get_recruitment(self, message_id: int):
        """메시지 ID로 모집 레코드를 찾습니다."""
        return await self.bot.database.fetch_one(
            "SELECT * FROM recruitments WHERE message_id = ?",
            (message_id,),
        )

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
        embed.add_field(name="참가", value=mention_list(joined), inline=False)
        embed.add_field(name="대기", value=mention_list(waiting), inline=False)
        embed.add_field(name="불참", value=mention_list(declined), inline=False)
        return embed

    @app_commands.command(name="모집", description="팀원 모집 Embed와 참가 버튼을 생성합니다.")
    async def create_recruitment(self, interaction: discord.Interaction) -> None:
        """모집 생성 Modal을 엽니다."""
        await interaction.response.send_modal(RecruitmentModal(self))

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
            title, description = await self.generate_recruitment_copy(target_info)
        except RuntimeError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            logging.exception("Gemini recruitment generation failed")
            await interaction.followup.send("Gemini API 호출 중 문제가 발생했습니다. API 키, 모델명, 할당량을 확인해 주세요.", ephemeral=True)
            return

        message, used_deferred_response = await self.post_recruitment_message(
            interaction,
            title=title,
            target=description,
            max_members=DEFAULT_AI_RECRUITMENT_CAPACITY,
            use_deferred_response=True,
        )

        if not used_deferred_response:
            await interaction.followup.send(f"Gemini가 모집 글을 만들었습니다: {message.jump_url}")


async def setup(bot: commands.Bot) -> None:
    """discord.py가 이 파일을 Cog로 로드할 때 호출하는 함수입니다."""
    await bot.add_cog(RecruitmentCog(bot))