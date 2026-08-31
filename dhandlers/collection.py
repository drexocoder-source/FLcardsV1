from __future__ import annotations

import asyncio
import base64
import binascii
import html
import os
from datetime import UTC, datetime

from pyrogram import Client, filters
from pyrogram.enums import ButtonStyle
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database.mongo import MongoDatabase
from config import Settings
from services.cards import render_player_card

from .ui import claim_keyboard

FORMATIONS = {
    "4-3-3": 1,
    "4-4-2": 1,
    "3-5-2": 2,
    "4-2-3-1": 2,
    "4-3-1-2": 3,
    "3-4-3": 4,
    "5-3-2": 5,
    "4-1-4-1": 6,
}


def _unlocked_formations(user: dict) -> list[str]:
    level = int(user.get("xp", 0)) // 1000 + 1
    return [formation for formation, required in FORMATIONS.items() if required <= level]


def _formation_keyboard(user: dict) -> InlineKeyboardMarkup:
    formations = _unlocked_formations(user)
    rows = [
        [
            InlineKeyboardButton(
                formation,
                callback_data=f"clubformation:{formation}",
                style=ButtonStyle.SUCCESS if formation == user.get("formation", "4-3-3") else ButtonStyle.PRIMARY,
            )
            for formation in formations[index : index + 3]
        ]
        for index in range(0, len(formations), 3)
    ]
    rows.append([InlineKeyboardButton("Main menu", callback_data="menu:home", style=ButtonStyle.PRIMARY)])
    return InlineKeyboardMarkup(rows)


def player_line(player: dict) -> str:
    return f"{player.get('nation', '🌐')} <b>{player['name']}</b> · {player.get('position', 'MID')} · OVR {player.get('ovr', 0)}"


def card_text(player: dict, claimed_by: str | None = None) -> str:
    traits = " · ".join(player.get("traits", [])) or "Complete Footballer"
    owner_line = f"👤 Claimed by: {claimed_by}\n\n" if claimed_by else ""
    return f"""<b>PLAYER CARD</b>

{owner_line}{player.get('nation', '🌐')} <b>{player['name']}</b>
🏟 Club: {player.get('club', 'Free Agent')}
🎴 Rarity: <b>{player.get('rarity', 'COMMON')}</b>
🧤 Position: <b>{player.get('position', 'MID')}</b>
⭐ OVR: <b>{player.get('ovr', 0)}</b>

⚡ PAC  {player.get('pace', 0):>2}    🎯 SHO  {player.get('shooting', 0):>2}
🧠 PAS  {player.get('passing', 0):>2}    ✨ DRI  {player.get('dribbling', 0):>2}
🛡 DEF  {player.get('defending', 0):>2}    💪 PHY  {player.get('physical', 0):>2}

🔹 {traits}"""


def _search_token(query: str) -> str:
    compact = query.strip()[:48]
    return base64.urlsafe_b64encode(compact.encode()).decode().rstrip("=")


def _search_query(token: str) -> str:
    padded = token + "=" * (-len(token) % 4)
    return base64.urlsafe_b64decode(padded.encode()).decode()


def _player_page_keyboard(token: str, page: int, total: int) -> InlineKeyboardMarkup | None:
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("⬅️ Back", callback_data=f"playerpage:{token}:{page - 1}"))
    if page + 1 < total:
        buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"playerpage:{token}:{page + 1}"))
    return InlineKeyboardMarkup([buttons]) if buttons else None


def _player_caption(player: dict, query: str, page: int, total: int) -> str:
    return f"{card_text(player)}\n\n🔎 <b>Search:</b> {html.escape(query)}\n📄 Card <b>{page + 1}/{total}</b>"


async def collection_text(database: MongoDatabase, user_id: int, squad_only: bool = False) -> str:
    user, players = await database.get_user_players(user_id, squad_only=squad_only)
    title = "ACTIVE SQUAD" if squad_only else "YOUR COLLECTION"
    if not players:
        return f"<b>{title}</b>\n\nNo cards yet. Use /debut to start your club."
    grouped = {"GK": [], "DEF": [], "MID": [], "ATT": []}
    for player in players:
        grouped.setdefault(player.get("position", "MID"), []).append(player)
    lines = [f"<b>{title}</b>", f"Cards: {len(players)}/{'25' if squad_only else '100'}", ""]
    for position, label in (("GK", "🧤 GOALKEEPERS"), ("DEF", "🛡 DEFENDERS"), ("MID", "🎯 MIDFIELDERS"), ("ATT", "⚡ ATTACKERS")):
        if grouped.get(position):
            lines.append(f"<b>{label}</b>")
            lines.extend(f"{index}. {player_line(player)}" for index, player in enumerate(grouped[position], 1))
            lines.append("")
    if squad_only:
        lines.append(f"Formation: <b>{user.get('formation', '4-3-3')}</b>")
    return "\n".join(lines)


async def team_text(database: MongoDatabase, user_id: int) -> str:
    user, players = await database.get_user_players(user_id, squad_only=True)
    subs = await database.get_players(user.get("substitutes", []))
    level = int(user.get("xp", 0)) // 1000 + 1
    lineup = players[:11]
    lines = [
        "<b>⚽ TEAM MANAGEMENT</b>",
        "",
        f"Team: <b>{user.get('team_name', 'Legacy United')}</b>",
        f"Level: <b>{level}</b> · XP: <b>{user.get('xp', 0):,}</b>",
        f"Formation: <b>{user.get('formation', '4-3-3')}</b>",
        f"Tactic: <b>{user.get('tactics', 'Balanced')}</b> · Mentality: <b>{user.get('mentality', 'Balanced')}</b>",
        "",
        "<b>Starting lineup</b>",
    ]
    lines.extend(f"{index}. {player_line(player)}" for index, player in enumerate(lineup, 1))
    lines.append("")
    lines.append("<b>Substitutes</b>")
    lines.extend(f"• {player_line(player)}" for player in subs) if subs else lines.append("No substitutes selected.")
    lines.extend(["", "Use /teamname Name, /formation 4-3-3, or /subs Player One, Player Two."])
    return "\n".join(lines)


async def _render_card(bot: Client, database: MongoDatabase, player: dict) -> str:
    template_path = None
    template = await database.get_template(player.get("rarity"), player.get("position"))
    if template and template.get("image_file_id"):
        try:
            template_path = await bot.download_media(
                template["image_file_id"],
                file_name=f"/tmp/fl-template-{template['template_id']}.png",
            )
        except Exception:
            template_path = None
    card_path = await asyncio.to_thread(render_player_card, player, template_path, template.get("layout") if template else None)
    if template_path:
        try:
            os.unlink(template_path)
        except OSError:
            pass
    return card_path


async def _send_card(
    bot: Client,
    database: MongoDatabase,
    message: Message,
    player: dict,
    caption: str | None = None,
    reply_markup=None,
) -> None:
    if player.get("card_photo_file_id"):
        await message.reply_photo(
            photo=player["card_photo_file_id"],
            caption=caption or card_text(player),
            reply_markup=reply_markup,
        )
        return
    card_path = await _render_card(bot, database, player)
    try:
        await message.reply_photo(
            photo=card_path,
            caption=caption or card_text(player),
            reply_markup=reply_markup,
        )
    finally:
        try:
            os.unlink(card_path)
        except OSError:
            pass


def register_collection_handlers(bot: Client, database: MongoDatabase, settings: Settings) -> None:
    @bot.on_message(filters.command("debut"))
    async def debut_handler(_: Client, message: Message) -> None:
        user = await database.get_or_create_user(message.from_user)
        if user.get("collection"):
            await message.reply_text("Your debut has already happened. Open /collection to view your cards.")
            return
        players = await database.add_debut_squad(message.from_user.id)
        squad_rating = round(sum(player.get("ovr", 0) for player in players) / max(len(players), 1))
        lines = [
            "<b>⚽ DEBUT SQUAD</b>",
            "",
        ]
        for player in players:
            lines.append(player_line(player))
        lines.extend(["", f"Formation: <b>4-3-3</b>", f"Squad OVR: <b>{squad_rating}</b>", "Players added: <b>11/25</b>"])
        await message.reply_text("\n".join(lines))

    @bot.on_message(filters.command("claim"))
    async def claim_handler(_: Client, message: Message) -> None:
        await database.get_or_create_user(message.from_user)
        player, available_at = await database.claim_candidate(message.from_user.id)
        if available_at:
            remaining = available_at - datetime.now(UTC)
            hours, remainder = divmod(max(0, int(remaining.total_seconds())), 3600)
            minutes = remainder // 60
            await message.reply_text(f"🕒 Your next claim unlocks in <b>{hours}h {minutes}m</b>.")
            return
        if not player:
            await message.reply_text("No player cards are available yet. Ask an admin to add players.")
            return
        username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
        text = f"""<b>PLAYER ASSIGNED</b>

👤 Claimed by: <b>{username}</b>

{card_text(player)}

💰 Claim reward: <b>+1,000 Coins</b>
💎 Market value: <b>{player.get('ovr', 0) * 100_000:,}</b>

Choose what happens to this card:"""
        await _send_card(bot, database, message, player, caption=text, reply_markup=claim_keyboard())

    @bot.on_message(filters.command("collection"))
    async def collection_handler(_: Client, message: Message) -> None:
        await database.get_or_create_user(message.from_user)
        await message.reply_text(await collection_text(database, message.from_user.id))

    @bot.on_message(filters.command("player"))
    async def player_handler(_: Client, message: Message) -> None:
        search = message.text.partition(" ")[2].strip()
        if not search:
            await message.reply_text(
                "<b>Player search</b>\n\nUse <code>/player Ronaldo</code> or <code>/player Ronaldo CR7</code> to search all added cards.",
            )
            return
        results = await database.search_players(search)
        if not results:
            await message.reply_text(
                f"No added card matching <b>{html.escape(search)}</b> was found.",
            )
            return
        token = _search_token(search)
        await _send_card(
            bot,
            database,
            message,
            results[0],
            caption=_player_caption(results[0], search, 0, len(results)),
            reply_markup=_player_page_keyboard(token, 0, len(results)),
        )

    @bot.on_message(filters.command("squad"))
    async def squad_handler(_: Client, message: Message) -> None:
        await database.get_or_create_user(message.from_user)
        await message.reply_text(await collection_text(database, message.from_user.id, squad_only=True))

    @bot.on_message(filters.command("profile"))
    async def profile_handler(_: Client, message: Message) -> None:
        user, players = await database.get_user_players(message.from_user.id, squad_only=True)
        rating = round(sum(player.get("ovr", 0) for player in players) / max(len(players), 1))
        await message.reply_text(
            f"""<b>CLUB PROFILE</b>

👤 {message.from_user.first_name}
🪙 Coins: <b>{user.get('coins', 0):,}</b>
💎 Glory: <b>{user.get('glory', 0):,}</b>
✨ XP: <b>{user.get('xp', 0):,}</b>
🏟 Squad: <b>{len(players)}/25</b>
⭐ Squad OVR: <b>{rating}</b>""",
        )

    @bot.on_message(filters.command("team"))
    async def team_handler(_: Client, message: Message) -> None:
        await database.get_or_create_user(message.from_user)
        user = await database.get_user(message.from_user.id) or {}
        await message.reply_text(await team_text(database, message.from_user.id), reply_markup=_formation_keyboard(user))

    @bot.on_message(filters.command("formation"))
    async def formation_handler(_: Client, message: Message) -> None:
        user = await database.get_or_create_user(message.from_user)
        requested = message.text.partition(" ")[2].strip()
        if requested:
            requested = requested.upper()
            if requested not in _unlocked_formations(user):
                level = int(user.get("xp", 0)) // 1000 + 1
                await message.reply_text(
                    f"That formation is locked or invalid. Your club is level {level}.",
                    reply_markup=_formation_keyboard(user),
                )
                return
            await database.update_user(message.from_user.id, {"formation": requested})
            await message.reply_text(f"Formation set to <b>{requested}</b>.", reply_markup=_formation_keyboard({**user, "formation": requested}))
            return
        await message.reply_text(
            f"<b>FORMATION ROOM</b>\n\nClub level: <b>{int(user.get('xp', 0)) // 1000 + 1}</b>\nChoose an unlocked formation. More formations unlock as XP grows.",
            reply_markup=_formation_keyboard(user),
        )

    @bot.on_message(filters.command("teamname"))
    async def teamname_handler(_: Client, message: Message) -> None:
        name = message.text.partition(" ")[2].strip()
        if not name:
            await message.reply_text("Use <code>/teamname Your Club Name</code>.")
            return
        name = name[:40]
        await database.get_or_create_user(message.from_user)
        await database.update_user(message.from_user.id, {"team_name": name})
        await message.reply_text(f"Your team is now <b>{name}</b>.")

    @bot.on_message(filters.command("subs"))
    async def subs_handler(_: Client, message: Message) -> None:
        user = await database.get_or_create_user(message.from_user)
        raw = message.text.partition(" ")[2].strip()
        if not raw:
            subs = await database.get_players(user.get("substitutes", []))
            text = "<b>SUBSTITUTES</b>\n\n" + ("\n".join(f"• {player_line(player)}" for player in subs) if subs else "No substitutes selected.")
            await message.reply_text(text + "\n\nUse <code>/subs Player One, Player Two</code>.")
            return
        selected = []
        for name in raw.split(",")[:7]:
            matches = await database.search_user_players(message.from_user.id, name.strip())
            if matches and matches[0]["player_id"] not in selected:
                selected.append(matches[0]["player_id"])
        await database.update_user(message.from_user.id, {"substitutes": selected})
        await message.reply_text(
            f"Saved <b>{len(selected)}</b> substitutes. Use /team to review your lineup.",
        )

    @bot.on_callback_query(filters.regex(r"^clubformation:([0-9-]+)$"))
    async def club_formation_handler(_: Client, query: CallbackQuery) -> None:
        formation = query.data.split(":", 1)[1]
        user = await database.get_or_create_user(query.from_user)
        if formation not in _unlocked_formations(user):
            await query.answer("That formation is still locked.", show_alert=True)
            return
        await database.update_user(query.from_user.id, {"formation": formation})
        await query.answer(f"{formation} selected.")
        await query.message.edit_text(await team_text(database, query.from_user.id), reply_markup=_formation_keyboard({**user, "formation": formation}))

    @bot.on_callback_query(filters.regex(r"^menu:(claim|debut|collection|squad|profile|team)$"))
    async def menu_action_handler(_: Client, query: CallbackQuery) -> None:
        await query.answer()
        command = query.data.split(":", 1)[1]
        if command == "claim":
            await database.get_or_create_user(query.from_user)
            player, available_at = await database.claim_candidate(query.from_user.id)
            if available_at:
                remaining = available_at - datetime.now(UTC)
                hours, remainder = divmod(max(0, int(remaining.total_seconds())), 3600)
                minutes = remainder // 60
                await query.message.edit_text(
                    f"🕒 Your next claim unlocks in <b>{hours}h {minutes}m</b>.",
                )
            elif player:
                username = f"@{query.from_user.username}" if query.from_user.username else query.from_user.first_name
                await _send_card(
                    bot,
                    database,
                    query.message,
                    player,
                    f"""<b>PLAYER ASSIGNED</b>

👤 Claimed by: <b>{username}</b>

{card_text(player)}

💰 Claim reward: <b>+1,000 Coins</b>
💎 Market value: <b>{player.get('ovr', 0) * 100_000:,}</b>

Choose what happens to this card:""",
                    claim_keyboard(),
                )
            else:
                await query.message.edit_text(
                    "No player cards are available yet. Ask an admin to add players.",
                )
        elif command == "debut":
            await query.message.reply_text("Use /debut to receive a balanced 4-3-3 starter squad.")
        elif command == "profile":
            user, players = await database.get_user_players(query.from_user.id, squad_only=True)
            rating = round(sum(player.get("ovr", 0) for player in players) / max(len(players), 1))
            await query.message.edit_text(
                f"""<b>CLUB PROFILE</b>

👤 {query.from_user.first_name}
🪙 Coins: <b>{user.get('coins', 0):,}</b>
💎 Glory: <b>{user.get('glory', 0):,}</b>
✨ XP: <b>{user.get('xp', 0):,}</b>
🏟 Squad: <b>{len(players)}/25</b>
⭐ Squad OVR: <b>{rating}</b>""",
                reply_markup=back_keyboard("Club hub"),
            )
        elif command == "team":
            user = await database.get_or_create_user(query.from_user)
            await query.message.edit_text(await team_text(database, query.from_user.id), reply_markup=_formation_keyboard(user))
        else:
            await query.message.edit_text(
                await collection_text(database, query.from_user.id, squad_only=command == "squad"),
                reply_markup=back_keyboard(),
            )

    @bot.on_callback_query(filters.regex(r"^claim:(retain|release|view)$"))
    async def claim_action_handler(_: Client, query: CallbackQuery) -> None:
        await query.answer()
        action = query.data.split(":", 1)[1]
        if action == "retain":
            player = await database.retain_pending(query.from_user.id)
            text = "🟢 Card retained and added to your active squad." if player else "This claim is no longer available."
        elif action == "release":
            player = await database.release_pending(query.from_user.id)
            text = f"🔴 {player['name']} released. Coins added to your club." if player else "This claim is no longer available."
            try:
                await query.message.edit_caption(caption=text, reply_markup=None)
            except Exception:
                await query.message.edit_text(text)
            return
        else:
            user = await database.get_or_create_user(query.from_user)
            player_id = user.get("pending_claim")
            player = (await database.get_players([player_id]))[0] if player_id else None
            if player:
                await _send_card(bot, database, query.message, player)
            else:
                await query.message.edit_text("This claim is no longer available.")
            return
        try:
            await query.message.edit_caption(caption=text, reply_markup=None)
        except Exception:
            await query.message.edit_text(text)

    @bot.on_callback_query(filters.regex(r"^playerpage:[A-Za-z0-9_-]+:[0-9]+$"))
    async def player_page_handler(_: Client, query: CallbackQuery) -> None:
        _, token, page_text = query.data.split(":")
        try:
            search = _search_query(token)
            page = int(page_text)
        except (ValueError, UnicodeDecodeError, binascii.Error):
            await query.answer("That search has expired.", show_alert=True)
            return
        results = await database.search_players(search)
        if not results or page < 0 or page >= len(results):
            await query.answer("That player page is no longer available.", show_alert=True)
            return
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass
        player = results[page]
        await _send_card(
            bot,
            database,
            query.message,
            player,
            caption=_player_caption(player, search, page, len(results)),
            reply_markup=_player_page_keyboard(token, page, len(results)),
        )
