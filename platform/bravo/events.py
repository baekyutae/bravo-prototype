"""진행 이벤트 저장소. 콘솔은 SSE로 구독하고, 마지막 이벤트 ID로 재접속한다."""

import threading
import time


class EventBus:
    def __init__(self):
        self._events: list[dict] = []
        self._lock = threading.Lock()

    def publish(self, plan_id: str, type_: str, message: str = "", **data) -> dict:
        with self._lock:
            event = {
                "id": len(self._events) + 1,
                "ts": time.time(),
                "plan_id": plan_id,
                "type": type_,
                "message": message,
                "data": data,
            }
            self._events.append(event)
        return event

    def since(self, plan_id: str, last_id: int) -> list[dict]:
        with self._lock:
            return [e for e in self._events[last_id:] if e["plan_id"] == plan_id]


bus = EventBus()
