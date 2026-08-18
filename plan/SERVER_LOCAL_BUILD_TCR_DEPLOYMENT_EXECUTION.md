# 服务器本地构建与腾讯云 TCR 部署实施记录

## 1. 目标与最终架构

本方案用于解决国内部署服务器从 GHCR/Docker Hub 跨境拉取 API 发布镜像速度慢的问题。构建机与部署服务器为同一台机器：

```text
GitHub 发布 test-vX.Y.Z / product-vX.Y.Z tag
    ↓
GitHub Actions 解析环境和 tag，并 SSH 登录部署服务器
    ↓
部署服务器使用仓库级只读 GitHub Deploy Key fetch 固定 tag
    ↓
部署服务器本地构建 linux/amd64 API 镜像
    ↓
推送同一镜像到腾讯云 TCR，保留本地镜像
    ↓
正常发布直接使用本地镜像 docker compose up --no-build
    ↓
TCR 保存历史 immutable tag，workflow_dispatch 回滚时 pull 历史镜像
```

正常发布不删除本地镜像，也不重新 pull；删除后重新 pull 仅作为首次接入 TCR 的恢复演练。首次发布前由独立 `init.yml` 手动初始化外部 Docker 网络、PostgreSQL 和 Redis；Init 与 Deploy 没有触发、状态文件或 marker 关联。

## 2. 已实现的代码调整

### `.github/workflows/deploy.yml`

- 去除 GitHub-hosted runner 上的 API `docker build`/`docker push`。
- `prepare-image` 改为 SSH 到部署服务器：
  - 校验 TCR Registry、完整镜像路径、镜像 tag、部署目录和构建平台；
  - 在 `DEPLOY_REPO_PATH` 初始化或复用 Git 工作区；
  - 使用 `git@github.com:${GITHUB_REPOSITORY}.git` 作为 remote；
  - fetch 指定 `test-vX.Y.Z` 或 `product-vX.Y.Z` tag；
  - 同时校验远程 tag、fetch 后 tag commit 与 `GITHUB_SHA` 一致；
  - detached checkout、清理工作树并确认最终 HEAD；
  - 在服务器本地构建并添加 OCI revision/version label；
  - 推送 TCR 后保留本地镜像，并输出本地镜像 ID/digest 信息。
- 正常发布路径先构建并推送，再上传运行配置；启动 API/nginx 前固定执行 `alembic current → upgrade head → current`、一次性 seed、permission sync dry-run/apply/check，全部成功后才执行 `docker compose up -d --no-build api nginx`。
- seed 管理员邮箱和密码来自对应 GitHub Environment 的 `SEED_ADMIN_EMAIL`、`SEED_ADMIN_PASSWORD` Secrets，只注入一次性容器，不写入常驻 `.env`；已有用户密码不会被重置。
- `workflow_dispatch` 回滚路径登录 TCR，执行 `docker compose pull api` 后启动选定历史 tag，明确不执行 Alembic、seed 或 permission synchronization。
- 移除不再需要的 `packages: write` 权限。
- 增加 workflow 并发锁，避免同一仓库的部署同时修改同一个服务器工作区。
- 保留原有环境/tag 交叉校验、运行时密钥注入、外部 Docker 网络、容器健康检查、smoke test 和公网健康检查。

### `.github/workflows/init.yml`

- 仅支持 `workflow_dispatch`，选择 `test`/`product` 并输入精确确认文本。
- 绑定对应 GitHub Environment，幂等创建/复用外部 Docker 网络。
- 上传 `docker-compose.infra.yml` 和权限 `0600` 的 `.env.infra`，仅启动 PostgreSQL/Redis。
- 使用 `pg_isready` 与 `redis-cli ping` 重试检查 readiness。
- 不触发 Deploy，不运行 API/nginx、Alembic、seed、permission synchronization，不删除 volume 或 prune 镜像。

### `.env.deploy.example`

新增非敏感配置示例：

```env
DOCKER_REGISTRY=ccr.ccs.tencentyun.com
DOCKER_IMAGE_NAME=ccr.ccs.tencentyun.com/tsuz/tsuz-api-main-test
DOCKER_REGISTRY_USERNAME=__TCR_ACCOUNT_ID__
DEPLOY_REPO_PATH=/opt/tsuz-api/source
DOCKER_BUILD_PLATFORM=linux/amd64
APP_VERSION=test-v1.0.0
```

原有的 `DEPLOY_PUBLIC_HEALTH_URL` 和用户已有环境配置修改均保留。

### 文档

- `docs/GITHUB_AUTO_DEPLOYMENT.md` 已补充服务器构建架构、GitHub Deploy Key、TCR、GitHub Environment Variables/Secrets、发布、回滚、版本保留和恢复演练说明。
- `README.md` 已同步新的 TCR/服务器本机构建发布路径。
- `docker-compose.deploy.yml` 无需改动，仍使用 `${DOCKER_IMAGE_NAME}:${APP_VERSION}` 选择 API 镜像。

## 3. GitHub 配置清单

为 `test` 和 `product` 各创建一个 GitHub Environment。本产品方案不要求 Required reviewers；Init 与 Deploy 彼此独立，不依赖 Environment 审批建立关联。

### Environment Variables

| Variable | test 示例 | product 示例 |
| --- | --- | --- |
| `DOCKER_REGISTRY` | `ccr.ccs.tencentyun.com` | `ccr.ccs.tencentyun.com` |
| `DOCKER_IMAGE_NAME` | `ccr.ccs.tencentyun.com/tsuz/tsuz-api-main-test` | `ccr.ccs.tencentyun.com/tsuz/tsuz-api-main-product` |
| `DOCKER_REGISTRY_USERNAME` | 腾讯云账号 ID | 腾讯云账号 ID |
| `DEPLOY_HOST` | 测试服务器地址 | 生产服务器地址 |
| `DEPLOY_PORT` | `22` 或自定义 SSH 端口 | `22` 或自定义 SSH 端口 |
| `DEPLOY_USER` | 专用部署用户 | 专用部署用户 |
| `DEPLOY_PATH` | `/opt/tsuz-api/runtime` | `/opt/tsuz-api/runtime` |
| `DEPLOY_REPO_PATH` | `/opt/tsuz-api/source` | `/opt/tsuz-api/source` |
| `DOCKER_BUILD_PLATFORM` | `linux/amd64` | `linux/amd64` |
| `DOCKER_NETWORK_NAME` | 测试环境外部网络 | 生产环境外部网络 |
| `APP_ENV` | `test` | `product` |
| `JWT_ISSUER` / `JWT_AUDIENCE` | 测试值 | 生产值 |
| `CORS_ALLOW_ORIGINS` | 测试来源 | 生产来源 |
| `DEPLOY_PUBLIC_HEALTH_URL` | 测试健康 URL | 生产健康 URL |
| `POSTGRES_CONTAINER_NAME` / `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PORT` | Init 的测试 PostgreSQL 配置 | Init 的生产 PostgreSQL 配置 |
| `REDIS_CONTAINER_NAME` / `REDIS_PORT` | Init 的测试 Redis 配置 | Init 的生产 Redis 配置 |
| `INIT_HEALTH_RETRIES` / `INIT_HEALTH_INTERVAL_SECONDS` | Init readiness 重试参数 | Init readiness 重试参数 |

同时配置原 workflow 所需的其他非敏感运行变量：容器名称/端口、邮件配置、腾讯云区域/SES endpoint、部署健康检查重试参数和 `TRUSTED_PROXY_IPS` 等。

### Environment Secrets

- `SSH_PRIVATE_KEY`：GitHub Actions 登录部署服务器的私钥。
- `SSH_KNOWN_HOSTS`：部署服务器 SSH host key，建议固定而不是依赖运行时扫描。
- `DOCKER_REGISTRY_TOKEN`：腾讯云 TCR 镜像访问密码。
- `DATABASE_URL`、`REDIS_URL`。
- `JWT_PRIVATE_KEY`、`JWT_PUBLIC_KEY`。
- `TENCENTCLOUD_SECRET_ID`、`TENCENTCLOUD_SECRET_KEY`。
- `POSTGRES_PASSWORD`：仅由 Init workflow 写入权限 `0600` 的 `.env.infra`。
- `SEED_ADMIN_EMAIL`、`SEED_ADMIN_PASSWORD`：仅注入正常发布的一次性 seed 容器，不写入常驻运行时 `.env`。

服务器向 GitHub 拉代码所需的 Deploy Key 私钥只保存在服务器，**不**放入上述 GitHub Secrets。

## 4. 服务器一次性配置

以部署用户执行：

```bash
sudo apt-get install -y git docker.io docker-compose-plugin
sudo usermod -aG docker deploy
sudo install -d -o deploy -g deploy -m 750 /opt/tsuz-api/runtime
sudo install -d -o deploy -g deploy -m 750 /opt/tsuz-api/source
```

根据服务器发行版调整安装命令。重新登录使 Docker 用户组生效。

生成该仓库专用、只读 GitHub Deploy Key：

```bash
install -d -m 700 ~/.ssh
ssh-keygen -t ed25519 \
  -C "tsuz-api-main deployment read-only" \
  -f ~/.ssh/tsuz_api_main_deploy \
  -N ""
chmod 600 ~/.ssh/tsuz_api_main_deploy
chmod 644 ~/.ssh/tsuz_api_main_deploy.pub
cat ~/.ssh/tsuz_api_main_deploy.pub
```

在 GitHub 仓库打开：

```text
Settings → Deploy keys → Add deploy key
```

添加公钥，**不要勾选 Allow write access**。Deploy Key 默认只读且只能绑定一个仓库。

人工核验 GitHub 官方 SSH host key 指纹后再固定：

```bash
ssh-keyscan -H github.com >> ~/.ssh/known_hosts
chmod 600 ~/.ssh/known_hosts
```

创建 `~/.ssh/config`：

```sshconfig
Host github.com
  HostName github.com
  User git
  IdentityFile ~/.ssh/tsuz_api_main_deploy
  IdentitiesOnly yes
```

验证：

```bash
chmod 600 ~/.ssh/config
ssh -T git@github.com || test "$?" -eq 1
git ls-remote git@github.com:zhengzebiao/tsuz-api-main.git 'refs/tags/*'
```

`ssh -T` 认证成功时可能因 GitHub 不提供 shell 返回退出码 1；`git ls-remote` 能读取 tag 才是有效验证。

## 5. TCR 配置

在腾讯云 TCR 创建 `tsuz` 命名空间及 test/product 仓库，建议保持私有并使用最小权限。个人版示例地址：

```text
ccr.ccs.tencentyun.com
```

登录测试：

```bash
docker login ccr.ccs.tencentyun.com --username=<腾讯云账号ID>
docker pull ccr.ccs.tencentyun.com/tsuz/tsuz-api-main-test:<历史tag>
```

服务器与 TCR 同地域时，按腾讯云控制台实际可用能力优先使用内网访问。标签使用 immutable `test-vX.Y.Z`/`product-vX.Y.Z`，不要覆盖已发布 tag；生命周期清理必须保留回滚窗口。

## 6. Init、发布与回滚

首次发布前手动运行 Actions → Init，选择 `test`/`product` 并输入 `INITIALIZE-test`/`INITIALIZE-product`。Init 只准备 network、PostgreSQL、Redis，完成后不会自动触发 Deploy。

发布前确保 tag 指向已经通过 CI 的 commit：

```bash
git tag test-v1.0.0
git push origin test-v1.0.0
```

workflow 会在服务器生成并推送：

```text
ccr.ccs.tencentyun.com/tsuz/tsuz-api-main-test:test-v1.0.0
```

正常发布不执行 `docker compose pull api`。服务器本地镜像可用时先固定执行 Alembic current/upgrade/current、seed、permission dry-run/apply/check，全部成功后才启动；构建、推送或数据库引导失败均不会切换当前运行容器。

回滚：GitHub Actions → Deploy → Run workflow：

```text
environment = test/product
image_tag = 对应环境的历史 immutable tag
```

回滚会从 TCR pull API 镜像，再执行 compose 更新。回滚不运行 Alembic、seed 或 permission synchronization，也不回滚数据库 schema、PostgreSQL 数据或 Redis 数据；schema 变更遵循兼容迁移/forward fix 策略。

## 7. 版本保留

每次发布记录：

```text
Git commit: <40位SHA>
Image tag: <test/product-vX.Y.Z>
Image digest: sha256:<digest>
GitHub Actions run: <run id/url>
```

TCR 作为完整历史版本库；服务器可根据磁盘容量保留最近 3～5 个本地版本。不要使用 `latest` 或未经审查的 `docker system prune -a`。

首次接入后的恢复演练（只在测试环境）：

```bash
IMAGE=ccr.ccs.tencentyun.com/tsuz/tsuz-api-main-test
TAG=test-v1.0.0
docker image rm "$IMAGE:$TAG"
docker pull "$IMAGE:$TAG"
docker image inspect "$IMAGE:$TAG" --format '{{join .RepoDigests "\n"}}'
```

## 8. 跨境下载边界

本方案消除了正常发布时部署服务器从 GHCR 拉取 API 发布镜像的步骤，但服务器本地构建在缓存缺失时仍可能访问：

- `Dockerfile` 的 `python:3.12-slim`（Docker Hub）；
- PDM/PyPI Python 依赖；
- `docker-compose.deploy.yml` 的 `nginx:1.27-alpine`（本地无该镜像时）。

如这些步骤仍慢，后续应把 Python/nginx 基础镜像同步到 TCR，配置可信的 Python 依赖镜像或缓存，并保留 Docker BuildKit cache。这不影响当前 API 最终镜像的 TCR 归档与回滚设计。

## 9. 验证结果与限制

已完成本地静态验证：

- 所有 workflow YAML 均可解析，所有 workflow Bash `run` block 均通过 `bash -n`；
- 使用临时 bare remote 分别对 lightweight tag 和 annotated tag 执行了 tag 解析、fetch、detached checkout、HEAD/clean 校验并通过；
- `git diff --check` 与 `git diff --cached --check` 通过；
- `docker compose config` 成功解析 API TCR 镜像和 nginx 镜像；
- 核心变更文件通过 Ruff，全部变更 Python 文件通过语法/导入检查和编译；四个既有大型 phase validator 仍各保留原有 `SIM115`/`BLE001` lint 项；
- focused pytest（seed、local init、workflow、permission command、permission concurrency）：`32 passed, 1 skipped`。

完整 `pdm run test` 当前存在与本次部署改动无关的既有环境问题：`tests/test_auth_service.py` 多项测试因本机 Redis 连接被关闭而失败；其余测试输出中包含既有集成测试 skip。未连接实际部署服务器、GitHub Deploy Key 或腾讯云 TCR，因此以下项目仍需人工完成：

- GitHub 仓库 Deploy Key 添加和 host key 指纹核验；
- 两个 GitHub Environment 的 Variables/Secrets（包括 Init 与 seed 所需配置；本方案不要求 Required reviewers）；
- 服务器 Git/Docker/Compose、目录权限与 Docker 权限；
- TCR 仓库、访问密码、权限、网络和 tag 保留策略；
- 首次真实 Init、正常 tag 的 Alembic/seed/permission 引导、TCR digest 比对、健康/smoke 验证和删除本地镜像后的恢复回滚演练；回滚需确认不执行数据库引导。

文档和示例中未写入真实 token、私钥、JWT、数据库连接串或其他生产密钥。
