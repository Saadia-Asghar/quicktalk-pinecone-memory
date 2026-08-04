"""Flask API for organizational customer memory and human-agent handoff cards."""

from __future__ import annotations

import os
import hmac
import threading

from flask import Flask, jsonify, request, send_from_directory
from dotenv import load_dotenv
from werkzeug.exceptions import BadRequest, NotFound

from analytics import AnalyticsRepository
from mem0_memory import create_memory_store
from pinecone_memory import MemoryStore, build_handoff_bullets
from tool_calling import TOOL_DEFINITIONS, ToolRegistry

load_dotenv()


def create_app(store: MemoryStore | None = None, analytics_repository=None) -> Flask:
    app = Flask(__name__, static_folder="static")
    memory_store = store or create_memory_store()
    analytics = analytics_repository or AnalyticsRepository()
    tools = ToolRegistry(memory_store, build_handoff_bullets, analytics)

    @app.before_request
    def authenticate():
        expected = os.getenv("SERVICE_API_KEY")
        if not expected or request.endpoint in {
            "index", "health", "static", "custom_inbox", "organization_dashboard", "list_demo_users"
        }:
            return None
        supplied = request.headers.get("X-API-Key", "")
        if not hmac.compare_digest(supplied, expected):
            return jsonify({"error": "unauthorized"}), 401

    @app.errorhandler(ValueError)
    @app.errorhandler(BadRequest)
    @app.errorhandler(NotFound)
    def invalid_request(error):
        return jsonify({"error": str(error)}), getattr(error, "code", 400)

    @app.errorhandler(RuntimeError)
    def backend_error(error):
        app.logger.exception("Memory backend failure")
        return jsonify({"error": "memory_backend_unavailable", "detail": str(error)}), 503

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "inbox.html")

    @app.get("/api/health")
    def health():
        return jsonify({
            "status": "ok", "memory_backend": memory_store.backend,
            "profile_cache": analytics.cache.backend, "tools": len(TOOL_DEFINITIONS),
        })

    @app.get("/api/tools")
    def list_tools():
        return jsonify({"tools": TOOL_DEFINITIONS})

    @app.post("/api/tools/<tool_name>/invoke")
    def invoke_tool(tool_name: str):
        body = request.get_json(force=True)
        arguments = body.get("arguments", body)
        try:
            result = tools.invoke(tool_name, arguments)
        except KeyError as exc:
            raise NotFound(f"Unknown tool: {tool_name}") from exc
        return jsonify({"tool": tool_name, "result": result})

    @app.post("/api/memories")
    def add_memory():
        body = request.get_json(force=True)
        required = ("organization_id", "session_id", "mobile_no", "text")
        missing = [key for key in required if not body.get(key)]
        if missing:
            raise BadRequest(f"Missing required fields: {', '.join(missing)}")
        record = memory_store.add(
            organization_id=body["organization_id"], session_id=body["session_id"],
            mobile_no=body["mobile_no"], text=body["text"],
            role=body.get("role", "customer"), timestamp=body.get("timestamp"),
        )
        analytics.record_memory(record)
        return jsonify(record), 201

    @app.get("/api/demo-users")
    def list_demo_users():
        with analytics._connect() as db:
            rows = db.execute(
                """SELECT p.organization_scope, p.mobile_no, p.current_issue, p.previous_session_count, o.organization_name
                FROM customer_profiles p
                JOIN organizations o ON p.organization_scope = o.organization_scope
                ORDER BY p.memory_count DESC LIMIT 3000"""
            ).fetchall()
        return jsonify({"users": [dict(row) for row in rows]})

    @app.get("/api/analytics/organizations")
    def analytics_organizations():
        return jsonify({"organizations": analytics.list_organizations()})

    @app.get("/api/analytics/dashboard")
    def analytics_dashboard():
        organization_id = request.args.get("organization_id", "")
        if not organization_id:
            raise BadRequest("organization_id is required")
        try:
            days = int(request.args.get("days", "30"))
        except ValueError as exc:
            raise BadRequest("days must be an integer") from exc
        return jsonify(analytics.dashboard(organization_id, days))

    @app.get("/api/profiles/<path:mobile_no>")
    def customer_profile(mobile_no: str):
        organization_id = request.args.get("organization_id", "")
        if not organization_id:
            raise BadRequest("organization_id is required")
        try:
            limit = min(max(int(request.args.get("session_limit", "5")), 1), 50)
            offset = max(int(request.args.get("session_offset", "0")), 0)
        except ValueError as exc:
            raise BadRequest("session_limit and session_offset must be integers") from exc
        return jsonify(analytics.get_profile(
            organization_id, mobile_no, session_limit=limit, session_offset=offset
        ))

    @app.get("/api/memories")
    def find_memories():
        organization_id = request.args.get("organization_id", "")
        mobile_no = request.args.get("mobile_no", "")
        if not organization_id or not mobile_no:
            raise BadRequest("organization_id and mobile_no are required")
        try:
            limit = min(max(int(request.args.get("limit", 10)), 1), 50)
        except ValueError as exc:
            raise BadRequest("limit must be an integer") from exc
        items = memory_store.search(
            organization_id=organization_id, mobile_no=mobile_no,
            session_id=request.args.get("session_id"), query=request.args.get("q", "conversation history"),
            limit=limit,
        )
        return jsonify({"items": items, "count": len(items)})

    @app.get("/api/inbox/context-card")
    def context_card():
        organization_id = request.args.get("organization_id", "")
        mobile_no = request.args.get("mobile_no", "")
        if not organization_id or not mobile_no:
            raise BadRequest("organization_id and mobile_no are required")
        return jsonify(tools.invoke("get_handoff_context", {
            "organization_id": organization_id, "mobile_no": mobile_no,
        }))

    if analytics_repository is None and os.getenv("WARMUP_ON_STARTUP", "false").lower() == "true":
        threading.Thread(target=analytics.warm, name="profile-cache-warmup", daemon=True).start()

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8765")), debug=os.getenv("FLASK_DEBUG") == "1")
