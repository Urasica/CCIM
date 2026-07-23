# ADR-0003: 클라우드 중립 단일 VM delivery를 사용한다

- 상태: Accepted
- 날짜: 2026-07-23
- 대체: [ADR-0002](ADR-0002-single-aws-vm-cicd.md)

## 맥락

CCIM은 한 대의 VM에서 실제 배포·rollback·backup·운영 관측 경험을 쌓되, 특정 클라우드의 registry, IAM federation 또는 원격 명령 서비스에 종속되지 않아야 한다. AWS ECR·OIDC·Systems Manager 조합은 안전한 선택이지만 GCP, Oracle Cloud 또는 일반 Linux VM으로 이전할 때 delivery control plane을 다시 구현해야 한다.

포트폴리오 단계에서는 다중 node 가용성보다 commit, image digest, migration, readiness, rollback과 복구 증거를 동일한 절차로 반복하는 것이 더 중요하다.

## 결정

- application runtime과 stateful data plane은 Linux VM 한 대의 Docker Compose로 유지한다.
- CI와 image 게시에는 GitHub-hosted runner와 GitHub Container Registry를 사용한다.
- `master` push의 CI가 성공한 경우에만 별도 `workflow_run` delivery가 정확한 source SHA를 checkout한다.
- delivery candidate는 한 번 build하고 migration·`/live`·`/ready` smoke를 통과한 동일 image를 SHA tag로 GHCR에 게시한다.
- 배포 단위는 tag가 아니라 `ghcr.io/...@sha256:...` 형식의 immutable digest다.
- VM에는 `self-hosted`, `linux`, `x64`, `ccim-production` label을 가진 전용 GitHub Actions runner를 설치한다. runner는 outbound HTTPS로 GitHub에 연결하므로 배포를 위한 inbound SSH나 클라우드 원격 명령 API가 필요하지 않다.
- production job은 GitHub `production` environment와 `master` delivery 조건을 모두 만족해야 한다. pull request와 일반 branch workflow는 production runner를 사용하지 않는다.
- VM 배포 스크립트는 digest와 OCI source revision label을 검증하고, Docker Compose migration과 readiness를 통과한 image만 last-known-good state로 기록한다.
- 실패하면 직전 digest를 다시 배포하고 rollback 결과를 비식별 artifact로 남긴다.
- PostgreSQL, Redis와 evidence volume backup은 provider-neutral archive로 만들고 `age` recipient로 암호화한다. 암호화 파일의 외부 보관 위치는 S3, GCS, OCI Object Storage 또는 별도 저장소 중 운영자가 선택한다.
- Roadmap 03 canary는 VM loopback gateway에서 mock provider와 simulated ledger만 사용한다. 실제 provider 호출은 Roadmap 05까지 금지한다.

## 권한과 네트워크 경계

- CI workflow: repository read와 test artifact write만 사용한다.
- publish job: 성공한 `master` CI SHA에 한해 `packages: write`를 사용한다.
- production runner job: repository read와 `packages: read`만 사용한다.
- VM runtime secret과 backup identity는 `/etc/ccim`에 두고 repository, image, Actions artifact에 넣지 않는다.
- Redis와 PostgreSQL은 host port를 게시하지 않는다. gateway도 기본적으로 `127.0.0.1:8080`에만 bind한다.
- 배포 runner는 PR job에 할당하지 않는다. public repository에서는 fork workflow 위험을 줄이기 위해 production environment, branch protection과 workflow/deploy 경로의 code-owner 검토를 함께 사용한다.

## 결과

긍정적 결과:

- AWS, GCP, Oracle Cloud와 일반 x64 Linux VM에서 같은 workflow와 스크립트를 사용한다.
- 별도의 장기 cloud access key 없이 GitHub와 GHCR token 범위만으로 배포할 수 있다.
- source SHA, OCI revision, registry digest, VM last-known-good state와 artifact를 연결할 수 있다.
- 배포를 위해 public SSH 22번 포트를 계속 열 필요가 없다.

감수하는 제약:

- self-hosted runner와 Docker daemon을 직접 patch·감시해야 한다.
- public repository의 self-hosted runner는 GitHub가 권장하는 보수적인 보호 설정이 필요하다.
- VM이 offline이면 production job이 queue에 머물고 자동 배포가 지연된다.
- cloud snapshot, object storage upload와 비용 경보는 선택한 provider에서 별도로 설정해야 한다.
- 단일 VM은 계속 single point of failure이며 무중단 배포를 보장하지 않는다.

## 재검토 조건

- self-hosted runner 보안 경계를 유지할 수 없음
- 배포 queue 또는 VM offline 시간이 반복적으로 운영 목표를 위반함
- backup/restore 시간이 복구 목표를 만족하지 못함
- 서로 다른 환경이 하나의 VM 장애 영역을 공유할 수 없음
- 실제 운영 지표가 관리형 database 또는 다중 runtime node 분리를 정당화함

## 참고

- [GitHub self-hosted runner 추가](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners)
- [self-hosted runner label 사용](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/use-in-a-workflow)
- [GitHub Container Registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
- [checkout v6와 Node.js 24](https://github.com/actions/checkout)
- [upload-artifact v6와 Node.js 24](https://github.com/actions/upload-artifact/releases/tag/v6.0.0)
