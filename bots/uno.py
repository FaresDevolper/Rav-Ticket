import asyncio
import io
import json
import logging
import math
import os
import random
from functools import lru_cache

import discord
from aiohttp import web
from discord.ext import commands
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("uno")

# ======================================================================
# Settings
# ======================================================================
TURN_SECONDS = 15
MAX_TIMEOUTS = 2
LOBBY_SECONDS = 100
MIN_PLAYERS = 2
MAX_PLAYERS = 10
KEEP_EVENT_HISTORY = False  # False: only the latest action message stays under the table
START_CARDS = 7


def _parse_ids(raw):
    return {int(x) for x in raw.replace(" ", "").split(",") if x.isdigit()}


# Who is allowed to stop a game (nobody else can).
# Put role IDs here, separated by commas, or set STOP_ROLE_IDS in Render.
# Example: "123456789012345678,987654321098765432"
STOP_ROLE_IDS = _parse_ids("" + "," + os.getenv("STOP_ROLE_IDS", "1551149519675072612"))
# Optional: user IDs that can also stop a game (env STOP_USER_IDS).
STOP_USER_IDS = _parse_ids("" + "," + os.getenv("STOP_USER_IDS", "912349519990095892"))


# Dedicated Uno room(s): in these rooms every message that is not an Uno command is
# deleted after CLEAN_DELAY seconds (for everyone, including admins).
# Put the room ID between the first quotes, or set UNO_CHANNEL_ID in Render.
# In every other room nothing is ever deleted.
UNO_CHANNEL_IDS = _parse_ids("" + "," + os.getenv("UNO_CHANNEL_ID", "1571858833062760478"))
CLEAN_DELAY = 2


def can_stop(member):
    if member.id in STOP_USER_IDS:
        return True
    return any(r.id in STOP_ROLE_IDS for r in getattr(member, "roles", []))


def norm(text):
    text = text.strip().lower()
    for a in ("أ", "إ", "آ"):
        text = text.replace(a, "ا")
    return " ".join(text.split())


START_WORDS = {"اونو", "uno"}
STOP_WORDS = {"ايقاف اونو", "stop uno", "uno stop"}
LEAVE_WORDS = {"انسحاب", "انسحاب اونو", "leave uno"}
RULES_WORDS = {"قوانين اونو", "uno rules"}
ALL_WORDS = START_WORDS | STOP_WORDS | LEAVE_WORDS | RULES_WORDS

COLOR_KEYS = ["red", "yellow", "green", "blue"]
COLOR_AR = {"red": "الأحمر", "yellow": "الأصفر", "green": "الأخضر", "blue": "الأزرق"}
COLOR_NAME = {"red": "أحمر", "yellow": "أصفر", "green": "أخضر", "blue": "أزرق"}
WILDS = ("wild", "wild4")
VALUE_ORDER = [str(i) for i in range(10)] + ["skip", "reverse", "draw2", "wild", "wild4"]

RGB = {
    "red": (237, 28, 36),
    "yellow": (255, 204, 0),
    "green": (0, 166, 81),
    "blue": (0, 114, 188),
}
RGBA = {k: v + (255,) for k, v in RGB.items()}
WHITE = (255, 255, 255, 255)
BLACK = (24, 24, 24, 255)

# ======================================================================
# Cards
# ======================================================================


def build_deck():
    deck = []
    for color in COLOR_KEYS:
        deck.append((color, "0"))
        for value in [str(i) for i in range(1, 10)] + ["skip", "reverse", "draw2"]:
            deck.append((color, value))
            deck.append((color, value))
    for _ in range(4):
        deck.append((None, "wild"))
        deck.append((None, "wild4"))
    random.shuffle(deck)
    return deck


def sort_key(card):
    color_index = COLOR_KEYS.index(card[0]) if card[0] else 4
    return (color_index, VALUE_ORDER.index(card[1]))


def card_name(card):
    color, value = card
    if value == "wild":
        return "تغيير اللون"
    if value == "wild4":
        return "تغيير اللون وسحب 4"
    label = {"skip": "منع", "reverse": "عكس", "draw2": "سحب 2"}.get(value, value)
    return f"{label} {COLOR_NAME[color]}"


# ======================================================================
# Card rendering (Pillow)
# ======================================================================
S = 3  # supersampling
W, H = 240 * S, 360 * S
OUT_W, OUT_H = 300, 450
THUMB_W, THUMB_H = 150, 225

FONT_CANDIDATES = [
    "font.ttf",
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "LiberationSans-Bold.ttf",
    "arialbd.ttf",
    "Arial Bold.ttf",
]


@lru_cache(maxsize=None)
def get_font(size):
    for name in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _text(d, xy, text, size, fill, shadow=False, shadow_fill=(170, 170, 170, 255)):
    font = get_font(size)
    stroke = max(1, size // 34)
    if shadow:
        off = max(2, size // 30)
        d.text((xy[0] + off, xy[1] + off), text, font=font, fill=shadow_fill,
               anchor="mm", stroke_width=stroke, stroke_fill=shadow_fill)
    d.text(xy, text, font=font, fill=fill, anchor="mm", stroke_width=stroke, stroke_fill=fill)


def _arrow(d, cx, cy, length, thick, angle_deg, fill):
    a = math.radians(angle_deg)
    ca, sa = math.cos(a), math.sin(a)
    head_len = thick * 1.6
    head_w = thick * 1.45
    local = [
        (-length / 2, -thick / 2),
        (length / 2 - head_len, -thick / 2),
        (length / 2 - head_len, -head_w),
        (length / 2, 0),
        (length / 2 - head_len, head_w),
        (length / 2 - head_len, thick / 2),
        (-length / 2, thick / 2),
    ]
    pts = [(cx + x * ca - y * sa, cy + x * sa + y * ca) for x, y in local]
    d.polygon(pts, fill=fill)


def _draw_skip(d, cx, cy, r, fill):
    w = max(2, int(r * 0.30))
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=fill, width=w)
    k = r * 0.70
    d.line((cx - k, cy - k, cx + k, cy + k), fill=fill, width=w)


def _draw_reverse(d, cx, cy, r, fill):
    length, thick, gap = r * 1.9, r * 0.34, r * 0.36
    for ang in (-45, 135):
        a = math.radians(ang)
        px, py = -math.sin(a), math.cos(a)
        _arrow(d, cx + px * gap, cy + py * gap, length, thick, ang, fill)


def _draw_draw2(d, cx, cy, r, fill):
    w, h = r * 0.95, r * 1.35
    for dx, dy in ((-0.28, -0.18), (0.28, 0.18)):
        x, y = cx + dx * r, cy + dy * r
        d.rounded_rectangle((x - w / 2, y - h / 2, x + w / 2, y + h / 2), radius=int(r * 0.16),
                            fill=fill, outline=WHITE, width=max(2, int(r * 0.09)))


def _draw_four_cards(d, cx, cy, r):
    w, h = r * 0.80, r * 1.10
    spots = [("red", -0.36, -0.40), ("blue", 0.36, -0.40), ("yellow", -0.36, 0.40), ("green", 0.36, 0.40)]
    for color, dx, dy in spots:
        x, y = cx + dx * r, cy + dy * r
        d.rounded_rectangle((x - w / 2, y - h / 2, x + w / 2, y + h / 2), radius=int(r * 0.14),
                            fill=RGBA[color], outline=WHITE, width=max(2, int(r * 0.07)))


def _draw_pie(d, box, inset=0):
    b = (box[0] + inset, box[1] + inset, box[2] - inset, box[3] - inset)
    d.pieslice(b, 180, 270, fill=RGBA["red"])
    d.pieslice(b, 270, 360, fill=RGBA["blue"])
    d.pieslice(b, 0, 90, fill=RGBA["green"])
    d.pieslice(b, 90, 180, fill=RGBA["yellow"])


def _corners(img, value, is_wild):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer, "RGBA")
    px, py = 48 * S, 58 * S
    if value.isdigit():
        _text(ld, (px, py), value, 56 * S, WHITE)
    elif value == "skip":
        _draw_skip(ld, px, py, 20 * S, WHITE)
    elif value == "reverse":
        _draw_reverse(ld, px, py, 22 * S, WHITE)
    elif value == "draw2":
        _text(ld, (px, py), "+2", 48 * S, WHITE)
    elif value == "wild4":
        _text(ld, (px, py), "+4", 48 * S, WHITE)
    elif value == "wild":
        r = 19 * S
        _draw_pie(ld, (px - r, py - r, px + r, py + r))
    img = Image.alpha_composite(img, layer)
    img = Image.alpha_composite(img, layer.rotate(180))
    return img


def _render(color, value, wild_color=None):
    is_wild = value in WILDS
    chosen = is_wild and wild_color in RGBA
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img, "RGBA")
    border = RGBA[wild_color] if chosen else WHITE
    d.rounded_rectangle((0, 0, W - 1, H - 1), radius=32 * S, fill=border)
    m = (21 if chosen else 15) * S
    base = BLACK if is_wild else RGBA[color]
    d.rounded_rectangle((m, m, W - 1 - m, H - 1 - m), radius=22 * S, fill=base)

    cx, cy = W // 2, H // 2
    rx, ry = 84 * S, 140 * S
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer, "RGBA")
    ob = (cx - rx, cy - ry, cx + rx, cy + ry)
    ld.ellipse(ob, fill=WHITE)
    if is_wild:
        inner = (ob[0] + 9 * S, ob[1] + 9 * S, ob[2] - 9 * S, ob[3] - 9 * S)
        if chosen:
            ld.ellipse(inner, fill=RGBA[wild_color])
        else:
            _draw_pie(ld, inner)
    layer = layer.rotate(-25, resample=Image.Resampling.BICUBIC)
    img = Image.alpha_composite(img, layer)
    d = ImageDraw.Draw(img, "RGBA")

    if value.isdigit():
        _text(d, (cx, cy), value, 170 * S, RGBA[color], shadow=True)
        if value in ("6", "9"):
            d.line((cx - 40 * S, cy + 90 * S, cx + 40 * S, cy + 90 * S), fill=RGBA[color], width=9 * S)
    elif value == "skip":
        _draw_skip(d, cx, cy, 74 * S, RGBA[color])
    elif value == "reverse":
        _draw_reverse(d, cx, cy, 70 * S, RGBA[color])
    elif value == "draw2":
        _draw_draw2(d, cx, cy, 78 * S, RGBA[color])
    elif value == "wild4":
        if chosen:
            _text(d, (cx, cy), "+4", 110 * S, WHITE, shadow=True,
                  shadow_fill=tuple(int(v * 0.6) for v in RGB[wild_color]) + (255,))
        else:
            _draw_four_cards(d, cx, cy, 70 * S)

    img = _corners(img, value, is_wild)
    return img.resize((OUT_W, OUT_H), Image.Resampling.LANCZOS)


_card_cache = {}
_png_cache = {}
_thumb_cache = {}


def card_image(card, wild_color=None):
    if card[1] not in WILDS:
        wild_color = None
    key = (card[0], card[1], wild_color)
    if key not in _card_cache:
        _card_cache[key] = _render(card[0], card[1], wild_color)
    return _card_cache[key]


def card_png(card, wild_color=None):
    if card[1] not in WILDS:
        wild_color = None
    key = (card[0], card[1], wild_color)
    if key not in _png_cache:
        buf = io.BytesIO()
        card_image(card, wild_color).save(buf, format="PNG")
        _png_cache[key] = buf.getvalue()
    return _png_cache[key]


def card_thumb(card):
    if card not in _thumb_cache:
        _thumb_cache[card] = card_image(card).resize((THUMB_W, THUMB_H), Image.Resampling.LANCZOS)
    return _thumb_cache[card]


def render_hand(hand, playable=None):
    cards = sorted(hand, key=sort_key)
    gap, per_row = 10, 8
    n = max(1, len(cards))
    cols = min(n, per_row)
    rows = math.ceil(n / per_row)
    canvas = Image.new("RGBA", (cols * THUMB_W + (cols + 1) * gap, rows * THUMB_H + (rows + 1) * gap), (32, 34, 37, 255))
    for i, card in enumerate(cards):
        im = card_thumb(card)
        if playable is not None and card not in playable:
            black = Image.new("RGBA", im.size, (0, 0, 0, 255))
            dim = Image.blend(im, black, 0.55)
            dim.putalpha(im.getchannel("A"))
            im = dim
        r, c = divmod(i, per_row)
        canvas.alpha_composite(im, (gap + c * (THUMB_W + gap), gap + r * (THUMB_H + gap)))
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def prerender():
    for color in COLOR_KEYS:
        for value in VALUE_ORDER:
            if value in WILDS:
                continue
            card_png((color, value))
            card_thumb((color, value))
    for value in WILDS:
        card_png((None, value))
        card_thumb((None, value))
        for color in COLOR_KEYS:
            card_png((None, value), color)


# ======================================================================
# Table rendering (one image, edited in place every move)
# ======================================================================
TS = 2  # supersampling for the table
CW, CH = 1200, 760
AV_D = 104 * TS
SEAT_AX, SEAT_AY = 480, 278
PALETTE = [
    (237, 28, 36), (0, 114, 188), (0, 166, 81), (255, 160, 0), (142, 68, 173),
    (0, 150, 150), (210, 70, 130), (90, 90, 100), (180, 100, 40), (60, 60, 200),
]
GOLD = (255, 214, 0, 255)

_back_cache = {}
_fallback_cache = {}


def _darken(c, f):
    return tuple(int(x * f) for x in c)


def _uno_word(d, xy, size, fill):
    font = get_font(size)
    d.text(xy, "UNO", font=font, fill=fill, anchor="mm",
           stroke_width=max(2, size // 11), stroke_fill=(0, 0, 0, 255))


def _render_back_full():
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle((0, 0, W - 1, H - 1), radius=32 * S, fill=WHITE)
    m = 15 * S
    d.rounded_rectangle((m, m, W - 1 - m, H - 1 - m), radius=22 * S, fill=BLACK)
    cx, cy = W // 2, H // 2
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(layer, "RGBA").ellipse((cx - 84 * S, cy - 140 * S, cx + 84 * S, cy + 140 * S), fill=RGBA["red"])
    layer = layer.rotate(-25, resample=Image.Resampling.BICUBIC)
    img = Image.alpha_composite(img, layer)
    tl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    _uno_word(ImageDraw.Draw(tl, "RGBA"), (cx, cy), 74 * S, RGBA["yellow"])
    tl = tl.rotate(25, resample=Image.Resampling.BICUBIC)
    return Image.alpha_composite(img, tl)


def get_back(size):
    if size not in _back_cache:
        _back_cache[size] = _render_back_full().resize(size, Image.Resampling.LANCZOS)
    return _back_cache[size]


def make_circle(img, diameter):
    img = img.convert("RGBA").resize((diameter, diameter), Image.Resampling.LANCZOS)
    big = diameter * 4
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
    mask = mask.resize((diameter, diameter), Image.Resampling.LANCZOS)
    out = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def fallback_avatar(index):
    if index not in _fallback_cache:
        _fallback_cache[index] = make_circle(_fallback_src(index), AV_D)
    return _fallback_cache[index]


def _paste_center(img, im, center, angle=0):
    r = im.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True) if angle else im
    x, y = int(center[0] - r.width / 2), int(center[1] - r.height / 2)
    pad = 24 * TS
    sh = Image.new("RGBA", (r.width + 2 * pad, r.height + 2 * pad), (0, 0, 0, 0))
    black = Image.new("RGBA", r.size, (0, 0, 0, 255))
    black.putalpha(r.getchannel("A").point(lambda v: int(v * 0.5)))
    sh.paste(black, (pad, pad))
    sh = sh.filter(ImageFilter.GaussianBlur(8 * TS))
    img.paste(sh, (x - pad + 6 * TS, y - pad + 10 * TS), sh)
    img.paste(r, (x, y), r)


def _pill(d, center, text, fill, fg, size, width, height, outline=WHITE):
    x, y = center
    d.rounded_rectangle((x - width / 2, y - height / 2, x + width / 2, y + height / 2),
                        radius=height / 2, fill=fill, outline=outline, width=3 * TS)
    d.text((x, y), text, font=get_font(size), fill=fg, anchor="mm")


def render_table(snap):
    s = TS
    w, h = CW * s, CH * s
    cx, cy = w // 2, h // 2
    color = snap["color"]
    base = RGB[color]
    img = Image.new("RGB", (w, h), (12, 12, 16))
    d = ImageDraw.Draw(img, "RGBA")

    # table: white edge, Uno colored rim, felt in the current color
    tb = (cx - 540 * s, cy - 320 * s, cx + 540 * s, cy + 320 * s)
    d.ellipse(tb, fill=WHITE)
    rim = (tb[0] + 8 * s, tb[1] + 8 * s, tb[2] - 8 * s, tb[3] - 8 * s)
    d.pieslice(rim, 180, 270, fill=RGBA["red"])
    d.pieslice(rim, 270, 360, fill=RGBA["blue"])
    d.pieslice(rim, 0, 90, fill=RGBA["green"])
    d.pieslice(rim, 90, 180, fill=RGBA["yellow"])
    fb = (rim[0] + 18 * s, rim[1] + 18 * s, rim[2] - 18 * s, rim[3] - 18 * s)
    d.ellipse(fb, fill=_darken(base, 0.55) + (255,))
    fw, fh = fb[2] - fb[0], fb[3] - fb[1]
    grad = Image.radial_gradient("L").resize((fw, fh), Image.Resampling.BILINEAR)
    emask = Image.new("L", (fw, fh), 0)
    ImageDraw.Draw(emask).ellipse((0, 0, fw - 1, fh - 1), fill=255)
    shade_mask = ImageChops.multiply(grad.point(lambda v: int(v * 0.6)), emask)
    img.paste(Image.new("RGBA", (fw, fh), (0, 0, 0, 255)), (fb[0], fb[1]), shade_mask)
    d.ellipse((fb[0] + 14 * s, fb[1] + 14 * s, fb[2] - 14 * s, fb[3] - 14 * s),
              outline=(255, 255, 255, 70), width=3 * s)

    # direction arrows
    a, b = 380 * s, 190 * s
    for c in (45, 135, 225, 315):
        t0, t1 = math.radians(c - 26), math.radians(c + 26)
        ts = [t0 + (t1 - t0) * k / 24 for k in range(25)]
        d.line([(cx + a * math.cos(t), cy + b * math.sin(t)) for t in ts],
               fill=(225, 225, 225, 255), width=9 * s, joint="curve")
        if snap["direction"] == 1:
            t = t1
            tx, ty = -a * math.sin(t), b * math.cos(t)
        else:
            t = t0
            tx, ty = a * math.sin(t), -b * math.cos(t)
        n = math.hypot(tx, ty)
        tx, ty = tx / n, ty / n
        px, py = cx + a * math.cos(t), cy + b * math.sin(t)
        nx, ny = -ty, tx
        d.polygon([(px + tx * 30 * s, py + ty * 30 * s),
                   (px + nx * 20 * s, py + ny * 20 * s),
                   (px - nx * 20 * s, py - ny * 20 * s)], fill=(225, 225, 225, 255))

    # piles
    cw_, ch_ = 170 * s, 255 * s
    dpx, dcx = cx - 105 * s, cx + 105 * s

    gw, gh = 460 * s, 500 * s
    glow = Image.new("RGBA", (gw, gh), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((gw // 2 - 150 * s, gh // 2 - 170 * s, gw // 2 + 150 * s, gh // 2 + 170 * s),
                                 fill=base + (170,))
    glow = glow.filter(ImageFilter.GaussianBlur(28 * s))
    img.paste(glow, (dcx - gw // 2, cy - gh // 2), glow)

    back = get_back((cw_, ch_))
    for k in (3, 2, 1, 0):
        _paste_center(img, back, (dpx - k * 3 * s, cy - k * 3 * s))

    if snap["prev"] is not None:
        prev = card_image(snap["prev"]).resize((cw_, ch_), Image.Resampling.LANCZOS)
        _paste_center(img, prev, (dcx - 16 * s, cy + 6 * s), 11)
    top = card_image(snap["top"], snap["wild_color"]).resize((cw_, ch_), Image.Resampling.LANCZOS)
    _paste_center(img, top, (dcx, cy), -6)

    d = ImageDraw.Draw(img, "RGBA")
    _pill(d, (dpx, cy + 165 * s), str(snap["deck"]), (20, 20, 24, 235), WHITE, 26 * s, 96 * s, 40 * s)
    fg = (20, 20, 24, 255) if color == "yellow" else WHITE
    _pill(d, (dcx, cy + 165 * s), color.upper(), RGBA[color], fg, 24 * s, 130 * s, 40 * s)

    # logo
    lw, lh = 170 * s, 100 * s
    lg = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lg, "RGBA")
    ld.ellipse((4 * s, 6 * s, lw - 4 * s, lh - 6 * s), fill=RGBA["yellow"], outline=WHITE, width=5 * s)
    _uno_word(ld, (lw // 2, lh // 2), 46 * s, RGBA["red"])
    lg = lg.rotate(12, resample=Image.Resampling.BICUBIC)
    img.paste(lg, (24 * s, 20 * s), lg)

    # seats
    players = snap["players"]
    n = len(players)
    R = AV_D // 2
    for i, p in enumerate(players):
        th = math.radians(90 + 360 * i / n)
        x = int(cx + SEAT_AX * s * math.cos(th))
        y = int(cy + SEAT_AY * s * math.sin(th))
        if p["current"]:
            d.ellipse((x - R - 20 * s, y - R - 20 * s, x + R + 20 * s, y + R + 20 * s), fill=(255, 214, 0, 70))
            d.ellipse((x - R - 9 * s, y - R - 9 * s, x + R + 9 * s, y + R + 9 * s), fill=GOLD)
        elif p["winner"]:
            d.ellipse((x - R - 20 * s, y - R - 20 * s, x + R + 20 * s, y + R + 20 * s), fill=(255, 214, 0, 90))
            d.ellipse((x - R - 9 * s, y - R - 9 * s, x + R + 9 * s, y + R + 9 * s), fill=GOLD)
        else:
            d.ellipse((x - R - 5 * s, y - R - 5 * s, x + R + 5 * s, y + R + 5 * s), fill=(255, 255, 255, 235))
        img.paste(p["avatar"], (x - R, y - R), p["avatar"])
        if p["skipped"]:
            kx, ky, kr = x + int(R * 0.72), y - int(R * 0.72), 24 * s
            d.ellipse((kx - kr, ky - kr, kx + kr, ky + kr), fill=(220, 30, 30, 255), outline=WHITE, width=3 * s)
            d.line((kx - kr * 0.6, ky - kr * 0.6, kx + kr * 0.6, ky + kr * 0.6), fill=WHITE, width=5 * s)
        if p["winner"]:
            cyc = y - R - 6 * s
            d.polygon([(x - 30 * s, cyc), (x - 30 * s, cyc - 28 * s), (x - 15 * s, cyc - 14 * s),
                       (x, cyc - 34 * s), (x + 15 * s, cyc - 14 * s), (x + 30 * s, cyc - 28 * s),
                       (x + 30 * s, cyc)], fill=GOLD, outline=(0, 0, 0, 255))
            _pill(d, (x, y + R - 2 * s), "WIN", GOLD, (20, 20, 24, 255), 24 * s, 84 * s, 36 * s)
        elif p["count"] == 1:
            _pill(d, (x, y + R - 2 * s), "UNO", RGBA["red"], WHITE, 24 * s, 84 * s, 36 * s)
        else:
            _pill(d, (x, y + R - 2 * s), str(p["count"]), (20, 20, 24, 240), WHITE, 26 * s, 72 * s, 36 * s)

    return _to_png(img.resize((CW, CH), Image.Resampling.LANCZOS).convert("RGB"))


def _to_png(im):
    buf = io.BytesIO()
    im.save(buf, format="PNG", compress_level=6)
    return buf.getvalue()


# ======================================================================
# Lobby image, winner image and win statistics
# ======================================================================
def _fallback_src(index):
    img = Image.new("RGBA", (256, 256), PALETTE[index % len(PALETTE)] + (255,))
    ImageDraw.Draw(img, "RGBA").text((128, 128), str(index + 1), font=get_font(128), fill=WHITE, anchor="mm")
    return img


def _crown(d, x, base_y, width):
    hw = width / 2
    hgt = width * 0.55
    d.polygon([
        (x - hw, base_y), (x - hw, base_y - hgt * 0.8), (x - hw * 0.5, base_y - hgt * 0.45),
        (x, base_y - hgt), (x + hw * 0.5, base_y - hgt * 0.45), (x + hw, base_y - hgt * 0.8),
        (x + hw, base_y),
    ], fill=GOLD, outline=(0, 0, 0, 255))


def _paste_logo(img, cx, cy, width):
    lw, lh = int(width), int(width * 0.59)
    lg = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lg, "RGBA")
    ld.ellipse((lw * 0.02, lh * 0.06, lw * 0.98, lh * 0.94), fill=RGBA["yellow"],
               outline=WHITE, width=max(2, int(lw * 0.03)))
    _uno_word(ld, (lw // 2, lh // 2), int(lw * 0.27), RGBA["red"])
    lg = lg.rotate(12, resample=Image.Resampling.BICUBIC, expand=True)
    img.paste(lg, (int(cx - lg.width / 2), int(cy - lg.height / 2)), lg)


def _draw_table_base(img, d, cx, cy, rx, ry, felt):
    s = TS
    tb = (cx - rx, cy - ry, cx + rx, cy + ry)
    d.ellipse(tb, fill=WHITE)
    rim = (tb[0] + 8 * s, tb[1] + 8 * s, tb[2] - 8 * s, tb[3] - 8 * s)
    d.pieslice(rim, 180, 270, fill=RGBA["red"])
    d.pieslice(rim, 270, 360, fill=RGBA["blue"])
    d.pieslice(rim, 0, 90, fill=RGBA["green"])
    d.pieslice(rim, 90, 180, fill=RGBA["yellow"])
    fb = (rim[0] + 18 * s, rim[1] + 18 * s, rim[2] - 18 * s, rim[3] - 18 * s)
    d.ellipse(fb, fill=tuple(felt) + (255,))
    fw, fh = fb[2] - fb[0], fb[3] - fb[1]
    grad = Image.radial_gradient("L").resize((fw, fh), Image.Resampling.BILINEAR)
    emask = Image.new("L", (fw, fh), 0)
    ImageDraw.Draw(emask).ellipse((0, 0, fw - 1, fh - 1), fill=255)
    shade_mask = ImageChops.multiply(grad.point(lambda v: int(v * 0.6)), emask)
    img.paste(Image.new("RGBA", (fw, fh), (0, 0, 0, 255)), (fb[0], fb[1]), shade_mask)
    d.ellipse((fb[0] + 14 * s, fb[1] + 14 * s, fb[2] - 14 * s, fb[3] - 14 * s),
              outline=(255, 255, 255, 70), width=3 * s)


def render_lobby(snap):
    s = TS
    LW, LH = 1200, 700
    w, h = LW * s, LH * s
    cx, cy = w // 2, 380 * s
    img = Image.new("RGB", (w, h), (12, 12, 16))
    d = ImageDraw.Draw(img, "RGBA")
    _draw_table_base(img, d, cx, cy, 560 * s, 300 * s, (96, 14, 20))

    cw_, ch_ = 120 * s, 180 * s
    back = get_back((cw_, ch_))
    deco = [
        ((100, 150), ("red", "7"), -12, 14),
        ((1100, 150), ("blue", "draw2"), 12, -14),
        ((100, 570), ("green", "skip"), 10, -12),
        ((1100, 570), ("yellow", "reverse"), -10, 12),
    ]
    for (x, y), card, ang, back_ang in deco:
        _paste_center(img, back, (x * s - 14 * s, y * s + 6 * s), back_ang)
        _paste_center(img, card_image(card).resize((cw_, ch_), Image.Resampling.LANCZOS), (x * s, y * s), ang)

    d = ImageDraw.Draw(img, "RGBA")
    _paste_logo(img, cx, 112 * s, 300 * s)

    srcs = snap["srcs"]
    D = 120 * s
    R = D // 2
    for k in range(snap["max"]):
        row, col = divmod(k, 5)
        x = cx + (col - 2) * 170 * s
        y = (330 + row * 160) * s
        if k < len(srcs):
            ring = GOLD if k == 0 else WHITE
            d.ellipse((x - R - 6 * s, y - R - 6 * s, x + R + 6 * s, y + R + 6 * s), fill=ring)
            av = make_circle(srcs[k], D)
            img.paste(av, (x - R, y - R), av)
            if k == 0:
                _crown(d, x, y - R - 6 * s, 64 * s)
        else:
            d.ellipse((x - R, y - R, x + R, y + R), fill=(255, 255, 255, 22),
                      outline=(255, 255, 255, 90), width=3 * s)
            arm = 22 * s
            d.line((x - arm, y, x + arm, y), fill=(255, 255, 255, 110), width=6 * s)
            d.line((x, y - arm, x, y + arm), fill=(255, 255, 255, 110), width=6 * s)

    n = len(srcs)
    _pill(d, (cx, 232 * s), f"{n} / {snap['max']}", (20, 20, 24, 240), WHITE, 40 * s, 200 * s, 58 * s)
    dark = (20, 20, 24, 255)
    if snap["state"] == "starting":
        label, fill, fg = "STARTING", GOLD, dark
    elif n >= snap["min"]:
        label, fill, fg = "READY", RGBA["green"], WHITE
    else:
        label, fill, fg = "WAITING", RGBA["yellow"], dark
    _pill(d, (cx, 628 * s), label, fill, fg, 34 * s, 250 * s, 54 * s)
    return _to_png(img.resize((LW, LH), Image.Resampling.LANCZOS).convert("RGB"))


def render_win(snap):
    s = TS
    WW, WH = 1200, 760
    w, h = WW * s, WH * s
    cx = w // 2
    img = Image.new("RGB", (w, h), (10, 10, 14))

    gw, gh = 1100 * s, 760 * s
    glow = Image.new("RGBA", (gw, gh), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((gw // 2 - 400 * s, 60 * s, gw // 2 + 400 * s, 600 * s), fill=(255, 190, 0, 120))
    glow = glow.filter(ImageFilter.GaussianBlur(90 * s))
    img.paste(glow, (cx - gw // 2, 0), glow)

    rnd = random.Random(snap["seed"])
    colors = [RGBA[k] for k in COLOR_KEYS] + [GOLD, WHITE]
    for _ in range(150):
        pw, ph = rnd.randint(10, 20) * s, rnd.randint(18, 34) * s
        piece = Image.new("RGBA", (pw, ph), rnd.choice(colors))
        piece = piece.rotate(rnd.randint(0, 359), expand=True)
        px, py = rnd.randint(0, w), rnd.randint(0, h)
        if abs(px - cx) < 340 * s and 90 * s < py < 690 * s:
            continue
        img.paste(piece, (px, py), piece)

    d = ImageDraw.Draw(img, "RGBA")
    ay = 290 * s
    R = 115 * s
    d.ellipse((cx - R - 34 * s, ay - R - 34 * s, cx + R + 34 * s, ay + R + 34 * s), fill=(255, 214, 0, 70))
    d.ellipse((cx - R - 12 * s, ay - R - 12 * s, cx + R + 12 * s, ay + R + 12 * s), fill=GOLD)
    av = make_circle(snap["src"], 2 * R)
    img.paste(av, (cx - R, ay - R), av)
    _crown(d, cx, ay - R - 10 * s, 120 * s)

    d.text((cx, 500 * s), "WINNER", font=get_font(112 * s), fill=GOLD, anchor="mm",
           stroke_width=6 * s, stroke_fill=(0, 0, 0, 255))
    d.rounded_rectangle((cx - 290 * s, 560 * s, cx + 290 * s, 660 * s), radius=50 * s,
                        fill=(20, 20, 24, 240), outline=GOLD, width=4 * s)
    d.text((cx - 150 * s, 610 * s), "TOTAL WINS", font=get_font(36 * s), fill=WHITE, anchor="mm")
    d.text((cx + 175 * s, 610 * s), str(snap["wins"]), font=get_font(70 * s), fill=GOLD, anchor="mm")

    others = snap["others"]
    if others:
        spacing = 100 * s
        x0 = cx - (len(others) - 1) * spacing / 2
        oy = 705 * s
        r = 32 * s
        for i, (src, count) in enumerate(others):
            x = int(x0 + i * spacing)
            d.ellipse((x - r - 3 * s, oy - r - 3 * s, x + r + 3 * s, oy + r + 3 * s), fill=(255, 255, 255, 235))
            oav = make_circle(src, 2 * r)
            img.paste(oav, (x - r, oy - r), oav)
            _pill(d, (x, oy + r + 2 * s), str(count), (20, 20, 24, 240), WHITE, 22 * s, 52 * s, 26 * s)

    _paste_logo(img, 130 * s, 70 * s, 190 * s)
    return _to_png(img.resize((WW, WH), Image.Resampling.LANCZOS).convert("RGB"))


class WinStore:
    """Keeps win counts. Saved to a local json file, and also to a Discord channel if
    STORAGE_CHANNEL_ID is set (so the counts survive Render restarts)."""

    def __init__(self):
        self.wins = {}
        self.msg = None
        self.lock = asyncio.Lock()
        self.path = os.getenv("WINS_FILE", "wins.json")
        raw = os.getenv("STORAGE_CHANNEL_ID", "").strip()
        self.channel_id = int(raw) if raw.isdigit() else 0

    def _load_local(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.wins = {int(k): int(v) for k, v in json.load(f).items()}
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("could not read local wins file")

    async def load(self, client):
        self._load_local()
        if not self.channel_id:
            return
        try:
            ch = await client.fetch_channel(self.channel_id)
            async for m in ch.history(limit=100):
                if m.author.id == client.user.id and m.content == "UNO_WINS_DATA" and m.attachments:
                    data = json.loads((await m.attachments[0].read()).decode("utf-8"))
                    self.wins = {int(k): int(v) for k, v in data.items()}
                    self.msg = m
                    break
        except Exception:
            log.exception("could not load wins from the storage channel")

    async def _save(self):
        payload = json.dumps({str(k): v for k, v in self.wins.items()}).encode("utf-8")
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(payload.decode("utf-8"))
        except Exception:
            log.exception("could not write local wins file")
        if not self.channel_id:
            return
        try:
            if self.msg is not None:
                try:
                    await self.msg.edit(attachments=[discord.File(io.BytesIO(payload), filename="wins.json")])
                    return
                except discord.HTTPException:
                    self.msg = None
            ch = await bot.fetch_channel(self.channel_id)
            self.msg = await ch.send(content="UNO_WINS_DATA",
                                     file=discord.File(io.BytesIO(payload), filename="wins.json"))
        except Exception:
            log.exception("could not save wins to the storage channel")

    async def add_win(self, uid):
        async with self.lock:
            self.wins[uid] = self.wins.get(uid, 0) + 1
            n = self.wins[uid]
            await self._save()
            return n


stats = WinStore()


# ======================================================================
# Game
# ======================================================================
games = {}


class Player:
    def __init__(self, member):
        self.member = member
        self.id = member.id
        self.hand = []
        self.uno = False
        self.timeouts = 0
        self.avatar = None
        self.avatar_src = None

    @property
    def mention(self):
        return self.member.mention


async def load_avatar(member, index):
    try:
        data = await member.display_avatar.replace(size=256, format="png").read()
        src = Image.open(io.BytesIO(data)).convert("RGBA")
        return make_circle(src, AV_D), src
    except Exception:
        log.warning("avatar load failed", exc_info=True)
        src = _fallback_src(index)
        return make_circle(src, AV_D), src


class UnoGame:
    def __init__(self, channel, host):
        self.channel = channel
        self.host = host
        self.players = []
        self.started = False
        self.ended = False
        self.deck = []
        self.discard = []
        self.turn = 0
        self.direction = 1
        self.color = None
        self.top = None
        self.drawn = False
        self.drawn_card = None
        self.skip_marks = set()
        self.uno_pending = None
        self.table_msg = None
        self.lobby_msg = None
        self.lobby_view = None
        self.event_msg = None
        self.lock = asyncio.Lock()
        self.timer = None
        self.turn_token = 0

    # ---------- helpers ----------
    def find(self, uid):
        for p in self.players:
            if p.id == uid:
                return p
        return None

    def current(self):
        return self.players[self.turn]

    def step(self, idx, k=1):
        return (idx + self.direction * k) % len(self.players)

    def draw(self, n):
        out = []
        for _ in range(n):
            if not self.deck:
                if len(self.discard) <= 1:
                    break
                top = self.discard.pop()
                self.deck = self.discard
                self.discard = [top]
                random.shuffle(self.deck)
            out.append(self.deck.pop())
        return out

    def give(self, player, n):
        cards = self.draw(n)
        player.hand.extend(cards)
        player.uno = False
        return len(cards)

    def card_ok(self, player, card):
        color, value = card
        if value == "wild":
            return True
        if value == "wild4":
            return not any(c[0] == self.color for c in player.hand if c[0] is not None)
        if color == self.color:
            return True
        return self.top[1] not in WILDS and value == self.top[1]

    def playable_unique(self, player):
        cards = {c for c in player.hand if self.card_ok(player, c)}
        if self.drawn:
            cards = {c for c in cards if c == self.drawn_card}
        return sorted(cards, key=sort_key)

    def check_turn(self, uid, card=None):
        if self.ended or not self.started:
            return "اللعبة غير نشطة حاليا."
        player = self.current()
        if player.id != uid:
            return "ليس دورك الآن."
        if card is not None:
            if card not in player.hand:
                return "هذه الورقة ليست بحوزتك."
            if self.drawn and card != self.drawn_card:
                return "بعد السحب يمكنك لعب الورقة المسحوبة فقط، أو تمرير الدور."
            if not self.card_ok(player, card):
                return "لا يمكنك لعب هذه الورقة الآن."
        return None

    def settle_skips(self, lines):
        guard = 0
        while self.current().id in self.skip_marks and guard < 30:
            p = self.current()
            self.skip_marks.discard(p.id)
            lines.append(f"تم تخطي دور {p.mention} بسبب ورقة المنع.")
            self.turn = self.step(self.turn, 1)
            guard += 1

    def remove_player(self, player):
        idx = self.players.index(player)
        self.deck[0:0] = player.hand
        player.hand = []
        self.players.pop(idx)
        self.skip_marks.discard(player.id)
        if self.uno_pending == player.id:
            self.uno_pending = None
        if idx < self.turn:
            self.turn -= 1
        elif idx == self.turn:
            self.drawn = False
            self.drawn_card = None
            if self.players:
                if self.direction == -1:
                    self.turn = (idx - 1) % len(self.players)
                else:
                    self.turn = idx % len(self.players)

    def _cancel_timer(self):
        if self.timer and self.timer is not asyncio.current_task():
            self.timer.cancel()
        self.timer = None

    def _arm_timer(self):
        self._cancel_timer()
        self.turn_token += 1
        self.timer = asyncio.create_task(self._timeout(self.turn_token))

    # ---------- embeds ----------
    def lobby_snapshot(self, state="open"):
        srcs = [p.avatar_src if p.avatar_src is not None else _fallback_src(i) for i, p in enumerate(self.players)]
        return {"srcs": srcs, "max": MAX_PLAYERS, "min": MIN_PLAYERS, "state": state}

    async def lobby_png(self, state="open"):
        return await asyncio.to_thread(render_lobby, self.lobby_snapshot(state))

    # ---------- table ----------
    def snapshot(self, winner=None):
        wild_color = self.color if self.top[1] in WILDS else None
        prev = self.discard[-2] if len(self.discard) >= 2 else None
        players = []
        for i, p in enumerate(self.players):
            players.append({
                "avatar": p.avatar or fallback_avatar(i),
                "count": len(p.hand),
                "current": winner is None and i == self.turn,
                "skipped": p.id in self.skip_marks,
                "winner": winner is not None and p.id == winner.id,
            })
        return {
            "color": self.color,
            "top": self.top,
            "wild_color": wild_color,
            "prev": prev,
            "direction": self.direction,
            "deck": len(self.deck),
            "players": players,
        }

    async def _update_table(self, png, view=None):
        if self.table_msg is not None:
            try:
                file = discord.File(io.BytesIO(png), filename="table.png")
                await self.table_msg.edit(attachments=[file], view=view)
                return
            except discord.HTTPException:
                log.warning("table edit failed, sending a new table message", exc_info=True)
                self.table_msg = None
        file = discord.File(io.BytesIO(png), filename="table.png")
        kwargs = {"view": view} if view is not None else {}
        self.table_msg = await self.channel.send(file=file, **kwargs)

    async def _send_event(self, text, replace=True):
        old = self.event_msg
        self.event_msg = await self.channel.send(text)
        if replace and old is not None and not KEEP_EVENT_HISTORY:
            try:
                await old.delete()
            except discord.HTTPException:
                pass

    async def post_table(self, lines):
        player = self.current()
        png = await asyncio.to_thread(render_table, self.snapshot())
        await self._update_table(png, TableView(self))
        text = "\n".join(lines + [f"الدور الآن على {player.mention}"])
        await self._send_event(text)
        self._arm_timer()

    async def begin(self):
        async with self.lock:
            for i, p in enumerate(self.players):
                if p.avatar is None:
                    p.avatar, p.avatar_src = await load_avatar(p.member, i)
            self.deck = build_deck()
            for p in self.players:
                p.hand = [self.deck.pop() for _ in range(START_CARDS)]
            while True:
                card = self.deck.pop()
                if card[1].isdigit():
                    break
                self.deck.insert(0, card)
            self.discard = [card]
            self.top = card
            self.color = card[0]
            self.turn = random.randrange(len(self.players))
            lines = [
                f"بدأت اللعبة. تم توزيع {START_CARDS} أوراق لكل لاعب.",
                f"الورقة الأولى على الطاولة: {card_name(card)}",
            ]
            await self.post_table(lines)

    async def finish(self, winner, lines):
        self.ended = True
        self._cancel_timer()
        if games.get(self.channel.id) is self:
            games.pop(self.channel.id, None)
        wins = await stats.add_win(winner.id)
        try:
            png = await asyncio.to_thread(render_table, self.snapshot(winner))
            await self._update_table(png, None)
        except Exception:
            log.exception("final table failed")
        text = "\n".join(
            ["@here"] + lines + [f"الفائز في لعبة أونو هو {winner.mention}. هذا فوزه رقم {wins}. مبروك!"]
        )
        try:
            others = [
                (p.avatar_src if p.avatar_src is not None else _fallback_src(i), len(p.hand))
                for i, p in enumerate(self.players) if p.id != winner.id
            ]
            wsnap = {
                "src": winner.avatar_src if winner.avatar_src is not None else _fallback_src(0),
                "wins": wins,
                "others": others,
                "seed": winner.id,
            }
            wpng = await asyncio.to_thread(render_win, wsnap)
            await self.channel.send(content=text, file=discord.File(io.BytesIO(wpng), filename="winner.png"))
        except Exception:
            log.exception("winner image failed")
            await self.channel.send(content=text)

    async def force_end(self, by):
        async with self.lock:
            if self.ended:
                return
            self.ended = True
            self._cancel_timer()
            if games.get(self.channel.id) is self:
                games.pop(self.channel.id, None)
            if self.table_msg:
                try:
                    await self.table_msg.edit(view=None)
                except discord.HTTPException:
                    pass
            await self.channel.send(f"{by.mention} أنهى اللعبة.")

    # ---------- actions ----------
    async def play(self, uid, card, chosen_color=None, target_id=None):
        async with self.lock:
            err = self.check_turn(uid, card)
            if err:
                return err
            player = self.current()
            value = card[1]
            if value in WILDS and chosen_color not in COLOR_KEYS:
                return "لم يتم اختيار لون."
            target = None
            if value == "skip":
                target = self.find(target_id) if target_id != uid else None
                if target is None:
                    return "اللاعب المختار غير موجود في اللعبة."

            idx = self.turn
            m = player.mention
            player.hand.remove(card)
            self.discard.append(card)
            self.top = card
            self.color = chosen_color if value in WILDS else card[0]
            self.drawn = False
            self.drawn_card = None
            self.uno_pending = None
            player.timeouts = 0
            n = len(self.players)
            lines = []

            if value == "reverse":
                self.direction *= -1
                if n == 2:
                    lines.append(f"{m} لعب ورقة العكس. بما أن عدد اللاعبين اثنان، يلعب {m} مرة أخرى.")
                    nxt = idx
                else:
                    lines.append(f"{m} لعب ورقة العكس. تم عكس اتجاه اللعب.")
                    nxt = self.step(idx, 1)
            elif value == "skip":
                self.skip_marks.add(target.id)
                lines.append(f"{m} لعب ورقة المنع على {target.mention}. سيتم تخطي دور {target.mention} القادم.")
                nxt = self.step(idx, 1)
            elif value == "draw2":
                victim = self.players[self.step(idx, 1)]
                got = self.give(victim, 2)
                self.skip_marks.discard(victim.id)
                lines.append(f"{m} لعب ورقة سحب 2. {victim.mention} سحب {got} أوراق وخسر دوره.")
                nxt = self.step(idx, 2)
            elif value == "wild":
                lines.append(f"{m} لعب ورقة تغيير اللون وجعل اللون {COLOR_AR[chosen_color]}.")
                nxt = self.step(idx, 1)
            elif value == "wild4":
                victim = self.players[self.step(idx, 1)]
                got = self.give(victim, 4)
                self.skip_marks.discard(victim.id)
                lines.append(
                    f"{m} لعب ورقة تغيير اللون وسحب 4، وجعل اللون {COLOR_AR[chosen_color]}. "
                    f"{victim.mention} سحب {got} أوراق وخسر دوره."
                )
                nxt = self.step(idx, 2)
            else:
                lines.append(f"{m} لعب ورقة {card_name(card)}.")
                nxt = self.step(idx, 1)

            if not player.hand:
                await self.finish(player, lines)
                return None

            if len(player.hand) == 1 and not player.uno:
                self.uno_pending = player.id
                lines.append(f"{m} بقيت معه ورقة واحدة. على الجميع الضغط على زر أونو لكشفه إن لم يقلها.")
            elif len(player.hand) == 1:
                lines.append(f"{m} بقيت معه ورقة واحدة.")

            self.turn = nxt
            self.settle_skips(lines)
            await self.post_table(lines)
            return None

    async def draw_action(self, uid):
        async with self.lock:
            err = self.check_turn(uid)
            if err:
                return err
            if self.drawn:
                return "سحبت ورقة بالفعل، العبها أو مرر الدور."
            player = self.current()
            m = player.mention
            self.uno_pending = None
            player.timeouts = 0
            got = self.give(player, 1)
            if got == 0:
                lines = [f"{m} حاول السحب لكن لا توجد أوراق متبقية. تم تمرير الدور."]
                self.turn = self.step(self.turn, 1)
                self.settle_skips(lines)
                await self.post_table(lines)
                return None
            card = player.hand[-1]
            if self.card_ok(player, card):
                self.drawn = True
                self.drawn_card = card
                lines = [f"{m} سحب ورقة من الكومة وأصبح بإمكانه لعبها أو تمرير الدور."]
            else:
                lines = [f"{m} سحب ورقة ولم تكن مناسبة. تم تمرير الدور."]
                self.turn = self.step(self.turn, 1)
                self.settle_skips(lines)
            await self.post_table(lines)
            return None

    async def pass_action(self, uid):
        async with self.lock:
            err = self.check_turn(uid)
            if err:
                return err
            if not self.drawn:
                return "لا يمكنك التمرير قبل سحب ورقة."
            player = self.current()
            self.drawn = False
            self.drawn_card = None
            self.uno_pending = None
            lines = [f"{player.mention} مرر دوره."]
            self.turn = self.step(self.turn, 1)
            self.settle_skips(lines)
            await self.post_table(lines)
            return None

    async def uno_action(self, uid):
        async with self.lock:
            if self.ended or not self.started:
                return "اللعبة غير نشطة حاليا."
            me = self.find(uid)
            if me is None:
                return "أنت لست ضمن هذه اللعبة."
            pend = self.uno_pending
            if pend is not None and pend != uid:
                target = self.find(pend)
                if target and len(target.hand) == 1 and not target.uno:
                    self.uno_pending = None
                    got = self.give(target, 2)
                    lines = [
                        f"{me.mention} كشف {target.mention} لأنه لم يقل أونو. "
                        f"{target.mention} سحب {got} أوراق عقوبة."
                    ]
                    await self.post_table(lines)
                    return None
            if len(me.hand) == 1 or (len(me.hand) == 2 and self.current().id == uid):
                if me.uno:
                    return "لقد قلت أونو بالفعل."
                me.uno = True
                if pend == uid:
                    self.uno_pending = None
                await self.channel.send(f"{me.mention} قال أونو!")
                return None
            return "لا يوجد ما يمكنك فعله بزر أونو الآن."

    async def leave(self, uid):
        async with self.lock:
            if self.ended:
                return "اللعبة غير نشطة حاليا."
            player = self.find(uid)
            if player is None:
                return "أنت لست ضمن هذه اللعبة."
            lines = [f"{player.mention} انسحب من اللعبة."]
            self.remove_player(player)
            if len(self.players) < 2:
                await self.finish(self.players[0], lines)
                return None
            self.settle_skips(lines)
            await self.post_table(lines)
            return None

    async def _timeout(self, token):
        try:
            await asyncio.sleep(TURN_SECONDS)
        except asyncio.CancelledError:
            return
        try:
            async with self.lock:
                if self.ended or token != self.turn_token:
                    return
                p = self.current()
                p.timeouts += 1
                self.uno_pending = None
                lines = []
                if p.timeouts >= MAX_TIMEOUTS:
                    lines.append(f"تم إخراج {p.mention} من اللعبة بسبب عدم اللعب.")
                    self.remove_player(p)
                    if len(self.players) < 2:
                        await self.finish(self.players[0], lines)
                        return
                    self.settle_skips(lines)
                    await self.post_table(lines)
                    return
                self.give(p, 1)
                lines.append(f"انتهى وقت {p.mention}. تم سحب ورقة له وتخطي دوره.")
                self.drawn = False
                self.drawn_card = None
                self.turn = self.step(self.turn, 1)
                self.settle_skips(lines)
                await self.post_table(lines)
        except Exception:
            log.exception("turn timeout failed")

    # ---------- hand ----------
    async def show_hand(self, interaction):
        player = self.find(interaction.user.id)
        if player is None:
            await interaction.response.send_message("أنت لست ضمن هذه اللعبة.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        my_turn = self.started and not self.ended and self.current().id == player.id
        options = self.playable_unique(player) if my_turn else []
        png = render_hand(player.hand, set(options) if my_turn else None)
        if not my_turn:
            content = "هذه أوراقك. انتظر دورك."
        elif options:
            content = "هذه أوراقك. الأوراق المضيئة هي التي يمكنك لعبها، اختر واحدة من القائمة."
        else:
            content = "ليس لديك ورقة مناسبة. اضغط على زر السحب في رسالة الطاولة."
        kwargs = {}
        if options:
            kwargs["view"] = HandView(self, player, options)
        await interaction.followup.send(
            content=content,
            file=discord.File(io.BytesIO(png), filename="hand.png"),
            ephemeral=True,
            **kwargs,
        )


# ======================================================================
# Views
# ======================================================================
class BaseView(discord.ui.View):
    async def on_error(self, interaction, error, item):
        log.exception("view error", exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send("حدث خطأ غير متوقع.", ephemeral=True)
            else:
                await interaction.response.send_message("حدث خطأ غير متوقع.", ephemeral=True)
        except discord.HTTPException:
            pass


async def submit_play(interaction, game, uid, card, color=None, target=None):
    await interaction.response.defer()
    err = await game.play(uid, card, color, target)
    try:
        await interaction.edit_original_response(content=err or "تم لعب الورقة.", view=None, attachments=[])
    except discord.HTTPException:
        pass


class ColorView(BaseView):
    def __init__(self, game, uid, card):
        super().__init__(timeout=120)
        self.game, self.uid, self.card = game, uid, card
        styles = {
            "red": discord.ButtonStyle.danger,
            "yellow": discord.ButtonStyle.secondary,
            "green": discord.ButtonStyle.success,
            "blue": discord.ButtonStyle.primary,
        }
        for color in COLOR_KEYS:
            btn = discord.ui.Button(label=COLOR_NAME[color], style=styles[color])
            btn.callback = self._make(color)
            self.add_item(btn)

    def _make(self, color):
        async def callback(interaction):
            await submit_play(interaction, self.game, self.uid, self.card, color=color)
        return callback


class TargetView(BaseView):
    def __init__(self, game, uid, card):
        super().__init__(timeout=120)
        self.game, self.uid, self.card = game, uid, card
        options = [
            discord.SelectOption(label=p.member.display_name[:100], value=str(p.id))
            for p in game.players if p.id != uid
        ]
        self.select = discord.ui.Select(placeholder="اختر اللاعب", options=options[:25])
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction):
        target_id = int(self.select.values[0])
        await submit_play(interaction, self.game, self.uid, self.card, target=target_id)


class HandView(BaseView):
    def __init__(self, game, player, cards):
        super().__init__(timeout=120)
        self.game, self.player = game, player
        options = []
        for c in cards:
            count = player.hand.count(c)
            options.append(discord.SelectOption(
                label=card_name(c),
                value=f"{c[0] or 'wild'}:{c[1]}",
                description=f"لديك {count} من هذه الورقة" if count > 1 else None,
            ))
        self.select = discord.ui.Select(placeholder="اختر الورقة التي تريد لعبها", options=options[:25])
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction):
        game = self.game
        color, value = self.select.values[0].split(":")
        card = (None if color == "wild" else color, value)
        async with game.lock:
            err = game.check_turn(self.player.id, card)
        if err:
            await interaction.response.edit_message(content=err, view=None, attachments=[])
            return
        if value in WILDS:
            await interaction.response.edit_message(
                content="اختر اللون الجديد:", view=ColorView(game, self.player.id, card), attachments=[]
            )
            return
        if value == "skip":
            others = [p for p in game.players if p.id != self.player.id]
            if len(others) == 1:
                await submit_play(interaction, game, self.player.id, card, target=others[0].id)
            else:
                await interaction.response.edit_message(
                    content="اختر اللاعب الذي تريد منعه من الدور القادم:",
                    view=TargetView(game, self.player.id, card),
                    attachments=[],
                )
            return
        await submit_play(interaction, game, self.player.id, card)


class TableView(BaseView):
    def __init__(self, game):
        super().__init__(timeout=None)
        self.game = game
        self.draw_btn.disabled = game.drawn
        self.pass_btn.disabled = not game.drawn

    @discord.ui.button(label="أوراقي", style=discord.ButtonStyle.primary)
    async def cards_btn(self, interaction, button):
        await self.game.show_hand(interaction)

    @discord.ui.button(label="سحب", style=discord.ButtonStyle.secondary)
    async def draw_btn(self, interaction, button):
        await interaction.response.defer()
        err = await self.game.draw_action(interaction.user.id)
        if err:
            await interaction.followup.send(err, ephemeral=True)

    @discord.ui.button(label="تمرير", style=discord.ButtonStyle.secondary)
    async def pass_btn(self, interaction, button):
        await interaction.response.defer()
        err = await self.game.pass_action(interaction.user.id)
        if err:
            await interaction.followup.send(err, ephemeral=True)

    @discord.ui.button(label="أونو", style=discord.ButtonStyle.danger)
    async def uno_btn(self, interaction, button):
        await interaction.response.defer()
        err = await self.game.uno_action(interaction.user.id)
        if err:
            await interaction.followup.send(err, ephemeral=True)


class LobbyView(BaseView):
    def __init__(self, game):
        super().__init__(timeout=LOBBY_SECONDS)
        self.game = game
        self.message = None

    async def _show(self, interaction, state="open", view="keep"):
        png = await self.game.lobby_png(state)
        file = discord.File(io.BytesIO(png), filename="lobby.png")
        await interaction.edit_original_response(attachments=[file], view=self if view == "keep" else None)

    @discord.ui.button(label="انضمام", style=discord.ButtonStyle.success)
    async def join_btn(self, interaction, button):
        g = self.game
        if g.started:
            await interaction.response.send_message("اللعبة بدأت بالفعل.", ephemeral=True)
            return
        if g.find(interaction.user.id):
            await interaction.response.send_message("أنت منضم بالفعل.", ephemeral=True)
            return
        if len(g.players) >= MAX_PLAYERS:
            await interaction.response.send_message("اللعبة مكتملة.", ephemeral=True)
            return
        await interaction.response.defer()
        player = Player(interaction.user)
        g.players.append(player)
        player.avatar, player.avatar_src = await load_avatar(interaction.user, len(g.players) - 1)
        await self._show(interaction)

    @discord.ui.button(label="خروج", style=discord.ButtonStyle.secondary)
    async def leave_btn(self, interaction, button):
        g = self.game
        p = g.find(interaction.user.id)
        if g.started or p is None:
            await interaction.response.send_message("لا يمكنك الخروج الآن.", ephemeral=True)
            return
        if interaction.user.id == g.host.id:
            await interaction.response.send_message("المضيف يستطيع إلغاء اللعبة بزر الإلغاء.", ephemeral=True)
            return
        await interaction.response.defer()
        g.players.remove(p)
        await self._show(interaction)

    @discord.ui.button(label="ابدأ", style=discord.ButtonStyle.primary)
    async def start_btn(self, interaction, button):
        g = self.game
        if interaction.user.id != g.host.id:
            await interaction.response.send_message("المضيف فقط يستطيع بدء اللعبة.", ephemeral=True)
            return
        if g.started:
            await interaction.response.send_message("اللعبة بدأت بالفعل.", ephemeral=True)
            return
        if len(g.players) < MIN_PLAYERS:
            await interaction.response.send_message(f"يلزم {MIN_PLAYERS} لاعبين على الأقل.", ephemeral=True)
            return
        g.started = True
        self.stop()
        await interaction.response.defer()
        await self._show(interaction, "starting", None)
        await g.begin()

    @discord.ui.button(label="إلغاء", style=discord.ButtonStyle.danger)
    async def cancel_btn(self, interaction, button):
        g = self.game
        if not can_stop(interaction.user):
            await interaction.response.send_message("لا تملك صلاحية إلغاء اللعبة.", ephemeral=True)
            return
        if g.started:
            await interaction.response.send_message("اللعبة بدأت بالفعل.", ephemeral=True)
            return
        if games.get(g.channel.id) is g:
            games.pop(g.channel.id, None)
        g.ended = True
        self.stop()
        await interaction.response.defer()
        await interaction.edit_original_response(
            content="تم إلغاء اللعبة.", view=None, allowed_mentions=discord.AllowedMentions.none()
        )

    async def on_timeout(self):
        g = self.game
        if g.started or g.ended:
            return
        if games.get(g.channel.id) is g:
            games.pop(g.channel.id, None)
        g.ended = True
        if self.message:
            try:
                await self.message.edit(
                    content="انتهت مهلة غرفة الانتظار.", view=None, allowed_mentions=discord.AllowedMentions.none()
                )
            except discord.HTTPException:
                pass


# ======================================================================
# Bot and commands
# ======================================================================
def rules_embed():
    e = discord.Embed(title="قوانين أونو", color=discord.Color.from_rgb(*RGB["red"]))
    e.description = (
        "الهدف: التخلص من جميع أوراقك أولا.\n"
        "العب ورقة تطابق لون أو رقم أو رمز الورقة الحالية.\n"
        "إذا لم تملك ورقة مناسبة اضغط سحب، فإن كانت الورقة المسحوبة مناسبة تلعبها أو تمرر."
    )
    e.add_field(name="منع", value="تختار لاعبا، ويتم تخطي دوره القادم.", inline=False)
    e.add_field(name="عكس", value="ينعكس اتجاه اللعب. مع لاعبين اثنين تلعب مرة أخرى.", inline=False)
    e.add_field(name="سحب 2", value="اللاعب التالي يسحب ورقتين ويخسر دوره.", inline=False)
    e.add_field(name="تغيير اللون", value="تختار اللون الجديد.", inline=False)
    e.add_field(name="تغيير اللون وسحب 4", value="تختار اللون، واللاعب التالي يسحب 4 ويخسر دوره. لا تلعبها إلا إذا لم يكن معك لون الورقة الحالية.", inline=False)
    e.add_field(name="أونو", value="عند بقاء ورقة واحدة اضغط زر أونو، وإلا يستطيع أي لاعب كشفك فتسحب ورقتين.", inline=False)
    e.add_field(name="الأوامر", value="اونو : بدء لعبة\nانسحاب : الخروج من اللعبة\nقوانين اونو : عرض القوانين", inline=False)
    return e


_bg_tasks = set()


async def _delete_later(message, delay):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except discord.NotFound:
        pass
    except discord.Forbidden:
        log.warning("Missing Manage Messages permission in channel %s", message.channel.id)
    except discord.HTTPException:
        pass


def schedule_delete(message, delay=CLEAN_DELAY):
    task = asyncio.create_task(_delete_later(message, delay))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def say(message, text=None, **kwargs):
    """Reply to a command. In the Uno room the command message is deleted, so the bot
    sends a normal message with a mention instead of a reply."""
    if message.channel.id in UNO_CHANNEL_IDS:
        content = f"{message.author.mention} {text}" if text else None
        return await message.channel.send(content=content, **kwargs)
    return await message.reply(text, **kwargs)


async def start_game(message):
    channel = message.channel
    if channel.id in games:
        await say(message, "توجد لعبة أونو جارية في هذا الروم بالفعل.", delete_after=10)
        return
    game = UnoGame(channel, message.author)
    games[channel.id] = game
    try:
        host = Player(message.author)
        host.avatar, host.avatar_src = await load_avatar(message.author, 0)
        game.players.append(host)
        png = await game.lobby_png()
        view = LobbyView(game)
        game.lobby_view = view
        view.message = await channel.send(
            content="@here", file=discord.File(io.BytesIO(png), filename="lobby.png"), view=view
        )
    except Exception:
        games.pop(channel.id, None)
        log.exception("could not open the lobby")
        await channel.send("تعذر فتح غرفة الانتظار، حاول مرة أخرى.", delete_after=10)


async def stop_game(message):
    channel = message.channel
    game = games.get(channel.id)
    if game is None:
        await say(message, "لا توجد لعبة جارية في هذا الروم.", delete_after=10)
        return
    if not can_stop(message.author):
        await say(message, "لا تملك صلاحية إيقاف اللعبة.", delete_after=10)
        return
    if game.started:
        await game.force_end(message.author)
        return
    game.ended = True
    games.pop(channel.id, None)
    if game.lobby_view:
        game.lobby_view.stop()
        if game.lobby_view.message:
            try:
                await game.lobby_view.message.edit(
                    content="تم إلغاء اللعبة.", view=None, allowed_mentions=discord.AllowedMentions.none()
                )
            except discord.HTTPException:
                pass
    await channel.send(f"{message.author.mention} ألغى اللعبة.")


async def leave_game(message):
    game = games.get(message.channel.id)
    if game is None or not game.started:
        await say(message, "لا توجد لعبة جارية في هذا الروم.", delete_after=10)
        return
    err = await game.leave(message.author.id)
    if err:
        await say(message, err, delete_after=10)


class UnoBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=True),
        )

    async def setup_hook(self):
        await asyncio.to_thread(prerender)
        await stats.load(self)
        # السيرفر الصغير (Render) يشغّله main.py الرئيسي مرة واحدة لكل البوتات

    async def on_ready(self):
        log.info("Logged in as %s", self.user)

    async def on_message(self, message):
        if message.author.bot or message.guild is None:
            return
        text = norm(message.content)
        is_command = text in ALL_WORDS
        in_uno_room = message.channel.id in UNO_CHANNEL_IDS
        if in_uno_room and not is_command:
            schedule_delete(message)
        if not is_command:
            return
        try:
            if text in START_WORDS:
                await start_game(message)
            elif text in STOP_WORDS:
                await stop_game(message)
            elif text in LEAVE_WORDS:
                await leave_game(message)
            elif text in RULES_WORDS:
                await say(message, None, embed=rules_embed(), delete_after=120 if in_uno_room else None)
        except Exception:
            log.exception("message handler error")


# ======================================================================
# Web server (keeps Render alive) and run
# ======================================================================
async def _health(request):
    return web.Response(text="ok")


async def start_web():
    app = web.Application()
    app.router.add_get("/", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", "10000")))
    await site.start()


# ======================================================================
# التشغيل: يتم من ملف main.py الرئيسي (التوكن من متغير UNO_TOKEN)
# ======================================================================
TOKEN_ENV = "UNO_TOKEN"
BOT_NAME = "Uno"


def create_bot():
    return UnoBot()
