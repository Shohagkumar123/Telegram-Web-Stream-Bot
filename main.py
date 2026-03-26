import sqlite3
import threading
import os
import re
import time
import requests as req
from flask import Flask, render_template, Response, abort, request as flask_request, jsonify
from telebot import TeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ==============================
# Config
# ==============================
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID   = os.environ.get("CHANNEL_ID", "")
ADMIN_USER_ID = os.environ.get("ADMIN_USER_ID", "")

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
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL UNIQUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    # Migrate: add new columns if they don't exist yet
    existing_cols = {row[1] for row in c.execute("PRAGMA table_info(videos)")}
    new_cols = {
        "description": "TEXT DEFAULT ''",
        "category_id": "INTEGER",
        "views":       "INTEGER DEFAULT 0",
        "created_at":  "TEXT",
    }
    for col_name, col_def in new_cols.items():
        if col_name not in existing_cols:
            c.execute(f"ALTER TABLE videos ADD COLUMN {col_name} {col_def}")
    conn.commit()

# ==============================
# Bot helpers
# ==============================
user_states = {}  # {user_id: {state, title, category_id, ...}}

def is_admin(user_id):
    if not ADMIN_USER_ID:
        return True
    return str(user_id) == str(ADMIN_USER_ID)

def main_menu():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🚀 নতুন পোস্ট আপলোড",     callback_data="new_upload"),
        InlineKeyboardButton("💎 ফুল কালেকশন",          callback_data="full_collection"),
        InlineKeyboardButton("📝 প্রি-সেভ টাইটেল",      callback_data="preset_title"),
        InlineKeyboardButton("🗂️ ক্যাটাগরি ম্যানেজমেন্ট", callback_data="cat_mgmt"),
        InlineKeyboardButton("🗑️ পোস্ট ডিলিট করুন",     callback_data="delete_post"),
        InlineKeyboardButton("⚙️ অ্যাড সেটিংস",         callback_data="ad_settings"),
        InlineKeyboardButton("📊 পোল তৈরি করুন",        callback_data="create_poll"),
        InlineKeyboardButton("📅 শিডিউল লিস্ট",         callback_data="schedule_list"),
    )
    kb.add(InlineKeyboardButton("❌ বাতিল করুন", callback_data="cancel"))
    return kb

def category_menu():
    cats = db_fetch("SELECT id, name FROM categories ORDER BY name")
    kb = InlineKeyboardMarkup(row_width=2)
    for cat in cats:
        kb.add(InlineKeyboardButton(f"📁 {cat[1]}", callback_data=f"set_cat_{cat[0]}"))
    kb.add(
        InlineKeyboardButton("➕ নতুন ক্যাটাগরি", callback_data="add_cat"),
        InlineKeyboardButton("⬅️ পিছনে",          callback_data="back_main"),
    )
    return kb

def cancel_btn():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("❌ বাতিল করুন", callback_data="cancel"))
    return kb

# ==============================
# Bot Thread
# ==============================
def start_bot():
    if not BOT_TOKEN:
        print("[Bot] BOT_TOKEN নেই।")
        return

    bot = TeleBot(BOT_TOKEN, threaded=False)

    # Clear pending updates
    print("[Bot] পুরনো updates পরিষ্কার করছে...")
    try:
        while True:
            updates = bot.get_updates(limit=100, timeout=3)
            if not updates:
                break
            mid = max(u.update_id for u in updates)
            bot.get_updates(offset=mid + 1, limit=1)
            print(f"[Bot] Cleared up to {mid}...")
        print("[Bot] Queue পরিষ্কার!")
    except Exception as e:
        print(f"[Bot] Clear error: {e}")

    # ── /start ────────────────────────────────────────────────────
    @bot.message_handler(commands=['start'])
    def cmd_start(message):
        if not is_admin(message.from_user.id):
            bot.reply_to(message, "⛔ আপনার এই বটে অ্যাক্সেস নেই।")
            return
        user_states.pop(message.from_user.id, None)
        bot.send_message(message.chat.id, "🏠 *মেনু ওপেন হয়েছে।*", parse_mode="Markdown", reply_markup=main_menu())

    # ── Callback queries ──────────────────────────────────────────
    @bot.callback_query_handler(func=lambda c: True)
    def handle_cb(call):
        uid  = call.from_user.id
        cid  = call.message.chat.id
        mid  = call.message.message_id
        data = call.data

        if not is_admin(uid):
            bot.answer_callback_query(call.id, "⛔ অ্যাক্সেস নেই।")
            return

        bot.answer_callback_query(call.id)

        # ── Main menu actions ──────────────────────────────────
        if data == "cancel" or data == "back_main":
            user_states.pop(uid, None)
            bot.edit_message_text("🏠 *মেনু ওপেন হয়েছে।*", cid, mid, parse_mode="Markdown", reply_markup=main_menu())

        elif data == "new_upload":
            user_states[uid] = {'state': 'waiting_title'}
            bot.edit_message_text(
                "📝 ভিডিওর *টাইটেল* লিখুন:\n(বা /skip লিখলে 'Untitled' হবে)",
                cid, mid, parse_mode="Markdown", reply_markup=cancel_btn()
            )

        elif data == "preset_title":
            row = db_fetchone("SELECT value FROM settings WHERE key='preset_title'")
            current = row[0] if row else "(কোনো টাইটেল নেই)"
            user_states[uid] = {'state': 'waiting_preset_title'}
            bot.edit_message_text(
                f"📝 বর্তমান প্রি-সেভ টাইটেল:\n`{current}`\n\nনতুন টাইটেল লিখুন:",
                cid, mid, parse_mode="Markdown", reply_markup=cancel_btn()
            )

        elif data == "cat_mgmt":
            cats = db_fetch("SELECT id, name FROM categories ORDER BY name")
            if cats:
                text = "🗂️ *ক্যাটাগরি তালিকা:*\n\n" + "\n".join(f"• {c[1]}" for c in cats)
            else:
                text = "🗂️ কোনো ক্যাটাগরি নেই।"
            kb = InlineKeyboardMarkup(row_width=1)
            for cat in cats:
                kb.add(InlineKeyboardButton(f"🗑️ {cat[1]} মুছুন", callback_data=f"del_cat_{cat[0]}"))
            kb.add(
                InlineKeyboardButton("➕ নতুন ক্যাটাগরি যোগ করুন", callback_data="add_cat"),
                InlineKeyboardButton("⬅️ পিছনে", callback_data="back_main"),
            )
            bot.edit_message_text(text, cid, mid, parse_mode="Markdown", reply_markup=kb)

        elif data == "add_cat":
            user_states[uid] = {'state': 'waiting_cat_name'}
            bot.edit_message_text(
                "➕ নতুন ক্যাটাগরির নাম লিখুন:",
                cid, mid, reply_markup=cancel_btn()
            )

        elif data.startswith("del_cat_"):
            cat_id = int(data.split("_")[-1])
            cat = db_fetchone("SELECT name FROM categories WHERE id=?", (cat_id,))
            if cat:
                db_query("UPDATE videos SET category_id=NULL WHERE category_id=?", (cat_id,))
                db_query("DELETE FROM categories WHERE id=?", (cat_id,))
                bot.edit_message_text(f"✅ ক্যাটাগরি '{cat[0]}' মুছে গেছে।", cid, mid, reply_markup=main_menu())

        elif data == "full_collection":
            videos = db_fetch("SELECT id, title, file_size, views FROM videos ORDER BY id DESC LIMIT 20")
            if not videos:
                bot.edit_message_text("💎 কোনো ভিডিও নেই।", cid, mid, reply_markup=main_menu())
                return
            domain = os.environ.get("REPLIT_DEV_DOMAIN", "")
            text = "💎 *সকল ভিডিও (সর্বশেষ ২০টি):*\n\n"
            for v in videos:
                size = f"{round(v[2]/1024/1024,1)} MB" if v[2] else "?"
                url  = f"https://{domain}/video/{v[0]}" if domain else f"/video/{v[0]}"
                text += f"🎬 [{v[1] or 'Untitled'}]({url}) — {size} — 👁 {v[3]}\n"
            bot.edit_message_text(text, cid, mid, parse_mode="Markdown", reply_markup=main_menu(), disable_web_page_preview=True)

        elif data == "delete_post":
            videos = db_fetch("SELECT id, title FROM videos ORDER BY id DESC LIMIT 20")
            if not videos:
                bot.edit_message_text("🗑️ কোনো ভিডিও নেই।", cid, mid, reply_markup=main_menu())
                return
            kb = InlineKeyboardMarkup(row_width=1)
            for v in videos:
                kb.add(InlineKeyboardButton(f"🗑️ {v[1] or 'Untitled'} (#{v[0]})", callback_data=f"del_video_{v[0]}"))
            kb.add(InlineKeyboardButton("⬅️ পিছনে", callback_data="back_main"))
            bot.edit_message_text("🗑️ কোন ভিডিও মুছবেন?", cid, mid, reply_markup=kb)

        elif data.startswith("del_video_"):
            vid_id = int(data.split("_")[-1])
            video  = db_fetchone("SELECT title FROM videos WHERE id=?", (vid_id,))
            if video:
                db_query("DELETE FROM videos WHERE id=?", (vid_id,))
                bot.edit_message_text(
                    f"✅ '{video[0] or 'Untitled'}' মুছে গেছে।",
                    cid, mid, reply_markup=main_menu()
                )

        elif data == "ad_settings":
            row = db_fetchone("SELECT value FROM settings WHERE key='ad_code'")
            current = row[0] if row else "(কোনো অ্যাড কোড নেই)"
            user_states[uid] = {'state': 'waiting_ad_code'}
            bot.edit_message_text(
                f"⚙️ বর্তমান অ্যাড কোড:\n`{current[:100]}...`\n\nনতুন অ্যাড HTML কোড পেস্ট করুন:",
                cid, mid, parse_mode="Markdown", reply_markup=cancel_btn()
            )

        elif data == "create_poll":
            user_states[uid] = {'state': 'waiting_poll_question'}
            bot.edit_message_text(
                "📊 পোলের *প্রশ্ন* লিখুন:",
                cid, mid, parse_mode="Markdown", reply_markup=cancel_btn()
            )

        elif data.startswith("set_cat_"):
            cat_id = int(data.split("_")[-1])
            if uid in user_states:
                user_states[uid]['category_id'] = cat_id
                user_states[uid]['state']        = 'waiting_video'
                cat = db_fetchone("SELECT name FROM categories WHERE id=?", (cat_id,))
                bot.edit_message_text(
                    f"✅ ক্যাটাগরি: *{cat[0]}*\n\nএখন ভিডিও পাঠান:",
                    cid, mid, parse_mode="Markdown", reply_markup=cancel_btn()
                )

        elif data == "schedule_list":
            bot.edit_message_text(
                "📅 এই ফিচার শীঘ্রই আসছে।",
                cid, mid, reply_markup=main_menu()
            )

    # ── Text handler ──────────────────────────────────────────────
    @bot.message_handler(content_types=['text'])
    def handle_text(message):
        if message.chat.type != 'private':
            return
        uid   = message.from_user.id
        if not is_admin(uid):
            return

        state = user_states.get(uid, {}).get('state')
        text  = message.text.strip()

        if state == 'waiting_title':
            title = '' if text == '/skip' else text
            user_states[uid] = {'state': 'waiting_category_select', 'title': title}
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
            bot.send_message(uid, f"✅ প্রি-সেভ টাইটেল সেট হয়েছে:\n`{text}`", parse_mode="Markdown", reply_markup=main_menu())

        elif state == 'waiting_ad_code':
            db_query("INSERT OR REPLACE INTO settings (key, value) VALUES ('ad_code', ?)", (text,))
            user_states.pop(uid, None)
            bot.send_message(uid, "✅ অ্যাড কোড সেভ হয়েছে।", reply_markup=main_menu())

        elif state == 'waiting_poll_question':
            user_states[uid]['state']    = 'waiting_poll_options'
            user_states[uid]['question'] = text
            bot.send_message(uid, "📊 অপশনগুলো লিখুন (প্রতিটা আলাদা লাইনে, সর্বোচ্চ ১০টা):", reply_markup=cancel_btn())

        elif state == 'waiting_poll_options':
            options = [o.strip() for o in text.split('\n') if o.strip()][:10]
            if len(options) < 2:
                bot.send_message(uid, "❌ কমপক্ষে ২টা অপশন দিন।")
                return
            question = user_states[uid].get('question', 'পোল')
            try:
                bot.send_poll(uid, question, options, is_anonymous=False)
                user_states.pop(uid, None)
                bot.send_message(uid, "✅ পোল তৈরি হয়েছে।", reply_markup=main_menu())
            except Exception as e:
                bot.send_message(uid, f"❌ Error: {e}")

        elif state is None and text == '/start':
            pass  # handled by command handler

        else:
            if is_admin(uid):
                bot.send_message(uid, "🏠 মেনু:", reply_markup=main_menu())

    # ── Video handler ─────────────────────────────────────────────
    @bot.message_handler(content_types=['video', 'document'])
    def handle_video(message):
        if message.chat.type != 'private':
            return
        uid = message.from_user.id
        if not is_admin(uid):
            return

        media     = message.video or message.document
        if not media:
            return

        state     = user_states.get(uid, {}).get('state', '')
        file_id   = media.file_id
        file_size = getattr(media, "file_size", 0)

        # If no state, start quick upload
        if state not in ('waiting_video', 'waiting_category_select'):
            preset = db_fetchone("SELECT value FROM settings WHERE key='preset_title'")
            title  = message.caption or (preset[0] if preset and preset[0] else "Untitled Video")
            user_states[uid] = {'state': 'waiting_category_select', 'title': title, 'pending_video': message}
            bot.reply_to(message, "🗂️ ক্যাটাগরি বেছে নিন:", reply_markup=category_menu())
            return

        if state == 'waiting_category_select':
            # Store video for later
            user_states[uid]['pending_video'] = message
            return

        # state == 'waiting_video'
        _save_video(bot, message, uid, media, file_id, file_size)

    def _save_video(bot, message, uid, media, file_id, file_size):
        title       = user_states.get(uid, {}).get('title') or message.caption or "Untitled Video"
        category_id = user_states.get(uid, {}).get('category_id')

        # Forward to channel
        channel_msg_id = None
        if CHANNEL_ID:
            try:
                fwd = bot.forward_message(CHANNEL_ID, message.chat.id, message.message_id)
                channel_msg_id = fwd.message_id
            except Exception as e:
                print(f"[Bot] Forward error: {e}")

        # Get file_path
        file_path = None
        try:
            info = bot.get_file(file_id)
            file_path = info.file_path
        except Exception:
            pass

        cur = db_query(
            "INSERT INTO videos (title, file_id, file_path, channel_msg_id, file_size, category_id, created_at) VALUES (?,?,?,?,?,?,datetime('now'))",
            (title, file_id, file_path, channel_msg_id, file_size, category_id)
        )
        vid_id = cur.lastrowid
        user_states.pop(uid, None)

        size_mb  = round(file_size / 1024 / 1024, 2) if file_size else "?"
        cat_name = ""
        if category_id:
            cat = db_fetchone("SELECT name FROM categories WHERE id=?", (category_id,))
            cat_name = f"\n🗂️ ক্যাটাগরি: {cat[0]}" if cat else ""

        domain     = os.environ.get("REPLIT_DEV_DOMAIN", "")
        stream_url = f"https://{domain}/video/{vid_id}" if domain else ""

        reply = (
            f"✅ ভিডিও সেভ হয়েছে!\n"
            f"📌 শিরোনাম: {title}\n"
            f"📦 সাইজ: {size_mb} MB"
            f"{cat_name}"
        )
        if stream_url:
            reply += f"\n🔗 লিংক: {stream_url}"
        if channel_msg_id:
            reply += "\n✅ Channel-এ সেভ হয়েছে"

        try:
            bot.send_message(uid, reply, reply_markup=main_menu())
        except Exception as e:
            print(f"[Bot] Reply error: {e}")

    # Category selection callback also needs to handle pending video
    _orig_handle_cb = handle_cb.__wrapped__ if hasattr(handle_cb, '__wrapped__') else None

    @bot.callback_query_handler(func=lambda c: c.data.startswith("set_cat_"))
    def handle_set_cat(call):
        uid    = call.from_user.id
        cat_id = int(call.data.split("_")[-1])
        bot.answer_callback_query(call.id)
        if uid in user_states:
            user_states[uid]['category_id'] = cat_id
            pending = user_states[uid].get('pending_video')
            if pending:
                media = pending.video or pending.document
                user_states[uid]['state'] = 'waiting_video'
                _save_video(bot, pending, uid, media, media.file_id, getattr(media, "file_size", 0))
                bot.delete_message(call.message.chat.id, call.message.message_id)
            else:
                user_states[uid]['state'] = 'waiting_video'
                cat = db_fetchone("SELECT name FROM categories WHERE id=?", (cat_id,))
                bot.edit_message_text(
                    f"✅ ক্যাটাগরি: *{cat[0] if cat else ''}*\n\nএখন ভিডিও পাঠান:",
                    call.message.chat.id, call.message.message_id,
                    parse_mode="Markdown", reply_markup=cancel_btn()
                )

    # Polling
    while True:
        try:
            bot.polling(none_stop=True, skip_pending=True, timeout=30, long_polling_timeout=20)
        except Exception as e:
            print(f"[Bot] Polling crashed, restarting: {e}")
            time.sleep(5)

# ==============================
# Flask App
# ==============================
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    search = flask_request.args.get("q", "").strip()
    cat_id = flask_request.args.get("cat", "")
    
    sql    = "SELECT v.id, v.title, v.file_size, v.views, v.created_at, c.name FROM videos v LEFT JOIN categories c ON v.category_id=c.id"
    params = []
    where  = []
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
        "SELECT v.id, v.title, v.description, v.file_size, v.views, v.created_at, c.name FROM videos v LEFT JOIN categories c ON v.category_id=c.id WHERE v.id=?",
        (video_id,)
    )
    if not video:
        abort(404)
    db_query("UPDATE videos SET views=views+1 WHERE id=?", (video_id,))
    related = db_fetch(
        "SELECT id, title, file_size, views FROM videos WHERE id!=? ORDER BY id DESC LIMIT 10",
        (video_id,)
    )
    return render_template("video.html", video=video, related=related)

@flask_app.route("/stream/<int:video_id>")
def stream_video(video_id):
    row = db_fetchone("SELECT file_path, file_size FROM videos WHERE id=?", (video_id,))
    if not row:
        abort(404)
    file_path, file_size = row
    file_size = file_size or 0

    if not file_path or not BOT_TOKEN:
        abort(503)

    range_header = flask_request.headers.get("Range")
    req_headers  = {"Range": range_header} if range_header else {}
    tg_url       = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

    try:
        tg_resp = req.get(tg_url, headers=req_headers, stream=True, timeout=30)

        def generate():
            for chunk in tg_resp.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk

        resp_headers = {
            "Content-Type":        "video/mp4",
            "Content-Disposition": "inline",
            "Accept-Ranges":       "bytes",
        }
        if "Content-Length" in tg_resp.headers:
            resp_headers["Content-Length"] = tg_resp.headers["Content-Length"]
        if "Content-Range" in tg_resp.headers:
            resp_headers["Content-Range"] = tg_resp.headers["Content-Range"]

        return Response(generate(), status=tg_resp.status_code, headers=resp_headers)
    except Exception as e:
        print(f"[Stream] Error: {e}")
        abort(503)

# ==============================
# Start
# ==============================
threading.Thread(target=start_bot, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port, threaded=True)
