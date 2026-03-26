import sqlite3
import threading
import os
import time
import requests as req
from flask import Flask, render_template, Response, abort, request as flask_request
from telebot import TeleBot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
)

# ==============================
# Config
# ==============================
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
USER_BOT_TOKEN = os.environ.get("USER_BOT_TOKEN", "")
CHANNEL_ID     = os.environ.get("CHANNEL_ID", "")
ADMIN_USER_ID  = os.environ.get("ADMIN_USER_ID", "")  # Primary admin

# ==============================
# Database
# ==============================
conn    = sqlite3.connect("videos.db", check_same_thread=False)
db_lock = threading.Lock()

def db_query(sql, params=()):
    with db_lock:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        return cur

def db_fetch(sql, params=()):
    with db_lock:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur.fetchall()

def db_fetchone(sql, params=()):
    with db_lock:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur.fetchone()

with db_lock:
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS categories (
        id   INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
    );
    CREATE TABLE IF NOT EXISTS videos (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        title          TEXT,
        file_id        TEXT,
        file_path      TEXT,
        channel_msg_id INTEGER,
        file_size      INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT DEFAULT ''
    );
    """)
    # Safe migrations
    existing = {row[1] for row in c.execute("PRAGMA table_info(videos)")}
    migrations = [
        ("description",        "TEXT DEFAULT ''"),
        ("category_id",        "INTEGER"),
        ("views",              "INTEGER DEFAULT 0"),
        ("created_at",         "TEXT"),
        ("thumbnail_file_id",  "TEXT"),
    ]
    for col, defn in migrations:
        if col not in existing:
            c.execute(f"ALTER TABLE videos ADD COLUMN {col} {defn}")
    conn.commit()

# ==============================
# Admin helpers
# ==============================
def get_admin_ids():
    ids = set()
    if ADMIN_USER_ID:
        ids.add(str(ADMIN_USER_ID))
    row = db_fetchone("SELECT value FROM settings WHERE key='admin_ids'")
    if row and row[0]:
        ids.update(x.strip() for x in row[0].split(',') if x.strip())
    return ids

def is_admin(user_id):
    admins = get_admin_ids()
    if not admins:
        return True   # No admin configured → allow all
    return str(user_id) in admins

def save_admin_ids(ids: set):
    db_query(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('admin_ids', ?)",
        (','.join(ids),)
    )

# ==============================
# Bot keyboard builders
# ==============================
user_states = {}  # {user_id: {state, title, category_id, pending_video, ...}}

def main_menu():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🚀 নতুন পোস্ট আপলোড",      callback_data="new_upload"),
        InlineKeyboardButton("💎 ফুল কালেকশন",           callback_data="full_collection"),
        InlineKeyboardButton("📝 প্রি-সেভ টাইটেল",       callback_data="preset_title"),
        InlineKeyboardButton("🗂️ ক্যাটাগরি ম্যানেজমেন্ট",callback_data="cat_mgmt"),
        InlineKeyboardButton("🗑️ পোস্ট ডিলিট করুন",      callback_data="delete_post"),
        InlineKeyboardButton("⚙️ অ্যাড সেটিংস",          callback_data="ad_settings"),
        InlineKeyboardButton("👥 এডমিন ম্যানেজ",         callback_data="admin_mgmt"),
        InlineKeyboardButton("📊 পোল তৈরি করুন",         callback_data="create_poll"),
    )
    kb.add(InlineKeyboardButton("❌ বাতিল করুন", callback_data="cancel"))
    return kb

def category_menu(back="back_main"):
    cats = db_fetch("SELECT id, name FROM categories ORDER BY name")
    kb   = InlineKeyboardMarkup(row_width=2)
    for cat in cats:
        kb.add(InlineKeyboardButton(f"📁 {cat[1]}", callback_data=f"set_cat_{cat[0]}"))
    kb.add(
        InlineKeyboardButton("➕ নতুন ক্যাটাগরি", callback_data="add_cat"),
        InlineKeyboardButton("⬅️ পিছনে",          callback_data=back),
    )
    return kb

def cancel_btn():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("❌ বাতিল করুন", callback_data="cancel"))
    return kb

def skip_cancel_btn():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("⏭️ Skip (থাম্বনেইল ছাড়া)", callback_data="skip_thumb"),
        InlineKeyboardButton("❌ বাতিল করুন",             callback_data="cancel"),
    )
    return kb

# ==============================
# Thumbnail proxy helper
# ==============================
def proxy_tg_file(file_id, content_type="image/jpeg"):
    try:
        info = req.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
            params={"file_id": file_id}, timeout=10
        ).json()
        file_path = info["result"]["file_path"]
        tg_url    = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        r         = req.get(tg_url, stream=True, timeout=15)

        def gen():
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk
        return Response(gen(), content_type=content_type)
    except Exception:
        abort(404)

# ==============================
# Core video save
# ==============================
def save_video_to_db(bot, video_msg, uid, thumb_file_id=None):
    """Forward video to channel and save to DB. Returns video_id or None."""
    media     = video_msg.video or video_msg.document
    if not media:
        return None

    file_id   = media.file_id
    file_size = getattr(media, "file_size", 0)
    title     = user_states.get(uid, {}).get('title') or video_msg.caption or "Untitled Video"
    cat_id    = user_states.get(uid, {}).get('category_id')

    # Forward video to storage channel
    channel_msg_id = None
    if CHANNEL_ID:
        try:
            fwd = bot.forward_message(CHANNEL_ID, video_msg.chat.id, video_msg.message_id)
            channel_msg_id = fwd.message_id
        except Exception as e:
            print(f"[Bot] Forward error: {e}")

    # Get file_path for streaming
    file_path = None
    try:
        info      = bot.get_file(file_id)
        file_path = info.file_path
    except Exception:
        pass

    cur    = db_query(
        """INSERT INTO videos
           (title, file_id, file_path, channel_msg_id, file_size,
            category_id, thumbnail_file_id, created_at)
           VALUES (?,?,?,?,?,?,?,datetime('now'))""",
        (title, file_id, file_path, channel_msg_id, file_size, cat_id, thumb_file_id)
    )
    return cur.lastrowid

# ==============================
# Admin Bot
# ==============================
def start_admin_bot():
    if not BOT_TOKEN:
        print("[AdminBot] BOT_TOKEN নেই।")
        return

    bot = TeleBot(BOT_TOKEN, threaded=False)

    # Drain pending updates
    print("[AdminBot] পুরনো updates পরিষ্কার করছে...")
    try:
        while True:
            updates = bot.get_updates(limit=100, timeout=3)
            if not updates:
                break
            mid = max(u.update_id for u in updates)
            bot.get_updates(offset=mid + 1, limit=1)
            print(f"[AdminBot] Cleared up to {mid}...")
        print("[AdminBot] Queue পরিষ্কার!")
    except Exception as e:
        print(f"[AdminBot] Clear error: {e}")

    # ── /start ────────────────────────────────────────────────────
    @bot.message_handler(commands=['start'])
    def cmd_start(msg):
        if not is_admin(msg.from_user.id):
            bot.reply_to(msg, "⛔ আপনার এই বটে অ্যাক্সেস নেই।")
            return
        user_states.pop(msg.from_user.id, None)
        bot.send_message(msg.chat.id, "🏠 *এডমিন মেনু*", parse_mode="Markdown", reply_markup=main_menu())

    # ── Callback queries ──────────────────────────────────────────
    @bot.callback_query_handler(func=lambda c: True)
    def cb(call):
        uid  = call.from_user.id
        cid  = call.message.chat.id
        mid  = call.message.message_id
        data = call.data

        if not is_admin(uid):
            bot.answer_callback_query(call.id, "⛔ অ্যাক্সেস নেই।")
            return
        bot.answer_callback_query(call.id)

        # ── Navigation ─────────────────────────────────────────
        if data in ("cancel", "back_main"):
            user_states.pop(uid, None)
            bot.edit_message_text("🏠 *এডমিন মেনু*", cid, mid, parse_mode="Markdown", reply_markup=main_menu())

        # ── New upload ─────────────────────────────────────────
        elif data == "new_upload":
            user_states[uid] = {'state': 'waiting_title'}
            bot.edit_message_text(
                "📝 ভিডিওর *টাইটেল* লিখুন:\n_(বা /skip দিলে 'Untitled' হবে)_",
                cid, mid, parse_mode="Markdown", reply_markup=cancel_btn()
            )

        # ── Preset title ───────────────────────────────────────
        elif data == "preset_title":
            row = db_fetchone("SELECT value FROM settings WHERE key='preset_title'")
            cur = row[0] if row else "(সেট হয়নি)"
            user_states[uid] = {'state': 'waiting_preset_title'}
            bot.edit_message_text(
                f"📝 বর্তমান প্রি-সেভ টাইটেল:\n`{cur}`\n\nনতুন টাইটেল লিখুন:",
                cid, mid, parse_mode="Markdown", reply_markup=cancel_btn()
            )

        # ── Category management ────────────────────────────────
        elif data == "cat_mgmt":
            cats = db_fetch("SELECT id, name FROM categories ORDER BY name")
            text = "🗂️ *ক্যাটাগরি তালিকা:*\n\n" + "\n".join(f"• {c[1]}" for c in cats) if cats else "🗂️ কোনো ক্যাটাগরি নেই।"
            kb   = InlineKeyboardMarkup(row_width=1)
            for cat in cats:
                kb.add(InlineKeyboardButton(f"🗑️ {cat[1]} মুছুন", callback_data=f"del_cat_{cat[0]}"))
            kb.add(
                InlineKeyboardButton("➕ নতুন ক্যাটাগরি", callback_data="add_cat"),
                InlineKeyboardButton("⬅️ পিছনে",          callback_data="back_main"),
            )
            bot.edit_message_text(text, cid, mid, parse_mode="Markdown", reply_markup=kb)

        elif data == "add_cat":
            user_states[uid] = {'state': 'waiting_cat_name'}
            bot.edit_message_text("➕ নতুন ক্যাটাগরির নাম লিখুন:", cid, mid, reply_markup=cancel_btn())

        elif data.startswith("del_cat_"):
            cat_id = int(data.split("_")[-1])
            cat    = db_fetchone("SELECT name FROM categories WHERE id=?", (cat_id,))
            if cat:
                db_query("UPDATE videos SET category_id=NULL WHERE category_id=?", (cat_id,))
                db_query("DELETE FROM categories WHERE id=?", (cat_id,))
                bot.edit_message_text(f"✅ ক্যাটাগরি '{cat[0]}' মুছে গেছে।", cid, mid, reply_markup=main_menu())

        # ── Category select during upload ──────────────────────
        elif data.startswith("set_cat_"):
            cat_id = int(data.split("_")[-1])
            if uid not in user_states:
                user_states[uid] = {}
            user_states[uid]['category_id'] = cat_id
            cat = db_fetchone("SELECT name FROM categories WHERE id=?", (cat_id,))
            cat_name = cat[0] if cat else ""

            pending = user_states[uid].get('pending_video')
            if pending:
                # Video already waiting → ask for thumbnail
                user_states[uid]['state'] = 'waiting_thumb'
                bot.edit_message_text(
                    f"✅ ক্যাটাগরি: *{cat_name}*\n\n📸 এখন ভিডিওর *থাম্বনেইল ইমেজ* পাঠান:\n_(বা Skip বাটনে চাপুন)_",
                    cid, mid, parse_mode="Markdown", reply_markup=skip_cancel_btn()
                )
            else:
                user_states[uid]['state'] = 'waiting_thumb'
                bot.edit_message_text(
                    f"✅ ক্যাটাগরি: *{cat_name}*\n\n📸 এখন ভিডিওর *থাম্বনেইল ইমেজ* পাঠান:\n_(বা Skip বাটনে চাপুন)_",
                    cid, mid, parse_mode="Markdown", reply_markup=skip_cancel_btn()
                )

        elif data == "skip_thumb":
            # Skip thumbnail, move to waiting_video
            if uid in user_states:
                pending = user_states[uid].get('pending_video')
                if pending:
                    _finalize_upload(bot, uid, pending, None)
                    bot.delete_message(cid, mid)
                else:
                    user_states[uid]['state'] = 'waiting_video'
                    bot.edit_message_text(
                        "🎬 এখন ভিডিও পাঠান:",
                        cid, mid, reply_markup=cancel_btn()
                    )

        # ── Full collection ────────────────────────────────────
        elif data == "full_collection":
            videos = db_fetch("SELECT id, title, file_size, views FROM videos ORDER BY id DESC LIMIT 20")
            if not videos:
                bot.edit_message_text("💎 কোনো ভিডিও নেই।", cid, mid, reply_markup=main_menu())
                return
            domain = os.environ.get("REPLIT_DEV_DOMAIN", "")
            text   = "💎 *সকল ভিডিও (সর্বশেষ ২০টি):*\n\n"
            for v in videos:
                sz  = f"{round(v[2]/1024/1024,1)} MB" if v[2] else "?"
                url = f"https://{domain}/video/{v[0]}" if domain else f"/video/{v[0]}"
                text += f"🎬 [{v[1] or 'Untitled'}]({url}) — {sz} — 👁 {v[3]}\n"
            bot.edit_message_text(text, cid, mid, parse_mode="Markdown", reply_markup=main_menu(), disable_web_page_preview=True)

        # ── Delete post ────────────────────────────────────────
        elif data == "delete_post":
            videos = db_fetch("SELECT id, title FROM videos ORDER BY id DESC LIMIT 20")
            if not videos:
                bot.edit_message_text("🗑️ কোনো ভিডিও নেই।", cid, mid, reply_markup=main_menu())
                return
            kb = InlineKeyboardMarkup(row_width=1)
            for v in videos:
                kb.add(InlineKeyboardButton(f"🗑️ {(v[1] or 'Untitled')[:40]} (#{v[0]})", callback_data=f"del_video_{v[0]}"))
            kb.add(InlineKeyboardButton("⬅️ পিছনে", callback_data="back_main"))
            bot.edit_message_text("🗑️ কোন ভিডিও মুছবেন?", cid, mid, reply_markup=kb)

        elif data.startswith("del_video_"):
            vid_id = int(data.split("_")[-1])
            video  = db_fetchone("SELECT title FROM videos WHERE id=?", (vid_id,))
            if video:
                db_query("DELETE FROM videos WHERE id=?", (vid_id,))
                bot.edit_message_text(f"✅ '{video[0] or 'Untitled'}' মুছে গেছে।", cid, mid, reply_markup=main_menu())

        # ── Ad settings ────────────────────────────────────────
        elif data == "ad_settings":
            row = db_fetchone("SELECT value FROM settings WHERE key='ad_code'")
            cur = (row[0] or "")[:80] + "..." if row and row[0] else "(সেট হয়নি)"
            user_states[uid] = {'state': 'waiting_ad_code'}
            bot.edit_message_text(
                f"⚙️ বর্তমান অ্যাড কোড:\n`{cur}`\n\nনতুন HTML অ্যাড কোড পেস্ট করুন:",
                cid, mid, parse_mode="Markdown", reply_markup=cancel_btn()
            )

        # ── Admin management ────────────────────────────────────
        elif data == "admin_mgmt":
            admins = get_admin_ids()
            text   = "👥 *এডমিন তালিকা:*\n\n" + "\n".join(f"• `{a}`" for a in admins) if admins else "👥 কোনো এডমিন নেই।"
            kb     = InlineKeyboardMarkup(row_width=1)
            for a in admins:
                if a != str(ADMIN_USER_ID):   # Can't remove primary admin
                    kb.add(InlineKeyboardButton(f"🗑️ {a} সরান", callback_data=f"del_admin_{a}"))
            kb.add(
                InlineKeyboardButton("➕ নতুন এডমিন যোগ করুন", callback_data="add_admin"),
                InlineKeyboardButton("⬅️ পিছনে",               callback_data="back_main"),
            )
            bot.edit_message_text(text, cid, mid, parse_mode="Markdown", reply_markup=kb)

        elif data == "add_admin":
            user_states[uid] = {'state': 'waiting_admin_id'}
            bot.edit_message_text(
                "👤 নতুন এডমিনের *Telegram User ID* লিখুন:\n_(ID জানতে @userinfobot-এ /start দিন)_",
                cid, mid, parse_mode="Markdown", reply_markup=cancel_btn()
            )

        elif data.startswith("del_admin_"):
            a_id   = data[len("del_admin_"):]
            admins = get_admin_ids()
            admins.discard(a_id)
            save_admin_ids(admins)
            bot.edit_message_text(f"✅ এডমিন `{a_id}` সরানো হয়েছে।", cid, mid, parse_mode="Markdown", reply_markup=main_menu())

        # ── Poll ───────────────────────────────────────────────
        elif data == "create_poll":
            user_states[uid] = {'state': 'waiting_poll_q'}
            bot.edit_message_text("📊 পোলের *প্রশ্ন* লিখুন:", cid, mid, parse_mode="Markdown", reply_markup=cancel_btn())

    # ── Text handler ──────────────────────────────────────────────
    @bot.message_handler(content_types=['text'])
    def handle_text(msg):
        if msg.chat.type != 'private':
            return
        uid   = msg.from_user.id
        if not is_admin(uid):
            return
        state = user_states.get(uid, {}).get('state')
        text  = msg.text.strip()

        if state == 'waiting_title':
            title = '' if text.lower() == '/skip' else text
            user_states[uid] = {'state': 'waiting_cat_select', 'title': title}
            bot.send_message(uid, "🗂️ ক্যাটাগরি বেছে নিন:", reply_markup=category_menu())

        elif state == 'waiting_cat_name':
            try:
                db_query("INSERT OR IGNORE INTO categories (name) VALUES (?)", (text,))
                user_states.pop(uid, None)
                bot.send_message(uid, f"✅ ক্যাটাগরি '{text}' যোগ হয়েছে।", reply_markup=main_menu())
            except Exception as e:
                bot.send_message(uid, f"❌ Error: {e}")

        elif state == 'waiting_preset_title':
            db_query("INSERT OR REPLACE INTO settings (key, value) VALUES ('preset_title', ?)", (text,))
            user_states.pop(uid, None)
            bot.send_message(uid, f"✅ প্রি-সেভ টাইটেল সেট:\n`{text}`", parse_mode="Markdown", reply_markup=main_menu())

        elif state == 'waiting_ad_code':
            db_query("INSERT OR REPLACE INTO settings (key, value) VALUES ('ad_code', ?)", (text,))
            user_states.pop(uid, None)
            bot.send_message(uid, "✅ অ্যাড কোড সেভ হয়েছে।", reply_markup=main_menu())

        elif state == 'waiting_admin_id':
            new_id = text.strip()
            if not new_id.lstrip('-').isdigit():
                bot.send_message(uid, "❌ সঠিক Telegram User ID লিখুন (শুধু সংখ্যা)।")
                return
            admins = get_admin_ids()
            admins.add(new_id)
            save_admin_ids(admins)
            user_states.pop(uid, None)
            bot.send_message(uid, f"✅ এডমিন `{new_id}` যোগ হয়েছে।", parse_mode="Markdown", reply_markup=main_menu())

        elif state == 'waiting_poll_q':
            user_states[uid]['question'] = text
            user_states[uid]['state']    = 'waiting_poll_opts'
            bot.send_message(uid, "📊 অপশনগুলো লিখুন (প্রতিটা আলাদা লাইনে, ২–১০টা):", reply_markup=cancel_btn())

        elif state == 'waiting_poll_opts':
            opts = [o.strip() for o in text.split('\n') if o.strip()][:10]
            if len(opts) < 2:
                bot.send_message(uid, "❌ কমপক্ষে ২টা অপশন দিন।")
                return
            q = user_states[uid].get('question', 'পোল')
            try:
                bot.send_poll(uid, q, opts, is_anonymous=False)
                user_states.pop(uid, None)
                bot.send_message(uid, "✅ পোল তৈরি হয়েছে।", reply_markup=main_menu())
            except Exception as e:
                bot.send_message(uid, f"❌ Error: {e}")

        else:
            bot.send_message(uid, "🏠 *এডমিন মেনু*", parse_mode="Markdown", reply_markup=main_menu())

    # ── Photo handler (thumbnail) ─────────────────────────────────
    @bot.message_handler(content_types=['photo'])
    def handle_photo(msg):
        if msg.chat.type != 'private':
            return
        uid   = msg.from_user.id
        if not is_admin(uid):
            return
        state = user_states.get(uid, {}).get('state')
        if state != 'waiting_thumb':
            return

        # Largest photo = best quality
        thumb_file_id = msg.photo[-1].file_id
        user_states[uid]['thumb_file_id'] = thumb_file_id

        pending = user_states[uid].get('pending_video')
        if pending:
            _finalize_upload(bot, uid, pending, thumb_file_id)
        else:
            user_states[uid]['state'] = 'waiting_video'
            bot.reply_to(msg, "✅ থাম্বনেইল সেভ হয়েছে!\n\n🎬 এখন ভিডিও পাঠান:", reply_markup=cancel_btn())

    # ── Video handler ─────────────────────────────────────────────
    @bot.message_handler(content_types=['video', 'document'])
    def handle_video(msg):
        if msg.chat.type != 'private':
            return
        uid   = msg.from_user.id
        if not is_admin(uid):
            return

        state = user_states.get(uid, {}).get('state', '')

        if state == 'waiting_video':
            thumb_id = user_states[uid].get('thumb_file_id')
            _finalize_upload(bot, uid, msg, thumb_id)

        else:
            # No active state → quick upload: get preset title, ask category
            preset = db_fetchone("SELECT value FROM settings WHERE key='preset_title'")
            title  = msg.caption or (preset[0] if preset and preset[0] else "Untitled Video")
            user_states[uid] = {
                'state':         'waiting_cat_select',
                'title':         title,
                'pending_video': msg,
            }
            bot.reply_to(msg, "🗂️ ক্যাটাগরি বেছে নিন:", reply_markup=category_menu())

    def _finalize_upload(bot, uid, video_msg, thumb_file_id):
        vid_id = save_video_to_db(bot, video_msg, uid, thumb_file_id)
        user_states.pop(uid, None)

        if not vid_id:
            bot.send_message(uid, "❌ ভিডিও সেভ হয়নি।", reply_markup=main_menu())
            return

        domain     = os.environ.get("REPLIT_DEV_DOMAIN", "")
        stream_url = f"https://{domain}/video/{vid_id}" if domain else ""
        row        = db_fetchone("SELECT title, file_size, category_id FROM videos WHERE id=?", (vid_id,))
        title      = row[0] if row else "?"
        size_mb    = f"{round(row[1]/1024/1024,2)} MB" if row and row[1] else "?"
        cat_name   = ""
        if row and row[2]:
            cat = db_fetchone("SELECT name FROM categories WHERE id=?", (row[2],))
            cat_name = f"\n🗂️ ক্যাটাগরি: {cat[0]}" if cat else ""

        thumb_txt  = "✅ থাম্বনেইল সহ" if thumb_file_id else "⚠️ থাম্বনেইল ছাড়া"
        msg_text   = (
            f"✅ ভিডিও আপলোড সফল!\n"
            f"📌 টাইটেল: {title}\n"
            f"📦 সাইজ: {size_mb}"
            f"{cat_name}\n"
            f"{thumb_txt}"
        )
        if stream_url:
            msg_text += f"\n🔗 {stream_url}"

        bot.send_message(uid, msg_text, reply_markup=main_menu())

    # ── Polling ───────────────────────────────────────────────────
    while True:
        try:
            bot.polling(none_stop=True, skip_pending=True, timeout=30, long_polling_timeout=20)
        except Exception as e:
            print(f"[AdminBot] Polling crashed: {e}")
            time.sleep(5)

# ==============================
# User Stream Bot (optional)
# ==============================
def start_user_bot():
    if not USER_BOT_TOKEN:
        print("[UserBot] USER_BOT_TOKEN নেই — User Bot চালু হচ্ছে না।")
        return

    ubot   = TeleBot(USER_BOT_TOKEN, threaded=False)
    domain = os.environ.get("REPLIT_DEV_DOMAIN", "")

    # Drain pending
    try:
        while True:
            updates = ubot.get_updates(limit=100, timeout=3)
            if not updates:
                break
            mid = max(u.update_id for u in updates)
            ubot.get_updates(offset=mid + 1, limit=1)
        print("[UserBot] Queue পরিষ্কার!")
    except Exception as e:
        print(f"[UserBot] Clear error: {e}")

    @ubot.message_handler(commands=['start'])
    def user_start(msg):
        kb = InlineKeyboardMarkup()
        if domain:
            kb.add(InlineKeyboardButton(
                "🎬 সব ভিডিও দেখুন",
                url=f"https://{domain}"
            ))
            kb.add(InlineKeyboardButton(
                "🌐 ওয়েবসাইট খুলুন",
                web_app=WebAppInfo(url=f"https://{domain}")
            ))
        ubot.send_message(
            msg.chat.id,
            "🎬 *VideoHub-এ স্বাগতম!*\n\nনিচের বাটনে ক্লিক করে সব ভিডিও দেখুন:",
            parse_mode="Markdown",
            reply_markup=kb
        )

    @ubot.message_handler(func=lambda m: True)
    def user_any(msg):
        kb = InlineKeyboardMarkup()
        if domain:
            kb.add(InlineKeyboardButton("🎬 ভিডিও দেখুন", url=f"https://{domain}"))
        ubot.send_message(msg.chat.id, "👇 এখানে ক্লিক করুন:", reply_markup=kb)

    while True:
        try:
            ubot.polling(none_stop=True, skip_pending=True, timeout=30, long_polling_timeout=20)
        except Exception as e:
            print(f"[UserBot] Crashed: {e}")
            time.sleep(5)

# ==============================
# Flask App
# ==============================
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    search    = flask_request.args.get("q", "").strip()
    cat_id    = flask_request.args.get("cat", "")
    sql       = """SELECT v.id, v.title, v.file_size, v.views, v.created_at,
                          c.name, v.thumbnail_file_id
                   FROM videos v LEFT JOIN categories c ON v.category_id=c.id"""
    params    = []
    where     = []
    if search:
        where.append("v.title LIKE ?")
        params.append(f"%{search}%")
    if cat_id:
        where.append("v.category_id=?")
        params.append(cat_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY v.id DESC"
    videos = db_fetch(sql, params)
    cats   = db_fetch("SELECT id, name FROM categories ORDER BY name")
    return render_template("index.html", videos=videos, cats=cats, search=search, selected_cat=cat_id)

@flask_app.route("/video/<int:video_id>")
def video_page(video_id):
    video = db_fetchone(
        """SELECT v.id, v.title, v.description, v.file_size, v.views,
                  v.created_at, c.name, v.thumbnail_file_id
           FROM videos v LEFT JOIN categories c ON v.category_id=c.id
           WHERE v.id=?""",
        (video_id,)
    )
    if not video:
        abort(404)
    db_query("UPDATE videos SET views=views+1 WHERE id=?", (video_id,))
    related = db_fetch(
        """SELECT id, title, file_size, views, thumbnail_file_id
           FROM videos WHERE id!=? ORDER BY id DESC LIMIT 12""",
        (video_id,)
    )
    return render_template("video.html", video=video, related=related)

@flask_app.route("/thumb/<int:video_id>")
def thumb(video_id):
    row = db_fetchone("SELECT thumbnail_file_id FROM videos WHERE id=?", (video_id,))
    if not row or not row[0]:
        abort(404)
    return proxy_tg_file(row[0], "image/jpeg")

@flask_app.route("/stream/<int:video_id>")
def stream_video(video_id):
    row = db_fetchone("SELECT file_path, file_size FROM videos WHERE id=?", (video_id,))
    if not row:
        abort(404)
    file_path, file_size = row
    if not file_path or not BOT_TOKEN:
        abort(503)

    range_hdr = flask_request.headers.get("Range")
    tg_url    = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    try:
        r = req.get(tg_url, headers={"Range": range_hdr} if range_hdr else {}, stream=True, timeout=30)

        def gen():
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk

        headers = {
            "Content-Type":        "video/mp4",
            "Content-Disposition": "inline",
            "Accept-Ranges":       "bytes",
        }
        for h in ("Content-Length", "Content-Range"):
            if h in r.headers:
                headers[h] = r.headers[h]
        return Response(gen(), status=r.status_code, headers=headers)
    except Exception as e:
        print(f"[Stream] Error: {e}")
        abort(503)

# ==============================
# Start
# ==============================
threading.Thread(target=start_admin_bot, daemon=True).start()
threading.Thread(target=start_user_bot,  daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port, threaded=True)
