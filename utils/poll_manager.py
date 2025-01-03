# utils/poll_manager.py

import json
import os
import aiofiles
import logging

from typing import Optional, Dict

logger = logging.getLogger(__name__)

class PollManager:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        logger.info(f"PollManager 초기화 완료: 데이터 디렉토리={self.data_dir}")

    async def save_poll(self, poll_id: str, data: dict):
        """특정 poll_id에 대한 투표 데이터를 저장합니다."""
        file_path = os.path.join(self.data_dir, f"{poll_id}.json")
        try:
            async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(data, indent=4, ensure_ascii=False))
            logger.debug(f"투표 데이터 저장 완료: {file_path}")
        except Exception as e:
            logger.error(f"투표 데이터 저장 실패: {file_path}, 오류: {e}", exc_info=True)
            raise e

    async def load_poll(self, poll_id: str) -> Optional[dict]:
        """특정 poll_id에 대한 투표 데이터를 불러옵니다."""
        file_path = os.path.join(self.data_dir, f"{poll_id}.json")
        if not os.path.exists(file_path):
            logger.warning(f"투표 데이터 파일 존재하지 않음: {file_path}")
            return None
        try:
            async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
                content = await f.read()
                data = json.loads(content)
            logger.debug(f"투표 데이터 로드 완료: {file_path}")
            return data
        except Exception as e:
            logger.error(f"투표 데이터 로드 실패: {file_path}, 오류: {e}", exc_info=True)
            return None

    async def load_all_polls(self) -> Dict[str, dict]:
        """모든 투표 데이터를 불러옵니다."""
        polls = {}
        try:
            for filename in os.listdir(self.data_dir):
                if filename.endswith('.json'):
                    poll_id = filename[:-5]
                    data = await self.load_poll(poll_id)
                    if data:
                        polls[poll_id] = data
            logger.info(f"모든 투표 데이터 로드 완료: 총 {len(polls)}개")
            return polls
        except Exception as e:
            logger.error(f"모든 투표 데이터 로드 중 오류 발생: {e}", exc_info=True)
            return polls

    async def delete_poll(self, poll_id: str):
        """특정 poll_id에 대한 투표 데이터를 삭제합니다."""
        file_path = os.path.join(self.data_dir, f"{poll_id}.json")
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.debug(f"투표 데이터 삭제 완료: {file_path}")
            else:
                logger.warning(f"삭제하려는 투표 데이터 파일이 존재하지 않음: {file_path}")
        except Exception as e:
            logger.error(f"투표 데이터 삭제 실패: {file_path}, 오류: {e}", exc_info=True)
            raise e
