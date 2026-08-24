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
PROMPT_DIR = Path(__file__).parent / "prompts"
NO_APPROVED_ANSWER = (
    "I apologize, but I could not find an approved answer to your question "
    "in our human-agent knowledge records."
)
NO_KNOWLEDGE_ANSWER = (
    "I apologize, but I could not find an answer in either our bot knowledge base "
    "or our approved human-agent knowledge records."
)


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
        token = {
            "saturday": "weekend", "sunday": "weekend", "weekends": "weekend",
            "install": "installation", "installed": "installation", "installing": "installation",
        }.get(token, token)
        result.add(token)
    return result


def _prompt(name: str, **values: str) -> str:
    template = (PROMPT_DIR / name).read_text(encoding="utf-8")
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    return template


def _safe_tone_output(generated: str | None, original: str) -> str:
    """Accept only clean customer-facing text; never expose reasoning or prompt content."""
    if not generated:
        return original
    cleaned = str(generated).strip()
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.I | re.S).strip()
    unsafe_markers = (
        "<think", "</think", "thinking process", "analyze user input", "style_guidance",
        "**task", "**constraints", "**response", "return only the rewritten response",
        "rewrite response", "system prompt", "developer message",
    )
    lowered = cleaned.casefold()
    if not cleaned or any(marker in lowered for marker in unsafe_markers):
        return original
    if len(cleaned) > max(600, len(original) * 3):
        return original
    return cleaned.strip(' "')


_NON_ANSWER_PATTERNS = (
    r"\b(?:will|shall)\s+(?:assist|contact|call|reply|respond|check)\b",
    r"\b(?:contact|connect(?:ed)?|transfer(?:red)?|forward(?:ed)?)\s+(?:you\s+)?(?:to|with)\b",
    r"\b(?:in|by)\s+(?:the\s+)?(?:morning|evening|tomorrow)\b",
    r"\b(?:please\s+)?(?:wait|hold on|stay connected)\b",
    r"\bwhat\s+(?:is\s+the\s+)?(?:issue|problem)\b",
    r"\b(?:not sure|cannot confirm|can't confirm|will confirm)\b",
)


def _is_reusable_pair(question: str, answer: str) -> bool:
    """Conservative gate: incomplete hand-offs and ambiguous chat are not knowledge."""
    question, answer = _redact(question), _redact(answer)
    q_words = re.findall(r"[\w]+", question.casefold(), re.UNICODE)
    a_words = re.findall(r"[\w]+", answer.casefold(), re.UNICODE)
    if len(q_words) < 4 or len(a_words) < 5:
        return False
    if re.fullmatch(r"[\W_]*(?:ok(?:ay)?|yes|no|hi|hello|urgent|its urgent much)[\W_]*", question, re.I):
        return False
    if any(re.search(pattern, answer, re.I) for pattern in _NON_ANSWER_PATTERNS):
        return False
    # Deictic replies cannot safely stand alone outside the original conversation.
    if re.search(r"\b(?:this one|that one|it can|we can change it|same one)\b", answer, re.I):
        return False
    combined = f"{question} {answer}"
    if re.search(r"\b(?:price|fee|charges?|cost|pkr|rs\.?|usd)\b", combined, re.I):
        has_currency = bool(re.search(r"\b(?:pkr|rs\.?|usd)\s*\d|\d\s*(?:pkr|rs\.?|usd)\b", answer, re.I))
        has_scope = bool(re.search(r"\b(?:package|plan|mbps|consultation|appointment|service|installation)\b", combined, re.I))
        has_validity = bool(re.search(r"\b(?:effective|valid|current(?:ly)?|as of|from)\b|\b(?:20\d{2})\b", answer, re.I))
        if not (has_currency and has_scope and has_validity):
            return False
    if re.search(r"\b(?:policy|regulation|rule)\b", combined, re.I):
        if not re.search(r"\b(?:effective|valid|current(?:ly)?|as of|from|version)\b|\b(?:20\d{2})\b", answer, re.I):
            return False
    return True


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
                CREATE TABLE IF NOT EXISTS bot_knowledge_articles (
                    id TEXT PRIMARY KEY, organization_scope TEXT NOT NULL,
                    question TEXT NOT NULL, answer TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_curation_policies (
                    organization_scope TEXT PRIMARY KEY, rules_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_sessions_org_status
                    ON agent_sessions(organization_scope, status, closed_at);
                CREATE INDEX IF NOT EXISTS idx_agent_messages_session_time
                    ON agent_messages(organization_scope, session_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_articles_org_status_topic
                    ON knowledge_articles(organization_scope, status, canonical_topic);
                CREATE INDEX IF NOT EXISTS idx_versions_article_status
                    ON knowledge_article_versions(article_id, status, version DESC);
                CREATE INDEX IF NOT EXISTS idx_bot_knowledge_org_status
                    ON bot_knowledge_articles(organization_scope, status, updated_at DESC);
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
                """SELECT s.*, (
                    SELECT COUNT(*) FROM agent_messages m
                    WHERE m.organization_scope=s.organization_scope AND m.session_id=s.id
                ) AS message_count
                FROM agent_sessions s WHERE s.organization_scope=?
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
        reusable = self._extract_reusable(organization_scope, messages)
        now = _now()
        with self._connect() as db:
            db.execute(
                "UPDATE agent_sessions SET status='closed',resolution_status='resolved',closed_at=? WHERE id=? AND organization_scope=?",
                (now, session_id, organization_scope),
            )
        article = None
        if reusable:
            question, answer = reusable
            article = self.upsert_article(
                organization_scope, question, answer, actor="application:auto-agent-chat",
                source_session_id=session_id,
            )
        return {"session": self.get_session(organization_scope, session_id), "article": article,
                "reusable": bool(article),
                "rejection_reason": None if article else
                "No complete, standalone, generalizable human-agent answer passed the organization policy.",
                "idempotent": False}

    def curation_policy(self, organization_scope: str) -> dict[str, Any]:
        default = {
            "scope": organization_scope,
            "save": [
                "complete generally applicable factual answers",
                "resolved procedures with the required steps or conditions",
                "standalone questions and answers that remain clear outside the source chat",
            ],
            "reject": [
                "greetings, acknowledgements, urgency, filler, and clarification questions",
                "transfers, referrals, promises to answer later, and unresolved responses",
                "ambiguous replies, missing requested details, customer-specific facts, and identifiers",
                "opinions, guesses, unsafe claims, or content that cannot be generalized",
            ],
            "controlled_facts": {
                "prices_and_fees": "save only with currency, applicable product/service and effective/current period",
                "regulations_and_policies": "save only with clear organization scope, rule conditions and version/effective context",
                "service_rules": "save when complete, customer-general and not tied to one account or transaction",
            },
        }
        with self._connect() as db:
            row = db.execute(
                "SELECT rules_json FROM knowledge_curation_policies WHERE organization_scope=?",
                (organization_scope,),
            ).fetchone()
        if row:
            try:
                custom = json.loads(row["rules_json"])
                if isinstance(custom, dict):
                    default.update(custom)
            except json.JSONDecodeError:
                pass
        return default

    def set_curation_policy(self, organization_scope: str, policy: dict[str, Any]) -> dict[str, Any]:
        allowed = {key: policy[key] for key in ("save", "reject", "controlled_facts") if key in policy}
        if not allowed:
            raise ValueError("policy must include save, reject, or controlled_facts")
        if "save" in allowed and not isinstance(allowed["save"], list):
            raise ValueError("save rules must be a list")
        if "reject" in allowed and not isinstance(allowed["reject"], list):
            raise ValueError("reject rules must be a list")
        if "controlled_facts" in allowed and not isinstance(allowed["controlled_facts"], dict):
            raise ValueError("controlled_facts must be an object")
        with self._connect() as db:
            db.execute(
                "INSERT INTO knowledge_curation_policies(organization_scope,rules_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(organization_scope) DO UPDATE SET rules_json=excluded.rules_json,updated_at=excluded.updated_at",
                (organization_scope, json.dumps(allowed, ensure_ascii=False), _now()),
            )
        return self.curation_policy(organization_scope)

    def tone_profile(self, organization_scope: str, limit: int = 500) -> dict[str, Any]:
        """Derive style-only guidance; message facts are deliberately excluded."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT text FROM agent_messages WHERE organization_scope=? AND sender_role='agent' "
                "ORDER BY created_at DESC LIMIT ?", (organization_scope, min(max(limit, 1), 2000)),
            ).fetchall()
        texts = [str(row["text"]).strip() for row in rows if str(row["text"]).strip()]
        if not texts:
            return {"organization_id": organization_scope, "sample_count": 0,
                    "style_guidance": "Use a concise, polite, clear support tone.", "facts_learned": False}
        average_words = round(sum(len(text.split()) for text in texts) / len(texts), 1)
        roman_urdu = sum(bool(re.search(r"\b(?:ap|aap|ha|hai|abhi|kr|kar|sir|g)\b", text, re.I)) for text in texts)
        polite = sum(bool(re.search(r"\b(?:please|thank|thanks|sir|madam|kindly)\b", text, re.I)) for text in texts)
        language = "concise English with natural Roman Urdu when the customer uses it" if roman_urdu / len(texts) >= .1 else "clear conversational English"
        courtesy = "Use a polite acknowledgement." if polite / len(texts) >= .1 else "Be friendly without unnecessary formality."
        return {
            "organization_id": organization_scope, "sample_count": len(texts),
            "style_guidance": f"Use {language}. {courtesy} Keep most replies near {max(6, round(average_words))} words, unless steps require detail.",
            "facts_learned": False,
            "safety_rule": "Learn wording style only; never copy customer data, factual claims, promises, errors, or identifiers from tone samples.",
        }

    def _extract_reusable(self, organization_scope: str, messages: list[dict[str, Any]]) -> tuple[str, str] | None:
        transcript = "\n".join(f"{m['sender_role'].title()}: {_redact(m['text'])}" for m in messages)
        policy = self.curation_policy(organization_scope)
        prompt = _prompt("knowledge_curator.txt", organization_policy=json.dumps(policy, ensure_ascii=False),
                         transcript=transcript)
        generated = _ollama(prompt, workload="knowledge")
        if generated:
            try:
                match = re.search(r"\{.*\}", generated, re.DOTALL)
                payload = json.loads(match.group(0) if match else generated)
                if payload.get("reusable") and payload.get("question") and payload.get("answer"):
                    pair = _redact(str(payload["question"])), _redact(str(payload["answer"]))
                    return pair if _is_reusable_pair(*pair) else None
                return None
            except (ValueError, TypeError, json.JSONDecodeError):
                pass
        # Offline fallback remains conservative and accepts only a standalone-looking pair.
        questions = [_redact(m["text"]) for m in messages if m["sender_role"] == "customer"]
        answers = [_redact(m["text"]) for m in messages if m["sender_role"] == "agent"]
        question = max(questions, key=lambda value: len(_tokens(value)))
        answer = max(answers, key=lambda value: len(_tokens(value)))
        return (question, answer) if _is_reusable_pair(question, answer) else None

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
                f"""SELECT a.*,v.canonical_question,v.answer,v.approved_by,v.approved_at,
                v.source_session_id
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

    def upsert_bot_article(self, organization_scope: str, question: str, answer: str) -> dict[str, Any]:
        if not question.strip() or not answer.strip():
            raise ValueError("question and answer are required")
        article_id, now = str(uuid.uuid4()), _now()
        with self._connect() as db:
            db.execute("INSERT INTO bot_knowledge_articles VALUES (?,?,?,?,?,?,?)", (
                article_id, organization_scope, _redact(question), _redact(answer), "active", now, now,
            ))
        return {"id": article_id, "organization_scope": organization_scope,
                "question": _redact(question), "answer": _redact(answer), "status": "active"}

    def list_bot_articles(self, organization_scope: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM bot_knowledge_articles WHERE organization_scope=? AND status='active' ORDER BY updated_at DESC",
                (organization_scope,),
            ).fetchall()
        return [dict(row) for row in rows]

    def search_bot_articles(self, organization_scope: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
        query_tokens = _tokens(query)
        rows = self.list_bot_articles(organization_scope)
        for row in rows:
            tokens = _tokens(row["question"] + " " + row["answer"])
            row["score"] = len(query_tokens & tokens) / max(len(query_tokens), 1)
        return [row for row in sorted(rows, key=lambda item: item["score"], reverse=True)[:limit]
                if row["score"] >= 0.40]


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

    def apply_tone(self, organization_scope: str, answer: str) -> tuple[str, dict[str, Any]]:
        profile = self.repository.tone_profile(organization_scope)
        prompt = _prompt("tone_response.txt", style_guidance=profile["style_guidance"], response=answer)
        generated = _ollama(prompt, workload="knowledge", timeout=2.0, max_retries=1)
        return _safe_tone_output(generated, answer), profile

    def search(self, organization_scope: str, query: str, limit: int = 5) -> dict[str, Any]:
        lexical = [row for row in self.repository.lexical_search(organization_scope, query, limit)
                   if row["score"] >= 0.50]
        if lexical:
            best = lexical[0]
            return {"status": "answer_found", "answer": best["answer"].strip(), "items": lexical,
                    "article_id": best["id"], "article_version": best["active_version"],
                    "answer_source": "approved_agent_knowledge", "grounded": True,
                    "retrieval": "sql-topic-index",
                    "searched_sources": ["active_approved_agent_articles"]}
        matches = self.memory_store.search(
            organization_id=organization_scope, mobile_no=KNOWLEDGE_MOBILE,
            query=query, limit=min(max(limit, 1), 20),
        )
        verified = []
        query_tokens = _tokens(query)
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
            article_tokens = _tokens(article["canonical_question"] + " " + article["answer"])
            shared = len(query_tokens & article_tokens)
            required_shared = 1 if len(query_tokens) <= 2 else max(2, (len(query_tokens) + 1) // 2)
            overlap = shared / max(len(query_tokens), 1)
            if article["score"] >= 0.55 and shared >= required_shared and overlap >= 0.50:
                verified.append(article)
        if not verified:
            verified = [row for row in self.repository.lexical_search(organization_scope, query, limit) if row["score"] >= 0.50]
        if not verified:
            return {
                "status": "no_evidence",
                "answer": NO_APPROVED_ANSWER,
                "items": [],
                "grounded": False,
                "answer_source": "approved_agent_knowledge",
                "searched_sources": ["active_approved_agent_articles"],
            }
        best = verified[0]
        return {"status": "answer_found", "answer": best["answer"].strip(), "items": verified,
                "article_id": best["id"], "article_version": best["active_version"],
                "answer_source": "approved_agent_knowledge", "grounded": True,
                "retrieval": "mem0-pinecone-semantic",
                "searched_sources": ["active_approved_agent_articles"]}

    def search_bot(self, organization_scope: str, query: str, limit: int = 5) -> dict[str, Any]:
        matches = self.repository.search_bot_articles(organization_scope, query, limit)
        if not matches:
            return {"status": "no_evidence", "answer": None, "items": [], "grounded": False,
                    "answer_source": "bot_knowledge_base", "searched_sources": ["bot_knowledge_base"]}
        best = matches[0]
        return {"status": "answer_found", "answer": best["answer"].strip(),
                "items": matches, "grounded": True, "answer_source": "bot_knowledge_base",
                "searched_sources": ["bot_knowledge_base"]}

    def resolve(self, organization_scope: str, query: str, limit: int = 5) -> dict[str, Any]:
        primary = self.search_bot(organization_scope, query, limit)
        if primary["grounded"]:
            primary["fallback_used"] = False
            result = primary
        else:
            agent = self.search(organization_scope, query, limit)
            agent["fallback_used"] = True
            agent["searched_sources"] = ["bot_knowledge_base", "active_approved_agent_articles"]
            if not agent["grounded"]:
                agent["answer"] = NO_KNOWLEDGE_ANSWER
            result = agent
        result["answer"], result["tone_profile"] = self.apply_tone(organization_scope, result["answer"])
        result["tone_applied"] = True
        return result
