# 시스템 아키텍처

## 구성과 경계

```text
Coding agent
  -> FastAPI /v1/messages
  -> MiddlewareChain
       PCFI -> Compress -> Forward/retrieve -> Write guard
       -> Orphan marker scan -> Write remap -> Telemetry
  -> Upstream LLM

Redis: hot context, ToolResult, session index, line mapping
SQLite (optional): persistent evidence fallback
PostgreSQL: request telemetry and operational metrics view
Admin UI: 설정·상태·측정·context 검사
```

| 경계 | 책임 | 주요 위치 |
|---|---|---|
| API | Anthropic 호환 요청 검증, 세션 식별, 응답/SSE 반환 | `src/ccim/api/`, `src/ccim/main.py` |
| Middleware | 요청별 처리 순서와 진단 정보 관리 | `src/ccim/middleware/chain.py` |
| 압축 | 코드 AST, 구조화 출력, 텍스트 span을 보수적으로 치환 | `src/ccim/compress/` |
| 복구/evidence | 원문·metadata 저장, persistent fallback, retrieve·guard 판단 | `src/ccim/reversibility/` |
| Provider | Anthropic/OpenAI 형식 변환과 upstream 호출 | `src/ccim/llm/` |
| 안전 | PCFI와 원문 확인 전 write 방지 | `src/ccim/pcfi/`, `src/ccim/middleware/chain.py` |
| 관측 | PostgreSQL 기록, OTel, Admin UI/비교 도구 | `src/ccim/telemetry/`, `tools/admin_ui/`, `tools/compare/` |

## 처리 순서의 의미

- PCFI는 신뢰되지 않은 입력을 분리·판정하는 첫 방어 계층이다.
- 압축은 forwarding 전 수행하고, 생성한 context ID와 diagnostics를 요청 context에 남긴다.
- Forward/retrieve 단계는 upstream tool loop 안에서 원문 요청을 해석한다.
- write guard는 모델이 생성한 write tool use를 검증하므로 forward 뒤에 있다.
- orphan scan과 write remap은 marker 유출 및 line mapping 문제를 후속 보정한다.
- telemetry는 앞 단계의 결과를 응답 경로와 분리해 기록하되, 기능 판단에 필요한 flag를 남긴다.

## 권한과 실패 모드

| 저장소/경로 | 허용 책임 | 실패 시 동작 |
|---|---|---|
| Redis | hot context 조회·저장·TTL | 압축을 건너뛰거나 persistent store에서 복구; 원문을 확인할 수 없으면 guard가 차단 |
| SQLite evidence store | hash 기반 context 영속·lazy warm load | Redis에 원문이 있으면 계속 사용, 둘 다 없으면 retrieve 실패를 명확히 반환 |
| PostgreSQL | telemetry 비동기 기록·측정 | LLM 응답 경로를 막지 않되 관측 손실을 로그/상태로 드러냄 |
| Upstream LLM | 메시지 처리와 tool response | provider 형식 오류·timeout은 API 오류로 전파하고 진단을 기록 |

## 관측 지점

요청마다 원본/전송/출력 토큰, 압축 후보와 skip reason, context ID, retrieve 호출·cache/persistent hit, evidence reload, guard decision, stream 모드, 각 단계 지연을 기록한다. 새 기능은 이 중 최소 하나의 상태를 검증 가능하게 확장해야 한다.
