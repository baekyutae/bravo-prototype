"""결정적 워크플로: API 요청을 받아 상태를 옮기고 각 단계 모듈을 호출한다.

단계 모듈(steps)은 판정 결과만 돌려주고, 다음 상태는 이 파일의 코드가 정한다.
"""

import time

from . import agent, infra, proposal, steps, tools
from .events import bus
from .state import MAX_AI_ATTEMPTS, Plan, S, TransitionError

APPROVAL_TTL_S = 600

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
        plan.data["approval_request"] = _approval_request(plan)
    return result


def run_rehearsal(plan: Plan) -> dict:
    """S2 수정 루프: AI가 시도하고 코드가 합격선을 고정한다 (최대 MAX_AI_ATTEMPTS회)."""
    advance(plan, S.PROPOSE, "AI 보완 제안")
    context = proposal.context_for_agent(plan)
    feedback: list[str] = []
    attempt = 0
    while True:
        attempt += 1
        plan.ai_attempts += 1
        if plan.ai_attempts > MAX_AI_ATTEMPTS:
            advance(plan, S.NEEDS_HUMAN, f"AI 시도 {MAX_AI_ATTEMPTS}회 초과")
            return {"ok": False, "reason": "NEEDS_HUMAN"}
        try:
            data = agent.propose_translation(plan, context, feedback, attempt)
        except agent.AgentError as e:
            feedback = [f"호출 실패: {e}"]
            continue
        violations = proposal.validate_schema(data) or proposal.lint(data)
        _record_attempt(plan, data, violations, injected=False)
        if violations:
            feedback = violations
            continue

        plan.data["proposal"] = data
        plan.data["proposal_digest"] = proposal.digest(data)
        advance(plan, S.REHEARSE, "스키마 · 린트 통과")
        proposal.prepare_rehearsal(plan)
        result = proposal.rehearse(plan, data)
        if not result["ok"]:
            feedback = [f"리허설 검증 실패: {result['checks']}"]
            advance(plan, S.PROPOSE, "리허설 실패 → 재제안")
            continue
        result["applied"] = proposal.apply_to_target(plan, data)
        plan.data["rehearsal"] = result
        return result


def _record_attempt(plan: Plan, data: dict, violations: list[str], injected: bool) -> None:
    plan.data.setdefault("attempts", []).append(
        {"proposal": data, "violations": violations, "injected": injected})
    verdict = "거부: " + "; ".join(violations) if violations else "스키마 · 린트 통과"
    bus.publish(plan.id, "proposal_checked", ("[주입] " if injected else "") + f"제안서 {verdict}",
                ok=not violations, injected=injected)


def _binding(plan: Plan) -> dict:
    """승인이 묶이는 현재 값. 하나라도 바뀌면 이전 승인은 무효."""
    return {"plan_id": plan.id,
            "proposal_digest": proposal.digest(plan.data["proposal"]),
            "gate_id": plan.data["gate"]["gate_id"]}


def _approval_request(plan: Plan) -> dict:
    facts = {
        "migration": f"{plan.source} → {plan.target}",
        "gate": [{k: q[k] for k in ("id", "question", "source", "target", "verdict")}
                 for q in plan.data["gate"]["questions"]],
        "proposal": plan.data["proposal"],
        "rehearsal": plan.data.get("rehearsal"),
        "blocked_tool_calls": plan.data.get("blocked", []),
        "dependencies": plan.data["analyze"]["dependencies"],
    }
    try:
        summary = agent.approval_summary(plan, facts)
    except agent.AgentError:
        summary = None
    return {**_binding(plan), "expires_at": time.time() + APPROVAL_TTL_S, "summary": summary}


def _check_binding(plan: Plan, given: dict, what: str) -> None:
    req = plan.data.get("approval_request")
    current = _binding(plan)
    problems = [k for k, v in current.items() if given.get(k) != v or (req or {}).get(k) != v]
    if req is None or problems or time.time() > req["expires_at"]:
        reason = f"결속 값 불일치 {problems}" if problems else "승인 요청 없음 또는 만료"
        bus.publish(plan.id, "transition_rejected", f"{what} 거부: {reason}")
        raise TransitionError(plan.state, S.SHIFT, f"{what} 거부: {reason}")


def approve(plan: Plan, approver: str, binding: dict) -> dict:
    require(plan, S.AWAIT_APPROVAL, "승인")
    _check_binding(plan, binding, "승인")
    approval = {**_binding(plan), "approver": approver, "approved_at": time.time(),
                "expires_at": plan.data["approval_request"]["expires_at"]}
    plan.data["approval"] = approval
    bus.publish(plan.id, "approved", f"{approver} 승인 (proposal {approval['proposal_digest']}, "
                f"gate {approval['gate_id']})")
    return approval


def shift(plan: Plan) -> dict:
    if "approval" not in plan.data:
        bus.publish(plan.id, "transition_rejected", "승인 없이 SHIFT 불가")
        raise TransitionError(plan.state, S.SHIFT, "승인이 필요함")
    _check_binding(plan, plan.data["approval"], "SHIFT")
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
    """데모 전용 주입. 화면에 주입했다고 표시한다."""
    if kind == "target_failure":
        bus.publish(plan.id, "fault_injected", "목적지 장애 주입", kind=kind, simulated=True)
        return failback(plan, "목적지 장애 감지")
    if kind == "destructive_tool":
        try:
            tools.check(plan, "rehearsal.exec",
                        {"command": f"s3.bucket.delete -name {infra.BUCKET}"}, injected=True)
        except tools.ToolBlocked as e:
            return {"blocked": True, "reason": str(e)}
        return {"blocked": False}
    if kind == "invalid_proposal":
        bad = {"step": "translate",
               "source": {"type": "aws_s3_bucket_policy", "id": infra.BUCKET},
               "target": {"type": "seaweedfs_s3_identity", "name": infra.BUCKET},
               "proposal": {"anonymous": "allow", "principal": "*", "actions": ["Read", "Admin"]},
               "rationale": "모든 주체에 읽기 허용", "risk": "low"}
        violations = proposal.validate_schema(bad) or proposal.lint(bad)
        _record_attempt(plan, bad, violations, injected=True)
        return {"rejected": bool(violations), "violations": violations}
    raise ValueError(f"unknown fault: {kind}")
