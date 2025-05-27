# main.py
import asyncio
from app import flask_app_instance, init_telegram_app_async # Import the Flask app and the async init function
from asgiref.wsgi import WsgiToAsgi

# This is the ASGI application that Uvicorn will run.
# It wraps your Flask app.
asgi_app = WsgiToAsgi(flask_app_instance)

# Define an async startup function that Uvicorn will automatically call.
async def startup_event():
    print("Uvicorn startup event triggered. Initializing Telegram Application...")
    try:
        await init_telegram_app_async()
        print("Telegram Application initialized successfully during startup.")
    except Exception as e:
        print(f"ERROR: Telegram Application failed to initialize during startup: {e}")
        # Re-raise the exception to indicate a critical startup failure
        raise

# You can also define a shutdown event if specific cleanup is needed
async def shutdown_event():
    print("Uvicorn shutdown event triggered. Performing cleanup if necessary.")
    # For python-telegram-bot Application, explicit stop() is usually not required
    # when the process exits, but can be added if needed for graceful shutdown.
    # global telegram_app
    # if telegram_app and telegram_app._initialized:
    #     await telegram_app.stop()

# Assign startup/shutdown events to the ASGI application
# Uvicorn will call these hooks automatically.
asgi_app.add_event_handler("startup", startup_event)
asgi_app.add_event_handler("shutdown", shutdown_event)

# This is the ASGI callable that Uvicorn will look for.
application = asgi_app
