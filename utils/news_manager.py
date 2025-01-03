# utils/news_manager.py

import json
import os
import aiofiles
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

class NewsManager:
    def __init__(self, news_dir: str):
        self.news_dir = news_dir
        os.makedirs(self.news_dir, exist_ok=True)
        logger.info(f"NewsManager 초기화 완료: 뉴스 데이터 디렉토리={self.news_dir}")

    async def load_news_data(self, guild_id: str) -> Dict[str, Any]:
        """특정 guild_id에 대한 뉴스 데이터를 불러옵니다."""
        file_path = os.path.join(self.news_dir, f"{guild_id}.json")
        if not os.path.exists(file_path):
            logger.info(f"뉴스 데이터 파일이 존재하지 않아 새로 생성합니다: {file_path}")
            return {"channels": {}}
        try:
            async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
                content = await f.read()
                data = json.loads(content)
            logger.debug(f"뉴스 데이터 로드 완료: {file_path}")
            return data
        except Exception as e:
            logger.error(f"뉴스 데이터 로드 실패: {file_path}, 오류: {e}", exc_info=True)
            return {"channels": {}}

    async def save_news_data(self, guild_id: str, data: Dict[str, Any]) -> bool:
        """특정 guild_id에 대한 뉴스 데이터를 저장합니다."""
        file_path = os.path.join(self.news_dir, f"{guild_id}.json")
        try:
            async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(data, indent=4, ensure_ascii=False))
            logger.debug(f"뉴스 데이터 저장 완료: {file_path}")
            return True
        except Exception as e:
            logger.error(f"뉴스 데이터 저장 실패: {file_path}, 오류: {e}", exc_info=True)
            return False

    async def load_all_news_data(self) -> Dict[str, Dict[str, Any]]:
        """모든 guild_id에 대한 뉴스 데이터를 불러옵니다."""
        news_data = {}
        try:
            for filename in os.listdir(self.news_dir):
                if filename.endswith('.json'):
                    guild_id = filename[:-5]
                    data = await self.load_news_data(guild_id)
                    news_data[guild_id] = data
            logger.info(f"모든 뉴스 데이터 로드 완료: 총 {len(news_data)}개 서버")
            return news_data
        except Exception as e:
            logger.error(f"모든 뉴스 데이터 로드 중 오류 발생: {e}", exc_info=True)
            return news_data

    async def delete_news_data(self, guild_id: str):
        """특정 guild_id에 대한 뉴스 데이터를 삭제합니다."""
        file_path = os.path.join(self.news_dir, f"{guild_id}.json")
        try:
            if os.path.exists(file_path):
                await aiofiles.os.remove(file_path)
                logger.debug(f"뉴스 데이터 삭제 완료: {file_path}")
            else:
                logger.warning(f"삭제하려는 뉴스 데이터 파일이 존재하지 않음: {file_path}")
        except Exception as e:
            logger.error(f"뉴스 데이터 삭제 실패: {file_path}, 오류: {e}", exc_info=True)
