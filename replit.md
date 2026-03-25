# Telegram Video Site

## Overview
A Flask web app combined with a Telegram bot that lets users send videos to a Telegram bot, which stores them in a SQLite database and displays them on a website.

## Architecture
- **Backend**: Flask (Python) serving the web UI
- **Bot**: pyTelegramBotAPI Telegram bot running in a background thread
- **Database**: SQLite (`videos.db`) storing video titles and Telegram file IDs
- **Templates**: Jinja2 templates in `templates/` directory

## Project Structure
```
main.py            # Main entry point - Flask app + Telegram bot
templates/
  index.html       # Jinja2 template displaying videos
requirements.txt   # Python dependencies
videos.db          # SQLite database (auto-created on first run)
```

## Environment Variables
- `BOT_TOKEN` - Telegram bot token (required for bot functionality)
- `PORT` - Port for Flask server (defaults to 5000)

## Running Locally
```bash
python main.py
```

The Flask server runs on port 5000. The Telegram bot runs in a background daemon thread (only if `BOT_TOKEN` is set).

## Dependencies
- `flask` - Web framework
- `pyTelegramBotAPI` - Telegram bot library
- `gunicorn` - Production WSGI server

## Deployment
Deployed as a VM (always-running) to support the persistent Telegram bot thread.
Run command: `python main.py`

## Notes
- The bot will not start if `BOT_TOKEN` is not set; the Flask web UI will still work.
- Videos are embedded using the Telegram file API via `https://api.telegram.org/file/bot<token>/<file_id>`.
