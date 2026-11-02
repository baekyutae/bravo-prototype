"""에이전트 호출부 (설계 문서 8.3 역할 중 3개: 의존성 분류, 번역 제안서, 승인 요약).

- AI는 JSON 스키마로 정해진 답만 낸다. 실행 권한과 자격 증명은 없다.
- 응답은 플랫폼 쪽에서 다시 검증한다(API의 형식 보장과 별개).
- AI_MODE=live: 실제 호출 후 응답을 replay/ 에 저장. AI_MODE=replay: 저장된 응답 사용.
- 본선 구현(Claude Agent SDK 워커)으로 바꿀 때는 이 파일만 교체한다.
"""

import json
import os
import time
from pathlib import Path

import anthropic

from .events import bus
from .state import Plan

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
MODE = os.environ.get("AI_MODE", "live")
REPLAY_DIR = Path(os.environ.get("REPLAY_DIR", "/srv/replay"))

_client: anthropic.Anthropic | None = None
_recorded: dict[str, list] = {}


class AgentError(Exception):
    pass


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def call(plan: Plan, kind: str, system: str, user: str, schema: dict, attempt: int = 1) -> dict:
    """구조화 출력 1회 호출. kind는 replay 파일 이름."""
    started = time.monotonic()
    if MODE == "replay":
        records = json.loads((REPLAY_DIR / f"{kind}.json").read_text())
        data = records[min(attempt, len(records)) - 1]
        bus.publish(plan.id, "ai_call", f"[replay] {kind} #{attempt}", kind=kind, mode="replay")
        return data

    try:
        response = _get_client().beta.messages.create(
            model=MODEL,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # 안전 분류기 거절 시 서버가 다른 모델로 재시도
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
        )
    except anthropic.APIError as e:
        bus.publish(plan.id, "ai_error", f"{kind} 호출 실패: {type(e).__name__}", kind=kind)
        raise AgentError(str(e)) from e

    if response.stop_reason == "refusal":
        raise AgentError("모델이 요청을 거절함")
    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)

    _recorded.setdefault(kind, [])
    if attempt == 1:
        _recorded[kind] = []
    _recorded[kind].append(data)
    REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    (REPLAY_DIR / f"{kind}.json").write_text(json.dumps(_recorded[kind], ensure_ascii=False, indent=2))

    bus.publish(plan.id, "ai_call",
                f"{kind} #{attempt} ({response.model}, {time.monotonic() - started:.1f}s, "
                f"in {response.usage.input_tokens} / out {response.usage.output_tokens} tok)",
                kind=kind, mode="live", model=response.model,
                input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens)
    return data


# ---------- 역할 1: 애매한 의존성 분류 (ANALYZE) ----------

GRADES = ["설정 교체", "어댑터 추가", "코드 수정"]

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "grade": {"type": "string", "enum": GRADES},
                    "reason": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["id", "grade", "reason", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

CLASSIFY_SYSTEM = """당신은 AWS에서 온프렘(RKE2, CNPG, SeaweedFS, OpenBao)으로 서비스를 옮기는 플랫폼의 분석 보조다.
정적 분석이 등급을 정하지 못한 클라우드 의존성만 받는다. 각 항목을 아래 3등급 중 하나로 분류하고 한국어로 이유를 2문장 이내로 쓴다.
- 설정 교체: 엔드포인트·주소만 바꾸면 그대로 동작
- 어댑터 추가: 코드는 두고 호환 계층·사이드카·익스포터를 붙여야 함
- 코드 수정: 애플리케이션 코드를 바꿔야 함
분류는 제안이며 사람이 확인한다."""


def classify_dependencies(plan: Plan, items: list[dict]) -> list[dict]:
    user = "다음 항목을 분류하라.\n" + json.dumps(items, ensure_ascii=False, indent=2)
    return call(plan, "classify", CLASSIFY_SYSTEM, user, CLASSIFY_SCHEMA)["items"]


# ---------- 역할 2: 번역 제안서 (PROPOSE) ----------

ALLOWED_ACTIONS = ["Read", "Write", "List"]

PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "step": {"type": "string", "enum": ["translate"]},
        "source": {
            "type": "object",
            "properties": {"type": {"type": "string"}, "id": {"type": "string"}},
            "required": ["type", "id"], "additionalProperties": False,
        },
        "target": {
            "type": "object",
            "properties": {"type": {"type": "string"}, "name": {"type": "string"}},
            "required": ["type", "name"], "additionalProperties": False,
        },
        "proposal": {
            "type": "object",
            "properties": {
                "anonymous": {"type": "string", "enum": ["deny", "allow"]},
                "principal": {"type": "string"},
                "actions": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["anonymous", "principal", "actions"], "additionalProperties": False,
        },
        "rationale": {"type": "string"},
        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["step", "source", "target", "proposal", "rationale", "risk"],
    "additionalProperties": False,
}

PROPOSE_SYSTEM = """당신은 AWS에서 온프렘으로 서비스를 옮기는 플랫폼의 번역 보조다.
원본 대조 게이트가 막은 항목을 보고, 출발지 AWS 설정과 같은 보호 수준을 목적지 SeaweedFS에서 재현하는 번역 제안서 1개를 낸다.
제안서는 사람이 아니라 플랫폼이 검증하고 적용한다. 당신에게는 실행 권한이 없다.
- source.type은 aws_s3_bucket_policy, target.type은 seaweedfs_s3_identity로 쓴다.
- proposal.principal은 앱 전용 접근 주체 이름, proposal.actions는 그 주체에게 필요한 최소 권한(Read, Write, List 중에서)이다.
- rationale은 한국어 2문장 이내."""


def propose_translation(plan: Plan, context: dict, feedback: list[str], attempt: int) -> dict:
    user = "게이트 결과와 양쪽 설정:\n" + json.dumps(context, ensure_ascii=False, indent=2)
    if feedback:
        user += "\n\n직전 제안서가 검증에서 거부됐다. 사유:\n- " + "\n- ".join(feedback)
    return call(plan, "propose", PROPOSE_SYSTEM, user, PROPOSAL_SCHEMA, attempt)


# ---------- 역할 3: 승인 요약 (AWAIT_APPROVAL) ----------

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "changes": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "changes", "risks"],
    "additionalProperties": False,
}

SUMMARY_SYSTEM = """당신은 이전 플랫폼의 승인 화면에 붙는 요약을 쓴다. 승인자는 금융사 인프라 운영팀이다.
주어진 데이터만 근거로 한국어로 쓴다. 데이터에 없는 수치나 사실을 만들지 않는다.
summary는 3문장 이내, changes와 risks는 각각 3개 이내의 짧은 항목이다.
승인 여부를 권하지 않는다. 판단은 승인자가 한다."""


def approval_summary(plan: Plan, facts: dict) -> dict:
    user = "승인 대상 데이터:\n" + json.dumps(facts, ensure_ascii=False, indent=2, default=str)
    return call(plan, "summary", SUMMARY_SYSTEM, user, SUMMARY_SCHEMA)
