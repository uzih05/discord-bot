# cogs/__init__.py

from .memory_cog import MemoryCog
from .poll_cog import PollCog
from .news_cog import NewsCog
from .tts_cog import TTSCog
from .music_cog import MusicCog
from .moderation_cog import ModerationCog

__all__ = ["MemoryCog", "PollCog", "NewsCog", "TTSCog", "MusicCog", "ModerationCog"]
