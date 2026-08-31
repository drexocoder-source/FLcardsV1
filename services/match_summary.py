from __future__ import annotations

import tempfile
from typing import Any

from PIL import Image, ImageDraw

from .cards import _font


WIDTH, HEIGHT = 1024, 576
BACKGROUND = (5, 8, 14)
PANEL = (12, 17, 27)
PANEL_ALT = (16, 22, 33)
RED = (235, 42, 58)
RED_DARK = (105, 22, 35)
WHITE = (245, 247, 251)
MUTED = (155, 166, 181)
GREEN = (80, 214, 139)


def _short(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _number(state: dict[str, Any], *keys: str, default: int = 0) -> int:
    for key in keys:
        value = state.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    return default


def _winner(state: dict[str, Any]) -> str | None:
    if state.get("penalty_winner") in {"home", "away"}:
        return state["penalty_winner"]
    home_goals = _number(state, "home_goals")
    away_goals = _number(state, "away_goals")
    if home_goals > away_goals:
        return "home"
    if away_goals > home_goals:
        return "away"
    return None


def _goals(state: dict[str, Any]) -> list[tuple[str, str, str]]:
    result: list[tuple[str, str, str]] = []
    for event in state.get("events", []) or []:
        if not isinstance(event, dict) or event.get("type") != "goal" and not event.get("goal"):
            continue
        side = str(event.get("side") or event.get("team") or "")
        if side not in {"home", "away"}:
            continue
        minute = _short(event.get("minute", "?"), 5)
        scorer = _short(event.get("scorer_name") or event.get("scorer") or event.get("player_name") or "Unknown scorer", 24)
        result.append((minute, scorer, side))
    return result


def _gradient(image: Image.Image) -> None:
    draw = ImageDraw.Draw(image)
    for y in range(HEIGHT):
        ratio = y / HEIGHT
        draw.line(
            (0, y, WIDTH, y),
            fill=(
                round(5 + ratio * 8),
                round(8 + ratio * 4),
                round(14 + ratio * 10),
            ),
        )


def _label_value(draw: ImageDraw.ImageDraw, x: int, y: int, label: str, value: str, width: int) -> None:
    draw.text((x, y), label.upper(), fill=RED, font=_font(15, True))
    value_font = _font(22, True)
    value = _short(value, max(8, width // 13))
    draw.text((x, y + 24), value, fill=WHITE, font=value_font)


def render_match_summary(
    state: dict[str, Any],
    home_players: list[dict[str, Any]] | None = None,
    away_players: list[dict[str, Any]] | None = None,
    competition_name: str = "MANAGER MATCH",
) -> str:
    """Create a deployment-safe 16:9 football result image."""
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    _gradient(image)
    draw = ImageDraw.Draw(image)
    home = _short(state.get("home", "HOME"), 22).upper()
    away = _short(state.get("away", "AWAY"), 22).upper()
    home_goals = _number(state, "home_goals")
    away_goals = _number(state, "away_goals")
    winner = _winner(state)
    result = "DRAW" if winner is None else f"{home if winner == 'home' else away} WIN"
    penalty_home = _number(state, "home_penalties", default=-1)
    penalty_away = _number(state, "away_penalties", default=-1)

    # Header and brand line.
    draw.text((42, 27), "FOOTBALL LEGACY", fill=RED, font=_font(17, True))
    draw.text((WIDTH // 2, 26), "MATCH SUMMARY", fill=WHITE, font=_font(34, True), anchor="ma")
    draw.text((WIDTH - 42, 32), _short(competition_name, 26).upper(), fill=MUTED, font=_font(14, True), anchor="ra")
    draw.line((42, 76, WIDTH - 42, 76), fill=RED_DARK, width=2)

    # Main score panel.
    draw.rounded_rectangle((42, 98, WIDTH - 42, 258), radius=18, fill=PANEL, outline=(74, 30, 42), width=2)
    draw.text((220, 126), home, fill=WHITE, font=_font(23, True), anchor="ma")
    draw.text((WIDTH - 220, 126), away, fill=WHITE, font=_font(23, True), anchor="ma")
    draw.text((WIDTH // 2, 119), f"{home_goals}  —  {away_goals}", fill=WHITE, font=_font(64, True), anchor="ma")
    draw.text((WIDTH // 2, 198), result, fill=GREEN if winner else MUTED, font=_font(18, True), anchor="ma")
    if penalty_home >= 0 and penalty_away >= 0:
        draw.text((WIDTH // 2, 226), f"Penalties  {penalty_home} — {penalty_away}", fill=MUTED, font=_font(15, True), anchor="ma")
    draw.line((96, 239, WIDTH - 96, 239), fill=(42, 48, 61), width=1)

    # Match facts row.
    draw.rounded_rectangle((42, 280, WIDTH - 42, 363), radius=14, fill=PANEL_ALT, outline=(35, 44, 60), width=1)
    possession = _number(state, "home_possession", "possession_home", default=50)
    shots_home = _number(state, "home_shots", "shots_home")
    shots_away = _number(state, "away_shots", "shots_away")
    target_home = _number(state, "home_shots_on_target", "shots_on_target_home")
    target_away = _number(state, "away_shots_on_target", "shots_on_target_away")
    corners_home = _number(state, "home_corners", "corners_home")
    corners_away = _number(state, "away_corners", "corners_away")
    _label_value(draw, 72, 300, "Possession", f"{possession}% — {100 - possession}%", 190)
    _label_value(draw, 300, 300, "Shots", f"{shots_home} — {shots_away}", 160)
    _label_value(draw, 500, 300, "On target", f"{target_home} — {target_away}", 190)
    _label_value(draw, 730, 300, "Corners", f"{corners_home} — {corners_away}", 190)

    # Lower section: goals and key players.
    draw.rounded_rectangle((42, 386, 625, 530), radius=14, fill=PANEL, outline=(35, 44, 60), width=1)
    draw.rounded_rectangle((643, 386, WIDTH - 42, 530), radius=14, fill=PANEL, outline=(35, 44, 60), width=1)
    draw.text((68, 405), "GOAL TIMELINE", fill=RED, font=_font(16, True))
    goals = _goals(state)
    if goals:
        for index, (minute, scorer, side) in enumerate(goals[:5]):
            y = 440 + index * 18
            draw.text((70, y), f"{minute}'", fill=WHITE, font=_font(15, True))
            draw.text((120, y), _short(scorer, 27), fill=WHITE, font=_font(15, False))
            draw.text((460, y), "HOME" if side == "home" else "AWAY", fill=MUTED, font=_font(13, True), anchor="ra")
    else:
        draw.text((70, 447), "No goals recorded", fill=MUTED, font=_font(16, False))

    draw.text((669, 405), "KEY PLAYERS", fill=RED, font=_font(16, True))
    home_players = home_players or []
    away_players = away_players or []
    all_players = [("HOME", player) for player in home_players] + [("AWAY", player) for player in away_players]
    ranked = sorted(all_players, key=lambda item: int(item[1].get("ovr", 0)), reverse=True)
    if ranked:
        for index, (side, player) in enumerate(ranked[:3]):
            y = 440 + index * 25
            draw.text((670, y), _short(player.get("name", "Player"), 22), fill=WHITE, font=_font(16, True))
            draw.text((955, y), f"{side}  {int(player.get('ovr', 0))}", fill=MUTED, font=_font(13, True), anchor="ra")
    else:
        draw.text((670, 447), "Squad data unavailable", fill=MUTED, font=_font(16, False))

    output = tempfile.NamedTemporaryFile(prefix="fl-summary-", suffix=".jpg", delete=False)
    output.close()
    image.save(output.name, "JPEG", quality=91, optimize=False, subsampling=0)
    return output.name