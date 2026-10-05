import os
import io
import asyncio
import datetime
import traceback

import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web

# ============================================================
#  الإعدادات: بدّل الأصفار بالآيديات حقتك (التوكن يبقى في Render)
# ============================================================
TOKEN = os.getenv("DISCORD_TOKEN")                  # التوكن من Environment Variables في ريندر

OWNER_ROLE_ID = 1425199301956599888                  # آيدي رتبة الاونر (يستخدم أمر /panel)
ADMIN_ROLE_IDS = [1465057908554334220, 1425199301956599888]               # آيدي رتبة (أو رتب) ادمن تيكت. مثال لرتبتين: [111111, 222222]
PANEL_CHANNEL_ID = 1465041190163578972               # آيدي روم صورة التيكت (اللي تنرسل فيه اللوحة)
CATEGORY_ID = 1465040666282168470                    # آيدي كاتيجوري التذاكر
LOG_CHANNEL_ID = 1550546365338099803                                  # آيدي روم السجل (اتركه 0 إذا ما تبيه)
GUILD_ID = 1425195739591610451                       # آيدي السيرفر (يخلي الأوامر تظهر فوراً)

TICKET_NAME_PREFIX = ":tickets:تذكرة"                         # بداية اسم روم التذكرة (يصير الاسم: تذكرة ثم الرقم)

# ---------- الرومات الصوتية المؤقتة (Temp Voice) ----------
VOICE_CREATE_CHANNEL_ID = 1556472549280587917                         # آيدي الروم الصوتي اللي يدخله العضو عشان ينفتح له روم خاص
VOICE_PANEL_CHANNEL_ID = 1556472672207114240                          # آيدي روم لوحة التحكم (لازم يكون غير روم لوحة التذاكر)
VOICE_PANEL_IMAGE = "voice_panel.png"               # صورة اللوحة (ارفعها في جيت هوب بنفس الاسم)
VOICE_PANEL_TITLE = "Rav - Temp Voice"              # عنوان اللوحة

START_TICKET_NUMBER = 114                             # رقم أول تذكرة

# صورة اللوحة: ارفعها في جيت هوب بجانب main.py وبنفس الاسم
PANEL_IMAGE = "background.png"

# أقسام التذاكر (تقدر تغيّر الأسماء أو تضيف أقسام من هنا)
# كل قسم له صورة اختيارية داخل التذكرة: banner_1.png للقسم الأول، banner_2.png للثاني ... وهكذا
# إذا ما لقى صورة القسم يستخدم PANEL_IMAGE
SECTIONS = [
    {"label": "استفسار"},
    {"label": "طلب رول"},
    {"label": "الفعاليات"},
    {"label": "شكوى على عضو"},
    {"label": "شكوى على اداري"},
]
for _i, _s in enumerate(SECTIONS):
    _s["banner"] = f"banner_{_i + 1}.png"

PORT = int(os.getenv("PORT", "10000"))              # بورت ريندر (لا تغيره)

# بعد الاستلام: هل يقدر ادمن ثاني يستدعي/ينهي؟ (False = المستلم فقط)
ALLOW_OTHER_ADMINS_AFTER_CLAIM = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COUNTER_FILE = os.path.join(BASE_DIR, "ticket_counter.txt")

COLOR_MAIN = 0x2B2D31
COLOR_TICKET = 0xE0A526
COLOR_OPEN = 0x5865F2
COLOR_CLAIM = 0x57F287
COLOR_CLOSE = 0xED4245
COLOR_WARN = 0xFEE75C


_ticket_lock = asyncio.Lock()


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


# ============================================================
#  دوال مساعدة
# ============================================================
def is_admin(member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    return any(r.id in ADMIN_ROLE_IDS for r in member.roles)


def is_owner(member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    return any(r.id == OWNER_ROLE_ID for r in member.roles)


def read_topic(channel) -> dict:
    """بيانات التذكرة محفوظة في وصف الروم: owner=ID;claimed=ID;number=N;section=الاسم"""
    data = {"owner": None, "claimed": None, "number": None, "section": None}
    topic = getattr(channel, "topic", None)
    if not topic:
        return data
    for part in topic.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k in ("owner", "claimed", "number") and v.isdigit():
                data[k] = int(v)
            elif k == "section" and v:
                data["section"] = v
    return data


async def write_topic(channel, owner, claimed):
    info = read_topic(channel)
    topic = (
        f"owner={owner};claimed={claimed or 0};"
        f"number={info['number'] or 0};section={info['section'] or ''}"
    )
    await channel.edit(topic=topic)


def next_ticket_number(category: discord.CategoryChannel) -> int:
    last = START_TICKET_NUMBER - 1
    try:
        with open(COUNTER_FILE, "r", encoding="utf-8") as f:
            last = max(last, int(f.read().strip()))
    except (OSError, ValueError):
        pass
    for ch in category.text_channels:
        n = read_topic(ch)["number"]
        if n and n > last:
            last = n
    number = last + 1
    try:
        with open(COUNTER_FILE, "w", encoding="utf-8") as f:
            f.write(str(number))
    except OSError:
        pass
    return number


async def get_member(guild: discord.Guild, user_id: int):
    member = guild.get_member(user_id)
    if member:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound:
        return None


async def send_log(guild: discord.Guild, embed: discord.Embed, file: discord.File = None):
    if not LOG_CHANNEL_ID:
        return
    channel = guild.get_channel(LOG_CHANNEL_ID)
    if channel:
        try:
            await channel.send(embed=embed, file=file)
        except discord.HTTPException:
            pass


async def build_transcript(channel: discord.TextChannel) -> discord.File:
    lines = []
    async for m in channel.history(limit=None, oldest_first=True):
        time = m.created_at.strftime("%Y-%m-%d %H:%M")
        content = m.content or ""
        if m.attachments:
            content += " " + " ".join(a.url for a in m.attachments)
        if m.embeds and not content:
            content = "[Embed]"
        lines.append(f"[{time}] {m.author} : {content}")
    text = "\n".join(lines) or "لا توجد رسائل"
    return discord.File(io.BytesIO(text.encode("utf-8")), filename=f"transcript-{channel.name}.txt")


async def find_main_message(channel: discord.TextChannel):
    """رسالة التذكرة الأساسية (اللي فيها الإمبد والأزرار)"""
    async for m in channel.history(limit=30, oldest_first=True):
        if m.author.id == channel.guild.me.id and m.embeds:
            for f in m.embeds[0].fields:
                if "مالك التذكرة" in f.name:
                    return m
    return None


async def deny(interaction: discord.Interaction, text: str):
    embed = discord.Embed(description=f"{text}", color=COLOR_CLOSE)
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def can_manage(interaction: discord.Interaction) -> bool:
    """تحقق: ادمن تيكت + (المستلم فقط إذا فيه استلام)"""
    if not is_admin(interaction.user):
        await deny(interaction, "هذا الزر مخصص لإدارة التذاكر فقط.")
        return False
    info = read_topic(interaction.channel)
    claimed = info["claimed"]
    if claimed and not ALLOW_OTHER_ADMINS_AFTER_CLAIM and claimed != interaction.user.id:
        await deny(interaction, f"هذه التذكرة مستلمة من <@{claimed}> وهو المسؤول عنها.")
        return False
    return True


# ============================================================
#  إنشاء التذكرة (مباشرة بعد اختيار القسم)
# ============================================================
async def create_ticket(interaction: discord.Interaction, section_index: int):
    guild = interaction.guild
    user = interaction.user
    section = SECTIONS[section_index]

    category = guild.get_channel(CATEGORY_ID)
    admin_roles = [r for r in (guild.get_role(i) for i in ADMIN_ROLE_IDS) if r]
    if not isinstance(category, discord.CategoryChannel) or not admin_roles:
        return await interaction.followup.send("إعدادات البوت غير مكتملة، تواصل مع الإدارة.", ephemeral=True)

    async with _ticket_lock:
        # تذكرة واحدة مفتوحة لكل عضو
        for ch in category.text_channels:
            if read_topic(ch)["owner"] == user.id:
                return await interaction.followup.send(f"عندك تذكرة مفتوحة بالفعل: {ch.mention}", ephemeral=True)

        number = next_ticket_number(category)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                attach_files=True, embed_links=True,
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True,
                read_message_history=True, embed_links=True, attach_files=True,
            ),
        }
        for role in admin_roles:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                attach_files=True, embed_links=True,
            )

        channel = await guild.create_text_channel(
            name=f"{TICKET_NAME_PREFIX}\u30fb{number}",
            category=category,
            overwrites=overwrites,
            topic=f"owner={user.id};claimed=0;number={number};section={section['label']}",
            reason=f"تذكرة جديدة من {user}",
        )

    try:
        embed = discord.Embed(color=COLOR_TICKET)
        embed.add_field(name=f"مالك التذكرة", value=user.mention, inline=False)
        embed.add_field(name=f"مشرفي التذاكر", value=" ".join(r.mention for r in admin_roles), inline=False)
        embed.add_field(name=f"تاريخ التذكرة", value=f"<t:{int(now_utc().timestamp())}:F>", inline=False)
        embed.add_field(name=f"رقم التذكرة", value=f"```{number}```", inline=False)
        embed.add_field(name=f"قسم التذكرة", value=f"```{section['label']}```", inline=False)
        embed.set_thumbnail(url=user.display_avatar.url)

        banner_path = os.path.join(BASE_DIR, section["banner"])
        if not os.path.exists(banner_path):
            banner_path = os.path.join(BASE_DIR, PANEL_IMAGE)
        file = None
        if os.path.exists(banner_path):
            file = discord.File(banner_path, filename="banner.png")
            embed.set_image(url="attachment://banner.png")

        content = f"{user.mention} | " + " | ".join(r.mention for r in admin_roles)
        msg = await channel.send(
            content=content,
            embed=embed,
            file=file,
            view=TicketControls(),
            allowed_mentions=discord.AllowedMentions(roles=True, users=True),
        )
        try:
            await msg.pin()
        except discord.HTTPException:
            pass

    except Exception:
        try:
            await channel.delete(reason="فشل إنشاء التذكرة")
        except discord.HTTPException:
            pass
        raise

    await interaction.followup.send(f"تم إنشاء التذكرة: {channel.mention}", ephemeral=True)

    log = discord.Embed(title=f"تذكرة جديدة", color=COLOR_OPEN, timestamp=now_utc())
    log.add_field(name="الروم", value=channel.mention)
    log.add_field(name="صاحب التذكرة", value=user.mention)
    log.add_field(name="القسم", value=section["label"])
    await send_log(guild, log)


# ============================================================
#  لوحة التذاكر (القائمة تحت الصورة)
# ============================================================
class SectionSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=s["label"], value=str(i))
            for i, s in enumerate(SECTIONS)
        ]
        super().__init__(
            placeholder="اختر خيار التذكرة",
            options=options,
            min_values=1,
            max_values=1,
            custom_id="ticket:section",
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await create_ticket(interaction, int(self.values[0]))
        except discord.Forbidden:
            traceback.print_exc()
            await interaction.followup.send(
                f"البوت ما عنده صلاحيات كافية لفتح التذكرة.\n"
                "أعطه صلاحية **Administrator** (أو: Manage Channels + Manage Roles + View Channels + "
                "Send Messages + Manage Messages) وتأكد إن رتبته مرفوعة فوق.",
                ephemeral=True,
            )
        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send(
                f"صار خطأ أثناء فتح التذكرة:\n`{type(e).__name__}: {e}`",
                ephemeral=True,
            )


class TicketPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(SectionSelect())


# ============================================================
#  إنهاء التذكرة
# ============================================================
async def close_ticket(interaction: discord.Interaction):
    channel = interaction.channel
    guild = interaction.guild
    info = read_topic(channel)
    owner = await get_member(guild, info["owner"]) if info["owner"] else None

    # إخفاء التذكرة عن صاحبها
    if owner:
        await channel.set_permissions(owner, overwrite=discord.PermissionOverwrite(view_channel=False))

    main = await find_main_message(channel)
    if main:
        try:
            await main.edit(view=None)
        except discord.HTTPException:
            pass

    embed = discord.Embed(
        title=f"تم إنهاء التذكرة",
        description=f"أُغلقت التذكرة بواسطة {interaction.user.mention}.\nالتحكم الآن للإدارة فقط.",
        color=COLOR_CLOSE,
        timestamp=now_utc(),
    )
    await channel.send(embed=embed, view=ClosedControls())

    transcript = await build_transcript(channel)
    log = discord.Embed(
        title=f"إنهاء تذكرة",
        description=f"تم إنهاء التذكرة بواسطة {interaction.user.mention}",
        color=COLOR_CLOSE,
        timestamp=now_utc(),
    )
    log.add_field(name="الروم", value=f"#{channel.name}")
    log.add_field(name="رقم التذكرة", value=str(info["number"] or "-"))
    log.add_field(name="القسم", value=info["section"] or "-")
    log.add_field(name="صاحب التذكرة", value=f"<@{info['owner']}>" if info["owner"] else "-")
    if info["claimed"]:
        log.add_field(name="المستلم", value=f"<@{info['claimed']}>")
    await send_log(guild, log, transcript)


# ============================================================
#  أزرار التذكرة: استلام / خيارات التذكرة
# ============================================================
class TicketControls(discord.ui.View):
    def __init__(self, claimed: bool = False):
        super().__init__(timeout=None)
        if claimed:
            self.claim.disabled = True
            self.claim.label = "تم الاستلام"

    # ---------- استلام ----------
    @discord.ui.button(label="استلام", style=discord.ButtonStyle.secondary, custom_id="ticket:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            return await deny(interaction, "هذا الزر مخصص لإدارة التذاكر فقط.")

        info = read_topic(interaction.channel)
        if info["claimed"]:
            return await deny(interaction, f"التذكرة مستلمة بالفعل من <@{info['claimed']}>.")

        await write_topic(interaction.channel, info["owner"], interaction.user.id)
        await interaction.response.edit_message(view=TicketControls(claimed=True))

        await interaction.channel.send(
            embed=discord.Embed(
                description=f"استلم {interaction.user.mention} هذه التذكرة وأصبح مسؤولاً عنها بالكامل.",
                color=COLOR_CLAIM,
            )
        )

        log = discord.Embed(title=f"استلام تذكرة", color=COLOR_CLAIM, timestamp=now_utc())
        log.add_field(name="الروم", value=interaction.channel.mention)
        log.add_field(name="المستلم", value=interaction.user.mention)
        await send_log(interaction.guild, log)

    # ---------- خيارات التذكرة ----------
    @discord.ui.button(label="خيارات التذكرة", style=discord.ButtonStyle.secondary, custom_id="ticket:options")
    async def options_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            return await deny(interaction, "هذا الزر مخصص لإدارة التذاكر فقط.")
        await interaction.response.send_message(
            embed=discord.Embed(
                title="خيارات التذكرة",
                description="اختر الإجراء المطلوب:",
                color=COLOR_TICKET,
            ),
            view=OptionsView(),
            ephemeral=True,
        )


class OptionsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    # ---------- استدعاء ----------
    @discord.ui.button(label="استدعاء", style=discord.ButtonStyle.secondary)
    async def summon(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await can_manage(interaction):
            return

        info = read_topic(interaction.channel)
        owner = await get_member(interaction.guild, info["owner"]) if info["owner"] else None
        if not owner:
            return await deny(interaction, "صاحب التذكرة غير موجود في السيرفر.")

        dm = discord.Embed(
            title=f"استدعاء لتذكرتك",
            description=(
                f"مرحباً {owner.mention}،\n"
                f"وصلك استدعاء لتذكرتك في سيرفر **{interaction.guild.name}** لأنك لم تتفاعل معها.\n\n"
                "تعال رد على التذكرة بأقرب وقت، وإلا سيتم **إغلاقها قريباً**."
            ),
            color=COLOR_WARN,
            timestamp=now_utc(),
        )
        dm.add_field(name=f"التذكرة", value=interaction.channel.jump_url, inline=False)
        dm.set_footer(text=interaction.guild.name)

        try:
            await owner.send(embed=dm)
        except discord.Forbidden:
            await interaction.channel.send(
                content=owner.mention,
                embed=discord.Embed(
                    description=f"تم استدعاؤك لهذه التذكرة، تعال رد قبل أن تُغلق. (الخاص مقفل عندك)",
                    color=COLOR_WARN,
                ),
            )
            return await interaction.response.send_message(
                embed=discord.Embed(description=f"الخاص مقفل عنده، تم تنبيهه داخل التذكرة.", color=COLOR_WARN),
                ephemeral=True,
            )

        await interaction.response.send_message(
            embed=discord.Embed(description=f"تم إرسال الاستدعاء إلى {owner.mention} في الخاص.", color=COLOR_CLAIM),
            ephemeral=True,
        )
        await interaction.channel.send(
            embed=discord.Embed(description=f"قام {interaction.user.mention} باستدعاء صاحب التذكرة.", color=COLOR_WARN)
        )

    # ---------- إنهاء ----------
    @discord.ui.button(label="إنهاء", style=discord.ButtonStyle.secondary)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await can_manage(interaction):
            return
        await interaction.response.defer()
        await close_ticket(interaction)
        await interaction.edit_original_response(
            embed=discord.Embed(description=f"تم إنهاء التذكرة.", color=COLOR_CLAIM),
            view=None,
        )


# ============================================================
#  أزرار ما بعد الإنهاء: إعادة فتح / حذف (للإدارة فقط)
# ============================================================
class ClosedControls(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="إعادة فتح", style=discord.ButtonStyle.secondary, custom_id="ticket:reopen")
    async def reopen(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            return await deny(interaction, "هذا الزر مخصص لإدارة التذاكر فقط.")

        await interaction.response.defer()
        channel = interaction.channel
        info = read_topic(channel)
        owner = await get_member(interaction.guild, info["owner"]) if info["owner"] else None
        if owner:
            await channel.set_permissions(
                owner,
                overwrite=discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                    attach_files=True, embed_links=True,
                ),
            )

        main = await find_main_message(channel)
        if main:
            try:
                await main.edit(view=TicketControls(claimed=bool(info["claimed"])))
            except discord.HTTPException:
                pass

        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass

        await channel.send(
            content=owner.mention if owner else None,
            embed=discord.Embed(
                description=f"أعاد {interaction.user.mention} فتح التذكرة.",
                color=COLOR_OPEN,
            ),
        )

    @discord.ui.button(label="حذف التذكرة", style=discord.ButtonStyle.secondary, custom_id="ticket:delete")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            return await deny(interaction, "هذا الزر مخصص لإدارة التذاكر فقط.")

        await interaction.response.send_message(
            embed=discord.Embed(description=f"سيتم حذف التذكرة خلال 5 ثوانٍ...", color=COLOR_CLOSE)
        )
        await asyncio.sleep(5)
        await interaction.channel.delete(reason=f"حذف تذكرة بواسطة {interaction.user}")


# ============================================================
#  الرومات الصوتية المؤقتة (Temp Voice)
# ============================================================
temp_rooms: dict = {}   # آيدي الروم -> آيدي المالك

VOICE_BUTTONS = [
    ("rename", "تغيير الاسم", 0), ("limit", "تحديد العدد", 0), ("kick", "طرد", 0),
    ("transfer", "نقل الملكية", 0), ("delete", "حذف الروم", 0),
    ("lock", "قفل", 1), ("unlock", "فتح", 1), ("hide", "إخفاء", 1),
    ("show", "إظهار", 1), ("region", "الريجن", 1),
    ("mute", "ميوت", 2), ("unmute", "فك الميوت", 2), ("ban", "منع", 2),
    ("allow", "سماح", 2), ("invite", "دعوة", 2),
]

TARGET_PROMPTS = {
    "kick": "اختر العضو المراد طرده:",
    "transfer": "اختر العضو الذي تريد نقل الملكية إليه:",
    "mute": "اختر العضو المراد ميوته:",
    "unmute": "اختر العضو المراد فك الميوت عنه:",
    "ban": "اختر العضو المراد منعه:",
    "allow": "اختر العضو المراد السماح له:",
    "invite": "اختر العضو المراد دعوته:",
}

PERM_ACTIONS = {
    "lock": ({"connect": False}, "تم قفل الروم."),
    "unlock": ({"connect": None}, "تم فتح الروم."),
    "hide": ({"view_channel": False}, "تم إخفاء الروم."),
    "show": ({"view_channel": None}, "تم إظهار الروم."),
}

REGIONS = [
    ("افتراضي", "auto"), ("البرازيل", "brazil"), ("الهند", "india"), ("اليابان", "japan"),
    ("سنغافورة", "singapore"), ("أمريكا - الشرق", "us-east"), ("أمريكا - الغرب", "us-west"),
]


def owner_from_overwrites(channel):
    """مالك الروم = العضو اللي عنده صلاحية إدارة القناة في الروم (يرجع بعد إعادة التشغيل)"""
    for target, ow in channel.overwrites.items():
        if isinstance(target, discord.Role):
            continue
        if ow.manage_channels:
            return target.id
    return None


async def say(interaction: discord.Interaction, text: str):
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


async def get_owned_room(interaction: discord.Interaction):
    voice = getattr(interaction.user, "voice", None)
    room = voice.channel if voice else None
    if room is None or room.id not in temp_rooms:
        await deny(interaction, "يجب أن تكون داخل رومك الصوتي.")
        return None
    if temp_rooms[room.id] != interaction.user.id:
        await deny(interaction, "أنت لست مالك هذا الروم.")
        return None
    return room


async def run_target_action(action: str, interaction: discord.Interaction, room, target) -> str:
    owner = interaction.user
    if not isinstance(target, discord.Member):
        return "هذا الشخص ليس في السيرفر."
    if target.bot:
        return "لا يمكن تنفيذ هذا الإجراء على بوت."
    if target.id == owner.id:
        return "لا يمكنك تنفيذ هذا الإجراء على نفسك."

    in_room = bool(target.voice and target.voice.channel and target.voice.channel.id == room.id)

    if action == "kick":
        if not in_room:
            return "العضو ليس داخل رومك."
        await target.move_to(None)
        return f"تم طرد {target.mention}."
    if action == "mute":
        await room.set_permissions(target, speak=False)
        return f"تم ميوت {target.mention} في رومك."
    if action == "unmute":
        await room.set_permissions(target, speak=None)
        return f"تم فك الميوت عن {target.mention}."
    if action == "ban":
        await room.set_permissions(target, connect=False)
        if in_room:
            await target.move_to(None)
        return f"تم منع {target.mention} من دخول رومك."
    if action == "allow":
        await room.set_permissions(target, connect=True)
        return f"تم السماح لـ {target.mention} بدخول رومك."
    if action == "invite":
        await room.set_permissions(target, connect=True)
        try:
            await target.send(f"تمت دعوتك إلى روم صوتي من {owner.display_name}\n{room.jump_url}")
        except discord.HTTPException:
            return "تم السماح له بالدخول، لكن خاصه مقفل فما وصلته الدعوة."
        return "تم إرسال الدعوة."
    if action == "transfer":
        await room.set_permissions(target, manage_channels=True, connect=True, speak=True)
        await room.set_permissions(owner, overwrite=None)
        temp_rooms[room.id] = target.id
        return f"تم نقل ملكية الروم إلى {target.mention}."
    return "إجراء غير معروف."


class TargetSelect(discord.ui.UserSelect):
    def __init__(self, action: str):
        super().__init__(placeholder="اختر العضو", min_values=1, max_values=1)
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        room = await get_owned_room(interaction)
        if not room:
            return
        target = self.values[0]
        await interaction.response.defer()
        try:
            text = await run_target_action(self.action, interaction, room, target)
        except discord.HTTPException:
            traceback.print_exc()
            text = "تعذّر تنفيذ الإجراء، تأكد من صلاحيات البوت."
        await interaction.edit_original_response(content=text, view=None)


class TargetView(discord.ui.View):
    def __init__(self, action: str):
        super().__init__(timeout=120)
        self.add_item(TargetSelect(action))


class RegionSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="اختر الريجن",
            options=[discord.SelectOption(label=l, value=v) for l, v in REGIONS],
        )

    async def callback(self, interaction: discord.Interaction):
        room = await get_owned_room(interaction)
        if not room:
            return
        value = self.values[0]
        await interaction.response.defer()
        await room.edit(rtc_region=None if value == "auto" else value)
        await interaction.edit_original_response(content="تم تغيير الريجن.", view=None)


class RegionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(RegionSelect())


class RenameModal(discord.ui.Modal, title="تغيير اسم الروم"):
    new_name = discord.ui.TextInput(label="اسم الروم الجديد", max_length=100, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        room = await get_owned_room(interaction)
        if not room:
            return
        await interaction.response.defer(ephemeral=True)
        await room.edit(name=self.new_name.value)
        await interaction.followup.send(f"تم تغيير الاسم إلى {self.new_name.value}", ephemeral=True)


class LimitModal(discord.ui.Modal, title="تحديد عدد الروم"):
    number = discord.ui.TextInput(label="العدد (اكتب 0 لإلغاء الحد)", placeholder="مثال: 5", max_length=2, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        room = await get_owned_room(interaction)
        if not room:
            return
        try:
            n = int(self.number.value)
        except ValueError:
            return await deny(interaction, "اكتب رقماً صحيحاً.")
        n = max(0, min(99, n))
        await interaction.response.defer(ephemeral=True)
        await room.edit(user_limit=n)
        await interaction.followup.send("تم تحديد العدد." if n else "تم إلغاء حد العدد.", ephemeral=True)


async def handle_voice_action(interaction: discord.Interaction, key: str):
    room = await get_owned_room(interaction)
    if not room:
        return

    if key == "rename":
        return await interaction.response.send_modal(RenameModal())
    if key == "limit":
        return await interaction.response.send_modal(LimitModal())
    if key == "region":
        return await interaction.response.send_message("اختر الريجن:", view=RegionView(), ephemeral=True)
    if key in TARGET_PROMPTS:
        return await interaction.response.send_message(TARGET_PROMPTS[key], view=TargetView(key), ephemeral=True)
    if key in PERM_ACTIONS:
        perms, text = PERM_ACTIONS[key]
        await interaction.response.defer(ephemeral=True)
        await room.set_permissions(interaction.guild.default_role, **perms)
        return await interaction.followup.send(text, ephemeral=True)
    if key == "delete":
        await say(interaction, "تم حذف الروم.")
        temp_rooms.pop(room.id, None)
        await room.delete(reason=f"حذف الروم بواسطة {interaction.user}")


class VoiceButton(discord.ui.Button):
    def __init__(self, key: str, label: str, row: int):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, custom_id=f"vc:{key}", row=row)
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        try:
            await handle_voice_action(interaction, self.key)
        except Exception:
            traceback.print_exc()
            try:
                await say(interaction, "صار خطأ، تأكد من صلاحيات البوت وحاول مرة ثانية.")
            except discord.HTTPException:
                pass


class VoicePanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for key, label, row in VOICE_BUTTONS:
            self.add_item(VoiceButton(key, label, row))


async def create_temp_room(member: discord.Member, create_channel: discord.VoiceChannel):
    guild = member.guild
    room = await guild.create_voice_channel(
        name=member.display_name[:100],
        category=create_channel.category,
        reason=f"روم مؤقت لـ {member}",
    )
    temp_rooms[room.id] = member.id
    await room.set_permissions(member, manage_channels=True, connect=True, speak=True)
    try:
        await member.move_to(room)
    except discord.HTTPException:
        temp_rooms.pop(room.id, None)
        await room.delete(reason="تعذر نقل العضو")


async def recover_temp_rooms(bot: commands.Bot):
    """بعد إعادة تشغيل البوت: نرجّع مالكي الرومات المؤقتة الموجودة"""
    if not VOICE_CREATE_CHANNEL_ID:
        return
    create_channel = bot.get_channel(VOICE_CREATE_CHANNEL_ID)
    if not isinstance(create_channel, discord.VoiceChannel):
        return
    for ch in create_channel.guild.voice_channels:
        if ch.id == create_channel.id or ch.category_id != create_channel.category_id:
            continue
        owner_id = owner_from_overwrites(ch)
        if owner_id:
            temp_rooms[ch.id] = owner_id


async def send_voice_panel(bot: commands.Bot):
    if not VOICE_PANEL_CHANNEL_ID:
        return
    channel = bot.get_channel(VOICE_PANEL_CHANNEL_ID)
    if channel is None:
        return
    # نحذف لوحة الصوت القديمة فقط
    try:
        async for m in channel.history(limit=20):
            if m.author.id == bot.user.id and m.embeds and m.embeds[0].title == VOICE_PANEL_TITLE:
                await m.delete()
    except discord.HTTPException:
        pass

    embed = discord.Embed(title=VOICE_PANEL_TITLE, color=0x0F172A)
    file = None
    path = os.path.join(BASE_DIR, VOICE_PANEL_IMAGE)
    if os.path.exists(path):
        file = discord.File(path, filename=VOICE_PANEL_IMAGE)
        embed.set_image(url=f"attachment://{VOICE_PANEL_IMAGE}")
    await channel.send(embed=embed, file=file, view=VoicePanel())


# ============================================================
#  البوت
# ============================================================
class TicketBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self._voice_panel_sent = False

    async def setup_hook(self):
        # الأزرار والقائمة الدائمة (تشتغل حتى بعد إعادة تشغيل البوت)
        self.add_view(TicketPanel())
        self.add_view(TicketControls())
        self.add_view(ClosedControls())
        self.add_view(VoicePanel())

        # سيرفر ويب صغير عشان ريندر ما يوقف الخدمة
        app = web.Application()
        app.router.add_get("/", lambda r: web.Response(text="Ticket bot is running"))
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()

        # مزامنة أوامر السلاش
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        print(f"Bot is ready: {self.user} ({self.user.id})")
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="Tickets"))
        try:
            await recover_temp_rooms(self)
            if not self._voice_panel_sent:
                self._voice_panel_sent = True
                await send_voice_panel(self)
        except Exception:
            traceback.print_exc()

    async def on_voice_state_update(self, member, before, after):
        if not VOICE_CREATE_CHANNEL_ID:
            return
        try:
            if after.channel and after.channel.id == VOICE_CREATE_CHANNEL_ID:
                await create_temp_room(member, after.channel)
            ch = before.channel
            if ch and ch.id in temp_rooms and len(ch.voice_states) == 0:
                temp_rooms.pop(ch.id, None)
                await ch.delete(reason="روم مؤقت فارغ")
        except Exception:
            traceback.print_exc()


bot = TicketBot()


@bot.tree.command(name="panel", description="إرسال لوحة فتح التذاكر")
@app_commands.guild_only()
async def panel(interaction: discord.Interaction):
    # الأمر للاونر فقط
    if not is_owner(interaction.user):
        return await deny(interaction, "هذا الأمر مخصص للاونر فقط.")

    channel = interaction.guild.get_channel(PANEL_CHANNEL_ID)
    if not channel:
        return await deny(interaction, "آيدي روم اللوحة غلط أو البوت ما يشوف الروم.")

    image_path = os.path.join(BASE_DIR, PANEL_IMAGE)
    if not os.path.exists(image_path):
        return await deny(interaction, f"ما لقيت الصورة `{PANEL_IMAGE}` بجانب main.py في جيت هوب.")

    embed = discord.Embed(
        description="حياك الله في حال تبي تفتح تكت شوف مبتغاك واضغطه",
        color=COLOR_MAIN,
    )
    embed.set_image(url=f"attachment://{PANEL_IMAGE}")

    await channel.send(
        embed=embed,
        file=discord.File(image_path, filename=PANEL_IMAGE),
        view=TicketPanel(),
    )
    await interaction.response.send_message(f"تم إرسال اللوحة في {channel.mention}", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing in Environment Variables")
    bot.run(TOKEN)
