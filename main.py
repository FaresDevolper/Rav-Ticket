import os
import io
import asyncio
import datetime

import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web

# ============================================================
#  الإعدادات: بدّل الأرقام بالآيديات حقتك (التوكن يبقى في Render)
# ============================================================
TOKEN = os.getenv("DISCORD_TOKEN")                # التوكن من Environment Variables في ريندر

OWNER_ROLE_ID = 1425199301956599888                # آيدي رتبة الاونر (يستخدم أمر /panel)
ADMIN_ROLE_ID = 1465057908554334220                # آيدي رتبة ادمن تيكت
PANEL_CHANNEL_ID = 1465041190163578972             # آيدي روم صورة التيكت (اللي تنرسل فيه اللوحة)
CATEGORY_ID = 1465040666282168470                  # آيدي كاتيجوري التذاكر
LOG_CHANNEL_ID = 1550546365338099803                                # آيدي روم السجل (اتركه 0 إذا ما تبيه)
GUILD_ID = 1425195739591610451                     # آيدي السيرفر (يخلي الأوامر تظهر فوراً)

# صورة اللوحة: ارفعها في جيت هوب بجانب main.py وبنفس الاسم
PANEL_IMAGE = "background.png"

# أقسام التذاكر (تقدر تغيّر الأسماء أو تضيف أقسام من هنا)
SECTIONS = [
    {"label": "استفسار", "emoji": "", "desc": "اسأل عن أي شيء يخص السيرفر"},
    {"label": "طلب رول", "emoji": "", "desc": "اطلب رتبة أو رول"},
    {"label": "الفعاليات", "emoji": "", "desc": "كل ما يخص الفعاليات"},
    {"label": "شكوى على عضو", "emoji": "", "desc": "قدّم شكوى ضد عضو"},
    {"label": "شكوى على اداري", "emoji": "", "desc": "قدّم شكوى ضد إداري"},
    {"label": "طلب بروفايل كامل للبنت", "emoji": "", "desc": "طلب بروفايل كامل (بنات)"},
    {"label": "طلب بروفايل كامل للرجال", "emoji": "", "desc": "طلب بروفايل كامل (رجال)"},
]

PORT = int(os.getenv("PORT", "10000"))            # بورت ريندر (لا تغيره)

# بعد الاستلام: هل يقدر ادمن ثاني يستدعي/ينهي؟ (False = المستلم فقط)
ALLOW_OTHER_ADMINS_AFTER_CLAIM = False

COLOR_MAIN = 0x2B2D31
COLOR_OPEN = 0x5865F2
COLOR_CLAIM = 0x57F287
COLOR_CLOSE = 0xED4245
COLOR_WARN = 0xFEE75C


# ============================================================
#  دوال مساعدة
# ============================================================
def is_admin(member: discord.abc.User) -> bool:
    """هل العضو يملك رتبة ادمن تيكت؟"""
    if not isinstance(member, discord.Member):
        return False
    return any(r.id == ADMIN_ROLE_ID for r in member.roles)


def read_topic(channel: discord.TextChannel) -> dict:
    """نخزن بيانات التذكرة في وصف الروم: owner=ID;claimed=ID"""
    data = {"owner": None, "claimed": None, "section": None}
    if not channel.topic:
        return data
    for part in channel.topic.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k in ("owner", "claimed") and v.isdigit():
                data[k] = int(v)
            elif k == "section" and v:
                data["section"] = v
    return data


async def write_topic(channel: discord.TextChannel, owner, claimed):
    section = read_topic(channel)["section"] or ""
    topic = f"owner={owner};claimed={claimed or 0};section={section}"
    await channel.edit(topic=topic)


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


async def deny(interaction: discord.Interaction, text: str):
    embed = discord.Embed(description=f"⛔ {text}", color=COLOR_CLOSE)
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
#  نافذة فتح التذكرة (Modal)
# ============================================================
class TicketModal(discord.ui.Modal, title="فتح تذكرة"):
    def __init__(self, section: dict):
        super().__init__()
        self.section = section
        self.title = f"{section['emoji']} {section['label']}"[:45]

    subject = discord.ui.TextInput(
        label="موضوع التذكرة",
        placeholder="اكتب مشكلتك أو استفسارك بوضوح...",
        style=discord.TextStyle.paragraph,
        min_length=5,
        max_length=800,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        user = interaction.user
        category = guild.get_channel(CATEGORY_ID)
        admin_role = guild.get_role(ADMIN_ROLE_ID)

        if not category or not admin_role:
            return await deny(interaction, "إعدادات البوت غير مكتملة، تواصل مع الإدارة.")

        # منع تكرار التذاكر: تذكرة واحدة مفتوحة لكل عضو
        for ch in category.text_channels:
            if read_topic(ch)["owner"] == user.id:
                return await deny(interaction, f"عندك تذكرة مفتوحة بالفعل: {ch.mention}")

        await interaction.response.defer(ephemeral=True)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                attach_files=True, embed_links=True,
            ),
            admin_role: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                attach_files=True, embed_links=True, manage_messages=True,
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True,
                read_message_history=True, embed_links=True, attach_files=True,
            ),
        }

        channel = await guild.create_text_channel(
            name=f"ticket-{user.name}",
            category=category,
            overwrites=overwrites,
            topic=f"owner={user.id};claimed=0;section={self.section['label']}",
            reason=f"تذكرة جديدة من {user}",
        )

        embed = discord.Embed(
            title="🎫 تذكرة دعم فني",
            description=(
                f"أهلاً {user.mention} 👋\n"
                "تم فتح تذكرتك بنجاح سيتم الرد عليك من فريق الدعم بأقرب وقت.\n"
                " ننصحك بكتابة كل التفاصيل وإرفاق الصور إذا لزم الامر."
            ),
            color=COLOR_OPEN,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.add_field(name="📂 القسم", value=f"{self.section['emoji']} {self.section['label']}", inline=True)
        embed.add_field(name="📝 الموضوع", value=self.subject.value, inline=False)
        embed.add_field(name="👤 صاحب التذكرة", value=user.mention, inline=True)
        embed.add_field(name="📌 الحالة", value="🟡 بانتظار الاستلام", inline=True)
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(text=f"{guild.name} • نظام التذاكر", icon_url=guild.icon.url if guild.icon else None)

        await channel.send(
            content=f"{user.mention} | {admin_role.mention}",
            embed=embed,
            view=TicketControls(),
            allowed_mentions=discord.AllowedMentions(roles=True, users=True),
        )

        await interaction.followup.send(
            embed=discord.Embed(description=f"✅ تم فتح تذكرتك: {channel.mention}", color=COLOR_CLAIM),
            ephemeral=True,
        )

        log = discord.Embed(title="📂 تذكرة جديدة", color=COLOR_OPEN, timestamp=datetime.datetime.now(datetime.timezone.utc))
        log.add_field(name="الروم", value=channel.mention)
        log.add_field(name="صاحب التذكرة", value=user.mention)
        log.add_field(name="القسم", value=f"{self.section['emoji']} {self.section['label']}")
        await send_log(guild, log)


# ============================================================
#  لوحة فتح التذاكر
# ============================================================
class SectionSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=s["label"], value=str(i), emoji=s["emoji"], description=s["desc"])
            for i, s in enumerate(SECTIONS)
        ]
        super().__init__(placeholder="اختر القسم المناسب...", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        section = SECTIONS[int(self.values[0])]
        await interaction.response.send_modal(TicketModal(section))


class SectionPickView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(SectionSelect())


class TicketPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="فتح تذكرة", emoji="🎫", style=discord.ButtonStyle.primary, custom_id="ticket:open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=discord.Embed(
                title="📂 اختر القسم",
                description="اختر القسم المناسب لطلبك من القائمة بالأسفل.",
                color=COLOR_OPEN,
            ),
            view=SectionPickView(),
            ephemeral=True,
        )


# ============================================================
#  أزرار التحكم داخل التذكرة: استلام / استدعاء / إنهاء
# ============================================================
class TicketControls(discord.ui.View):
    def __init__(self, claimed: bool = False):
        super().__init__(timeout=None)
        if claimed:
            self.claim.disabled = True
            self.claim.label = "تم الاستلام"

    # ---------- استلام ----------
    @discord.ui.button(label="استلام", emoji="✋", style=discord.ButtonStyle.success, custom_id="ticket:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            return await deny(interaction, "هذا الزر مخصص لإدارة التذاكر فقط.")

        info = read_topic(interaction.channel)
        if info["claimed"]:
            return await deny(interaction, f"التذكرة مستلمة بالفعل من <@{info['claimed']}>.")

        await write_topic(interaction.channel, info["owner"], interaction.user.id)

        embed = interaction.message.embeds[0]
        # تحديث حقل الحالة
        for i, f in enumerate(embed.fields):
            if f.name == "📌 الحالة":
                embed.set_field_at(i, name="📌 الحالة", value="🟢 تم الاستلام", inline=True)
        embed.add_field(name="🛡️ المسؤول عن التذكرة", value=interaction.user.mention, inline=True)
        embed.color = COLOR_CLAIM

        await interaction.response.edit_message(embed=embed, view=TicketControls(claimed=True))
        await interaction.channel.send(
            embed=discord.Embed(
                description=f"✅ استلم {interaction.user.mention} هذه التذكرة وأصبح مسؤولاً عنها بالكامل.",
                color=COLOR_CLAIM,
            )
        )

        log = discord.Embed(title="✋ استلام تذكرة", color=COLOR_CLAIM, timestamp=datetime.datetime.now(datetime.timezone.utc))
        log.add_field(name="الروم", value=interaction.channel.mention)
        log.add_field(name="المستلم", value=interaction.user.mention)
        await send_log(interaction.guild, log)

    # ---------- استدعاء ----------
    @discord.ui.button(label="استدعاء", emoji="🔔", style=discord.ButtonStyle.primary, custom_id="ticket:summon")
    async def summon(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await can_manage(interaction):
            return

        info = read_topic(interaction.channel)
        owner = await get_member(interaction.guild, info["owner"]) if info["owner"] else None
        if not owner:
            return await deny(interaction, "صاحب التذكرة غير موجود في السيرفر.")

        dm = discord.Embed(
            title="🔔 استدعاء لتذكرتك",
            description=(
                f"مرحباً {owner.mention}،\n"
                f"وصلك استدعاء لتذكرتك في سيرفر **{interaction.guild.name}** لأنك لم تتفاعل معها.\n\n"
                "تعال رد على التذكرة بأقرب وقت، وإلا سيتم **إغلاقها قريباً**."
            ),
            color=COLOR_WARN,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        dm.add_field(name="🎫 التذكرة", value=interaction.channel.jump_url, inline=False)
        dm.set_footer(text=interaction.guild.name)

        try:
            await owner.send(embed=dm)
        except discord.Forbidden:
            await interaction.channel.send(
                content=owner.mention,
                embed=discord.Embed(
                    description="🔔 تم استدعاؤك لهذه التذكرة، تعال رد قبل أن تُغلق. (الخاص مقفل عندك)",
                    color=COLOR_WARN,
                ),
            )
            return await interaction.response.send_message(
                embed=discord.Embed(description="⚠️ الخاص مقفل عنده، تم تنبيهه داخل التذكرة.", color=COLOR_WARN),
                ephemeral=True,
            )

        await interaction.response.send_message(
            embed=discord.Embed(description=f" تم إرسال الاستدعاء إلى {owner.mention} في الخاص.", color=COLOR_CLAIM),
            ephemeral=True,
        )
        await interaction.channel.send(
            embed=discord.Embed(description=f"🔔 قام {interaction.user.mention} باستدعاء صاحب التذكرة.", color=COLOR_WARN)
        )

    # ---------- إنهاء ----------
    @discord.ui.button(label="إنهاء", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await can_manage(interaction):
            return

        channel = interaction.channel
        info = read_topic(channel)
        owner = await get_member(interaction.guild, info["owner"]) if info["owner"] else None

        await interaction.response.edit_message(view=None)

        # إخفاء التذكرة عن صاحبها: ما يقدر يشوفها ولا يستخدمها
        if owner:
            await channel.set_permissions(owner, overwrite=discord.PermissionOverwrite(view_channel=False))

        embed = discord.Embed(
            title="🔒 تم إنهاء التذكرة",
            description=f"أُغلقت التذكرة بواسطة {interaction.user.mention}.\nالتحكم الآن للإدارة فقط.",
            color=COLOR_CLOSE,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        await channel.send(embed=embed, view=ClosedControls())

        # حفظ نسخة المحادثة في السجل
        transcript = await build_transcript(channel)
        log = discord.Embed(
            title="🔒 إنهاء تذكرة",
            description=f"تم إنهاء التذكرة بواسطة {interaction.user.mention}",
            color=COLOR_CLOSE,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        log.add_field(name="الروم", value=f"#{channel.name}")
        log.add_field(name="القسم", value=info["section"] or "—")
        log.add_field(name="صاحب التذكرة", value=f"<@{info['owner']}>" if info["owner"] else "—")
        if info["claimed"]:
            log.add_field(name="المستلم", value=f"<@{info['claimed']}>")
        await send_log(interaction.guild, log, transcript)


# ============================================================
#  أزرار ما بعد الإنهاء: إعادة فتح / حذف (للإدارة فقط)
# ============================================================
class ClosedControls(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="إعادة فتح", emoji="🔓", style=discord.ButtonStyle.success, custom_id="ticket:reopen")
    async def reopen(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            return await deny(interaction, "هذا الزر مخصص لإدارة التذاكر فقط.")

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

        await interaction.message.delete()
        embed = discord.Embed(
            description=f"🔓 أعاد {interaction.user.mention} فتح التذكرة.",
            color=COLOR_OPEN,
        )
        await channel.send(
            content=owner.mention if owner else None,
            embed=embed,
            view=TicketControls(claimed=bool(info["claimed"])),
        )

    @discord.ui.button(label="حذف التذكرة", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="ticket:delete")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            return await deny(interaction, "هذا الزر مخصص لإدارة التذاكر فقط.")

        await interaction.response.send_message(
            embed=discord.Embed(description="🗑️ سيتم حذف التذكرة خلال 5 ثوانٍ...", color=COLOR_CLOSE)
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
        # تسجيل الأزرار الدائمة (تشتغل حتى بعد إعادة تشغيل البوت)
        self.add_view(TicketPanel())
        self.add_view(TicketControls())
        self.add_view(ClosedControls())

        # سيرفر ويب صغير عشان ريندر ما يوقف الخدمة
        app = web.Application()
        app.router.add_get("/", lambda r: web.Response(text="Ticket bot is running ✅"))
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
        print(f"✅ البوت شغال باسم: {self.user} ({self.user.id})")
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="🎫 التذاكر"))


bot = TicketBot()


@bot.tree.command(name="panel", description="إرسال لوحة فتح التذاكر")
@app_commands.guild_only()
async def panel(interaction: discord.Interaction):
    # الأمر للاونر فقط
    if not any(r.id == OWNER_ROLE_ID for r in interaction.user.roles):
        return await deny(interaction, "هذا الأمر مخصص للاونر فقط.")

    channel = interaction.guild.get_channel(PANEL_CHANNEL_ID)
    if not channel:
        return await deny(interaction, "آيدي روم اللوحة غلط أو البوت ما يشوف الروم.")

    image_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), PANEL_IMAGE)
    if not os.path.exists(image_path):
        return await deny(interaction, f"ما لقيت الصورة `{PANEL_IMAGE}` بجانب main.py في جيت هوب.")

    embed = discord.Embed(
        description="🎫 اضغط على الزر بالأسفل واختر القسم المناسب لفتح تذكرتك.",
        color=COLOR_MAIN,
    )
    embed.set_image(url=f"attachment://{PANEL_IMAGE}")
    embed.set_footer(text=f"{interaction.guild.name} • نظام التذاكر")

    await channel.send(
        embed=embed,
        file=discord.File(image_path, filename=PANEL_IMAGE),
        view=TicketPanel(),
    )
    await interaction.response.send_message(f"✅ تم إرسال اللوحة في {channel.mention}", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("❌ متغير DISCORD_TOKEN غير موجود في Environment Variables")
    bot.run(TOKEN)
