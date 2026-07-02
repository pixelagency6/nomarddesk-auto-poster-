import os
import time
import random
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx
import psycopg2
import psycopg2.extras
from aiohttp import web
from openai import AsyncOpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ChatMemberHandler, PreCheckoutQueryHandler, ContextTypes, ConversationHandler, filters
)

# ---------- CONFIG ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
PORT = int(os.environ.get("PORT", 10000))
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]

TON_ADDRESS = os.environ.get("TON_ADDRESS", "")          # your TON receiver
TRON_ADDRESS = os.environ.get("TRON_ADDRESS", "")        # your USDT-TRC20 receiver
TONCENTER_KEY = os.environ.get("TONCENTER_API_KEY", "")  # optional (higher limits)
TRONGRID_KEY = os.environ.get("TRONGRID_API_KEY", "")    # optional
STARS_PER_USD = int(os.environ.get("STARS_PER_USD", "65"))  # tune to real Star rate

USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

PLANS = {
    "daily":   {"days": 1,  "usd": 2.0,  "label": "1 Day"},
    "weekly":  {"days": 7,  "usd": 10.0, "label": "1 Week"},
    "monthly": {"days": 30, "usd": 30.0, "label": "1 Month"},
}

TRIAL_HOURS = 24
POSTS_PER_DAY = 12
INTERVAL_MINUTES = (14 * 60) // POSTS_PER_DAY
ACTIVE_START_HOUR = 8
ACTIVE_END_HOUR = 22
WAITING_TOPIC = 1

if DEEPSEEK_API_KEY:
    ai_client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
    AI_MODEL, AI_PROVIDER = "deepseek-chat", "DeepSeek"
elif OPENAI_API_KEY:
    ai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    AI_MODEL, AI_PROVIDER = "gpt-4o-mini", "OpenAI"
else:
    ai_client, AI_MODEL, AI_PROVIDER = None, None, None

POST_CATEGORIES = [
    ("why", "Explain WHY this topic matters. Positive and motivating. 2-3 short paragraphs."),
    ("important", "Share an IMPORTANT concept everyone should know. Educational."),
    ("history", "Tell a short HISTORICAL fact or background story. Engaging."),
    ("fun fact", "Share a surprising FUN FACT. Make people say 'wow'."),
    ("quiz", "Create a QUIZ with 4 options (A,B,C,D) and reveal the answer with a short explanation."),
    ("tips", "Give 3-5 practical TIPS as a numbered list."),
]

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- DATABASE ----------
def dbc():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    c = dbc(); cur = c.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY, first_name TEXT, created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS channels (
        id BIGINT PRIMARY KEY, owner_id BIGINT, title TEXT, topic TEXT,
        category_index INT DEFAULT 0, active BOOLEAN DEFAULT false,
        posts_today INT DEFAULT 0, last_post_date TEXT, next_post_time TIMESTAMPTZ,
        trial_start TIMESTAMPTZ, subscription_until TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS orders (
        id SERIAL PRIMARY KEY, user_id BIGINT, channel_id BIGINT, plan TEXT, days INT,
        currency TEXT, expected_amount NUMERIC, address TEXT, status TEXT DEFAULT 'pending',
        tx_hash TEXT, created_at TIMESTAMPTZ DEFAULT now(), expires_at TIMESTAMPTZ
    );
    """)
    c.commit(); cur.close(); c.close()
    logger.info("DB ready.")

def ensure_user(uid, name):
    c = dbc(); cur = c.cursor()
    cur.execute("INSERT INTO users (user_id, first_name) VALUES (%s,%s) ON CONFLICT (user_id) DO UPDATE SET first_name=EXCLUDED.first_name", (uid, name))
    c.commit(); cur.close(); c.close()

# ---- channels ----
def add_channel(owner_id, cid, title):
    now = datetime.now(timezone.utc)
    c = dbc(); cur = c.cursor()
    cur.execute("""INSERT INTO channels (id, owner_id, title, trial_start) VALUES (%s,%s,%s,%s)
                   ON CONFLICT (id) DO UPDATE SET title=EXCLUDED.title, owner_id=EXCLUDED.owner_id""",
                (cid, owner_id, title, now))
    c.commit(); cur.close(); c.close()

def remove_channel(cid):
    c = dbc(); cur = c.cursor(); cur.execute("DELETE FROM channels WHERE id=%s", (cid,))
    c.commit(); cur.close(); c.close()

def update_channel(cid, **f):
    if not f: return
    sets = ", ".join(f"{k}=%s" for k in f)
    c = dbc(); cur = c.cursor()
    cur.execute(f"UPDATE channels SET {sets} WHERE id=%s", list(f.values()) + [cid])
    c.commit(); cur.close(); c.close()

def get_user_channels(owner_id):
    c = dbc(); cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM channels WHERE owner_id=%s ORDER BY created_at", (owner_id,))
    rows = cur.fetchall(); cur.close(); c.close()
    return [dict(r) for r in rows]

def find_channel(cid):
    c = dbc(); cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM channels WHERE id=%s", (cid,))
    r = cur.fetchone(); cur.close(); c.close()
    return dict(r) if r else None

def all_active_channels():
    c = dbc(); cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM channels WHERE active=true AND topic IS NOT NULL")
    rows = cur.fetchall(); cur.close(); c.close()
    return [dict(r) for r in rows]

def grant_channel(cid, days):
    now = datetime.now(timezone.utc)
    ch = find_channel(cid)
    base = ch["subscription_until"] if ch and ch.get("subscription_until") and ch["subscription_until"] > now else now
    until = base + timedelta(days=days)
    update_channel(cid, subscription_until=until, active=True, next_post_time=now)
    return until

def channel_has_access(ch):
    if not ch: return False
    now = datetime.now(timezone.utc)
    if ch.get("subscription_until") and now < ch["subscription_until"]:
        return True
    if ch.get("trial_start") and now < ch["trial_start"] + timedelta(hours=TRIAL_HOURS):
        return True
    return False

def channel_status_text(ch):
    now = datetime.now(timezone.utc)
    if ch.get("subscription_until") and now < ch["subscription_until"]:
        left = ch["subscription_until"] - now
        return f"✅ Paid — {left.days}d {left.seconds//3600}h left"
    if ch.get("trial_start") and now < ch["trial_start"] + timedelta(hours=TRIAL_HOURS):
        left = (ch["trial_start"] + timedelta(hours=TRIAL_HOURS)) - now
        return f"🎁 Trial — {left.seconds//3600}h {(left.seconds//60)%60}m left"
    return "⛔ Expired — subscribe to post"

# ---- orders ----
def pending_amount_exists(currency, amount):
    c = dbc(); cur = c.cursor()
    cur.execute("""SELECT 1 FROM orders WHERE currency=%s AND status='pending'
                   AND expires_at>now() AND ABS(expected_amount-%s)<0.0000005""", (currency, amount))
    r = cur.fetchone(); cur.close(); c.close()
    return r is not None

def unique_amount(base, currency):
    for _ in range(60):
        amt = round(base + random.randint(1, 9999) / 1_000_000, 6)
        if not pending_amount_exists(currency, amt):
            return amt
    return round(base + random.randint(1, 999999) / 1_000_000, 6)

def create_order(uid, cid, plan, days, currency, amount, address):
    exp = datetime.now(timezone.utc) + timedelta(minutes=30)
    c = dbc(); cur = c.cursor()
    cur.execute("""INSERT INTO orders (user_id,channel_id,plan,days,currency,expected_amount,address,expires_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (uid, cid, plan, days, currency, amount, address, exp))
    oid = cur.fetchone()[0]; c.commit(); cur.close(); c.close()
    return oid

def get_order(oid):
    c = dbc(); cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM orders WHERE id=%s", (oid,))
    r = cur.fetchone(); cur.close(); c.close()
    return dict(r) if r else None

def pending_orders(currency):
    c = dbc(); cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM orders WHERE currency=%s AND status='pending' AND expires_at>now()", (currency,))
    rows = cur.fetchall(); cur.close(); c.close()
    return [dict(r) for r in rows]

def tx_already_used(tx_hash):
    c = dbc(); cur = c.cursor()
    cur.execute("SELECT 1 FROM orders WHERE tx_hash=%s", (tx_hash,))
    r = cur.fetchone(); cur.close(); c.close()
    return r is not None

def mark_order_paid(oid, tx_hash):
    c = dbc(); cur = c.cursor()
    cur.execute("UPDATE orders SET status='paid', tx_hash=%s WHERE id=%s", (tx_hash, oid))
    c.commit(); cur.close(); c.close()

# ---------- AI ----------
async def generate_post(topic, category, instructions):
    if not ai_client:
        return None
    sysp = (f"You create Telegram channel posts about: '{topic}'. "
            "Keep content advertiser-friendly and compliant: no gambling/adult content, no financial "
            "guarantees or 'get rich' claims, no hate or misinformation, safe for all audiences. "
            "Light emojis, under 800 characters, no hashtags unless natural, no greeting or sign-off.")
    usrp = f"Write a Telegram post for category: {category.upper()}.\n\n{instructions}\n\nTopic: {topic}"
    try:
        r = await ai_client.chat.completions.create(
            model=AI_MODEL, messages=[{"role": "system", "content": sysp}, {"role": "user", "content": usrp}],
            temperature=0.9, max_tokens=600)
        return r.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"AI error: {e}")
        return None

# ---------- PRICE ----------
_ton_cache = {"p": None, "t": 0}
async def ton_price():
    if _ton_cache["p"] and time.time() - _ton_cache["t"] < 300:
        return _ton_cache["p"]
    try:
        async with httpx.AsyncClient(timeout=15) as x:
            r = await x.get("https://api.coingecko.com/api/v3/simple/price",
                            params={"ids": "the-open-network", "vs_currencies": "usd"})
            p = float(r.json()["the-open-network"]["usd"])
            _ton_cache.update(p=p, t=time.time())
            return p
    except Exception as e:
        logger.error(f"ton price: {e}")
        return _ton_cache["p"] or 5.0

# ---------- UI ----------
def main_menu(owner_id):
    kb = []
    for ch in get_user_channels(owner_id):
        s = "🟢" if ch["active"] else "⚪"
        t = "✏️" if ch["topic"] else "❓"
        kb.append([InlineKeyboardButton(f"{s}{t} {ch['title']}", callback_data=f"channel:{ch['id']}")])
    kb.append([InlineKeyboardButton("➕ Add Channel", callback_data="add_channel")])
    kb.append([InlineKeyboardButton("ℹ️ Help", callback_data="help")])
    return InlineKeyboardMarkup(kb)

def channel_detail(ch):
    cid = ch["id"]
    status = "🟢 Active" if ch["active"] else "⚪ Paused"
    topic = ch["topic"] or "_not set_"
    today = datetime.now(timezone.utc).date().isoformat()
    done = ch["posts_today"] if ch["last_post_date"] == today else 0
    filled = int((done / POSTS_PER_DAY) * 10)
    bar = "🟩" * filled + "⬜" * (10 - filled)
    kb = [
        [InlineKeyboardButton("✏️ Set Topic", callback_data=f"settopic:{cid}")],
        [InlineKeyboardButton("⏸ Pause" if ch["active"] else "▶️ Activate", callback_data=f"toggle:{cid}")],
        [InlineKeyboardButton("💳 Subscribe this channel", callback_data=f"plan:{cid}")],
        [InlineKeyboardButton("📝 Post Now (test)", callback_data=f"postnow:{cid}")],
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"channel:{cid}"),
         InlineKeyboardButton("🗑 Remove", callback_data=f"remove:{cid}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back")],
    ]
    text = (f"📺 *{ch['title']}*\n\n{channel_status_text(ch)}\n\nStatus: {status}\nTopic: {topic}\n\n"
            f"📊 Today: {bar}  {done}/{POSTS_PER_DAY}")
    return text, InlineKeyboardMarkup(kb)

def plan_markup(cid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"1 Day — $2", callback_data=f"cur:{cid}:daily")],
        [InlineKeyboardButton(f"1 Week — $10", callback_data=f"cur:{cid}:weekly")],
        [InlineKeyboardButton(f"1 Month — $30", callback_data=f"cur:{cid}:monthly")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"channel:{cid}")],
    ])

def currency_markup(cid, plan):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ Telegram Stars", callback_data=f"pay:{cid}:{plan}:stars")],
        [InlineKeyboardButton("💎 TON", callback_data=f"pay:{cid}:{plan}:ton")],
        [InlineKeyboardButton("💵 USDT (TRC-20)", callback_data=f"pay:{cid}:{plan}:usdt")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"plan:{cid}")],
    ])

# ---------- COMMANDS ----------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.first_name)
    text = (
        f"👋 Hi {u.first_name}!\n\n🤖 *AI Channel Auto-Poster*\n\n"
        f"I post {POSTS_PER_DAY}× per day of fresh, approval-friendly content to your channel — "
        f"rotating through 💡 Why • ⭐ Important • 📜 History • 🎉 Fun Fact • ❓ Quiz • 💪 Tips.\n\n"
        f"🎁 Each channel gets a *free 24h trial*, then $2/day, $10/week, or $30/month.\n\n"
        f"Add me as *admin* to a channel to begin.\n\n_Powered by {AI_PROVIDER or 'AI'}_"
    )
    await update.message.reply_text(text, reply_markup=main_menu(u.id), parse_mode="Markdown")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📚 *How it works*\n\n"
        "1️⃣ Add me as *Admin* to your channel (with Post Messages)\n"
        "2️⃣ I detect it automatically\n"
        "3️⃣ Set the topic\n"
        f"4️⃣ Activate — I post {POSTS_PER_DAY}×/day\n\n"
        "🎁 *Free 24h trial per channel.* Then subscribe that channel:\n"
        "• $2 / day\n• $10 / week\n• $30 / month\n"
        "Pay with ⭐ Stars, TON, or USDT.\n\n"
        f"⏰ Posts run {ACTIVE_START_HOUR}:00–{ACTIVE_END_HOUR}:00 UTC.\n\nCommands: /start /help /cancel"
    )
    if update.callback_query:
        await update.callback_query.message.reply_text(text, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled.", reply_markup=main_menu(update.effective_user.id))
    return ConversationHandler.END

async def grant_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    try:
        cid = int(context.args[0]); days = int(context.args[1])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /grant <channel_id> <days>")
        return
    until = grant_channel(cid, days)
    await update.message.reply_text(f"✅ Channel {cid} granted {days}d (until {until.date()}).")

# ---------- CHANNEL DETECTION ----------
async def my_chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.my_chat_member
    if cmu.chat.type != "channel":
        return
    actor = cmu.from_user
    if not actor:
        return
    if cmu.new_chat_member.status == "administrator":
        ensure_user(actor.id, actor.first_name)
        add_channel(actor.id, cmu.chat.id, cmu.chat.title or "Untitled")
        try:
            await context.bot.send_message(
                actor.id, f"✅ Channel *{cmu.chat.title}* linked! Free 24h trial started.\n\nSet its topic to begin.",
                reply_markup=main_menu(actor.id), parse_mode="Markdown")
        except Exception:
            pass
    elif cmu.new_chat_member.status in ("left", "kicked", "member"):
        remove_channel(cmu.chat.id)
        try:
            await context.bot.send_message(actor.id, f"⚠️ Removed *{cmu.chat.title}* (lost admin).", parse_mode="Markdown")
        except Exception:
            pass

# ---------- CALLBACKS ----------
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = q.from_user.id

    if data == "add_channel":
        await q.message.reply_text(
            "➕ *Add a channel*\n\n1. Open your channel\n2. Settings → Administrators → Add Admin\n"
            "3. Add me\n4. I detect it instantly ✅", parse_mode="Markdown")
        return
    if data == "help":
        await help_command(update, context); return
    if data == "back":
        await q.edit_message_text("📋 *Your Channels*", reply_markup=main_menu(uid), parse_mode="Markdown"); return

    if data.startswith("channel:"):
        ch = find_channel(int(data.split(":")[1]))
        if not ch:
            await q.edit_message_text("❌ Not found.", reply_markup=main_menu(uid)); return
        text, kb = channel_detail(ch)
        try: await q.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        except Exception: pass
        return

    if data.startswith("settopic:"):
        context.user_data["topic_chat_id"] = int(data.split(":")[1])
        await q.message.reply_text("✏️ Send the topic for this channel.\n\nExample: _Teaching English to Arabic speakers, beginner level._", parse_mode="Markdown")
        return WAITING_TOPIC

    if data.startswith("toggle:"):
        cid = int(data.split(":")[1]); ch = find_channel(cid)
        if not ch: return
        if not ch["topic"]:
            await q.answer("⚠️ Set the topic first!", show_alert=True); return
        if not ch["active"] and not channel_has_access(ch):
            await q.answer()
            await q.edit_message_text(f"⛔ {channel_status_text(ch)}\n\nSubscribe this channel:",
                                      reply_markup=plan_markup(cid), parse_mode="Markdown")
            return
        new_active = not ch["active"]
        upd = {"active": new_active}
        if new_active: upd["next_post_time"] = datetime.now(timezone.utc)
        update_channel(cid, **upd)
        await q.answer("✅ Activated!" if new_active else "⏸ Paused", show_alert=True)
        ch = find_channel(cid); text, kb = channel_detail(ch)
        try: await q.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        except Exception: pass
        return

    if data.startswith("plan:"):
        cid = int(data.split(":")[1])
        await q.edit_message_text("💳 *Choose a plan for this channel:*", reply_markup=plan_markup(cid), parse_mode="Markdown")
        return

    if data.startswith("cur:"):
        _, cid, plan = data.split(":")
        await q.edit_message_text(f"💳 *{PLANS[plan]['label']} — ${PLANS[plan]['usd']:.0f}*\n\nChoose payment method:",
                                  reply_markup=currency_markup(int(cid), plan), parse_mode="Markdown")
        return

    if data.startswith("pay:"):
        _, cid, plan, cur = data.split(":")
        cid = int(cid)
        await handle_pay(context, q, uid, cid, plan, cur)
        return

    if data.startswith("check:"):
        oid = int(data.split(":")[1])
        await check_order_now(context, q, oid)
        return

    if data.startswith("postnow:"):
        cid = int(data.split(":")[1]); ch = find_channel(cid)
        if not channel_has_access(ch):
            await q.answer("Subscribe to post", show_alert=True)
            await q.edit_message_text(f"⛔ {channel_status_text(ch)}", reply_markup=plan_markup(cid), parse_mode="Markdown")
            return
        await q.answer("Generating…")
        await do_post(context, cid, force=True)
        return

    if data.startswith("remove:"):
        remove_channel(int(data.split(":")[1]))
        await q.edit_message_text("🗑 Removed.", reply_markup=main_menu(uid))
        return

async def handle_pay(context, q, uid, cid, plan, cur):
    p = PLANS[plan]
    if cur == "stars":
        stars = int(round(p["usd"] * STARS_PER_USD))
        await context.bot.send_invoice(
            chat_id=uid, title=f"Channel Access — {p['label']}",
            description=f"{p['label']} of auto-posting for your channel.",
            payload=f"{plan}:{cid}", provider_token="", currency="XTR",
            prices=[LabeledPrice(p["label"], stars)])
        return
    if cur == "ton":
        if not TON_ADDRESS:
            await q.answer("TON not configured", show_alert=True); return
        price = await ton_price()
        base = round(p["usd"] / price, 6)
        amount = unique_amount(base, "TON")
        oid = create_order(uid, cid, plan, p["days"], "TON", amount, TON_ADDRESS)
        await q.edit_message_text(
            f"💎 *Pay with TON*\n\nSend *exactly* this amount:\n\n`{amount}` TON\n\n"
            f"To this address:\n`{TON_ADDRESS}`\n\n"
            f"⚠️ Send the *exact* amount (it identifies your order). Expires in 30 min.\n"
            f"I'll activate the channel automatically once received.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 I've paid — check", callback_data=f"check:{oid}")]]),
            parse_mode="Markdown")
        return
    if cur == "usdt":
        if not TRON_ADDRESS:
            await q.answer("USDT not configured", show_alert=True); return
        amount = unique_amount(p["usd"], "USDT")
        oid = create_order(uid, cid, plan, p["days"], "USDT", amount, TRON_ADDRESS)
        await q.edit_message_text(
            f"💵 *Pay with USDT (TRC-20)*\n\nSend *exactly* this amount:\n\n`{amount}` USDT\n\n"
            f"To this TRC-20 address:\n`{TRON_ADDRESS}`\n\n"
            f"⚠️ TRC-20 network only. Send the *exact* amount. Expires in 30 min.\n"
            f"I'll activate the channel automatically once received.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 I've paid — check", callback_data=f"check:{oid}")]]),
            parse_mode="Markdown")
        return

async def check_order_now(context, q, oid):
    order = get_order(oid)
    if not order:
        await q.answer("Order not found", show_alert=True); return
    if order["status"] == "paid":
        await q.answer("✅ Already paid!", show_alert=True); return
    if order["currency"] == "TON":
        await poll_ton(context.application)
    else:
        await poll_usdt(context.application)
    order = get_order(oid)
    if order["status"] == "paid":
        await q.edit_message_text("✅ Payment confirmed! Channel activated. 🎉")
    else:
        await q.answer("Not detected yet. Wait ~1 min and check again.", show_alert=True)

async def receive_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = context.user_data.get("topic_chat_id")
    if not cid: return ConversationHandler.END
    topic = update.message.text.strip()
    if len(topic) < 5:
        await update.message.reply_text("Topic too short. Try again or /cancel."); return WAITING_TOPIC
    update_channel(cid, topic=topic)
    context.user_data.clear()
    await update.message.reply_text("✅ Topic saved! Tap *▶️ Activate* to start.",
                                    reply_markup=main_menu(update.effective_user.id), parse_mode="Markdown")
    return ConversationHandler.END

# ---------- STARS PAYMENT ----------
async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    payload = update.message.successful_payment.invoice_payload
    plan, cid = payload.split(":")
    cid = int(cid); days = PLANS[plan]["days"]
    until = grant_channel(cid, days)
    ch = find_channel(cid)
    await update.message.reply_text(
        f"🎉 Payment received! *{ch['title'] if ch else 'Channel'}* activated for {days} day(s).\n"
        f"Active until *{until.strftime('%Y-%m-%d %H:%M')} UTC*.",
        reply_markup=main_menu(uid), parse_mode="Markdown")

# ---------- CRYPTO POLLERS ----------
async def notify_paid(app, order):
    ch = find_channel(order["channel_id"])
    try:
        await app.bot.send_message(
            order["user_id"],
            f"🎉 Payment received! *{ch['title'] if ch else 'Channel'}* activated for {order['days']} day(s).",
            parse_mode="Markdown")
    except Exception:
        pass

async def poll_ton(app):
    if not TON_ADDRESS:
        return
    orders = pending_orders("TON")
    if not orders:
        return
    try:
        params = {"address": TON_ADDRESS, "limit": 30}
        if TONCENTER_KEY: params["api_key"] = TONCENTER_KEY
        async with httpx.AsyncClient(timeout=20) as x:
            r = await x.get("https://toncenter.com/api/v2/getTransactions", params=params)
            txs = r.json().get("result", [])
    except Exception as e:
        logger.error(f"TON poll: {e}"); return
    for tx in txs:
        in_msg = tx.get("in_msg", {})
        val = in_msg.get("value")
        if not val: continue
        amount = round(int(val) / 1e9, 6)
        tx_hash = (tx.get("transaction_id") or {}).get("hash", "")
        if not tx_hash or tx_already_used(tx_hash): continue
        for o in orders:
            if abs(float(o["expected_amount"]) - amount) < 0.0000015:
                mark_order_paid(o["id"], tx_hash)
                grant_channel(o["channel_id"], o["days"])
                await notify_paid(app, o)
                break

async def poll_usdt(app):
    if not TRON_ADDRESS:
        return
    orders = pending_orders("USDT")
    if not orders:
        return
    try:
        headers = {"TRON-PRO-API-KEY": TRONGRID_KEY} if TRONGRID_KEY else {}
        url = f"https://api.trongrid.io/v1/accounts/{TRON_ADDRESS}/transactions/trc20"
        params = {"only_to": "true", "limit": 40, "contract_address": USDT_CONTRACT}
        async with httpx.AsyncClient(timeout=20) as x:
            r = await x.get(url, params=params, headers=headers)
            txs = r.json().get("data", [])
    except Exception as e:
        logger.error(f"USDT poll: {e}"); return
    for tx in txs:
        try:
            amount = round(int(tx["value"]) / 1e6, 6)
        except Exception:
            continue
        tx_hash = tx.get("transaction_id", "")
        if not tx_hash or tx_already_used(tx_hash): continue
        for o in orders:
            if abs(float(o["expected_amount"]) - amount) < 0.0000015:
                mark_order_paid(o["id"], tx_hash)
                grant_channel(o["channel_id"], o["days"])
                await notify_paid(app, o)
                break

async def crypto_loop(app):
    await asyncio.sleep(15)
    while True:
        try:
            await poll_ton(app)
            await poll_usdt(app)
        except Exception as e:
            logger.error(f"crypto loop: {e}")
        await asyncio.sleep(30)

# ---------- POSTING ----------
async def do_post(context, cid, force=False):
    ch = find_channel(cid)
    if not ch or not ch["topic"]:
        return
    if not channel_has_access(ch):
        if ch["active"]:
            update_channel(cid, active=False)
            try:
                await context.bot.send_message(
                    ch["owner_id"], f"⛔ Auto-posting paused for *{ch['title']}* — access ended. Subscribe to resume.",
                    reply_markup=plan_markup(cid), parse_mode="Markdown")
            except Exception:
                pass
        return
    idx = ch["category_index"] % len(POST_CATEGORIES)
    cat_name, cat_instr = POST_CATEGORIES[idx]
    content = await generate_post(ch["topic"], cat_name, cat_instr)
    if not content:
        return
    try:
        await context.bot.send_message(cid, content)
    except Exception as e:
        logger.error(f"post failed {cid}: {e}"); return
    today = datetime.now(timezone.utc).date().isoformat()
    posts_today = ch["posts_today"] if ch["last_post_date"] == today else 0
    posts_today += 1
    update_channel(cid, category_index=(idx + 1) % len(POST_CATEGORIES),
                   posts_today=posts_today, last_post_date=today,
                   next_post_time=datetime.now(timezone.utc) + timedelta(minutes=INTERVAL_MINUTES))

async def scheduler_loop(app):
    await asyncio.sleep(10)
    while True:
        try:
            now = datetime.now(timezone.utc)
            for ch in all_active_channels():
                today = now.date().isoformat()
                posts_today = ch["posts_today"] if ch["last_post_date"] == today else 0
                if posts_today >= POSTS_PER_DAY: continue
                if not (ACTIVE_START_HOUR <= now.hour < ACTIVE_END_HOUR): continue
                if ch["next_post_time"] and now < ch["next_post_time"]: continue
                ctx = ContextTypes.DEFAULT_TYPE(application=app, chat_id=None, user_id=ch["owner_id"])
                await do_post(ctx, ch["id"])
        except Exception as e:
            logger.error(f"scheduler: {e}")
        await asyncio.sleep(60)

# ---------- HEALTH ----------
async def health(request):
    return web.Response(text="OK")

async def run_web():
    app = web.Application(); app.router.add_get("/", health)
    r = web.AppRunner(app); await r.setup()
    await web.TCPSite(r, "0.0.0.0", PORT).start()
    logger.info(f"Health on :{PORT}")

async def post_init(app):
    asyncio.create_task(run_web())
    asyncio.create_task(scheduler_loop(app))
    asyncio.create_task(crypto_loop(app))

def main():
    if not BOT_TOKEN or not DATABASE_URL:
        raise RuntimeError("BOT_TOKEN or DATABASE_URL missing")
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    topic_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(menu_callback, pattern=r"^settopic:")],
        states={WAITING_TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_topic)]},
        fallbacks=[CommandHandler("cancel", cancel_command)],
    )
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("grant", grant_command))
    app.add_handler(topic_conv)
    app.add_handler(CallbackQueryHandler(menu_callback))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(ChatMemberHandler(my_chat_member_handler, ChatMemberHandler.MY_CHAT_MEMBER))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
