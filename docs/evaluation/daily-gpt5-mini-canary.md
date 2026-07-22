# GPT-5 mini 일일 운영 검증 계획

## 목적

단일 AWS EC2에 배포된 CCIM을 매일 같은 수준의 coding-agent 작업으로 통과시켜 압축, 원문 복구, write guard, provider 호출, telemetry와 배포 상태를 함께 확인한다. 이 작업은 synthetic daily benchmark이며 개인 실제 작업 데이터와 분리한다.

하루 작업은 같은 fixture를 compression off와 on으로 각각 실행하는 A/B 한 쌍이다. 무료 token을 소진하는 것이 목표가 아니라, 매일 비교 가능한 품질·절감·지연 표본을 남기는 것이 목표다.

## 무료 일일 사용 조건

이 계획은 2026-07-22 현재 계정의 Data Sharing 설정에 표시된 다음 조건을 전제로 한다.

- `gpt-5-mini`가 포함된 소형 모델군의 공유 traffic 합계에 하루 최대 2,500,000 token이 적용된다.
- 별도의 대형 모델군에는 하루 최대 250,000 token이 적용되지만 이 canary의 예산이나 fallback으로 사용하지 않는다.
- 한도는 model별·project별이 아니라 안내에 열거된 소형 모델군 전체에서 공유한다.
- 무료 counter는 매일 `00:00 UTC`, 한국 시간 `09:00 KST`에 초기화된다.
- 한도를 넘기게 하는 요청은 초과분만이 아니라 해당 요청 전체가 정상 요금으로 청구된다.
- 입력·출력을 OpenAI와 공유하도록 opt-in한 project의 적격 traffic에만 무료 사용이 적용된다.
- fine-tuned model, fine-tuning training, evals와 tool use는 무료 사용 대상에 포함되지 않는다.
- 계정에 positive balance가 있어야 하며 프로그램과 대상 model은 변경될 수 있다.

운영 전에는 Data Sharing 설정의 등록 문구와 Usage Dashboard의 `data sharing incentive tier`를 확인한다. CCIM의 local ledger는 이 canary project의 사용량만 보여주는 보조 자료이고, model군 전체의 실제 무료 적용 여부는 OpenAI Usage와 Costs 화면을 기준으로 판단한다.

민감한 개인·고객·회사 code를 무료 token 때문에 공유하지 않는다. daily benchmark에는 synthetic fixture, 공개 가능한 code와 비식별 log만 사용한다. 개인 실제 작업은 별도의 비공유 project에서 운영하며 이 무료 예산에 포함됐다고 가정하지 않는다.

공식 조건은 [OpenAI의 data sharing 및 complimentary token 안내](https://help.openai.com/en/articles/10306912-sharing-feedback-evaluation-and-fine-tuning-data-and-api-inputs-and-outputs-with-openai)에서 다시 확인한다.

## 일일 token 예산

- 일일 공유 token 상한: 2,500,000 provider-reported input+output token
- model: `gpt-5-mini-2025-08-07` snapshot
- 기준일 경계: `00:00 UTC`부터 다음 `00:00 UTC` 직전까지
- run당 hard cap: 900,000 token
- request envelope: input 최대 180,000, output 상한 20,000, 합계 최대 200,000 token
- 일일 model 호출 hard stop: 소형 모델군 누적 2,100,000 token

| 용도 | 일일 상한 | 의미 |
|---|---:|---|
| baseline run | 900,000 | CCIM compression off |
| compressed run | 900,000 | CCIM compression on |
| 일시 오류 진단·부분 재실행 | 300,000 | 자동 전체 재실행 금지 |
| 사용하지 않는 안전 여유 | 400,000 | 집계 지연과 다음 요청 전체 과금 방지 |
| 합계 | 2,500,000 | 일일 무료 공유 traffic 한도 |

위 수치는 소비 목표가 아니라 상한이다. 정상 run이 더 적은 token으로 끝나면 남은 quota를 채우기 위한 추가 호출을 만들지 않는다.

매 request 전에 다음을 모두 확인한다.

1. `00:00 UTC` 이후 이 model군의 알려진 사용량을 합산한다.
2. 예상 input과 설정한 max output을 더한 request envelope가 200,000 이하인지 확인한다.
3. 현재 누적과 request envelope의 합이 2,100,000을 넘으면 호출하지 않는다.
4. 하루 중 다른 project가 같은 model군을 사용했거나 전체 사용량을 확신할 수 없으면 canary를 skip한다.

token quota와 비용은 같은 값으로 취급하지 않는다. raw input, cached input, output, reasoning을 provider usage가 제공하는 범위에서 분리하고 비용은 가격 기준일과 함께 별도로 계산한다. 무료 적용 여부는 Usage Dashboard의 activity와 Costs를 대조해 확인한다.

2026-07-22 공식 기준에서 GPT-5 mini는 400,000 context window와 128,000 max output을 제공하지만, canary는 위 request envelope를 더 낮은 운영 상한으로 사용한다. 표준 API 가격은 1M token당 input $0.25, cached input $0.025, output $2.00이다. 최신 값은 [GPT-5 mini model page](https://developers.openai.com/api/docs/models/gpt-5-mini)에서 다시 확인한다.

## 표준 일일 작업

### Fixture

- 4,000~6,000 LOC의 synthetic Python service
- 서로 연결된 production module 3~5개
- 1,500~2,000 LOC의 반복 pipeline 또는 handler file 1개
- 공개 API와 call/non-call 관계를 확인하는 test 30~40개
- 성공·실패·retry가 섞인 800~1,200줄의 구조화 test log
- retry/idempotency, cache invalidation, version mismatch를 순환하는 fault variant
- 매 run 전에 immutable template에서 새 임시 workspace로 복사

fixture, 정답과 task에 version을 부여한다. version이 다른 run은 같은 A/B 집단으로 비교하지 않는다. fault variant는 요일이나 run ID로 결정적으로 선택해 모델의 단일 정답 암기를 줄인다.

### 작업 지시 예시

```text
pipeline fixture에서 retry 이후 중복 write와 잘못된 transform 선택이 함께 발생하는 원인을 찾는다.
공개 함수 signature는 바꾸지 말고 production file은 최대 두 곳만 최소 수정한다.
관련 원문과 call site를 확인한 뒤 회귀 test 하나를 추가하고 지정된 test suite를 실행한다.
실패하면 log를 근거로 한 번만 수정 보완한다.
마지막에는 root cause, 변경한 field/call 관계, 실행한 test, 남은 위험을 요약한다.
```

한 run은 최대 900,000 token 안에서 4~6회의 model cycle을 허용하되, request envelope와 run hard cap에 먼저 도달하면 중지한다. 작업은 다음 경로를 사용하도록 설계한다.

1. 큰 `Read`와 ToolResult 입력
2. AST/current-turn compression 후보 생성
3. 필요한 경우 `retrieve_original`
4. current-turn write guard 검증
5. 최대 두 production file의 제한된 write
6. 결정적 test와 semantic checker
7. 실패 log를 사용한 1회 보완
8. token·latency·retrieve·guard telemetry 기록

## 실행 절차

1. GitHub Actions schedule을 `09:10 KST` 이후로 두거나 `workflow_dispatch`로 실행한다.
2. workflow가 OIDC로 AWS deploy role을 얻는다.
3. Systems Manager Run Command가 단일 EC2의 canary runner를 시작한다.
4. runner가 UTC 일일 ledger, 당일 중복 run, 현재 deployment digest를 확인한다.
5. immutable fixture를 baseline과 compressed용 임시 workspace 두 곳에 복사한다.
6. 홀수 날짜는 baseline-first, 짝수 날짜는 compressed-first로 실행 순서를 교대한다.
7. 각 run 전에 request envelope와 일일 model군 누적량을 검사한다.
8. 배포된 gateway의 loopback endpoint로 동일한 task를 실행한다.
9. pytest와 semantic checker를 실행한다.
10. request telemetry에 `canary_run_id`, `task_version`, `deployment_sha`, `image_digest`, `model_snapshot`, `compression_mode`를 기록한다.
11. 원문을 제외한 요약 JSON만 Systems Manager command output과 CI artifact로 보관한다.
12. 두 임시 workspace를 폐기한다.

## A/B 비교 규칙

- 같은 날의 baseline과 compressed run은 같은 deployment SHA, model snapshot, fixture/task version을 사용한다.
- 두 run은 서로 다른 새 workspace와 session을 사용한다.
- compression 관련 설정 외의 차이는 허용하지 않는다.
- original input, sent input, cached input, output, retrieve overhead, net saving, p50/p95 latency, test/semantic 결과를 비교한다.
- 실행 순서를 날짜별로 교대하고 순서별 결과도 보고해 order effect를 숨기지 않는다.
- 한쪽 run이 budget 또는 provider 문제로 끝나지 못하면 그 날의 A/B는 불완전 표본으로 표시한다.

## 성공 조건

- task test와 semantic checker가 모두 통과한다.
- orphan marker가 응답이나 patch에 남지 않는다.
- write가 발생했다면 필요한 context가 retrieve·검증됐다.
- provider usage와 PostgreSQL telemetry가 run ID로 연결된다.
- image digest와 deployment SHA가 현재 VM 상태와 일치한다.
- raw code, prompt, API key, 전체 source path가 CI artifact에 포함되지 않는다.
- Usage와 Costs 화면에서 무료 적용 여부를 사후 확인할 수 있다.

## 실패와 재시도

- provider 429/5xx는 남은 일일 진단 예산 안에서 실패한 request만 같은 날 1회 retry한다.
- deterministic test 또는 semantic failure는 전체 run을 자동 retry하지 않고 실패 표본으로 보존한다.
- telemetry 누락, budget preflight 실패, deployment digest mismatch는 model을 호출하지 않는다.
- 일일 누적 2,100,000 token에 도달하면 retry를 포함한 모든 model 호출을 중지한다.
- 한도를 넘겨 청구된 request가 발견되면 다음 run을 중지하고 local ledger와 Usage Dashboard의 차이를 incident로 기록한다.

## 일일·30일 보고

일일 record에는 다음을 남긴다.

- 기준 UTC date, 실행 순서, 성공·실패·skip·retry 상태
- baseline/compressed의 실제 input·cached input·output token
- 일일 model군 사용량, 무료 적용량, 청구된 overage
- gross/net saving과 retrieve overhead
- test·semantic 결과와 latency
- 배포 SHA, image digest, task/fault version

30일 집계에는 active day, 완전한 A/B pair 수, pass rate, 절감·지연 분포, retrieve miss, guard block/false positive, rollback, provider 비용과 AWS 비용을 포함한다. 2,500,000은 일일 한도이므로 30일 총예산으로 표기하지 않고 날짜별 사용량과 30일 누적량을 함께 표시한다.

daily benchmark 결과는 synthetic으로 표시하고 개인 실제 코딩 작업의 production observation과 별도 표로 공개한다.
