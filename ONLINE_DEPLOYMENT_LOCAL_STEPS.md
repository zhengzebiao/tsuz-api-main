# 按线上部署方式在本地运行

如果一定要按线上部署方式运行，可以先执行 `infra`，再执行 `deploy`。

- `infra`：启动长期存在的 PostgreSQL、Redis，并创建共享 Docker 网络。
- `deploy`：构建/启动 API 与 Nginx，并让它们接入 `infra` 创建的共享网络。
- API 容器内连接 PostgreSQL、Redis 时使用容器名，不使用 `localhost`。

## 1. 准备 `.env.infra`

在项目根目录创建或确认 `.env.infra`：

```env
DOCKER_NETWORK_NAME=tsuz-api-main-test

POSTGRES_CONTAINER_NAME=postgres
POSTGRES_DB=test_auth
POSTGRES_USER=test_user
POSTGRES_PASSWORD=test_password
POSTGRES_PORT=5432

REDIS_CONTAINER_NAME=redis
REDIS_PORT=6379
```

如果本机 `5432` 或 `6379` 端口被占用，可以改成其他宿主机端口，例如：

```env
POSTGRES_PORT=15432
REDIS_PORT=16379
```

注意：API 容器内部仍然通过 `postgres:5432`、`redis:6379` 访问。

## 2. 准备 `.env`

复制部署模板：

```bash
cp .env.deploy.example .env
chmod 600 .env
```

编辑 `.env`，至少保证以下值正确：

```env
DOCKER_IMAGE_NAME=tsuz-api-main-test
APP_VERSION=local

CONTAINER_NAME=auth-service
NGINX_CONTAINER_NAME=auth-service-nginx
APP_PORT=8000
NGINX_PORT=8080

APP_ENV=test
DOCKER_NETWORK_NAME=tsuz-api-main-test

DATABASE_URL=postgresql+psycopg://test_user:test_password@postgres:5432/test_auth
DB_SSLMODE=disable

REDIS_URL=redis://redis:6379/0
REDIS_KEY_PREFIX=auth:test:

JWT_ISSUER=auth-service-test
JWT_AUDIENCE=backend-api-test

TOKEN_BLACKLIST_PREFIX=auth:test:blacklist:jti:
REFRESH_TOKEN_PREFIX=auth:test:refresh:
SESSION_PREFIX=auth:test:session:

OPENAPI_ENABLED=true
DOCS_ENABLED=true
REDOC_ENABLED=true
```

`DOCKER_NETWORK_NAME` 必须与 `.env.infra` 中一致。

## 3. 生成 JWT 密钥

```bash
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out jwt-private.pem
openssl rsa -pubout -in jwt-private.pem -out jwt-public.pem
```

将 `jwt-private.pem` 内容写入 `.env` 的：

```env
JWT_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----"
```

将 `jwt-public.pem` 内容写入 `.env` 的：

```env
JWT_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----"
```

写入 `.env` 后删除临时密钥文件：

```bash
rm jwt-private.pem jwt-public.pem
```

## 4. 先启动 infra

```bash
docker compose \
  --env-file .env.infra \
  -f docker-compose.infra.yml \
  up -d
```

查看基础设施状态：

```bash
docker compose \
  --env-file .env.infra \
  -f docker-compose.infra.yml \
  ps
```

确认共享网络存在：

```bash
docker network inspect tsuz-api-main-test
```

## 5. 构建 API 镜像

`docker-compose.deploy.yml` 使用的是已有镜像，因此本地需要先构建：

```bash
docker build -t tsuz-api-main-test:local .
```

确认镜像存在：

```bash
docker image inspect tsuz-api-main-test:local
```

## 6. 执行数据库迁移

```bash
docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  run --rm --no-deps api alembic upgrade head
```

查看当前 Alembic 版本：

```bash
docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  run --rm --no-deps api alembic current
```

## 7. 测试环境初始化默认用户

线上部署默认不自动 seed；如果是本地或测试环境需要默认账号，显式执行：

```bash
docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  run --rm --no-deps api python -m app.seed
```

默认测试账号：

```text
username: admin@example.com
password: password123
```

## 8. 再启动 deploy

```bash
docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  up -d --no-build api nginx
```

查看应用状态：

```bash
docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  ps
```

查看日志：

```bash
docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  logs -f --tail=100 api nginx
```

## 9. 验证服务

直连 API：

```bash
curl --fail -i http://127.0.0.1:8000/health
```

通过 Nginx：

```bash
curl --fail -i http://127.0.0.1:8080/health
```

测试登录：

```bash
curl -i \
  -H "Content-Type: application/json" \
  -d '{"username":"admin@example.com","password":"password123"}' \
  http://127.0.0.1:8080/auth/login
```

## 10. 日常重新部署 API

基础设施不动，只重新构建、迁移并重启应用：

```bash
docker build -t tsuz-api-main-test:local .

docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  run --rm --no-deps api alembic upgrade head

docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  up -d --no-build api nginx

curl --fail http://127.0.0.1:8080/health
```

## 11. 停止服务

只停止 API 和 Nginx：

```bash
docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  stop api nginx
```

停止 API 和 Nginx，并删除应用容器：

```bash
docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  down
```

停止 PostgreSQL 和 Redis，但保留数据卷：

```bash
docker compose \
  --env-file .env.infra \
  -f docker-compose.infra.yml \
  down
```

不要执行下面命令，除非明确要删除 PostgreSQL 和 Redis 数据：

```bash
docker compose \
  --env-file .env.infra \
  -f docker-compose.infra.yml \
  down -v
```
