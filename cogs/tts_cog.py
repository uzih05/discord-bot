# cogs/tts_cog.py
from typing import Optional

import os
import asyncio
import discord
from discord.ext import commands
from discord import app_commands, Interaction
from utils.common_checks import is_not_dm

from gtts import gTTS
import logging
import aiofiles
import aiofiles.os
import uuid

from typing import Optional, Dict

from utils.tts_config_manager import TTSConfigManager  # 추가


logger = logging.getLogger(__name__)

# TTS를 위한 폴더 준비
TTS_TEMP_DIR = os.path.join("cogs_data", "tts_cog", "tts_temp")
os.makedirs(TTS_TEMP_DIR, exist_ok=True)

class TTSCog(commands.Cog):
    # tts_group 정의
    tts_group = app_commands.Group(name="tts", description="TTS 관련 명령어를 제공합니다.")

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # TTSConfigManager를 이용해 서버별 TTS 채널 설정을 관리
        config_path = os.path.join("cogs_data", "tts_cog", "config.json")
        self.config_manager = TTSConfigManager(config_path)

        # guild_id -> 음성 클라이언트
        self.tts_enabled_guilds: Dict[int, discord.VoiceClient] = {}
        # guild_id -> voice_channel_id
        self.voice_channel: Dict[int, int] = {}
        # guild_id -> asyncio.Queue (TTS 재생 큐)
        self.tts_queue: Dict[int, asyncio.Queue] = {}
        # user_id -> 언어코드
        self.user_voice_preferences: Dict[int, str] = {}

        self.cleanup_task = self.bot.loop.create_task(self.cleanup_files())

        # on_message 이벤트 등록
        bot.add_listener(self.on_message, "on_message")

        # **load_config() 비동기 호출**
        self.bot.loop.create_task(self.load_initial_config())

        logger.info("TTSCog 초기화 완료.")

    async def load_initial_config(self):
        """봇 로드 시 TTS config JSON 불러오기"""
        await self.config_manager.load_config()
        logger.info("TTS config가 성공적으로 로드되었습니다.")

    async def cog_unload(self):
        try:
            await self.disconnect_all()
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                logger.info("TTS 파일 정리 Task가 취소되었습니다.")
            logger.info("TTSCog 언로드 완료.")
        except Exception as e:
            logger.error(f"TTSCog 언로드 중 오류 발생: {e}")

    async def disconnect_all(self):
        logger.info("모든 음성 클라이언트를 종료합니다.")
        for guild_id, voice_client in self.tts_enabled_guilds.items():
            if voice_client.is_connected():
                try:
                    await voice_client.disconnect()
                    logger.info(f"Guild ID {guild_id}에서 음성 클라이언트를 종료했습니다.")
                except Exception as e:
                    logger.error(f"Guild ID {guild_id}에서 음성 채널 연결 해제 중 오류 발생: {e}")
        self.tts_enabled_guilds.clear()
        self.voice_channel.clear()
        self.tts_queue.clear()
        # config_manager는 메모리 해제 없이 유지, on_unload 때만

    async def cleanup_files(self):
        """정기적으로 TTS 파일을 정리하는 Task"""
        while True:
            try:
                if os.path.exists(TTS_TEMP_DIR):
                    for filename in os.listdir(TTS_TEMP_DIR):
                        file_path = os.path.join(TTS_TEMP_DIR, filename)
                        try:
                            if os.path.isfile(file_path):
                                await aiofiles.os.remove(file_path)
                                logger.debug(f"파일 삭제됨: {file_path}")
                        except Exception as e:
                            logger.error(f"파일 삭제 중 오류 발생: {file_path} - {e}")
                await asyncio.sleep(3600)  # 1시간마다 정리
            except asyncio.CancelledError:
                logger.info("TTS 파일 정리 Task가 취소되었습니다.")
                break
            except Exception as e:
                logger.error(f"TTS 파일 정리 중 오류 발생: {e}")
                await asyncio.sleep(3600)

    # -------------------------------------------------------------------
    # /tts on
    # -------------------------------------------------------------------
    @tts_group.command(name="on", description="현재 음성 채널에서 메시지를 읽어주는 TTS 모드를 켭니다.")
    @app_commands.default_permissions(manage_channels=True)
    @is_not_dm()
    async def tts_on(self, interaction: Interaction):
        try:
            guild = interaction.guild
            if guild is None:
                await interaction.response.send_message("이 명령어는 서버에서만 사용할 수 있습니다.", ephemeral=True)
                return

            if not interaction.user.voice or not interaction.user.voice.channel:
                await interaction.response.send_message("먼저 음성 채널에 접속해주세요.", ephemeral=True)
                return

            channel = interaction.user.voice.channel
            guild_id = guild.id

            if guild_id in self.tts_enabled_guilds:
                await interaction.response.send_message("이미 TTS 모드가 활성화되어 있습니다.", ephemeral=True)
                return

            permissions = channel.permissions_for(guild.me)
            if not permissions.connect or not permissions.speak:
                await interaction.response.send_message("봇에게 음성 채널 접속 / 발화 권한이 없습니다.", ephemeral=True)
                return

            try:
                voice_client = await channel.connect()
                self.tts_enabled_guilds[guild_id] = voice_client
                self.voice_channel[guild_id] = channel.id
                self.tts_queue[guild_id] = asyncio.Queue()

                self.bot.loop.create_task(self.process_tts_queue(guild_id))

                await interaction.response.send_message(
                    f"음성 채널 **{channel.name}**에서 TTS 모드를 활성화했습니다."
                )
                logger.info(f"Guild ID {guild_id} TTS on.")
            except Exception as e:
                logger.error(f"음성 채널 연결 오류: {e}")
                await interaction.response.send_message("음성 채널에 접속할 수 없습니다.", ephemeral=True)
        except Exception as e:
            logger.error(f"tts_on 명령어 실행 중 오류: {e}")
            await interaction.response.send_message("명령어 실행 중 오류가 발생했습니다.", ephemeral=True)

    # -------------------------------------------------------------------
    # /tts off
    # -------------------------------------------------------------------
    @tts_group.command(name="off", description="TTS 모드를 종료합니다.")
    @app_commands.default_permissions(manage_channels=True)
    @is_not_dm()
    async def tts_off(self, interaction: Interaction):
        try:
            guild = interaction.guild
            if guild is None:
                await interaction.response.send_message("이 명령어는 서버에서만 사용할 수 있습니다.", ephemeral=True)
                return

            guild_id = guild.id
            voice_client = self.tts_enabled_guilds.pop(guild_id, None)

            if voice_client and voice_client.is_connected():
                try:
                    await voice_client.disconnect()
                except Exception as e:
                    logger.error(f"음성 채널 연결 해제 중 오류 발생: {e}")

            self.voice_channel.pop(guild_id, None)
            self.tts_queue.pop(guild_id, None)

            # JSON config에서 채널 설정을 지우고 싶다면 다음과 같이:
            # (필수는 아니며, 남겨두면 다음에 /tts on 할 때 그대로 유지)
            # guild_key = str(guild_id)
            # if guild_key in self.config_manager._data:
            #     self.config_manager._data.pop(guild_key)
            #     await self.config_manager.save_config()

            await interaction.response.send_message("TTS 모드를 종료했습니다.", ephemeral=True)
            logger.info(f"Guild ID {guild_id} TTS off.")
        except Exception as e:
            logger.error(f"tts_off 명령어 실행 중 오류: {e}")
            await interaction.response.send_message("오류가 발생했습니다.", ephemeral=True)

    # -------------------------------------------------------------------
    # /tts channel
    # -------------------------------------------------------------------
    @tts_group.command(name="channel", description="TTS 자동 읽기용 텍스트 채널을 지정합니다.")
    @app_commands.describe(채널="TTS 기능이 동작할 텍스트 채널")
    @is_not_dm()
    async def tts_channel(self, interaction: Interaction, 채널: discord.TextChannel):
        """해당 텍스트 채널에서의 대화를 TTS로 읽어주는 기능을 설정 + JSON 저장"""
        try:
            guild = interaction.guild
            if guild is None:
                await interaction.response.send_message("이 명령어는 서버에서만 사용할 수 있습니다.", ephemeral=True)
                return

            guild_id = guild.id
            if guild_id not in self.tts_enabled_guilds:
                await interaction.response.send_message(
                    "먼저 `/tts on`으로 TTS 모드를 켜주세요.", ephemeral=True
                )
                return

            # JSON 파일에도 저장
            await self.config_manager.set_text_channel_id(guild_id, 채널.id)

            await interaction.response.send_message(f"TTS 전용 채널을 **{채널.mention}**으로 설정했습니다.")
            logger.info(f"Guild ID {guild_id} TTS channel set to {채널.name}.")
        except Exception as e:
            logger.error(f"tts_channel 명령어 실행 중 오류: {e}")
            await interaction.response.send_message("오류가 발생했습니다.", ephemeral=True)

    # -------------------------------------------------------------------
    # /tts voice
    # -------------------------------------------------------------------
    @tts_group.command(name="voice", description="TTS 음성 언어를 변경합니다 (ko, en 등).")
    @app_commands.describe(언어코드="gTTS가 지원하는 언어코드 (ko, en, ja 등)")
    @is_not_dm()
    async def tts_voice(self, interaction: Interaction, 언어코드: str):
        try:
            user_id = interaction.user.id
            self.user_voice_preferences[user_id] = 언어코드.lower()
            await interaction.response.send_message(
                f"{interaction.user.display_name}님의 TTS 언어가 `{언어코드.lower()}`로 설정되었습니다.",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"tts_voice 명령어 오류: {e}")
            await interaction.response.send_message("오류가 발생했습니다.", ephemeral=True)

    # -------------------------------------------------------------------
    # /tts read (수동)
    # -------------------------------------------------------------------
    @tts_group.command(name="read", description="메시지를 즉시 음성으로 읽어줍니다.")
    @app_commands.describe(메시지="읽어줄 메시지를 입력하세요.")
    @is_not_dm()
    async def tts_read(self, interaction: Interaction, 메시지: str):
        try:
            await interaction.response.defer(ephemeral=True)
            guild = interaction.guild
            if guild is None:
                await interaction.followup.send("이 명령어는 서버에서만 사용할 수 있습니다.", ephemeral=True)
                return

            guild_id = guild.id
            if guild_id not in self.tts_enabled_guilds:
                await interaction.followup.send("TTS 모드가 켜져있지 않습니다.", ephemeral=True)
                return

            if not 메시지.strip():
                await interaction.followup.send("읽을 메시지를 입력하세요.", ephemeral=True)
                return

            await self.tts_queue[guild_id].put((interaction.user.id, 메시지))
            await interaction.followup.send("메시지를 음성으로 읽어드립니다.", ephemeral=True)
        except Exception as e:
            logger.error(f"tts_read 명령어 오류: {e}")
            await interaction.followup.send("오류가 발생했습니다.", ephemeral=True)

    # -------------------------------------------------------------------
    # on_message
    # -------------------------------------------------------------------
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        guild = message.guild
        if not guild:
            return

        guild_id = guild.id
        if guild_id not in self.tts_enabled_guilds:
            return

        voice_client = self.tts_enabled_guilds[guild_id]
        if not voice_client or not voice_client.is_connected():
            return

        # JSON 에서 채널 설정 가져오기
        tts_channel_id = self.config_manager.get_text_channel_id(guild_id)
        if not tts_channel_id or (message.channel.id != tts_channel_id):
            return

        # 작성자가 봇과 같은 음성 채널에 있는지 확인
        author_voice_state = message.author.voice
        if not author_voice_state or not author_voice_state.channel:
            return
        if author_voice_state.channel.id != voice_client.channel.id:
            return

        text_to_read = message.content.strip()
        if not text_to_read:
            return

        user_id = message.author.id
        await self.tts_queue[guild_id].put((user_id, text_to_read))

    # -------------------------------------------------------------------
    # 내부 로직: 재생
    # -------------------------------------------------------------------
    async def tts_convert_and_play(self, guild_id: int, user_id: int, text: str):
        if guild_id not in self.tts_enabled_guilds:
            return

        voice_client = self.tts_enabled_guilds[guild_id]
        if not voice_client.is_connected():
            self.tts_enabled_guilds.pop(guild_id, None)
            self.voice_channel.pop(guild_id, None)
            return

        lang_code = self.user_voice_preferences.get(user_id, "ko")
        unique_id = uuid.uuid4().hex
        tts_file = os.path.join(TTS_TEMP_DIR, f"tts_{guild_id}_{unique_id}.mp3")

        try:
            tts = gTTS(text=text, lang=lang_code)
            tts.save(tts_file)
            logger.debug(f"TTS 파일 생성 완료: {tts_file}")
        except Exception as e:
            logger.error(f"gTTS 변환 실패: {e}")
            return

        if not os.path.exists(tts_file):
            logger.error("TTS 파일이 생성되지 않았습니다.")
            return

        if not voice_client.is_playing():
            def after_playback(error):
                if error:
                    logger.error(f"TTS 재생 오류: {error}")
                coro = self.delete_file(tts_file)
                asyncio.run_coroutine_threadsafe(coro, self.bot.loop)

            try:
                voice_client.play(
                    discord.FFmpegPCMAudio(tts_file),
                    after=after_playback
                )
                logger.info(f"TTS 재생 시작: {tts_file}")
            except Exception as e:
                logger.error(f"TTS 재생 중 오류 발생: {e}")
        else:
            # 이미 다른 TTS 재생 중인 경우 -> 큐 처리 흐름상 자동 대기
            logger.debug(f"현재 TTS 재생 중. 큐에 추가됨: {tts_file}")

    async def process_tts_queue(self, guild_id: int):
        while guild_id in self.tts_enabled_guilds:
            try:
                user_id, msg = await self.tts_queue[guild_id].get()
                await self.tts_convert_and_play(guild_id, user_id, msg)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"TTS 큐 처리 오류: {e}")

    async def delete_file(self, file_path: str):
        try:
            if await aiofiles.os.path.exists(file_path):
                await aiofiles.os.remove(file_path)
                logger.debug(f"파일 삭제됨: {file_path}")
        except Exception as e:
            logger.error(f"파일 삭제 오류: {file_path} - {e}")

async def setup(bot: commands.Bot):
    try:
        if "TTSCog" not in bot.cogs:
            await bot.add_cog(TTSCog(bot))
            logger.info("TTSCog이 성공적으로 로드되었습니다.")
    except Exception as e:
        logger.error(f"TTSCog 로드 중 오류: {e}")
