"""콘솔 토폴로지용 실시간 상태. 각 항목은 실패해도 None으로 두고 나머지를 돌려준다."""

import os
from pathlib import Path

from . import infra

APP_URL = os.environ.get("APP_URL", "http://app:8080")
WRITER_URL = os.environ.get("WRITER_URL", "http://writer:8090")
PGBOUNCER_TARGET = Path(os.environ.get("PGBOUNCER_TARGET", "/pgbouncer-target/databases.ini"))


def _safe(fn):
    try:
        return fn()
    except Exception:
        return None


def _db(side: str) -> dict:
    with infra.pg(side) as conn:
        rows = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        seq = conn.execute("SELECT last_value FROM messages_id_seq").fetchone()[0]
        subs = [r[0] for r in conn.execute("SELECT subname FROM pg_subscription WHERE subenabled")]
        slots = conn.execute(
            "SELECT slot_name, active, pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)"
            " FROM pg_replication_slots WHERE slot_type = 'logical'").fetchall()
        can_write = conn.execute(
            "SELECT has_table_privilege('chat_app', 'messages', 'INSERT')").fetchone()[0]
    return {"rows": rows, "seq": seq, "subscriptions": subs, "app_can_write": can_write,
            "slots": [{"name": s[0], "active": s[1], "lag_bytes": int(s[2] or 0)} for s in slots]}


def _pgbouncer_target() -> str | None:
    text = PGBOUNCER_TARGET.read_text()
    for part in text.split():
        if part.startswith("host="):
            return part.removeprefix("host=")
    return None


def snapshot() -> dict:
    return {
        "source": {"db": _safe(lambda: _db("source")),
                   "anonymous_http": infra.anonymous_get("source")},
        "target": {"db": _safe(lambda: _db("target")),
                   "anonymous_http": infra.anonymous_get("target")},
        "pgbouncer_target": _safe(_pgbouncer_target),
        "app_db": _safe(lambda: infra.http.get(f"{APP_URL}/healthz", timeout=1.5).json()["db"]),
        "writer": _safe(lambda: infra.http.get(f"{WRITER_URL}/stats", timeout=1.5).json()),
    }


def writer_control(action: str, rate: float | None = None) -> dict:
    body = {"rate": rate} if rate else {}
    return infra.http.post(f"{WRITER_URL}/{action}", json=body).json()
