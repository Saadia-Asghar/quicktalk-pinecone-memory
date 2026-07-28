"""Flask API for organizational customer memory and human-agent handoff cards."""

from __future__ import annotations

import os

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import BadRequest

from pinecone_memory import MemoryStore, build_handoff_bullets


def create_app(store: MemoryStore | None = None) -> Flask:
    app = Flask(__name__, static_folder="static")
    memory_store = store or MemoryStore()

    @app.errorhandler(ValueError)
    @app.errorhandler(BadRequest)
    def invalid_request(error):
        return jsonify({"error": str(error)}), 400

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "inbox.html")

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok", "memory_backend": memory_store.backend})

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
        return jsonify(record), 201

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
        memories = memory_store.recent(organization_id=organization_id, mobile_no=mobile_no)
        return jsonify({
            "organization_id": organization_id,
            "mobile_no": mobile_no,
            "history_summary": build_handoff_bullets(memories),
            "memory_count": len(memories),
        })

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8765")), debug=os.getenv("FLASK_DEBUG") == "1")
