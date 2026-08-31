from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


# ---------------------------------------------------------------------------
# Generic fallback card size
# ---------------------------------------------------------------------------
WIDTH, HEIGHT = 720, 960


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------
def _font(
    size: int,
    bold: bool = False,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        ),
        (
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"
        ),
    ]

    for candidate in candidates:
        if os.path.exists(candidate):
            return ImageFont.truetype(candidate, size)

    return ImageFont.load_default()


def _fit_font(
    text: str,
    max_width: int,
    start_size: int,
    min_size: int,
    bold: bool = True,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """
    Automatically finds the largest font that fits inside max_width.
    This prevents long player names / club names from overflowing.
    """
    text = str(text)

    for size in range(start_size, min_size - 1, -2):
        font = _font(size, bold)

        bbox = font.getbbox(text)
        width = bbox[2] - bbox[0]

        if width <= max_width:
            return font

    return _font(min_size, bold)


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
) -> str:
    """
    Final safety truncation if even the minimum font cannot fit.
    """
    text = str(text)

    if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
        return text

    while text:
        shortened = text[:-2] + "…"

        if draw.textbbox((0, 0), shortened, font=font)[2] <= max_width:
            return shortened

        text = text[:-2]

    return "…"


# ---------------------------------------------------------------------------
# Rarity colors
# ---------------------------------------------------------------------------
def _rarity_colors(
    rarity: str,
) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    return {
        "COMMON": ((43, 52, 70), (105, 118, 145)),
        "UNCOMMON": ((24, 91, 73), (58, 190, 139)),
        "RARE": ((24, 64, 130), (74, 143, 255)),
        "EPIC": ((84, 40, 142), (194, 91, 255)),
        "ELITE": ((146, 73, 20), (255, 173, 72)),
        "LEGENDARY": ((134, 35, 33), (255, 93, 78)),
        "MYTHIC": ((99, 28, 83), (255, 73, 187)),
        "ICONIC": ((24, 74, 79), (84, 235, 217)),
    }.get(
        rarity.upper(),
        ((24, 64, 130), (74, 143, 255)),
    )


# ---------------------------------------------------------------------------
# GK TEMPLATE LAYOUT
#
# Template is 1280 × 720.
#
# Coordinates are proportional so the renderer also works if the template
# is replaced with another resolution having the same layout.
# ---------------------------------------------------------------------------
TEMPLATE_LAYOUT: dict[str, tuple[float, float]] = {
    # Rating badge
    "rating": (0.1107, 0.1541),

    # Left information pills
    "club": (0.0897, 0.3975),
    "name": (0.0897, 0.4634),

    # Nation pill
    "nation": (0.8595, 0.1775),

    # Bottom statistics
    "stat_pac": (0.0727, 0.8002),
    "stat_sho": (0.1567, 0.8002),
    "stat_pas": (0.2390, 0.8002),

    "stat_dri": (0.7291, 0.8002),
    "stat_def": (0.8122, 0.8002),
    "stat_phy": (0.8961, 0.8002),
}


def _resolve_coordinates(
    layout: dict[str, Any] | None,
) -> dict[str, tuple[float, float]]:
    coordinates = dict(TEMPLATE_LAYOUT)

    overrides = (layout or {}).get("coordinates", {})

    for key, value in overrides.items():
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            coordinates[key] = (
                float(value[0]),
                float(value[1]),
            )

    return coordinates


# ---------------------------------------------------------------------------
# TEMPLATE RENDERER
# ---------------------------------------------------------------------------
def _render_with_template(
    player: dict[str, Any],
    template_path: str,
    layout: dict[str, Any] | None,
) -> str:
    image = Image.open(template_path).convert("RGBA")

    canvas_width, canvas_height = image.size

    overlay = Image.new(
        "RGBA",
        (canvas_width, canvas_height),
        (0, 0, 0, 0),
    )

    draw = ImageDraw.Draw(overlay)

    # -----------------------------------------------------------------------
    # Colors
    # -----------------------------------------------------------------------
    white = (255, 255, 255, 255)
    soft = (226, 237, 245, 255)

    coordinates = _resolve_coordinates(layout)

    def px(name: str) -> tuple[int, int]:
        fx, fy = coordinates[name]

        return (
            round(fx * canvas_width),
            round(fy * canvas_height),
        )

    # -----------------------------------------------------------------------
    # SCALE
    #
    # The original problem was that the fields were using very small
    # percentages of the 720px canvas height.
    #
    # These sizes are deliberately larger and are designed around the
    # actual 1280×720 template.
    # -----------------------------------------------------------------------
    rating_size = round(canvas_height * 0.105)
    field_size = round(canvas_height * 0.050)
    nation_size = round(canvas_height * 0.043)
    stat_size = round(canvas_height * 0.062)

    # -----------------------------------------------------------------------
    # RATING
    # -----------------------------------------------------------------------
    rating = str(player.get("ovr", 0))

    rating_font = _fit_font(
        rating,
        max_width=canvas_width * 0.095,
        start_size=rating_size,
        min_size=round(canvas_height * 0.070),
        bold=True,
    )

    draw.text(
        px("rating"),
        rating,
        fill=white,
        font=rating_font,
        anchor="mm",
    )

    # -----------------------------------------------------------------------
    # CLUB
    #
    # Available pill width is roughly 250px on the 1280 template.
    # -----------------------------------------------------------------------
    club = str(
        player.get(
            "club",
            "Free Agent",
        )
    ).strip()

    club_font = _fit_font(
        club,
        max_width=canvas_width * 0.185,
        start_size=field_size,
        min_size=round(canvas_height * 0.032),
        bold=True,
    )

    club = _fit_text(
        draw,
        club,
        club_font,
        max_width=round(canvas_width * 0.185),
    )

    draw.text(
        px("club"),
        club,
        fill=soft,
        font=club_font,
        anchor="lm",
    )

    # -----------------------------------------------------------------------
    # PLAYER NAME
    # -----------------------------------------------------------------------
    name = str(
        player.get(
            "name",
            "Unknown Player",
        )
    ).strip()

    name_font = _fit_font(
        name,
        max_width=canvas_width * 0.185,
        start_size=field_size,
        min_size=round(canvas_height * 0.030),
        bold=True,
    )

    name = _fit_text(
        draw,
        name,
        name_font,
        max_width=round(canvas_width * 0.185),
    )

    draw.text(
        px("name"),
        name,
        fill=white,
        font=name_font,
        anchor="lm",
    )

    # -----------------------------------------------------------------------
    # NATION
    # -----------------------------------------------------------------------
    nation = str(
        player.get(
            "nation",
            "",
        )
    ).strip()

    if nation:
        nation_font = _fit_font(
            nation,
            max_width=canvas_width * 0.18,
            start_size=nation_size,
            min_size=round(canvas_height * 0.030),
            bold=True,
        )

        nation = _fit_text(
            draw,
            nation,
            nation_font,
            max_width=round(canvas_width * 0.18),
        )

        draw.text(
            px("nation"),
            nation,
            fill=white,
            font=nation_font,
            anchor="mm",
        )

    # -----------------------------------------------------------------------
    # STATS
    # -----------------------------------------------------------------------
    stat_values = {
        "stat_pac": player.get("pace", 0),
        "stat_sho": player.get("shooting", 0),
        "stat_pas": player.get("passing", 0),
        "stat_dri": player.get("dribbling", 0),
        "stat_def": player.get("defending", 0),
        "stat_phy": player.get("physical", 0),
    }

    for key, value in stat_values.items():
        value = str(value)

        value_font = _fit_font(
            value,
            max_width=canvas_width * 0.055,
            start_size=stat_size,
            min_size=round(canvas_height * 0.045),
            bold=True,
        )

        draw.text(
            px(key),
            value,
            fill=white,
            font=value_font,
            anchor="mm",
        )

    # -----------------------------------------------------------------------
    # COMPOSE
    # -----------------------------------------------------------------------
    composed = Image.alpha_composite(
        image,
        overlay,
    ).convert("RGB")

    # -----------------------------------------------------------------------
    # OUTPUT
    # -----------------------------------------------------------------------
    output = tempfile.NamedTemporaryFile(
        prefix="fl-card-",
        suffix=".jpg",
        delete=False,
    )

    output.close()

    composed.save(
        output.name,
        "JPEG",
        quality=95,
        optimize=True,
        subsampling=0,
    )

    return output.name


# ---------------------------------------------------------------------------
# GENERIC FALLBACK CARD
# ---------------------------------------------------------------------------
def _render_generic(
    player: dict[str, Any],
) -> str:
    rarity = str(
        player.get(
            "rarity",
            "RARE",
        )
    ).upper()

    start, end = _rarity_colors(rarity)

    image = Image.new(
        "RGB",
        (WIDTH, HEIGHT),
        start,
    )

    pixels = image.load()

    for y in range(HEIGHT):
        ratio = y / HEIGHT

        color = tuple(
            round(
                start[i] * (1 - ratio)
                + end[i] * ratio
            )
            for i in range(3)
        )

        for x in range(WIDTH):
            pixels[x, y] = color

    draw = ImageDraw.Draw(image)

    # -----------------------------------------------------------------------
    # Card border
    # -----------------------------------------------------------------------
    draw.rounded_rectangle(
        (
            22,
            22,
            WIDTH - 22,
            HEIGHT - 22,
        ),
        radius=34,
        outline=end,
        width=8,
    )

    draw.rounded_rectangle(
        (
            42,
            42,
            WIDTH - 42,
            HEIGHT - 42,
        ),
        radius=26,
        outline=(255, 255, 255),
        width=2,
    )

    white = (255, 255, 255)
    soft = (226, 237, 245)

    # -----------------------------------------------------------------------
    # Larger fallback typography
    # -----------------------------------------------------------------------
    title_font = _font(52, True)
    name_font = _font(46, True)
    stat_font = _font(34, True)
    small_font = _font(25, True)
    rating_font = _font(88, True)

    # -----------------------------------------------------------------------
    # Position
    # -----------------------------------------------------------------------
    draw.text(
        (72, 68),
        str(player.get("position", "MID")),
        fill=white,
        font=title_font,
    )

    # -----------------------------------------------------------------------
    # Rarity
    # -----------------------------------------------------------------------
    draw.text(
        (WIDTH - 180, 84),
        rarity,
        fill=soft,
        font=small_font,
    )

    # -----------------------------------------------------------------------
    # Rating
    # -----------------------------------------------------------------------
    draw.text(
        (WIDTH - 170, 62),
        str(player.get("ovr", 0)),
        fill=white,
        font=rating_font,
    )

    # -----------------------------------------------------------------------
    # Nation
    # -----------------------------------------------------------------------
    draw.text(
        (72, 150),
        str(player.get("nation", "🌐")),
        fill=white,
        font=_font(38, True),
    )

    # -----------------------------------------------------------------------
    # Club
    # -----------------------------------------------------------------------
    club = str(
        player.get(
            "club",
            "Free Agent",
        )
    )

    club_font = _fit_font(
        club,
        max_width=WIDTH - 144,
        start_size=28,
        min_size=20,
        bold=True,
    )

    draw.text(
        (72, 205),
        club,
        fill=soft,
        font=club_font,
    )

    # -----------------------------------------------------------------------
    # Portrait area
    # -----------------------------------------------------------------------
    portrait_box = (
        92,
        275,
        WIDTH - 92,
        610,
    )

    draw.rounded_rectangle(
        portrait_box,
        radius=28,
        fill=(0, 0, 0, 48),
        outline=soft,
        width=2,
    )

    draw.text(
        (WIDTH // 2, 430),
        "FOOTBALL",
        fill=(255, 255, 255, 120),
        font=_font(38, True),
        anchor="mm",
    )

    draw.text(
        (WIDTH // 2, 478),
        "LEGACY",
        fill=(255, 255, 255, 90),
        font=_font(30, True),
        anchor="mm",
    )

    # -----------------------------------------------------------------------
    # Player name
    # -----------------------------------------------------------------------
    name = str(
        player.get(
            "name",
            "Unknown Player",
        )
    )

    name_font = _fit_font(
        name,
        max_width=WIDTH - 150,
        start_size=46,
        min_size=28,
        bold=True,
    )

    name = _fit_text(
        draw,
        name,
        name_font,
        WIDTH - 150,
    )

    draw.text(
        (WIDTH / 2, 645),
        name,
        fill=white,
        font=name_font,
        anchor="mm",
    )

    # -----------------------------------------------------------------------
    # Foot
    # -----------------------------------------------------------------------
    draw.text(
        (WIDTH / 2, 690),
        (
            f"{player.get('position', 'MID')} · "
            f"{player.get('preferred_foot', 'Right')} foot"
        ),
        fill=soft,
        font=small_font,
        anchor="mm",
    )

    # -----------------------------------------------------------------------
    # Stats
    # -----------------------------------------------------------------------
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

        draw.text(
            (x, y),
            label,
            fill=soft,
            font=small_font,
        )

        draw.text(
            (x + 65, y - 5),
            str(value),
            fill=white,
            font=stat_font,
        )

    # -----------------------------------------------------------------------
    # Output
    # -----------------------------------------------------------------------
    output = tempfile.NamedTemporaryFile(
        prefix="fl-card-",
        suffix=".jpg",
        delete=False,
    )

    output.close()

    image.save(
        output.name,
        "JPEG",
        quality=95,
        optimize=True,
        subsampling=0,
    )

    return output.name


# ---------------------------------------------------------------------------
# PUBLIC RENDER FUNCTION
# ---------------------------------------------------------------------------
def render_player_card(
    player: dict[str, Any],
    template_path: str | None = None,
    layout: dict[str, Any] | None = None,
) -> str:
    """
    Render a player card.

    If a valid template is supplied, the GK template is used.

    Otherwise, the generic card renderer is used.
    """

    if (
        template_path
        and Path(template_path).exists()
    ):
        return _render_with_template(
            player,
            template_path,
            layout,
        )

    return _render_generic(player)
