from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

from database.mongo import MongoDatabase
from config import Settings
from plugins.audit import audit

from .ui import arena_keyboard, back_keyboard, club_keyboard, help_keyboard, info_keyboard, menu_keyboard

WELCOME = """<b>Fʟ | Cᴀʀᴅs 🃏</b>

<i>Build the club. Manage every match.</i>

Collect players, shape your squad, and manage live group matches.

Use the short main menu below, or open club controls for collection and squad tools."""

HELP = """<b>Fʟ | Cᴀʀᴅs 🃏 — Commands</b>

<b>Start your journey</b>
/start — Open the club hub
/debut — Receive a balanced starting XI when player cards exist
/claim — Claim a random player every 12 hours
/player Name — Search all added player cards

<b>Your club</b>
/collection — Browse all owned cards
/squad — View your active 25-player squad
/profile — View coins, glory, XP, and squad rating
/team — Manage your team name, formation, lineup, and substitutes
/formation — Choose a formation unlocked by club level
/teamname — Rename your team
/subs — Set substitute players

<b>Competitions</b>
/arena — Open group competitions
/playcl, /playwc, /playacl — Open owner-created group modes
Custom modes such as /playcwc work automatically after the owner creates them.

<b>Player imports</b>
/addplayer — Add one or many pipe-delimited players (admin)
/players — Browse the added player database (admin)
/templateguide — View the bulk import template (admin)

<b>Command areas</b>
• Private chat: club tools, owner/admin tools, templates, and /resetall
• Group chat: /arena, /playcl, /playwc, custom /play… modes, and /challenge

Owners can use /owner for the complete private owner control list."""

SUPPORT = """<b>🛟 SUPPORT DESK</b>

Need help with your club?

• Use /help for the complete command guide
• Use /templateguide for the player import format
• For access, data, or bot issues, contact the bot owner or a moderator in your group

Please include the command you used and the message you received."""

DEVELOPER = """<b>🛠 OWNER / DEVELOPER</b>

Football Legacy Manager keeps player data, card artwork, clubs, and match competitions separate.

• Player cards are stored once and can be reused by multiple systems
• Every competition and fixed team is owner-created
• A group has one active lobby at a time
• Challenges use collected squads and live manager controls

The Developer button opens the owner account directly.

Owner command scopes:
• Private chat: /owner, /resetall, player/card administration
• Groups: /addcompetition, /addteam, /editteam, /deleteteam

Use /owner for the complete owner control list."""

PVP_HELP = """<b>⚔️ PLAYER CHALLENGES</b>

Reply to another player’s message with /challenge in a group. They can accept or decline, then both collected squads enter a live manager match.

Competition matches use owner-created fixed team rosters. Managers choose conditions, teams, formations, and lineups before kickoff."""


def register_start_handlers(bot: Client, database: MongoDatabase, settings: Settings) -> None:
    @bot.on_message(filters.command("start"))
    async def start_handler(_: Client, message: Message) -> None:
        is_new = await database.get_user(message.from_user.id) is None
        await database.get_or_create_user(message.from_user)
        if is_new:
            await audit(bot, settings, f"New user: <b>{message.from_user.first_name}</b> (<code>{message.from_user.id}</code>)")
        owner_id = min(settings.owner_ids) if settings.owner_ids else 8186068163
        await message.reply_text(WELCOME, reply_markup=menu_keyboard(owner_id))

    @bot.on_message(filters.command("help"))
    async def help_handler(_: Client, message: Message) -> None:
        await message.reply_text(HELP)

    @bot.on_callback_query(filters.regex(r"^menu:(home|help|arena|support|developer|pvp|club)$"))
    async def menu_handler(_: Client, query: CallbackQuery) -> None:
        await query.answer()
        destination = query.data.split(":", 1)[1]
        if destination == "home":
            owner_id = min(settings.owner_ids) if settings.owner_ids else 8186068163
            await query.message.edit_text(WELCOME, reply_markup=menu_keyboard(owner_id))
        elif destination == "club":
            await query.message.edit_text(HELP, reply_markup=club_keyboard())
        elif destination == "help":
            await query.message.edit_text(HELP, reply_markup=help_keyboard())
        elif destination == "arena":
            competitions = await database.list_competitions()
            await query.message.edit_text(
                "<b>🔥 ARENA</b>\n\nChoose a group competition. Each opponent below has its own fixed squad.",
                reply_markup=arena_keyboard(competitions),
            )
        elif destination == "support":
            await query.message.edit_text(SUPPORT, reply_markup=info_keyboard("menu:home"))
        elif destination == "developer":
            await query.message.edit_text(DEVELOPER, reply_markup=info_keyboard("menu:home"))
        else:
            await query.message.edit_text(PVP_HELP, reply_markup=info_keyboard("menu:home"))
