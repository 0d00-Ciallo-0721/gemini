import json
import os
import time
import threading
import tempfile
import sqlite3
from pathlib import Path
from typing import Any, Dict
from .auth_status import AuthStatus

def atomic_write_json(path: Path | str, data: Any):
    """
    通用安全 JSON 文件写入器。
    通过系统级的临时文件创建与 os.replace 进行原子替换，
    用以屏蔽任何进程崩溃、断电、外部中止导致的 JSON 文件被写一半破坏的问题。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=p.parent, prefix=".tmp_", suffix=".json", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, p)
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise e

class TicketStore:
    """
    底层认证长效存储仓库。
    统一职责：
    1. Active Ticket 存取（现已剥离至 SQLite 保障强一致性）。
    2. Nonce 去重（防重放重贴攻击）。
    3. 事件审计日志留痕。
    4. 历史版 JSON 向 SQLite 迁移。
    """
    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.auth_runtime_path = self.data_dir / "auth_runtime.json"
        self.auth_history_path = self.data_dir / "logs" / "auth_history.jsonl"
        self.db_path = self.data_dir / "auth_repo.sqlite"
        self._lock = threading.Lock()
        self._ensure_dir()
        self._init_db()
        self._migrate_json_to_sqlite()

    def _ensure_dir(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.auth_history_path.parent.mkdir(parents=True, exist_ok=True)

    def _init_db(self):
        """初始化底层的 SQLite 表结构。保证热重载的高并发安全与原子级操作。"""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''CREATE TABLE IF NOT EXISTS active_ticket (
                                id INTEGER PRIMARY KEY CHECK (id = 1),
                                data TEXT NOT NULL
                            )''')
                conn.execute('''CREATE TABLE IF NOT EXISTS used_nonces (
                                nonce TEXT PRIMARY KEY,
                                expires_at REAL NOT NULL
                            )''')
                conn.commit()

    def _migrate_json_to_sqlite(self):
        """
        [旧版本兼容] JSON 到 SQLite 的平滑迁移逻辑。
        将原本处于 auth_runtime.json 中的松散核心凭据吸纳进受保护的 db 中。
        """
        with self._lock:
            if self.auth_runtime_path.exists():
                try:
                    ticket = json.loads(self.auth_runtime_path.read_text(encoding="utf-8"))
                    with sqlite3.connect(self.db_path) as conn:
                        conn.execute("INSERT OR IGNORE INTO active_ticket (id, data) VALUES (1, ?)", 
                                    (json.dumps(ticket, ensure_ascii=False),))
                        conn.commit()
                    self.auth_runtime_path.rename(self.data_dir / "auth_runtime.json.bak")
                except Exception:
                    pass

    def load_active_ticket(self) -> Dict[str, Any] | None:
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("SELECT data FROM active_ticket WHERE id = 1")
                row = cursor.fetchone()
                if row:
                    ticket = json.loads(row[0])
                    if "schema_version" not in ticket:
                        ticket["schema_version"] = "1.0"
                    return ticket
            return None

    def save_active_ticket(self, payload: Dict[str, Any]):
        payload["schema_version"] = "1.0"
        payload["updated_at"] = time.time()
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("INSERT OR REPLACE INTO active_ticket (id, data) VALUES (1, ?)", 
                             (json.dumps(payload, ensure_ascii=False),))
                conn.commit()

    def invalidate_ticket(self):
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("SELECT data FROM active_ticket WHERE id = 1")
                row = cursor.fetchone()
                if row:
                    ticket = json.loads(row[0])
                    ticket["status"] = AuthStatus.INVALIDATED.value
                    conn.execute("INSERT OR REPLACE INTO active_ticket (id, data) VALUES (1, ?)", 
                                 (json.dumps(ticket, ensure_ascii=False),))
                    conn.commit()

    def is_nonce_used(self, nonce: str) -> bool:
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("SELECT 1 FROM used_nonces WHERE nonce = ?", (nonce,))
                return cursor.fetchone() is not None

    def mark_nonce_used(self, nonce: str, expire_seconds: int = 300):
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("INSERT OR IGNORE INTO used_nonces (nonce, expires_at) VALUES (?, ?)", 
                             (nonce, time.time() + expire_seconds))
                conn.execute("DELETE FROM used_nonces WHERE expires_at < ?", (time.time(),))
                conn.commit()

    def log_event(self, event_type: str, details: Dict[str, Any]):
        """
        持久化行为审计记录。
        【核心安全要求】：对任何存入此日志中的 cookie、身份标识特征进行截断脱敏，
        绝不允许完整原始 Cookie 裸露在物理文本当中。
        """
        entry = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "type": event_type,
            **details
        }
        # 脱敏处理
        if "raw_cookie" in entry:
            entry["raw_cookie"] = "***MASKED***"
        if "SECURE_1PSID" in entry:
            entry["SECURE_1PSID"] = str(entry["SECURE_1PSID"])[:10] + "***"
        if "cookie_data" in entry and isinstance(entry["cookie_data"], dict):
            safe_cookie_data = {}
            for k, v in entry["cookie_data"].items():
                if k in ("SECURE_1PSID", "SECURE_1PSIDTS", "__Secure-1PSID", "__Secure-1PSIDTS"):
                    safe_cookie_data[k] = str(v)[:10] + "***"
                elif k == "cookies_dict":
                    safe_cookie_data[k] = list(v.keys()) if isinstance(v, dict) else "***MASKED***"
                else:
                    safe_cookie_data[k] = v
            entry["cookie_data"] = safe_cookie_data
            
        try:
            with self._lock:
                with open(self.auth_history_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass
