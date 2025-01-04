# Part 1: Imports and Base Classes
import asyncio
import hashlib
import logging
import os
import random
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Dict, List, Tuple, Any, Union
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

# Constants for configuration
CACHE_CLEANUP_INTERVAL = 3600  # 1 hour in seconds
DOWNLOAD_TIMEOUT = 300  # Increased to 5 minutes
VOICE_TIMEOUT = 600  # Increased to 10 minutes
MAX_RETRIES = 3
DEFAULT_VOLUME = 0.05

# Exception classes
class MusicBotError(Exception):
    """Base exception class for music bot errors"""
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


# Part 2: Utility Functions

class VoiceConnectionPool:
    def __init__(self):
        self.connections = {}
        self.lock = asyncio.Lock()

    async def get_connection(self, channel: discord.VoiceChannel) -> discord.VoiceClient:
        async with self.lock:
            if channel.guild.id in self.connections:
                connection = self.connections[channel.guild.id]
                if connection.is_connected():
                    if connection.channel.id != channel.id:
                        await connection.move_to(channel)
                    return connection

            connection = await channel.connect()
            self.connections[channel.guild.id] = connection
            return connection

    async def disconnect(self, guild_id: int):
        if guild_id in self.connections:
            try:
                await self.connections[guild_id].disconnect()
            finally:
                self.connections.pop(guild_id, None)


# Utility Functions
def construct_youtube_url(video_id: str) -> str:
    """Constructs a proper YouTube URL from a video ID."""
    return f"https://www.youtube.com/watch?v={video_id}"


def generate_song_id(url: str, title: str) -> str:
    """Generate a unique ID for a song based on URL and title."""
    combined = f"{url}-{title}"
    return hashlib.md5(combined.encode()).hexdigest()[:8]


def get_video_id(url: str) -> Optional[str]:
    """Extract video ID from YouTube URL."""
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
    """Validate if the URL is a valid YouTube URL."""
    try:
        parsed = urlparse(url)
        if parsed.netloc in ('www.youtube.com', 'youtube.com', 'youtu.be'):
            if parsed.path == '/watch' or parsed.netloc == 'youtu.be':
                return True
    except Exception:
        pass
    return False

def sanitize_query(query: str) -> str:
    """Sanitize search query string."""
    sanitized = ' '.join(query.split())
    return sanitized[:100]

def format_duration(seconds: int) -> str:
    """Format duration in seconds to HH:MM:SS format."""
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
    """Clean filename for safe file system operations."""
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
    """Handle command errors and send appropriate response."""
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
    """Delete a message after specified delay."""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except discord.NotFound:
        pass  # Message already deleted
    except Exception as e:
        logger.error(f"Failed to delete message: {e}")



class ErrorHandler:
    @staticmethod
    async def handle_ffmpeg_error(error: Exception) -> str:
        """Handle FFmpeg-related errors."""
        error_str = str(error).lower()
        if "ffmpeg not found" in error_str:
            return "FFmpeg가 설치되어 있지 않습니다. 관리자에게 문의해주세요."
        elif "opus" in error_str:
            return "Opus 코덱이 설치되어 있지 않습니다. 관리자에게 문의해주세요."
        return str(error)

    @staticmethod
    async def handle_playback_error(error: Exception, player: 'MusicPlayer') -> bool:
        """Handle playback-related errors with improved error handling."""
        if isinstance(error, discord.ClientException):
            await player.handle_disconnect()
            return False
        elif isinstance(error, discord.opus.OpusNotLoaded):
            try:
                discord.opus.load_opus('libopus.so.0')
                return True
            except:
                return False
        elif isinstance(error, Exception):
            error_msg = await ErrorHandler.handle_ffmpeg_error(error)
            logger.error(f"Playback error: {error_msg}")
            return False
        return False

class DownloadQueue:
    def __init__(self, max_concurrent=3):
        self.queue = asyncio.Queue()
        self.active = set()
        self.max_concurrent = max_concurrent
        self.semaphore = asyncio.Semaphore(max_concurrent)

    async def add_download(self, song_id: str, url: str, callback):
        """Add a download task to the queue."""
        await self.queue.put((song_id, url, callback))
        asyncio.create_task(self._process_queue())

    async def _process_queue(self):
        """Process the download queue with rate limiting."""
        async with self.semaphore:
            if self.queue.empty():
                return

            song_id, url, callback = await self.queue.get()
            if song_id in self.active:
                self.queue.task_done()
                return

            self.active.add(song_id)
            try:
                await callback(song_id, url)
            finally:
                self.active.remove(song_id)
                self.queue.task_done()


# Part 3: Cache Manager and YTDLSource

class CacheManager:
    def __init__(self):
        self.cache: Dict[str, SongData] = {}
        self.lock = Lock()
        self._cleanup_task = None
        self.download_queue = DownloadQueue()

    async def get(self, song_id: str) -> Optional[SongData]:
        """Get a song from cache."""
        async with self.lock:
            return self.cache.get(song_id)

    async def set(self, song_id: str, song_data: SongData) -> None:
        """Add or update a song in cache."""
        async with self.lock:
            self.cache[song_id] = song_data

    async def remove(self, song_id: str) -> None:
        """Remove a song from cache."""
        async with self.lock:
            if song_id in self.cache:
                song_data = self.cache[song_id]
                if song_data.file_path and os.path.exists(song_data.file_path):
                    try:
                        os.remove(song_data.file_path)
                        logger.debug(f"Deleted file: {song_data.file_path}")
                    except OSError as e:
                        logger.error(f"Failed to delete file: {e}")
                self.cache.pop(song_id)

    async def cleanup_expired(self) -> None:
        """Remove expired entries and their files."""
        async with self.lock:
            current_time = datetime.now()
            expired_ids = [
                song_id for song_id, data in self.cache.items()
                if (current_time - data.added_at) > timedelta(hours=1) and not data.is_downloading
            ]

            for song_id in expired_ids:
                await self.remove(song_id)

    def start_cleanup_task(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start the periodic cleanup task."""

        async def cleanup_loop():
            while True:
                try:
                    await asyncio.sleep(CACHE_CLEANUP_INTERVAL)
                    await self.cleanup_expired()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Cache cleanup error: {e}")

        self._cleanup_task = loop.create_task(cleanup_loop())

    def stop_cleanup_task(self) -> None:
        """Stop the cleanup task."""
        if self._cleanup_task:
            self._cleanup_task.cancel()

    async def get_or_download(self, song_id: str, url: str, download_callback) -> SongData:
        """Get a song from cache or download it if not present."""
        song_data = await self.get(song_id)
        if not song_data:
            song_data = SongData(
                title="Downloading...",
                url=url,
                thumbnail="",
                duration=0,
                is_downloading=True
            )
            await self.set(song_id, song_data)
            await self.download_queue.add_download(song_id, url, download_callback)
        return song_data


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

    @classmethod
    async def cleanup_partial_downloads(cls, music_dir: str, song_id: str):
        """Clean up any partial downloads for a given song ID."""
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
        """Download a song and return its info and file path."""
        for attempt in range(MAX_RETRIES):
            try:
                async with asyncio.timeout(DOWNLOAD_TIMEOUT):
                    # Get info without downloading
                    info_options = ytdlp_format_options.copy()
                    info_options['extract_flat'] = False
                    info_options['download'] = False

                    info = await loop.run_in_executor(
                        download_executor,
                        lambda: yt_dlp.YoutubeDL(info_options).extract_info(url, download=False)
                    )

                    if not info:
                        raise DownloadError("No data received from yt-dlp")

                    # Create output path with cleaned title
                    clean_title = clean_filename(info.get('title', 'unknown'))
                    base_path = os.path.join(music_dir, f'{song_id}-{clean_title}')

                    # Download with specific options
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

                    # Download and process
                    await loop.run_in_executor(
                        download_executor,
                        lambda: yt_dlp.YoutubeDL(download_options).download([url])
                    )

                    # Check for downloaded file
                    expected_path = f"{base_path}.opus"
                    if os.path.exists(expected_path):
                        return info, expected_path

                    # Check other possible extensions
                    for ext in ['.opus', '.m4a', '.mp3', '.webm']:
                        test_path = f"{base_path}{ext}"
                        if os.path.exists(test_path):
                            return info, test_path

                    raise DownloadError("Downloaded file not found")

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

    @classmethod
    async def from_url(cls, url: str, download: bool, loop, music_dir: str, cache_manager: CacheManager) -> Tuple[
        'YTDLSource', str]:
        """Create a YTDLSource from a URL."""
        try:
            # Extract info
            info = await cls.get_video_info(url, loop)

            # Get best thumbnail
            thumbnail_url = cls.get_best_thumbnail(info)

            # Generate song ID and path
            song_id = generate_song_id(url, info['title'])
            clean_title = clean_filename(info['title'])
            file_path = os.path.join(music_dir, f'{song_id}-{clean_title}.opus')

            # Handle download
            song_entry = await cache_manager.get(song_id)
            if not song_entry:
                song_entry = SongData(
                    title=info['title'],
                    url=url,
                    thumbnail=thumbnail_url,
                    duration=info.get('duration', 0),
                    is_downloading=True,
                    file_path=file_path
                )
                await cache_manager.set(song_id, song_entry)
                song_entry.download_future = asyncio.create_task(
                    cls.download_song(song_id, url, music_dir, loop))

            return cls(
                file_path=file_path if download else info.get('url'),
                data=info,
                thumbnail=thumbnail_url,
                duration=info.get('duration', 0),
                song_id=song_id,
                volume=DEFAULT_VOLUME
            ), song_id

        except Exception as e:
            logger.exception(f"Error processing URL {url}: {e}")
            raise

    @staticmethod
    async def get_video_info(url: str, loop) -> dict:
        """Get video information without downloading."""
        info_options = ytdlp_format_options.copy()
        info_options['extract_flat'] = False
        try:
            async with asyncio.timeout(10):
                data = await loop.run_in_executor(
                    download_executor,
                    lambda: yt_dlp.YoutubeDL(info_options).extract_info(url, download=False)
                )
                if not data:
                    raise ValueError("No data received from yt-dlp")
                if 'entries' in data:
                    data = data['entries'][0]
                return data
        except asyncio.TimeoutError:
            logger.error(f"Timeout while processing URL: {url}")
            raise

    @staticmethod
    def get_best_thumbnail(info: dict) -> Optional[str]:
        """Get the best quality thumbnail from video info."""
        if 'thumbnails' in info and isinstance(info['thumbnails'], list):
            thumbnails = sorted(
                [t for t in info['thumbnails'] if isinstance(t, dict) and 'url' in t],
                key=lambda x: x.get('width', 0) * x.get('height', 0),
                reverse=True
            )
            if thumbnails:
                return thumbnails[0]['url']
        return info.get('thumbnail')


# Part 4: UI Views - Search Results and Music Controls

class SearchResultsView(View):
    def __init__(self, music_cog, interaction: Interaction, results: List[Dict]):
        super().__init__(timeout=60)
        self.music_cog = music_cog
        self.original_interaction = interaction
        self.original_user = interaction.user
        self.voice_channel = interaction.user.voice.channel
        self.results = results[:5]
        self.message = None

        # Add buttons for each search result
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
            # Validate user and voice state
            if not await self.validate_interaction(interaction):
                return

            try:
                logger.debug(f"SearchResultsView button click: index={index}")

                # Clean up search results message
                await self.cleanup_search_message()

                # Get selected result and prepare data
                result = self.prepare_result(self.results[index])
                if not result:
                    await interaction.response.send_message(
                        "선택한 곡의 정보를 찾을 수 없습니다.",
                        ephemeral=True
                    )
                    return

                # Initialize player and song
                player = self.music_cog.get_player(interaction.guild, interaction.channel)
                song_id = generate_song_id(result['webpage_url'], result['title'])

                # Show download status
                await interaction.response.defer()
                status_msg = await self.show_download_status(interaction, result)

                # Add to queue and start playback
                try:
                    await player.add_to_queue(result, song_id)
                    if not player.voice_client or not player.voice_client.is_playing():
                        if not await player.play_next():
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

                # Clean up status and show success message
                await self.cleanup_and_show_success(interaction, status_msg, result)

            except Exception as e:
                logger.exception(f"Error processing selection: {e}")
                await self.handle_error(interaction)

        return callback

    async def validate_interaction(self, interaction: Interaction) -> bool:
        """Validate user interaction and voice state."""
        if not interaction.user.voice or interaction.user.voice.channel != self.voice_channel:
            await interaction.response.send_message(
                "이 명령어를 사용하려면 음성 채널에 참가해야 합니다.",
                ephemeral=True
            )
            return False

        if (interaction.guild.voice_client and
                interaction.guild.voice_client.channel != self.voice_channel and
                len(interaction.guild.voice_client.channel.members) > 1):
            await interaction.response.send_message(
                "다른 음성 채널에서 이미 음악이 재생 중입니다.",
                ephemeral=True
            )
            return False

        return True

    async def cleanup_search_message(self):
        """Clean up the search results message."""
        if self.message:
            try:
                await self.message.delete()
            except discord.NotFound:
                pass
            except Exception as e:
                logger.error(f"Error deleting search results message: {e}")

    def prepare_result(self, result: Dict) -> Optional[Dict]:
        """Prepare the selected result data."""
        if 'webpage_url' not in result and 'id' in result:
            result['webpage_url'] = f"https://www.youtube.com/watch?v={result['id']}"
        elif 'webpage_url' not in result and 'url' in result:
            result['webpage_url'] = result['url']

        if 'webpage_url' not in result:
            return None

        if 'title' not in result:
            result['title'] = f"Unknown Title {result.get('id', 'No ID')}"

        return result

    async def show_download_status(self, interaction: Interaction, result: Dict) -> Optional[discord.Message]:
        """Show download status message."""
        try:
            return await interaction.followup.send(
                embed=discord.Embed(
                    title="🔄 다운로드 중...",
                    description=f"**{result['title']}**\n잠시만 기다려주세요...",
                    color=discord.Color.blue()
                ),
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error sending status message: {e}")
            return None

    async def cleanup_and_show_success(self, interaction: Interaction, status_msg: Optional[discord.Message],
                                       result: Dict):
        """Clean up status message and show success message."""
        if status_msg:
            try:
                await status_msg.delete()
            except (discord.NotFound, discord.HTTPException):
                pass

        success_msg = await interaction.followup.send(
            f"🎵 추가됨: {result['title']}",
            ephemeral=False
        )

        try:
            await asyncio.sleep(3)
            await success_msg.delete()
        except (discord.NotFound, discord.HTTPException):
            pass

    async def handle_error(self, interaction: Interaction):
        """Handle errors during song selection."""
        try:
            await interaction.followup.send(
                "선택한 곡을 처리하는 중 오류가 발생했습니다.",
                ephemeral=True
            )
        except:
            pass

    async def on_timeout(self):
        """Handle view timeout."""
        try:
            logger.debug("SearchResultsView timeout occurred")
            # Disable all buttons
            for item in self.children:
                item.disabled = True

            if self.message:
                try:
                    # Show timeout message
                    timeout_embed = discord.Embed(
                        title="⏰ 시간 만료",
                        description="검색 시간이 초과되었습니다. 다시 검색해주세요.",
                        color=discord.Color.red()
                    )
                    await self.message.edit(embed=timeout_embed, view=None)

                    # Delete message after delay
                    await asyncio.sleep(5)
                    await self.message.delete()
                except discord.NotFound:
                    pass
                except Exception as e:
                    logger.error(f"Error in timeout handler: {e}")
        except Exception as e:
            logger.error(f"Error in timeout handler: {e}")

    def set_message(self, message):
        """Set the view's message reference."""
        self.message = message


class MusicControlView(View):
    def __init__(self, player: 'MusicPlayer', music_cog: 'MusicCog'):
        super().__init__(timeout=None)
        self.player = player
        self.music_cog = music_cog
        self._update_button_states()

    def _update_button_states(self) -> None:
        """Update button states based on player state."""
        is_playing = bool(self.player.voice_client and self.player.voice_client.is_playing())
        is_paused = bool(self.player.voice_client and self.player.voice_client.is_paused())
        has_queue = bool(self.player.queue)

        for child in self.children:
            if isinstance(child, Button):
                self._update_button(child, is_playing, is_paused, has_queue)

    def _update_button(self, button: Button, is_playing: bool, is_paused: bool, has_queue: bool):
        """Update individual button state."""
        if button.custom_id == "pause":
            button.disabled = not is_playing
        elif button.custom_id == "resume":
            button.disabled = not is_paused
        elif button.custom_id == "skip":
            button.disabled = not is_playing and not has_queue
        elif button.custom_id == "stop":
            button.disabled = not (is_playing or is_paused or has_queue)
        elif button.custom_id == "shuffle":
            button.disabled = len(self.player.queue) < 2

    async def _handle_interaction(self, interaction: Interaction, action: str, handler: callable) -> None:
        """Generic interaction handler with error handling."""
        if not await self._validate_user(interaction):
            return

        try:
            await handler()
            self._update_button_states()
            await self._update_view(interaction)
        except Exception as e:
            logger.error(f"Error in {action}: {e}")
            await interaction.response.send_message(
                f"{action} 처리 중 오류가 발생했습니다.",
                ephemeral=True
            )

    async def _validate_user(self, interaction: Interaction) -> bool:
        """Validate user's voice state."""
        if not interaction.user.voice or interaction.user.voice.channel != self.player.voice_client.channel:
            await interaction.response.send_message(
                "이 명령어를 사용하려면 음성 채널에 참가해야 합니다.",
                ephemeral=True
            )
            return False
        return True

    async def _update_view(self, interaction: Interaction):
        """Update view in response."""
        if interaction.response.is_done():
            await interaction.edit_original_response(view=self)
        else:
            await interaction.response.edit_message(view=self)

    # Button Definitions
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
        async def resume_handler():
            if self.player.voice_client.is_paused():
                self.player.voice_client.resume()
                for child in self.children:
                    if child.custom_id == "pause":
                        child.style = discord.ButtonStyle.secondary
                        child.label = "⏸️"
                await interaction.response.send_message("재생을 재개했습니다.", ephemeral=True)

        await self._handle_interaction(interaction, "재개", resume_handler)

    @discord.ui.button(label="⏭️", style=discord.ButtonStyle.secondary, custom_id="skip", row=0)
    async def skip(self, interaction: Interaction, button: Button):
        async def skip_handler():
            if self.player.voice_client.is_playing():
                self.player.voice_client.stop()
                await interaction.response.send_message("현재 곡을 건너뜁니다.", ephemeral=True)

        await self._handle_interaction(interaction, "건너뛰기", skip_handler)

    @discord.ui.button(label="🔄", style=discord.ButtonStyle.secondary, custom_id="loop", row=1)
    async def toggle_loop(self, interaction: Interaction, button: Button):
        async def loop_handler():
            if not self.player.current and not self.player.queue:
                await interaction.response.send_message(
                    "재생 중인 곡이 없어 반복 모드를 설정할 수 없습니다.",
                    ephemeral=True
                )
                return

            self.player.loop = not self.player.loop
            button.style = discord.ButtonStyle.primary if self.player.loop else discord.ButtonStyle.secondary
            button.label = "🔄 (반복)" if self.player.loop else "🔄"

            if self.player.current:
                await self.player.update_now_playing()

        await self._handle_interaction(interaction, "반복 모드", loop_handler)

    @discord.ui.button(label="⏹️", style=discord.ButtonStyle.danger, custom_id="stop", row=1)
    async def stop(self, interaction: Interaction, button: Button):
        async def stop_handler():
            self.player.loop = False
            await self.player.cleanup()
            self.music_cog.players.pop(interaction.guild.id, None)
            await interaction.response.send_message("재생을 멈추고 대기열을 비웠습니다.", ephemeral=True)

        await self._handle_interaction(interaction, "정지", stop_handler)

    @discord.ui.button(label="🔀", style=discord.ButtonStyle.secondary, custom_id="shuffle", row=1)
    async def shuffle(self, interaction: Interaction, button: Button):
        async def shuffle_handler():
            if len(self.player.queue) < 2:
                await interaction.response.send_message("대기열에 곡이 충분하지 않습니다.", ephemeral=True)
                return

            random.shuffle(self.player.queue)
            await interaction.response.send_message("대기열을 섞었습니다.", ephemeral=True)
            await self.player.update_now_playing()

        await self._handle_interaction(interaction, "셔플", shuffle_handler)


# Part 5A: Music Player Core Class

class MusicPlayer:
    def __init__(self, bot, guild, channel, music_cog, music_dir, cache_manager):
        self.bot = bot
        self.guild = guild
        self.channel = channel
        self.music_cog = music_cog
        self.music_dir = music_dir
        self.cache_manager = cache_manager
        self.queue = deque()  # Removed maxlen limit
        self.voice_client = None
        self.current = None
        self.loop = False
        self.embed_message = None
        self._volume = DEFAULT_VOLUME
        self.control_view = None
        self.check_task = self.bot.loop.create_task(self.check_voice_channel())
        self.default_thumbnail = 'https://cdn.discordapp.com/attachments/1134007524320870451/default_music.png'
        self.messages_to_clean = set()
        self.is_playing = False
        self.download_queue = DownloadQueue()

    async def connect_to_voice(self, voice_channel: discord.VoiceChannel) -> bool:
        """Connect to a voice channel with error handling."""
        try:
            if self.voice_client:
                if self.voice_client.is_connected():
                    if self.voice_client.channel != voice_channel:
                        await self.voice_client.move_to(voice_channel)
                    return True
                else:
                    await self.voice_client.disconnect()
                    self.voice_client = None

            self.voice_client = await voice_channel.connect()
            return True
        except Exception as e:
            logger.error(f"Voice connection error: {e}")
            return False

    async def cleanup(self):
        """Clean up resources and reset player state with improved cleanup."""
        try:
            logger.debug("MusicPlayer.cleanup called")

            # Stop playback and disconnect
            if self.voice_client:
                if self.voice_client.is_playing():
                    self.voice_client.stop()
                await self.voice_client.disconnect()
                self.voice_client = None

            # Clean up current song file
            if self.current and 'file_path' in self.current:
                await self.cleanup_file(self.current['file_path'])
                # Clean up any partial downloads
                await YTDLSource.cleanup_partial_downloads(
                    self.music_dir,
                    self.current.get('song_id', '')
                )

            # Clean up download queue
            if hasattr(self, 'download_queue'):
                try:
                    while not self.download_queue.queue.empty():
                        song_id, _, _ = await self.download_queue.queue.get()
                        await YTDLSource.cleanup_partial_downloads(self.music_dir, song_id)
                except Exception as e:
                    logger.error(f"Error cleaning download queue: {e}")

            # Clean up embedded message
            await self.cleanup_embed_message()

            # Reset control view
            await self.reset_control_view()

            # Cancel check task
            if self.check_task:
                self.check_task.cancel()

            # Clean up remaining files
            await self.cleanup_music_directory()

            # Reset player state
            self.queue.clear()
            self.current = None
            self.loop = False
            self._volume = DEFAULT_VOLUME
            self.is_playing = False

            logger.debug("MusicPlayer.cleanup completed")
        except Exception as e:
            logger.exception(f"Cleanup error: {e}")

    async def cleanup_file(self, file_path: str, max_retries: int = 5) -> None:
        """Clean up a single file with improved retry logic."""
        if not file_path or not os.path.exists(file_path):
            return

        for attempt in range(max_retries):
            try:
                os.remove(file_path)
                logger.debug(f"Deleted file: {file_path}")
                break
            except PermissionError:
                await asyncio.sleep(1)
                continue
            except FileNotFoundError:
                logger.debug(f"File already deleted: {file_path}")
                break
            except Exception as e:
                logger.error(f"Error deleting file {file_path}: {e}")
                if attempt == max_retries - 1:
                    raise

    async def cleanup_embed_message(self):
        """Clean up the embedded message."""
        if self.embed_message:
            try:
                await self.embed_message.delete()
            except Exception as e:
                logger.error(f"Failed to delete embed message: {e}")
            finally:
                self.embed_message = None

    async def reset_control_view(self):
        """Reset control view state."""
        if self.control_view:
            for child in self.control_view.children:
                if child.custom_id == "loop":
                    child.style = discord.ButtonStyle.secondary
                    child.label = "🔄"
                elif child.custom_id == "pause":
                    child.style = discord.ButtonStyle.secondary
                    child.label = "⏸️"
            self.control_view = None

    async def cleanup_music_directory(self):
        """Clean up the music directory."""
        try:
            for file in os.listdir(self.music_dir):
                file_path = os.path.join(self.music_dir, file)
                await self.cleanup_file(file_path)
        except Exception as e:
            logger.error(f"Error cleaning up music directory: {e}")

    async def check_voice_channel(self):
        """Monitor voice channel for inactivity."""
        try:
            logger.debug("MusicPlayer.check_voice_channel started")
            await self.bot.wait_until_ready()

            while not self.bot.is_closed():
                try:
                    if self.voice_client and len(self.voice_client.channel.members) <= 1:
                        logger.info("No users remaining in voice channel, cleaning up")
                        await self.cleanup_and_disconnect()
                        break
                except Exception as e:
                    logger.error(f"Voice channel check error: {e}")
                await asyncio.sleep(10)

        except asyncio.CancelledError:
            logger.debug("MusicPlayer.check_voice_channel cancelled")
        except Exception as e:
            logger.exception(f"Voice channel check error: {e}")

    async def cleanup_and_disconnect(self):
        """Clean up messages and disconnect."""
        try:
            logger.debug("MusicPlayer.cleanup_and_disconnect called")

            # Clean up messages
            for message_id in self.messages_to_clean:
                try:
                    message = await self.channel.fetch_message(message_id)
                    await message.delete()
                except Exception as e:
                    logger.error(f"Failed to delete message ID {message_id}: {e}")

            self.messages_to_clean.clear()
            await self.cleanup()

            logger.debug("MusicPlayer.cleanup_and_disconnect completed")
        except Exception as e:
            logger.exception(f"Cleanup and disconnect error: {e}")

    # Part 5B: Music Player Playback Functions

    # Continue MusicPlayer class...
    async def add_to_queue(self, song_data: Dict[str, Any], song_id: str) -> None:
        """Add a song to the queue."""
        song_entry = await self.cache_manager.get(song_id)
        if song_entry:
            if song_entry.is_downloading:
                logger.info(f"Already downloading: {song_entry.title}")
            else:
                logger.info(f"Already downloaded: {song_entry.title}")
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

        self.queue.append(song_id)
        await self.update_controls()

    async def download_song(self, song_id: str, song_data: dict):
        """Download a song with error handling."""
        try:
            logger.debug(f"Downloading song: song_id={song_id}")
            song_info, file_path = await YTDLSource.download_song(
                song_id, song_data['url'], self.music_dir, self.bot.loop
            )

            song_entry = await self.cache_manager.get(song_id)
            if song_entry:
                song_entry.file_path = file_path
                song_entry.is_downloading = False
                await self.cache_manager.set(song_id, song_entry)

            logger.debug(f"Download completed: song_id={song_id}")

        except Exception as e:
            logger.exception(f"Download failed for {song_data.get('title')}: {e}")
            if song_id in self.queue:
                self.queue.remove(song_id)
            await self.cache_manager.remove(song_id)
            await self.update_now_playing()

    async def update_controls(self):
        """Update control view state."""
        if self.control_view:
            self.control_view._update_button_states()
            if self.embed_message:
                try:
                    await self.embed_message.edit(view=self.control_view)
                except Exception as e:
                    logger.error(f"Error updating control view: {e}")

    async def update_now_playing(self) -> None:
        """Update the now playing embed message."""
        try:
            logger.debug("Updating now playing message")
            await self.cleanup_embed_message()

            embed = await self.create_now_playing_embed()

            if not self.control_view:
                self.control_view = MusicControlView(self, self.music_cog)

            try:
                self.embed_message = await self.channel.send(embed=embed, view=self.control_view)
                self.messages_to_clean.add(self.embed_message.id)
            except discord.HTTPException:
                embed.set_thumbnail(url=None)
                self.embed_message = await self.channel.send(embed=embed, view=self.control_view)
                self.messages_to_clean.add(self.embed_message.id)

        except Exception as e:
            logger.exception(f"Update now playing error: {e}")

    async def create_now_playing_embed(self) -> discord.Embed:
        """Create the now playing embed."""
        embed = discord.Embed(color=discord.Color.blue())

        if not self.current:
            embed.title = "🎵 현재 재생 중인 곡 없음"
            embed.description = "현재 재생 중인 곡이 없습니다."
            embed.color = discord.Color.red()
            if is_url(self.default_thumbnail):
                embed.set_thumbnail(url=self.default_thumbnail)
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
                embed.add_field(name="반복 모드", value="🔄 활성화", inline=False)

            # Set thumbnail
            thumbnail_url = self.current.get('thumbnail')
            if thumbnail_url and is_url(thumbnail_url):
                embed.set_thumbnail(url=thumbnail_url)
            elif is_url(self.default_thumbnail):
                embed.set_thumbnail(url=self.default_thumbnail)

            # Add next song info if available
            if self.queue:
                next_song = await self.get_next_song_info()
                if next_song:
                    embed.add_field(
                        name="다음 곡",
                        value=f"**{next_song['title']}**\n⏱️ {format_duration(next_song['duration'])}",
                        inline=False
                    )

        return embed

    async def get_next_song_info(self) -> Optional[Dict[str, Union[str, int]]]:
        """Get information about the next song in queue."""
        if not self.queue:
            return None

        next_song_id = self.queue[0]
        next_song_entry = await self.cache_manager.get(next_song_id)
        if next_song_entry:
            return {
                'title': next_song_entry.title,
                'duration': next_song_entry.duration
            }
        return None

    async def play_next(self) -> bool:
        """Play the next song in queue."""
        try:
            logger.debug("Playing next song")
            await self.cleanup_embed_message()

            # Handle loop functionality
            if self.loop and self.current:
                current_song_id = self.current['song_id']
                if current_song_id not in self.queue:
                    self.queue.appendleft(current_song_id)
                    logger.debug(f"Loop mode: Added current song back to queue")

            if not self.queue:
                self.current = None
                await self.update_now_playing()
                return False

            # Get next song
            song_id = self.queue[0]  # Peek at next song without removing
            song_entry = await self.cache_manager.get(song_id)

            if not song_entry:
                logger.error(f"Song ID {song_id} not found in cache")
                self.queue.popleft()  # Remove invalid song
                return await self.play_next()

            # Wait for download if needed
            if song_entry.is_downloading:
                try:
                    await asyncio.wait_for(song_entry.download_future, timeout=30)
                except asyncio.TimeoutError:
                    logger.error(f"Download timeout for {song_entry.title}")
                    self.queue.popleft()
                    return await self.play_next()

            # Verify file exists
            if not song_entry.file_path or not os.path.exists(song_entry.file_path):
                logger.error(f"File not found for {song_entry.title}")
                self.queue.popleft()
                return await self.play_next()

            # Create audio source
            audio_source = discord.FFmpegPCMAudio(song_entry.file_path, **ffmpeg_options)
            transformed_source = PCMVolumeTransformer(audio_source, volume=self._volume)

            # Start playback
            if self.voice_client and self.voice_client.is_connected():
                self.queue.popleft()  # Remove song from queue only after everything is ready
                self.voice_client.play(
                    transformed_source,
                    after=lambda e: asyncio.run_coroutine_threadsafe(
                        self.handle_playback_finished(e), self.bot.loop
                    )
                )

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
            logger.exception(f"Error in play_next: {e}")
            return False

    async def handle_playback_finished(self, error):
        """Handle playback finished event."""
        if error:
            logger.error(f"Error during playback: {error}")
        try:
            await self.play_next()
        except Exception as e:
            logger.exception(f"Error in handle_playback_finished: {e}")

    def create_current_song_info(self, song_entry: SongData) -> dict:
        """Create current song info dictionary."""
        return {
            'title': song_entry.title,
            'duration': song_entry.duration,
            'thumbnail': song_entry.thumbnail,
            'file_path': song_entry.file_path,
            'song_id': song_entry.song_id if hasattr(song_entry, 'song_id') else None
        }

    async def handle_disconnect(self):
        """Handle disconnection from voice channel."""
        try:
            await self.cleanup()
            self.voice_client = None
            logger.info("Successfully handled disconnect")
        except Exception as e:
            logger.error(f"Error handling disconnect: {e}")


# Part 6: Music Cog Class

@app_commands.guild_only()
class MusicCog(commands.Cog, name="Music"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.base_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cogs_data", "music_cog")
        os.makedirs(self.base_path, exist_ok=True)
        self.players = {}
        self.cache_manager = CacheManager()
        self.cache_manager.start_cleanup_task(self.bot.loop)
        self.voice_connection_pool = VoiceConnectionPool()

    async def cog_unload(self):
        """Clean up when the cog is unloaded."""
        self.cache_manager.stop_cleanup_task()
        for player in self.players.values():
            await player.cleanup()

    song = app_commands.Group(name="곡", description="음악 관련 명령어")

    def get_player(self, guild: discord.Guild, channel: discord.TextChannel) -> MusicPlayer:
        """Get or create a music player for a guild."""
        if guild.id not in self.players:
            guild_path = os.path.join(self.base_path, str(guild.id))
            os.makedirs(guild_path, exist_ok=True)
            self.players[guild.id] = MusicPlayer(
                self.bot, guild, channel, self,
                guild_path,
                self.cache_manager
            )
            logger.debug(f"Created new MusicPlayer: guild_id={guild.id}")
        return self.players[guild.id]

    @song.command(name="재생", description="노래를 검색하거나 URL로 바로 재생합니다.")
    @app_commands.describe(query="곡 이름으로 검색하거나 URL로 바로 재생해보세요.")
    async def play(self, interaction: Interaction, query: str):
        """Play a song by URL or search query."""
        await interaction.response.defer()

        try:
            if not await self.validate_voice_state(interaction):
                return

            player = self.get_player(interaction.guild, interaction.channel)

            # Connect to voice
            if not await self.handle_voice_connection(interaction, player):
                return

            if is_url(query):
                await self.handle_url_play(interaction, player, query)
            else:
                await self.handle_search_play(interaction, player, query)

        except Exception as e:
            logger.exception(f"Play command error: {e}")
            await interaction.followup.send("재생 중 오류가 발생했습니다.", ephemeral=True)

    async def validate_voice_state(self, interaction: Interaction) -> bool:
        """Validate user's voice state."""
        if not interaction.user.voice:
            await interaction.followup.send("먼저 음성 채널에 접속해주세요.", ephemeral=True)
            return False

        voice_channel = interaction.user.voice.channel
        if (interaction.guild.voice_client and
            interaction.guild.voice_client.channel != voice_channel and
            len(interaction.guild.voice_client.channel.members) > 1):
            await interaction.followup.send(
                "다른 음성 채널에서 이미 음악이 재생 중입니다.",
                ephemeral=True
            )
            return False

        return True

    async def handle_voice_connection(self, interaction: Interaction, player: MusicPlayer) -> bool:
        """Handle voice client connection."""
        try:
            voice_channel = interaction.user.voice.channel
            if not await player.connect_to_voice(voice_channel):
                await interaction.followup.send("음성 채널 연결에 실패했습니다.", ephemeral=True)
                return False
            return True
        except Exception as e:
            logger.error(f"Voice connection error: {e}")
            await interaction.followup.send("음성 채널 연결에 실패했습니다.", ephemeral=True)
            return False

    async def handle_url_play(self, interaction: Interaction, player: MusicPlayer, url: str):
        """Handle playing from URL."""
        try:
            status_msg = await interaction.followup.send(
                "🔄 곡을 다운로드하고 있습니다...",
                ephemeral=True
            )

            logger.debug(f"Downloading song from URL: {url}")
            source, song_id = await YTDLSource.from_url(
                url, download=True,
                loop=self.bot.loop,
                music_dir=player.music_dir,
                cache_manager=self.cache_manager
            )

            logger.debug(f"Adding song to queue: {song_id}")
            await player.add_to_queue(source.data, song_id)

            # Wait for download
            song_entry = await self.cache_manager.get(song_id)
            if song_entry and song_entry.is_downloading:
                try:
                    await asyncio.wait_for(song_entry.download_future, timeout=30)
                except asyncio.TimeoutError:
                    await status_msg.edit(content="다운로드 시간이 초과되었습니다.")
                    return

            # Start playback if not playing
            if not player.voice_client.is_playing():
                logger.debug("Starting playback")
                if not await player.play_next():
                    await status_msg.edit(content="재생을 시작할 수 없습니다.")
                    return

            # Show success message
            await status_msg.delete()
            message = await interaction.followup.send(
                f"🎵 추가됨: {source.title}",
                ephemeral=False
            )

            # Delete success message after delay
            await self.delete_message_after_delay(message)

        except Exception as e:
            logger.exception(f"Error processing URL: {e}")
            await interaction.followup.send(
                "URL 처리 중 오류가 발생했습니다.",
                ephemeral=True
            )

    async def handle_search_play(self, interaction: Interaction, player: MusicPlayer, query: str):
        """Handle search and play."""
        try:
            status_msg = await interaction.followup.send(
                "🔍 검색 중...",
                ephemeral=True
            )

            # Search for songs
            search_results = await self.search_songs(query)
            if not search_results:
                await status_msg.edit(content="검색 결과가 없습니다.")
                return

            # Delete status message
            await status_msg.delete()

            # Create and send search results
            embed = self.create_search_results_embed(search_results)
            view = SearchResultsView(self, interaction, search_results)
            message = await interaction.followup.send(
                embed=embed,
                view=view,
                ephemeral=True
            )
            view.set_message(message)

        except Exception as e:
            logger.exception(f"Search error: {e}")
            await interaction.followup.send(
                "검색 중 오류가 발생했습니다.",
                ephemeral=True
            )

    async def search_songs(self, query: str) -> List[Dict]:
        """Search for songs with timeout."""
        search_options = ytdlp_format_options.copy()
        search_options.update({
            'default_search': 'ytsearch5',
            'extract_flat': True,
            'force_generic_extractor': True
        })

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

            if not info.get('entries'):
                return []

            return [
                {**entry, 'url': f"https://www.youtube.com/watch?v={entry['id']}"}
                for entry in info['entries'][:5]
                if entry and isinstance(entry, dict)
            ]

        except asyncio.TimeoutError:
            logger.error("Search timeout")
            return []
        except Exception as e:
            logger.error(f"Search error: {e}")
            return []

    def create_search_results_embed(self, results: List[Dict]) -> discord.Embed:
        """Create embed for search results."""
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

        return embed

    async def delete_message_after_delay(self, message: discord.Message, delay: int = 3):
        """Delete a message after a delay."""
        try:
            await asyncio.sleep(delay)
            await message.delete()
        except (discord.NotFound, discord.HTTPException):
            pass

    # Add these commands to the MusicCog class

    @song.command(name="정지", description="재생을 멈추고 대기열을 비웁니다.")
    async def stop(self, interaction: Interaction):
        try:
            if not interaction.user.voice:
                await interaction.response.send_message("먼저 음성 채널에 접속해주세요.", ephemeral=True)
                return

            player = self.get_player(interaction.guild, interaction.channel)

            # Reset player states before cleanup
            player.loop = False

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
            if not interaction.user.voice:
                await interaction.response.send_message("먼저 음성 채널에 접속해주세요.", ephemeral=True)
                return

            player = self.get_player(interaction.guild, interaction.channel)
            if not player.voice_client or not player.voice_client.is_playing():
                await interaction.response.send_message("현재 재생 중인 곡이 없습니다.", ephemeral=True)
                return

            player.voice_client.stop()
            await interaction.response.send_message("현재 곡을 건너뜁니다.", ephemeral=True)
            logger.debug("스킵 명령어 실행")
        except Exception as e:
            logger.exception(f"스킵 명령어 오류: {e}")
            await handle_command_error(interaction, e, "스킵 명령어 오류")

    @song.command(name="대기열", description="현재 대기열을 보여줍니다.")
    async def queue(self, interaction: Interaction):
        try:
            player = self.get_player(interaction.guild, interaction.channel)
            if not player.queue and not player.current:
                await interaction.response.send_message("대기열이 비어있습니다.", ephemeral=True)
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
                song_entry = await self.cache_manager.get(song_id)
                if song_entry:
                    duration = format_duration(song_entry.duration)
                    embed.add_field(
                        name=f"{idx}번 곡",
                        value=f"**{song_entry.title}**\n⏱️ {duration}",
                        inline=False
                    )

            await interaction.response.send_message(embed=embed, ephemeral=True)
            logger.debug("대기열 명령어 실행")
        except Exception as e:
            logger.exception(f"대기열 명령어 오류: {e}")
            await handle_command_error(interaction, e, "대기열 명령어 오류")

    @song.command(name="셔플", description="대기열의 곡 순서를 무작위로 섞습니다.")
    async def shuffle(self, interaction: Interaction):
        try:
            if not interaction.user.voice:
                await interaction.response.send_message("먼저 음성 채널에 접속해주세요.", ephemeral=True)
                return

            player = self.get_player(interaction.guild, interaction.channel)
            if not player.queue:
                await interaction.response.send_message("대기열이 비어있습니다.", ephemeral=True)
                return

            random.shuffle(player.queue)
            await interaction.response.send_message("대기열을 섞었습니다.", ephemeral=True)
            await player.update_now_playing()
            logger.debug("셔플 명령어 실행")
        except Exception as e:
            logger.exception(f"셔플 명령어 오류: {e}")
            await handle_command_error(interaction, e, "셔플 명령어 오류")

    @song.command(name="볼륨", description="재생 볼륨을 조절합니다. (1-200)")
    async def volume(self, interaction: Interaction, level: app_commands.Range[int, 1, 200]):
        try:
            if not interaction.user.voice:
                await interaction.response.send_message("먼저 음성 채널에 접속해주세요.", ephemeral=True)
                return

            player = self.get_player(interaction.guild, interaction.channel)
            if not player.voice_client or not player.voice_client.source:
                await interaction.response.send_message("현재 재생 중인 곡이 없습니다.", ephemeral=True)
                return

            volume = level / 100
            player.voice_client.source.volume = volume
            player._volume = volume
            await interaction.response.send_message(f"볼륨을 {level}%로 설정했습니다.", ephemeral=True)
            await player.update_now_playing()
            logger.debug(f"볼륨 조절: {level}%")
        except Exception as e:
            logger.exception(f"볼륨 명령어 오류: {e}")
            await handle_command_error(interaction, e, "볼륨 명령어 오류")

    @song.command(name="반복", description="현재 곡 반복을 설정/해제합니다.")
    async def toggle_loop(self, interaction: Interaction):
        try:
            if not interaction.user.voice:
                await interaction.response.send_message("먼저 음성 채널에 접속해주세요.", ephemeral=True)
                return

            player = self.get_player(interaction.guild, interaction.channel)
            if not player.current and not player.queue:
                await interaction.response.send_message("재생 중인 곡이 없어 반복 모드를 설정할 수 없습니다.", ephemeral=True)
                return

            player.loop = not player.loop
            status = "활성화" if player.loop else "비활성화"
            await interaction.response.send_message(f"반복 모드를 {status}했습니다.", ephemeral=True)
            await player.update_now_playing()
            logger.debug(f"반복 모드 {status}")
        except Exception as e:
            logger.exception(f"반복 모드 명령어 오류: {e}")
            await handle_command_error(interaction, e, "반복 모드 명령어 오류")

async def setup(bot: commands.Bot, reloaded: bool = False):
    await bot.add_cog(MusicCog(bot))
    logger.info("MusicCog loaded successfully.")
