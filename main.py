import sqlite3
import threading
import os
import requests
from flask import Flask, render_template, Response, abort

# ==============================
# 1. Database Setup (SQLite)
# ==============================
conn = sqlite3.connect("videos.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    file_id TEXT,
    file_path TEXT
)
""")

try:
    cursor.execute("ALTER TABLE videos ADD COLUMN file_path TEXT")
except Exception:
    pass

conn.commit()

# ==============================
# 2. Flask Website Setup
# ==============================
app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")

@app.route("/")
def home():
    cursor.execute("SELECT * FROM videos ORDER BY id DESC")
    videos = cursor.fetchall()
    return render_template("index.html", videos=videos)

@app.route("/stream/<int:video_id>")
def stream_video(video_id):
    cursor.execute("SELECT file_path FROM videos WHERE id = ?", (video_id,))
    row = cursor.fetchone()
    if not row or not row[0]:
        abort(404)

    file_path = row[0]
    tg_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

    range_header = requests.utils.default_headers()
    req_range = None

    from flask import request as flask_request
    if "Range" in flask_request.headers:
        req_range = flask_request.headers["Range"]

    headers = {}
    if req_range:
        headers["Range"] = req_range

    tg_resp = requests.get(tg_url, headers=headers, stream=True)

    def generate():
        for chunk in tg_resp.iter_content(chunk_size=8192):
            if chunk:
                yield chunk

    response_headers = {
        "Content-Type": "video/mp4",
        "Content-Disposition": "inline",
        "Accept-Ranges": "bytes",
    }

    if "Content-Length" in tg_resp.headers:
        response_headers["Content-Length"] = tg_resp.headers["Content-Length"]
    if "Content-Range" in tg_resp.headers:
        response_headers["Content-Range"] = tg_resp.headers["Content-Range"]

    status_code = tg_resp.status_code

    return Response(generate(), status=status_code, headers=response_headers)

# ==============================
# 3. Bot Setup (optional - requires BOT_TOKEN)
# ==============================
def run_bot():
    if not BOT_TOKEN:
        print("BOT_TOKEN not set - Telegram bot will not start")
        return
    try:
        import telebot
        bot = telebot.TeleBot(BOT_TOKEN)

        @bot.message_handler(content_types=['video'])
        def handle_video(message):
            file_id = message.video.file_id
            title = message.caption if message.caption else "Untitled Video"

            try:
                file_info = bot.get_file(file_id)
                file_path = file_info.file_path
            except Exception:
                file_path = None

            cursor.execute(
                "INSERT INTO videos (title, file_id, file_path) VALUES (?, ?, ?)",
                (title, file_id, file_path)
            )
            conn.commit()

            bot.reply_to(message, f"✅ ভিডিও সেভ হয়েছে!\nশিরোনাম: {title}")

        bot.polling(none_stop=True)
    except Exception as e:
        print(f"Bot error: {e}")

# ==============================
# 4. Run Bot + Flask Together
# ==============================
threading.Thread(target=run_bot, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
