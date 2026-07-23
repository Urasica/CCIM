# CCIM (Coding-agent Context & Integrity Middleware)

CCIM은 코딩 에이전트와 LLM API 사이에서 동작하는 로컬 게이트웨이입니다.

긴 코딩 작업에서는 큰 파일의 `Read` 결과, 테스트 로그, 도구 출력이 여러 요청에 반복해서 포함됩니다. CCIM은 이런 컨텍스트를 보수적으로 압축해 전송량을 줄이고, 원문은 별도로 보관해 필요할 때 다시 확인할 수 있게 합니다. 압축된 내용을 근거로 파일을 수정할 때는 원문 확인 여부를 검사하고, 요청별 토큰·지연·복구·차단 결과도 기록합니다.

즉, CCIM은 텍스트 요약기가 아니라 **압축, 원문 복구, 수정 안전성, 관측을 하나의 요청 흐름에서 관리하는 미들웨어**입니다.

## 왜 필요한가

코딩 에이전트는 보통 큰 파일을 읽은 뒤 여러 턴에 걸쳐 분석, 수정, 테스트를 반복합니다. 이때 이전의 긴 도구 결과가 계속 전송되면 다음 문제가 생깁니다.

- 입력 토큰과 비용이 증가합니다.
- 오래된 로그와 반복 내용이 중요한 정보를 가립니다.
- 본문을 단순히 줄이면 함수 관계나 필드 값 같은 사실이 사라질 수 있습니다.
- 압축된 파일을 원문 확인 없이 수정하면 잘못된 위치나 내용을 변경할 수 있습니다.
- 어떤 요청이 왜 압축되거나 차단됐는지 알기 어렵습니다.

CCIM은 반복 내용을 줄이되, 원본과 구조적 사실을 함께 관리해 이런 위험을 낮춥니다.

## 동작 방식

```text
Coding Agent
    |
    | Anthropic Messages 또는 OpenAI Chat Completions 요청
    v
CCIM Gateway
    |
    | 1. 입력 검사
    | 2. 큰 코드와 도구 결과 압축
    | 3. 원문 저장 및 필요 시 복구
    | 4. write 안전성 검사와 telemetry 기록
    v
Upstream LLM
```

1. 코딩 에이전트의 요청을 공통 형식으로 정규화합니다.
2. 큰 코드, `Read` 결과, ToolResult, 로그에서 압축할 부분을 선택합니다.
3. 모델이 이해하는 데 필요한 구조와 사실은 남기고, 긴 원문은 Redis에 저장합니다.
4. 모델이 원문을 요청하면 `retrieve_original`을 통해 같은 세션과 버전의 내용을 복구합니다.
5. 압축된 내용을 바탕으로 write 도구를 호출하면 필요한 원문이 확인됐는지 검사합니다.
6. 토큰, 지연, 압축 사유, 복구와 차단 결과를 PostgreSQL과 Admin UI에서 확인할 수 있게 기록합니다.

## 주요 기능

- **컨텍스트 압축**: Python, Java, C# 코드와 반복되는 ToolResult·로그·문서 구간을 입력 성격에 맞게 줄입니다.
- **사실 보존**: 함수 시그니처, 호출 관계, 주요 값처럼 코드 이해에 필요한 정보를 압축본에 남깁니다.
- **원문 복구**: 압축한 원문을 Redis에 저장하고, 선택적으로 SQLite 저장소를 사용해 재시작이나 TTL 만료에 대비합니다.
- **write 안전 장치**: 현재 턴에서 압축된 파일을 수정할 때 관련 원문의 복구 여부와 지원되는 write 형식을 확인합니다.
- **입력 신뢰 경계**: 코드·로그·문서 안의 지시문이 사용자 명령처럼 취급되지 않도록 검사합니다.
- **호환 API**: Anthropic Messages와 OpenAI Chat Completions 요청을 받아 Anthropic, OpenAI 또는 OpenAI-compatible upstream으로 전달합니다.
- **관측과 비교**: 요청별 토큰, 지연, 압축, 복구, guard 결과를 Admin UI와 비교 도구에서 확인합니다.

## 최근 결과와 성능 지표

저장소에 기록된 최신 비교에서는 같은 task2 시나리오를 비압축 `q1`과 current-turn 압축 `q2`로 실행했습니다.  
두 실행은 같은 2시간 구간에서 각각 9개 요청을 사용했으며, `q2` 산출물은 원본 코드의 주요 사실을 확인하는 semantic checker를 통과했습니다.

![q1과 q2 비교](img/compare.png)

| 항목 | q1 비압축 | q2 current-turn 압축 |
|---|---:|---:|
| 요청 수 | 9 | 9 |
| 원본 입력 | 331,732 | 331,754 |
| 전송 입력 | 331,732 | 259,815 |
| 출력 | 5,550 | 6,115 |
| 전송 합계 | 337,282 | 265,930 |
| 입력 절감 | 0 (0%) | 71,939 (21.7%) |
| 평균 지연 | 8,482 ms | 11,972 ms |



- 두 실행의 원본 입력 규모가 거의 같아 A/B 비교 가능.
- `q2`는 전송 입력을 71,939 토큰, 21.7% 줄이면서 semantic checker를 통과.
- 평균 지연은 8,482 ms에서 11,972 ms로 증가.  
- 토큰 절감과 사실 보존을 위한 압축·저장·검사 비용이 포함된 결과입니다.
- 이 수치는 실제 요청 전체를 비교한 결과이며, 압축기만 따로 실행한 수치와 구분해야 합니다.

대형 반복 코드 파일 하나를 AST 압축기에 직접 넣은 측정에서는 다음 결과가 나왔습니다.

| 항목 | 압축 전 | 압축 후 | 절감 |
|---|---:|---:|---:|
| 추정 토큰 | 12,708 | 702 | 12,006 (94.5%) |
| 바이트 | 54,107 | 2,727 | 51,380 (95.0%) |
| AST context 수 | - | 3 | - |

단일 파일 결과는 압축기 자체의 상한에 가까운 예시입니다.  
실제 LLM 요청에는 시스템 메시지, 사용자 지시, 도구 호출, 출력과 사실 보존 정보가 함께 포함되므로 전체 절감률은 더 낮아집니다.

## 빠른 시작

### 준비 사항

- Docker와 Docker Compose
- 사용할 LLM provider의 API key
- 로컬 CLI와 Admin UI를 사용할 경우 Python 3.12와 [uv](https://docs.astral.sh/uv/)

### 1. 환경 설정

예제 설정을 복사합니다.

```powershell
Copy-Item .env.example .env
```

`.env`에서 provider, API key, model을 실제 값으로 바꿉니다.

```env
CCIM_LLM_PROVIDER=openai
OPENAI_API_KEY=replace-me
CCIM_LLM_MODEL=gpt-5-mini
```

Provider별 지원 범위와 필요한 설정은 [호환성 행렬](docs/development/compatibility-matrix.md)을 참고하세요.

### 2. 실행

```powershell
docker compose up -d --build --wait
```

Gateway는 기본적으로 `http://127.0.0.1:8080`에서 동작합니다. Redis와 PostgreSQL은 Docker 내부 네트워크에만 노출됩니다.

상태를 확인합니다.

```powershell
curl.exe --fail http://127.0.0.1:8080/live
curl.exe --fail http://127.0.0.1:8080/ready
```

- `/live`: 프로세스가 응답 가능한지 확인합니다.
- `/ready`: Redis, PostgreSQL, migration, telemetry를 포함해 요청을 처리할 준비가 됐는지 확인합니다.

### 3. 코딩 에이전트 연결

Anthropic Messages 호환 클라이언트는 `/v1/messages`, OpenAI Chat Completions 호환 클라이언트는 `/v1/chat/completions`를 사용합니다.

Claude Code가 설치되어 있다면 CCIM CLI로 현재 프로세스에만 필요한 endpoint 환경을 적용할 수 있습니다.

```powershell
uv run ccim doctor
uv run ccim run --session local -- claude
```

`doctor`는 설정과 의존성을 읽기 전용으로 검사합니다. `run`은 사용자의 전역 Claude Code 설정을 변경하지 않습니다.

### 4. 종료

```powershell
docker compose down
```

## Admin UI

설정과 서비스 상태, 요청별 측정 결과, 저장된 context를 브라우저에서 확인할 수 있습니다.

```powershell
uv run python tools/admin_server.py
```

접속 주소:

```text
http://127.0.0.1:8090
```

## 개발과 검증

Windows에서는 다음 명령으로 기본 로컬 검증을 실행할 수 있습니다.

```powershell
.\scripts\verify.ps1
```

이 검증은 lint, 문서 링크, 단위 테스트, 외부 LLM이 필요 없는 integration test, 운영 데이터 dry-run과 semantic fixture를 확인합니다.

## 현재 제약

- CCIM은 범용 RAG, 벡터 검색 또는 사내 지식베이스가 아닙니다.
- MCP 기반 압축 workflow와 multimodal ingress는 현재 지원하지 않습니다.
- 검증되지 않은 patch/write 도구 형식은 안전하게 처리할 수 없으므로 차단하거나 unsupported로 반환합니다.
- 스트리밍 요청은 현재 upstream의 complete 응답을 받은 뒤 SSE 형식으로 변환합니다. 실시간 chunk relay는 지원하지 않습니다.
- 압축과 안전 검사는 전송 토큰을 줄이는 대신 추가 지연을 만들 수 있으므로, 절감률과 사실 보존·복구·guard 결과를 함께 평가해야 합니다.

## 더 알아보기

- [개발 문서 안내](docs/README.md)
- [목표와 지원 범위](docs/foundation/goal-and-scope.md)
- [시스템 아키텍처](docs/architecture/system-architecture.md)
- [개발 워크플로와 검증](docs/development/workflow.md)
- [로드맵](docs/development/roadmap.md)
- [단일 VM 배포](docs/development/single-vm-delivery.md)
