import sqlite3
import threading
import asyncio
import queue
import os
import re
from flask import Flask, render_template, Response, abort, request as flask_request

# ==============================
# 1. Config
# ==============================
BOT_TOKEN  = os.environ.get("BOT_TOKEN")
API_ID     = os.environ.get("API_ID")
API_HASH   = os.environ.get("API_HASH")
CHANNEL_ID = os.environ.get("CHANNEL_ID")  # e.g. @mychannel or -100123456789

# ==============================
# 2. Database Setup (SQLite)
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
# 3. Pyrogram Setup
# ==============================
pyrogram_client = None
pyrogram_loop   = None

def start_pyrogram():
    global pyrogram_client, pyrogram_loop

    if not all([API_ID, API_HASH, BOT_TOKEN]):
        print("[Pyrogram] API_ID, API_HASH বা BOT_TOKEN সেট নেই — বট চালু হবে না।")
        return

    from pyrogram import Client, filters
    from pyrogram.errors import FloodWait
    import time

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    pyrogram_loop = loop

    client = Client(
        "bot_session",
        api_id=int(API_ID),
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        sleep_threshold=60,
    )

    @client.on_message(filters.video | filters.document)
    async def handle_video(c, message):
        media = message.video or message.document
        if not media:
            return

        title = message.caption if message.caption else "Untitled Video"

        # Channel-এ forward করে message ID সেভ করি
        if CHANNEL_ID:
            try:
                forwarded = await c.forward_messages(CHANNEL_ID, message.chat.id, message.id)
                channel_msg_id = forwarded.id
            except Exception as e:
                print(f"[Bot] Forward error: {e}")
                channel_msg_id = None
        else:
            channel_msg_id = None

        file_size = getattr(media, "file_size", 0)

        cursor.execute(
            "INSERT INTO videos (title, file_id, channel_msg_id, file_size) VALUES (?, ?, ?, ?)",
            (title, media.file_id, channel_msg_id, file_size)
        )
        conn.commit()

        size_mb = round(file_size / 1024 / 1024, 2) if file_size else "?"
        await message.reply(
            f"✅ ভিডিও সেভ হয়েছে!\n"
            f"শিরোনাম: {title}\n"
            f"সাইজ: {size_mb} MB\n"
            f"{'✅ Channel-এ সেভ হয়েছে' if channel_msg_id else '⚠️ Channel সেভ হয়নি (CHANNEL_ID সেট করুন)'}"
        )

    async def run():
        await client.start()
        pyrogram_client = client
        globals()["pyrogram_client"] = client
        print("[Pyrogram] বট চালু হয়েছে।")
        await asyncio.Future()  # run forever

    loop.run_until_complete(run())


# ==============================
# 4. Flask App
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
        "SELECT channel_msg_id, file_path, file_size FROM videos WHERE id = ?",
        (video_id,)
    )
    row = cursor.fetchone()
    if not row:
        abort(404)

    channel_msg_id, file_path, file_size = row
    file_size = file_size or 0

    # Range header parse
    range_header = flask_request.headers.get("Range", None)
    start, end = 0, file_size - 1 if file_size > 0 else 0

    if range_header:
        m = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if m:
            start = int(m.group(1))
            if m.group(2):
                end = int(m.group(2))

    length = (end - start + 1) if file_size > 0 else None

    # ── Pyrogram streaming (channel, large files) ──────────────────
    if channel_msg_id and pyrogram_client and pyrogram_loop and CHANNEL_ID:
        chunk_q = queue.Queue(maxsize=30)

        async def _download():
            try:
                ch = int(CHANNEL_ID) if str(CHANNEL_ID).lstrip("-").isdigit() else CHANNEL_ID
                msg = await pyrogram_client.get_messages(ch, channel_msg_id)
                media = msg.video or msg.document
                if not media:
                    chunk_q.put(None)
                    return
                async for chunk in pyrogram_client.iter_download(media, offset=start):
                    chunk_q.put(chunk)
            except Exception as e:
                print(f"[Stream] Pyrogram error: {e}")
            finally:
                chunk_q.put(None)

        asyncio.run_coroutine_threadsafe(_download(), pyrogram_loop)

        def generate_pyrogram():
            while True:
                chunk = chunk_q.get()
                if chunk is None:
                    break
                yield chunk

        headers = {
            "Content-Type":        "video/mp4",
            "Content-Disposition": "inline",
            "Accept-Ranges":       "bytes",
        }
        if file_size > 0:
            headers["Content-Length"] = str(length)
            headers["Content-Range"]  = f"bytes {start}-{end}/{file_size}"

        status = 206 if range_header else 200
        return Response(generate_pyrogram(), status=status, headers=headers)

    # ── Fallback: Telegram Bot API proxy (files ≤ 20 MB) ───────────
    if file_path and BOT_TOKEN:
        import requests as req
        tg_url  = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        headers = {"Range": range_header} if range_header else {}
        tg_resp = req.get(tg_url, headers=headers, stream=True)

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

    abort(404)


# ==============================
# 5. Start Everything
# ==============================
threading.Thread(target=start_pyrogram, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port)
