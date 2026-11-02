#!/bin/sh
# 오브젝트 저장소 초기 상태
#   s3-src (AWS S3 역할): 앱 자격으로만 접근, 무인증 거부
#   s3-dst (온프렘 SeaweedFS): 이전 직후 상태를 재현해 무인증 읽기가 열려 있음 → S1 게이트 BLOCK의 원인
# 사용법: s3-init.sh <호스트> <open|closed>
set -eu

HOST=$1
MODE=$2
BUCKET=chat-attachments

shell() { echo "$1" | weed shell -master="$HOST:9333" -filer="$HOST:8888"; }

shell "s3.bucket.create -name $BUCKET" || true
shell "s3.configure -user admin -access_key bravo-admin -secret_key bravo-admin-secret -actions Admin,Read,Write,List,Tagging -apply"
shell "s3.configure -user chat_app -access_key chat-app -secret_key chat-app-secret -actions Read,Write,List -buckets $BUCKET -apply"
if [ "$MODE" = "open" ]; then
  shell "s3.configure -user anonymous -actions Read -buckets $BUCKET -apply"
fi

echo "첨부 샘플 (상담 증빙 문서)" > /tmp/sample.txt
curl -sf -F file=@/tmp/sample.txt "http://$HOST:8888/buckets/$BUCKET/" > /dev/null
echo "[s3-init] $HOST ready (anonymous=$MODE)"
