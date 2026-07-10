# 개발 워크플로와 검증

## 변경 전 확인

1. [목표와 범위](../foundation/goal-and-scope.md)에서 요청이 CCIM의 압축·복구·안전·관측 경계에 맞는지 확인한다.
2. 영향을 받는 처리 루프와 context metadata/guard/telemetry 영향을 적는다.
3. 기존 코드와 관련 unit test·fixture를 읽고, 구현 전 기대 상태와 차단 조건을 정한다.
4. provider 또는 write tool schema를 바꾸면 지원 행렬과 fixture를 함께 갱신한다.

## 구현 규칙

- 새 압축 경로는 원문 저장, marker/context metadata, retrieve 경로, diagnostics를 분리하지 않는다.
- source kind별 text 처리와 AST 코드를 같은 heuristic으로 뭉개지 않는다.
- write와 evidence guard는 “모델이 그럴듯하게 답했다”가 아니라 retrieve·session·version 검증에만 의존한다.
- telemetry는 부가 로그가 아니라 기능의 검사 인터페이스다. 새 decision/reason은 구조화 feature flag 또는 안정된 필드로 남긴다.
- live LLM 호출이 없어도 fixture에서 재현할 수 있는 검증을 우선한다.

## 기본 검증 명령

```powershell
uv run pytest tests/unit -q
uv run ruff check src tests tools/admin_ui tools/compare
uv run python -m py_compile src/ccim/middleware/chain.py tools/admin_ui/html.py
uv run python tools/compare/check_task2_semantics.py tools/compare/workspace/q2
```

변경한 영역에 맞는 좁은 테스트를 먼저 실행한다. 예를 들어 middleware 변경은 `tests/unit/test_middleware_chain.py`, evidence 저장소 변경은 `tests/unit/test_reversibility.py`, Admin UI 변경은 `tests/unit/test_admin_ui_app.py`와 `tests/unit/test_admin_measure.py`를 우선한다.

## 수동 운영 확인

- Admin UI: `uv run python tools/admin_server.py`
- A/B measure: `uv run python tools/compare/measure.py --compare <left> <right> --since 120 --verbose`
- 직접 압축 경로: `uv run python tools/compare/direct_test.py --session direct-check`

PowerShell에서 한글이 깨져 보이면 파일 인코딩을 변경하지 말고 먼저 콘솔 출력 인코딩을 UTF-8로 설정해 확인한다. 저장소 문서는 UTF-8로 유지한다.
