"""원본 대조 게이트 (설계 문서 8.5). LLM을 쓰지 않는 규칙 판정.

- compare 질문: 원본과 대상에 같은 질문을 던져 답이 같아야 PASS.
- ready 질문: 지금 전환해도 되는 상태인지 (데이터 경로 준비).
- 하나라도 다르거나 UNKNOWN이면 BLOCK.
"""

import hashlib
import json
import os
import time

from . import infra
from .events import bus
from .state import Plan

MAX_LAG_BYTES = int(os.environ.get("GATE_MAX_LAG_BYTES", str(1024 * 1024)))  # 임시 기준
UNKNOWN = "확인 불가"


def _anon_answer(status: int | None) -> str:
    if status in (401, 403):
        return "거부"
    if status == 200:
        return "허용"
    return UNKNOWN


def q_anonymous_read() -> dict:
    src, dst = infra.anonymous_get("source"), infra.anonymous_get("target")
    return {
        "id": "G1", "kind": "compare",
        "question": "인증 없이 첨부 객체를 읽을 수 있는가?",
        "source": _anon_answer(src), "target": _anon_answer(dst),
        "evidence": {"source_http": src, "target_http": dst,
                     "object": f"{infra.BUCKET}/{infra.PROBE_OBJECT}"},
    }


def q_schema_hash() -> dict:
    def safe(side):
        try:
            return infra.schema_hash(side)
        except Exception:
            return None
    src, dst = safe("source"), safe("target")
    return {
        "id": "G2", "kind": "compare",
        "question": "스키마(테이블 · 컬럼 · 제약) 해시가 같은가?",
        "source": src[:8] if src else UNKNOWN, "target": dst[:8] if dst else UNKNOWN,
        "evidence": {"source": src, "target": dst},
    }


def q_replication_lag() -> dict:
    try:
        with infra.pg("source") as conn:
            row = conn.execute(
                "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn), active"
                " FROM pg_replication_slots WHERE slot_name = %s", (infra.SUBSCRIPTION,)
            ).fetchone()
    except Exception:
        row = None
    if row is None:
        answer, evidence = UNKNOWN, {"slot": None}
    else:
        lag, active = int(row[0]), row[1]
        answer = "예" if active and lag <= MAX_LAG_BYTES else "아니오"
        evidence = {"lag_bytes": lag, "slot_active": active, "max_lag_bytes": MAX_LAG_BYTES}
    return {
        "id": "G3", "kind": "ready",
        "question": "복제 지연이 기준 이하인가?",
        "source": "-", "target": answer, "evidence": evidence,
    }


def _judge(q: dict) -> str:
    if UNKNOWN in (q["source"], q["target"]):
        return "UNKNOWN"
    if q["kind"] == "compare":
        return "PASS" if q["source"] == q["target"] else "BLOCK"
    return "PASS" if q["target"] == "예" else "BLOCK"


def run(plan: Plan) -> dict:
    questions = [q_anonymous_read(), q_schema_hash(), q_replication_lag()]
    for q in questions:
        q["verdict"] = _judge(q)
    verdict = "PASS" if all(q["verdict"] == "PASS" for q in questions) else "BLOCK"
    # 판정에 쓴 답만으로 ID를 만든다: 같은 입력이면 같은 gate_id (승인 결속에 사용)
    answers = [(q["id"], q["source"], q["target"], q["verdict"]) for q in questions]
    gate_id = "g-" + hashlib.sha256(json.dumps(answers).encode()).hexdigest()[:10]
    for q in questions:
        bus.publish(plan.id, "gate_question", f"{q['id']} {q['question']} "
                    f"원본={q['source']} 대상={q['target']} → {q['verdict']}", step="GATE")
    bus.publish(plan.id, "gate_result", f"게이트 {verdict} ({gate_id})", step="GATE",
                verdict=verdict, gate_id=gate_id)
    return {"gate_id": gate_id, "verdict": verdict, "questions": questions,
            "evaluated_at": time.time()}
