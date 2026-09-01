from __future__ import annotations

import asyncio
import base64
import binascii
import html
import os
from pathlib import Path
from datetime import UTC, datetime

from pyrogram import Client, filters
from pyrogram.enums import ButtonStyle, MessageEntityType, ParseMode
from pyrogram.parser import Parser
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, MessageEntity

from database.mongo import MongoDatabase
from config import Settings
from services.cards import render_player_card

from .ui import (
    back_keyboard,
    claim_keyboard,
    shop_button,
    shop_keyboard,
    shop_pack_keyboard,
    shop_pack_text,
    shop_text,
)


_TEMPLATE_CACHE_DIR = Path("/tmp/fl-card-templates")
_TEMPLATE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_TEMPLATE_DOWNLOAD_LOCK = asyncio.Lock()
MAX_PLAYER_SEARCH_RESULTS = 24

PREMIUM_EMOJI_IDS = {
    "👤": "5408846628763217930",
    "🏟": "5195426924981154277",
    "❔": "5452061640507803327",
    "⚽️": "5875210601717830561",
    "⚽": "5875210601717830561",
    "⭐️": "5895511022340411227",
    "⭐": "5895511022340411227",
    "⚡️": "5852800639188341430",
    "⚡": "5852800639188341430",
    "🎯": "6125218994455582617",
    "🧠": "5237799019329105246",
    "✨": "5451636889717062286",
    "🛡": "5251203410396458957",
    "🛡️": "5251203410396458957",
    "💪": "5427342093674630148",
    "🔹": "5971895400792067820",
    "💰": "6278294652541996868",
    "💎": "5471952986970267163",
}

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
    rows.append([shop_button(), InlineKeyboardButton("Main menu", callback_data="menu:home", style=ButtonStyle.PRIMARY)])
    return InlineKeyboardMarkup(rows)


def player_line(player: dict) -> str:
    return f"{player.get('nation', '🌐')} <b>{player['name']}</b> · {player.get('position', 'MID')} · OVR {player.get('ovr', 0)}"


def card_text(player: dict, claimed_by: str | None = None) -> str:
    traits = " · ".join(player.get("traits", [])) or "Complete Footballer"
    owner_line = f"👤 Claimed by: {claimed_by}\n\n" if claimed_by else ""
    edition = str(player.get("edition", "")).strip()
    card_type_line = (
        f"❔ Edition: <b>{html.escape(edition)}</b>"
        if edition
        else f"❔ Rarity: <b>{player.get('rarity', 'COMMON')}</b>"
    )
    return f"""<b>PLAYER CARD</b>

{owner_line}{player.get('nation', '🌐')} <b>{player['name']}</b>
🏟 Club: {player.get('club', 'Free Agent')}
{card_type_line}
⚽️ Position: <b>{player.get('position', 'MID')}</b>
⭐️ OVR: <b>{player.get('ovr', 0)}</b>

⚡️ PAC  {player.get('pace', 0):>2}    🎯 SHO  {player.get('shooting', 0):>2}
🧠 PAS  {player.get('passing', 0):>2}    ✨ DRI  {player.get('dribbling', 0):>2}
🛡 DEF  {player.get('defending', 0):>2}    💪 PHY  {player.get('physical', 0):>2}

🔹 {traits}"""


def _search_token(query: str) -> str:
    compact = query.strip()[:32]
    return base64.urlsafe_b64encode(compact.encode()).decode().rstrip("=")


def _search_query(token: str) -> str:
    padded = token + "=" * (-len(token) % 4)
    return base64.urlsafe_b64decode(padded.encode()).decode()


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


async def _premium_text_entities(text: str) -> tuple[str, list[MessageEntity]]:
    parsed = await Parser(None).parse(text, ParseMode.HTML)
    clean_text = parsed["message"]
    entities = [
        await MessageEntity._parse(None, entity, {})
        for entity in (parsed["entities"] or [])
    ]

    matches: list[tuple[int, int, str]] = []
    occupied: list[tuple[int, int]] = []
    for symbol, custom_emoji_id in sorted(PREMIUM_EMOJI_IDS.items(), key=lambda item: -len(item[0])):
        start = clean_text.find(symbol)
        while start >= 0:
            end = start + len(symbol)
            if not any(start < occupied_end and end > occupied_start for occupied_start, occupied_end in occupied):
                matches.append((start, end, custom_emoji_id))
                occupied.append((start, end))
            start = clean_text.find(symbol, start + len(symbol))

    for start, end, custom_emoji_id in matches:
        entities.append(
            MessageEntity(
                type=MessageEntityType.CUSTOM_EMOJI,
                offset=_utf16_length(clean_text[:start]),
                length=_utf16_length(clean_text[start:end]),
                custom_emoji_id=custom_emoji_id,
            )
        )
    entities.sort(key=lambda entity: (entity.offset, -entity.length))
    return clean_text, entities


async def _reply_premium_text(message: Message, text: str, reply_markup=None) -> None:
    clean_text, entities = await _premium_text_entities(text)
    await message.reply_text(
        clean_text,
        parse_mode=ParseMode.DISABLED,
        entities=entities,
        reply_markup=reply_markup,
    )


def _card_type_text(player: dict) -> str:
    edition = str(player.get("edition", "")).strip()
    rarity = str(player.get("rarity", "")).strip()
    if edition and rarity:
        return f"Rarity {rarity} · Edition {edition}"
    if edition:
        return f"Edition {edition}"
    return f"Rarity {rarity or 'COMMON'}"


def _player_search_text(query: str, results: list[dict]) -> str:
    lines = [
        "<b>PLAYER SEARCH</b>",
        f"Matches for: <b>{html.escape(query)}</b>",
        "",
    ]
    for index, player in enumerate(results, 1):
        lines.append(
            f"{index}. ⚽️ <b>{html.escape(str(player.get('name', 'Unknown')))}</b>"
            f" · 🏟 {html.escape(str(player.get('club', 'Free Agent')))}"
            f" · ⭐️ <b>{player.get('ovr', 0)}</b>"
            f" · ❔ {html.escape(_card_type_text(player))}"
        )
    lines.extend(["", "Tap a card below to open its full card."])
    return "\n".join(lines)


def _player_results_keyboard(token: str, results: list[dict]) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            f"{index} · {str(player.get('name', 'Unknown'))[:28]}",
            callback_data=f"playercard:{token}:{index - 1}",
            style=ButtonStyle.PRIMARY,
        )
        for index, player in enumerate(results, 1)
    ]
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


def _player_card_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("↩️ Search results", callback_data=f"playerresults:{token}", style=ButtonStyle.PRIMARY)]]
    )


def _player_caption(player: dict, query: str, page: int, total: int) -> str:
    return f"{card_text(player)}\n\n🔎 <b>Search:</b> {html.escape(query)}\n📄 Card <b>{page + 1}/{total}</b>"


def _pack_result_text(result: dict) -> str:
    pack = result["pack"]
    cards = result.get("cards", [])
    lines = [
        f"<b>{pack['emoji']} {pack['name']} opened ×{result['quantity']}</b>",
        "",
        *(
            f"{index}. {player.get('nation', '🌐')} <b>{player['name']}</b> · "
            f"{player.get('rarity', 'COMMON')} · OVR {player.get('ovr', 0)}"
            for index, player in enumerate(cards, 1)
        ),
        "",
        f"Spent: <b>{result['cost']:,}</b> coins",
        f"New cards: <b>{result['new_cards']}</b> · Duplicates: <b>{len(result['duplicates'])}</b>",
    ]
    if result["duplicate_credit"]:
        lines.append(f"Duplicate credit: <b>+{result['duplicate_credit']:,}</b> coins")
    lines.append(f"Balance: <b>{result['balance']:,}</b> coins")
    lines.append(f"Receipt: <code>{result['receipt_id']}</code>")
    return "\n".join(lines)


async def collection_text(database: MongoDatabase, user_id: int, squad_only: bool = False) -> str:
    user, players = await database.get_user_players(user_id, squad_only=squad_only)
    title = "ACTIVE SQUAD" if squad_only else "YOUR COLLECTION"
    if not players:
        return f"<b>{title}</b>\n\nNo cards yet. Use /debut to start your club."
    grouped = {"GK": [], "DEF": [], "MID": [], "ATT": []}
    position_groups = {
        "GK": "GK",
        "DEF": "DEF", "CB": "DEF", "LB": "DEF", "RB": "DEF", "LWB": "DEF", "RWB": "DEF",
        "MID": "MID", "CDM": "MID", "CM": "MID", "CAM": "MID", "LM": "MID", "RM": "MID",
        "ATT": "ATT", "LW": "ATT", "RW": "ATT", "CF": "ATT", "SS": "ATT", "ST": "ATT",
    }
    for player in players:
        position = str(player.get("position", "MID")).upper()
        grouped.setdefault(position_groups.get(position, "MID"), []).append(player)
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
    template = None
    template_positions = [
        *[str(position).upper() for position in player.get("secondary_positions", [])],
        str(player.get("position", "MID")).upper(),
    ]
    for template_position in template_positions:
        if player.get("edition"):
            template = await database.get_template(
                position=template_position,
                edition=player.get("edition"),
            )
        else:
            template = await database.get_template(player.get("rarity"), template_position)
        if template:
            break
    if template and template.get("image_file_id"):
        cached_path = _TEMPLATE_CACHE_DIR / f"{template['template_id']}.png"
        async with _TEMPLATE_DOWNLOAD_LOCK:
            if cached_path.exists():
                template_path = str(cached_path)
            else:
                temporary_path = _TEMPLATE_CACHE_DIR / f".{template['template_id']}.download"
                try:
                    downloaded = await bot.download_media(
                        template["image_file_id"],
                        file_name=str(temporary_path),
                    )
                    downloaded_path = Path(downloaded or temporary_path)
                    downloaded_path.replace(cached_path)
                    template_path = str(cached_path)
                except Exception:
                    for path in (temporary_path, cached_path):
                        try:
                            path.unlink()
                        except OSError:
                            pass
                    template_path = None
    card_path = await asyncio.to_thread(render_player_card, player, template_path, template.get("layout") if template else None)
    return card_path


def _profile_text(user: dict, players: list[dict], display_name: str) -> str:
    xp = int(user.get("xp", 0))
    level = xp // 1000 + 1
    xp_in_level = xp % 1000
    collection_count = len(user.get("collection", []))
    best_card = max(players, key=lambda player: int(player.get("ovr", 0)), default=None)
    wins = int(user.get("wins", 0))
    draws = int(user.get("draws", 0))
    losses = int(user.get("losses", 0))
    return f"""<b>MANAGER PROFILE</b>

👤 <b>{html.escape(display_name)}</b>
🏟 Team: <b>{html.escape(str(user.get('team_name', 'Legacy United')))}</b>
🎚 Level: <b>{level}</b> · XP: <b>{xp_in_level:,}/1,000</b>

🪙 Coins: <b>{int(user.get('coins', 0)):,}</b>
💎 Glory: <b>{int(user.get('glory', 0)):,}</b>
📚 Collection: <b>{collection_count}/100</b>
🟢 Active squad: <b>{len(players)}/25</b>
⭐ Squad OVR: <b>{round(sum(player.get('ovr', 0) for player in players) / max(len(players), 1))}</b>

📊 Record: <b>{wins}W · {draws}D · {losses}L</b>
🎯 Formation: <b>{html.escape(str(user.get('formation', '4-3-3')))}</b>
🧠 Style: <b>{html.escape(str(user.get('tactics', 'Balanced')))}</b> · <b>{html.escape(str(user.get('mentality', 'Balanced')))}</b>
🏅 Best active card: <b>{html.escape(str(best_card.get('name', '—') if best_card else '—'))}</b> · OVR <b>{int(best_card.get('ovr', 0)) if best_card else 0}</b>"""


async def _send_card(
    bot: Client,
    database: MongoDatabase,
    message: Message,
    player: dict,
    caption: str | None = None,
    reply_markup=None,
    premium_caption: bool = False,
) -> None:
    caption_text = caption or card_text(player)
    caption_entities = None
    if premium_caption:
        caption_text, caption_entities = await _premium_text_entities(caption_text)
    if player.get("card_photo_file_id"):
        await message.reply_photo(
            photo=player["card_photo_file_id"],
            caption=caption_text,
            caption_entities=caption_entities,
            reply_markup=reply_markup,
        )
        return
    card_path = await _render_card(bot, database, player)
    try:
        await message.reply_photo(
            photo=card_path,
            caption=caption_text,
            caption_entities=caption_entities,
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
        lines.extend(["", f"Formation: <b>4-3-3</b>", f"Squad OVR: <b>{squad_rating}</b>", f"Players added: <b>{len(players)}/25</b>"])
        await message.reply_text("\n".join(lines), reply_markup=back_keyboard())

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
        await _send_card(
            bot,
            database,
            message,
            player,
            caption=text,
            reply_markup=claim_keyboard(),
            premium_caption=True,
        )

    @bot.on_message(filters.command("shop"))
    async def shop_handler(_: Client, message: Message) -> None:
        user = await database.get_or_create_user(message.from_user)
        packs = await database.get_shop_packs()
        await message.reply_text(
            shop_text(int(user.get("coins", 0)), packs),
            reply_markup=shop_keyboard(packs),
        )

    @bot.on_callback_query(filters.regex(r"^shop:rarity:(COMMON|RARE|EPIC|ELITE|LEGENDARY)$"))
    async def shop_rarity_handler(_: Client, query: CallbackQuery) -> None:
        pack_key = query.data.split(":")[-1]
        packs = await database.get_shop_packs()
        pack = packs.get(pack_key)
        if not pack:
            await query.answer("That pack is unavailable.", show_alert=True)
            return
        user = await database.get_user(query.from_user.id) or {}
        await query.answer()
        await query.message.edit_text(
            shop_pack_text(pack, int(user.get("coins", 0))),
            reply_markup=shop_pack_keyboard(pack_key, pack),
        )

    @bot.on_callback_query(filters.regex(r"^shop:back$"))
    async def shop_back_handler(_: Client, query: CallbackQuery) -> None:
        packs = await database.get_shop_packs()
        user = await database.get_user(query.from_user.id) or {}
        await query.answer()
        await query.message.edit_text(
            shop_text(int(user.get("coins", 0)), packs),
            reply_markup=shop_keyboard(packs),
        )

    @bot.on_callback_query(filters.regex(r"^shop:buy:(COMMON|RARE|EPIC|ELITE|LEGENDARY):([1-3])$"))
    async def shop_purchase_handler(_: Client, query: CallbackQuery) -> None:
        _, _, pack_key, quantity_text = query.data.split(":")
        await database.get_or_create_user(query.from_user)
        result = await database.buy_pack(query.from_user.id, pack_key, int(quantity_text))
        if not result.get("ok"):
            await query.answer("Purchase unavailable.", show_alert=True)
            user = await database.get_user(query.from_user.id) or {}
            packs = await database.get_shop_packs()
            pack = packs.get(result.get("pack_key", pack_key), packs.get(pack_key))
            text = (
                shop_pack_text(pack, int(user.get("coins", 0))) + f"\n\n⚠️ {result['reason']}"
                if pack
                else shop_text(int(user.get("coins", 0)), packs) + f"\n\n⚠️ {result['reason']}"
            )
            try:
                await query.message.edit_text(
                    text,
                    reply_markup=shop_pack_keyboard(pack_key, pack) if pack else shop_keyboard(packs),
                )
            except Exception:
                await query.message.edit_caption(
                    caption=text,
                    reply_markup=shop_pack_keyboard(pack_key, pack) if pack else shop_keyboard(packs),
                )
            return

        await query.answer("Pack opened.")
        packs = await database.get_shop_packs()
        result_text = _pack_result_text(result)
        pack = packs.get(pack_key)
        try:
            await query.message.edit_text(result_text, reply_markup=shop_pack_keyboard(pack_key, pack) if pack else shop_keyboard(packs))
        except Exception:
            await query.message.edit_caption(caption=result_text, reply_markup=shop_pack_keyboard(pack_key, pack) if pack else shop_keyboard(packs))
        for index, player in enumerate(result["cards"], 1):
            await _send_card(
                bot,
                database,
                query.message,
                player,
                caption=f"<b>PACK CARD {index}/{result['quantity']}</b>\n\n{card_text(player)}",
                reply_markup=shop_pack_keyboard(pack_key, pack) if pack else shop_keyboard(packs),
            )

    @bot.on_message(filters.command("collection"))
    async def collection_handler(_: Client, message: Message) -> None:
        await database.get_or_create_user(message.from_user)
        await message.reply_text(await collection_text(database, message.from_user.id), reply_markup=back_keyboard())

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
        results = results[:MAX_PLAYER_SEARCH_RESULTS]
        token = _search_token(search)
        await _reply_premium_text(
            message,
            _player_search_text(search, results),
            reply_markup=_player_results_keyboard(token, results),
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
            _profile_text(user, players, message.from_user.first_name),
            reply_markup=back_keyboard(),
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
                    True,
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
                _profile_text(user, players, query.from_user.first_name),
                reply_markup=back_keyboard(),
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
                await _send_card(bot, database, query.message, player, premium_caption=True)
            else:
                await query.message.edit_text("This claim is no longer available.")
            return
        try:
            await query.message.edit_caption(caption=text, reply_markup=None)
        except Exception:
            await query.message.edit_text(text)

    @bot.on_callback_query(filters.regex(r"^playercard:[A-Za-z0-9_-]+:[0-9]+$"))
    async def player_card_handler(_: Client, query: CallbackQuery) -> None:
        _, token, page_text = query.data.split(":")
        try:
            search = _search_query(token)
            page = int(page_text)
        except (ValueError, UnicodeDecodeError, binascii.Error):
            await query.answer("That search has expired.", show_alert=True)
            return
        results = (await database.search_players(search))[:MAX_PLAYER_SEARCH_RESULTS]
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
            reply_markup=_player_card_keyboard(token),
            premium_caption=True,
        )

    @bot.on_callback_query(filters.regex(r"^playerresults:[A-Za-z0-9_-]+$"))
    async def player_results_handler(_: Client, query: CallbackQuery) -> None:
        token = query.data.split(":", 1)[1]
        try:
            search = _search_query(token)
        except (ValueError, UnicodeDecodeError, binascii.Error):
            await query.answer("That search has expired.", show_alert=True)
            return
        results = (await database.search_players(search))[:MAX_PLAYER_SEARCH_RESULTS]
        if not results:
            await query.answer("That search has expired.", show_alert=True)
            return
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass
        await _reply_premium_text(
            query.message,
            _player_search_text(search, results),
            reply_markup=_player_results_keyboard(token, results),
        )
