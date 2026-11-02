#!/usr/bin/env bash
# 0단계 완료 기준 확인: 복제 · PgBouncer 경유 쓰기 · 오브젝트 저장소 무인증 접근
set -euo pipefail
cd "$(dirname "$0")/.."

src() { docker compose exec -T -e PGPASSWORD=postgres pg-src psql -U postgres -d chat -tAc "$1"; }
dst() { docker compose exec -T -e PGPASSWORD=postgres pg-dst psql -U postgres -d chat -tAc "$1"; }
via_bouncer() {
  docker run --rm --network bravo_default -e PGPASSWORD="$2" postgres:16 \
    psql -h pgbouncer -p 6432 -U "$1" -d "$3" -tAc "$4"
}

echo "[1] PgBouncer 대상"
via_bouncer postgres postgres pgbouncer "SHOW DATABASES" | grep '^chat|' | cut -d'|' -f1-3

echo "[2] PgBouncer 경유 쓰기 → 출발지 → 목적지 복제"
key="check-$(date +%s)"
via_bouncer chat_app chat_app_pw chat "INSERT INTO messages (idem_key, body) VALUES ('$key', 'infra check')" > /dev/null
sleep 1
echo "    src=$(src "SELECT count(*) FROM messages WHERE idem_key='$key'") dst=$(dst "SELECT count(*) FROM messages WHERE idem_key='$key'")"

echo "[3] 행 수 / 시퀀스 (시퀀스는 복제되지 않음)"
echo "    rows src=$(src 'SELECT count(*) FROM messages') dst=$(dst 'SELECT count(*) FROM messages')"
echo "    seq  src=$(src 'SELECT last_value FROM messages_id_seq') dst=$(dst 'SELECT last_value FROM messages_id_seq')"

echo "[4] 무인증 첨부 읽기 (403=거부, 200=허용)"
echo "    s3-src=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:18333/chat-attachments/sample.txt)" \
     "s3-dst=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:28333/chat-attachments/sample.txt)"
