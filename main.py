import os
import json
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from aiohttp import web
from openai import AsyncOpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ChatMemberHandler, ContextTypes, ConversationHandler, filters
)

# ---------- CONFIG ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
PORT = int(os.environ.get("PORT", 10000))
DATA_FILE = "channels.json"

# Use DeepSeek if available, else OpenAI
if DEEPSEEK_API_KEY:
    ai_client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
    AI_MODEL = "deepseek-chat"
    AI_PROVIDER = "DeepSeek"
elif OPENAI_API_KEY:
    ai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    AI_MODEL = "gpt-4o-mini"
    AI_PROVIDER = "OpenAI"
else:
    ai_client = None
    AI_MODEL = None
    AI_PROVIDER = None

POST_CATEGORIES = [
    ("why", "Explain WHY this topic matters. Make it motivating and emotional. 2-3 short paragraphs."),
    ("important", "Share an IMPORTANT key concept or rule everyone must know about this topic. Be educational."),
    ("history", "Tell a short HISTORICAL fact or background story about this topic. Make it engaging."),
    ("fun fact", "Share a surprising or FUN FACT about this topic. Make people say 'wow'."),
    ("quiz", "Create a QUIZ question with 4 options (A, B, C, D) and reveal the correct answer at the end with explanation."),
    ("tips", "Give 3-5 practical TIPS related to this topic. Use bullet points or numbered list."),
]

POSTS_PER_DAY = 12
INTERVAL_MINUTES = (14 * 60) // POSTS_PER_DAY  # ~70 min across 14 active hours
ACTIVE_START_HOUR = 8   # 8 AM UTC
ACTIVE_END_HOUR = 22    # 10 PM UTC

# ---------- STATES ----------
WAITING_TOPIC = 1

# ---------- LOGGING ----------
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- STORAGE ----------
def load_data():
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)

def get_user_channels(user_id):
    return load_data().get(str(user_id), [])

def add_channel(user_id, chat_id, title):
    data = load_data()
    uid = str(user_id)
    if uid not in data:
        data[uid] = []
    for ch in data[uid]:
        if ch["id"] == chat_id:
            ch["title"] = title
            save_data(data)
            return
    data[uid].append({
        "id": chat_id, "title": title, "topic": None, "category_index": 0,
        "next_post_time": None, "active": False, "posts_today": 0, "last_post_date": None,
    })
    save_data(data)

def remove_channel(user_id, chat_id):
    data = load_data()
    uid = str(user_id)
    if uid in data:
        data[uid] = [c for c in data[uid] if c["id"] != chat_id]
        save_data(data)

def update_channel(user_id, chat_id, **updates):
    data = load_data()
    uid = str(user_id)
    if uid in data:
        for ch in data[uid]:
            if ch["id"] == chat_id:
                ch.update(updates)
                save_data(data)
                return ch
    return None

def find_channel(user_id, chat_id):
    for ch in get_user_channels(user_id):
        if ch["id"] == chat_id:
            return ch
    return None

# ---------- AI ----------
async def generate_post(topic: str, category: str, instructions: str):
    if not ai_client:
        return f"⚠️ No AI API key configured. Topic: {topic} | Category: {category}"
    system_prompt = (
        f"You are a Telegram channel content creator. The channel topic is: '{topic}'. "
        f"Write engaging Telegram posts. Use emojis. Keep posts under 800 characters. "
        f"Do not include hashtags unless natural. Do not greet or sign off — just deliver the content."
    )
    user_prompt = f"Write a Telegram post for category: {category.upper()}.\n\n{instructions}\n\nTopic: {topic}"
    try:
        resp = await ai_client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.9, max_tokens=600,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"AI error: {e}")
        return None

# ---------- UI HELPERS ----------
def main_menu(user_id):
    channels = get_user_channels(user_id)
    kb = []
    for ch in channels:
        status = "🟢" if ch.get("active") else "⚪"
        topic_set = "✏️" if ch.get("topic") else "❓"
        kb.append([InlineKeyboardButton(f"{status}{topic_set} {ch['title']}", callback_data=f"channel:{ch['id']}")])
    kb.append([InlineKeyboardButton("➕ Add Channel", callback_data="add_channel")])
    kb.append([InlineKeyboardButton("ℹ️ Help", callback_data="help")])
    return InlineKeyboardMarkup(kb)

def channel_detail_view(ch):
    chat_id = ch["id"]
    status = "🟢 Active" if ch.get("active") else "⚪ Paused"
    topic = ch.get("topic") or "_not set_"
    next_cat = POST_CATEGORIES[ch.get("category_index", 0)][0]

    today = datetime.now(timezone.utc).date().isoformat()
    done = ch.get("posts_today", 0) if ch.get("last_post_date") == today else 0
    remaining = max(0, POSTS_PER_DAY - done)
    filled = int((done / POSTS_PER_DAY) * 10)
    bar = "🟩" * filled + "⬜" * (10 - filled)
    percent = int((done / POSTS_PER_DAY) * 100)
    progress = (f"\n📊 *Today's Progress*\n{bar} {percent}%\n"
                f"✅ {done} post{'s' if done != 1 else ''} done — {remaining} more to go")

    countdown = ""
    npt = ch.get("next_post_time")
    if ch.get("active") and npt:
        try:
            npt_dt = datetime.fromisoformat(npt)
            mins = int((npt_dt - datetime.now(timezone.utc)).total_seconds() / 60)
            countdown = f"\n⏳ Next post in ~{mins} min" if mins > 0 else "\n⏳ Next post: due now"
        except Exception:
            pass

    kb = [
        [InlineKeyboardButton("✏️ Set Topic", callback_data=f"settopic:{chat_id}")],
        [InlineKeyboardButton("⏸ Pause" if ch.get("active") else "▶️ Activate", callback_data=f"toggle:{chat_id}")],
        [InlineKeyboardButton("📝 Post Now (test)", callback_data=f"postnow:{chat_id}")],
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"channel:{chat_id}")],
        [InlineKeyboardButton("🗑 Remove", callback_data=f"remove:{chat_id}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back")],
    ]
    text = (f"📺 *{ch['title']}*\n\nStatus: {status}\nTopic: {topic}\n"
            f"Next category: *{next_cat}*{progress}{countdown}")
    return text, InlineKeyboardMarkup(kb)

# ---------- HANDLERS ----------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    channels = get_user_channels(user.id)
    text = (
        f"👋 Hi {user.first_name}!\n\n🤖 *AI Channel Auto-Poster*\n\n"
        f"I post to your channels {POSTS_PER_DAY}× per day with fresh AI content rotating through:\n"
        f"💡 Why • ⭐ Important • 📜 History • 🎉 Fun Fact • ❓ Quiz • 💪 Tips\n\n"
    )
    if not channels:
        text += "Click *➕ Add Channel* to begin."
    else:
        text += f"You have {len(channels)} channel(s). Tap one to manage."
    text += f"\n\n_Powered by {AI_PROVIDER or '⚠️ No AI configured'}_"
    await update.message.reply_text(text, reply_markup=main_menu(user.id), parse_mode="Markdown")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📚 *How it works*\n\n"
        "1️⃣ Add me as *Admin* to your channel\n"
        "2️⃣ I detect it automatically — no IDs needed\n"
        "3️⃣ Set your channel topic (e.g. 'Teaching English to Arabic speakers')\n"
        f"4️⃣ Activate — I post {POSTS_PER_DAY}× per day\n\n"
        "*Cycle:* 💡 Why → ⭐ Important → 📜 History → 🎉 Fun Fact → ❓ Quiz → 💪 Tips → 🔁\n\n"
        f"⏰ Posts run {ACTIVE_START_HOUR}:00–{ACTIVE_END_HOUR}:00 UTC, every ~{INTERVAL_MINUTES} min\n\n"
        "Commands: /start /help /cancel"
    )
    if update.callback_query:
        await update.callback_query.message.reply_text(text, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled.", reply_markup=main_menu(update.effective_user.id))
    return ConversationHandler.END

async def my_chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.my_chat_member
    if cmu.chat.type != "channel":
        return
    new_status = cmu.new_chat_member.status
    actor = cmu.from_user
    if not actor:
        return
    if new_status == "administrator":
        add_channel(actor.id, cmu.chat.id, cmu.chat.title or "Untitled")
        try:
            await context.bot.send_message(
                actor.id,
                f"✅ Channel *{cmu.chat.title}* linked!\n\nNow set its topic so I can generate content.",
                reply_markup=main_menu(actor.id), parse_mode="Markdown")
        except Exception:
            pass
    elif new_status in ("left", "kicked", "member"):
        remove_channel(actor.id, cmu.chat.id)
        try:
            await context.bot.send_message(actor.id, f"⚠️ Lost admin in *{cmu.chat.title}* — removed.", parse_mode="Markdown")
        except Exception:
            pass

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    user_id = q.from_user.id

    if data == "add_channel":
        await q.message.reply_text(
            "➕ *Add a channel*\n\n1. Open your channel\n2. Settings → Administrators → Add Admin\n"
            "3. Search my username and add me\n4. I'll detect it instantly ✅", parse_mode="Markdown")
        return

    if data == "help":
        await help_command(update, context)
        return

    if data == "back":
        await q.edit_message_text("📋 *Your Channels*", reply_markup=main_menu(user_id), parse_mode="Markdown")
        return

    if data.startswith("channel:"):
        chat_id = int(data.split(":")[1])
        ch = find_channel(user_id, chat_id)
        if not ch:
            await q.edit_message_text("❌ Channel not found.", reply_markup=main_menu(user_id))
            return
        text, kb = channel_detail_view(ch)
        try:
            await q.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        except Exception:
            pass
        return

    if data.startswith("settopic:"):
        chat_id = int(data.split(":")[1])
        context.user_data["topic_chat_id"] = chat_id
        await q.message.reply_text(
            "✏️ Send me the topic / instructions for this channel.\n\n"
            "Example: _Teaching English grammar to Arabic speakers — beginner to intermediate._",
            parse_mode="Markdown")
        return WAITING_TOPIC

    if data.startswith("toggle:"):
        chat_id = int(data.split(":")[1])
        ch = find_channel(user_id, chat_id)
        if not ch:
            await q.answer("Channel not found", show_alert=True)
            return
        if not ch.get("topic"):
            await q.answer("⚠️ Set the topic first!", show_alert=True)
            return
        new_active = not ch.get("active")
        updates = {"active": new_active}
        if new_active:
            updates["next_post_time"] = datetime.now(timezone.utc).isoformat()
        update_channel(user_id, chat_id, **updates)

        if new_active:
            await q.answer("✅ Activated!", show_alert=True)
            await context.bot.send_message(
                user_id,
                f"🚀 *Auto-posting STARTED* for *{ch['title']}*\n\n"
                f"📝 Topic: _{ch.get('topic')}_\n📊 {POSTS_PER_DAY} posts/day\n"
                f"⏰ {ACTIVE_START_HOUR}:00–{ACTIVE_END_HOUR}:00 UTC\n"
                f"🔁 why → important → history → fun fact → quiz → tips\n\n"
                f"⏳ First post within 1 minute…", parse_mode="Markdown")
        else:
            await q.answer("⏸ Paused", show_alert=True)
            await context.bot.send_message(user_id, f"⏸ *Auto-posting PAUSED* for *{ch['title']}*.", parse_mode="Markdown")

        ch = find_channel(user_id, chat_id)
        text, kb = channel_detail_view(ch)
        try:
            await q.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        except Exception:
            pass
        return

    if data.startswith("postnow:"):
        chat_id = int(data.split(":")[1])
        await q.answer("Generating…")
        await do_post(context, user_id, chat_id, force=True)
        return

    if data.startswith("remove:"):
        chat_id = int(data.split(":")[1])
        remove_channel(user_id, chat_id)
        await q.edit_message_text("🗑 Removed.", reply_markup=main_menu(user_id))
        return

async def receive_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.user_data.get("topic_chat_id")
    if not chat_id:
        return ConversationHandler.END
    topic = update.message.text.strip()
    if len(topic) < 5:
        await update.message.reply_text("Topic too short. Try again or /cancel.")
        return WAITING_TOPIC
    update_channel(update.effective_user.id, chat_id, topic=topic)
    context.user_data.clear()
    await update.message.reply_text(
        "✅ Topic saved!\n\nNow tap *▶️ Activate* to start auto-posting.",
        reply_markup=main_menu(update.effective_user.id), parse_mode="Markdown")
    return ConversationHandler.END

# ---------- POSTING LOGIC ----------
async def do_post(context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int, force: bool = False):
    ch = find_channel(user_id, chat_id)
    if not ch or not ch.get("topic"):
        return
    cat_name, cat_instructions = POST_CATEGORIES[ch.get("category_index", 0) % len(POST_CATEGORIES)]
    content = await generate_post(ch["topic"], cat_name, cat_instructions)
    if not content:
        try:
            await context.bot.send_message(user_id, f"⚠️ AI failed for *{ch['title']}*.", parse_mode="Markdown")
        except Exception:
            pass
        return
    try:
        await context.bot.send_message(chat_id, content)
    except Exception as e:
        logger.error(f"Failed to post to {chat_id}: {e}")
        try:
            await context.bot.send_message(user_id, f"❌ Failed to post to *{ch['title']}*: {e}", parse_mode="Markdown")
        except Exception:
            pass
        return

    today = datetime.now(timezone.utc).date().isoformat()
    posts_today = ch.get("posts_today", 0)
    if ch.get("last_post_date") != today:
        posts_today = 0
    posts_today += 1
    next_time = datetime.now(timezone.utc) + timedelta(minutes=INTERVAL_MINUTES)
    update_channel(user_id, chat_id,
        category_index=(ch.get("category_index", 0) + 1) % len(POST_CATEGORIES),
        posts_today=posts_today, last_post_date=today, next_post_time=next_time.isoformat())

    remaining = max(0, POSTS_PER_DAY - posts_today)
    filled = int((posts_today / POSTS_PER_DAY) * 10)
    bar = "🟩" * filled + "⬜" * (10 - filled)
    percent = int((posts_today / POSTS_PER_DAY) * 100)
    try:
        await context.bot.send_message(
            user_id,
            f"✅ Posted *{cat_name}* to *{ch['title']}*\n\n{bar} {percent}%\n"
            f"📊 {posts_today}/{POSTS_PER_DAY} posts done — {remaining} more to go today",
            parse_mode="Markdown")
    except Exception:
        pass

async def scheduler_loop(app: Application):
    await asyncio.sleep(10)
    while True:
        try:
            now = datetime.now(timezone.utc)
            data = load_data()
            for uid_str, channels in data.items():
                user_id = int(uid_str)
                for ch in channels:
                    if not ch.get("active") or not ch.get("topic"):
                        continue
                    today = now.date().isoformat()
                    posts_today = ch.get("posts_today", 0) if ch.get("last_post_date") == today else 0
                    if posts_today >= POSTS_PER_DAY:
                        continue
                    if not (ACTIVE_START_HOUR <= now.hour < ACTIVE_END_HOUR):
                        continue
                    npt = ch.get("next_post_time")
                    if npt:
                        try:
                            if now < datetime.fromisoformat(npt):
                                continue
                        except Exception:
                            pass
                    ctx = ContextTypes.DEFAULT_TYPE(application=app, chat_id=None, user_id=user_id)
                    await do_post(ctx, user_id, ch["id"])
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
        await asyncio.sleep(60)

# ---------- HEALTH SERVER ----------
async def health(request):
    return web.Response(text="OK")

async def run_web():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logger.info(f"Health server on :{PORT}")

# ---------- MAIN ----------
async def post_init(app: Application):
    asyncio.create_task(run_web())
    asyncio.create_task(scheduler_loop(app))

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN missing")
    if not ai_client:
        logger.warning("No DEEPSEEK_API_KEY or OPENAI_API_KEY set — AI generation disabled")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    topic_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(menu_callback, pattern=r"^settopic:")],
        states={WAITING_TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_topic)]},
        fallbacks=[CommandHandler("cancel", cancel_command)],
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(topic_conv)
    app.add_handler(CallbackQueryHandler(menu_callback))
    app.add_handler(ChatMemberHandler(my_chat_member_handler, ChatMemberHandler.MY_CHAT_MEMBER))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
