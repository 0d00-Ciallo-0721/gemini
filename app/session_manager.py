import json
import os
import sqlite3
import time
from typing import Any, Dict, Optional

from .config import get_runtime_config


class SessionManager:
    """Persist logical session id -> Gemini physical chat metadata mapping."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or self._default_db_path()
        self._active_sessions: Dict[str, Dict[str, Any]] = {}
        self._ensure_db()

    def _default_db_path(self) -> str:
        runtime_config = get_runtime_config()
        db_path = str(runtime_config.get("session_db_path") or "reverse_sessions.sqlite3")
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        return db_path

    def set_db_path(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._ensure_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _ensure_db(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reverse_sessions (
                    session_id TEXT PRIMARY KEY,
                    chat_metadata TEXT NOT NULL DEFAULT '',
                    last_msg_idx INTEGER NOT NULL DEFAULT 0,
                    parent_session_id TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    agent_type TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.commit()

    def _deserialize_metadata(self, raw: str) -> list[Any] | None:
        raw_text = str(raw or "").strip()
        if not raw_text:
            return None
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, list) else None

    def _serialize_metadata(self, metadata: Any) -> str:
        if not metadata:
            return ""
        try:
            return json.dumps(metadata, ensure_ascii=False)
        except TypeError:
            return ""

    def _row_to_record(self, row: sqlite3.Row | tuple | None) -> Dict[str, Any] | None:
        if not row:
            return None
        if not isinstance(row, sqlite3.Row):
            row = {
                "session_id": row[0],
                "chat_metadata": row[1],
                "last_msg_idx": row[2],
                "parent_session_id": row[3],
                "model": row[4],
                "agent_type": row[5],
                "status": row[6],
                "created_at": row[7],
                "updated_at": row[8],
            }
        return {
            "session_id": row["session_id"],
            "chat": None,
            "chat_metadata": self._deserialize_metadata(row["chat_metadata"]),
            "last_msg_idx": int(row["last_msg_idx"] or 0),
            "parent_session_id": row["parent_session_id"] or "",
            "model": row["model"] or "",
            "agent_type": row["agent_type"] or "",
            "status": row["status"] or "active",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _upsert_record(self, record: Dict[str, Any]):
        now = time.time()
        created_at = float(record.get("created_at") or now)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO reverse_sessions (
                    session_id, chat_metadata, last_msg_idx, parent_session_id,
                    model, agent_type, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    chat_metadata=excluded.chat_metadata,
                    last_msg_idx=excluded.last_msg_idx,
                    parent_session_id=excluded.parent_session_id,
                    model=excluded.model,
                    agent_type=excluded.agent_type,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (
                    record["session_id"],
                    self._serialize_metadata(record.get("chat_metadata")),
                    int(record.get("last_msg_idx") or 0),
                    record.get("parent_session_id", "") or "",
                    record.get("model", "") or "",
                    record.get("agent_type", "") or "",
                    record.get("status", "active") or "active",
                    created_at,
                    now,
                ),
            )
            conn.commit()
        record["created_at"] = created_at
        record["updated_at"] = now

    def _fetch_record(self, session_id: str) -> Dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT session_id, chat_metadata, last_msg_idx, parent_session_id,
                       model, agent_type, status, created_at, updated_at
                FROM reverse_sessions
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        return self._row_to_record(row)

    def get_session(self, session_id: str) -> Dict[str, Any] | None:
        if not session_id:
            return None
        cached = self._active_sessions.get(session_id)
        if cached:
            return cached
        loaded = self._fetch_record(session_id)
        if loaded:
            self._active_sessions[session_id] = loaded
        return loaded

    def create_or_reset_session(
        self,
        session_id: str,
        chat_session: Any,
        *,
        last_msg_idx: int = 0,
        parent_session_id: str = "",
        model: str = "",
        agent_type: str = "",
        status: str = "active",
    ) -> Dict[str, Any]:
        record = self.get_session(session_id) or {
            "session_id": session_id,
            "created_at": time.time(),
        }
        record.update(
            {
                "chat": chat_session,
                "chat_metadata": getattr(chat_session, "metadata", None) if chat_session else None,
                "last_msg_idx": int(last_msg_idx or 0),
                "parent_session_id": parent_session_id or "",
                "model": model or record.get("model", "") or "",
                "agent_type": agent_type or record.get("agent_type", "") or "",
                "status": status or "active",
            }
        )
        self._active_sessions[session_id] = record
        self._upsert_record(record)
        return record

    def remove_session(self, session_id: str) -> bool:
        if session_id in self._active_sessions:
            del self._active_sessions[session_id]
        if not session_id:
            return False
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM reverse_sessions WHERE session_id = ?", (session_id,))
            conn.commit()
            return cursor.rowcount > 0

    def update_last_msg_idx(self, session_id: str, new_idx: int):
        record = self.get_session(session_id)
        if not record:
            return
        record["last_msg_idx"] = int(new_idx or 0)
        if record.get("chat") is not None and hasattr(record["chat"], "metadata"):
            record["chat_metadata"] = getattr(record["chat"], "metadata", None)
        self._upsert_record(record)

    def update_chat_session(self, session_id: str, new_chat_session: Any):
        record = self.get_session(session_id)
        if not record:
            return
        record["chat"] = new_chat_session
        record["chat_metadata"] = getattr(new_chat_session, "metadata", None) if new_chat_session else None
        self._upsert_record(record)

    def persist_live_session(
        self,
        session_id: str,
        chat_session: Any,
        *,
        last_msg_idx: Optional[int] = None,
        model: str = "",
        parent_session_id: str = "",
        agent_type: str = "",
        status: str = "active",
    ) -> Dict[str, Any]:
        record = self.get_session(session_id) or {
            "session_id": session_id,
            "created_at": time.time(),
        }
        record["chat"] = chat_session
        record["chat_metadata"] = getattr(chat_session, "metadata", None) if chat_session else None
        if last_msg_idx is not None:
            record["last_msg_idx"] = int(last_msg_idx or 0)
        record["model"] = model or record.get("model", "") or ""
        record["parent_session_id"] = parent_session_id or record.get("parent_session_id", "") or ""
        record["agent_type"] = agent_type or record.get("agent_type", "") or ""
        record["status"] = status or record.get("status", "active") or "active"
        self._active_sessions[session_id] = record
        self._upsert_record(record)
        return record

    def get_or_restore_chat_session(
        self,
        session_id: str,
        client: Any,
        *,
        model: str = "",
        parent_session_id: str = "",
        agent_type: str = "",
    ) -> tuple[Any, bool]:
        record = self.get_session(session_id)
        if record and record.get("chat") is not None:
            return record["chat"], False

        if client is None:
            raise RuntimeError("Gemini client is not ready")

        restored = False
        chat_session = None
        metadata = (record or {}).get("chat_metadata")
        if metadata:
            try:
                chat_session = client.start_chat(metadata=metadata, model=model)
                restored = True
            except Exception:
                chat_session = None

        if chat_session is None:
            chat_session = client.start_chat(model=model)

        last_msg_idx = int((record or {}).get("last_msg_idx") or 0)
        self.create_or_reset_session(
            session_id,
            chat_session,
            last_msg_idx=last_msg_idx,
            parent_session_id=parent_session_id or (record or {}).get("parent_session_id", ""),
            model=model or (record or {}).get("model", ""),
            agent_type=agent_type or (record or {}).get("agent_type", ""),
            status="active",
        )
        return chat_session, restored

    def has_parent_session(self, parent_id: str) -> bool:
        if not parent_id:
            return False
        if parent_id in self._active_sessions:
            return True
        return self._fetch_record(parent_id) is not None


session_manager = SessionManager()
