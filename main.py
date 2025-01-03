# main.py
from typing import Optional

import asyncio
import json
import logging
import os
import ssl
import aiohttp
import discord
from discord import Interaction, app_commands
from discord.ext import commands
from dotenv import load_dotenv

# 로깅 설정
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger(__name__)

# 로깅 레벨을 환경 변수로 설정 (기본값 ERROR)
LOG_LEVEL = "ERROR"

logger.setLevel(getattr(logging, LOG_LEVEL))

# 기존 핸들러 제거
for handler in logger.handlers[:]:
    logger.removeHandler(handler)

# 핸들러 생성
file_handler = logging.FileHandler(filename=os.path.join(LOG_DIR, 'bot.log'), encoding='utf-8', mode='a')
stream_handler = logging.StreamHandler()

# 포매터 설정
file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
console_formatter = logging.Formatter('%(levelname)s: %(message)s')  # 간단한 포맷

file_handler.setFormatter(file_formatter)
stream_handler.setFormatter(console_formatter)

# 핸들러 추가
logger.addHandler(file_handler)
logger.addHandler(stream_handler)

# propagate 설정 (중복 로깅 방지)
logger.propagate = False

logger.info(f"로깅 레벨이 {LOG_LEVEL}로 설정되었습니다.")

# .env 파일에서 환경 변수 로드
ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path=ENV_PATH)

# 환경 변수 로드 및 확인
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    logger.error("DISCORD_TOKEN 환경 변수가 설정되지 않았습니다. .env 파일을 확인하세요.")
    raise ValueError("DISCORD_TOKEN 환경 변수가 누락되었습니다.")

# SSL 설정
ssl_context = ssl.create_default_context()

class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.message_content = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self.session = None
        self._loaded_cogs = set()

    async def setup_hook(self):
        self.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context))

        # Cog 자동 로딩
        for filename in os.listdir('./cogs'):
            if filename.endswith('.py') and filename != '__init__.py':
                extension = f'cogs.{filename[:-3]}'
                if extension not in self._loaded_cogs:
                    try:
                        await self.load_extension(extension)
                        self._loaded_cogs.add(extension)
                        logger.info(f'{extension} 로드 완료.')
                    except Exception as e:
                        logger.error(f'{extension} 로드 중 오류 발생: {e}')

        # 슬래시 명령어 동기화
        try:
            await self.tree.sync()
            logger.info("슬래시 명령어 동기화 완료!")
        except Exception as e:
            logger.error(f"슬래시 명령어 동기화 중 오류: {e}")

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        activity = discord.Game(name="/도움말")
        await self.change_presence(status=discord.Status.online, activity=activity)
        logger.info("봇이 준비되었습니다!")

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
        await super().close()
        logger.info("봇이 안전하게 종료되었습니다.")

# 봇 객체 생성
bot = MyBot()

# 전역 예외 핸들러 추가 (메인 스크립트에 위치)
@bot.event
async def on_error(event, *args, **kwargs):
    logger.exception(f"Unhandled exception in event {event}")

# 슬래시 명령어 에러 핸들러
@bot.tree.error
async def on_app_command_error(interaction: Interaction, error: app_commands.AppCommandError):
    try:
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"명령어를 너무 자주 사용했습니다. {error.retry_after:.1f}초 후에 다시 시도하세요.",
                ephemeral=True
            )
        elif isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(
                "이 명령어를 사용할 권한이 없거나 DM에서 사용할 수 없는 명령어입니다.",
                ephemeral=True
            )
        elif isinstance(error, app_commands.CommandInvokeError):
            logger.error(f"명령어 실행 중 오류 발생: {error.original}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "명령어 실행 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "명령어 실행 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
                    ephemeral=True
                )
        else:
            logger.error(f"예상치 못한 명령어 오류: {error}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "알 수 없는 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
                    ephemeral=True
                )
    except Exception as e:
        logger.error(f"에러 핸들러에서 오류 발생: {e}")

# 슬래시 명령어: 도움말
@bot.tree.command(
    name="도움말",
    description="기능 그룹 목록 또는 특정 기능의 상세 도움말을 확인합니다."
)
@app_commands.describe(
    category="도움말을 볼 기능 카테고리 (예: 관리, 곡 등)"
)
async def help_command(interaction: Interaction, category: Optional[str] = None):
    try:
        await interaction.response.defer()

        if not category:
            # 전체 기능 그룹 및 특수 명령어 목록 표시
            embed = discord.Embed(
                title="🔍 도움말: 기능 및 명령어 목록",
                description="사용 가능한 모든 기능 그룹 및 특수 명령어입니다.\n각 기능의 자세한 설명을 보려면 `/도움말 [카테고리]`를 입력하세요.",
                color=discord.Color.blue()
            )

            # 그룹 명령어 목록 추가
            for command in bot.tree.get_commands():
                if isinstance(command, app_commands.Group):
                    embed.add_field(
                        name=f"📎 {command.name.capitalize()}",
                        value=f"`/도움말 {command.name}`으로 자세히 보기",
                        inline=False
                    )

            # 특수 명령어 목록 추가
            standalone_commands = [
                cmd for cmd in bot.tree.get_commands() if not isinstance(cmd, app_commands.Group)
            ]
            if standalone_commands:
                special_commands = "\n".join(
                    [f"/{cmd.name} - {cmd.description}" for cmd in standalone_commands]
                )
                embed.add_field(
                    name="--특수 명령어--",
                    value=special_commands,
                    inline=False
                )

            await interaction.followup.send(embed=embed)
            return

        # 특정 카테고리 도움말 표시
        category = category.lower()
        group = bot.tree.get_command(category)

        if not group or not isinstance(group, app_commands.Group):
            await interaction.followup.send(
                f"❌ '{category}' 카테고리를 찾을 수 없습니다.\n사용 가능한 카테고리 목록을 보려면 `/도움말`을 입력하세요.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"📚 {category.capitalize()} 카테고리 도움말",
            description=f"{category.capitalize()} 카테고리에서 사용할 수 있는 모든 명령어입니다.",
            color=discord.Color.blue()
        )

        for subcommand in group.commands:
            name = f"/{group.name} {subcommand.name}"
            params = []
            for param in subcommand.parameters:
                param_desc = f"[{param.name}]" if param.required else f"({param.name})"
                params.append(param_desc)

            usage = f"{name} {' '.join(params)}"
            value = f"💡 {subcommand.description}"
            if params:
                value += f"\n```사용법: {usage}```"

            embed.add_field(name=usage, value=value, inline=False)

        embed.set_footer(text="[] = 필수 항목, () = 선택 항목")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        logger.error(f"도움말 명령어 실행 중 오류 발생: {e}")
        await interaction.followup.send(
            "도움말을 불러오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
            ephemeral=True
        )

async def main():
    try:
        async with bot:
            await bot.start(DISCORD_TOKEN)
    except asyncio.CancelledError:
        logger.info("Bot is shutting down gracefully...")
    except Exception as e:
        logger.error(f"봇 실행 중 예기치 않은 오류 발생: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt detected. Shutting down...")
    except asyncio.CancelledError:
        logger.info("Asyncio task was cancelled. Gracefully exiting...")
    except Exception as e:
        logger.error(f"프로그램 실행 중 예기치 않은 오류 발생: {e}")
    finally:
        logger.info("프로그램 종료 완료.")
