"""부하 생성기: 컷오버 중에도 앱에 계속 쓰고, 유실·중복·쓰기 공백을 센다.

- 논리 요청 1건 = 멱등 키 1개. 실패하면 같은 키로 재시도한다(시도는 여러 번, 논리 요청은 1건).
- ACK 받은 키 목록을 공개해 플랫폼이 DB와 대조할 수 있게 한다.
- 쓰기 공백 = 연속된 두 ACK 사이 간격의 최댓값 (클라이언트가 체감한 정지 시간).
"""

import asyncio
import os
import time
import uuid

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

APP_URL = os.environ.get("APP_URL", "http://app:8080")
ATTEMPT_TIMEOUT_S = 10.0
RETRY_BACKOFF_S = 0.2
GIVE_UP_AFTER_S = 120.0

app = FastAPI(title="writer")


class State:
    def __init__(self):
        self.running = False
        self.rate = 0.0
        self.run_id = ""
        self.issued = 0
        self.acked: dict[str, int] = {}  # idem_key -> message id
        self.attempts = 0
        self.retries = 0
        self.gave_up: list[str] = []
        self.in_flight = 0
        self.last_ack = 0.0
        self.max_gap_ms = 0.0
        self.task: asyncio.Task | None = None


s = State()
client = httpx.AsyncClient(timeout=ATTEMPT_TIMEOUT_S)


async def send(key: str, seq: int):
    s.in_flight += 1
    started = time.monotonic()
    first = True
    try:
        while time.monotonic() - started < GIVE_UP_AFTER_S:
            if not first:
                s.retries += 1
                await asyncio.sleep(RETRY_BACKOFF_S)
            first = False
            s.attempts += 1
            try:
                r = await client.post(
                    f"{APP_URL}/messages",
                    json={"body": f"writer {s.run_id} #{seq}"},
                    headers={"Idempotency-Key": key},
                )
            except httpx.HTTPError:
                continue
            if r.status_code == 200:
                now = time.monotonic()
                if s.last_ack:
                    s.max_gap_ms = max(s.max_gap_ms, (now - s.last_ack) * 1000)
                s.last_ack = now
                s.acked[key] = r.json()["id"]
                return
        s.gave_up.append(key)
    finally:
        s.in_flight -= 1


async def loop():
    seq = 0
    interval = 1.0 / s.rate
    while s.running:
        seq += 1
        s.issued += 1
        asyncio.create_task(send(f"w-{s.run_id}-{seq}", seq))
        await asyncio.sleep(interval)


class StartIn(BaseModel):
    rate: float = 10.0


@app.post("/start")
async def start(body: StartIn):
    if s.running:
        return stats()
    s.__init__()
    s.running, s.rate, s.run_id = True, body.rate, uuid.uuid4().hex[:6]
    s.task = asyncio.create_task(loop())
    return stats()


@app.post("/stop")
async def stop():
    s.running = False
    return stats()


@app.get("/stats")
def stats():
    return {
        "running": s.running,
        "run_id": s.run_id,
        "rate": s.rate,
        "issued": s.issued,
        "acked": len(s.acked),
        "in_flight": s.in_flight,
        "attempts": s.attempts,
        "retries": s.retries,
        "gave_up": len(s.gave_up),
        "max_gap_ms": round(s.max_gap_ms),
    }


@app.get("/acked")
def acked():
    """플랫폼 대조용: ACK 받은 멱등 키와 응답 id."""
    return {"run_id": s.run_id, "acked": s.acked, "gave_up": s.gave_up}
