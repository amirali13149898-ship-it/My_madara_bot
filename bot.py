import os
import re
import io
import html as htmllib
import time
import asyncio
import threading
import requests
from urllib.parse import urljoin
from flask import Flask
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters,
)

# ================== تنظیمات ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ALLOWED_IDS = [int(x.strip()) for x in os.getenv("ALLOWED_IDS", "").split(",") if x.strip()]
PORT = int(os.getenv("PORT", 10000))

_raw_channel = os.getenv("CHANNEL_ID", "@Manhwa_Hub_News").strip()
CHANNEL_ID = int(_raw_channel) if _raw_channel.lstrip("-").isdigit() else _raw_channel
CHANNEL_TAG = os.getenv("CHANNEL_TAG", "@Manhwa_Hub_News").strip()

SITE_ROOT = os.getenv("SITE_URL", "https://manhwahub-tau.vercel.app/").strip()
ARCHIVE_URL = os.getenv("ARCHIVE_URL", SITE_ROOT.rstrip("/") + "/manhwas").strip()

# فوتر و دکمه‌ی خودکار زیر پیام‌هایی که دستی تو کانال می‌ذاری (مثل قبل)
FOOTER_TEXT = os.getenv("CHANNEL_FOOTER", "").strip()
BUTTON_ENABLED = os.getenv("CHANNEL_BUTTON_ENABLED", "1").strip() != "0"
BUTTON_TEXT = os.getenv("CHANNEL_BUTTON_TEXT", "🌐 بازکردن سایت").strip()
BUTTON_URL = os.getenv("CHANNEL_BUTTON_URL", SITE_ROOT).strip()

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096

app = Flask(__name__)


@app.route("/")
def home():
    return "Manhwa Channel Bot is running ✅", 200


# ================== ابزارهای کمکی ==================
DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
# ایموجی‌ها حذف می‌شن؛ نیم‌فاصله (200c) عمداً دست نمی‌خوره
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200d]")


def is_allowed(user_id: int) -> bool:
    return user_id in ALLOWED_IDS


def clean(s) -> str:
    s = _EMOJI_RE.sub("", s or "")
    return re.sub(r"\s+", " ", s).strip()


def make_hashtag(text: str) -> str:
    if not text or text == "—":
        return ""
    cleaned = "".join(c for c in text if c.isalnum() or c in "آابپتثجچحخدذرزژسشصضطظعغفقکگلمنوهیء ")
    return "#" + cleaned.replace(" ", "_")


def parse_genres(text: str) -> list:
    text = text or ""
    if re.search(r"[#,،;\n]", text):
        parts = re.split(r"[#,،;\n]+", text)
    else:
        parts = text.split()
    out = []
    for p in parts:
        p = clean(p)
        if p and p not in out:
            out.append(p)
    return out


def parse_int(text: str):
    m = re.search(r"\d+", (text or "").translate(DIGITS))
    return int(m.group()) if m else None


def extract_summary(lines: list):
    for i, l in enumerate(lines):
        if "خلاصه" in l:
            first = re.split(r"[:：]", l, maxsplit=1)
            buf = [first[1].strip()] if len(first) > 1 and first[1].strip() else []
            for nl in lines[i + 1:]:
                if re.search(r"برای مشاهده|Comic ?Screen|@\w+", nl):
                    break
                buf.append(nl)
            s = "\n".join(buf).strip()
            s = re.sub(r"^[«\"“]+|[»\"”]+$", "", s).strip()
            s = re.sub(r"\n{2,}", "\n", s)
            return s or None
    return None


def parse_post(text: str) -> dict:
    """از متن یه پست کانال (فوروارد یا کپی‌شده) هر چی پیدا بشه برمی‌داره. چیزی که پیدا نشه None می‌مونه."""
    lines = [l.strip() for l in (text or "").splitlines()]
    d = {"fa": None, "en": None, "summary": None, "genres": None, "rating": None,
         "chapters": None, "status": None, "ended": None}

    fa_idx = None
    for i, l in enumerate(lines):
        m = re.search(r"مانهوا\s*[:：]\s*(.+)", l)
        if m:
            d["fa"] = clean(m.group(1)) or None
            fa_idx = i
            break

    if fa_idx is not None:
        for l in lines[fa_idx + 1: fa_idx + 4]:
            c = clean(l)
            if c and re.search(r"[A-Za-z]", c) and ":" not in c and "：" not in c:
                d["en"] = c
                break
    if not d["en"]:
        for l in lines:
            m = re.match(r"#([A-Za-z0-9_]+)$", l)
            if m:
                d["en"] = m.group(1).replace("_", " ")
                break

    d["summary"] = extract_summary(lines)

    for l in lines:
        if "ژانر" in l:
            parts = re.split(r"[:：]", l, maxsplit=1)
            if len(parts) > 1:
                g = parse_genres(parts[1])
                if g:
                    d["genres"] = g
            break

    for l in lines:
        if "نمره" in l or "امتیاز" in l:
            m = re.search(r"\d+(?:[.,٫]\d+)?", l.translate(DIGITS))
            if m:
                d["rating"] = m.group().replace(",", ".").replace("٫", ".")
            break
    return d



# ================== کاور از لینک (پیش‌نمایش لینک) ==================
_UA = {"User-Agent": "Mozilla/5.0"}
_OG_RES = [
    re.compile(r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]*content=["\']([^"\']+)', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\']', re.I),
]


def extract_urls(msg) -> list:
    """لینک‌های پیام به ترتیب (تلگرام پیش‌نمایش رو از اولین لینک می‌سازه)."""
    urls = []
    lp = getattr(msg, "link_preview_options", None)
    if lp and getattr(lp, "url", None):
        urls.append(lp.url)
    types = [MessageEntity.URL, MessageEntity.TEXT_LINK]
    try:
        ents = msg.parse_entities(types=types) if msg.text else msg.parse_caption_entities(types=types)
    except Exception:
        ents = {}
    for e, t in sorted(ents.items(), key=lambda kv: kv[0].offset):
        u = e.url if e.type == MessageEntity.TEXT_LINK else t
        if u and not re.match(r"^https?://", u):
            u = "https://" + u
        if u and u not in urls:
            urls.append(u)
    return urls


def fetch_cover_from_url(url: str):
    """اگه لینک خودش عکسه همونو برمی‌گردونه، اگه صفحه‌ست عکس og:image/twitter:image رو دانلود می‌کنه."""
    r = requests.get(url, headers=_UA, timeout=20)
    r.raise_for_status()
    if r.headers.get("Content-Type", "").lower().startswith("image/"):
        return r.content if len(r.content) < 15_000_000 else None
    page = r.text
    img_url = None
    for rx in _OG_RES:
        m = rx.search(page)
        if m:
            img_url = urljoin(r.url, htmllib.unescape(m.group(1)))
            break
    if not img_url:
        return None
    r2 = requests.get(img_url, headers=_UA, timeout=25)
    r2.raise_for_status()
    return r2.content if len(r2.content) < 15_000_000 else None


def _to_jpeg_bytes(data: bytes) -> bytes:
    from PIL import Image
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((2000, 2000))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()


async def get_cover(url: str):
    try:
        return await asyncio.to_thread(fetch_cover_from_url, url)
    except Exception as e:
        print(f"دریافت کاور از لینک نشد ({url}): {e}")
        return None


# ================== ساخت پست ==================
def build_caption(d: dict, max_len: int) -> str:
    fa = d["fa"]
    en = d.get("en") or "—"
    genres = " ".join(f"#{g.replace(' ', '_')}" for g in d["genres"]) if d.get("genres") else "—"
    symbol = "🔚" if d.get("ended") else "🔄"
    chapter_line = f"𓆩 chapter 01_{d['chapters']:02d}{symbol}"
    rating_line = f"نمره : {d['rating']}\n" if d.get("rating") else ""
    fa_tag, en_tag = make_hashtag(fa), make_hashtag(en)

    def build(summ):
        return (
            f"👤اسم فارسی مانهوا : {fa}\n"
            f"👤 اسم انگلیسی مانهوا : {en}\n"
            f"⛓ژانر ها : {genres}\n"
            f"{rating_line}"
            f"👁وضعیت پخش : {d['status']}\n"
            f"نحوه پیدا کردن : {fa_tag} {en_tag}\n"
            f"خلاصه :\n«{summ}»\n\n"
            f"{chapter_line}\n\n"
            f"🗣️{CHANNEL_TAG}\n{en_tag}"
        )

    summary = (d.get("summary") or "—").strip()
    caption = build(summary)
    if len(caption) > max_len:
        cut = len(caption) - max_len + 3
        summary = summary[: max(0, len(summary) - cut)].rstrip() + "..."
        caption = build(summary)
    return caption


def channel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌐 بازکردن سایت", url=SITE_ROOT),
            InlineKeyboardButton("📚 آرشیو مانهواها", url=ARCHIVE_URL),
        ],
    ])


def _send_button() -> list:
    return [InlineKeyboardButton("📢 ارسال به کانال", callback_data="pub")]


def preview_keyboard() -> InlineKeyboardMarkup:
    rows = [list(r) for r in channel_keyboard().inline_keyboard]
    rows.append(_send_button())
    return InlineKeyboardMarkup(rows)


def _keyboard_with_row(query, buttons: list) -> InlineKeyboardMarkup:
    rows = [list(r) for r in query.message.reply_markup.inline_keyboard[:-1]]
    rows.append(buttons)
    return InlineKeyboardMarkup(rows)


async def send_photo_safe(bot, chat_id, photo, caption, kb):
    try:
        return await bot.send_photo(chat_id, photo, caption=caption, reply_markup=kb)
    except Exception as e:
        if isinstance(photo, (bytes, bytearray)):
            print(f"ارسال مستقیم عکس نشد ({e})؛ تبدیل به JPEG...")
            jpg = await asyncio.to_thread(_to_jpeg_bytes, bytes(photo))
            return await bot.send_photo(chat_id, jpg, caption=caption, reply_markup=kb)
        raise


async def send_preview(bot, chat_id, d: dict):
    kb = preview_keyboard()
    if d.get("photo"):
        await send_photo_safe(bot, chat_id, d["photo"], build_caption(d, CAPTION_LIMIT), kb)
    else:
        await bot.send_message(chat_id, build_caption(d, TEXT_LIMIT), reply_markup=kb)


# ================== مراحل پرسیدن ==================
PROMPTS = {
    "fa": "اسم فارسی مانهوا رو نتونستم پیدا کنم. بنویسش:",
    "en": "اسم انگلیسی مانهوا رو نتونستم پیدا کنم. بنویسش:",
    "summary": "خلاصه‌ی داستان رو نتونستم پیدا کنم. بنویسش:",
    "genres": "🏷 ژانرها رو بفرست (با کاما یا فاصله جدا کن). مثلاً: اکشن، درام\nاگه نمی‌خوای: -",
    "chapters": "🔢 تعداد چپترها چندتاست؟ (فقط عدد)",
}
ORDER = ["fa", "en", "summary", "genres", "chapters"]


async def ask_next(bot, chat_id, d: dict):
    for key in ORDER:
        if d.get(key) is None:
            d["step"] = key
            await bot.send_message(chat_id, PROMPTS[key])
            return
    if d.get("status") is None:
        d["step"] = "status"
        await bot.send_message(chat_id, "وضعیت پخش؟", reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 ادامه دارد", callback_data="st_on"),
            InlineKeyboardButton("🔚 پایان یافته", callback_data="st_end"),
        ]]))
        return
    if "photo" not in d:
        d["step"] = "cover"
        await bot.send_message(chat_id, "🖼 پست کاور نداشت. عکس کاور رو بفرست، یا اگه نمی‌خوای: -")
        return
    d["step"] = "done"
    await send_preview(bot, chat_id, d)


# ================== دستورات ==================
HELP_TEXT = (
    "📖 راهنما:\n\n"
    "📩 «از روی پست»: یه پست کانال (با عکس و متن) برام فوروارد کن یا متنش رو بفرست؛ "
    "اسم فارسی و انگلیسی، خلاصه و کاور رو خودم برمی‌دارم.\n"
    "✍️ «ساخت دستی»: همه‌چیز رو مرحله‌به‌مرحله ازت می‌پرسم.\n\n"
    "تو هر دو حالت، ژانرها (اگه تو پست نبود)، تعداد چپترها و وضعیت پخش رو ازت می‌پرسم. "
    "بعد پیش‌نمایش میاد و با «📢 ارسال به کانال» منتشر می‌شه.\n\n"
    "/start → باز کردن پنل\n/cancel → لغو پست فعلی"
)
PANEL_TEXT = "🎛 پنل پست‌سازی فعال شد.\nاز دکمه‌های پایین صفحه یکی رو انتخاب کن 👇"

BTN_NEW = "📩 ساخت پست از روی پست"
BTN_MANUAL = "✍️ ساخت دستی"
BTN_HELP = "📖 راهنما"
BTN_CANCEL = "❌ لغو پست فعلی"
PANEL_BUTTONS = {BTN_NEW, BTN_MANUAL, BTN_HELP, BTN_CANCEL}


def panel_keyboard() -> ReplyKeyboardMarkup:
    """پنل کیبوردی ثابت (دکمه‌های پایین صفحه، کنار جای تایپ)."""
    return ReplyKeyboardMarkup(
        [[BTN_NEW], [BTN_MANUAL], [BTN_HELP, BTN_CANCEL]],
        resize_keyboard=True,
        is_persistent=False,
        input_field_placeholder="یکی از گزینه‌ها رو انتخاب کن...",
    )


def empty_draft() -> dict:
    return {"fa": None, "en": None, "summary": None, "genres": None, "rating": None,
            "chapters": None, "status": None, "ended": None}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("شما مجاز به استفاده از این بات نیستید.")
        return
    context.user_data.pop("draft", None)
    await update.message.reply_text(PANEL_TEXT, reply_markup=panel_keyboard())


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    context.user_data.pop("draft", None)
    await update.message.reply_text("لغو شد ✅", reply_markup=panel_keyboard())


async def handle_panel_button(msg, context: ContextTypes.DEFAULT_TYPE, text: str):
    if text == BTN_NEW:
        context.user_data["draft"] = {"step": "await_post"}
        await msg.reply_text(
            "📩 پست کانال رو (با عکس و متن) برام فوروارد کن، یا متنش رو بفرست.\n"
            "متن باید خط «مانهوا: ...» داشته باشه.",
            reply_markup=panel_keyboard(),
        )
    elif text == BTN_MANUAL:
        d = empty_draft()
        context.user_data["draft"] = d
        await msg.reply_text("✍️ ساخت دستی شروع شد.", reply_markup=panel_keyboard())
        await ask_next(context.bot, msg.chat_id, d)
    elif text == BTN_HELP:
        await msg.reply_text(HELP_TEXT, reply_markup=panel_keyboard())
    elif text == BTN_CANCEL:
        context.user_data.pop("draft", None)
        await msg.reply_text("لغو شد ✅", reply_markup=panel_keyboard())


# ================== دریافت پیام‌ها ==================
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not is_allowed(update.effective_user.id):
        return
    chat_id = msg.chat_id
    text = (msg.text or msg.caption or "").strip()
    photo = msg.photo[-1].file_id if msg.photo else None
    if text in PANEL_BUTTONS:
        await handle_panel_button(msg, context, text)
        return

    d = context.user_data.get("draft")

    # مرحله‌ی کاور
    if d and d.get("step") == "cover":
        if photo:
            d["photo"] = photo
        elif text in ("-", "ندارد", "بدون"):
            d["photo"] = None
        elif re.match(r"^(https?://|t\.me/)\S+$", text):
            img = await get_cover(text if text.startswith("http") else "https://" + text)
            if not img:
                await msg.reply_text("از این لینک عکسی درنیومد. عکس رو مستقیم بفرست، یا برای رد کردن: -")
                return
            d["photo"] = img
        else:
            await msg.reply_text("عکس کاور (یا لینکش) رو بفرست، یا برای رد کردن: -")
            return
        await ask_next(context.bot, chat_id, d)
        return

    # پست جدید (حتی وسط یه پست نیمه‌کاره)
    if text and re.search(r"مانهوا\s*[:：]", text):
        d = parse_post(text)
        if photo:
            d["photo"] = photo
        else:
            urls = extract_urls(msg)
            if urls:
                img = await get_cover(urls[0])
                if img:
                    d["photo"] = img
        context.user_data["draft"] = d
        found = []
        found.append(f"فارسی: {d['fa']}" if d["fa"] else "فارسی: ❌")
        found.append(f"انگلیسی: {d['en']}" if d["en"] else "انگلیسی: ❌")
        found.append("خلاصه: ✅" if d["summary"] else "خلاصه: ❌")
        found.append("کاور: ✅" if d.get("photo") else "کاور: ❌")
        if d["genres"]:
            found.append("ژانر: " + "، ".join(d["genres"]))
        await msg.reply_text("این‌ها رو از پست برداشتم:\n" + "\n".join(found))
        await ask_next(context.bot, chat_id, d)
        return

    # جواب یکی از سؤال‌ها
    if d and d.get("step") in ORDER and text:
        step = d["step"]
        if step == "chapters":
            n = parse_int(text)
            if not n or n <= 0:
                await msg.reply_text("یه عدد درست بفرست. مثلاً 25")
                return
            d["chapters"] = n
        elif step == "genres":
            d["genres"] = [] if text in ("-", "ندارد", "بدون") else parse_genres(text)
        else:
            d[step] = text
        await ask_next(context.bot, chat_id, d)
        return

    if d and d.get("step") == "await_post":
        await msg.reply_text("خط «مانهوا: ...» تو متن پیدا نشد. پست رو دوباره فوروارد کن یا از «ساخت دستی» استفاده کن.",
                             reply_markup=panel_keyboard())
        return
    await msg.reply_text(PANEL_TEXT, reply_markup=panel_keyboard())


async def status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(query.from_user.id):
        return
    d = context.user_data.get("draft")
    if not d or d.get("step") != "status":
        await query.edit_message_text("این مرحله دیگه منقضی شده. پست رو دوباره بفرست.")
        return
    if query.data == "st_end":
        d["status"], d["ended"] = "پایان یافته", True
    else:
        d["status"], d["ended"] = "ادامه دارد", False
    await query.edit_message_text(f"وضعیت پخش: {d['status']} ✅")
    await ask_next(context.bot, query.message.chat_id, d)


# ================== ارسال به کانال ==================
async def ask_publish_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_allowed(query.from_user.id):
        await query.answer()
        return
    if query.message is None or query.message.reply_markup is None:
        await query.answer("این پیام دیگه در دسترس نیست.", show_alert=True)
        return
    published = context.bot_data.setdefault("published", set())
    if (query.message.chat_id, query.message.message_id) in published:
        await query.answer("این پست قبلاً به کانال فرستاده شده ✅", show_alert=True)
        return
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=_keyboard_with_row(query, [
        InlineKeyboardButton("✅ آره، بفرست", callback_data="pubok"),
        InlineKeyboardButton("❌ لغو", callback_data="pubno"),
    ]))


async def cancel_publish_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_allowed(query.from_user.id):
        await query.answer()
        return
    await query.answer("لغو شد.")
    if query.message is None or query.message.reply_markup is None:
        return
    await query.edit_message_reply_markup(reply_markup=_keyboard_with_row(query, _send_button()))


async def publish_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پیش‌نمایش رو عیناً (عکس + کپشن) تو کانال کپی می‌کنه، با دکمه‌های شیشه‌ای سالم."""
    query = update.callback_query
    if not is_allowed(query.from_user.id):
        await query.answer()
        return
    if query.message is None or query.message.reply_markup is None:
        await query.answer("این پیام دیگه در دسترس نیست.", show_alert=True)
        return
    published = context.bot_data.setdefault("published", set())
    key = (query.message.chat_id, query.message.message_id)
    if key in published:
        await query.answer("این پست قبلاً به کانال فرستاده شده ✅", show_alert=True)
        return
    await query.answer("در حال ارسال به کانال...")
    try:
        await context.bot.copy_message(
            chat_id=CHANNEL_ID,
            from_chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            reply_markup=channel_keyboard(),
        )
        published.add(key)
        await query.edit_message_reply_markup(reply_markup=_keyboard_with_row(
            query, [InlineKeyboardButton("✅ به کانال ارسال شد", callback_data="noop")]
        ))
        context.user_data.pop("draft", None)
        await context.bot.send_message(query.from_user.id, "پست تو کانال منتشر شد ✅\n\n" + PANEL_TEXT,
                                       reply_markup=panel_keyboard())
    except Exception as e:
        print(f"خطا در ارسال به کانال: {e}")
        try:
            await query.edit_message_reply_markup(reply_markup=_keyboard_with_row(query, _send_button()))
        except Exception:
            pass
        await context.bot.send_message(
            query.from_user.id,
            f"❌ ارسال به کانال ناموفق بود:\n{e}\n\n"
            "چک کن که بات تو کانال ادمین باشه و دسترسی «ارسال پیام» داشته باشه، "
            f"و مقدار CHANNEL_ID درست باشه (الان: {CHANNEL_ID}).",
        )


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


# ================== فوتر خودکار زیر پیام‌های دستی کانال ==================
def _is_our_channel(chat) -> bool:
    if isinstance(CHANNEL_ID, int):
        return chat.id == CHANNEL_ID
    return (chat.username or "").lower() == str(CHANNEL_ID).lstrip("@").lower()


def _markup_with_button(old_markup):
    rows = [list(r) for r in old_markup.inline_keyboard] if old_markup else []
    if any(getattr(b, "url", None) == BUTTON_URL for r in rows for b in r):
        return None
    rows.append([InlineKeyboardButton(BUTTON_TEXT, url=BUTTON_URL)])
    return InlineKeyboardMarkup(rows)


async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg or not _is_our_channel(msg.chat):
        return
    want_button = BUTTON_ENABLED and bool(BUTTON_URL)
    if not FOOTER_TEXT and not want_button:
        return

    if msg.text is not None:
        is_text, original, entities, limit = True, msg.text, msg.entities, TEXT_LIMIT
    elif msg.photo or msg.video or msg.document or msg.animation or msg.audio or msg.voice:
        if msg.media_group_id:
            return
        is_text, original, entities, limit = False, msg.caption or "", msg.caption_entities, CAPTION_LIMIT
    else:
        return

    new_text = None
    if FOOTER_TEXT and FOOTER_TEXT not in original:
        candidate = f"{original}\n\n{FOOTER_TEXT}" if original else FOOTER_TEXT
        if len(candidate) <= limit:
            new_text = candidate

    new_markup = _markup_with_button(msg.reply_markup) if want_button else None
    if new_text is None and new_markup is None:
        return

    ents = list(entities) if entities else None
    markup = new_markup or msg.reply_markup
    chat_id, mid = msg.chat.id, msg.message_id
    try:
        if new_text is not None and is_text:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=mid, text=new_text,
                                                entities=ents, reply_markup=markup)
        elif new_text is not None:
            await context.bot.edit_message_caption(chat_id=chat_id, message_id=mid, caption=new_text,
                                                   caption_entities=ents, reply_markup=markup)
        else:
            await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=mid, reply_markup=markup)
    except Exception as e:
        print(f"خطا در ادیت پیام {mid}: {e}")


# ================== اجرا ==================
def build_application() -> Application:
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("cancel", cancel))

    application.add_handler(CallbackQueryHandler(status_callback, pattern=r"^st_(on|end)$"))
    application.add_handler(CallbackQueryHandler(ask_publish_callback, pattern=r"^pub$"))
    application.add_handler(CallbackQueryHandler(publish_callback, pattern=r"^pubok$"))
    application.add_handler(CallbackQueryHandler(cancel_publish_callback, pattern=r"^pubno$"))
    application.add_handler(CallbackQueryHandler(noop_callback, pattern=r"^noop$"))

    application.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, channel_post_handler))
    application.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND & (filters.TEXT | filters.PHOTO), on_message
    ))
    return application


def run_flask():
    app.run(host="0.0.0.0", port=PORT, use_reloader=False)


def run_bot():
    while True:
        try:
            asyncio.set_event_loop(asyncio.new_event_loop())
            application = build_application()
            application.run_polling(drop_pending_updates=True)
            break
        except Exception as e:
            print("پولینگ بات کرش کرد، ۵ ثانیه دیگه دوباره تلاش می‌کنیم:", e)
            time.sleep(5)


if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    print(f"Flask در حال اجرا روی پورت {PORT}...")
    if not BOT_TOKEN or not ALLOWED_IDS:
        print("❌ BOT_TOKEN یا ALLOWED_IDS تنظیم نشده!")
        while True:
            time.sleep(3600)
    else:
        print(f"بات شروع شد | آیدی‌های مجاز: {ALLOWED_IDS}")
        run_bot()
