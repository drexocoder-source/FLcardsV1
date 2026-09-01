from __future__ import annotations

import asyncio
import html
import re
from uuid import uuid4

from pyrogram import Client, filters
from pyrogram.enums import ButtonStyle, ChatType
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import Settings
from database.mongo import MongoDatabase
from plugins.audit import audit
from services.match_summary import render_match_summary



async def _is_admin(user_id: int, database: MongoDatabase, settings: Settings) -> bool:
    return user_id in settings.owner_ids or await database.is_mod(user_id)


async def _permission_level(user_id: int, database: MongoDatabase, settings: Settings) -> int:
    if user_id in settings.owner_ids:
        return 3
    return await database.mod_level(user_id)


async def _has_level(user_id: int, database: MongoDatabase, settings: Settings, required: int) -> bool:
    return await _permission_level(user_id, database, settings) >= required


def _is_owner(user_id: int, settings: Settings) -> bool:
    return user_id in settings.owner_ids


def _is_private(message: Message) -> bool:
    chat_type = message.chat.type
    return chat_type == ChatType.PRIVATE or str(chat_type).lower().split(".")[-1] == "private"


def _is_group_chat(message: Message) -> bool:
    chat_type = message.chat.type
    return chat_type in (ChatType.GROUP, ChatType.SUPERGROUP) or str(chat_type).lower().split(".")[-1] in {
        "group",
        "supergroup",
    }


def _owner_private(message: Message, settings: Settings) -> bool:
    return _is_private(message) and _is_owner(message.from_user.id, settings)


def _reset_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🧨 Reset entire database", callback_data="reset:all", style=ButtonStyle.DANGER)],
            [InlineKeyboardButton("👤 Reset one user's stats", callback_data="reset:user", style=ButtonStyle.PRIMARY)],
            [InlineKeyboardButton("Cancel", callback_data="reset:cancel", style=ButtonStyle.SUCCESS)],
        ]
    )


def _reset_all_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🛑 Confirm full reset", callback_data="reset:all_confirm", style=ButtonStyle.DANGER),
            InlineKeyboardButton("Cancel", callback_data="reset:cancel", style=ButtonStyle.SUCCESS),
        ]]
    )


def _admin_page_keyboard(page: int, total: int, page_size: int) -> InlineKeyboardMarkup | None:
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("⬅️ Back", callback_data=f"adminplayers:{page - 1}", style=ButtonStyle.PRIMARY))
    if (page + 1) * page_size < total:
        buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"adminplayers:{page + 1}", style=ButtonStyle.PRIMARY))
    return InlineKeyboardMarkup([buttons]) if buttons else None


async def _player_database_page(database: MongoDatabase, page: int, page_size: int = 12) -> tuple[str, int]:
    total = await database.count_players()
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    players = await database.list_players(skip=page * page_size, limit=page_size)
    lines = [
        "<b>PLAYER DATABASE</b>",
        f"Cards: <b>{total}</b> · Page <b>{page + 1}/{pages}</b>",
        "",
    ]
    if not players:
        lines.append("No player cards have been added yet.")
    else:
        for index, player in enumerate(players, page * page_size + 1):
            lines.append(
                f"{index}. <b>{html.escape(str(player.get('name', 'Unknown')))}</b>"
                f" · {html.escape(str(player.get('club', 'Free Agent')))}"
                f" · {player.get('position', 'MID')} · OVR {player.get('ovr', 0)}"
            )
    lines.extend(["", "Competition-only mode rosters are hidden from this browser."])
    return "\n".join(lines), page


def _player_from_parts(parts: list[str]) -> dict:
    if len(parts) != 18:
        raise ValueError(f"Expected 18 fields, received {len(parts)}")
    (
        name,
        nation,
        club,
        position,
        secondary_positions,
        rarity,
        ovr,
        pace,
        shooting,
        passing,
        dribbling,
        defending,
        physical,
        preferred_foot,
        weak_foot,
        skill_moves,
        height,
        traits,
    ) = parts[:18]
    return {
        "player_id": f"custom-{uuid4().hex[:10]}",
        "name": name,
        "nation": nation or "🌐",
        "club": club,
        "position": position.upper(),
        "secondary_positions": [item.strip().upper() for item in secondary_positions.split(",") if item.strip()],
        "rarity": rarity.upper() or "RARE",
        "ovr": int(ovr),
        "pace": int(pace),
        "shooting": int(shooting),
        "passing": int(passing),
        "dribbling": int(dribbling),
        "defending": int(defending),
        "physical": int(physical),
        "preferred_foot": preferred_foot or "Right",
        "weak_foot": int(weak_foot),
        "skill_moves": int(skill_moves),
        "height": height,
        "traits": [item.strip() for item in traits.split(",") if item.strip()],
    }


async def _bulk_import(
    bot: Client,
    database: MongoDatabase,
    settings: Settings,
    message: Message,
    source: str,
) -> None:
    if not source.strip() and message.reply_to_message:
        source = message.reply_to_message.text or message.reply_to_message.caption or ""
    lines = [
        line.strip()
        for line in source.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines:
        await message.reply_text(
            "<b>No player rows found.</b>\n\nUse /templateguide for the 18-field template. Send multiple rows on separate lines, or reply to a message containing them.",
        )
        return

    progress = await message.reply_text(
        f"<b>PLAYER IMPORT</b>\n\n🔎 Analyzing <b>0/{len(lines)}</b> rows...\nPlease keep this message open for the live import report."
    )
    added_names: list[str] = []
    duplicate_names: list[str] = []
    invalid_rows: list[str] = []

    for index, line in enumerate(lines, 1):
        try:
            player = _player_from_parts([re.sub(r"\s+", " ", part.strip()) for part in line.split("|")])
            if await database.add_player_if_new(player):
                added_names.append(player["name"])
            else:
                duplicate_names.append(player["name"])
        except (ValueError, IndexError) as exc:
            invalid_rows.append(f"Row {index}: {str(exc)}")

        await progress.edit_text(
            f"<b>PLAYER IMPORT</b>\n\n"
            f"⏳ Processing <b>{index}/{len(lines)}</b>\n"
            f"✅ Added: <b>{len(added_names)}</b> · ♻️ Existing: <b>{len(duplicate_names)}</b> · ⚠️ Invalid: <b>{len(invalid_rows)}</b>"
        )
        await asyncio.sleep(0)

    report = [
        "<b>PLAYER IMPORT COMPLETE</b>",
        "",
        f"✅ Added: <b>{len(added_names)}</b>",
        f"♻️ Existing cards skipped: <b>{len(duplicate_names)}</b>",
        f"⚠️ Invalid rows: <b>{len(invalid_rows)}</b>",
    ]
    if added_names:
        report.extend(["", "<b>Added</b>", " · ".join(added_names[:20])])
    if duplicate_names:
        report.extend(["", "<b>Skipped as existing</b>", " · ".join(duplicate_names[:20])])
    if invalid_rows:
        report.extend(["", "<b>Rows needing correction</b>", *invalid_rows[:10]])
        if len(invalid_rows) > 10:
            report.append(f"…and {len(invalid_rows) - 10} more invalid rows.")
    report.extend(["", "Use /templateguide to review the exact 18-field format."])
    await progress.edit_text("\n".join(report))
    await audit(
        bot,
        settings,
        f"Player import by <code>{message.from_user.id}</code>: {len(added_names)} added, {len(duplicate_names)} existing, {len(invalid_rows)} invalid",
    )


def register_admin_handlers(bot: Client, database: MongoDatabase, settings: Settings) -> None:
    @bot.on_message(filters.command("owner"))
    async def owner_handler(_: Client, message: Message) -> None:
        if not _is_private(message):
            await message.reply_text("Owner controls are available only in the owner's private chat.")
            return
        if not _is_owner(message.from_user.id, settings):
            await message.reply_text("This command is owner-only.")
            return
        await message.reply_text(
            """<b>🛠 OWNER CONTROL ROOM</b>

<b>Private owner commands</b>
/resetall CONFIRM — permanently clear the bot database
/players — browse every player card (private or group)
/botinfo — show bot-wide statistics
/addplayer · /addplayers — import player cards
/addtemplate · /template · /templates · /templateguide — manage card art
/shopprice — edit card pack prices
/tplayer · /editionplayer — add special/limited cards
/addcompetition · /addteam · /editteam · /deleteteam — manage arena data
/mods · /addmod · /removemod — manage level 1/2 moderator access

<b>Group player commands</b>
/arena · /playucl · /playwc · /playacl · /challenge

Use owner tools in this private chat. Arena and challenge commands belong in groups.""",
        )

    @bot.on_message(filters.command("resetall"))
    async def reset_all_handler(_: Client, message: Message) -> None:
        if not _is_private(message):
            await message.reply_text("The reset command can only run in the owner's private chat.")
            return
        if not _is_owner(message.from_user.id, settings):
            await message.reply_text("This command is owner-only.")
            return
        confirmation = message.text.partition(" ")[2].strip().upper()
        if confirmation != "CONFIRM":
            await message.reply_text(
                "<b>⚠️ RESET ALL</b>\n\n"
                "This permanently deletes every player, user collection, template, competition, match, challenge, and moderator record.\n\n"
                "Nothing is deleted yet. Choose an option below, or send:\n"
                "<code>/resetall CONFIRM</code>",
                reply_markup=_reset_menu_keyboard(),
            )
            return
        counts = await database.reset_all()
        details = " · ".join(f"{name}: {count}" for name, count in counts.items())
        await message.reply_text(f"<b>DATABASE RESET COMPLETE</b>\n\nDeleted records — {details}")
        await audit(bot, settings, f"Full database reset by owner <code>{message.from_user.id}</code>: {details}")

    @bot.on_callback_query(filters.regex(r"^reset:(all|all_confirm|user|cancel)$"))
    async def reset_menu_handler(_: Client, query: CallbackQuery) -> None:
        if not _is_private(query.message) or not _is_owner(query.from_user.id, settings):
            await query.answer("Owner access required in private chat.", show_alert=True)
            return
        action = query.data.split(":")[1]
        if action == "cancel":
            await query.answer("Reset cancelled.")
            await query.message.edit_text("No data was changed.")
        elif action == "all":
            await query.answer()
            await query.message.edit_text(
                "<b>⚠️ CONFIRM FULL DATABASE RESET</b>\n\n"
                "This deletes all users, cards, templates, competitions, matches, challenges, moderators, shop purchases, and saved shop prices.",
                reply_markup=_reset_all_confirm_keyboard(),
            )
        elif action == "all_confirm":
            counts = await database.reset_all()
            details = " · ".join(f"{name}: {count}" for name, count in counts.items())
            await query.answer("Database reset complete.")
            await query.message.edit_text(f"<b>DATABASE RESET COMPLETE</b>\n\nDeleted records — {details}")
            await audit(bot, settings, f"Full database reset by owner <code>{query.from_user.id}</code>: {details}")
        else:
            await query.answer()
            await query.message.edit_text(
                "<b>👤 RESET ONE USER</b>\n\n"
                "This clears that manager's coins, glory, XP, cards, squad, cooldowns, team settings, and match record.\n"
                "The account identity stays in place.\n\n"
                "Use <code>/resetuser USER_ID CONFIRM</code>, or reply to their message with <code>/resetuser CONFIRM</code>."
            )

    @bot.on_message(filters.command("resetuser"))
    async def reset_user_handler(_: Client, message: Message) -> None:
        if not _owner_private(message, settings):
            await message.reply_text("This command is owner-only in private chat.")
            return
        raw_parts = message.text.partition(" ")[2].strip().split()
        target = message.reply_to_message.from_user if message.reply_to_message else None
        confirmation = "CONFIRM" in {part.upper() for part in raw_parts}
        try:
            target_id = target.id if target else int(next(part for part in raw_parts if part.upper() != "CONFIRM"))
        except (ValueError, StopIteration):
            await message.reply_text(
                "Use <code>/resetuser USER_ID CONFIRM</code>, or reply to the user's message with <code>/resetuser CONFIRM</code>."
            )
            return
        if not confirmation:
            await message.reply_text(
                f"This will permanently clear user <code>{target_id}</code>'s cards and game stats. "
                f"Send <code>/resetuser {target_id} CONFIRM</code> to continue."
            )
            return
        if not await database.reset_user_stats(target_id):
            await message.reply_text("That user does not have a saved account.")
            return
        await message.reply_text(f"✅ User <code>{target_id}</code> stats and collection were reset.")
        await audit(bot, settings, f"User stats reset by owner <code>{message.from_user.id}</code>: <code>{target_id}</code>")

    @bot.on_message(filters.command("admin"))
    async def admin_handler(_: Client, message: Message) -> None:
        if not _is_private(message):
            await message.reply_text("Admin controls are available only in private chat.")
            return
        if not await _has_level(message.from_user.id, database, settings, 1):
            await message.reply_text("This command is for club administrators.")
            return
        await message.reply_text(
            """<b>ADMIN CONTROL ROOM</b>

<b>Level 1 · Player database</b>
/players
/addplayer · /addplayers

<b>Level 2 · Templates and arena data</b>
/addtemplate · /template · /templates · /templateguide
/addcompetition · /addteam · /editteam · /deleteteam

<b>Access controls</b>
/mods (owner and moderators)

<b>Owner-only</b>
/addmod · /removemod · /resetall · /resetuser · /tplayer · /editionplayer · /botinfo

Keep the player database and card artwork separate so new card templates can be added safely later.""",
        )

    @bot.on_message(filters.command("seedplayers"))
    async def seed_handler(_: Client, message: Message) -> None:
        if not _owner_private(message, settings):
            return
        await database.seed_mode_catalog()
        await message.reply_text(
            "Built-in UCL, World Cup, and ACL modes are configured with five named teams each. "
            "Use /playucl, /playwc, or /playacl in a group. Add another mode with /addcompetition."
        )

    @bot.on_message(filters.command("players"))
    async def players_handler(_: Client, message: Message) -> None:
        if not (_is_private(message) or _is_group_chat(message)) or not await _has_level(message.from_user.id, database, settings, 1):
            return
        raw_page = message.text.partition(" ")[2].strip()
        try:
            requested_page = max(0, int(raw_page) - 1) if raw_page else 0
        except ValueError:
            requested_page = 0
        text, page = await _player_database_page(database, requested_page)
        await message.reply_text(text, reply_markup=_admin_page_keyboard(page, await database.count_players(), 12))

    @bot.on_callback_query(filters.regex(r"^adminplayers:[0-9]+$"))
    async def player_database_page_handler(_: Client, query: CallbackQuery) -> None:
        if not (_is_private(query.message) or _is_group_chat(query.message)) or not await _has_level(query.from_user.id, database, settings, 1):
            await query.answer("Administrator access required.", show_alert=True)
            return
        try:
            requested_page = int(query.data.split(":", 1)[1])
        except ValueError:
            await query.answer("That page is no longer available.", show_alert=True)
            return
        text, page = await _player_database_page(database, requested_page)
        await query.answer()
        await query.message.edit_text(
            text,
            reply_markup=_admin_page_keyboard(page, await database.count_players(), 12),
        )

    @bot.on_message(filters.command("botinfo"))
    async def bot_info_handler(_: Client, message: Message) -> None:
        if not _owner_private(message, settings):
            await message.reply_text("This command is owner-only in private chat.")
            return
        stats = await database.get_bot_stats()
        await message.reply_text(
            f"""<b>📊 BOT INFORMATION</b>

<b>Community</b>
👥 Total users: <b>{stats['users']:,}</b>
⚽ Clubs with a squad: <b>{stats['users_with_squads']:,}</b>
🃏 Collected cards: <b>{stats['collected_cards']:,}</b>
🪙 Coins in circulation: <b>{stats['coins']:,}</b>
✨ XP earned: <b>{stats['xp']:,}</b>

<b>Card database</b>
🎴 Total cards: <b>{stats['players']:,}</b>
✅ Claimable cards: <b>{stats['collectible_players']:,}</b>
🏆 Competition-only cards: <b>{stats['competition_players']:,}</b>
🎨 Templates: <b>{stats['templates']:,}</b>

<b>Arena and access</b>
🏟 Competitions: <b>{stats['competitions']:,}</b>
⚽ Teams: <b>{stats['teams']:,}</b>
🎮 Group games: <b>{stats['group_games']:,}</b> · Active <b>{stats['active_group_games']:,}</b>
⚔️ Challenges: <b>{stats['challenges']:,}</b> · Active <b>{stats['active_challenges']:,}</b>
📋 Saved matches: <b>{stats['matches']:,}</b>
🛡 Moderators: <b>{stats['moderators']:,}</b>""",
        )

    @bot.on_message(filters.command("shopprice"))
    async def shop_price_handler(_: Client, message: Message) -> None:
        if not _owner_private(message, settings):
            await message.reply_text("This command is owner-only in private chat.")
            return
        parts = [part.strip() for part in message.text.partition(" ")[2].split("|") if part.strip()]
        packs = await database.get_shop_packs()
        if not parts:
            prices = "\n".join(
                f"{pack['emoji']} <b>{key.title()}</b>: <b>{pack['price']:,}</b> coins"
                for key, pack in packs.items()
            )
            await message.reply_text(
                f"<b>SHOP PRICES</b>\n\n{prices}\n\n"
                "Edit one with <code>/shopprice COMMON | 1500</code>."
            )
            return
        if len(parts) != 2:
            await message.reply_text("Use <code>/shopprice RARITY | PRICE</code>.")
            return
        pack_key, price_text = parts
        try:
            price = int(price_text.replace(",", ""))
        except ValueError:
            await message.reply_text("The price must be a whole number greater than zero.")
            return
        if pack_key.upper() not in packs or price < 1:
            await message.reply_text("Choose a valid pack rarity and a price greater than zero.")
            return
        await database.set_shop_price(pack_key, price, message.from_user.id)
        await message.reply_text(
            f"✅ {pack_key.upper().title()} Pack now costs <b>{price:,}</b> coins per pack.\n"
            "The new price is saved and appears on the shop buttons."
        )
        await audit(
            bot,
            settings,
            f"Shop price updated by owner <code>{message.from_user.id}</code>: {pack_key.upper()} = {price}",
        )

    @bot.on_message(filters.command("addplayer"))
    async def add_player_handler(_: Client, message: Message) -> None:
        if not _is_private(message) or not await _has_level(message.from_user.id, database, settings, 1):
            await message.reply_text("This command is for club administrators.")
            return
        raw = message.text.partition(" ")[2].strip()
        await _bulk_import(bot, database, settings, message, raw)

    @bot.on_message(filters.command("addplayers"))
    async def add_players_handler(_: Client, message: Message) -> None:
        if not _is_private(message) or not await _has_level(message.from_user.id, database, settings, 1):
            return
        source = message.reply_to_message.text or message.reply_to_message.caption if message.reply_to_message else message.text.partition(" ")[2]
        await _bulk_import(bot, database, settings, message, source or "")

    @bot.on_message(filters.command("addcompetition"))
    async def add_competition_handler(_: Client, message: Message) -> None:
        if not _is_private(message) or not await _has_level(message.from_user.id, database, settings, 2):
            await message.reply_text("Level 2 moderator or owner access is required for competitions.")
            return
        parts = [part.strip() for part in message.text.partition(" ")[2].split("|")]
        if len(parts) != 4:
            await message.reply_text(
                "<b>Competition format</b>\n\n<code>/addcompetition key | Competition name | Emoji | CLUB or NATIONAL</code>\n\nExample:\n<code>/addcompetition copa-libertadores | Copa Libertadores | 🏆 | CLUB</code>"
            )
            return
        key = re.sub(r"[^a-z0-9_-]+", "-", parts[0].lower()).strip("-_")
        team_type = parts[3].lower()
        if not key or team_type not in {"club", "national"}:
            await message.reply_text("Use a simple key and choose either CLUB or NATIONAL.")
            return
        created = await database.add_competition(
            {
                "competition_key": key,
                "name": parts[1],
                "emoji": parts[2] or "🏆",
                "short_name": key.upper()[:8],
                "team_type": team_type,
            }
        )
        status = "created" if created else "already exists"
        await message.reply_text(
            f"🏆 Competition <b>{parts[1]}</b> {status}.\n\nNow add selectable teams with /addteam.",
        )
        await audit(bot, settings, f"Competition {status}: <b>{parts[1]}</b> by <code>{message.from_user.id}</code>")

    @bot.on_message(filters.command("addteam"))
    async def add_team_handler(_: Client, message: Message) -> None:
        if not _is_private(message) or not await _has_level(message.from_user.id, database, settings, 2):
            await message.reply_text("Level 2 moderator or owner access is required for competition teams.")
            return
        parts = [part.strip() for part in message.text.partition(" ")[2].split("|")]
        if len(parts) not in {4, 5, 6}:
            await message.reply_text(
                "<b>Team format</b>\n\n<code>/addteam competition_key | team-key | Team name | Rating | Emoji | Player names</code>\n\nEmoji and the comma-separated player roster are optional."
            )
            return
        competition_key, team_key, team_name, rating_text = parts[:4]
        competition = await database.get_competition(competition_key.lower())
        try:
            rating = max(1, min(99, int(rating_text)))
        except ValueError:
            rating = 75
        if not competition:
            await message.reply_text("That competition does not exist. Create it first with /addcompetition.")
            return
        team = {
            "team_key": re.sub(r"[^a-z0-9_-]+", "-", team_key.lower()).strip("-_"),
            "name": team_name,
            "rating": rating,
            "emoji": parts[4] if len(parts) >= 5 and parts[4] else "⚽",
        }
        if len(parts) == 6 and parts[5]:
            names = [name.strip() for name in parts[5].split(",") if name.strip()]
            roster = await database.players.find({"name": {"$in": names}}).to_list(length=25)
            team["player_ids"] = [player["player_id"] for player in roster]
        if not team["team_key"] or not team["name"]:
            await message.reply_text("A team key and team name are required.")
            return
        created = await database.add_competition_team(competition_key.lower(), team)
        status = "added" if created else "already exists"
        await message.reply_text(
            f"⚽ Team <b>{team['name']}</b> {status} in <b>{competition['name']}</b>.",
        )
        await audit(bot, settings, f"Competition team {status}: <b>{team['name']}</b> by <code>{message.from_user.id}</code>")

    @bot.on_message(filters.command("editteam"))
    async def edit_team_handler(_: Client, message: Message) -> None:
        if not _is_private(message) or not await _has_level(message.from_user.id, database, settings, 2):
            await message.reply_text("Level 2 moderator or owner access is required for competition teams.")
            return
        parts = [part.strip() for part in message.text.partition(" ")[2].split("|")]
        if len(parts) < 3:
            await message.reply_text("<code>/editteam competition_key | team-key | New name | Rating | Emoji</code>")
            return
        competition_key, team_key = parts[:2]
        updates = {"name": parts[2]}
        if len(parts) > 3 and parts[3]:
            try:
                updates["rating"] = max(1, min(99, int(parts[3])))
            except ValueError:
                pass
        if len(parts) > 4 and parts[4]:
            updates["emoji"] = parts[4]
        if len(parts) > 5 and parts[5]:
            names = [name.strip() for name in parts[5].split(",") if name.strip()]
            roster = await database.players.find({"name": {"$in": names}}).to_list(length=25)
            updates["player_ids"] = [player["player_id"] for player in roster]
        changed = await database.update_competition_team(competition_key.lower(), team_key.lower(), updates)
        await message.reply_text("Team updated." if changed else "That team was not found.")

    @bot.on_message(filters.command("deleteteam"))
    async def delete_team_handler(_: Client, message: Message) -> None:
        if not _is_private(message) or not await _has_level(message.from_user.id, database, settings, 2):
            await message.reply_text("Level 2 moderator or owner access is required for competition teams.")
            return
        parts = [part.strip() for part in message.text.partition(" ")[2].split("|")]
        if len(parts) != 2:
            await message.reply_text("<code>/deleteteam competition_key | team-key</code>")
            return
        deleted = await database.remove_competition_team(parts[0].lower(), parts[1].lower())
        await message.reply_text("Team deleted." if deleted else "That team was not found.")

    @bot.on_message(filters.command("tplayer"))
    async def photo_player_handler(_: Client, message: Message) -> None:
        if not _owner_private(message, settings):
            return
        reply = message.reply_to_message
        raw_name = message.text.partition(" ")[2].strip()
        name, separator, edition = raw_name.partition("|")
        name = name.strip()
        edition = edition.strip().upper() if separator and edition.strip() else "SPECIAL"
        if not reply or not reply.photo or not name:
            await message.reply_text("Reply to a finished card image with <code>/tplayer Player Name | POTW</code>.")
            return
        player = {
            "player_id": f"photo-{uuid4().hex[:10]}",
            "name": name[:60],
            "nation": "🌐",
            "club": edition,
            "position": "ST",
            "secondary_positions": ["CF", "ATT"],
            "edition": edition,
            "ovr": 99,
            "pace": 99,
            "shooting": 99,
            "passing": 99,
            "dribbling": 99,
            "defending": 50,
            "physical": 90,
            "preferred_foot": "Right",
            "weak_foot": 5,
            "skill_moves": 5,
            "height": "",
            "traits": [f"{edition} Edition"],
            "card_photo_file_id": reply.photo.file_id,
            "created_by": message.from_user.id,
        }
        await database.add_player(player)
        await message.reply_text(
            f"Photo card <b>{name}</b> added as <b>{edition}</b>. "
            "It will be delivered as the original image without rendering."
        )

    @bot.on_message(filters.command("editionplayer"))
    async def edition_player_handler(_: Client, message: Message) -> None:
        if not _is_private(message) or not await _has_level(message.from_user.id, database, settings, 2):
            await message.reply_text("Level 2 moderator or owner access is required to add edition cards.")
            return
        parts = [part.strip() for part in message.text.partition(" ")[2].split("|")]
        if len(parts) != 17:
            await message.reply_text(
                "<b>Edition player format</b>\n\n"
                "<code>/editionplayer Name | Nation | Club | Position | Edition | OVR | PAC | SHO | PAS | DRI | DEF | PHY | Foot | Weak foot | Skill moves | Height | Traits</code>\n\n"
                "Example edition names: POTW, POTY, TOTY, UCL TOTY."
            )
            return
        try:
            player = {
                "player_id": f"edition-{uuid4().hex[:10]}",
                "name": parts[0][:60],
                "nation": parts[1] or "🌐",
                "club": parts[2] or "Special Edition",
                "position": parts[3].upper(),
                "edition": parts[4].upper(),
                "ovr": int(parts[5]),
                "pace": int(parts[6]),
                "shooting": int(parts[7]),
                "passing": int(parts[8]),
                "dribbling": int(parts[9]),
                "defending": int(parts[10]),
                "physical": int(parts[11]),
                "preferred_foot": parts[12] or "Right",
                "weak_foot": int(parts[13]),
                "skill_moves": int(parts[14]),
                "height": parts[15],
                "traits": [item.strip() for item in parts[16].split(",") if item.strip()],
                "claimable": True,
                "card_type": "limited",
                "created_by": message.from_user.id,
            }
        except ValueError:
            await message.reply_text("OVR and all six stats must be whole numbers.")
            return
        if not player["name"] or not player["edition"]:
            await message.reply_text("A player name and edition name are required.")
            return
        await database.add_player(player)
        await message.reply_text(
            f"✅ <b>{player['name']}</b> added as the limited edition <b>{player['edition']}</b> card. "
            "This card intentionally has no rarity."
        )
        await audit(
            bot,
            settings,
            f"Edition player added: <b>{player['name']}</b> ({player['edition']}) by <code>{message.from_user.id}</code>",
        )

    @bot.on_message(filters.command("testms"))
    async def test_match_summary_handler(_: Client, message: Message) -> None:
        if not _owner_private(message, settings):
            return
        state = {
            "home": "Bengaluru FC",
            "away": "Mumbai City",
            "home_goals": 2,
            "away_goals": 1,
            "home_possession": 54,
            "home_shots": 13,
            "away_shots": 8,
            "home_shots_on_target": 7,
            "away_shots_on_target": 3,
            "home_corners": 6,
            "away_corners": 2,
            "events": [
                {"type": "goal", "side": "home", "minute": 18, "scorer_name": "Sunil Chhetri"},
                {"type": "goal", "side": "away", "minute": 51, "scorer_name": "Jorge Diaz"},
                {"type": "goal", "side": "home", "minute": 83, "scorer_name": "Ryan Williams"},
            ],
        }
        home_players = [
            {"name": "Sunil Chhetri", "ovr": 84},
            {"name": "Ryan Williams", "ovr": 82},
        ]
        away_players = [
            {"name": "Jorge Diaz", "ovr": 79},
            {"name": "Lalengmawia", "ovr": 77},
        ]
        image_path = None
        try:
            image_path = await asyncio.to_thread(
                render_match_summary,
                state,
                home_players,
                away_players,
                "TEST MATCH",
            )
            await message.reply_photo(
                image_path,
                caption="⚽ Test football match summary generated successfully.",
            )
        except Exception:
            await message.reply_text("The test summary could not be generated. Check the workflow logs.")
        finally:
            if image_path:
                try:
                    import os
                    os.unlink(image_path)
                except OSError:
                    pass

    @bot.on_message(filters.command("addmod"))
    async def add_mod_handler(_: Client, message: Message) -> None:
        if not _owner_private(message, settings):
            await message.reply_text("Only the owner can change moderator access.")
            return
        target = message.reply_to_message.from_user if message.reply_to_message else None
        raw_parts = message.text.partition(" ")[2].strip().split()
        try:
            target_id = target.id if target else int(raw_parts[0])
        except (ValueError, IndexError):
            await message.reply_text("Reply to a user or provide a numeric Telegram user ID.")
            return
        try:
            level = int(raw_parts[1]) if len(raw_parts) > 1 else 1
        except ValueError:
            level = 0
        if level not in {1, 2}:
            await message.reply_text(
                "<b>Moderator levels</b>\n\n"
                "Level 1: browse and add player cards.\n"
                "Level 2: level 1 plus templates and competition management.\n\n"
                "Use <code>/addmod USER_ID 1</code> or <code>/addmod USER_ID 2</code>."
            )
            return
        await database.add_mod(target_id, message.from_user.id, level)
        await message.reply_text(f"Level <b>{level}</b> moderator access granted to <code>{target_id}</code>.")
        await audit(bot, settings, f"Level {level} moderator added: <code>{target_id}</code> by owner <code>{message.from_user.id}</code>")

    @bot.on_message(filters.command("removemod"))
    async def remove_mod_handler(_: Client, message: Message) -> None:
        if not _owner_private(message, settings):
            return
        raw_id = message.text.partition(" ")[2].strip()
        target = message.reply_to_message.from_user if message.reply_to_message else None
        try:
            target_id = target.id if target else int(raw_id)
        except ValueError:
            await message.reply_text("Reply to a user or provide a numeric Telegram user ID.")
            return
        await database.remove_mod(target_id)
        await message.reply_text(f"Moderator access removed from <code>{target_id}</code>.")
        await audit(bot, settings, f"Moderator removed: <code>{target_id}</code> by owner <code>{message.from_user.id}</code>")

    @bot.on_message(filters.command("mods"))
    async def mods_handler(_: Client, message: Message) -> None:
        if not _is_private(message) or not await _has_level(message.from_user.id, database, settings, 1):
            return
        mods = await database.db.admins.find().sort("user_id", 1).to_list(length=100)
        lines = ["<b>MODERATORS</b>", f"Owner IDs: {', '.join(str(item) for item in settings.owner_ids)}"]
        lines.extend(f"• <code>{mod['user_id']}</code> · Level <b>{mod.get('level', 1)}</b>" for mod in mods)
        await message.reply_text("\n".join(lines))

    @bot.on_message(filters.command("templateguide"))
    async def template_guide_handler(_: Client, message: Message) -> None:
        if not _is_private(message) or not await _has_level(message.from_user.id, database, settings, 1):
            return
        await message.reply_text(
            """<b>CARD TEMPLATE GUIDE</b>

<b>Widescreen designer · 16:9 or 2:1</b>
The supplied red/black goalkeeper artwork is the reference style. Keep the background dark where text sits and leave the portrait area transparent or low contrast. The renderer keeps the uploaded image aspect ratio.

<b>Positions to add</b>
Use one template for each card position when the artwork changes by role:
<code>GK, CB, LB, RB, LWB, RWB, CDM, CM, CAM, LM, RM, LW, RW, CF, ST, SS</code>
You can also save <code>DEF</code>, <code>MID</code>, <code>ATT</code>, or <code>ALL</code> as a fallback template.

<b>GK layout coordinates (x, y)</b>
• OVR: (54, 42) · Position: (54, 122)
• Nation: (54, 176) · Club: (54, 220) · Rarity: (1070, 56)
• Portrait: (370, 88) → (930, 472)
• Player name: center (650, 506)
• Club/edition: center (650, 550)
• PAC / SHO / PAS: (54, 590), (214, 590), (374, 590)
• DRI / DEF / PHY: (760, 590), (920, 590), (1080, 590)

For CB, LB, and RB, use the same canvas and portrait zone, with DEF as the visual emphasis. For CDM, CM, and CAM, keep the portrait center and reserve the right side for PAS/DRI. For LW, RW, CF, and ST, reserve the right side for SHO/PAC. GK uses the attached goalkeeper layout.

<b>Safe zones</b>
Keep all text inside x=40..1240 and y=35..615. Use a 16:9 or 2:1 export consistently; this bot preserves a widescreen template's aspect ratio.

Reply to the finished image with:
<code>/addtemplate gk-wide | GK | RARE | Widescreen 2:1</code>

<b>Limited and special editions</b>
Use <code>/template ID | POSITION | EDITION | VERSION</code> for editions such as
<code>POTW</code>, <code>POTY</code>, <code>TOTY</code>, or <code>UCL TOTY</code>.
These templates and player cards use an edition label instead of a rarity.
Add a text-based edition card with <code>/editionplayer</code>, or add an original image with
<code>/tplayer Player Name | EDITION</code>.

<b>Bulk player template</b>
Use one player per line. Each line has exactly 18 pipe-separated fields:
<code>Name | Nation | Club | Position | Secondary positions | Rarity | OVR | PAC | SHO | PAS | DRI | DEF | PHY | Foot | Weak foot | Skill moves | Height | Traits</code>

You can send one line after <code>/addplayer</code>, or paste many lines after it. You can also send the lines as a separate message and reply to them with <code>/addplayer</code>.

The bot analyzes every row, shows live progress, adds valid new cards, skips duplicates by name + club, and reports invalid rows instead of silently ignoring them.

Example:
<code>/addplayer Lionel Messi | 🇦🇷 | Inter Miami | ATT | RW,CAM | ICONIC | 97 | 91 | 96 | 97 | 99 | 38 | 70 | Left | 4 | 5 | 170cm | Playmaker,Technical,Dead Ball</code>""",
        )

    @bot.on_message(filters.command("addtemplate"))
    async def add_template_handler(_: Client, message: Message) -> None:
        if not _is_private(message) or not await _has_level(message.from_user.id, database, settings, 2):
            await message.reply_text("Level 2 moderator or owner access is required to add templates.")
            return
        reply = message.reply_to_message
        if not reply or not reply.photo:
            await message.reply_text("Reply to a card image and use <code>/addtemplate ID | POSITION | RARITY | VERSION</code>.")
            return
        parts = [part.strip() for part in message.text.partition(" ")[2].split("|")]
        if len(parts) == 3:
            parts.insert(1, "ALL")
        if len(parts) < 4:
            await message.reply_text("Use <code>/addtemplate ID | POSITION | RARITY | VERSION</code>.")
            return
        is_widescreen = "wide" in parts[3].lower() or "2:1" in parts[3] or "16:9" in parts[3]
        source_width = int(getattr(reply.photo, "width", 1280) or 1280)
        source_height = int(getattr(reply.photo, "height", 640) or 640)
        template = {
            "template_id": parts[0],
            "position": parts[1].upper(),
            "rarity": parts[2].upper(),
            "version": parts[3],
            "aspect_ratio": f"{source_width}:{source_height}" if is_widescreen else "3:4",
            "canvas": {"width": source_width, "height": source_height} if is_widescreen else {"width": 720, "height": 960},
            "image_file_id": reply.photo.file_id,
            "layout": {
                "rating": "top-left",
                "nation": "top-right",
                "portrait": "center",
                "identity": "lower-center",
                "stats": "bottom",
                "traits": "bottom-strip",
                "coordinates": {
                    "rating": [54, 42],
                    "position": [54, 122],
                    "nation": [54, 176],
                    "club_top": [54, 220],
                    "rarity": [1070, 56],
                    "portrait": [370, 88, 930, 472],
                    "identity": [650, 506],
                    "club": [650, 550],
                    "stats": [54, 590],
                } if is_widescreen else {},
            },
            "created_by": message.from_user.id,
        }
        await database.save_template(template)
        await message.reply_text(f"Template <b>{template['template_id']}</b> saved.")
        await audit(bot, settings, f"Template added: <b>{template['template_id']}</b> by <code>{message.from_user.id}</code>")

    @bot.on_message(filters.command("template"))
    async def special_template_handler(_: Client, message: Message) -> None:
        if not _is_private(message) or not await _has_level(message.from_user.id, database, settings, 2):
            await message.reply_text("Level 2 moderator or owner access is required to add templates.")
            return
        reply = message.reply_to_message
        if not reply or not reply.photo:
            await message.reply_text(
                "Reply to a card image and use <code>/template ID | POSITION | EDITION | VERSION</code>."
            )
            return
        parts = [part.strip() for part in message.text.partition(" ")[2].split("|")]
        if len(parts) == 3:
            parts.insert(1, "ALL")
        if len(parts) < 4 or not parts[0] or not parts[2]:
            await message.reply_text(
                "Use <code>/template ID | POSITION | EDITION | VERSION</code> — for example "
                "<code>/template potw-st | ST | POTW | Limited 2:1</code>."
            )
            return
        edition = parts[2].upper()
        source_width = int(getattr(reply.photo, "width", 1280) or 1280)
        source_height = int(getattr(reply.photo, "height", 640) or 640)
        is_widescreen = "wide" in parts[3].lower() or "2:1" in parts[3] or "16:9" in parts[3]
        template = {
            "template_id": parts[0],
            "position": parts[1].upper(),
            "edition": edition,
            "version": parts[3],
            "aspect_ratio": f"{source_width}:{source_height}" if is_widescreen else "3:4",
            "canvas": {"width": source_width, "height": source_height} if is_widescreen else {"width": 720, "height": 960},
            "image_file_id": reply.photo.file_id,
            "layout": {
                "rating": "top-left",
                "nation": "top-right",
                "portrait": "center",
                "identity": "lower-center",
                "stats": "bottom",
                "traits": "bottom-strip",
            },
            "created_by": message.from_user.id,
        }
        await database.save_template(template)
        await message.reply_text(
            f"✅ Limited/special template <b>{template['template_id']}</b> saved for <b>{edition}</b> cards."
        )
        await audit(bot, settings, f"Edition template added: <b>{template['template_id']}</b> ({edition}) by <code>{message.from_user.id}</code>")

    @bot.on_message(filters.command("templates"))
    async def templates_handler(_: Client, message: Message) -> None:
        if not _is_private(message) or not await _has_level(message.from_user.id, database, settings, 1):
            return
        templates = await database.db.templates.find().sort("template_id", 1).to_list(length=100)
        if not templates:
            await message.reply_text("No card templates saved yet. Use /templateguide.")
            return
        lines = ["<b>CARD TEMPLATES</b>"]
        lines.extend(
            f"• <b>{item['template_id']}</b> · "
            f"{item.get('edition') or item.get('rarity', 'UNSPECIFIED')} · {item['version']}"
            for item in templates
        )
        await message.reply_text("\n".join(lines))
