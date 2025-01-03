import aiofiles
import json
import logging
import os
import tempfile
from typing import Dict, Optional
from asyncio import Lock
from json import JSONDecodeError

logger = logging.getLogger(__name__)

# 파일 접근을 동기화하기 위한 전역 딕셔너리
file_locks: Dict[str, Lock] = {}


def get_file_lock(file_path: str) -> Lock:
    """파일별로 고유한 Lock을 반환"""
    if file_path not in file_locks:
        file_locks[file_path] = Lock()
    return file_locks[file_path]


async def load_json(file_path: str, default: Optional[Dict] = None) -> Dict:
    """
    JSON 파일을 읽어 딕셔너리로 반환합니다.
    파일이 없거나 읽기 오류가 발생하면 기본값을 반환합니다.

    Args:
        file_path (str): 읽을 JSON 파일의 경로.
        default (Optional[Dict]): 파일이 없거나 오류 발생 시 반환할 기본 딕셔너리.

    Returns:
        Dict: 파일에서 읽은 데이터 또는 기본값.
    """
    if default is None:
        default = {}
    lock = get_file_lock(file_path)
    async with lock:
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                content = await f.read()
                data = json.loads(content)
                logger.debug(f"파일 '{file_path}'에서 데이터 로드 성공.")
                return data
        except FileNotFoundError:
            logger.warning(f"파일 '{file_path}'이(가) 존재하지 않습니다. 기본값을 반환합니다.")
            return default
        except JSONDecodeError as e:
            logger.error(f"파일 '{file_path}'의 JSON 디코딩 오류: {e}. 기본값을 반환합니다.")
            return default
        except OSError as e:
            logger.error(f"파일 '{file_path}' 읽기 중 OS 오류 발생: {e}. 기본값을 반환합니다.")
            return default
        except Exception as e:
            logger.error(f"파일 '{file_path}' 읽기 중 예상치 못한 오류 발생: {e}. 기본값을 반환합니다.")
            return default


async def save_json(file_path: str, data: Dict) -> bool:
    """
    딕셔너리를 JSON 파일로 저장합니다.
    저장에 실패하면 False를 반환하고, 성공하면 True를 반환합니다.

    Args:
        file_path (str): 저장할 JSON 파일의 경로.
        data (Dict): 저장할 데이터.

    Returns:
        bool: 저장 성공 여부.
    """
    lock = get_file_lock(file_path)
    async with lock:
        try:
            # 디렉토리 존재 확인 및 생성
            os.makedirs(os.path.dirname(file_path), exist_ok=True)

            # 원자적 쓰기를 위해 임시 파일 사용
            with tempfile.NamedTemporaryFile("w", delete=False, dir=os.path.dirname(file_path),
                                             encoding="utf-8") as tmp_file:
                tmp_file_path = tmp_file.name
                json.dump(data, tmp_file, indent=4, ensure_ascii=False)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())

            # 임시 파일을 원본 파일로 이동 (원자적 이동)
            os.replace(tmp_file_path, file_path)
            logger.debug(f"파일 '{file_path}'에 데이터 저장 성공.")
            return True
        except OSError as e:
            logger.error(f"파일 '{file_path}' 쓰기 중 OS 오류 발생: {e}.")
            return False
        except Exception as e:
            logger.error(f"파일 '{file_path}' 쓰기 중 예상치 못한 오류 발생: {e}.")
            return False
