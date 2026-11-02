"""S1 앞부분: COLLECT(출발지 설정 수집), ANALYZE(의존성 정적 탐지), 기본 이전안."""

import re
from pathlib import Path

from . import infra
from .events import bus
from .state import Plan

# 클러스터·계정 단위 자원은 로컬에 없으므로 목록만 보인다.
SIMULATED_INVENTORY = [
    {"kind": "EKS cluster", "id": "chat-eks"},
    {"kind": "IAM role", "id": "chat-app-irsa"},
    {"kind": "KMS key", "id": "alias/chat-data"},
    {"kind": "Secrets Manager", "id": "chat/db-password"},
]


def collect(plan: Plan) -> dict:
    with infra.pg("source") as conn:
        rows = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        pubs = [r[0] for r in conn.execute("SELECT pubname FROM pg_publication")]
    result = {
        "bucket": infra.BUCKET,
        "identities": infra.identities("source"),
        "schema_hash": infra.schema_hash("source"),
        "messages_rows": rows,
        "publications": pubs,
        "simulated_inventory": SIMULATED_INVENTORY,
    }
    bus.publish(plan.id, "step", f"출발지 수집: 테이블 행 {rows}, 버킷 접근 주체 "
                f"{sorted(result['identities'])}", step="COLLECT")
    bus.publish(plan.id, "step", "출발지 IAM · KMS · Secrets Manager 목록", step="COLLECT",
                simulated=True)
    return result


# (패턴, 등급, 설명). 등급: 설정 교체 / 어댑터 추가 / 코드 수정
RULES = [
    (r"DATABASE_URL|postgresql://", "설정 교체", "DB 주소 (PgBouncer 경유, 대상 변경은 PgBouncer에서)"),
    (r"boto3\.client\(\s*\"s3\"", "설정 교체", "S3 호환 API (엔드포인트만 교체)"),
    (r"boto3\.client\(\s*\"(sqs|sns)\"", "어댑터 추가", "AWS 메시징 (호환 어댑터 필요)"),
    (r"boto3\.client\(\s*\"kms\"", "코드 수정", "KMS 직접 호출 (OpenBao transit으로 수정)"),
    (r"boto3\.client\(\s*\"secretsmanager\"", "코드 수정",
     "Secrets Manager 직접 조회 (OpenBao · ESO 주입으로 수정)"),
]


def analyze(plan: Plan) -> dict:
    found = []
    for path in sorted(Path(infra.WORKLOAD_DIR).rglob("*.py")):
        text = path.read_text()
        for pattern, grade, desc in RULES:
            m = re.search(pattern, text)  # 파일당 규칙별 첫 위치만 (여러 줄에 걸친 호출 포함)
            if m:
                no = text.count("\n", 0, m.start()) + 1
                found.append({"file": path.name, "line": no,
                              "code": text.splitlines()[no - 1].strip(),
                              "grade": grade, "desc": desc})
    by_grade = {}
    for f in found:
        by_grade[f["grade"]] = by_grade.get(f["grade"], 0) + 1
    bus.publish(plan.id, "step", f"의존성 {len(found)}건 탐지: {by_grade}", step="ANALYZE")
    return {"dependencies": found, "by_grade": by_grade}


def baseline_proposal(plan: Plan) -> dict:
    """1회차 제안: 규칙으로 옮길 수 있는 것만 옮긴다. 버킷 접근 설정 번역 규칙은 없다."""
    proposal = {
        "source": "rule",
        "items": [
            "목적지 버킷 chat-attachments 생성",
            "목적지 스키마 적용 (마이그레이션 Job)",
            f"정방향 논리 복제 구독 {infra.SUBSCRIPTION}",
        ],
        "untranslated": ["버킷 접근 설정 (번역 규칙 없음)"],
    }
    bus.publish(plan.id, "step", "기본 이전안: 버킷 · 스키마 · 복제 (접근 설정 미번역)", step="PROPOSE")
    return proposal


def apply_baseline(plan: Plan) -> dict:
    """사전 실행된 목적지 구축이 실제로 되어 있는지 확인한다 (설계 문서 10장: 미리 실행)."""
    with infra.pg("target") as conn:
        sub = conn.execute("SELECT subenabled FROM pg_subscription WHERE subname = %s",
                           (infra.SUBSCRIPTION,)).fetchone()
    checks = {
        "target_bucket": infra.bucket_exists("target"),
        "target_schema": infra.schema_hash("target") is not None,
        "subscription_enabled": bool(sub and sub[0]),
    }
    ok = all(checks.values())
    bus.publish(plan.id, "step", f"목적지 구축 확인: {checks}", step="REHEARSE", ok=ok)
    return {"ok": ok, "checks": checks}
