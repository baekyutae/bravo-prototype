# bravo-prototype

Team Bravo 본선 설계 문서 v0.1의 S1~S4를 로컬 Docker Compose 위에서 끝까지 실행하는 축소판 이전 플랫폼.
11/2 팀 회의 시연용이며, 팀 공식 코드로 그대로 옮기지 않는다. 범위와 실제/모의 구분은 [docs/spec.md](docs/spec.md).

## 실행

준비: Docker, 저장소 루트의 `.env`

```
ANTHROPIC_API_KEY=...
ANTHROPIC_MODEL=claude-sonnet-5-5
```

```bash
./scripts/reset.sh                    # 전체 재구축 → S1 직전 상태 (약 30초)
AI_MODE=replay ./scripts/reset.sh     # API 호출 없이 저장된 AI 응답 사용 (시연 권장)
```

콘솔: http://localhost:8000 · 인프라 확인: `./scripts/check_infra.sh`

컷오버와 되돌리기는 DB 권한 · 구독 · 진입점을 실제로 바꾼다. 다시 시연하려면 `reset.sh`를 실행한다.

## 시연 순서

콘솔의 "다음 단계" 버튼을 위에서부터 누른다. 30초 버전은 replay 모드에서 굵게 표시한 말만 한다.

| # | 누를 것 | 화면에서 볼 것 | 말할 것 |
|---|---|---|---|
| 1 | 새 이전 계획 | 의존성 표: CloudWatch만 AI 분류 | 탐지는 규칙, 애매한 것만 AI |
| 2 | 게이트 실행 | 토폴로지 s3-dst 빨간 테두리, G1 BLOCK | **원본에선 거부되던 접근이 대상에선 허용 → 차단** |
| 3 | AI 보완 시작 | AI 시도 1 거부 → 시도 2 통과 → 리허설 → 목적지 반영 | **AI는 제안만, 코드가 검증하고 틀리면 돌려보냄** |
| 4 | 게이트 재실행 | G1 거부/거부 → PASS | |
| 5 | 결속 값으로 승인 | AI 요약 + plan · 제안서 digest · gate_id | 값이 바뀌면 승인 무효 |
| 6 | 부하 시작 → 전환 시작 | 가중치 막대 (SIMULATED) | |
| 7 | 컷오버 실행 | 쓰기 화살표가 pg-dst로, 복제 방향 반전, 8단계 · 영수증 MATCH · 유실 0 | **쓰기 정지 수백 ms, 유실 · 중복 0 (로컬 측정)** |
| 8 | 수동 실행 › 목적지 장애 | 화살표가 pg-src로 복귀, DONE_ROLLED_BACK | **목적지에 쓴 데이터까지 들고 복귀** |

곁들일 장면 (수동 실행 메뉴): 잘못된 제안서 주입 → 린트 거부, 파괴적 명령 주입 → 도구 계층 차단, 순서 밖 버튼 → 409.

## 구성

| 서비스 | 역할 | 포트 |
|---|---|---|
| `pg-src` · `s3-src` | 출발지 DB · 오브젝트 저장소 (AWS 역할) | 15432 · 18333 |
| `pg-dst` · `s3-dst` | 목적지 DB · 오브젝트 저장소 (온프렘 역할) | 25432 · 28333 |
| `pgbouncer` | 앱의 유일한 DB 진입점 | 6432 |
| `app` · `writer` | 이전 대상 앱 · 부하 생성기 | 8080 · 8090 |
| `platform` | API · 상태 머신 · 게이트 · 에이전트 · Executor · 콘솔 | 8000 |

## 코드와 설계 문서 작업 대응

역할 분담 논의용. 각 파일은 해당 작업의 축소판이다.

| 설계 문서 작업 | 코드 |
|---|---|
| T10 상태 머신 + API + SSE | `platform/bravo/state.py`, `workflow.py`, `main.py`, `events.py` |
| T16 의존성 인벤토리 | `platform/bravo/collect.py` |
| T19 원본 대조 게이트 | `platform/bravo/gate.py` |
| T11 에이전트 워커 · 도구 계층 · 제안서 스키마 | `platform/bravo/agent.py`, `tools.py` |
| T15 리허설 러너 · 승인 결속 | `platform/bravo/proposal.py`, `workflow.py` (approve) |
| T12 Executor · T13 영수증 · T20 되돌리기 | `platform/bravo/executor.py` |
| T18 부하 · 대조 | `writer/`, `executor.py` (reconcile) |
| T21 콘솔 | `platform/static/index.html`, `platform/bravo/live.py` |
| T6 · T8 출발지 · 복제 경로 (로컬 대체) | `compose.yaml`, `infra/` |

## 한계

- 같은 머신 안의 측정이라 쓰기 정지 · 복제 지연은 실제 AWS ↔ 온프렘 값이 아니다 (화면에 LOCAL 표시).
- EKS · RKE2 · IAM · KMS · OpenBao · Cloudflare는 없다 (SIMULATED 표시). 리허설은 네임스페이스 대신 리허설 버킷.
- PgBouncer 1개, 상태는 메모리 저장(재기동 시 계획 소실), 인증 없음.
- 확인하지 않은 경로: ABORT_PRE_WRITE · SAFE_HOLD 실제 유발, 단절 중 되돌리기 금지.
- AI 1회차 거부는 실제 응답이지만 매번 재현되지 않는다. 같은 장면이 필요하면 replay 모드.
