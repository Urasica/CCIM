# CCIM 개발 문서

이 디렉터리는 CCIM의 개발 기준 문서다. 사용자용 기능 소개와 실행 방법은 루트 [README](../README.md)에 두고, 설계 판단·구현 경계·검증 기준은 여기에서 관리한다.

CCIM은 범용 RAG나 도메인 에이전트가 아니다. 코딩 에이전트와 LLM API 사이에서 반복 컨텍스트를 압축하고, 원문 근거를 복구하며, 원문 확인 없는 변경을 차단하고, 그 결과를 관측하는 로컬 게이트웨이다.

## 읽는 순서

1. [목표와 범위](foundation/goal-and-scope.md)와 [문제 정의](foundation/problem-definition.md)
2. [시스템 아키텍처](architecture/system-architecture.md), [도메인 모델](architecture/domain-model.md), [요청·복구 흐름](architecture/event-and-question-flow.md)
3. [근거 및 복구 정책](policies/rag-policy.md)
4. [개발 워크플로](development/workflow.md), [운영 기준선과 CI 계약](development/operations-baseline.md), [운영 데이터 축적 준비 계약](development/operational-data-readiness.md), [로드맵](development/roadmap.md)
5. [평가 계획](evaluation/eval-plan.md)과 [GPT-5 mini 일일 운영 검증](evaluation/daily-gpt5-mini-canary.md)
6. [초기 방향 ADR](decisions/ADR-0001-project-direction.md)과 [단일 AWS VM CI/CD ADR](decisions/ADR-0002-single-aws-vm-cicd.md)

## 문서 유지 규칙

- 코드 변경 전에 영향을 받는 처리 루프, 안전 속성, 검증 경로를 이 문서에서 먼저 갱신한다.
- 현재 구현·확정된 방향·향후 후보를 구분한다. 구현되지 않은 계획을 현재 동작처럼 서술하지 않는다.
- 압축률만으로 기능을 승인하지 않는다. 원문 복구 가능성, 사실 보존, write guard, telemetry 또는 결정적 테스트 중 적어도 하나의 확인 경로가 있어야 한다.
- 이 디렉터리의 책임별 문서만 Git으로 추적하는 설계 기준선으로 사용한다. 임시 실행 계획과 개인 작업 메모는 추적하지 않는다.
