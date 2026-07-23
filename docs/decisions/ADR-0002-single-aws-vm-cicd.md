# ADR-0002: 로컬 개발과 단일 AWS VM 자동 배포를 사용한다

- 상태: Superseded by [ADR-0003](ADR-0003-cloud-neutral-single-vm-delivery.md)
- 날짜: 2026-07-22

> 이 결정의 단일 VM·immutable digest·rollback 경계는 유지하지만 AWS ECR·OIDC·Systems Manager 종속 경로는 ADR-0003의 GHCR·self-hosted runner 기반 cloud-neutral delivery로 대체한다.

## 맥락

CCIM은 기능 MVP를 넘어 실제 운용 기록, 배포 실패 대응, token·latency·guard 지표를 축적해야 한다. 그러나 초기 개인 운영 단계에서 Kubernetes, 다중 runtime node, 관리형 Redis/PostgreSQL을 먼저 도입하면 비용과 운영 복잡성이 검증 가치보다 커진다.

동시에 source code 원문과 evidence mapping을 저장하므로 public gateway나 공개 database로 시작할 수 없다. 로컬 개발의 반복 속도를 유지하면서도 CI/CD와 AWS 운영 경험을 재현 가능한 형태로 남길 경계가 필요하다.

## 결정

- 구현과 로컬 검증은 개발자 PC의 branch 작업 트리에서 수행한다.
- branch push와 pull request는 CI만 실행하며 운영 VM을 변경하지 않는다.
- `main`에 반영된 commit이 전체 CI를 통과하면 container image를 한 번 빌드한다.
- image는 AWS ECR에 commit SHA와 immutable digest로 게시한다.
- GitHub Actions는 OIDC로 단기 AWS deploy role을 얻는다. 장기 AWS access key는 GitHub에 저장하지 않는다.
- AWS Systems Manager Run Command로 tag가 일치하는 단일 EC2 VM에 배포 명령을 전달한다. 배포용 인바운드 SSH는 열지 않는다.
- EC2에서는 gateway, Redis, PostgreSQL과 persistent evidence volume을 Docker Compose로 운영한다.
- migration과 readiness smoke가 성공한 digest만 정상 배포로 기록한다. 실패하면 직전 정상 digest로 rollback한다.
- 예약된 일일 canary도 GitHub Actions에서 Systems Manager를 통해 VM 내부 runner를 호출한다.
- daily canary는 현재 계정에 표시된 GPT-5 mini 계열의 하루 2,500,000 무료 공유 token 한도와 별도 안전 여유 안에서 실행한다.
- 무료 사용을 위해 data sharing을 활성화한 project에는 synthetic·공개 가능 fixture만 보내고 개인 실제 작업은 비공유 project와 별도 ledger로 분리한다.

ECR, IAM/OIDC, Systems Manager는 배포 control plane이다. CCIM의 application runtime과 stateful data plane은 EC2 한 대에만 존재한다.

## 권한 경계

- GitHub CI job: repository read와 test artifact write만 허용한다.
- GitHub deploy job: ECR push, 대상 tag의 Systems Manager command 실행, command 상태 조회만 허용한다.
- EC2 instance role: ECR pull과 Systems Manager managed-instance 동작에 필요한 권한만 허용한다.
- runtime secret: repository와 image에 넣지 않고 VM의 제한된 runtime 환경에서만 읽는다.
- network: gateway는 허용된 개발자 경로에만 열고 Redis/PostgreSQL은 public port를 열지 않는다.

## 결과

긍정적 결과:

- 로컬 개발 속도를 유지하면서 push부터 배포까지의 자동화 기록을 남길 수 있다.
- commit SHA, image digest, migration version, 배포 command를 연결할 수 있다.
- 장기 AWS key와 public SSH 없이 배포할 수 있다.
- 한 대의 VM만 운영하므로 초기 비용과 장애 분석 범위가 작다.

감수하는 제약:

- VM 장애나 재부팅 동안 서비스가 중단되는 single point of failure다.
- application과 Redis/PostgreSQL이 자원을 공유한다.
- 무중단 배포를 보장하지 않으며 짧은 교체 시간을 허용한다.
- volume backup, restore, disk saturation, memory pressure를 직접 관리해야 한다.

## 재검토 조건

다음 중 하나가 실제 telemetry나 장애 기록으로 확인될 때만 runtime 분리를 검토한다.

- 서로 독립적인 사용자 또는 환경이 하나의 VM 장애 영역을 공유할 수 없음
- Redis/PostgreSQL 자원 경합이 반복적으로 SLO를 위반함
- 배포 중단 시간이 허용 범위를 넘음
- backup/restore 시간이 복구 목표를 만족하지 못함
- 단일 VM 비용이 관리형 서비스 또는 다중 node보다 비효율적임

## 참고

- [GitHub Actions에서 AWS OIDC 구성](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws)
- [AWS Systems Manager Run Command](https://docs.aws.amazon.com/systems-manager/latest/userguide/run-command.html)
