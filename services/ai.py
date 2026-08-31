from __future__ import annotations

import json
import logging

import httpx

from config import Settings

logger = logging.getLogger("fl_cards.ai")


async def generate_match_summary(
    settings: Settings,
    home_name: str,
    away_name: str,
    home_goals: int,
    away_goals: int,
    motm: str,
    competition: str,
) -> str:
    """Ask OpenRouter for a short football broadcast summary.

    The configured OPENROUTER_API_KEY is preferred. OPENAI_API_KEY is accepted
    as a compatibility fallback because Replit stores the provided secret under
    that name in this project.
    """
    if not settings.openrouter_api_key:
        return f"<i>{motm} controlled the tempo as {home_name} completed a {competition} match against {away_name}.</i>"

    prompt = (
        "Write one exciting but concise football-broadcast sentence. "
        f"{home_name} drew {home_goals}-{away_goals} against {away_name} in the {competition}. "
        f"Man of the match: {motm}. Do not use markdown, quotation marks, or emojis."
    )
    payload = {
        "model": settings.openrouter_model,
        "messages": [
            {"role": "system", "content": "You are a football match commentator."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 80,
    }
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://replit.com",
        "X-Title": "Football Legacy Manager",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, content=json.dumps(payload))
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"].strip()
            return f"<i>{content.replace('<', '').replace('>', '')}</i>"
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("OpenRouter summary failed, using manual fallback: %s", exc)
        return f"<i>{motm} controlled the tempo as {home_name} completed a {competition} match against {away_name}.</i>"