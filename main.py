import sqlite3
import threading
import asyncio
import queue
import os
import re
import requests as req
from flask import Flask, render_template, Response, abort, request as flask_request
from pyrogram import Client, filters as pyro_filters

# ==============================
# 1. Config
# ==============================
BOT_TOKEN  = os.environ.get("BOT_TOKEN")
API_ID     = os.environ.get("API_ID")
API_HASH   = os.environ.get("API_HASH")
CHANNEL_ID = os.environ.get("CHANNEL_ID")  # e.g. -100123456789 or @username

# ==============================
# 2. Database Setup
# ==============================
conn   = sqlite3.connect("videos.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
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
        cursor.execute(f"ALTER TABLE videos ADD COLUMN {col}")
    except Exception:
        pass
conn.commit()

# ==============================
# 3. Pyrogram (শুধু streaming-এর জন্য)
# ==============================
pyrogram_client = None
pyrogram_loop   = None

def start_pyrogram():
    global pyrogram_client, pyrogram_loop

    if not all([API_ID, API_HASH, BOT_TOKEN]):
        print("[Pyrogram] API_ID/API_HASH/BOT_TOKEN নেই — streaming সীমিত থাকবে।")
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
        globals()["pyrogram_client"] = client
        print("[Pyrogram] Streaming client চালু।")
        await asyncio.Future()

    loop.run_until_complete(run())

# ==============================
# 4. Telegram Bot (telebot — বট + channel forward)
# ==============================
def start_bot():
    if not BOT_TOKEN:
        print("[Bot] BOT_TOKEN নেই।")
        return
    try:
        import telebot
        bot = telebot.TeleBot(BOT_TOKEN)

        @bot.message_handler(content_types=['video', 'document'])
        def handle_video(message):
            # Channel post থেকে আসা update ignore করো — না হলে infinite loop হয়
            if message.chat.type == 'channel':
                return

            media   = message.video or message.document
            if not media:
                return

            file_id   = media.file_id
            file_size = getattr(media, "file_size", 0)
            title     = message.caption if message.caption else "Untitled Video"

            # Channel-এ forward করো (Bot API HTTP — peer resolution দরকার নেই)
            channel_msg_id = None
            if CHANNEL_ID:
                try:
                    fwd = bot.forward_message(CHANNEL_ID, message.chat.id, message.message_id)
                    channel_msg_id = fwd.message_id
                except Exception as e:
                    print(f"[Bot] Forward error: {e}")

            # file_path বের করো (Bot API getFile — ≤20MB ভিডিওতে কাজ করে)
            file_path = None
            try:
                info = bot.get_file(file_id)
                file_path = info.file_path
            except Exception:
                pass  # 20MB+ হলে file_path পাওয়া যাবে না, streaming Pyrogram করবে

            cursor.execute(
                "INSERT INTO videos (title, file_id, file_path, channel_msg_id, file_size) VALUES (?,?,?,?,?)",
                (title, file_id, file_path, channel_msg_id, file_size)
            )
            conn.commit()
            video_db_id = cursor.lastrowid

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

            bot.reply_to(message, reply_text)

        bot.polling(none_stop=True, timeout=60)
    except Exception as e:
        print(f"[Bot] Error: {e}")

# ==============================
# 5. Flask App
# ==============================
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    cursor.execute("SELECT * FROM videos ORDER BY id DESC")
    videos = cursor.fetchall()
    return render_template("index.html", videos=videos)


@flask_app.route("/stream/<int:video_id>")
def stream_video(video_id):
    cursor.execute(
        "SELECT file_id, file_path, channel_msg_id, file_size FROM videos WHERE id = ?",
        (video_id,)
    )
    row = cursor.fetchone()
    if not row:
        abort(404)

    file_id, file_path, channel_msg_id, file_size = row
    file_size = file_size or 0

    # Range header parse
    range_header = flask_request.headers.get("Range")
    start = 0
    if range_header:
        m = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if m:
            start = int(m.group(1))

    # ── Pyrogram streaming (file_id দিয়ে — যেকোনো সাইজ) ──────────
    if pyrogram_client and pyrogram_loop and file_id:
        import tempfile, io

        done_event = threading.Event()
        result_holder = [None]

        async def _download():
            try:
                data = await pyrogram_client.download_media(file_id, in_memory=True)
                result_holder[0] = data
            except Exception as e:
                print(f"[Stream] Pyrogram error: {e}")
            finally:
                done_event.set()

        asyncio.run_coroutine_threadsafe(_download(), pyrogram_loop)
        done_event.wait(timeout=120)

        if result_holder[0] is not None:
            buf = result_holder[0]
            buf.seek(start)
            data_bytes = buf.read()
            total = start + len(data_bytes)

            headers = {
                "Content-Type":        "video/mp4",
                "Content-Disposition": "inline",
                "Accept-Ranges":       "bytes",
                "Content-Length":      str(len(data_bytes)),
            }
            if range_header:
                end = (file_size - 1) if file_size > 0 else total - 1
                headers["Content-Range"] = f"bytes {start}-{end}/{file_size if file_size else total}"

            def generate_pyrogram():
                offset = 0
                chunk_size = 8192
                while offset < len(data_bytes):
                    yield data_bytes[offset:offset + chunk_size]
                    offset += chunk_size

            return Response(generate_pyrogram(), status=206 if range_header else 200, headers=headers)

    # ── Fallback: Bot API proxy (≤20MB) ────────────────────────────
    if file_path and BOT_TOKEN:
        tg_url       = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        req_headers  = {"Range": range_header} if range_header else {}
        tg_resp      = req.get(tg_url, headers=req_headers, stream=True)

        def generate_fallback():
            for chunk in tg_resp.iter_content(chunk_size=8192):
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

        return Response(generate_fallback(), status=tg_resp.status_code, headers=resp_headers)

    abort(503)

# ==============================
# 6. Start Everything
# ==============================
threading.Thread(target=start_pyrogram, daemon=True).start()
threading.Thread(target=start_bot,      daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port)
