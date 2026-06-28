from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from utils.datetime import now_utc_iso
from utils.embeds import STOP_COLOR, SUCCESS_COLOR, WARNING_COLOR, base_embed, mention_list


STATE_JOIN = "join"
STATE_DECLINE = "decline"
STATE_WAIT = "wait"
STATUS_OPEN = "open"
STATUS_CLOSED = "closed"


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
        channel = await self.cog.resolve_recruitment_channel(interaction)

        placeholder = base_embed("모집을 준비하는 중입니다.")
        message = await channel.send(embed=placeholder, view=RecruitmentView(self.cog))

        await self.cog.bot.database.execute(
            """
            INSERT INTO recruitments (guild_id, channel_id, message_id, author_id, title, target, max_members, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interaction.guild.id,
                channel.id,
                message.id,
                interaction.user.id,
                str(self.title_input.value),
                str(self.target_input.value),
                max_members,
                STATUS_OPEN,
                now_utc_iso(),
            ),
        )

        embed = await self.cog.build_recruitment_embed(message.id)
        await message.edit(embed=embed, view=RecruitmentView(self.cog))
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


async def setup(bot: commands.Bot) -> None:
    """discord.py가 이 파일을 Cog로 로드할 때 호출하는 함수입니다."""
    await bot.add_cog(RecruitmentCog(bot))