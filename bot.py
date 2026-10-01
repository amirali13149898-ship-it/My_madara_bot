import os
import re
import io
import json
import hashlib
import html as htmllib
import time
import asyncio
import threading
import requests
from urllib.parse import urljoin
from flask import Flask
from telegram.error import BadRequest
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
FOOTER_QUOTE = os.getenv("CHANNEL_FOOTER_QUOTE", "1").strip() != "0"
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
            f"{footer_line()}\n{en_tag}"
        )

    summary = (d.get("summary") or "—").strip()
    caption = build(summary)
    if len(caption) > max_len:
        cut = len(caption) - max_len + 3
        summary = summary[: max(0, len(summary) - cut)].rstrip() + "..."
        caption = build(summary)
    return caption



def footer_line() -> str:
    return f"🗣️{CHANNEL_TAG}"


def _u16(text: str) -> int:
    """طول متن بر حسب UTF-16 (واحدی که تلگرام برای offset موجودیت‌ها استفاده می‌کنه)."""
    return len(text.encode("utf-16-le")) // 2


def quote_entities(text: str):
    """خط فوتر (🗣️@کانال) رو داخل نقل‌قول (blockquote) می‌ذاره."""
    marker = footer_line()
    idx = text.rfind(marker)
    if idx < 0:
        return None
    return [MessageEntity(type=MessageEntity.BLOCKQUOTE, offset=_u16(text[:idx]), length=_u16(marker))]


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


async def send_photo_safe(bot, chat_id, photo, caption, kb, entities=None):
    try:
        return await bot.send_photo(chat_id, photo, caption=caption, caption_entities=entities, reply_markup=kb)
    except Exception as e:
        if isinstance(photo, (bytes, bytearray)):
            print(f"ارسال مستقیم عکس نشد ({e})؛ تبدیل به JPEG...")
            jpg = await asyncio.to_thread(_to_jpeg_bytes, bytes(photo))
            return await bot.send_photo(chat_id, jpg, caption=caption, caption_entities=entities, reply_markup=kb)
        raise


async def send_preview(bot, chat_id, d: dict):
    kb = preview_keyboard()
    if d.get("photo"):
        cap = build_caption(d, CAPTION_LIMIT)
        await send_photo_safe(bot, chat_id, d["photo"], cap, kb, quote_entities(cap))
    else:
        txt = build_caption(d, TEXT_LIMIT)
        await bot.send_message(chat_id, txt, entities=quote_entities(txt), reply_markup=kb)


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
    "📥 «افزودن مانهواهای قبلی»: پست‌های قدیمی کانال رو یکی‌یکی فوروارد کن تا تو لیست‌ها ثبت بشن؛ آخرش «ثبت و به‌روزرسانی لیست‌ها».\n\n"
    "پست‌های جدید خودکار تو لیست «پایان یافته» یا «در حال ترجمه» میرن و وقتی یه مانهوا تموم شد از لیست دوم به اولی منتقل می‌شه.\n\n"
    "/start → باز کردن پنل\n/cancel → لغو پست فعلی\n/refresh → به‌روزرسانی لیست‌ها"
)
PANEL_TEXT = "🎛 پنل پست‌سازی فعال شد.\nاز دکمه‌های پایین صفحه یکی رو انتخاب کن 👇"

BTN_NEW = "📩 ساخت پست از روی پست"
BTN_MANUAL = "✍️ ساخت دستی"
BTN_HELP = "📖 راهنما"
BTN_CANCEL = "❌ لغو پست فعلی"
BTN_IMPORT = "📥 افزودن مانهواهای قبلی به لیست"
BTN_REFRESH = "🔄 ثبت و به‌روزرسانی لیست‌ها"
PANEL_BUTTONS = {BTN_NEW, BTN_MANUAL, BTN_HELP, BTN_CANCEL, BTN_IMPORT, BTN_REFRESH}


def panel_keyboard() -> ReplyKeyboardMarkup:
    """پنل کیبوردی ثابت (دکمه‌های پایین صفحه، کنار جای تایپ)."""
    return ReplyKeyboardMarkup(
        [[BTN_NEW], [BTN_MANUAL], [BTN_IMPORT], [BTN_REFRESH], [BTN_HELP, BTN_CANCEL]],
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
    context.user_data.pop("import", None)
    await update.message.reply_text(PANEL_TEXT, reply_markup=panel_keyboard())


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    context.user_data.pop("draft", None)
    context.user_data.pop("import", None)
    await update.message.reply_text("لغو شد ✅", reply_markup=panel_keyboard())


async def refresh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await do_refresh(update.message, context)


async def handle_panel_button(msg, context: ContextTypes.DEFAULT_TYPE, text: str):
    if text == BTN_REFRESH:
        await do_refresh(msg, context)
        return
    if text == BTN_IMPORT:
        context.user_data.pop("draft", None)
        context.user_data["import"] = {"n": 0}
        await msg.reply_text(
            "📥 حالت افزودن روشنه.\nپست‌های قدیمی کانال رو (با کپشن) یکی‌یکی برام فوروارد کن.\n"
            "وقتی تموم شد «🔄 ثبت و به‌روزرسانی لیست‌ها» رو بزن.",
            reply_markup=panel_keyboard())
        return
    context.user_data.pop("import", None)
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

    if context.user_data.get("import") is not None:
        if text:
            await handle_import(msg, context, text)
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
        try:
            info = parse_channel_post(query.message.caption or query.message.text or "")
            if info:
                await register_info(context.bot, info)
        except Exception as e:
            print(f"ثبت تو لیست بعد از انتشار نشد: {e}")
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
    await _auto_register(msg, context.bot)
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
    footer_off = 0
    if FOOTER_TEXT and FOOTER_TEXT not in original:
        candidate = f"{original}\n\n{FOOTER_TEXT}" if original else FOOTER_TEXT
        if len(candidate) <= limit:
            new_text = candidate
            footer_off = _u16(original + "\n\n") if original else 0

    new_markup = _markup_with_button(msg.reply_markup) if want_button else None
    if new_text is None and new_markup is None:
        return

    ents = list(entities) if entities else []
    if new_text is not None and FOOTER_QUOTE:
        ents.append(MessageEntity(type=MessageEntity.BLOCKQUOTE, offset=footer_off, length=_u16(FOOTER_TEXT)))
    ents = ents or None
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



# ================== لیست‌های خودکار (پایان‌یافته / در حال ترجمه) ==================
LIST_TITLE_END = os.getenv("LIST_TITLE_ENDED", "مانهوا های پایان یافته سایت").strip()
LIST_TITLE_ON = os.getenv("LIST_TITLE_ONGOING", "مانهوا های در حال ترجمه سایت").strip()
LIST_CHUNK = 3800      # سقف کاراکتر هر پیام (تلگرام: 4096)
LIST_MAX_ITEMS = 45    # سقف آیتم هر پیام (تلگرام: حداکثر ۱۰۰ موجودیت)
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DATA_FILE = os.getenv("DATA_FILE", "manhwa_lists.json")


def norm_key(s) -> str:
    s = (s or "").lower().replace("ي", "ی").replace("ك", "ک")
    return re.sub(r"[\W_]+", "", s)


def list_tag(name) -> str:
    t = re.sub(r"[^\w\u200c]+", "_", name or "")
    t = re.sub(r"_+", "_", t).strip("_")
    return f"#{t}" if t else ""


def _kid(k: str) -> str:
    return hashlib.md5(k.encode("utf-8")).hexdigest()[:10]


class Store:
    """نگهداری لیست مانهواها: Postgres (DATABASE_URL) یا در غیر این‌صورت فایل JSON."""

    def __init__(self):
        self.items = {}   # key -> {"k","fa","en","ended","ts"}
        self.meta = {}
        self.pg = bool(DATABASE_URL)
        if self.pg:
            try:
                self._pg_init()
            except Exception as e:
                print(f"اتصال به دیتابیس نشد، می‌رم روی فایل JSON: {e}")
                self.pg = False
        if not self.pg:
            print("⚠️ DATABASE_URL تنظیم نیست؛ داده تو فایل JSON ذخیره می‌شه (روی Render با هر دیپلوی پاک می‌شه!)")
            self._file_load()

    # ---- Postgres ----
    def _conn(self):
        import psycopg2
        return psycopg2.connect(DATABASE_URL, connect_timeout=10)

    def _pg_init(self):
        conn = self._conn()
        try:
            with conn, conn.cursor() as cur:
                cur.execute("CREATE TABLE IF NOT EXISTS manhwa_items (k TEXT PRIMARY KEY, fa TEXT, en TEXT, "
                            "ended BOOLEAN NOT NULL DEFAULT FALSE, ts DOUBLE PRECISION NOT NULL)")
                cur.execute("CREATE TABLE IF NOT EXISTS manhwa_meta (k TEXT PRIMARY KEY, v TEXT)")
                cur.execute("SELECT k, fa, en, ended, ts FROM manhwa_items")
                for k, fa, en, ended, ts in cur.fetchall():
                    self.items[k] = {"k": k, "fa": fa, "en": en, "ended": bool(ended), "ts": ts}
                cur.execute("SELECT k, v FROM manhwa_meta")
                self.meta = dict(cur.fetchall())
        finally:
            conn.close()

    # ---- فایل ----
    def _file_load(self):
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                data = json.load(f)
            self.items, self.meta = data.get("items", {}), data.get("meta", {})
        except Exception:
            pass

    def _file_save(self):
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump({"items": self.items, "meta": self.meta}, f, ensure_ascii=False)
        except Exception as e:
            print(f"ذخیره‌ی فایل نشد: {e}")

    # ---- ذخیره ----
    def put_item(self, it: dict):
        if not self.pg:
            return self._file_save()
        try:
            conn = self._conn()
            try:
                with conn, conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO manhwa_items (k, fa, en, ended, ts) VALUES (%s,%s,%s,%s,%s) "
                        "ON CONFLICT (k) DO UPDATE SET fa=EXCLUDED.fa, en=EXCLUDED.en, "
                        "ended=EXCLUDED.ended, ts=EXCLUDED.ts",
                        (it["k"], it["fa"], it.get("en"), it["ended"], it["ts"]))
            finally:
                conn.close()
        except Exception as e:
            print(f"ذخیره تو دیتابیس نشد: {e}")

    def set_meta(self, k: str, v: str):
        self.meta[k] = v
        if not self.pg:
            return self._file_save()
        try:
            conn = self._conn()
            try:
                with conn, conn.cursor() as cur:
                    cur.execute("INSERT INTO manhwa_meta (k, v) VALUES (%s,%s) "
                                "ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v", (k, v))
            finally:
                conn.close()
        except Exception as e:
            print(f"ذخیره‌ی meta نشد: {e}")

    # ---- منطق ----
    def find(self, en, fa):
        ek, fk = norm_key(en), norm_key(fa)
        for it in self.items.values():
            if (ek and ek == norm_key(it.get("en"))) or (fk and fk == norm_key(it.get("fa"))):
                return it
        return None

    def upsert(self, info: dict):
        """(آیتم, تغییر_کرد). هر مانهوا فقط یک بار و فقط تو یکی از دو لیست هست.
        وضعیت فقط از «در حال ترجمه» به «پایان یافته» خودکار تغییر می‌کنه."""
        fa, en, ended = info.get("fa"), info.get("en"), info.get("ended")
        it = self.find(en, fa)
        if it is None:
            k = norm_key(en) or norm_key(fa)
            if not k or not fa:
                return None, False
            it = {"k": k, "fa": fa, "en": en, "ended": bool(ended), "ts": time.time()}
            self.items[k] = it
            self.put_item(it)
            return it, True
        changed = False
        if en and not it.get("en"):
            it["en"], changed = en, True
        if ended is True and not it["ended"]:
            it["ended"], it["ts"], changed = True, time.time(), True
        if changed:
            self.put_item(it)
        return it, changed

    def set_status(self, it: dict, ended: bool):
        if it["ended"] != ended:
            it["ended"], it["ts"] = ended, time.time()
            self.put_item(it)

    def by_kid(self, kid: str):
        return next((x for x in self.items.values() if _kid(x["k"]) == kid), None)

    def entries(self, ended: bool) -> list:
        return sorted((x for x in self.items.values() if x["ended"] == ended), key=lambda x: x["ts"])


_store = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


def parse_channel_post(text: str):
    """اسم فارسی/انگلیسی و وضعیت رو از کپشن پست کانال درمیاره. اگه مانهوایی نبود None."""
    if not text:
        return None
    base = parse_post(text)
    m_fa = re.search(r"اسم\s*فارسی\s*مانهوا\s*[:：]\s*(.+)", text)
    m_en = re.search(r"اسم\s*انگلیسی\s*مانهوا\s*[:：]\s*(.+)", text)
    fa = clean(m_fa.group(1)) if m_fa else base["fa"]
    en = clean(m_en.group(1)) if m_en else base["en"]
    if en in ("", "—", "-"):
        en = None
    if not fa:
        return None
    ended = None
    m_st = re.search(r"وضعیت\s*پخش\s*[:：]\s*(.+)", text)
    if m_st:
        s = m_st.group(1)
        if "پایان" in s or "تمام" in s:
            ended = True
        elif "ادامه" in s or "در حال" in s or "انتشار" in s:
            ended = False
    if ended is None:
        if "🔚" in text:
            ended = True
        elif "🔄" in text:
            ended = False
    return {"fa": fa, "en": en, "ended": ended}


def build_list_messages(entries: list, title: str) -> list:
    """[(متن, entities)] — هر آیتم داخل یه نقل‌قول، شماره‌ی پیوسته بین پیام‌ها."""
    msgs, part = [], 1
    head = lambda p: title if p == 1 else f"{title} (بخش {p})"
    text, ents, count = head(part) + "\n\n", [], 0
    for n, e in enumerate(entries, 1):
        tags = [t for t in (list_tag(e.get("fa")), list_tag(e.get("en"))) if t]
        block = f"{n}." + "\n".join(tags)
        if count and (len(text) + len(block) + 1 > LIST_CHUNK or count >= LIST_MAX_ITEMS):
            msgs.append((text, ents or None))
            part += 1
            text, ents, count = head(part) + "\n\n", [], 0
        if count:
            text += "\n"
        ents.append(MessageEntity(type=MessageEntity.BLOCKQUOTE, offset=_u16(text), length=_u16(block)))
        text += block
        count += 1
    if not count:
        text += "فعلاً موردی نیست."
    msgs.append((text, ents or None))
    return msgs


_refresh_lock = asyncio.Lock()


async def _sync_category(bot, ended: bool):
    st = get_store()
    key = "msgs_end" if ended else "msgs_on"
    msgs = build_list_messages(st.entries(ended), LIST_TITLE_END if ended else LIST_TITLE_ON)
    ids = json.loads(st.meta.get(key) or "[]")
    new_ids = []
    for i, (text, ents) in enumerate(msgs):
        mid = ids[i] if i < len(ids) else None
        if mid:
            try:
                await bot.edit_message_text(chat_id=CHANNEL_ID, message_id=mid, text=text, entities=ents)
                new_ids.append(mid)
                continue
            except BadRequest as e:
                if "not modified" in str(e).lower():
                    new_ids.append(mid)
                    continue
                print(f"ادیت پیام لیست {mid} نشد، پیام جدید می‌فرستم: {e}")
        sent = await bot.send_message(CHANNEL_ID, text, entities=ents)
        new_ids.append(sent.message_id)
    for mid in ids[len(msgs):]:
        try:
            await bot.delete_message(CHANNEL_ID, mid)
        except Exception as e:
            print(f"حذف پیام اضافه‌ی لیست نشد: {e}")
    st.set_meta(key, json.dumps(new_ids))


async def refresh_lists(bot):
    async with _refresh_lock:
        await _sync_category(bot, True)
        await _sync_category(bot, False)


async def register_info(bot, info: dict):
    it, changed = get_store().upsert(info)
    if it and changed:
        await refresh_lists(bot)
    return it, changed


async def _auto_register(msg, bot):
    try:
        info = parse_channel_post(msg.text or msg.caption or "")
        if info:
            await register_info(bot, info)
    except Exception as e:
        print(f"ثبت خودکار تو لیست نشد: {e}")


async def channel_edit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.edited_channel_post
    if msg and _is_our_channel(msg.chat):
        await _auto_register(msg, context.bot)


def _st_label(ended: bool) -> str:
    return "🔚 پایان یافته" if ended else "🔄 در حال ترجمه"


async def handle_import(msg, context: ContextTypes.DEFAULT_TYPE, text: str):
    info = parse_channel_post(text)
    if not info:
        await msg.reply_text("اسم مانهوا رو از این پست پیدا نکردم (خط «مانهوا: ...» یا «اسم فارسی مانهوا: ...» لازمه).")
        return
    it, changed = get_store().upsert(info)
    if not it:
        await msg.reply_text("ثبت نشد.")
        return
    context.user_data["import"]["n"] += 1
    if info["ended"] is None and changed:
        kid = _kid(it["k"])
        await msg.reply_text(
            f"❓ وضعیت «{it['fa']}» تو پست نبود. کدوم؟",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔚 پایان یافته", callback_data=f"setst:{kid}:1"),
                InlineKeyboardButton("🔄 در حال ترجمه", callback_data=f"setst:{kid}:0"),
            ]]))
        return
    tag = "اضافه شد" if changed else "از قبل بود"
    await msg.reply_text(f"✅ {it['fa']} → {_st_label(it['ended'])} ({tag})")


async def setst_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_allowed(q.from_user.id):
        await q.answer()
        return
    _, kid, flag = q.data.split(":")
    st = get_store()
    it = st.by_kid(kid)
    if not it:
        await q.answer("پیدا نشد.", show_alert=True)
        return
    st.set_status(it, flag == "1")
    await q.answer("ثبت شد ✅")
    await q.edit_message_text(f"✅ {it['fa']} → {_st_label(it['ended'])}")
    if context.user_data.get("import") is None:
        try:
            await refresh_lists(context.bot)
        except Exception as e:
            print(f"به‌روزرسانی لیست نشد: {e}")


async def do_refresh(msg, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("import", None)
    await msg.reply_text("⏳ در حال به‌روزرسانی لیست‌های کانال...", reply_markup=panel_keyboard())
    st = get_store()
    try:
        await refresh_lists(context.bot)
        await msg.reply_text(
            f"✅ لیست‌ها به‌روز شد.\n🔚 پایان یافته: {len(st.entries(True))}\n🔄 در حال ترجمه: {len(st.entries(False))}",
            reply_markup=panel_keyboard())
    except Exception as e:
        await msg.reply_text(
            f"❌ به‌روزرسانی ناموفق بود:\n{e}\n\nچک کن بات تو کانال ادمین باشه و دسترسی ارسال و ویرایش پیام داشته باشه.",
            reply_markup=panel_keyboard())


# ================== اجرا ==================
def build_application() -> Application:
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("refresh", refresh_cmd))

    application.add_handler(CallbackQueryHandler(status_callback, pattern=r"^st_(on|end)$"))
    application.add_handler(CallbackQueryHandler(ask_publish_callback, pattern=r"^pub$"))
    application.add_handler(CallbackQueryHandler(publish_callback, pattern=r"^pubok$"))
    application.add_handler(CallbackQueryHandler(cancel_publish_callback, pattern=r"^pubno$"))
    application.add_handler(CallbackQueryHandler(noop_callback, pattern=r"^noop$"))
    application.add_handler(CallbackQueryHandler(setst_callback, pattern=r"^setst:"))

    application.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, channel_post_handler))
    application.add_handler(MessageHandler(filters.UpdateType.EDITED_CHANNEL_POST, channel_edit_handler))
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
