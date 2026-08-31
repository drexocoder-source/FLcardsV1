from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import Message

from config import Settings
from database.mongo import MongoDatabase
from plugins.audit import audit


def register_audit_handlers(bot: Client, database: MongoDatabase, settings: Settings) -> None:
    @bot.on_message(filters.group & filters.new_chat_members, group=90)
    async def group_member_handler(_: Client, message: Message) -> None:
        names = ", ".join(member.first_name for member in message.new_chat_members or [])
        await audit(
            bot,
            settings,
            f"New group member event in <b>{message.chat.title or message.chat.id}</b>: {names}",
        )

    @bot.on_message(
        filters.group & filters.command(["start", "debut", "claim", "challenge", "playcl", "playwc", "playacl"]),
        group=91,
    )
    async def group_command_handler(_: Client, message: Message) -> None:
        command = message.command[0] if message.command else "unknown"
        await audit(
            bot,
            settings,
            f"Group command <code>/{command}</code> in <b>{message.chat.title or message.chat.id}</b> by <code>{message.from_user.id}</code>",
        )