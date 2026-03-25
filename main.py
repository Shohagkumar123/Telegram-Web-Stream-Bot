import sqlite3
import threading
import os
import re
import time
import requests as req
from flask import Flask, render_template, Response, abort, request as flask_request

# ==============================
# 1. Config
# ==============================
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")

# ==============================
# 2. Database Setup
# ==============================
conn    = sqlite3.connect("videos.db", check_same_thread=False)
db_lock = threading.Lock()

def db_query(sql, params=()):
    with db_lock:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        return cur

with db_lock:
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS videos (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        title          TEXT,
        file_id        TEXT,
        file_path      TEXT,
        channel_msg_id INTEGER,
        file_size      INTEGER
    )
    """)
    for col in ["file_path TEXT", "channel_msg_id INTEGER", "file_size INTEGER"]:
        try:
            c.execute(f"ALTER TABLE videos ADD COLUMN {col}")
        except Exception:
            pass
    conn.commit()

# ==============================
# 3. Telegram Bot (telebot only)
# ==============================
def start_bot():
    if not BOT_TOKEN:
        print("[Bot] BOT_TOKEN নেই।")
        return

    import telebot
    bot = telebot.TeleBot(BOT_TOKEN, threaded=False)

    # ── সব pending update drain করো ──────────────────────────────
    print("[Bot] Clearing all pending updates...")
    try:
        while True:
            updates = bot.get_updates(limit=100, timeout=3)
            if not updates:
                break
            max_id = max(u.update_id for u in updates)
            bot.get_updates(offset=max_id + 1, limit=1)
            print(f"[Bot] Cleared up to update_id {max_id}...")
        print("[Bot] Queue সম্পূর্ণ পরিষ্কার!")
    except Exception as e:
        print(f"[Bot] Clear error: {e}")

    # ── Video handler ─────────────────────────────────────────────
    @bot.message_handler(content_types=['video', 'document'])
    def handle_video(message):
        if message.chat.type == 'channel':
            return

        media = message.video or message.document
        if not media:
            return

        file_id   = media.file_id
        file_size = getattr(media, "file_size", 0)
        title     = message.caption if message.caption else "Untitled Video"

        # Channel-এ forward
        channel_msg_id = None
        if CHANNEL_ID:
            try:
                fwd = bot.forward_message(CHANNEL_ID, message.chat.id, message.message_id)
                channel_msg_id = fwd.message_id
            except Exception as e:
                print(f"[Bot] Forward error: {e}")

        # file_path (≤20MB ফাইলে পাওয়া যাবে)
        file_path = None
        try:
            info = bot.get_file(file_id)
            file_path = info.file_path
        except Exception:
            pass

        cur = db_query(
            "INSERT INTO videos (title, file_id, file_path, channel_msg_id, file_size) VALUES (?,?,?,?,?)",
            (title, file_id, file_path, channel_msg_id, file_size)
        )
        video_db_id = cur.lastrowid

        size_mb = round(file_size / 1024 / 1024, 2) if file_size else "?"
        channel_status = "✅ Channel-এ সেভ হয়েছে" if channel_msg_id else "⚠️ Channel-এ সেভ হয়নি"

        domain     = os.environ.get("REPLIT_DEV_DOMAIN", "")
        stream_url = f"https://{domain}/stream/{video_db_id}" if domain else ""

        reply_text = (
            f"✅ ভিডিও সেভ হয়েছে!\n"
            f"শিরোনাম: {title}\n"
            f"সাইজ: {size_mb} MB\n"
            f"{channel_status}"
        )
        if stream_url:
            reply_text += f"\n🔗 Stream: {stream_url}"

        try:
            bot.reply_to(message, reply_text)
        except Exception as e:
            print(f"[Bot] Reply error: {e}")

    # ── Polling loop ──────────────────────────────────────────────
    while True:
        try:
            bot.polling(none_stop=True, skip_pending=True, timeout=30, long_polling_timeout=20)
        except Exception as e:
            print(f"[Bot] Polling crashed, restarting in 5s: {e}")
            time.sleep(5)

# ==============================
# 4. Flask App
# ==============================
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    with db_lock:
        cur = conn.cursor()
        cur.execute("SELECT * FROM videos ORDER BY id DESC")
        videos = cur.fetchall()
    return render_template("index.html", videos=videos)


@flask_app.route("/stream/<int:video_id>")
def stream_video(video_id):
    with db_lock:
        cur = conn.cursor()
        cur.execute("SELECT file_id, file_path, file_size FROM videos WHERE id = ?", (video_id,))
        row = cur.fetchone()
    if not row:
        abort(404)

    file_id, file_path, file_size = row
    file_size = file_size or 0

    range_header = flask_request.headers.get("Range")
    start = 0
    end   = file_size - 1 if file_size > 0 else 0
    if range_header:
        m = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if m:
            start = int(m.group(1))
            if m.group(2):
                end = int(m.group(2))

    if not file_path or not BOT_TOKEN:
        abort(503)

    tg_url      = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    req_headers = {"Range": range_header} if range_header else {}

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
# 5. Start
# ==============================
threading.Thread(target=start_bot, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port, threaded=True)
