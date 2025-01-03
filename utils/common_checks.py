from discord import Interaction
from discord.app_commands import check

# 디엠 예외처리
def is_not_dm():
    async def predicate(interaction: Interaction) -> bool:
        return interaction.guild is not None
    return check(predicate)