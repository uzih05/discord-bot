"""
Discord Music Bot - Single File Implementation
This module contains all components of the music bot organized into logical sections.
Part 1/3: Core Components and Models
"""

import os
import time
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
import logging
from typing import Optional, Dict, List, Tuple, Set
import shutil
from urllib.parse import urlparse
import re
from dataclasses import dataclass

# =============================================================================
# Logging Setup
# =============================================================================

logger = logging.getLogger(__name__)

# =============================================================================
# Core Models
# =============================================================================

class Song:
    """Represents a song in the queue"""

    def __init__(self, source: dict, requester: discord.Member):
        self.source = source
        self.requester = requester
        self.title = source.get('title', 'Unknown title')
        self.thumbnail = source.get('thumbnail', '')
        self.duration = source.get('duration', 0)
        self.filename = source.get('filename', '')
        self.preloaded = False  # Add this line
        self.added_at = time.time()  # Add this line

    @property
    def age(self) -> float:
        return time.time() - self.added_at

class MusicQueue:
    """Manages the music queue and playback state"""
    def __init__(self):
        self.queue: List[Song] = []
        self._volume = 0.05  # Default volume 5%
        self.current: Optional[Song] = None
        self.now_playing_message: Optional[discord.Message] = None
        self.text_channel: Optional[discord.TextChannel] = None
        self.preloaded_song: Optional[Song] = None
        self.start_time: Optional[float] = None
        self.loop_mode = 'none'  # none, song, queue
        self.last_progress_update = 0

    @property
    def volume(self) -> float:
        return self._volume

    @volume.setter
    def volume(self, value: float):
        self._volume = min(max(value, 0.0), 1.0)

    def clear(self):
        """Clear the queue and reset state"""
        self.queue.clear()
        self.current = None
        self.preloaded_song = None
        self.start_time = None

    def get_song_progress(self) -> float:
        """Get current song progress in seconds"""
        if not self.start_time or not self.current:
            return 0
        return time.time() - self.start_time

    def toggle_loop_mode(self) -> str:
        """Toggle between loop modes"""
        modes = {'none': 'song', 'song': 'queue', 'queue': 'none'}
        self.loop_mode = modes[self.loop_mode]
        return self.loop_mode

    def shuffle(self):
        """Shuffle the queue"""
        import random
        random.shuffle(self.queue)
        self.preloaded_song = None

class SongCache:
    """Manages caching of downloaded songs"""
    def __init__(self, max_size: int = 10, max_age: int = 3600):
        self.cache: Dict[str, Tuple[str, float]] = {}
        self.max_size = max_size
        self.max_age = max_age

    def get(self, video_id: str) -> Optional[str]:
        """Get cached filename for video ID"""
        if video_id in self.cache:
            filename, _ = self.cache[video_id]
            self.cache[video_id] = (filename, time.time())
            return filename
        return None

    def add(self, video_id: str, filename: str):
        """Add a file to cache"""
        if len(self.cache) >= self.max_size:
            oldest = min(self.cache.items(), key=lambda x: x[1][1])
            del self.cache[oldest[0]]
        self.cache[video_id] = (filename, time.time())

    def cleanup(self):
        """Remove expired cache entries"""
        current_time = time.time()
        expired = []
        for video_id, (filename, timestamp) in self.cache.items():
            if current_time - timestamp > self.max_age:
                expired.append(video_id)
                try:
                    if os.path.exists(filename):
                        os.remove(filename)
                except Exception as e:
                    logger.error(f"Error removing expired cache file {filename}: {e}")

        for video_id in expired:
            del self.cache[video_id]

class MusicBotError(Exception):
    """Base exception for music bot"""
    pass

class DownloadError(MusicBotError):
    """Download related errors"""
    pass

class ResourceLimitError(MusicBotError):
    """Resource limit exceeded"""
    pass


@dataclass
class ResourceLimits:
    max_queue_size: int = 100
    max_song_duration: int = 3600  # 1 hour
    max_total_duration: int = 18000  # 5 hours
    max_cached_files: int = 50
    cache_duration: int = 3600  # 1 hour


class SecurityManager:
    """Manages security aspects of the bot"""

    def __init__(self):
        self.url_whitelist = [
            'youtube.com',
            'youtu.be',
            'soundcloud.com'
        ]
        self.command_rate_limiter = RateLimiter(calls=5, period=60)

    def validate_url(self, url: str) -> bool:
        """Validate URL against whitelist"""
        try:
            parsed = urlparse(url)
            return any(domain in parsed.netloc for domain in self.url_whitelist)
        except Exception:
            return False

    def sanitize_query(self, query: str) -> str:
        """Sanitize search query"""
        sanitized = re.sub(r'[;&|]', '', query)
        return sanitized[:200]  # Limit query length


class RateLimiter:
    """Rate limiting implementation"""

    def __init__(self, calls: int, period: float):
        self.calls = calls
        self.period = period
        self.timestamps: Dict[int, List[float]] = {}

    async def acquire(self, user_id: int) -> bool:
        now = time.time()
        if user_id not in self.timestamps:
            self.timestamps[user_id] = []

        # Clean old timestamps
        self.timestamps[user_id] = [ts for ts in self.timestamps[user_id]
                                    if now - ts <= self.period]

        if len(self.timestamps[user_id]) >= self.calls:
            return False

        self.timestamps[user_id].append(now)
        return True


class SongPreloader:
    """Handles preloading of upcoming songs"""

    def __init__(self, max_preload: int = 2):
        self.max_preload = max_preload
        self.preload_queue = asyncio.Queue()
        self.current_tasks: Set[asyncio.Task] = set()

    async def preload_songs(self, queue: List[Song]):
        """Preload multiple songs asynchronously"""
        for song in queue[:self.max_preload]:
            if not hasattr(song, 'preloaded') or not song.preloaded:
                task = asyncio.create_task(self._preload_song(song))
                self.current_tasks.add(task)
                task.add_done_callback(self.current_tasks.discard)

    async def _preload_song(self, song: Song):
        """Preload a single song"""
        try:
            song.preloaded = True
        except Exception as e:
            logger.error(f"Error preloading song {song.title}: {e}")


async def download_with_retry(url: str, ydl_opts: dict, max_retries: int = 3) -> dict:
    """Download with retry logic for transient failures"""
    for attempt in range(max_retries):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: ydl.extract_info(url, download=False)
                )
        except Exception as e:
            if attempt == max_retries - 1:
                raise DownloadError(f"Failed after {max_retries} attempts: {str(e)}")
            await asyncio.sleep(1.5 ** attempt)  # Exponential backoff

# =============================================================================
# UI Components - Views
# =============================================================================

class SongSelectView(discord.ui.View):
    """View for song selection interface"""
    def __init__(self, entries, timeout=60):
        super().__init__(timeout=timeout)
        self.selected_entry = None
        self.message = None
        self.entries = entries
        self.current_page = 0
        self.items_per_page = 5

        # Add selection buttons
        for i in range(min(5, len(entries))):
            button = discord.ui.Button(
                label=str(i + 1),
                style=discord.ButtonStyle.primary,
                custom_id=f"select_{i}"
            )
            button.callback = self.create_callback(entries[i])
            self.add_item(button)

        # Add cancel button
        cancel_button = discord.ui.Button(
            label="취소",
            style=discord.ButtonStyle.danger,
            custom_id="cancel"
        )
        cancel_button.callback = self.cancel_callback
        self.add_item(cancel_button)

    def create_callback(self, entry):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.message.interaction.user.id:
                await interaction.response.send_message("다른 사용자의 선택창입니다!", ephemeral=True)
                return

            self.selected_entry = entry
            self.stop()

            embed = discord.Embed(
                title="🎵 노래 선택 완료",
                description=f"선택한 노래: **{entry.get('title', 'Unknown')}**\n재생 준비중...",
                color=int('f9e54b', 16)
            )

            duration = entry.get('duration')
            if duration:
                minutes = int(duration) // 60
                seconds = int(duration) % 60
                embed.add_field(name="길이", value=f"{minutes:02d}:{seconds:02d}")

            await interaction.response.edit_message(embed=embed, view=None)
            await asyncio.sleep(3)
            try:
                await self.message.delete()
            except:
                pass

        return callback

    async def cancel_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.message.interaction.user.id:
            await interaction.response.send_message("다른 사용자의 선택창입니다!", ephemeral=True)
            return

        embed = discord.Embed(
            title="❌ 검색 취소됨",
            description="노래 선택이 취소되었습니다.",
            color=discord.Color.red()
        )
        await interaction.response.edit_message(embed=embed, view=None)
        await asyncio.sleep(3)
        try:
            await self.message.delete()
        except:
            pass
        self.stop()

    async def on_timeout(self):
        try:
            embed = discord.Embed(
                title="⏰ 시간 초과",
                description="60초 내에 선택하지 않아 검색이 취소되었습니다.",
                color=discord.Color.orange()
            )
            await self.message.edit(embed=embed, view=None)
            await asyncio.sleep(3)
            await self.message.delete()
        except:
            pass

# =============================================================================
# UI Components - Player Controls
# =============================================================================

class PlayerControlsView(discord.ui.View):
    """View for music player controls"""
    def __init__(self, cog: 'MusicCog', timeout: int = None):
        super().__init__(timeout=timeout)
        self.cog = cog

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle previous track button"""
        await interaction.response.defer()
        if interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
            interaction.guild.voice_client.stop()

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.primary)
    async def play_pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle play/pause button"""
        await interaction.response.defer()
        vc = interaction.guild.voice_client
        if not vc:
            return

        if vc.is_paused():
            vc.resume()
            await interaction.followup.send("▶️ 다시 재생합니다.", ephemeral=True)
        else:
            vc.pause()
            await interaction.followup.send("⏸️ 일시정지되었습니다.", ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle skip button"""
        await interaction.response.defer()
        if interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
            interaction.guild.voice_client.stop()
            await interaction.followup.send("⏭️ 노래를 건너뛰었습니다.", ephemeral=True)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary)
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle loop mode button"""
        await interaction.response.defer()
        queue = self.cog.get_queue(interaction.guild.id)
        mode = queue.toggle_loop_mode()

        modes = {'none': '없음', 'song': '한곡', 'queue': '전체'}
        await interaction.followup.send(f"🔁 반복 모드를 '{modes[mode]}'으로 설정했습니다.", ephemeral=True)

        embed = queue.now_playing_message.embeds[0]
        loop_modes = {'none': '', 'song': ' | 🔂 한곡 반복', 'queue': ' | 🔁 전체 반복'}
        embed.set_footer(text=f"요청자: {queue.current.requester.display_name}{loop_modes[mode]}")
        await queue.now_playing_message.edit(embed=embed)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary)
    async def shuffle_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle shuffle button"""
        await interaction.response.defer()
        queue = self.cog.get_queue(interaction.guild.id)
        if len(queue.queue) >= 2:
            queue.shuffle()
            await interaction.followup.send("🔀 대기열을 섞었습니다.", ephemeral=True)
        else:
            await interaction.followup.send("셔플할 노래가 충분하지 않습니다.", ephemeral=True)

# =============================================================================
# UI Components - Queue Controls
# =============================================================================

class QueueControlsView(discord.ui.View):
    """View for queue management controls"""
    def __init__(self, cog: 'MusicCog', format_duration, timeout: int = None):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.format_duration = format_duration
        self.page = 0
        self.max_items = 10

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle previous page button"""
        await interaction.response.defer()
        if self.page > 0:
            self.page -= 1
            await self.update_queue_message(interaction)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle next page button"""
        await interaction.response.defer()
        queue = self.cog.get_queue(interaction.guild.id)
        if (self.page + 1) * self.max_items < len(queue.queue):
            self.page += 1
            await self.update_queue_message(interaction)

    @discord.ui.button(label="새로고침", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle refresh button"""
        await interaction.response.defer()
        await self.update_queue_message(interaction)

    async def update_queue_message(self, interaction: discord.Interaction):
        """Update the queue display message"""
        queue = self.cog.get_queue(interaction.guild.id)
        embed = discord.Embed(title="🎵 재생 대기열", color=discord.Color.blue())

        if queue.current:
            progress = queue.get_song_progress()
            duration = queue.current.duration or 0
            time_info = (
                f"\n⏰ {self.format_duration(int(progress))}/{self.format_duration(duration)}"
                if duration else ""
            )
            embed.add_field(
                name="현재 재생 중",
                value=f"**{queue.current.title}** (요청: {queue.current.requester.display_name}){time_info}",
                inline=False
            )

        if queue.queue:
            start_idx = self.page * self.max_items
            end_idx = min(start_idx + self.max_items, len(queue.queue))
            queue_slice = queue.queue[start_idx:end_idx]

            accumulated_time = queue.current.duration - queue.get_song_progress() if queue.current else 0
            for i in range(start_idx):
                accumulated_time += queue.queue[i].duration or 0

            description = []
            for i, song in enumerate(queue_slice, start=start_idx + 1):
                time_info = f"⏰ 예상 대기시간: {self.cog.format_duration(int(accumulated_time))}"
                description.append(
                    f"{i}. **{song.title}** (요청: {song.requester.display_name})\n   {time_info}"
                )
                accumulated_time += song.duration or 0

            embed.add_field(
                name=f"대기 중인 노래 (총 {len(queue.queue)}곡)",
                value="\n".join(description) if description else "없음",
                inline=False
            )

            embed.set_footer(text=f"페이지 {self.page + 1}/{(len(queue.queue) - 1) // self.max_items + 1} | "
                                f"총 재생시간: {self.format_duration(self.cog.get_queue_duration(queue))}")

        await interaction.message.edit(embed=embed, view=self)

# =============================================================================
# Main Music Cog
# =============================================================================

class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.queues: Dict[int, MusicQueue] = {}
        self.base_music_dir = 'cogs_data/music_cog'
        self.song_cache = SongCache(max_size=10)
        self.security = SecurityManager()
        self.resource_limits = ResourceLimits()
        self.preloader = SongPreloader()
        self.rate_limiter = RateLimiter(calls=5, period=60)

        # Create cleanup tasks
        self.cache_cleanup_task = self.bot.loop.create_task(self.periodic_cache_cleanup())
        self.directory_cleanup_task = self.bot.loop.create_task(self.periodic_directory_cleanup())

        # Create necessary directories
        os.makedirs(self.base_music_dir, exist_ok=True)

        # YouTube download options
        self.ydl_opts = {
            'format': 'bestaudio/best',
            'restrictfilenames': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'retries': 10,
            'socket_timeout': 15,
        }

        self.search_opts = {
            'format': 'bestaudio/best',
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'extract_flat': 'in_playlist',
            'skip_download': True,
            'default_search': 'ytsearch',
        }

    def get_guild_directory(self, guild_id: int) -> str:
        """Get guild-specific directory path"""
        guild_dir = os.path.join(self.base_music_dir, str(guild_id))
        os.makedirs(guild_dir, exist_ok=True)
        return guild_dir

    async def cleanup_guild_directory(self, guild_id: int):
        """Clean up guild-specific directory"""
        guild_dir = self.get_guild_directory(guild_id)
        try:
            # Remove all files in the guild directory
            for filename in os.listdir(guild_dir):
                file_path = os.path.join(guild_dir, filename)
                try:
                    if os.path.isfile(file_path):
                        os.remove(file_path)
                except Exception as e:
                    logger.error(f"Error removing file {file_path}: {e}")

            # Remove the guild directory itself
            os.rmdir(guild_dir)
        except Exception as e:
            logger.error(f"Error cleaning up guild directory: {e}")

    async def periodic_directory_cleanup(self):
        """Periodically clean up empty guild directories"""
        while not self.bot.is_closed():
            try:
                for guild_folder in os.listdir(self.base_music_dir):
                    guild_path = os.path.join(self.base_music_dir, guild_folder)
                    if os.path.isdir(guild_path):
                        # Check if directory is empty and guild is not active
                        if not os.listdir(guild_path) and int(guild_folder) not in self.queues:
                            try:
                                os.rmdir(guild_path)
                                logger.info(f"Removed empty guild directory: {guild_folder}")
                            except Exception as e:
                                logger.error(f"Error removing empty guild directory {guild_folder}: {e}")

                await asyncio.sleep(3600)  # Check every hour
            except Exception as e:
                logger.error(f"Error in directory cleanup: {e}")
                await asyncio.sleep(60)

    def get_queue(self, guild_id: int) -> MusicQueue:
        """Get or create a queue for a guild"""
        if guild_id not in self.queues:
            self.queues[guild_id] = MusicQueue()
        return self.queues[guild_id]

    async def cleanup_files(self, guild_id: int):
        queue = self.queues.pop(guild_id, None)
        if not queue:
            return

        # Cancel progress task if exists
        if hasattr(queue, 'progress_task') and queue.progress_task:
            queue.progress_task.cancel()
            try:
                await queue.progress_task
            except asyncio.CancelledError:
                pass

        # Delete now playing message
        if queue.now_playing_message:
            try:
                await queue.now_playing_message.delete()
            except Exception as e:
                logger.error(f"Error removing now playing message: {e}")

        queue.clear()

        # Clean up guild directory if needed
        if not self.bot.get_guild(guild_id):  # If guild no longer exists
            await self.cleanup_guild_directory(guild_id)

    async def preload_next_song(self, guild_id: int):
        """Preload the next song in queue"""
        queue = self.get_queue(guild_id)
        if not queue.queue or queue.preloaded_song:
            return

        next_song = queue.queue[0]
        try:
            video_id = next_song.source.get('id')
            if video_id:
                cached_file = self.song_cache.get(video_id)
                if cached_file and os.path.exists(cached_file):
                    next_song.filename = cached_file
                    queue.preloaded_song = next_song
                    return

            guild_dir = self.get_guild_directory(guild_id)
            ydl_opts = self.ydl_opts.copy()
            ydl_opts['outtmpl'] = os.path.join(guild_dir, '%(title)s.%(ext)s')

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await self.bot.loop.run_in_executor(
                    None,
                    lambda: ydl.extract_info(next_song.source['webpage_url'], download=True)
                )
                filename = ydl.prepare_filename(info).replace('.webm', '.mp3').replace('.m4a', '.mp3')
                next_song.filename = filename
                queue.preloaded_song = next_song

                if video_id:
                    self.song_cache.add(video_id, filename)
        except Exception as e:
            logger.error(f"Error preloading next song: {e}")

    def format_duration(self, seconds: float) -> str:
        """Format duration in seconds to string"""
        if not seconds:
            return "00:00"

        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        seconds = int(seconds % 60)

        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def get_queue_duration(self, queue: MusicQueue) -> int:
        """Calculate total duration of queue"""
        total = 0
        if queue.current:
            total += max(0, queue.current.duration - queue.get_song_progress())
        for song in queue.queue:
            if song.duration:
                total += song.duration
        return total

    def create_progress_bar(self, progress: float, duration: float, length: int = 20) -> str:
        """Create a text progress bar"""
        filled = int((progress / duration) * length)
        bar = '▓' * filled + '░' * (length - filled)
        timestamp = f"{self.format_duration(int(progress))}/{self.format_duration(int(duration))}"
        return f"`{bar}` {timestamp}"

    async def update_progress_bar(self, message: discord.Message, queue: MusicQueue):
        """Update the progress bar periodically"""
        try:
            while True:
                if not queue.current or not message:
                    return

                if time.time() - queue.last_progress_update < 10:
                    await asyncio.sleep(10)
                    continue

                queue.last_progress_update = time.time()
                progress = queue.get_song_progress()
                duration = queue.current.duration or 0

                if duration > 0:
                    progress_bar = self.create_progress_bar(progress, duration)
                    try:
                        embed = message.embeds[0]
                        embed.description = f"**{queue.current.title}**\n{progress_bar}\n볼륨: {int(queue.volume * 100)}%"
                        await message.edit(embed=embed)
                    except discord.NotFound:
                        return
                    except Exception as e:
                        logger.error(f"Error updating progress bar: {e}")

                await asyncio.sleep(10)
        except asyncio.CancelledError:
            return

    async def periodic_cache_cleanup(self):
        """Run periodic cache cleanup"""
        while not self.bot.is_closed():
            try:
                self.song_cache.cleanup()
                await asyncio.sleep(300)  # Every 5 minutes
            except Exception as e:
                logger.error(f"Error in cache cleanup: {e}")
                await asyncio.sleep(60)

    async def process_song(self, info: dict, requester: discord.Member, ydl_opts: dict) -> Song:
        """Process song info and download"""
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                download_info = await self.bot.loop.run_in_executor(
                    None,
                    lambda: ydl.extract_info(info['webpage_url'], download=True)
                )

                if not download_info:
                    raise DownloadError("Failed to download song info")

                filename = ydl.prepare_filename(download_info).replace('.webm', '.mp3').replace('.m4a', '.mp3')

                if not os.path.exists(filename):
                    raise DownloadError("Downloaded file not found")

                source = {
                    'title': download_info.get('title', 'Unknown Title'),
                    'thumbnail': download_info.get('thumbnail'),
                    'duration': download_info.get('duration'),
                    'filename': filename,
                    'id': download_info.get('id'),
                    'webpage_url': download_info.get('webpage_url'),
                }

                return Song(source, requester)

        except Exception as e:
            logger.error(f"Error processing song: {e}")
            raise DownloadError(f"Failed to process song: {str(e)}")

    # =============================================================================
    # Command Group Setup
    # =============================================================================

    music_group = app_commands.Group(name="곡", description="음악 관련 명령어")

    @music_group.command(name="재생", description="노래를 재생합니다")
    async def play(self, interaction: discord.Interaction, query: str):
        """Play a song command implementation"""
        try:
            # Permission and state checks
            if not interaction.guild:
                await interaction.response.send_message("서버에서만 사용 가능한 명령어입니다.", ephemeral=True)
                return

            if not interaction.guild.voice_client and not interaction.guild.me.guild_permissions.connect:
                await interaction.response.send_message("음성 채널 연결 권한이 없습니다.", ephemeral=True)
                return

            if not interaction.user.voice:
                await interaction.response.send_message("음성 채널에 먼저 입장해주세요.", ephemeral=True)
                return

            # Rate limit check
            if not await self.rate_limiter.acquire(interaction.user.id):
                await interaction.response.send_message(
                    "명령어 사용 제한에 걸렸습니다. 잠시 후 다시 시도해주세요.",
                    ephemeral=True
                )
                return

            # URL validation and sanitization
            if query.startswith(('http://', 'https://')):
                if not self.security.validate_url(query):
                    await interaction.response.send_message(
                        "지원하지 않는 URL입니다.",
                        ephemeral=True
                    )
                    return
            else:
                query = self.security.sanitize_query(query)

            await interaction.response.defer()

            try:
                if not query.startswith(('https://', 'http://')):
                    # Search functionality with retry
                    with yt_dlp.YoutubeDL(self.search_opts) as ydl:
                        search_term = f"ytsearch5:{query}"
                        info = await download_with_retry(search_term, self.search_opts)

                        if not info or 'entries' not in info:
                            await interaction.followup.send("검색 결과를 찾을 수 없습니다.", ephemeral=True)
                            return

                        entries = info.get('entries', [])[:5]
                        if not entries:
                            await interaction.followup.send("검색 결과를 찾을 수 없습니다.", ephemeral=True)
                            return

                        view = SongSelectView(entries)
                        embed = discord.Embed(
                            title="🎵 노래 선택",
                            description="\n".join(f"{i + 1}. {entry['title']}" for i, entry in enumerate(entries))
                        )
                        embed.set_footer(text="60초 내에 선택해주세요")

                        msg = await interaction.followup.send(embed=embed, view=view)
                        view.message = msg
                        await view.wait()

                        if not view.selected_entry:
                            return

                        info = view.selected_entry
                else:
                    # Direct URL with retry
                    info = await download_with_retry(query, self.ydl_opts)

                # Resource limit checks
                if info.get('duration', 0) > self.resource_limits.max_song_duration:
                    await interaction.followup.send("노래 길이가 제한을 초과합니다.", ephemeral=True)
                    return

                queue = self.get_queue(interaction.guild.id)
                if len(queue.queue) >= self.resource_limits.max_queue_size:
                    await interaction.followup.send("대기열이 가득 찼습니다.", ephemeral=True)
                    return

                # Download and process
                guild_dir = self.get_guild_directory(interaction.guild.id)
                ydl_opts = self.ydl_opts.copy()
                ydl_opts['outtmpl'] = os.path.join(guild_dir, '%(title)s.%(ext)s')

                song = await self.process_song(info, interaction.user, ydl_opts)
                queue.queue.append(song)
                queue.text_channel = interaction.channel

                # Connect and play
                if not interaction.guild.voice_client:
                    await interaction.user.voice.channel.connect()
                    await self.play_next(interaction.guild, interaction.channel)
                elif not interaction.guild.voice_client.is_playing():
                    await self.play_next(interaction.guild, interaction.channel)

                await interaction.followup.send(
                    f"🎵 **{song.title}** 를 재생목록에 추가했습니다.",
                    ephemeral=True
                )

                # Start preloading next songs
                await self.preloader.preload_songs(queue.queue)

            except ResourceLimitError as e:
                await interaction.followup.send(f"제한 초과: {str(e)}", ephemeral=True)
            except DownloadError as e:
                await interaction.followup.send(f"다운로드 실패: {str(e)}", ephemeral=True)
            except Exception as e:
                logger.error(f"Error processing song: {str(e)}", exc_info=True)
                await interaction.followup.send(f"노래 처리 중 오류가 발생했습니다: {str(e)}", ephemeral=True)

        except Exception as e:
            logger.error(f"Critical error in play command: {str(e)}", exc_info=True)
            await interaction.followup.send(f"명령어 처리 중 오류가 발생했습니다: {str(e)}", ephemeral=True)

    async def play_next(self, guild: discord.Guild, text_channel: Optional[discord.TextChannel] = None):
        if not guild.voice_client:
            return

        queue = self.get_queue(guild.id)
        if text_channel:
            queue.text_channel = text_channel

        # Cancel existing progress task if it exists
        if hasattr(queue, 'progress_task') and queue.progress_task:
            queue.progress_task.cancel()
            try:
                await queue.progress_task
            except asyncio.CancelledError:
                pass

        try:
            if queue.now_playing_message:
                try:
                    await queue.now_playing_message.delete()
                except discord.NotFound:
                    pass
                except Exception as e:
                    logger.error(f"Error deleting now playing message: {e}")
                queue.now_playing_message = None

            if queue.current:
                if queue.loop_mode == 'song':
                    queue.queue.insert(0, queue.current)
                elif queue.loop_mode == 'queue':
                    queue.queue.append(queue.current)

            if not queue.queue:
                await self.cleanup_files(guild.id)
                await guild.voice_client.disconnect()
                return

            queue.current = queue.queue.pop(0)
            queue.start_time = time.time()
            queue.last_progress_update = time.time()

            def after_playing(error):
                if error:
                    logger.error(f"Error playing song: {error}")

                async def cleanup():
                    await asyncio.sleep(1)
                    try:
                        if queue.current and os.path.exists(queue.current.filename):
                            for attempt in range(3):
                                try:
                                    os.remove(queue.current.filename)
                                    logger.info(f"Removed finished song file: {queue.current.filename}")
                                    break
                                except Exception:
                                    if attempt < 2:
                                        await asyncio.sleep(1)
                    except Exception as e:
                        logger.error(f"Error removing finished song file: {e}")

                asyncio.run_coroutine_threadsafe(cleanup(), self.bot.loop)
                asyncio.run_coroutine_threadsafe(self.play_next(guild), self.bot.loop)

            try:
                ffmpeg_options = {
                    'options': '-vn',
                    'executable': r'C:\Users\luvwl\ffmpeg\bin\ffmpeg.exe'
                }

                logger.info(f"Playing file: {queue.current.filename}")

                source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(
                        queue.current.filename,
                        **ffmpeg_options
                    ),
                    volume=queue.volume
                )

                guild.voice_client.play(source, after=after_playing)

                progress_bar = ""
                if queue.current.duration:
                    progress_bar = f"\n{self.create_progress_bar(0, queue.current.duration)}"

                embed = discord.Embed(
                    title="🎵 현재 재생 중",
                    description=f"**{queue.current.title}**{progress_bar}\n볼륨: {int(queue.volume * 100)}%",
                    color=discord.Color.blue()
                )
                if queue.current.thumbnail:
                    embed.set_thumbnail(url=queue.current.thumbnail)

                loop_modes = {'none': '', 'song': ' | 🔂 한곡 반복', 'queue': ' | 🔁 전체 반복'}
                embed.set_footer(text=f"요청자: {queue.current.requester.display_name}{loop_modes[queue.loop_mode]}")

                channel_to_use = queue.text_channel or guild.text_channels[0]
                view = PlayerControlsView(self)
                queue.now_playing_message = await channel_to_use.send(embed=embed, view=view)

                if queue.current.duration:
                    queue.progress_task = self.bot.loop.create_task(
                        self.update_progress_bar(queue.now_playing_message, queue)
                    )

                await self.preload_next_song(guild.id)

            except Exception as e:
                logger.error(f"Error setting up playback: {e}")
                await asyncio.sleep(1)
                await self.play_next(guild)

        except Exception as e:
            logger.error(f"Critical error in play_next: {e}")
            try:
                await self.cleanup_files(guild.id)
                if guild.voice_client:
                    await guild.voice_client.disconnect()
            except Exception as cleanup_error:
                logger.error(f"Error during cleanup after critical error: {cleanup_error}")

    @music_group.command(name="스킵", description="현재 재생 중인 노래를 건너뜁니다")
    async def skip(self, interaction: discord.Interaction):
        if not interaction.guild.voice_client or not interaction.guild.voice_client.is_playing():
            await interaction.response.send_message("현재 재생 중인 노래가 없습니다.", ephemeral=True)
            return

        queue = self.get_queue(interaction.guild.id)
        current_song = queue.current

        queue.text_channel = interaction.channel
        interaction.guild.voice_client.stop()

        if current_song:
            try:
                if os.path.exists(current_song.filename):
                    os.remove(current_song.filename)
                    logger.info(f"Removed skipped song file: {current_song.filename}")
            except Exception as e:
                logger.error(f"Error removing skipped song file: {e}")

        await interaction.response.send_message("⏭️ 노래를 건너뛰었습니다.", ephemeral=True)

    @music_group.command(name="정지", description="재생을 멈추고 대기열을 초기화합니다")
    async def stop(self, interaction: discord.Interaction):
        if not interaction.guild.voice_client:
            await interaction.response.send_message("봇이 음성 채널에 없습니다.", ephemeral=True)
            return

        try:
            await interaction.response.send_message("⏹️ 재생을 멈추고 대기열을 초기화했습니다.", ephemeral=True)

            if interaction.guild.voice_client.is_playing():
                interaction.guild.voice_client.stop()

            await self.cleanup_files(interaction.guild.id)
            await interaction.guild.voice_client.disconnect()

        except Exception as e:
            logger.error(f"Error in stop command: {e}")

    @music_group.command(name="일시정지", description="현재 재생 중인 노래를 일시정지합니다")
    async def pause(self, interaction: discord.Interaction):
        if not interaction.guild.voice_client:
            await interaction.response.send_message("봇이 음성 채널에 없습니다.", ephemeral=True)
            return

        if not interaction.guild.voice_client.is_playing():
            await interaction.response.send_message("현재 재생 중인 노래가 없습니다.", ephemeral=True)
            return

        if interaction.guild.voice_client.is_paused():
            await interaction.response.send_message("이미 일시정지되어 있습니다.", ephemeral=True)
            return

        interaction.guild.voice_client.pause()
        await interaction.response.send_message("⏸️ 일시정지되었습니다.", ephemeral=True)

    @music_group.command(name="다시재생", description="일시정지된 노래를 다시 재생합니다")
    async def resume(self, interaction: discord.Interaction):
        if not interaction.guild.voice_client:
            await interaction.response.send_message("봇이 음성 채널에 없습니다.", ephemeral=True)
            return

        if not interaction.guild.voice_client.is_paused():
            await interaction.response.send_message("일시정지된 노래가 없습니다.", ephemeral=True)
            return

        interaction.guild.voice_client.resume()
        await interaction.response.send_message("▶️ 다시 재생합니다.", ephemeral=True)

    @music_group.command(name="볼륨", description="볼륨을 조절합니다 (1-10, 기본값: 5)")
    async def volume(self, interaction: discord.Interaction, volume: app_commands.Range[int, 1, 10]):
        queue = self.get_queue(interaction.guild.id)
        queue.volume = volume / 10.0  # Convert to a percentage (0.0 to 1.0)

        if interaction.guild.voice_client and interaction.guild.voice_client.source:
            interaction.guild.voice_client.source.volume = queue.volume

        await interaction.response.send_message(f"🔊 볼륨을 {int(queue.volume * 10)}로 설정했습니다.", ephemeral=True)

        if queue.now_playing_message:
            try:
                embed = queue.now_playing_message.embeds[0]
                progress_bar = self.create_progress_bar(queue.get_song_progress(), queue.current.duration)
                embed.description = f"**{queue.current.title}**\n{progress_bar}\n볼륨: {int(queue.volume * 10)}"
                await queue.now_playing_message.edit(embed=embed)
            except Exception as e:
                logger.error(f"Error updating now playing message volume: {e}")

    @music_group.command(name="대기열", description="대기열에 있는 노래 목록을 보여줍니다")
    async def queue(self, interaction: discord.Interaction):
        queue = self.get_queue(interaction.guild.id)

        if not queue.current and not queue.queue:
            await interaction.response.send_message("대기열이 비어있습니다.", ephemeral=True)
            return

        embed = discord.Embed(title="🎵 재생 대기열", color=discord.Color.blue())

        if queue.current:
            progress = queue.get_song_progress()
            duration = queue.current.duration or 0
            time_info = (
                f"\n⏰ {self.format_duration(int(progress))}/{self.format_duration(duration)}"
                if duration else ""
            )
            embed.add_field(
                name="현재 재생 중",
                value=f"**{queue.current.title}** (요청: {queue.current.requester.display_name}){time_info}",
                inline=False
            )

        if queue.queue:
            queue_slice = queue.queue[:10]
            accumulated_time = queue.current.duration - queue.get_song_progress() if queue.current else 0

            description = []
            for i, song in enumerate(queue_slice, start=1):
                time_info = f"⏰ 예상 대기시간: {self.format_duration(int(accumulated_time))}"
                description.append(
                    f"{i}. **{song.title}** (요청: {song.requester.display_name})\n   {time_info}"
                )
                accumulated_time += song.duration or 0

            embed.add_field(
                name=f"대기 중인 노래 (총 {len(queue.queue)}곡)",
                value="\n".join(description),
                inline=False
            )

            total_duration = int(self.get_queue_duration(queue))
            embed.set_footer(text=f"총 재생시간: {self.format_duration(total_duration)}")

        view = QueueControlsView(self, self.format_duration)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @music_group.command(name="삭제", description="대기열에서 특정 노래를 제거합니다")
    async def remove(self, interaction: discord.Interaction, number: int):
        try:
            queue = self.get_queue(interaction.guild.id)

            if not 1 <= number <= len(queue.queue):
                await interaction.response.send_message("올바른 대기열 번호를 입력해주세요.", ephemeral=True)
                return

            removed_song = queue.queue.pop(number - 1)
            try:
                os.remove(removed_song.filename)
            except Exception as e:
                logger.error(f"Error removing song file: {e}")

            await interaction.response.send_message(
                f"🗑️ **{removed_song.title}**를 대기열에서 제거했습니다.",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error in remove command: {e}")
            await interaction.response.send_message("노래 제거 중 오류가 발생했습니다.", ephemeral=True)

    @music_group.command(name="이동", description="대기열에서 노래의 순서를 변경합니다")
    async def move(self, interaction: discord.Interaction, from_pos: int, to_pos: int):
        queue = self.get_queue(interaction.guild.id)

        if not 1 <= from_pos <= len(queue.queue) or not 1 <= to_pos <= len(queue.queue):
            await interaction.response.send_message("올바른 대기열 번호를 입력해주세요.", ephemeral=True)
            return

        song = queue.queue.pop(from_pos - 1)
        queue.queue.insert(to_pos - 1, song)

        await interaction.response.send_message(
            f"🔄 **{song.title}**를 {from_pos}번에서 {to_pos}번으로 이동했습니다.",
            ephemeral=True
        )

        if from_pos == 1 or to_pos == 1:
            queue.preloaded_song = None
            await self.preload_next_song(interaction.guild.id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState,
                                    after: discord.VoiceState):
        if not member.guild.voice_client:
            return

        if len(member.guild.voice_client.channel.members) == 1:  # Only bot remains
            try:
                await self.cleanup_files(member.guild.id)
                await member.guild.voice_client.disconnect()
            except Exception as e:
                logger.error(f"Error in voice state update: {e}")

    @music_group.command(name="반복", description="반복 모드를 설정합니다 (없음/한곡/전체)")
    async def loop(self, interaction: discord.Interaction):
        queue = self.get_queue(interaction.guild.id)
        mode = queue.toggle_loop_mode()
        modes = {'none': '없음', 'song': '한곡', 'queue': '전체'}
        await interaction.response.send_message(f"🔁 반복 모드를 '{modes[mode]}'으로 설정했습니다.", ephemeral=True)

    @music_group.command(name="셔플", description="대기열의 노래를 무작위로 섞습니다")
    async def shuffle(self, interaction: discord.Interaction):
        queue = self.get_queue(interaction.guild.id)
        if len(queue.queue) < 2:
            await interaction.response.send_message("셔플할 노래가 충분하지 않습니다.", ephemeral=True)
            return
        queue.shuffle()
        await interaction.response.send_message("🔀 대기열을 섞었습니다.", ephemeral=True)

    async def periodic_cache_cleanup(self):
        while not self.bot.is_closed():
            try:
                self.song_cache.cleanup()
                await asyncio.sleep(300)  # Run every 5 minutes
            except Exception as e:
                logger.error(f"Error in cache cleanup: {e}")
                await asyncio.sleep(60)

# =============================================================================
# Setup Function
# =============================================================================

async def setup(bot: commands.Bot):
    """Setup function to add the cog to the bot"""
    await bot.add_cog(MusicCog(bot))