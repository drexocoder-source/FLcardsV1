from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MatchResult:
    home_goals: int
    away_goals: int
    home_shots: int
    away_shots: int
    home_possession: int
    motm: str
    commentary: list[str]


def new_live_state(home: str, away: str, home_rating: int, away_rating: int) -> dict[str, Any]:
    return {
        "home": home,
        "away": away,
        "home_rating": home_rating,
        "away_rating": away_rating,
        "home_goals": 0,
        "away_goals": 0,
        "minute": 0,
        "period": "regular",
        "home_shots": 0,
        "away_shots": 0,
        "home_possession": 50,
        "commentary": [],
        "events": [],
    }


def advance_live_state(
    state: dict[str, Any],
    home_players: list[dict[str, Any]],
    away_players: list[dict[str, Any]],
    home_tactic: str = "Balanced",
    away_tactic: str = "Balanced",
    home_mentality: str = "Balanced",
    away_mentality: str = "Balanced",
) -> tuple[dict[str, Any], str]:
    """Advance a manager match by one 5–6 minute block."""
    step = random.choice((5, 6))
    state["minute"] += step
    home_rating = state["home_rating"] + _tactic_edge(home_tactic, home_mentality)
    away_rating = state["away_rating"] + _tactic_edge(away_tactic, away_mentality)
    edge = max(-0.5, min(0.5, (home_rating - away_rating) / 100))
    state["home_possession"] = max(35, min(65, int(50 + edge * 20 + random.randint(-3, 3))))
    home_chance = max(0.02, min(0.32, 0.10 + edge * 0.08 + (home_mentality.lower() == "attacking") * 0.035))
    away_chance = max(0.02, min(0.32, 0.09 - edge * 0.08 + (away_mentality.lower() == "attacking") * 0.035))
    state["home_shots"] += random.randint(0, 3)
    state["away_shots"] += random.randint(0, 3)

    lines = [
        f"{state['minute']}' — both managers adjust their shape in the middle third.",
        f"{state['minute']}' — {state['home']} try to play through the press.",
        f"{state['minute']}' — {state['away']} threaten on the counter.",
    ]
    event = random.choice(lines)
    if random.random() < home_chance:
        state["home_goals"] += 1
        scorer = _scorer(home_players, state["home"])
        event = f"⚽ {state['minute']}' — {scorer} scores for {state['home']}."
        state["events"].append({"minute": state["minute"], "team": "home", "scorer": scorer})
    elif random.random() < away_chance:
        state["away_goals"] += 1
        scorer = _scorer(away_players, state["away"])
        event = f"⚽ {state['minute']}' — {scorer} scores for {state['away']}."
        state["events"].append({"minute": state["minute"], "team": "away", "scorer": scorer})
    state["commentary"].append(event)
    return state, event


def _tactic_edge(tactic: str, mentality: str) -> int:
    tactic_edges = {"Balanced": 0, "Possession": 2, "Counter": 1, "Press": 3}
    mentality_edges = {"Balanced": 0, "Attacking": 3, "Defensive": -2}
    return tactic_edges.get(tactic, 0) + mentality_edges.get(mentality, 0)


def _scorer(players: list[dict[str, Any]], fallback: str) -> str:
    attackers = [
        player.get("name", fallback)
        for player in players
        if str(player.get("position", "")).upper() in {"ATT", "ST", "CF", "FW", "RW", "LW"}
    ]
    return random.choice(attackers or [player.get("name", fallback) for player in players] or [fallback])


def finish_live_state(state: dict[str, Any], home_players: list[dict[str, Any]], away_players: list[dict[str, Any]]) -> dict[str, Any]:
    """Resolve extra time and penalties after a tied 90 minutes."""
    if state["home_goals"] == state["away_goals"]:
        state["period"] = "extra_time"
        state["minute"] = 120
        if random.random() < 0.45:
            if random.random() < 0.5:
                state["home_goals"] += 1
            else:
                state["away_goals"] += 1
            state["commentary"].append("⏱ Extra time — a late winner finally breaks the deadlock.")
    if state["home_goals"] == state["away_goals"]:
        state["period"] = "penalties"
        home_pen = random.randint(3, 5)
        away_pen = random.randint(3, 5)
        if home_pen == away_pen:
            home_pen = 5
            away_pen = 4
        state["penalties"] = {"home": home_pen, "away": away_pen}
        state["commentary"].append(f"🥅 Penalties — {home_pen}–{away_pen}.")
    state["period"] = "finished"
    return state


def live_scorecard(state: dict[str, Any], home_emoji: str = "🟦", away_emoji: str = "🟥") -> str:
    if state.get("penalties"):
        penalty_home = state["penalties"]["home"]
        penalty_away = state["penalties"]["away"]
        winner = state["home"] if penalty_home > penalty_away else state["away"]
    else:
        winner = state["home"] if state["home_goals"] > state["away_goals"] else state["away"] if state["away_goals"] > state["home_goals"] else "Match drawn"
    penalty_line = ""
    if state.get("penalties"):
        penalty_line = f"\n🥅 Penalties: <b>{state['penalties']['home']} — {state['penalties']['away']}</b>"
    return f"""<b>FULL-TIME MANAGER REPORT</b>

{home_emoji} <b>{state['home']}</b>    <b>{state['home_goals']}</b>
{away_emoji} <b>{state['away']}</b>    <b>{state['away_goals']}</b>{penalty_line}

<b>Match facts</b>
Possession       {state['home_possession']}% — {100 - state['home_possession']}%
Shots            {state['home_shots']} — {state['away_shots']}
Extra time       {"Yes" if state.get("period") == "finished" and state.get("minute", 0) >= 120 else "No"}

<b>{winner}</b>"""


def _team_rating(players: list[dict[str, Any]]) -> float:
    return sum(player.get("ovr", 70) for player in players) / max(len(players), 1)


def simulate_match(
    user_players: list[dict[str, Any]],
    opponent: list[dict[str, Any]] | int,
    team_name: str,
    opponent_name: str = "Opposition",
) -> MatchResult:
    user_rating = _team_rating(user_players)
    opponent_rating = _team_rating(opponent) if isinstance(opponent, list) else opponent
    edge = max(-0.45, min(0.45, (user_rating - opponent_rating) / 100))
    home_goals = max(0, int(random.triangular(0, 5, 2.1 + edge)))
    away_goals = max(0, int(random.triangular(0, 4, 1.8 - edge)))
    if home_goals == away_goals and random.random() < 0.25:
        home_goals += 1 if edge >= 0 else 0
        away_goals += 1 if edge < 0 else 0

    shots = max(home_goals + 2, int(random.triangular(5, 18, 10 + edge * 10)))
    away_shots = max(away_goals + 2, int(random.triangular(5, 17, 9 - edge * 8)))
    possession = max(35, min(65, int(50 + edge * 24 + random.randint(-5, 5))))
    scorers = [player["name"] for player in user_players if player.get("position") == "ATT"] or [team_name]
    commentary = [
        f"⚡ {random.randint(8, 24)}' — {random.choice(scorers)} finds space behind the line.",
        f"🎯 {random.randint(28, 42)}' — {team_name} work a sharp passing move through midfield.",
        f"🔥 {random.randint(56, 78)}' — {opponent_name} answer as fatigue starts to show.",
    ]
    motm = max(user_players, key=lambda player: player.get("ovr", 0))["name"] if user_players else "Your captain"
    return MatchResult(home_goals, away_goals, shots, away_shots, possession, motm, commentary)


def scorecard(result: MatchResult, home_name: str, away_name: str, home_emoji: str = "🟦", away_emoji: str = "🟥") -> str:
    winner = home_name if result.home_goals > result.away_goals else away_name if result.away_goals > result.home_goals else "Match drawn"
    return f"""<b>MATCH SUMMARY</b>

{home_emoji} <b>{home_name}</b>    <b>{result.home_goals}</b>
{away_emoji} <b>{away_name}</b>    <b>{result.away_goals}</b>

<b>Match facts</b>
Possession       {result.home_possession}% — {100 - result.home_possession}%
Shots            {result.home_shots} — {result.away_shots}
Man of the match <b>{result.motm}</b>

<b>{winner}</b>"""
