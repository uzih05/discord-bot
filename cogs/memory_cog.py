# cogs/memory_cog.py
import asyncio
import json
import logging
import os
import uuid

import aiofiles
import discord
from discord import app_commands, Interaction
from utils import is_not_dm
from discord.ext import commands

logger = logging.getLogger(__name__)

# 상수 정의
DM_NOT_ALLOWED_MESSAGE = "이 명령어는 DM에서 사용할 수 없습니다. 서버에서 시도하세요!"
DATA_ACCESS_ERROR_MESSAGE = "데이터 접근 중 문제가 발생했습니다. 잠시 후 다시 시도해주세요."
NO_MEMORY_MESSAGE = "기억된 내용이 없습니다."

# 데이터 저장 경로
DATA_DIR = os.path.join(os.path.dirname(__file__), "../cogs_data/memory_cog/data")
IMAGE_DIR = os.path.join(os.path.dirname(__file__), "../cogs_data/memory_cog/images")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(IMAGE_DIR, exist_ok=True)


class MemoryCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        try:
            self.bot = bot
            self.locks = {}  # 각 guild_id마다 Lock을 저장
            logger.info("MemoryCog 초기화 완료.")
        except Exception as e:
            logger.error(f"MemoryCog 초기화 중 오류 발생: {e}")
            raise e  # Cog 로드 실패를 알림

    async def get_lock(self, guild_id: str) -> asyncio.Lock:
        if guild_id not in self.locks:
            self.locks[guild_id] = asyncio.Lock()
        return self.locks[guild_id]

    async def load_memory(self, guild_id: str) -> dict:
        """기억 데이터 로드: 실패 시 기본 구조 반환"""
        file_path = os.path.join(DATA_DIR, f"{guild_id}.json")
        if not os.path.exists(file_path):
            return {"memories": {}}

        lock = await self.get_lock(guild_id)
        async with lock:
            try:
                async with aiofiles.open(file_path, "r", encoding='utf-8') as f:
                    data = json.loads(await f.read())
                    logger.debug(f"[Guild: {guild_id}] 메모리 데이터 로드 성공.")
                    return data
            except json.JSONDecodeError:
                logger.error(f"[Guild: {guild_id}] 파일 {file_path}에서 JSON 디코딩 오류 발생. 초기화합니다.")
                return {"memories": {}}
            except Exception as e:
                logger.error(f"[Guild: {guild_id}] 파일 {file_path} 읽기 오류: {e}")
                return {"memories": {}}

    async def save_memory(self, guild_id: str, data: dict) -> bool:
        """기억 데이터 저장: 실패 시 False 반환"""
        file_path = os.path.join(DATA_DIR, f"{guild_id}.json")
        lock = await self.get_lock(guild_id)
        async with lock:
            try:
                async with aiofiles.open(file_path, "w", encoding='utf-8') as f:
                    await f.write(json.dumps(data, indent=4, ensure_ascii=False))
                logger.debug(f"[Guild: {guild_id}] 메모리 데이터 저장 성공.")
                return True
            except Exception as e:
                logger.error(f"[Guild: {guild_id}] 파일 {file_path} 쓰기 오류: {e}")
                return False

    # 그룹 명령어 정의
    memory_group = app_commands.Group(name="기억", description="기억 관련 명령어들")

    @memory_group.command(name="기억해", description="이름과 내용을 기억하고, 필요 시 이미지를 저장합니다.")
    @app_commands.describe(
        name="기억할 항목의 이름을 입력하세요.",
        content="기억할 내용을 입력하세요.",
        attachment="저장할 이미지 파일을 첨부하세요 (선택 사항)."
    )
    @is_not_dm()
    async def remember(self, interaction: Interaction, name: str, content: str, attachment: discord.Attachment = None):
        try:
            await interaction.response.defer()  # ephemeral=True 제거
            guild_id = str(interaction.guild_id)
            logger.info(f"[Guild: {guild_id}] 기억해 명령어 실행: name='{name}', content='{content}'")

            data = await self.load_memory(guild_id)
            if "memories" not in data:
                data["memories"] = {}

            memory_entry = {"content": content}

            # 이미지 처리
            if attachment and attachment.content_type and "image" in attachment.content_type:
                unique_id = uuid.uuid4().hex
                file_extension = os.path.splitext(attachment.filename)[1]
                file_path = os.path.join(IMAGE_DIR, f"{guild_id}_{name}_{unique_id}{file_extension}")
                try:
                    await attachment.save(file_path)
                    memory_entry["image"] = file_path
                    logger.info(f"[Guild: {guild_id}] 이미지 저장 완료: {file_path}")
                except Exception as e:
                    logger.error(f"[Guild: {guild_id}] 이미지 저장 중 오류: {e}")
                    memory_entry["image"] = None
            else:
                memory_entry["image"] = None

            data["memories"][name] = memory_entry
            if not await self.save_memory(guild_id, data):
                await interaction.followup.send(DATA_ACCESS_ERROR_MESSAGE)  # ephemeral=True 제거
                return

            image_msg = " 이미지도 저장했습니다." if memory_entry["image"] else ""
            await interaction.followup.send(f"'{name}'을(를) 기억했어요!{image_msg}")  # ephemeral=True 제거
            logger.info(f"[Guild: {guild_id}] '{name}' 기억 완료.")
        except Exception as e:
            logger.error(f"remember 명령어 실행 중 오류 발생: {e}")
            await interaction.followup.send("기억하는 중 오류가 발생했습니다.", ephemeral=True)

    @memory_group.command(name="출력", description="기억한 내용을 출력합니다.")
    @app_commands.describe(name="출력할 기억의 이름을 입력하세요.")
    @is_not_dm()
    async def recall(self, interaction: Interaction, name: str):
        try:
            await interaction.response.defer()  # ephemeral=True 제거
            guild_id = str(interaction.guild_id)
            logger.info(f"[Guild: {guild_id}] 출력 명령어 실행: name='{name}'")

            data = await self.load_memory(guild_id)
            memory = data.get("memories", {}).get(name)

            if memory is None:
                await interaction.followup.send(f"'{name}'에 대한 기억이 없습니다.")  # ephemeral=True 제거
                logger.warning(f"[Guild: {guild_id}] '{name}' 기억 없음.")
                return

            content = memory.get("content", "내용 없음")
            image = memory.get("image", None)
            embed = discord.Embed(title=name, description=content, color=discord.Color.green())

            if image:
                if os.path.exists(image):
                    try:
                        file = discord.File(image, filename="image.png")
                        embed.set_image(url="attachment://image.png")
                        await interaction.followup.send(embed=embed, file=file)  # ephemeral=True 제거
                        logger.info(f"[Guild: {guild_id}] '{name}' 내용 출력 완료 (이미지 포함).")
                    except Exception as e:
                        logger.error(f"[Guild: {guild_id}] 이미지 전송 중 오류: {e}")
                        await interaction.followup.send(
                            f"'{name}'의 이미지를 전송하는 중 오류가 발생했습니다. 텍스트만 표시합니다."
                        )  # ephemeral=True 제거
                        await interaction.followup.send(embed=embed)  # ephemeral=True 제거
                else:
                    logger.warning(f"[Guild: {guild_id}] 이미지 파일 {image} 존재하지 않음.")
                    await interaction.followup.send(
                        f"'{name}'의 이미지를 찾을 수 없어 텍스트만 표시합니다."
                    )  # ephemeral=True 제거
                    await interaction.followup.send(embed=embed)  # ephemeral=True 제거
            else:
                await interaction.followup.send(embed=embed)  # ephemeral=True 제거
                logger.info(f"[Guild: {guild_id}] '{name}' 내용 출력 완료 (텍스트만).")
        except Exception as e:
            logger.error(f"recall 명령어 실행 중 오류 발생: {e}")
            await interaction.followup.send("내용을 출력하는 중 오류가 발생했습니다.", ephemeral=True)

    @memory_group.command(name="잊어줘", description="기억한 내용을 삭제합니다.")
    @app_commands.describe(name="삭제할 기억의 이름을 입력하세요.")
    @app_commands.default_permissions(manage_messages=True)
    @is_not_dm()
    async def forget(self, interaction: Interaction, name: str):
        try:
            await interaction.response.defer()  # ephemeral=True 제거
            guild_id = str(interaction.guild_id)
            logger.info(f"[Guild: {guild_id}] 잊어줘 명령어 실행: name='{name}'")

            data = await self.load_memory(guild_id)
            memories = data.get("memories", {})

            memory = memories.pop(name, None)
            if memory is None:
                await interaction.followup.send(f"'{name}'에 대한 기억이 없습니다.")  # ephemeral=True 제거
                logger.warning(f"[Guild: {guild_id}] '{name}' 기억 없음. 삭제 시도.")
                return

            image = memory.get("image")
            if image and os.path.exists(image):
                try:
                    os.remove(image)
                    logger.info(f"[Guild: {guild_id}] 이미지 파일 {image} 삭제 완료.")
                except FileNotFoundError:
                    logger.warning(f"[Guild: {guild_id}] 이미지 파일 {image}를 찾을 수 없습니다.")
                except Exception as e:
                    logger.error(f"[Guild: {guild_id}] 이미지 파일 삭제 중 오류: {e}")

            data["memories"] = memories
            if not await self.save_memory(guild_id, data):
                await interaction.followup.send(DATA_ACCESS_ERROR_MESSAGE)  # ephemeral=True 제거
                return

            await interaction.followup.send(f"'{name}'을(를) 잊어버렸습니다.")  # ephemeral=True 제거
            logger.info(f"[Guild: {guild_id}] '{name}' 기억 삭제 완료.")
        except Exception as e:
            logger.error(f"forget 명령어 실행 중 오류 발생: {e}")
            await interaction.followup.send("기억을 삭제하는 중 오류가 발생했습니다.", ephemeral=True)

    @memory_group.command(name="리스트", description="기억한 모든 내용을 출력합니다.")
    @is_not_dm()
    async def list_memories(self, interaction: Interaction):
        try:
            await interaction.response.defer()  # ephemeral=True 제거
            guild_id = str(interaction.guild_id)
            logger.info(f"[Guild: {guild_id}] 리스트 명령어 실행.")

            data = await self.load_memory(guild_id)
            memories = data.get("memories", {})

            if not memories:
                await interaction.followup.send(NO_MEMORY_MESSAGE)  # ephemeral=True 제거
                logger.info(f"[Guild: {guild_id}] 기억된 내용 없음.")
                return

            embed = discord.Embed(title="기억된 내용 리스트", description="저장된 기억들의 목록입니다.", color=discord.Color.green())
            for name, content in memories.items():
                text_content = content.get("content", "내용 없음")
                image_url = content.get("image")
                has_image = "있음" if image_url else "없음"
                embed.add_field(name=f"{name} (이미지: {has_image})", value=text_content, inline=False)

            await interaction.followup.send(embed=embed)  # ephemeral=True 제거
            logger.info(f"[Guild: {guild_id}] 기억된 내용 리스트 출력 완료.")
        except Exception as e:
            logger.error(f"list_memories 명령어 실행 중 오류 발생: {e}")
            await interaction.followup.send("기억 리스트를 출력하는 중 오류가 발생했습니다.", ephemeral=True)

    @app_commands.command(name="프로필", description="특정 유저의 프로필 사진을 표시합니다.")
    @app_commands.describe(user="프로필을 확인할 유저를 선택하세요.")
    @is_not_dm()
    async def profile(self, interaction: Interaction, user: discord.Member):
        try:
            await interaction.response.defer()  # ephemeral=True 제거
            guild_id = str(interaction.guild_id)
            logger.info(f"[Guild: {guild_id}] 프로필 명령어 실행: user='{user.display_name}'")

            embed = discord.Embed(title=f"{user.display_name}의 프로필", color=discord.Color.green())
            avatar_url = user.avatar.url if user.avatar else user.default_avatar.url
            embed.set_image(url=avatar_url)
            await interaction.followup.send(embed=embed)  # ephemeral=True 제거
            logger.info(f"[Guild: {guild_id}] '{user.display_name}'의 프로필 이미지 전송 완료.")
        except Exception as e:
            logger.error(f"profile 명령어 실행 중 오류 발생: {e}")
            await interaction.followup.send("프로필을 표시하는 중 오류가 발생했습니다.", ephemeral=True)

# 모듈 레벨의 setup 함수 정의
async def setup(bot: commands.Bot):
    try:
        if "MemoryCog" not in bot.cogs:
            await bot.add_cog(MemoryCog(bot))
            logger.info("MemoryCog이 성공적으로 로드되었습니다.")
    except Exception as e:
        logger.error(f"MemoryCog 로드 중 오류 발생: {e}")