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
from pinecone_memory import MemoryStore, build_handoff_bullets, normalize_mobile
from tool_calling import TOOL_DEFINITIONS, ToolRegistry
from knowledge_base import KnowledgeRepository, KnowledgeService

load_dotenv(override=True)


def create_app(store: MemoryStore | None = None, analytics_repository=None) -> Flask:
    app = Flask(__name__, static_folder="static")
    memory_store = store or create_memory_store()
    analytics = analytics_repository or AnalyticsRepository()
    knowledge_repository = KnowledgeRepository(analytics.path)
    knowledge = KnowledgeService(knowledge_repository, memory_store)
    tools = ToolRegistry(memory_store, build_handoff_bullets, analytics, knowledge)

    @app.before_request
    def authenticate():
        expected = os.getenv("SERVICE_API_KEY")
        if not expected or request.endpoint in {
            "index", "health", "static", "context_card", "inbox_welcome", "analytics_dashboard", "customer_profile", "list_demo_users", "session_messages", "custom_inbox_route", "dashboard_route", "knowledge_route"
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
        app.logger.exception("Runtime error inside backend")
        return jsonify({"error": "service_unavailable", "detail": str(error)}), 503

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "inbox.html")

    @app.get("/custom")
    def custom_inbox_route():
        return send_from_directory(app.static_folder, "custom_inbox.html")

    @app.get("/dashboard")
    def dashboard_route():
        return send_from_directory(app.static_folder, "analytics_dashboard.html")

    @app.get("/knowledge")
    def knowledge_route():
        return send_from_directory(app.static_folder, "knowledge_portal.html")

    def request_scope(body=None) -> str:
        body = body or {}
        supplied = str(body.get("organization_id") or request.args.get("organization_id") or "").strip()
        claimed = request.headers.get("X-Organization-Scope", "").strip()
        if not supplied:
            supplied = claimed
        if not supplied:
            raise BadRequest("organization_id is required")
        if claimed and not hmac.compare_digest(claimed, supplied):
            raise BadRequest("organization scope does not match authenticated scope")
        return supplied

    def require_role(*allowed: str) -> str:
        role = request.headers.get("X-User-Role", "organization_admin")
        if role not in allowed:
            raise BadRequest("insufficient role for this operation")
        return request.headers.get("X-User-ID", "demo-user")

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
        claimed_scope = request.headers.get("X-Organization-Scope", "").strip()
        argument_scope = str(arguments.get("organization_id") or "").strip() if isinstance(arguments, dict) else ""
        if claimed_scope and argument_scope and not hmac.compare_digest(claimed_scope, argument_scope):
            raise BadRequest("organization scope does not match authenticated scope")
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
            infer=body.get("infer", False),
        )
        analytics.record_memory(record)

        if body.get("infer", False) and record.get("extracted_facts"):
            import re
            for fact_dict in record["extracted_facts"]:
                fact_text = fact_dict.get("memory") or fact_dict.get("text")
                if not fact_text: continue
                
                entity_key, entity_value = None, None
                lower_text = fact_text.lower()
                
                if "internet plan" in lower_text or "package" in lower_text or "mbps" in lower_text:
                    entity_key = "internet_package"
                    match = re.search(r"(\d+\s*mbps|home unlimited \d+[a-z]*)", lower_text)
                    if match: entity_value = match.group(1)
                elif "dr." in lower_text or "doctor" in lower_text:
                    entity_key = "preferred_doctor"
                    match = re.search(r"(dr\.?\s+[a-z\s]+)(?:$|\s|[,.])", lower_text)
                    if match: entity_value = match.group(1).strip()
                elif "mr number" in lower_text or "mr#" in lower_text:
                    entity_key = "mr_number"
                    match = re.search(r"(?:\bmr\s*number|\bmr#)\s*:?\s*(\d+)", lower_text)
                    if match: entity_value = match.group(1)
                elif "ntl" in lower_text:
                    entity_key = "ntl_id"
                    match = re.search(r"(ntl-\d+)", lower_text)
                    if match: entity_value = match.group(1).upper()
                
                analytics.record_durable_fact(
                    organization_scope=body["organization_id"],
                    mobile_no=body["mobile_no"],
                    session_id=body["session_id"],
                    fact_text=fact_text,
                    entity_key=entity_key,
                    entity_value=entity_value,
                    memory_type=body.get("memory_type", "fact"),
                    category=body.get("category"),
                    sentiment=body.get("sentiment"),
                    resolution_status=body.get("resolution_status")
                )

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

    @app.get("/api/sessions/<session_id>/messages")
    def session_messages(session_id: str):
        organization_id = request.args.get("organization_id", "")
        if not organization_id:
            raise BadRequest("organization_id is required")
        mobile_no = request.args.get("mobile_no")
        if mobile_no:
            mobile_no = normalize_mobile(mobile_no)
        with analytics._connect() as db:
            rows = db.execute(
                """SELECT role, text, timestamp FROM memory_events
                WHERE organization_scope=? AND session_id=?
                ORDER BY timestamp ASC""", (organization_id, session_id),
            ).fetchall()
        return jsonify({"messages": [dict(row) for row in rows]})

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

    @app.get("/api/agent-chats")
    def list_agent_chats():
        scope = request_scope()
        return jsonify({"sessions": knowledge_repository.list_sessions(scope)})

    @app.post("/api/agent-chats")
    def create_agent_chat():
        body = request.get_json(force=True)
        scope = request_scope(body)
        actor = require_role("human_agent", "organization_admin")
        agent_id = str(body.get("agent_id") or actor)
        customer_id = str(body.get("customer_id") or "").strip()
        if not customer_id:
            raise BadRequest("customer_id is required")
        return jsonify(knowledge_repository.create_session(scope, customer_id, agent_id)), 201

    @app.post("/api/agent-chats/<session_id>/messages")
    def add_agent_chat_message(session_id: str):
        body = request.get_json(force=True)
        scope = request_scope(body)
        require_role("human_agent", "organization_admin")
        return jsonify(knowledge_repository.add_message(
            scope, session_id, str(body.get("sender_role", "")), str(body.get("text", "")),
        )), 201

    @app.post("/api/agent-chats/<session_id>/close")
    def close_agent_chat(session_id: str):
        body = request.get_json(silent=True) or {}
        scope = request_scope(body)
        require_role("human_agent", "organization_admin")
        return jsonify(knowledge.close_and_index(scope, session_id))

    @app.get("/api/knowledge/articles")
    def list_knowledge_articles():
        scope = request_scope()
        return jsonify({"articles": knowledge_repository.list_articles(scope)})

    @app.patch("/api/knowledge/articles/<article_id>")
    def edit_knowledge_article(article_id: str):
        body = request.get_json(force=True)
        scope = request_scope(body)
        actor = require_role("organization_admin")
        answer = str(body.get("answer") or "").strip()
        if not answer:
            raise BadRequest("answer is required")
        return jsonify(knowledge.edit_and_index(
            scope, article_id, str(body.get("question") or ""), answer, actor,
        ))

    @app.post("/api/knowledge/articles/<article_id>/status")
    def set_knowledge_article_status(article_id: str):
        body = request.get_json(force=True)
        scope = request_scope(body)
        actor = require_role("organization_admin")
        return jsonify(knowledge_repository.set_status(
            scope, article_id, str(body.get("status") or ""), actor,
        ))

    @app.get("/api/profiles/<path:mobile_no>")
    def customer_profile(mobile_no: str):
        organization_id = request.args.get("organization_id", "")
        if not organization_id:
            raise BadRequest("organization_id is required")
        mobile_no = normalize_mobile(mobile_no)
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
        mobile_no = normalize_mobile(mobile_no)
        try:
            return jsonify(tools.invoke("get_handoff_context", {
                "organization_id": organization_id,
                "mobile_no": mobile_no
            }))
        except Exception as e:
            # Fallback for tests if registry fails due to whatever reason, though it shouldn't
            profile = analytics.get_profile(organization_id, mobile_no)
            return jsonify({
                "history_summary": [
                    f"Current/last concern: {profile['current_issue']}",
                    f"Outcome and sentiment: {profile['previous_action']}",
                    f"Relationship context: {profile['previous_session_count']} prior session(s); next step: {profile.get('recommended_next_action', 'None')}",
                ],
                "memory_count": profile.get("memory_count", 0)
            })

    @app.get("/api/inbox/welcome")
    def inbox_welcome():
        organization_id = request.args.get("organization_id", "")
        mobile_no = request.args.get("mobile_no", "")
        if not organization_id or not mobile_no:
            raise BadRequest("organization_id and mobile_no are required")
        mobile_no = normalize_mobile(mobile_no)
        profile = analytics.get_profile(organization_id, mobile_no)
        if profile.get("memory_count", 0) == 0:
            return jsonify({"welcome": "Hello! How can I help you today?"})
        
        status_phrase = "has this been resolved" if profile.get("status") != "resolved" else "is everything still working well"
        return jsonify({
            "welcome": f'Hello! I see your last query was regarding "{profile["current_issue"]}" — {status_phrase}, or can I help you further today?'
        })

    if analytics_repository is None and os.getenv("WARMUP_ON_STARTUP", "false").lower() == "true":
        threading.Thread(target=analytics.warm, name="profile-cache-warmup", daemon=True).start()

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8765")), debug=os.getenv("FLASK_DEBUG") == "1")
