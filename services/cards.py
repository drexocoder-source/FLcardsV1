from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps


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


def render_player_card(
    player: dict[str, Any],
    template_path: str | None = None,
    layout: dict[str, Any] | None = None,
) -> str:
    rarity = str(player.get("rarity", "RARE")).upper()
    start, end = _rarity_colors(rarity)

    if template_path and Path(template_path).exists():
        source = Image.open(template_path).convert("RGB")
        source_ratio = source.width / max(source.height, 1)
        if source_ratio >= 1.45:
            canvas_width, canvas_height = source.size
        else:
            canvas_width, canvas_height = WIDTH, HEIGHT
        image = source.resize((canvas_width, canvas_height))
        overlay = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
    else:
        canvas_width, canvas_height = WIDTH, HEIGHT
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

    if template_path and Path(template_path).exists():
        draw = ImageDraw.Draw(overlay)

    white = (255, 255, 255, 255)
    soft = (226, 237, 245, 255)
    wide = canvas_width / canvas_height >= 1.45
    scale = canvas_width / 1280 if wide else canvas_width / WIDTH
    def point(x: float, y: float) -> tuple[int, int]:
        return round(x * scale), round(y * scale)

    title_font = _font(round((34 if wide else 48) * scale), True)
    name_font = _font(round((34 if wide else 42) * scale), True)
    stat_font = _font(round((23 if wide else 28) * scale), True)
    small_font = _font(round((18 if wide else 22) * scale))
    rating_font = _font(round((66 if wide else 78) * scale), True)

    coordinates = (layout or {}).get("coordinates", {}) if wide else {}
    rating_xy = tuple(coordinates.get("rating", point(54, 42) if wide else point(72, 68)))
    position_xy = tuple(coordinates.get("position", point(54, 122) if wide else point(72, 68)))
    nation_xy = tuple(coordinates.get("nation", point(54, 176) if wide else point(72, 150)))
    rarity_xy = tuple(coordinates.get("rarity", point(canvas_width / scale - 265, 84)))
    identity_xy = tuple(coordinates.get("identity", point(canvas_width / scale / 2, 645)))
    club_xy = tuple(coordinates.get("club", point(canvas_width / scale / 2, 690)))

    draw.text(position_xy, str(player.get("position", "MID")), fill=white, font=title_font)
    draw.text(rarity_xy, rarity, fill=soft, font=small_font)
    rating_position = rating_xy if wide else point(WIDTH - 170, 62)
    draw.text(rating_position, str(player.get("ovr", 0)), fill=white, font=rating_font)
    draw.text(nation_xy, str(player.get("nation", "🌐")), fill=white, font=_font(round((28 if wide else 34) * scale)))
    club_top = point(54, 176) if wide else point(72, 205)
    draw.text(club_top, str(player.get("club", "Free Agent")), fill=soft, font=small_font)

    if wide:
        portrait_box = tuple(coordinates.get("portrait", (370, 88, 930, 472)))
        portrait_box = tuple(round(value * scale) for value in portrait_box)
        draw.rounded_rectangle(portrait_box, radius=round(24 * scale), fill=(0, 0, 0, 48), outline=soft, width=max(1, round(2 * scale)))
        draw.text(point(650, 280), "FOOTBALL", fill=(255, 255, 255, 120), font=_font(round(28 * scale), True), anchor="mm")
        draw.text(point(650, 325), "LEGACY", fill=(255, 255, 255, 90), font=_font(round(22 * scale), True), anchor="mm")
    else:
        portrait_box = (92, 275, WIDTH - 92, 610)
        draw.rounded_rectangle(portrait_box, radius=28, fill=(0, 0, 0, 48), outline=soft, width=2)
        draw.text((WIDTH // 2, 430), "FOOTBALL", fill=(255, 255, 255, 120), font=_font(34, True), anchor="mm")
        draw.text((WIDTH // 2, 478), "LEGACY", fill=(255, 255, 255, 90), font=_font(26, True), anchor="mm")

    name = str(player.get("name", "Unknown Player"))
    if len(name) > 20:
        name = name[:19] + "…"
    draw.text(identity_xy, name, fill=white, font=name_font, anchor="mm")
    draw.text(club_xy, f"{player.get('position', 'MID')} · {player.get('preferred_foot', 'Right')} foot", fill=soft, font=small_font, anchor="mm")

    stats = [
        ("PAC", player.get("pace", 0)),
        ("SHO", player.get("shooting", 0)),
        ("PAS", player.get("passing", 0)),
        ("DRI", player.get("dribbling", 0)),
        ("DEF", player.get("defending", 0)),
        ("PHY", player.get("physical", 0)),
    ]
    for index, (label, value) in enumerate(stats):
        if wide:
            x = 54 + index * 160 if index < 3 else 760 + (index - 3) * 160
            y = 590
        else:
            x = 90 + (index % 3) * 215
            y = 760 + (index // 3) * 68
        draw.text((x, y), label, fill=soft, font=small_font)
        draw.text((x + round(56 * scale), y - round(4 * scale)), str(value), fill=white, font=stat_font)

    if template_path and Path(template_path).exists():
        image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")

    output = tempfile.NamedTemporaryFile(prefix="fl-card-", suffix=".jpg", delete=False)
    output.close()
    image.save(output.name, "JPEG", quality=92, optimize=True)
    return output.name
