# --- app.py (NEW FILE) ---
import os
import asyncio
import logging
from flask import Flask, request, abort
from telegram import Update
# from telegram.constants import ParseMode # Not directly used in this snippet

# Import the setup function and necessary global variables from bot.py
from bot import setup_application, TELEGRAM_TOKEN, logger

# Initialize Flask App
app = Flask(__name__)

# Global variable for the PTB Application instance
ptb_app = None

# Set up logging for Flask (optional, but good for debugging on Render)
# (This logging is separate from the bot.py's logger to distinguish Flask logs)
flask_logger = logging.getLogger("flask_app")
flask_logger.setLevel(logging.INFO)
flask_logger.addHandler(logging.StreamHandler())


@app.before_serving
async def startup_event():
    """
    Initializes the PTB Application before the Flask server starts handling requests.
    This runs only once when the Render service starts.
    """
    global ptb_app
    if ptb_app is None: # Ensure it's only initialized once
        flask_logger.info("Starting PTB Application setup for webhooks...")
        ptb_app = await setup_application()

        # Set the webhook URL
        webhook_url = os.getenv("WEBHOOK_URL")
        if not webhook_url:
            flask_logger.error("WEBHOOK_URL environment variable is not set!")
            # Optionally raise an error or exit if webhook URL is critical
            # For Render, this MUST be set in environment variables
            raise ValueError("WEBHOOK_URL is required for webhook deployment.")

        await ptb_app.bot.set_webhook(url=webhook_url)
        flask_logger.info(f"Webhook set to: {webhook_url}")

        # Start PTB's internal operations (JobQueue etc.) in the background
        # This is where run_in_background() is used
        ptb_app.run_in_background()
        flask_logger.info("PTB Application running in background for webhooks.")

@app.route("/", methods=["GET"])
def home():
    """Simple home route for health checks."""
    return "LanLan Bot is running!"

@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
async def webhook():
    """Handle incoming Telegram updates."""
    if request.method == "POST":
        try:
            # Get JSON update from Telegram
            update = Update.de_json(request.get_json(force=True), ptb_app.bot)
            # Process the update with PTB
            await ptb_app.process_update(update)
            return "ok" # Telegram expects 'ok' status
        except Exception as e:
            flask_logger.error(f"Error processing update: {e}")
            abort(500) # Indicate server error
    return "ok" # For other methods (GET, etc.) return ok


# This block is for local development with Flask's built-in server (optional)
# For Render, gunicorn will run the app
if __name__ == "__main__":
    # For local testing, ensure WEBHOOK_URL is set (e.g., via localtunnel)
    # and PORT is set (e.g., 8080)
    if not os.getenv("TELEGRAM_TOKEN"):
        flask_logger.error("TELEGRAM_TOKEN not set locally. Please set it.")
        exit(1)
    if not os.getenv("WEBHOOK_URL"):
        flask_logger.warning("WEBHOOK_URL not set locally. Webhook will not be set.")

    # You might run this locally via: python3 app.py
    # To run this with gunicorn locally: pip install gunicorn; gunicorn app:app --bind 0.0.0.0:8080
    asyncio.run(startup_event()) # Manually run startup before running Flask
    app.run(host='0.0.0.0', port=os.getenv("PORT", 8080))
