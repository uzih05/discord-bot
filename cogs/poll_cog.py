# cogs/poll_cog.py
import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import discord
from discord import app_commands, Interaction, Embed, ButtonStyle
from discord.ext import commands
from discord.ui import View, Button


import aiofiles  # 비동기 파일 입출력을 위해 aiofiles 사용
from utils.common_checks import is_not_dm
from utils.poll_manager import PollManager


logger = logging.getLogger(__name__)


def parse_duration(duration_str: str) -> int:
    """
    지속 시간 문자열을 파싱해 총 '분' 단위로 반환.
    예: "30m" -> 30, "2h" -> 120, "1h30m" -> 90
    실패 시 -1 반환.
    """
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
        cog_reference: "PollCog",
        member_count: int,
        poll_manager: PollManager,
        channel_id: Optional[int] = None,
        message_id: Optional[int] = None
    ):
        super().__init__(timeout=None)
        self.poll_id = poll_id
        self.title = title
        self.options = options
        self.votes = {option: 0 for option in options}  # 득표 현황 초기화
        self.voters = {}  # 투표자 목록 {user_id: [옵션들]}
        self.allow_multiple_votes = allow_multiple_votes
        self.message: Optional[discord.Message] = None

        self.channel_id = channel_id
        self.message_id = message_id

        self.cog_reference = cog_reference
        self.poll_manager = poll_manager

        # 메시지가 저장된 경우 메시지를 다시 불러오기
        if channel_id and message_id:
            channel = self.cog_reference.bot.get_channel(channel_id)
            if channel:
                asyncio.create_task(self.fetch_message(channel, message_id))

        # 종료 시각(UTC)
        if timeout_minutes > 0:
            self.end_time = datetime.utcnow() + timedelta(minutes=timeout_minutes)
        else:
            self.end_time = datetime.utcnow()

        self.is_closed = False
        self.lock = asyncio.Lock()
        self.timeout_task: Optional[asyncio.Task] = None
        self.member_count = member_count

        # 옵션당 버튼 생성
        for idx, option in enumerate(options):
            btn = Button(
                label=option,
                style=ButtonStyle.primary,
                custom_id=f"{self.poll_id}_option_{idx}"
            )
            btn.callback = self.create_button_callback(idx)
            self.add_item(btn)

        logger.info(f"[{poll_id}] 투표 '{title}' 생성됨. 옵션: {options}")
        self.start_timeout_task()
        asyncio.create_task(self.save_poll_data())  # 초기 데이터 저장

    async def fetch_message(self, channel: discord.abc.GuildChannel, message_id: int):
        """채널과 메시지 ID를 통해 메시지를 불러와 설정합니다."""
        try:
            self.message = await channel.fetch_message(message_id)
            logger.info(f"[{self.poll_id}] 메시지 불러오기 성공: 채널 ID={channel.id}, 메시지 ID={message_id}")
            # 메시지를 업데이트하여 상태를 최신화
            await self.update_message()
        except discord.NotFound:
            logger.error(f"[{self.poll_id}] 메시지를 찾을 수 없습니다: 채널 ID={channel.id}, 메시지 ID={message_id}")
        except discord.Forbidden:
            logger.error(f"[{self.poll_id}] 메시지에 접근할 권한이 없습니다: 채널 ID={channel.id}, 메시지 ID={message_id}")
        except Exception as e:
            logger.error(f"[{self.poll_id}] 메시지 가져오기 오류: {e}", exc_info=True)

    async def save_poll_data(self):
        """
        현재 투표 데이터를 PollManager를 통해 저장합니다.
        """
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
            await self.poll_manager.save_poll(
                poll_id=self.poll_id,
                data=data
            )
            logger.debug(f"[{self.poll_id}] 투표 데이터 저장 성공.")
        except Exception as e:
            logger.error(f"[{self.poll_id}] 투표 데이터 저장 오류: {e}", exc_info=True)

    def start_timeout_task(self):
        """주기적으로 투표 종료 시간을 체크"""
        self.timeout_task = asyncio.create_task(self.check_timeout())

    def stop_timeout_task(self):
        if self.timeout_task and not self.timeout_task.done():
            self.timeout_task.cancel()
            logger.info(f"[{self.poll_id}] check_timeout Task 중단됨.")

    async def check_timeout(self):
        try:
            while not self.is_closed:
                now = datetime.utcnow()
                logger.debug(f"[{self.poll_id}] check_timeout... now={now}, end_time={self.end_time}")
                if now >= self.end_time:
                    logger.debug(f"[{self.poll_id}] 투표 종료 시간 도달 → force_close 호출 예정")
                    await self.force_close()
                    break
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            logger.info(f"[{self.poll_id}] check_timeout Task가 취소됨.")
        except Exception as e:
            logger.error(f"[{self.poll_id}] check_timeout 오류: {e}", exc_info=True)

    def create_button_callback(self, idx: int):
        async def button_callback(interaction: Interaction):
            if self.is_closed:
                try:
                    await interaction.response.send_message("이미 종료된 투표입니다.", ephemeral=True)
                except Exception as e:
                    logger.error(f"[{self.poll_id}] 종료된 투표 버튼 클릭 응답 오류: {e}")
                return

            async with self.lock:
                user_id = interaction.user.id
                option = self.options[idx]

                if not self.allow_multiple_votes and user_id in self.voters:
                    # 중복 투표 불가
                    try:
                        await interaction.response.send_message(
                            "이미 투표하셨습니다.", ephemeral=True
                        )
                    except Exception as e:
                        logger.error(f"[{self.poll_id}] 중복 투표 응답 실패: {e}")
                    return

                # 투표 처리
                if user_id not in self.voters:
                    self.voters[user_id] = []
                self.voters[user_id].append(option)
                self.votes[option] += 1
                logger.info(f"[{self.poll_id}] {interaction.user} → '{option}' 투표.")

                # 저장
                await self.save_poll_data()

                # 메시지 갱신
                embed = self.generate_embed()
                try:
                    if not interaction.response.is_done():
                        await interaction.response.edit_message(embed=embed, view=self)
                    else:
                        await interaction.message.edit(embed=embed, view=self)
                    logger.debug(f"[{self.poll_id}] 메시지 갱신 성공.")
                except discord.NotFound:
                    logger.warning(f"[{self.poll_id}] 투표 메시지를 찾을 수 없어 수정 불가.")
                except discord.Forbidden:
                    logger.error(f"[{self.poll_id}] 메시지 편집 권한 부족.")
                except Exception as e:
                    logger.error(f"[{self.poll_id}] 투표 업데이트 오류: {e}", exc_info=True)

        return button_callback

    def generate_embed(self, is_closed: bool = False) -> Embed:
        """
        투표 진행 상황 임베드
        - is_closed: 투표가 종료되었는지 여부
        """
        logger.debug(f"[{self.poll_id}] generate_embed called with is_closed={is_closed}")

        # KST 변환
        kst = timezone(timedelta(hours=9))
        end_kst = self.end_time.replace(tzinfo=timezone.utc).astimezone(kst)
        end_time_str = end_kst.strftime("%Y-%m-%d %H:%M:%S KST")

        if is_closed:
            title = f"{self.title} (투표 종료됨)"
            footer_text = "투표가 종료되었습니다."
            color = discord.Color.red()
        else:
            title = self.title
            footer_text = f"종료 시각: {end_time_str}"
            color = color=int('f9e54b', 16)

        embed = Embed(title=title, color=color)
        total_votes = sum(self.votes.values())

        max_bar_length = 20
        for option, count in self.votes.items():
            if self.allow_multiple_votes:
                # 득표수만큼 막대 표시
                bar = '█' * count
            else:
                # 참여율에 따른 막대 표시
                ratio = count / self.member_count if self.member_count > 0 else 0
                scaled_length = min(int(ratio * max_bar_length), max_bar_length)
                bar = '█' * scaled_length

            percentage = (count / total_votes * 100) if total_votes else 0
            embed.add_field(
                name=option,
                value=f"`{bar:<20}` {count}표 ({percentage:.2f}%)",
                inline=False
            )

        # 중복 투표 불가 시 참여율
        if not self.allow_multiple_votes:
            unique_voters_count = len(self.voters)
            part_rate = (unique_voters_count / self.member_count * 100) if self.member_count else 0
            embed.add_field(name="참여율", value=f"{part_rate:.2f}%", inline=False)

        embed.set_footer(text=footer_text)
        logger.debug(f"[{self.poll_id}] Generated embed: {embed.to_dict()}")
        return embed

    async def force_close(self):
        logger.debug(f"[{self.poll_id}] force_close 메서드 호출됨")
        async with self.lock:
            if self.is_closed:
                logger.warning(f"[{self.poll_id}] 이미 종료된 투표 force_close 재호출됨.")
                return

            self.is_closed = True
            self.stop_timeout_task()

            logger.info(f"[{self.poll_id}] 투표 '{self.title}' → force_close 실행.")

            # 버튼 비활성화
            for child in self.children:
                if isinstance(child, Button):
                    child.disabled = True

            # 종결 임베드 생성
            embed = self.generate_embed(is_closed=True)
            logger.debug(f"[{self.poll_id}] Generated embed: {embed.to_dict()}")

            # 메시지 편집
            if self.message:
                logger.debug(f"[{self.poll_id}] force_close: 메시지 편집 시도")
                try:
                    await asyncio.wait_for(
                        self.message.edit(embed=embed, view=self),
                        timeout=10.0  # 타임아웃을 10초로 증가
                    )
                    logger.debug(f"[{self.poll_id}] 메시지 편집 성공.")
                except asyncio.TimeoutError:
                    logger.error(f"[{self.poll_id}] 메시지 편집 타임아웃! API 응답 없음.")
                except discord.NotFound:
                    logger.error(f"[{self.poll_id}] 메시지를 찾을 수 없습니다.")
                except discord.Forbidden:
                    logger.error(f"[{self.poll_id}] 메시지 편집 권한이 없습니다.")
                except Exception as e:
                    logger.error(f"[{self.poll_id}] 종료 시 메시지 편집 오류: {e}", exc_info=True)
            else:
                logger.error(f"[{self.poll_id}] 메시지 정보가 없어 투표 종료 메시지를 업데이트할 수 없습니다.")

            # 폴더에서 투표 파일 삭제
            try:
                await self.poll_manager.delete_poll(self.poll_id)
                logger.debug(f"[{self.poll_id}] 투표 데이터 파일 삭제 완료.")
            except FileNotFoundError:
                logger.info(f"[{self.poll_id}] force_close 시점에 이미 파일 삭제됨.")
            except Exception as e:
                logger.error(f"[{self.poll_id}] 투표 데이터 파일 삭제 오류: {e}", exc_info=True)

            # 메모리에서 제거
            if self.cog_reference:
                await self.cog_reference.remove_poll(self.poll_id)
                logger.debug(f"[{self.poll_id}] 메모리에서 투표 제거 완료.")

    async def update_message(self):
        """
        메시지를 업데이트하여 현재 상태를 반영합니다.
        """
        if self.message:
            embed = self.generate_embed(is_closed=self.is_closed)  # is_closed 상태 반영
            try:
                await self.message.edit(embed=embed, view=self)
                logger.debug(f"[{self.poll_id}] 메시지 업데이트 성공.")
            except discord.NotFound:
                logger.error(f"[{self.poll_id}] 메시지를 찾을 수 없어 업데이트 실패.")
            except discord.Forbidden:
                logger.error(f"[{self.poll_id}] 메시지 편집 권한이 없어 업데이트 실패.")
            except Exception as e:
                logger.error(f"[{self.poll_id}] 메시지 업데이트 오류: {e}", exc_info=True)

class PollCog(commands.Cog):
    poll_group = app_commands.Group(name="투표", description="투표 관련 명령어들")

    def __init__(self, bot: commands.Bot):
        try:
            self.bot = bot
            # poll_manager 폴더 경로 설정
            self.poll_manager = PollManager(
                os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "..",
                    "cogs_data",
                    "poll_cog",
                    "polls"
                )
            )
            self.active_polls: Dict[str, PollView] = {}
            logger.info("PollCog 초기화 완료.")
            asyncio.create_task(self.load_existing_polls())
        except Exception as e:
            logger.error(f"PollCog 초기화 중 오류: {e}", exc_info=True)
            raise e

    async def load_existing_polls(self):
        """
        봇 시작 시 기존 투표 로드.
        이미 종료된(is_closed=True) 또는 만료된(시간 지난) 투표는 폴더에서 삭제.
        """
        try:
            polls_data = await self.poll_manager.load_all_polls()
            for poll_id, data in polls_data.items():
                try:
                    if data.get("is_closed", False):
                        logger.info(f"[{poll_id}] 이미 종료된 투표 → 폴더에서 삭제.")
                        await self.poll_manager.delete_poll(poll_id)
                        continue

                    end_str = data["end_time"]
                    end_dt = datetime.fromisoformat(end_str)
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)

                    now_utc = datetime.now(timezone.utc)
                    if now_utc >= end_dt:
                        logger.info(f"[{poll_id}] 재부팅 시 만료된 투표 → 폴더에서 삭제.")
                        await self.poll_manager.delete_poll(poll_id)
                        continue

                    remaining_minutes = max(int((end_dt - now_utc).total_seconds() // 60), 1)

                    channel_id = data.get("channel_id")
                    message_id = data.get("message_id")

                    view = PollView(
                        poll_id=poll_id,
                        title=data["title"],
                        options=data["options"],
                        timeout_minutes=remaining_minutes,
                        allow_multiple_votes=data["allow_multiple_votes"],
                        cog_reference=self,
                        member_count=data["member_count"],
                        poll_manager=self.poll_manager,
                        channel_id=channel_id,
                        message_id=message_id
                    )
                    view.votes = data["votes"]
                    view.voters = data["voters"]

                    self.active_polls[poll_id] = view
                    logger.info(f"[{poll_id}] 기존 투표 '{data['title']}' 로드 완료.")
                except Exception as e:
                    logger.error(f"[{poll_id}] 기존 투표 로드 중 오류: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"기존 투표 로드 중 오류: {e}", exc_info=True)

    async def remove_poll(self, poll_id: str):
        poll = self.active_polls.pop(poll_id, None)
        if poll:
            logger.info(f"[{poll_id}] 투표 '{poll.title}' 메모리에서 제거됨.")
        else:
            logger.warning(f"[{poll_id}] 이미 제거되었거나 존재하지 않음.")

    @poll_group.command(
        name="생성",
        description="투표를 생성합니다. (예: /투표 생성 제목='점심?' 옵션='피자,햄버거' 시간='30m' 중복=True)"
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
        logger.info(f"[Guild:{interaction.guild_id}] /투표 생성: 제목={제목}, 옵션={옵션}, 시간={시간}, 중복={중복}")

        # 시간 파싱
        total_minutes = parse_duration(시간)
        if total_minutes < 1:
            await interaction.response.send_message("시간 형식이 잘못되었거나 1분 미만입니다.", ephemeral=True)
            return
        if total_minutes > 1440:
            await interaction.response.send_message("최대 24시간(1440분)까지만 설정 가능합니다.", ephemeral=True)
            return

        # 옵션 파싱
        options = [o.strip() for o in 옵션.split(",") if o.strip()]
        if len(options) < 2:
            await interaction.response.send_message("최소 2개 이상의 옵션이 필요합니다.", ephemeral=True)
            return

        # 서버 멤버 수(봇 제외)
        guild = interaction.guild
        if guild is None:
            member_count = 100
        else:
            humans = sum(1 for m in guild.members if not m.bot)
            member_count = humans if humans > 0 else 1

        poll_id = str(uuid.uuid4())

        view = PollView(
            poll_id=poll_id,
            title=제목,
            options=options,
            timeout_minutes=total_minutes,
            allow_multiple_votes=중복,
            cog_reference=self,
            member_count=member_count,
            poll_manager=self.poll_manager
        )
        embed = view.generate_embed()

        # 명령어 응답(간단 안내)
        await interaction.response.send_message(
            f"투표가 생성되었습니다!\n"
            f"잠시 후 채널에 표시됩니다.\n"
            f"투표 ID: `{poll_id}`",
            ephemeral=True
        )

        # 실제 투표 메시지
        channel = interaction.channel
        if not channel:
            logger.error(f"[{poll_id}] 투표 생성 시 채널 정보를 찾을 수 없음.")
            return

        try:
            msg = await channel.send(embed=embed, view=view)
            view.message = msg
            view.channel_id = channel.id  # 채널 ID 저장
            view.message_id = msg.id     # 메시지 ID 저장

            # 메시지 ID와 채널 ID를 저장
            await self.poll_manager.save_poll(
                poll_id=poll_id,
                data={
                    "title": view.title,
                    "options": view.options,
                    "votes": view.votes,
                    "voters": view.voters,
                    "end_time": view.end_time.isoformat(),
                    "allow_multiple_votes": view.allow_multiple_votes,
                    "member_count": view.member_count,
                    "is_closed": view.is_closed,
                    "channel_id": channel.id,
                    "message_id": msg.id
                }
            )

            self.active_polls[poll_id] = view
            logger.info(f"[{poll_id}] 새 투표 '{제목}' 생성됨. ({total_minutes}분, 중복={중복})")
        except Exception as e:
            logger.error(f"[{poll_id}] 투표 메시지 전송 오류: {e}", exc_info=True)
            await interaction.followup.send("투표 메시지를 전송하는 중 오류가 발생했습니다.", ephemeral=True)

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
            await interaction.followup.send(f"`{poll_id}` 투표를 찾을 수 없습니다.", ephemeral=True)
            return

        try:
            if poll_view.message:
                await poll_view.message.delete()
                logger.debug(f"[{poll_id}] 투표 메시지 삭제 완료.")
            await poll_view.force_close()
            await interaction.followup.send(f"투표 `{poll_id}`가 취소되었습니다.", ephemeral=True)
        except discord.NotFound:
            await interaction.followup.send("이미 메시지가 삭제된 것 같습니다.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("메시지 삭제 권한이 없어 투표를 삭제할 수 없습니다.", ephemeral=True)
        except Exception as e:
            logger.error(f"[{poll_id}] 취소 과정에서 오류: {e}", exc_info=True)
            await interaction.followup.send("투표 취소 중 오류가 발생했습니다.", ephemeral=True)

    @poll_group.command(
        name="조회",
        description="현재 진행 중인 투표 목록을 조회합니다."
    )
    @app_commands.default_permissions(manage_channels=True)
    @is_not_dm()
    async def view_polls(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        if not self.active_polls:
            await interaction.followup.send("현재 진행 중인 투표가 없습니다.", ephemeral=True)
            return

        embed = Embed(title="진행 중인 투표 목록", color=discord.Color.gold())
        for poll_id, poll_view in self.active_polls.items():
            remain = (poll_view.end_time - datetime.utcnow()).total_seconds()
            if remain < 0:
                remain = 0
            m, s = divmod(int(remain), 60)
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

    async def close_all_polls(self):
        logger.info("close_all_polls: 활성 투표 일괄 force_close")
        for pid, poll_view in list(self.active_polls.items()):
            await poll_view.force_close()

    async def cog_unload(self):
        try:
            await self.close_all_polls()
            logger.info("PollCog 언로드 완료.")
        except Exception as e:
            logger.error(f"PollCog 언로드 중 오류: {e}", exc_info=True)

async def setup(bot: commands.Bot):
    try:
        if "PollCog" not in bot.cogs:
            await bot.add_cog(PollCog(bot))
            logger.info("PollCog 로드 완료.")
    except Exception as e:
        logger.error(f"PollCog 로드 중 오류: {e}", exc_info=True)
