# GitHub 自动部署方案

## 1. 总体流程

当前仓库使用 GitHub Actions 编排发布，由国内部署服务器拉取固定 Git tag、在本机完成 Docker 构建，并把不可变镜像推送到腾讯云 TCR。构建机与部署机是同一台服务器，因此正常发布直接使用本地构建镜像；TCR 保存历史版本，并在回滚或服务器恢复时提供镜像拉取。

```text
开发分支
   ↓ Pull Request
main 分支
   ↓ CI：检查、测试、迁移验证
test-v1.0.0 标签
   ↓
GitHub Actions SSH 登录测试服务器
   ↓
服务器用只读 GitHub Deploy Key 拉取固定 tag
   ↓
服务器本地构建 → 推送腾讯云 TCR → 直接部署本地镜像
   ↓
健康检查与测试环境验收
product-v1.0.0 标签
   ↓
生产服务器拉取固定 tag → 本地构建 → 推送 TCR → 部署与健康检查
```

正常发布不会删除并重新拉取本地镜像。只有 `workflow_dispatch` 回滚历史版本时，部署服务器才从 TCR 执行 `docker compose pull api`。

---

## 2. 首次部署前的一次性准备

### 2.1 准备服务器

服务器上需要完成：

- 安装 Git、Docker Engine 和 Docker Compose Plugin；
- 创建专用部署用户，例如 `deploy`；
- 配置 GitHub Actions 使用的 SSH 公钥；
- 让部署用户可以直接执行 Docker 命令；
- 创建运行目录和独立源码目录，例如 `/opt/tsuz-api/runtime` 与 `/opt/tsuz-api/source`；
- 给源码拉取配置仓库级、只读的 GitHub Deploy Key；
- 配置防火墙：
  - 开放 SSH；
  - 开放 HTTP/HTTPS；
  - 不向公网开放 PostgreSQL、Redis 和 API 容器端口；
- 配置域名和 HTTPS；
- 准备数据库备份与恢复方案。

当前仓库中的部署相关文件：

- `docker-compose.deploy.yml`
- `docker-compose.infra.yml`
- `nginx/default.conf`

PostgreSQL 和 Redis 可以选择：

1. 使用当前仓库的 `docker-compose.infra.yml` 部署在同一台服务器；
2. 使用云 PostgreSQL 和云 Redis。

生产环境更推荐托管数据库，至少数据库备份不应只保存在应用服务器上。

### 2.2 配置服务器访问 GitHub

这里有两条方向不同的 SSH 链路，不要混用密钥：

1. GitHub Actions → 部署服务器：使用 Environment Secret `SSH_PRIVATE_KEY`；
2. 部署服务器 → GitHub 仓库：使用只保存在服务器上的只读 Deploy Key。

以部署用户登录服务器，生成该仓库专用密钥：

```bash
install -d -m 700 ~/.ssh
ssh-keygen -t ed25519 \
  -C "tsuz-api-main deployment read-only" \
  -f ~/.ssh/tsuz_api_main_deploy \
  -N ""
chmod 600 ~/.ssh/tsuz_api_main_deploy
chmod 644 ~/.ssh/tsuz_api_main_deploy.pub
```

复制公钥内容：

```bash
cat ~/.ssh/tsuz_api_main_deploy.pub
```

进入 GitHub 仓库：

```text
Settings → Deploy keys → Add deploy key
```

填写一个可识别的标题并粘贴公钥，**不要勾选 `Allow write access`**。Deploy Key 默认只读，而且一个 key 只能绑定一个仓库；其他仓库应生成独立密钥。

把 GitHub 官方公布的 SSH host key 指纹与当前连接结果人工核对后，将 host key 固定到服务器：

```bash
ssh-keyscan -H github.com >> ~/.ssh/known_hosts
chmod 600 ~/.ssh/known_hosts
ssh-keygen -F github.com -f ~/.ssh/known_hosts
```

不要只依赖未核验的 `ssh-keyscan` 输出。核验后配置 `~/.ssh/config`：

```sshconfig
Host github.com
  HostName github.com
  User git
  IdentityFile ~/.ssh/tsuz_api_main_deploy
  IdentitiesOnly yes
```

```bash
chmod 600 ~/.ssh/config
ssh -T git@github.com || test "$?" -eq 1
git ls-remote git@github.com:zhengzebiao/tsuz-api-main.git 'refs/tags/*'
```

GitHub 的 `ssh -T` 成功认证后仍可能返回退出码 `1`，因为 GitHub 不提供 shell；`git ls-remote` 能列出 tags 才是本工作流需要的最终验证。服务器上的 Deploy Key 私钥不得复制到 GitHub Actions Secrets。

### 2.3 配置腾讯云 TCR

当前示例使用腾讯云容器镜像服务个人版域名：

```text
ccr.ccs.tencentyun.com
```

在 TCR 控制台初始化或重置镜像仓库访问密码，并创建 `tsuz` 命名空间及 test/product 仓库。个人版通常用腾讯云账号 ID 作为用户名：

```bash
docker login ccr.ccs.tencentyun.com --username=<腾讯云账号ID>
docker pull ccr.ccs.tencentyun.com/tsuz/tsuz-api-main-test:<已存在tag>
```

测试与生产建议分别使用：

```text
ccr.ccs.tencentyun.com/tsuz/tsuz-api-main-test
ccr.ccs.tencentyun.com/tsuz/tsuz-api-main-product
```

仓库保持私有并使用最小 push/pull 权限；如启用标签不可变和生命周期策略，应确保所有回滚窗口内的 tag 不会被覆盖或清理。服务器与 TCR 同地域时优先按控制台提供的内网访问能力配置；个人版可用区域与跨地域内网能力以控制台实际显示为准。

### 2.4 配置 GitHub Environments

在 GitHub 仓库中创建两个 Environment：

- `test`
- `product`

本方案不要求为 `product` 配置 **Required reviewers**。如组织另有生产审批要求，可独立配置，但 Init 与 Deploy 不依赖该机制，也不会互相触发。

#### Environment Secrets

每个 Environment 至少配置：

| Secret | 用途 |
| --- | --- |
| `SSH_PRIVATE_KEY` | 登录服务器的部署用户私钥 |
| `SSH_KNOWN_HOSTS` | 服务器 SSH host key |
| `DOCKER_REGISTRY_TOKEN` | 腾讯云 TCR 镜像仓库访问密码 |
| `DATABASE_URL` | PostgreSQL 连接地址 |
| `REDIS_URL` | Redis 连接地址 |
| `JWT_PRIVATE_KEY` | JWT RS256 私钥 |
| `JWT_PUBLIC_KEY` | JWT RS256 公钥 |
| `POSTGRES_PASSWORD` | 独立 Init workflow 创建/复用 PostgreSQL 时使用的密码 |
| `SEED_ADMIN_EMAIL` | 正常发布的一次性 seed 管理员邮箱 |
| `SEED_ADMIN_PASSWORD` | 正常发布的一次性 seed 管理员密码，不写入常驻 `.env` |

测试和生产环境必须使用：

- 不同数据库；
- 不同 Redis；
- 不同 JWT 密钥；
- 不同 Redis key prefix。

#### Environment Variables

至少配置：

测试环境建议 `DOCKER_IMAGE_NAME=ccr.ccs.tencentyun.com/tsuz/tsuz-api-main-test`，生产环境建议使用独立的 `.../tsuz-api-main-product` 仓库；不要把两个环境写入同一个可覆盖 tag。

| Variable | 用途 |
| --- | --- |
| `DEPLOY_HOST` | 目标服务器地址 |
| `DEPLOY_USER` | SSH 部署用户 |
| `DEPLOY_PORT` | SSH 端口 |
| `DEPLOY_PATH` | 服务器运行目录，例如 `/opt/tsuz-api/runtime` |
| `DEPLOY_REPO_PATH` | 独立 Git 源码目录，例如 `/opt/tsuz-api/source` |
| `DOCKER_REGISTRY` | TCR host，例如 `ccr.ccs.tencentyun.com`，不带协议和路径 |
| `DOCKER_IMAGE_NAME` | 完整 TCR 镜像路径，例如 `ccr.ccs.tencentyun.com/tsuz/tsuz-api-main-test` |
| `DOCKER_REGISTRY_USERNAME` | TCR 用户名；个人版通常为腾讯云账号 ID |
| `DOCKER_BUILD_PLATFORM` | 构建平台，当前服务器推荐 `linux/amd64` |
| `COMPOSE_PROJECT_NAME` | Init 和 Deploy 共用的显式 Docker Compose 项目名称 |
| `DOCKER_NETWORK_NAME` | Docker 外部网络名称 |
| `APP_ENV` | 应用环境名称 |
| `JWT_ISSUER` | JWT issuer |
| `JWT_AUDIENCE` | JWT audience |
| `CORS_ALLOW_ORIGINS` | 允许的跨域来源 |
| `DEPLOY_PUBLIC_HEALTH_URL` | 公网健康检查地址 |

Init 还使用 `POSTGRES_CONTAINER_NAME`、`POSTGRES_DB`、`POSTGRES_USER`、`POSTGRES_PORT`、`REDIS_CONTAINER_NAME`、`REDIS_PORT`、`INIT_HEALTH_RETRIES` 和 `INIT_HEALTH_INTERVAL_SECONDS` Environment Variables。

变量和 Secret 的现有读取逻辑可参考：

- `.github/workflows/init.yml`
- `.github/workflows/deploy.yml`
- `.env.deploy.example`

### 2.5 独立初始化基础设施

首次发布前，在 Actions -> Init 手动选择 `test` 或 `product`，并输入精确确认文本 `INITIALIZE-test` 或 `INITIALIZE-product`。Init 幂等创建/复用外部 Docker 网络，只启动 PostgreSQL 和 Redis，并通过 `pg_isready` 与 `redis-cli ping` 等待就绪。

Init 不调用或触发 Deploy，不写状态标记，不启动 API/nginx，不运行 Alembic、seed 或 permission synchronization，也不执行 `down -v`、volume 删除或 Docker prune。Init 完成后，再单独推送不可变 tag 触发 Deploy。

---

## 3. 日常自动部署流程

### 3.1 提交 Pull Request

开发完成后执行以下流转：

```text
feature branch → Pull Request → main
```

CI 应执行：

1. Ruff 代码检查；
2. pytest 测试；
3. 启动临时 PostgreSQL、Redis；
4. 从空数据库执行 `alembic upgrade head`；
5. 构建 Docker 镜像；
6. 所有检查通过后才允许合并。

当前 CI 文件：

- `.github/workflows/ci.yml`

建议同时为 `main` 配置分支保护，禁止 CI 未通过时合并。

### 3.2 发布到测试环境

从已经合并并通过 CI 的 `main` commit 创建标签：

```bash
git tag test-v1.0.0
git push origin test-v1.0.0
```

现有 Deploy workflow 会根据标签识别 `test` 环境，随后自动执行：

1. 解析不可变发布 tag 和环境；
2. SSH 连接目标服务器；
3. 服务器用仓库只读 Deploy Key fetch 并校验该 tag 对应的 commit；
4. 服务器本地构建 `linux/amd64` API 镜像；
5. 登录并推送到 TCR，例如 `ccr.ccs.tencentyun.com/tsuz/tsuz-api-main-test:test-v1.0.0`；
6. 保留本地镜像，不执行删除和重新 pull；
7. 上传 Compose、nginx 和运行时 `.env`（不包含 seed 凭据）；
8. 正常发布固定使用目标镜像执行 `alembic current → alembic upgrade head → alembic current`；
9. 使用 `SEED_ADMIN_EMAIL`/`SEED_ADMIN_PASSWORD` Environment Secrets 执行一次性 seed；
10. 依次执行 permission synchronization dry-run、apply 和 check；
11. 只有上述步骤成功后，执行 `docker compose up -d --no-build api nginx`；
12. 等待容器健康，执行 API smoke test，并检查公网健康地址。

上述三类数据库引导只属于正常不可变 tag 发布，且每次正常发布都会执行。历史镜像回滚只 pull 并更新 API/nginx，不执行 Alembic、seed 或 permission synchronization。

### 3.3 测试环境验收

测试环境至少验证：

- `/health` 正常；
- 用户登录正常；
- access token 签发正常；
- refresh token 正常；
- 退出登录正常；
- PostgreSQL 数据正常；
- Redis session/token 状态正常；
- nginx 和 HTTPS 正常；
- 容器重启后数据不丢失。

### 3.4 发布生产环境

测试通过后，对**同一个 commit**创建生产标签：

```bash
git tag product-v1.0.0
git push origin product-v1.0.0
```

生产部署流程：

1. 按组织流程确认数据库备份和发布条件（本方案不依赖 Required reviewers）；
2. 构建并推送生产镜像；
3. 使用目标镜像固定执行 Alembic current/upgrade/current、seed、permission dry-run/apply/check；
4. 更新生产 API/nginx 容器；
5. 执行内部和公网健康检查；
6. 检查日志和核心接口。

这样可确保生产版本与测试环境验证过的代码完全一致。

---

## 4. 数据库迁移流程

每次正常不可变 tag 发布固定在启动新应用版本之前执行：

```text
构建并归档目标镜像
  ↓
alembic current → upgrade head → current
  ↓
seed
  ↓
permission-sync --dry-run → apply → --check
  ↓
启动新版本容器
  ↓
健康、smoke 和公网检查
```

任一步失败都会在 `docker compose up` 前停止，当前 API/nginx 不会被切换。仓库已有的 `.github/workflows/migrate.yml` 继续作为独立人工维护工具，不被 Init 或 Deploy 调用。历史镜像回滚不运行任何 Alembic、seed 或权限同步命令。

生产数据库迁移要求：

- 先备份；
- 按组织发布流程审查变更；
- 尽可能使用向后兼容迁移；
- 不依赖自动数据库 downgrade 回滚。

---

## 5. 回滚流程

当前部署 Workflow 已支持选择历史镜像标签进行手动回滚：

```text
GitHub Actions
  → Deploy
  → Run workflow
  → environment=product
  → image_tag=product-v0.9.0
```

回滚会执行：

1. 通过 `workflow_dispatch` 选择环境和历史 immutable tag；
2. 服务器登录 TCR 并拉取该历史 API 镜像（本地已有时 Docker 会复用层）；
3. 更新 API 和 nginx 容器；
4. 重新执行 health、smoke test 和公网健康检查。

需要注意：

- 应用镜像可以回滚；
- 回滚明确跳过 Alembic、seed 和 permission synchronization；
- 数据库 schema 与数据不会自动回滚；
- 数据库应采用兼容迁移或 forward fix。

### 5.1 版本保留与恢复演练

每次发布使用新的 `test-vX.Y.Z` 或 `product-vX.Y.Z`，不要使用 `latest`，也不要覆盖已经发布的 tag。建议记录：

```text
Git commit: <40位SHA>
Image tag: test-v1.0.0
Image digest: sha256:<digest>
GitHub Actions run: <run id/url>
```

正常发布后 TCR 保存历史镜像；部署服务器可按磁盘容量保留最近 3～5 个本地版本。清理时不要对生产服务器直接执行未经审查的 `docker system prune -a`。

首次接入 TCR 后，在测试环境完成一次恢复演练：

```bash
IMAGE=ccr.ccs.tencentyun.com/tsuz/tsuz-api-main-test
TAG=test-v1.0.0

docker image inspect "$IMAGE:$TAG"
docker image rm "$IMAGE:$TAG"
docker pull "$IMAGE:$TAG"
docker image inspect "$IMAGE:$TAG" --format '{{join .RepoDigests "\n"}}'
```

删除本地镜像只用于恢复演练，不属于日常发布流程。随后用 `workflow_dispatch` 回滚到该 tag，确认 health、smoke test 和公网健康检查通过。

需要注意，本方案只消除正常发布时从 GHCR 拉取 API 最终镜像的步骤。服务器本地构建在缓存失效时仍可能访问 Docker Hub 的 `python:3.12-slim`、PyPI/PDM 依赖；服务器本地没有 `nginx:1.27-alpine` 时，Compose 也可能从 Docker Hub 拉取。若这些链路仍慢，应继续把基础镜像同步到 TCR，并配置可信的依赖镜像或构建缓存。

---

## 6. 当前上线前必须处理的问题

### 6.1 确认 CI 与实际发布分支一致

当前 `.github/workflows/ci.yml` 已监听 `main`。若仓库默认发布分支发生变化，需要同步修改 CI 触发分支和分支保护规则。

### 6.2 完整验证数据库迁移

现在 CI 执行的是 `alembic current`，还不足以证明空数据库能够成功升级。应增加：

```bash
pdm run alembic upgrade head
pdm run alembic current
```

### 6.3 验证正常发布数据库引导

Deploy workflow 已在容器切换前固定执行 migration、seed 和 permission synchronization。首次以及后续正常发布都应确认 Actions 日志顺序为：

```text
目标镜像推送成功
  → alembic current/upgrade/current
  → seed
  → permission dry-run/apply/check
  → 启动 API/nginx
```

还应执行一次历史镜像回滚验收，确认回滚日志不包含上述三类数据库引导。独立 Migrate workflow 仅作为维护工具保留。

### 6.4 收紧服务器 `.env` 权限

部署时会把数据库密码和 JWT 私钥写入服务器 `.env`，上传后需要执行：

```bash
chmod 600 .env
```

部署目录也应只允许部署用户访问。

### 6.5 Docker 构建使用锁文件

当前 `Dockerfile` 没有在安装依赖前复制 `pdm.lock`，生产镜像构建的依赖可能不完全可重复。应复制锁文件并使用 frozen install。

### 6.6 完善健康检查

当前 `/health` 只返回固定成功结果，没有检查 PostgreSQL 和 Redis。建议区分：

- `liveness`：API 进程是否存活；
- `readiness`：数据库、Redis 和迁移状态是否正常。

### 6.7 配置生产 HTTPS

现有 nginx 只监听 HTTP。生产环境需要选择一种 HTTPS 方案：

- 云负载均衡/CDN 终止 TLS；
- 服务器前置 Caddy/Traefik；
- 扩展现有 nginx，监听 443 并配置证书。

### 6.8 限制数据库和 Redis 暴露

如果使用仓库自带的 `docker-compose.infra.yml`，PostgreSQL 和 Redis 当前映射了宿主机端口。生产环境应取消不必要的端口映射、只绑定本机，或至少通过防火墙禁止公网访问。

### 6.9 建立生产发布门禁

Deploy workflow 由标签直接触发，目前不会自动验证：

- 标签对应 commit 是否来自 `main`；
- 对应 commit 的 CI 是否成功；
- 生产标签是否对应已在测试环境验证过的 commit。

建议通过分支保护、Release workflow 或 Deploy workflow 内的检查强制建立发布门禁。

---

## 7. 建议实施顺序

按优先级推进：

1. 完善 CI 数据库迁移验证；
2. 验证 Deploy 的 migration/seed/permission 固定顺序；
3. 修复 Dockerfile 锁文件安装；
4. 在 GitHub 创建 `test`、`product` Environments；
5. 配置 Variables、Secrets（包括 `POSTGRES_PASSWORD`、`SEED_ADMIN_EMAIL`、`SEED_ADMIN_PASSWORD`），不把 seed 密码写入常驻 `.env`；
6. 初始化服务器 Git、Docker、Compose、Deploy Key、目录和网络；
7. 创建 TCR 仓库并验证 push/pull；
8. 配置域名、HTTPS、防火墙和备份；
9. 手动运行独立 Init workflow 并验证网络、PostgreSQL、Redis；
10. 发布 `test-v1.0.0`，验证 Deploy 的数据库引导；
11. 完成测试环境和 TCR 恢复演练；
12. 使用同一 commit 发布 `product-v1.0.0`。

---

## 8. 当前完成度评估

当前仓库的服务器本地构建/TCR 自动部署代码与文档已经完成；正式上线前仍需完成以下外部配置和发布门禁：

- 配置 GitHub `test`/`product` Environments、Secrets 和 Variables；本方案不要求 Required reviewers；
- 在服务器配置只读 GitHub Deploy Key、Docker、Compose、目录权限和 TCR 登录；
- 创建并验证 TCR test/product 仓库及版本保留策略；
- 手动运行 Init workflow 并验证网络、PostgreSQL、Redis；
- 完成至少一次真实测试 tag 发布，确认 migration/seed/permission bootstrap 顺序、digest 校验和 TCR 恢复回滚；
- 完善 HTTPS、备份和生产安全配置。

发布验收应同时确认：正常 Deploy 包含数据库引导步骤；历史镜像回滚不重复执行数据库引导；seed 凭据只来自 GitHub Environment Secrets，并且不落盘到常驻服务器环境文件。
