#!/usr/bin/env bash
# 데모를 S1 직전 상태로 되돌린다: 모든 컨테이너 · 볼륨 삭제 후 다시 구축 (초기 복사 포함)
# AI_MODE=replay ./scripts/reset.sh 로 replay 모드로 띄울 수 있다.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose down -v --remove-orphans
docker compose up -d --build
echo "준비 대기..."
for _ in $(seq 1 60); do
  if curl -sf localhost:8000/v1/live > /dev/null 2>&1 && \
     [ "$(curl -s -o /dev/null -w '%{http_code}' localhost:28333/chat-attachments/sample.txt)" = "200" ]; then
    echo "완료: http://localhost:8000"; exit 0
  fi
  sleep 1
done
echo "시간 초과: docker compose ps 로 상태를 확인하세요" >&2; exit 1
