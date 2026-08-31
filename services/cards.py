from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


WIDTH, HEIGHT = 720, 960


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def _rarity_colors(rarity: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    return {
        "COMMON": ((43, 52, 70), (105, 118, 145)),
        "UNCOMMON": ((24, 91, 73), (58, 190, 139)),
        "RARE": ((24, 64, 130), (74, 143, 255)),
        "EPIC": ((84, 40, 142), (194, 91, 255)),
        "ELITE": ((146, 73, 20), (255, 173, 72)),
        "LEGENDARY": ((134, 35, 33), (255, 93, 78)),
        "MYTHIC": ((99, 28, 83), (255, 73, 187)),
        "ICONIC": ((24, 74, 79), (84, 235, 217)),
    }.get(rarity.upper(), ((24, 64, 130), (74, 143, 255)))


# ---------------------------------------------------------------------------
# Template overlay coordinates.
#
# These are given as PROPORTIONS (0-1) of the template image's own width /
# height, measured directly off the GK-edition template PNG (1672x941):
#   - rating   -> centered inside the shield badge, top-left
#   - club     -> left-aligned inside the first pill (shield icon row)
#   - name     -> left-aligned inside the second pill (person icon row)
#   - nation   -> centered inside the empty pill, top-right
#   - stats    -> centered inside each of the 6 stat boxes along the bottom
#
# Everything else visible on the template (the "GK" position label, the
# "COMMON" rarity text, the portrait frame, and the FOOTBALL LEGACY /
# FL | CARDS branding) is already baked into the template artwork itself,
# so the code no longer draws any of that -- it only writes the numbers
# and text that actually change per player.
#
# If you swap in a different template with a different layout, override
# any of these via `layout={"coordinates": {...}}` using the same keys,
# with values as (x, y) proportions (0-1) of that template's width/height.
# ---------------------------------------------------------------------------
TEMPLATE_LAYOUT: dict[str, tuple[float, float]] = {
    "rating": (0.1107, 0.1541),
    "club": (0.0897, 0.3975),
    "name": (0.0897, 0.4634),
    "nation": (0.8595, 0.1775),
    "stat_pac": (0.0727, 0.8002),
    "stat_sho": (0.1567, 0.8002),
    "stat_pas": (0.2390, 0.8002),
    "stat_dri": (0.7291, 0.8002),
    "stat_def": (0.8122, 0.8002),
    "stat_phy": (0.8961, 0.8002),
}


def _resolve_coordinates(layout: dict[str, Any] | None) -> dict[str, tuple[float, float]]:
    coordinates = dict(TEMPLATE_LAYOUT)
    overrides = (layout or {}).get("coordinates", {})
    for key, value in overrides.items():
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            coordinates[key] = (float(value[0]), float(value[1]))
    return coordinates


def _render_with_template(
    player: dict[str, Any],
    template_path: str,
    layout: dict[str, Any] | None,
) -> str:
    image = Image.open(template_path).convert("RGB")
    canvas_width, canvas_height = image.size
    overlay = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    white = (255, 255, 255, 255)
    soft = (226, 237, 245, 255)

    coordinates = _resolve_coordinates(layout)

    def px(name: str) -> tuple[int, int]:
        fx, fy = coordinates[name]
        return round(fx * canvas_width), round(fy * canvas_height)

    rating_font = _font(round(canvas_height * 0.10), True)
    field_font = _font(round(canvas_height * 0.032), True)
    nation_font = _font(round(canvas_height * 0.032), True)
    stat_font = _font(round(canvas_height * 0.047), True)

    # Rating number, centered in the shield badge.
    draw.text(px("rating"), str(player.get("ovr", 0)), fill=white, font=rating_font, anchor="mm")

    # Club field (first pill, shield icon).
    club = str(player.get("club", "Free Agent"))
    draw.text(px("club"), club, fill=soft, font=field_font, anchor="lm")

    # Player name field (second pill, person icon).
    name = str(player.get("name", "Unknown Player"))
    if len(name) > 24:
        name = name[:23] + "…"
    draw.text(px("name"), name, fill=white, font=field_font, anchor="lm")

    # Nation, centered in the top-right pill.
    nation = str(player.get("nation", ""))
    if nation:
        draw.text(px("nation"), nation, fill=white, font=nation_font, anchor="mm")

    # Stat values, centered inside each of the 6 stat boxes.
    stat_keys = {
        "stat_pac": player.get("pace", 0),
        "stat_sho": player.get("shooting", 0),
        "stat_pas": player.get("passing", 0),
        "stat_dri": player.get("dribbling", 0),
        "stat_def": player.get("defending", 0),
        "stat_phy": player.get("physical", 0),
    }
    for key, value in stat_keys.items():
        draw.text(px(key), str(value), fill=white, font=stat_font, anchor="mm")

    composed = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")

    output = tempfile.NamedTemporaryFile(prefix="fl-card-", suffix=".jpg", delete=False)
    output.close()
    composed.save(output.name, "JPEG", quality=92, optimize=True)
    return output.name


def _render_generic(player: dict[str, Any]) -> str:
    rarity = str(player.get("rarity", "RARE")).upper()
    start, end = _rarity_colors(rarity)

    image = Image.new("RGB", (WIDTH, HEIGHT), start)
    pixels = image.load()
    for y in range(HEIGHT):
        ratio = y / HEIGHT
        color = tuple(round(start[i] * (1 - ratio) + end[i] * ratio) for i in range(3))
        for x in range(WIDTH):
            pixels[x, y] = color
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((22, 22, WIDTH - 22, HEIGHT - 22), radius=34, outline=end, width=8)
    draw.rounded_rectangle((42, 42, WIDTH - 42, HEIGHT - 42), radius=26, outline=(255, 255, 255), width=2)

    white = (255, 255, 255, 255)
    soft = (226, 237, 245, 255)

    title_font = _font(48, True)
    name_font = _font(42, True)
    stat_font = _font(28, True)
    small_font = _font(22)
    rating_font = _font(78, True)

    draw.text((72, 68), str(player.get("position", "MID")), fill=white, font=title_font)
    draw.text((WIDTH - 180, 84), rarity, fill=soft, font=small_font)
    draw.text((WIDTH - 170, 62), str(player.get("ovr", 0)), fill=white, font=rating_font)
    draw.text((72, 150), str(player.get("nation", "🌐")), fill=white, font=_font(34))
    draw.text((72, 205), str(player.get("club", "Free Agent")), fill=soft, font=small_font)

    portrait_box = (92, 275, WIDTH - 92, 610)
    draw.rounded_rectangle(portrait_box, radius=28, fill=(0, 0, 0, 48), outline=soft, width=2)
    draw.text((WIDTH // 2, 430), "FOOTBALL", fill=(255, 255, 255, 120), font=_font(34, True), anchor="mm")
    draw.text((WIDTH // 2, 478), "LEGACY", fill=(255, 255, 255, 90), font=_font(26, True), anchor="mm")

    name = str(player.get("name", "Unknown Player"))
    if len(name) > 20:
        name = name[:19] + "…"
    draw.text((WIDTH / 2, 645), name, fill=white, font=name_font, anchor="mm")
    draw.text(
        (WIDTH / 2, 690),
        f"{player.get('position', 'MID')} · {player.get('preferred_foot', 'Right')} foot",
        fill=soft,
        font=small_font,
        anchor="mm",
    )

    stats = [
        ("PAC", player.get("pace", 0)),
        ("SHO", player.get("shooting", 0)),
        ("PAS", player.get("passing", 0)),
        ("DRI", player.get("dribbling", 0)),
        ("DEF", player.get("defending", 0)),
        ("PHY", player.get("physical", 0)),
    ]
    for index, (label, value) in enumerate(stats):
        x = 90 + (index % 3) * 215
        y = 760 + (index // 3) * 68
        draw.text((x, y), label, fill=soft, font=small_font)
        draw.text((x + 56, y - 4), str(value), fill=white, font=stat_font)

    output = tempfile.NamedTemporaryFile(prefix="fl-card-", suffix=".jpg", delete=False)
    output.close()
    image.save(output.name, "JPEG", quality=92, optimize=True)
    return output.name


def render_player_card(
    player: dict[str, Any],
    template_path: str | None = None,
    layout: dict[str, Any] | None = None,
) -> str:
    if template_path and Path(template_path).exists():
        return _render_with_template(player, template_path, layout)
    return _render_generic(player)