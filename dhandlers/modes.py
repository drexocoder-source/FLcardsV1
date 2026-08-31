from __future__ import annotations

import asyncio
import random
from typing import Any

from pyrogram import Client, filters
from pyrogram.enums import ButtonStyle, ChatType
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import Settings
from database.mongo import MongoDatabase
from plugins.audit import audit
from services.ai import generate_match_summary
from services.match import (
    advance_live_state,
    finish_live_state,
    live_scorecard,
    new_live_state,
    scorecard,
    simulate_match,
)

from .ui import arena_keyboard, back_keyboard, challenge_keyboard


LIVE_TASKS: dict[str, asyncio.Task] = {}
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
TACTICS = ("Balanced", "Possession", "Counter", "Press")
MENTALITIES = ("Balanced", "Attacking", "Defensive")


def unlocked_formations(user: dict[str, Any]) -> list[str]:
    level = int(user.get("xp", 0)) // 1000 + 1
    return [formation for formation, required_level in FORMATIONS.items() if required_level <= level]


def _rating(players: list[dict[str, Any]], fallback: int = 75) -> int:
    return round(sum(player.get("ovr", fallback) for player in players) / max(len(players), 1))


def _is_group_chat(message: Message) -> bool:
    chat_type = message.chat.type
    return chat_type in (ChatType.GROUP, ChatType.SUPERGROUP) or str(chat_type).lower().split(".")[-1] in {
        "group",
        "supergroup",
    }


def _synthetic_team(team: dict[str, Any]) -> list[dict[str, Any]]:
    positions = ["GK", "DEF", "DEF", "DEF", "DEF", "MID", "MID", "MID", "ATT", "ATT", "ATT"]
    rating = int(team.get("rating", 75))
    return [
        {
            "player_id": f"team-{team.get('team_key', 'side')}-{index}",
            "name": f"{team.get('name', 'Team')} Player {index}",
            "position": position,
            "ovr": rating,
            "pace": rating,
            "shooting": rating,
            "passing": rating,
            "dribbling": rating,
            "defending": rating,
            "physical": rating,
        }
        for index, position in enumerate(positions, 1)
    ]


def _team_keyboard(competition: dict[str, Any], game_id: str) -> InlineKeyboardMarkup:
    teams = competition.get("teams", [])
    rows = [
        [
            InlineKeyboardButton(
                f"{team.get('emoji', '⚽')} {team['name']}",
                callback_data=f"game:team:{game_id}:{team['team_key']}",
                style=ButtonStyle.SUCCESS if competition.get("team_type") == "national" else ButtonStyle.PRIMARY,
            )
            for team in teams[index : index + 2]
        ]
        for index in range(0, len(teams), 2)
    ]
    rows.append([InlineKeyboardButton("Cancel lobby", callback_data=f"game:cancel:{game_id}", style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(rows)


def _lobby_keyboard(game_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🙋 Join match", callback_data=f"game:join:{game_id}", style=ButtonStyle.SUCCESS)],
            [InlineKeyboardButton("Cancel lobby", callback_data=f"game:cancel:{game_id}", style=ButtonStyle.DANGER)],
        ]
    )


def _pitch_keyboard(game_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🌱 Perfect", callback_data=f"game:pitch:{game_id}:perfect"),
                InlineKeyboardButton("💧 Wet", callback_data=f"game:pitch:{game_id}:wet"),
                InlineKeyboardButton("🪨 Heavy", callback_data=f"game:pitch:{game_id}:heavy"),
            ]
        ]
    )


def _weather_keyboard(game_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("☀️ Clear", callback_data=f"game:weather:{game_id}:clear"),
                InlineKeyboardButton("🌧 Rain", callback_data=f"game:weather:{game_id}:rain"),
                InlineKeyboardButton("🌬 Wind", callback_data=f"game:weather:{game_id}:wind"),
            ]
        ]
    )


def _formation_rows(prefix: str, game_id: str, formations: list[str]) -> list[list[InlineKeyboardButton]]:
    buttons = [InlineKeyboardButton(formation, callback_data=f"{prefix}:{game_id}:{formation}") for formation in formations]
    return [buttons[index : index + 3] for index in range(0, len(buttons), 3)]


def _challenge_setup_keyboard(challenge: dict[str, Any], user: dict[str, Any]) -> InlineKeyboardMarkup:
    side = "home" if challenge["challenger_id"] == user["user_id"] else "away"
    formations = unlocked_formations(user)
    rows = _formation_rows("challenge:formation", challenge["challenge_id"], formations)
    rows += [
        [
            InlineKeyboardButton(
                f"⚙️ {tactic}",
                callback_data=f"challenge:tactic:{challenge['challenge_id']}:{side}:{tactic}",
            )
            for tactic in TACTICS
        ],
        [
            InlineKeyboardButton(
                f"🧠 {mentality}",
                callback_data=f"challenge:mentality:{challenge['challenge_id']}:{side}:{mentality}",
            )
            for mentality in MENTALITIES
        ],
    ]
    lineup = challenge.get(f"{side}_lineup", [])[:11]
    if lineup:
        rows.append([InlineKeyboardButton("🎛 Player instructions", callback_data=f"challenge:instructions:{challenge['challenge_id']}:{side}")])
    rows.append([InlineKeyboardButton("✅ Ready", callback_data=f"challenge:ready:{challenge['challenge_id']}")])
    return InlineKeyboardMarkup(rows)


def _live_keyboard(challenge_id: str, side: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Possession", callback_data=f"live:{challenge_id}:tactic:{side}:Possession"),
                InlineKeyboardButton("Counter", callback_data=f"live:{challenge_id}:tactic:{side}:Counter"),
                InlineKeyboardButton("Press", callback_data=f"live:{challenge_id}:tactic:{side}:Press"),
            ],
            [
                InlineKeyboardButton("⚔️ Attack", callback_data=f"live:{challenge_id}:mentality:{side}:Attacking"),
                InlineKeyboardButton("⚖️ Balance", callback_data=f"live:{challenge_id}:mentality:{side}:Balanced"),
                InlineKeyboardButton("🛡 Defend", callback_data=f"live:{challenge_id}:mentality:{side}:Defensive"),
            ],
            [InlineKeyboardButton("🔁 Make a substitution", callback_data=f"live:{challenge_id}:sub:{side}")],
        ]
    )


def _combined_live_keyboard(challenge_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🏠 Possession", callback_data=f"live:{challenge_id}:tactic:home:Possession"),
                InlineKeyboardButton("🏠 Counter", callback_data=f"live:{challenge_id}:tactic:home:Counter"),
                InlineKeyboardButton("🏠 Press", callback_data=f"live:{challenge_id}:tactic:home:Press"),
            ],
            [
                InlineKeyboardButton("✈️ Possession", callback_data=f"live:{challenge_id}:tactic:away:Possession"),
                InlineKeyboardButton("✈️ Counter", callback_data=f"live:{challenge_id}:tactic:away:Counter"),
                InlineKeyboardButton("✈️ Press", callback_data=f"live:{challenge_id}:tactic:away:Press"),
            ],
            [
                InlineKeyboardButton("🏠 Attack", callback_data=f"live:{challenge_id}:mentality:home:Attacking"),
                InlineKeyboardButton("🏠 Balance", callback_data=f"live:{challenge_id}:mentality:home:Balanced"),
                InlineKeyboardButton("🏠 Defend", callback_data=f"live:{challenge_id}:mentality:home:Defensive"),
            ],
            [
                InlineKeyboardButton("✈️ Attack", callback_data=f"live:{challenge_id}:mentality:away:Attacking"),
                InlineKeyboardButton("✈️ Balance", callback_data=f"live:{challenge_id}:mentality:away:Balanced"),
                InlineKeyboardButton("✈️ Defend", callback_data=f"live:{challenge_id}:mentality:away:Defensive"),
            ],
            [
                InlineKeyboardButton("🔁 Home sub", callback_data=f"live:{challenge_id}:sub:home"),
                InlineKeyboardButton("🔁 Away sub", callback_data=f"live:{challenge_id}:sub:away"),
            ],
        ]
    )


def _setup_text(challenge: dict[str, Any]) -> str:
    return f"""<b>⚔️ MANAGER MATCH SETUP</b>

<b>{challenge['challenger_name']}</b> vs <b>{challenge['challenged_name']}</b>

Each manager chooses a formation, tactic, mentality, and starting player instructions. During the match you can change tactics, mentality, and make substitutions every match window.

{challenge['challenger_name']}: {challenge.get('home_formation', '4-3-3')} · {challenge.get('home_tactic', 'Balanced')} · {challenge.get('home_mentality', 'Balanced')} · {"READY" if challenge.get("home_ready") else "choosing"}
{challenge['challenged_name']}: {challenge.get('away_formation', '4-3-3')} · {challenge.get('away_tactic', 'Balanced')} · {challenge.get('away_mentality', 'Balanced')} · {"READY" if challenge.get("away_ready") else "choosing"}

Managers: use your own controls below. The collected squads are used for this challenge."""


def _live_text(state: dict[str, Any], latest: str) -> str:
    commentary = "\n".join(f"• {line}" for line in state.get("commentary", [])[-5:])
    return f"""<b>🔴 LIVE MANAGER MATCH · {state['minute']}'</b>

🟦 <b>{state['home']}</b>  <b>{state['home_goals']}</b>
🟥 <b>{state['away']}</b>  <b>{state['away_goals']}</b>

<b>Latest update</b>
<blockquote expandable>{latest}</blockquote>

<b>Commentator</b>
<blockquote expandable>{commentary}</blockquote>

Use your manager controls for the next 5–6 minute window."""


def _mode_text(game: dict[str, Any], competition: dict[str, Any]) -> str:
    host = game.get("host_name", "Manager A")
    opponent = game.get("opponent_name")
    if game["status"] == "lobby":
        return f"""<b>{competition.get('emoji', '🏆')} {competition['name']}</b>

<b>{host}</b> opened the group match lobby.
One manager joins as the opponent. This group has one active lobby for all modes.

Mode: <b>/{game['mode']}</b>"""
    if game["status"] == "setup":
        phase = game.get("phase", "pitch")
        if phase == "pitch":
            return f"<b>{competition['name']}</b>\n\n<b>{host}</b>, choose the pitch condition."
        if phase == "weather":
            return f"<b>{competition['name']}</b>\n\n<b>{host}</b>, choose the weather."
        if phase == "team_host":
            return f"<b>{competition['name']}</b>\n\n<b>{host}</b>, choose your team."
        if phase == "team_away":
            return f"<b>{competition['name']}</b>\n\n<b>{opponent}</b>, choose your team."
        if phase == "formation_host":
            return f"<b>{competition['name']}</b>\n\n<b>{host}</b>, choose your formation and set your lineup."
        if phase == "formation_away":
            return f"<b>{competition['name']}</b>\n\n<b>{opponent}</b>, choose your formation and set your lineup."
        return f"<b>{competition['name']}</b>\n\nTeams and conditions are ready. The match can start."
    return f"<b>{competition['name']}</b>\n\nThe match is being managed live."


async def _team_players(database: MongoDatabase, competition: dict[str, Any], team_key: str) -> list[dict[str, Any]]:
    team = next((item for item in competition.get("teams", []) if item.get("team_key") == team_key), None)
    if not team:
        return []
    players = await database.get_team_players(team, competition.get("team_type", "club"))
    return players if len(players) >= 11 else _synthetic_team(team)


async def _run_group_match(bot: Client, database: MongoDatabase, settings: Settings, game_id: str) -> None:
    game = await database.get_group_game(game_id)
    if not game:
        return
    competition = await database.get_competition(game["mode"])
    if not competition:
        return
    home_players = await _team_players(database, competition, game["home_team"])
    away_players = await _team_players(database, competition, game["away_team"])
    state = new_live_state(
        game["home_team_name"],
        game["away_team_name"],
        _rating(home_players, game.get("home_rating", 75)),
        _rating(away_players, game.get("away_rating", 75)),
    )
    await database.update_group_game(game_id, {"status": "live", "live_state": state})
    while state["minute"] < 90:
        await asyncio.sleep(2)
        state, latest = advance_live_state(state, home_players, away_players)
        await database.update_group_game(game_id, {"live_state": state})
        try:
            await bot.edit_message_text(game["chat_id"], game["message_id"], _live_text(state, latest))
        except Exception:
            pass
    if state["home_goals"] == state["away_goals"]:
        finish_live_state(state, home_players, away_players)
    await database.finish_group_game(game_id, state)
    final = f"{live_scorecard(state)}\n\n<b>Commentator</b>\n<blockquote expandable>{chr(10).join(state.get('commentary', [])[-8:])}</blockquote>"
    try:
        await bot.edit_message_text(game["chat_id"], game["message_id"], final)
    except Exception:
        pass
    await audit(bot, settings, f"Group match finished: <b>{state['home']}</b> {state['home_goals']}-{state['away_goals']} <b>{state['away']}</b>")
    LIVE_TASKS.pop(game_id, None)


async def _start_group_match(bot: Client, database: MongoDatabase, settings: Settings, game: dict[str, Any], message: Message) -> None:
    await database.update_group_game(game["game_id"], {"phase": "live", "message_id": message.id})
    LIVE_TASKS[game["game_id"]] = asyncio.create_task(_run_group_match(bot, database, settings, game["game_id"]))
    await message.edit_text("<b>🔴 KICK-OFF</b>\n\nManagers are on the touchline. Live match updates will arrive every 5–6 minutes.")


async def _reward_challenge(database: MongoDatabase, challenge: dict[str, Any]) -> None:
    for user_id in (challenge["challenger_id"], challenge["challenged_id"]):
        user = await database.get_user(user_id) or {}
        await database.update_user(user_id, {"coins": user.get("coins", 0) + 1500, "xp": user.get("xp", 0) + 250})


async def _run_challenge(bot: Client, database: MongoDatabase, settings: Settings, challenge_id: str) -> None:
    challenge = await database.get_challenge(challenge_id)
    if not challenge:
        return
    home_players = await database.get_players(challenge.get("home_lineup", []))
    away_players = await database.get_players(challenge.get("away_lineup", []))
    home_all = await database.get_players(challenge.get("home_players", []))
    away_all = await database.get_players(challenge.get("away_players", []))
    state = challenge.get("live_state") or new_live_state(
        challenge["challenger_name"],
        challenge["challenged_name"],
        _rating(home_players),
        _rating(away_players),
    )
    while state["minute"] < 90:
        await asyncio.sleep(3)
        challenge = await database.get_challenge(challenge_id) or challenge
        home_players = await database.get_players(challenge.get("home_lineup", []))
        away_players = await database.get_players(challenge.get("away_lineup", []))
        state, latest = advance_live_state(
            state,
            home_players,
            away_players,
            challenge.get("home_tactic", "Balanced"),
            challenge.get("away_tactic", "Balanced"),
            challenge.get("home_mentality", "Balanced"),
            challenge.get("away_mentality", "Balanced"),
        )
        await database.update_challenge(challenge_id, {"live_state": state})
        try:
            await bot.edit_message_text(
                challenge["chat_id"],
                challenge["message_id"],
                _live_text(state, latest),
                reply_markup=_combined_live_keyboard(challenge_id),
            )
        except Exception:
            pass
    if state["home_goals"] == state["away_goals"]:
        finish_live_state(state, home_players, away_players)
    await database.finish_challenge(challenge_id, state)
    challenge = await database.get_challenge(challenge_id) or challenge
    summary = live_scorecard(state)
    ai_line = await generate_match_summary(
        settings,
        challenge["challenger_name"],
        challenge["challenged_name"],
        state["home_goals"],
        state["away_goals"],
        _scorer_of_state(state, home_players),
        "Manager Challenge",
    )
    final = f"{summary}\n\n<b>Commentator</b>\n<blockquote expandable>{chr(10).join(state.get('commentary', [])[-10:])}\n\n{ai_line}</blockquote>"
    try:
        await bot.edit_message_text(challenge["chat_id"], challenge["message_id"], final)
    except Exception:
        pass
    for user_id in (challenge["challenger_id"], challenge["challenged_id"]):
        try:
            await bot.send_message(user_id, final)
        except Exception:
            pass
    await _reward_challenge(database, challenge)
    await audit(bot, settings, f"Manager challenge finished: <b>{challenge['challenger_name']}</b> {state['home_goals']}-{state['away_goals']} <b>{challenge['challenged_name']}</b>")
    LIVE_TASKS.pop(challenge_id, None)


def _scorer_of_state(state: dict[str, Any], players: list[dict[str, Any]]) -> str:
    events = state.get("events", [])
    for event in reversed(events):
        if event.get("team") == "home":
            return event.get("scorer", "Captain")
    return players[0].get("name", "Captain") if players else "Captain"


def register_mode_handlers(bot: Client, database: MongoDatabase, settings: Settings) -> None:
    @bot.on_message(filters.command("arena"))
    async def arena_handler(_: Client, message: Message) -> None:
        if not _is_group_chat(message):
            await message.reply_text("Arena commands are group-only. Use /arena inside a group.")
            return
        competitions = await database.list_competitions()
        note = "No competitions yet. An owner must add one with /addcompetition." if not competitions else "Choose an owner-created group competition."
        await message.reply_text(f"<b>🔥 ARENA</b>\n\n{note}", reply_markup=arena_keyboard(competitions))

    @bot.on_message(filters.regex(r"^/play(?!ers(?:@\w+)?(?:\s|$))[a-z0-9_-]+(?:@\w+)?(?:\s|$)"))
    async def mode_handler(_: Client, message: Message) -> None:
        if not _is_group_chat(message):
            await message.reply_text("Arena matches are group-only. Use this command inside a group.")
            return
        mode = message.text.split()[0].split("@", 1)[0][1:].lower()
        competition = await database.get_competition(mode)
        if not competition:
            await message.reply_text(f"<b>/{mode}</b> is not configured. The owner can create it with /addcompetition.")
            return
        active = await database.get_active_group_game(message.chat.id)
        if active:
            await message.reply_text(
                "This group already has one active match or lobby.",
                reply_markup=_lobby_keyboard(active["game_id"]) if active["status"] == "lobby" else None,
            )
            return
        await database.get_or_create_user(message.from_user)
        game = await database.create_group_game(message.chat.id, mode, message.from_user)
        await message.reply_text(_mode_text(game, competition), reply_markup=_lobby_keyboard(game["game_id"]))

    @bot.on_callback_query(filters.regex(r"^mode:([a-z0-9_-]+)$"))
    async def mode_menu_handler(_: Client, query: CallbackQuery) -> None:
        await query.answer()
        competition = await database.get_competition(query.data.split(":", 1)[1])
        if not competition:
            await query.message.edit_text("That owner-created competition is not available.")
            return
        await query.message.edit_text(
            f"{competition.get('emoji', '🏆')} <b>{competition['name']}</b>\n\nUse /{competition['competition_key']} in a group to open its one-match lobby.",
            reply_markup=back_keyboard("Back to Arena", "menu:arena"),
        )

    @bot.on_callback_query(filters.regex(r"^game:(join|cancel):([a-f0-9]+)$"))
    async def group_lobby_handler(_: Client, query: CallbackQuery) -> None:
        action, game_id = query.data.split(":")[1:]
        game = await database.get_group_game(game_id)
        if not game or game.get("status") != "lobby":
            await query.answer("This lobby is closed.", show_alert=True)
            return
        if action == "cancel":
            if query.from_user.id != game["host_id"]:
                await query.answer("Only the host can cancel this lobby.", show_alert=True)
                return
            await database.finish_group_game(game_id, {"status": "cancelled"})
            await query.answer("Lobby cancelled.")
            await query.message.edit_text("The group match lobby was cancelled.")
            return
        if query.from_user.id == game["host_id"]:
            await query.answer("You are already the host.", show_alert=True)
            return
        await database.update_group_game(
            game_id,
            {"status": "setup", "phase": "pitch", "opponent_id": query.from_user.id, "opponent_name": query.from_user.first_name or "Manager B"},
        )
        game = await database.get_group_game(game_id)
        competition = await database.get_competition(game["mode"])
        await query.answer("You joined the match.")
        await query.message.edit_text(_mode_text(game, competition), reply_markup=_pitch_keyboard(game_id))

    @bot.on_callback_query(filters.regex(r"^game:(pitch|weather):([a-f0-9]+):([a-z]+)$"))
    async def group_condition_handler(_: Client, query: CallbackQuery) -> None:
        action, game_id, value = query.data.split(":")[1:]
        game = await database.get_group_game(game_id)
        if not game or query.from_user.id != game.get("host_id"):
            await query.answer("Only Manager A controls the conditions.", show_alert=True)
            return
        if game.get("phase") != action:
            await query.answer("That choice is no longer active.", show_alert=True)
            return
        next_phase = "weather" if action == "pitch" else "team_host"
        await database.update_group_game(game_id, {action: value, "phase": next_phase})
        game = await database.get_group_game(game_id)
        competition = await database.get_competition(game["mode"])
        await query.answer(value.title())
        if next_phase == "weather":
            await query.message.edit_text(_mode_text(game, competition), reply_markup=_weather_keyboard(game_id))
        else:
            await query.message.edit_text(_mode_text(game, competition), reply_markup=_team_keyboard(competition, game_id))

    @bot.on_callback_query(filters.regex(r"^game:team:([a-f0-9]+):([a-z0-9_-]+)$"))
    async def group_team_handler(_: Client, query: CallbackQuery) -> None:
        game_id, team_key = query.data.split(":")[2:]
        game = await database.get_group_game(game_id)
        if not game or game.get("status") != "setup":
            await query.answer("This match is no longer selecting teams.", show_alert=True)
            return
        is_host = query.from_user.id == game.get("host_id")
        is_opponent = query.from_user.id == game.get("opponent_id")
        if game.get("phase") == "team_host" and is_host:
            next_phase = "team_away"
            updates = {"home_team": team_key, "phase": next_phase}
        elif game.get("phase") == "team_away" and is_opponent:
            if team_key == game.get("home_team"):
                await query.answer("Choose a different team.", show_alert=True)
                return
            next_phase = "formation_host"
            updates = {"away_team": team_key, "phase": next_phase}
        else:
            await query.answer("Wait for your manager turn.", show_alert=True)
            return
        competition = await database.get_competition(game["mode"])
        selected = next(item for item in competition.get("teams", []) if item.get("team_key") == team_key)
        if "home_team" in updates:
            updates.update({"home_team_name": selected["name"], "home_rating": selected.get("rating", 75)})
        else:
            updates.update({"away_team_name": selected["name"], "away_rating": selected.get("rating", 75)})
        await database.update_group_game(game_id, updates)
        game = await database.get_group_game(game_id)
        await query.answer(selected["name"])
        await query.message.edit_text(_mode_text(game, competition), reply_markup=_team_keyboard(competition, game_id))

    @bot.on_callback_query(filters.regex(r"^game:formation:([a-f0-9]+):([a-z0-9-]+)$"))
    async def group_formation_handler(_: Client, query: CallbackQuery) -> None:
        game_id, formation = query.data.split(":")[2:]
        game = await database.get_group_game(game_id)
        if not game:
            return
        expected = game.get("host_id") if game.get("phase") == "formation_host" else game.get("opponent_id")
        if query.from_user.id != expected:
            await query.answer("Wait for your manager turn.", show_alert=True)
            return
        field = "home_formation" if game.get("phase") == "formation_host" else "away_formation"
        next_phase = "formation_away" if field == "home_formation" else "ready"
        await database.update_group_game(game_id, {field: formation, "phase": next_phase})
        game = await database.get_group_game(game_id)
        competition = await database.get_competition(game["mode"])
        await query.answer(f"{formation} selected.")
        if next_phase == "formation_away":
            await query.message.edit_text(
                _mode_text(game, competition),
                reply_markup=InlineKeyboardMarkup(_formation_rows("game:formation", game_id, list(FORMATIONS))),
            )
        else:
            await query.message.edit_text(_mode_text(game, competition), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Start match", callback_data=f"game:start:{game_id}", style=ButtonStyle.SUCCESS)]]))

    @bot.on_callback_query(filters.regex(r"^game:start:([a-f0-9]+)$"))
    async def group_start_handler(_: Client, query: CallbackQuery) -> None:
        game_id = query.data.split(":")[2]
        game = await database.get_group_game(game_id)
        if not game or query.from_user.id != game.get("host_id") or game.get("phase") != "ready":
            await query.answer("Manager A starts this match when both lineups are ready.", show_alert=True)
            return
        if not game.get("home_team") or not game.get("away_team"):
            await query.answer("Both teams are required.", show_alert=True)
            return
        await query.answer("Kick-off.")
        await _start_group_match(bot, database, settings, game, query.message)

    @bot.on_message(filters.command("challenge"))
    async def challenge_handler(_: Client, message: Message) -> None:
        if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            await message.reply_text("Challenges are group-only. Use /challenge in a group.")
            return
        target_message = message.reply_to_message
        target = target_message.from_user if target_message else None
        if not target or target.id == message.from_user.id or target.is_bot:
            await message.reply_text("Reply to another manager’s message with /challenge.")
            return
        await database.get_or_create_user(message.from_user)
        await database.get_or_create_user(target)
        challenge_id = await database.create_challenge(message.from_user, target, message.chat.id)
        await database.update_challenge(challenge_id, {"message_id": message.id})
        text = f"""<b>⚔️ MANAGER CHALLENGE</b>

<b>{message.from_user.first_name}</b> has challenged <b>{target.first_name}</b>.

Accept to choose formations, tactics, player instructions, and substitutions during a live match."""
        try:
            await bot.send_message(target.id, text, reply_markup=challenge_keyboard(challenge_id))
        except Exception:
            await message.reply_text(f"{text}\n\nThe opponent must start the bot before accepting.", reply_markup=challenge_keyboard(challenge_id))
        await message.reply_text("Challenge sent. Both collected squads will be used.")

    @bot.on_callback_query(filters.regex(r"^challenge:(accept|decline):([a-f0-9]+)$"))
    async def challenge_action_handler(_: Client, query: CallbackQuery) -> None:
        action, challenge_id = query.data.split(":")[1:]
        challenge = await database.get_challenge(challenge_id)
        if not challenge or challenge.get("status") != "pending":
            await query.answer("This challenge has expired.", show_alert=True)
            return
        if query.from_user.id != challenge["challenged_id"]:
            await query.answer("This challenge belongs to another manager.", show_alert=True)
            return
        if action == "decline":
            await query.answer("Challenge declined.")
            await database.finish_challenge(challenge_id, {"status": "declined"})
            await query.message.edit_text("<b>Challenge declined.</b>")
            return
        _, home_players = await database.get_user_players(challenge["challenger_id"], squad_only=True)
        _, away_players = await database.get_user_players(challenge["challenged_id"], squad_only=True)
        if len(home_players) < 11 or len(away_players) < 11:
            await query.message.edit_text("Both managers need an 11-player active squad.")
            return
        home_user = await database.get_user(challenge["challenger_id"]) or {}
        away_user = await database.get_user(challenge["challenged_id"]) or {}
        await database.update_challenge(
            challenge_id,
            {
                "status": "setup",
                "home_players": [player["player_id"] for player in home_players],
                "away_players": [player["player_id"] for player in away_players],
                "home_lineup": [player["player_id"] for player in home_players[:11]],
                "away_lineup": [player["player_id"] for player in away_players[:11]],
                "home_formation": home_user.get("formation", "4-3-3"),
                "away_formation": away_user.get("formation", "4-3-3"),
                "home_tactic": "Balanced",
                "away_tactic": "Balanced",
                "home_mentality": "Balanced",
                "away_mentality": "Balanced",
                "home_instructions": {},
                "away_instructions": {},
                "home_ready": False,
                "away_ready": False,
            },
        )
        challenge = await database.get_challenge(challenge_id)
        try:
            group_message = await bot.send_message(
                challenge["chat_id"],
                _setup_text(challenge),
            )
            await database.update_challenge(challenge_id, {"message_id": group_message.id})
        except Exception:
            await database.update_challenge(challenge_id, {"message_id": query.message.id})
        await query.answer("Challenge accepted.")
        await query.message.edit_text(_setup_text(challenge), reply_markup=_challenge_setup_keyboard(challenge, away_user))
        try:
            await bot.send_message(challenge["challenger_id"], _setup_text(challenge), reply_markup=_challenge_setup_keyboard(challenge, home_user))
        except Exception:
            pass

    @bot.on_callback_query(filters.regex(r"^challenge:instructions:([a-f0-9]+):(home|away)$"))
    async def challenge_instructions_handler(_: Client, query: CallbackQuery) -> None:
        challenge_id, side = query.data.split(":")[2:]
        challenge = await database.get_challenge(challenge_id)
        expected = challenge.get("challenger_id") if side == "home" else challenge.get("challenged_id") if challenge else None
        if not challenge or query.from_user.id != expected or challenge.get("status") != "setup":
            await query.answer("That instruction panel is not yours.", show_alert=True)
            return
        players = await database.get_players(challenge.get(f"{side}_lineup", [])[:11])
        names = ", ".join(player.get("name", "Player") for player in players)
        await query.answer("Use the command shown below.")
        await query.message.reply_text(
            f"<b>PLAYER INSTRUCTIONS</b>\n\nStarting XI: {names}\n\n"
            "Set each player before kickoff with:\n"
            "<code>/instruction Player Name | Attack</code>\n"
            "Allowed instructions: Attack, Hold, Defend, Mark.",
        )

    @bot.on_message(filters.command("instruction"))
    async def instruction_handler(_: Client, message: Message) -> None:
        challenge = await database.get_active_challenge(message.from_user.id)
        if not challenge or challenge.get("status") != "setup":
            await message.reply_text("Player instructions are available after a challenge is accepted and before kickoff.")
            return
        parts = [part.strip() for part in message.text.partition(" ")[2].split("|")]
        if len(parts) != 2 or not parts[0] or parts[1].title() not in {"Attack", "Hold", "Defend", "Mark"}:
            await message.reply_text("<code>/instruction Player Name | Attack</code>\nAllowed: Attack, Hold, Defend, Mark.")
            return
        side = "home" if message.from_user.id == challenge["challenger_id"] else "away"
        lineup = await database.get_players(challenge.get(f"{side}_lineup", [])[:11])
        player = next((item for item in lineup if parts[0].casefold() in item.get("name", "").casefold()), None)
        if not player:
            await message.reply_text("That player is not in your starting XI.")
            return
        instructions = challenge.get(f"{side}_instructions", {})
        instructions[player["player_id"]] = parts[1].title()
        await database.update_challenge(challenge["challenge_id"], {f"{side}_instructions": instructions})
        await message.reply_text(f"Instruction saved: <b>{player['name']}</b> → <b>{parts[1].title()}</b>.")

    @bot.on_callback_query(filters.regex(r"^challenge:(formation|tactic|mentality):([a-f0-9]+):([a-z]+):([a-z0-9-]+)$"))
    async def challenge_choice_handler(_: Client, query: CallbackQuery) -> None:
        action, challenge_id, side, value = query.data.split(":")[1:]
        challenge = await database.get_challenge(challenge_id)
        expected = challenge.get("challenger_id") if side == "home" else challenge.get("challenged_id") if challenge else None
        if not challenge or query.from_user.id != expected or challenge.get("status") != "setup":
            await query.answer("That manager control is not yours.", show_alert=True)
            return
        user = await database.get_user(query.from_user.id) or {}
        if action == "formation" and value not in unlocked_formations(user):
            await query.answer("Level up to unlock that formation.", show_alert=True)
            return
        field = f"{side}_{'formation' if action == 'formation' else 'tactic' if action == 'tactic' else 'mentality'}"
        await database.update_challenge(challenge_id, {field: value})
        challenge = await database.get_challenge(challenge_id)
        await query.answer(f"{action.title()} updated.")
        await query.message.edit_text(_setup_text(challenge), reply_markup=_challenge_setup_keyboard(challenge, user))

    @bot.on_callback_query(filters.regex(r"^challenge:(ready):([a-f0-9]+)$"))
    async def challenge_ready_handler(_: Client, query: CallbackQuery) -> None:
        challenge_id = query.data.split(":")[2]
        challenge = await database.get_challenge(challenge_id)
        if not challenge or challenge.get("status") != "setup":
            await query.answer("This setup is closed.", show_alert=True)
            return
        side = "home" if query.from_user.id == challenge["challenger_id"] else "away" if query.from_user.id == challenge["challenged_id"] else None
        if not side:
            await query.answer("You are not a manager in this match.", show_alert=True)
            return
        await database.update_challenge(challenge_id, {f"{side}_ready": True})
        challenge = await database.get_challenge(challenge_id)
        await query.answer("Lineup marked ready.")
        if challenge.get("home_ready") and challenge.get("away_ready"):
            home_lineup = await database.get_players(challenge.get("home_lineup", []))
            away_lineup = await database.get_players(challenge.get("away_lineup", []))
            state = new_live_state(
                challenge["challenger_name"],
                challenge["challenged_name"],
                _rating(home_lineup),
                _rating(away_lineup),
            )
            await database.update_challenge(challenge_id, {"status": "live", "live_state": state})
            LIVE_TASKS[challenge_id] = asyncio.create_task(_run_challenge(bot, database, settings, challenge_id))
            await query.message.edit_text("<b>🔴 KICK-OFF</b>\n\nManagers are on the touchline. Controls will remain available during the match.", reply_markup=_combined_live_keyboard(challenge_id))
        else:
            await query.message.edit_text(_setup_text(challenge), reply_markup=_challenge_setup_keyboard(challenge, await database.get_user(query.from_user.id) or {}))

    @bot.on_callback_query(filters.regex(r"^live:([a-f0-9]+):(tactic|mentality|sub):([a-z]+)(?::([A-Za-z]+))?$"))
    async def live_manager_handler(_: Client, query: CallbackQuery) -> None:
        _, challenge_id, action, side, value = (query.data.split(":") + [None])[:5]
        challenge = await database.get_challenge(challenge_id)
        expected = challenge.get("challenger_id") if side == "home" else challenge.get("challenged_id") if challenge else None
        if not challenge or query.from_user.id != expected or challenge.get("status") != "live":
            await query.answer("This is not your live manager control.", show_alert=True)
            return
        if action == "sub":
            lineup_key = f"{side}_lineup"
            all_key = f"{side}_players"
            lineup = challenge.get(lineup_key, [])[:]
            bench = [player for player in challenge.get(all_key, []) if player not in lineup]
            if lineup and bench:
                outgoing = lineup.pop(random.randrange(len(lineup)))
                incoming = bench.pop(0)
                lineup.append(incoming)
                await database.update_challenge(challenge_id, {lineup_key: lineup, f"{side}_substitutions": challenge.get(f"{side}_substitutions", 0) + 1})
                await query.answer("Substitution made.")
            else:
                await query.answer("No substitute is available.", show_alert=True)
            return
        field = f"{side}_{action}"
        await database.update_challenge(challenge_id, {field: value.title() if value else "Balanced"})
        await query.answer(f"{action.title()} changed for the next window.")