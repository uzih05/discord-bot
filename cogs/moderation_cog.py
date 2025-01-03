import discord
from discord.ext import commands
from discord import app_commands
from discord.utils import utcnow  # 추가 필요
import json
import os
import asyncio
from datetime import datetime, timedelta
from typing import Optional
import threading
import re
import logging
import logging.handlers
import sys

BASE_DATA_DIR = "./cogs_data/moderation_cog"
data_lock = threading.Lock()

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)

def get_server_data_path(guild_id, filename):
    """서버별 데이터 경로 생성"""
    server_dir = os.path.join(BASE_DATA_DIR, str(guild_id))
    os.makedirs(server_dir, exist_ok=True)
    return os.path.join(server_dir, filename)

def get_default_config():
    """기본 설정 데이터 반환"""
    return {
        "auto_filtering": [],
        "filter_warnings": {},  # 단어별 경고 횟수
        "warnings_enabled": False,
        "warnings_threshold": 3,
        "warnings_action": "timeout",
        "timeout_duration": 10,
        "logging_enabled": False,
        "log_channel_id": None
    }

class ModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.pending_setup = set()
        logger.info("ModerationCog initialized")



    def initialize_server_data(self, guild_id):
        config_path = get_server_data_path(guild_id, "config.json")
        warnings_path = get_server_data_path(guild_id, "warnings.json")

        if not os.path.exists(config_path):
            self.save_data(guild_id, "config.json", get_default_config())
            print(f"서버 {guild_id}의 초기 데이터를 생성했습니다.")

        if not os.path.exists(warnings_path):
            self.save_data(guild_id, "warnings.json", {})

    def load_data(self, guild_id, filename):
        path = get_server_data_path(guild_id, filename)
        with data_lock:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            else:
                # 파일 이름에 따라 기본값을 다르게 설정
                if filename == "config.json":
                    default_data = get_default_config()
                elif filename == "warnings.json":
                    default_data = {}
                else:
                    default_data = {}
                self.save_data(guild_id, filename, default_data)
                return default_data

    def save_data(self, guild_id, filename, data):
        path = get_server_data_path(guild_id, filename)
        with data_lock:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)

    moderation_group = app_commands.Group(name="관리", description="서버 관리 명령어")

    ### 기존 명령어 수정 ###
    @moderation_group.command(name="경고설정", description="최대 경고 횟수, 초과 시 조치, 타임아웃 지속 시간을 설정합니다.")
    @app_commands.describe(
        max_warnings="최대 허용 경고 횟수",
        action="경고 초과 시 조치 (kick, ban, timeout 중 하나)",
        timeout_duration="타임아웃 조치일 경우 지속 시간 (분 단위, 기본값 10분)"
    )
    async def set_warnings(self, interaction: discord.Interaction, max_warnings: int, action: str,
                           timeout_duration: Optional[int] = 10):
        guild_id = interaction.guild_id
        config = self.load_data(guild_id, "config.json")

        # 허용된 액션만 설정 가능
        allowed_actions = ["kick", "ban", "timeout"]
        if action not in allowed_actions:
            await interaction.response.send_message(
                f"'{action}'은 유효한 조치가 아닙니다. 허용된 조치: {', '.join(allowed_actions)}",
                ephemeral=True
            )
            return

        config["warnings_enabled"] = True  # 경고 시스템 활성화
        config["warnings_threshold"] = max_warnings
        config["warnings_action"] = action

        # 타임아웃 지속 시간 설정
        if action == "timeout":
            if timeout_duration <= 0:
                await interaction.response.send_message(
                    "타임아웃 지속 시간은 1분 이상이어야 합니다.",
                    ephemeral=True
                )
                return
            config["timeout_duration"] = timeout_duration

        self.save_data(guild_id, "config.json", config)

        response_message = (
            f"최대 경고 횟수가 {max_warnings}로 설정되었으며, 초과 시 조치는 '{action}'으로 설정되었습니다."
        )
        if action == "timeout":
            response_message += f" 타임아웃 지속 시간은 {timeout_duration}분입니다."

        await interaction.response.send_message(response_message)

    ### 경고 시스템 해제 명령어 ###
    @moderation_group.command(name="경고해제", description="경고 시스템을 비활성화합니다.")
    async def disable_warnings(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        config = self.load_data(guild_id, "config.json")

        if not config.get("warnings_enabled", True):
            await interaction.response.send_message("경고 시스템이 이미 비활성화되어 있습니다.", ephemeral=True)
            return

        config["warnings_enabled"] = False
        self.save_data(guild_id, "config.json", config)

        await interaction.response.send_message("경고 시스템이 비활성화되었습니다.")

    @moderation_group.command(name="경고상태", description="현재 서버의 경고 상태를 확인합니다.")
    async def warnings_status(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        warnings = self.load_data(guild_id, "warnings.json")
        if not warnings:
            await interaction.response.send_message("현재 경고 상태가 없습니다.")
            return
        warning_list = "\n".join([f"{user}: {count}회" for user, count in warnings.items()])
        await interaction.response.send_message(f"현재 경고 상태:\n{warning_list}")

    @moderation_group.command(name="경고초기화", description="서버의 경고 시스템을 초기화합니다.")
    async def reset_warnings(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        warnings_path = get_server_data_path(guild_id, "warnings.json")
        if os.path.exists(warnings_path):
            os.remove(warnings_path)
            self.save_data(guild_id, "warnings.json", {})
            await interaction.response.send_message("서버의 경고 시스템이 초기화되었습니다.")
        else:
            await interaction.response.send_message("초기화할 경고 데이터가 없습니다.")

    @moderation_group.command(name="유저", description="특정 유저의 경고 상태를 확인합니다.")
    @app_commands.describe(user="경고 상태를 확인할 유저")
    async def user_warnings(self, interaction: discord.Interaction, user: discord.User):
        guild_id = interaction.guild_id
        warnings = self.load_data(guild_id, "warnings.json")
        user_id = str(user.id)
        user_warnings = warnings.get(user_id, 0)
        await interaction.response.send_message(f"{user.mention}의 현재 경고 상태: {user_warnings}회")

    @moderation_group.command(name="유저경고", description="특정 유저의 경고 횟수를 조작합니다.")
    @app_commands.describe(user="경고를 조작할 유저", count="설정할 경고 횟수")
    async def modify_user_warnings(self, interaction: discord.Interaction, user: discord.User, count: int):
        guild_id = interaction.guild_id
        config = self.load_data(guild_id, "config.json")
        warnings = self.load_data(guild_id, "warnings.json")
        user_id = str(user.id)

        warnings[user_id] = count
        self.save_data(guild_id, "warnings.json", warnings)

        await interaction.response.send_message(f"{user.mention}의 경고 횟수를 {count}로 설정했습니다.")

        # 경고 초과 시 조치 실행
        if count >= config["warnings_threshold"]:
            action_msg = await self.execute_action(interaction.guild, user, config)
            await interaction.followup.send(action_msg)

    ### 필터 명령어들 ###
    @moderation_group.command(name="필터추가", description="금지 단어를 추가하고 경고 횟수를 설정합니다.")
    @app_commands.describe(
        words="추가할 금지 단어들 (쉼표로 구분)",
        warnings="각 단어에 부여할 경고 횟수 (기본값 1)"
    )
    async def add_filter(self, interaction: discord.Interaction, words: str, warnings: Optional[int] = 1):
        guild_id = interaction.guild_id
        config = self.load_data(guild_id, "config.json")

        if not config.get("filter_warnings"):
            config["filter_warnings"] = {}

        new_words = [word.strip() for word in words.split(",") if word.strip()]
        for word in new_words:
            config["filter_warnings"][word] = warnings

        self.save_data(guild_id, "config.json", config)
        await interaction.response.send_message(
            f"다음 단어들이 금지 단어로 추가되었습니다: {', '.join(new_words)} (경고 {warnings}회)"
        )

    @moderation_group.command(name="필터리스트", description="현재 설정된 금지 단어와 경고 횟수를 확인합니다.")
    async def list_filters(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        config = self.load_data(guild_id, "config.json")
        filters = config.get("filter_warnings", {})

        if not filters:
            await interaction.response.send_message("현재 설정된 금지 단어가 없습니다.")
            return

        filter_list = "\n".join([f"'{word}': {count}회 경고" for word, count in filters.items()])
        await interaction.response.send_message(f"현재 설정된 금지 단어 목록:\n{filter_list}")

    @moderation_group.command(name="필터초기화", description="모든 필터된 단어를 초기화합니다.")
    async def reset_filters(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        config = self.load_data(guild_id, "config.json")
        config["filter_warnings"] = {}
        self.save_data(guild_id, "config.json", config)
        await interaction.response.send_message("모든 필터된 단어가 초기화되었습니다.")

    @moderation_group.command(name="로그활성화", description="로그 기록을 활성화합니다.")
    async def logging_enable(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        config = self.load_data(guild_id, "config.json")
        config["logging_enabled"] = True
        self.save_data(guild_id, "config.json", config)
        await interaction.response.send_message("로그 기록이 활성화되었습니다.")

    @moderation_group.command(name="로그비활성화", description="로그 기록을 비활성화합니다.")
    async def logging_disable(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        config = self.load_data(guild_id, "config.json")
        config["logging_enabled"] = False
        self.save_data(guild_id, "config.json", config)
        await interaction.response.send_message("로그 기록이 비활성화되었습니다.")

    @moderation_group.command(name="로그채널설정", description="로그를 기록할 채널을 설정합니다.")
    @app_commands.describe(channel_id="로그 채널의 ID")
    async def set_log_channel(self, interaction: discord.Interaction, channel_id: str):
        guild_id = interaction.guild_id
        config = self.load_data(guild_id, "config.json")
        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            await interaction.response.send_message("유효하지 않은 채널 ID입니다.")
            return
        config["log_channel_id"] = int(channel_id)
        self.save_data(guild_id, "config.json", config)
        await interaction.response.send_message(f"로그 채널이 {channel.mention}으로 설정되었습니다.")

    @commands.Cog.listener()
    async def on_ready(self):
        """Initialize data for all guilds when the bot is ready"""
        logger.info("Initializing data for all guilds...")
        for guild in self.bot.guilds:
            await self.ensure_guild_data(guild.id)
        logger.info("Guild data initialization complete")

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("이 명령어를 실행할 권한이 없습니다.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"필수 인자가 누락되었습니다: {error.param}")
        else:
            await ctx.send("명령어 실행 중 오류가 발생했습니다.")
            print(f"Unhandled error: {error}")

    ### 메시지 필터링 수정 ###
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        try:
            # Skip if message is from bot or not in a guild
            if message.author.bot or not message.guild:
                return

            guild_id = message.guild.id
            config = self.load_data(guild_id, "config.json")

            # Skip if warnings are disabled
            if not config.get("warnings_enabled", True):
                return

            # Get current warnings
            warnings = self.load_data(guild_id, "warnings.json")

            # Check for forbidden words
            detected_words = []
            for word, warn_count in config.get("filter_warnings", {}).items():
                if re.search(re.escape(word), message.content, re.IGNORECASE):
                    detected_words.append((word, warn_count))

            if detected_words:
                # Create warning message
                warning_msg_content = (
                        f"{message.author.mention}, 다음 금지 단어를 사용하여 메시지가 삭제되었습니다: "
                        + ", ".join([f"'{word}'" for word, _ in detected_words])
                        + ". "
                        + " ".join([f"{warn_count} 회 경고" for _, warn_count in detected_words])
                        + "이 부여되었습니다."
                )

                try:
                    # Try to delete the original message
                    await message.delete()

                    # Send and then delete warning message
                    warning_msg = await message.channel.send(warning_msg_content)
                    await asyncio.sleep(3)
                    await warning_msg.delete()

                except discord.Forbidden:
                    pass  # Skip if no permission
                except discord.NotFound:
                    pass  # Skip if message already deleted
                except Exception as e:
                    print(f"Error in message handling: {e}")
                    return

                # Update warnings count
                user_id = str(message.author.id)
                total_warns = sum(warn_count for _, warn_count in detected_words)
                current_warnings = warnings.get(user_id, 0) + total_warns
                warnings[user_id] = current_warnings
                self.save_data(guild_id, "warnings.json", warnings)

                # Check if warnings exceeded threshold
                if current_warnings >= config["warnings_threshold"]:
                    try:
                        action_msg = await self.execute_action(message.guild, message.author, config)
                        if action_msg:
                            await message.channel.send(action_msg)
                    except Exception as e:
                        print(f"Error executing action: {e}")

        except Exception as e:
            print(f"Error in on_message: {e}")
            # Don't raise the exception - this keeps the bot running

    # Add these helper functions to improve reliability:

    async def safe_send(self, channel, content):
        try:
            return await channel.send(content)
        except discord.Forbidden:
            print(f"Missing permissions to send message in {channel}")
        except Exception as e:
            print(f"Error sending message: {e}")
        return None

    async def safe_delete(self, message):
        try:
            await message.delete()
            return True
        except discord.Forbidden:
            print("Missing permissions to delete message")
        except discord.NotFound:
            print("Message already deleted")
        except Exception as e:
            print(f"Error deleting message: {e}")
        return False

    # Add this method to your ModerationCog class - this was missing in your implementation:

    async def execute_action(self, guild, user, config):
        """Execute the configured punishment action"""
        try:
            action_msg = None
            log_channel_id = config.get("log_channel_id")
            log_channel = self.bot.get_channel(log_channel_id) if log_channel_id else None

            action = config["warnings_action"]

            if action == "kick":
                if not guild.me.guild_permissions.kick_members:
                    logger.warning(f"Missing kick permissions in guild {guild.id}")
                    return "킥 권한이 없습니다. 봇의 권한을 확인해주세요."
                await guild.kick(user, reason="Warning threshold exceeded")
                action_msg = f"{user.mention}이(가) 서버에서 킥되었습니다."

            elif action == "ban":
                if not guild.me.guild_permissions.ban_members:
                    logger.warning(f"Missing ban permissions in guild {guild.id}")
                    return "밴 권한이 없습니다. 봇의 권한을 확인해주세요."
                await guild.ban(user, reason="Warning threshold exceeded")
                action_msg = f"{user.mention}이(가) 서버에서 밴되었습니다."

            elif action == "timeout":
                if not guild.me.guild_permissions.moderate_members:
                    logger.warning(f"Missing timeout permissions in guild {guild.id}")
                    return "타임아웃 권한이 없습니다. 봇의 권한을 확인해주세요."
                duration = config.get("timeout_duration", 10)
                until = utcnow() + timedelta(minutes=duration)
                await user.timeout(until, reason="Warning threshold exceeded")
                action_msg = f"{user.mention}이(가) {duration}분 동안 타임아웃되었습니다."

            # Log the action if logging is enabled
            if log_channel and action_msg:
                try:
                    embed = discord.Embed(
                        title="🚫 Warning Action Executed",
                        description=action_msg,
                        color=discord.Color.red(),
                        timestamp=utcnow()
                    )
                    embed.add_field(name="Action", value=action)
                    embed.add_field(name="User", value=f"{user} ({user.id})")
                    await log_channel.send(embed=embed)
                except Exception as e:
                    logger.error(f"Failed to send log message: {e}")

            return action_msg

        except discord.Forbidden:
            logger.error(f"Missing permissions to execute {action} on {user}")
            return f"{user.mention}에 대한 {action} 조치를 실행하지 못했습니다. 봇 권한이 부족합니다."
        except Exception as e:
            logger.error(f"Error executing action: {e}")
            return f"{user.mention}에 대한 조치를 실행하지 못했습니다. 오류: {e}"

    async def ensure_guild_data(self, guild_id: int):
        """Ensure guild data exists"""
        try:
            config_path = get_server_data_path(guild_id, "config.json")
            warnings_path = get_server_data_path(guild_id, "warnings.json")

            # Create default config if it doesn't exist
            if not os.path.exists(config_path):
                self.save_data(guild_id, "config.json", get_default_config())
                logger.info(f"Created default config for guild {guild_id}")

            # Create empty warnings if it doesn't exist
            if not os.path.exists(warnings_path):
                self.save_data(guild_id, "warnings.json", {})
                logger.info(f"Created empty warnings for guild {guild_id}")

            return True

        except Exception as e:
            logger.error(f"Error ensuring guild data for {guild_id}: {e}")
            return False

async def setup(bot: commands.Bot):
    try:
        await bot.add_cog(ModerationCog(bot))
        logger.info("ModerationCog loaded successfully")
    except Exception as e:
        logger.error(f"Error loading ModerationCog: {e}")
        raise
