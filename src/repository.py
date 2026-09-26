from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    @contextmanager
    def locked(self):
        with self._lock:
            yield

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS escalation_confirmations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    estimated_quantity REAL NOT NULL,
                    threshold REAL NOT NULL,
                    severity TEXT NOT NULL,
                    criterion TEXT NOT NULL,
                    item_version INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','invalidated')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    invalidated_reason TEXT,
                    invalidated_by TEXT,
                    invalidated_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_active_escalation_confirmation
                    ON escalation_confirmations(item_id)
                    WHERE status='active';
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _confirmation(row: sqlite3.Row) -> Dict[str, Any]:
        confirmation = dict(row)
        confirmation["criterion"] = json.loads(confirmation["criterion"])
        return confirmation

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def get_latest_confirmation(self, item_id: int) -> Optional[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            row = self.conn.execute(
                """SELECT c.* FROM escalation_confirmations c
                   JOIN (SELECT item_id, MAX(id) AS id FROM escalation_confirmations
                         WHERE item_id=? GROUP BY item_id) latest
                   ON latest.id=c.id""",
                (item_id,),
            ).fetchone()
        return self._confirmation(row) if row else None

    def latest_confirmation_map(self) -> Dict[int, Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT c.* FROM escalation_confirmations c
                   JOIN (SELECT item_id, MAX(id) AS id
                         FROM escalation_confirmations GROUP BY item_id) latest
                   ON latest.id=c.id"""
            ).fetchall()
        return {int(row["item_id"]): self._confirmation(row) for row in rows}

    def create_escalation_confirmation(self, item_id: int, estimated_quantity: float,
                                       threshold: float, severity: str,
                                       criterion: dict, item_version: int,
                                       actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM items WHERE id=?", (item_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("项目不存在")
            if int(row["version"]) != item_version:
                raise ConflictError("版本冲突，请刷新后重试")
            if row["status"] != "assessing":
                raise ConflictError("只有评估中的事件可以提交升级确认")
            try:
                cur = self.conn.execute(
                    """INSERT INTO escalation_confirmations(item_id, estimated_quantity,
                       threshold, severity, criterion, item_version, status, created_by,
                       created_at) VALUES(?,?,?,?,?,?, 'active', ?,?)""",
                    (item_id, estimated_quantity, threshold, severity,
                     json.dumps(criterion, ensure_ascii=False, sort_keys=True),
                     item_version, actor, now),
                )
                confirmation_id = int(cur.lastrowid)
            except sqlite3.IntegrityError as exc:
                raise ConflictError("已有有效升级确认，请勿重复提交") from exc
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM escalation_confirmations WHERE id=?", (confirmation_id,)
            ).fetchone()
        return self._confirmation(row)

    def correct_estimate(self, item_id: int, quantity: float, expected_version: int,
                         invalidated_reason: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            current = self.conn.execute(
                "SELECT * FROM items WHERE id=?", (item_id,)
            ).fetchone()
            if current is None:
                raise NotFoundError("项目不存在")
            if int(current["version"]) != expected_version:
                raise ConflictError("版本冲突，请刷新后重试")
            if current["status"] not in ("reported", "assessing"):
                raise ConflictError("事件已进入围控，不能再更正估算油量")
            cur = self.conn.execute(
                """UPDATE items SET quantity=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (quantity, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                raise ConflictError("版本冲突，请刷新后重试")
            old_rows = self.conn.execute(
                "SELECT id FROM escalation_confirmations WHERE item_id=? AND status='active'",
                (item_id,),
            ).fetchall()
            invalidated_ids = [int(row["id"]) for row in old_rows]
            self.conn.execute(
                """UPDATE escalation_confirmations
                   SET status='invalidated', invalidated_reason=?, invalidated_by=?,
                       invalidated_at=?
                   WHERE item_id=? AND status='active'""",
                (invalidated_reason, actor, now, item_id),
            )
        item = self.get_item(item_id)
        confirmations = []
        if invalidated_ids:
            with self._lock:
                rows = self.conn.execute(
                    f"SELECT * FROM escalation_confirmations WHERE id IN ({','.join('?' for _ in invalidated_ids)}) ORDER BY id",
                    invalidated_ids,
                ).fetchall()
            confirmations = [self._confirmation(row) for row in rows]
        return {"item": item, "invalidated_confirmations": confirmations}

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
