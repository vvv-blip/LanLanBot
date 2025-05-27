# app.py
import os
import asyncio
from flask import Flask, request, jsonify
from bot import setup_application, TELEGRAM_TOKEN, logger
from telegram import Update

# Initialize Flask app
app = Flask(__name__)

# Global variable to hold the Telegram Application instance
telegram_app = None

@app.route("/webhook", methods=["POST"])
async def telegram_webhook():
    global telegram_app
    if telegram_app is None:
        logger.error("Telegram Application not initialized for webhook. Rejecting update.")
        return jsonify({"status": "error", "message": "Bot not ready"}), 503

    try:
        update = Update.de_json(request.json, telegram_app.bot)
        await telegram_app.process_update(update)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Error processing Telegram webhook update: {e}")
        return jsonify({"status": "error", "message": "Processing failed"}), 500

@app.route("/")
def home():
    return "LanLan Bot is running!"

@app.route("/health")
def health_check():
    return jsonify({"status": "healthy", "message": "Bot operational"})

async def main():
    global telegram_app
    try:
        # Set up the Telegram application
        telegram_app = await setup_application()
        await telegram_app.initialize()
        await telegram_app.start()

        # Set webhook
        webhook_url = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/webhook"
        await telegram_app.bot.set_webhook(url=webhook_url, allowed_updates=["message", "callback_query"])
        logger.info(f"Webhook set to {webhook_url}")

    except Exception as e:
        logger.error(f"Failed to initialize bot or set webhook: {e}")
        raise

# Run the async main function before starting the Flask app
if __name__ == "__main__":
    asyncio.run(main())
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
