# main.py
import asyncio
import sys
from app import flask_app_instance, init_telegram_app_async
from asgiref.wsgi import WsgiToAsgi

class LifespanASGIApp:
    """
    A custom ASGI callable that wraps a WSGI-to-ASGI app and
    implements the ASGI Lifespan Protocol for startup/shutdown events.
    """
    def __init__(self, wsgi_app):
        # Wrap the Flask WSGI app with asgiref's WsgiToAsgi adapter
        self.wsgi_app = WsgiToAsgi(wsgi_app)
        self.startup_completed = False

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            # Handle ASGI Lifespan events (startup and shutdown)
            while True:
                message = await receive()
                if message['type'] == 'lifespan.startup':
                    if not self.startup_completed:
                        print("Custom ASGI Lifespan: Startup event triggered. Initializing Telegram Application...")
                        try:
                            await init_telegram_app_async()
                            print("Custom ASGI Lifespan: Telegram Application initialized successfully.")
                            self.startup_completed = True
                            await send({"type": "lifespan.startup.complete"})
                        except Exception as e:
                            print(f"ERROR: Custom ASGI Lifespan: Telegram Application failed to initialize: {e}")
                            await send({"type": "lifespan.startup.failed", "message": str(e)})
                            sys.exit(1) # Crucial: exit if startup fails to prevent bad deployments
                    else:
                        # Should not happen if Uvicorn respects protocol, but for robustness:
                        await send({"type": "lifespan.startup.complete"})

                elif message['type'] == 'lifespan.shutdown':
                    print("Custom ASGI Lifespan: Shutdown event triggered. Performing cleanup if necessary.")
                    # Add specific shutdown logic for your telegram_app here if needed
                    # e.g., if you have long-running tasks that need to be explicitly cancelled
                    # global telegram_app # if telegram_app is declared global in app.py
                    # if telegram_app and telegram_app._initialized:
                    #     await telegram_app.stop() # Example
                    await send({"type": "lifespan.shutdown.complete"})
                    return # Exit the lifespan loop

        else:
            # For HTTP or WebSocket requests, delegate to the wrapped WSGI app
            # This fallback init ensures startup even if lifespan somehow isn't fully used
            if not self.startup_completed:
                print("Custom ASGI App: Initializing Telegram Application during first request (lifespan protocol might not have fully run).")
                try:
                    await init_telegram_app_async()
                    self.startup_completed = True
                except Exception as e:
                    print(f"ERROR: Telegram Application failed to initialize during request: {e}")
                    # If init fails here, you might return an error response
                    raise

            await self.wsgi_app(scope, receive, send)

# Create an instance of our custom ASGI application
application = LifespanASGIApp(flask_app_instance)
