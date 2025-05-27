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

def init_telegram_app():
    """Initialize the Telegram Application synchronously."""
    global telegram_app
    if telegram_app is None:
        try:
            # Create a new event loop for initialization
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            # Run the async main function
            telegram_app = loop.run_until_complete(setup_application())
            loop.run_until_complete(telegram_app.initialize())
            loop.run_until_complete(telegram_app.start())
            
            # Set webhook
            webhook_url = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/webhook"
            loop.run_until_complete(telegram_app.bot.set_webhook(url=webhook_url, allowed_updates=["message", "callback_query"]))
            logger.info(f"Webhook set to {webhook_url}")
            
            # Close the loop
            loop.close()
        except Exception as e:
            logger.error(f"Failed to initialize Telegram Application: {e}")
            raise

# Initialize the Telegram app when the module is imported
init_telegram_app()

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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
