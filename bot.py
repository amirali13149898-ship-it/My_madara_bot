import os
import json
import time
import asyncio
import hashlib
import threading
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ================== تنظیمات ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ALLOWED_IDS = [int(x.strip()) for x in os.getenv("ALLOWED_IDS", "").split(",") if x.strip()]
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
PORT = int(os.getenv("PORT", 10000))

STATE_FILE = "state.json"
API_MANHWAS = "https://manhwahub-tau.vercel.app/api/manhwas"
API_GENRES = "https://manhwahub-tau.vercel.app/api/genres"
API_CHAPTERS = "https://manhwahub-tau.vercel.app/api/chapters?manhwa_id={}"
SITE_ROOT = "https://manhwahub-tau.vercel.app/"
ARCHIVE_URL = "https://manhwahub-tau.vercel.app/manhwas"

# وقت ایران (برای انتخاب "یک روز خاص")
TEHRAN = timezone(timedelta(hours=3, minutes=30))

app = Flask(__name__)

@app.route("/")
def home():
    return "Manhwa Hub Bot is running ✅", 200

# ================== ابزارهای کمکی ==================

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"known_manhwas": {}, "last_check": None}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def is_allowed(user_id: int) -> bool:
    return user_id in ALLOWED_IDS

def fetch_manhwas():
    r = requests.get(API_MANHWAS, timeout=20)
    r.raise_for_status()
    return r.json()

def get_genres_map():
    r = requests.get(API_GENRES, timeout=15)
    r.raise_for_status()
    return {g["id"]: g["name"] for g in r.json()}

def get_chapters(manhwa_id):
    try:
        r = requests.get(API_CHAPTERS.format(manhwa_id), timeout=12)
        r.raise_for_status()
        return r.json()
    except Exception:
        return []

def get_max_chapter(manhwa_id):
    chapters = get_chapters(manhwa_id)
    return max([c.get("chapter_number", 0) for c in chapters], default=0)

def parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def fingerprint(m: dict) -> str:
    """اثر انگشت مشخصات مانهوا؛ اگه چیزی از این فیلدها عوض بشه یعنی مانهوا تغییر کرده."""
    keys = ["title", "english_title", "rating", "status", "summary", "cover_url", "genre_ids"]
    raw = json.dumps({k: m.get(k) for k in keys}, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()

def make_hashtag(text: str) -> str:
    if not text or text == "—":
        return ""
    cleaned = "".join(c for c in text if c.isalnum() or c in "آابپتثجچحخدذرزژسشصضطظعغفقکگلمنوهیء ")
    return "#" + cleaned.replace(" ", "_")

def format_caption(m, genres_map, chapter_count=None):
    fa_title = m["title"]
    en_title = m.get("english_title") or "—"
    rating = m.get("rating", "—")
    status = m.get("status", "—")
    summary = (m.get("summary") or "—").strip()

    genre_names = [genres_map.get(gid, "") for gid in m.get("genre_ids", [])]
    genre_names = [g for g in genre_names if g]
    genres_str = " ".join([f"#{g.replace(' ', '_')}" for g in genre_names]) if genre_names else "—"

    if chapter_count is None:
        chapter_count = get_max_chapter(m["id"])

    chapter_line = f"𓆩 chapter 01_{chapter_count:02d}🔚" if chapter_count > 0 else "𓆩 chapter 01_01🔚"
    fa_tag = make_hashtag(fa_title)
    en_tag = make_hashtag(en_title)

    # لینک خام از کپشن حذف شد؛ حالا دکمه «بازکردن سایت» جاشو گرفته
    caption = f"""👤اسم فارسی مانهوا : {fa_title}
👤 اسم انگلیسی مانهوا : {en_title}
⛓ژانر ها : {genres_str}
نمره : {rating}
👁وضعیت پخش : {status}
نحوه پیدا کردن : {fa_tag} {en_tag}
خلاصه :
«{summary}»

{chapter_line}

🗣️@Manhwa_Hub_News
{en_tag}"""
    return caption

def make_keyboard(slug: str):
    """ردیف اول: بازکردن سایت | ردیف دوم: مشاهده مانهوا + آرشیو کنار هم"""
    url = f"{SITE_ROOT}manhwa/{slug}"
    keyboard = [
        [InlineKeyboardButton("🌐 بازکردن سایت", url=SITE_ROOT)],
        [
            InlineKeyboardButton("📖 مشاهده مانهوا", url=url),
            InlineKeyboardButton("📚 آرشیو مانهواها", url=ARCHIVE_URL),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)

def make_notify_keyboard(m: dict):
    """کیبورد پیام اطلاع‌رسانی: ادمین با زدن دکمه اول مشخصات کامل رو می‌گیره."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📩 دریافت مشخصات", callback_data=f"info_{m['id']}")],
        [InlineKeyboardButton("📖 مشاهده مانهوا", url=f"{SITE_ROOT}manhwa/{m['slug']}")],
    ])

async def send_manhwa(bot: Bot, chat_id: int, m: dict, genres_map: dict, chapter_count=None):
    caption = format_caption(m, genres_map, chapter_count)
    keyboard = make_keyboard(m["slug"])
    cover = m.get("cover_url")

    try:
        if cover:
            await bot.send_photo(chat_id=chat_id, photo=cover, caption=caption, reply_markup=keyboard)
        else:
            await bot.send_message(chat_id=chat_id, text=caption, reply_markup=keyboard)
    except Exception as e:
        print(f"خطا در ارسال {m['title']}: {e}")
        await bot.send_message(chat_id=chat_id, text=caption, reply_markup=keyboard)

async def send_list(bot: Bot, chat_id: int, items: list):
    if not items:
        await bot.send_message(chat_id, "هیچ مانهوایی در این بازه پیدا نشد.")
        return

    genres_map = await asyncio.to_thread(get_genres_map)
    await bot.send_message(chat_id, f"پیدا شد: {len(items)} مانهوا\nشروع ارسال...")

    for m in items:
        ch_count = await asyncio.to_thread(get_max_chapter, m["id"])
        await send_manhwa(bot, chat_id, m, genres_map, ch_count)
        await asyncio.sleep(1.5)

    await bot.send_message(chat_id, "✅ ارسال تمام شد.")

# ================== منوها ==================

def history_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardButton
    return InlineKeyboardMarkup([
        [b("۱ روز پیش", callback_data="hist_1d"), b("۲ روز پیش", callback_data="hist_2d")],
        [b("۱ هفته پیش", callback_data="hist_7d"), b("۱ ماه پیش", callback_data="hist_30d")],
        [b("۳ ماه پیش", callback_data="hist_90d"), b("۶ ماه پیش", callback_data="hist_180d")],
        [b("۱ سال پیش", callback_data="hist_365d")],
        [b("📅 یک روز خاص", callback_data="pickday")],
        [b("📚 همه مانهواها", callback_data="hist_all")],
    ])

# ================== دستورات ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("شما مجاز به استفاده از این بات نیستید.")
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ نه", callback_data="start_no"),
        InlineKeyboardButton("✅ آره", callback_data="start_yes"),
    ]])
    await update.message.reply_text(
        "چطوری ارباب 👑\nمی‌خوای مانهواهایی که تا الان اومدن رو دریافت کنی؟",
        reply_markup=keyboard,
    )

async def start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(query.from_user.id):
        return

    if query.data == "start_no":
        await query.edit_message_text(
            "باشه ارباب 🙏\nهر مانهوا یا چپتر جدیدی که بیاد همون لحظه خبرت می‌کنم."
        )
    else:  # start_yes یا menu_back
        await query.edit_message_text(
            "کدوم بازه‌ی زمانی رو می‌خوای؟",
            reply_markup=history_keyboard(),
        )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        "📖 راهنما:\n\n"
        "/history → انتخاب بازه زمانی و دریافت مانهواهای اون دوره\n"
        "/status → آخرین چک + تعداد مانهواهای ثبت‌شده\n\n"
        "بات هر ۵ دقیقه چک می‌کنه و اگه مانهوای جدید، چپتر جدید یا تغییری باشه بهت خبر می‌ده. "
        "با دکمه «📩 دریافت مشخصات» می‌تونی مشخصات کامل اون مانهوا رو بگیری."
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    state = load_state()
    known_count = len(state.get("known_manhwas", {}))
    last = state.get("last_check", "هنوز چک نشده")
    await update.message.reply_text(
        f"📊 وضعیت بات:\n\n"
        f"تعداد مانهواهای شناخته‌شده: {known_count}\n"
        f"آخرین چک: {last}\n"
        f"فاصله چک: هر {CHECK_INTERVAL // 60} دقیقه"
    )

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text("کدوم بازه‌ی زمانی رو می‌خوای؟", reply_markup=history_keyboard())

async def history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(query.from_user.id):
        return

    days_map = {
        "hist_1d": 1, "hist_2d": 2, "hist_7d": 7,
        "hist_30d": 30, "hist_90d": 90, "hist_180d": 180, "hist_365d": 365,
    }

    if query.data == "hist_all":
        days = None
        label = "همه‌ی مانهواها"
    else:
        days = days_map.get(query.data)
        if not days:
            return
        label = f"مانهواهای {days} روز گذشته"

    await query.edit_message_text(f"در حال پیدا کردن {label}...")

    try:
        manhwas = await asyncio.to_thread(fetch_manhwas)
        if days:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            items = [m for m in manhwas if parse_dt(m["created_at"]) >= cutoff]
        else:
            items = list(manhwas)
        items.sort(key=lambda x: parse_dt(x["created_at"]), reverse=True)
        await send_list(context.bot, query.from_user.id, items)
    except Exception as e:
        await context.bot.send_message(query.from_user.id, f"خطا: {e}")

async def pickday_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(query.from_user.id):
        return

    today = datetime.now(TEHRAN).date()
    rows, row = [], []
    for i in range(14):
        d = today - timedelta(days=i)
        label = "امروز" if i == 0 else "دیروز" if i == 1 else d.strftime("%m/%d")
        row.append(InlineKeyboardButton(label, callback_data=f"day_{d.isoformat()}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🔙 برگشت", callback_data="menu_back")])

    await query.edit_message_text(
        "کدوم روز؟ (۱۴ روز اخیر، به وقت ایران)",
        reply_markup=InlineKeyboardMarkup(rows),
    )

async def day_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(query.from_user.id):
        return

    try:
        day = datetime.strptime(query.data[4:], "%Y-%m-%d").date()
    except ValueError:
        return

    await query.edit_message_text(f"در حال پیدا کردن مانهواهای {day.isoformat()}...")

    try:
        manhwas = await asyncio.to_thread(fetch_manhwas)
        items = [m for m in manhwas if parse_dt(m["created_at"]).astimezone(TEHRAN).date() == day]
        items.sort(key=lambda x: parse_dt(x["created_at"]), reverse=True)
        await send_list(context.bot, query.from_user.id, items)
    except Exception as e:
        await context.bot.send_message(query.from_user.id, f"خطا: {e}")

async def info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دکمه «📩 دریافت مشخصات» زیر پیام‌های اطلاع‌رسانی."""
    query = update.callback_query
    if not is_allowed(query.from_user.id):
        await query.answer()
        return
    await query.answer("در حال ارسال مشخصات...")

    mid = query.data[5:]
    try:
        manhwas = await asyncio.to_thread(fetch_manhwas)
        m = next((x for x in manhwas if str(x["id"]) == mid), None)
        if not m:
            await context.bot.send_message(query.from_user.id, "این مانهوا دیگه پیدا نشد.")
            return
        genres_map = await asyncio.to_thread(get_genres_map)
        ch_count = await asyncio.to_thread(get_max_chapter, m["id"])
        await send_manhwa(context.bot, query.from_user.id, m, genres_map, ch_count)
    except Exception as e:
        await context.bot.send_message(query.from_user.id, f"خطا: {e}")

# ================== چک خودکار (بدون JobQueue) ==================

def check_loop():
    """تو یه ترد جدا اجرا می‌شه. لوپ و Bot اختصاصی خودش رو داره تا با پولینگ تداخل نکنه.
    فقط اطلاع می‌ده؛ مشخصات کامل با دکمه‌ی «دریافت مشخصات» فرستاده می‌شه."""
    print("حلقه چک خودکار شروع شد...")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot = Bot(BOT_TOKEN)

    def notify(text, m):
        kb = make_notify_keyboard(m)
        for uid in ALLOWED_IDS:
            try:
                loop.run_until_complete(bot.send_message(uid, text, reply_markup=kb))
            except Exception as e:
                print(f"خطا در اطلاع‌رسانی به {uid}: {e}")

    while True:
        try:
            if not ALLOWED_IDS:
                time.sleep(CHECK_INTERVAL)
                continue

            state = load_state()
            known = state.get("known_manhwas", {})
            first_run = not known  # اولین اجرا: فقط ثبت می‌کنیم، اسپم نمی‌کنیم

            manhwas = fetch_manhwas()

            for m in manhwas:
                mid = str(m["id"])
                fp = fingerprint(m)
                current_ch = get_max_chapter(m["id"])
                entry = known.get(mid)

                # سازگاری با state.json قدیمی (که فقط عدد چپتر بود)
                if isinstance(entry, int):
                    entry = {"ch": entry, "fp": fp}
                    known[mid] = entry

                if entry is None:
                    known[mid] = {"ch": current_ch, "fp": fp}
                    if not first_run:
                        print(f"مانهوای جدید: {m['title']}")
                        en = m.get("english_title") or ""
                        notify(f"🆕 مانهوای جدید اضافه شد!\n\n{m['title']}\n{en}".strip(), m)
                else:
                    last_ch = entry["ch"]
                    if current_ch > last_ch:
                        print(f"چپتر جدید برای {m['title']}: {last_ch} → {current_ch}")
                        notify(f"🔔 چپتر جدید!\n\n{m['title']}\nاز چپتر {last_ch} به {current_ch}", m)
                    elif fp != entry.get("fp"):
                        print(f"مشخصات تغییر کرد: {m['title']}")
                        notify(f"✏️ مشخصات این مانهوا تغییر کرده:\n\n{m['title']}", m)
                    # اگه دریافت چپترها خطا داد (۰ برگشت)، عدد قبلی حفظ می‌شه
                    known[mid] = {"ch": max(current_ch, last_ch), "fp": fp}

            state["known_manhwas"] = known
            state["last_check"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            save_state(state)
            print("چک انجام شد.")

        except Exception as e:
            print("خطا در چک خودکار:", e)

        time.sleep(CHECK_INTERVAL)

# ================== اجرا ==================

def build_application() -> Application:
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("history", history))

    application.add_handler(CallbackQueryHandler(start_callback, pattern=r"^(start_yes|start_no|menu_back)$"))
    application.add_handler(CallbackQueryHandler(history_callback, pattern=r"^hist_(\d+d|all)$"))
    application.add_handler(CallbackQueryHandler(pickday_callback, pattern=r"^pickday$"))
    application.add_handler(CallbackQueryHandler(day_callback, pattern=r"^day_"))
    application.add_handler(CallbackQueryHandler(info_callback, pattern=r"^info_"))
    return application

def run_flask():
    app.run(host="0.0.0.0", port=PORT, use_reloader=False)

def run_bot():
    """پولینگ حتماً باید روی ترد اصلی اجرا بشه؛ python-telegram-bot تو ترد فرعی
    event loop نداره و پولینگ اصلاً بالا نمیاد (نتیجه: نه /start جواب می‌ده نه دکمه‌ها)."""
    threading.Thread(target=check_loop, daemon=True).start()

    while True:
        try:
            # هر بار لوپ و Application تازه، چون run_polling در پایان لوپ رو می‌بنده
            asyncio.set_event_loop(asyncio.new_event_loop())
            application = build_application()
            application.run_polling(drop_pending_updates=True)
            break  # اگه عادی تموم شد (مثلاً سیگنال توقف)، از حلقه خارج شو
        except Exception as e:
            print("پولینگ بات کرش کرد، ۵ ثانیه دیگه دوباره تلاش می‌کنیم:", e)
            time.sleep(5)

if __name__ == "__main__":
    # Flask تو ترد فرعی، بات (پولینگ) تو ترد اصلی
    threading.Thread(target=run_flask, daemon=True).start()
    print(f"Flask در حال اجرا روی پورت {PORT}...")

    if not BOT_TOKEN or not ALLOWED_IDS:
        print("❌ BOT_TOKEN یا ALLOWED_IDS تنظیم نشده!")
        while True:
            time.sleep(3600)  # سرور زنده بمونه تا لاگ‌ها دیده بشن
    else:
        print(f"بات شروع شد | آیدی‌های مجاز: {ALLOWED_IDS}")
        run_bot()
