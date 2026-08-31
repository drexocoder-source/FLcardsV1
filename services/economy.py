from __future__ import annotations

import random
from typing import Any


RARITY_ORDER = ("COMMON", "RARE", "EPIC", "ELITE", "LEGENDARY", "ICONIC")

SHOP_PACKS: dict[str, dict[str, Any]] = {
    "COMMON": {
        "name": "Common Pack",
        "emoji": "🟢",
        "price": 1_500,
        "drops": {"COMMON": 70, "RARE": 24, "EPIC": 5, "ELITE": 1},
    },
    "RARE": {
        "name": "Rare Pack",
        "emoji": "🔵",
        "price": 1_500,
        "drops": {"COMMON": 25, "RARE": 52, "EPIC": 17, "ELITE": 5, "LEGENDARY": 1},
    },
    "EPIC": {
        "name": "Epic Pack",
        "emoji": "🟣",
        "price": 4_000,
        "drops": {"RARE": 25, "EPIC": 50, "ELITE": 20, "LEGENDARY": 5},
    },
    "ELITE": {
        "name": "Elite Pack",
        "emoji": "🔴",
        "price": 10_000,
        "drops": {"EPIC": 25, "ELITE": 50, "LEGENDARY": 22, "ICONIC": 3},
    },
    "LEGENDARY": {
        "name": "Legendary Pack",
        "emoji": "🟡",
        "price": 25_000,
        "drops": {"ELITE": 25, "LEGENDARY": 55, "ICONIC": 20},
    },
}

# Lower rarity and lower OVR cards should be noticeably more common in free
# claims, while strong cards remain possible rather than impossible.
CLAIM_RARITY_WEIGHTS = {
    "COMMON": 70,
    "RARE": 24,
    "EPIC": 8,
    "ELITE": 4,
    "LEGENDARY": 2,
    "ICONIC": 1,
}


def weighted_player_choice(players: list[dict[str, Any]], rarity_weights: dict[str, float]) -> dict[str, Any] | None:
    if not players:
        return None
    weights = []
    for player in players:
        rarity = str(player.get("rarity", "COMMON")).upper()
        rarity_weight = max(0.05, float(rarity_weights.get(rarity, 0.05)))
        ovr = max(1, min(99, int(player.get("ovr", 50))))
        lower_ovr_weight = max(0.8, (110 - ovr) / 10)
        weights.append(rarity_weight * lower_ovr_weight)
    return random.choices(players, weights=weights, k=1)[0]