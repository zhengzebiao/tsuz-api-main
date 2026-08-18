# 已开发模块的 Docker 与本机 API 部署方案

## 1. 文档目标

本文说明如何把当前仓库中的已开发模块部署到本地或测试环境。当前模块至少包括：

- 认证、Refresh Token、退出登录和 Session 管理；
- 用户管理、细粒度 RBAC 权限和管理审计；
- 子应用管理、App Secret 创建与重新生成；
- 当前仓库中的全部 Alembic 迁移和幂等 Seed 数据。

支持两种运行模式：

| 模式 | PostgreSQL / Redis | API | nginx | 主要用途 |
| --- | --- | --- | --- | --- |
| A：Docker 基础设施 + 本机 API | Docker | 本机 PDM/Uvicorn | 默认不启动 | 日常开发和调试，推荐 |
| B：完整 Docker API 部署 | Docker | Docker 镜像/Gunicorn | Docker | 测试部署、发布演练和类生产运行 |

部署当前源码时，数据库必须升级到**当前 Alembic head**。不要把部署命令固定为某个历史 revision；始终使用 `alembic upgrade head`，再用 `alembic current` 确认结果。当前仓库包含 `0003_app_management`，将来新增迁移后 `head` 还会继续变化。

---

## 2. 文件和命令对应关系

| 文件或命令 | 用途 |
| --- | --- |
| `docker-compose.infra.yml` | 长期运行 PostgreSQL 16 和 Redis 7，保留数据卷 |
| `docker-compose.deploy.yml` | 使用已有镜像运行 API 和 nginx |
| `docker-compose.yml` | 一体化本地 Docker 开发栈，会从当前源码构建 API |
| `.env.infra` | PostgreSQL、Redis、端口和共享 Docker 网络配置，不提交 Git |
| `.env` | API 运行配置及 JWT 密钥，不提交 Git，建议权限为 `0600` |
| `pdm run migrate` | 本机执行 `alembic upgrade head` |
| `pdm run alembic-current` | 本机检查当前 Alembic revision |
| `pdm run seed` | 本机执行幂等 Seed |
| `pdm run dev` | 本机以 Uvicorn reload 模式启动 API |

开始前安装依赖：

```bash
pdm install
```

确认基础工具可用：

```bash
docker compose version
pdm --version
openssl version
```

> `.env`、`.env.infra`、数据库口令、Redis 口令、JWT 私钥、Access Token、Refresh Token 和 App Secret 都不能提交到 Git，也不要粘贴到日志、工单或部署文档中。

---

## 3. 模式 A：Docker 基础设施 + 本机 API

这是日常开发的推荐方式：Docker 只运行 PostgreSQL 和 Redis，本机直接运行当前工作区源码。修改 Python 文件后 Uvicorn 自动 reload，不需要重新构建 API 镜像。

### 3.1 准备 Docker 基础设施配置

在项目根目录创建 `.env.infra`。以下仅为本地示例，真实口令应自行生成：

```env
DOCKER_NETWORK_NAME=tsuz-api-main-local

POSTGRES_CONTAINER_NAME=tsuz-api-postgres
POSTGRES_DB=test_auth
POSTGRES_USER=test_user
POSTGRES_PASSWORD=<local-postgres-password>
POSTGRES_PORT=5432

REDIS_CONTAINER_NAME=tsuz-api-redis
REDIS_PORT=6379
```

如果本机端口已占用，可把宿主机映射改为其他端口：

```env
POSTGRES_PORT=15432
REDIS_PORT=16379
```

这种修改只改变宿主机访问端口；容器内部仍监听 PostgreSQL `5432` 和 Redis `6379`。

### 3.2 启动 PostgreSQL 和 Redis

```bash
docker compose \
  --env-file .env.infra \
  -f docker-compose.infra.yml \
  up -d
```

检查容器状态和最近日志：

```bash
docker compose \
  --env-file .env.infra \
  -f docker-compose.infra.yml \
  ps

docker compose \
  --env-file .env.infra \
  -f docker-compose.infra.yml \
  logs --tail=100 postgres redis
```

`docker-compose.infra.yml` 使用命名数据卷 `postgres_data` 和 `redis_data`。停止或重建容器不会自动清空数据。

### 3.3 准备本机 API 配置

本机 API 不能使用 Docker DNS 名 `postgres`、`redis` 或自定义容器名作为连接主机，应使用 `127.0.0.1` 和 `.env.infra` 中映射到宿主机的端口。

如果已有 `.env`，先备份并确认不要覆盖其中的有效 JWT 密钥。用于模式 A 的关键配置如下：

```env
APP_ENV=test
DEBUG=true
LOG_LEVEL=debug
LOG_FORMAT=json
REQUEST_ID_HEADER=X-Request-ID

DATABASE_URL=postgresql+psycopg://test_user:<local-postgres-password>@127.0.0.1:5432/test_auth
DB_SSLMODE=disable
REDIS_URL=redis://127.0.0.1:6379/0

REDIS_KEY_PREFIX=auth:local:
TOKEN_BLACKLIST_PREFIX=auth:local:blacklist:jti:
REFRESH_TOKEN_PREFIX=auth:local:refresh:
SESSION_PREFIX=auth:local:session:

JWT_ALGORITHM=RS256
JWT_ISSUER=auth-service-local
JWT_AUDIENCE=backend-api-local
JWT_PRIVATE_KEY="<escaped-private-key>"
JWT_PUBLIC_KEY="<escaped-public-key>"

OPENAPI_ENABLED=true
DOCS_ENABLED=true
REDOC_ENABLED=true
```

若 `.env.infra` 使用 `POSTGRES_PORT=15432` 和 `REDIS_PORT=16379`，这里也要分别改成 `127.0.0.1:15432` 和 `127.0.0.1:16379`。

可以基于 `.env.local.example` 准备 `.env`，但必须把其中供 Compose 使用的 `postgres` 和 `redis` 主机名改成 `127.0.0.1`。`.env` 应只允许当前用户读取：

```bash
chmod 600 .env
```

需要生成新的本地 RS256 密钥时，可以先写入临时文件，再把 PEM 内容转换为 `.env` 支持的转义换行格式。写入 `.env` 后立即安全删除临时文件，不要提交或输出私钥。也可使用 `pdm run init` 已生成的现有本地密钥，但 `init` 本身会管理完整 Docker 开发栈，不是本模式的日常启动命令。

### 3.4 在 Docker 数据库执行迁移

本机进程读取 `.env` 后，通过宿主机端口连接 Docker PostgreSQL：

```bash
pdm run alembic-current
pdm run migrate
pdm run alembic-current
```

预期行为：

1. 第一次 `alembic-current` 显示数据库当前 revision；空数据库可能没有输出；
2. `pdm run migrate` 执行 `alembic upgrade head`；
3. 第二次 `alembic-current` 应显示当前仓库 head，例如当前版本为 `0003_app_management (head)`。

不要在部署脚本中写死 `0002_user_management` 或 `0003_app_management`。上线前应先备份数据库，并在测试数据库验证目标版本迁移。

### 3.5 执行幂等 Seed

```bash
pdm run seed
```

当前 Seed 会幂等确保以下基础数据存在：

- 本地管理员；
- `admin` 角色；
- 用户管理权限；
- 子应用管理权限；
- 管理员与角色、角色与权限之间的关联。

重复执行不会重复插入已有角色和权限。默认管理员仅用于本地开发，生产环境不得继续使用仓库默认密码。生产是否执行 Seed 必须经人工审查，并应在首次登录前修改或替换默认凭据。

### 3.6 启动本机 API

默认命令：

```bash
pdm run dev
```

当前环境若出现 Uvicorn access logger 与应用 JSON formatter 的兼容错误，可关闭 Uvicorn access log，保留应用自己的结构化请求日志：

```bash
pdm run uvicorn app.main:app \
  --reload \
  --host 0.0.0.0 \
  --port 8000 \
  --no-access-log
```

若需要后台运行并保存 PID：

```bash
mkdir -p /tmp/tsuz-api-main-host
nohup pdm run uvicorn app.main:app \
  --reload \
  --host 0.0.0.0 \
  --port 8000 \
  --no-access-log \
  > /tmp/tsuz-api-main-host/api.log 2>&1 &
printf '%s\n' "$!" > /tmp/tsuz-api-main-host/api.pid
```

查看本机 API 日志：

```bash
tail -f /tmp/tsuz-api-main-host/api.log
```

停止该后台 API launcher；Uvicorn reloader 会同步回收其子进程：

```bash
kill -TERM "$(cat /tmp/tsuz-api-main-host/api.pid)"
```

如果进程没有退出，或不是按以上方式启动，应先用 `lsof -nP -iTCP:8000 -sTCP:LISTEN` 和 `ps` 确认父子进程，再停止正确进程，不能向未经确认的 PID 或进程组发送信号。

### 3.7 验证服务

```bash
curl --fail -i http://127.0.0.1:8000/health
curl --fail -i http://127.0.0.1:8000/openapi.json
```

开发环境启用文档时，可打开：

- Swagger：`http://127.0.0.1:8000/docs`
- ReDoc：`http://127.0.0.1:8000/redoc`

### 3.8 日常更新当前源码

代码更新后，按以下顺序操作：

```bash
pdm install
pdm run migrate
pdm run alembic-current
pdm run seed
```

- Uvicorn `--reload` 会自动加载 Python 源码变化；
- 依赖或配置变化后应完整重启本机 API；
- Alembic 迁移应在启动依赖新结构的代码前完成；
- Seed 当前是幂等的，可在新增权限后再次执行。

---

## 4. 模式 B：完整 Docker API 部署

该模式把 PostgreSQL、Redis、API 和 nginx 都放在 Docker 中运行。长期基础设施由 `docker-compose.infra.yml` 管理，API/nginx 发布由 `docker-compose.deploy.yml` 管理。

### 4.1 启动长期基础设施

按模式 A 的 3.1 和 3.2 准备 `.env.infra` 并启动：

```bash
docker compose \
  --env-file .env.infra \
  -f docker-compose.infra.yml \
  up -d
```

确认共享网络存在：

```bash
docker network inspect "$(grep '^DOCKER_NETWORK_NAME=' .env.infra | cut -d= -f2-)"
```

### 4.2 准备 Docker API 的 `.env`

可从部署模板复制：

```bash
cp .env.deploy.example .env
chmod 600 .env
```

然后逐项填写真实值。关键区别是：Docker API 通过共享网络访问基础设施，因此连接主机必须使用 `.env.infra` 中配置的容器名，而不是 `127.0.0.1`。

示例结构：

```env
DOCKER_IMAGE_NAME=tsuz-api-main
APP_VERSION=<immutable-version>
CONTAINER_NAME=auth-service
NGINX_CONTAINER_NAME=auth-service-nginx
APP_PORT=8000
NGINX_PORT=8080

APP_ENV=test
DOCKER_NETWORK_NAME=tsuz-api-main-local
DATABASE_URL=postgresql+psycopg://test_user:<postgres-password>@tsuz-api-postgres:5432/test_auth
DB_SSLMODE=disable
REDIS_URL=redis://tsuz-api-redis:6379/0

JWT_PRIVATE_KEY="<escaped-private-key>"
JWT_PUBLIC_KEY="<escaped-public-key>"
```

必须满足：

- `.env` 和 `.env.infra` 的 `DOCKER_NETWORK_NAME` 相同；
- `DATABASE_URL` 主机等于 `POSTGRES_CONTAINER_NAME`；
- `REDIS_URL` 主机等于 `REDIS_CONTAINER_NAME`；
- 发布镜像 tag 不使用可变的 `latest`；
- 产品环境使用 Secrets 管理真实密码和 JWT 私钥；
- 产品环境默认关闭 Swagger、ReDoc 和 OpenAPI。

### 4.3 构建或拉取不可变镜像

本地根据当前源码构建：

```bash
docker build -t tsuz-api-main:<immutable-version> .
```

并令 `.env` 中的变量与镜像一致：

```env
DOCKER_IMAGE_NAME=tsuz-api-main
APP_VERSION=<immutable-version>
```

从镜像仓库部署时不在服务器重新构建源码，而是设置完整镜像名和不可变 tag，然后执行：

```bash
docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  pull api
```

### 4.4 先执行迁移，再启动新 API

使用目标镜像的一次性容器执行迁移：

```bash
docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  run --rm --no-deps api alembic current

docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  run --rm --no-deps api alembic upgrade head

docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  run --rm --no-deps api alembic current
```

在本地或经过批准的测试环境执行 Seed：

```bash
docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  run --rm --no-deps api python -m app.seed
```

产品环境不应自动创建仓库默认管理员。执行产品 Seed 前，必须审查 Seed 内容、目标数据库和凭据处理方案。

### 4.5 启动 API 和 nginx

```bash
docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  up -d --no-build api nginx
```

检查容器、健康状态和日志：

```bash
docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  ps
docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  logs --tail=100 api nginx
```

健康检查：

```bash
curl --fail -i http://127.0.0.1:8000/health
curl --fail -i http://127.0.0.1:8080/health
```

`docker-compose.deploy.yml` 中 API 由镜像的 Dockerfile CMD 启动，使用 Gunicorn + Uvicorn worker，不使用开发模式的 `--reload`。

### 4.6 使用一体化本地 Compose 的替代方式

如果只需要首次完整本地初始化，也可运行：

```bash
pdm install
cp .env.local.example .env
$EDITOR .env  # 替换两个 CHANGE_ME seed 值
chmod 600 .env
pdm run init
```

初始化器不会创建已知的本地管理员默认密码，因此会拒绝模板中的 `CHANGE_ME`。或直接使用 `docker-compose.yml`。它会从当前源码构建并运行 API、PostgreSQL、Redis 和 nginx，API 使用 Uvicorn reload 和源码挂载。这个方式适合一次性本地环境，不应与模式 A 在同一端口同时运行，否则 `8000`、`5432`、`6379` 或 `8080` 会冲突。

---

## 5. 鉴权与管理接口 Smoke Test

### 5.1 登录并安全提取 Access Token

以下命令把响应写入权限受限的临时文件，避免直接把完整 Token 输出到终端历史或文档：

```bash
umask 077
LOGIN_RESPONSE=$(mktemp)
curl --fail --silent --show-error \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin@example.com","password":"<local-admin-password>"}' \
  http://127.0.0.1:8000/auth/login \
  > "$LOGIN_RESPONSE"

ACCESS_TOKEN=$(python -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["access_token"])' \
  "$LOGIN_RESPONSE")
rm -f "$LOGIN_RESPONSE"
```

不要把 `$ACCESS_TOKEN` 写入 shell trace、日志、截图或提交文件。验证结束后执行：

```bash
unset ACCESS_TOKEN
```

### 5.2 调用用户管理接口

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  'http://127.0.0.1:8000/admin/users?page=1&page_size=20'
```

### 5.3 调用子应用管理接口

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  'http://127.0.0.1:8000/admin/apps?page=1&page_size=20'
```

### 5.4 为什么地址栏访问 `/admin/users` 返回 401

直接在浏览器地址栏打开：

```text
http://127.0.0.1:8000/admin/users?page=1&page_size=20
```

浏览器不会自动附带 Bearer Token，因此返回：

```json
{"detail":"invalid access token"}
```

这是预期的权限保护，不代表接口部署失败。需要先调用 `POST /auth/login`，再携带：

```http
Authorization: Bearer <access_token>
```

注意：

- 管理接口使用 Access Token，不能使用 Refresh Token；
- 已退出、被强制下线、已撤销 Session 或已进入 JTI 黑名单的 Access Token 会返回 401；
- 已认证但缺少目标权限时返回 403；
- Swagger 可在 `/docs` 中通过 **Authorize** 设置 Bearer Token；
- 产品环境通常关闭 Swagger，应用端必须自行添加 Authorization Header。

---

## 6. 部署检查清单

### 6.1 部署前

- [ ] 已确认目标分支、提交或不可变镜像 tag；
- [ ] 已备份 PostgreSQL，并确认恢复流程；
- [ ] `.env` 指向正确环境，没有指向生产以外的误目标；
- [ ] `.env` 权限为 `0600`，没有占位符或默认产品密码；
- [ ] PostgreSQL/Redis/JWT issuer、audience 和 key prefix 与目标环境匹配；
- [ ] 已在隔离测试数据库运行当前迁移；
- [ ] 没有本机 API 与 Docker API 同时占用 `8000`。

### 6.2 部署中

- [ ] PostgreSQL 和 Redis 可达；
- [ ] `alembic upgrade head` 成功；
- [ ] `alembic current` 显示当前 head；
- [ ] 经审查后执行了需要的 Seed；
- [ ] API 启动且 `/health` 返回 2xx；
- [ ] nginx 模式下代理 `/health` 返回 2xx；
- [ ] 日志没有 migration error、traceback 或持续 5xx。

### 6.3 部署后

- [ ] 管理员真实登录成功；
- [ ] `/auth/me` 成功；
- [ ] `/admin/users` 携带 Access Token 后成功；
- [ ] `/admin/apps` 携带 Access Token 后成功；
- [ ] 未携带 Token 返回 401，权限不足返回 403；
- [ ] logout 后旧 Access Token 不再可用；
- [ ] 响应、审计和日志不包含密码 Hash、完整 Token 或 App Secret；
- [ ] 临时登录响应文件和 shell Token 变量已删除。

---

## 7. 日志、停止和重启

### 7.1 模式 A

查看 API 日志：

```bash
tail -f /tmp/tsuz-api-main-host/api.log
```

停止后台 API：

```bash
kill -TERM "$(cat /tmp/tsuz-api-main-host/api.pid)"
```

停止 Docker 基础设施但保留数据卷：

```bash
docker compose \
  --env-file .env.infra \
  -f docker-compose.infra.yml \
  down
```

### 7.2 模式 B

查看日志：

```bash
docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  logs -f --tail=100 api nginx
```

只停止应用，不停止基础设施：

```bash
docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  stop api nginx
```

删除应用容器但保留基础设施和数据：

```bash
docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  down
```

重新启动同一镜像：

```bash
docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  up -d --no-build api nginx
```

---

## 8. 失败处理和回滚

### 8.1 迁移失败

1. 不启动依赖新数据库结构的 API；
2. 保存脱敏后的 Alembic 错误和数据库日志；
3. 检查 `alembic current`、目标镜像版本和数据库连接目标；
4. 从备份恢复或发布 forward repair 迁移；
5. 只在开发、测试或经过评审的回滚演练中执行 downgrade。

生产环境优先使用向前修复迁移。不能仅回滚 API 镜像，却忽略旧镜像是否兼容已升级的数据库结构。

### 8.2 API 启动失败

检查：

```bash
docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  ps
docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  logs --tail=200 api
```

常见原因：

- 本机 `.env` 仍使用容器 DNS 名，或 Docker API 错误使用 `127.0.0.1`；
- `.env` 与 `.env.infra` 的网络名、容器名或端口不一致；
- JWT PEM 换行格式错误；
- 数据库尚未升级到 head；
- `8000` 或 `8080` 已被其他进程占用；
- 新镜像缺少当前迁移或依赖。

### 8.3 镜像回滚

将 `.env` 中 `APP_VERSION` 切回已验证的不可变历史 tag，再执行：

```bash
docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  pull api
docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  up -d --no-build api nginx
```

回滚前必须确认历史镜像与当前数据库 schema 兼容。若不兼容，应先制定经过验证的数据迁移或恢复方案，不能直接对生产数据库执行临时 downgrade。

---

## 9. 数据与安全红线

除非已经明确决定永久删除本地数据，否则禁止执行：

```bash
docker compose \
  --env-file .env.infra \
  -f docker-compose.infra.yml \
  down -v
```

同时遵守：

- 不在开发或生产数据库执行迁移 downgrade 验证；应使用临时隔离数据库；
- 不执行 Redis `FLUSHDB` 或 `FLUSHALL`；清理测试数据时只删除专用 prefix；
- 不把数据库口令、JWT 私钥、Access/Refresh Token、Authorization Header 或 App Secret写入文档与日志；
- App Secret 只在创建或重新生成时返回一次，调用方应立即保存到安全的 Secret 管理系统；
- 不复用不同环境的数据库、Redis DB/key prefix、JWT 密钥、issuer 或 audience；
- 产品环境禁用仓库默认管理员密码，并按最小权限配置数据库和部署账号；
- 对外发布前启用 TLS，并通过 nginx、负载均衡器或 ingress 代理 API；
- 发布后使用 Request ID 排查请求，但日志仍必须保持敏感字段脱敏。

---

## 10. 可选的隔离验证

仓库提供真实 PostgreSQL/Redis 集成验证入口：

```bash
pdm run phase4-validate
pdm run app-phase5-validate
```

这些验证器用于隔离测试数据库和专用 Redis prefix，不应直接针对开发或生产数据运行。真实集成 pytest 默认跳过；只有按对应文档准备安全的临时基础设施并显式设置 opt-in 环境变量后才执行。

日常代码检查：

```bash
pdm run lint
pdm run test
git diff --check
```

部署验证的最终依据应是：当前 revision 为 Alembic head、健康检查通过、真实鉴权 smoke test 通过、日志无持续异常，并且数据库和 Redis 数据没有被破坏性清理。
