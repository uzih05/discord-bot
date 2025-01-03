# cogs/news_cog.py
from typing import Optional

import asyncio
import json
import logging
import os
from urllib.parse import quote_plus
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo  # Python 3.9 이상에서 사용 가능

import aiofiles
import aiohttp
import discord
from discord import app_commands, Interaction, Embed, TextChannel
from discord.ext import commands
from dotenv import load_dotenv
from utils.common_checks import is_not_dm

from utils.news_manager import NewsManager  # NewsManager import


logger = logging.getLogger(__name__)

# 상수 정의
DM_NOT_ALLOWED_MESSAGE = "이 명령어는 DM에서 사용할 수 없습니다. 서버에서 시도하세요!"
DATA_ACCESS_ERROR_MESSAGE = "데이터에 접근하는 중 문제가 발생했습니다. 잠시 후 다시 시도해주세요."
NEWS_API_ERROR_MESSAGE = "뉴스를 가져오는 중 문제가 발생했습니다. 잠시 후 다시 시도해주세요."

# .env 파일 로드
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
load_dotenv(dotenv_path=ENV_PATH)

# API 키 가져오기
NEWS_API_KEY = os.getenv("NEWS_API_KEY")
if not NEWS_API_KEY:
    logger.error("NEWS_API_KEY 환경 변수가 설정되지 않았습니다.")
    raise ValueError("NEWS_API_KEY 환경 변수가 누락되었습니다.")

# 데이터 저장 경로
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cogs_data")
NEWS_DIR = os.path.join(DATA_DIR, "news_cog")
os.makedirs(NEWS_DIR, exist_ok=True)

class NewsCog(commands.Cog):
    # 클래스 변수로 news_group 정의
    news_group = app_commands.Group(name="뉴스", description="뉴스 관련 명령어들")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: aiohttp.ClientSession = bot.session  # MyBot의 세션 공유
        self.news_manager = NewsManager(os.path.join(DATA_DIR, "news_cog"))  # NewsManager 인스턴스 생성

        # 한국 시간대 설정
        self.timezone = ZoneInfo("Asia/Seoul")

        # 스케줄된 시간을 리스트로 정의 (24시간 형식)
        self.scheduled_hours = [15, 16, 17]  # 오후 3시, 4시, 5시

        # 뉴스 전송을 위한 백그라운드 태스크 생성
        self.send_news_task = asyncio.create_task(self.send_news_at_scheduled_times())
        logger.info("NewsCog 초기화 완료.")

    async def cog_unload(self):
        # 백그라운드 태스크 취소 및 NewsManager 종료
        self.send_news_task.cancel()
        try:
            await self.send_news_task
        except asyncio.CancelledError:
            logger.info("send_news_at_scheduled_times 태스크가 정상적으로 취소되었습니다.")
        logger.info("NewsCog 언로드 완료.")

    async def fetch_latest_news(self, query: str, retries: int = 3) -> list:
        """뉴스 API로부터 최신 뉴스 가져오기, 최대 retries회 재시도"""
        query = " AND ".join([keyword.strip() for keyword in query.split(",")])
        encoded_query = quote_plus(query)
        url = f"https://newsapi.org/v2/everything?q={encoded_query}&sortBy=publishedAt&language=ko&apiKey={NEWS_API_KEY}"
        for i in range(retries):
            try:
                async with self.session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        logger.debug(f"뉴스 API 호출 성공: {len(data.get('articles', []))}개 기사 수신.")
                        return data.get("articles", [])
                    else:
                        logger.warning(f"뉴스 API 호출 실패({i+1}/{retries}): 상태 코드 {response.status}, 응답: {await response.text()}")
            except Exception as e:
                logger.error(f"뉴스 데이터를 가져오는 중 오류 발생({i+1}/{retries}): {e}")
        # 모든 재시도 실패
        logger.error("모든 뉴스 API 재시도 실패.")
        return []

    async def send_news_at_scheduled_times(self):
        """지정된 시간에 뉴스를 전송하는 백그라운드 태스크"""
        while not self.bot.is_closed():
            now = datetime.now(self.timezone)
            logger.debug(f"현재 시간: {now.strftime('%Y-%m-%d %H:%M:%S')}")

            # 오늘의 스케줄된 시간 중 아직 지나지 않은 시간 찾기
            future_times = [
                datetime.combine(now.date(), time(hour=hr, minute=0, second=0, tzinfo=self.timezone))
                for hr in self.scheduled_hours
            ]
            future_times = [ft for ft in future_times if ft > now]

            if future_times:
                next_time = future_times[0]
            else:
                # 모든 스케줄된 시간이 지났다면 다음 날 첫 스케줄 시간으로 설정
                next_day = now.date() + timedelta(days=1)
                next_time = datetime.combine(next_day, time(hour=self.scheduled_hours[0], minute=0, second=0, tzinfo=self.timezone))

            delta_seconds = (next_time - now).total_seconds()
            logger.debug(f"다음 뉴스 전송 시간: {next_time.strftime('%Y-%m-%d %H:%M:%S')}, 대기 시간: {delta_seconds}초")

            try:
                await asyncio.sleep(delta_seconds)
            except asyncio.CancelledError:
                logger.info("send_news_at_scheduled_times 태스크가 취소되었습니다.")
                break

            await self.send_news()

    async def send_news(self):
        """등록된 모든 채널에 최신 뉴스를 전송"""
        news_data_all = await self.news_manager.load_all_news_data()
        logger.info(f"뉴스 전송 시작: {len(news_data_all)}개의 서버 처리 중")

        for guild_id, news_data in news_data_all.items():
            channels_data = news_data.get("channels", {})

            for channel_id, info in channels_data.items():
                query = info.get("query", "")
                last_sent_url = info.get("last_sent_url", "")
                if not query:
                    logger.warning(f"[Guild: {guild_id}, Channel: {channel_id}] 키워드가 설정되지 않았습니다.")
                    continue

                articles = await self.fetch_latest_news(query)
                if not articles:
                    logger.info(f"뉴스를 가져오지 못했습니다. (guild_id: {guild_id}, channel_id: {channel_id})")
                    continue

                article = articles[0]
                current_url = article.get("url", "")
                if last_sent_url == current_url:
                    logger.info(f"[Guild: {guild_id}, Channel: {channel_id}] 이미 전송된 뉴스: {current_url}")
                    continue

                channel = self.bot.get_channel(int(channel_id))
                if channel:
                    permissions = channel.permissions_for(channel.guild.me)
                    if not permissions.send_messages:
                        logger.warning(f"[Guild: {guild_id}, Channel: {channel_id}] 봇에게 메시지 전송 권한이 없습니다.")
                        continue

                    embed = Embed(
                        title=article.get("title", "제목 없음"),
                        description=f"{article.get('description', '요약 없음')[:200]}...\n\n[기사 읽기]({article.get('url', '')})",
                        color=discord.Color.green()
                    )
                    if article.get("urlToImage"):
                        embed.set_image(url=article["urlToImage"])

                    try:
                        await channel.send(embed=embed)
                        info["last_sent_url"] = current_url
                        news_data["channels"] = channels_data

                        if not await self.news_manager.save_news_data(guild_id, news_data):
                            logger.warning(f"[Guild: {guild_id}, Channel: {channel_id}] 뉴스 데이터 저장 실패: 변경사항 적용 불가")
                    except discord.errors.Forbidden:
                        logger.error(f"[Guild: {guild_id}, Channel: {channel_id}] 봇에게 메시지 전송 권한이 없습니다.")
                    except discord.errors.HTTPException as e:
                        logger.error(f"[Guild: {guild_id}, Channel: {channel_id}] 뉴스 메시지 전송 중 HTTP 오류 발생: {e}")
                    except Exception as e:
                        logger.error(f"[Guild: {guild_id}, Channel: {channel_id}] 뉴스 메시지 전송 중 예상치 못한 오류 발생: {e}")
                else:
                    logger.warning(f"[Guild: {guild_id}] 채널 {channel_id}을(를) 찾을 수 없습니다.")

        logger.info("뉴스 전송 완료.")

    async def close(self):
        """NewsManager의 정리 작업을 수행합니다. 현재는 별도의 정리 작업이 없지만, 필요 시 추가할 수 있습니다."""
        logger.info("NewsManager 종료 작업을 수행합니다.")

    # 하위 명령어: 뉴스 등록
    @news_group.command(
        name="등록",
        description="뉴스 전송을 위한 채널을 등록하고 최신 뉴스를 전송합니다. 예: /뉴스 등록 키워드='카카오, 네이버'"
    )
    @app_commands.describe(키워드="검색할 뉴스 키워드를 쉼표로 구분하여 입력하세요.")
    @app_commands.default_permissions(manage_channels=True)
    @is_not_dm()
    async def register_news(self, interaction: Interaction, 키워드: str):
        await interaction.response.defer(ephemeral=True)  # 명령어 처리 중임을 사용자에게 알림
        guild_id = str(interaction.guild_id)
        channel_id = str(interaction.channel_id)
        logger.info(f"[Guild: {guild_id}, Channel: {channel_id}] 뉴스등록 명령어 실행: 키워드='{키워드}'")

        # 봇의 메시지 전송 권한 확인
        permissions = interaction.channel.permissions_for(interaction.guild.me)
        if not permissions.send_messages:
            await interaction.followup.send("봇에게 이 채널에 메시지를 보낼 권한이 없습니다.", ephemeral=True)
            logger.warning(f"[Guild: {guild_id}, Channel: {channel_id}] 메시지 전송 권한 없음.")
            return

        news_data = await self.news_manager.load_news_data(guild_id)
        channels_data = news_data.get("channels", {})

        if channel_id in channels_data:
            await interaction.followup.send(
                f"채널 <#{channel_id}>은 이미 뉴스 전송 채널로 등록되어 있습니다.", ephemeral=True
            )
            logger.info(f"[Guild: {guild_id}, Channel: {channel_id}] 이미 등록된 채널.")
            return

        channels_data[channel_id] = {"query": 키워드, "last_sent_url": None}
        news_data["channels"] = channels_data
        if not await self.news_manager.save_news_data(guild_id, news_data):
            await interaction.followup.send(DATA_ACCESS_ERROR_MESSAGE, ephemeral=True)
            logger.error(f"[Guild: {guild_id}, Channel: {channel_id}] 뉴스 데이터 저장 실패.")
            return

        articles = await self.fetch_latest_news(키워드)
        if not articles:
            await interaction.followup.send(
                f"뉴스를 가져오지 못했습니다. {NEWS_API_ERROR_MESSAGE}", ephemeral=True
            )
            logger.warning(f"[Guild: {guild_id}, Channel: {channel_id}] 뉴스 가져오기 실패.")
            return

        article = articles[0]
        embed = Embed(
            title=article.get("title", "제목 없음"),
            description=f"{article.get('description', '요약 없음')[:200]}...\n\n[기사 읽기]({article.get('url', '')})",
            color=discord.Color.green()
        )
        if article.get("urlToImage"):
            embed.set_image(url=article["urlToImage"])

        try:
            channel = self.bot.get_channel(int(channel_id))
            if not channel:
                raise ValueError("채널을 찾을 수 없습니다.")

            await channel.send(embed=embed)
            channels_data[channel_id]["last_sent_url"] = article.get("url", "")
            news_data["channels"] = channels_data

            if not await self.news_manager.save_news_data(guild_id, news_data):
                await interaction.followup.send(DATA_ACCESS_ERROR_MESSAGE, ephemeral=True)
                logger.error(f"[Guild: {guild_id}, Channel: {channel_id}] 뉴스 데이터 저장 실패.")
                return

            await interaction.followup.send(f"채널 <#{channel_id}>이(가) 뉴스 전송 채널로 등록되었습니다!", ephemeral=True)
            logger.info(f"[Guild: {guild_id}, Channel: {channel_id}] 뉴스 전송 채널 등록 완료.")
        except discord.errors.Forbidden:
            logger.error(f"[Guild: {guild_id}, Channel: {channel_id}] 봇에게 메시지 전송 권한이 없습니다.")
            await interaction.followup.send("봇에게 이 채널에 메시지를 보낼 권한이 없어 뉴스를 전송할 수 없습니다.", ephemeral=True)
        except discord.errors.HTTPException as e:
            logger.error(f"[Guild: {guild_id}, Channel: {channel_id}] 뉴스 메시지 전송 중 HTTP 오류 발생: {e}")
            await interaction.followup.send("뉴스를 전송하는 중 오류가 발생했습니다. 나중에 다시 시도해주세요.", ephemeral=True)
        except Exception as e:
            logger.error(f"[Guild: {guild_id}, Channel: {channel_id}] 뉴스 메시지 전송 중 예상치 못한 오류 발생: {e}")
            await interaction.followup.send("뉴스를 전송하는 중 예상치 못한 오류가 발생했습니다.", ephemeral=True)

    # 하위 명령어: 뉴스 취소 또는 수정
    @news_group.command(
        name="취소",
        description="뉴스 전송을 취소하거나 특정 채널의 키워드를 수정합니다."
    )
    @app_commands.describe(
        채널="뉴스 전송을 취소하거나 수정할 채널을 선택하세요.",
        키워드="수정할 새로운 키워드를 입력하세요. 이 인자를 생략하면 해당 채널의 뉴스 전송이 취소됩니다."
    )
    @app_commands.default_permissions(manage_channels=True)
    @is_not_dm()
    async def cancel_news(self, interaction: Interaction, 채널: TextChannel, 키워드: str = None):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        channel_id = str(채널.id)
        logger.info(f"[Guild: {guild_id}, Channel: {channel_id}] 뉴스취소 명령어 실행: 키워드='{키워드}'")

        news_data = await self.news_manager.load_news_data(guild_id)
        channels_data = news_data.get("channels", {})

        if channel_id not in channels_data:
            await interaction.followup.send("해당 채널은 뉴스 전송 채널로 등록되어 있지 않습니다.", ephemeral=True)
            logger.warning(f"[Guild: {guild_id}, Channel: {channel_id}] 뉴스 취소 시도됨, 등록되지 않은 채널.")
            return

        if 키워드:
            # 키워드 수정
            channels_data[channel_id]["query"] = 키워드
            news_data["channels"] = channels_data
            if not await self.news_manager.save_news_data(guild_id, news_data):
                await interaction.followup.send(DATA_ACCESS_ERROR_MESSAGE, ephemeral=True)
                logger.error(f"[Guild: {guild_id}, Channel: {channel_id}] 뉴스 데이터 저장 실패.")
                return
            await interaction.followup.send(f"채널 <#{channel_id}>의 뉴스 키워드가 수정되었습니다: {키워드}", ephemeral=True)
            logger.info(f"[Guild: {guild_id}, Channel: {channel_id}] 뉴스 키워드 수정 완료.")
        else:
            # 뉴스 전송 취소
            del channels_data[channel_id]
            news_data["channels"] = channels_data
            if not await self.news_manager.save_news_data(guild_id, news_data):
                await interaction.followup.send(DATA_ACCESS_ERROR_MESSAGE, ephemeral=True)
                logger.error(f"[Guild: {guild_id}, Channel: {channel_id}] 뉴스 데이터 저장 실패.")
                return
            await interaction.followup.send(f"채널 <#{channel_id}>의 뉴스 전송이 취소되었습니다.", ephemeral=True)
            logger.info(f"[Guild: {guild_id}, Channel: {channel_id}] 뉴스 전송 취소 완료.")

    # 하위 명령어: 뉴스 조회
    @news_group.command(
        name="조회",
        description="현재 서버에 등록된 뉴스 전송 채널과 키워드를 조회합니다."
    )
    @app_commands.default_permissions(manage_channels=True)
    @is_not_dm()
    async def view_news(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        logger.info(f"[Guild: {guild_id}] 뉴스조회 명령어 실행.")

        news_data = await self.news_manager.load_news_data(guild_id)
        channels_data = news_data.get("channels", {})

        if not channels_data:
            await interaction.followup.send("현재 이 서버에는 등록된 뉴스 전송 채널이 없습니다.", ephemeral=True)
            logger.info(f"[Guild: {guild_id}] 뉴스 전송 채널 조회: 없음.")
            return

        embed = Embed(title="등록된 뉴스 전송 채널", color=discord.Color.green())
        for channel_id, info in channels_data.items():
            channel = self.bot.get_channel(int(channel_id))
            channel_name = channel.mention if channel else f"채널 ID: {channel_id}"
            query = info.get("query", "키워드 없음")
            embed.add_field(name=channel_name, value=f"키워드: `{query}`", inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.info(f"[Guild: {guild_id}] 뉴스 전송 채널 조회 완료.")

async def setup(bot: commands.Bot):
    try:
        if "NewsCog" not in bot.cogs:
            await bot.add_cog(NewsCog(bot))
            logger.info("NewsCog이 성공적으로 로드되었습니다.")
    except Exception as e:
        logger.error(f"NewsCog 로드 중 오류 발생: {e}")
