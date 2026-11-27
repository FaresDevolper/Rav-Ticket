"""
نقطة تشغيل بوتات حماية القذف الصوتية:  python -m voiceguard

تُشغَّل من main.py الرئيسي في عملية منفصلة ولا ترى إلا متغيرات GUARD_* (توكنات بقية البوتات غير متاحة لها).

المتغيرات (لكل نسخة رقم n = 1..5):
  GUARD_TOKEN_n      توكن البوت رقم n            (إجباري)
  GUARD_ROOM_n       آيدي الروم الصوتي للنسخة n   (إجباري)
مشتركة بين كل النسخ (أو تحط نسخة خاصة بلاحقة _n):
  GUARD_GUILD_ID, GUARD_LOG_CHANNEL_ID, GUARD_STT_API_KEY   (إجباري)
  GUARD_STT_API_URL, GUARD_STT_MODEL, GUARD_TIMEOUT_MINUTES, GUARD_TIMEZONE,
  GUARD_EXEMPT_ROLE_IDS, GUARD_LOG_PING_ROLE_ID, GUARD_ANNOUNCE_NOTICE, GUARD_MIN_LOGPROB,
  GUARD_STATUS_TEXT (نص "يشاهد" تحت اسم البوت، الافتراضي: by Fares)
  GUARD_STT_CONCURRENCY (كم طلب تحويل بنفس الوقت لكل النسخ، الافتراضي 3)

أكواد الخروج: 0 = انتهى بشكل طبيعي | 2 = خطأ دائم (توكن غلط / مكتبات ناقصة) لا يعاد تشغيله | 3 = ما فيه نسخ مضبوطة | غيرها = يعاد تشغيله
"""
import asyncio
import logging
import os
import re
import sys

EXIT_OK, EXIT_FATAL, EXIT_NO_CONFIG = 0, 2, 3
log = logging.getLogger("voiceguard")


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def load_configs(env=None):
    """يقرأ نسخ البوت من المتغيرات. النسخة الناقصة تنتخطى مع رسالة واضحة."""
    from .guard import GuardConfig

    env = os.environ if env is None else env
    indexes = sorted({int(m.group(1)) for k in env for m in [re.fullmatch(r"GUARD_TOKEN_(\d+)", k)] if m})
    cfgs = []

    def pick(base, n, default=None):
        return (env.get(f"{base}_{n}") or env.get(base) or default)

    for n in indexes:
        token = (env.get(f"GUARD_TOKEN_{n}") or "").strip()
        if not token:
            continue
        missing = []
        room = _int(env.get(f"GUARD_ROOM_{n}"))
        guild = _int(pick("GUARD_GUILD_ID", n))
        logch = _int(pick("GUARD_LOG_CHANNEL_ID", n))
        stt = (pick("GUARD_STT_API_KEY", n) or "").strip()
        if room is None:
            missing.append(f"GUARD_ROOM_{n}")
        if guild is None:
            missing.append("GUARD_GUILD_ID")
        if logch is None:
            missing.append("GUARD_LOG_CHANNEL_ID")
        if not stt:
            missing.append("GUARD_STT_API_KEY")
        if missing:
            log.error("[Guard%s] ناقص: %s — تم تخطي هذه النسخة", n, ", ".join(missing))
            continue

        try:
            timeout_min = min(max(int(float(pick("GUARD_TIMEOUT_MINUTES", n, "10"))), 1), 40320)
        except ValueError:
            timeout_min = 10
        try:
            min_logprob = float(pick("GUARD_MIN_LOGPROB", n, "-1.0"))
        except ValueError:
            min_logprob = -1.0
        roles = [x for x in (_int(r) for r in (pick("GUARD_EXEMPT_ROLE_IDS", n, "") or "").split(",")) if x]

        cfgs.append(GuardConfig(
            name=f"Guard{n}",
            token=token,
            guild_id=guild,
            log_channel_id=logch,
            watch_channel_id=room,
            stt_key=stt,
            stt_url=pick("GUARD_STT_API_URL", n, "https://api.groq.com/openai/v1/audio/transcriptions"),
            stt_model=pick("GUARD_STT_MODEL", n, "whisper-large-v3"),
            timeout_min=timeout_min,
            tz=pick("GUARD_TIMEZONE", n, "Asia/Riyadh"),
            exempt_roles=roles,
            ping_role_id=_int(pick("GUARD_LOG_PING_ROLE_ID", n)),
            announce=(pick("GUARD_ANNOUNCE_NOTICE", n, "true") or "true").lower() != "false",
            min_logprob=min_logprob,
            status_text=(pick("GUARD_STATUS_TEXT", n, "by Fares") or "by Fares").strip()[:120],
        ))
    return cfgs


def ensure_opus():
    """يحمّل libopus: من النظام، وإذا ما لقاها يدوّر عليها داخل مكتبات بايثون المثبتة (مثل av.libs)."""
    import ctypes.util
    import glob

    import discord.opus as opus

    if opus.is_loaded():
        return True
    names = [ctypes.util.find_library("opus"), "libopus.so.0", "libopus.so", "libopus.dylib"]
    for n in names:
        if not n:
            continue
        try:
            opus.load_opus(n)
            if opus.is_loaded():
                log.info("libopus محمّلة: %s", n)
                return True
        except Exception:
            pass
    seen = set()
    for base in list(sys.path):
        for pattern in ("*.libs/libopus*.so*", "*/libopus*.so*", "libopus*.so*"):
            for path in glob.glob(os.path.join(base or ".", pattern)):
                if path in seen:
                    continue
                seen.add(path)
                try:
                    opus.load_opus(path)
                    if opus.is_loaded():
                        log.info("libopus محمّلة من: %s", path)
                        return True
                except Exception:
                    pass
    return False


def diagnostics():
    """يطبع حالة المكتبات في اللوق. يرجع False إذا فيه شي أساسي ناقص (ما فيه فايدة من التشغيل)."""
    ok = True
    log.info("Python %s", sys.version.split()[0])
    try:
        import discord
        log.info("discord.py %s", discord.__version__)
    except Exception as e:
        log.error("discord.py غير مثبتة: %s", e)
        return False
    try:
        from discord.ext import voice_recv  # noqa: F401
        log.info("discord-ext-voice-recv: موجودة")
    except Exception as e:
        log.error("discord-ext-voice-recv غير مثبتة (%s). تأكد إن Build Command يثبّت requirements-voice.txt", e)
        ok = False
    try:
        import nacl  # noqa: F401
    except Exception:
        log.error("PyNaCl غير مثبتة")
        ok = False
    try:
        import davey  # type: ignore
        log.info("davey: %s", getattr(davey, "__version__", "موجودة"))
    except Exception:
        log.warning("davey غير مثبتة: التشفير DAVE ما راح يشتغل")
    try:
        import voice_recv_dave  # type: ignore
        voice_recv_dave.install()
        log.info("voice-recv-dave: مثبّتة ومفعّلة")
    except Exception as e:
        log.warning("voice-recv-dave غير متوفرة (%s): إذا الروم مشفّر DAVE ما راح يسمع", e)
    if ensure_opus():
        log.info("libopus: محمّلة")
    else:
        log.error("libopus غير موجودة: ما يقدر يفك الصوت. (تأكد من تثبيت av من requirements-voice.txt)")
        ok = False
    return ok


async def run_all(cfgs):
    import aiohttp
    import discord

    from .guard import GuardBot

    try:
        conc = max(1, int(os.environ.get("GUARD_STT_CONCURRENCY", "3")))
    except ValueError:
        conc = 3
    sem = asyncio.Semaphore(conc)
    login_failed = 0

    async with aiohttp.ClientSession() as session:
        async def run_one(cfg):
            nonlocal login_failed
            bot = GuardBot(cfg, session, sem)
            try:
                async with bot.client:
                    await bot.client.start(cfg.token)
            except discord.LoginFailure:
                login_failed += 1
                log.error("[%s] التوكن غير صحيح، تم تخطي هذه النسخة", cfg.name)
            except asyncio.CancelledError:
                raise
            except BaseException:
                log.exception("[%s] توقفت النسخة بسبب خطأ", cfg.name)
            else:
                log.warning("[%s] توقفت النسخة", cfg.name)

        await asyncio.gather(*(run_one(c) for c in cfgs))
    return EXIT_FATAL if login_failed == len(cfgs) else 1


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("discord.http").setLevel(logging.WARNING)

    cfgs = load_configs()
    if not cfgs:
        log.error("ما فيه أي نسخة مضبوطة (محتاج GUARD_TOKEN_1 و GUARD_ROOM_1 و GUARD_GUILD_ID و GUARD_LOG_CHANNEL_ID و GUARD_STT_API_KEY)")
        return EXIT_NO_CONFIG
    log.info("النسخ المضبوطة: %s", ", ".join(c.name for c in cfgs))
    if not diagnostics():
        return EXIT_FATAL
    try:
        return asyncio.run(run_all(cfgs))
    except KeyboardInterrupt:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
