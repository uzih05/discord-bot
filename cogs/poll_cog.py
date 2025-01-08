# cogs/poll_cog.py
import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Union

import discord
from discord import app_commands, Interaction, Embed, ButtonStyle
from discord.ext import commands
from discord.ui import View, Button
import aiofiles
from utils.common_checks import is_not_dm
from utils.poll_manager import PollManager

logger = logging.getLogger(__name__)


def parse_duration(duration_str: str) -> int:
    """지속 시간 문자열을 총 '분' 단위로 반환"""
    pattern = re.compile(r'(?:(\d+)h)?(?:(\d+)m)?$')
    match = pattern.match(duration_str.strip().lower())
    if not match:
        return -1

    hours, minutes = match.groups()
    total_minutes = 0
    if hours:
        total_minutes += int(hours) * 60
    if minutes:
        total_minutes += int(minutes)

    return total_minutes if total_minutes > 0 else -1


class PollView(View):
    def __init__(
            self,
            poll_id: str,
            title: str,
            options: List[str],
            timeout_minutes: int,
            allow_multiple_votes: bool,
            cog_reference: 'PollCog',
            member_count: int,
            poll_manager: PollManager,
            channel_id: Optional[int] = None,
            message_id: Optional[int] = None
    ):
        super().__init__(timeout=None)
        # 기본 속성 설정
        self.poll_id = poll_id
        self.title = title
        self.options = options
        self.votes = {option: 0 for option in options}
        self.voters = {}
        self.allow_multiple_votes = allow_multiple_votes
        self.message = None
        self.channel_id = channel_id
        self.message_id = message_id
        self.cog_reference = cog_reference
        self.poll_manager = poll_manager

        # 시간 관련 설정
        self.end_time = datetime.utcnow() + timedelta(minutes=timeout_minutes)
        self.is_closed = False
        self.last_progress_update = 0

        # 동기화 및 작업 관리
        self.lock = asyncio.Lock()
        self.timeout_task = None
        self.member_count = member_count

        # 버튼 생성
        for idx, option in enumerate(options):
            btn = Button(
                label=option,
                style=ButtonStyle.primary,
                custom_id=f"{self.poll_id}_option_{idx}"
            )
            btn.callback = self.create_button_callback(idx)
            self.add_item(btn)

        # 초기화 작업
        logger.info(f"[{poll_id}] 투표 '{title}' 생성됨.")
        self.start_timeout_task()
        asyncio.create_task(self.save_poll_data())

        # 메시지 복구 시도
        if channel_id and message_id:
            channel = self.cog_reference.bot.get_channel(channel_id)
            if channel:
                asyncio.create_task(self.fetch_message(channel, message_id))

    async def fetch_message(self, channel: discord.abc.GuildChannel, message_id: int):
        """채널과 메시지 ID로 메시지 복구"""
        try:
            self.message = await channel.fetch_message(message_id)
            await self.update_message()
        except Exception as e:
            logger.error(f"[{self.poll_id}] 메시지 복구 실패: {e}")

    async def save_poll_data(self):
        """투표 데이터 저장"""
        try:
            data = {
                "title": self.title,
                "options": self.options,
                "votes": self.votes,
                "voters": self.voters,
                "end_time": self.end_time.isoformat(),
                "allow_multiple_votes": self.allow_multiple_votes,
                "member_count": self.member_count,
                "is_closed": self.is_closed,
                "channel_id": self.channel_id,
                "message_id": self.message_id
            }
            await self.poll_manager.save_poll(self.poll_id, data)
        except Exception as e:
            logger.error(f"[{self.poll_id}] 데이터 저장 실패: {e}")

    def create_button_callback(self, idx: int):
        async def button_callback(interaction: Interaction):
            if self.is_closed:
                await interaction.response.send_message("이미 종료된 투표입니다.", ephemeral=True)
                return

            async with self.lock:
                try:
                    user_id = interaction.user.id
                    option = self.options[idx]

                    if not self.allow_multiple_votes and user_id in self.voters:
                        await interaction.response.send_message("이미 투표하셨습니다.", ephemeral=True)
                        return

                    # 투표 기록
                    if user_id not in self.voters:
                        self.voters[user_id] = []
                    self.voters[user_id].append(option)
                    self.votes[option] += 1

                    # 저장 및 업데이트
                    await self.save_poll_data()
                    embed = self.generate_embed()

                    if not interaction.response.is_done():
                        await interaction.response.edit_message(embed=embed, view=self)
                    else:
                        await interaction.message.edit(embed=embed, view=self)

                except Exception as e:
                    logger.error(f"[{self.poll_id}] 투표 처리 실패: {e}")
                    if not interaction.response.is_done():
                        await interaction.response.send_message("오류가 발생했습니다.", ephemeral=True)

        return button_callback

    def generate_embed(self, is_closed: bool = False) -> Embed:
        """투표 현황 임베드 생성"""
        # 종료 시각 변환 (KST)
        end_kst = self.end_time.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=9)))

        # 기본 임베드 설정
        embed = Embed(
            title=f"{self.title} (투표 종료됨)" if is_closed else self.title,
            color=discord.Color.red() if is_closed else int('f9e54b', 16)
        )

        # 투표 현황 계산
        total_votes = sum(self.votes.values())
        max_bar_length = 20

        # 옵션별 결과 표시
        for option, count in self.votes.items():
            ratio = count / (total_votes if self.allow_multiple_votes and total_votes else self.member_count or 1)
            bar_length = min(int(ratio * max_bar_length), max_bar_length)
            bar = '█' * bar_length
            percentage = (count / total_votes * 100) if total_votes else 0

            embed.add_field(
                name=option,
                value=f"`{bar:<20}` {count}표 ({percentage:.2f}%)",
                inline=False
            )

        # 참여율 표시 (중복투표 불가 시)
        if not self.allow_multiple_votes:
            part_rate = (len(self.voters) / self.member_count * 100) if self.member_count else 0
            embed.add_field(name="참여율", value=f"{part_rate:.2f}%", inline=False)

        embed.set_footer(text="투표가 종료되었습니다." if is_closed
        else f"종료 시각: {end_kst.strftime('%Y-%m-%d %H:%M:%S KST')}")
        return embed

    async def force_close(self):
        """투표 강제 종료"""
        async with self.lock:
            if self.is_closed:
                return

            try:
                self.is_closed = True
                self.stop_timeout_task()

                # 버튼 비활성화
                for item in self.children:
                    if isinstance(item, Button):
                        item.disabled = True

                # 메시지 업데이트
                if self.message:
                    try:
                        await self.message.edit(
                            embed=self.generate_embed(is_closed=True),
                            view=self
                        )
                    except Exception as e:
                        logger.error(f"[{self.poll_id}] 종료 메시지 업데이트 실패: {e}")

                # 정리
                await self.poll_manager.delete_poll(self.poll_id)
                if self.cog_reference:
                    await self.cog_reference.remove_poll(self.poll_id)

            except Exception as e:
                logger.error(f"[{self.poll_id}] 투표 종료 실패: {e}")

    def start_timeout_task(self):
        """종료 시간 체크 태스크 시작"""
        self.timeout_task = asyncio.create_task(self.check_timeout())

    def stop_timeout_task(self):
        """종료 시간 체크 태스크 중지"""
        if self.timeout_task and not self.timeout_task.done():
            self.timeout_task.cancel()

    async def check_timeout(self):
        """종료 시간 체크"""
        try:
            while not self.is_closed:
                if datetime.utcnow() >= self.end_time:
                    await self.force_close()
                    break
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[{self.poll_id}] 시간 체크 실패: {e}")


class PollCog(commands.Cog):
    poll_group = app_commands.Group(name="투표", description="투표 관련 명령어")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_polls = {}
        self.poll_manager = PollManager(
            os.path.join(os.path.dirname(__file__), "..", "cogs_data", "poll_cog", "polls")
        )
        asyncio.create_task(self.load_existing_polls())

    async def load_existing_polls(self):
        """기존 투표 로드"""
        try:
            polls_data = await self.poll_manager.load_all_polls()
            for poll_id, data in polls_data.items():
                try:
                    if data.get("is_closed", False):
                        await self.poll_manager.delete_poll(poll_id)
                        continue

                    end_time = datetime.fromisoformat(data["end_time"])
                    if end_time.tzinfo is None:
                        end_time = end_time.replace(tzinfo=timezone.utc)

                    if datetime.now(timezone.utc) >= end_time:
                        await self.poll_manager.delete_poll(poll_id)
                        continue

                    remaining_minutes = max(int((end_time - datetime.now(timezone.utc)).total_seconds() // 60), 1)

                    view = PollView(
                        poll_id=poll_id,
                        title=data["title"],
                        options=data["options"],
                        timeout_minutes=remaining_minutes,
                        allow_multiple_votes=data["allow_multiple_votes"],
                        cog_reference=self,
                        member_count=data["member_count"],
                        poll_manager=self.poll_manager,
                        channel_id=data.get("channel_id"),
                        message_id=data.get("message_id")
                    )
                    view.votes = data["votes"]
                    view.voters = data["voters"]

                    self.active_polls[poll_id] = view

                except Exception as e:
                    logger.error(f"[{poll_id}] 투표 로드 실패: {e}")

        except Exception as e:
            logger.error(f"투표 로드 실패: {e}")

    @poll_group.command(
        name="생성",
        description="투표를 생성합니다."
    )
    @app_commands.describe(
        제목="투표의 제목",
        옵션="투표 옵션(쉼표로 구분)",
        시간="예: 30m, 2h, 1h30m",
        중복="중복 투표 허용 여부"
    )
    @is_not_dm()
    async def create_poll(
            self,
            interaction: Interaction,
            제목: str,
            옵션: str,
            시간: str,
            중복: bool
    ):
        try:
            # 입력값 검증
            duration = parse_duration(시간)
            if duration < 1 or duration > 1440:
                await interaction.response.send_message(
                    "시간은 1분~24시간(1440분) 사이여야 합니다.",
                    ephemeral=True
                )
                return

            options = [o.strip() for o in 옵션.split(",") if o.strip()]
            if len(options) < 2:
                await interaction.response.send_message(
                    "최소 2개 이상의 옵션이 필요합니다.",
                    ephemeral=True
                )
                return

            # 투표 생성
            poll_id = str(uuid.uuid4())
            member_count = sum(1 for m in interaction.guild.members if not m.bot) if interaction.guild else 100

            view = PollView(
                poll_id=poll_id,
                title=제목,
                options=options,
                timeout_minutes=duration,
                allow_multiple_votes=중복,
                cog_reference=self,
                member_count=member_count,
                poll_manager=self.poll_manager
            )

            # 메시지 전송
            await interaction.response.send_message(
                f"투표가 생성되었습니다! ID: `{poll_id}`",
                ephemeral=True
            )

            msg = await interaction.channel.send(embed=view.generate_embed(), view=view)
            view.message = msg
            view.channel_id = interaction.channel.id
            view.message_id = msg.id

            # 저장
            await view.save_poll_data()
            self.active_polls[poll_id] = view

        except Exception as e:
            logger.error(f"투표 생성 실패: {e}")
            await interaction.followup.send(
                "투표 생성 중 오류가 발생했습니다.",
                ephemeral=True
            )

    @poll_group.command(
        name="취소",
        description="특정 투표를 즉시 취소(삭제)합니다."
    )
    @app_commands.describe(poll_id="취소할 투표 ID")
    @app_commands.default_permissions(manage_channels=True)
    @is_not_dm()
    async def cancel_poll(self, interaction: Interaction, poll_id: str):
        await interaction.response.defer(ephemeral=True)
        poll_view = self.active_polls.get(poll_id)

        if not poll_view:
            await interaction.followup.send(
                f"`{poll_id}` 투표를 찾을 수 없습니다.",
                ephemeral=True
            )
            return

        try:
            if poll_view.message:
                await poll_view.message.delete()
            await poll_view.force_close()
            await interaction.followup.send(
                f"투표 `{poll_id}`가 취소되었습니다.",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"투표 취소 실패: {e}")
            await interaction.followup.send(
                "투표 취소 중 오류가 발생했습니다.",
                ephemeral=True
            )

    @poll_group.command(
        name="조회",
        description="현재 진행 중인 투표 목록을 조회합니다."
    )
    @app_commands.default_permissions(manage_channels=True)
    @is_not_dm()
    async def view_polls(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        if not self.active_polls:
            await interaction.followup.send(
                "현재 진행 중인 투표가 없습니다.",
                ephemeral=True
            )
            return

        embed = Embed(title="진행 중인 투표 목록", color=discord.Color.blue())
        for poll_id, poll_view in self.active_polls.items():
            remain = (poll_view.end_time - datetime.utcnow()).total_seconds()
            m, s = divmod(max(int(remain), 0), 60)

            embed.add_field(
                name=f"{poll_view.title} (ID: {poll_id})",
                value=(
                    f"남은 시간: {m}분 {s}초\n"
                    f"옵션: {', '.join(poll_view.options)}\n"
                    f"중복 투표: {'허용' if poll_view.allow_multiple_votes else '불가'}"
                ),
                inline=False
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def remove_poll(self, poll_id: str):
        """투표 제거"""
        self.active_polls.pop(poll_id, None)
        logger.info(f"[{poll_id}] 투표가 제거되었습니다.")

    async def close_all_polls(self):
        """모든 투표 종료"""
        for poll_id, poll_view in list(self.active_polls.items()):
            await poll_view.force_close()

    async def cog_unload(self):
        """Cog 언로드 시 정리"""
        try:
            await self.close_all_polls()
            logger.info("PollCog 언로드 완료")
        except Exception as e:
            logger.error(f"PollCog 언로드 실패: {e}")


async def setup(bot: commands.Bot):
    """Cog 설치"""
    try:
        if "PollCog" not in bot.cogs:
            await bot.add_cog(PollCog(bot))
            logger.info("PollCog 로드 완료")
    except Exception as e:
        logger.error(f"PollCog 로드 실패: {e}")