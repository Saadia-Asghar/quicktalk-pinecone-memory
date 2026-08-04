"""Local custom contact-center demo built on the Flask memory tool service."""

from __future__ import annotations

import os

from flask import make_response, send_from_directory
from dotenv import load_dotenv

from flask_app import create_app


load_dotenv(override=True)


def create_custom_app(store=None, analytics_repository=None):
    app = create_app(store, analytics_repository)

    @app.get("/custom")
    def custom_inbox():
        response = make_response(send_from_directory(app.static_folder, "custom_inbox.html"))
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return response

    @app.get("/dashboard")
    def organization_dashboard():
        response = make_response(send_from_directory(app.static_folder, "analytics_dashboard.html"))
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return response

    return app


app = create_custom_app()


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=int(os.getenv("PORT", "8765")),
        debug=os.getenv("FLASK_DEBUG") == "1",
    )
