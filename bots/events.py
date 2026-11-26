import asyncio
import csv
import io
import os
import re
import sqlite3
import sys
import traceback
from datetime import datetime, timedelta, timezone

import discord
from aiohttp import web
from discord.ext import commands

TOKEN_ENV = "EVENT_TOKEN"   # التوكن من متغير EVENT_TOKEN (التشغيل من main.py الرئيسي)
BOT_NAME = "Events"
OWNER_ID = 912349519990095892
DB_PATH = os.environ.get("DB_PATH", "subscribers.db")
PORT = int(os.environ.get("PORT", "8080"))
COLOR = discord.Color.from_rgb(155, 89, 182)

# ═════════════ إعدادات أمر «منشن الكل» ═════════════
# حط هنا ايديات الرومات اللي تبي البوت يمنشن فيها everyone (ارقام بدون علامات)
# ملاحظة: نفس هذي الرومات تُستخدم لمنشن العضو الجديد عند دخوله السيرفر
MENTION_CHANNEL_IDS = [
    1438114964794642543,
    1550679256172994660,
    1519817140084019271,
    1548600970487791616,
    1436723940029497447,
    1545136298849865849,
    1465041190163578972,
    1569670243402645616,
    
   
    


]
MENTION_ALLOWED_ID = 912349519990095892  # الوحيد اللي يقدر يستخدم الأمر
MENTION_COMMAND = "11"            # اسم الأمر (بدون بريفكس)
MENTION_DELETE_AFTER = 3                 # ثواني قبل حذف المنشن
# ═══════════════════════════════════════════════════


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ───────────────────────── قاعدة البيانات ─────────────────────────
class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS subscribers (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                username TEXT, subscribed_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id))"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
                title TEXT, sent INTEGER, failed INTEGER, at TEXT NOT NULL)"""
        )
        self.conn.commit()

    def add(self, gid, uid, name) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO subscribers VALUES (?,?,?,?)", (gid, uid, name, now_iso())
        )
        self.conn.commit()
        return cur.rowcount > 0

    def remove(self, gid, uid) -> bool:
        cur = self.conn.execute(
            "DELETE FROM subscribers WHERE guild_id=? AND user_id=?", (gid, uid)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get(self, gid, uid):
        return self.conn.execute(
            "SELECT user_id, username, subscribed_at FROM subscribers WHERE guild_id=? AND user_id=?",
            (gid, uid),
        ).fetchone()

    def all(self, gid):
        return self.conn.execute(
            "SELECT user_id, username, subscribed_at FROM subscribers WHERE guild_id=? ORDER BY subscribed_at",
            (gid,),
        ).fetchall()

    def count(self, gid, since: str | None = None) -> int:
        q = "SELECT COUNT(*) FROM subscribers WHERE guild_id=?"
        args = [gid]
        if since:
            q += " AND subscribed_at>=?"
            args.append(since)
        return self.conn.execute(q, args).fetchone()[0]

    def clear(self, gid) -> int:
        cur = self.conn.execute("DELETE FROM subscribers WHERE guild_id=?", (gid,))
        self.conn.commit()
        return cur.rowcount

    def log_broadcast(self, gid, title, sent, failed):
        self.conn.execute(
            "INSERT INTO broadcasts (guild_id,title,sent,failed,at) VALUES (?,?,?,?,?)",
            (gid, title, sent, failed, now_iso()),
        )
        self.conn.commit()

    def last_broadcasts(self, gid, n=5):
        return self.conn.execute(
            "SELECT title, sent, failed, at FROM broadcasts WHERE guild_id=? ORDER BY id DESC LIMIT ?",
            (gid, n),
        ).fetchall()


db = Database(DB_PATH)


# ───────────────────────── الواجهات (الأزرار) ─────────────────────────
class SubscribeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="ارسل لي فعاليات السيرفر", emoji="❄️", style=discord.ButtonStyle.primary, custom_id="rav:subscribe")
    async def subscribe(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = interaction.user
        if not db.add(interaction.guild_id, user.id, str(user)):
            return await interaction.response.send_message("الإرسال مفعّل لك بالفعل ✅", ephemeral=True)
        try:
            await user.send(
                embed=discord.Embed(
                    title="تم تفعيل الإرسال 🤘🏻",
                    description=f"راح توصلك إشعارات الفعاليات والأخبار من **{interaction.guild.name}** هنا في الخاص.",
                    color=COLOR,
                )
            )
            await interaction.response.send_message("شطور 🎉 راح توصلك فعاليات السيرفر بالخاص .", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "تم التفعيل ، لكن الخاص عندك مقفل ⚠️\n"
                "افتح: إعدادات السيرفر ← الخصوصية ← السماح بالرسائل الخاصة من أعضاء السيرفر، "
                "وإلا ما راح توصلك الإشعارات.",
                ephemeral=True,
            )

    @discord.ui.button(label="إيقاف إرسال الفعاليات", emoji="🔕", style=discord.ButtonStyle.secondary, custom_id="rav:unsubscribe")
    async def unsubscribe(self, interaction: discord.Interaction, button: discord.ui.Button):
        removed = db.remove(interaction.guild_id, interaction.user.id)
        await interaction.response.send_message(
            "تم إيقاف إرسال الفعاليات لك 🔕" if removed else "الإرسال غير مفعّل لك أصلاً.", ephemeral=True
        )


class ConfirmView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.value: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("هذا الأمر لصاحب البوت فقط.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="تأكيد", emoji="✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="إلغاء", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        await interaction.response.defer()
        self.stop()


# ───────────────────────── البوت ─────────────────────────
class SubscribeBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True  # مطلوب لاكتشاف دخول الأعضاء الجدد (فعّل Server Members Intent من بوابة المطورين)
        super().__init__(
            command_prefix="",  # بدون أي علامة: اكتب اسم الأمر مباشرة
            intents=intents,
            help_command=None,
            owner_ids={OWNER_ID} if OWNER_ID else None,
            case_insensitive=True,
        )

    async def setup_hook(self):
        self.add_view(SubscribeView())
        await self.tree.sync()  # شجرة فاضية = يمسح أي أوامر سلاش قديمة
        # السيرفر الصغير (Render) يشغّله main.py الرئيسي مرة واحدة لكل البوتات

    async def on_ready(self):
        print(f"Logged in as {self.user} ({self.user.id})")

    # ───── أمر «منشن الكل» ─────
    async def _mention_one(self, channel_id: int):
        """يرسل everyone في روم واحد ثم يحذفه بعد المدة المحددة."""
        try:
            channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
            msg = await channel.send(
                "@everyone",
                allowed_mentions=discord.AllowedMentions(everyone=True),
            )
        except Exception as e:
            print(f"[mention-all] فشل الإرسال في الروم {channel_id}: {e!r}")
            return channel_id, False
        await asyncio.sleep(MENTION_DELETE_AFTER)
        try:
            await msg.delete()
        except Exception as e:
            print(f"[mention-all] فشل حذف الرسالة في الروم {channel_id}: {e!r}")
        return channel_id, True

    async def handle_mention_all(self, message: discord.Message):
        if not MENTION_CHANNEL_IDS:
            return await message.reply("ما فيه رومات محددة داخل الكود.", mention_author=False)
        results = await asyncio.gather(*(self._mention_one(cid) for cid in MENTION_CHANNEL_IDS))
        failed = [cid for cid, ok in results if not ok]
        try:
            await message.add_reaction("✅" if not failed else "⚠️")
        except discord.HTTPException:
            pass
        if failed:
            ids = "\n".join(f"<#{cid}> (`{cid}`)" for cid in failed)
            await message.reply(
                f"⚠️ ما قدرت أرسل في هذي الرومات (تأكد من الايدي وصلاحيات البوت):\n{ids}",
                mention_author=False,
                delete_after=20,
            )

    # ───── منشن العضو الجديد ─────
    async def _mention_member_one(self, channel_id: int, member: discord.Member):
        """يمنشن العضو الجديد في روم واحد ثم يحذف المنشن بعد المدة المحددة."""
        try:
            channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
            # نتأكد إن الروم تابع لنفس السيرفر اللي دخله العضو
            if getattr(channel, "guild", None) is None or channel.guild.id != member.guild.id:
                return
            await channel.send(
                member.mention,
                allowed_mentions=discord.AllowedMentions(users=[member]),
                delete_after=MENTION_DELETE_AFTER,
            )
        except Exception as e:
            print(f"[member-join] فشل المنشن في الروم {channel_id}: {e!r}")

    async def on_member_join(self, member: discord.Member):
        if member.bot or not MENTION_CHANNEL_IDS:
            return
        await asyncio.gather(
            *(self._mention_member_one(cid, member) for cid in MENTION_CHANNEL_IDS),
            return_exceptions=True,
        )

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if " ".join(message.content.split()) == MENTION_COMMAND:
            if message.guild is not None and message.author.id == MENTION_ALLOWED_ID:
                await self.handle_mention_all(message)
            return  # أي شخص ثاني: تجاهل صامت
        await self.process_commands(message)

    async def on_command_error(self, ctx: commands.Context, error):
        if isinstance(error, (commands.CommandNotFound, commands.CheckFailure)):
            return  # تجاهل صامت: أي رسالة عادية أو شخص مو الأونر
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            usage = f"{ctx.invoked_with} {ctx.command.signature}".strip()
            return await ctx.reply(f"⚠️ طريقة الاستخدام الصحيحة:\n`{usage}`", mention_author=False)
        original = getattr(error, "original", error)
        print("Command error:", repr(original), file=sys.stderr, flush=True)
        traceback.print_exception(type(original), original, original.__traceback__, file=sys.stderr)
        sys.stderr.flush()
        await ctx.reply("حصل خطأ غير متوقع، راجع اللوق.", mention_author=False)


bot = SubscribeBot()


@bot.check
async def owner_only(ctx: commands.Context) -> bool:
    return ctx.guild is not None and await bot.is_owner(ctx.author)


# ───────────────────────── دوال مساعدة ─────────────────────────
def parse_notice(text: str, guild: discord.Guild, attachments) -> tuple[str, str]:
    """'العنوان | النص' أو النص فقط. يرجع (العنوان، الرسالة العادية الجاهزة للإرسال)."""
    title, body = None, text
    if "|" in text:
        t, b = text.split("|", 1)
        if t.strip() and b.strip():
            title, body = t.strip(), b.strip()
    title = (title or " # فعالية جديدة ❄️ ")[:256]
    content = f"**{title}**\n{body.strip()[:1600]}"
    for a in attachments:
        if a.content_type and a.content_type.startswith("image/"):
            content += f"\n{a.url}"
            break
    return title, content[:2000]


async def run_broadcast(guild: discord.Guild, title: str, content: str, status: discord.Message):
    subs = db.all(guild.id)
    sent = failed = 0
    for i, (uid, _, _) in enumerate(subs, 1):
        try:
            user = bot.get_user(uid) or await bot.fetch_user(uid)
            await user.send(content)
            sent += 1
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            failed += 1
        if i % 10 == 0:
            try:
                await status.edit(content=f"⏳ جاري الإرسال... {i}/{len(subs)}")
            except discord.HTTPException:
                pass
        await asyncio.sleep(1.2)  # حماية من الريت ليمت
    db.log_broadcast(guild.id, title, sent, failed)
    await status.edit(
        content=f"✅ انتهى الإرسال\nوصلت: **{sent}**\nفشلت (الخاص مقفل أو الحساب محذوف): **{failed}**"
    )


# ───────────────────────── الأوامر ─────────────────────────
@bot.command(name="اوامر", aliases=["الاوامر", "مساعدة"])
async def help_cmd(ctx):
    e = discord.Embed(title="📖 أوامر البوت (للأونر فقط)", color=COLOR)
    e.description = (
        "تكتب الأمر مباشرة بدون أي علامة.\n\n"
        "**🔔 اللوحة**\n"
        "`لوحة` ← يرسل لوحة الفعاليات في الروم الحالي\n"
        "`لوحة #الروم` ← يرسلها في روم ثاني\n\n"
        "**📣 المنشن**\n"
        "`منشن الكل` ← يرسل everyone في الرومات المحددة بالكود ويحذفه بعد 3 ثواني\n\n"
        "**📨 الإرسال**\n"
        "`ارسل العنوان | النص` ← يرسل لكل المشتركين بالخاص (بعد معاينة وتأكيد)\n"
        "`ارسل النص` ← بدون عنوان\n"
        "تقدر ترفق صورة مع الرسالة وتنرسل معها\n"
        "`تجربة العنوان | النص` ← يرسل لك أنت بس لتشوف الشكل\n\n"
        "**👥 المشتركين**\n"
        "`مشتركين` ← قائمة المشتركين\n"
        "`احصائيات` ← الأعداد والمشتركين الجدد\n"
        "`بحث @عضو` ← هل العضو مشترك؟\n"
        "`اضافة @عضو` أو `اضافة id1 id2 id3` ← إضافة مشترك أو أكثر يدوياً\n"
        "`حذف @عضو` ← حذف مشترك\n"
        "`مسح_الكل` ← حذف كل المشتركين (بتأكيد)\n"
        "`تصدير` ← ملف CSV نسخة احتياطية\n\n"
        "**🛠️ أدوات**\n"
        "`اخر_ارسال` ← آخر 5 إرسالات ونتائجها\n"
        "`بنق` ← سرعة اتصال البوت\n"
        "`اوامر` ← هذي القائمة"
    )
    await ctx.reply(embed=e, mention_author=False)


@bot.command(name="لوحة", aliases=["بانل"])
async def panel(ctx, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    embed = discord.Embed(
        title="🔔 فعاليات السيرفر",
        description="اضغط **ارسل لي فعاليات السيرفر** وراح توصلك كل الفعاليات والأخبار المهمة مباشرة في الخاص.\n\nتقدر توقف الإرسال في أي وقت.",
        color=COLOR,
    )
    if ctx.guild.icon:
        embed.set_thumbnail(url=ctx.guild.icon.url)
    await channel.send(embed=embed, view=SubscribeView())
    if channel == ctx.channel:
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass
    else:
        await ctx.reply(f"تم إرسال اللوحة في {channel.mention} ✅", mention_author=False)


@bot.command(name="ارسل", aliases=["اشعار", "بث"])
async def broadcast(ctx, *, text: str):
    total = db.count(ctx.guild.id)
    if total == 0:
        return await ctx.reply("ما فيه مشتركين حالياً.", mention_author=False)
    title, content = parse_notice(text, ctx.guild, ctx.message.attachments)
    view = ConfirmView(ctx.author.id)
    msg = await ctx.reply(
        f"هذي معاينة الرسالة، راح تنرسل إلى **{total}** مشترك. تأكيد؟\n\n{content}"[:2000],
        view=view, mention_author=False,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    await view.wait()
    if view.value is not True:
        return await msg.edit(content="تم الإلغاء ✖️" if view.value is False else "انتهى الوقت، ما تم الإرسال.", embed=None, view=None)
    await msg.edit(content="⏳ جاري الإرسال...", embed=None, view=None)
    await run_broadcast(ctx.guild, title, content, msg)


@bot.command(name="تجربة")
async def test_send(ctx, *, text: str):
    title, content = parse_notice(text, ctx.guild, ctx.message.attachments)
    try:
        await ctx.author.send(content)
        await ctx.reply("أرسلت لك التجربة بالخاص ✅", mention_author=False)
    except discord.Forbidden:
        await ctx.reply("خاصك مقفل، افتحه وجرب مرة ثانية.", mention_author=False)


@bot.command(name="مشتركين", aliases=["المشتركين"])
async def list_subs(ctx):
    subs = db.all(ctx.guild.id)
    if not subs:
        return await ctx.reply("ما فيه مشتركين حالياً.", mention_author=False)
    lines = [f"{i}. <@{u}> — {n} — {a[:10]}" for i, (u, n, a) in enumerate(subs, 1)]
    text = "\n".join(lines)
    if len(text) <= 3800:
        return await ctx.reply(
            embed=discord.Embed(title=f"المشتركين ({len(subs)})", description=text, color=COLOR),
            mention_author=False,
        )
    plain = "\n".join(f"{i}. {n} ({u}) — {a[:10]}" for i, (u, n, a) in enumerate(subs, 1))
    await ctx.reply(
        f"عدد المشتركين: **{len(subs)}**",
        file=discord.File(io.BytesIO(plain.encode("utf-8")), filename="subscribers.txt"),
        mention_author=False,
    )


@bot.command(name="احصائيات", aliases=["عدد", "احصائية"])
async def stats(ctx):
    gid = ctx.guild.id
    day = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    e = discord.Embed(title="📊 إحصائيات الفعاليات", color=COLOR)
    e.add_field(name="إجمالي المشتركين", value=str(db.count(gid)))
    e.add_field(name="آخر 24 ساعة", value=str(db.count(gid, day)))
    e.add_field(name="آخر 7 أيام", value=str(db.count(gid, week)))
    await ctx.reply(embed=e, mention_author=False)


@bot.command(name="بحث")
async def search(ctx, user: discord.User):
    row = db.get(ctx.guild.id, user.id)
    if row:
        await ctx.reply(f"✅ {user.mention} مشترك منذ {row[2][:10]}", mention_author=False)
    else:
        await ctx.reply(f"❌ {user.mention} غير مشترك.", mention_author=False)


@bot.command(name="اضافة")
async def add_sub(ctx, *, text: str):
    # يلقط كل الايديات من الرسالة (ايدي مجرد أو منشن)، ويشيل المكرر
    ids = list(dict.fromkeys(int(x) for x in re.findall(r"\d{17,20}", text)))
    if not ids:
        return await ctx.reply("⚠️ ما لقيت أي ايدي أو منشن في رسالتك.", mention_author=False)

    added, existing, invalid = [], [], []
    for uid in ids:
        try:
            user = bot.get_user(uid) or await bot.fetch_user(uid)
        except (discord.NotFound, discord.HTTPException):
            invalid.append(uid)
            continue
        if db.add(ctx.guild.id, user.id, str(user)):
            added.append(user.id)
        else:
            existing.append(user.id)

    parts = []
    if added:
        parts.append(f"✅ تمت إضافة **{len(added)}**:\n" + " ".join(f"<@{u}>" for u in added))
    if existing:
        parts.append(f"ℹ️ مشتركين أصلاً **{len(existing)}**:\n" + " ".join(f"<@{u}>" for u in existing))
    if invalid:
        parts.append(f"❌ ايديات غير صحيحة **{len(invalid)}**:\n" + " ".join(f"`{u}`" for u in invalid))
    parts.append("(ما تم التأكد إن الخاص مفتوح عندهم)")
    await ctx.reply(
        "\n\n".join(parts)[:2000],
        mention_author=False,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.command(name="حذف")
async def remove_sub(ctx, user: discord.User):
    if db.remove(ctx.guild.id, user.id):
        await ctx.reply(f"تم حذف {user.mention} من المشتركين ✅", mention_author=False)
    else:
        await ctx.reply("العضو غير مشترك.", mention_author=False)


@bot.command(name="مسح_الكل", aliases=["مسح"])
async def clear_all(ctx):
    total = db.count(ctx.guild.id)
    if total == 0:
        return await ctx.reply("ما فيه مشتركين أصلاً.", mention_author=False)
    view = ConfirmView(ctx.author.id)
    msg = await ctx.reply(f"⚠️ راح تنحذف قائمة **{total}** مشترك بالكامل. متأكد؟", view=view, mention_author=False)
    await view.wait()
    if view.value is True:
        db.clear(ctx.guild.id)
        await msg.edit(content="تم مسح كل المشتركين 🗑️", view=None)
    else:
        await msg.edit(content="تم الإلغاء ✖️", view=None)


@bot.command(name="تصدير", aliases=["نسخة"])
async def export(ctx):
    subs = db.all(ctx.guild.id)
    if not subs:
        return await ctx.reply("ما فيه مشتركين للتصدير.", mention_author=False)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id", "username", "subscribed_at"])
    w.writerows(subs)
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    await ctx.reply(file=discord.File(data, filename="subscribers.csv"), mention_author=False)


@bot.command(name="اخر_ارسال", aliases=["السجل"])
async def last_sends(ctx):
    rows = db.last_broadcasts(ctx.guild.id)
    if not rows:
        return await ctx.reply("ما فيه إرسالات سابقة.", mention_author=False)
    lines = [f"**{t}**\nوصلت: {s} | فشلت: {f} | {a[:16].replace('T', ' ')} UTC" for t, s, f, a in rows]
    await ctx.reply(embed=discord.Embed(title="🕘 آخر الإرسالات", description="\n\n".join(lines), color=COLOR), mention_author=False)


@bot.command(name="بنق")
async def ping(ctx):
    await ctx.reply(f"🏓 {round(bot.latency * 1000)}ms", mention_author=False)


def create_bot():
    return bot
