# Docker 基础设施 + 本机 API 更新方案

## 1. 目标

不构建、不启动 Docker API，只执行以下操作：

1. Docker 继续运行 PostgreSQL 和 Redis；
2. 将 Docker PostgreSQL 的 Alembic 迁移升级到当前仓库 `head`；
3. 将当前 Seed 更新到 Docker PostgreSQL；
4. 在本机使用当前源码启动 API。

---

## 2. 确认 Docker PostgreSQL 和 Redis 正常运行

如果使用 `docker-compose.infra.yml`：

```bash
docker compose \
  --env-file .env.infra \
  -f docker-compose.infra.yml \
  up -d postgres redis

docker compose \
  --env-file .env.infra \
  -f docker-compose.infra.yml \
  ps
```

如果基础设施已经运行，只需执行 `ps` 检查状态，不需要重建或清空容器。

不要执行：

```bash
docker compose \
  --env-file .env.infra \
  -f docker-compose.infra.yml \
  down -v
```

该命令会删除 PostgreSQL 和 Redis 数据卷。

---

## 3. 配置本机连接 Docker 基础设施

本机 API 和本机 Alembic/Seed 进程必须通过 Docker 映射到宿主机的端口连接 PostgreSQL 和 Redis。因此 `.env` 中应使用 `127.0.0.1`，不能使用只在 Docker 网络中可解析的 `postgres` 或 `redis`。

示例：

```env
DATABASE_URL=postgresql+psycopg://<user>:<password>@127.0.0.1:5432/<database>
REDIS_URL=redis://127.0.0.1:6379/0
```

如果 `.env.infra` 使用了其他宿主机端口，例如：

```env
POSTGRES_PORT=15432
REDIS_PORT=16379
```

则 `.env` 应对应配置为：

```env
DATABASE_URL=postgresql+psycopg://<user>:<password>@127.0.0.1:15432/<database>
REDIS_URL=redis://127.0.0.1:16379/0
```

保留 `.env` 中已有的 JWT、Redis Key Prefix 和其他 API 配置，不要在文档或日志中输出真实密码和 JWT 私钥。

---

## 4. 将 Alembic 更新到 Docker PostgreSQL

先检查当前数据库版本：

```bash
pdm run alembic-current
```

升级到当前仓库的 Alembic `head`：

```bash
pdm run migrate
```

再次检查版本：

```bash
pdm run alembic-current
```

`pdm run migrate` 等价于：

```bash
alembic upgrade head
```

部署时始终使用 `head`，不要把命令固定为某个历史 revision。

---

## 5. 将 Seed 更新到 Docker PostgreSQL

执行当前源码中的幂等 Seed：

```bash
pdm run seed
```

该命令会补充当前版本需要的管理员角色及用户管理、子应用管理等权限。Seed 是幂等的，已有数据不会被重复创建。

本地默认管理员仅用于开发环境，生产环境不得继续使用默认密码。

---

## 6. 启动本机 API

不构建 Docker API，也不启动 Docker API 容器。本机直接运行当前源码：

```bash
pdm run uvicorn app.main:app \
  --reload \
  --host 0.0.0.0 \
  --port 8000 \
  --no-access-log
```

`--no-access-log` 用于避免 Uvicorn access logger 与当前应用 JSON 日志格式之间的兼容错误；应用自身的请求日志仍会保留。

API 地址：

```text
http://127.0.0.1:8000
```

Swagger 地址：

```text
http://127.0.0.1:8000/docs
```

---

## 7. 验证

检查健康接口：

```bash
curl --fail -i http://127.0.0.1:8000/health
```

检查 OpenAPI：

```bash
curl --fail -i http://127.0.0.1:8000/openapi.json
```

管理接口需要先登录并携带 Access Token。直接在浏览器地址栏访问 `/admin/users` 不会附带 Bearer Token，因此返回 `invalid access token` 属于预期行为。

---

## 8. 后续代码更新流程

以后更新本机源码后，按以下顺序操作即可：

```bash
pdm install
pdm run migrate
pdm run alembic-current
pdm run seed
```

然后启动或重启本机 API：

```bash
pdm run uvicorn app.main:app \
  --reload \
  --host 0.0.0.0 \
  --port 8000 \
  --no-access-log
```

整个流程不需要构建 Docker API 镜像，也不需要启动 Docker API 容器。
