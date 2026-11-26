"""
بوت التمب فويس (الرومات المؤقتة) - discord.py 2.6+
- روم "إنشاء" ثابت: أي عضو يدخله ينشأ له روم خاص ويُسحب إليه فوراً
- لوحة تحكم مثبتة في شات الروم (Components V2) أزرارها تعمل لصاحب الروم فقط
- لوج لإنشاء الرومات + أوامر إضافية
- بدون قاعدة بيانات: الصلاحيات محفوظة في صلاحيات الروم نفسه، والمالك محفوظ في رسالة اللوحة

ملاحظة: هذا الملف هو الملف الرئيسي لكل البوتات. بوت التمب فويس (مع اضف / ايديات / المكلبة) يعمل هنا،
وبقية البوتات (Test me / Uno / الفعاليات) في مجلد bots/ ويشغّلها القسم الأخير "التشغيل" بتوكن مستقل لكل بوت.
"""
import asyncio
import importlib
import logging
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

import discord
from aiohttp import web
from discord.http import Route

# كل المسارات النسبية (الأيقونات، الخطوط، قواعد البيانات) تكون نسبة لمجلد المشروع
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# ============================================================ الإعدادات
TOKEN = os.getenv("TOKEN") or os.getenv("DISCORD_TOKEN")
CREATE_CHANNEL_ID = int(os.getenv("CREATE_CHANNEL_ID", "0"))  # روم "اضغط لإنشاء روم"
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))        # روم اللوج
CATEGORY_ID = int(os.getenv("CATEGORY_ID", "0"))              # 0 = نفس كاتيجوري روم الإنشاء
PORT = int(os.getenv("PORT", "8080"))

ACCENT_COLOR = 0x333153        # لون شريط اللوحة
ICON_VERSION = 1               # ارفعه (2، 3...) إذا غيّرت لون الأيقونات وأعدت رفعها
ICON_DIR = "."
ROOM_NAME_FORMAT = "روم {name}"
AUTO_TRANSFER_ON_OWNER_LEAVE = True   # لو المالك طلع وبقي ناس: ينتقل الملك تلقائياً
FLIP_ROWS = False                     # اجعلها True لو ترتيب الأزرار معكوس عندك (واجهة RTL)

COMMANDS = {
    "panel": {"لوحة الروم", "لوحه الروم"},
    "claim": {"استلام الروم", "استلام"},
    "info": {"معلومات الروم", "معلومات"},
    "reset": {"إعادة ضبط الروم", "اعادة ضبط الروم", "ريست الروم"},
}

NO_MENTIONS = discord.AllowedMentions.none()
log = logging.getLogger("tempvoice")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ============================================================ الأيقونات
ICON_KEYS = [
    "lock", "unlock", "hide", "show", "rename", "limit", "region", "status",
    "kick", "ban", "unban", "invite", "trust", "untrust", "trusted", "banned",
    "allow_role", "remove_role", "roles", "owner",
]
EMOJI_FALLBACK = {
    "lock": "🔒", "unlock": "🔓", "hide": "🙈", "show": "👁️", "rename": "✏️", "limit": "👥",
    "region": "🌐", "status": "💬", "kick": "👢", "ban": "⛔", "unban": "✅", "invite": "✉️",
    "trust": "🛡️", "untrust": "❌", "trusted": "📋", "banned": "📋", "allow_role": "🏷️",
    "remove_role": "🏷️", "roles": "📃", "owner": "👑",
}
EMOJIS: dict = {}

LAYOUT_SETTINGS = [
    [("lock", "قفل"), ("unlock", "فتح"), ("hide", "إخفاء"), ("show", "إظهار")],
    [("rename", "تغيير الاسم"), ("limit", "عدد الأعضاء"), ("region", "منطقة الاتصال"), ("status", "حالة الروم")],
]
LAYOUT_MEMBERS = [
    [("kick", "طرد عضو"), ("ban", "حظر عضو"), ("unban", "إلغاء الحظر"), ("invite", "دعوة عضو")],
    [("trust", "توثيق عضو"), ("untrust", "إلغاء التوثيق"), ("trusted", "الموثوقون"), ("banned", "المحظورون")],
    [("allow_role", "السماح لرتبة"), ("remove_role", "إزالة رتبة"), ("roles", "الرتب المسموحة"), ("owner", "نقل الملكية")],
]

REGIONS = [
    ("تلقائي", "automatic"), ("روتردام", "rotterdam"), ("روسيا", "russia"), ("الهند", "india"),
    ("سنغافورة", "singapore"), ("هونغ كونغ", "hongkong"), ("اليابان", "japan"), ("سيدني", "sydney"),
    ("جنوب أفريقيا", "southafrica"), ("البرازيل", "brazil"), ("أمريكا الشرقية", "us-east"),
    ("أمريكا الغربية", "us-west"), ("أمريكا الوسطى", "us-central"), ("أمريكا الجنوبية", "us-south"),
]


# ============================================================ السيرفر الصغير (Render + UptimeRobot)
# سيرفر واحد فقط لكل البوتات (نفس البورت)، ويُشغَّل مرة واحدة مهما استدعيناه.
_web_runner = None


async def ensure_web_server():
    global _web_runner
    if _web_runner is not None:
        return

    async def ok(_):
        return web.Response(text="Rav bots are running")

    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    _web_runner = runner
    log.info("web server on port %s", PORT)


# ============================================================ بيانات الرومات (في الذاكرة)
@dataclass
class Room:
    owner_id: int
    panel_id: Optional[int]
    created: float


class TempVoiceBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.voice_states = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.rooms: dict[int, Room] = {}
        self.locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.panel_lock = asyncio.Lock()
        self._ready_once = False
        self.created_total = 0

    # ---------------------------------------------------------- تشغيل
    async def setup_hook(self):
        await self.sync_emojis()
        self.add_view(build_panel(None))  # يجعل الأزرار تعمل حتى بعد إعادة تشغيل البوت
        await self.start_web()
        asyncio.create_task(self.cleanup_loop())

    async def start_web(self):
        await ensure_web_server()

    async def sync_emojis(self):
        """يرفع الأيقونات كإيموجي للتطبيق (مرة واحدة فقط) ويستخدمها في الأزرار."""
        try:
            existing = {e.name: e for e in await self.fetch_application_emojis()}
        except Exception as e:
            log.warning("fetch_application_emojis failed: %s", e)
            existing = {}
        for key in ICON_KEYS:
            name = f"tv{ICON_VERSION}_{key}"
            emoji = existing.get(name)
            path = os.path.join(ICON_DIR, f"{key}.png")
            if emoji is None and os.path.exists(path):
                try:
                    with open(path, "rb") as f:
                        emoji = await self.create_application_emoji(name=name, image=f.read())
                except Exception as e:
                    log.warning("could not upload icon %s: %s", key, e)
            EMOJIS[key] = emoji if emoji is not None else EMOJI_FALLBACK[key]

    async def on_ready(self):
        log.info("logged in as %s", self.user)
        if self._ready_once:
            return
        self._ready_once = True
        await self.recover_rooms()

    # ---------------------------------------------------------- استرجاع الرومات بعد إعادة التشغيل
    async def recover_rooms(self):
        create = self.get_channel(CREATE_CHANNEL_ID)
        if not isinstance(create, discord.VoiceChannel):
            log.warning("CREATE_CHANNEL_ID غير صحيح أو البوت لا يراه")
            return
        category = self.get_category(create)
        if category is None:
            return
        for ch in category.voice_channels:
            if ch.id == CREATE_CHANNEL_ID:
                continue
            try:
                async for m in ch.history(limit=50):
                    if m.author.id != self.user.id or not m.pinned:
                        continue
                    owner_id = extract_owner_id(m)
                    if owner_id:
                        self.rooms[ch.id] = Room(owner_id, m.id, time.time())
                        break
            except discord.HTTPException:
                continue
        for cid in list(self.rooms):
            ch = self.get_channel(cid)
            if ch and not [m for m in ch.members if not m.bot]:
                await self.delete_room(ch, "روم فارغ بعد إعادة التشغيل", silent=True)
        log.info("recovered %d rooms", len(self.rooms))

    def get_category(self, create: discord.VoiceChannel) -> Optional[discord.CategoryChannel]:
        if CATEGORY_ID:
            c = self.get_channel(CATEGORY_ID)
            if isinstance(c, discord.CategoryChannel):
                return c
        return create.category

    # ---------------------------------------------------------- أحداث الصوت
    async def on_voice_state_update(self, member, before, after):
        if member.bot or before.channel == after.channel:
            return
        if before.channel and before.channel.id in self.rooms:
            await self.handle_leave(member, before.channel)
        if after.channel and after.channel.id == CREATE_CHANNEL_ID:
            await self.handle_create(member, after.channel)

    async def handle_leave(self, member: discord.Member, ch: discord.VoiceChannel):
        room = self.rooms.get(ch.id)
        if not room:
            return
        humans = [m for m in ch.members if not m.bot]
        if not humans:
            await self.delete_room(ch, "الروم فاضي")
        elif member.id == room.owner_id and AUTO_TRANSFER_ON_OWNER_LEAVE:
            await self.transfer_owner(ch, room, humans[0], "المالك غادر الروم - نقل تلقائي")

    async def handle_create(self, member: discord.Member, source: discord.VoiceChannel):
        async with self.locks[member.id]:
            # لو عنده روم موجود، نرجعه له بدل ما نسوي ثاني
            for cid, room in self.rooms.items():
                if room.owner_id == member.id:
                    existing = self.get_channel(cid)
                    if existing:
                        try:
                            await member.move_to(existing)
                        except discord.HTTPException:
                            pass
                        return
            guild = member.guild
            category = self.get_category(source)
            overwrites = dict(category.overwrites) if category else {}
            overwrites[member] = owner_overwrite()
            name = ROOM_NAME_FORMAT.format(name=member.display_name)[:100]
            try:
                ch = await guild.create_voice_channel(
                    name=name,
                    category=category,
                    overwrites=overwrites,
                    bitrate=min(source.bitrate, guild.bitrate_limit),
                    reason=f"تمب فويس: {member} أنشأ روماً",
                )
            except discord.HTTPException as e:
                log.error("create room failed: %s", e)
                return
            self.rooms[ch.id] = Room(member.id, None, time.time())
            self.created_total += 1
            try:
                await member.move_to(ch)
            except discord.HTTPException:
                await self.delete_room(ch, "العضو غادر قبل السحب", silent=True)
                return
            await self.send_panel(ch, member.id)
            ts = int(time.time())
            await self.send_log(
                title="إنشاء روم جديد",
                description=f"{member.mention} أنشأ الروم {ch.mention}\nالوقت: <t:{ts}:F> (<t:{ts}:R>)",
                member=member,
                fields=[("اسم الروم", ch.name), ("آيدي المالك", str(member.id))],
            )

    # ---------------------------------------------------------- اللوحة
    async def send_panel(self, ch: discord.VoiceChannel, owner_id: int):
        async with self.panel_lock:
            room = self.rooms.get(ch.id)
            if not room:
                return
            try:
                msg = await ch.send(
                    view=build_panel(owner_id),
                    allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=owner_id)]),
                )
            except discord.HTTPException as e:
                log.error("send panel failed: %s", e)
                return
            room.panel_id = msg.id
            try:
                await msg.pin()
            except discord.HTTPException as e:
                log.warning("pin failed (تأكد من صلاحية تثبيت الرسائل): %s", e)

    async def refresh_panel(self, ch: discord.VoiceChannel, room: Room):
        if room.panel_id:
            try:
                msg = await ch.fetch_message(room.panel_id)
                await msg.edit(view=build_panel(room.owner_id))
                return
            except discord.NotFound:
                pass
            except discord.HTTPException as e:
                log.warning("edit panel failed: %s", e)
                return
        await self.send_panel(ch, room.owner_id)

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        room = self.rooms.get(payload.channel_id)
        if room and room.panel_id == payload.message_id:
            ch = self.get_channel(payload.channel_id)
            if ch:
                room.panel_id = None
                await self.send_panel(ch, room.owner_id)  # اللوحة ما تنحذف أبداً

    async def on_guild_channel_delete(self, channel):
        self.rooms.pop(channel.id, None)

    # ---------------------------------------------------------- ملكية الروم
    async def transfer_owner(self, ch: discord.VoiceChannel, room: Room, new_owner: discord.Member, reason: str):
        old_id = room.owner_id
        await set_ow(ch, new_owner, **OWNER_PERMS)
        await set_ow(ch, ch.guild.get_member(old_id) or discord.Object(id=old_id), **{k: None for k in OWNER_PERMS})
        room.owner_id = new_owner.id
        await self.refresh_panel(ch, room)
        try:
            await ch.send(
                f"{new_owner.mention} أصبحت الآن مالك الروم.",
                allowed_mentions=discord.AllowedMentions(users=[new_owner]),
                delete_after=20,
            )
        except discord.HTTPException:
            pass
        await self.send_log(
            title="نقل ملكية روم",
            description=f"{ch.mention}\nمن <@{old_id}> إلى {new_owner.mention}\nالسبب: {reason}",
            member=new_owner,
        )

    async def delete_room(self, ch, reason: str, silent: bool = False):
        room = self.rooms.pop(ch.id, None)
        try:
            await ch.delete(reason=f"تمب فويس: {reason}")
        except discord.HTTPException:
            pass
        if room and not silent:
            await self.send_log(
                title="حذف روم",
                description=f"تم حذف الروم **{ch.name}** (المالك <@{room.owner_id}>)\nعاش: {int(time.time() - room.created) // 60} دقيقة",
            )

    async def cleanup_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(45)
            for cid, room in list(self.rooms.items()):
                try:
                    ch = self.get_channel(cid)
                    if ch is None:
                        self.rooms.pop(cid, None)
                    elif time.time() - room.created > 20 and not [m for m in ch.members if not m.bot]:
                        await self.delete_room(ch, "تنظيف دوري - روم فاضي")
                except Exception as e:
                    log.warning("cleanup error: %s", e)

    # ---------------------------------------------------------- اللوج
    async def send_log(self, title: str, description: str, member: Optional[discord.Member] = None, fields=None):
        if not LOG_CHANNEL_ID:
            return
        channel = self.get_channel(LOG_CHANNEL_ID)
        if channel is None:
            return
        embed = discord.Embed(title=title, description=description, colour=discord.Colour(ACCENT_COLOR),
                              timestamp=datetime.now(timezone.utc))
        if member:
            embed.set_thumbnail(url=member.display_avatar.url)
        for n, v in fields or []:
            embed.add_field(name=n, value=v, inline=True)
        try:
            await channel.send(embed=embed, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException as e:
            log.warning("log send failed: %s", e)

    # ---------------------------------------------------------- الأوامر (بدون بادئة)
    async def on_message(self, message: discord.Message):
        if message.guild is None:
            return
        if message.type == discord.MessageType.pins_add and message.channel.id in self.rooms:
            try:
                await message.delete()  # حذف رسالة "ثبّت رسالة" المزعجة
            except discord.HTTPException:
                pass
            return
        if message.author.bot:
            return
        text = message.content.strip()
        cmd = next((k for k, names in COMMANDS.items() if text in names), None)
        if not cmd:
            return
        await getattr(self, f"cmd_{cmd}")(message)

    def resolve_room(self, message: discord.Message):
        """الروم المقصود: شات الروم نفسه، أو الروم الصوتي اللي العضو فيه."""
        if message.channel.id in self.rooms:
            return message.channel, self.rooms[message.channel.id]
        vs = message.author.voice
        if vs and vs.channel and vs.channel.id in self.rooms:
            return vs.channel, self.rooms[vs.channel.id]
        return None, None

    async def say(self, message: discord.Message, text: str, **kw):
        try:
            await message.reply(text, mention_author=False, delete_after=15, allowed_mentions=NO_MENTIONS, **kw)
        except discord.HTTPException:
            pass

    async def cleanup_cmd(self, message: discord.Message):
        try:
            await message.delete()
        except discord.HTTPException:
            pass

    def is_owner_or_admin(self, member: discord.Member, room: Room) -> bool:
        return member.id == room.owner_id or member.guild_permissions.administrator

    async def cmd_panel(self, message: discord.Message):
        ch, room = self.resolve_room(message)
        if not room:
            return await self.say(message, "ادخل رومك الصوتي أولاً، أو اكتب الأمر داخل شات الروم.")
        if not self.is_owner_or_admin(message.author, room):
            return await self.say(message, "هذا الأمر لصاحب الروم فقط.")
        async with self.panel_lock:
            old_id, room.panel_id = room.panel_id, None  # لتعطيل إعادة الإرسال التلقائية
        if old_id:
            try:
                old = await ch.fetch_message(old_id)
                await old.delete()
            except discord.HTTPException:
                pass
        await self.send_panel(ch, room.owner_id)
        await self.cleanup_cmd(message)

    async def cmd_claim(self, message: discord.Message):
        ch, room = self.resolve_room(message)
        vs = message.author.voice
        if not room or not vs or vs.channel != ch:
            return await self.say(message, "لازم تكون داخل الروم الصوتي.")
        if message.author.id == room.owner_id:
            return await self.say(message, "أنت مالك الروم أصلاً.")
        if any(m.id == room.owner_id for m in ch.members):
            return await self.say(message, "المالك الحالي ما زال داخل الروم، ما تقدر تستلمه.")
        await self.transfer_owner(ch, room, message.author, "استلام الروم (المالك غير موجود)")
        await self.cleanup_cmd(message)

    async def cmd_info(self, message: discord.Message):
        ch, room = self.resolve_room(message)
        if not room:
            return await self.say(message, "ادخل روماً مؤقتاً أو اكتب الأمر داخل شات الروم.")
        everyone = ch.overwrites_for(ch.guild.default_role)
        lists = classify(ch, room)
        embed = discord.Embed(title=f"معلومات {ch.name}", colour=discord.Colour(ACCENT_COLOR))
        embed.add_field(name="المالك", value=f"<@{room.owner_id}>")
        embed.add_field(name="الأعضاء", value=f"{len([m for m in ch.members])}/{ch.user_limit or '∞'}")
        embed.add_field(name="الحالة", value=("مقفل" if everyone.connect is False else "مفتوح") + " / " +
                        ("مخفي" if everyone.view_channel is False else "ظاهر"))
        embed.add_field(name="المنطقة", value=str(ch.rtc_region or "تلقائي"))
        embed.add_field(name="الموثوقون / المحظورون", value=f"{len(lists['trusted'])} / {len(lists['banned'])}")
        embed.add_field(name="أُنشئ", value=f"<t:{int(ch.created_at.timestamp())}:R>")
        try:
            await message.reply(embed=embed, mention_author=False, delete_after=40, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException:
            pass

    async def cmd_reset(self, message: discord.Message):
        ch, room = self.resolve_room(message)
        if not room:
            return await self.say(message, "ادخل روماً مؤقتاً أو اكتب الأمر داخل شات الروم.")
        if not self.is_owner_or_admin(message.author, room):
            return await self.say(message, "هذا الأمر لصاحب الروم فقط.")
        wait = await message.reply("جاري إعادة ضبط الروم...", mention_author=False)
        guild = ch.guild
        for target, _ in list(ch.overwrites.items()):
            if target == guild.default_role or target.id in (room.owner_id, self.user.id):
                continue
            try:
                await clear_ow(ch, target)
            except Exception:
                pass
        await set_ow(ch, guild.default_role, connect=None, view_channel=None)
        try:
            await asyncio.wait_for(ch.edit(user_limit=0, rtc_region=None), 10)
            await set_status(ch, "")
        except Exception:
            pass
        await wait.edit(content="تمت إعادة ضبط الروم: فتح + إظهار + مسح الحظر والموثوقين والرتب والحد والمنطقة.")
        await wait.delete(delay=15)
        await self.cleanup_cmd(message)


bot = TempVoiceBot()


# ============================================================ أدوات مساعدة
OWNER_PERMS = dict(view_channel=True, connect=True, speak=True, stream=True, send_messages=True)


def owner_overwrite() -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite(**OWNER_PERMS)


async def set_ow(ch: discord.abc.GuildChannel, target, **changes):
    """يعدّل صلاحيات محددة فقط مع الحفاظ على باقي صلاحيات نفس الهدف."""
    ow = ch.overwrites_for(target)
    ow.update(**changes)
    await apply_ow(ch, target, None if ow.is_empty() else ow)


async def clear_ow(ch: discord.abc.GuildChannel, target):
    await apply_ow(ch, target, None)


async def apply_ow(ch, target, ow: Optional[discord.PermissionOverwrite]):
    if isinstance(target, (discord.Member, discord.Role)):
        await ch.set_permissions(target, overwrite=ow, reason="تمب فويس")
        return
    # هدف غير موجود في الكاش (عضو غادر السيرفر مثلاً): نستخدم الطلب المباشر
    if ow is None:
        await bot.http.delete_channel_permissions(ch.id, target.id, reason="تمب فويس")
    else:
        allow, deny = ow.pair()
        await bot.http.edit_channel_permissions(ch.id, target.id, allow.value, deny.value, 1, reason="تمب فويس")


async def set_status(ch: discord.VoiceChannel, text: str):
    await bot.http.request(
        Route("PUT", "/channels/{channel_id}/voice-status", channel_id=ch.id),
        json={"status": text or None},
    )


def extract_owner_id(message: discord.Message) -> Optional[int]:
    texts: list[str] = [message.content or ""]

    def walk(obj):
        c = getattr(obj, "content", None)
        if isinstance(c, str):
            texts.append(c)
        for attr in ("children", "components"):
            for child in getattr(obj, attr, None) or []:
                walk(child)

    for comp in message.components:
        walk(comp)
    m = re.search(r"<@!?(\d+)>", "\n".join(texts))
    if m:
        return int(m.group(1))
    return message.mentions[0].id if message.mentions else None


def classify(ch: discord.VoiceChannel, room: Room) -> dict[str, list[int]]:
    out = {"banned": [], "trusted": [], "roles": []}
    for target, ow in ch.overwrites.items():
        if isinstance(target, discord.Role):
            if not target.is_default() and not target.managed and ow.connect is True:
                out["roles"].append(target.id)
        elif target.id not in (room.owner_id, bot.user.id):
            if ow.connect is False:
                out["banned"].append(target.id)
            elif ow.connect is True:
                out["trusted"].append(target.id)
    return out


# ============================================================ لوحة التحكم
class PanelButton(discord.ui.Button):
    def __init__(self, key: str, label: str):
        super().__init__(style=discord.ButtonStyle.secondary, label=label,
                         custom_id=f"tv:{key}", emoji=EMOJIS.get(key) or EMOJI_FALLBACK[key])
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        await HANDLERS[self.key](interaction)


def build_panel(owner_id: Optional[int]) -> discord.ui.LayoutView:
    def rows(spec):
        out = []
        for row in spec:
            buttons = [PanelButton(k, label) for k, label in row]
            if FLIP_ROWS:
                buttons.reverse()
            out.append(discord.ui.ActionRow(*buttons))
        return out

    header = "### لوحة تيمب فويس\nادخل رومك الصوتي واستخدم الأزرار للتحكم فيه."
    if owner_id:
        header += f"\n\nمالك الروم: <@{owner_id}>"
    sep = lambda: discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small)

    container = discord.ui.Container(accent_colour=discord.Colour(ACCENT_COLOR))
    container.add_item(discord.ui.TextDisplay(header))
    container.add_item(sep())
    container.add_item(discord.ui.TextDisplay("**إعدادات الروم**"))
    for r in rows(LAYOUT_SETTINGS):
        container.add_item(r)
    container.add_item(sep())
    container.add_item(discord.ui.TextDisplay("**الأعضاء والصلاحيات**"))
    for r in rows(LAYOUT_MEMBERS):
        container.add_item(r)

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


# ============================================================ فحص المالك
async def ctx(interaction: discord.Interaction):
    room = bot.rooms.get(interaction.channel_id)
    ch = interaction.guild.get_channel(interaction.channel_id) if interaction.guild else None
    if room is None or ch is None:
        await interaction.response.send_message("هذا الروم غير مسجل كروم مؤقت.", ephemeral=True)
        return None
    if interaction.user.id != room.owner_id:
        await interaction.response.send_message(
            f"هذه الأزرار لصاحب الروم فقط (<@{room.owner_id}>).", ephemeral=True, allowed_mentions=NO_MENTIONS)
        return None
    return room, ch


def obj_of(guild: discord.Guild, user):
    return guild.get_member(user.id) or discord.Object(id=user.id)


def to_members(guild: discord.Guild, values) -> list[discord.Member]:
    return [m for m in (guild.get_member(v.id) for v in values) if m]


# ============================================================ نوافذ الإدخال
class TextModal(discord.ui.Modal):
    def __init__(self, title: str, label: str, callback: Callable[[discord.Interaction, str], Awaitable[None]],
                 max_length: int, placeholder: str = "", default: str = "", required: bool = True):
        super().__init__(title=title, timeout=300)
        self.input = discord.ui.TextInput(label=label, max_length=max_length, placeholder=placeholder,
                                          default=default or None, required=required)
        self.add_item(self.input)
        self.cb = callback

    async def on_submit(self, interaction: discord.Interaction):
        await self.cb(interaction, self.input.value.strip())


async def h_rename(interaction: discord.Interaction):
    r = await ctx(interaction)
    if not r:
        return
    _, ch = r

    async def done(i: discord.Interaction, value: str):
        r2 = await ctx(i)
        if not r2:
            return
        await i.response.defer(ephemeral=True)
        try:
            await asyncio.wait_for(ch.edit(name=value[:100], reason="تمب فويس: تغيير الاسم"), 12)
            await i.followup.send(f"تم تغيير الاسم إلى: **{value}**", ephemeral=True)
        except asyncio.TimeoutError:
            await i.followup.send("ديسكورد يسمح بتغيير الاسم مرتين فقط كل 10 دقائق، جرب بعد شوي.", ephemeral=True)
        except discord.HTTPException as e:
            await i.followup.send(f"تعذر تغيير الاسم: {e.text}", ephemeral=True)

    await interaction.response.send_modal(TextModal("تغيير اسم الروم", "الاسم الجديد", done, 100, default=ch.name))


async def h_limit(interaction: discord.Interaction):
    r = await ctx(interaction)
    if not r:
        return
    _, ch = r

    async def done(i: discord.Interaction, value: str):
        r2 = await ctx(i)
        if not r2:
            return
        if not value.isdigit() or not 0 <= int(value) <= 99:
            return await i.response.send_message("اكتب رقماً من 0 إلى 99 (0 = بدون حد).", ephemeral=True)
        await i.response.defer(ephemeral=True)
        try:
            await asyncio.wait_for(ch.edit(user_limit=int(value), reason="تمب فويس: عدد الأعضاء"), 12)
            await i.followup.send("تم إلغاء الحد." if int(value) == 0 else f"تم تحديد الحد بـ **{value}** عضو.",
                                  ephemeral=True)
        except Exception as e:
            await i.followup.send(f"تعذر التنفيذ: {e}", ephemeral=True)

    await interaction.response.send_modal(
        TextModal("عدد الأعضاء", "الحد الأقصى (0 = بدون حد)", done, 2, "مثال: 5", str(ch.user_limit)))


async def h_status(interaction: discord.Interaction):
    r = await ctx(interaction)
    if not r:
        return
    _, ch = r

    async def done(i: discord.Interaction, value: str):
        r2 = await ctx(i)
        if not r2:
            return
        await i.response.defer(ephemeral=True)
        try:
            await set_status(ch, value)
            await i.followup.send("تم تحديث حالة الروم." if value else "تم مسح حالة الروم.", ephemeral=True)
        except discord.HTTPException as e:
            await i.followup.send(f"تعذر تحديث الحالة (تأكد أن البوت عنده صلاحية Set Voice Channel Status): {e.text}",
                                  ephemeral=True)

    await interaction.response.send_modal(
        TextModal("حالة الروم", "اكتب الحالة (فارغ = مسح)", done, 500, "مثال: نلعب فالورانت", required=False))


# ============================================================ قوائم الاختيار (مخفية)
class PickView(discord.ui.View):
    def __init__(self, channel_id: int, owner_id: int, kind: str, placeholder: str, action, max_values: int = 1):
        super().__init__(timeout=120)
        self.channel_id, self.owner_id, self.action = channel_id, owner_id, action
        if kind == "user":
            self.sel = discord.ui.UserSelect(placeholder=placeholder, min_values=1, max_values=max_values)
        else:
            self.sel = discord.ui.RoleSelect(placeholder=placeholder, min_values=1, max_values=max_values)
        self.sel.callback = self.on_select
        self.add_item(self.sel)

    async def on_select(self, interaction: discord.Interaction):
        room = bot.rooms.get(self.channel_id)
        ch = interaction.guild.get_channel(self.channel_id)
        if not room or not ch:
            return await interaction.response.edit_message(content="الروم لم يعد موجوداً.", view=None)
        if interaction.user.id != room.owner_id:
            return await interaction.response.send_message("هذه الأزرار لصاحب الروم فقط.", ephemeral=True)
        await interaction.response.defer()
        try:
            text = await self.action(interaction, ch, room, list(self.sel.values))
        except discord.HTTPException as e:
            text = f"تعذر التنفيذ: {e.text}"
        await interaction.edit_original_response(content=text, view=None, allowed_mentions=NO_MENTIONS)


def picker(kind: str, placeholder: str, action, max_values: int = 1):
    async def handler(interaction: discord.Interaction):
        r = await ctx(interaction)
        if not r:
            return
        _, ch = r
        view = PickView(ch.id, interaction.user.id, kind, placeholder, action, max_values)
        await interaction.response.send_message(placeholder, view=view, ephemeral=True)
    return handler


# ---- الإجراءات (تُستدعى بعد اختيار العضو/الرتبة)
async def act_kick(i, ch, room, targets):
    out = []
    for m in to_members(ch.guild, targets):
        if m.id != room.owner_id and m in ch.members:
            await m.move_to(None, reason="تمب فويس: طرد من المالك")
            out.append(m.mention)
    return "تم طرد: " + "، ".join(out) if out else "لا أحد من المحددين داخل الروم (أو المحدد هو المالك)."


async def act_ban(i, ch, room, targets):
    out = []
    for m in to_members(ch.guild, targets):
        if m.id in (room.owner_id, bot.user.id):
            continue
        await set_ow(ch, m, connect=False, view_channel=None)
        if m in ch.members:
            await m.move_to(None, reason="تمب فويس: حظر")
        out.append(m.mention)
    return "تم حظر: " + "، ".join(out) if out else "ما تقدر تحظر نفسك أو البوت."


async def act_unban(i, ch, room, targets):
    out = []
    for m in targets:
        t = obj_of(ch.guild, m)
        if ch.overwrites_for(t).connect is False:
            await set_ow(ch, t, connect=None)
            out.append(f"<@{m.id}>")
    return "تم فك الحظر عن: " + "، ".join(out) if out else "المحددون غير محظورين."


async def act_invite(i, ch, room, targets):
    out = []
    for m in to_members(ch.guild, targets):
        if m.bot:
            continue
        await set_ow(ch, m, view_channel=True, connect=True)
        try:
            await m.send(f"{i.user.mention} دعاك للانضمام إلى الروم {ch.jump_url}", allowed_mentions=NO_MENTIONS)
        except discord.HTTPException:
            pass
        out.append(m.mention)
    return "تمت دعوة: " + "، ".join(out) + " (يقدرون يدخلون حتى لو الروم مقفل)." if out else "اختر أعضاء حقيقيين."


async def act_trust(i, ch, room, targets):
    out = []
    for m in to_members(ch.guild, targets):
        if m.bot or m.id == room.owner_id:
            continue
        await set_ow(ch, m, view_channel=True, connect=True)
        out.append(m.mention)
    return "تم توثيق: " + "، ".join(out) if out else "اختر أعضاء حقيقيين."


async def act_untrust(i, ch, room, targets):
    out = []
    for m in targets:
        t = obj_of(ch.guild, m)
        if ch.overwrites_for(t).connect is True and m.id != room.owner_id:
            await set_ow(ch, t, view_channel=None, connect=None)
            out.append(f"<@{m.id}>")
    return "تم إلغاء توثيق: " + "، ".join(out) if out else "المحددون غير موثوقين."


async def act_allow_role(i, ch, room, targets):
    out = []
    for r in targets:
        if r.is_default():
            continue
        await set_ow(ch, r, view_channel=True, connect=True)
        out.append(r.mention)
    return "تم السماح للرتب: " + "، ".join(out) if out else "لا يمكن اختيار @everyone."


async def act_remove_role(i, ch, room, targets):
    out = []
    for r in targets:
        if not r.is_default() and ch.overwrites_for(r).connect is True:
            await set_ow(ch, r, view_channel=None, connect=None)
            out.append(r.mention)
    return "تمت إزالة: " + "، ".join(out) if out else "هذه الرتب غير مسموحة أصلاً."


async def act_owner(i, ch, room, targets):
    m = ch.guild.get_member(targets[0].id)
    if not m or m.bot or m.id == room.owner_id:
        return "اختر عضواً حقيقياً غير المالك الحالي."
    if m not in ch.members:
        return "العضو لازم يكون داخل الروم عشان تنقل له الملكية."
    await bot.transfer_owner(ch, room, m, f"نقل ملكية بواسطة {i.user}")
    return f"تم نقل ملكية الروم إلى {m.mention}."


# ---- أزرار فورية
def perm_button(**changes_msg):
    changes, msg = changes_msg["changes"], changes_msg["msg"]

    async def handler(interaction: discord.Interaction):
        r = await ctx(interaction)
        if not r:
            return
        _, ch = r
        await interaction.response.defer(ephemeral=True)
        try:
            await set_ow(ch, ch.guild.default_role, **changes)
            await interaction.followup.send(msg, ephemeral=True)
        except discord.HTTPException as e:
            await interaction.followup.send(f"تعذر التنفيذ: {e.text}", ephemeral=True)
    return handler


def list_button(kind: str, title: str):
    async def handler(interaction: discord.Interaction):
        r = await ctx(interaction)
        if not r:
            return
        room, ch = r
        ids = classify(ch, room)[kind]
        mention = (lambda x: f"<@&{x}>") if kind == "roles" else (lambda x: f"<@{x}>")
        body = "\n".join(f"- {mention(x)}" for x in ids) if ids else "لا يوجد."
        await interaction.response.send_message(f"**{title} ({len(ids)})**\n{body}"[:1900],
                                                ephemeral=True, allowed_mentions=NO_MENTIONS)
    return handler


async def h_region(interaction: discord.Interaction):
    r = await ctx(interaction)
    if not r:
        return
    _, ch = r
    view = discord.ui.View(timeout=120)
    select = discord.ui.Select(placeholder="اختر منطقة الاتصال",
                               options=[discord.SelectOption(label=l, value=v) for l, v in REGIONS])

    async def on_select(i: discord.Interaction):
        r2 = await ctx(i)
        if not r2:
            return
        await i.response.defer()
        value = select.values[0]
        try:
            await asyncio.wait_for(ch.edit(rtc_region=None if value == "automatic" else value), 12)
            text = f"تم تغيير المنطقة إلى: **{dict((v, l) for l, v in REGIONS)[value]}**"
        except Exception as e:
            text = f"تعذر تغيير المنطقة: {e}"
        await i.edit_original_response(content=text, view=None)

    select.callback = on_select
    view.add_item(select)
    await interaction.response.send_message("اختر المنطقة:", view=view, ephemeral=True)


HANDLERS: dict[str, Callable[[discord.Interaction], Awaitable[None]]] = {
    "lock": perm_button(changes=dict(connect=False), msg="تم قفل الروم، ما أحد يقدر يدخل إلا الموثوقين والمدعوين."),
    "unlock": perm_button(changes=dict(connect=None), msg="تم فتح الروم للجميع."),
    "hide": perm_button(changes=dict(view_channel=False), msg="تم إخفاء الروم عن الجميع عدا المالك والموثوقين والمدعوين."),
    "show": perm_button(changes=dict(view_channel=None), msg="تم إظهار الروم للجميع."),
    "rename": h_rename,
    "limit": h_limit,
    "region": h_region,
    "status": h_status,
    "kick": picker("user", "اختر العضو المراد طرده من الروم", act_kick, 5),
    "ban": picker("user", "اختر العضو المراد حظره", act_ban, 5),
    "unban": picker("user", "اختر العضو المراد فك حظره", act_unban, 5),
    "invite": picker("user", "اختر العضو المراد دعوته", act_invite, 5),
    "trust": picker("user", "اختر العضو المراد توثيقه", act_trust, 5),
    "untrust": picker("user", "اختر العضو المراد إلغاء توثيقه", act_untrust, 5),
    "trusted": list_button("trusted", "الموثوقون"),
    "banned": list_button("banned", "المحظورون"),
    "allow_role": picker("role", "اختر الرتبة المراد السماح لها", act_allow_role, 5),
    "remove_role": picker("role", "اختر الرتبة المراد إزالتها", act_remove_role, 5),
    "roles": list_button("roles", "الرتب المسموحة"),
    "owner": picker("user", "اختر العضو (داخل الروم) لنقل الملكية إليه", act_owner, 1),
}

# ============================================================ إضافة: بوت الإيموجيات والستكرات (أمر اضف)
# الصق هذا القسم كاملاً قبل قسم "التشغيل" (قبل: if __name__ == "__main__":)
# لا يعدّل أي سطر من كودك الأصلي، يربط نفسه بالبوت من الخارج فقط.
# يحتاج: pip install aiohttp  (موجودة عندك أصلاً لأنك تستخدم aiohttp.web)
#
# المميزات:
#  - يتجاهل أي إيموجي/ستكر موجود في السيرفر مسبقاً (بنفس الاسم) ويكمل إضافة الناقص فقط
#  - لو ديسكورد حط Rate Limit ينتظر ويكمل لحاله بعد ما يخلص، وتظهر مدة الانتظار في رسالة الحالة
#  - خيار حد للدفعة (حد=40): يوقف بعد 40 إضافة، وتعيد نفس الأمر لاحقاً فيكمل الباقي

import io
import ipaddress
import socket
import struct
import zipfile
import zlib
from dataclasses import field as eb_field
from urllib.parse import unquote, urljoin, urlparse

import aiohttp
from discord import app_commands

# ---------- إعدادات الإضافة ----------
EB_GUILD_ID = int(os.getenv("GUILD_ID", "0")) or None     # اختياري: مزامنة فورية لأمر السلاش في سيرفر واحد
EB_EDIT_INTERVAL = 4.0                  # كل كم ثانية نحدّث رسالة الحالة (لتجنب Rate Limit)
EB_ADD_DELAY = 0.6                      # تأخير بسيط بين كل إضافة وأخرى
EB_MAX_RATE_RETRIES = 50                # كم مرة ينتظر ويعيد المحاولة لنفس العنصر عند الـ Rate Limit
EB_MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
EB_MAX_UNZIPPED_TOTAL = 300 * 1024 * 1024   # حماية من zip bomb
EB_MAX_ITEMS = 400
EB_EMOJI_MAX_BYTES = 256 * 1024
EB_STICKER_MAX_BYTES = 512 * 1024
EB_STICKER_MIN_DIM = 300                # صورة بهذا الحجم (بكسل) أو أكبر تُعتبر ستكر في الوضع التلقائي
EB_STICKER_TAG = "slight_smile"         # الإيموجي المرتبط بالستكر (مطلوب من ديسكورد)

EB_EMOJI_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
EB_STICKER_EXTS = {".png", ".apng", ".gif", ".json"}    # json = Lottie
EB_ALL_EXTS = EB_EMOJI_EXTS | EB_STICKER_EXTS
EB_STICKER_HINTS = ("sticker", "ستكر", "ستيكر", "ملصق")
EB_EMOJI_HINTS = ("emoji", "emote", "ايموجي", "إيموجي", "ايموجى")
EB_COMMAND_WORDS = {"اضف", "!اضف"}      # كلمة الأمر في الرسائل (بدون بادئة مثل بقية أوامرك)
EB_KIND_WORDS = {
    "ايموجي": "emoji", "إيموجي": "emoji", "emoji": "emoji",
    "ستكر": "sticker", "ستيكر": "sticker", "sticker": "sticker",
}
EB_LIMIT_RE = re.compile(r"^(?:حد|limit)[=:](\d+)$", re.IGNORECASE)   # مثال: حد=40
EB_LOCKS: dict = {}
EB_RATE = {"until": 0.0}                # وقت انتهاء آخر Rate Limit (يُحدَّث تلقائياً)


# ---------- مراقبة رسائل الـ Rate Limit من مكتبة discord.py (لعرض مدة الانتظار) ----------
class _EBRateHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = record.getMessage()
            if "rate limited" in msg and ("/emojis" in msg or "/stickers" in msg):
                m = re.search(r"Retrying in ([\d.]+) seconds", msg)
                if m:
                    EB_RATE["until"] = time.time() + float(m.group(1))
        except Exception:
            pass


logging.getLogger("discord.http").addHandler(_EBRateHandler(level=logging.WARNING))


# ---------- هياكل البيانات ----------
@dataclass
class EBItem:
    path: str
    data: bytes
    size: int
    ext: str
    kind: str                       # emoji / sticker
    error: Optional[str] = None
    name: str = ""

    @property
    def animated(self) -> bool:
        return self.ext == ".gif"


@dataclass
class EBStats:
    total_emoji: int = 0
    total_sticker: int = 0
    ok_emoji: int = 0
    ok_sticker: int = 0
    skipped: int = 0                # موجود مسبقاً في السيرفر (تم تجاهله)
    deferred: int = 0               # لم يُعالج بسبب حد الدفعة
    ignored: int = 0                # ملفات غير مدعومة داخل الأرشيف
    failures: list = eb_field(default_factory=list)     # (الاسم, النوع, السبب)
    current: str = "—"
    started: float = eb_field(default_factory=time.monotonic)
    finished: Optional[float] = None

    @property
    def total(self): return self.total_emoji + self.total_sticker
    @property
    def ok(self): return self.ok_emoji + self.ok_sticker
    @property
    def failed(self): return len(self.failures)
    @property
    def done(self): return self.ok + self.failed + self.skipped + self.deferred
    @property
    def elapsed(self): return (self.finished or time.monotonic()) - self.started


# ---------- أدوات مساعدة ----------
def eb_fmt_time(seconds: float) -> str:
    s = int(seconds)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}س {m}د {s}ث" if h else f"{m}د {s}ث"


def eb_bar(done: int, total: int, width: int = 12) -> str:
    if total <= 0:
        return "▱" * width
    filled = round(width * done / total)
    return "▰" * filled + "▱" * (width - filled)


def eb_clean_name(raw: str, kind: str, taken: set) -> str:
    """تنظيف الاسم ليناسب شروط ديسكورد. الاسم ثابت لنفس الملف عشان يتعرف على الموجود مسبقاً."""
    stem = os.path.splitext(os.path.basename(raw))[0]
    if kind == "emoji":
        name = re.sub(r"[^A-Za-z0-9_]+", "_", stem)     # الإيموجي: إنجليزي وأرقام و _ فقط (2-32)
        max_len = 32
    else:
        name = re.sub(r"[^\w]+", "_", stem, flags=re.UNICODE)   # الستكر: يدعم العربي (2-30)
        max_len = 30
    name = re.sub(r"_+", "_", name).strip("_")
    if len(name) < 2:
        # اسم غير صالح (مثلاً عربي للإيموجي): نولّد اسماً ثابتاً من اسم الملف الأصلي
        name = f"{kind}_{zlib.crc32(stem.encode('utf-8')):08x}"
    name = name[:max_len]
    base, i = name, 2
    while name.lower() in taken:          # تكرار داخل نفس الطلب فقط
        suffix = f"_{i}"
        name = base[:max_len - len(suffix)] + suffix
        i += 1
    taken.add(name.lower())
    return name


def eb_image_size(data: bytes):
    """قراءة أبعاد PNG / GIF من الهيدر بدون مكتبات إضافية."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    return None


def eb_is_junk(parts: list) -> bool:
    """تجاهل ملفات النظام والمجلدات المخفية."""
    for p in parts:
        if p == "__MACOSX" or p.startswith(".") or p.lower() in ("thumbs.db", "desktop.ini"):
            return True
    return False


def eb_decide_kind(path: str, ext: str, size: int, data: bytes, forced: Optional[str]):
    """يرجع (النوع, سبب_الخطأ أو None)."""
    if ext == ".json":
        if forced == "emoji":
            return "sticker", "ملفات Lottie (JSON) تُضاف كستكر فقط"
        return "sticker", None
    if forced == "sticker":
        if ext not in EB_STICKER_EXTS:
            return "sticker", f"صيغة {ext} غير مدعومة للستكرات"
        return "sticker", None
    if forced == "emoji":
        return "emoji", None

    folders = " ".join(p.lower() for p in re.split(r"[\\/]", path)[:-1])
    if any(k in folders for k in EB_STICKER_HINTS) and ext in EB_STICKER_EXTS:
        return "sticker", None
    if any(k in folders for k in EB_EMOJI_HINTS):
        return "emoji", None
    if ext in (".jpg", ".jpeg", ".webp"):
        return "emoji", None
    if size > EB_EMOJI_MAX_BYTES:
        return "sticker", None
    dims = eb_image_size(data) if data else None
    if dims and max(dims) >= EB_STICKER_MIN_DIM:
        return "sticker", None
    return "emoji", None


def eb_make_item(path: str, size: int, data: bytes, forced: Optional[str]) -> Optional[EBItem]:
    ext = os.path.splitext(path)[1].lower()
    if ext not in EB_ALL_EXTS:
        return None
    kind, err = eb_decide_kind(path, ext, size, data, forced)
    if not err:
        limit = EB_EMOJI_MAX_BYTES if kind == "emoji" else EB_STICKER_MAX_BYTES
        if size > limit:
            err = f"الحجم {size // 1024}KB أكبر من الحد المسموح ({limit // 1024}KB)"
    return EBItem(path=path, data=data, size=size, ext=ext, kind=kind, error=err)


# ---------- التحميل واستخراج الملفات ----------
async def eb_ensure_public_host(url: str):
    """حماية: منع الروابط التي تشير لعناوين داخلية (SSRF)."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("الرابط غير صالح")
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(
        p.hostname, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM)
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ValueError("الرابط يشير إلى عنوان داخلي غير مسموح")


async def eb_download(url: str, limit: int):
    """تحميل ملف من رابط (مع التحويلات) بحد أقصى للحجم. يرجع (الرابط_النهائي, البيانات)."""
    timeout = aiohttp.ClientTimeout(total=180)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for _ in range(5):
            await eb_ensure_public_host(url)
            async with session.get(url, allow_redirects=False) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("Location")
                    if not loc:
                        raise ValueError("تحويل غير صالح في الرابط")
                    url = urljoin(url, loc)
                    continue
                if resp.status != 200:
                    raise ValueError(f"فشل تحميل الرابط (HTTP {resp.status})")
                if resp.content_length and resp.content_length > limit:
                    raise ValueError("حجم الملف أكبر من الحد المسموح")
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    buf.extend(chunk)
                    if len(buf) > limit:
                        raise ValueError("حجم الملف أكبر من الحد المسموح")
                return url, bytes(buf)
        raise ValueError("تحويلات كثيرة في الرابط")


def eb_extract_zip(raw: bytes, forced: Optional[str]):
    """استخراج الملفات الصالحة من zip. يرجع (items, ignored)."""
    items, ignored, total = [], 0, 0
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ValueError("الملف ليس أرشيف zip صالح")
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            path = info.filename.replace("\\", "/")
            if eb_is_junk(path.split("/")):
                continue
            ext = os.path.splitext(path)[1].lower()
            if ext not in EB_ALL_EXTS:
                ignored += 1
                continue
            total += info.file_size
            if total > EB_MAX_UNZIPPED_TOTAL:
                break
            data = b""
            if info.file_size <= EB_STICKER_MAX_BYTES:     # لا نقرأ الملفات الضخمة
                try:
                    data = zf.read(info)
                except Exception:
                    items.append(EBItem(path, b"", info.file_size, ext, "emoji",
                                        error="تعذر قراءة الملف (قد يكون مشفراً)"))
                    continue
            it = eb_make_item(path, info.file_size, data, forced)
            if it:
                items.append(it)
    return items, ignored


async def eb_collect(attachments, url: Optional[str], forced: Optional[str]):
    """يجمع كل العناصر من المرفقات والرابط. يرجع (items, ignored)."""
    items, ignored = [], 0

    async def from_raw(name: str, raw: bytes):
        nonlocal ignored
        if raw[:4] == b"PK\x03\x04":                     # أرشيف zip
            got, ign = await asyncio.to_thread(eb_extract_zip, raw, forced)
            items.extend(got)
            ignored += ign
            return
        it = eb_make_item(name, len(raw), raw, forced)
        if it:
            items.append(it)
        else:
            ignored += 1

    for att in attachments:
        ext = os.path.splitext(att.filename)[1].lower()
        if ext != ".zip" and att.size > EB_STICKER_MAX_BYTES:    # ملف مفرد كبير: نرفضه بدون تحميله
            it = eb_make_item(att.filename, att.size, b"", forced)
            if it:
                items.append(it)
            else:
                ignored += 1
            continue
        if ext == ".zip" and att.size > EB_MAX_ARCHIVE_BYTES:
            raise ValueError(f"الأرشيف {att.filename} كبير جداً")
        await from_raw(att.filename, await att.read())

    if url:
        final_url, raw = await eb_download(url, EB_MAX_ARCHIVE_BYTES)
        fname = unquote(os.path.basename(urlparse(final_url).path)) or "file"
        before = len(items)
        await from_raw(fname, raw)
        if len(items) == before:
            raise ValueError("الرابط لا يحتوي zip ولا ملفات بصيغة مدعومة")
    return items, ignored


# ---------- الإيمبدات ----------
def eb_accepted_embed(author) -> discord.Embed:
    return discord.Embed(
        title="✅ تم قبول الطلب",
        description=f"بدأت معالجة طلب {author.mention}...\nجاري تحميل الملفات وتجهيزها.",
        color=discord.Color.green())


def eb_error_embed(text: str) -> discord.Embed:
    return discord.Embed(title="❌ تعذر تنفيذ الطلب", description=text, color=discord.Color.red())


def eb_status_embed(stats: EBStats, final: bool = False) -> discord.Embed:
    if final:
        if stats.deferred:
            color, title = discord.Color.gold(), "⏸️ توقف عند حد الدفعة"
        elif stats.ok == 0 and stats.failed == 0 and stats.skipped:
            color, title = discord.Color.blurple(), "ℹ️ كل العناصر موجودة مسبقاً في السيرفر"
        elif stats.ok == 0:
            color, title = discord.Color.red(), "❌ انتهت العملية بدون إضافة أي عنصر"
        elif stats.failed:
            color, title = discord.Color.orange(), "⚠️ اكتملت العملية مع بعض الأخطاء"
        else:
            color, title = discord.Color.green(), "🎉 اكتملت العملية بنجاح"
    else:
        color, title = discord.Color.blurple(), "⏳ جاري الإضافة..."
    e = discord.Embed(title=title, color=color)
    e.add_field(name="📦 المطلوب إضافته",
                value=f"😀 إيموجيات: **{stats.total_emoji}**\n🏷️ ستكرات: **{stats.total_sticker}**\n"
                      f"المجموع: **{stats.total}**", inline=True)
    e.add_field(name="✅ تمت إضافته",
                value=f"😀 **{stats.ok_emoji}**  |  🏷️ **{stats.ok_sticker}**\nالمجموع: **{stats.ok}**",
                inline=True)
    e.add_field(name="❌ فشل", value=f"**{stats.failed}**", inline=True)
    if stats.skipped:
        e.add_field(name="⏭️ موجود مسبقاً (تم تجاهله)", value=f"**{stats.skipped}**", inline=True)
    if stats.deferred:
        e.add_field(name="⏸️ متبقي بعد الحد", value=f"**{stats.deferred}**", inline=True)
    e.add_field(name="⏱️ الوقت الإجمالي" if final else "⏱️ الوقت المستغرق",
                value=eb_fmt_time(stats.elapsed), inline=True)
    wait_left = EB_RATE["until"] - time.time()
    if not final and wait_left > 1:
        e.add_field(name="⏳ ديسكورد حط Rate Limit",
                    value=f"باقي تقريباً **{eb_fmt_time(wait_left)}** وبعدها يكمل تلقائياً بدون ما تسوي شي.",
                    inline=False)
    e.add_field(name="التقدم", value=f"{eb_bar(stats.done, stats.total)}  **{stats.done}/{stats.total}**",
                inline=False)
    if not final:
        e.set_footer(text=f"الآن: {stats.current}")
    elif stats.deferred:
        e.set_footer(text="أعد نفس الأمر لإكمال الباقي، الموجود مسبقاً يُتجاهل تلقائياً.")
    elif stats.ignored:
        e.set_footer(text=f"تم تجاهل {stats.ignored} ملف غير مدعوم داخل الأرشيف")
    return e


# ---------- منطق الإضافة ----------
def eb_explain_http_error(e: discord.HTTPException):
    """يرجع (السبب, نوع_الامتلاء أو None)."""
    code = getattr(e, "code", 0)
    if code == 30008:
        return "امتلأت خانات الإيموجي في السيرفر", "emoji"
    if code == 30039:
        return "امتلأت خانات الستكرات في السيرفر", "sticker"
    if code == 50035:
        return f"ملف أو اسم غير صالح: {str(e.text)[:90]}", None
    if e.status == 413:
        return "حجم الملف أكبر من المسموح", None
    if e.status == 429:
        return "استمر الـ Rate Limit من ديسكورد لفترة طويلة جداً", None
    return f"خطأ من ديسكورد ({e.status}): {str(e.text)[:90]}", None


def eb_retry_after(e: discord.HTTPException) -> float:
    """مدة الانتظار المطلوبة من ديسكورد (بالثواني)."""
    try:
        return max(1.0, float(e.response.headers.get("Retry-After", 60)))
    except Exception:
        return 60.0


async def eb_create_one(guild: discord.Guild, it: EBItem, reason: str):
    if it.kind == "emoji":
        await guild.create_custom_emoji(name=it.name, image=it.data, reason=reason)
    else:
        await guild.create_sticker(
            name=it.name, description=it.name, emoji=EB_STICKER_TAG,
            file=discord.File(io.BytesIO(it.data), filename=f"{it.name}{it.ext}"), reason=reason)


async def eb_add_items(guild: discord.Guild, items: list, stats: EBStats, reason: str, limit: int = 0):
    emojis = await guild.fetch_emojis()
    stickers = await guild.fetch_stickers()
    used = {"static": sum(1 for e in emojis if not e.animated),
            "anim": sum(1 for e in emojis if e.animated),
            "sticker": len(stickers)}
    existing = {"emoji": {e.name.lower() for e in emojis},
                "sticker": {s.name.lower() for s in stickers}}

    # نثبّت اسم كل عنصر قبل البدء (نفس الملف = نفس الاسم دائماً) عشان يتعرف على الموجود مسبقاً
    batch_names = {"emoji": set(), "sticker": set()}
    for it in items:
        it.name = eb_clean_name(it.path, it.kind, batch_names[it.kind])

    def fail(it: EBItem, why: str):
        stats.failures.append((it.name or it.path, it.kind, why))

    added = 0
    for it in items:
        stats.current = it.name

        # 1) موجود في السيرفر مسبقاً؟ نتجاهله ونكمل
        if it.name.lower() in existing[it.kind]:
            stats.skipped += 1
            continue
        # 2) مرفوض مسبقاً (حجم / صيغة)
        if it.error:
            fail(it, it.error)
            continue
        # 3) وصلنا حد الدفعة؟ نؤجل الباقي
        if limit and added >= limit:
            stats.deferred += 1
            continue
        # 4) فحص الخانات
        if it.kind == "emoji":
            slot = "anim" if it.animated else "static"
            if used[slot] >= guild.emoji_limit:
                fail(it, "خانات الإيموجي المتحركة ممتلئة" if it.animated else "خانات الإيموجي الثابتة ممتلئة")
                continue
        else:
            slot = "sticker"
            if used[slot] >= guild.sticker_limit:
                fail(it, "خانات الستكرات ممتلئة")
                continue

        # 5) الإضافة، ولو جاء Rate Limit ننتظر ونعيد المحاولة لنفس العنصر
        created = False
        for attempt in range(EB_MAX_RATE_RETRIES + 1):
            try:
                await eb_create_one(guild, it, reason)
                created = True
                break
            except discord.Forbidden:
                fail(it, "لا أملك صلاحية Manage Emojis and Stickers")
                break
            except discord.HTTPException as e:
                if e.status == 429 and attempt < EB_MAX_RATE_RETRIES:
                    wait = eb_retry_after(e)
                    EB_RATE["until"] = time.time() + wait
                    await asyncio.sleep(wait + 1)
                    continue
                why, full = eb_explain_http_error(e)
                if full == "emoji":
                    used["static"] = used["anim"] = guild.emoji_limit
                elif full == "sticker":
                    used["sticker"] = guild.sticker_limit
                fail(it, why)
                break
            except Exception as e:      # أي خطأ غير متوقع لا يوقف بقية الملفات
                fail(it, f"خطأ غير متوقع: {type(e).__name__}")
                break

        if created:
            used[slot] += 1
            existing[it.kind].add(it.name.lower())
            added += 1
            if it.kind == "emoji":
                stats.ok_emoji += 1
            else:
                stats.ok_sticker += 1
        await asyncio.sleep(EB_ADD_DELAY)


async def eb_live_updater(msg: discord.Message, stats: EBStats):
    """يحدّث رسالة الحالة كل بضع ثوانٍ بدل كل عنصر لتجنب Rate Limit."""
    try:
        while True:
            await asyncio.sleep(EB_EDIT_INTERVAL)
            try:
                await msg.edit(embed=eb_status_embed(stats))
            except discord.HTTPException:
                pass
    except asyncio.CancelledError:
        pass


async def eb_run_job(channel, guild, author, attachments, url, forced, limit: int = 0):
    lock = EB_LOCKS.setdefault(guild.id, asyncio.Lock())
    if lock.locked():
        return await channel.send(embed=eb_error_embed("يوجد طلب قيد التنفيذ في هذا السيرفر، انتظر حتى ينتهي."))
    if not attachments and not url:
        return await channel.send(embed=eb_error_embed(
            "أرسل ملف `.zip` أو رابطاً مباشراً له، أو أرفق صور الإيموجيات/الستكرات مع الأمر."))
    perms = guild.me.guild_permissions
    if not (perms.manage_emojis_and_stickers or getattr(perms, "create_expressions", False)):
        return await channel.send(embed=eb_error_embed("البوت يحتاج صلاحية **Manage Expressions / Emojis and Stickers**."))

    async with lock:
        await channel.send(embed=eb_accepted_embed(author))
        try:
            items, ignored = await eb_collect(attachments, url, forced)
        except ValueError as e:
            return await channel.send(embed=eb_error_embed(str(e)))
        except Exception as e:
            return await channel.send(embed=eb_error_embed(f"تعذر قراءة الملفات: {type(e).__name__}"))
        if not items:
            return await channel.send(embed=eb_error_embed("لم أجد أي ملفات صالحة (PNG / JPG / WEBP / GIF / JSON)."))
        items = items[:EB_MAX_ITEMS]

        stats = EBStats(ignored=ignored)
        stats.total_emoji = sum(1 for i in items if i.kind == "emoji")
        stats.total_sticker = len(items) - stats.total_emoji

        status_msg = await channel.send(embed=eb_status_embed(stats))
        updater = asyncio.create_task(eb_live_updater(status_msg, stats))
        try:
            await eb_add_items(guild, items, stats, reason=f"إضافة جماعية بواسطة {author} ({author.id})",
                               limit=limit)
        finally:
            stats.finished = time.monotonic()
            updater.cancel()

        final = eb_status_embed(stats, final=True)
        try:
            await status_msg.edit(embed=final)
        except discord.HTTPException:
            pass
        kwargs = {}
        if stats.failures:
            text = "\n".join(f"[{k}] {n} — {r}" for n, k, r in stats.failures)
            preview = "\n".join(text.split("\n")[:10])[:1000]
            final.add_field(name="أسباب الفشل (أول 10)", value=f"```{preview}```", inline=False)
            if stats.failed > 10:
                kwargs["file"] = discord.File(io.BytesIO(text.encode("utf-8")), filename="failures.txt")
        await channel.send(content=author.mention, embed=final, **kwargs)


# ---------- ربط الأمر بالبوت (من الخارج، بدون تعديل كلاس TempVoiceBot) ----------
eb_tree = app_commands.CommandTree(bot)


@eb_tree.command(name="اضف", description="إضافة إيموجيات وستكرات دفعة واحدة من zip أو رابط أو صور")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_emojis_and_stickers=True)
@app_commands.describe(file="ملف zip أو صورة", file2="صورة إضافية", file3="صورة إضافية",
                       file4="صورة إضافية", file5="صورة إضافية",
                       url="رابط مباشر لملف zip", kind="تحديد النوع (الافتراضي: تلقائي)",
                       limit="حد الدفعة: يوقف بعد هذا العدد من الإضافات (الافتراضي: بدون حد)")
@app_commands.rename(file="ملف", file2="ملف2", file3="ملف3", file4="ملف4", file5="ملف5",
                     url="رابط", kind="النوع", limit="حد")
@app_commands.choices(kind=[
    app_commands.Choice(name="تلقائي", value="auto"),
    app_commands.Choice(name="ايموجي", value="emoji"),
    app_commands.Choice(name="ستكر", value="sticker"),
])
async def eb_slash_add(interaction: discord.Interaction,
                       file: Optional[discord.Attachment] = None,
                       file2: Optional[discord.Attachment] = None,
                       file3: Optional[discord.Attachment] = None,
                       file4: Optional[discord.Attachment] = None,
                       file5: Optional[discord.Attachment] = None,
                       url: Optional[str] = None,
                       kind: Optional[app_commands.Choice[str]] = None,
                       limit: Optional[app_commands.Range[int, 1, 400]] = None):
    attachments = [a for a in (file, file2, file3, file4, file5) if a]
    forced = kind.value if kind and kind.value != "auto" else None
    await interaction.response.send_message("📨 استلمت طلبك، تابع الرسائل في الروم...", ephemeral=True)
    await eb_run_job(interaction.channel, interaction.guild, interaction.user, attachments, url, forced,
                     limit or 0)


@eb_tree.error
async def eb_slash_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "تحتاج صلاحية **Manage Expressions / Emojis and Stickers** لاستخدام هذا الأمر."
    else:
        msg = f"حدث خطأ: {type(error).__name__}"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# مزامنة أمر السلاش عند التشغيل (نلف setup_hook الأصلي دون تعديله)
_eb_orig_setup_hook = bot.setup_hook


async def _eb_setup_hook():
    await _eb_orig_setup_hook()
    try:
        if EB_GUILD_ID:
            eb_tree.copy_global_to(guild=discord.Object(id=EB_GUILD_ID))
            await eb_tree.sync(guild=discord.Object(id=EB_GUILD_ID))
        else:
            await eb_tree.sync()
    except discord.HTTPException as e:
        log.warning("slash sync failed: %s", e)


bot.setup_hook = _eb_setup_hook


# الأمر النصي بدون بادئة: "اضف" مع مرفقات / رابط / ايموجي / ستكر / حد=40  (نفس أسلوب أوامرك)
_eb_orig_on_message = bot.on_message


async def _eb_on_message(message: discord.Message):
    if message.guild and not message.author.bot:
        tokens = message.content.split()
        if tokens and tokens[0] in EB_COMMAND_WORDS:
            perms = message.author.guild_permissions
            if perms.manage_emojis_and_stickers or getattr(perms, "create_expressions", False):
                url, forced, limit = None, None, 0
                for t in tokens[1:]:
                    m = EB_LIMIT_RE.match(t)
                    if t.lower().startswith(("http://", "https://")):
                        url = t.strip("<>")
                    elif t.lower() in EB_KIND_WORDS:
                        forced = EB_KIND_WORDS[t.lower()]
                    elif m:
                        limit = int(m.group(1))
                asyncio.create_task(eb_run_job(message.channel, message.guild, message.author,
                                               list(message.attachments), url, forced, limit))
                return      # تم التعامل معها، لا نمررها لبقية أوامر التمب فويس
    await _eb_orig_on_message(message)


bot.on_message = _eb_on_message
# ============================================================ نهاية إضافة الإيموجيات والستكرات


# ============================================================ إضافة: أمر جلب آيديات الأعضاء (ايديات)
# الاستخدام: ايديات <آيدي السيرفر>   (أو بدون آيدي داخل السيرفر نفسه)
# يرسل آيدي كل الأعضاء بدون البوتات، آيدي في كل سطر داخل بلوك كود، مقسّمة على عدة رسائل.
# يحتاج Server Members Intent مفعّل في الـ Developer Portal (وهو مفعّل في الكود أصلاً: intents.members)

IDS_COMMAND_WORDS = {"ايديات", "!ايديات"}
IDS_CHUNK_LIMIT = 1900      # أقصى حروف للآيديات في الرسالة الواحدة (حد ديسكورد 2000 بعد الغلاف)


def ids_build_chunks(ids: list, limit: int = IDS_CHUNK_LIMIT) -> list:
    """يقسّم الآيديات على رسائل بدون ما يشق أي آيدي نصفين."""
    chunks, cur, cur_len = [], [], 0
    for uid in ids:
        add = len(uid) + 1
        if cur and cur_len + add > limit:
            chunks.append("\n".join(cur))
            cur, cur_len = [], 0
        cur.append(uid)
        cur_len += add
    if cur:
        chunks.append("\n".join(cur))
    return chunks


async def ids_can_use(user, guild: discord.Guild) -> bool:
    """الأمر لمالك السيرفر أو أصحاب Administrator في السيرفر المطلوب فقط."""
    if user.id == guild.owner_id:
        return True
    member = guild.get_member(user.id)
    if member is None:
        try:
            member = await guild.fetch_member(user.id)
        except discord.HTTPException:
            return False
    return member.guild_permissions.administrator


async def ids_run(message: discord.Message, args: list):
    channel = message.channel

    async def say(text: str):
        try:
            await channel.send(text)
        except discord.HTTPException:
            pass

    # تحديد السيرفر المطلوب
    if args:
        if not args[0].isdigit():
            return await say("الاستخدام: `ايديات <آيدي السيرفر>`")
        guild = bot.get_guild(int(args[0]))
    else:
        guild = message.guild
        if guild is None:
            return await say("الاستخدام: `ايديات <آيدي السيرفر>`")
    if guild is None:
        return await say("البوت غير موجود في هذا السيرفر، أو الآيدي غير صحيح.")
    if not await ids_can_use(message.author, guild):
        return await say("ما عندك صلاحية، لازم تكون Administrator في السيرفر المطلوب.")

    # جلب كل الأعضاء مباشرة من ديسكورد (مو من الكاش) عشان ما ينقص أحد
    await say(f"⏳ جاري جلب أعضاء **{guild.name}**...")
    ids, total = [], 0
    try:
        async for m in guild.fetch_members(limit=None):
            total += 1
            if not m.bot:
                ids.append(str(m.id))
    except Exception as e:
        return await say(f"تعذر جلب الأعضاء ({type(e).__name__}). تأكد أن **Server Members Intent** مفعّل في Developer Portal.")

    ids = list(dict.fromkeys(ids))      # حذف أي تكرار مع الحفاظ على الترتيب
    if not ids:
        return await say("ما لقيت أعضاء (غير البوتات) في هذا السيرفر.")

    chunks = ids_build_chunks(ids)
    n = len(chunks)
    head = f"👥 **{guild.name}**\nعدد الأعضاء (بدون البوتات): **{len(ids)}**\nسيتم الإرسال في **{n}** رسالة."
    if guild.member_count and total < guild.member_count:
        head += f"\n⚠️ ديسكورد أعاد {total} من أصل {guild.member_count} عضو، قد ينقص بعضهم."
    await say(head)
    for i, chunk in enumerate(chunks, 1):
        await say(f"**({i}/{n})**\n```\n{chunk}\n```")
        await asyncio.sleep(0.7)
    await say(f"✅ تم إرسال **{len(ids)}** آيدي كاملة.")


# ربط الأمر بالبوت من الخارج (نلف on_message الحالي دون تعديله)
_ids_prev_on_message = bot.on_message


async def _ids_on_message(message: discord.Message):
    if not message.author.bot:
        tokens = message.content.split()
        if tokens and tokens[0] in IDS_COMMAND_WORDS:
            asyncio.create_task(ids_run(message, tokens[1:]))
            return
    await _ids_prev_on_message(message)


bot.on_message = _ids_on_message
# ============================================================ نهاية إضافة أمر ايديات


# ============================================================ إضافة: كشف المكلبة (ModAbuse)
# الصق هذا القسم كاملاً قبل قسم "التشغيل" (قبل: if __name__ == "__main__":)
# لا يعدّل أي سطر من كودك الأصلي، يربط نفسه بالبوت من الخارج فقط.
# ملاحظة: الكود الأصلي كان Cog (يحتاج commands.Bot) وبوتك discord.Client،
# فالمنطق نفسه بدون أي تغيير، والتغيير الوحيد هو طريقة الربط بالأحداث (في الأسفل).
# يحتاج: صلاحية View Audit Log + Manage Roles + Mute/Deafen Members للبوت، ورتبة البوت أعلى من الرولات المسحوبة.

import json
from datetime import timedelta

# ================= إعدادات كشف المكلبة =================
MOD_ABUSE_LOG_CHANNEL_ID = 1550546384787087410   # <<< ايدي روم اللوج
# كل واحد منها يقبل آيدي واحد أو أكثر بين قوسين وفواصل، مثال: MUTE_ROLE_ID = [111111111111111111, 222222222222222222]
MOVE_ROLE_ID = 1460374666748105036               # <<< ايدي رول السحب
MUTE_ROLE_ID = [                # <<< ايدي رول الميوت
    1487545475707044052,
    1556615926059892838,
    1511160154085658805,
    1507815334395121865,
    1543621789433733120,
    1465057908554334220,
    1563995047278674000,
    1563994711864377567,
    1563994354937233449,
]
DEAFEN_ROLE_ID = 1487545475707044052             # <<< ايدي رول الدفن (الديفن)
UNDO_VICTIM_PUNISHMENT = True  # يفك الميوت/الديفن عن الضحية تلقائياً (False لإيقافه)
STRIP_BY_PERMISSION = True     # يسحب أيضاً أي رول عند المشرف يعطي صلاحية سحب/ميوت/ديفن (حتى لو الآيدي فوق غلط أو ناقص)

MOD_ABUSE_DEFAULT_MESSAGE = "برا رولك يا مكلبجي"
MOD_ABUSE_CONFIG_FILE = "mod_abuse_config.json"
MOD_ABUSE_CHANGE_TRIGGER = "تغيير الرسالة"
MOD_ABUSE_RESTORE_TRIGGER = "نو مكلبة"
# =======================================================


def _ma_as_ids(value) -> list:
    """يقبل آيدي واحد أو قائمة آيديات ويرجع قائمة أرقام صحيحة."""
    if isinstance(value, (list, tuple, set)):
        return [int(x) for x in value if x]
    return [int(value)] if value else []


class ModAbuse:
    def __init__(self, bot):
        self.bot = bot
        self.message = MOD_ABUSE_DEFAULT_MESSAGE
        self.removed = {}  # user_id(str) -> [role ids]
        self._cooldown = {}
        self._pending = set()
        self._load()

    # ---------- حفظ وتحميل ----------
    def _load(self):
        try:
            with open(MOD_ABUSE_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            msg = data.get("message")
            if isinstance(msg, str) and msg.strip():
                self.message = msg
            removed = data.get("removed")
            if isinstance(removed, dict):
                self.removed = removed
        except Exception:
            pass

    def _save(self):
        try:
            with open(MOD_ABUSE_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {"message": self.message, "removed": self.removed},
                    f,
                    ensure_ascii=False,
                )
        except Exception as e:
            print(f"[ModAbuse] فشل الحفظ: {e}")

    # ---------- البحث عن المشرف اللي نفّذ الميوت/الديفن ----------
    async def _find_executor(self, guild, member, muted, deafened):
        for _ in range(4):
            try:
                async for entry in guild.audit_logs(
                    limit=15, action=discord.AuditLogAction.member_update
                ):
                    if entry.target is None or entry.target.id != member.id:
                        continue
                    if discord.utils.utcnow() - entry.created_at > timedelta(seconds=15):
                        continue
                    after = entry.after
                    if (muted and getattr(after, "mute", None) is True) or (
                        deafened and getattr(after, "deaf", None) is True
                    ):
                        return entry.user
            except discord.Forbidden:
                print("[ModAbuse] البوت ما عنده صلاحية View Audit Log")
                return None
            except discord.HTTPException:
                pass
            await asyncio.sleep(1)
        return None

    # ---------- سحب الرولات من المشرف ----------
    async def _strip_roles(self, guild, executor_id):
        """يرجع (قائمة الرولات المسحوبة, نجح؟, ملاحظات للتشخيص)"""
        notes = []
        member = guild.get_member(executor_id)
        if member is None:
            try:
                member = await guild.fetch_member(executor_id)
            except discord.HTTPException:
                return [], False, ["ما قدرت أجيب المشرف من السيرفر"]

        # 1) الرولات المحددة بالآيديات
        candidates = {}
        for label, value in (("السحب", MOVE_ROLE_ID), ("الميوت", MUTE_ROLE_ID), ("الدفن", DEAFEN_ROLE_ID)):
            ids = _ma_as_ids(value)
            if not ids:
                notes.append(f"آيدي رول {label} = 0 (ما انحط في الإعدادات)")
                continue
            for rid in ids:
                role = guild.get_role(rid)
                if role is None:
                    notes.append(f"رول {label} ({rid}) غير موجود في هذا السيرفر، تأكد من الآيدي")
                    continue
                if role in member.roles:
                    candidates[role.id] = role

        # 2) أي رول عند المشرف يعطي صلاحية سحب/ميوت/ديفن (القانون يطبق على الكل)
        if STRIP_BY_PERMISSION:
            for role in member.roles:
                if role.is_default() or role.managed:
                    continue
                perms = role.permissions
                if not (perms.move_members or perms.mute_members or perms.deafen_members):
                    continue
                if perms.administrator and role.id not in candidates:
                    notes.append(f"رول {role.name} أدمن، ما أسحبه تلقائياً (حطه في الآيديات لو تبيه ينسحب)")
                    continue
                candidates[role.id] = role

        if not candidates:
            notes.append("المشرف ما عنده أي رول سحب/ميوت/ديفن يتسحب منه")
            return [], True, notes

        # 3) فحص ترتيب رتبة البوت
        me = guild.me
        to_remove = []
        ok = True
        for role in candidates.values():
            if role.managed or role.position >= me.top_role.position:
                ok = False
                notes.append(f"ما أقدر أسحب {role.name}: رتبة البوت لازم تكون أعلى منه في ترتيب الرولات")
            else:
                to_remove.append(role)
        if not to_remove:
            return [], False, notes

        try:
            await member.remove_roles(
                *to_remove, reason="مكلبة: ميوت/ديفن على شخص في روم المشرف مو موجود فيه"
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[ModAbuse] فشل سحب الرولات: {e}")
            notes.append(f"فشل السحب من ديسكورد: {type(e).__name__} (تأكد من Manage Roles ومن رتبة البوت)")
            return [], False, notes

        saved = set(self.removed.get(str(executor_id), []))
        saved.update(r.id for r in to_remove)
        self.removed[str(executor_id)] = list(saved)
        self._save()
        return to_remove, ok, notes

    # ---------- مراقبة الميوت والديفن ----------
    async def on_voice_state_update(self, member, before, after):
        if before.channel is None or after.channel is None:
            return
        if before.channel.id != after.channel.id:
            return

        muted = after.mute and not before.mute
        deafened = after.deaf and not before.deaf
        if not (muted or deafened):
            return

        channel = after.channel
        guild = member.guild
        present_ids = set(channel.voice_states.keys())

        await asyncio.sleep(1)

        executor = await self._find_executor(guild, member, muted, deafened)
        if executor is None:
            return
        if getattr(executor, "bot", False) or executor.id == member.id:
            return
        if executor.id in present_ids:
            return  # المشرف داخل نفس الروم، عادي

        # منع التكرار (ميوت + ديفن بنفس اللحظة)
        key = (executor.id, member.id)
        now = discord.utils.utcnow()
        last = self._cooldown.get(key)
        if last and (now - last) < timedelta(seconds=8):
            return
        self._cooldown[key] = now

        if muted and deafened:
            action = "ميوت + ديفن"
        elif muted:
            action = "ميوت"
        else:
            action = "ديفن"

        # 1) سحب الرولات فوراً
        removed_roles, roles_ok, strip_notes = await self._strip_roles(guild, executor.id)
        for n in strip_notes:
            print(f"[ModAbuse] {n}")

        # 2) فك العقوبة عن الضحية
        victim_fixed = False
        if UNDO_VICTIM_PUNISHMENT:
            kwargs = {}
            if muted:
                kwargs["mute"] = False
            if deafened:
                kwargs["deafen"] = False
            try:
                await member.edit(**kwargs, reason="مكلبة: فك عقوبة غير مشروعة")
                victim_fixed = True
            except (discord.Forbidden, discord.HTTPException) as e:
                print(f"[ModAbuse] فشل فك العقوبة عن الضحية: {e}")

        # 3) المنشن داخل الروم
        try:
            await channel.send(
                f"<@{executor.id}> {self.message}",
                allowed_mentions=discord.AllowedMentions(
                    users=[discord.Object(id=executor.id)],
                    roles=False,
                    everyone=False,
                ),
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[ModAbuse] فشل الإرسال في الروم: {e}")

        # 4) اللوج
        if MOD_ABUSE_LOG_CHANNEL_ID:
            log_channel = self.bot.get_channel(MOD_ABUSE_LOG_CHANNEL_ID)
            if log_channel is None:
                try:
                    log_channel = await self.bot.fetch_channel(MOD_ABUSE_LOG_CHANNEL_ID)
                except Exception:
                    log_channel = None
            if log_channel is not None:
                roles_text = (
                    "، ".join(r.mention for r in removed_roles)
                    if removed_roles
                    else ("ما عنده رولات تنسحب" if roles_ok else "فشل السحب (تأكد من صلاحيات ورتبة البوت)")
                )
                embed = discord.Embed(
                    title="🚨 مكلبة مشرف",
                    color=discord.Color.red(),
                    timestamp=now,
                )
                embed.add_field(name="المشرف", value=f"<@{executor.id}>", inline=True)
                embed.add_field(name="العضو", value=member.mention, inline=True)
                embed.add_field(name="الإجراء", value=action, inline=True)
                embed.add_field(name="الروم", value=channel.mention, inline=True)
                embed.add_field(name="الرولات المسحوبة", value=roles_text, inline=False)
                if strip_notes:
                    embed.add_field(name="ملاحظات", value="\n".join(f"• {n}" for n in strip_notes)[:1000], inline=False)
                if UNDO_VICTIM_PUNISHMENT:
                    embed.add_field(
                        name="فك العقوبة عن العضو",
                        value="تم ✅" if victim_fixed else "فشل ❌",
                        inline=True,
                    )
                try:
                    await log_channel.send(
                        embed=embed, allowed_mentions=discord.AllowedMentions.none()
                    )
                except (discord.Forbidden, discord.HTTPException) as e:
                    print(f"[ModAbuse] فشل الإرسال في اللوج: {e}")

    # ---------- الأوامر (تغيير الرسالة + نو مكلبة) ----------
    async def on_message(self, message):
        if message.author.bot or message.guild is None:
            return
        content = message.content.strip().lstrip("!.-+$?;/#").strip()

        if content == MOD_ABUSE_CHANGE_TRIGGER:
            if message.author.guild_permissions.administrator:
                await self._handle_change(message)
        elif content.startswith(MOD_ABUSE_RESTORE_TRIGGER):
            if message.author.guild_permissions.administrator:
                await self._handle_restore(message)

    async def _handle_change(self, message):
        key = (message.author.id, message.channel.id)
        if key in self._pending:
            return
        self._pending.add(key)
        try:
            await message.reply(
                "اكتب الرسالة اللي تبيها تجي مع منشن المكلبجي (عندك 60 ثانية).\n"
                "اكتب `الغاء` للإلغاء.",
                mention_author=False,
            )

            def check(m):
                return (
                    m.author.id == message.author.id
                    and m.channel.id == message.channel.id
                )

            try:
                reply = await self.bot.wait_for("message", check=check, timeout=60)
            except asyncio.TimeoutError:
                await message.channel.send("انتهى الوقت، ما تغيّرت الرسالة.")
                return

            new_msg = reply.content.strip()
            if not new_msg or new_msg == "الغاء":
                await reply.reply("تم الإلغاء.", mention_author=False)
                return
            if len(new_msg) > 1500:
                await reply.reply("الرسالة طويلة، الحد 1500 حرف.", mention_author=False)
                return

            self.message = new_msg
            self._save()
            await reply.reply(
                f"تم تغيير الرسالة ✅\n{new_msg}",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        finally:
            self._pending.discard(key)

    async def _handle_restore(self, message):
        if not message.mentions:
            await message.reply(
                "منشن الشخص مع الأمر، مثال: `نو مكلبة @الشخص`",
                mention_author=False,
            )
            return

        target = message.mentions[0]
        saved_ids = self.removed.get(str(target.id), [])
        if not saved_ids:
            await message.reply(
                "هذا الشخص ما انسحبت منه رولات من البوت.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        guild = message.guild
        member = guild.get_member(target.id)
        if member is None:
            try:
                member = await guild.fetch_member(target.id)
            except discord.HTTPException:
                await message.reply("ما لقيت الشخص في السيرفر.", mention_author=False)
                return

        roles = [guild.get_role(rid) for rid in saved_ids]
        roles = [r for r in roles if r is not None]
        try:
            await member.add_roles(*roles, reason=f"نو مكلبة بواسطة {message.author}")
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[ModAbuse] فشل إرجاع الرولات: {e}")
            await message.reply(
                "ما قدرت أرجع الرولات، تأكد إن رتبة البوت أعلى منها وعنده Manage Roles.",
                mention_author=False,
            )
            return

        self.removed.pop(str(target.id), None)
        self._save()
        await message.reply(
            f"تم إرجاع الرولات لـ {member.mention} ✅ ({'، '.join(r.name for r in roles)})",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )


# ---------- ربط المكلبة بالبوت (من الخارج، بدون تعديل كلاس TempVoiceBot) ----------
mod_abuse = ModAbuse(bot)
_ma_tasks: set = set()


def _ma_spawn(coro):
    """يشغّل مهمة المكلبة بالخلفية عشان ما تعطّل أحداث التمب فويس، ويسجّل أي خطأ."""
    task = asyncio.create_task(coro)
    _ma_tasks.add(task)

    def _done(t):
        _ma_tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.warning("[ModAbuse] error: %r", t.exception())

    task.add_done_callback(_done)


# أحداث الصوت: نلف on_voice_state_update الحالي دون تعديله
_ma_prev_on_voice_state_update = bot.on_voice_state_update


async def _ma_on_voice_state_update(member, before, after):
    _ma_spawn(mod_abuse.on_voice_state_update(member, before, after))
    await _ma_prev_on_voice_state_update(member, before, after)


bot.on_voice_state_update = _ma_on_voice_state_update


# أوامر الرسائل (تغيير الرسالة / نو مكلبة): نلف on_message الحالي دون تعديله
_ma_prev_on_message = bot.on_message


async def _ma_on_message(message: discord.Message):
    if message.guild and not message.author.bot:
        _ma_spawn(mod_abuse.on_message(message))
    await _ma_prev_on_message(message)


bot.on_message = _ma_on_message
# ============================================================ نهاية إضافة كشف المكلبة


# ============================================================ التشغيل (كل البوتات)
# كل بوت له توكن مستقل في Environment Variables بـ Render:
#   TOKEN        = توكن بوت التمب فويس (الأصلي، ويقبل DISCORD_TOKEN أيضاً)
#   TESTME_TOKEN = توكن بوت Test me
#   UNO_TOKEN    = توكن بوت Uno
#   EVENT_TOKEN  = توكن بوت الفعاليات (الاشتراكات / منشن الكل)
# أي بوت ما تحط له توكن ينتخطى بدون ما يوقف الباقي، وأي خطأ في بوت ما يأثر على غيره.
EXTRA_BOTS = ["bots.testme", "bots.uno", "bots.events"]


async def _run_client(name: str, client: discord.Client, token: str):
    """يشغّل بوت واحد. أي خطأ فيه يُسجَّل فقط ولا يوقف البوتات الثانية."""
    try:
        async with client:
            await client.start(token)
    except discord.LoginFailure:
        log.error("[%s] التوكن غير صحيح، تم تخطي هذا البوت", name)
    except asyncio.CancelledError:
        raise
    except BaseException:
        log.exception("[%s] توقف البوت بسبب خطأ", name)
    else:
        log.warning("[%s] توقف البوت", name)


async def run_all():
    await ensure_web_server()
    jobs = []

    # --- بوت التمب فويس (الرئيسي) ---
    if not TOKEN:
        log.error("[TempVoice] ما فيه توكن (TOKEN)، تم تخطي البوت")
    elif not CREATE_CHANNEL_ID:
        log.error("[TempVoice] ما فيه CREATE_CHANNEL_ID، تم تخطي البوت")
    else:
        jobs.append(_run_client("TempVoice", bot, TOKEN))

    # --- بقية البوتات (كل واحد بتوكنه، وكل واحد معزول بملفه) ---
    for modname in EXTRA_BOTS:
        try:
            mod = importlib.import_module(modname)
        except BaseException:
            log.exception("[%s] فشل تحميل الملف، تم تخطي هذا البوت", modname)
            continue
        name = getattr(mod, "BOT_NAME", modname)
        token = (os.getenv(getattr(mod, "TOKEN_ENV", "")) or "").strip()
        if not token:
            log.error("[%s] ما فيه توكن (%s)، تم تخطي البوت", name, getattr(mod, "TOKEN_ENV", "?"))
            continue
        try:
            client = mod.create_bot()
        except BaseException:
            log.exception("[%s] فشل إنشاء البوت، تم تخطي هذا البوت", name)
            continue
        jobs.append(_run_client(name, client, token))

    if not jobs:
        log.error("ما فيه أي بوت يشتغل، تأكد من التوكنات في Environment Variables")
        await asyncio.Event().wait()   # نخلي السيرفر الصغير شغال عشان تقدر تشوف اللوق وتصلح المتغيرات
    await asyncio.gather(*jobs)


if __name__ == "__main__":
    try:
        asyncio.run(run_all())
    except KeyboardInterrupt:
        pass
