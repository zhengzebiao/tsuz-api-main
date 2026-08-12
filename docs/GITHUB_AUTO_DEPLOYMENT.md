# GitHub 自动部署方案

## 1. 总体流程

当前仓库已经具备 GitHub Actions、Docker Compose 和 GHCR 自动部署的基础框架，不需要从零搭建。推荐采用以下发布链路：

```text
开发分支
   ↓ Pull Request
main 分支
   ↓ CI：检查、测试、迁移验证、镜像构建
test-v1.0.0 标签
   ↓
构建 Docker 镜像并推送 GHCR
   ↓
SSH 登录测试服务器
   ↓
数据库迁移 → 更新容器 → 健康检查
   ↓ 测试环境验收
product-v1.0.0 标签
   ↓ GitHub Environment 人工审批
生产备份 → 数据库迁移 → 更新容器 → 健康检查
```

---

## 2. 首次部署前的一次性准备

### 2.1 准备服务器

服务器上需要完成：

- 安装 Docker Engine；
- 安装 Docker Compose Plugin；
- 创建专用部署用户，例如 `deploy`；
- 配置 GitHub Actions 使用的 SSH 公钥；
- 让部署用户可以直接执行 Docker 命令；
- 创建部署目录，例如 `/opt/tsuz-api`；
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

### 2.2 配置 GitHub Environments

在 GitHub 仓库中创建两个 Environment：

- `test`
- `product`

建议给 `product` 配置 **Required reviewers**，确保生产发布必须经过人工批准。

#### Environment Secrets

每个 Environment 至少配置：

| Secret | 用途 |
| --- | --- |
| `SSH_PRIVATE_KEY` | 登录服务器的部署用户私钥 |
| `SSH_KNOWN_HOSTS` | 服务器 SSH host key |
| `DOCKER_REGISTRY_TOKEN` | 登录 GHCR 的 Token |
| `DATABASE_URL` | PostgreSQL 连接地址 |
| `REDIS_URL` | Redis 连接地址 |
| `JWT_PRIVATE_KEY` | JWT RS256 私钥 |
| `JWT_PUBLIC_KEY` | JWT RS256 公钥 |

测试和生产环境必须使用：

- 不同数据库；
- 不同 Redis；
- 不同 JWT 密钥；
- 不同 Redis key prefix。

#### Environment Variables

至少配置：

| Variable | 用途 |
| --- | --- |
| `DEPLOY_HOST` | 目标服务器地址 |
| `DEPLOY_USER` | SSH 部署用户 |
| `DEPLOY_PORT` | SSH 端口 |
| `DEPLOY_PATH` | 服务器部署目录 |
| `DOCKER_IMAGE_NAME` | 镜像名称 |
| `DOCKER_NETWORK_NAME` | Docker 外部网络名称 |
| `APP_ENV` | 应用环境名称 |
| `JWT_ISSUER` | JWT issuer |
| `JWT_AUDIENCE` | JWT audience |
| `CORS_ALLOW_ORIGINS` | 允许的跨域来源 |
| `DEPLOY_PUBLIC_HEALTH_URL` | 公网健康检查地址 |

变量和 Secret 的现有读取逻辑可参考：

- `.github/workflows/deploy.yml`
- `.env.deploy.example`

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

1. Checkout 标签对应代码；
2. 登录 GitHub Container Registry；
3. 构建 Docker 镜像；
4. 推送镜像，例如 `ghcr.io/<owner>/<repo>:test-v1.0.0`；
5. SSH 连接测试服务器；
6. 上传 Compose、nginx 和环境配置；
7. 使用目标版本镜像执行 Alembic migration；
8. 执行 `docker compose pull`；
9. 执行 `docker compose up -d`；
10. 等待容器健康；
11. 执行 API smoke test；
12. 检查公网健康地址。

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

1. GitHub `product` Environment 人工审批；
2. 确认数据库备份完成；
3. 构建并推送生产镜像；
4. 使用目标镜像执行数据库迁移；
5. 更新生产 API/nginx 容器；
6. 执行内部和公网健康检查；
7. 检查日志和核心接口。

这样可确保生产版本与测试环境验证过的代码完全一致。

---

## 4. 数据库迁移流程

数据库迁移应放在启动新应用版本之前，推荐顺序：

```text
构建目标镜像
  ↓
上传目标版本配置
  ↓
使用目标镜像运行 alembic upgrade head
  ↓
启动新版本容器
  ↓
健康检查
```

仓库已经有独立迁移 Workflow：

- `.github/workflows/migrate.yml`

生产数据库迁移要求：

- 先备份；
- 经过人工审批；
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

1. 拉取上一个稳定镜像；
2. 更新应用容器；
3. 重新执行健康检查。

需要注意：

- 应用镜像可以回滚；
- 数据库不能默认自动回滚；
- 数据库应采用兼容迁移或 forward fix。

---

## 6. 当前上线前必须处理的问题

### 6.1 修正 CI 监听分支

当前 CI 监听的是 `master`，但仓库主分支是 `main`。需要先将 `.github/workflows/ci.yml` 的触发分支改为 `main`，否则 PR 到 `main` 不会正常触发 CI。

### 6.2 完整验证数据库迁移

现在 CI 执行的是 `alembic current`，还不足以证明空数据库能够成功升级。应增加：

```bash
pdm run alembic upgrade head
pdm run alembic current
```

### 6.3 衔接部署和迁移顺序

当前 Deploy workflow 负责更新容器，但不会自动执行 migration；独立的 Migrate workflow 又依赖服务器现有镜像配置。

首次部署时可能出现：

```text
API 已启动 → 数据库表还不存在 → smoke test 返回 500
```

因此需要调整成：

```text
目标镜像推送成功 → 目标镜像执行迁移 → 启动 API
```

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

1. 修复 CI 的 `master` → `main`；
2. 完善 CI 数据库迁移验证；
3. 调整 Deploy 与 Migration 顺序；
4. 修复 Dockerfile 锁文件安装；
5. 增加服务器 `.env` 文件权限控制；
6. 在 GitHub 创建 `test`、`product` Environments；
7. 配置 Variables 和 Secrets；
8. 初始化服务器 Docker、SSH、网络、PostgreSQL、Redis；
9. 配置域名、HTTPS、防火墙和备份；
10. 发布 `test-v1.0.0`；
11. 完成测试环境验收；
12. 使用同一 commit 发布 `product-v1.0.0`。

---

## 8. 当前完成度评估

当前项目自动部署骨架已经完成约 **70%～80%**。主要剩余工作为：

- 完善 CI 门禁；
- 调整数据库迁移顺序；
- 初始化服务器环境；
- 配置 GitHub Environments；
- 完善 HTTPS、备份和生产安全配置。
