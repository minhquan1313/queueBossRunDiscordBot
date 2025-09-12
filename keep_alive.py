from threading import Thread
import os

try:
    from flask import Flask
except Exception as e:
    # Defer import error until used; Replit will install Flask from requirements
    Flask = None  # type: ignore


def _create_app():
    if Flask is None:
        raise RuntimeError("Flask is not installed. Please add Flask to requirements.txt")
    app = Flask(__name__)

    @app.get("/")
    def root():
        return "OK", 200

    @app.get("/healthz")
    def health():
        return {"status": "ok"}, 200

    return app


def _run():
    app = _create_app()
    port = int(os.getenv("PORT", "8080"))
    # host=0.0.0.0 so Replit exposes it
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    """Start a background web server for uptime pings (Replit free)."""
    t = Thread(target=_run, daemon=True)
    t.start()

