"""단계 모듈. 2단계(뼈대)에서는 모두 STUB이며, 3~6단계에서 실제 구현으로 바꾼다.

각 함수는 결과(dict)만 돌려주고 상태 전이는 하지 않는다.
"""

import time

from .events import bus
from .state import Plan


def _stub(plan: Plan, step: str, message: str, **data) -> dict:
    bus.publish(plan.id, "step", f"[STUB] {message}", step=step, stub=True, **data)
    return {"stub": True, **data}


def collect(plan: Plan) -> dict:
    return _stub(plan, "COLLECT", "출발지 설정 수집")


def analyze(plan: Plan) -> dict:
    return _stub(plan, "ANALYZE", "의존성 분석")


def baseline_proposal(plan: Plan) -> dict:
    return _stub(plan, "PROPOSE", "기본 이전안 생성", source="rule")


def apply_baseline(plan: Plan) -> dict:
    return _stub(plan, "REHEARSE", "기본 이전안 적용", ok=True)


def gate(plan: Plan) -> dict:
    # STUB: AI 제안 전에는 BLOCK, 후에는 PASS
    verdict = "PASS" if plan.ai_attempts > 0 else "BLOCK"
    return _stub(plan, "GATE", f"게이트 판정 {verdict}", verdict=verdict)


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
