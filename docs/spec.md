# 데모 Spec

설계 문서 v0.1의 S1~S4를 로컬 Docker Compose 위에서 한 번 끝까지 실행하는 축소판 이전 플랫폼.
11/2 21:00 팀 회의 시연용이며, 팀 공식 코드로 그대로 옮기지 않는다.

## 1. 원칙

- 게이트·영수증이 판정에 쓰는 입력은 실제 시스템에서 읽는다. 클라우드가 있어야만 되는 부분만 모의로 둔다.
- 모의값은 화면에 `SIMULATED`, 로컬 측정값은 `LOCAL` 배지를 붙인다. 로컬 측정값은 설계 문서 실증 계약의 근거로 쓰지 않는다.
- 흐름은 상태 머신 코드가 쥔다. AI는 제안서 JSON만 내고, 게이트·컷오버·되돌리기는 LLM을 호출하지 않는다.
- 상태가 불명확하면 SAFE_HOLD. 업무 쓰기를 받는 DB는 항상 최대 하나.

## 2. 구성

| 설계 문서 | Compose 서비스 | 수준 |
|---|---|---|
| 출발지 CNPG | `pg-src` (Postgres 16, publication) | 실제 |
| 목적지 CNPG | `pg-dst` (Postgres 16, subscription) | 실제 |
| PgBouncer | `pgbouncer` (transaction 모드, 1개, ini는 공유 볼륨) | 실제 |
| 출발지 S3 | `s3-src` (SeaweedFS, 무인증 거부) | 실제 |
| 목적지 SeaweedFS | `s3-dst` (SeaweedFS, 처음엔 무인증 읽기 허용) | 실제 |
| 이전 대상 앱 | `app` | 실제 |
| 부하 | `writer` | 실제 |
| 플랫폼 | `platform` (API·상태 머신·Executor·콘솔) | 실제 |
| EKS·RKE2·IAM·KMS·Secrets Manager·OpenBao | 없음 | SIMULATED (패널 표시) |
| Cloudflare 가중치, Tailscale | 없음 | SIMULATED |

- MinIO 공개 이미지는 받을 수 없어(11/2 확인) 양쪽 오브젝트 저장소 모두 SeaweedFS를 쓴다.
- 설계 문서와 다른 점: PgBouncer 1개, 쿠버네티스 없음, 리허설은 네임스페이스 대신 목적지의 리허설 전용 버킷.

스택: Python 3.12, FastAPI, psycopg 3, boto3, pydantic, anthropic SDK. 콘솔은 빌드 없는 단일 HTML + EventSource.

## 3. 이전 대상 앱 (`app`)

설계 문서 8.10 조건 중 플랫폼이 읽는 것만 지킨다.

- 테이블 `messages(id bigserial PK, idem_key text UNIQUE, body text, attachment_key text, created_at timestamptz)`, 앱 전용 역할 `chat_app`.
- `POST /messages` (헤더 `Idempotency-Key`, 같은 키는 한 번만 저장), `GET /messages?after=<id>`.
- DB 접속은 `pgbouncer`만 가리킨다. 트랜잭션은 단건 INSERT.
- 첨부는 boto3 S3 클라이언트, 엔드포인트는 환경 변수.
- Secrets Manager 조회 코드와 CloudWatch 지표 전송 코드는 흔적만 둔다(의존성 탐지용, 실행 안 함). CloudWatch는 규칙으로 등급을 정할 수 없는 애매한 항목 예시다.
- 제외: WebSocket, QR, 채팅 UI.

`writer`: 일정 속도로 `POST /messages`를 호출하고, 실패하면 같은 멱등 키로 재시도한다. 논리 요청 ID를 기록해 DB와 대조한다(유실·중복 건수, 최대 쓰기 공백).

## 4. 상태 머신

```
COLLECT → ANALYZE → PROPOSE → REHEARSE → GATE ─PASS→ AWAIT_APPROVAL → SHIFT → CUTOVER → WATCH → DONE
                       ↑                     │BLOCK
                       └─────────────────────┘ (AI 재시도 3회 초과 → NEEDS_HUMAN)
CUTOVER: 쓰기 전 실패 → ABORT_PRE_WRITE(이전 상태 복원) / 원격 상태 불명 → SAFE_HOLD
WATCH: 목적지 장애 → FAILBACK → DONE_ROLLED_BACK
```

- 정의되지 않은 전이는 거부하고 이벤트로 남긴다.
- 상태와 이벤트는 프로세스 메모리에 둔다(재기동 복구는 범위 밖).

## 5. 시나리오

| 단계 | 내용 | 실제 / 모의 |
|---|---|---|
| COLLECT | 출발지 버킷 접근 설정, 스키마 수집 | 실제 |
| ANALYZE | 앱 소스 정적 탐지 → 의존성 3등급(설정 교체 / 어댑터 추가 / 코드 수정). 규칙이 등급을 못 정한 항목만 Claude가 분류하고 이유를 씀 | 실제(정규식) + 실제 호출 |
| PROPOSE 1회차 | 규칙 기반 기본 이전안: 목적지 버킷 생성만, 접근 설정 미번역 | 실제 |
| GATE 1차 | 아래 게이트 질문 → G1에서 **BLOCK** | 실제 |
| PROPOSE 2회차 | Claude가 접근 설정 번역 제안서 작성 | 실제 호출(`AI_MODE=replay`면 녹화본) |
| REHEARSE | 스키마 검증 → 정책 린트 → 리허설 버킷에 적용 → 무인증 읽기 재시험 | 실제 |
| GATE 재대조 | 실제 목적지 버킷에 적용 후 재질문 → **PASS** | 실제 |
| AWAIT_APPROVAL | Claude가 승인 요약 작성(사람 확인용). 결속 값(plan_id, 제안서 digest, 게이트 결과 ID, 만료)은 코드가 표시하고 검증 | 실제 |
| SHIFT | 가중치 10 → 50 → 100% 표시 | SIMULATED |
| CUTOVER | 6절 순서 | 실제 |
| WATCH → FAILBACK | 장애 주입 버튼(트리거는 모의) → 역방향 복제로 되돌리기 | 트리거 SIMULATED, 절차 실제 |

게이트 질문 (하나라도 다르거나 UNKNOWN이면 BLOCK, LLM 미사용):

| ID | 질문 | 방법 |
|---|---|---|
| G1 | 인증 없이 첨부 객체를 읽을 수 있는가 | 양쪽에 무인증 GET |
| G2 | 스키마(테이블·컬럼·제약) 해시가 같은가 | information_schema 해시 |
| G3 | 복제 지연이 기준 이하인가 | LSN 비교, 기준은 설정값(임시) |

통제 비교표(6개 통제)는 SIMULATED 고정값으로 표시한다.

## 6. 컷오버·되돌리기

설계 문서 8.6 순서를 1개 PgBouncer 기준으로 줄인다.

1. G3 재확인, epoch 증가, 타이머 시작
2. `PAUSE` (deadline 초과 → ABORT_PRE_WRITE)
3. 출발지 펜싱: `chat_app`의 INSERT 권한 회수, 쓰기 거부 시험
4. barrier 행 commit → 목적지 도착 확인
5. 영수증: 행 수, PK 해시, 내용 해시, 시퀀스(목적지 시퀀스는 올리기만)
6. 정방향 구독 중지, 역방향 구독 생성(`copy_data=false`), 전환 확정 기록
7. 목적지 `chat_app` 쓰기 허용, PgBouncer ini를 목적지로 바꿔 `RELOAD` → `RESUME`
8. 첫 쓰기 ACK에서 타이머 종료 = 쓰기 정지 시간(`LOCAL`)

- 1~5단계 실패: ABORT_PRE_WRITE. 6단계 이후 실패: FAILBACK. 원격 상태 불명: SAFE_HOLD.
- 되돌리기는 방향만 바꿔 같은 순서로 진행하고 영수증을 `direction=failback`으로 남긴다.

## 7. AI

설계 문서 8.3의 에이전트 역할 중 3개를 넣는다.

| 역할 | 단계 | 2×2 위치 | 검증 |
|---|---|---|---|
| 애매한 의존성 분류 | ANALYZE | AI 제안 + 사람 판단 | 스키마 검증, 등급은 3개 중 하나만 허용 |
| 번역 제안서 (버킷 접근 설정) | PROPOSE | AI 시도, 코드가 합격선 고정 | 스키마 → 린트 → 리허설 → 게이트 |
| 승인 요약 | AWAIT_APPROVAL | AI 작성 + 사람 확인 | 결속 값은 코드가 따로 표시 |

뺀 역할: 실패 원인 추정, 사후 리포트 초안, 교차 검토(P1).

- 모델은 `.env`의 `ANTHROPIC_MODEL`(claude-sonnet-5-5). 모두 구조화 출력(JSON 스키마)으로 받는다.
- 번역 제안서: 입력은 G1 판정과 출발지·목적지 접근 설정. 출력은 설계 문서 8.3 예시 형식(`step, source, target, proposal, rationale, risk`).
- 정책 린트: 와일드카드 principal 금지, `anonymous`는 `deny`만 허용, action은 허용 목록 안에서만.
- 도구 계층: 에이전트 요청은 화이트리스트를 거친다. 파괴적 명령(버킷 삭제 등)은 차단하고 기록한다. 시연에서는 차단 장면을 버튼으로 주입하며, 화면에 주입했다고 표시한다.
- 재시도: 검증 실패 시 사유를 넣어 다시 요청하고, 3회를 넘기면 NEEDS_HUMAN.
- `AI_MODE=live|replay`. live 응답은 저장해 두고, replay는 저장된 응답을 쓴다.

## 8. API

설계 문서 8.9의 부분 집합. 실행 요청(POST)은 모두 `Idempotency-Key`를 받는다. 같은 키에 같은 입력이면 저장된 응답을 돌려주고, 다른 입력이면 409.

| 메서드 | 경로 | 내용 |
|---|---|---|
| POST | `/v1/plans` | 계획 생성, COLLECT·ANALYZE 실행 |
| POST | `/v1/plans/{id}/gate` | 게이트 실행 |
| POST | `/v1/plans/{id}/rehearsals` | PROPOSE → REHEARSE |
| POST | `/v1/plans/{id}/approvals` | 결속 승인 (값이 현재와 다르면 거부) |
| POST | `/v1/plans/{id}/shift`, `/cutover`, `/failback` | 단계 실행 |
| POST | `/v1/plans/{id}/faults` | 데모 전용: 목적지 장애, 파괴적 명령 주입 |
| GET | `/v1/plans/{id}` | 현재 상태 |
| GET | `/v1/plans/{id}/events` | SSE, `Last-Event-ID`로 재접속 |
| GET | `/` | 콘솔 |

## 9. 콘솔 (한 화면)

상태 진행 막대 · 환경 패널(출발지/목적지, SIMULATED 항목 표시) · 의존성 목록 · 게이트 표 · 제안서와 검증 단계·차단 기록 · 승인 버튼(결속 값) · 컷오버 단계 타이머와 영수증 · writer 집계(요청·재시도·유실·중복·최대 공백) · 이벤트 로그 · 장애 주입 버튼

## 10. 범위 밖

쿠버네티스·실제 클라우드, 인증, 다중 계획 동시 실행, 재기동 복구, 감사 로그 영구 저장, 의존성 지도 시각화, 채팅 데모 앱
