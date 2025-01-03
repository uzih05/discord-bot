# cogs/music_cog.py
import asyncio
import hashlib
import logging
import os
import random
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

import discord
import yt_dlp
from discord import PCMVolumeTransformer
from discord import app_commands, Interaction, Embed
from discord.ext import commands
from discord.ui import Button, View
from dataclasses import dataclass
from asyncio import Lock

# 로깅 설정 (메인 스크립트에서 이미 설정했으므로 중복되지 않게 주의)
logger = logging.getLogger(__name__)

ytdlp_format_options = {
    'format': 'bestaudio/best',  # Simpler format selection
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,  # Standard error stream에 로그를 출력하지 않음
    'quiet': True,  # INFO 레벨의 로그 메시지를 출력하지 않음
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'extract_flat': True,  # Keep this for search only
}

ffmpeg_options = {
    'options': '-vn -loglevel error -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5'
}

# 글로벌 변수
download_executor = ThreadPoolExecutor(max_workers=10)

@dataclass
class SongData:
    title: str
    url: str
    thumbnail: str
    duration: int
    file_path: Optional[str] = None
    is_downloading: bool = False
    download_future: Optional[asyncio.Future] = None

song_cache: Dict[str, SongData] = {}
song_cache_lock = Lock()  # Lock 추가

# 유틸리티 함수들
# Add this utility function near the top of your file with other utility functions
def construct_youtube_url(video_id: str) -> str:
    """Constructs a proper YouTube URL from a video ID."""
    return f"https://www.youtube.com/watch?v={video_id}"

def generate_song_id(url: str, title: str) -> str:
    combined = f"{url}-{title}"
    return hashlib.md5(combined.encode()).hexdigest()[:8]

def get_video_id(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
        if parsed.hostname in ('www.youtube.com', 'youtube.com'):
            if parsed.path == '/watch':
                return parse_qs(parsed.query)['v'][0]
        elif parsed.hostname == 'youtu.be':
            return parsed.path[1:]
    except Exception as e:
        logger.error(f"Error extracting video ID: {e}")
    return None

def is_url(url: str) -> bool:
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except ValueError:
        return False

def format_duration(seconds: int) -> str:
    try:
        if not seconds:
            return "00:00"
        hours, remainder = divmod(int(seconds), 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"
    except Exception as e:
        logger.error(f"Error formatting duration: {e}")
        return "00:00"

def clean_filename(filename: str) -> str:
    # Keep more characters while still ensuring filename safety
    import re
    # Replace invalid characters with underscore
    cleaned = re.sub(r'[\\/*?:"<>|]', '_', filename)
    # Remove leading/trailing spaces and dots
    cleaned = cleaned.strip('. ')
    # Ensure the filename isn't too long
    if len(cleaned) > 200:
        cleaned = cleaned[:197] + "..."
    return cleaned

async def handle_command_error(interaction: Interaction, error: Exception, message: str):
    error_msg = f"{message}: {str(error)}"
    logger.error(error_msg)
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
                ephemeral=True
            )
    except Exception as e:
        logger.error(f"Failed to send error message: {e}")

async def delete_message_after_delay(message: discord.Message, delay: int):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except discord.NotFound:
        pass  # 메시지가 이미 삭제된 경우 무시
    except Exception as e:
        logger.error(f"Failed to delete message: {e}")


class YTDLSource:
    def __init__(self, file_path: str, data: dict, thumbnail: str, duration: int,
                 song_id: str, volume: float = 0.05):
        self.file_path = file_path
        self.data = data
        self.thumbnail = thumbnail
        self.title = data.get('title', 'Unknown Title')
        self.duration = duration
        self.volume = volume
        self.song_id = song_id
        self.url = data.get('webpage_url')

    @classmethod
    async def cleanup_partial_downloads(cls, music_dir: str, song_id: str):
        """Clean up any partial downloads for a given song ID"""
        try:
            partial_files = [f for f in os.listdir(music_dir) if f.startswith(song_id)]
            for partial_file in partial_files:
                try:
                    os.remove(os.path.join(music_dir, partial_file))
                    logger.info(f"Cleaned up partial download: {partial_file}")
                except OSError as e:
                    logger.error(f"Failed to clean up file {partial_file}: {e}")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")

    @classmethod
    async def download_song(cls, song_id: str, url: str, music_dir: str, loop):
        try:
            # Create download options
            download_options = {
                'format': 'bestaudio/best',
                'restrictfilenames': False,  # Let us handle filename cleaning
                'noplaylist': True,
                'nocheckcertificate': True,
                'ignoreerrors': False,
                'logtostderr': False,
                'quiet': True,
                'no_warnings': True,
                'default_search': 'auto',
                'source_address': '0.0.0.0',
                'outtmpl': os.path.join(music_dir, f'{song_id}-%(title)s.%(ext)s')
            }

            logger.info(f"Starting download for song ID: {song_id}, URL: {url}")

            try:
                async with asyncio.timeout(30):
                    # Get info first
                    info = await loop.run_in_executor(
                        download_executor,
                        lambda: yt_dlp.YoutubeDL(download_options).extract_info(url, download=False)
                    )

                    if not info:
                        raise ValueError("No data received from yt-dlp")

                    if 'entries' in info:
                        info = info['entries'][0]

                    # Clean the filename before download
                    clean_title = clean_filename(info['title'])
                    download_options['outtmpl'] = os.path.join(music_dir, f'{song_id}-{clean_title}.%(ext)s')

                    # Now download with the cleaned filename
                    data = await loop.run_in_executor(
                        download_executor,
                        lambda: yt_dlp.YoutubeDL(download_options).extract_info(url, download=True)
                    )

                # Find the downloaded file
                possible_exts = ['webm', 'm4a', 'mp3', 'opus']
                file_path = None

                for ext in possible_exts:
                    temp_path = os.path.join(music_dir, f'{song_id}-{clean_title}.{ext}')
                    if os.path.exists(temp_path):
                        file_path = temp_path
                        logger.info(f"Found downloaded file: {temp_path}")
                        break

                if not file_path:
                    # Try to find any file starting with the song_id
                    for file in os.listdir(music_dir):
                        if file.startswith(song_id):
                            file_path = os.path.join(music_dir, file)
                            logger.info(f"Found alternative file: {file_path}")
                            break

                if not file_path:
                    await cls.cleanup_partial_downloads(music_dir, song_id)
                    raise FileNotFoundError(f"Downloaded file not found for song ID: {song_id}")

                # Update cache with file information
                async with song_cache_lock:
                    if song_id in song_cache:
                        song_cache[song_id].file_path = file_path
                        song_cache[song_id].is_downloading = False
                        logger.info(f"Download completed: {song_cache[song_id].title} -> {file_path}")

            except asyncio.TimeoutError:
                logger.error(f"Timeout while downloading song ID: {song_id}")
                await cls.cleanup_partial_downloads(music_dir, song_id)
                raise

            except Exception as e:
                logger.error(f"Error during download: {e}")
                await cls.cleanup_partial_downloads(music_dir, song_id)
                raise

        except Exception as e:
            logger.exception(f"Failed to download song {url}: {e}")
            async with song_cache_lock:
                if song_id in song_cache:
                    del song_cache[song_id]

    # Update the YTDLSource class's from_url method to better handle thumbnails
    @classmethod
    async def from_url(cls, url: str, download: bool, loop, music_dir: str) -> Tuple['YTDLSource', str]:
        try:
            logger.debug(f"from_url 호출: URL={url}, download={download}")
            async with song_cache_lock:
                info_options = ytdlp_format_options.copy()
                info_options['extract_flat'] = False

                try:
                    async with asyncio.timeout(10):
                        data = await loop.run_in_executor(
                            download_executor,
                            lambda: yt_dlp.YoutubeDL(info_options).extract_info(url, download=False)
                        )
                except asyncio.TimeoutError:
                    logger.error(f"Timeout while processing URL: {url}")
                    raise

                if not data:
                    raise ValueError("No data received from yt-dlp")

                if 'entries' in data:
                    data = data['entries'][0]

                # Get best quality thumbnail
                thumbnail_url = None
                if 'thumbnails' in data and isinstance(data['thumbnails'], list):
                    # Sort thumbnails by resolution if width/height available
                    thumbnails = sorted(
                        [t for t in data['thumbnails'] if isinstance(t, dict) and 'url' in t],
                        key=lambda x: x.get('width', 0) * x.get('height', 0),
                        reverse=True
                    )
                    if thumbnails:
                        thumbnail_url = thumbnails[0]['url']

                if not thumbnail_url and 'thumbnail' in data:
                    thumbnail_url = data['thumbnail']

                logger.debug(f"Selected thumbnail URL: {thumbnail_url}")

                song_id = generate_song_id(url, data['title'])
                clean_title = clean_filename(data['title'])
                file_path = os.path.join(music_dir, f'{song_id}-{clean_title}.webm')

                if song_id not in song_cache:
                    song_entry = SongData(
                        title=data['title'],
                        url=url,
                        thumbnail=thumbnail_url,
                        duration=data.get('duration', 0),
                        is_downloading=True
                    )
                    song_cache[song_id] = song_entry
                    song_entry.download_future = asyncio.create_task(
                        cls.download_song(song_id, url, music_dir, loop))

                return cls(
                    file_path=file_path if download else data.get('url'),
                    data=data,
                    thumbnail=thumbnail_url,
                    duration=data.get('duration', 0),
                    song_id=song_id,
                    volume=0.05
                ), song_id

        except Exception as e:
            logger.exception(f"Error processing URL {url}: {e}")
            raise

    @staticmethod
    async def cleanup_file(file_path: str, retries: int = 5, delay: float = 1.0):
        if not file_path or not os.path.exists(file_path):
            return

        for attempt in range(retries):
            try:
                os.remove(file_path)
                logger.info(f"File deleted: {file_path}")
                return
            except PermissionError:
                logger.warning(f"PermissionError: 파일을 삭제할 수 없습니다. 재시도 {attempt + 1}/{retries}...")
                await asyncio.sleep(delay)
        logger.error(f"Failed to delete file after {retries} attempts: {file_path}")

class SearchResultsView(View):
    def __init__(self, music_cog, interaction: Interaction, results: List[Dict]):
        super().__init__(timeout=60)
        self.music_cog = music_cog
        self.original_user = interaction.user
        self.results = results[:5]
        self.message = None

        for index, result in enumerate(self.results, start=1):
            button = Button(
                label=str(index),
                style=discord.ButtonStyle.primary,
                custom_id=f"select_{index}"
            )
            button.callback = self.create_callback(index - 1)
            self.add_item(button)

    def create_callback(self, index: int):
        async def callback(interaction: Interaction):
            if interaction.user.id != self.original_user.id:
                await interaction.response.send_message(
                    "이 버튼은 명령어를 실행한 사용자만 사용할 수 있습니다.",
                    ephemeral=True
                )
                return

            try:
                logger.debug(f"SearchResultsView 버튼 클릭: index={index}")
                if interaction.message:
                    await interaction.message.delete()

                result = self.results[index]

                # Ensure we have a valid URL
                if 'webpage_url' not in result and 'id' in result:
                    result['webpage_url'] = f"https://www.youtube.com/watch?v={result['id']}"
                elif 'webpage_url' not in result and 'url' in result:
                    result['webpage_url'] = result['url']

                if 'webpage_url' not in result:
                    raise ValueError("No valid URL found in search result")

                # Ensure we have a title
                if 'title' not in result:
                    result['title'] = f"Unknown Title {result.get('id', 'No ID')}"

                song_id = generate_song_id(result['webpage_url'], result['title'])
                player = self.music_cog.get_player(interaction.guild, interaction.channel)

                # Download status display
                embed = discord.Embed(
                    title="🔄 다운로드 중...",
                    description=f"**{result['title']}**\n잠시만 기다려주세요...",
                    color=discord.Color.blue()
                )
                status_msg = await interaction.channel.send(embed=embed)
                player.messages_to_clean.add(status_msg.id)

                await player.add_to_queue(result, song_id)

                if not player.voice_client or not player.voice_client.is_playing():
                    await player.play_next()

                await status_msg.delete()
                logger.debug(f"SearchResultsView 버튼 처리 완료: song_id={song_id}")

            except Exception as e:
                logger.exception(f"Error processing selection: {e}")
                await handle_command_error(interaction, e, "Error processing selection")

        return callback

    async def on_timeout(self):
        try:
            logger.debug("SearchResultsView 타임아웃 발생")
            for item in self.children:
                item.disabled = True
            if self.message:
                await self.message.edit(view=self)
        except Exception as e:
            logger.error(f"Error in timeout handler: {e}")

class MusicControlView(View):
    def __init__(self, player: 'MusicPlayer', music_cog: 'MusicCog'):
        super().__init__(timeout=None)
        self.player = player
        self.music_cog = music_cog

    @discord.ui.button(label="⏸️", style=discord.ButtonStyle.secondary, custom_id="pause", row=0)
    async def pause(self, interaction: Interaction, button: Button):
        if not self.player.voice_client or not self.player.voice_client.is_playing():
            await interaction.response.send_message("현재 재생 중인 곡이 없습니다.", ephemeral=True)
            return

        try:
            self.player.voice_client.pause()
            button.style = discord.ButtonStyle.primary
            button.label = "⏸️ (일시정지됨)"
            await interaction.response.edit_message(view=self)
            await interaction.followup.send("재생을 일시정지했습니다.", ephemeral=True)
            logger.debug("재생 일시정지")
        except Exception as e:
            logger.exception(f"Error pausing playback: {e}")
            await handle_command_error(interaction, e, "Error pausing playback")

    @discord.ui.button(label="▶️", style=discord.ButtonStyle.secondary, custom_id="resume", row=0)
    async def resume(self, interaction: Interaction, button: Button):
        if not self.player.voice_client or not self.player.voice_client.is_paused():
            await interaction.response.send_message("일시정지된 곡이 없습니다.", ephemeral=True)
            return

        try:
            self.player.voice_client.resume()
            for child in self.children:
                if child.custom_id == "pause":
                    child.style = discord.ButtonStyle.secondary
                    child.label = "⏸️"
            await interaction.response.edit_message(view=self)
            await interaction.followup.send("재생을 재개했습니다.", ephemeral=True)
            logger.debug("재생 재개")
        except Exception as e:
            logger.exception(f"Error resuming playback: {e}")
            await handle_command_error(interaction, e, "Error resuming playback")

    @discord.ui.button(label="⏭️", style=discord.ButtonStyle.secondary, custom_id="skip", row=0)
    async def skip(self, interaction: Interaction, button: Button):
        if not self.player.voice_client or not self.player.voice_client.is_playing():
            await interaction.response.send_message("현재 재생 중인 곡이 없습니다.", ephemeral=True)
            return

        try:
            self.player.voice_client.stop()
            await interaction.response.send_message("현재 곡을 건너뜁니다.", ephemeral=True)
            logger.debug("현재 곡 건너뜀")
        except Exception as e:
            logger.exception(f"Error skipping track: {e}")
            await handle_command_error(interaction, e, "Error skipping track")

    @discord.ui.button(label="🔊", style=discord.ButtonStyle.secondary, custom_id="volume_up", row=0)
    async def volume_up(self, interaction: Interaction, button: Button):
        if not self.player.voice_client or not self.player.voice_client.source:
            await interaction.response.send_message("현재 재생 중인 곡이 없습니다.", ephemeral=True)
            return

        try:
            current_volume = self.player.voice_client.source.volume
            new_volume = min(current_volume + 0.1, 2.0)
            self.player.voice_client.source.volume = new_volume
            self.player._volume = new_volume  # Update stored volume
            await self.player.update_now_playing()  # Update embed with new volume
            await interaction.response.defer()  # No volume message
            logger.debug(f"볼륨 증가: {new_volume * 100}%")
        except Exception as e:
            logger.exception(f"Error increasing volume: {e}")
            await handle_command_error(interaction, e, "Error increasing volume")

    @discord.ui.button(label="🔉", style=discord.ButtonStyle.secondary, custom_id="volume_down", row=0)
    async def volume_down(self, interaction: Interaction, button: Button):
        if not self.player.voice_client or not self.player.voice_client.source:
            await interaction.response.send_message("현재 재생 중인 곡이 없습니다.", ephemeral=True)
            return

        try:
            current_volume = self.player.voice_client.source.volume
            new_volume = max(current_volume - 0.1, 0.0)
            self.player.voice_client.source.volume = new_volume
            self.player._volume = new_volume  # Update stored volume
            await self.player.update_now_playing()  # Update embed with new volume
            await interaction.response.defer()  # No volume message
            logger.debug(f"볼륨 감소: {new_volume * 100}%")
        except Exception as e:
            logger.exception(f"Error decreasing volume: {e}")
            await handle_command_error(interaction, e, "Error decreasing volume")

    @discord.ui.button(label="🔄", style=discord.ButtonStyle.secondary, custom_id="loop", row=1)
    async def toggle_loop(self, interaction: Interaction, button: Button):
        try:
            self.player.loop = not self.player.loop
            button.style = discord.ButtonStyle.primary if self.player.loop else discord.ButtonStyle.secondary
            button.label = "🔄 (반복)" if self.player.loop else "🔄"
            await interaction.response.edit_message(view=self)
            await self.player.update_now_playing()  # Update without sending message
            logger.debug(f"반복 모드: {'활성화' if self.player.loop else '비활성화'}")
        except Exception as e:
            logger.exception(f"Error toggling loop: {e}")
            await handle_command_error(interaction, e, "Error toggling loop")

    @discord.ui.button(label="⏹️", style=discord.ButtonStyle.danger, custom_id="stop", row=1)
    async def stop_button(self, interaction: Interaction, button: Button):
        if not self.player.voice_client:
            await interaction.response.send_message("봇이 음성 채널에 연결되어 있지 않습니다.", ephemeral=True)
            return

        try:
            await self.player.cleanup()
            self.music_cog.players.pop(interaction.guild.id, None)
            await interaction.response.send_message("재생을 멈추고 대기열을 비웠습니다.", ephemeral=True)
            logger.debug("재생 중지 및 대기열 비움")
        except Exception as e:
            logger.exception(f"Error stopping playback: {e}")
            await handle_command_error(interaction, e, "Error stopping playback")

    @discord.ui.button(label="🔀", style=discord.ButtonStyle.secondary, custom_id="shuffle", row=1)
    async def shuffle_queue(self, interaction: Interaction, button: Button):
        try:
            if not self.player.queue:
                await interaction.response.send_message("대기열이 비어있습니다.", ephemeral=True)
                logger.debug("셔플 명령어: 대기열이 비어있음")
                return

            random.shuffle(self.player.queue)
            await interaction.response.send_message("대기열을 섞었습니다.", ephemeral=True)
            await self.player.update_now_playing()
            logger.debug("셔플 명령어: 대기열 섞기 완료")
        except Exception as e:
            logger.exception(f"셔플 명령어 오류: {e}")
            await handle_command_error(interaction, e, "셔플 명령어 오류")

class MusicPlayer:
    # In the MusicPlayer class, update the default_thumbnail initialization
    def __init__(self, bot, guild, channel, music_cog, music_dir):
        self.bot = bot
        self.guild = guild
        self.channel = channel
        self.music_cog = music_cog
        self.music_dir = music_dir
        self.queue = deque()
        self.voice_client = None
        self.current = None
        self.loop = False
        self.embed_message = None
        self._volume = 0.05
        self.control_view = None
        self.check_task = self.bot.loop.create_task(self.check_voice_channel())

        # Use a Discord CDN URL for default thumbnail
        self.default_thumbnail = 'https://cdn.discordapp.com/attachments/1134007524320870451/default_music.png'
        self.messages_to_clean = set()

    # Update cleanup method to also clean files
    async def cleanup(self):
        try:
            logger.debug("MusicPlayer.cleanup 호출")
            if self.voice_client:
                if self.voice_client.is_playing():
                    self.voice_client.stop()
                await self.voice_client.disconnect()
                self.voice_client = None

            # Delete current file if it exists
            if self.current and 'file_path' in self.current:
                try:
                    if os.path.exists(self.current['file_path']):
                        os.remove(self.current['file_path'])
                        logger.debug(f"Deleted file: {self.current['file_path']}")
                except Exception as e:
                    logger.error(f"Error deleting file: {e}")

            # Delete embed message if it exists
            if self.embed_message:
                try:
                    await self.embed_message.delete()
                except Exception as e:
                    logger.error(f"Failed to delete embed message: {e}")
                self.embed_message = None

            if self.check_task:
                self.check_task.cancel()

            # Clean up any remaining files in the music directory
            try:
                for file in os.listdir(self.music_dir):
                    file_path = os.path.join(self.music_dir, file)
                    try:
                        os.remove(file_path)
                        logger.debug(f"Deleted remaining file: {file_path}")
                    except Exception as e:
                        logger.error(f"Error deleting file {file_path}: {e}")
            except Exception as e:
                logger.error(f"Error cleaning up music directory: {e}")

            self.queue.clear()
            self.current = None
            logger.debug("MusicPlayer.cleanup 완료")
        except Exception as e:
            logger.exception(f"Cleanup error: {e}")

    async def check_voice_channel(self):
        try:
            logger.debug("MusicPlayer.check_voice_channel 시작")
            await self.bot.wait_until_ready()
            while not self.bot.is_closed():
                try:
                    if self.voice_client and len(self.voice_client.channel.members) <= 1:
                        logger.info("음성 채널에 남은 사용자가 없어 청소 및 연결 해제")
                        await self.cleanup_and_disconnect()
                        break
                except Exception as e:
                    logger.exception(f"Voice channel check iteration error: {e}")
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            logger.debug("MusicPlayer.check_voice_channel 취소됨")
        except Exception as e:
            logger.exception(f"Voice channel check error: {e}")

    async def cleanup_and_disconnect(self):
        try:
            logger.debug("MusicPlayer.cleanup_and_disconnect 호출")
            for message_id in self.messages_to_clean:
                try:
                    message = await self.channel.fetch_message(message_id)
                    await message.delete()
                except Exception as e:
                    logger.error(f"Failed to delete message ID {message_id}: {e}")
            self.messages_to_clean.clear()
            await self.cleanup()
            logger.debug("MusicPlayer.cleanup_and_disconnect 완료")
        except Exception as e:
            logger.exception(f"Cleanup and disconnect error: {e}")

    async def update_now_playing(self):
        try:
            logger.debug("MusicPlayer.update_now_playing 호출")

            # Always try to delete any existing message first
            if self.embed_message:
                try:
                    await self.embed_message.delete()
                    self.embed_message = None
                except Exception as e:
                    logger.error(f"Failed to delete existing embed message: {e}")

            embed = discord.Embed(color=discord.Color.blue())

            if not self.current:
                embed.title = "🎵 현재 재생 중인 곡 없음"
                embed.description = "현재 재생 중인 곡이 없습니다."
                embed.color = discord.Color.red()
                # Use default thumbnail
                try:
                    if is_url(self.default_thumbnail):
                        embed.set_thumbnail(url=self.default_thumbnail)
                except Exception as e:
                    logger.error(f"Failed to set default thumbnail: {e}")
            else:
                embed.title = "🎵 현재 재생 중"
                duration = format_duration(self.current.get('duration', 0))
                current_volume = int(self._volume * 100)
                embed.add_field(
                    name="곡 정보",
                    value=f"**{self.current['title']}**\n⏱️ {duration}\n🔊 볼륨: {current_volume}%",
                    inline=False
                )

                # Set thumbnail with validation
                thumbnail_url = self.current.get('thumbnail')
                if thumbnail_url and is_url(thumbnail_url):
                    try:
                        embed.set_thumbnail(url=thumbnail_url)
                        logger.debug(f"Setting thumbnail URL: {thumbnail_url}")
                    except Exception as e:
                        logger.error(f"Failed to set song thumbnail: {e}")
                        if is_url(self.default_thumbnail):
                            embed.set_thumbnail(url=self.default_thumbnail)
                elif is_url(self.default_thumbnail):
                    embed.set_thumbnail(url=self.default_thumbnail)

                if self.queue:
                    next_song_id = self.queue[0]
                    next_song_entry = song_cache.get(next_song_id)
                    if next_song_entry:
                        next_duration = format_duration(next_song_entry.duration)
                        embed.add_field(
                            name="다음 곡",
                            value=f"**{next_song_entry.title}**\n⏱️ {next_duration}",
                            inline=False
                        )

            if not self.control_view:
                self.control_view = MusicControlView(self, self.music_cog)

            # Send new message
            try:
                self.embed_message = await self.channel.send(embed=embed, view=self.control_view)
                self.messages_to_clean.add(self.embed_message.id)
                logger.debug("현재 재생 중인 곡 임베드 업데이트 완료")
            except discord.HTTPException as e:
                logger.error(f"Failed to send embed message: {e}")
                # Try without thumbnail if there was an error
                embed.set_thumbnail(url=None)
                self.embed_message = await self.channel.send(embed=embed, view=self.control_view)
                self.messages_to_clean.add(self.embed_message.id)

        except Exception as e:
            logger.exception(f"Update now playing error: {e}")

    async def add_to_queue(self, song_data: dict, song_id: str):
        async with song_cache_lock:
            if song_id in song_cache:
                # 이미 다운로드 중이거나 다운로드 완료된 경우
                song_entry = song_cache[song_id]
                if song_entry.is_downloading:
                    logger.info(f"이미 다운로드 중인 곡: {song_entry.title}")
                else:
                    logger.info(f"이미 다운로드된 곡: {song_entry.title}")
            else:
                # 새로운 곡인 경우
                song_entry = SongData(
                    title=song_data.get('title'),
                    url=song_data.get('webpage_url'),
                    thumbnail=song_data.get('thumbnail', ''),
                    duration=song_data.get('duration', 0),
                    is_downloading=True
                )
                song_cache[song_id] = song_entry
                # 다운로드 작업 시작
                song_entry.download_future = asyncio.create_task(self.download_song(song_id, song_data))
                logger.debug(f"새 곡 추가 및 다운로드 시작: {song_id}")

        # 큐에 추가 (file_path는 다운로드가 완료된 후 참조)
        self.queue.append(song_id)
        logger.info(f"Added to queue: {song_entry.title}")

        # 다운로드가 완료되길 기다린 후 재생
        if not self.voice_client or not self.voice_client.is_playing():
            await self.play_next()

    async def download_song(self, song_id: str, song_data: dict):
        try:
            logger.debug(f"MusicPlayer.download_song 호출: song_id={song_id}")
            await YTDLSource.download_song(song_id, song_data['url'], self.music_dir, self.bot.loop)
            logger.debug(f"MusicPlayer.download_song 완료: song_id={song_id}")
        except Exception as e:
            logger.exception(f"Failed to download song {song_data.get('title')}: {e}")
            async with song_cache_lock:
                # 다운로드 실패 시 큐에서 제거
                if song_id in self.queue:
                    self.queue.remove(song_id)
                song_cache.pop(song_id, None)
            await self.update_now_playing()

    # In the play_next method, add file cleanup
    async def play_next(self):
        while True:
            try:
                logger.debug("MusicPlayer.play_next 호출")

                # Clean up previous song's file if it exists
                if self.current and 'file_path' in self.current:
                    try:
                        if os.path.exists(self.current['file_path']):
                            os.remove(self.current['file_path'])
                            logger.debug(f"Deleted file: {self.current['file_path']}")
                    except Exception as e:
                        logger.error(f"Error deleting file: {e}")

                # Delete current embed message if it exists
                if self.embed_message:
                    try:
                        await self.embed_message.delete()
                        self.embed_message = None
                    except Exception as e:
                        logger.error(f"Failed to delete old embed message: {e}")

                if self.loop and self.current:
                    self.queue.appendleft(self.current['song_id'])
                    logger.debug("반복 모드: 현재 곡을 큐의 앞에 추가")

                if not self.queue:
                    self.current = None
                    await self.update_now_playing()
                    logger.debug("큐가 비어있어 play_next 종료")
                    return

                song_id = self.queue.popleft()
                async with song_cache_lock:
                    song_entry = song_cache.get(song_id)

                if not song_entry:
                    logger.error(f"Song ID {song_id} not found in cache")
                    continue

                if song_entry.is_downloading:
                    try:
                        async with asyncio.timeout(45):
                            logger.info(f"대기 중: {song_entry.title} 다운로드 완료를 기다리는 중...")
                            await song_entry.download_future
                    except asyncio.TimeoutError:
                        logger.error(f"Download timeout for song: {song_entry.title}")
                        continue

                if not song_entry.file_path or not os.path.exists(song_entry.file_path):
                    logger.error(f"File not found: {song_entry.file_path}")
                    continue

                # Create current song info
                self.current = {
                    'song_id': song_id,
                    'title': song_entry.title,
                    'duration': song_entry.duration,
                    'file_path': song_entry.file_path
                }

                if not self.voice_client:
                    logger.warning("voice_client가 없음. play_next 종료")
                    return

                audio_source = PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(self.current['file_path'], **ffmpeg_options),
                    volume=self._volume
                )

                def after_playing(error):
                    if error:
                        logger.error(f"Playback error: {error}")
                    asyncio.run_coroutine_threadsafe(self.play_next(), self.bot.loop)

                self.voice_client.play(audio_source, after=after_playing)
                await self.update_now_playing()
                logger.debug(f"현재 곡 재생 시작: {self.current['title']}")
                return

            except Exception as e:
                logger.exception(f"Play next error: {e}")
                await asyncio.sleep(1)


@app_commands.guild_only()
class MusicCog(commands.Cog, name="Music"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.base_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cogs_data", "music_cog")
        os.makedirs(self.base_path, exist_ok=True)
        self.players = {}

    song = app_commands.Group(name="곡", description="음악 관련 명령어")

    def get_player(self, guild: discord.Guild, channel: discord.TextChannel) -> MusicPlayer:
        if guild.id not in self.players:
            guild_path = os.path.join(self.base_path, str(guild.id))
            os.makedirs(guild_path, exist_ok=True)
            self.players[guild.id] = MusicPlayer(
                self.bot, guild, channel, self,
                guild_path
            )
            logger.debug(f"새 MusicPlayer 생성: guild_id={guild.id}")
        return self.players[guild.id]

    # Inside MusicCog class, update the play command
    @song.command(name="재생", description="노래를 검색하거나 URL로 바로 재생합니다.")
    @app_commands.describe(query="곡 이름으로 검색하거나 URL로 바로 재생해보세요.")
    async def play(self, interaction: Interaction, query: str):
        await interaction.response.defer()

        try:
            if not interaction.user.voice:
                await interaction.followup.send("먼저 음성 채널에 접속해주세요.", ephemeral=True)
                return

            player = self.get_player(interaction.guild, interaction.channel)
            voice_channel = interaction.user.voice.channel

            if not player.voice_client:
                player.voice_client = await voice_channel.connect()
            elif player.voice_client.channel != voice_channel:
                await player.voice_client.move_to(voice_channel)

            if is_url(query):
                source, song_id = await YTDLSource.from_url(
                    query, download=True,
                    loop=self.bot.loop,
                    music_dir=player.music_dir
                )
                await player.add_to_queue(source.data, song_id)
                message = await interaction.followup.send(
                    f"🎵 추가됨: {source.title}",
                    ephemeral=False
                )
                # Schedule message deletion
                try:
                    await asyncio.sleep(3)
                    await message.delete()
                except discord.NotFound:
                    pass
                except Exception as e:
                    logger.error(f"Error deleting message: {e}")
            else:
                try:
                    # Optimize search options
                    search_options = ytdlp_format_options.copy()
                    search_options.update({
                        'default_search': 'ytsearch5',
                        'extract_flat': True,  # Faster search results
                        'force_generic_extractor': True  # Even faster search
                    })

                    info = await asyncio.wait_for(
                        self.bot.loop.run_in_executor(
                            download_executor,
                            lambda: yt_dlp.YoutubeDL(search_options).extract_info(
                                f"ytsearch5:{query}", download=False
                            )
                        ),
                        timeout=10
                    )

                    if not info.get('entries'):
                        await interaction.followup.send("검색 결과가 없습니다.", ephemeral=True)
                        return

                    results = []
                    for entry in info['entries'][:5]:
                        if entry and isinstance(entry, dict):
                            if 'url' not in entry and 'id' in entry:
                                entry['url'] = f"https://www.youtube.com/watch?v={entry['id']}"
                            results.append(entry)

                    embed = discord.Embed(title="🔍 검색 결과", color=discord.Color.blue())
                    for idx, result in enumerate(results, 1):
                        title = result.get('title', 'Unknown Title')[:100]
                        embed.add_field(
                            name=f"{idx}. {title}",
                            value=f"⏱️ {format_duration(result.get('duration', 0))}",
                            inline=False
                        )

                    view = SearchResultsView(self, interaction, results)
                    await interaction.followup.send(embed=embed, view=view, ephemeral=True)

                except asyncio.TimeoutError:
                    await interaction.followup.send(
                        "검색 시간이 초과되었습니다. 다시 시도해주세요.",
                        ephemeral=True
                    )
                except Exception as e:
                    logger.exception(f"Search error: {e}")
                    await interaction.followup.send(
                        "검색 중 오류가 발생했습니다.",
                        ephemeral=True
                    )

        except Exception as e:
            logger.exception(f"Play command error: {e}")
            await interaction.followup.send("재생 중 오류가 발생했습니다.", ephemeral=True)

        if is_url(query):
            source, song_id = await YTDLSource.from_url(
                query, download=True,
                loop=self.bot.loop,
                music_dir=player.music_dir
            )
            await player.add_to_queue(source.data, song_id)
            # Send message and schedule deletion
            message = await interaction.followup.send(
                f"🎵 추가됨: {source.title}",
                ephemeral=False
            )
            await asyncio.sleep(3)
            try:
                await message.delete()
            except discord.NotFound:
                pass

    @song.command(name="정지", description="재생을 멈추고 대기열을 비웁니다.")
    async def stop(self, interaction: Interaction):
        try:
            player = self.get_player(interaction.guild, interaction.channel)
            await player.cleanup()
            self.players.pop(interaction.guild.id, None)
            await interaction.response.send_message("재생을 멈추고 대기열을 비웠습니다.", ephemeral=True)
            logger.debug("정지 명령어 실행 완료")
        except Exception as e:
            logger.exception(f"정지 명령어 오류: {e}")
            await handle_command_error(interaction, e, "정지 명령어 오류")

    @song.command(name="스킵", description="현재 곡을 건너뜁니다.")
    async def skip(self, interaction: Interaction):
        try:
            player = self.get_player(interaction.guild, interaction.channel)
            if not player.voice_client or not player.voice_client.is_playing():
                await interaction.response.send_message("현재 재생 중인 곡이 없습니다.", ephemeral=True)
                logger.debug("스킵 명령어: 현재 재생 중인 곡 없음")
                return

            player.voice_client.stop()
            await interaction.response.send_message("현재 곡을 건너뜁니다.", ephemeral=True)
            logger.debug("스킵 명령어: 현재 곡 건너뜀")
        except Exception as e:
            logger.exception(f"스킵 명령어 오류: {e}")
            await handle_command_error(interaction, e, "스킵 명령어 오류")

    @song.command(name="대기열", description="현재 대기열을 보여줍니다.")
    async def queue(self, interaction: Interaction):
        try:
            player = self.get_player(interaction.guild, interaction.channel)
            if not player.queue and not player.current:
                await interaction.response.send_message("대기열이 비어있습니다.", ephemeral=True)
                logger.debug("대기열 명령어: 대기열이 비어있음")
                return

            embed = discord.Embed(title="🎵 재생 대기열", color=discord.Color.blue())

            if player.current:
                duration = format_duration(player.current.get('duration', 0))
                embed.add_field(
                    name="현재 재생 중",
                    value=f"**{player.current['title']}**\n⏱️ {duration}",
                    inline=False
                )

            for idx, song_id in enumerate(player.queue, 1):
                song_entry = song_cache.get(song_id)
                if song_entry:
                    duration = format_duration(song_entry.duration)
                    embed.add_field(
                        name=f"{idx}번 곡",
                        value=f"**{song_entry.title}**\n⏱️ {duration}",
                        inline=False
                    )
                else:
                    embed.add_field(
                        name=f"{idx}번 곡",
                        value="Unknown",
                        inline=False
                    )

            await interaction.response.send_message(embed=embed, ephemeral=True)
            logger.debug("대기열 명령어: 대기열 임베드 전송 완료")
        except Exception as e:
            logger.exception(f"대기열 명령어 오류: {e}")
            await handle_command_error(interaction, e, "대기열 명령어 오류")

    @song.command(name="셔플", description="대기열의 곡 순서를 무작위로 섞습니다.")
    async def shuffle(self, interaction: Interaction):
        try:
            player = self.get_player(interaction.guild, interaction.channel)
            if not player.queue:
                await interaction.response.send_message("대기열이 비어있습니다.", ephemeral=True)
                logger.debug("셔플 명령어: 대기열이 비어있음")
                return

            random.shuffle(player.queue)
            await interaction.response.send_message("대기열을 섞었습니다.", ephemeral=True)
            await player.update_now_playing()
            logger.debug("셔플 명령어: 대기열 섞기 완료")
        except Exception as e:
            logger.exception(f"셔플 명령어 오류: {e}")
            await handle_command_error(interaction, e, "셔플 명령어 오류")

    @song.command(name="볼륨", description="재생 볼륨을 조절합니다. (1-200)")
    async def volume(self, interaction: Interaction, level: app_commands.Range[int, 1, 200]):
        try:
            player = self.get_player(interaction.guild, interaction.channel)
            if not player.voice_client or not player.voice_client.source:
                await interaction.response.send_message("현재 재생 중인 곡이 없습니다.", ephemeral=True)
                logger.debug("볼륨 명령어: 현재 재생 중인 곡 없음")
                return

            volume = level / 100
            player.voice_client.source.volume = volume
            player._volume = volume  # 올바르게 수정
            await interaction.response.send_message(f"볼륨을 {level}%로 설정했습니다.", ephemeral=True)
            await player.update_now_playing()  # 임베디드 메시지 업데이트
            logger.debug(f"볼륨 조절: {level}%")
        except Exception as e:
            logger.exception(f"볼륨 명령어 오류: {e}")
            await handle_command_error(interaction, e, "볼륨 명령어 오류")

async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
    logger.info("MusicCog loaded successfully.")