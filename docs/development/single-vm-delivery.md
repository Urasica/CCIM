# 클라우드 중립 단일 VM delivery

## 범위와 상태

Roadmap 03은 GitHub Actions CI를 통과한 `master` commit을 GHCR immutable digest로 게시하고, 단일 Linux VM의 Docker Compose에 배포하는 delivery loop를 구현한다.

repository에 구현된 범위:

- CI와 production delivery workflow 분리
- Node.js 24 기반 `actions/checkout@v6`, `actions/upload-artifact@v6`
- source SHA image build, migration·`/live`·`/ready` smoke, GHCR digest 게시
- production label의 self-hosted runner 배포
- digest·OCI revision 검증, last-known-good 기록과 자동 rollback
- VM 내부 mock-provider operational dry-run
- PostgreSQL·Redis·evidence의 `age` 암호화 backup과 명시적 restore

외부에서 한 번 수행해야 하는 범위:

- VM 생성, disk와 방화벽 설정
- Docker, Python 3.12, `age`, GitHub Actions runner 설치
- GitHub `production` environment와 branch protection 설정
- `/etc/ccim/ccim.env`, backup key와 외부 encrypted archive 보관 위치 설정
- 최초 deployment, rollback과 backup/restore 훈련

## provider-neutral 토폴로지

```text
Local branch
  -> GitHub Actions CI (GitHub-hosted)
  -> successful master push only
  -> build once + migration/readiness smoke
  -> GHCR SHA tag + immutable digest
  -> production environment
  -> self-hosted runner (outbound HTTPS)
  -> one Linux VM on AWS, GCP, Oracle Cloud, or another provider
       -> Docker Compose
            gateway: 127.0.0.1:8080
            Redis: internal network only
            PostgreSQL: internal network only
            evidence volume
```

클라우드 provider API, instance role, workload identity, remote command service는 필수 계약이 아니다. provider는 compute, persistent disk, firewall, optional object storage만 제공한다.

## 1. VM 준비

권장 초기 크기는 x64 Linux, 2 vCPU, RAM 4 GiB 이상, persistent disk 40 GiB 이상이다. 실제 memory·disk telemetry를 확보한 뒤 조정한다.

어느 provider에서도 다음 조건을 맞춘다.

1. Ubuntu 22.04/24.04, Debian 12, Oracle Linux 8 이상 등 GitHub runner와 Docker가 지원하는 x64 Linux VM을 만든다.
2. OS와 Docker data root가 재부팅 후 유지되는 persistent disk에 있는지 확인한다.
3. outbound TCP 443에서 `github.com`, `api.github.com`, `ghcr.io`에 접근할 수 있게 한다.
4. Redis 6379와 PostgreSQL 5432 inbound rule을 만들지 않는다.
5. gateway 8080도 public internet에 열지 않는다. loopback, private network 또는 승인된 tunnel만 사용한다.
6. SSH는 bootstrap 동안 고정된 관리자 source에만 일시 허용하거나 provider console을 사용한다. runner service가 동작한 뒤 배포 목적의 22번 inbound는 제거할 수 있다.
7. provider disk snapshot과 비용 경보를 별도로 설정한다. 이 항목만 AWS EBS, GCP Persistent Disk, OCI Block Volume마다 절차가 다르다.

VM에 다음 도구를 설치한다.

```text
Docker Engine
Docker Compose plugin
GitHub Actions runner >= 2.327.1
Python >= 3.12
curl
flock (util-linux)
age
```

`checkout@v6`와 `upload-artifact@v6`는 Node.js 24를 사용하므로 self-hosted runner는 최소 `2.327.1`이어야 한다. 설치 시 GitHub repository의 `Settings > Actions > Runners > New self-hosted runner`에 표시되는 최신 package와 명령을 사용한다.

## 2. 전용 runner와 directory

production runner는 application runtime과 같은 VM에 설치하되 전용 비로그인 사용자로 실행한다. 이 runner를 PR이나 일반 CI에 사용하지 않는다.

runner 등록 시 custom label을 추가한다.

```bash
./config.sh \
  --url https://github.com/Urasica/CCIM \
  --token <ONE_TIME_REGISTRATION_TOKEN> \
  --labels ccim-production
sudo ./svc.sh install
sudo ./svc.sh start
```

GitHub가 자동으로 붙이는 `self-hosted`, `linux`, `x64` label과 `ccim-production`이 모두 있어야 한다.

runner 사용자에게 필요한 directory만 준비한다.

```bash
sudo install -d -m 0750 -o <RUNNER_USER> -g <RUNNER_USER> /var/lib/ccim-deploy
sudo install -d -m 0750 -o <RUNNER_USER> -g <RUNNER_USER> /var/backups/ccim
sudo install -d -m 0750 -o root -g <RUNNER_USER> /etc/ccim
sudo install -m 0640 -o root -g <RUNNER_USER> /dev/null /etc/ccim/ccim.env
sudo usermod -aG docker <RUNNER_USER>
```

Docker group은 사실상 root에 가까운 권한이다. production runner 계정에 shell login이나 다른 repository runner를 함께 할당하지 않는다.

public repository의 self-hosted runner는 fork가 위험한 workflow를 만들 가능성이 있으므로 GitHub도 private repository 사용을 우선 권장한다. 공개 상태를 유지한 채 운영할 경우 다음 보호가 모두 필요하다.

- `production` environment의 deployment branch를 `master`로 제한
- `master` branch protection과 CI required check
- `.github/workflows/`, `deploy/`, `docker-compose.yml` 변경의 소유자 검토
- fork workflow 자동 승인을 허용하지 않음
- production runner를 `pull_request` workflow에 절대 지정하지 않음

## 3. runtime secret

`/etc/ccim/ccim.env`에는 최소한 강한 PostgreSQL password와 provider runtime 설정을 둔다. 이 파일은 GitHub secret이나 artifact로 업로드하지 않는다.

```env
CCIM_POSTGRES_PASSWORD=<RANDOM_PASSWORD>
CCIM_LLM_PROVIDER=openai-compatible
CCIM_LLM_MODEL=<MODEL>
CCIM_LLM_BASE_URL=<PRIVATE_OR_LOCAL_ENDPOINT>
OPENAI_API_KEY=<RUNTIME_SECRET>
CCIM_COMPRESSION_ENABLED=true
CCIM_COMPRESSION_ENABLE_RETRIEVE=true
CCIM_CURRENT_TURN_COMPRESSION_ENABLED=true
```

Roadmap 03의 VM canary는 이 provider 설정을 호출하지 않고 deterministic mock fixture만 실행한다.

## 4. GitHub 설정

1. `Settings > Environments`에서 `production`을 만든다.
2. deployment branch를 `master`로 제한한다.
3. 공개 repository라면 required reviewer 또는 동등한 보호 규칙을 적용한다.
4. `Settings > Actions`에서 workflow의 package read/write가 허용되는지 확인한다.
5. 첫 GHCR publish 뒤 package가 repository 권한을 상속하는지 확인한다.
6. self-hosted runner가 online이고 `ccim-production` label을 갖는지 확인한다.
7. 위 준비가 모두 끝난 뒤 repository variable `CCIM_DELIVERY_ENABLED=true`를 만든다.

`CCIM_DELIVERY_ENABLED`가 없거나 `true`가 아니면 delivery workflow는 안전하게 skip된다. `CI` workflow는 PR과 branch에서 test만 수행한다. 활성화된 `Single VM Delivery`는 성공한 `master` push CI의 `head_sha`만 받아 GHCR publish와 production job을 실행한다. 진행 중인 production delivery는 새 push가 취소하지 않는다.

## 5. 배포와 rollback

배포 스크립트는 다음 순서로 실행된다.

1. `ghcr.io/...@sha256:...` 형식 확인
2. image pull
3. OCI `org.opencontainers.image.revision`과 source SHA 일치 확인
4. Compose migration과 dependency health 실행
5. `/live`, `/ready` 확인
6. 성공 digest를 `/var/lib/ccim-deploy/last-known-good.env`에 원자적으로 기록
7. 실패 시 이전 digest로 같은 검증을 수행하고 workflow는 실패로 유지

자동 image rollback은 이전 application이 현재 database schema를 읽을 수 있는 backward-compatible migration을 전제로 한다. migration version을 새로 추가하는 배포는 expand-only 변경으로 만들고 이전 image readiness도 별도 fixture에서 확인한다. 이전 image가 새 schema를 거부하면 rollback을 성공으로 꾸미지 않고 `rollback_status=failed`로 기록한 뒤 encrypted backup restore 또는 수정 image의 roll-forward를 선택한다.

수동 계약 확인은 실제 배포 없이 실행할 수 있다.

```bash
export CCIM_IMAGE_REF="ghcr.io/urasica/ccim@sha256:<64_HEX>"
export CCIM_SOURCE_REVISION="<40_HEX_COMMIT>"
bash deploy/single-vm/deploy.sh --validate-only
```

배포 artifact에는 source SHA, image digest, 성공·실패 단계와 rollback 상태만 포함한다. runtime env, provider key, 원문과 host 절대 경로는 포함하지 않는다.

## 6. VM mock canary

성공한 배포 뒤 `deploy/single-vm/canary-dry-run.sh`가 실행된다.

- 배포된 gateway container 안에서 `ccim.operations dry-run`
- 현재 PostgreSQL migration 검사
- network call과 external provider call이 모두 0인지 확인
- simulated UTC ledger와 budget boundary 포함
- deployment SHA와 image digest 연결
- artifact safety 검사

실제 GPT-5 mini 호출과 daily record 축적은 Roadmap 04 호환성 검증 뒤 Roadmap 05에서만 시작한다.

## 7. 암호화 backup과 restore

VM에서 age identity를 한 번 만든다. identity file은 VM과 별도의 안전한 위치에 보관하고 repository에 넣지 않는다.

```bash
sudo age-keygen -o /etc/ccim/backup.agekey
sudo chmod 0600 /etc/ccim/backup.agekey
sudo age-keygen -y /etc/ccim/backup.agekey
```

출력된 public recipient로 backup을 만든다.

```bash
export CCIM_BACKUP_AGE_RECIPIENT="age1..."
export CCIM_BACKUP_OUTPUT="/var/backups/ccim/ccim-$(date -u +%Y%m%dT%H%M%SZ).tar.gz.age"
bash deploy/single-vm/backup.sh
```

backup은 gateway write를 잠시 멈추고 PostgreSQL custom dump, Redis snapshot과 evidence volume을 묶은 뒤 `age`로 암호화한다. 생성된 encrypted file만 선택한 S3, GCS, OCI Object Storage 또는 별도 backup host로 복사한다.

restore는 기존 Compose volume을 제거하므로 maintenance window에서만 실행한다. 정확한 파일과 identity를 확인하고 명시적 확인 값을 설정해야 한다.

```bash
export CCIM_BACKUP_INPUT="/var/backups/ccim/<BACKUP>.tar.gz.age"
export CCIM_BACKUP_AGE_IDENTITY_FILE="/etc/ccim/backup.agekey"
export CCIM_RESTORE_CONFIRM="RESTORE_CCIM_VOLUMES"
bash deploy/single-vm/restore.sh
```

restore는 archive schema와 각 file checksum, image digest와 OCI revision을 확인하고 새 volume에 PostgreSQL·Redis·evidence를 복구한다. migration, `/live`, `/ready`가 성공해야 last-known-good state를 갱신한다.

최초 운영 전 rollback과 restore를 각각 한 번 훈련하고 다음을 기록한다.

- GitHub delivery run ID
- source SHA와 GHCR digest
- 배포 전후 readiness
- rollback 결과와 소요 시간
- encrypted backup digest와 외부 보관 확인
- restore 소요 시간과 migration/live/ready 결과
- provider disk snapshot ID와 월간 예상 비용

## 결정적 검증

```powershell
uv run python scripts/check_delivery_contract.py
docker compose config --quiet
.\scripts\verify.ps1
```

실제 VM setup 전에는 cloud API, 외부 LLM, production secret이 필요하지 않다.
