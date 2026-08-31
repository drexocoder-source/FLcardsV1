from __future__ import annotations

import asyncio
import logging
import os

from pyrogram import Client, idle
from pyrogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
)

from app import start_health_server
from config import Settings
from database.mongo import MongoDatabase
from dhandlers import register_handlers

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("fl_cards")


GENERAL_COMMANDS = [
    BotCommand("start", "Open the club hub"),
    BotCommand("help", "See all player commands"),
    BotCommand("debut", "Receive your balanced starter squad"),
    BotCommand("claim", "Claim a random player"),
    BotCommand("player", "Search every added player card"),
    BotCommand("shop", "Open the card shop"),
    BotCommand("collection", "Browse your card collection"),
    BotCommand("squad", "View your active squad"),
    BotCommand("profile", "View your club profile"),
    BotCommand("team", "Manage your club team"),
    BotCommand("formation", "Choose an unlocked formation"),
    BotCommand("teamname", "Rename your team"),
    BotCommand("subs", "Set your substitutes"),
    BotCommand("instruction", "Set a player instruction"),
]

GROUP_COMMANDS = GENERAL_COMMANDS + [
    BotCommand("arena", "Open group competitions"),
    BotCommand("playcl", "Choose a Champions League club"),
    BotCommand("playucl", "Choose a Champions League club"),
    BotCommand("playwc", "Choose a World Cup nation"),
    BotCommand("playacl", "Choose an Asian club"),
    BotCommand("challenge", "Challenge a player in a group"),
]

OWNER_COMMANDS = GENERAL_COMMANDS + [
    BotCommand("owner", "Open owner controls"),
    BotCommand("resetall", "Permanently clear all bot data"),
    BotCommand("admin", "Open administrator tools"),
    BotCommand("addplayer", "Add one or many player cards"),
    BotCommand("addplayers", "Bulk import player cards"),
    BotCommand("players", "Browse every player card"),
    BotCommand("botinfo", "Show bot statistics"),
    BotCommand("shopprice", "Edit card pack prices"),
    BotCommand("addtemplate", "Save a card template"),
    BotCommand("templates", "List saved card templates"),
    BotCommand("templateguide", "View the card template guide"),
    BotCommand("addcompetition", "Create a competition"),
    BotCommand("addteam", "Add a team to a competition"),
    BotCommand("editteam", "Edit an owner-created team"),
    BotCommand("deleteteam", "Delete an owner-created team"),
    BotCommand("tplayer", "Create an owner photo card"),
    BotCommand("testms", "Generate a test football match summary"),
    BotCommand("mods", "List moderators"),
    BotCommand("addmod", "Grant moderator access"),
    BotCommand("removemod", "Remove moderator access"),
]


async def register_bot_commands(bot: Client, settings: Settings) -> None:
    await bot.set_bot_commands(GENERAL_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    await bot.set_bot_commands(GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats())
    for owner_id in settings.owner_ids:
        await bot.set_bot_commands(OWNER_COMMANDS, scope=BotCommandScopeChat(chat_id=owner_id))


async def main() -> None:
    settings = Settings.from_env()
    database = MongoDatabase(settings.mongo_uri, settings.mongo_db_name)
    await database.connect()
    await database.seed_mode_catalog()
    bot = Client(
        "fl_cards_session",
        api_id=settings.api_id,
        api_hash=settings.api_hash,
        bot_token=settings.bot_token,
        workdir=os.getenv("SESSION_WORKDIR", "."),
    )
    register_handlers(bot, database, settings)
    health_server = await start_health_server(database, settings.port)
    bot_started = False

    try:
        await bot.start()
        bot_started = True
        me = await bot.get_me()
        await register_bot_commands(bot, settings)
        logger.info("Fʟ | Cᴀʀᴅs 🃏 started as @%s", me.username or me.id)
        await idle()
    finally:
        health_server.close()
        await health_server.wait_closed()
        if bot_started:
            await bot.stop()
        await database.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Fʟ | Cᴀʀᴅs stopped")
