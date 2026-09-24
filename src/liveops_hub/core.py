"""One-customer review inbox for an installed A2Z Resolution Deploy app."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import subprocess
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol


def _now() -> str:
    return datetime.now(UTC).isoformat()


class AppPort(Protocol):
    def prepare(self, ticket_id: int, locale: str) -> dict: ...
    def prepare_demo(self, locale: str) -> dict: ...
    def review(self, draft_id: str, reviewer: str, approve: bool) -> dict: ...
    def send(self, draft_id: str, ticket_id: int) -> dict: ...
    def outcome(self, draft_id: str, reviewer: str, status: str) -> dict: ...
    def summary(self) -> dict: ...


class InstalledResolutionApp:
    """Runs a verified App Factory installation through argv, never a shell."""

    def __init__(self, target: Path, knowledge: Path, db: Path, subdomain: str,
                 expected_commit: str, demo_ticket: Path | None = None,
                 factory_command: tuple[str, ...] = ("a2z-app",)):
        self.target, self.knowledge, self.db = Path(target).resolve(), Path(knowledge).resolve(), Path(db).resolve()
        self.subdomain, self.expected_commit = subdomain, expected_commit
        self.demo_ticket = Path(demo_ticket).resolve() if demo_ticket else None
        self.factory_command = factory_command
        if not re.fullmatch(r"[0-9a-f]{40}", expected_commit):
            raise ValueError("expected app commit must be a full SHA")
        if not factory_command:
            raise ValueError("factory command required")

    def _invoke(self, args: list[str], *, timeout: int = 30) -> dict:
        try:
            verified = subprocess.run([*self.factory_command, "verify", str(self.target)],
                                      capture_output=True, text=True, timeout=15,
                                      env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        except (subprocess.TimeoutExpired, OSError):
            raise RuntimeError("installed app verification unavailable") from None
        if verified.returncode:
            raise RuntimeError("installed app verification failed")
        try:
            identity = json.loads(verified.stdout)
        except json.JSONDecodeError:
            raise RuntimeError("installed app verifier returned invalid JSON") from None
        if identity.get("app_id") != "a2z-resolution-deploy" or identity.get("source_commit") != self.expected_commit:
            raise RuntimeError("installed app identity differs from configured revision")
        command = [*self.factory_command, "run", str(self.target), "--", "--knowledge", str(self.knowledge),
                   "--db", str(self.db), *args]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        except (subprocess.TimeoutExpired, OSError):
            raise RuntimeError("app command timed out; reconcile state before retry") from None
        if result.returncode:
            raise RuntimeError("app command failed; inspect the local application state before retry")
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise RuntimeError("app command returned invalid JSON; reconcile state before retry") from None
        if not isinstance(value, dict):
            raise RuntimeError("app command returned invalid result")
        return value

    def prepare(self, ticket_id: int, locale: str) -> dict:
        if not self.subdomain:
            raise ValueError("Zendesk subdomain is not configured")
        return self._invoke(["fetch-prepare", "--ticket-id", str(ticket_id), "--subdomain", self.subdomain,
                             "--locale", locale])

    def prepare_demo(self, locale: str) -> dict:
        if self.demo_ticket is None:
            raise ValueError("synthetic demo ticket is not configured")
        return self._invoke(["prepare", "--ticket-file", str(self.demo_ticket), "--locale", locale])

    def review(self, draft_id: str, reviewer: str, approve: bool) -> dict:
        return self._invoke(["review", "--draft-id", draft_id, "--reviewer", reviewer,
                             "--decision", "approve" if approve else "reject"])

    def send(self, draft_id: str, ticket_id: int) -> dict:
        if not self.subdomain:
            raise ValueError("Zendesk subdomain is not configured")
        return self._invoke(["send", "--draft-id", draft_id, "--ticket-id", str(ticket_id),
                             "--subdomain", self.subdomain], timeout=45)

    def outcome(self, draft_id: str, reviewer: str, status: str) -> dict:
        return self._invoke(["outcome", "--draft-id", draft_id, "--reviewer", reviewer, "--status", status])

    def summary(self) -> dict:
        return self._invoke(["summary"])


class LiveOpsHub:
    """Local state machine; no authenticated customer identity or billable outcomes."""

    def __init__(self, db: Path, customer_key: str, port: AppPort, reviewer_label: str, sender_label: str):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,63}", customer_key):
            raise ValueError("customer_key must be a pseudonymous slug")
        if not reviewer_label.strip() or not sender_label.strip():
            raise ValueError("reviewer and sender labels required")
        self.db, self.customer_key, self.port = Path(db), customer_key, port
        self.reviewer_label, self.sender_label = reviewer_label.strip(), sender_label.strip()
        created_parent = not self.db.parent.exists()
        self.db.parent.mkdir(parents=True, exist_ok=True)
        if created_parent:
            os.chmod(self.db.parent, 0o700)
        parent_stat = self.db.parent.stat()
        if parent_stat.st_uid != os.getuid() or stat.S_IMODE(parent_stat.st_mode) & 0o077:
            raise ValueError("hub database directory must be owned by this user and mode 0700")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS scope (customer_key TEXT PRIMARY KEY)")
            conn.execute("""CREATE TABLE IF NOT EXISTS drafts (
                draft_id TEXT PRIMARY KEY, ticket_id INTEGER NOT NULL, locale TEXT NOT NULL,
                answer TEXT NOT NULL, source_url TEXT NOT NULL, article_id TEXT NOT NULL,
                source_stamp TEXT NOT NULL, status TEXT NOT NULL, outcome TEXT,
                is_demo INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id TEXT, kind TEXT NOT NULL,
                actor TEXT NOT NULL, at TEXT NOT NULL
            )""")
            row = conn.execute("SELECT customer_key FROM scope").fetchone()
            if row is None:
                conn.execute("INSERT INTO scope VALUES (?)", (customer_key,))
            elif row[0] != customer_key:
                raise ValueError("hub database belongs to a different customer scope")
        os.chmod(self.db, 0o600)

    def _event(self, conn: sqlite3.Connection, draft_id: str | None, kind: str, actor: str) -> None:
        conn.execute("INSERT INTO events(draft_id,kind,actor,at) VALUES (?,?,?,?)", (draft_id, kind, actor, _now()))

    def _row(self, draft_id: str) -> dict:
        with closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
        if row is None:
            raise ValueError("draft not found")
        return dict(row)

    def _prepare(self, result: dict, ticket_id: int, locale: str, *, is_demo: bool = False) -> dict:
        if result.get("status") == "ESCALATE":
            with closing(sqlite3.connect(self.db)) as conn, conn:
                self._event(conn, None, "ESCALATED", "engine")
            return {"status": "ESCALATE", "reason": result.get("reason"),
                    "customer_acceptance": "NOT_MEASURED"}
        if result.get("status") not in ("PENDING_REVIEW", "APPROVED_PENDING_SEND", "REJECTED", "SENT_UNVERIFIED") or not all(
            isinstance(result.get(key), str) and result[key] for key in
            ("draft_id", "answer", "source", "article_id", "source_stamp")):
            raise ValueError("app did not return a new reviewable draft")
        if result["status"] != "PENDING_REVIEW":
            old = self._row(result["draft_id"])
            if old["ticket_id"] != ticket_id or old["source_stamp"] != result["source_stamp"] or old["is_demo"] != int(is_demo):
                raise ValueError("draft identity collision")
            return {"draft_id": result["draft_id"], "status": old["status"],
                    "claim_boundary": "Existing local draft; no new action taken."}
        with closing(sqlite3.connect(self.db)) as conn, conn:
            try:
                conn.execute("INSERT INTO drafts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (result["draft_id"], ticket_id, locale, result["answer"], result["source"],
                              result["article_id"], result["source_stamp"], "PENDING_REVIEW", None, int(is_demo), _now(), _now()))
                self._event(conn, result["draft_id"], "PREPARED", "engine")
            except sqlite3.IntegrityError:
                old = self._row(result["draft_id"])
                if old["ticket_id"] != ticket_id or old["source_stamp"] != result["source_stamp"] or old["is_demo"] != int(is_demo):
                    raise ValueError("draft identity collision") from None
        current = self._row(result["draft_id"])
        return {"draft_id": result["draft_id"], "status": current["status"],
                "claim_boundary": "Draft not sent or accepted by a customer."}

    def import_ticket(self, ticket_id: int, locale: str) -> dict:
        if type(ticket_id) is not int or ticket_id < 1 or locale not in ("ar", "en"):
            raise ValueError("positive ticket_id and ar/en locale required")
        return self._prepare(self.port.prepare(ticket_id, locale), ticket_id, locale)

    def import_demo(self, locale: str) -> dict:
        if locale not in ("ar", "en"):
            raise ValueError("ar/en locale required")
        result = self.port.prepare_demo(locale)
        # The demo uses one fixed invented ticket ID, never a live integration.
        return self._prepare(result, 101, locale, is_demo=True)

    def drafts(self) -> list[dict]:
        with closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM drafts ORDER BY created_at DESC, draft_id").fetchall()
        return [dict(row) for row in rows]

    def _reserve(self, draft_id: str, expected: str, interim: str, actor: str) -> dict:
        row = self._row(draft_id)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            changed = conn.execute("UPDATE drafts SET status=?,updated_at=? WHERE draft_id=? AND status=?",
                                   (interim, _now(), draft_id, expected)).rowcount
            if not changed:
                raise ValueError(f"draft is not in {expected} state")
            self._event(conn, draft_id, interim, actor)
        return row

    def _finish(self, draft_id: str, interim: str, status: str, actor: str, *, outcome: str | None = None) -> None:
        with closing(sqlite3.connect(self.db)) as conn, conn:
            changed = conn.execute("UPDATE drafts SET status=?,outcome=COALESCE(?,outcome),updated_at=? WHERE draft_id=? AND status=?",
                                   (status, outcome, _now(), draft_id, interim)).rowcount
            if not changed:
                raise RuntimeError("local state changed unexpectedly; reconcile before another action")
            self._event(conn, draft_id, status, actor)

    def review(self, draft_id: str, approve: bool) -> dict:
        if type(approve) is not bool:
            raise ValueError("approve must be boolean")
        self._reserve(draft_id, "PENDING_REVIEW", "REVIEW_IN_PROGRESS", self.reviewer_label)
        try:
            result = self.port.review(draft_id, self.reviewer_label, approve)
            expected = "APPROVED_PENDING_SEND" if approve else "REJECTED"
            if result.get("status") != expected or result.get("draft_id") != draft_id:
                raise ValueError("app review result differs from requested decision")
        except Exception:
            self._finish(draft_id, "REVIEW_IN_PROGRESS", "REVIEW_UNCERTAIN_RECONCILE", self.reviewer_label)
            raise
        self._finish(draft_id, "REVIEW_IN_PROGRESS", expected, self.reviewer_label)
        return {"draft_id": draft_id, "status": expected}

    def send(self, draft_id: str) -> dict:
        if self._row(draft_id)["is_demo"]:
            raise ValueError("synthetic demo drafts cannot be sent")
        row = self._reserve(draft_id, "APPROVED_PENDING_SEND", "SENDING", self.sender_label)
        try:
            result = self.port.send(draft_id, row["ticket_id"])
            if result.get("status") != "SENT_UNVERIFIED" or result.get("draft_id") != draft_id:
                raise ValueError("app send result differs from requested draft")
        except Exception:
            self._finish(draft_id, "SENDING", "UNCERTAIN_RECONCILE", self.sender_label)
            raise
        self._finish(draft_id, "SENDING", "SENT_UNVERIFIED", self.sender_label)
        return {"draft_id": draft_id, "status": "SENT_UNVERIFIED", "customer_acceptance": "NOT_MEASURED"}

    def outcome(self, draft_id: str, status: str) -> dict:
        if status not in ("ACCEPTED", "REWORK", "UNRESOLVED"):
            raise ValueError("invalid outcome")
        self._reserve(draft_id, "SENT_UNVERIFIED", "OUTCOME_IN_PROGRESS", self.reviewer_label)
        try:
            result = self.port.outcome(draft_id, self.reviewer_label, status)
            if result.get("outcome") != status or result.get("draft_id") != draft_id:
                raise ValueError("app outcome differs from requested status")
        except Exception:
            self._finish(draft_id, "OUTCOME_IN_PROGRESS", "OUTCOME_UNCERTAIN_RECONCILE", self.reviewer_label)
            raise
        self._finish(draft_id, "OUTCOME_IN_PROGRESS", "OUTCOME_RECORDED", self.reviewer_label, outcome=status)
        return {"draft_id": draft_id, "outcome": status,
                "evidence_class": "OPERATOR_DECLARED_UNVERIFIED"}

    def summary(self) -> dict:
        with closing(sqlite3.connect(self.db)) as conn:
            states = dict(conn.execute("SELECT status,COUNT(*) FROM drafts GROUP BY status").fetchall())
            outcomes = dict(conn.execute("SELECT outcome,COUNT(*) FROM drafts WHERE outcome IS NOT NULL GROUP BY outcome").fetchall())
        return {"customer_key": self.customer_key, "draft_states": states,
                "operator_declared_outcomes": outcomes, "verified_accepted_resolutions": None,
                "claim_boundary": "One local customer scope; self-declared outcomes are not authenticated customer resolutions."}

    def events(self, limit: int = 100) -> list[dict]:
        if not 1 <= limit <= 500:
            raise ValueError("event limit out of range")
        with closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT event_id,draft_id,kind,actor,at FROM events ORDER BY event_id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]
