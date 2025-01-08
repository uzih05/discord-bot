from typing import Optional, List
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

# Configure logging
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Remove existing handlers
for handler in logger.handlers[:]:
    logger.removeHandler(handler)

# Create console handler with formatter
stream_handler = logging.StreamHandler()
console_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
stream_handler.setFormatter(console_formatter)
logger.addHandler(stream_handler)
logger.propagate = False

# Load environment variables
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=ENV_PATH)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    logger.error("DISCORD_TOKEN 환경 변수가 설정되지 않았습니다. .env 파일을 확인하세요.")
    raise ValueError("DISCORD_TOKEN 환경 변수가 누락되었습니다.")


class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.message_content = True
        intents.voice_states = True

        super().__init__(command_prefix="!", intents=intents)
        self.session: Optional[aiohttp.ClientSession] = None
        self._loaded_cogs: set = set()
        self._is_closing = False

    async def setup_hook(self) -> None:
        # Create aiohttp session
        self.session = aiohttp.ClientSession()

        failed_cogs: List[tuple] = []
        cogs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cogs')

        if not os.path.exists(cogs_dir):
            logger.warning(f"Cogs directory not found: {cogs_dir}")
            return

        for filename in os.listdir(cogs_dir):
            if filename.endswith('.py') and filename != '__init__.py':
                extension = f'cogs.{filename[:-3]}'
                if extension not in self._loaded_cogs:
                    try:
                        await self.load_extension(extension)
                        self._loaded_cogs.add(extension)
                        logger.info(f"Successfully loaded extension: {extension}")
                    except Exception as e:
                        logger.error(f'{extension} load failed: {str(e)}')
                        failed_cogs.append((extension, str(e)))

        if failed_cogs:
            self.loop.create_task(self._retry_failed_cogs(failed_cogs))

    async def _retry_failed_cogs(self, failed_cogs: List[tuple]) -> None:
        await asyncio.sleep(5)
        for extension, error in failed_cogs:
            if not self._is_closing:  # Check if bot is shutting down
                try:
                    await self.load_extension(extension)
                    self._loaded_cogs.add(extension)
                    logger.info(f'Successfully reloaded {extension}')
                except Exception as e:
                    logger.error(f'Retry loading {extension} failed: {str(e)}')

    async def on_ready(self) -> None:
        logger.info(f'Logged in as {self.user.name} (ID: {self.user.id})')
        self.loop.create_task(self._rotate_activity())

    async def _rotate_activity(self) -> None:
        activities = [
            discord.Game(name="/도움말"),
            discord.Activity(type=discord.ActivityType.listening, name="명령어"),
        ]

        idx = 0
        while not self._is_closing:  # Check if bot is shutting down
            try:
                activity = activities[idx]
                await self.change_presence(status=discord.Status.online, activity=activity)
                idx = (idx + 1) % len(activities)
                await asyncio.sleep(300)  # 5 minutes
            except Exception as e:
                logger.error(f"Error rotating activity: {str(e)}")
                await asyncio.sleep(60)  # Wait before retry

    async def close(self) -> None:
        """Graceful shutdown with cleanup"""
        try:
            self._is_closing = True

            # Clean up session
            if self.session and not self.session.closed:
                await self.session.close()

            # Clean up cogs
            for extension in list(self._loaded_cogs):
                try:
                    await self.unload_extension(extension)
                    logger.info(f"Successfully unloaded extension: {extension}")
                except Exception as e:
                    logger.error(f'Extension {extension} unload failed: {str(e)}')

            await super().close()
        except Exception as e:
            logger.error(f"Error during shutdown: {str(e)}")
            raise  # Re-raise the exception after logging


# Create bot instance
bot = MyBot()

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name}')
    await bot.tree.sync()

@bot.event
async def on_error(event: str, *args, **kwargs) -> None:
    logger.exception(f"Unhandled exception in event {event}")


@bot.tree.error
async def on_app_command_error(interaction: Interaction, error: app_commands.AppCommandError) -> None:
    try:
        if isinstance(error, app_commands.CommandOnCooldown):
            retry_after = round(error.retry_after)
            await interaction.response.send_message(
                f"명령어 쿨다운: {retry_after}초 남음",
                ephemeral=True
            )
            return

        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(
                "권한이 없거나 이 채널에서 사용할 수 없는 명령어입니다.",
                ephemeral=True
            )
            return

        if isinstance(error, app_commands.CommandInvokeError):
            logger.exception("Command error", exc_info=error.original)

            error_msg = "명령어 실행 중 오류가 발생했습니다."
            if isinstance(error.original, discord.HTTPException):
                error_msg = "Discord API 오류가 발생했습니다."
            elif isinstance(error.original, asyncio.TimeoutError):
                error_msg = "요청 시간이 초과되었습니다."

            if not interaction.response.is_done():
                await interaction.response.send_message(error_msg, ephemeral=True)
            else:
                await interaction.followup.send(error_msg, ephemeral=True)
            return

        # Handle any other errors
        logger.error(f"Unhandled command error: {str(error)}")
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "예기치 않은 오류가 발생했습니다.",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                "예기치 않은 오류가 발생했습니다.",
                ephemeral=True
            )

    except Exception as e:
        logger.exception(f"Error handler failed: {str(e)}")


@bot.tree.command(
    name="도움말",
    description="기능 그룹 목록 또는 특정 기능의 상세 도움말을 확인합니다."
)
@app_commands.describe(
    category="도움말을 볼 기능 카테고리 (예: 관리, 곡 등)"
)
async def help_command(interaction: Interaction, category: Optional[str] = None) -> None:
    try:
        await interaction.response.defer()

        if not category:
            embed = discord.Embed(
                title="🔍 도움말: 기능 및 명령어 목록",
                description="사용 가능한 모든 기능 그룹 및 특수 명령어입니다.\n각 기능의 자세한 설명을 보려면 `/도움말 [카테고리]`를 입력하세요.",
                color=int('f9e54b', 16)
            )

            # Add group commands
            for command in bot.tree.get_commands():
                if isinstance(command, app_commands.Group):
                    embed.add_field(
                        name=f"📎 {command.name.capitalize()}",
                        value=f"`/도움말 {command.name}`으로 자세히 보기",
                        inline=False
                    )

            # Add standalone commands
            standalone_commands = [
                cmd for cmd in bot.tree.get_commands()
                if not isinstance(cmd, app_commands.Group)
            ]

            if standalone_commands:
                special_commands = "\n".join(
                    f"/{cmd.name} - {cmd.description}"
                    for cmd in standalone_commands
                )
                embed.add_field(
                    name="--특수 명령어--",
                    value=special_commands,
                    inline=False
                )

            await interaction.followup.send(embed=embed)
            return

        # Show specific category help
        category = category.lower()
        group = bot.tree.get_command(category)

        if not group or not isinstance(group, app_commands.Group):
            await interaction.followup.send(
                f"❌ '{category}' 카테고리를 찾을 수 없습니다.\n"
                "사용 가능한 카테고리 목록을 보려면 `/도움말`을 입력하세요.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"📚 {category.capitalize()} 카테고리 도움말",
            description=f"{category.capitalize()} 카테고리에서 사용할 수 있는 모든 명령어입니다.",
            color=int('f9e54b', 16)
        )

        for subcommand in group.commands:
            name = f"/{group.name} {subcommand.name}"
            params = [
                f"[{param.name}]" if param.required else f"({param.name})"
                for param in subcommand.parameters
            ]

            usage = f"{name} {' '.join(params)}"
            value = f"💡 {subcommand.description}"
            if params:
                value += f"\n```사용법: {usage}```"

            embed.add_field(name=usage, value=value, inline=False)

        embed.set_footer(text="[] = 필수 항목, () = 선택 항목")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        logger.error(f"도움말 명령어 실행 중 오류 발생: {str(e)}")
        await interaction.followup.send(
            "도움말을 불러오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
            ephemeral=True
        )


async def main() -> None:
    try:
        async with bot:
            await bot.start(DISCORD_TOKEN)
    except Exception as e:
        logger.error(f"봇 실행 중 예기치 않은 오류 발생: {str(e)}")
        raise  # Re-raise to ensure proper shutdown


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot shutdown initiated by user")
    except Exception as e:
        logger.error(f"프로그램 실행 중 예기치 않은 오류 발생: {str(e)}")
        raise  # Re-raise for proper error handling