"""단계 모듈. 2단계에서 STUB으로 시작해 3~6단계에서 실제 구현으로 바꿨다 (SHIFT만 SIMULATED).

각 함수는 결과(dict)만 돌려주고 상태 전이는 하지 않는다.
"""

import time

from . import executor
from .collect import analyze, apply_baseline, baseline_proposal, collect  # noqa: F401  (3단계)
from .events import bus
from .gate import run as gate  # noqa: F401  (3단계)
from .state import Plan


def shift(plan: Plan) -> dict:
    for w in (10, 50, 100):
        bus.publish(plan.id, "step", f"[SIMULATED] 목적지 가중치 {w}%", step="SHIFT",
                    weight=w, simulated=True)
        time.sleep(0.5)
    return {"simulated": True, "weight": 100}


def cutover(plan: Plan) -> dict:
    result = executor.run(plan, "cutover")
    if result["outcome"] == "COMMITTED":
        time.sleep(2)  # 진행 중이던 writer 요청이 ACK를 받을 시간
        result["reconcile"] = executor.reconcile(plan)
    return result


def failback(plan: Plan) -> dict:
    """S4: 컷오버와 같은 순서를 반대 방향으로. 목적지에 쓴 데이터는 역방향 복제로 이미 출발지에 와 있다."""
    result = executor.run(plan, "failback")
    if result["outcome"] == "COMMITTED":
        time.sleep(2)
        result["reconcile"] = executor.reconcile(plan)
    return result
