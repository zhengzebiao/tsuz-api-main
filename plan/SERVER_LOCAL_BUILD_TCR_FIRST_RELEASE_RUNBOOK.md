# 服务器本地构建与腾讯云 TCR 首次发布操作手册

## 1. 目的与执行顺序

GitHub 和部署服务器完成基础配置后，按以下顺序完成上线：

```text
轮换已暴露凭据
  ↓
提交并合并部署改动
  ↓
检查测试服务器并配置 GitHub Environment
  ↓
手动运行独立 Init Workflow
  ↓
发布测试 tag
  ↓
观察 Deploy 在启动前固定执行 migration、seed、permission-sync
  ↓
验证服务器与 TCR
  ↓
执行测试环境恢复/回滚演练
  ↓
使用同一 commit 发布生产 tag
```

首次发布应从 `test` 环境开始。测试环境、TCR 恢复和回滚验证全部通过后，再发布 `product`。

---

## 2. 发布前安全处理

此前本地 `.env.test` 中存在真实的腾讯云凭据、TCR 密码、SSH 私钥、JWT 私钥和数据库密码。正式发布前应轮换这些凭据，并同步更新 GitHub `test`/`product` Environment Secrets。

需要区分两种密钥格式：

- `SSH_PRIVATE_KEY`：保留真实多行 OpenSSH 私钥格式；
- `JWT_PRIVATE_KEY`、`JWT_PUBLIC_KEY`：当前 Workflow 建议保存为单行、使用字面量 `\n`，例如：

```text
-----BEGIN PRIVATE KEY-----\nMIIE...\n-----END PRIVATE KEY-----
```

不要把 `.env.test` 提交到 Git。当前仓库的 `.gitignore` 已忽略 `.env.*`（示例文件除外），但仍需在提交前检查。

---

## 3. 提交并合并部署改动

服务器构建只会拉取 GitHub 上已经存在的固定 tag，因此必须先把当前部署改动提交、推送并合并到发布分支。

```bash
cd /Users/zhengzebiao/code/tsuz-api-main

git status
git diff --cached --check

pdm run lint
pdm run pytest tests/test_phase_4_deployment_checks.py -q

git commit -m "ci: build deployment images on server"
git push origin feature
```

然后在 GitHub 执行：

1. 创建 `feature → main` Pull Request；
2. 等待 CI 全部通过；
3. 合并到 `main`；
4. 确认 `main` 最新一次 CI 成功。

不要在尚未合并到 `main` 的 `feature` commit 上创建发布 tag。

---

## 4. 测试服务器首发检查

以部署用户登录测试服务器：

```bash
ssh zhengzebiao_test@1.14.132.121
```

### 4.1 验证服务器访问 GitHub

```bash
ssh -T git@github.com || test "$?" -eq 1

git ls-remote \
  git@github.com:zhengzebiao/tsuz-api-main.git \
  'refs/tags/*'
```

GitHub 不提供 shell，因此 `ssh -T` 在认证成功后仍可能返回状态码 `1`。本流程的最终判断标准是 `git ls-remote` 能成功访问仓库。

### 4.2 验证 Docker 与部署目录

```bash
docker info
docker compose version

test -d /opt/test/tsuz-api-main/runtime
test -d /opt/test/tsuz-api-main/source

test -w /opt/test/tsuz-api-main/runtime
test -w /opt/test/tsuz-api-main/source
```

源码目录可以暂时为空，Deploy Workflow 会在首次发布时初始化 Git 仓库。

### 4.3 通过独立 Init Workflow 初始化并验证基础设施

在 GitHub `test` Environment 配置 Init 所需 Variables 以及 `POSTGRES_PASSWORD` Secret，然后打开 Actions → Init，选择 `environment=test`，输入 `INITIALIZE-test`。Init 与 Deploy 完全独立，只创建/复用外部 Docker 网络并启动 PostgreSQL、Redis；它不启动 API/nginx，也不执行 Alembic、seed 或 permission synchronization。

Init 成功后，可在服务器复核：

```bash
docker network inspect tsuz-api-main-test

docker ps --filter name=main-postgres-test
docker ps --filter name=main-redis-test

docker exec main-postgres-test \
  pg_isready -U zhengzebiao_test -d tsuz

docker exec main-redis-test redis-cli ping
```

Redis 检查应返回：

```text
PONG
```

如果上述检查失败，修正 GitHub Environment 的基础设施 Variables/Secret 后重跑 Init。不要使用 `down -v`、删除 volume 或 `docker system prune`。Init 在部署目录维护权限为 `0600` 的 `.env.infra`，不会覆盖 Deploy 使用的运行时 `.env`。

---

## 5. 创建首个测试发布标签

在本地切换到已经合并并通过 CI 的 `main`：

```bash
cd /Users/zhengzebiao/code/tsuz-api-main

git fetch origin --tags
git switch main
git pull --ff-only origin main
```

记录本次发布 commit：

```bash
RELEASE_SHA="$(git rev-parse HEAD)"
echo "$RELEASE_SHA"
```

确认目标 tag 尚不存在：

```bash
git ls-remote --tags origin refs/tags/test-v1.0.0
```

没有输出表示该 tag 尚未使用。创建并推送 annotated tag：

```bash
git tag -a test-v1.0.0 "$RELEASE_SHA" \
  -m "Release test-v1.0.0"

git push origin test-v1.0.0
```

不要覆盖或强制更新已经发布的 tag。如果 `test-v1.0.0` 已存在，改用下一个未使用版本，例如 `test-v1.0.1`。

---

## 6. 查看 GitHub Actions 部署过程

打开：

```text
GitHub 仓库
→ Actions
→ Deploy
→ test-v1.0.0 对应的运行记录
```

预期按以下顺序执行。

### 6.1 `resolve-deploy`

- 识别部署环境为 `test`；
- 校验 tag 格式；
- 输出 immutable image tag `test-v1.0.0`。

### 6.2 `prepare-image`

- GitHub Actions SSH 登录测试服务器；
- 服务器在 `DEPLOY_REPO_PATH` 拉取固定 Git tag；
- 校验 tag commit 与 `GITHUB_SHA` 一致；
- 在服务器本地构建 `linux/amd64` 镜像；
- 推送镜像到腾讯云 TCR；
- 保留服务器本地镜像，不重新 pull。

测试镜像应为：

```text
ccr.ccs.tencentyun.com/tsuz/tsuz-api-main-test:test-v1.0.0
```

### 6.3 `deploy`

- 上传 `docker-compose.deploy.yml`、nginx 配置和运行时 `.env`（不包含 seed 凭据）；
- 创建或复用外部 Docker 网络；
- 正常发布固定执行 `alembic current`、`alembic upgrade head`、`alembic current`；
- 使用 `SEED_ADMIN_EMAIL`/`SEED_ADMIN_PASSWORD` Environment Secrets 执行一次性管理员 seed；
- 固定执行 permission-sync dry-run、apply、check；
- 上述引导成功后，使用服务器上的本地目标镜像启动 API 和 nginx；
- 等待 API 容器健康；
- 执行部署 smoke test；
- 检查公网健康地址。

引导失败时 Deploy 在 `docker compose up` 前停止，不切换当前 API/nginx。历史镜像回滚只 pull 并更新 API/nginx，不执行 migration、seed 或 permission synchronization。

构建或推送镜像失败时，Workflow 不应切换当前正在运行的容器。

---

## 7. 正常发布数据库引导

当前 Deploy Workflow 已把 Alembic、seed 和 permission synchronization 固定纳入正常不可变 tag 发布。首次数据库或后续待执行 migration 都由目标镜像在 API/nginx 启动前处理，不需要先让 API 启动后再单独运行 Migrate。

固定顺序为：

```text
目标镜像构建并推送 TCR
  ↓
alembic current
  ↓
alembic upgrade head
  ↓
alembic current
  ↓
seed（Environment Secrets）
  ↓
permission-sync --dry-run
  ↓
permission-sync apply
  ↓
permission-sync --check
  ↓
启动 API/nginx，再执行 health/smoke/public checks
```

`SEED_ADMIN_EMAIL` 和 `SEED_ADMIN_PASSWORD` 必须配置在对应 GitHub Environment Secrets 中。它们只传给一次性 seed 命令，不写入常驻运行时 `.env`；seed 对已有管理员保持幂等且不会重置密码。任一步失败都会阻止容器切换。

`.github/workflows/migrate.yml` 保持独立的人工维护 workflow，Init 和 Deploy 不会调用它；历史镜像回滚同样不运行 Alembic、seed 或 permission synchronization。

---

## 8. 服务器部署验证

GitHub Actions 成功后登录服务器：

```bash
cd /opt/test/tsuz-api-main/runtime
```

### 8.1 查看容器状态和日志

```bash
docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  ps
```

API 和 nginx 应为 `Up`，API 最终应为 `healthy`。

```bash
docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  logs --tail=100 api nginx
```

### 8.2 确认部署版本

```bash
grep '^APP_VERSION=' .env
grep '^DOCKER_IMAGE_NAME=' .env
```

预期包含：

```text
APP_VERSION=test-v1.0.0
```

确认本地镜像存在并查看 digest：

```bash
docker image inspect \
  ccr.ccs.tencentyun.com/tsuz/tsuz-api-main-test:test-v1.0.0 \
  --format 'ID={{.Id}} Digests={{json .RepoDigests}}'
```

确认镜像的 Git revision label：

```bash
docker image inspect \
  ccr.ccs.tencentyun.com/tsuz/tsuz-api-main-test:test-v1.0.0 \
  --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'
```

输出应与发布前记录的 `RELEASE_SHA` 相同。

验证服务器源码：

```bash
git -C /opt/test/tsuz-api-main/source rev-parse HEAD
git -C /opt/test/tsuz-api-main/source status --short
```

- `HEAD` 应等于 `RELEASE_SHA`；
- `status --short` 应无输出。

### 8.3 健康检查

服务器内部：

```bash
curl --fail http://127.0.0.1:18000/health
curl --fail http://127.0.0.1:18080/health
```

公网：

```bash
curl --fail https://test-api.tusz.online/health
```

随后人工验证：

- 邮箱验证码发送；
- 注册和登录；
- access token 签发；
- refresh token；
- 退出登录；
- PostgreSQL 数据；
- Redis session/token 状态；
- nginx 和 HTTPS；
- 容器重启后的数据持久性。

---

## 9. 验证 TCR 镜像归档

在腾讯云 TCR 控制台确认存在：

```text
tsuz/tsuz-api-main-test
└── test-v1.0.0
```

记录本次发布信息：

```text
Git commit: <RELEASE_SHA>
Image tag: test-v1.0.0
Image digest: sha256:...
GitHub Actions run: <运行链接>
```

服务器本地镜像的 RepoDigest 应与 TCR 控制台显示的 digest 一致。

---

## 10. 测试环境恢复与回滚演练

首次先验证删除本地镜像 tag 后可以从 TCR 恢复：

```bash
IMAGE=ccr.ccs.tencentyun.com/tsuz/tsuz-api-main-test
TAG=test-v1.0.0

docker image inspect "$IMAGE:$TAG"
docker image rm "$IMAGE:$TAG"
docker pull "$IMAGE:$TAG"
docker image inspect "$IMAGE:$TAG" \
  --format '{{join .RepoDigests "\n"}}'
```

然后打开：

```text
GitHub
→ Actions
→ Deploy
→ Run workflow
```

输入：

```text
environment: test
image_tag: test-v1.0.0
```

手动部署路径会从 TCR pull 目标镜像，并重新执行容器健康检查、smoke test 和公网健康检查。

如需验证真正的版本回滚，应先发布 `test-v1.0.1`，再通过 `workflow_dispatch` 回滚到 `test-v1.0.0`。

应用镜像回滚不会回滚数据库 schema、PostgreSQL 数据或 Redis 数据；数据库变更应使用兼容迁移或 forward fix。

---

## 11. 发布生产环境

生产环境必须使用测试环境已经验证过的同一个 commit：

```bash
cd /Users/zhengzebiao/code/tsuz-api-main

git fetch origin --tags

git tag -a product-v1.0.0 "$RELEASE_SHA" \
  -m "Release product-v1.0.0"

git push origin product-v1.0.0
```

然后：

1. 按组织发布流程确认生产数据库备份完成（本方案不要求 GitHub Required reviewers）；
2. 打开 GitHub Actions，观察目标镜像构建与推送；
3. 确认 Deploy 在启动前完成 Alembic current/upgrade/current、seed、permission dry-run/apply/check；
4. 检查生产 TCR 镜像、容器、日志、内部和公网健康；
5. 记录生产 image digest 和 GitHub Actions run。

如果任一数据库引导步骤失败，不要绕过或手工启动新容器；修复配置/代码后重跑发布。历史镜像回滚不会撤销 schema，也不会重复 seed 或权限同步。

---

## 12. 首发完成标准

满足以下条件后，测试环境首次发布才算完成：

- `main` 上发布 commit 的 CI 通过；
- 独立 Init Workflow 成功，network/PostgreSQL/Redis readiness 检查通过；
- `test-vX.Y.Z` tag 与预期 commit 一致；
- 服务器源码目录的 `HEAD` 与发布 commit 一致且工作树干净；
- 服务器本地构建成功；
- 同一镜像成功推送到 TCR；
- API 和 nginx 容器正常运行；
- API 容器健康检查通过；
- 部署 smoke test 与公网健康检查通过；
- 正常 Deploy 的 Alembic current/upgrade/current、seed、permission dry-run/apply/check 全部成功；
- 服务器常驻 `.env` 不包含 `SEED_ADMIN_EMAIL` 或 `SEED_ADMIN_PASSWORD`；
- 核心认证和邮件流程人工验收通过；
- TCR digest 已记录并与服务器镜像一致；
- 删除本地镜像后的 TCR 恢复演练通过；
- `workflow_dispatch` 历史 tag 部署路径通过，且日志确认未执行 Alembic、seed、permission synchronization。
