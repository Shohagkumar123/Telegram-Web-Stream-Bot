import sqlite3
import telebot
from flask import Flask, render_template
import threading
import os

# ==============================
# 1. Bot Setup
# ==============================
BOT_TOKEN = os.environ.get("BOT_TOKEN")  # Use environment variable for security
bot = telebot.TeleBot(BOT_TOKEN)

# ==============================
# 2. Database Setup (SQLite)
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
# 3. Telegram Video Handler
# ==============================
@bot.message_handler(content_types=['video'])
def handle_video(message):
    file_id = message.video.file_id
    title = message.caption if message.caption else "Untitled Video"
    
    cursor.execute("INSERT INTO videos (title, file_id) VALUES (?, ?)", (title, file_id))
    conn.commit()
    
    bot.reply_to(message, f"Video saved with title: {title}")

# ==============================
# 4. Flask Website Setup
# ==============================
app = Flask(__name__)

@app.route("/")
def home():
    cursor.execute("SELECT * FROM videos ORDER BY id DESC")
    videos = cursor.fetchall()
    return render_template("index.html", videos=videos, token=BOT_TOKEN)

# ==============================
# 5. Run Bot + Flask Together
# ==============================
def run_bot():
    bot.polling(none_stop=True)

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

# Start both threads
threading.Thread(target=run_bot).start()
run_flask()
