from __future__ import annotations

import asyncio
import html
import os
import random
import re
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
from services.match_summary import render_match_summary

from .ui import arena_keyboard, back_keyboard, challenge_keyboard


LIVE_TASKS: dict[str, asyncio.Task] = {}
MODE_ALIASES = {"playucl": "playcl"}
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
TACTICS = (
    "Balanced", "Possession", "Counter", "Press",
    "Direct", "Wide", "Fast Build", "Slow Build",
)
MENTALITIES = (
    "Balanced", "Attacking", "Defensive",
    "High Press", "Mid Block", "Low Block",
    "Man Marking", "Offside Trap", "Overload", "Protect Lead",
)

# A manager turn should feel like a quick simulation, not a timed minute-by-minute
# game. Each click advances a variable 5-10 match minutes; the bot never waits
# five/ten real minutes.
WINDOW_WAIT_SECONDS = 30
WINDOW_POLL_SECONDS = 1
SIM_MIN_MINUTES = 4
SIM_MAX_MINUTES = 6
HALFTIME_MINUTE = 45

# Live manager controls are intentionally compact: exactly six football actions.
LIVE_ACTIONS = (
    ("🧠 Keep Possession", "Possession"),
    ("⚔️ Attack Through Middle", "Attacking"),
    ("⚡ Launch Counter Attack", "Counter"),
    ("📣 Press High Upfield", "Press"),
    ("↔️ Attack Down the Wings", "Wide"),
    ("🛡️ Defend Deep & Hold Shape", "Defensive"),
)

# How long (seconds) the live loop waits for BOTH managers to lock in an
# action before auto-advancing the window with whatever was last set. Keeps
# a match from stalling forever if someone goes AFK.


# Bonus reward on top of the base match payout, applied to the winner (and
# halved for a draw). Losers keep only the base participation reward.
WIN_BONUS_MIN_COINS = 10
WIN_BONUS_MAX_COINS = 50
WIN_BONUS_XP = 50


MENTALITY_ENGINE_MAP = {
    "Balanced": "Balanced",
    "Attacking": "Attacking",
    "Defensive": "Defensive",
    "High Press": "Attacking",
    "Mid Block": "Balanced",
    "Low Block": "Defensive",
    "Man Marking": "Defensive",
    "Offside Trap": "Attacking",
    "Overload": "Attacking",
    "Protect Lead": "Defensive",
}


def _engine_mentality(value: str) -> str:
    return MENTALITY_ENGINE_MAP.get(value, "Balanced")

_SYNTHETIC_FIRST_NAMES = (
    "Carlos", "Luis", "Marco", "Kwame", "Ivan", "Noah", "Rafael", "Dmitri",
    "Tariq", "Hiro", "Diego", "Andres", "Kai", "Mateo", "Sam", "Leon",
    "Theo", "Omar", "Jonas", "Felix", "Bruno", "Kenji", "Malik", "Ryo",
)
_SYNTHETIC_LAST_NAMES = (
    "Rossi", "Silva", "Costa", "Mensah", "Petrov", "Alves", "Nakamura",
    "Haddad", "Novak", "Sorensen", "Vidal", "Duarte", "Okafor", "Larsen",
    "Bianchi", "Serrano", "Kovac", "Fischer", "Barros", "Adeyemi",
)


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


def _canonical_mode(mode: str) -> str:
    return MODE_ALIASES.get(mode, mode)


def _mode_command(mode: str) -> str:
    return "playucl" if mode == "playcl" else mode


def _synthetic_team(team: dict[str, Any]) -> list[dict[str, Any]]:
    positions = ["GK", "DEF", "DEF", "DEF", "DEF", "MID", "MID", "MID", "ATT", "ATT", "ATT"]
    rating = int(team.get("rating", 75))
    pool = [f"{first} {last}" for first in _SYNTHETIC_FIRST_NAMES for last in _SYNTHETIC_LAST_NAMES]
    names = random.sample(pool, k=len(positions))
    return [
        {
            "player_id": f"team-{team.get('team_key', 'side')}-{index}",
            "name": names[index - 1],
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


def _user_mention(user_id: int | None, name: str | None, username: str | None = None) -> str:
    """Telegram HTML mention; shows @username when available."""
    display = html.escape(name or "Manager")
    if username:
        return f"@{html.escape(username.lstrip('@'))}"
    if user_id:
        return f'<a href="tg://user?id={user_id}">{display}</a>'
    return display


def _cancel_confirm_keyboard(game_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("❌ Confirm cancel", callback_data=f"game:cancel_confirm:{game_id}", style=ButtonStyle.DANGER),
            InlineKeyboardButton("↩️ Keep game", callback_data=f"game:cancel_abort:{game_id}", style=ButtonStyle.PRIMARY),
        ]]
    )


def _penalty_keyboard(game_id: str, side: str, players: list[dict[str, Any]], prefix: str = "game") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(0, min(len(players), 11), 2):
        row = []
        for player in players[index:index + 2]:
            row.append(
                InlineKeyboardButton(
                    f"⚽ {player.get('name', 'Player')[:18]}",
                    callback_data=f"{prefix}:penalty:{game_id}:{side}:{player.get('player_id')}",
                )
            )
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def _cancel_group_game(bot: Client, database: MongoDatabase, game: dict[str, Any], user: Any) -> bool:
    """Cancel a group game if the caller is a manager or a group admin."""
    if not game:
        return False
    uid = getattr(user, "id", None)
    allowed = uid in {game.get("host_id"), game.get("opponent_id")}
    if not allowed:
        try:
            member = await bot.get_chat_member(game["chat_id"], uid)
            allowed = str(member.status).lower().split(".")[-1] in {"administrator", "owner"}
        except Exception:
            allowed = False
    if not allowed:
        return False
    task = LIVE_TASKS.pop(game["game_id"], None)
    if task and not task.done():
        task.cancel()
    await database.finish_group_game(
        game["game_id"],
        {"status": "cancelled", "cancelled_by": uid},
    )
    return True




SCENARIOS = (
    ("midfield battle", "The midfield is crowded; both sides are fighting for control."),
    ("quick transition", "A turnover opens space for a dangerous transition."),
    ("wing pressure", "The wingers are finding room and forcing the full-backs deep."),
    ("set-piece threat", "A dead-ball situation creates pressure in the box."),
    ("counter window", "One side leaves space behind and a counter is developing."),
    ("defensive stand", "The back lines are holding firm under pressure."),
    ("late surge", "The tempo rises as one side pushes numbers forward."),
    ("scrappy spell", "A scrappy spell brings tackles, second balls and broken attacks."),
)


def _scenario_note(state: dict[str, Any]) -> str:
    minute = int(state.get("minute", 0))
    home = int(state.get("home_goals", 0))
    away = int(state.get("away_goals", 0))
    if minute >= 75 and home != away:
        return "🔥 late-game pressure — the trailing side is chasing the match."
    if minute <= 20:
        return "⚡ opening spell — both teams are testing the defensive shape."
    return random.choice(SCENARIOS)[1]


async def _advance_window(
    state: dict[str, Any],
    home_players: list[dict[str, Any]],
    away_players: list[dict[str, Any]],
    home_tactic: str,
    away_tactic: str,
    home_mentality: str,
    away_mentality: str,
    max_minute: int = 90,
) -> tuple[dict[str, Any], str]:
    """Advance a variable 5-10 match-minute simulation window."""
    target = random.randint(SIM_MIN_MINUTES, SIM_MAX_MINUTES)
    latest = ""
    previous = state.get("minute", 0)
    # advance_live_state owns the actual match engine. Calling it in short
    # chunks lets one manager click simulate several match minutes instantly.
    guard = 0
    while state.get("minute", 0) < min(previous + target, max_minute) and guard < 4:
        state, latest = advance_live_state(
            state, home_players, away_players,
            home_tactic, away_tactic,
            _engine_mentality(home_mentality), _engine_mentality(away_mentality),
        )
        guard += 1
    return state, latest


async def _wait_for_halftime_subs(database: MongoDatabase, game_id: str, kind: str) -> None:
    """Give managers a short halftime decision window, then continue automatically."""
    await asyncio.sleep(0.2)
    await asyncio.sleep(0.2)


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
    rows.append(
        [
            InlineKeyboardButton("↩️ Back to Arena", callback_data=f"game:back:{game_id}", style=ButtonStyle.PRIMARY),
            InlineKeyboardButton("Cancel lobby", callback_data=f"game:cancel:{game_id}", style=ButtonStyle.DANGER),
        ],
    )
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
            ],
        ]
    )


def _weather_keyboard(game_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("☀️ Clear", callback_data=f"game:weather:{game_id}:clear"),
                InlineKeyboardButton("🌧 Rain", callback_data=f"game:weather:{game_id}:rain"),
                InlineKeyboardButton("🌬 Wind", callback_data=f"game:weather:{game_id}:wind"),
            ],
        ]
    )


def _scenario_actions(state: dict[str, Any], side: str) -> list[tuple[str, str]]:
    """Return six clear, context-sensitive manager controls.

    The labels describe the football instruction the click actually represents,
    while the second value remains the match-engine tactic/mentality key.
    """
    home_goals = int(state.get("home_goals", 0))
    away_goals = int(state.get("away_goals", 0))
    score_for = home_goals if side == "home" else away_goals
    score_against = away_goals if side == "home" else home_goals
    minute = int(state.get("minute", 0))
    chasing = score_for < score_against or (score_for == score_against and minute >= 70)

    if chasing:
        return [
            ("⚔️ Push More Players Forward", "Attacking"),
            ("↔️ Attack Down the Wings", "Wide"),
            ("🧠 Keep the Ball & Build", "Possession"),
            ("⚡ Launch a Fast Counter", "Counter"),
            ("📣 Press High & Win It Back", "Press"),
            ("🛡️ Keep Defensive Shape", "Defensive"),
        ]
    return [
        ("🧠 Keep Possession", "Possession"),
        ("⚔️ Attack Through the Middle", "Attacking"),
        ("⚡ Break Forward on Counter", "Counter"),
        ("📣 Press High Upfield", "Press"),
        ("↔️ Attack Down the Wings", "Wide"),
        ("🛡️ Defend Deep & Hold Shape", "Defensive"),
    ]


def _turn_keyboard(prefix: str, match_id: str, side: str, state: dict[str, Any] | None = None, halftime: bool = False) -> InlineKeyboardMarkup:
    """Six context-sensitive controls, two per row, for the active manager."""
    state = state or {}
    actions = _scenario_actions(state, side)
    rows = [
        [
            InlineKeyboardButton(label, callback_data=f"{prefix}:turn:{match_id}:{side}:{value.replace(' ', '_')}")
            for label, value in actions[index:index + 2]
        ]
        for index in range(0, 6, 2)
    ]
    if halftime:
        rows.append([InlineKeyboardButton("🔁 Substitution", callback_data=f"{prefix}:sub:{match_id}:{side}")])
    return InlineKeyboardMarkup(rows)


def _group_live_keyboard(game_id: str, side: str = "home", halftime: bool = False, state: dict[str, Any] | None = None) -> InlineKeyboardMarkup:
    return _turn_keyboard("game:live", game_id, side, state, halftime)


def _challenge_live_keyboard(challenge_id: str, side: str = "home", halftime: bool = False, state: dict[str, Any] | None = None) -> InlineKeyboardMarkup:
    return _turn_keyboard("live", challenge_id, side, state, halftime)


def _team_side_label(record: dict[str, Any], side: str) -> str:
    key = "home_team_name" if side == "home" else "away_team_name"
    fallback = "Manager A" if side == "home" else "Manager B"
    name = str(record.get(key) or fallback)
    return f"{name} ({"A" if side == "home" else "B"})"


def _group_halftime_keyboard(game: dict[str, Any]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"🔁 {_team_side_label(game, 'home')} · Sub", callback_data=f"game:sub:{game['game_id']}:home"),
            InlineKeyboardButton(f"✅ {_team_side_label(game, 'home')} · Ready", callback_data=f"game:halfready:{game['game_id']}:home", style=ButtonStyle.SUCCESS),
        ],
        [
            InlineKeyboardButton(f"🔁 {_team_side_label(game, 'away')} · Sub", callback_data=f"game:sub:{game['game_id']}:away"),
            InlineKeyboardButton(f"✅ {_team_side_label(game, 'away')} · Ready", callback_data=f"game:halfready:{game['game_id']}:away", style=ButtonStyle.SUCCESS),
        ],
    ])


def _challenge_halftime_keyboard(challenge: dict[str, Any]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"🔁 {_team_side_label(challenge, 'home')} · Sub", callback_data=f"live:halfready_sub:{challenge['challenge_id']}:home"),
            InlineKeyboardButton(f"✅ {_team_side_label(challenge, 'home')} · Ready", callback_data=f"live:halfready:{challenge['challenge_id']}:home", style=ButtonStyle.SUCCESS),
        ],
        [
            InlineKeyboardButton(f"🔁 {_team_side_label(challenge, 'away')} · Sub", callback_data=f"live:halfready_sub:{challenge['challenge_id']}:away"),
            InlineKeyboardButton(f"✅ {_team_side_label(challenge, 'away')} · Ready", callback_data=f"live:halfready:{challenge['challenge_id']}:away", style=ButtonStyle.SUCCESS),
        ],
    ])


def _formation_rows(
    prefix: str,
    game_id: str,
    formations: list[str],
    back_callback: str | None = None,
) -> list[list[InlineKeyboardButton]]:
    buttons = [InlineKeyboardButton(formation, callback_data=f"{prefix}:{game_id}:{formation}") for formation in formations]
    rows = [buttons[index : index + 3] for index in range(0, len(buttons), 3)]
    if back_callback:
        rows.append([InlineKeyboardButton("↩️ Back", callback_data=back_callback, style=ButtonStyle.PRIMARY)])
    return rows


def _challenge_setup_keyboard(challenge: dict[str, Any], user: dict[str, Any]) -> InlineKeyboardMarkup:
    side = "home" if challenge["challenger_id"] == user["user_id"] else "away"
    formations = unlocked_formations(user)
    rows = _formation_rows(
        "challenge:formation",
        challenge["challenge_id"],
        formations,
        back_callback=f"challenge:back:{challenge['challenge_id']}",
    )
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


def _live_keyboard(challenge_id: str, side: str, state: dict[str, Any] | None = None) -> InlineKeyboardMarkup:
    return _challenge_live_keyboard(challenge_id, side, state=state)


def _setup_text(challenge: dict[str, Any]) -> str:
    return f"""<b>⚔️ MANAGER MATCH SETUP</b>

<b>{challenge['challenger_name']}</b> vs <b>{challenge['challenged_name']}</b>

Each manager chooses a formation, tactic, mentality, and starting player instructions. During the match you can change tactics, mentality, and make substitutions every match window.

{challenge['challenger_name']}: {challenge.get('home_formation', '4-3-3')} · {challenge.get('home_tactic', 'Balanced')} · {challenge.get('home_mentality', 'Balanced')} · {"READY" if challenge.get("home_ready") else "choosing"}
{challenge['challenged_name']}: {challenge.get('away_formation', '4-3-3')} · {challenge.get('away_tactic', 'Balanced')} · {challenge.get('away_mentality', 'Balanced')} · {"READY" if challenge.get("away_ready") else "choosing"}

Managers: use your own controls below. The collected squads are used for this challenge."""


def _display_minute(state: dict[str, Any]) -> str:
    """Display stoppage time as 45+N / 90+N instead of raw 46/91."""
    minute = int(state.get("minute", 0))
    if state.get("halftime"):
        return f"45+{int(state.get("first_half_stoppage", 0))}'"
    if minute > 90:
        return f"90+{minute - 90}'"
    return f"{minute}'"


def _set_piece_kind(text: str) -> str | None:
    low = text.lower()
    if "corner" in low:
        return "corner"
    if "free kick" in low or "free-kick" in low:
        return "free_kick"
    return None


def _player_rows(
    prefix: str,
    match_id: str,
    side: str,
    players: list[dict[str, Any]],
    action: str,
    back_callback: str | None = None,
) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(players), 2):
        rows.append([
            InlineKeyboardButton(
                f"⚽ {p.get('name', 'Player')[:18]}",
                callback_data=f"{prefix}:{action}:{match_id}:{side}:{p.get('player_id')}",
            ) for p in players[i:i+2]
        ])
    if back_callback:
        rows.append([InlineKeyboardButton("↩️ Back", callback_data=back_callback, style=ButtonStyle.PRIMARY)])
    return InlineKeyboardMarkup(rows)


def _live_text(
    state: dict[str, Any],
    latest: str = "",
    active_side: str | None = None,
    active_name: str | None = None,
    home_done: bool = False,
    away_done: bool = False,
) -> str:
    commentary = "\n".join(f"• {line}" for line in state.get("commentary", [])[-7:])
    active_label = "Manager A" if active_side == "home" else "Manager B" if active_side == "away" else "Both managers"
    if active_side == "sim":
        turn_line = "<b>🎬 LIVE SIMULATION</b> — play is unfolding..."
    else:
        turn_line = (
            f"<b>🎮 {html.escape(active_name or active_label)}</b> — choose the next move."
            if active_side else "<b>⏸ HALF-TIME</b>"
        )
    minute_display = _display_minute(state)
    return f"""<b>🔴 LIVE MATCH · {minute_display}</b>


🟦 <b>{state['home']}</b>  <b>{state['home_goals']}</b>
🟥 <b>{state['away']}</b>  <b>{state['away_goals']}</b>

<b>Latest play</b>
<blockquote expandable>{html.escape(latest or 'The match is underway...')}</blockquote>

<b>Match feed</b>
<blockquote expandable>{html.escape(commentary or 'No major event yet.')}</blockquote>

{turn_line}

<i>One manager acts → the other manager responds → the next passage is simulated.</i>"""


def _turn_name(game: dict[str, Any], side: str) -> str:
    if side == "home":
        return game.get("host_username") and f"@{game['host_username'].lstrip('@')}" or game.get("host_name", "Manager A")
    return game.get("opponent_username") and f"@{game['opponent_username'].lstrip('@')}" or game.get("opponent_name", "Manager B")


def _turn_label(game: dict[str, Any], side: str) -> str:
    manager = _turn_name(game, side)
    team = game.get("home_team_name") if side == "home" else game.get("away_team_name")
    return f"{manager} · {team or 'your team'}"


def _players_for_event(players: list[dict[str, Any]], count: int = 4) -> list[str]:
    names = [str(p.get("name", "Player")) for p in players if p.get("name")]
    return random.sample(names, min(count, len(names))) if names else ["Player"]


def _play_by_play(
    state: dict[str, Any],
    home_players: list[dict[str, Any]],
    away_players: list[dict[str, Any]],
    active_side: str,
    action: str,
    latest: str,
    start_minute: int,
) -> str:
    """Build a compact, realistic passage instead of making a turn feel like a score jump."""
    attacking = home_players if active_side == "home" else away_players
    defending = away_players if active_side == "home" else home_players
    a = _players_for_event(attacking, 5)
    d = _players_for_event(defending, 4)
    end_minute = int(state.get("minute", start_minute + 4))
    m1 = min(start_minute + 1, end_minute)
    m2 = min(start_minute + 2, end_minute)
    m3 = min(start_minute + 3, end_minute)
    m4 = min(start_minute + 4, end_minute)
    lines = [
        f"{m1}' {a[0]} receives and turns away from pressure.",
        f"{m2}' {a[min(1, len(a)-1)]} combines with {a[min(2, len(a)-1)]} to move the attack forward.",
    ]
    if action in {"Press", "Defensive"}:
        lines += [
            f"{m2}' {d[0]} steps up and makes the challenge — possession changes hands.",
            f"{m3}' The back line resets quickly and closes the central lane.",
        ]
    elif action == "Counter":
        lines += [
            f"{m3}' Turnover! {a[min(2, len(a)-1)]} accelerates into open space.",
            f"{m4}' {a[min(3, len(a)-1)]} makes the run beyond the defence.",
        ]
    elif action == "Wide":
        lines += [
            f"{m3}' {a[min(2, len(a)-1)]} pulls wide and delivers toward the box.",
            f"{m4}' {d[min(1, len(d)-1)]} gets across to cut out the danger.",
        ]
    elif action == "Attacking":
        lines += [
            f"{m3}' {a[min(2, len(a)-1)]} drives between the lines and slips a pass into the area.",
            f"{m4}' The defence is stretched; a shot is coming.",
        ]
    else:
        lines += [
            f"{m3}' {a[min(2, len(a)-1)]} recycles the ball and keeps the tempo under control.",
            f"{m4}' A patient move opens a narrow shooting lane.",
        ]

    if latest:
        lines.append(f"{end_minute}' {latest}")
    entries = _goal_entries(state, str(state.get("home", "Home")), str(state.get("away", "Away")))
    if entries and latest and "goal" in latest.casefold():
        minute, scorer, side = entries[-1]
        team_name = state.get("home") if side == "home" else state.get("away")
        callout = f"⚽ GOAL! {scorer} scores for {team_name} ({minute}')"
        if callout not in lines:
            lines.insert(0, callout)
    return "\n".join(lines)



def _mode_text(game: dict[str, Any], competition: dict[str, Any]) -> str:
    host = _user_mention(game.get("host_id"), game.get("host_name"), game.get("host_username"))
    opponent = _user_mention(game.get("opponent_id"), game.get("opponent_name"), game.get("opponent_username"))
    if game["status"] == "lobby":
        return f"""<b>{competition.get('emoji', '🏆')} {html.escape(competition['name'])}</b>

{host} opened the group match lobby.
One manager joins as Manager B.

Mode: <b>/{_mode_command(game['mode'])}</b>"""
    if game["status"] == "setup":
        phase = game.get("phase", "team_host")
        if phase == "team_host":
            return f"""<b>{html.escape(competition['name'])}</b>

<b>TEAM SELECTION · MANAGER A</b>
{host}

Manager A, choose your team from the full menu below."""
        if phase == "team_away":
            return f"""<b>{html.escape(competition['name'])}</b>

<b>TEAM SELECTION · MANAGER B</b>
{opponent}

Manager A selected <b>{html.escape(game.get('home_team_name', 'their team'))}</b>.
Manager B, choose your team from the full menu below."""
        if phase == "formation_host":
            return f"<b>{html.escape(competition['name'])}</b>\n\n{host}, choose your formation."
        if phase == "formation_away":
            return f"<b>{html.escape(competition['name'])}</b>\n\n{opponent}, choose your formation."
        return f"<b>{html.escape(competition['name'])}</b>\n\nTeams are ready. Start the match."
    return f"<b>{html.escape(competition['name'])}</b>\n\nThe match is being simulated live."


async def _team_players(database: MongoDatabase, competition: dict[str, Any], team_key: str) -> list[dict[str, Any]]:
    team = next((item for item in competition.get("teams", []) if item.get("team_key") == team_key), None)
    if not team:
        return []
    players = await database.get_team_players(team, competition.get("team_type", "club"))
    return players if len(players) >= 11 else _synthetic_team(team)


async def _animate_play(
    bot: Client, chat_id: int, message_id: int, state: dict[str, Any], lines: list[str]
) -> None:
    """Render the whole passage in one edit; Pyrogram/Telegram rate-limit edits."""
    if not lines:
        return
    text = "\n".join(lines[:7])
    try:
        await bot.edit_message_text(
            chat_id, message_id,
            _live_text(state, text, "sim"),
        )
    except Exception:
        pass


def _stat_value(state: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = state.get(key)
        if value is not None:
            return str(value)
    return "—"


def _event_side(team: Any, home: str, away: str) -> str | None:
    if team in {"home", "HOME", home}:
        return "home"
    if team in {"away", "AWAY", away}:
        return "away"
    text = str(team or "").casefold()
    if text in {"home", home.casefold()} or "home" == text:
        return "home"
    if text in {"away", away.casefold()} or "away" == text:
        return "away"
    return None


def _normalize_event_minute(value: Any) -> str:
    """Render football minutes as 45+N / 90+N when the engine gives raw stoppage minutes."""
    text = str(value or "?").strip().replace("’", "'").rstrip("'")
    if text == "?":
        return text
    # Already correctly formatted.
    if "+" in text:
        return text
    try:
        minute = int(float(text))
    except (TypeError, ValueError):
        return text
    if minute > 90:
        return f"90+{minute - 90}"
    return str(minute)


def _goal_entries(state: dict[str, Any], home: str, away: str) -> list[tuple[str, str, str]]:
    """Collect scorer metadata from every goal representation used by the engine."""
    entries: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    events = state.get("events", []) or []
    if isinstance(events, dict):
        events = list(events.values())
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type", event.get("event", event.get("kind", "")))).casefold()
        is_goal = bool(event.get("goal") is True or "goal" in kind or event.get("is_goal") is True)
        if not is_goal:
            continue
        scorer = (event.get("scorer") or event.get("scorer_name") or event.get("player_name") or
                  event.get("player") or event.get("goalscorer") or event.get("goal_scorer"))
        team = _event_side(event.get("team") or event.get("side") or event.get("team_name"), home, away)
        if not scorer or not team:
            continue
        minute = event.get("minute") or event.get("time") or event.get("match_minute") or "?"
        item = (_normalize_event_minute(minute), str(scorer), team)
        if item not in seen:
            entries.append(item)
            seen.add(item)

    for key in ("goal_scorers", "goalscorers", "scorers"):
        data = state.get(key)
        if not data:
            continue
        if isinstance(data, dict):
            for team_key, names in data.items():
                side = _event_side(team_key, home, away)
                if not side:
                    continue
                if isinstance(names, str):
                    names = [names]
                for item in names or []:
                    if isinstance(item, dict):
                        minute = item.get("minute") or item.get("time") or "?"
                        name = item.get("name") or item.get("scorer") or "Player"
                    else:
                        minute, name = "?", item
                    entry = (_normalize_event_minute(minute), str(name), side)
                    if entry not in seen:
                        entries.append(entry)
                        seen.add(entry)
    return entries


def _pick_scorer(players: list[dict[str, Any]], latest: str = "") -> str:
    """Use an engine-mentioned player first, otherwise choose a plausible attacker."""
    text = str(latest or "").casefold()
    named = [p for p in players if p.get("name") and str(p["name"]).casefold() in text]
    if named:
        return str(named[0]["name"])
    attackers = [p for p in players if str(p.get("position", "")).upper() in {"ATT", "ST", "CF", "FW", "FWD", "MID", "MF"}]
    pool = attackers or players
    if not pool:
        return "Player"
    # Keep the scorer plausible without always making the highest-rated player score.
    ranked = sorted(pool, key=lambda p: int(p.get("shooting", p.get("ovr", 75))), reverse=True)
    return str(random.choice(ranked[:min(4, len(ranked))]).get("name", "Player"))


def _attach_missing_goal_metadata(
    state: dict[str, Any],
    before_home_goals: int,
    before_away_goals: int,
    home_players: list[dict[str, Any]],
    away_players: list[dict[str, Any]],
    latest: str = "",
) -> None:
    """Repair engine score-only goals so every goal always has scorer + team metadata."""
    after_home = int(state.get("home_goals", 0))
    after_away = int(state.get("away_goals", 0))
    new_home = max(0, after_home - before_home_goals)
    new_away = max(0, after_away - before_away_goals)
    if not (new_home or new_away):
        return

    events = state.setdefault("events", [])
    if isinstance(events, dict):
        events = list(events.values())
        state["events"] = events
    existing = _goal_entries(state, str(state.get("home", "Home")), str(state.get("away", "Away")))
    existing_counts = {"home": sum(1 for _, _, side in existing if side == "home"),
                       "away": sum(1 for _, _, side in existing if side == "away")}
    minute = _normalize_event_minute(state.get("minute", "?"))

    for side, count, players in (("home", new_home, home_players), ("away", new_away, away_players)):
        missing = max(0, count - existing_counts[side])
        for _ in range(missing):
            scorer = _pick_scorer(players, latest)
            events.append({
                "type": "goal",
                "goal": True,
                "team": side,
                "side": side,
                "scorer": scorer,
                "scorer_name": scorer,
                "player_name": scorer,
                "minute": minute,
            })
            state.setdefault("commentary", []).append(
                f"{minute}' ⚽ GOAL! {scorer} scores for {state.get('home') if side == 'home' else state.get('away')}."
            )
            existing_counts[side] += 1


def _scorecard_stat_lines(state: dict[str, Any], side: str) -> list[str]:
    if side == "home":
        return [
            f"Possession {_stat_value(state, 'home_possession', 'possession_home')}",
            f"Shots {_stat_value(state, 'home_shots', 'shots_home')}",
            f"On target {_stat_value(state, 'home_shots_on_target', 'shots_on_target_home')}",
            f"Corners {_stat_value(state, 'home_corners', 'corners_home')}",
            f"Fouls {_stat_value(state, 'home_fouls', 'fouls_home')}",
            f"Offside {_stat_value(state, 'home_offsides', 'offsides_home')}",
            f"Yellow {_stat_value(state, 'home_yellow_cards', 'home_yellows', 'yellow_home')}",
            f"Red {_stat_value(state, 'home_red_cards', 'home_reds', 'red_home')}",
        ]
    return [
        f"Possession {_stat_value(state, 'away_possession', 'possession_away')}",
        f"Shots {_stat_value(state, 'away_shots', 'shots_away')}",
        f"On target {_stat_value(state, 'away_shots_on_target', 'shots_on_target_away')}",
        f"Corners {_stat_value(state, 'away_corners', 'corners_away')}",
        f"Fouls {_stat_value(state, 'away_fouls', 'fouls_away')}",
        f"Offside {_stat_value(state, 'away_offsides', 'offsides_away')}",
        f"Yellow {_stat_value(state, 'away_yellow_cards', 'away_yellows', 'yellow_away')}",
        f"Red {_stat_value(state, 'away_red_cards', 'away_reds', 'red_away')}",
    ]


def _full_match_commentary(state: dict[str, Any]) -> str:
    """One clean post-match commentary message with every log independently expandable."""
    logs = state.get("commentary", []) or []
    if not logs:
        return "<b>🎙 FULL MATCH COMMENTARY</b>\n\n<blockquote expandable>No commentary was recorded.</blockquote>"
    blocks = []
    for log in logs:
        clean = html.escape(str(log).strip())
        if clean:
            blocks.append(f"<blockquote expandable>{clean}</blockquote>")
    return "<b>🎙 FULL MATCH COMMENTARY</b>\n\n" + "\n".join(blocks)


async def _send_match_summary_image(
    bot: Client,
    chat_id: int,
    state: dict[str, Any],
    home_players: list[dict[str, Any]],
    away_players: list[dict[str, Any]],
    competition_name: str,
) -> None:
    """Send the visual result without allowing image failures to break a match."""
    image_path = None
    try:
        image_path = await asyncio.to_thread(
            render_match_summary,
            state,
            home_players,
            away_players,
            competition_name,
        )
        await bot.send_photo(chat_id, image_path, caption="⚽ Full-time football match summary")
    except Exception:
        pass
    finally:
        if image_path:
            try:
                os.unlink(image_path)
            except OSError:
                pass


def _football_scorecard(
    state: dict[str, Any],
    home_players: list[dict[str, Any]] | None = None,
    away_players: list[dict[str, Any]] | None = None,
    extra: str = "",
    heading: str = "🏁 FULL MATCH SCORECARD",
    phase_label: str = "FULL TIME",
) -> str:
    """Clean scorecard. POTM/duration are final-match fields, never half-time fields."""
    home_raw = str(state.get("home", "Home"))
    away_raw = str(state.get("away", "Away"))
    home, away = html.escape(home_raw), html.escape(away_raw)
    hg, ag = int(state.get("home_goals", 0)), int(state.get("away_goals", 0))
    entries = _goal_entries(state, home_raw, away_raw)

    goal_lines = [
        f"⚽ <b>{html.escape(minute)}'</b> {html.escape(scorer)} — {home if side == 'home' else away}"
        for minute, scorer, side in entries
    ]
    if not goal_lines:
        goal_lines = ["No goals" if hg + ag == 0 else "⚠️ Goal metadata unavailable."]

    home_stats = _scorecard_stat_lines(state, "home")
    away_stats = _scorecard_stat_lines(state, "away")
    subs_home = int(state.get("home_substitutions", 0))
    subs_away = int(state.get("away_substitutions", 0))
    result = "DRAW" if hg == ag else (f"{home} WIN" if hg > ag else f"{away} WIN")
    is_halftime = "HALF-TIME" in heading.upper() or "HALFTIME" in phase_label.upper()

    lines = [
        f"<b>{heading}</b>",
        "",
        f"🟦 <b>{home}</b> <b>{hg}</b>  —  <b>{ag}</b> <b>{away}</b>",
        f"<b>{phase_label} · {result}</b>",
        "",
        "<b>⚽ GOALS</b>",
        "\n".join(goal_lines[:12]),
        "",
        "<b>📊 MATCH STATS</b>",
        f"<b>{home}</b>",
        html.escape(" · ".join(home_stats[:4])),
        html.escape(" · ".join(home_stats[4:])),
        "",
        f"<b>{away}</b>",
        html.escape(" · ".join(away_stats[:4])),
        html.escape(" · ".join(away_stats[4:])),
    ]
    if is_halftime:
        lines += [
            "",
            "<b>🔁 SUBSTITUTIONS</b>",
            f"🟦 {home}: {subs_home}  ·  🟥 {away}: {subs_away}",
        ]
    else:
        potm = None
        for _, scorer, _ in reversed(entries):
            potm = scorer
            break
        if not potm:
            players = (home_players or []) + (away_players or [])
            if players:
                potm = max(players, key=lambda p: int(p.get("ovr", 75))).get("name", "Player")
        duration = _stat_value(state, "duration", "match_duration")
        if duration == "—":
            duration = f"90+{int(state.get('second_half_stoppage', 0))} minutes"
        lines += [
            "",
            "<b>🔁 SUBSTITUTIONS</b>",
            f"🟦 {home}: {subs_home}  ·  🟥 {away}: {subs_away}",
            "",
            f"🏅 <b>POTM</b> · {html.escape(str(potm or '—'))}",
            f"⏱ <b>Duration</b> · {html.escape(duration)}",
        ]
    if extra:
        lines += ["", extra]
    return "\n".join(lines)


async def _run_extra_time(
    bot: Client, database: MongoDatabase, match_id: str, state: dict[str, Any],
    home_players: list[dict[str, Any]], away_players: list[dict[str, Any]],
    record_kind: str, match_data: dict[str, Any],
) -> None:
    """Play extra time as two real 15-minute periods: 90-105 and 105-120.

    Extra time is entered only after a 90-minute draw. It is never used for a
    match that already has a winner.
    """
    get_record = database.get_group_game if record_kind == "group" else database.get_challenge
    update_record = database.update_group_game if record_kind == "group" else database.update_challenge
    state["extra_time"] = True
    state["extra_time_stoppage_1"] = random.randint(1, 3)
    state["extra_time_stoppage_2"] = random.randint(1, 3)
    await update_record(match_id, {
        "phase": "extra_time_1", "extra_time": True, "live_state": state,
    })
    for period, target in ((1, 105), (2, 120)):
        state["minute"] = 90 if period == 1 else 105
        period_end = target + int(state.get(f"extra_time_stoppage_{period}", 1))
        while state["minute"] < period_end:
            match_data = await get_record(match_id) or match_data
            if match_data.get("status") in {"cancelled", "finished", "declined"}:
                return
            start_minute = int(state.get("minute", 90))
            before_home_goals = int(state.get("home_goals", 0))
            before_away_goals = int(state.get("away_goals", 0))
            # ET keeps the same two-manager response flow, but the passage is
            # resolved immediately once both choices are present.
            active = match_data.get("active_turn", "home")
            action = match_data.get("active_turn_action")
            if not action:
                await asyncio.sleep(WINDOW_POLL_SECONDS)
                continue
            if active == "home":
                await update_record(match_id, {"active_turn": "away", "active_turn_action": None})
                try:
                    await bot.edit_message_text(
                        match_data["chat_id"], match_data["message_id"],
                        _live_text(state, f"{_turn_name(match_data, 'home')} has chosen a move. Manager B responds.", "away", _turn_label(match_data, "away")),
                        reply_markup=_group_live_keyboard(match_id, "away", state=state) if record_kind == "group" else _challenge_live_keyboard(match_id, "away", state=state),
                    )
                except Exception:
                    pass
                continue
            home_players = await database.get_players(match_data.get("home_lineup", [])) or home_players
            away_players = await database.get_players(match_data.get("away_lineup", [])) or away_players
            state, latest = await _advance_window(
                state, home_players, away_players,
                match_data.get("home_tactic", "Balanced"), match_data.get("away_tactic", "Balanced"),
                match_data.get("home_mentality", "Balanced"), match_data.get("away_mentality", "Balanced"),
                max_minute=period_end,
            )
            _attach_missing_goal_metadata(state, before_home_goals, before_away_goals, home_players, away_players, latest)
            latest = _play_by_play(state, home_players, away_players, "away", action, latest, start_minute)
            state.setdefault("commentary", []).extend(latest.splitlines())
            await update_record(match_id, {
                "live_state": state, "active_turn": "home", "active_turn_action": None,
                "phase": f"extra_time_{period}",
            })
            try:
                await bot.edit_message_text(
                    match_data["chat_id"], match_data["message_id"],
                    _live_text(state, latest, "home", _turn_label(match_data, "home")),
                    reply_markup=_group_live_keyboard(match_id, "home", state=state) if record_kind == "group" else _challenge_live_keyboard(match_id, "home", state=state),
                )
            except Exception:
                pass
        # At 105 there is a proper extra-time break; no automatic continuation.
        if period == 1:
            state["minute"] = 105
            await update_record(match_id, {"phase": "extra_time_halftime", "halftime": True, "live_state": state, "half_ready_home": False, "half_ready_away": False})
            report = _football_scorecard(state, home_players, away_players, heading="⏸ EXTRA TIME HALF-TIME SCORECARD", phase_label="105' · EXTRA TIME")
            try:
                await bot.edit_message_text(
                    match_data["chat_id"], match_data["message_id"],
                    f"<b>⏸ EXTRA TIME BREAK · 105'</b>\n\nBoth managers must be ready for the final 15 minutes.",
                    reply_markup=_group_halftime_keyboard(match_data) if record_kind == "group" else _challenge_halftime_keyboard(match_data),
                )
                await bot.send_message(match_data["chat_id"], report)
            except Exception:
                pass
            for _ in range(180):
                fresh = await get_record(match_id) or match_data
                if fresh.get("half_ready_home") and fresh.get("half_ready_away"):
                    break
                await asyncio.sleep(1)
            else:
                await update_record(match_id, {"half_ready_home": True, "half_ready_away": True})
            await update_record(match_id, {"halftime": False, "phase": "extra_time_2", "active_turn": "home", "active_turn_action": None})
            match_data = await get_record(match_id) or match_data
            try:
                await bot.edit_message_text(
                    match_data["chat_id"], match_data["message_id"],
                    _live_text(state, "▶️ Extra time second half — Manager A has the first move.", "home", _turn_label(match_data, "home")),
                    reply_markup=_group_live_keyboard(match_id, "home", state=state) if record_kind == "group" else _challenge_live_keyboard(match_id, "home", state=state),
                )
            except Exception:
                pass


    state["minute"] = 120
    await update_record(match_id, {"live_state": state, "phase": "extra_time_complete", "halftime": False, "active_turn": None, "active_turn_action": None})


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
        game["home_team_name"], game["away_team_name"],
        _rating(home_players, game.get("home_rating", 75)),
        _rating(away_players, game.get("away_rating", 75)),
    )
    state["first_half_stoppage"] = random.randint(2, 11)
    state["second_half_stoppage"] = random.randint(2, 6)
    await database.update_group_game(
        game_id,
        {
            "status": "live", "phase": "live", "live_state": state,
            "home_tactic": game.get("home_tactic", "Balanced"),
            "away_tactic": game.get("away_tactic", "Balanced"),
            "home_mentality": game.get("home_mentality", "Balanced"),
            "away_mentality": game.get("away_mentality", "Balanced"),
            "active_turn": "home", "active_turn_action": None,
            "home_window_done": False, "away_window_done": False,
            "home_players": [p.get("player_id") for p in home_players],
            "away_players": [p.get("player_id") for p in away_players],
            "home_lineup": [p.get("player_id") for p in home_players[:11]],
            "away_lineup": [p.get("player_id") for p in away_players[:11]],
            "home_substitutions": 0, "away_substitutions": 0,
            "half_ready_home": False, "half_ready_away": False,
        },
    )
    try:
        await bot.edit_message_text(
            game["chat_id"], game["message_id"],
            _live_text(state, "Kick-off. The first move belongs to Manager A.", "home", _turn_label(game, "home")),
            reply_markup=_group_live_keyboard(game_id, "home", state=state),
        )
    except Exception:
        pass

    halftime_done = False
    match_end_minute = 90 + int(state.get("second_half_stoppage", 3))
    while state["minute"] < match_end_minute:
        game = await database.get_group_game(game_id) or game
        if game.get("status") == "cancelled":
            return
        active = game.get("active_turn", "home")
        action = game.get("active_turn_action")
        if not action:
            await asyncio.sleep(WINDOW_POLL_SECONDS)
            continue

        # First manager has acted: no simulation yet. Hand the ball to the
        # opponent and show only the opponent's six controls.
        if active == "home":
            await database.update_group_game(
                game_id,
                {"active_turn": "away", "active_turn_action": None, "home_window_done": True},
            )
            game = await database.get_group_game(game_id) or game
            try:
                await bot.edit_message_text(
                    game["chat_id"], game["message_id"],
                    _live_text(state, f"{_turn_name(game, 'home')} has made a move. The response is next.", "away", _turn_label(game, "away")),
                    reply_markup=_group_live_keyboard(game_id, "away", state=state),
                )
            except Exception:
                pass
            continue

        # Manager B has responded. Now the two decisions are resolved together
        # and the match engine advances one short match passage.
        start_minute = int(state.get("minute", 0))
        home_players = await database.get_players(game.get("home_lineup", [])) or home_players
        away_players = await database.get_players(game.get("away_lineup", [])) or away_players
        before_home_goals = int(state.get("home_goals", 0))
        before_away_goals = int(state.get("away_goals", 0))
        state, latest = await _advance_window(
            state, home_players, away_players,
            game.get("home_tactic", "Balanced"),
            game.get("away_tactic", "Balanced"),
            game.get("home_mentality", "Balanced"),
            game.get("away_mentality", "Balanced"),
            max_minute=match_end_minute,
        )
        _attach_missing_goal_metadata(state, before_home_goals, before_away_goals, home_players, away_players, latest)
        set_piece = _set_piece_kind(latest)
        if set_piece:
            attacking_side = _set_piece_attacking_side(game, "away" if game.get("active_turn") == "away" else "home")
            # The action that created the passage stays hidden; only the set-piece decision is public.
            await database.update_group_game(game_id, {
                "pending_set_piece": set_piece, "set_piece_taker": None, "set_piece_defence": None,
                "set_piece_side": attacking_side,
            })
            game = await database.get_group_game(game_id) or game
            taker_players = await database.get_players(game.get(f"{attacking_side}_lineup", [])[:11])
            await bot.edit_message_text(
                game["chat_id"], game["message_id"],
                _live_text(state, f"🎯 A {set_piece.replace('_', ' ')} has been awarded. Choose the taker.", attacking_side, _turn_label(game, attacking_side)),
                reply_markup=_set_piece_keyboard("game", game_id, attacking_side, set_piece, taker_players),
            )
            resolved = await _wait_for_group_set_piece(database, game_id)
            if resolved and resolved.get("set_piece_taker") and resolved.get("set_piece_defence"):
                outcome = _apply_set_piece_result(
                    state, resolved, attacking_side, set_piece, resolved["set_piece_taker"],
                    resolved["set_piece_defence"], taker_players,
                    allow_goal=(int(state.get("home_goals", 0)) == before_home_goals and int(state.get("away_goals", 0)) == before_away_goals),
                )
                state.setdefault("commentary", []).append(f"{_display_minute(state)} {outcome}")
                latest = outcome
            await database.update_group_game(game_id, {"pending_set_piece": None, "set_piece_taker": None, "set_piece_defence": None})
        latest = _play_by_play(state, home_players, away_players, "away", action, latest, start_minute)
        play_lines = latest.splitlines()
        state.setdefault("commentary", []).extend(play_lines)
        await _animate_play(bot, game["chat_id"], game["message_id"], state, play_lines)
        if not game.get("home_tactic"):
            game["home_tactic"] = "Balanced"

        if not halftime_done and state["minute"] >= HALFTIME_MINUTE:
            halftime_done = True
            state["minute"] = HALFTIME_MINUTE
            await database.update_group_game(
                game_id,
                {
                    "live_state": state, "phase": "halftime", "halftime": True,
                    "active_turn": "home", "active_turn_action": None,
                    "home_window_done": False, "away_window_done": False,
                },
            )
            await database.update_group_game(game_id, {"half_ready_home": False, "half_ready_away": False})
            report = _football_scorecard(state, home_players, away_players, heading="⏸ HALF-TIME SCORECARD", phase_label=f"45+{state.get('first_half_stoppage', 0)} · HALF-TIME")
            try:
                await bot.edit_message_text(
                    game["chat_id"], game["message_id"],
                    f"<b>⏸ HALF-TIME</b>\n\nBoth managers can make a substitution, then press <b>READY</b>.\n\nManager A: {'✅ READY' if game.get('half_ready_home') else '⏳ NOT READY'}\nManager B: {'✅ READY' if game.get('half_ready_away') else '⏳ NOT READY'}",
                    reply_markup=_group_halftime_keyboard(game),
                )
                await bot.send_message(game["chat_id"], report)
            except Exception:
                pass
            for _ in range(180):
                game = await database.get_group_game(game_id) or game
                if game.get("status") == "cancelled":
                    return
                if game.get("half_ready_home") and game.get("half_ready_away"):
                    break
                await asyncio.sleep(1)
            else:
                await database.update_group_game(game_id, {"half_ready_home": True, "half_ready_away": True})
            game = await database.get_group_game(game_id) or game
            await database.update_group_game(game_id, {"phase": "live", "halftime": False, "active_turn": "home", "active_turn_action": None})
            try:
                await bot.edit_message_text(game["chat_id"], game["message_id"], _live_text(state, "▶️ SECOND HALF — Manager A has the first move.", "home", _turn_label(game, "home")), reply_markup=_group_live_keyboard(game_id, "home", state=state))
            except Exception:
                pass
            continue

        await database.update_group_game(
            game_id,
            {
                "live_state": state, "active_turn": "home", "active_turn_action": None,
                "home_window_done": False, "away_window_done": False, "phase": "live",
            },
        )
        game = await database.get_group_game(game_id) or game
        try:
            await bot.edit_message_text(
                game["chat_id"], game["message_id"],
                _live_text(state, latest, "home", _turn_label(game, "home")),
                reply_markup=_group_live_keyboard(game_id, "home", state=state),
            )
        except Exception:
            pass

    if state["home_goals"] == state["away_goals"]:
        # 90-minute draw -> extra time. Penalties only if still level after 120.
        await bot.send_message(game["chat_id"], _football_scorecard(state, home_players, away_players, heading="⏱ 90-MINUTE SCORECARD", phase_label="90+" + str(state.get("second_half_stoppage", 0)) + " · END OF REGULATION"))
        await database.update_group_game(game_id, {"active_turn": "home", "active_turn_action": None, "phase": "extra_time_1"})
        await _run_extra_time(bot, database, game_id, state, home_players, away_players, "group", game)
        game = await database.get_group_game(game_id) or game
        if state["home_goals"] == state["away_goals"]:
            await _group_penalty_shootout(bot, database, game_id, home_players, away_players, state)
            return

    finish_live_state(state, home_players, away_players)
    await database.finish_group_game(game_id, state)
    payouts = await _reward_group_match(database, game, state)
    scorecard_text = _football_scorecard(state, home_players, away_players)
    final = f"<b>🏁 FULL TIME</b>\n\n{html.escape(state['home'])} {state['home_goals']} — {state['away_goals']} {html.escape(state['away'])}"
    try:
        await bot.edit_message_text(game["chat_id"], game["message_id"], final)
        await bot.send_message(game["chat_id"], scorecard_text)
        await bot.send_message(game["chat_id"], _full_match_commentary(state))
    except Exception:
        pass
    await _send_match_summary_image(
        bot,
        game["chat_id"],
        state,
        home_players,
        away_players,
        game.get("mode", "Manager Match"),
    )
    reward_text = _reward_text(payouts)
    if reward_text:
        try:
            await bot.send_message(game["chat_id"], reward_text)
        except Exception:
            pass
    await audit(bot, settings, f"Group match finished: <b>{state['home']}</b> {state['home_goals']}-{state['away_goals']} <b>{state['away']}</b>")
    LIVE_TASKS.pop(game_id, None)


async def _group_penalty_shootout(
    bot: Client, database: MongoDatabase, game_id: str,
    home_players: list[dict[str, Any]], away_players: list[dict[str, Any]],
    state: dict[str, Any],
) -> None:
    """Five rounds, manager selects each taker in order; sudden death after round five."""
    await database.update_group_game(
        game_id,
        {
            "status": "live", "phase": "penalties", "penalty_round": 1,
            "penalty_home_score": 0, "penalty_away_score": 0,
            "penalty_home_taker": None, "penalty_away_taker": None,
            "penalty_home_taken": 0, "penalty_away_taken": 0,
        },
    )
    for round_no in range(1, 11):
        game = await database.get_group_game(game_id)
        if not game or game.get("status") == "cancelled":
            return
        side = "home"
        await database.update_group_game(game_id, {
            "penalty_round": round_no,
            "penalty_home_taker": None, "penalty_away_taker": None,
            "penalty_home_taken": 0, "penalty_away_taken": 0,
        })
        try:
            await bot.edit_message_text(
                game["chat_id"], game["message_id"],
                f"<b>🥅 PENALTY SHOOTOUT · ROUND {round_no}</b>\n\n"
                f"🟦 <b>{game['home_team_name']}</b> {game.get('penalty_home_score', 0)}\n"
                f"🟥 <b>{game['away_team_name']}</b> {game.get('penalty_away_score', 0)}\n\n"
                f"@{game.get('host_username') or game.get('host_name', 'Manager A')} — choose your penalty taker:",
                reply_markup=_penalty_keyboard(game_id, "home", home_players),
            )
        except Exception:
            pass
        await _wait_for_penalty_choice(database, game_id, "home")

        game = await database.get_group_game(game_id)
        try:
            await bot.edit_message_text(
                game["chat_id"], game["message_id"],
                f"<b>🥅 PENALTY SHOOTOUT · ROUND {round_no}</b>\n\n"
                f"🟦 <b>{game['home_team_name']}</b> {game.get('penalty_home_score', 0)}\n"
                f"🟥 <b>{game['away_team_name']}</b> {game.get('penalty_away_score', 0)}\n\n"
                f"@{game.get('opponent_username') or game.get('opponent_name', 'Manager B')} — choose your penalty taker:",
                reply_markup=_penalty_keyboard(game_id, "away", away_players),
            )
        except Exception:
            pass
        await _wait_for_penalty_choice(database, game_id, "away")

        game = await database.get_group_game(game_id)
        home_taker = next((p for p in home_players if p.get("player_id") == game.get("penalty_home_taker")), None)
        away_taker = next((p for p in away_players if p.get("player_id") == game.get("penalty_away_taker")), None)
        home_scored = random.random() < (0.72 + (int((home_taker or {}).get("shooting", 75)) - 75) / 300)
        away_scored = random.random() < (0.72 + (int((away_taker or {}).get("shooting", 75)) - 75) / 300)
        if home_scored:
            game["penalty_home_score"] = int(game.get("penalty_home_score", 0)) + 1
        if away_scored:
            game["penalty_away_score"] = int(game.get("penalty_away_score", 0)) + 1
        await database.update_group_game(
            game_id,
            {
                "penalty_home_score": game.get("penalty_home_score", 0),
                "penalty_away_score": game.get("penalty_away_score", 0),
                "penalty_last": f"Round {round_no}: {'⚽' if home_scored else '❌'} / {'⚽' if away_scored else '❌'}",
            },
        )
        try:
            home_name = home_t.get("name", "Home taker") if home_t else "Home taker"
            away_name = away_t.get("name", "Away taker") if away_t else "Away taker"
            await bot.edit_message_text(
                game["chat_id"], game["message_id"],
                f"<b>🥅 PENALTY SHOOTOUT · ROUND {round_no}</b>\n\n"
                f"🟦 {home_name}: {'⚽ SCORED' if home_scored else '❌ SAVED/MISSED'}\n"
                f"🟥 {away_name}: {'⚽ SCORED' if away_scored else '❌ SAVED/MISSED'}\n\n"
                f"Score: <b>{game.get('penalty_home_score', 0)} — {game.get('penalty_away_score', 0)}</b>",
            )
        except Exception:
            pass
        # After five rounds, stop when the score cannot be caught or is won.
        if round_no >= 5:
            remaining = 5 - round_no
            if game.get("penalty_home_score", 0) != game.get("penalty_away_score", 0):
                break
        await asyncio.sleep(0.8)

    game = await database.get_group_game(game_id)
    winner = "home" if game.get("penalty_home_score", 0) > game.get("penalty_away_score", 0) else "away"
    state["penalty_winner"] = winner
    state["home_penalties"] = game.get("penalty_home_score", 0)
    state["away_penalties"] = game.get("penalty_away_score", 0)
    await database.finish_group_game(game_id, state)
    final = (
        f"{live_scorecard(state)}\n\n"
        f"<b>🥅 PENALTIES</b>\n"
        f"🟦 {game['home_team_name']}: {game.get('penalty_home_score', 0)}\n"
        f"🟥 {game['away_team_name']}: {game.get('penalty_away_score', 0)}\n\n"
        f"🏆 Winner: <b>{game['home_team_name'] if winner == 'home' else game['away_team_name']}</b>"
    )
    try:
        await bot.edit_message_text(game["chat_id"], game["message_id"], final)
        await bot.send_message(game["chat_id"], _full_match_commentary(state))
    except Exception:
        pass
    await _send_match_summary_image(
        bot,
        game["chat_id"],
        state,
        home_players,
        away_players,
        game.get("mode", "Manager Match"),
    )
    payouts = await _reward_group_match(database, game, state)
    reward_text = _reward_text(payouts)
    if reward_text:
        try:
            await bot.send_message(game["chat_id"], reward_text)
        except Exception:
            pass
    LIVE_TASKS.pop(game_id, None)


async def _wait_for_penalty_choice(database: MongoDatabase, game_id: str, side: str) -> None:
    for _ in range(12):
        game = await database.get_group_game(game_id)
        if not game or game.get("status") == "cancelled":
            return
        if game.get(f"penalty_{side}_taker"):
            return
        await asyncio.sleep(1)
    # AFK fallback: choose the first available player.
    game = await database.get_group_game(game_id)
    if game and not game.get(f"penalty_{side}_taker"):
        players = game.get(f"{side}_players", [])
        if players:
            await database.update_group_game(game_id, {f"penalty_{side}_taker": players[0]})


async def _start_group_match(bot: Client, database: MongoDatabase, settings: Settings, game: dict[str, Any], message: Message) -> None:
    await database.update_group_game(game["game_id"], {"phase": "live", "status": "live", "message_id": message.id})
    LIVE_TASKS[game["game_id"]] = asyncio.create_task(_run_group_match(bot, database, settings, game["game_id"]))
    await message.edit_text(
        "<b>🔴 KICK-OFF</b>\n\n"
        f"<b>{_turn_name(game, 'home')}</b> — your team has the first turn.\n"
        "Choose 1 of 6 actions. Then the opponent responds and the passage is simulated.",
        reply_markup=_group_live_keyboard(game["game_id"], "home"),
    )


def _winner_side(state: dict[str, Any]) -> str | None:
    penalty_winner = state.get("penalty_winner")
    if penalty_winner in {"home", "away"}:
        return penalty_winner
    if state["home_goals"] > state["away_goals"]:
        return "home"
    if state["away_goals"] > state["home_goals"]:
        return "away"
    return None


async def _reward_group_match(
    database: MongoDatabase,
    game: dict[str, Any],
    state: dict[str, Any],
) -> list[tuple[str, int, int, str]]:
    winner = _winner_side(state)
    draw = winner is None
    payouts = []
    for user_id, is_home, manager_name in (
        (game.get("host_id"), True, game.get("host_name", "Manager A")),
        (game.get("opponent_id"), False, game.get("opponent_name", "Manager B")),
    ):
        if not user_id:
            continue
        won = winner == ("home" if is_home else "away")
        coins = random.randint(WIN_BONUS_MIN_COINS, WIN_BONUS_MAX_COINS) if won else random.randint(5, 25) if draw else 0
        xp = WIN_BONUS_XP if won else WIN_BONUS_XP // 2
        outcome = "win" if won else "draw"
        if draw is False and not won:
            outcome = "loss"
            xp = 0
        if await database.award_match_result("group", game["game_id"], user_id, coins, xp, outcome) and (won or draw):
            payouts.append((manager_name, coins, xp, outcome))
    return payouts


async def _reward_challenge(
    database: MongoDatabase,
    challenge: dict[str, Any],
    state: dict[str, Any],
) -> list[tuple[str, int, int, str]]:
    winner = _winner_side(state)
    draw = winner is None
    payouts = []
    for user_id, is_home, manager_name in (
        (challenge["challenger_id"], True, challenge.get("challenger_name", "Manager A")),
        (challenge["challenged_id"], False, challenge.get("challenged_name", "Manager B")),
    ):
        won = winner == ("home" if is_home else "away")
        coins, xp = 1500, 250
        if won:
            coins += random.randint(WIN_BONUS_MIN_COINS, WIN_BONUS_MAX_COINS)
            xp += WIN_BONUS_XP
        outcome = "win" if won else "draw" if draw else "loss"
        if await database.award_match_result("challenge", challenge["challenge_id"], user_id, coins, xp, outcome):
            payouts.append((manager_name, coins, xp, outcome))
    return payouts


def _reward_text(payouts: list[tuple[str, int, int, str]]) -> str:
    if not payouts:
        return ""
    lines = ["<b>🎁 MATCH REWARDS</b>"]
    for name, coins, xp, outcome in payouts:
        label = "Winner" if outcome == "win" else "Draw share" if outcome == "draw" else "Participation"
        lines.append(f"🏅 {html.escape(str(name))} · {label}: <b>+{coins:,} coins</b> · +{xp} XP")
    return "\n".join(lines)


async def _challenge_penalty_shootout(
    bot: Client, database: MongoDatabase, challenge: dict[str, Any],
    home_players: list[dict[str, Any]], away_players: list[dict[str, Any]], state: dict[str, Any],
) -> None:
    cid = challenge["challenge_id"]
    await database.update_challenge(
        cid,
        {"status": "live", "phase": "penalties", "penalty_round": 1, "penalty_home_score": 0, "penalty_away_score": 0,
         "penalty_home_taker": None, "penalty_away_taker": None},
    )
    for round_no in range(1, 11):
        challenge = await database.get_challenge(cid) or challenge
        await database.update_challenge(cid, {"penalty_round": round_no, "penalty_home_taker": None, "penalty_away_taker": None})
        try:
            await bot.edit_message_text(
                challenge["chat_id"], challenge["message_id"],
                f"<b>🥅 PENALTIES · ROUND {round_no}</b>\n\n"
                f"🟦 {challenge['challenger_name']} — choose your penalty taker:",
                reply_markup=_penalty_keyboard(cid, "home", home_players, "live"),
            )
        except Exception:
            pass
        for side, players in (("home", home_players), ("away", away_players)):
            if side == "away":
                try:
                    await bot.edit_message_text(
                        challenge["chat_id"], challenge["message_id"],
                        f"<b>🥅 PENALTIES · ROUND {round_no}</b>\n\n"
                        f"🟦 {challenge.get('challenger_name', 'Manager A')} {challenge.get('penalty_home_score', 0)}\n"
                        f"🟥 {challenge.get('challenged_name', 'Manager B')} {challenge.get('penalty_away_score', 0)}\n\n"
                        f"🟥 <b>{challenge.get('challenged_name', 'Manager B')}</b> — choose your penalty taker:",
                        reply_markup=_penalty_keyboard(cid, "away", away_players, "live"),
                    )
                except Exception:
                    pass
            for _ in range(12):
                challenge = await database.get_challenge(cid) or challenge
                if challenge.get(f"penalty_{side}_taker"):
                    break
                await asyncio.sleep(1)
            challenge = await database.get_challenge(cid) or challenge
            if not challenge.get(f"penalty_{side}_taker") and players:
                await database.update_challenge(cid, {f"penalty_{side}_taker": players[0]["player_id"]})
        challenge = await database.get_challenge(cid) or challenge
        home_t = next((p for p in home_players if p.get("player_id") == challenge.get("penalty_home_taker")), None)
        away_t = next((p for p in away_players if p.get("player_id") == challenge.get("penalty_away_taker")), None)
        hs = random.random() < (0.72 + (int((home_t or {}).get("shooting", 75)) - 75) / 300)
        as_ = random.random() < (0.72 + (int((away_t or {}).get("shooting", 75)) - 75) / 300)
        if hs:
            challenge["penalty_home_score"] = int(challenge.get("penalty_home_score", 0)) + 1
        if as_:
            challenge["penalty_away_score"] = int(challenge.get("penalty_away_score", 0)) + 1
        await database.update_challenge(cid, {
            "penalty_home_score": challenge.get("penalty_home_score", 0),
            "penalty_away_score": challenge.get("penalty_away_score", 0),
        })
        try:
            home_name = home_t.get("name", "Home taker") if home_t else "Home taker"
            away_name = away_t.get("name", "Away taker") if away_t else "Away taker"
            await bot.edit_message_text(
                challenge["chat_id"], challenge["message_id"],
                f"<b>🥅 PENALTY SHOOTOUT · ROUND {round_no}</b>\n\n"
                f"🟦 {home_name}: {'⚽ SCORED' if hs else '❌ SAVED/MISSED'}\n"
                f"🟥 {away_name}: {'⚽ SCORED' if as_ else '❌ SAVED/MISSED'}\n\n"
                f"Score: <b>{challenge.get('penalty_home_score', 0)} — {challenge.get('penalty_away_score', 0)}</b>",
            )
        except Exception:
            pass
        if round_no >= 5 and challenge.get("penalty_home_score") != challenge.get("penalty_away_score"):
            break
    challenge = await database.get_challenge(cid) or challenge
    state["penalty_winner"] = "home" if challenge.get("penalty_home_score", 0) > challenge.get("penalty_away_score", 0) else "away"
    state["home_penalties"] = challenge.get("penalty_home_score", 0)
    state["away_penalties"] = challenge.get("penalty_away_score", 0)
    await database.finish_challenge(cid, state)
    final = (
        f"{live_scorecard(state)}\n\n<b>🥅 PENALTIES</b>\n"
        f"🟦 {challenge['challenger_name']}: {state['home_penalties']}\n"
        f"🟥 {challenge['challenged_name']}: {state['away_penalties']}"
    )
    try:
        await bot.edit_message_text(challenge["chat_id"], challenge["message_id"], final)
        await bot.send_message(challenge["chat_id"], _full_match_commentary(state))
    except Exception:
        pass
    await _send_match_summary_image(
        bot,
        challenge["chat_id"],
        state,
        home_players,
        away_players,
        "Manager Challenge",
    )
    payouts = await _reward_challenge(database, challenge, state)
    reward_text = _reward_text(payouts)
    if reward_text:
        try:
            await bot.send_message(challenge["chat_id"], reward_text)
        except Exception:
            pass
    LIVE_TASKS.pop(cid, None)


async def _run_challenge(bot: Client, database: MongoDatabase, settings: Settings, challenge_id: str) -> None:
    challenge = await database.get_challenge(challenge_id)
    if not challenge:
        return
    home_players = await database.get_players(challenge.get("home_lineup", []))
    away_players = await database.get_players(challenge.get("away_lineup", []))
    state = challenge.get("live_state") or new_live_state(
        challenge["challenger_name"], challenge["challenged_name"],
        _rating(home_players), _rating(away_players),
    )
    state.setdefault("first_half_stoppage", random.randint(2, 11))
    state.setdefault("second_half_stoppage", random.randint(2, 6))
    await database.update_challenge(
        challenge_id,
        {
            "active_turn": "home", "active_turn_action": None,
            "home_window_done": False, "away_window_done": False,
            "half_ready_home": False, "half_ready_away": False,
        },
    )
    try:
        await bot.edit_message_text(
            challenge["chat_id"], challenge["message_id"],
            _live_text(state, "Kick-off. Manager A has the first move.", "home", challenge.get("challenger_name", "Manager A")),
            reply_markup=_challenge_live_keyboard(challenge_id, "home", state=state),
        )
    except Exception:
        pass

    halftime_done = False
    match_end_minute = 90 + int(state.get("second_half_stoppage", 3))
    while state["minute"] < match_end_minute:
        challenge = await database.get_challenge(challenge_id) or challenge
        if challenge.get("status") in {"finished", "cancelled", "declined"}:
            return
        active = challenge.get("active_turn", "home")
        action = challenge.get("active_turn_action")
        if not action:
            await asyncio.sleep(WINDOW_POLL_SECONDS)
            continue

        if active == "home":
            await database.update_challenge(
                challenge_id,
                {"active_turn": "away", "active_turn_action": None, "home_window_done": True},
            )
            challenge = await database.get_challenge(challenge_id) or challenge
            try:
                await bot.edit_message_text(
                    challenge["chat_id"], challenge["message_id"],
                    _live_text(state, f"{challenge.get('challenger_name', 'Manager A')} has made a move. The response is next.", "away", challenge.get("challenged_name", "Manager B")),
                    reply_markup=_challenge_live_keyboard(challenge_id, "away", state=state),
                )
            except Exception:
                pass
            continue

        start_minute = int(state.get("minute", 0))
        home_players = await database.get_players(challenge.get("home_lineup", []))
        away_players = await database.get_players(challenge.get("away_lineup", []))
        before_home_goals = int(state.get("home_goals", 0))
        before_away_goals = int(state.get("away_goals", 0))
        state, latest = await _advance_window(
            state,
            home_players,
            away_players,
            challenge.get("home_tactic", "Balanced"),
            challenge.get("away_tactic", "Balanced"),
            challenge.get("home_mentality", "Balanced"),
            challenge.get("away_mentality", "Balanced"),
            max_minute=match_end_minute,
        )
        _attach_missing_goal_metadata(state, before_home_goals, before_away_goals, home_players, away_players, latest)
        set_piece = _set_piece_kind(latest)
        if set_piece:
            attacking_side = _set_piece_attacking_side(challenge, "away" if challenge.get("active_turn") == "away" else "home")
            await database.update_challenge(challenge_id, {
                "pending_set_piece": set_piece, "set_piece_taker": None, "set_piece_defence": None,
                "set_piece_side": attacking_side,
            })
            challenge = await database.get_challenge(challenge_id) or challenge
            taker_players = await database.get_players(challenge.get(f"{attacking_side}_lineup", [])[:11])
            await bot.edit_message_text(
                challenge["chat_id"], challenge["message_id"],
                _live_text(state, f"🎯 A {set_piece.replace('_', ' ')} has been awarded. Choose the taker.", attacking_side, challenge.get("challenger_name" if attacking_side == "home" else "challenged_name", "Manager")),
                reply_markup=_set_piece_keyboard("live", challenge_id, attacking_side, set_piece, taker_players),
            )
            resolved = await _wait_for_challenge_set_piece(database, challenge_id)
            if resolved and resolved.get("set_piece_taker") and resolved.get("set_piece_defence"):
                outcome = _apply_set_piece_result(
                    state, resolved, attacking_side, set_piece, resolved["set_piece_taker"],
                    resolved["set_piece_defence"], taker_players,
                    allow_goal=(int(state.get("home_goals", 0)) == before_home_goals and int(state.get("away_goals", 0)) == before_away_goals),
                )
                state.setdefault("commentary", []).append(f"{_display_minute(state)} {outcome}")
                latest = outcome
            await database.update_challenge(challenge_id, {"pending_set_piece": None, "set_piece_taker": None, "set_piece_defence": None})
        latest = _play_by_play(state, home_players, away_players, "away", action, latest, start_minute)
        play_lines = latest.splitlines()
        state.setdefault("commentary", []).extend(play_lines)
        await _animate_play(bot, challenge["chat_id"], challenge["message_id"], state, play_lines)

        if not challenge.get("halftime") and state.get("minute", 0) >= HALFTIME_MINUTE + int(state.get("first_half_stoppage", 3)):
            state["minute"] = HALFTIME_MINUTE
            halftime_done = True
            await database.update_challenge(
                challenge_id,
                {
                    "live_state": state, "halftime": True,
                    "active_turn": "home", "active_turn_action": None,
                    "home_window_done": False, "away_window_done": False,
                },
            )
            await database.update_challenge(challenge_id, {"half_ready_home": False, "half_ready_away": False})
            report = _football_scorecard(state, home_players, away_players, heading="⏸ HALF-TIME SCORECARD", phase_label=f"45+{state.get('first_half_stoppage', 0)} · HALF-TIME")
            try:
                await bot.edit_message_text(challenge["chat_id"], challenge["message_id"], f"<b>⏸ HALF-TIME</b>\n\nBoth managers can make a substitution, then press <b>READY</b>.\n\nManager A: ⏳ NOT READY\nManager B: ⏳ NOT READY", reply_markup=_challenge_halftime_keyboard(challenge))
                await bot.send_message(challenge["chat_id"], report)
            except Exception:
                pass
            for _ in range(180):
                challenge = await database.get_challenge(challenge_id) or challenge
                if challenge.get("status") in {"finished", "cancelled", "declined"}:
                    return
                if challenge.get("half_ready_home") and challenge.get("half_ready_away"):
                    break
                await asyncio.sleep(1)
            else:
                await database.update_challenge(challenge_id, {"half_ready_home": True, "half_ready_away": True})
            challenge = await database.get_challenge(challenge_id) or challenge
            await database.update_challenge(challenge_id, {"halftime": False, "active_turn": "home", "active_turn_action": None, "phase": "live"})
            try:
                await bot.edit_message_text(challenge["chat_id"], challenge["message_id"], _live_text(state, "▶️ SECOND HALF — Manager A has the first move.", "home", challenge.get("challenger_name", "Manager A")), reply_markup=_challenge_live_keyboard(challenge_id, "home", state=state))
            except Exception:
                pass
            continue

        await database.update_challenge(
            challenge_id,
            {
                "live_state": state, "active_turn": "home", "active_turn_action": None,
                "home_window_done": False, "away_window_done": False,
            },
        )
        challenge = await database.get_challenge(challenge_id) or challenge
        try:
            await bot.edit_message_text(
                challenge["chat_id"], challenge["message_id"],
                _live_text(state, latest, "home", challenge.get("challenger_name", "Manager A")),
                reply_markup=_challenge_live_keyboard(challenge_id, "home", state=state),
            )
        except Exception:
            pass

    if state["home_goals"] == state["away_goals"]:
        await bot.send_message(challenge["chat_id"], _football_scorecard(state, home_players, away_players, heading="⏱ 90-MINUTE SCORECARD", phase_label="90+" + str(state.get("second_half_stoppage", 0)) + " · END OF REGULATION"))
        await database.update_challenge(challenge_id, {"active_turn": "home", "active_turn_action": None, "phase": "extra_time_1"})
        await _run_extra_time(bot, database, challenge_id, state, home_players, away_players, "challenge", challenge)
        challenge = await database.get_challenge(challenge_id) or challenge
        if state["home_goals"] == state["away_goals"]:
            await _challenge_penalty_shootout(bot, database, challenge, home_players, away_players, state)
            return

    finish_live_state(state, home_players, away_players)
    await database.finish_challenge(challenge_id, state)
    challenge = await database.get_challenge(challenge_id) or challenge
    ai_line = await generate_match_summary(
        settings,
        challenge["challenger_name"], challenge["challenged_name"],
        state["home_goals"], state["away_goals"],
        _scorer_of_state(state, home_players), "Manager Challenge",
    )
    scorecard_text = _football_scorecard(state, home_players, away_players, f"<b>Commentator</b>\n{html.escape(ai_line)}")
    final = f"<b>🏁 FULL TIME</b>\n\n{html.escape(state['home'])} {state['home_goals']} — {state['away_goals']} {html.escape(state['away'])}"
    try:
        await bot.edit_message_text(challenge["chat_id"], challenge["message_id"], final)
        await bot.send_message(challenge["chat_id"], scorecard_text)
        await bot.send_message(challenge["chat_id"], _full_match_commentary(state))
    except Exception:
        pass
    for user_id in (challenge["challenger_id"], challenge["challenged_id"]):
        try:
            await bot.send_message(user_id, scorecard_text)
            await bot.send_message(user_id, _full_match_commentary(state))
        except Exception:
            pass
    await _send_match_summary_image(
        bot,
        challenge["chat_id"],
        state,
        home_players,
        away_players,
        "Manager Challenge",
    )
    payouts = await _reward_challenge(database, challenge, state)
    reward_text = _reward_text(payouts)
    if reward_text:
        try:
            await bot.send_message(challenge["chat_id"], reward_text)
        except Exception:
            pass
    await audit(bot, settings, f"Manager challenge finished: <b>{challenge['challenger_name']}</b> {state['home_goals']}-{state['away_goals']} <b>{challenge['challenged_name']}</b>")
    LIVE_TASKS.pop(challenge_id, None)


def _scorer_of_state(state: dict[str, Any], players: list[dict[str, Any]]) -> str:
    events = state.get("events", [])
    for event in reversed(events):
        if event.get("team") == "home":
            return event.get("scorer", "Captain")
    return players[0].get("name", "Captain") if players else "Captain"


def _set_piece_keyboard(prefix: str, match_id: str, side: str, kind: str, players: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(players), 2):
        rows.append([
            InlineKeyboardButton(
                f"⚽ {p.get('name', 'Player')[:18]}",
                callback_data=f"{prefix}:setpiece:{match_id}:{side}:{kind}:{p.get('player_id')}",
            ) for p in players[i:i+2]
        ])
    return InlineKeyboardMarkup(rows)


def _set_piece_defence_keyboard(prefix: str, match_id: str, side: str) -> InlineKeyboardMarkup:
    values = [("🧱 Mark Tight", "mark"), ("🧤 Guard Zone", "zone"), ("⚡ Counter Ready", "counter"), ("🚀 Clear Long", "clear")]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"{prefix}:setdef:{match_id}:{side}:{value}") for label, value in values[i:i+2]]
        for i in range(0, len(values), 2)
    ])


async def _wait_for_group_set_piece(database: MongoDatabase, game_id: str) -> dict[str, Any] | None:
    for _ in range(25):
        game = await database.get_group_game(game_id)
        if not game or game.get("status") == "cancelled":
            return None
        if game.get("set_piece_taker") and game.get("set_piece_defence"):
            return game
        await asyncio.sleep(1)
    game = await database.get_group_game(game_id)
    if game:
        updates = {}
        if not game.get("set_piece_taker"):
            lineup = game.get(f"{game.get('set_piece_side', 'home')}_lineup", [])[:11]
            if lineup:
                updates["set_piece_taker"] = lineup[0]
        if not game.get("set_piece_defence"):
            updates["set_piece_defence"] = "zone"
        if updates:
            await database.update_group_game(game_id, updates)
            game = await database.get_group_game(game_id)
    return game


async def _wait_for_challenge_set_piece(database: MongoDatabase, challenge_id: str) -> dict[str, Any] | None:
    for _ in range(25):
        challenge = await database.get_challenge(challenge_id)
        if not challenge or challenge.get("status") in {"finished", "cancelled", "declined"}:
            return None
        if challenge.get("set_piece_taker") and challenge.get("set_piece_defence"):
            return challenge
        await asyncio.sleep(1)
    challenge = await database.get_challenge(challenge_id)
    if challenge:
        updates = {}
        if not challenge.get("set_piece_taker"):
            lineup = challenge.get(f"{challenge.get('set_piece_side', 'home')}_lineup", [])[:11]
            if lineup:
                updates["set_piece_taker"] = lineup[0]
        if not challenge.get("set_piece_defence"):
            updates["set_piece_defence"] = "zone"
        if updates:
            await database.update_challenge(challenge_id, updates)
            challenge = await database.get_challenge(challenge_id)
    return challenge


def _set_piece_attacking_side(match_data: dict[str, Any], fallback: str = "home") -> str:
    """Infer the side awarded the dead ball from the last manager choices."""
    home_action = match_data.get("home_last_action")
    away_action = match_data.get("away_last_action")
    attacking = {"Attacking", "Wide", "Possession", "Counter"}
    defending = {"Defensive", "Press"}
    if home_action in attacking and away_action in defending:
        return "home"
    if away_action in attacking and home_action in defending:
        return "away"
    return fallback


def _apply_set_piece_result(state: dict[str, Any], match_data: dict[str, Any], attacking_side: str,
                            kind: str, taker_id: str, defence: str,
                            attacking_players: list[dict[str, Any]], allow_goal: bool = True) -> str:
    taker = next((p for p in attacking_players if p.get("player_id") == taker_id), None) or {}
    taker_name = str(taker.get("name", "Set-piece taker"))
    minute = int(state.get("minute", 0))
    if kind == "corner":
        state[f"{attacking_side}_corners"] = int(state.get(f"{attacking_side}_corners", 0)) + 1
        state.setdefault("events", []).append({
            "type": "corner", "team": attacking_side, "minute": minute, "taker": taker_name,
        })
    chance = 0.08 if kind == "corner" else 0.13
    chance += (int(taker.get("shooting", 75)) - 75) / 450
    chance += {"mark": -0.035, "zone": -0.02, "counter": 0.005, "clear": -0.05}.get(defence, 0)
    chance = max(0.02, min(0.32, chance))
    scored = allow_goal and random.random() < chance
    if scored:
        state[f"{attacking_side}_goals"] = int(state.get(f"{attacking_side}_goals", 0)) + 1
        state.setdefault("events", []).append({
            "type": "goal", "goal": True, "team": attacking_side,
            "scorer": taker_name, "minute": minute,
            "source": kind,
        })
        return f"⚽ GOAL! {taker_name} finishes the {kind.replace('_', ' ')}."
    return f"{taker_name} delivers the {kind.replace('_', ' ')} — the defence deals with it ({defence.replace('_', ' ')})."


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
        requested_mode = message.text.split()[0].split("@", 1)[0][1:].lower()
        mode = _canonical_mode(requested_mode)
        competition = await database.get_competition(mode)
        if not competition and requested_mode != mode:
            mode = requested_mode
            competition = await database.get_competition(mode)
        if not competition:
            await message.reply_text(
                f"<b>/{requested_mode}</b> is not configured. "
                f"The owner can create it with <code>/addcompetition {requested_mode} | Name | 🏆 | CLUB</code>."
            )
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
        await database.update_group_game(
            game["game_id"],
            {"host_username": getattr(message.from_user, "username", None)},
        )
        game = await database.get_group_game(game["game_id"]) or game
        await message.reply_text(_mode_text(game, competition), reply_markup=_lobby_keyboard(game["game_id"]))

    @bot.on_callback_query(filters.regex(r"^mode:([a-z0-9_-]+)$"))
    async def mode_menu_handler(_: Client, query: CallbackQuery) -> None:
        await query.answer()
        competition = await database.get_competition(query.data.split(":", 1)[1])
        if not competition:
            await query.message.edit_text("That owner-created competition is not available.")
            return
        await query.message.edit_text(
            f"{competition.get('emoji', '🏆')} <b>{competition['name']}</b>\n\n"
            f"Use /{_mode_command(competition['competition_key'])} in a group to open its one-match lobby.",
            reply_markup=back_keyboard("Back to Arena", "menu:arena"),
        )

    @bot.on_callback_query(filters.regex(r"^game:(join|back|cancel|cancel_confirm|cancel_abort):([a-f0-9]+)$"))
    async def group_lobby_handler(_: Client, query: CallbackQuery) -> None:
        action, game_id = query.data.split(":")[1:]
        game = await database.get_group_game(game_id)
        if not game or game.get("status") in {"finished", "cancelled"}:
            await query.answer("This game is already closed.", show_alert=True)
            return

        if action == "back":
            if game.get("status") not in {"lobby", "setup"} or not await _cancel_group_game(bot, database, game, query.from_user):
                await query.answer("Only a manager can leave this match.", show_alert=True)
                return
            await query.answer("Returned to Arena.")
            competitions = await database.list_competitions()
            await query.message.edit_text(
                "<b>🔥 ARENA</b>\n\nChoose a group competition.",
                reply_markup=arena_keyboard(competitions),
            )
            return

        if action in {"cancel", "cancel_confirm", "cancel_abort"}:
            if action == "cancel":
                uid = query.from_user.id
                allowed = uid in {game.get("host_id"), game.get("opponent_id")}
                if not allowed:
                    try:
                        member = await bot.get_chat_member(game["chat_id"], uid)
                        allowed = str(member.status).lower().split(".")[-1] in {"administrator", "owner"}
                    except Exception:
                        allowed = False
                if not allowed:
                    await query.answer("Only the two managers or a group admin can cancel.", show_alert=True)
                    return
                await query.answer("Confirm cancellation.")
                await query.message.edit_text(
                    "<b>🛑 CANCEL MATCH?</b>\n\nThis will forcefully end the current game for everyone.\n\n"
                    "Are you sure?",
                    reply_markup=_cancel_confirm_keyboard(game_id),
                )
                return

            if action == "cancel_abort":
                await query.answer("Game kept active.")
                competition = await database.get_competition(game["mode"])
                await query.message.edit_text(
                    _mode_text(game, competition),
                    reply_markup=_lobby_keyboard(game_id) if game.get("status") == "lobby" else _group_live_keyboard(game_id),
                )
                return

            if not await _cancel_group_game(bot, database, game, query.from_user):
                await query.answer("You are not allowed to cancel this game.", show_alert=True)
                return
            await query.answer("Game cancelled.")
            await query.message.edit_text("🛑 <b>GAME CANCELLED</b>\n\nThe match was forcefully cancelled by a manager or group admin.")
            return

        # join
        if game.get("status") != "lobby":
            await query.answer("This lobby is closed.", show_alert=True)
            return
        if query.from_user.id == game["host_id"]:
            await query.answer("You are already the host.", show_alert=True)
            return
        await database.update_group_game(
            game_id,
            {
                "status": "setup", "phase": "team_host",
                "opponent_id": query.from_user.id,
                "opponent_name": query.from_user.first_name or "Manager B",
                "opponent_username": getattr(query.from_user, "username", None),
            },
        )
        game = await database.get_group_game(game_id)
        competition = await database.get_competition(game["mode"])
        await query.answer("You joined the match.")
        await query.message.edit_text(_mode_text(game, competition), reply_markup=_team_keyboard(competition, game_id))


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
        if next_phase == "formation_host":
            host_user = await database.get_user(game.get("host_id")) or {}
            formation_rows = _formation_rows(
                "game:formation",
                game_id,
                unlocked_formations(host_user),
                back_callback=f"game:back:{game_id}",
            )
            await query.message.edit_text(_mode_text(game, competition), reply_markup=InlineKeyboardMarkup(formation_rows))
        else:
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
        user = await database.get_user(query.from_user.id) or {}
        if formation not in unlocked_formations(user):
            await query.answer("Level up to unlock that formation.", show_alert=True)
            return
        field = "home_formation" if game.get("phase") == "formation_host" else "away_formation"
        next_phase = "formation_away" if field == "home_formation" else "ready"
        await database.update_group_game(game_id, {field: formation, "phase": next_phase})
        game = await database.get_group_game(game_id)
        competition = await database.get_competition(game["mode"])
        await query.answer(f"{formation} selected.")
        if next_phase == "formation_away":
            opponent_user = await database.get_user(game.get("opponent_id")) or {}
            formation_rows = _formation_rows(
                "game:formation",
                game_id,
                unlocked_formations(opponent_user),
                back_callback=f"game:back:{game_id}",
            )
            await query.message.edit_text(
                _mode_text(game, competition),
                reply_markup=InlineKeyboardMarkup(formation_rows),
            )
        else:
            await query.message.edit_text(
                _mode_text(game, competition),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton(f"✅ {_team_side_label(game, 'home')} · Ready", callback_data=f"game:matchready:{game_id}:home", style=ButtonStyle.SUCCESS),
                     InlineKeyboardButton(f"✅ {_team_side_label(game, 'away')} · Ready", callback_data=f"game:matchready:{game_id}:away", style=ButtonStyle.SUCCESS)],
                    ]
                ),
            )

    @bot.on_callback_query(filters.regex(r"^game:live:turn:([a-f0-9]+):(home|away):([A-Za-z_]+)$"))
    async def group_live_turn_handler(_: Client, query: CallbackQuery) -> None:
        parts = query.data.split(":")
        game_id, side, value = parts[3], parts[4], parts[5].replace("_", " ")
        game = await database.get_group_game(game_id)
        if not game or game.get("status") != "live":
            await query.answer("This match is no longer live.", show_alert=True)
            return
        expected = game.get("host_id") if side == "home" else game.get("opponent_id")
        if query.from_user.id != expected:
            await query.answer("Wait for your manager turn.", show_alert=True)
            return
        if game.get("active_turn") != side:
            await query.answer("It is not your team's turn.", show_alert=True)
            return
        if game.get("active_turn_action"):
            await query.answer("Your move is already locked.", show_alert=True)
            return
        valid = {item[1] for item in LIVE_ACTIONS}
        if value not in valid:
            await query.answer("Invalid match action.", show_alert=True)
            return
        updates = {"active_turn_action": value, f"{side}_last_action": value}
        if value in {"Possession", "Counter", "Press", "Wide"}:
            updates[f"{side}_tactic"] = value
        else:
            updates[f"{side}_mentality"] = value
        await database.update_group_game(game_id, updates)
        await query.answer(f"{value} selected. Waiting for the other team.")

    @bot.on_callback_query(filters.regex(r"^game:sub:([a-f0-9]+):(home|away)$"))
    async def group_sub_panel_handler(_: Client, query: CallbackQuery) -> None:
        game_id, side = query.data.split(":")[2:]
        game = await database.get_group_game(game_id)
        expected = game.get("host_id") if side == "home" else game.get("opponent_id") if game else None
        if not game or query.from_user.id != expected or not game.get("halftime"):
            await query.answer("Substitutions are only available to the manager during half-time.", show_alert=True)
            return
        if int(game.get(f"{side}_substitutions", 0)) >= 3:
            await query.answer("Maximum 3 substitutions used.", show_alert=True)
            return
        lineup = game.get(f"{side}_lineup", [])[:11]
        players = await database.get_players(lineup)
        if not players:
            await query.answer("Starting XI unavailable.", show_alert=True)
            return
        await query.answer("Choose the player to take off.")
        await query.message.edit_text(
            f"<b>🔁 {html.escape(_turn_name(game, side))} · SUBSTITUTION</b>\n\nChoose the player coming off:",
            reply_markup=_player_rows(
                "game", game_id, side, players, "subout",
                back_callback=f"game:subback:{game_id}:{side}",
            ),
        )

    @bot.on_callback_query(filters.regex(r"^game:subout:([a-f0-9]+):(home|away):(.+)$"))
    async def group_sub_out_handler(_: Client, query: CallbackQuery) -> None:
        parts = query.data.split(":")
        game_id, side, player_id = parts[2], parts[3], ":".join(parts[4:])
        game = await database.get_group_game(game_id)
        expected = game.get("host_id") if side == "home" else game.get("opponent_id") if game else None
        if not game or query.from_user.id != expected or not game.get("halftime"):
            await query.answer("That substitution is no longer available.", show_alert=True)
            return
        lineup = game.get(f"{side}_lineup", [])[:11]
        bench_ids = [pid for pid in game.get(f"{side}_players", []) if pid not in lineup]
        if player_id not in lineup or not bench_ids:
            await query.answer("No valid substitute is available.", show_alert=True)
            await query.message.edit_text(
                "<b>🔁 SUBSTITUTION</b>\n\nNo valid substitute is available.",
                reply_markup=_group_halftime_keyboard(game),
            )
            return
        bench = await database.get_players(bench_ids)
        await database.update_group_game(game_id, {f"pending_{side}_out": player_id})
        await query.answer("Now choose the incoming player.")
        await query.message.edit_text(
            "<b>🔁 CHOOSE INCOMING PLAYER</b>\n\nSelect the substitute:",
            reply_markup=_player_rows(
                "game", game_id, side, bench, "subin",
                back_callback=f"game:subback:{game_id}:{side}",
            ),
        )

    @bot.on_callback_query(filters.regex(r"^game:subin:([a-f0-9]+):(home|away):(.+)$"))
    async def group_sub_in_handler(_: Client, query: CallbackQuery) -> None:
        parts = query.data.split(":")
        game_id, side, incoming = parts[2], parts[3], ":".join(parts[4:])
        game = await database.get_group_game(game_id)
        expected = game.get("host_id") if side == "home" else game.get("opponent_id") if game else None
        if not game or query.from_user.id != expected or not game.get("halftime"):
            await query.answer("That substitution is no longer available.", show_alert=True)
            return
        outgoing = game.get(f"pending_{side}_out")
        lineup = game.get(f"{side}_lineup", [])[:11]
        bench = [pid for pid in game.get(f"{side}_players", []) if pid not in lineup]
        if not outgoing or outgoing not in lineup or incoming not in bench:
            await query.answer("That substitute is no longer available.", show_alert=True)
            await query.message.edit_text(
                "<b>🔁 SUBSTITUTION</b>\n\nChoose another action.",
                reply_markup=_group_halftime_keyboard(game),
            )
            return
        lineup[lineup.index(outgoing)] = incoming
        await database.update_group_game(game_id, {
            f"{side}_lineup": lineup,
            f"{side}_substitutions": int(game.get(f"{side}_substitutions", 0)) + 1,
            f"pending_{side}_out": None,
        })
        await query.answer("Substitution completed.")
        # Let the other manager use their own half-time substitution.
        other = "away" if side == "home" else "home"
        await query.message.edit_text(
            f"<b>🔁 SUBSTITUTION COMPLETE</b>\n\n{html.escape(_turn_name(game, side))} made a change.\n\n"
            f"<b>{html.escape(_turn_name(game, other))}</b> may now make a substitution.",
            reply_markup=_group_halftime_keyboard(game),
        )

    @bot.on_callback_query(filters.regex(r"^(game|live):subback:([a-f0-9]+):(home|away)$"))
    async def substitution_back_handler(_: Client, query: CallbackQuery) -> None:
        prefix, match_id, side = query.data.split(":")[0], query.data.split(":")[2], query.data.split(":")[3]
        if prefix == "game":
            record = await database.get_group_game(match_id)
            expected = record.get("host_id") if side == "home" else record.get("opponent_id") if record else None
            keyboard = _group_halftime_keyboard(record) if record else None
            update = database.update_group_game
            title = "<b>⏸ HALF-TIME</b>\n\nChoose an option below."
        else:
            record = await database.get_challenge(match_id)
            expected = record.get("challenger_id") if side == "home" else record.get("challenged_id") if record else None
            keyboard = _challenge_halftime_keyboard(record) if record else None
            update = database.update_challenge
            title = "<b>⏸ HALF-TIME</b>\n\nChoose an option below."
        if not record or query.from_user.id != expected or not record.get("halftime"):
            await query.answer("This substitution panel is no longer active.", show_alert=True)
            return
        await update(match_id, {f"pending_{side}_out": None})
        await query.answer("Back to half-time options.")
        await query.message.edit_text(title, reply_markup=keyboard)

    @bot.on_callback_query(filters.regex(r"^game:setpiece:([a-f0-9]+):(home|away):(corner|free_kick):(.+)$"))
    async def group_set_piece_taker_handler(_: Client, query: CallbackQuery) -> None:
        parts = query.data.split(":")
        game_id, side, kind, player_id = parts[2], parts[3], parts[4], ":".join(parts[5:])
        game = await database.get_group_game(game_id)
        expected = game.get("host_id") if side == "home" else game.get("opponent_id") if game else None
        if not game or query.from_user.id != expected or game.get("pending_set_piece") != kind:
            await query.answer("This set piece is no longer active.", show_alert=True)
            return
        players = await database.get_players(game.get(f"{side}_lineup", [])[:11])
        if not any(p.get("player_id") == player_id for p in players):
            await query.answer("That player is not in the XI.", show_alert=True)
            return
        defending = "away" if side == "home" else "home"
        await database.update_group_game(game_id, {"set_piece_taker": player_id, "set_piece_defence": None})
        await query.answer("Taker selected. Defending manager chooses the response.")
        await query.message.edit_text(
            f"<b>🎯 {kind.replace('_', ' ').upper()}</b>\n\n"
            f"{html.escape(_turn_name(game, side))} selected the taker.\n"
            f"<b>{html.escape(_turn_name(game, defending))}</b> — decide how to defend:",
            reply_markup=_set_piece_defence_keyboard("game", game_id, defending),
        )

    @bot.on_callback_query(filters.regex(r"^game:setdef:([a-f0-9]+):(home|away):(mark|zone|counter|clear)$"))
    async def group_set_piece_defence_handler(_: Client, query: CallbackQuery) -> None:
        game_id, defending, response = query.data.split(":")[2:]
        game = await database.get_group_game(game_id)
        expected = game.get("host_id") if defending == "home" else game.get("opponent_id") if game else None
        if not game or query.from_user.id != expected or not game.get("pending_set_piece") or not game.get("set_piece_taker"):
            await query.answer("This set piece is no longer active.", show_alert=True)
            return
        await database.update_group_game(game_id, {"set_piece_defence": response})
        await query.answer("Defensive response locked.")

    @bot.on_callback_query(filters.regex(r"^game:penalty:([a-f0-9]+):(home|away):(.+)$"))
    async def group_penalty_handler(_: Client, query: CallbackQuery) -> None:
        game_id, side, player_id = query.data.split(":", 3)[1:]
        game = await database.get_group_game(game_id)
        if not game or game.get("phase") != "penalties":
            await query.answer("Penalty shootout is not active.", show_alert=True)
            return
        expected = game.get("host_id") if side == "home" else game.get("opponent_id")
        if query.from_user.id != expected:
            await query.answer("Only that manager can choose this taker.", show_alert=True)
            return
        players = await database.get_players(game.get(f"{side}_lineup", [])[:11])
        if not any(p.get("player_id") == player_id for p in players):
            await query.answer("Choose a player from the starting XI.", show_alert=True)
            return
        await database.update_group_game(game_id, {f"penalty_{side}_taker": player_id, f"penalty_{side}_taken": 1})
        await query.answer("Penalty taker selected.")

    @bot.on_callback_query(filters.regex(r"^game:matchready:([a-f0-9]+):(home|away)$"))
    async def group_match_ready_handler(_: Client, query: CallbackQuery) -> None:
        game_id, side = query.data.split(":")[2:]
        game = await database.get_group_game(game_id)
        expected = game.get("host_id") if side == "home" else game.get("opponent_id") if game else None
        if not game or query.from_user.id != expected or game.get("phase") != "ready":
            await query.answer("That Ready button is not active for you.", show_alert=True)
            return
        await database.update_group_game(game_id, {f"{side}_ready": True})
        game = await database.get_group_game(game_id)
        if game.get("home_ready") and game.get("away_ready"):
            await query.answer("Both managers ready — kick-off!")
            await _start_group_match(bot, database, settings, game, query.message)
        else:
            await query.answer("You are ready. Waiting for the other manager.")
            await query.message.edit_text(_mode_text(game, await database.get_competition(game["mode"])) + f"\n\nManager A: {'✅ READY' if game.get('home_ready') else '⏳ NOT READY'}\nManager B: {'✅ READY' if game.get('away_ready') else '⏳ NOT READY'}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ {_team_side_label(game, 'home')} · Ready", callback_data=f"game:matchready:{game_id}:home", style=ButtonStyle.SUCCESS), InlineKeyboardButton(f"✅ {_team_side_label(game, 'away')} · Ready", callback_data=f"game:matchready:{game_id}:away", style=ButtonStyle.SUCCESS)]]))

    @bot.on_callback_query(filters.regex(r"^game:halfready:([a-f0-9]+):(home|away)$"))
    async def group_halftime_ready_handler(_: Client, query: CallbackQuery) -> None:
        game_id, side = query.data.split(":")[2:]
        game = await database.get_group_game(game_id)
        expected = game.get("host_id") if side == "home" else game.get("opponent_id") if game else None
        if not game or query.from_user.id != expected or not game.get("halftime"):
            await query.answer("This half-time is not active for you.", show_alert=True)
            return
        await database.update_group_game(game_id, {f"half_ready_{side}": True})
        game = await database.get_group_game(game_id)
        await query.answer("Ready for the second half.")
        if game.get("half_ready_home") and game.get("half_ready_away"):
            await database.update_group_game(game_id, {"halftime": False, "phase": "live", "active_turn": "home", "active_turn_action": None})
            await query.message.edit_text(_live_text(game.get("live_state") or {}, "▶️ SECOND HALF — Manager A has the first move.", "home", _turn_label(game, "home")), reply_markup=_group_live_keyboard(game_id, "home", state=game.get("live_state") or {}))
        else:
            await query.message.edit_text(f"<b>⏸ HALF-TIME</b>\n\nManager A: {'✅ READY' if game.get('half_ready_home') else '⏳ NOT READY'}\nManager B: {'✅ READY' if game.get('half_ready_away') else '⏳ NOT READY'}\n\nMake any substitution first, then press READY.", reply_markup=_group_halftime_keyboard(game))

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
        await query.answer("Use the Ready button to start when both managers are ready.", show_alert=True)

    @bot.on_message(filters.command("cancel"))
    async def cancel_command(_: Client, message: Message) -> None:
        if not _is_group_chat(message):
            await message.reply_text("Use /cancel inside the group match.")
            return
        game = await database.get_active_group_game(message.chat.id)
        if not game:
            await message.reply_text("There is no active match or lobby to cancel.")
            return
        uid = message.from_user.id
        allowed = uid in {game.get("host_id"), game.get("opponent_id")}
        if not allowed:
            try:
                member = await bot.get_chat_member(message.chat.id, uid)
                allowed = str(member.status).lower().split(".")[-1] in {"administrator", "owner"}
            except Exception:
                allowed = False
        if not allowed:
            await message.reply_text("Only the two managers or a group admin can use /cancel.")
            return
        await message.reply_text(
            "<b>🛑 CANCEL MATCH?</b>\n\nThis will forcefully end the current game for everyone.\n\nConfirm?",
            reply_markup=_cancel_confirm_keyboard(game["game_id"]),
        )

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

    @bot.on_callback_query(filters.regex(r"^live:setpiece:([a-f0-9]+):(home|away):(corner|free_kick):(.+)$"))
    async def challenge_set_piece_taker_handler(_: Client, query: CallbackQuery) -> None:
        parts = query.data.split(":")
        challenge_id, side, kind, player_id = parts[2], parts[3], parts[4], ":".join(parts[5:])
        challenge = await database.get_challenge(challenge_id)
        expected = challenge.get("challenger_id") if side == "home" else challenge.get("challenged_id") if challenge else None
        if not challenge or query.from_user.id != expected or challenge.get("pending_set_piece") != kind:
            await query.answer("This set piece is no longer active.", show_alert=True)
            return
        players = await database.get_players(challenge.get(f"{side}_lineup", [])[:11])
        if not any(p.get("player_id") == player_id for p in players):
            await query.answer("That player is not in the XI.", show_alert=True)
            return
        defending = "away" if side == "home" else "home"
        await database.update_challenge(challenge_id, {"set_piece_taker": player_id, "set_piece_defence": None})
        await query.answer("Taker selected. Defending manager chooses the response.")
        await query.message.edit_text(
            f"<b>🎯 {kind.replace('_', ' ').upper()}</b>\n\n"
            f"{html.escape(challenge.get('challenger_name' if side == 'home' else 'challenged_name', 'Manager'))} selected the taker.\n"
            f"<b>{html.escape(challenge.get('challenged_name' if defending == 'away' else 'challenger_name', 'Manager'))}</b> — decide how to defend:",
            reply_markup=_set_piece_defence_keyboard("live", challenge_id, defending),
        )

    @bot.on_callback_query(filters.regex(r"^live:setdef:([a-f0-9]+):(home|away):(mark|zone|counter|clear)$"))
    async def challenge_set_piece_defence_handler(_: Client, query: CallbackQuery) -> None:
        challenge_id, defending, response = query.data.split(":")[2:]
        challenge = await database.get_challenge(challenge_id)
        expected = challenge.get("challenger_id") if defending == "home" else challenge.get("challenged_id") if challenge else None
        if not challenge or query.from_user.id != expected or not challenge.get("pending_set_piece") or not challenge.get("set_piece_taker"):
            await query.answer("This set piece is no longer active.", show_alert=True)
            return
        await database.update_challenge(challenge_id, {"set_piece_defence": response})
        await query.answer("Defensive response locked.")

    @bot.on_callback_query(filters.regex(r"^live:penalty:([a-f0-9]+):(home|away):(.+)$"))
    async def challenge_penalty_handler(_: Client, query: CallbackQuery) -> None:
        parts = query.data.split(":")
        challenge_id, side, player_id = parts[2], parts[3], ":".join(parts[4:])
        challenge = await database.get_challenge(challenge_id)
        if not challenge or challenge.get("phase") != "penalties":
            await query.answer("Penalty shootout is not active.", show_alert=True)
            return
        expected = challenge.get("challenger_id") if side == "home" else challenge.get("challenged_id")
        if query.from_user.id != expected:
            await query.answer("Only that manager can choose this taker.", show_alert=True)
            return
        players = await database.get_players(challenge.get(f"{side}_lineup", [])[:11])
        if not any(p.get("player_id") == player_id for p in players):
            await query.answer("Choose a player from the starting XI.", show_alert=True)
            return
        await database.update_challenge(challenge_id, {f"penalty_{side}_taker": player_id})
        await query.answer("Penalty taker selected.")

    @bot.on_callback_query(filters.regex(r"^challenge:(accept|decline|back):([a-f0-9]+)$"))
    async def challenge_action_handler(_: Client, query: CallbackQuery) -> None:
        action, challenge_id = query.data.split(":")[1:]
        challenge = await database.get_challenge(challenge_id)
        if not challenge or challenge.get("status") != "pending":
            if action == "back" and challenge and challenge.get("status") == "setup":
                if query.from_user.id not in {challenge["challenger_id"], challenge["challenged_id"]}:
                    await query.answer("This challenge belongs to another manager.", show_alert=True)
                    return
                await database.finish_challenge(challenge_id, {"status": "cancelled", "cancelled_by": query.from_user.id})
                task = LIVE_TASKS.pop(challenge_id, None)
                if task and not task.done():
                    task.cancel()
                await query.answer("Challenge closed.")
                await query.message.edit_text("<b>Challenge closed.</b>\n\nNo rewards were issued.")
                return
            await query.answer("This challenge has expired.", show_alert=True)
            return
        if query.from_user.id != challenge["challenged_id"]:
            await query.answer("This challenge belongs to another manager.", show_alert=True)
            return
        if action == "back":
            await query.answer("Challenge declined.")
            await database.finish_challenge(challenge_id, {"status": "declined"})
            await query.message.edit_text("<b>Challenge declined.</b>")
            return
        if action == "decline":
            await query.answer("Challenge declined.")
            await database.finish_challenge(challenge_id, {"status": "declined"})
            await query.message.edit_text("<b>Challenge declined.</b>")
            return
        _, home_players = await database.get_user_players(challenge["challenger_id"], squad_only=True)
        _, away_players = await database.get_user_players(challenge["challenged_id"], squad_only=True)
        if len(home_players) < 11 or len(away_players) < 11:
            await query.message.edit_text(
                "<b>Challenge unavailable.</b>\n\nBoth managers need an 11-player active squad before they can play.",
                reply_markup=back_keyboard("🏠 Home", "menu:home"),
            )
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
            await query.message.edit_text(
                f"<b>🔴 KICK-OFF</b>\n\n<b>{challenge.get('challenger_name', 'Manager A')}</b> — your team has the first turn.\n"
                "Choose 1 of 6 actions. Then the opponent responds and the passage is simulated.",
                reply_markup=_challenge_live_keyboard(challenge_id, "home", state=state),
            )
        else:
            await query.message.edit_text(_setup_text(challenge), reply_markup=_challenge_setup_keyboard(challenge, await database.get_user(query.from_user.id) or {}))

    @bot.on_callback_query(filters.regex(r"^live:sub:([a-f0-9]+):(home|away)$"))
    async def challenge_sub_panel_handler(_: Client, query: CallbackQuery) -> None:
        challenge_id, side = query.data.split(":")[2:]
        challenge = await database.get_challenge(challenge_id)
        expected = challenge.get("challenger_id") if side == "home" else challenge.get("challenged_id") if challenge else None
        if not challenge or query.from_user.id != expected or not challenge.get("halftime"):
            await query.answer("Substitutions are only available during half-time.", show_alert=True)
            return
        if int(challenge.get(f"{side}_substitutions", 0)) >= 3:
            await query.answer("Maximum 3 substitutions used.", show_alert=True)
            return
        lineup = challenge.get(f"{side}_lineup", [])[:11]
        players = await database.get_players(lineup)
        await query.answer("Choose the player to take off.")
        await query.message.edit_text(
            f"<b>🔁 SUBSTITUTION</b>\n\nChoose the player coming off:",
            reply_markup=_player_rows(
                "live", challenge_id, side, players, "subout",
                back_callback=f"live:subback:{challenge_id}:{side}",
            ),
        )

    @bot.on_callback_query(filters.regex(r"^live:subout:([a-f0-9]+):(home|away):(.+)$"))
    async def challenge_sub_out_handler(_: Client, query: CallbackQuery) -> None:
        parts = query.data.split(":")
        challenge_id, side, player_id = parts[2], parts[3], ":".join(parts[4:])
        challenge = await database.get_challenge(challenge_id)
        expected = challenge.get("challenger_id") if side == "home" else challenge.get("challenged_id") if challenge else None
        if not challenge or query.from_user.id != expected or not challenge.get("halftime"):
            await query.answer("That substitution is no longer available.", show_alert=True)
            return
        lineup = challenge.get(f"{side}_lineup", [])[:11]
        bench_ids = [pid for pid in challenge.get(f"{side}_players", []) if pid not in lineup]
        if player_id not in lineup or not bench_ids:
            await query.answer("No valid substitute is available.", show_alert=True)
            await query.message.edit_text(
                "<b>🔁 SUBSTITUTION</b>\n\nNo valid substitute is available.",
                reply_markup=_challenge_halftime_keyboard(challenge),
            )
            return
        bench = await database.get_players(bench_ids)
        rows = []
        for i in range(0, len(bench), 2):
            rows.append([
                InlineKeyboardButton(
                    f"➡️ {p.get('name', 'Player')[:18]}",
                    callback_data=f"live:subin:{challenge_id}:{side}:{p.get('player_id')}",
                )
                for p in bench[i:i+2]
            ])
        await database.update_challenge(challenge_id, {f"pending_{side}_out": player_id})
        await query.answer("Now choose the incoming player.")
        rows.append([InlineKeyboardButton("↩️ Back", callback_data=f"live:subback:{challenge_id}:{side}", style=ButtonStyle.PRIMARY)])
        await query.message.edit_text("<b>🔁 CHOOSE INCOMING PLAYER</b>\n\nSelect the substitute:", reply_markup=InlineKeyboardMarkup(rows))

    @bot.on_callback_query(filters.regex(r"^live:subin:([a-f0-9]+):(home|away):(.+)$"))
    async def challenge_sub_in_handler(_: Client, query: CallbackQuery) -> None:
        parts = query.data.split(":")
        challenge_id, side, incoming = parts[2], parts[3], ":".join(parts[4:])
        challenge = await database.get_challenge(challenge_id)
        expected = challenge.get("challenger_id") if side == "home" else challenge.get("challenged_id") if challenge else None
        if not challenge or query.from_user.id != expected or not challenge.get("halftime"):
            await query.answer("That substitution is no longer available.", show_alert=True)
            return
        outgoing = challenge.get(f"pending_{side}_out")
        lineup = challenge.get(f"{side}_lineup", [])[:11]
        bench = [pid for pid in challenge.get(f"{side}_players", []) if pid not in lineup]
        if not outgoing or outgoing not in lineup or incoming not in bench:
            await query.answer("Invalid substitution.", show_alert=True)
            return
        lineup[lineup.index(outgoing)] = incoming
        await database.update_challenge(
            challenge_id,
            {
                f"{side}_lineup": lineup,
                f"{side}_substitutions": int(challenge.get(f"{side}_substitutions", 0)) + 1,
                f"pending_{side}_out": None,
            },
        )
        await query.answer("Substitution completed.")
        await query.message.edit_text("<b>🔁 SUBSTITUTION COMPLETE</b>\n\nYour change is saved. Both managers must press <b>READY</b> to continue.", reply_markup=_challenge_halftime_keyboard(challenge))

    @bot.on_callback_query(filters.regex(r"^live:halfready:([a-f0-9]+):(home|away)$"))
    async def challenge_halftime_ready_handler(_: Client, query: CallbackQuery) -> None:
        challenge_id, side = query.data.split(":")[2:]
        challenge = await database.get_challenge(challenge_id)
        expected = challenge.get("challenger_id") if side == "home" else challenge.get("challenged_id") if challenge else None
        if not challenge or query.from_user.id != expected or not challenge.get("halftime"):
            await query.answer("This half-time is not active for you.", show_alert=True)
            return
        await database.update_challenge(challenge_id, {f"half_ready_{side}": True})
        challenge = await database.get_challenge(challenge_id)
        await query.answer("Ready for the second half.")
        if challenge.get("half_ready_home") and challenge.get("half_ready_away"):
            await database.update_challenge(challenge_id, {"halftime": False, "phase": "live", "active_turn": "home", "active_turn_action": None})
            await query.message.edit_text(_live_text(challenge.get("live_state") or {}, "▶️ SECOND HALF — Manager A has the first move.", "home", challenge.get("challenger_name", "Manager A")), reply_markup=_challenge_live_keyboard(challenge_id, "home", state=challenge.get("live_state") or {}))
        else:
            await query.message.edit_text(f"<b>⏸ HALF-TIME</b>\n\nManager A: {'✅ READY' if challenge.get('half_ready_home') else '⏳ NOT READY'}\nManager B: {'✅ READY' if challenge.get('half_ready_away') else '⏳ NOT READY'}\n\nMake any substitution first, then press READY.", reply_markup=_challenge_halftime_keyboard(challenge))

    @bot.on_callback_query(filters.regex(r"^live:halfready_sub:([a-f0-9]+):(home|away)$"))
    async def challenge_halftime_sub_alias(_: Client, query: CallbackQuery) -> None:
        challenge_id, side = query.data.split(":")[2:]
        challenge = await database.get_challenge(challenge_id)
        expected = challenge.get("challenger_id") if side == "home" else challenge.get("challenged_id") if challenge else None
        if not challenge or query.from_user.id != expected or not challenge.get("halftime"):
            await query.answer("Substitution is not active.", show_alert=True)
            return
        if int(challenge.get(f"{side}_substitutions", 0)) >= 3:
            await query.answer("Maximum 3 substitutions used.", show_alert=True)
            return
        players = await database.get_players(challenge.get(f"{side}_lineup", [])[:11])
        await query.answer("Choose the player coming off.")
        await query.message.edit_text(
            "<b>🔁 SUBSTITUTION</b>\n\nChoose the player coming off:",
            reply_markup=_player_rows(
                "live", challenge_id, side, players, "subout",
                back_callback=f"live:subback:{challenge_id}:{side}",
            ),
        )

    @bot.on_callback_query(filters.regex(r"^live:([a-f0-9]+):turn:(home|away):([A-Za-z_]+)$"))
    async def live_manager_turn_handler(_: Client, query: CallbackQuery) -> None:
        parts = query.data.split(":")
        challenge_id, side, value = parts[1], parts[3], parts[4].replace("_", " ")
        challenge = await database.get_challenge(challenge_id)
        expected = challenge.get("challenger_id") if side == "home" else challenge.get("challenged_id") if challenge else None
        if not challenge or query.from_user.id != expected or challenge.get("status") != "live":
            await query.answer("This is not your live manager control.", show_alert=True)
            return
        if challenge.get("active_turn") != side:
            await query.answer("It is not your team's turn.", show_alert=True)
            return
        if challenge.get("active_turn_action"):
            await query.answer("Your move is already locked.", show_alert=True)
            return
        valid = {item[1] for item in LIVE_ACTIONS}
        if value not in valid:
            await query.answer("Invalid match action.", show_alert=True)
            return
        updates = {"active_turn_action": value, f"{side}_last_action": value}
        if value in {"Possession", "Counter", "Press", "Wide"}:
            updates[f"{side}_tactic"] = value
        else:
            updates[f"{side}_mentality"] = value
        await database.update_challenge(challenge_id, updates)
        await query.answer(f"{value} selected. Waiting for the other team.")
