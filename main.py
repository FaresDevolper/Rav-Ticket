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

# الإيموجي (مكتوبة كأكواد عشان ما تخرب مع النسخ)
E_TICKET = "\U0001f3ab"
E_USER = "\U0001f464"
E_SHIELD = "\U0001f6e1\ufe0f"
E_CAL = "\U0001f4c5"
E_NUM = "\U0001f522"
E_Q = "\u2753"
E_OPTIONS = "\U0001f5c3\ufe0f"
E_BRIEFCASE = "\U0001f4bc"
E_BELL = "\U0001f514"
E_LOCK = "\U0001f512"
E_UNLOCK = "\U0001f513"
E_TRASH = "\U0001f5d1\ufe0f"
E_OK = "\u2705"
E_NO = "\u26d4"
E_WARN = "\u26a0\ufe0f"
E_FOLDER = "\U0001f4c2"
E_WAVE = "\U0001f44b"

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
    embed = discord.Embed(description=f"{E_NO} {text}", color=COLOR_CLOSE)
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
            name=f"{E_TICKET}\u30fb{number}",
            category=category,
            overwrites=overwrites,
            topic=f"owner={user.id};claimed=0;number={number};section={section['label']}",
            reason=f"تذكرة جديدة من {user}",
        )

    try:
        embed = discord.Embed(color=COLOR_TICKET)
        embed.add_field(name=f"[{E_USER}] : مالك التذكرة", value=user.mention, inline=False)
        embed.add_field(name=f"[{E_SHIELD}] : مشرفي التذاكر", value=" ".join(r.mention for r in admin_roles), inline=False)
        embed.add_field(name=f"[{E_CAL}] : تاريخ التذكرة", value=f"<t:{int(now_utc().timestamp())}:F>", inline=False)
        embed.add_field(name=f"[{E_NUM}] : رقم التذكرة", value=f"```{number}```", inline=False)
        embed.add_field(name=f"[{E_Q}] : قسم التذكرة", value=f"```{section['label']}```", inline=False)
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

    log = discord.Embed(title=f"{E_FOLDER} تذكرة جديدة", color=COLOR_OPEN, timestamp=now_utc())
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
                f"{E_NO} البوت ما عنده صلاحيات كافية لفتح التذكرة.\n"
                "أعطه صلاحية **Administrator** (أو: Manage Channels + Manage Roles + View Channels + "
                "Send Messages + Manage Messages) وتأكد إن رتبته مرفوعة فوق.",
                ephemeral=True,
            )
        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send(
                f"{E_NO} صار خطأ أثناء فتح التذكرة:\n`{type(e).__name__}: {e}`",
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
        title=f"{E_LOCK} تم إنهاء التذكرة",
        description=f"أُغلقت التذكرة بواسطة {interaction.user.mention}.\nالتحكم الآن للإدارة فقط .",
        color=COLOR_CLOSE,
        timestamp=now_utc(),
    )
    await channel.send(embed=embed, view=ClosedControls())

    transcript = await build_transcript(channel)
    log = discord.Embed(
        title=f"{E_LOCK} إنهاء تذكرة",
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
            self.claim.label = " تم الاستلام "

    # ---------- استلام ----------
    @discord.ui.button(label="استلام", style=discord.ButtonStyle.secondary, custom_id="ticket:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            return await deny(interaction, "هذا الزر مخصص لإدارة التذاكر فقط .")

        info = read_topic(interaction.channel)
        if info["claimed"]:
            return await deny(interaction, f"التذكرة مستلمة بالفعل من <@{info['claimed']}>.")

        await write_topic(interaction.channel, info["owner"], interaction.user.id)
        await interaction.response.edit_message(view=TicketControls(claimed=True))

        await interaction.channel.send(
            embed=discord.Embed(
                description=f"{E_OK} استلم {interaction.user.mention} هذه التذكرة وأصبح مسؤولاً عنها بالكامل .",
                color=COLOR_CLAIM,
            )
        )

        log = discord.Embed(title=f"{E_BRIEFCASE} استلام تذكرة", color=COLOR_CLAIM, timestamp=now_utc())
        log.add_field(name="الروم", value=interaction.channel.mention)
        log.add_field(name="المستلم", value=interaction.user.mention)
        await send_log(interaction.guild, log)

    # ---------- خيارات التذكرة ----------
    @discord.ui.button(label="خيارات التذكرة", style=discord.ButtonStyle.secondary, custom_id="ticket:options")
    async def options_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            return await deny(interaction, "هذا الزر مخصص لإدارة التذاكر فقط .")
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
    @discord.ui.button(label="استدعاء", emoji=E_BELL, style=discord.ButtonStyle.secondary)
    async def summon(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await can_manage(interaction):
            return

        info = read_topic(interaction.channel)
        owner = await get_member(interaction.guild, info["owner"]) if info["owner"] else None
        if not owner:
            return await deny(interaction, "صاحب التذكرة غير موجود في السيرفر .")

        dm = discord.Embed(
            title=f"{E_BELL} استدعاء لتذكرتك",
            description=(
                f"مرحباً {owner.mention}،\n"
                f"وصلك استدعاء لتذكرتك في سيرفر **{interaction.guild.name}** لأنك لم تتفاعل معها .\n\n"
                "تعال رد على التذكرة بأقرب وقت، وإلا سيتم ** إغلاقها قريباً **."
            ),
            color=COLOR_WARN,
            timestamp=now_utc(),
        )
        dm.add_field(name=f"{E_TICKET} التذكرة", value=interaction.channel.jump_url, inline=False)
        dm.set_footer(text=interaction.guild.name)

        try:
            await owner.send(embed=dm)
        except discord.Forbidden:
            await interaction.channel.send(
                content=owner.mention,
                embed=discord.Embed(
                    description=f"{E_BELL} تم استدعاؤك لهذه التذكرة، تعال رد قبل أن تُغلق . (الخاص مقفل عندك) ",
                    color=COLOR_WARN,
                ),
            )
            return await interaction.response.send_message(
                embed=discord.Embed(description=f"{E_WARN} الخاص مقفل عنده، تم تنبيهه داخل التذكرة.", color=COLOR_WARN),
                ephemeral=True,
            )

        await interaction.response.send_message(
            embed=discord.Embed(description=f"{E_OK} تم إرسال الاستدعاء إلى {owner.mention} في الخاص.", color=COLOR_CLAIM),
            ephemeral=True,
        )
        await interaction.channel.send(
            embed=discord.Embed(description=f"{E_BELL} قام {interaction.user.mention} باستدعاء صاحب التذكرة.", color=COLOR_WARN)
        )

    # ---------- إنهاء ----------
    @discord.ui.button(label="إنهاء", emoji=E_LOCK, style=discord.ButtonStyle.secondary)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await can_manage(interaction):
            return
        await interaction.response.defer()
        await close_ticket(interaction)
        await interaction.edit_original_response(
            embed=discord.Embed(description=f"{E_OK} تم إنهاء التذكرة.", color=COLOR_CLAIM),
            view=None,
        )


# ============================================================
#  أزرار ما بعد الإنهاء: إعادة فتح / حذف (للإدارة فقط)
# ============================================================
class ClosedControls(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="إعادة فتح", emoji=E_UNLOCK, style=discord.ButtonStyle.secondary, custom_id="ticket:reopen")
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
                description=f"{E_UNLOCK} أعاد {interaction.user.mention} فتح التذكرة.",
                color=COLOR_OPEN,
            ),
        )

    @discord.ui.button(label="حذف التذكرة", emoji=E_TRASH, style=discord.ButtonStyle.secondary, custom_id="ticket:delete")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            return await deny(interaction, "هذا الزر مخصص لإدارة التذاكر فقط.")

        await interaction.response.send_message(
            embed=discord.Embed(description=f"{E_TRASH} سيتم حذف التذكرة خلال 5 ثوانٍ...", color=COLOR_CLOSE)
        )
        await asyncio.sleep(5)
        await interaction.channel.delete(reason=f"حذف تذكرة بواسطة {interaction.user}")


# ============================================================
#  البوت
# ============================================================
class TicketBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())

    async def setup_hook(self):
        # الأزرار والقائمة الدائمة (تشتغل حتى بعد إعادة تشغيل البوت)
        self.add_view(TicketPanel())
        self.add_view(TicketControls())
        self.add_view(ClosedControls())

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
    await interaction.response.send_message(f"{E_OK} تم إرسال اللوحة في {channel.mention}", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing in Environment Variables")
    bot.run(TOKEN)
