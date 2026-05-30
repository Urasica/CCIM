# 토큰 사용량 비교 테스트

Roo Code가 동일한 작업을 수행할 때:
- **baseline**: CCIM 압축 OFF (토큰 원본 그대로 전송)
- **compressed**: CCIM 압축 ON (Python 코드 본문 마스킹 후 전송)

두 경우의 실제 LLM 입력 토큰을 PostgreSQL에 기록해 비교합니다.

---

## 사전 조건

```
Redis, PostgreSQL, CCIM 서버 기동 상태
Roo Code: base URL = http://localhost:8080/v1
```

---

## Run A — baseline (압축 OFF)

### 1. 서버 시작

```powershell
cd v1
$env:CCIM_COMPRESSION_TRIGGER_TOKENS = "999999"
$env:CCIM_COMPRESSION_ENABLE_RETRIEVE = "false"
$env:CCIM_SESSION_PREFIX = "baseline"
uv run ccim
```

또는 `.env` 파일에서:
```
CCIM_COMPRESSION_TRIGGER_TOKENS=999999
```

### 2. Roo Code 세션 ID 설정

Roo Code가 보내는 요청 헤더에 세션을 구분할 수 있도록,
`.env`에 추가하거나 Roo Code 설정의 Custom Headers에 입력:

```
x-ccim-session: baseline-run1
```

> Roo Code → Settings → API → Custom Headers 에서 설정 가능

### 3. 태스크 실행

`tools/compare/task.md` 내용을 Roo Code에 붙여 넣고 실행합니다.

---

## Run B — compressed (압축 ON)

### 1. 서버 재시작

```powershell
cd v1
$env:CCIM_COMPRESSION_TRIGGER_TOKENS = "4000"
$env:CCIM_COMPRESSION_ENABLE_RETRIEVE = "true"
uv run ccim
```

### 2. 세션 ID 변경

```
x-ccim-session: compressed-run1
```

### 3. 동일한 태스크 재실행

`tools/compare/task.md`를 새 Roo Code 채팅에 붙여 넣고 실행합니다.

---

## 결과 비교

두 실행이 끝나면:

```powershell
cd v1
uv run python tools/compare/measure.py --compare baseline compressed
```

예시 출력:
```
  토큰 사용량 비교: baseline  vs  compressed
  ============================================================
  항목                         baseline         compressed       변화
  ---------------------------- ---------------- ---------------- ------
  요청 수                      12               11
  입력 토큰 (원본)              48,320 t         49,100 t         ▲ 1.6%
  입력 토큰 (전송)              48,320 t         31,240 t         ▼ 35.3%
  출력 토큰                     6,200 t          6,100 t          ▼ 1.6%
  총 토큰                       54,520 t         37,340 t         ▼ 31.5%
  평균 지연                     1,240.0 ms       1,380.0 ms       ▲ 11.3%
  retrieve_original 호출        0                8

  입력 토큰 절감: 17,080 tokens  (35.3%)  ✓ 목표 달성 (30%+)
  ============================================================
```

요청별 상세:

```powershell
uv run python tools/compare/measure.py --compare baseline compressed --verbose
```

최근 30분 데이터만:

```powershell
uv run python tools/compare/measure.py --compare baseline compressed --since 30
```

---

## 포인트 체크리스트

| 체크 | 설명 |
|------|------|
| `입력 토큰 (전송)` | 실제 LLM에 전달된 토큰. baseline == compressed이면 압축 미작동 |
| `retrieve_original 호출` | compressed 런에서 > 0이면 LLM이 압축 마커를 인식하고 원본을 요청한 것 |
| `평균 지연 증가` | 압축 처리 + retrieve 루프 오버헤드 |
| `출력 토큰` | 두 런이 비슷해야 응답 품질 동등 |

---

## 트러블슈팅

**PostgreSQL에 데이터가 없음**

CCIM 로그에서 아래 메시지 확인:
```
PostgreSQL unavailable — telemetry disabled
```
→ PostgreSQL이 실행 중인지, `CCIM_DATABASE_URL`이 맞는지 확인

**압축이 발동하지 않음 (baseline == compressed)**

- `tools/compare/reference_pipeline.py`를 읽어야 컨텍스트가 충분히 커집니다
- Roo Code가 `reference_pipeline.py`를 실제로 읽었는지 확인
- `CCIM_COMPRESSION_TRIGGER_TOKENS=4000` 설정 적용 여부 확인

**retrieve_original이 0**

- 압축이 일어났더라도 LLM이 압축 마커를 참조하지 않으면 0
- 코드를 편집하거나 함수 본문을 분석하는 단계에서 호출됩니다
