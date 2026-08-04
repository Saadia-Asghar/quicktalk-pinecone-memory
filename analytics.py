"""Tenant-isolated structured analytics derived from conversational memory."""

from __future__ import annotations

import os
import re
import sqlite3
import hashlib
import json
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_DB = Path(__file__).parent / "data" / "analytics.db"

TAXONOMIES: dict[str, list[tuple[str, str]]] = {
    "telecom": [
        ("Connectivity", r"internet|disconnect|network|signal|fiber|speed|outage"),
        ("Billing", r"bill|charge|invoice|tax|payment"),
        ("Plan & Upgrade", r"plan|upgrade|package|mbps"),
        ("Installation", r"install|router|equipment|device"),
    ],
    "fintech": [
        ("Payments", r"payment|merchant|paid|purchase|deduct"),
        ("Transfers", r"transfer|bank|beneficiary|remittance"),
        ("Disputes & Fraud", r"unauthorized|fraud|charge|dispute|stolen"),
        ("KYC & Account", r"kyc|verify|verification|identity|account|login"),
    ],
    "healthcare": [
        ("Respiratory", r"cough|breath|asthma|respiratory|chest"),
        ("Cardiology", r"heart|cardiac|blood pressure|hypertension"),
        ("Dermatology", r"skin|rash|dermat|allergy"),
        ("Medication", r"medicine|medication|prescription|dose|refill"),
        ("Appointments", r"appointment|doctor|visit|schedule|follow-up"),
        ("Billing & Insurance", r"insurance|claim|bill|coverage|payment"),
    ],
    "generic": [
        ("Service Issue", r"issue|problem|error|not working|failed"),
        ("Billing", r"bill|charge|invoice|payment"),
        ("Request", r"request|please|need|want"),
    ],
}

RESOLVED_RE = re.compile(r"\b(resolved|fixed|restored|received|completed|active now)\b", re.I)
NEGATIVE_RE = re.compile(r"\b(frustrated|angry|upset|failed|still|unresolved|urgent)\b", re.I)
POSITIVE_RE = re.compile(r"\b(thank|satisfied|happy|resolved|fixed|received)\b", re.I)


class ProfileCache:
    """Cache-aside profile cache using Redis when configured, otherwise local TTL memory."""

    def __init__(self, ttl_seconds: int = 300) -> None:
        self.ttl = ttl_seconds
        self._local: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._redis = None
        redis_url = os.getenv("REDIS_URL")
        if redis_url:
            try:
                import redis

                client = redis.Redis.from_url(redis_url, decode_responses=True)
                client.ping()
                self._redis = client
            except (ImportError, OSError):
                self._redis = None

    @property
    def backend(self) -> str:
        return "redis" if self._redis else "local-ttl"

    @staticmethod
    def key(scope: str, mobile_no: str) -> str:
        digest = hashlib.sha256(f"{scope}:{mobile_no}".encode()).hexdigest()
        return f"quicktalk:profile:{digest}"

    def get(self, scope: str, mobile_no: str) -> dict[str, Any] | None:
        key = self.key(scope, mobile_no)
        if self._redis:
            value = self._redis.get(key)
            return json.loads(value) if value else None
        with self._lock:
            cached = self._local.get(key)
            if not cached or cached[0] <= time.monotonic():
                self._local.pop(key, None)
                return None
            return dict(cached[1])

    def set(self, scope: str, mobile_no: str, profile: dict[str, Any]) -> None:
        key = self.key(scope, mobile_no)
        if self._redis:
            self._redis.setex(key, self.ttl, json.dumps(profile))
            return
        with self._lock:
            self._local[key] = (time.monotonic() + self.ttl, dict(profile))

    def delete(self, scope: str, mobile_no: str) -> None:
        key = self.key(scope, mobile_no)
        if self._redis:
            self._redis.delete(key)
            return
        with self._lock:
            self._local.pop(key, None)


class AnalyticsRepository:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or os.getenv("ANALYTICS_DB_PATH", DEFAULT_DB))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cache = ProfileCache(int(os.getenv("PROFILE_CACHE_TTL", "300")))
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS organizations (
                    organization_scope TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    organization_id TEXT NOT NULL,
                    organization_name TEXT NOT NULL,
                    industry TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_events (
                    memory_id TEXT PRIMARY KEY,
                    organization_scope TEXT NOT NULL,
                    mobile_no TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    category TEXT NOT NULL,
                    resolution_status TEXT NOT NULL,
                    sentiment TEXT NOT NULL,
                    FOREIGN KEY (organization_scope) REFERENCES organizations(organization_scope)
                );
                CREATE TABLE IF NOT EXISTS customer_profiles (
                    organization_scope TEXT NOT NULL,
                    mobile_no TEXT NOT NULL,
                    current_issue TEXT NOT NULL,
                    status TEXT NOT NULL,
                    recent_contacts INTEGER NOT NULL,
                    previous_action TEXT NOT NULL,
                    recommended_next_action TEXT NOT NULL,
                    previous_session_count INTEGER NOT NULL,
                    profile_summary TEXT NOT NULL,
                    memory_count INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (organization_scope, mobile_no)
                );
                CREATE TABLE IF NOT EXISTS session_summaries (
                    organization_scope TEXT NOT NULL,
                    mobile_no TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    message_count INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    resolution_status TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    PRIMARY KEY (organization_scope, mobile_no, session_id)
                );
                CREATE INDEX IF NOT EXISTS idx_events_org_time
                    ON memory_events(organization_scope, timestamp);
                CREATE INDEX IF NOT EXISTS idx_events_org_session
                    ON memory_events(organization_scope, session_id);
                CREATE INDEX IF NOT EXISTS idx_events_org_mobile_time
                    ON memory_events(organization_scope, mobile_no, timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_events_org_category_time
                    ON memory_events(organization_scope, category, timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_events_org_status_time
                    ON memory_events(organization_scope, resolution_status, timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_sessions_org_mobile_time
                    ON session_summaries(organization_scope, mobile_no, ended_at DESC);
                CREATE INDEX IF NOT EXISTS idx_sessions_org_status_time
                    ON session_summaries(organization_scope, resolution_status, ended_at DESC);
                """
            )

    def register_organization(
        self, *, scope: str, tenant_id: str, organization_id: str,
        organization_name: str, industry: str,
    ) -> None:
        normalized = industry.lower().strip()
        if normalized not in TAXONOMIES:
            normalized = "generic"
        with self._connect() as db:
            db.execute(
                """INSERT INTO organizations VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(organization_scope) DO UPDATE SET
                tenant_id=excluded.tenant_id, organization_id=excluded.organization_id,
                organization_name=excluded.organization_name, industry=excluded.industry""",
                (scope, tenant_id, organization_id, organization_name, normalized),
            )

    def record_memory(self, record: dict[str, Any]) -> None:
        scope = str(record["organization_id"])
        industry = self._industry(scope)
        text = str(record["text"])
        with self._connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO memory_events
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(record["id"]), scope, str(record["mobile_no"]),
                    str(record["session_id"]), str(record.get("role", "customer")), text,
                    str(record["timestamp"]), classify_category(text, industry),
                    "resolved" if RESOLVED_RE.search(text) else "not_recorded",
                    classify_sentiment(text),
                ),
            )
        self._recompute_profile(scope, str(record["mobile_no"]))

    def record_memories_bulk(self, records, batch_size: int = 2000) -> int:
        """Insert large imports efficiently without recalculating profiles per message."""
        industry_cache: dict[str, str] = {}
        batch = []
        inserted = 0
        for record in records:
            scope = str(record["organization_id"])
            if scope not in industry_cache:
                industry_cache[scope] = self._industry(scope)
            industry = industry_cache[scope]
            text = str(record["text"])
            batch.append(
                (
                    str(record["id"]), scope, str(record["mobile_no"]),
                    str(record["session_id"]), str(record.get("role", "customer")), text,
                    str(record["timestamp"]),
                    str(record.get("category") or classify_category(text, industry)),
                    str(record.get("resolution_status") or (
                        "resolved" if RESOLVED_RE.search(text) else "not_recorded"
                    )),
                    str(record.get("sentiment") or classify_sentiment(text)),
                )
            )
            if len(batch) >= batch_size:
                self._insert_event_batch(batch)
                inserted += len(batch)
                batch.clear()
        if batch:
            self._insert_event_batch(batch)
            inserted += len(batch)
        return inserted

    def _insert_event_batch(self, batch: list[tuple[Any, ...]]) -> None:
        with self._connect() as db:
            db.executemany(
                """INSERT OR REPLACE INTO memory_events
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", batch,
            )

    def get_profile(
        self, organization_scope: str, mobile_no: str, *, session_limit: int = 5,
        session_offset: int = 0,
    ) -> dict[str, Any]:
        cached = self.cache.get(organization_scope, mobile_no) if session_offset == 0 else None
        if cached:
            profile = cached
            cache_hit = True
        else:
            with self._connect() as db:
                row = db.execute(
                    """SELECT * FROM customer_profiles
                    WHERE organization_scope=? AND mobile_no=?""",
                    (organization_scope, mobile_no),
                ).fetchone()
            if not row:
                self._recompute_profile(organization_scope, mobile_no)
                with self._connect() as db:
                    row = db.execute(
                        """SELECT * FROM customer_profiles
                        WHERE organization_scope=? AND mobile_no=?""",
                        (organization_scope, mobile_no),
                    ).fetchone()
            if not row:
                return self._empty_profile(organization_scope, mobile_no)
            profile = dict(row)
            cache_hit = False
            if session_offset == 0:
                self.cache.set(organization_scope, mobile_no, profile)
        profile = dict(profile)
        profile["session_summaries"] = self.session_summaries(
            organization_scope, mobile_no, limit=session_limit, offset=session_offset
        )
        profile["cache"] = {"hit": cache_hit, "backend": self.cache.backend}
        profile["has_older_sessions"] = (
            session_offset + len(profile["session_summaries"]) < profile["previous_session_count"]
        )
        return profile

    def session_summaries(
        self, organization_scope: str, mobile_no: str, *, limit: int = 5, offset: int = 0,
    ) -> list[dict[str, Any]]:
        limit = min(max(int(limit), 1), 50)
        offset = max(int(offset), 0)
        with self._connect() as db:
            rows = db.execute(
                """SELECT session_id, started_at, ended_at, message_count, category,
                resolution_status, summary FROM session_summaries
                WHERE organization_scope=? AND mobile_no=?
                ORDER BY ended_at DESC LIMIT ? OFFSET ?""",
                (organization_scope, mobile_no, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def backfill_profiles(self) -> int:
        with self._connect() as db:
            customers = db.execute(
                "SELECT DISTINCT organization_scope, mobile_no FROM memory_events"
            ).fetchall()
        for customer in customers:
            self._recompute_profile(customer["organization_scope"], customer["mobile_no"])
        return len(customers)

    def warm(self, limit: int = 100) -> dict[str, Any]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT organization_scope, mobile_no FROM customer_profiles
                ORDER BY updated_at DESC LIMIT ?""", (limit,)
            ).fetchall()
        for row in rows:
            self.get_profile(row["organization_scope"], row["mobile_no"])
        return {"profiles_warmed": len(rows), "cache_backend": self.cache.backend}

    def _recompute_profile(self, scope: str, mobile_no: str) -> None:
        since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        with self._connect() as db:
            all_rows = db.execute(
                """SELECT * FROM memory_events WHERE organization_scope=? AND mobile_no=?
                ORDER BY timestamp ASC""", (scope, mobile_no),
            ).fetchall()
            recent_rows = db.execute(
                """SELECT * FROM memory_events WHERE organization_scope=? AND mobile_no=?
                AND timestamp>=? ORDER BY timestamp ASC""",
                (scope, mobile_no, since),
            ).fetchall()
        all_events = [dict(row) for row in all_rows]
        recent_events = [dict(row) for row in recent_rows]
        if not all_events:
            return
        grouped: dict[str, list[dict[str, Any]]] = {}
        for event in all_events:
            grouped.setdefault(event["session_id"], []).append(event)
        recent_grouped: dict[str, list[dict[str, Any]]] = {}
        for event in recent_events:
            recent_grouped.setdefault(event["session_id"], []).append(event)
        summaries = [self._build_session_summary(scope, mobile_no, session) for session in grouped.values()]
        summaries.sort(key=lambda item: item["ended_at"], reverse=True)
        active_summaries = [
            summary for summary in summaries if summary["session_id"] in recent_grouped
        ] or summaries
        latest_session_id = active_summaries[0]["session_id"]
        latest_events = grouped[latest_session_id]
        latest_customer = next(
            (event for event in reversed(latest_events) if event["role"] != "assistant"),
            latest_events[-1],
        )
        latest_action = next(
            (event["text"] for event in reversed(latest_events) if event["role"] == "assistant"),
            "No previous support action is recorded.",
        )
        status = active_summaries[0]["resolution_status"]
        category = active_summaries[0]["category"]
        next_action = recommended_action(category, status, self._industry(scope))
        current_issue = concise_issue(latest_customer["text"])
        profile_summary = (
            f"Current issue: {current_issue} Status: "
            f"{'Resolved' if status == 'resolved' else 'Unresolved'}. "
            f"Previous action: {latest_action} Recommended next action: {next_action}"
        )
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            for summary in summaries:
                db.execute(
                    """INSERT INTO session_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(organization_scope, mobile_no, session_id) DO UPDATE SET
                    started_at=excluded.started_at, ended_at=excluded.ended_at,
                    message_count=excluded.message_count, category=excluded.category,
                    resolution_status=excluded.resolution_status, summary=excluded.summary""",
                    (
                        scope, mobile_no, summary["session_id"], summary["started_at"],
                        summary["ended_at"], summary["message_count"], summary["category"],
                        summary["resolution_status"], summary["summary"],
                    ),
                )
            db.execute(
                """INSERT INTO customer_profiles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(organization_scope, mobile_no) DO UPDATE SET
                current_issue=excluded.current_issue, status=excluded.status,
                recent_contacts=excluded.recent_contacts, previous_action=excluded.previous_action,
                recommended_next_action=excluded.recommended_next_action,
                previous_session_count=excluded.previous_session_count,
                profile_summary=excluded.profile_summary, memory_count=excluded.memory_count,
                updated_at=excluded.updated_at""",
                (
                    scope, mobile_no, current_issue, status, len(recent_grouped), latest_action,
                    next_action, len(grouped), profile_summary, len(all_events), updated_at,
                ),
            )
        self.cache.delete(scope, mobile_no)
        self.cache.set(scope, mobile_no, {
            "organization_scope": scope, "mobile_no": mobile_no,
            "current_issue": current_issue, "status": status,
            "recent_contacts": len(recent_grouped), "previous_action": latest_action,
            "recommended_next_action": next_action, "previous_session_count": len(grouped),
            "profile_summary": profile_summary, "memory_count": len(all_events), "updated_at": updated_at,
        })

    @staticmethod
    def _build_session_summary(
        scope: str, mobile_no: str, events: list[dict[str, Any]]
    ) -> dict[str, Any]:
        events = sorted(events, key=lambda item: item["timestamp"])
        customer_events = [event for event in events if event["role"] != "assistant"]
        assistant_events = [event for event in events if event["role"] == "assistant"]
        issue = (customer_events or events)[0]["text"]
        action = assistant_events[-1]["text"] if assistant_events else "No support action recorded."
        outcome = customer_events[-1]["text"] if len(customer_events) > 1 else "No outcome recorded."
        resolved = any(event["resolution_status"] == "resolved" for event in events)
        category = Counter(event["category"] for event in customer_events or events).most_common(1)[0][0]
        return {
            "organization_scope": scope, "mobile_no": mobile_no,
            "session_id": events[0]["session_id"], "started_at": events[0]["timestamp"],
            "ended_at": events[-1]["timestamp"], "message_count": len(events), "category": category,
            "resolution_status": "resolved" if resolved else "not_recorded",
            "summary": f"Issue: {issue} Action: {action} Outcome: {outcome}",
        }

    @staticmethod
    def _empty_profile(scope: str, mobile_no: str) -> dict[str, Any]:
        return {
            "organization_scope": scope, "mobile_no": mobile_no,
            "current_issue": "No recent issue recorded.", "status": "not_recorded",
            "recent_contacts": 0, "previous_action": "No previous support action is recorded.",
            "recommended_next_action": "Ask how the customer can be helped.",
            "previous_session_count": 0, "profile_summary": "No 30-day profile is available.",
            "memory_count": 0, "session_summaries": [], "has_older_sessions": False,
            "cache": {"hit": False, "backend": "none"},
        }

    def dashboard(self, organization_scope: str, days: int = 30) -> dict[str, Any]:
        days = min(max(int(days), 1), 365)
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._connect() as db:
            org = db.execute(
                "SELECT * FROM organizations WHERE organization_scope=?", (organization_scope,)
            ).fetchone()
            if not org:
                raise ValueError("Unknown organization dashboard")
            rows = db.execute(
                """SELECT * FROM memory_events
                WHERE organization_scope=? AND timestamp>=? ORDER BY timestamp DESC""",
                (organization_scope, since),
            ).fetchall()
        events = [dict(row) for row in rows]
        customer_events = [row for row in events if row["role"] != "assistant"]
        sessions: dict[str, list[dict[str, Any]]] = {}
        for row in events:
            sessions.setdefault(row["session_id"], []).append(row)
        resolved_sessions = sum(
            any(event["resolution_status"] == "resolved" for event in session)
            for session in sessions.values()
        )
        category_counts = Counter(row["category"] for row in customer_events)
        sentiment_counts = Counter(row["sentiment"] for row in customer_events)
        daily_counts = Counter(row["timestamp"][:10] for row in customer_events)
        unresolved = [
            session for session in sessions.values()
            if not any(event["resolution_status"] == "resolved" for event in session)
        ]
        total_sessions = len(sessions)
        return {
            "organization": dict(org),
            "window_days": days,
            "kpis": {
                "unique_customers": len({row["mobile_no"] for row in events}),
                "sessions": total_sessions,
                "memory_events": len(events),
                "resolution_rate": round(100 * resolved_sessions / total_sessions, 1) if total_sessions else 0,
                "unresolved_sessions": len(unresolved),
            },
            "categories": [{"name": key, "count": value} for key, value in category_counts.most_common()],
            "sentiment": [{"name": key, "count": value} for key, value in sentiment_counts.most_common()],
            "daily_volume": [{"date": key, "count": daily_counts[key]} for key in sorted(daily_counts)],
            "top_unresolved": [
                {
                    "session_id": session[0]["session_id"],
                    "mobile_no": session[0]["mobile_no"],
                    "category": Counter(item["category"] for item in session).most_common(1)[0][0],
                    "latest_text": session[0]["text"][:180],
                    "latest_timestamp": session[0]["timestamp"],
                }
                for session in unresolved[:10]
            ],
        }

    def list_organizations(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM organizations ORDER BY organization_name")]

    def _industry(self, scope: str) -> str:
        with self._connect() as db:
            row = db.execute(
                "SELECT industry FROM organizations WHERE organization_scope=?", (scope,)
            ).fetchone()
        return str(row["industry"]) if row else "generic"


def classify_category(text: str, industry: str) -> str:
    for name, pattern in TAXONOMIES.get(industry, TAXONOMIES["generic"]):
        if re.search(pattern, text, re.I):
            return name
    return "Other"


def classify_sentiment(text: str) -> str:
    if NEGATIVE_RE.search(text):
        return "negative"
    if POSITIVE_RE.search(text):
        return "positive"
    return "neutral"


def concise_issue(text: str, limit: int = 110) -> str:
    first_sentence = re.split(r"(?<=[.!?])\s+", text.strip(), maxsplit=1)[0]
    if len(first_sentence) <= limit:
        return first_sentence
    return first_sentence[:limit].rsplit(" ", 1)[0] + "…"


def recommended_action(category: str, status: str, industry: str) -> str:
    if status == "resolved":
        return "Confirm the resolution remains stable and close the follow-up if appropriate."
    actions = {
        "telecom": {
            "Connectivity": "Check diagnostics and monitoring results, then escalate to the network team.",
            "Billing": "Review the billing case and confirm the adjustment or expected completion date.",
            "Plan & Upgrade": "Verify plan provisioning and confirm the effective package with the customer.",
            "Installation": "Check technician scheduling and provide a confirmed installation appointment.",
        },
        "fintech": {
            "Payments": "Review the payment dispute status and confirm the refund or settlement timeline.",
            "Transfers": "Check the transfer trace and escalate to the banking partner if still pending.",
            "Disputes & Fraud": "Prioritize the fraud case, confirm account security, and provide an investigation update.",
            "KYC & Account": "Review verification evidence and route the case for manual KYC approval.",
        },
        "healthcare": {
            "Respiratory": "Confirm clinical triage and the next available respiratory appointment.",
            "Cardiology": "Confirm clinical triage and cardiology follow-up scheduling.",
            "Dermatology": "Provide the earliest appointment update and escalation path.",
            "Medication": "Confirm prescription authorization and safe refill completion.",
            "Appointments": "Confirm the appointment owner, date, and follow-up requirements.",
            "Billing & Insurance": "Review the claim documents and provide the next insurance action.",
        },
    }
    return actions.get(industry, {}).get(
        category, "Review the latest case status and assign the correct specialist team."
    )
