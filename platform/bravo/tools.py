"""도구 계층: 에이전트 쪽 요청은 모두 여기를 거친다 (설계 문서 8.3 가드레일).

- 화이트리스트에 없는 도구는 거부한다.
- 파괴적 명령은 프롬프트가 아니라 이 계층에서 차단하고 기록한다.
- 실제 작업(리허설 적용 등)은 플랫폼 코드가 대리 실행한다.
"""

import re

from .events import bus
from .state import Plan

ALLOWED = {"inventory.describe", "repo.read", "rehearsal.exec", "rehearsal.result",
           "policy.evaluate", "plan.propose"}

DESTRUCTIVE = [
    r"\bbucket\.delete\b", r"\bdelete_bucket\b", r"\brm\s+-rf\b", r"\bDROP\s+(TABLE|DATABASE)\b",
    r"\bTRUNCATE\b", r"\bfs\.rm\b",
]


class ToolBlocked(Exception):
    pass


def check(plan: Plan, tool: str, args: dict, injected: bool = False) -> None:
    """허용되지 않으면 기록하고 ToolBlocked를 던진다."""
    reason = None
    if tool not in ALLOWED:
        reason = f"화이트리스트 밖 도구: {tool}"
    else:
        text = " ".join(str(v) for v in args.values())
        for pattern in DESTRUCTIVE:
            if re.search(pattern, text, re.IGNORECASE):
                reason = f"파괴적 명령 패턴 {pattern}"
                break
    if reason:
        record = {"tool": tool, "args": args, "reason": reason, "injected": injected}
        plan.data.setdefault("blocked", []).append(record)
        bus.publish(plan.id, "tool_blocked", f"차단: {tool} {args} ({reason})",
                    injected=injected, **{"tool": tool})
        raise ToolBlocked(reason)
    bus.publish(plan.id, "tool_call", f"{tool} {args}", tool=tool)
