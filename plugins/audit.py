from __future__ import annotations

import logging

from pyrogram import Client

from config import Settings

logger = logging.getLogger("fl_cards.audit")


async def audit(bot: Client, settings: Settings, text: str) -> None:
    try:
        await bot.send_message(settings.log_group_id, f"<b>Fʟ | Cᴀʀᴅs LOG</b>\n\n{text}")
    except Exception as exc:
        logger.warning("Could not send audit event: %s", exc)
