# app.py
import os
import asyncio
import logging
from flask import Flask, request, jsonify
from bot import setup_application, TELEGRAM_TOKEN, logger # Assuming logger is correctly imported from bot.py
from telegram import Update

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
app_logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)

# Global variables
telegram_app = None

# Initialize event loop
# Use get_event_loop() if it's already running, otherwise create a new one.
# This pattern can sometimes be tricky with Gunicorn.
# For simplicity, let's try to get or create.
try:
    loop = asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)


async def init_telegram_app():
    """Initialize the Telegram Application asynchronously."""
    global telegram_app
    if telegram_app is None:
        try:
            app_logger.info("Attempting to set up Telegram Application...")
            telegram_app = await setup_application()
            app_logger.info("Telegram Application setup_application called.")

            # *** THIS IS THE CRUCIAL CHANGE: .initialized -> ._initialized ***
            if not telegram_app._initialized:
                app_logger.info("Telegram Application not yet initialized, performing explicit initialize and start.")
                await telegram_app.initialize()  # Explicitly initialize the Application
                await telegram_app.start()       # Start the Application (important for webhook to work)
                
            webhook_url = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/webhook"
            app_logger.info(f"Attempting to set webhook to: {webhook_url}")
            await telegram_app.bot.setWebhook(url=webhook_url, allowed_updates=["message", "callback_query"])
            app_logger.info(f"Webhook successfully set to {webhook_url}")
            
        except Exception as e:
            app_logger.error(f"Failed to initialize Telegram Application: {e}", exc_info=True)
            raise

# Initialize Telegram app at module import time
try:
    app_logger.info("Running init_telegram_app at module import time.")
    loop.run_until_complete(init_telegram_app())
    app_logger.info("Telegram Application initialization completed.")
except Exception as e:
    app_logger.error(f"Initialization failed at module level: {e}", exc_info=True)
    raise

@app.route("/webhook", methods=["POST"])
async def telegram_webhook():
    global telegram_app
    if telegram_app is None:
        app_logger.error("Telegram Application not initialized for webhook. Rejecting update.")
        return jsonify({"status": "error", "message": "Bot not ready"}), 503

    try:
        # Flask is generally synchronous. Running async code directly in a sync Flask route
        # works because `run_until_complete` is called at the module level for init_telegram_app
        # and the Application's process_update itself can be awaited.
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

# Standard Flask development server entry point (not used by Gunicorn)
if __name__ == "__main__":
    app_logger.info("Running Flask app in development mode.")
    try:
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
    finally:
        # Clean up Telegram Application gracefully on shutdown if running locally
        if telegram_app and telegram_app._initialized: # Check _initialized before trying to stop
            app_logger.info("Stopping Telegram Application.")
            loop.run_until_complete(telegram_app.stop())
        if not loop.is_closed():
            app_logger.info("Closing asyncio event loop.")
            loop.close()
