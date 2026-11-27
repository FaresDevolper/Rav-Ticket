"""
بوت حماية القذف في الرومات الصوتية (نسخة بايثون)
يسمع كل شخص لحاله -> يحول الصوت لنص (Whisper عبر Groq) -> يفحص الكلمات القوية -> تايم اوت + لوق للمشرفين

كل نسخة (GuardBot) = بوت ديسكورد مستقل بتوكنه وروم صوتي واحد.
البوت يرجع للروم تلقائياً إذا طلع / انفصل / انسحب لروم ثاني، وينفك عنه الميوت والدفن على طول.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import voice_recv

from .audio import to_wav_16k_mono
from .detector import find_bad_words, normalize

try:  # إصلاح DAVE لاستقبال الصوت (اختياري، يتثبت من requirements-voice.txt)
    import voice_recv_dave  # type: ignore
except Exception:  # pragma: no cover
    voice_recv_dave = None

# ============================ ضبط الصوت ============================
SILENCE_S = 0.9            # يعتبر الجملة انتهت بعد هذي الفترة من الصمت
MAX_CLIP_S = 12.0          # أقصى طول لمقطع واحد قبل ما يتقطع ويكمل
MIN_CLIP_SEC = 0.6         # أقصر مقطع يستاهل التحويل
MIN_RMS = 120              # أقل مستوى صوت (تحت هذا = ضجيج/صمت)
MAX_PCM_BYTES = 48000 * 4 * 14

# دقة التعرف
MAX_NO_SPEECH = 0.5
MAX_COMPRESSION = 2.4

# الطابور
CONCURRENCY = 2
MAX_QUEUE = 20
COOLDOWN_S = 60.0

# الحماية من الطلوع
RETURN_DELAY_S = 0.3
WATCHDOG_S = 10.0
HEALTH_LOG_EVERY = 90      # كل كم دورة watchdog نطبع حالة مختصرة (90 × 10 ثواني = 15 دقيقة)

# جمل يهلوس بها Whisper لما يكون في صمت/موسيقى
_HALLUCINATIONS = [normalize(x) for x in (
    "اشتركوا في القناه", "اشترك في القناه", "لا تنسوا الاشتراك", "ترجمه نانسي قنقر",
    "شكرا للمشاهده", "شكرا لكم على المشاهده", "تمت الترجمه", "ترجمه", "موسيقي",
)]


def is_hallucination(nt: str) -> bool:
    return len(nt) < 60 and any(h in nt for h in _HALLUCINATIONS)


def clip(s, n: int) -> str:
    s = str(s)
    return s[: n - 1] + "…" if len(s) > n else s


def _num(v, default):
    return default if v is None else v


# ============================ الإعدادات ============================
@dataclass
class GuardConfig:
    name: str
    token: str
    guild_id: int
    log_channel_id: int
    watch_channel_id: int
    stt_key: str
    stt_url: str = "https://api.groq.com/openai/v1/audio/transcriptions"
    stt_model: str = "whisper-large-v3"
    timeout_min: int = 10
    tz: str = "Asia/Riyadh"
    exempt_roles: list = field(default_factory=list)
    ping_role_id: Optional[int] = None
    announce: bool = True
    min_logprob: float = -1.0


@dataclass
class Stats:
    clips: int = 0
    transcribed: int = 0
    punished: int = 0
    suspects: int = 0
    dropped: int = 0
    errors: int = 0


# ============================ تجميع مقاطع الصوت ============================
class ClipBuffer:
    """يجمّع صوت كل شخص، ويطلع المقطع لما يسكت 0.9 ثانية أو يوصل 12 ثانية (مثل AfterSilence في النسخة الأصلية)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._bufs: dict = {}

    def feed(self, uid: int, pcm: bytes, now: Optional[float] = None):
        now = time.monotonic() if now is None else now
        with self._lock:
            b = self._bufs.get(uid)
            if b is None:
                b = self._bufs[uid] = [bytearray(), now, now]
            if len(b[0]) < MAX_PCM_BYTES:
                b[0] += pcm
            b[2] = now

    def collect(self, now: Optional[float] = None):
        now = time.monotonic() if now is None else now
        ready = []
        with self._lock:
            for uid, b in list(self._bufs.items()):
                if now - b[2] >= SILENCE_S or now - b[1] >= MAX_CLIP_S:
                    ready.append((uid, bytes(b[0])))
                    del self._bufs[uid]
        return ready

    def clear(self):
        with self._lock:
            self._bufs.clear()


class ClipSink(voice_recv.AudioSink):
    """يستقبل PCM جاهز من voice_recv (من thread خاص) ويحطه في ClipBuffer."""

    def __init__(self, buf: ClipBuffer, skip_fn, log: logging.Logger):
        super().__init__()
        self.buf = buf
        self.skip_fn = skip_fn
        self.log = log
        self.packets = 0
        self.unknown = 0          # حزم ما قدرنا نعرف صاحبها
        self.last_packet_at = 0.0
        self.vc = None            # يتحدد بعد الاتصال (لقراءة SSRC -> المستخدم)

    def wants_opus(self) -> bool:
        return False

    def _uid_from_ssrc(self, data):
        try:
            ssrc = getattr(getattr(data, "packet", None), "ssrc", None)
            mapping = getattr(self.vc, "_ssrc_to_id", None)
            if mapping is not None and ssrc is not None:
                return mapping.get(ssrc)
        except Exception:
            pass
        return None

    def write(self, user, data):
        try:
            self.packets += 1
            self.last_packet_at = time.monotonic()
            if self.packets == 1:
                self.log.info("وصل أول صوت من الروم (الاستقبال شغال)")

            if user is None:
                user = getattr(data, "source", None)
            uid = getattr(user, "id", None) if user is not None else None
            if uid is None:
                uid = self._uid_from_ssrc(data)
            if uid is None:
                self.unknown += 1
                return
            if user is not None:
                if getattr(user, "bot", False) or self.skip_fn(user):
                    return
            pcm = getattr(data, "pcm", None)
            if pcm:
                self.buf.feed(uid, pcm)
        except Exception:
            self.log.exception("sink.write error")

    def cleanup(self):
        pass


# ============================ تحليل نتيجة التحويل ============================
def classify_result(result: dict, min_logprob: float):
    """يرجع (strong, weak): مخالفة مؤكدة، أو مشتبه فيها (الصوت غير واضح)."""
    text = str(result.get("text") or "").strip()
    if not text:
        return None, None
    if is_hallucination(normalize(text)):
        return None, None

    segments = result.get("segments")
    if not (isinstance(segments, list) and segments):
        segments = [{"text": text, "avg_logprob": 0, "no_speech_prob": 0, "compression_ratio": 1}]

    strong = None
    weak = None
    for seg in segments:
        hits = find_bad_words(seg.get("text") or "")
        if not hits:
            continue
        confident = (
            _num(seg.get("avg_logprob"), 0) >= min_logprob
            and _num(seg.get("no_speech_prob"), 0) <= MAX_NO_SPEECH
            and _num(seg.get("compression_ratio"), 1) <= MAX_COMPRESSION
        )
        hit = dict(hits[0], segment=str(seg.get("text") or "").strip())
        if confident:
            strong = hit
            break
        if weak is None:
            weak = hit
    return strong, weak


@dataclass
class Job:
    user_id: int
    wav: bytes
    channel_id: Optional[int]
    at: float
    seconds: float


# ============================ العميل ============================
class _Client(discord.Client):
    def __init__(self, owner: "GuardBot", **kw):
        super().__init__(**kw)
        self.owner = owner

    async def setup_hook(self):
        await self.owner.setup()

    async def on_ready(self):
        await self.owner.ready()

    async def on_voice_state_update(self, member, before, after):
        await self.owner.voice_state_update(member, before, after)

    async def on_interaction(self, interaction):
        await self.owner.interaction(interaction)


class GuardBot:
    def __init__(self, cfg: GuardConfig, session: aiohttp.ClientSession, stt_sem: asyncio.Semaphore):
        self.cfg = cfg
        self.session = session
        self.stt_sem = stt_sem
        self.log = logging.getLogger(f"guard.{cfg.name}")

        try:
            self.tz = ZoneInfo(cfg.tz)
        except Exception:
            self.tz = ZoneInfo("Asia/Riyadh")

        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        self.client = _Client(self, intents=intents)
        self.tree = app_commands.CommandTree(self.client)

        self.stats = Stats()
        self.cooldown: dict = {}
        self.buf = ClipBuffer()
        self.sink = ClipSink(self.buf, self._skip_user, self.log)
        self.queue: asyncio.Queue = asyncio.Queue()
        self.joining = False
        self.started = False
        self.rejoin_handle = None
        self._tasks: set = set()
        self._health_tick = 0
        self._join_fail = 0
        self._skip_cache: dict = {}

    # ---------------------------------------------------- أدوات
    @property
    def guild(self) -> Optional[discord.Guild]:
        return self.client.get_guild(self.cfg.guild_id)

    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    async def get_member(self, uid: int) -> Optional[discord.Member]:
        g = self.guild
        if not g:
            return None
        m = g.get_member(uid)
        if m:
            return m
        try:
            return await g.fetch_member(uid)
        except discord.HTTPException:
            return None

    def is_exempt(self, member) -> bool:
        if not member:
            return False
        try:
            if member.guild_permissions.administrator:
                return True
            return any(r.id in self.cfg.exempt_roles for r in member.roles)
        except Exception:
            return False

    def _skip_user(self, user) -> bool:
        # يُستدعى من thread الصوت: الأدمن والرتب المستثناة ما نحولهم لنص
        return isinstance(user, discord.Member) and self.is_exempt(user)

    def fmt_clock(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, tz=self.tz).strftime("%I:%M:%S %p").lower()

    @staticmethod
    def join_mentions(items) -> str:
        out, n = "", 0
        for m in items:
            if len(out + " " + m) > 900:
                out += f" +{len(items) - n}"
                break
            out += (" " if out else "") + m
            n += 1
        return out or "لا أحد"

    # ---------------------------------------------------- دورة الحياة
    async def setup(self):
        for _ in range(CONCURRENCY):
            self._spawn(self._worker())
        self._spawn(self._flush_loop())

    async def ready(self):
        if self.started:
            return
        self.started = True
        c = self.cfg
        self.log.info("تم تسجيل الدخول: %s", self.client.user)
        try:
            await self.client.change_presence(
                activity=discord.Activity(type=discord.ActivityType.watching, name="حماية القذف الصوتية 🛡️"),
                status=discord.Status.online,
            )
        except Exception:
            self.log.exception("presence failed")

        guild = self.guild
        if not guild:
            self.log.error("ما لقيت السيرفر، تأكد من GUARD_GUILD_ID وإن البوت داخله")
            return

        try:
            self._register_commands()
            await self.tree.sync(guild=discord.Object(id=c.guild_id))
            self.log.info("تم تسجيل الأوامر")
        except Exception:
            self.log.exception("فشل تسجيل الأوامر")

        ch = guild.get_channel(c.watch_channel_id)
        if ch is None:
            try:
                ch = await guild.fetch_channel(c.watch_channel_id)
            except discord.HTTPException:
                ch = None
        if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
            try:
                await self.join_watch(ch, announce=True)
            except Exception as e:
                self.log.error("فشل الدخول التلقائي للروم: %s", e)
                self.schedule_rejoin(5.0)
        else:
            self.log.error("GUARD_ROOM غير صالح (لازم يكون آيدي روم صوتي)")

        self._spawn(self._watchdog())

    # ---------------------------------------------------- الاتصال بالروم الصوتي
    def schedule_rejoin(self, delay: float = 1.0):
        if self.rejoin_handle is not None:
            return

        def fire():
            self.rejoin_handle = None
            self._spawn(self.ensure_watching())

        self.rejoin_handle = asyncio.get_running_loop().call_later(delay, fire)

    async def stop_watching(self):
        self.buf.clear()
        g = self.guild
        vc = g.voice_client if g else None
        if vc is not None:
            try:
                if vc.is_listening():
                    vc.stop_listening()
            except Exception:
                pass
            try:
                await vc.disconnect(force=True)
            except Exception as e:
                self.log.warning("disconnect failed: %s", e)

    async def join_watch(self, channel, announce: bool = False):
        self.joining = True
        try:
            await self.stop_watching()
            try:
                vc = await channel.connect(cls=voice_recv.VoiceRecvClient, self_deaf=False, self_mute=True)
            except TypeError:
                vc = await channel.connect(cls=voice_recv.VoiceRecvClient, self_deaf=False)
                try:
                    await channel.guild.change_voice_state(channel=channel, self_mute=True, self_deaf=False)
                except Exception:
                    pass

            started = False
            if voice_recv_dave is not None:
                try:
                    await voice_recv_dave.listen(vc, self.sink)
                    started = True
                except Exception as e:
                    self.log.warning("voice_recv_dave.listen فشل (%s)، نستخدم listen العادي", e)
            if not started:
                vc.listen(self.sink)

            self.sink.vc = vc
            self._join_fail = 0
            self.log.info("يراقب #%s", channel.name)

            if announce and self.cfg.announce and hasattr(channel, "send"):
                try:
                    await channel.send(
                        "🛡️ هذا الروم تحت **حماية القذف الصوتية**. يتم تحويل الكلام لنص بشكل مؤقت للكشف "
                        "عن القذف والسب فقط، ولا يتم حفظ أي تسجيل."
                    )
                except Exception:
                    pass
        finally:
            self.joining = False

    async def ensure_watching(self):
        if self.joining:
            return
        g = self.guild
        if not g:
            return
        want = self.cfg.watch_channel_id
        vc = g.voice_client
        try:
            right = bool(vc and vc.is_connected() and vc.channel and vc.channel.id == want and vc.is_listening())
        except Exception:
            right = False
        if right:
            return

        ch = g.get_channel(want)
        if ch is None:
            try:
                ch = await g.fetch_channel(want)
            except discord.HTTPException:
                ch = None
        if not isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
            self.log.error("الروم المحدد غير موجود أو مو صوتي، تأكد من GUARD_ROOM")
            return
        if self.joining:
            return

        try:
            await self.join_watch(ch)
            self.log.info("رجعت للروم المحدد")
        except Exception as e:
            self._join_fail += 1
            delay = min(5.0 * self._join_fail, 60.0)
            self.log.error("فشل الرجوع للروم (%s)، محاولة بعد %ss", e, int(delay))
            self.schedule_rejoin(delay)

    async def _watchdog(self):
        while True:
            await asyncio.sleep(WATCHDOG_S)
            try:
                await self.ensure_watching()
                self._health_tick += 1
                if self._health_tick % HEALTH_LOG_EVERY == 0:
                    s = self.stats
                    self.log.info(
                        "حالة: حزم صوت=%s مقاطع=%s محوّلة=%s عقوبات=%s اشتباه=%s أخطاء=%s",
                        self.sink.packets, s.clips, s.transcribed, s.punished, s.suspects, s.errors,
                    )
            except Exception:
                self.log.exception("watchdog error")

    # ---------------------------------------------------- حماية البوت نفسه
    async def voice_state_update(self, member, before, after):
        try:
            if member.guild.id != self.cfg.guild_id or member.id != self.client.user.id:
                return

            edits = {}
            if after.channel is not None and after.mute:   # أحد عطاه ميوت سيرفر -> ينفك
                edits["mute"] = False
            if after.channel is not None and after.deaf:   # دفن سيرفر -> ينفك (الدفن يمنعه يسمع)
                edits["deafen"] = False
            if edits:
                try:
                    await member.edit(reason="حماية: البوت لازم يبقى غير مكتوم/مدفون", **edits)
                except Exception as e:
                    self.log.error("فك الميوت/الدفن فشل (تأكد من صلاحية Mute/Deafen Members): %s", e)

            if after.channel is None or after.channel.id != self.cfg.watch_channel_id:
                self.schedule_rejoin(RETURN_DELAY_S)
        except Exception:
            self.log.exception("voice_state_update error")

    # ---------------------------------------------------- معالجة المقاطع
    async def _flush_loop(self):
        while True:
            await asyncio.sleep(0.15)
            try:
                for uid, pcm in self.buf.collect():
                    self._spawn(self.on_clip(uid, pcm))
            except Exception:
                self.log.exception("flush error")

    async def _skip_uid(self, uid: int) -> bool:
        """بوتات / أدمن / رتب مستثناة: ما نحولهم لنص (نخزن القرار 10 دقايق)."""
        now = time.monotonic()
        hit = self._skip_cache.get(uid)
        if hit and hit[0] > now:
            return hit[1]
        if self.client.user and uid == self.client.user.id:
            skip = True
        else:
            member = await self.get_member(uid)
            skip = member is None or member.bot or self.is_exempt(member)
        if len(self._skip_cache) > 500:
            self._skip_cache.clear()
        self._skip_cache[uid] = (now + 600.0, skip)
        return skip

    async def on_clip(self, uid: int, pcm: bytes):
        try:
            if len(pcm) < 48000 * 4 * MIN_CLIP_SEC:
                return
            wav, rms, seconds = await asyncio.to_thread(to_wav_16k_mono, pcm)
            if seconds < MIN_CLIP_SEC or rms < MIN_RMS:
                return
            if await self._skip_uid(uid):
                return
            self.stats.clips += 1
            channel_id = self.cfg.watch_channel_id
            g = self.guild
            m = g.get_member(uid) if g else None
            if m is not None and m.voice is not None and m.voice.channel is not None:
                channel_id = m.voice.channel.id
            self.enqueue(Job(uid, wav, channel_id, time.time(), seconds))
        except Exception:
            self.log.exception("clip error")

    def enqueue(self, job: Job):
        if self.queue.qsize() >= MAX_QUEUE:
            try:
                self.queue.get_nowait()
                self.stats.dropped += 1
            except asyncio.QueueEmpty:
                pass
        self.queue.put_nowait(job)

    async def _worker(self):
        while True:
            job = await self.queue.get()
            try:
                async with self.stt_sem:
                    await self.run_job(job)
            except Exception as e:
                self.stats.errors += 1
                self.log.error("job error: %s", e)

    async def transcribe(self, wav: bytes) -> dict:
        c = self.cfg
        for attempt in range(2):
            form = aiohttp.FormData()
            form.add_field("file", wav, filename="speech.wav", content_type="audio/wav")
            form.add_field("model", c.stt_model)
            form.add_field("language", "ar")
            form.add_field("response_format", "verbose_json")
            form.add_field("temperature", "0")
            async with self.session.post(
                c.stt_url,
                headers={"Authorization": f"Bearer {c.stt_key}"},
                data=form,
                timeout=aiohttp.ClientTimeout(total=25),
            ) as res:
                if res.status == 429 and attempt == 0:
                    try:
                        wait = float(res.headers.get("retry-after") or 3)
                    except ValueError:
                        wait = 3.0
                    await asyncio.sleep(min(wait, 15.0))
                    continue
                if res.status >= 400:
                    raise RuntimeError(f"STT {res.status}: {(await res.text())[:200]}")
                return await res.json(content_type=None)
        raise RuntimeError("STT rate limited")

    async def run_job(self, job: Job):
        result = await self.transcribe(job.wav)
        self.stats.transcribed += 1
        strong, weak = classify_result(result, self.cfg.min_logprob)
        if strong:
            await self.punish(job, strong)
        elif weak:
            await self.log_suspect(job, weak)

    # ---------------------------------------------------- العقوبة واللوق
    async def send_log(self, **kw):
        ch = self.client.get_channel(self.cfg.log_channel_id)
        if ch is None:
            try:
                ch = await self.client.fetch_channel(self.cfg.log_channel_id)
            except discord.HTTPException:
                ch = None
        if ch is None or not hasattr(ch, "send"):
            self.log.error("روم اللوق غير صالح")
            return None
        try:
            return await ch.send(**kw)
        except Exception as e:
            self.log.error("send log failed: %s", e)
            return None

    def build_fields(self, guild, member, job: Job, hit: dict):
        vc = guild.get_channel(job.channel_id) if job.channel_id else None
        present = []
        if vc is not None and hasattr(vc, "members"):
            present = [f"<@{m.id}>" for m in vc.members if not m.bot and m.id != member.id]
        ts = int(job.at)
        return [
            ("👤 الشخص", f"<@{member.id}>\n`{member}`", True),
            ("🗣️ الكلمة", f"`{clip(hit['said'], 100)}`", True),
            ("🔊 الروم", f"<#{vc.id}>\n{clip(vc.name, 60)}" if vc else "غير معروف", True),
            ("🕒 الوقت بالضبط", f"{self.fmt_clock(job.at)}\n<t:{ts}:F>", True),
            ("📝 الجملة", f"«{clip(hit.get('segment') or hit['said'], 300)}»", False),
            (f"👥 الموجودين بالروم ({len(present)})", self.join_mentions(present), False),
        ]

    @staticmethod
    def _embed(color: int, title: str, fields, description: Optional[str] = None,
               footer: Optional[str] = None, at: Optional[float] = None, extra=None):
        e = discord.Embed(color=discord.Colour(color), title=title, description=description)
        for name, value, inline in fields[:4]:
            e.add_field(name=name, value=value, inline=inline)
        for name, value, inline in (extra or []):
            e.add_field(name=name, value=value, inline=inline)
        for name, value, inline in fields[4:]:
            e.add_field(name=name, value=value, inline=inline)
        if footer:
            e.set_footer(text=footer)
        if at is not None:
            e.timestamp = datetime.fromtimestamp(at, tz=timezone.utc)
        return e

    def can_moderate(self, guild, member) -> bool:
        me = guild.me
        if me is None or member.id == guild.owner_id:
            return False
        if not me.guild_permissions.moderate_members:
            return False
        return me.top_role > member.top_role

    async def punish(self, job: Job, hit: dict):
        c = self.cfg
        guild = self.guild
        if not guild:
            return
        member = await self.get_member(job.user_id)
        if not member or member.bot or self.is_exempt(member):
            return

        now = time.time()
        if self.cooldown.get(member.id, 0) > now:
            return
        if member.is_timed_out():
            return

        fields = self.build_fields(guild, member, job, hit)

        if not self.can_moderate(guild, member):
            await self.send_log(embed=self._embed(
                0xE74C3C, "⚠️ رصد قذف لكن ما قدرت أعطيه تايم اوت", fields,
                description="رتبة البوت لازم تكون أعلى من رتبة الشخص، ولازم أملك صلاحية Moderate Members.",
                at=job.at))
            self.cooldown[member.id] = now + COOLDOWN_S
            return

        try:
            await member.timeout(timedelta(minutes=c.timeout_min), reason=clip(f"قذف في الروم الصوتي: {hit['said']}", 500))
        except Exception as e:
            self.log.error("timeout failed: %s", e)
            await self.send_log(embed=self._embed(
                0xE74C3C, "⚠️ فشل تنفيذ التايم اوت", fields, description=clip(e, 300), at=job.at))
            return

        self.cooldown[member.id] = now + COOLDOWN_S
        self.stats.punished += 1

        # 1) رسالة في شات الروم الصوتي
        vc = guild.get_channel(job.channel_id) if job.channel_id else None
        if vc is not None and hasattr(vc, "send"):
            try:
                await vc.send(
                    f"<@{member.id}> تم إعطاؤك تايم اوت **{c.timeout_min} دقائق**، السبب: **قذف**.",
                    allowed_mentions=discord.AllowedMentions(users=[member]),
                )
            except Exception:
                pass

        # 2) لوق المشرفين
        end_ts = int(now + c.timeout_min * 60)
        embed = self._embed(
            0xC0392B, "🚨 قذف في روم صوتي — تم إعطاء تايم اوت", fields,
            footer=f"ID: {member.id}", at=job.at,
            extra=[("⏳ العقوبة", f"{c.timeout_min} دقائق — تنتهي <t:{end_ts}:R>", True)],
        )
        view = discord.ui.View(timeout=3600)
        btn = discord.ui.Button(
            style=discord.ButtonStyle.success,
            label="رفع التايم اوت (غلط؟)",
            custom_id=f"untimeout:{member.id}",
        )

        async def _noop(_i):  # الرد الفعلي يتم في interaction() عشان يشتغل حتى بعد إعادة التشغيل
            return

        btn.callback = _noop
        view.add_item(btn)

        ping = f" <@&{c.ping_role_id}>" if c.ping_role_id else ""
        await self.send_log(
            content=f"@here{ping}",
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions(
                everyone=True,
                roles=[discord.Object(id=c.ping_role_id)] if c.ping_role_id else False,
                users=False,
            ),
        )

    async def log_suspect(self, job: Job, hit: dict):
        guild = self.guild
        if not guild:
            return
        member = await self.get_member(job.user_id)
        if not member or member.bot or self.is_exempt(member):
            return
        self.stats.suspects += 1
        await self.send_log(
            embed=self._embed(
                0xF39C12, "🟡 اشتباه — لم تُطبَّق عقوبة (الصوت غير واضح)",
                self.build_fields(guild, member, job, hit),
                description="التعرف على الكلام كان ضعيف الثقة، فتركته للمراجعة اليدوية بدل ما أظلم أحد.",
                footer=f"ID: {member.id}", at=job.at),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ---------------------------------------------------- الأوامر والأزرار
    def _register_commands(self):
        guild_obj = discord.Object(id=self.cfg.guild_id)

        async def status_cb(interaction: discord.Interaction):
            await self._cmd_status(interaction)

        async def test_cb(interaction: discord.Interaction, text: str):
            await self._cmd_test(interaction, text)

        status_cb = app_commands.default_permissions(moderate_members=True)(status_cb)
        test_cb = app_commands.describe(text="النص المراد فحصه")(test_cb)
        test_cb = app_commands.default_permissions(moderate_members=True)(test_cb)

        self.tree.add_command(
            app_commands.Command(name="status", description="حالة البوت وإحصائيات الجلسة", callback=status_cb),
            guild=guild_obj,
        )
        self.tree.add_command(
            app_commands.Command(name="test", description="يجرب نص: هل يعتبره البوت قذف؟ (بدون أي عقوبة)", callback=test_cb),
            guild=guild_obj,
        )

    def _is_mod(self, interaction) -> bool:
        try:
            return bool(interaction.permissions.moderate_members)
        except Exception:
            return False

    async def _cmd_status(self, i: discord.Interaction):
        if i.guild_id != self.cfg.guild_id:
            return
        if not self._is_mod(i):
            return await i.response.send_message("هذا الأمر للمشرفين فقط.", ephemeral=True)
        g = self.guild
        vc = g.voice_client if g else None
        cur = f"<#{vc.channel.id}>" if (vc and vc.channel) else "غير متصل"
        me = g.me if g else None
        vs = me.voice if me else None
        lines = [
            f"الروم المحدد: <#{self.cfg.watch_channel_id}>",
            f"الروم الحالي: {cur}",
            f"ميوت سيرفر: {'نعم' if (vs and vs.mute) else 'لا'} | دفن سيرفر: {'نعم' if (vs and vs.deaf) else 'لا'}",
            f"مدة التايم اوت: {self.cfg.timeout_min} دقائق",
            f"حزم صوت مستلمة: {self.sink.packets} (مجهولة الصاحب: {self.sink.unknown})",
            f"مقاطع تم التقاطها: {self.stats.clips}",
            f"تم تحويلها لنص: {self.stats.transcribed}",
            f"عقوبات: {self.stats.punished}",
            f"اشتباهات بدون عقوبة: {self.stats.suspects}",
            f"في الطابور: {self.queue.qsize()} | مهملة: {self.stats.dropped} | أخطاء: {self.stats.errors}",
        ]
        if voice_recv_dave is not None:
            try:
                s = voice_recv_dave.summary()
                lines.append(
                    f"DAVE: فك ناجح={s.get('ok')} فاشل={s.get('fail')} غير مشفر={s.get('bypass')} e2ee={s.get('e2ee')}"
                )
            except Exception:
                pass
        else:
            lines.append("DAVE: مكتبة الإصلاح غير مثبتة")
        await i.response.send_message("\n".join(lines), ephemeral=True)

    async def _cmd_test(self, i: discord.Interaction, text: str):
        if i.guild_id != self.cfg.guild_id:
            return
        if not self._is_mod(i):
            return await i.response.send_message("هذا الأمر للمشرفين فقط.", ephemeral=True)
        hits = find_bad_words(text)
        msg = (
            "🚨 يعتبرها قذف: " + "، ".join(f"`{h['said']}`" for h in hits)
            if hits else "✅ ما فيها شي يعتبره قذف."
        )
        await i.response.send_message(msg, ephemeral=True)

    async def interaction(self, i: discord.Interaction):
        try:
            if i.type != discord.InteractionType.component:
                return
            cid = (i.data or {}).get("custom_id", "")
            if not str(cid).startswith("untimeout:"):
                return
            if not self._is_mod(i):
                return await i.response.send_message("هذا الزر للمشرفين فقط.", ephemeral=True)
            uid = int(str(cid).split(":", 1)[1])
            member = await self.get_member(uid)
            if member is not None and member.is_timed_out():
                try:
                    await member.timeout(None, reason=clip(f"رفع بواسطة {i.user}", 100))
                except Exception as e:
                    self.log.error("untimeout failed: %s", e)
            name = getattr(i.user, "display_name", None) or i.user.name
            view = discord.ui.View(timeout=5)
            view.add_item(discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label=clip(f"تم رفع التايم اوت بواسطة {name}", 80),
                custom_id="done",
                disabled=True,
            ))
            await i.response.edit_message(view=view)
        except Exception:
            self.log.exception("interaction error")
            try:
                if not i.response.is_done():
                    await i.response.send_message("صار خطأ، حاول مرة ثانية.", ephemeral=True)
            except Exception:
                pass
