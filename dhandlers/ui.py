from __future__ import annotations

from pyrogram.enums import ButtonStyle
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from services.economy import SHOP_PACKS


def shop_button() -> InlineKeyboardButton:
    return InlineKeyboardButton("🛒 Shop", callback_data="menu:shop", style=ButtonStyle.SUCCESS)


def shop_text(coins: int = 0, packs: dict | None = None) -> str:
    packs = packs or SHOP_PACKS
    lines = [
        "<b>🛒 CARD SHOP</b>",
        "",
        f"🪙 Your balance: <b>{coins:,} coins</b>",
        "",
        "Each pack gives one random collectible card. Buy 1, 2, or 3 packs at once.",
        "Lower rarity and lower OVR cards are more likely; stronger cards can still drop.",
        "",
    ]
    for pack_key, pack in packs.items():
        drops = ", ".join(f"{rarity.title()} {weight}%" for rarity, weight in pack["drops"].items())
        lines.append(f"{pack['emoji']} <b>{pack['name']}</b> · {pack['price']:,} coins")
        lines.append(f"Drop odds: {drops}")
    return "\n".join(lines)


def shop_keyboard(packs: dict | None = None) -> InlineKeyboardMarkup:
    packs = packs or SHOP_PACKS
    rows = []
    for pack_key, pack in packs.items():
        rows.append(
            [
                InlineKeyboardButton(
                    f"{pack['emoji']} {pack_key.title()} ×{quantity} · {pack['price'] * quantity:,}",
                    callback_data=f"shop:buy:{pack_key}:{quantity}",
                    style=ButtonStyle.SUCCESS if quantity == 1 else ButtonStyle.PRIMARY,
                )
                for quantity in (1, 2, 3)
            ]
        )
    rows.append([InlineKeyboardButton("🏠 Club hub", callback_data="menu:home", style=ButtonStyle.PRIMARY)])
    return InlineKeyboardMarkup(rows)


def menu_keyboard(owner_id: int = 8186068163) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⚽ Arena", url="https://t.me/+vzPRPu3ezZIzZTZl"),
                InlineKeyboardButton("➕ Add to group", url="https://t.me/NexoraaBotss"),
            ],
            [
                InlineKeyboardButton("🛠 Developer", url=f"tg://user?id={owner_id}", style=ButtonStyle.PRIMARY),
                InlineKeyboardButton("🛟 Support", url="https://t.me/NexoraaBotss"),
            ],
            [shop_button()],
            [InlineKeyboardButton("🎮 Open club controls", callback_data="menu:club", style=ButtonStyle.SUCCESS)],
        ]
    )


def club_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎴 Claim", callback_data="menu:claim", style=ButtonStyle.SUCCESS),
                InlineKeyboardButton("⚽ Debut", callback_data="menu:debut", style=ButtonStyle.PRIMARY),
            ],
            [
                InlineKeyboardButton("📚 Collection", callback_data="menu:collection", style=ButtonStyle.PRIMARY),
                InlineKeyboardButton("🟢 Squad", callback_data="menu:squad", style=ButtonStyle.SUCCESS),
            ],
            [InlineKeyboardButton("🏟 Profile", callback_data="menu:profile", style=ButtonStyle.PRIMARY)],
            [shop_button()],
            [InlineKeyboardButton("Club hub", callback_data="menu:home", style=ButtonStyle.PRIMARY)],
        ]
    )


def claim_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("RETAIN", callback_data="claim:retain", style=ButtonStyle.SUCCESS),
                InlineKeyboardButton("RELEASE", callback_data="claim:release", style=ButtonStyle.DANGER),
            ],
            [InlineKeyboardButton("VIEW CARD", callback_data="claim:view", style=ButtonStyle.PRIMARY)],
            [shop_button()],
        ]
    )


def back_keyboard(label: str = "Club hub", callback_data: str = "menu:home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [shop_button(), InlineKeyboardButton(label, callback_data=callback_data, style=ButtonStyle.PRIMARY)],
        ]
    )


def arena_keyboard(competitions: list[dict] | None = None) -> InlineKeyboardMarkup | None:
    if competitions:
        competition_buttons = [
            InlineKeyboardButton(
                f"{competition.get('emoji', '🏆')} {competition['name']}",
                callback_data=f"mode:{competition['competition_key']}",
                style=ButtonStyle.SUCCESS if competition.get("team_type") == "national" else ButtonStyle.PRIMARY,
            )
            for competition in competitions
        ]
        rows = [competition_buttons[index : index + 2] for index in range(0, len(competition_buttons), 2)]
        rows.append([shop_button(), InlineKeyboardButton("Club hub", callback_data="menu:home", style=ButtonStyle.PRIMARY)])
        return InlineKeyboardMarkup(rows)
    return InlineKeyboardMarkup([[shop_button(), InlineKeyboardButton("Club hub", callback_data="menu:home", style=ButtonStyle.PRIMARY)]])


def help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔥 Arena", callback_data="menu:arena", style=ButtonStyle.SUCCESS),
                InlineKeyboardButton("🛟 Support", callback_data="menu:support", style=ButtonStyle.PRIMARY),
            ],
            [shop_button()],
            [InlineKeyboardButton("Club hub", callback_data="menu:home", style=ButtonStyle.PRIMARY)],
        ]
    )


def info_keyboard(destination: str = "menu:home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [shop_button(), InlineKeyboardButton("Club hub", callback_data=destination, style=ButtonStyle.PRIMARY)],
        ]
    )


def challenge_keyboard(challenge_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Accept challenge", callback_data=f"challenge:accept:{challenge_id}", style=ButtonStyle.SUCCESS),
                InlineKeyboardButton("Decline", callback_data=f"challenge:decline:{challenge_id}", style=ButtonStyle.DANGER),
            ],
            [shop_button()],
            [InlineKeyboardButton("Match rules", callback_data="menu:help", style=ButtonStyle.PRIMARY)],
        ]
    )
