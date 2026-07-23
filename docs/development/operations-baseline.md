# 운영 기준선과 CI 계약

## 범위

roadmap 01은 evaluation과 telemetry 운영 루프를 재현 가능하게 만든다. 압축·복구·write guard의 evidence 선택 규칙은 바꾸지 않으며, session·version·retrieve 검증과 차단 사유는 기존 정책을 유지한다.

이번 기준선이 추가하는 검사 경계는 다음과 같다.

- Python 3.12와 `uv.lock`이 일치하지 않으면 로컬 검증과 image build가 실패한다.
- unit, 외부 LLM 없는 integration, semantic golden fixture를 별도 결과로 남긴다.
- 신규 PostgreSQL과 기존 idempotent schema 모두 migration ledger로 검사한다.
- telemetry background write의 pending, success, failure, drop과 drain timeout을 노출한다.
- image는 source commit SHA를 OCI revision label과 tag에 기록하고 `/live`, `/ready`를 통과해야 한다.

## 로컬 검증

```powershell
.\scripts\verify.ps1
```

검증 순서는 lockfile, Ruff, Markdown local link, unit, mock integration, semantic fixture, whitespace다. live provider, API key, Redis와 PostgreSQL은 이 기본 순서에 필요하지 않다.

인프라를 포함한 상태 확인은 다음과 같다.

```powershell
docker compose up -d --build --wait
uv run python -m ccim.migrations check
curl.exe --fail http://127.0.0.1:8080/live
curl.exe --fail http://127.0.0.1:8080/ready
```

## CI job 경계

| job | 책임 | 다음 단계 차단 조건 |
|---|---|---|
| `static` | lockfile, Ruff, Markdown link, whitespace | 하나라도 실패 |
| `unit` | 결정적 unit와 coverage 숫자 요약 | test 실패 또는 artifact safety 실패 |
| `integration` | Redis/PostgreSQL, 신규·기존 DB migration, mock integration, semantic fixture | live provider 없이 재현 실패 |
| `image` | SHA image build, Compose, migration, `/live`, `/ready` smoke | 앞 job 실패 또는 readiness 실패 |

image job은 앞의 세 job이 성공한 뒤에만 실행한다. 이 workflow는 image를 local runner에 만들지만 registry write나 production runner 권한을 사용하지 않는다. 성공한 `master` push 뒤의 GHCR 게시와 단일 VM 배포는 별도 `delivery.yml`에서만 연결한다.

## Artifact 정책

업로드 artifact는 test count, success/failure, duration, aggregate coverage, migration version, semantic pass와 image ID만 포함한다. JUnit 원문, source code, prompt, `.env`, API key, database file과 runner 절대 경로는 업로드하지 않는다.

`scripts/check_artifact_safety.py`가 common API key pattern, credential assignment, key/database file과 사용자 절대 경로를 검사한다. raw test output은 job 내부 진단에만 사용하고 artifact 경로에 포함하지 않는다.

## Runtime 상태

- `/live`는 dependency 상태와 무관한 process liveness다.
- `/ready`는 Redis가 압축에 사용 가능한지, PostgreSQL이 연결됐는지, migration ledger가 현재인지, telemetry writer가 활성화됐는지 확인한다.
- degraded process는 디버깅을 위해 살아 있을 수 있지만 정상 배포로 승격하지 않는다.
- telemetry는 응답 경로를 막지 않고 기록하되, shutdown에서 제한 시간 drain을 수행한다. 실패하거나 취소된 write는 수치로 남는다.

## Network와 secret 경계

Compose는 gateway를 `127.0.0.1:8080`에만 bind한다. Redis와 PostgreSQL에는 host port를 만들지 않고 internal network에 둔다. 운영 VM에서는 provider console, 승인된 private tunnel 또는 private network를 사용하며 public database와 지속적인 배포용 SSH를 열지 않는다.

실제 provider key는 `.env`나 VM runtime secret으로만 주입한다. `.dockerignore`는 `.env`, Git metadata, local DB, log, benchmark output과 개발 artifact를 image build context에서 제외한다.
