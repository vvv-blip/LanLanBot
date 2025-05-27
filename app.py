# app.py
import os
import asyncio
import logging
from flask import Flask, request, jsonify
from asgiref.wsgi import WsgiToAsgi
from bot import setup_application, TELEGRAM_TOKEN, logger
from telegram import Update

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
app_logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)

# Global variable for telegram_app, initialized to None
telegram_app = None

# We no longer explicitly manage the asyncio loop here. Uvicorn will do it.
# async def init_telegram_app_async() can remain, but its direct call will move.
async def init_telegram_app_async():
    """Initializes the Telegram Application asynchronously."""
    global telegram_app
    if telegram_app is None: # Ensure it's only initialized once
        try:
            app_logger.info("Attempting to set up Telegram Application...")
            telegram_app = await setup_application()
            app_logger.info("Telegram Application setup_application called.")

            if not telegram_app._initialized:
                app_logger.info("Telegram Application not yet initialized, performing explicit initialize and start.")
                await telegram_app.initialize()
                await telegram_app.start()

            webhook_url = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/webhook"
            app_logger.info(f"Attempting to set webhook to: {webhook_url}")
            await telegram_app.bot.setWebhook(url=webhook_url, allowed_updates=["message", "callback_query"])
            app_logger.info(f"Webhook successfully set to {webhook_url}")

        except Exception as e:
            app_logger.error(f"Failed to initialize Telegram Application: {e}", exc_info=True)
            raise
    else:
        app_logger.info("Telegram Application already initialized.")

# Remove this entire block, as Uvicorn will manage the startup:
# try:
#     app_logger.info("Running init_telegram_app at module import time.")
#     loop.run_until_complete(init_telegram_app())
#     app_logger.info("Telegram Application initialization completed.")
# except Exception as e:
#     app_logger.error(f"Initialization failed at module level: {e}", exc_info=True)
#     raise

@app.route("/webhook", methods=["POST"])
async def telegram_webhook():
    global telegram_app
    # Add a check/lazy init here as a fallback, though primary init is in main.py
    if telegram_app is None:
        app_logger.warning("Telegram Application not initialized for webhook. Attempting to initialize now.")
        try:
            await init_telegram_app_async()
        except Exception as e:
            app_logger.error(f"Failed to lazy-init Telegram Application: {e}", exc_info=True)
            return jsonify({"status": "error", "message": "Bot not ready: Lazy init failed"}), 503

    try:
        update = Update.de_json(request.get_json(), telegram_app.bot)
        await telegram_app.process_update(update)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        app_logger.error(f"Error processing Telegram webhook update: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/")
def health():
    return "LanLan Bot is running!"

@app.route("/health")
def health_check():
    return jsonify({"status": "healthy"})

# Define the Flask app instance for WsgiToAsgi to wrap
flask_app_instance = app

# The `if __name__ == "__main__":` block is for local development only and
# will not be executed by Uvicorn in production.
if __name__ == "__main__":
    app_logger.info("Running Flask app in development mode using Flask's built-in server.")
    # For local dev, you might run init_telegram_app_async here if not using uvicorn locally
    # asyncio.run(init_telegram_app_async()) # Only if you want to run this init before Flask's dev server
    flask_app_instance.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
