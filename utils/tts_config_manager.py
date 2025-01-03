# utils/tts_config_manager.py

import os
import json
import aiofiles
import asyncio
import logging

logger = logging.getLogger(__name__)

class TTSConfigManager:
    def __init__(self, config_path: str):
        """
        config_path: TTS 설정을 저장할 JSON 파일 경로
        """
        self.config_path = config_path
        self._data = {}
        self._lock = asyncio.Lock()  # 동시 접근 방지를 위한 Lock

    async def load_config(self):
        """JSON 파일에서 설정을 로드하여 self._data에 저장"""
        if not os.path.exists(self.config_path):
            logger.info(f"TTS config file이 존재하지 않아 생성 예정: {self.config_path}")
            self._data = {}
            return

        try:
            async with aiofiles.open(self.config_path, mode='r', encoding='utf-8') as f:
                content = await f.read()
                if content.strip():
                    self._data = json.loads(content)
                else:
                    self._data = {}
            logger.info(f"TTS config 불러오기 완료: {self.config_path}")
        except Exception as e:
            logger.error(f"TTS config 로드 중 오류 발생: {e}")
            self._data = {}

    async def save_config(self):
        """self._data 내용을 JSON 파일에 저장"""
        try:
            async with self._lock:
                async with aiofiles.open(self.config_path, mode='w', encoding='utf-8') as f:
                    await f.write(json.dumps(self._data, ensure_ascii=False, indent=2))
            logger.debug(f"TTS config 저장 완료: {self.config_path}")
        except Exception as e:
            logger.error(f"TTS config 저장 중 오류 발생: {e}")

    def get_text_channel_id(self, guild_id: int) -> int:
        """
        guild_id에 해당하는 text_channel_id를 반환.
        설정이 없으면 None
        """
        guild_key = str(guild_id)
        if guild_key in self._data:
            return self._data[guild_key].get("text_channel_id")
        return None

    async def set_text_channel_id(self, guild_id: int, channel_id: int):
        """
        guild_id의 text_channel_id를 설정 후 JSON에 저장
        """
        guild_key = str(guild_id)
        if guild_key not in self._data:
            self._data[guild_key] = {}
        self._data[guild_key]["text_channel_id"] = channel_id
        await self.save_config()
        logger.debug(f"TTS 채널 설정: guild={guild_id}, channel={channel_id}")
