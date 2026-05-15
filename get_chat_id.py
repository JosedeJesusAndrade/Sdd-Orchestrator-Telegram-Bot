"""
Run this script, then send /start to your bot on Telegram.
Your chat ID will be printed here.
"""

import os
import asyncio
import nest_asyncio
from pathlib import Path

nest_asyncio.apply()

from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)


async def start(update, context):
    chat_id = update.effective_chat.id
    print(f"\n  Your chat ID is: {chat_id}")
    print(f"  Add this to your .env file: ALLOWED_CHAT_IDS={chat_id}")
    await update.message.reply_text(f"Your chat ID is: {chat_id}")


async def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN not set in .env. Add your bot token first.")
        return

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    print("Bot started. Send /start to your bot on Telegram...")
    print("(This will run until you press Ctrl+C)\n")
    await app.run_polling()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDone.")
