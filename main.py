import sqlite3
import threading
import os
from flask import Flask, render_template

# ==============================
# 1. Database Setup (SQLite)
# ==============================
conn = sqlite3.connect("videos.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    file_id TEXT
)
""")
conn.commit()

# ==============================
# 2. Flask Website Setup
# ==============================
app = Flask(__name__)

@app.route("/")
def home():
    cursor.execute("SELECT * FROM videos ORDER BY id DESC")
    videos = cursor.fetchall()
    return render_template("index.html", videos=videos, token=BOT_TOKEN)

# ==============================
# 3. Bot Setup (optional - requires BOT_TOKEN)
# ==============================
BOT_TOKEN = os.environ.get("BOT_TOKEN")

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
            cursor.execute("INSERT INTO videos (title, file_id) VALUES (?, ?)", (title, file_id))
            conn.commit()
            bot.reply_to(message, f"Video saved with title: {title}")

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
