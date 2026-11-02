"""단계 모듈. STUB으로 시작해 3~6단계에서 실제 구현으로 바꾼다.

각 함수는 결과(dict)만 돌려주고 상태 전이는 하지 않는다.
"""

import time

from .collect import analyze, apply_baseline, baseline_proposal, collect  # noqa: F401  (3단계)
from .events import bus
from .gate import run as gate  # noqa: F401  (3단계)
from .state import Plan


def _stub(plan: Plan, step: str, message: str, **data) -> dict:
    bus.publish(plan.id, "step", f"[STUB] {message}", step=step, stub=True, **data)
    return {"stub": True, **data}


def ai_proposal(plan: Plan) -> dict:
    return _stub(plan, "PROPOSE", "AI 제안서 생성", source="ai")


def rehearse(plan: Plan, proposal: dict) -> dict:
    return _stub(plan, "REHEARSE", "리허설 검증", ok=True)


def approve(plan: Plan, approver: str) -> dict:
    return _stub(plan, "AWAIT_APPROVAL", f"{approver} 승인", approver=approver)


def shift(plan: Plan) -> dict:
    for w in (10, 50, 100):
        bus.publish(plan.id, "step", f"[SIMULATED] 목적지 가중치 {w}%", step="SHIFT",
                    weight=w, simulated=True)
        time.sleep(0.5)
    return {"simulated": True, "weight": 100}


def cutover(plan: Plan) -> dict:
    time.sleep(1)
    return {**_stub(plan, "CUTOVER", "컷오버"), "outcome": "COMMITTED"}


def failback(plan: Plan) -> dict:
    time.sleep(1)
    return {**_stub(plan, "FAILBACK", "되돌리기"), "outcome": "COMMITTED"}


def destructive_tool_attempt(plan: Plan) -> dict:
    return _stub(plan, "REHEARSE", "파괴적 명령 차단", blocked=True)
