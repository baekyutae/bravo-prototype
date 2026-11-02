"""상태 머신: 단계 순서와 전이 조건을 코드에 고정한다 (설계 문서 8.2).

전이 표에 없는 전이는 TransitionError로 거부한다. 에이전트나 API 호출자가 단계를 건너뛸 수 없다.
"""

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum


class S(StrEnum):
    COLLECT = "COLLECT"
    ANALYZE = "ANALYZE"
    PROPOSE = "PROPOSE"
    REHEARSE = "REHEARSE"
    GATE = "GATE"
    AWAIT_APPROVAL = "AWAIT_APPROVAL"
    SHIFT = "SHIFT"
    CUTOVER = "CUTOVER"
    WATCH = "WATCH"
    DONE = "DONE"
    FAILBACK = "FAILBACK"
    DONE_ROLLED_BACK = "DONE_ROLLED_BACK"
    SAFE_HOLD = "SAFE_HOLD"
    NEEDS_HUMAN = "NEEDS_HUMAN"


FLOW = [S.COLLECT, S.ANALYZE, S.PROPOSE, S.REHEARSE, S.GATE,
        S.AWAIT_APPROVAL, S.SHIFT, S.CUTOVER, S.WATCH, S.DONE]
EXCEPTIONS = [S.FAILBACK, S.DONE_ROLLED_BACK, S.SAFE_HOLD, S.NEEDS_HUMAN]

TRANSITIONS: dict[S, set[S]] = {
    S.COLLECT: {S.ANALYZE},
    S.ANALYZE: {S.PROPOSE},
    S.PROPOSE: {S.REHEARSE, S.NEEDS_HUMAN},
    S.REHEARSE: {S.GATE, S.PROPOSE, S.NEEDS_HUMAN},        # 리허설 실패 → 다시 제안
    S.GATE: {S.AWAIT_APPROVAL, S.PROPOSE, S.NEEDS_HUMAN},  # BLOCK → 다시 제안
    S.AWAIT_APPROVAL: {S.SHIFT},
    S.SHIFT: {S.CUTOVER},
    S.CUTOVER: {S.WATCH, S.AWAIT_APPROVAL, S.FAILBACK, S.SAFE_HOLD},  # AWAIT_APPROVAL = ABORT_PRE_WRITE
    S.WATCH: {S.DONE, S.FAILBACK},
    S.FAILBACK: {S.DONE_ROLLED_BACK, S.SAFE_HOLD},
}

MAX_AI_ATTEMPTS = 3


class TransitionError(Exception):
    def __init__(self, current: S, target: S, reason: str = ""):
        self.current, self.target = current, target
        super().__init__(reason or f"{current} → {target} 전이는 정의되지 않음")


@dataclass
class Plan:
    workload: str
    source: str
    target: str
    id: str = field(default_factory=lambda: "plan-" + uuid.uuid4().hex[:6])
    state: S = S.COLLECT
    created_at: float = field(default_factory=time.time)
    history: list[dict] = field(default_factory=list)
    ai_attempts: int = 0
    epoch: int = 0
    # 단계별 결과 (수집 결과, 게이트 판정, 제안서, 승인, 영수증 등)
    data: dict = field(default_factory=dict)

    def can(self, target: S) -> bool:
        return target in TRANSITIONS.get(self.state, set())

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "workload": self.workload,
            "source": self.source,
            "target": self.target,
            "state": self.state,
            "history": self.history,
            "ai_attempts": self.ai_attempts,
            "epoch": self.epoch,
            "data": self.data,
            "next": sorted(TRANSITIONS.get(self.state, set())),
        }
