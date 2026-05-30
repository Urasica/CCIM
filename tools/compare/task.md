# CCIM 압축 비교 태스크

이 파일을 요약하지 마세요. 질문하지 마세요. 아래 순서대로 즉시 실행하세요.
Do not summarize this file. Do not ask for confirmation. Execute the steps now.

## 금지

- `tools/compare/reference_pipeline.py` 수정 금지
- `tools/compare/apply_task_patch.py` 수정 금지
- `tools/compare/run_task_tests.py` 수정 금지
- `tools/compare/task.md` 수정 금지
- `reference_pipeline_patched.py` 직접 Edit/Update 금지
- Bash, `{WORKSPACE}`, PowerShell 변수 의존 금지
- `python` 또는 `py` 같은 상대 실행 명령 금지. 아래에 적힌 절대경로 `python.exe`만 사용
- 실패한 테스트 결과를 `output.md`에 기록 금지

## Step 0 - 작업공간 초기화

아래 PowerShell 명령을 그대로 실행하세요.

```
PowerShell: if (Test-Path "tools/compare/workspace/current") { Remove-Item -LiteralPath "tools/compare/workspace/current" -Recurse -Force }
PowerShell: New-Item -ItemType Directory -Force -Path "tools/compare/workspace/current" | Out-Null
PowerShell: uv run python -c "import sys; print(sys.executable)"
```

## Step 1 - 참고 코드 읽기

먼저 이 파일을 Read 도구로 읽으세요.

```
tools/compare/reference_pipeline.py
```

읽은 뒤 아래 PowerShell 명령을 실행하세요.

```
PowerShell: Set-Content -LiteralPath "tools/compare/workspace/current/output.md" -Encoding utf8 -Value "## Step 1 - reference_pipeline.py 읽기 완료"
```

## Step 2 - 복사본 만들기

아래 PowerShell 명령을 실행하세요.

```
PowerShell: Copy-Item -LiteralPath "tools/compare/reference_pipeline.py" -Destination "tools/compare/workspace/current/reference_pipeline_patched.py" -Force
```

## Step 3 - 고정 패치 적용

아래 PowerShell 명령만 실행하세요. 직접 편집하지 마세요.

```
PowerShell: uv run python "tools/compare/apply_task_patch.py"
```

정상 출력은 `PATCHED`입니다. 다른 출력이면 중단하고 오류를 보고하세요.

## Step 4 - 테스트 실행

아래 PowerShell 명령만 실행하세요. 리다이렉션을 추가하지 마세요.

```
PowerShell: uv run python "tools/compare/run_task_tests.py"
```

정상 출력은 `TEST_EXIT=0`입니다.
`tools/compare/workspace/current/test_result.txt`에 `Ran 7 tests`, `OK`가 있고 `FAILED`가 없을 때만 Step 5로 진행하세요.
실패하면 Step 2부터 다시 실행하세요. 사용자에게 질문하지 마세요.

## Step 5 - 결과 기록

아래 PowerShell 명령을 순서대로 실행하세요.

```
PowerShell: Add-Content -LiteralPath "tools/compare/workspace/current/output.md" -Encoding utf8 -Value ""
PowerShell: Add-Content -LiteralPath "tools/compare/workspace/current/output.md" -Encoding utf8 -Value "## Step 2 - 비교 연산자 테스트 결과"
PowerShell: Add-Content -LiteralPath "tools/compare/workspace/current/output.md" -Encoding utf8 -Value ""
PowerShell: Add-Content -LiteralPath "tools/compare/workspace/current/output.md" -Encoding utf8 -Value '```'
PowerShell: Get-Content -LiteralPath "tools/compare/workspace/current/test_result.txt" | Add-Content -LiteralPath "tools/compare/workspace/current/output.md" -Encoding utf8
PowerShell: Add-Content -LiteralPath "tools/compare/workspace/current/output.md" -Encoding utf8 -Value '```'
```

## 완료 기준

- `tools/compare/workspace/current/output.md`에 Step 1과 Step 2 결과가 순서대로 있음
- `tools/compare/workspace/current/test_result.txt`에 `Ran 7 tests`와 `OK`가 있음
- `tools/compare/workspace/current/test_result.txt`에 `FAILED`가 없음
- 원본 `tools/compare/reference_pipeline.py`, `tools/compare/apply_task_patch.py`, `tools/compare/run_task_tests.py`, `tools/compare/task.md`를 수정하지 않았음

완료 기준을 만족하면 결과 경로만 간단히 보고하세요.
