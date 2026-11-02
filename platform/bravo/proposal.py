"""S2: 제안서 검증 → 리허설 → 목적지 반영 (설계 문서 8.3 · 8.4).

제안서가 거치는 순서: ① 스키마 검증 → ② 정책 린트 → ③ 리허설 적용 · 검증 → (게이트는 workflow가 실행)
"""

import hashlib
import json
import time

import boto3
from botocore.config import Config
from pydantic import BaseModel, ConfigDict, ValidationError

from . import agent, infra, tools
from .events import bus
from .state import Plan

REHEARSAL_BUCKET = f"{infra.BUCKET}-rehearsal"


# ---------- ① 스키마 검증 (API 형식 보장과 별개로 플랫폼이 다시 확인) ----------

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Src(_Strict):
    type: str
    id: str


class _Tgt(_Strict):
    type: str
    name: str


class _Body(_Strict):
    anonymous: str
    principal: str
    actions: list[str]


class Proposal(_Strict):
    step: str
    source: _Src
    target: _Tgt
    proposal: _Body
    rationale: str
    risk: str


def validate_schema(data: dict) -> list[str]:
    try:
        Proposal.model_validate(data)
        return []
    except ValidationError as e:
        return [f"스키마: {err['loc']} {err['msg']}" for err in e.errors()]


# ---------- ② 정책 린트 ----------

def lint(data: dict) -> list[str]:
    p, violations = data["proposal"], []
    if data["target"]["name"] != infra.BUCKET:
        violations.append(f"대상 버킷은 {infra.BUCKET}이어야 함")
    if p["anonymous"] != "deny":
        violations.append("무인증 접근은 deny만 허용")
    if p["principal"] in ("*", "anonymous", ""):
        violations.append(f"와일드카드 · 무인증 principal 금지: {p['principal']!r}")
    extra = set(p["actions"]) - set(agent.ALLOWED_ACTIONS)
    if extra or not p["actions"]:
        violations.append(f"허용 목록 밖 권한: {sorted(extra) or '없음(빈 목록)'}")
    return violations


def digest(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


# ---------- SeaweedFS 접근 설정 조작 (플랫폼이 대리 실행) ----------

def _identity(side: str, name: str) -> dict | None:
    r = infra.http.get(f"{infra.FILER[side]}/etc/iam/identities/{name}.json")
    return r.json() if r.status_code == 200 else None


def _write_identity(side: str, doc: dict) -> None:
    body = json.dumps(doc, indent=2).encode()
    infra.http.post(f"{infra.FILER[side]}/etc/iam/identities/",
                    files={"file": (f"{doc['name']}.json", body)}).raise_for_status()


def set_anonymous_read(side: str, bucket: str, allow: bool) -> None:
    doc = _identity(side, "anonymous") or {"name": "anonymous", "actions": []}
    action = f"Read:{bucket}"
    actions = [a for a in doc["actions"] if a != action] + ([action] if allow else [])
    if actions:
        _write_identity(side, {**doc, "actions": actions})
    elif _identity(side, "anonymous") is not None:
        infra.http.delete(f"{infra.FILER[side]}/etc/iam/identities/anonymous.json")


def _admin_s3(side: str):
    return boto3.client("s3", endpoint_url=infra.S3[side], region_name="us-east-1",
                        aws_access_key_id="bravo-admin", aws_secret_access_key="bravo-admin-secret",
                        config=Config(retries={"max_attempts": 1}))


def _app_s3(side: str):
    return boto3.client("s3", endpoint_url=infra.S3[side], region_name="us-east-1",
                        aws_access_key_id="chat-app", aws_secret_access_key="chat-app-secret",
                        config=Config(retries={"max_attempts": 1}))


def _wait_anonymous(side: str, bucket: str, expect: int, tries: int = 20) -> int | None:
    """접근 설정 반영은 비동기라 기대값이 나올 때까지 잠깐 기다린다."""
    status = None
    for _ in range(tries):
        status = infra.anonymous_get(side, bucket)
        if status == expect:
            return status
        time.sleep(0.25)
    return status


# ---------- ③ 리허설 ----------

def prepare_rehearsal(plan: Plan) -> None:
    """목적지와 같은 상태의 리허설 버킷을 만든다 (현재 목적지 설정 복사)."""
    s3 = _admin_s3("target")
    try:
        s3.create_bucket(Bucket=REHEARSAL_BUCKET)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    s3.put_object(Bucket=REHEARSAL_BUCKET, Key=infra.PROBE_OBJECT, Body=b"rehearsal copy")
    target_open = f"Read:{infra.BUCKET}" in (_identity("target", "anonymous") or {}).get("actions", [])
    set_anonymous_read("target", REHEARSAL_BUCKET, allow=target_open)
    bus.publish(plan.id, "step", f"리허설 버킷 {REHEARSAL_BUCKET} 준비 (목적지 설정 복사: "
                f"무인증 읽기 {'허용' if target_open else '거부'})", step="REHEARSE")


def rehearse(plan: Plan, data: dict) -> dict:
    """리허설 버킷에 제안서를 적용하고 검증한다."""
    tools.check(plan, "rehearsal.exec", {"action": "apply_proposal", "bucket": REHEARSAL_BUCKET})
    set_anonymous_read("target", REHEARSAL_BUCKET, allow=data["proposal"]["anonymous"] == "allow")

    tools.check(plan, "rehearsal.result", {"bucket": REHEARSAL_BUCKET})
    anon = _wait_anonymous("target", REHEARSAL_BUCKET, expect=403)
    try:
        _admin_s3("target").get_object(Bucket=REHEARSAL_BUCKET, Key=infra.PROBE_OBJECT)
        object_ok = True
    except Exception:
        object_ok = False
    checks = {"anonymous_denied": anon in (401, 403), "object_intact": object_ok}
    ok = all(checks.values())
    bus.publish(plan.id, "step", f"리허설 검증 {'통과' if ok else '실패'}: {checks}",
                step="REHEARSE", ok=ok)
    return {"ok": ok, "checks": checks, "anonymous_http": anon}


def apply_to_target(plan: Plan, data: dict) -> dict:
    """리허설을 통과한 제안서만 실제 목적지 버킷에 반영한다."""
    set_anonymous_read("target", infra.BUCKET, allow=False)
    anon = _wait_anonymous("target", infra.BUCKET, expect=403)
    try:
        _app_s3("target").get_object(Bucket=infra.BUCKET, Key=infra.PROBE_OBJECT)
        app_ok = True
    except Exception:
        app_ok = False
    app_actions = (_identity("target", data["proposal"]["principal"]) or {}).get("actions", [])
    result = {"anonymous_http": anon, "app_read_ok": app_ok, "principal_actions": app_actions}
    bus.publish(plan.id, "step", f"목적지 반영: 무인증 {anon}, 앱 자격 읽기 "
                f"{'정상' if app_ok else '실패'}", step="REHEARSE")
    return result


def context_for_agent(plan: Plan) -> dict:
    tools.check(plan, "inventory.describe", {"sides": "source,target", "bucket": infra.BUCKET})
    g1 = next(q for q in plan.data["gate"]["questions"] if q["id"] == "G1")
    return {
        "blocked_question": {k: g1[k] for k in ("id", "question", "source", "target")},
        "bucket": infra.BUCKET,
        "source_identities": infra.identities("source"),
        "target_identities": infra.identities("target"),
        "allowed_actions": agent.ALLOWED_ACTIONS,
    }
