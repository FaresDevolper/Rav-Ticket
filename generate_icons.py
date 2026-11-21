"""
يولّد أيقونات اللوحة (20 أيقونة) بلون موحّد داخل مجلد icons/
للتشغيل:  python generate_icons.py
غيّر ICON_COLOR لو تبي لون ثاني، وبعدها ارفع ICON_VERSION في main.py
"""
import math
import os

from PIL import Image, ImageDraw

ICON_COLOR = "#333153"   # لون الأيقونات
OUT_DIR = "icons"
SIZE = 128               # حجم الصورة النهائي
SS = 4                   # تكبير داخلي للنعومة
S = SIZE * SS / 100.0    # تحويل إحداثيات 0-100 إلى بكسل

FILL = tuple(int(ICON_COLOR.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)) + (255,)
CLEAR = (0, 0, 0, 0)


def new():
    img = Image.new("RGBA", (SIZE * SS, SIZE * SS), CLEAR)
    return img, ImageDraw.Draw(img)


def P(pts):
    return [(x * S, y * S) for x, y in pts]


def box(b):
    return [b[0] * S, b[1] * S, b[2] * S, b[3] * S]


def line(d, pts, w, color=FILL):
    d.line(P(pts), fill=color, width=int(w * S), joint="curve")
    r = w * S / 2
    for x, y in (pts[0], pts[-1]):
        d.ellipse([x * S - r, y * S - r, x * S + r, y * S + r], fill=color)


def rrect(d, b, r, color=FILL):
    d.rounded_rectangle(box(b), radius=r * S, fill=color)


def circle(d, cx, cy, r, color=FILL):
    d.ellipse(box((cx - r, cy - r, cx + r, cy + r)), fill=color)


def ring(d, cx, cy, r, w, color=FILL):
    d.ellipse(box((cx - r, cy - r, cx + r, cy + r)), outline=color, width=int(w * S))


def person(d, cx, top=14, scale=1.0):
    hr = 17 * scale
    circle(d, cx, top + hr, hr)
    bw = 26 * scale
    by = top + hr * 2 + 6 * scale
    d.pieslice(box((cx - bw, by, cx + bw, by + bw * 2)), 180, 360, fill=FILL)


def badge(d, kind, cx=74, cy=74, r=22):
    circle(d, cx, cy, r + 6, CLEAR)
    circle(d, cx, cy, r)
    if kind == "minus":
        line(d, [(cx - 11, cy), (cx + 11, cy)], 6, CLEAR)
    elif kind == "plus":
        line(d, [(cx - 11, cy), (cx + 11, cy)], 6, CLEAR)
        line(d, [(cx, cy - 11), (cx, cy + 11)], 6, CLEAR)
    elif kind == "x":
        line(d, [(cx - 9, cy - 9), (cx + 9, cy + 9)], 6, CLEAR)
        line(d, [(cx + 9, cy - 9), (cx - 9, cy + 9)], 6, CLEAR)
    elif kind == "check":
        line(d, [(cx - 10, cy), (cx - 3, cy + 8), (cx + 11, cy - 8)], 6, CLEAR)


# ----------------------------------------------------------------- الأيقونات
def lock(opened=False):
    img, d = new()
    if opened:
        d.arc(box((28, 6, 72, 52)), 180, 360, fill=FILL, width=int(9 * S))
        line(d, [(32.5, 28), (32.5, 50)], 9)
    else:
        d.arc(box((28, 8, 72, 52)), 180, 360, fill=FILL, width=int(9 * S))
        line(d, [(32.5, 30), (32.5, 50)], 9)
        line(d, [(67.5, 30), (67.5, 50)], 9)
    rrect(d, (16, 44, 84, 92), 9)
    circle(d, 50, 64, 7, CLEAR)
    line(d, [(50, 66), (50, 78)], 6, CLEAR)
    return img


def eye(hidden=False):
    img, d = new()
    top, bot = [], []
    for i in range(0, 41):
        t = -1 + i / 20
        x = 50 + 44 * t
        h = 30 * (1 - t * t) ** 0.8
        top.append((x, 50 - h))
        bot.append((x, 50 + h))
    d.polygon(P(top + bot[::-1]), fill=FILL)
    circle(d, 50, 50, 17, CLEAR)
    circle(d, 50, 50, 9)
    if hidden:
        line(d, [(18, 84), (82, 16)], 18, CLEAR)
        line(d, [(18, 84), (82, 16)], 8)
    return img


def status():
    img, d = new()
    rrect(d, (8, 12, 92, 72), 14)
    d.polygon(P([(24, 68), (24, 92), (50, 68)]), fill=FILL)
    line(d, [(26, 32), (74, 32)], 6, CLEAR)
    line(d, [(26, 50), (60, 50)], 6, CLEAR)
    return img


def globe():
    img, d = new()
    ring(d, 50, 50, 40, 8)
    d.ellipse(box((30, 10, 70, 90)), outline=FILL, width=int(6 * S))
    line(d, [(10, 50), (90, 50)], 6)
    line(d, [(18, 30), (82, 30)], 5)
    line(d, [(18, 70), (82, 70)], 5)
    return img


def group():
    img, d = new()
    person(d, 66, top=10, scale=0.85)
    circle(d, 32, 40, 0, CLEAR)
    person(d, 34, top=26, scale=1.0)
    return img


def pencil():
    img, d = new()
    u = (math.cos(math.radians(45)), -math.sin(math.radians(45)))
    v = (math.cos(math.radians(45)), math.sin(math.radians(45)))
    tx, ty = 16, 76

    def w(x, y):
        return (tx + x * u[0] + y * v[0], ty + x * u[1] + y * v[1])

    d.polygon(P([w(0, 0), w(20, -12), w(84, -12), w(84, 12), w(20, 12)]), fill=FILL)
    a, b = w(68, -13), w(68, 13)
    line(d, [a, b], 3.5, CLEAR)
    rrect(d, (14, 87, 86, 94), 3.5)
    return img


def person_badge(kind):
    img, d = new()
    person(d, 40, top=10, scale=1.0)
    badge(d, kind)
    return img


def envelope():
    img, d = new()
    rrect(d, (6, 20, 94, 80), 9)
    line(d, [(14, 30), (50, 56), (86, 30)], 6, CLEAR)
    return img


def shield(kind):
    img, d = new()
    d.polygon(P([(50, 6), (88, 20), (88, 48), (80, 68), (50, 94), (20, 68), (12, 48), (12, 20)]), fill=FILL)
    if kind == "check":
        line(d, [(32, 50), (45, 63), (69, 36)], 8, CLEAR)
    else:
        line(d, [(32, 50), (68, 50)], 8, CLEAR)
    return img


def clipboard(kind):
    img, d = new()
    rrect(d, (16, 14, 84, 94), 9)
    rrect(d, (34, 5, 66, 24), 5)
    rrect(d, (40, 11, 60, 18), 3, CLEAR)
    line(d, [(30, 40), (70, 40)], 5, CLEAR)
    line(d, [(30, 54), (58, 54)], 5, CLEAR)
    badge(d, kind, cx=70, cy=74, r=20)
    return img


def tag(kind):
    img, d = new()
    d.polygon(P([(8, 10), (50, 10), (92, 52), (52, 92), (8, 48)]), fill=FILL)
    circle(d, 28, 30, 7, CLEAR)
    badge(d, kind, cx=72, cy=72, r=22)
    return img


def roles_list():
    img, d = new()
    for y in (24, 50, 76):
        circle(d, 18, y, 7)
        rrect(d, (34, y - 6, 90, y + 6), 5)
    return img


def crown():
    img, d = new()
    d.polygon(P([(8, 78), (8, 28), (30, 52), (50, 16), (70, 52), (92, 28), (92, 78)]), fill=FILL)
    rrect(d, (8, 82, 92, 92), 4)
    for cx, cy in ((8, 28), (50, 16), (92, 28)):
        circle(d, cx, cy, 6)
    return img


ICONS = {
    "lock": lambda: lock(False),
    "unlock": lambda: lock(True),
    "hide": lambda: eye(True),
    "show": lambda: eye(False),
    "rename": pencil,
    "limit": group,
    "region": globe,
    "status": status,
    "kick": lambda: person_badge("minus"),
    "ban": lambda: person_badge("x"),
    "unban": lambda: person_badge("check"),
    "invite": envelope,
    "trust": lambda: shield("check"),
    "untrust": lambda: shield("minus"),
    "trusted": lambda: clipboard("check"),
    "banned": lambda: clipboard("x"),
    "allow_role": lambda: tag("plus"),
    "remove_role": lambda: tag("minus"),
    "roles": roles_list,
    "owner": crown,
}

if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    for key, fn in ICONS.items():
        im = fn().resize((SIZE, SIZE), Image.LANCZOS)
        im.save(os.path.join(OUT_DIR, f"{key}.png"))
    print(f"تم إنشاء {len(ICONS)} أيقونة داخل {OUT_DIR}/")
