"""결정적 워크플로: API 요청을 받아 상태를 옮기고 각 단계 모듈을 호출한다.

단계 모듈(steps)은 판정 결과만 돌려주고, 다음 상태는 이 파일의 코드가 정한다.
"""

from . import steps
from .events import bus
from .state import MAX_AI_ATTEMPTS, Plan, S, TransitionError

plans: dict[str, Plan] = {}
current_plan_id: str | None = None


def advance(plan: Plan, target: S, reason: str = "") -> None:
    if not plan.can(target):
        bus.publish(plan.id, "transition_rejected", f"{plan.state} → {target} 거부",
                    current=plan.state, target=target)
        raise TransitionError(plan.state, target)
    prev = plan.state
    plan.state = target
    plan.history.append({"from": prev, "to": target, "reason": reason})
    bus.publish(plan.id, "state_changed", f"{prev} → {target}" + (f" ({reason})" if reason else ""),
                prev=prev, state=target)


def require(plan: Plan, state: S, what: str) -> None:
    """전이가 아닌 동작(승인 등)의 전제 상태 확인."""
    if plan.state != state:
        bus.publish(plan.id, "transition_rejected", f"{what}: {plan.state} 상태에서는 불가",
                    current=plan.state, required=state)
        raise TransitionError(plan.state, state, f"{what}은(는) {state} 상태에서만 가능")


def create_plan(workload: str, source: str, target: str) -> Plan:
    global current_plan_id
    plan = Plan(workload=workload, source=source, target=target)
    plans[plan.id] = plan
    current_plan_id = plan.id
    bus.publish(plan.id, "plan_created", f"{workload}: {source} → {target}", state=plan.state)

    plan.data["collect"] = steps.collect(plan)
    advance(plan, S.ANALYZE)
    plan.data["analyze"] = steps.analyze(plan)
    advance(plan, S.PROPOSE, "1회차: 규칙 기반 기본 이전안")
    plan.data["proposal"] = steps.baseline_proposal(plan)
    advance(plan, S.REHEARSE)
    plan.data["rehearsal"] = steps.apply_baseline(plan)
    return plan


def run_gate(plan: Plan) -> dict:
    advance(plan, S.GATE)
    result = steps.gate(plan)
    plan.data["gate"] = result
    if result["verdict"] == "PASS":
        advance(plan, S.AWAIT_APPROVAL, "게이트 PASS")
    return result


def run_rehearsal(plan: Plan) -> dict:
    advance(plan, S.PROPOSE, "AI 보완 제안")
    plan.ai_attempts += 1
    if plan.ai_attempts > MAX_AI_ATTEMPTS:
        advance(plan, S.NEEDS_HUMAN, f"AI 시도 {MAX_AI_ATTEMPTS}회 초과")
        return {"ok": False, "reason": "NEEDS_HUMAN"}
    proposal = steps.ai_proposal(plan)
    plan.data["proposal"] = proposal
    advance(plan, S.REHEARSE)
    result = steps.rehearse(plan, proposal)
    plan.data["rehearsal"] = result
    return result


def approve(plan: Plan, approver: str) -> dict:
    require(plan, S.AWAIT_APPROVAL, "승인")
    approval = steps.approve(plan, approver)
    plan.data["approval"] = approval
    return approval


def shift(plan: Plan) -> dict:
    if "approval" not in plan.data:
        bus.publish(plan.id, "transition_rejected", "승인 없이 SHIFT 불가")
        raise TransitionError(plan.state, S.SHIFT, "승인이 필요함")
    advance(plan, S.SHIFT)
    result = steps.shift(plan)
    plan.data["shift"] = result
    return result


def cutover(plan: Plan) -> dict:
    advance(plan, S.CUTOVER)
    plan.epoch += 1
    result = steps.cutover(plan)
    plan.data["cutover"] = result
    outcome = result["outcome"]
    if outcome == "COMMITTED":
        advance(plan, S.WATCH, "컷오버 완료")
    elif outcome == "ABORT_PRE_WRITE":
        plan.data.pop("approval", None)
        advance(plan, S.AWAIT_APPROVAL, "쓰기 전 실패, 출발지 복원")
    elif outcome == "FAILBACK":
        failback(plan, "컷오버 쓰기 후 실패")
    else:
        advance(plan, S.SAFE_HOLD, "원격 상태 불명")
    return result


def failback(plan: Plan, reason: str) -> dict:
    advance(plan, S.FAILBACK, reason)
    plan.epoch += 1
    result = steps.failback(plan)
    plan.data["failback"] = result
    if result["outcome"] == "COMMITTED":
        advance(plan, S.DONE_ROLLED_BACK, "출발지로 복귀")
    else:
        advance(plan, S.SAFE_HOLD, "되돌리기 확인 불가")
    return result


def complete(plan: Plan) -> dict:
    advance(plan, S.DONE, "관찰 종료")
    return {"ok": True}


def inject_fault(plan: Plan, kind: str) -> dict:
    if kind == "target_failure":
        bus.publish(plan.id, "fault_injected", "목적지 장애 주입", kind=kind, simulated=True)
        return failback(plan, "목적지 장애 감지")
    if kind == "destructive_tool":
        return steps.destructive_tool_attempt(plan)
    raise ValueError(f"unknown fault: {kind}")
