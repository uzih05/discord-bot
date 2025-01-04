# cogs/music_cog.py
import asyncio
import hashlib
import logging
import os
import random
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple, Any
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta
from dataclasses import dataclass

import discord
import yt_dlp
from discord import PCMVolumeTransformer, VoiceClient
from discord import app_commands, Interaction, Embed
from discord.ext import commands, tasks
from discord.ui import Button, View
from asyncio import Lock, TimeoutError as AsyncTimeoutError

# Set up logging
logger = logging.getLogger(__name__)

# Define the SongData class first since it's used in global variables
@dataclass
class SongData:
    title: str
    url: str
    thumbnail: str
    duration: int
    file_path: Optional[str] = None
    is_downloading: bool = False
    download_future: Optional[asyncio.Future] = None
    added_at: datetime = datetime.now()

    @property
    def is_expired(self) -> bool:
        """Check if song cache has expired (older than 1 hour)"""
        return (datetime.now() - self.added_at) > timedelta(hours=1)

# Global cache and lock -- remove
# song_cache: Dict[str, SongData] = {}
# song_cache_lock: Lock = asyncio.Lock()

# Constants for configuration
MAX_QUEUE_SIZE = 500
MAX_SONG_DURATION = 18000  # 5 hours in seconds
CACHE_CLEANUP_INTERVAL = 3600  # 1 hour in seconds
DOWNLOAD_TIMEOUT = 30
VOICE_TIMEOUT = 300  # 5 minutes of inactivity before disconnect
MAX_RETRIES = 3
DEFAULT_VOLUME = 0.05

# Exception classes
class MusicBotError(Exception):
    """Base exception class for music bot errors"""
    pass

class QueueFullError(MusicBotError):
    """Raised when queue is at maximum capacity"""
    pass

class SongTooLongError(MusicBotError):
    """Raised when song duration exceeds maximum limit"""
    pass

class DownloadError(MusicBotError):
    """Raised when song download fails"""
    pass

class VoiceConnectionError(MusicBotError):
    """Raised when voice connection fails"""
    pass

# YT-DLP configuration
ytdlp_format_options = {
    'format': 'bestaudio/best',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'extract_flat': True,
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'opus',
        'preferredquality': '192',
    }],
}

# FFmpeg configuration
ffmpeg_options = {
    'options': '-vn -loglevel error -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -analyzeduration 0'
}

# Thread pool for downloads
download_executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="music_downloader")

class CacheManager:
    def __init__(self):
        self.cache: Dict[str, SongData] = {}
        self.lock = Lock()
        self._cleanup_task = None

    async def get(self, song_id: str) -> Optional[SongData]:
        async with self.lock:
            return self.cache.get(song_id)

    async def set(self, song_id: str, song_data: SongData) -> None:
        async with self.lock:
            self.cache[song_id] = song_data

    async def remove(self, song_id: str) -> None:
        async with self.lock:
            self.cache.pop(song_id, None)

    async def cleanup_expired(self) -> None:
        """Remove expired entries and their files"""
        async with self.lock:
            expired_ids = [
                song_id for song_id, data in self.cache.items()
                if data.is_expired and not data.is_downloading
            ]

            for song_id in expired_ids:
                song_data = self.cache[song_id]
                if song_data.file_path and os.path.exists(song_data.file_path):
                    try:
                        os.remove(song_data.file_path)
                        logger.debug(f"Deleted expired file: {song_data.file_path}")
                    except OSError as e:
                        logger.error(f"Failed to delete expired file: {e}")
                del self.cache[song_id]

    def start_cleanup_task(self, loop: asyncio.AbstractEventLoop) -> None:
        async def cleanup_loop():
            while True:
                try:
                    await asyncio.sleep(CACHE_CLEANUP_INTERVAL)
                    await self.cleanup_expired()
                except Exception as e:
                    logger.error(f"Cache cleanup error: {e}")

        self._cleanup_task = loop.create_task(cleanup_loop())

    def stop_cleanup_task(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()

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
                 song_id: str, volume: float = DEFAULT_VOLUME):
        self.file_path = file_path
        self.data = data
        self.thumbnail = thumbnail
        self.title = data.get('title', 'Unknown Title')
        self.duration = duration
        self.volume = volume
        self.song_id = song_id
        self.url = data.get('webpage_url')

        # Validate duration
        if self.duration > MAX_SONG_DURATION:
            raise SongTooLongError(f"Song duration exceeds limit of {MAX_SONG_DURATION} seconds")

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
    async def download_song(cls, song_id: str, url: str, music_dir: str,
                            loop: asyncio.AbstractEventLoop) -> Tuple[Any, str]:
        for attempt in range(MAX_RETRIES):
            try:
                async with asyncio.timeout(DOWNLOAD_TIMEOUT):
                    # First get info without downloading
                    info_options = ytdlp_format_options.copy()
                    info_options['extract_flat'] = False
                    info_options['download'] = False

                    info = await loop.run_in_executor(
                        download_executor,
                        lambda: yt_dlp.YoutubeDL(info_options).extract_info(url, download=False)
                    )

                    if not info:
                        raise DownloadError("No data received from yt-dlp")

                    # Create the output path with cleaned title
                    clean_title = clean_filename(info.get('title', 'unknown'))
                    base_path = os.path.join(music_dir, f'{song_id}-{clean_title}')

                    # Now download with specific options
                    download_options = {
                        'format': 'bestaudio/best',
                        'postprocessors': [{
                            'key': 'FFmpegExtractAudio',
                            'preferredcodec': 'opus',
                            'preferredquality': '192',
                        }],
                        'restrictfilenames': True,
                        'noplaylist': True,
                        'nocheckcertificate': True,
                        'ignoreerrors': False,
                        'logtostderr': False,
                        'quiet': True,
                        'no_warnings': True,
                        'outtmpl': base_path,
                    }

                    logger.debug(f"Starting download with options: {download_options}")

                    # Download and process
                    await loop.run_in_executor(
                        download_executor,
                        lambda: yt_dlp.YoutubeDL(download_options).download([url])
                    )

                    # Wait a moment for the file to be fully written
                    await asyncio.sleep(1)

                    # Check for the file with .opus extension
                    expected_path = f"{base_path}.opus"
                    if os.path.exists(expected_path):
                        logger.debug(f"File successfully downloaded to: {expected_path}")
                        return info, expected_path

                    # If not found, check for other possible extensions
                    possible_extensions = ['.opus', '.m4a', '.mp3', '.webm']
                    for ext in possible_extensions:
                        test_path = f"{base_path}{ext}"
                        if os.path.exists(test_path):
                            logger.debug(f"Found file with different extension: {test_path}")
                            return info, test_path

                    logger.error(f"No file found after download. Checked paths: {base_path}.*")
                    raise DownloadError("Downloaded file not found at expected location")

            except AsyncTimeoutError:
                if attempt == MAX_RETRIES - 1:
                    raise DownloadError(f"Download timed out after {MAX_RETRIES} attempts")
                logger.warning(f"Download attempt {attempt + 1} timed out, retrying...")
                await asyncio.sleep(1)

            except Exception as e:
                if attempt == MAX_RETRIES - 1:
                    raise DownloadError(f"Download failed after {MAX_RETRIES} attempts: {e}")
                logger.warning(f"Download attempt {attempt + 1} failed: {e}, retrying...")
                await asyncio.sleep(1)

    # Update the YTDLSource class's from_url method to better handle thumbnails
    @classmethod
    async def from_url(cls, url: str, download: bool, loop, music_dir: str, cache_manager: CacheManager) -> Tuple[
        'YTDLSource', str]:
        try:
            logger.debug(f"from_url 호출: URL={url}, download={download}")
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
                thumbnails = sorted(
                    [t for t in data['thumbnails'] if isinstance(t, dict) and 'url' in t],
                    key=lambda x: x.get('width', 0) * x.get('height', 0),
                    reverse=True
                )
                if thumbnails:
                    thumbnail_url = thumbnails[0]['url']

            if not thumbnail_url and 'thumbnail' in data:
                thumbnail_url = data['thumbnail']

            song_id = generate_song_id(url, data['title'])
            clean_title = clean_filename(data['title'])
            file_path = os.path.join(music_dir, f'{song_id}-{clean_title}.opus')  # Changed to .opus

            song_entry = await cache_manager.get(song_id)
            if not song_entry:
                song_entry = SongData(
                    title=data['title'],
                    url=url,
                    thumbnail=thumbnail_url,
                    duration=data.get('duration', 0),
                    is_downloading=True,
                    file_path=file_path  # Set the file path here
                )
                await cache_manager.set(song_id, song_entry)
                song_entry.download_future = asyncio.create_task(
                    cls.download_song(song_id, url, music_dir, loop))

            return cls(
                file_path=file_path if download else data.get('url'),
                data=data,
                thumbnail=thumbnail_url,
                duration=data.get('duration', 0),
                song_id=song_id,
                volume=DEFAULT_VOLUME
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
        super().__init__(timeout=60)  # Set 60 second timeout
        self.music_cog = music_cog
        self.original_interaction = interaction
        self.original_user = interaction.user
        self.voice_channel = interaction.user.voice.channel
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
            # Check if user is in the same voice channel
            if not interaction.user.voice or interaction.user.voice.channel != self.voice_channel:
                await interaction.response.send_message(
                    "이 명령어를 사용하려면 음성 채널에 참가해야 합니다.",
                    ephemeral=True
                )
                return

            # Check if the bot is already being used in another channel
            if interaction.guild.voice_client and interaction.guild.voice_client.channel != self.voice_channel:
                if len(interaction.guild.voice_client.channel.members) > 1:
                    await interaction.response.send_message(
                        "다른 음성 채널에서 이미 음악이 재생 중입니다.",
                        ephemeral=True
                    )
                    return

            try:
                logger.debug(f"SearchResultsView 버튼 클릭: index={index}")

                # Try to delete the search results message
                if self.message:
                    try:
                        await self.message.delete()
                    except discord.NotFound:
                        pass
                    except Exception as e:
                        logger.error(f"Error deleting search results message: {e}")

                result = self.results[index]

                # Ensure we have a valid URL
                if 'webpage_url' not in result and 'id' in result:
                    result['webpage_url'] = f"https://www.youtube.com/watch?v={result['id']}"
                elif 'webpage_url' not in result and 'url' in result:
                    result['webpage_url'] = result['url']

                if 'webpage_url' not in result:
                    await interaction.response.send_message(
                        "선택한 곡의 URL을 찾을 수 없습니다.",
                        ephemeral=True
                    )
                    return

                # Ensure we have a title
                if 'title' not in result:
                    result['title'] = f"Unknown Title {result.get('id', 'No ID')}"

                # Get player and generate song ID
                song_id = generate_song_id(result['webpage_url'], result['title'])
                player = self.music_cog.get_player(interaction.guild, interaction.channel)

                # Download status display
                try:
                    await interaction.response.defer()
                    status_msg = await interaction.followup.send(
                        embed=discord.Embed(
                            title="🔄 다운로드 중...",
                            description=f"**{result['title']}**\n잠시만 기다려주세요...",
                            color=discord.Color.blue()
                        ),
                        ephemeral=True
                    )
                except Exception as e:
                    logger.error(f"Error sending status message: {e}")
                    status_msg = None

                # Add to queue and start playback
                try:
                    await player.add_to_queue(result, song_id)

                    if not player.voice_client or not player.voice_client.is_playing():
                        success = await player.play_next()
                        if not success:
                            await interaction.followup.send(
                                "재생을 시작할 수 없습니다.",
                                ephemeral=True
                            )
                            return
                except Exception as e:
                    logger.error(f"Error adding song to queue: {e}")
                    await interaction.followup.send(
                        "곡을 추가하는 중 오류가 발생했습니다.",
                        ephemeral=True
                    )
                    return

                # Clean up status message
                if status_msg:
                    try:
                        await status_msg.delete()
                    except (discord.NotFound, discord.HTTPException):
                        pass

                # Send success message
                success_msg = await interaction.followup.send(
                    f"🎵 추가됨: {result['title']}",
                    ephemeral=False
                )

                # Delete success message after 3 seconds
                try:
                    await asyncio.sleep(3)
                    await success_msg.delete()
                except (discord.NotFound, discord.HTTPException):
                    pass

                logger.debug(f"SearchResultsView 버튼 처리 완료: song_id={song_id}")

            except Exception as e:
                logger.exception(f"Error processing selection: {e}")
                try:
                    await interaction.followup.send(
                        "선택한 곡을 처리하는 중 오류가 발생했습니다.",
                        ephemeral=True
                    )
                except:
                    pass

        return callback

    async def on_timeout(self):
        try:
            logger.debug("SearchResultsView 타임아웃 발생")
            for item in self.children:
                item.disabled = True

            if self.message:
                try:
                    # Create timeout embed
                    timeout_embed = discord.Embed(
                        title="⏰ 시간 만료",
                        description="검색 시간이 초과되었습니다. 다시 검색해주세요.",
                        color=discord.Color.red()
                    )
                    await self.message.edit(embed=timeout_embed, view=None)

                    # Delete the message after 5 seconds
                    await asyncio.sleep(5)
                    await self.message.delete()
                except discord.NotFound:
                    pass
                except Exception as e:
                    logger.error(f"Error in timeout handler: {e}")
        except Exception as e:
            logger.error(f"Error in timeout handler: {e}")

    def set_message(self, message):
        self.message = message


class MusicControlView(View):
    def __init__(self, player: 'MusicPlayer', music_cog: 'MusicCog'):
        super().__init__(timeout=None)
        self.player = player
        self.music_cog = music_cog
        self._update_button_states()

    def _update_button_states(self) -> None:
        """Update button states based on player state"""
        is_playing = bool(self.player.voice_client and self.player.voice_client.is_playing())
        is_paused = bool(self.player.voice_client and self.player.voice_client.is_paused())
        # Change shuffle button condition to require 2 or more songs in queue
        has_multiple_songs_in_queue = len(self.player.queue) >= 2

        # Update individual button states
        for child in self.children:
            if isinstance(child, Button):
                if child.custom_id == "pause":
                    child.disabled = not is_playing
                elif child.custom_id == "resume":
                    child.disabled = not is_paused
                elif child.custom_id == "skip":
                    child.disabled = not is_playing
                elif child.custom_id == "stop":
                    child.disabled = not (is_playing or is_paused or self.player.queue)
                elif child.custom_id == "shuffle":
                    child.disabled = not has_multiple_songs_in_queue  # Enable only with 2+ songs
        # Update individual button states
        for child in self.children:
            if isinstance(child, Button):
                if child.custom_id == "pause":
                    child.disabled = not is_playing
                elif child.custom_id == "resume":
                    child.disabled = not is_paused
                elif child.custom_id == "skip":
                    child.disabled = not is_playing
                elif child.custom_id == "stop":
                    child.disabled = not (is_playing or is_paused or self.player.queue)
                elif child.custom_id == "shuffle":
                    child.disabled = not has_multiple_songs_in_queue  # Enable only with 2+ songs

    async def _handle_interaction(self, interaction: Interaction,
                                  action: str, handler: callable) -> None:
        """Generic interaction handler with proper error handling"""
        if not interaction.user.voice or interaction.user.voice.channel != self.player.voice_client.channel:
            await interaction.response.send_message(
                "이 명령어를 사용하려면 음성 채널에 참가해야 합니다.",
                ephemeral=True
            )
            return

        try:
            await handler()
            self._update_button_states()
            if interaction.response.is_done():
                await interaction.edit_original_response(view=self)
            else:
                await interaction.response.edit_message(view=self)
        except Exception as e:
            logger.error(f"Error in {action}: {e}")
            await interaction.response.send_message(
                f"{action} 처리 중 오류가 발생했습니다.",
                ephemeral=True
            )

    @discord.ui.button(label="⏸️", style=discord.ButtonStyle.secondary, custom_id="pause")
    async def pause(self, interaction: Interaction, button: Button):
        async def pause_handler():
            if self.player.voice_client.is_playing():
                self.player.voice_client.pause()
                button.style = discord.ButtonStyle.primary
                button.label = "▶️"
                await interaction.response.send_message("재생을 일시정지했습니다.", ephemeral=True)

        await self._handle_interaction(interaction, "일시정지", pause_handler)


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
            # Check if there's any song in current or queue
            if not self.player.current and not self.player.queue:
                await interaction.response.send_message("재생 중인 곡이 없어 반복 모드를 설정할 수 없습니다.", ephemeral=True)
                return

            self.player.loop = not self.player.loop
            button.style = discord.ButtonStyle.primary if self.player.loop else discord.ButtonStyle.secondary
            button.label = "🔄 (반복)" if self.player.loop else "🔄"
            await interaction.response.edit_message(view=self)

            # If there's a current song, update the now playing message
            if self.player.current:
                await self.player.update_now_playing()

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
            # Reset player states
            self.player.loop = False  # Disable repeat mode

            # Reset button states if control view exists
            if self.player.control_view:
                for child in self.player.control_view.children:
                    if child.custom_id == "loop":
                        child.style = discord.ButtonStyle.secondary
                        child.label = "🔄"
                    elif child.custom_id == "pause":
                        child.style = discord.ButtonStyle.secondary
                        child.label = "⏸️"

            # Delete current embed message before cleanup
            if self.player.embed_message:
                try:
                    await self.player.embed_message.delete()
                    self.player.embed_message = None
                except Exception as e:
                    logger.error(f"Failed to delete embed message: {e}")

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
    def __init__(self, bot, guild, channel, music_cog, music_dir, cache_manager):
        self.bot = bot
        self.guild = guild
        self.channel = channel
        self.music_cog = music_cog
        self.music_dir = music_dir
        self.cache_manager = cache_manager  # Store cache_manager reference
        self.queue = deque()
        self.voice_client = None
        self.current = None
        self.loop = False
        self.embed_message = None
        self._volume = 0.05
        self.control_view = None
        self.check_task = self.bot.loop.create_task(self.check_voice_channel())
        self.default_thumbnail = 'https://cdn.discordapp.com/attachments/1134007524320870451/default_music.png'
        self.messages_to_clean = set()
        # Use a Discord CDN URL for default thumbnail
        self.default_thumbnail = 'https://cdn.discordapp.com/attachments/1134007524320870451/default_music.png'
        self.messages_to_clean = set()

    async def cleanup(self):
        try:
            logger.debug("MusicPlayer.cleanup 호출")
            if self.voice_client:
                if self.voice_client.is_playing():
                    self.voice_client.stop()
                await self.voice_client.disconnect()
                self.voice_client = None

            self.cache_manager.stop_cleanup_task()

            # Reset player states
            self.loop = False
            self._volume = 0.05

            # Delete current file
            if self.current and 'file_path' in self.current:
                try:
                    if os.path.exists(self.current['file_path']):
                        os.remove(self.current['file_path'])
                        logger.debug(f"Deleted file: {self.current['file_path']}")
                except Exception as e:
                    logger.error(f"Error deleting file: {e}")

            # Important: Delete embed message AFTER disconnecting from voice channel
            if self.embed_message:
                try:
                    await self.embed_message.delete()
                except Exception as e:
                    logger.error(f"Failed to delete embed message: {e}")
                self.embed_message = None

            # Reset control view button states
            if self.control_view:
                for child in self.control_view.children:
                    if child.custom_id == "loop":
                        child.style = discord.ButtonStyle.secondary
                        child.label = "🔄"
                    elif child.custom_id == "pause":
                        child.style = discord.ButtonStyle.secondary
                        child.label = "⏸️"
                self.control_view = None

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

            # Clear queue and current song last
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

                if self.loop:
                    embed.add_field(
                        name="반복 모드",
                        value="🔄 활성화",
                        inline=False
                    )

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
                    next_song_entry = await self.cache_manager.get(next_song_id)
                    if next_song_entry:
                        next_duration = format_duration(next_song_entry.duration)
                        embed.add_field(
                            name="다음 곡",
                            value=f"**{next_song_entry.title}**\n⏱️ {next_duration}",
                            inline=False
                        )

            if not self.control_view:
                self.control_view = MusicControlView(self, self.music_cog)

            try:
                self.embed_message = await self.channel.send(embed=embed, view=self.control_view)
                self.messages_to_clean.add(self.embed_message.id)
                logger.debug("현재 재생 중인 곡 임베드 업데이트 완료")
            except discord.HTTPException as e:
                logger.error(f"Failed to send embed message: {e}")
                embed.set_thumbnail(url=None)
                self.embed_message = await self.channel.send(embed=embed, view=self.control_view)
                self.messages_to_clean.add(self.embed_message.id)

        except Exception as e:
            logger.exception(f"Update now playing error: {e}")

    async def add_to_queue(self, song_data: dict, song_id: str):
        song_entry = await self.cache_manager.get(song_id)
        if song_entry:
            if song_entry.is_downloading:
                logger.info(f"이미 다운로드 중인 곡: {song_entry.title}")
            else:
                logger.info(f"이미 다운로드된 곡: {song_entry.title}")
        else:
            song_entry = SongData(
                title=song_data.get('title'),
                url=song_data.get('webpage_url'),
                thumbnail=song_data.get('thumbnail', ''),
                duration=song_data.get('duration', 0),
                is_downloading=True
            )
            await self.cache_manager.set(song_id, song_entry)
            song_entry.download_future = asyncio.create_task(
                self.download_song(song_id, song_data))
            logger.debug(f"새 곡 추가 및 다운로드 시작: {song_id}")

        self.queue.append(song_id)
        logger.info(f"Added to queue: {song_entry.title}")

        # Update the control view whenever a song is added to the queue
        if self.control_view:
            self.control_view._update_button_states()
            if self.embed_message:
                try:
                    await self.embed_message.edit(view=self.control_view)
                except Exception as e:
                    logger.error(f"Error updating control view: {e}")

    async def download_song(self, song_id: str, song_data: dict):
        try:
            logger.debug(f"MusicPlayer.download_song 호출: song_id={song_id}")
            song_info, file_path = await YTDLSource.download_song(
                song_id, song_data['url'], self.music_dir, self.bot.loop
            )
            # Update the song entry with the actual file path
            song_entry = await self.cache_manager.get(song_id)
            if song_entry:
                song_entry.file_path = file_path
                song_entry.is_downloading = False
                await self.cache_manager.set(song_id, song_entry)
            logger.debug(f"MusicPlayer.download_song 완료: song_id={song_id}")
        except Exception as e:
            logger.exception(f"Failed to download song {song_data.get('title')}: {e}")
            if song_id in self.queue:
                self.queue.remove(song_id)
            await self.cache_manager.remove(song_id)
            await self.update_now_playing()

    async def play_next(self):
        while True:
            try:
                logger.debug("MusicPlayer.play_next 호출")

                if self.embed_message:
                    try:
                        await self.embed_message.delete()
                        self.embed_message = None
                    except Exception as e:
                        logger.error(f"Failed to delete old embed message: {e}")

                # Handle loop functionality
                if self.loop and self.current:
                    current_song_id = self.current['song_id']

                    # Add to queue only if it's not already in queue
                    if current_song_id not in self.queue:
                        self.queue.appendleft(current_song_id)
                        logger.debug(f"반복 모드: 현재 곡 '{self.current['title']}'을(를) 큐의 앞에 추가")
                    else:
                        logger.debug(f"반복 모드: 곡 '{self.current['title']}'이(가) 이미 큐에 있음")

                if not self.queue:
                    self.current = None
                    await self.update_now_playing()
                    logger.debug("큐가 비어있어 play_next 종료")
                    return False

                song_id = self.queue.popleft()

                # Wait for song to finish downloading if needed
                song_entry = await self.cache_manager.get(song_id)  # Use cache_manager instead

                if not song_entry:
                    logger.error(f"Song ID {song_id} not found in cache")
                    continue

                if song_entry.is_downloading:
                    logger.info(f"Song {song_entry.title} is still downloading, waiting...")
                    try:
                        await asyncio.wait_for(song_entry.download_future, timeout=30)
                    except asyncio.TimeoutError:
                        logger.error(f"Timed out waiting for {song_entry.title} to download")
                        continue

                # Delete the previous song's file only if it's not in queue and not the current song being looped
                if self.current and self.current['song_id'] not in self.queue and not self.loop:
                    try:
                        if os.path.exists(self.current['file_path']):
                            os.remove(self.current['file_path'])
                            logger.debug(f"Deleted file: {self.current['file_path']}")
                    except Exception as e:
                        logger.error(f"Error deleting file: {e}")

                # Begin actual playback
                try:
                    # Wait for file to be ready
                    retry_count = 0
                    max_retries = 5
                    while retry_count < max_retries:
                        if song_entry.file_path and os.path.exists(song_entry.file_path):
                            break
                        logger.debug(
                            f"Waiting for file to be ready: {song_entry.title} (Attempt {retry_count + 1}/{max_retries})")
                        await asyncio.sleep(1)
                        retry_count += 1

                    if not song_entry.file_path or not os.path.exists(song_entry.file_path):
                        logger.error(f"File not found for {song_entry.title} after {max_retries} attempts")
                        continue

                    # Create audio source
                    audio_source = discord.FFmpegPCMAudio(song_entry.file_path, **ffmpeg_options)
                    transformed_source = PCMVolumeTransformer(audio_source, volume=self._volume)

                    # Set up after function
                    def after_playing(error):
                        if error:
                            logger.error(f"Error during playback: {error}")
                        asyncio.run_coroutine_threadsafe(self.play_next(), self.bot.loop)

                    # Start playback
                    if self.voice_client and self.voice_client.is_connected():
                        self.voice_client.play(transformed_source, after=after_playing)

                        # Update current song info
                        self.current = {
                            'title': song_entry.title,
                            'duration': song_entry.duration,
                            'thumbnail': song_entry.thumbnail,
                            'file_path': song_entry.file_path,
                            'song_id': song_id
                        }

                        await self.update_now_playing()
                        logger.info(f"Now playing: {song_entry.title}")
                        return True
                    else:
                        logger.error("Voice client is not connected")
                        return False

                except Exception as e:
                    logger.exception(f"Error starting playback: {e}")
                    return False

            except Exception as e:
                logger.exception(f"Play next error: {e}")
                await asyncio.sleep(1)
                continue

@app_commands.guild_only()
class MusicCog(commands.Cog, name="Music"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.base_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cogs_data", "music_cog")
        os.makedirs(self.base_path, exist_ok=True)
        self.players = {}
        self.cache_manager = CacheManager()  # Add this line
        self.cache_manager.start_cleanup_task(self.bot.loop)  # Add this line

    async def cog_unload(self):  # Add this method
        self.cache_manager.stop_cleanup_task()
        for player in self.players.values():
            await player.cleanup()

    song = app_commands.Group(name="곡", description="음악 관련 명령어")

    # Update get_player to pass cache_manager
    def get_player(self, guild: discord.Guild, channel: discord.TextChannel) -> MusicPlayer:
        if guild.id not in self.players:
            guild_path = os.path.join(self.base_path, str(guild.id))
            os.makedirs(guild_path, exist_ok=True)
            self.players[guild.id] = MusicPlayer(
                self.bot, guild, channel, self,
                guild_path,
                self.cache_manager  # Pass the cache_manager instance
            )
            logger.debug(f"새 MusicPlayer 생성: guild_id={guild.id}")
        return self.players[guild.id]

    # Inside MusicCog class, update the play command
    @song.command(name="재생", description="노래를 검색하거나 URL로 바로 재생합니다.")
    @app_commands.describe(query="곡 이름으로 검색하거나 URL로 바로 재생해보세요.")
    async def play(self, interaction: Interaction, query: str):
        await interaction.response.defer()

        try:
            # Check if user is in a voice channel
            if not interaction.user.voice:
                await interaction.followup.send("먼저 음성 채널에 접속해주세요.", ephemeral=True)
                return

            voice_channel = interaction.user.voice.channel

            # Check if bot is already in a different voice channel
            if interaction.guild.voice_client and interaction.guild.voice_client.channel != voice_channel:
                # Check if anyone is using the bot in the other channel
                if len(interaction.guild.voice_client.channel.members) > 1:  # More than just the bot
                    await interaction.followup.send(
                        "다른 음성 채널에서 이미 음악이 재생 중입니다.",
                        ephemeral=True
                    )
                    return

            player = self.get_player(interaction.guild, interaction.channel)

            # Handle voice client connection with proper error checking
            try:
                if not player.voice_client or not player.voice_client.is_connected():
                    # Clean up any existing voice client
                    if player.voice_client:
                        await player.voice_client.disconnect()
                        await asyncio.sleep(1)

                    # Connect to voice channel
                    player.voice_client = await voice_channel.connect()
                    await asyncio.sleep(1)  # Give time for connection to stabilize

                elif player.voice_client.channel != voice_channel:
                    await player.voice_client.move_to(voice_channel)
                    await asyncio.sleep(1)  # Give time for movement to complete

                # Verify connection
                if not player.voice_client or not player.voice_client.is_connected():
                    raise Exception("Failed to establish voice connection")

            except Exception as e:
                logger.error(f"Voice connection error: {e}")
                await interaction.followup.send("음성 채널 연결에 실패했습니다.", ephemeral=True)
                return

            if is_url(query):
                try:
                    status_msg = await interaction.followup.send(
                        "🔄 곡을 다운로드하고 있습니다...",
                        ephemeral=True
                    )

                    source, song_id = await YTDLSource.from_url(
                        query, download=True,
                        loop=self.bot.loop,
                        music_dir=player.music_dir,
                        cache_manager=self.cache_manager  # Pass cache_manager here
                    )

                    await player.add_to_queue(source.data, song_id)

                    # Wait for download to complete
                    song_entry = await self.cache_manager.get(song_id)
                    if song_entry and song_entry.is_downloading:
                        try:
                            await asyncio.wait_for(song_entry.download_future, timeout=30)
                        except asyncio.TimeoutError:
                            await status_msg.edit(content="다운로드 시간이 초과되었습니다.")
                            return

                    # Start playback if not already playing
                    if not player.voice_client.is_playing():
                        success = await player.play_next()
                        if not success:
                            await status_msg.edit(content="재생을 시작할 수 없습니다.")
                            return

                    # Delete status message and show success message
                    await status_msg.delete()
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

                except Exception as e:
                    logger.exception(f"Error processing URL: {e}")
                    await interaction.followup.send(
                        "URL 처리 중 오류가 발생했습니다.",
                        ephemeral=True
                    )
                    return

            else:
                try:
                    # Show searching message
                    status_msg = await interaction.followup.send(
                        "🔍 검색 중...",
                        ephemeral=True
                    )

                    # Optimize search options
                    search_options = ytdlp_format_options.copy()
                    search_options.update({
                        'default_search': 'ytsearch5',
                        'extract_flat': True,  # Faster search results
                        'force_generic_extractor': True  # Even faster search
                    })

                    # Perform search with timeout
                    try:
                        info = await asyncio.wait_for(
                            self.bot.loop.run_in_executor(
                                download_executor,
                                lambda: yt_dlp.YoutubeDL(search_options).extract_info(
                                    f"ytsearch5:{query}", download=False
                                )
                            ),
                            timeout=10
                        )
                    except asyncio.TimeoutError:
                        await status_msg.edit(content="검색 시간이 초과되었습니다. 다시 시도해주세요.")
                        return

                    # Delete the searching message
                    await status_msg.delete()

                    if not info.get('entries'):
                        await interaction.followup.send("검색 결과가 없습니다.", ephemeral=True)
                        return

                    results = []
                    for entry in info['entries'][:5]:
                        if entry and isinstance(entry, dict):
                            if 'url' not in entry and 'id' in entry:
                                entry['url'] = f"https://www.youtube.com/watch?v={entry['id']}"
                            results.append(entry)

                    # Create search results embed
                    embed = discord.Embed(title="🔍 검색 결과", color=discord.Color.blue())
                    embed.set_footer(text="60초 후에 자동으로 만료됩니다.")

                    for idx, result in enumerate(results, 1):
                        title = result.get('title', 'Unknown Title')[:100]
                        duration = format_duration(result.get('duration', 0))
                        embed.add_field(
                            name=f"{idx}. {title}",
                            value=f"⏱️ {duration}",
                            inline=False
                        )

                    # Create view and send message
                    view = SearchResultsView(self, interaction, results)

                    # Get list of members in the voice channel
                    voice_channel_members = [member.id for member in voice_channel.members]

                    # Send message only visible to voice channel members
                    message = await interaction.followup.send(
                        embed=embed,
                        view=view,
                        ephemeral=True
                    )

                    # Store message reference in view
                    view.set_message(message)

                except Exception as e:
                    logger.exception(f"Search error: {e}")
                    await interaction.followup.send(
                        "검색 중 오류가 발생했습니다.",
                        ephemeral=True
                    )

        except Exception as e:
            logger.exception(f"Play command error: {e}")
            await interaction.followup.send("재생 중 오류가 발생했습니다.", ephemeral=True)

    @song.command(name="정지", description="재생을 멈추고 대기열을 비웁니다.")
    async def stop(self, interaction: Interaction):
        try:
            player = self.get_player(interaction.guild, interaction.channel)

            # Reset player states before cleanup
            player.loop = False  # Disable repeat mode

            # Reset button states if control view exists
            if player.control_view:
                for child in player.control_view.children:
                    if child.custom_id == "loop":
                        child.style = discord.ButtonStyle.secondary
                        child.label = "🔄"
                    elif child.custom_id == "pause":
                        child.style = discord.ButtonStyle.secondary
                        child.label = "⏸️"

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
                song_entry = await self.cache_manager.get(song_id)  # Use cache_manager instead
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