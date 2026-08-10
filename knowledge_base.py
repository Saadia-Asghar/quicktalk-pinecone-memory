"""Organization-owned agent-chat knowledge with versioned live retrieval."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory_summarizer import _ollama


KNOWLEDGE_MOBILE = "+10000000000"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact(text: str) -> str:
    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[email removed]", text)
    text = re.sub(r"(?<!\w)\+?\d[\d\s()-]{8,}\d(?!\w)", "[number removed]", text)
    text = re.sub(r"\b(?:token|mr|account|cnic|ticket)\s*#?\s*[:=-]?\s*[A-Z0-9-]{4,}\b", "[identifier removed]", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def _canonical(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))[:240]


def _tokens(text: str) -> set[str]:
    stop = {"a", "an", "the", "is", "are", "do", "does", "can", "you", "we", "i", "on", "in", "at", "to"}
    result = set()
    for token in re.findall(r"[a-z0-9]+", text.casefold()):
        if token in stop:
            continue
        if token.endswith("ies") and len(token) > 4:
            token = token[:-3] + "y"
        elif token.endswith("s") and len(token) > 4:
            token = token[:-1]
        result.add(token)
    return result


class KnowledgeRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._initialize()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_sessions (
                    id TEXT PRIMARY KEY, organization_scope TEXT NOT NULL,
                    customer_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                    status TEXT NOT NULL, resolution_status TEXT,
                    started_at TEXT NOT NULL, closed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS agent_messages (
                    id TEXT PRIMARY KEY, organization_scope TEXT NOT NULL,
                    session_id TEXT NOT NULL, sender_role TEXT NOT NULL,
                    text TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES agent_sessions(id)
                );
                CREATE TABLE IF NOT EXISTS knowledge_articles (
                    id TEXT PRIMARY KEY, organization_scope TEXT NOT NULL,
                    canonical_topic TEXT NOT NULL, status TEXT NOT NULL,
                    active_version INTEGER NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(organization_scope, canonical_topic)
                );
                CREATE TABLE IF NOT EXISTS knowledge_article_versions (
                    id TEXT PRIMARY KEY, article_id TEXT NOT NULL,
                    version INTEGER NOT NULL, canonical_question TEXT NOT NULL,
                    answer TEXT NOT NULL, status TEXT NOT NULL,
                    source_session_id TEXT, approved_by TEXT NOT NULL,
                    approved_at TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY(article_id) REFERENCES knowledge_articles(id),
                    UNIQUE(article_id, version)
                );
                CREATE TABLE IF NOT EXISTS knowledge_audit_log (
                    id TEXT PRIMARY KEY, organization_scope TEXT NOT NULL,
                    entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
                    action TEXT NOT NULL, actor_id TEXT NOT NULL,
                    previous_value TEXT, new_value TEXT, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_sessions_org_status
                    ON agent_sessions(organization_scope, status, closed_at);
                CREATE INDEX IF NOT EXISTS idx_agent_messages_session_time
                    ON agent_messages(organization_scope, session_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_articles_org_status_topic
                    ON knowledge_articles(organization_scope, status, canonical_topic);
                CREATE INDEX IF NOT EXISTS idx_versions_article_status
                    ON knowledge_article_versions(article_id, status, version DESC);
                """
            )

    def create_session(self, organization_scope: str, customer_id: str, agent_id: str) -> dict[str, Any]:
        session_id = str(uuid.uuid4())
        now = _now()
        with self._connect() as db:
            db.execute("INSERT INTO agent_sessions VALUES (?,?,?,?,?,?,?,?)", (
                session_id, organization_scope, customer_id, agent_id, "open", None, now, None,
            ))
        return self.get_session(organization_scope, session_id)

    def add_message(self, organization_scope: str, session_id: str, role: str, text: str) -> dict[str, Any]:
        if role not in {"customer", "agent"}:
            raise ValueError("sender_role must be customer or agent")
        session = self.get_session(organization_scope, session_id)
        if session["status"] != "open":
            raise ValueError("agent chat is already closed")
        message = {"id": str(uuid.uuid4()), "organization_scope": organization_scope,
                   "session_id": session_id, "sender_role": role, "text": text.strip(), "created_at": _now()}
        if not message["text"]:
            raise ValueError("message text is required")
        with self._connect() as db:
            db.execute("INSERT INTO agent_messages VALUES (?,?,?,?,?,?)", tuple(message.values()))
        return message

    def get_session(self, organization_scope: str, session_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM agent_sessions WHERE id=? AND organization_scope=?",
                (session_id, organization_scope),
            ).fetchone()
        if not row:
            raise ValueError("unknown agent chat for this organization")
        return dict(row)

    def session_messages(self, organization_scope: str, session_id: str) -> list[dict[str, Any]]:
        self.get_session(organization_scope, session_id)
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM agent_messages WHERE organization_scope=? AND session_id=? ORDER BY created_at",
                (organization_scope, session_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_sessions(self, organization_scope: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT s.*, COUNT(m.id) AS message_count FROM agent_sessions s
                LEFT JOIN agent_messages m ON m.session_id=s.id
                WHERE s.organization_scope=? GROUP BY s.id
                ORDER BY COALESCE(s.closed_at,s.started_at) DESC LIMIT ?""",
                (organization_scope, min(max(limit, 1), 500)),
            ).fetchall()
        return [dict(row) for row in rows]

    def close_session(self, organization_scope: str, session_id: str) -> dict[str, Any]:
        session = self.get_session(organization_scope, session_id)
        if session["status"] == "closed":
            article = self.article_for_source(organization_scope, session_id)
            return {"session": session, "article": article, "idempotent": True}
        messages = self.session_messages(organization_scope, session_id)
        customers = [m for m in messages if m["sender_role"] == "customer"]
        agents = [m for m in messages if m["sender_role"] == "agent"]
        if not customers or not agents:
            raise ValueError("a completed chat requires customer and human-agent messages")
        question, answer = self._extract_reusable(messages)
        now = _now()
        with self._connect() as db:
            db.execute(
                "UPDATE agent_sessions SET status='closed',resolution_status='resolved',closed_at=? WHERE id=? AND organization_scope=?",
                (now, session_id, organization_scope),
            )
        article = self.upsert_article(
            organization_scope, question, answer, actor="application:auto-agent-chat",
            source_session_id=session_id,
        )
        return {"session": self.get_session(organization_scope, session_id), "article": article, "idempotent": False}

    @staticmethod
    def _extract_reusable(messages: list[dict[str, Any]]) -> tuple[str, str]:
        transcript = "\n".join(f"{m['sender_role'].title()}: {_redact(m['text'])}" for m in messages)
        prompt = (
            "Extract one reusable support question and its human-agent answer from this completed chat. "
            "Remove customer-specific identifiers. Return JSON only: "
            '{"question":"...","answer":"...","reusable":true}. '
            "Do not invent information.\n\n" + transcript
        )
        generated = _ollama(prompt)
        if generated:
            try:
                match = re.search(r"\{.*\}", generated, re.DOTALL)
                payload = json.loads(match.group(0) if match else generated)
                if payload.get("reusable") and payload.get("question") and payload.get("answer"):
                    return _redact(str(payload["question"])), _redact(str(payload["answer"]))
            except (ValueError, TypeError, json.JSONDecodeError):
                pass
        question = next(_redact(m["text"]) for m in messages if m["sender_role"] == "customer")
        answer = next(_redact(m["text"]) for m in reversed(messages) if m["sender_role"] == "agent")
        return question, answer

    def upsert_article(self, organization_scope: str, question: str, answer: str, *, actor: str,
                       source_session_id: str | None = None) -> dict[str, Any]:
        topic = _canonical(question)
        if not topic or not answer.strip():
            raise ValueError("question and answer are required")
        now = _now()
        with self._connect() as db:
            current = db.execute(
                "SELECT * FROM knowledge_articles WHERE organization_scope=? AND canonical_topic=?",
                (organization_scope, topic),
            ).fetchone()
            if current:
                article_id = current["id"]
                version = int(current["active_version"]) + 1
                db.execute("UPDATE knowledge_article_versions SET status='superseded' WHERE article_id=? AND status='active'", (article_id,))
                db.execute(
                    "UPDATE knowledge_articles SET status='active',active_version=?,updated_at=? WHERE id=?",
                    (version, now, article_id),
                )
                action = "edited"
            else:
                article_id, version, action = str(uuid.uuid4()), 1, "auto_approved"
                db.execute("INSERT INTO knowledge_articles VALUES (?,?,?,?,?,?,?)", (
                    article_id, organization_scope, topic, "active", version, now, now,
                ))
            version_id = str(uuid.uuid4())
            db.execute("INSERT INTO knowledge_article_versions VALUES (?,?,?,?,?,?,?,?,?,?)", (
                version_id, article_id, version, _redact(question), _redact(answer), "active",
                source_session_id, actor, now, now,
            ))
            db.execute("INSERT INTO knowledge_audit_log VALUES (?,?,?,?,?,?,?,?,?)", (
                str(uuid.uuid4()), organization_scope, "knowledge_article", article_id,
                action, actor, None, json.dumps({"version": version, "answer": _redact(answer)}), now,
            ))
        return self.get_article(organization_scope, article_id)

    def get_article(self, organization_scope: str, article_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                """SELECT a.*,v.id AS version_id,v.canonical_question,v.answer,
                v.status AS version_status,v.source_session_id,v.approved_by,v.approved_at
                FROM knowledge_articles a JOIN knowledge_article_versions v
                ON v.article_id=a.id AND v.version=a.active_version
                WHERE a.id=? AND a.organization_scope=?""", (article_id, organization_scope),
            ).fetchone()
        if not row:
            raise ValueError("unknown knowledge article for this organization")
        return dict(row)

    def article_for_source(self, organization_scope: str, session_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT a.id FROM knowledge_articles a JOIN knowledge_article_versions v ON v.article_id=a.id
                WHERE a.organization_scope=? AND v.source_session_id=? ORDER BY v.version DESC LIMIT 1""",
                (organization_scope, session_id),
            ).fetchone()
        return self.get_article(organization_scope, row["id"]) if row else None

    def list_articles(self, organization_scope: str, include_inactive: bool = True) -> list[dict[str, Any]]:
        where = "a.organization_scope=?" if include_inactive else "a.organization_scope=? AND a.status='active'"
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT a.*,v.canonical_question,v.answer,v.approved_by,v.approved_at
                FROM knowledge_articles a JOIN knowledge_article_versions v
                ON v.article_id=a.id AND v.version=a.active_version WHERE {where}
                ORDER BY a.updated_at DESC""", (organization_scope,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_status(self, organization_scope: str, article_id: str, status: str, actor: str) -> dict[str, Any]:
        if status not in {"active", "disabled", "deleted"}:
            raise ValueError("status must be active, disabled, or deleted")
        previous = self.get_article(organization_scope, article_id)
        now = _now()
        with self._connect() as db:
            db.execute("UPDATE knowledge_articles SET status=?,updated_at=? WHERE id=? AND organization_scope=?",
                       (status, now, article_id, organization_scope))
            db.execute("INSERT INTO knowledge_audit_log VALUES (?,?,?,?,?,?,?,?,?)", (
                str(uuid.uuid4()), organization_scope, "knowledge_article", article_id,
                status, actor, json.dumps({"status": previous["status"]}), json.dumps({"status": status}), now,
            ))
        return self.get_article(organization_scope, article_id)

    def lexical_search(self, organization_scope: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
        query_tokens = _tokens(query)
        articles = self.list_articles(organization_scope, include_inactive=False)
        for article in articles:
            tokens = _tokens(article["canonical_question"] + " " + article["answer"])
            article["score"] = len(query_tokens & tokens) / max(len(query_tokens), 1)
        return sorted(articles, key=lambda row: row["score"], reverse=True)[:limit]


class KnowledgeService:
    def __init__(self, repository: KnowledgeRepository, memory_store) -> None:
        self.repository = repository
        self.memory_store = memory_store

    def close_and_index(self, organization_scope: str, session_id: str) -> dict[str, Any]:
        result = self.repository.close_session(organization_scope, session_id)
        article = result["article"]
        if article and not result["idempotent"]:
            self._index(article)
        return result

    def edit_and_index(self, organization_scope: str, article_id: str, question: str, answer: str, actor: str) -> dict[str, Any]:
        current = self.repository.get_article(organization_scope, article_id)
        article = self.repository.upsert_article(
            organization_scope, question or current["canonical_question"], answer,
            actor=actor, source_session_id=current.get("source_session_id"),
        )
        self._index(article)
        return article

    def _index(self, article: dict[str, Any]) -> None:
        self.memory_store.add(
            organization_id=article["organization_scope"], mobile_no=KNOWLEDGE_MOBILE,
            session_id=f"knowledge:{article['id']}", role="system",
            text=f"Question: {article['canonical_question']} Answer: {article['answer']}",
            infer=False, metadata={"memory_type": "approved_knowledge", "article_id": article["id"],
                                   "article_version": article["active_version"], "status": "active"},
        )

    def search(self, organization_scope: str, query: str, limit: int = 5) -> dict[str, Any]:
        matches = self.memory_store.search(
            organization_id=organization_scope, mobile_no=KNOWLEDGE_MOBILE,
            query=query, limit=min(max(limit, 1), 20),
        )
        verified = []
        for match in matches:
            article_id = match.get("article_id")
            if not article_id:
                continue
            try:
                article = self.repository.get_article(organization_scope, str(article_id))
            except ValueError:
                continue
            if article["status"] != "active" or int(match.get("article_version", 0)) != int(article["active_version"]):
                continue
            article["score"] = float(match.get("score", 0))
            if article["score"] >= 0.35:
                verified.append(article)
        if not verified:
            verified = [row for row in self.repository.lexical_search(organization_scope, query, limit) if row["score"] >= 0.30]
        if not verified:
            return {"status": "no_evidence", "answer": None, "items": []}
        best = verified[0]
        prompt = (
            "Answer the customer using only this approved organization knowledge. "
            "Do not add facts. If it does not answer the question, say no approved answer was found.\n"
            f"Question: {query}\nApproved knowledge: {best['answer']}\nAnswer:"
        )
        answer = _ollama(prompt) or best["answer"]
        return {"status": "answer_found", "answer": answer.strip(), "items": verified,
                "article_id": best["id"], "article_version": best["active_version"],
                "answer_source": "approved_agent_knowledge"}
