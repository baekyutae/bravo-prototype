"""출발지·목적지 접속 정보와 공통 조회 함수.

제어 역할은 데모 단순화를 위해 superuser(postgres)를 쓴다.
"""

import os
from contextlib import contextmanager

import httpx
import psycopg

SIDES = ("source", "target")

DSN = {
    "source": os.environ.get("SRC_DSN", "postgresql://postgres:postgres@pg-src:5432/chat"),
    "target": os.environ.get("DST_DSN", "postgresql://postgres:postgres@pg-dst:5432/chat"),
}
S3 = {
    "source": os.environ.get("SRC_S3", "http://s3-src:8333"),
    "target": os.environ.get("DST_S3", "http://s3-dst:8333"),
}
FILER = {
    "source": os.environ.get("SRC_FILER", "http://s3-src:8888"),
    "target": os.environ.get("DST_FILER", "http://s3-dst:8888"),
}
BUCKET = "chat-attachments"
PROBE_OBJECT = "sample.txt"
SUBSCRIPTION = "bravo_fwd"
WORKLOAD_DIR = os.environ.get("WORKLOAD_DIR", "/workload/app")

http = httpx.Client(timeout=5.0)


@contextmanager
def pg(side: str):
    with psycopg.connect(DSN[side], autocommit=True, connect_timeout=3) as conn:
        yield conn


def identities(side: str) -> dict[str, list[str]]:
    """오브젝트 저장소의 접근 주체와 권한 (자격 증명은 읽지 않는다)."""
    r = http.get(f"{FILER[side]}/etc/iam/identities/", headers={"Accept": "application/json"})
    r.raise_for_status()
    result = {}
    for entry in r.json().get("Entries") or []:
        doc = http.get(f"{FILER[side]}{entry['FullPath']}").json()
        result[doc["name"]] = doc.get("actions", [])
    return result


def anonymous_get(side: str, bucket: str = BUCKET, key: str = PROBE_OBJECT) -> int | None:
    """인증 없이 객체를 읽어 본다. 상태 코드, 접속 실패면 None."""
    try:
        return http.get(f"{S3[side]}/{bucket}/{key}").status_code
    except httpx.HTTPError:
        return None


def bucket_exists(side: str, bucket: str = BUCKET) -> bool:
    r = http.get(f"{FILER[side]}/buckets/{bucket}/", headers={"Accept": "application/json"})
    return r.status_code == 200


SCHEMA_HASH_SQL = """
SELECT md5(string_agg(x, '|' ORDER BY x)) FROM (
  SELECT format('col:%s.%s:%s:%s', table_name, column_name, data_type, is_nullable) AS x
    FROM information_schema.columns WHERE table_schema = 'public'
  UNION ALL
  SELECT format('con:%s:%s', conrelid::regclass, pg_get_constraintdef(oid))
    FROM pg_constraint WHERE connamespace = 'public'::regnamespace
) s
"""


def schema_hash(side: str) -> str:
    with pg(side) as conn:
        return conn.execute(SCHEMA_HASH_SQL).fetchone()[0]
