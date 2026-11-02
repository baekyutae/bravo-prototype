"""이전 대상 앱: 상담 메시지 저장 API.

설계 문서 8.10 조건 중 플랫폼이 읽는 것만 지킨다.
- DB 접속은 PgBouncer만 가리킨다 (컷오버 때 앱 설정은 바뀌지 않는다).
- 쓰기마다 클라이언트가 만든 멱등 키를 받는다. 같은 키는 한 번만 저장한다.
- 트랜잭션은 단건 INSERT (PgBouncer PAUSE가 제때 끝나야 하므로).
- 첨부는 S3 SDK, 엔드포인트는 설정값.
"""

import os
import uuid

import boto3
import psycopg
from fastapi import FastAPI, Header, HTTPException
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

DATABASE_URL = os.environ["DATABASE_URL"]
S3_ENDPOINT = os.environ["S3_ENDPOINT"]
S3_BUCKET = os.environ.get("S3_BUCKET", "chat-attachments")

# transaction 모드 PgBouncer에서는 서버 측 prepared statement를 쓰지 않는다.
pool = ConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=10,
    kwargs={"prepare_threshold": None, "autocommit": True},
    open=True,
)

# "설정 교체" 등급 의존성: 엔드포인트만 바꾸면 목적지 저장소를 쓸 수 있다.
s3 = boto3.client(
    "s3",
    endpoint_url=S3_ENDPOINT,
    aws_access_key_id=os.environ["S3_ACCESS_KEY"],
    aws_secret_access_key=os.environ["S3_SECRET_KEY"],
    region_name="us-east-1",
)


def load_db_password_from_secrets_manager() -> str:
    """'코드 수정' 등급 의존성 (의도된 의존성, 데모에서는 호출하지 않음).

    AWS SDK로 비밀을 직접 조회하므로 온프렘으로 옮기려면 코드를 바꿔야 한다.
    """
    client = boto3.client("secretsmanager")
    return client.get_secret_value(SecretId="chat/db-password")["SecretString"]


app = FastAPI(title="chat-app")


class MessageIn(BaseModel):
    body: str
    attachment: str | None = None


@app.get("/healthz")
def healthz():
    with pool.connection() as conn:
        db = conn.execute("SELECT current_setting('cluster_name')").fetchone()[0]
    return {"ok": True, "db": db}


def _existing_id(idempotency_key: str) -> int | None:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id FROM messages WHERE idem_key = %s", (idempotency_key,)
        ).fetchone()
    return row[0] if row else None


@app.post("/messages")
def create_message(msg: MessageIn, idempotency_key: str = Header(...)):
    try:
        attachment_key = None
        if msg.attachment is not None and _existing_id(idempotency_key) is None:
            attachment_key = f"{uuid.uuid4()}.txt"
            s3.put_object(Bucket=S3_BUCKET, Key=attachment_key, Body=msg.attachment.encode())

        with pool.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO messages (idem_key, body, attachment_key)
                VALUES (%s, %s, %s)
                ON CONFLICT (idem_key) DO NOTHING
                RETURNING id
                """,
                (idempotency_key, msg.body, attachment_key),
            ).fetchone()
            if row is not None:
                return {"id": row[0], "duplicate": False}
        # 이미 저장된 키: 재시도이므로 기존 행을 돌려준다.
        return {"id": _existing_id(idempotency_key), "duplicate": True}
    except psycopg.Error as e:
        # 펜싱 중(권한 회수) 또는 DB 전환 중: 클라이언트가 같은 키로 재시도한다.
        raise HTTPException(status_code=503, detail=type(e).__name__)


@app.get("/messages")
def list_messages(after: int = 0, limit: int = 50):
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, body, attachment_key, created_at FROM messages"
            " WHERE id > %s ORDER BY id LIMIT %s",
            (after, limit),
        ).fetchall()
    return [
        {"id": r[0], "body": r[1], "attachment_key": r[2], "created_at": r[3].isoformat()}
        for r in rows
    ]
