"""플랫폼 API (설계 문서 8.9의 부분 집합) + 콘솔 제공."""

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import agent, live, workflow
from .events import bus
from .state import EXCEPTIONS, FLOW, TransitionError

STATIC = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="bravo-platform")

# Idempotency-Key → (요청 지문, 상태 코드, 응답 본문)
_idem: dict[str, tuple[str, int, dict]] = {}
# 실행 요청은 한 번에 하나만 처리한다 (같은 계획에 두 단계가 동시에 돌지 않게).
_exec_lock = asyncio.Lock()


async def execute(request: Request, key: str | None, action: Callable[[], dict]) -> JSONResponse:
    if not key:
        raise HTTPException(400, "Idempotency-Key 헤더가 필요함")
    fingerprint = hashlib.sha256(request.url.path.encode() + b"\n" + await request.body()).hexdigest()
    async with _exec_lock:
        if key in _idem:
            fp, status, body = _idem[key]
            if fp != fingerprint:
                return JSONResponse({"error": "idempotency_conflict",
                                     "detail": "같은 키에 다른 요청"}, status_code=409)
            return JSONResponse(body, status_code=status, headers={"Idempotent-Replay": "true"})
        try:
            body, status = await run_in_threadpool(action), 200
        except TransitionError as e:
            body, status = {"error": "transition_rejected", "detail": str(e),
                            "current": e.current, "target": e.target}, 409
        except Exception as e:  # 단계 실행 중 예상하지 못한 오류: 기록하고 그대로 보고
            plan_id = request.path_params.get("plan_id", "-")
            bus.publish(plan_id, "error", f"{type(e).__name__}: {e}")
            body, status = {"error": "step_failed", "detail": f"{type(e).__name__}: {e}"}, 500
        _idem[key] = (fingerprint, status, body)
        return JSONResponse(body, status_code=status)


def get_plan(plan_id: str):
    plan = workflow.plans.get(plan_id)
    if plan is None:
        raise HTTPException(404, "plan not found")
    return plan


class PlanIn(BaseModel):
    workload: str = "chat"
    source: str = "aws (pg-src, s3-src)"
    target: str = "onprem (pg-dst, s3-dst)"


class ApprovalIn(BaseModel):
    approver: str = "operator"
    # 승인 화면에 표시된 결속 값. 서버의 현재 값과 다르면 거부한다.
    plan_id: str
    proposal_digest: str
    gate_id: str


class FaultIn(BaseModel):
    kind: str  # target_failure | destructive_tool | invalid_proposal


@app.get("/")
def console():
    return FileResponse(STATIC / "index.html")


@app.get("/v1/meta")
def meta():
    return {"flow": FLOW, "exceptions": EXCEPTIONS, "current_plan_id": workflow.current_plan_id,
            "ai_mode": agent.MODE, "model": agent.MODEL}


@app.get("/v1/live")
def live_state():
    """콘솔 토폴로지용: 쓰기 DB, 복제 방향 · 지연, 무인증 노출, 부하 현황."""
    return live.snapshot()


class LoadIn(BaseModel):
    rate: float = 20.0


@app.post("/v1/load/{action}")
def load(action: str, body: LoadIn | None = None):
    """데모 하네스: 부하 생성기 시작 · 중지 (이전 계획의 실행 단계가 아님)."""
    if action not in ("start", "stop"):
        raise HTTPException(404)
    return live.writer_control(action, body.rate if body and action == "start" else None)


@app.post("/v1/plans")
async def create_plan(body: PlanIn, request: Request, idempotency_key: str | None = Header(None)):
    return await execute(request, idempotency_key,
                         lambda: workflow.create_plan(body.workload, body.source, body.target).to_dict())


@app.get("/v1/plans/{plan_id}")
def read_plan(plan_id: str):
    return get_plan(plan_id).to_dict()


def _action(name: str, fn: Callable):
    async def handler(plan_id: str, request: Request, idempotency_key: str | None = Header(None)):
        plan = get_plan(plan_id)
        return await execute(request, idempotency_key, lambda: fn(plan))
    app.post(f"/v1/plans/{{plan_id}}/{name}", name=name)(handler)


_action("gate", workflow.run_gate)
_action("rehearsals", workflow.run_rehearsal)
_action("shift", workflow.shift)
_action("cutover", workflow.cutover)
_action("complete", workflow.complete)
_action("failback", lambda plan: workflow.failback(plan, "운영자 요청"))


@app.post("/v1/plans/{plan_id}/approvals")
async def approvals(plan_id: str, body: ApprovalIn, request: Request,
                    idempotency_key: str | None = Header(None)):
    plan = get_plan(plan_id)
    return await execute(request, idempotency_key, lambda: workflow.approve(plan, body.approver, body.model_dump()))


@app.post("/v1/plans/{plan_id}/faults")
async def faults(plan_id: str, body: FaultIn, request: Request,
                 idempotency_key: str | None = Header(None)):
    plan = get_plan(plan_id)
    return await execute(request, idempotency_key, lambda: workflow.inject_fault(plan, body.kind))


@app.get("/v1/plans/{plan_id}/events")
async def events(plan_id: str, request: Request, last_event_id: str | None = Header(None)):
    get_plan(plan_id)
    last = int(last_event_id or request.query_params.get("after", 0))

    async def stream():
        nonlocal last
        while not await request.is_disconnected():
            for e in bus.since(plan_id, last):
                last = e["id"]
                yield f"id: {e['id']}\ndata: {json.dumps(e, ensure_ascii=False, default=str)}\n\n"
            await asyncio.sleep(0.2)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})
