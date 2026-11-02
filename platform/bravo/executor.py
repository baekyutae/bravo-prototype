"""Cutover Executor (설계 문서 8.6을 PgBouncer 1개 기준으로 줄임). LLM을 쓰지 않는다.

같은 순서를 방향만 바꿔 컷오버(출발지 → 목적지)와 되돌리기(목적지 → 출발지)에 쓴다.
1~5단계에서 실패하면 쓰기 전이므로 출발지를 복원한다(ABORT_PRE_WRITE).
6단계(전환 확정) 이후 실패는 되돌리기 대상이다(FAILBACK).
"""

import os
import threading
import time
from pathlib import Path

import psycopg

from . import infra
from .events import bus
from .gate import MAX_LAG_BYTES
from .state import Plan

PGBOUNCER_ADMIN = os.environ.get("PGBOUNCER_ADMIN_DSN", "postgresql://postgres:postgres@pgbouncer:6432/pgbouncer")
APP_VIA_BOUNCER = os.environ.get("APP_VIA_BOUNCER_DSN", "postgresql://chat_app:chat_app_pw@pgbouncer:6432/chat")
TARGET_FILE = Path(os.environ.get("PGBOUNCER_TARGET", "/pgbouncer-target/databases.ini"))
HOST = {"source": "pg-src", "target": "pg-dst"}
PAUSE_DEADLINE_S = 5.0
BARRIER_DEADLINE_S = 10.0

# 방향별 설정: 누가 쓰기를 넘겨주고(from) 받는지(to), 어느 구독을 끊고 어느 구독을 새로 만드는지
DIRECTIONS = {
    "cutover": {"from": "source", "to": "target", "drop_sub": "bravo_fwd", "new_sub": "bravo_rev"},
    "failback": {"from": "target", "to": "source", "drop_sub": "bravo_rev", "new_sub": None},
}


class StepFailed(Exception):
    pass


def _admin(sql: str) -> None:
    with psycopg.connect(PGBOUNCER_ADMIN, autocommit=True, connect_timeout=3) as conn:
        conn.execute(sql)


def _set_bouncer_target(side: str) -> None:
    tmp = TARGET_FILE.with_suffix(".tmp")
    tmp.write_text(f"chat = host={HOST[side]} port=5432 dbname=chat\n")
    tmp.replace(TARGET_FILE)
    _admin("RELOAD")


def _lag_bytes(side: str, slot: str) -> int | None:
    with infra.pg(side) as conn:
        row = conn.execute("SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)"
                           " FROM pg_replication_slots WHERE slot_name = %s", (slot,)).fetchone()
    return int(row[0]) if row else None


def _table_receipt(side: str) -> dict:
    with infra.pg(side) as conn:
        conn.execute("SET TimeZone = 'UTC'")
        out = {}
        for table in ("messages", "bravo_barrier"):
            rows, pk, content = conn.execute(
                f"SELECT count(*), md5(coalesce(string_agg(id::text, ',' ORDER BY id), '')),"
                f" md5(coalesce(string_agg(t::text, '|' ORDER BY id), '')) FROM {table} t").fetchone()
            out[table] = {"rows": rows, "pk_hash": pk[:12], "content_hash": content[:12]}
        seqs = dict(conn.execute("SELECT sequencename, coalesce(last_value, 0) FROM pg_sequences"
                                 " WHERE schemaname = 'public'").fetchall())
    return {"tables": out, "sequences": seqs}


def run(plan: Plan, direction: str) -> dict:
    cfg = DIRECTIONS[direction]
    frm, to = cfg["from"], cfg["to"]
    steps: list[dict] = []
    t0 = time.monotonic()
    paused = fenced = committed = False
    result = {"direction": direction, "epoch": plan.epoch, "steps": steps,
              "from": HOST[frm], "to": HOST[to]}

    def step(no: int, name: str, fn):
        started = time.monotonic()
        try:
            detail = fn() or {}
            ok = True
        except Exception as e:
            detail, ok = {"error": f"{type(e).__name__}: {e}"}, False
        ms = round((time.monotonic() - started) * 1000)
        steps.append({"no": no, "name": name, "ok": ok, "ms": ms, "detail": detail})
        bus.publish(plan.id, "executor_step", f"[{direction}] {no}. {name} {'완료' if ok else '실패'} ({ms}ms)",
                    step=direction.upper(), ok=ok, no=no)
        if not ok:
            raise StepFailed(name)
        return detail

    try:
        # 1. 복제 지연 확인
        def check_lag():
            lag = _lag_bytes(frm, cfg["drop_sub"])
            if lag is None or lag > MAX_LAG_BYTES:
                raise RuntimeError(f"복제 지연 {lag} B (기준 {MAX_LAG_BYTES} B)")
            return {"lag_bytes": lag}
        step(1, "복제 지연 확인", check_lag)

        # 2. 쓰기 정지: PAUSE는 진행 중 트랜잭션을 기다린다
        pause_started = time.monotonic()
        def pause():
            nonlocal paused
            paused = True  # 기한을 넘겨도 PAUSE가 걸려 있을 수 있으므로 복원 대상으로 본다
            done: list = []
            th = threading.Thread(target=lambda: (_admin("PAUSE chat"), done.append(1)), daemon=True)
            th.start()
            th.join(PAUSE_DEADLINE_S)
            if not done:
                raise RuntimeError(f"PAUSE가 {PAUSE_DEADLINE_S}s 안에 끝나지 않음 (긴 트랜잭션)")
        step(2, "PgBouncer PAUSE (쓰기 정지 시작)", pause)

        # 3. 넘겨주는 쪽 펜싱: 앱 쓰기 권한 회수 → 기존 세션 종료 → 쓰기 거부 시험
        def fence():
            nonlocal fenced
            with infra.pg(frm) as conn:
                conn.execute("REVOKE INSERT ON messages FROM chat_app")
                conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = 'chat_app'")
            fenced = True
            dsn = infra.DSN[frm].replace("postgres:postgres@", "chat_app:chat_app_pw@")
            try:
                with psycopg.connect(dsn, autocommit=True, connect_timeout=3) as conn:
                    conn.execute("INSERT INTO messages (idem_key, body) VALUES ('fence-test', 'x')")
                raise RuntimeError("펜싱 후에도 업무 쓰기가 성공함")
            except psycopg.errors.InsufficientPrivilege:
                return {"write_rejected": True}
        step(3, f"{HOST[frm]} 펜싱 (업무 쓰기 거부 확인)", fence)

        # 4. barrier: 제어 역할이 표식 행을 쓰고 받는 쪽 도착을 확인 → 이전 쓰기가 모두 복제됨
        def barrier():
            with infra.pg(frm) as conn:
                bid = conn.execute("INSERT INTO bravo_barrier (epoch, direction) VALUES (%s, %s) RETURNING id",
                                   (plan.epoch, direction)).fetchone()[0]
            deadline = time.monotonic() + BARRIER_DEADLINE_S
            while time.monotonic() < deadline:
                with infra.pg(to) as conn:
                    if conn.execute("SELECT 1 FROM bravo_barrier WHERE id = %s", (bid,)).fetchone():
                        return {"barrier_id": bid}
                time.sleep(0.05)
            raise RuntimeError(f"barrier {bid} 미도착")
        step(4, "barrier 도착 확인", barrier)

        # 5. 무결성 영수증 + 시퀀스 보정(올리기만)
        def receipt():
            a, b = _table_receipt(frm), _table_receipt(to)
            mismatch = [t for t in a["tables"] if a["tables"][t] != b["tables"][t]]
            seqs = {}
            with infra.pg(to) as conn:
                for name, last in a["sequences"].items():
                    new = max(last, b["sequences"].get(name, 0))
                    if new > 0:
                        conn.execute("SELECT setval(%s, %s)", (name, new))
                    seqs[name] = {"from_last": last, "to_before": b["sequences"].get(name, 0), "to_set": new}
            rec = {"from": a["tables"], "to": b["tables"], "match": not mismatch, "sequences": seqs}
            result["receipt"] = rec
            if mismatch:
                raise RuntimeError(f"불일치 테이블 {mismatch}")
            return {"match": True}
        step(5, "무결성 영수증 · 시퀀스 보정", receipt)

        # 6. 전환 확정: 기존 구독 제거, 필요하면 반대 방향 구독 생성 (PAUSE 안이라 빠지는 쓰기 없음)
        def commit():
            nonlocal committed
            with infra.pg(to) as conn:
                conn.execute(f"DROP SUBSCRIPTION IF EXISTS {cfg['drop_sub']}")
            if cfg["new_sub"]:
                with infra.pg(frm) as conn:
                    conn.execute(f"CREATE SUBSCRIPTION {cfg['new_sub']} CONNECTION "
                                 f"'host={HOST[to]} port=5432 dbname=chat user=replicator password=replicator_pw' "
                                 f"PUBLICATION bravo_pub WITH (copy_data = false)")
            committed = True
            return {"dropped": cfg["drop_sub"], "created": cfg["new_sub"]}
        step(6, "전환 확정 (구독 전환)", commit)

        # 7. 받는 쪽 쓰기 허용 → 진입점 대상 변경 → RESUME
        def switch():
            with infra.pg(to) as conn:
                conn.execute("GRANT INSERT ON messages TO chat_app")
            _set_bouncer_target(to)
            _admin("RESUME chat")
            return {"pgbouncer_target": HOST[to]}
        step(7, f"진입점 전환 → {HOST[to]} · RESUME", switch)
        paused = False

        # 8. 실제 경로로 업무 쓰기 1건 → ACK에서 타이머 종료
        def probe():
            with psycopg.connect(APP_VIA_BOUNCER, autocommit=True, connect_timeout=5,
                                 prepare_threshold=None) as conn:
                served = conn.execute("SELECT current_setting('cluster_name')").fetchone()[0]
                conn.execute("INSERT INTO messages (idem_key, body) VALUES (%s, %s)",
                             (f"probe-{direction}-{plan.epoch}", f"{direction} 확인 쓰기"))
            if served != HOST[to]:
                raise RuntimeError(f"쓰기가 {served}에 도착")
            return {"served_by": served}
        step(8, "새 경로 쓰기 ACK", probe)
        result["write_pause_ms"] = round((time.monotonic() - pause_started) * 1000)
        result["outcome"] = "COMMITTED"

    except StepFailed:
        if not committed:
            _abort(plan, frm, to, paused, fenced)
            result["outcome"] = "ABORT_PRE_WRITE"
        else:
            result["outcome"] = "FAILBACK" if direction == "cutover" else "SAFE_HOLD"
    result["total_ms"] = round((time.monotonic() - t0) * 1000)
    return result


def _abort(plan: Plan, frm: str, to: str, paused: bool, fenced: bool) -> None:
    """쓰기 전 실패: 받는 쪽 쓰기 차단 확인 → 넘겨주는 쪽 권한 · 진입점 복원 → RESUME."""
    with infra.pg(to) as conn:
        conn.execute("REVOKE INSERT ON messages FROM chat_app")
    if fenced:
        with infra.pg(frm) as conn:
            conn.execute("GRANT INSERT ON messages TO chat_app")
    _set_bouncer_target(frm)
    if paused:
        _admin("RESUME chat")
    bus.publish(plan.id, "executor_step", f"ABORT_PRE_WRITE: {HOST[frm]} 쓰기 복원")


def reconcile(plan: Plan) -> dict | None:
    """writer가 ACK 받은 키가 현재 쓰기 DB에 모두 있는지 대조한다 (유실 · 중복)."""
    try:
        acked = infra.http.get(os.environ.get("WRITER_URL", "http://writer:8090") + "/acked").json()
    except Exception:
        return None
    keys = list(acked["acked"])
    if not keys:
        return None
    side = "target" if TARGET_FILE.read_text().find("pg-dst") >= 0 else "source"
    with infra.pg(side) as conn:
        rows = conn.execute("SELECT idem_key, count(*) FROM messages WHERE idem_key = ANY(%s) GROUP BY idem_key",
                            (keys,)).fetchall()
    found = {k: c for k, c in rows}
    lost = [k for k in keys if k not in found]
    dup = [k for k, c in found.items() if c > 1]
    result = {"checked_in": HOST[side], "acked": len(keys), "found": len(found),
              "lost": len(lost), "duplicated": len(dup), "gave_up": len(acked["gave_up"])}
    bus.publish(plan.id, "reconcile", f"writer 대조 ({HOST[side]}): ACK {len(keys)}건 중 유실 {len(lost)} · 중복 {len(dup)}",
                ok=not lost and not dup)
    return result
