#!/bin/sh
# 데모 전에 미리 해 두는 DB 준비 (설계 문서 10장: 초기 복사는 사전 실행)
# 1) PgBouncer 대상을 출발지로 설정
# 2) 목적지에 정방향 구독 생성 → 출발지 데이터 초기 복사 후 변경분 추종
set -eu

TARGET_FILE=/target/databases.ini
if [ ! -f "$TARGET_FILE" ]; then
  echo "chat = host=pg-src port=5432 dbname=chat" > "$TARGET_FILE"
  echo "[bootstrap] pgbouncer target -> pg-src"
fi

export PGPASSWORD=postgres
exists=$(psql -h pg-dst -U postgres -d chat -tAc "SELECT count(*) FROM pg_subscription WHERE subname = 'bravo_fwd'")
if [ "$exists" = "0" ]; then
  psql -h pg-dst -U postgres -d chat -v ON_ERROR_STOP=1 -c \
    "CREATE SUBSCRIPTION bravo_fwd
       CONNECTION 'host=pg-src port=5432 dbname=chat user=replicator password=replicator_pw'
       PUBLICATION bravo_pub"
  echo "[bootstrap] subscription bravo_fwd created on pg-dst"
fi

# 업무 쓰기를 받는 DB는 최대 하나: 목적지는 컷오버 전까지 앱 쓰기 권한이 없다.
psql -h pg-dst -U postgres -d chat -v ON_ERROR_STOP=1 -qc "REVOKE INSERT ON messages FROM chat_app"
echo "[bootstrap] pg-dst: chat_app INSERT revoked (fenced until cutover)"
