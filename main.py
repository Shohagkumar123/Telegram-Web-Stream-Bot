import sqlite3
import threading
import asyncio
import os
import re
import requests as req
from flask import Flask, render_template, Response, abort, request as flask_request
from pyrogram import Client, filters as pyro_filters

# ==============================
# 1. Config
# ==============================
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "")
API_ID     = os.environ.get("API_ID", "")
API_HASH   = os.environ.get("API_HASH", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")

# ==============================
# 2. Database Setup
# ==============================
conn    = sqlite3.connect("videos.db", check_same_thread=False)
db_lock = threading.Lock()

def db_execute(sql, params=()):
    with db_lock:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        return cur

with db_lock:
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS videos (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        title           TEXT,
        file_id         TEXT,
        file_path       TEXT,
        channel_msg_id  INTEGER,
        file_size       INTEGER
    )
    """)
    for col in ["file_path TEXT", "channel_msg_id INTEGER", "file_size INTEGER"]:
        try:
            cur.execute(f"ALTER TABLE videos ADD COLUMN {col}")
        except Exception:
            pass
    conn.commit()

# ==============================
# 3. Pyrogram Client (শুধু বট চালানোর জন্য)
# ==============================
pyrogram_loop   = None
pyrogram_client = None

def start_pyrogram():
    global pyrogram_loop, pyrogram_client

    if not all([API_ID, API_HASH, BOT_TOKEN]):
        print("[Pyrogram] credentials নেই — চালু হবে না।")
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    pyrogram_loop = loop

    client = Client(
        "stream_session",
        api_id=int(API_ID),
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        sleep_threshold=60,
    )

    async def run():
        await client.start()
        pyrogram_client = client
        globals()["pyrogram_client"] = client
        print("[Pyrogram] Client চালু।")
        await asyncio.Future()

    loop.run_until_complete(run())

# ==============================
# 4. Telegram Bot (telebot)
# ==============================
def start_bot():
    if not BOT_TOKEN:
        print("[Bot] BOT_TOKEN নেই।")
        return

    import telebot
    bot = telebot.TeleBot(BOT_TOKEN, threaded=False)

    @bot.message_handler(content_types=['video', 'document'])
    def handle_video(message):
        # Channel post থেকে আসা update ignore
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

        # Bot API getFile (≤20MB-এ কাজ করে)
        file_path = None
        try:
            info = bot.get_file(file_id)
            file_path = info.file_path
        except Exception:
            pass

        cur = db_execute(
            "INSERT INTO videos (title, file_id, file_path, channel_msg_id, file_size) VALUES (?,?,?,?,?)",
            (title, file_id, file_path, channel_msg_id, file_size)
        )
        video_db_id = cur.lastrowid

        size_mb = round(file_size / 1024 / 1024, 2) if file_size else "?"
        channel_status = "✅ Channel-এ সেভ হয়েছে" if channel_msg_id else "⚠️ Channel-এ সেভ হয়নি"

        domain = os.environ.get("REPLIT_DEV_DOMAIN", "")
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

    # পুরনো pending update সব clear করো
    try:
        bot.get_updates(offset=-1)
        print("[Bot] Pending updates cleared.")
    except Exception as e:
        print(f"[Bot] Clear error: {e}")

    while True:
        try:
            bot.polling(none_stop=True, skip_pending=True, timeout=30, long_polling_timeout=20)
        except Exception as e:
            print(f"[Bot] Polling crashed, restarting: {e}")
            import time
            time.sleep(5)

# ==============================
# 5. Flask App
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

    # ── Bot API proxy (সবসময় চেষ্টা করো) ──────────────────────────
    if file_path and BOT_TOKEN:
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
            print(f"[Stream] Bot API error: {e}")

    # ── Pyrogram fallback (>20MB ফাইলের জন্য) ─────────────────────
    if pyrogram_client and pyrogram_loop and file_id:
        import queue as q_module
        chunk_q     = q_module.Queue(maxsize=50)
        done_event  = threading.Event()

        async def _dl():
            try:
                buf = await pyrogram_client.download_media(file_id, in_memory=True)
                if buf:
                    buf.seek(start)
                    while True:
                        data = buf.read(65536)
                        if not data:
                            break
                        chunk_q.put(data)
            except Exception as e:
                print(f"[Stream] Pyrogram fallback error: {e}")
            finally:
                chunk_q.put(None)

        asyncio.run_coroutine_threadsafe(_dl(), pyrogram_loop)

        def generate_py():
            while True:
                chunk = chunk_q.get(timeout=60)
                if chunk is None:
                    break
                yield chunk

        resp_headers = {
            "Content-Type":        "video/mp4",
            "Content-Disposition": "inline",
            "Accept-Ranges":       "bytes",
        }
        if file_size > 0:
            resp_headers["Content-Length"] = str(file_size - start)
            resp_headers["Content-Range"]  = f"bytes {start}-{end}/{file_size}"

        return Response(generate_py(), status=206 if range_header else 200, headers=resp_headers)

    abort(503)


# ==============================
# 6. Start Everything
# ==============================
threading.Thread(target=start_pyrogram, daemon=True).start()
threading.Thread(target=start_bot,      daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port, threaded=True)
