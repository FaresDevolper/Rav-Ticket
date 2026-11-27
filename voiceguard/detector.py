"""كشف الكلمات القوية في النص (تحويل مطابق لملف detector.js الأصلي)."""
import re
import unicodedata

from .words import STRONG

_TASHKEEL = re.compile("[\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")
_WS = re.compile(r"\s+")
_REPLACE = [
    (re.compile("[أإآٱ]"), "ا"),
    (re.compile("ى"), "ي"),
    (re.compile("ئ"), "ي"),
    (re.compile("ؤ"), "و"),
    (re.compile("ة"), "ه"),
    (re.compile("گ"), "ك"),
    (re.compile("ک"), "ك"),
    (re.compile("ی"), "ي"),
    (re.compile("چ"), "ج"),
    (re.compile("پ"), "ب"),
    (re.compile("ڤ"), "ف"),
]


def _cat(ch):
    return unicodedata.category(ch)


def normalize(text):
    """توحيد النص العربي: يشيل التشكيل والتطويل ويوحد الحروف المتشابهة."""
    s = unicodedata.normalize("NFKC", str(text or "")).lower()
    s = _TASHKEEL.sub("", s)
    for rx, rep in _REPLACE:
        s = rx.sub(rep, s)
    # أي شي مو حرف ولا رقم ولا مسافة -> مسافة
    s = "".join(
        ch if (_cat(ch)[0] in ("L", "N") or ch.isspace()) else " " for ch in s
    )
    return _WS.sub(" ", s).strip()


_REPEAT = re.compile(r"(.)\1+", re.DOTALL)


def collapse(s):
    """يدمج الحروف المكررة (شرمووووطة = شرموطه)."""
    return _REPEAT.sub(r"\1", s)


def _split_tokens(text):
    out, cur = [], []
    for ch in str(text or ""):
        if _cat(ch)[0] in ("L", "M", "N"):
            cur.append(ch)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


def tokenize(text):
    toks = []
    for orig in _split_tokens(text):
        norm = collapse(normalize(orig))
        if norm:
            toks.append({"orig": orig, "norm": norm})
    return toks


PREFIXES = ["و", "ف", "ب", "ل", "ك", "ال", "وال", "بال", "لل", "فال"]


def variants(norm):
    out = [norm]
    for p in PREFIXES:
        if norm.startswith(p) and len(norm) - len(p) >= 3:
            out.append(norm[len(p):])
    return out


_seen = set()
_ENTRIES = []
for _w in STRONG:
    _n = collapse(normalize(_w))
    if _n and _n not in _seen:
        _seen.add(_n)
        _ENTRIES.append({"norm": _n, "words": _n.split(" ")})

SINGLES = {e["norm"] for e in _ENTRIES if len(e["words"]) == 1}
PHRASES = [e for e in _ENTRIES if len(e["words"]) > 1]


def find_bad_words(text):
    """يرجع قائمة المخالفات: [{'matched': ..., 'said': ...}]"""
    tokens = tokenize(text)
    hits = []
    used = set()

    # العبارات أولاً (أطول = أدق)
    for phrase in PHRASES:
        n = len(phrase["words"])
        for i in range(len(tokens) - n + 1):
            ok = True
            for j in range(n):
                tok_variants = variants(tokens[i + j]["norm"]) if j == 0 else [tokens[i + j]["norm"]]
                if phrase["words"][j] not in tok_variants:
                    ok = False
                    break
            if ok:
                hits.append({
                    "matched": phrase["norm"],
                    "said": " ".join(t["orig"] for t in tokens[i:i + n]),
                })
                for j in range(n):
                    used.add(i + j)

    # الكلمات المفردة
    for i, t in enumerate(tokens):
        if i in used:
            continue
        for v in variants(t["norm"]):
            if v in SINGLES:
                hits.append({"matched": v, "said": t["orig"]})
                break

    return hits
