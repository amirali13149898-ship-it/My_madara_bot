import os
import json
import time
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

app = Flask(__name__)

@app.route("/")
def home():
    return "Manhwa Hub Bot is running ✅", 200

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

def get_genres_map():
    r = requests.get(API_GENRES, timeout=15)
    r.raise_for_status()
    return {g["id"]: g["name"] for g in r.json()}

def get_chapters(manhwa_id):
    try:
        r = requests.get(API_CHAPTERS.format(manhwa_id), timeout=12)
        r.raise_for_status()
        return r.json()
    except:
        return []

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
    slug = m["slug"]
    link = f"https://manhwahub-tau.vercel.app/manhwa/{slug}"

    genre_names = [genres_map.get(gid, "") for gid in m.get("genre_ids", [])]
    genre_names = [g for g in genre_names if g]
    genres_str = " ".join([f"#{g.replace(' ', '_')}" for g in genre_names]) if genre_names else "—"

    if chapter_count is None:
        chapters = get_chapters(m["id"])
        chapter_count = max([c.get("chapter_number", 0) for c in chapters], default=0)

    chapter_line = f"𓆩 chapter 01_{chapter_count:02d}🔚" if chapter_count > 0 else "𓆩 chapter 01_01🔚"
    fa_tag = make_hashtag(fa_title)
    en_tag = make_hashtag(en_title)

    caption = f"""👤اسم فارسی مانهوا : {fa_title}
👤 اسم انگلیسی مانهوا : {en_title}
⛓ژانر ها : {genres_str}
نمره : {rating}
👁وضعیت پخش : {status}
نحوه پیدا کردن : {fa_tag} {en_tag}
خلاصه :
«{summary}»

{chapter_line}

{link}

🗣️@Manhwa_Hub_News
{en_tag}"""
    return caption

def make_keyboard(slug: str):
    url = f"https://manhwahub-tau.vercel.app/manhwa/{slug}"
    keyboard = [
        [InlineKeyboardButton("📖 مشاهده مانهوا", url=url)],
        [InlineKeyboardButton("📚 آرشیو مانهواها", url="https://manhwahub-tau.vercel.app/manhwas")]
    ]
    return InlineKeyboardMarkup(keyboard)

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

# ================== دستورات ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("شما مجاز به استفاده از این بات نیستید.")
        return
    await update.message.reply_text(
        "سلام! بات مانهوا هاب فعاله ✅\n\n"
        "دستورات:\n"
        "/history - مانهواهای قدیمی بر اساس بازه زمانی\n"
        "/status - وضعیت بات\n"
        "/help - راهنما"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        "📖 راهنما:\n\n"
        "/history → انتخاب بازه زمانی و دریافت مانهواهای اون دوره\n"
        "/status → آخرین چک + تعداد مانهواهای ثبت‌شده\n\n"
        "بات هر ۵ دقیقه به صورت خودکار مانهوای جدید و چپتر جدید رو برات می‌فرسته."
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

    keyboard = [
        [InlineKeyboardButton("۱ روز پیش", callback_data="hist_1d"),
         InlineKeyboardButton("۲ روز پیش", callback_data="hist_2d")],
        [InlineKeyboardButton("۱ هفته پیش", callback_data="hist_7d"),
         InlineKeyboardButton("۱ ماه پیش", callback_data="hist_30d")],
        [InlineKeyboardButton("۳ ماه پیش", callback_data="hist_90d"),
         InlineKeyboardButton("۶ ماه پیش", callback_data="hist_180d")],
        [InlineKeyboardButton("۱ سال پیش", callback_data="hist_365d")]
    ]
    await update.message.reply_text("کدام بازه زمانی رو می‌خوای؟", reply_markup=InlineKeyboardMarkup(keyboard))

async def history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(query.from_user.id):
        return

    days_map = {
        "hist_1d": 1, "hist_2d": 2, "hist_7d": 7,
        "hist_30d": 30, "hist_90d": 90, "hist_180d": 180, "hist_365d": 365
    }
    days = days_map.get(query.data)
    if not days:
        return

    await query.edit_message_text(f"در حال پیدا کردن مانهواهای {days} روز گذشته...")

    try:
        r = requests.get(API_MANHWAS, timeout=20)
        r.raise_for_status()
        manhwas = r.json()
        genres_map = get_genres_map()

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)

        filtered = [m for m in manhwas if datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")) >= cutoff]
        filtered.sort(key=lambda x: x["created_at"], reverse=True)

        if not filtered:
            await context.bot.send_message(query.from_user.id, "هیچ مانهوایی در این بازه پیدا نشد.")
            return

        await context.bot.send_message(query.from_user.id, f"پیدا شد: {len(filtered)} مانهوا\nشروع ارسال...")

        for m in filtered:
            chapters = get_chapters(m["id"])
            ch_count = max([c.get("chapter_number", 0) for c in chapters], default=0)
            await send_manhwa(context.bot, query.from_user.id, m, genres_map, ch_count)
            time.sleep(1.5)

        await context.bot.send_message(query.from_user.id, "✅ ارسال تمام شد.")
    except Exception as e:
        await context.bot.send_message(query.from_user.id, f"خطا: {e}")

# ================== چک خودکار (بدون JobQueue) ==================

def check_loop(application: Application):
    """این تابع تو یه ترد جداگانه اجرا می‌شه و هر چند دقیقه چک می‌کنه"""
    bot = application.bot
    print("حلقه چک خودکار شروع شد...")

    while True:
        try:
            if not ALLOWED_IDS:
                time.sleep(CHECK_INTERVAL)
                continue

            state = load_state()
            known = state.get("known_manhwas", {})
            genres_map = get_genres_map()

            r = requests.get(API_MANHWAS, timeout=20)
            r.raise_for_status()
            manhwas = r.json()

            for m in manhwas:
                mid = str(m["id"])
                chapters = get_chapters(m["id"])
                current_ch = max([c.get("chapter_number", 0) for c in chapters], default=0)

                if mid not in known:
                    print(f"مانهوای جدید: {m['title']}")
                    # چون تو ترد عادی هستیم، از asyncio استفاده می‌کنیم
                    import asyncio
                    for uid in ALLOWED_IDS:
                        asyncio.run(send_manhwa(bot, uid, m, genres_map, current_ch))
                    known[mid] = current_ch
                else:
                    last_ch = known[mid]
                    if current_ch > last_ch:
                        print(f"چپتر جدید برای {m['title']}: {last_ch} → {current_ch}")
                        text = f"🆕 چپتر جدید!\n\n{m['title']}\nاز چپتر {last_ch} به {current_ch}"
                        keyboard = make_keyboard(m["slug"])
                        import asyncio
                        for uid in ALLOWED_IDS:
                            asyncio.run(bot.send_message(uid, text, reply_markup=keyboard))
                        known[mid] = current_ch

            state["known_manhwas"] = known
            state["last_check"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            save_state(state)
            print("چک انجام شد.")

        except Exception as e:
            print("خطا در چک خودکار:", e)

        time.sleep(CHECK_INTERVAL)

# ================== اجرا ==================

def main():
    if not BOT_TOKEN or not ALLOWED_IDS:
        print("❌ BOT_TOKEN یا ALLOWED_IDS تنظیم نشده!")
        return

    print(f"بات شروع شد | آیدی‌های مجاز: {ALLOWED_IDS}")

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("history", history))
    application.add_handler(CallbackQueryHandler(history_callback, pattern="^hist_"))

    # شروع حلقه چک تو یه ترد جدا
    checker_thread = threading.Thread(target=check_loop, args=(application,), daemon=True)
    checker_thread.start()

    # شروع polling بات
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    # Flask رو تو ترد اصلی اجرا می‌کنیم
    bot_thread = threading.Thread(target=main, daemon=True)
    bot_thread.start()

    print(f"Flask در حال اجرا روی پورت {PORT}...")
    app.run(host="0.0.0.0", port=PORT)
