# QQ OAuth 第一阶段实施计划

## 1. 背景

本计划依据 [QQ 扫码登录接入完整方案](../docs/jiggly-scribbling-bunny.md)，实施 QQ OAuth 的第一阶段“配置、用户模型与数据库迁移”。

本阶段只建立后续 QQ 登录所需的配置、部署传播、用户身份数据结构和 Alembic 迁移，不提前实现 OAuth HTTP 调用、Redis state/ticket、登录路由或自动注册事务。

## 2. 阶段目标

1. 增加 QQ 网站应用 OAuth 配置，并通过环境变量安全加载。
2. 在部署流程中安全传播 `APP_ID`、`APP_KEY` 和其他 QQ 配置。
3. 允许 QQ-only 用户在数据库中不保存本地邮箱和密码。
4. 新增第三方身份模型 `UserIdentity`。
5. 新增 Alembic `0007_qq_login`，完成结构升级和安全降级保护。
6. 更新依赖最新 Alembic head 的既有验证入口。
7. 补充配置、ORM、迁移和部署静态测试。
8. 保持现有邮箱用户、管理员用户、Session、Refresh Token 和 RBAC 行为不变。

## 3. 范围边界

### 3.1 本阶段实现

- QQ OAuth 配置和环境变量映射；
- 本地与部署环境示例配置；
- GitHub Actions 部署配置传播；
- `users.email` 和 `users.hashed_password` 数据库层可空；
- `UserIdentity` ORM 模型；
- `0007_qq_login` Alembic 迁移；
- 升级、降级保护、唯一约束、外键级联和模型一致性测试；
- 既有 migration-head 断言更新；
- 认证单元测试的 Redis 依赖隔离修复。

### 3.2 本阶段不实现

- QQ authorize、token、openid 和用户资料 HTTP 请求；
- Redis OAuth state 和 ticket 存储；
- `GET /auth/qq/login`；
- `GET /auth/qq/callback`；
- `POST /auth/qq/exchange`；
- 首次 QQ 登录自动注册事务；
- `normal` 角色绑定；
- QQ 与邮箱的绑定或解绑；
- `/auth/me` 的身份响应扩展；
- 真实 QQ 扫码联调；
- 生产数据库迁移或生产部署。

## 4. 安全约束

1. `APP_KEY`、数据库密码及其他 Secret 只能使用空值、占位符或部署平台 Secret，不写入代码、文档、测试输出或日志。
2. 不读取、修改或覆盖可能包含真实凭证的运行时 `.env` 文件。
3. QQ 回调地址和 ticket 消费端地址由部署环境显式提供，生产部署不得静默回退到测试地址。
4. 单元测试不得访问共享 Redis、共享业务数据库或生产资源。
5. PostgreSQL 迁移测试默认跳过，仅在显式 opt-in 且确认使用本地、隔离、非默认端口管理员连接时执行。
6. 迁移测试必须创建随机临时数据库，并在 `finally` 中终止连接和删除数据库。
7. 存在空邮箱或空密码用户时，`0007` downgrade 必须在结构修改前失败，禁止填充伪邮箱、删除 QQ-only 用户或静默丢失身份数据。

## 5. 配置设计

### 5.1 应用配置

在 `app/core/config.py` 增加：

| 配置 | 默认值或来源 |
| --- | --- |
| `APP_ID` | 默认空，通过环境变量注入 |
| `APP_KEY` | 默认空，通过 Secret 注入 |
| `QQ_REDIRECT_URI` | 默认空，由环境显式配置 |
| `QQ_TICKET_REDIRECT_URI` | 默认空，由环境显式配置 |
| `QQ_AUTHORIZE_URL` | `https://graph.qq.com/oauth2.0/authorize` |
| `QQ_TOKEN_URL` | `https://graph.qq.com/oauth2.0/token` |
| `QQ_OPENID_URL` | `https://graph.qq.com/oauth2.0/me` |
| `QQ_USER_INFO_URL` | `https://graph.qq.com/user/get_user_info` |
| `QQ_STATE_PREFIX` | 环境隔离的 Redis 前缀 |
| `QQ_TICKET_PREFIX` | 环境隔离的 Redis 前缀 |
| `QQ_STATE_TTL_SECONDS` | `300` |
| `QQ_TICKET_TTL_SECONDS` | `60` |
| `QQ_HTTP_TIMEOUT_SECONDS` | `10` |

`APP_ID` 和 `APP_KEY` 使用显式 Pydantic 环境变量别名，确保加载准确的大写变量名。配置对象初始化时不发起网络请求，也不输出密钥。

### 5.2 环境模板

更新：

- `.env.local.example`；
- `.env.deploy.example`。

模板只保存空值或占位符。测试环境回调地址和 Redis 前缀与正式环境隔离。

### 5.3 部署传播

修改 `.github/workflows/deploy.yml`：

- `APP_ID`、`APP_KEY` 从 GitHub Environment Secrets 注入；
- QQ URL、回调地址、Redis 前缀、TTL 和超时从 Environment Variables 注入；
- 生成运行时环境文件前检查必填 Secret 和两个回调地址；
- 回调地址直接传播，不使用测试域名 fallback；
- 保持 `umask 077`、文件权限控制和生成文件清理；
- 不使用会打印 Secret 的命令。

## 6. ORM 设计

### 6.1 `User` 调整

在 `app/models/user.py` 中将：

```python
email: Mapped[str | None]
hashed_password: Mapped[str | None]
```

数据库层允许 `NULL`，以支持没有本地邮箱和密码的 QQ-only 用户。

保持不变：

- 邮箱唯一索引；
- 现有邮箱注册和管理员创建请求校验；
- 现有用户状态、时间戳和 RBAC 字段；
- 邮箱用户创建路径仍写入非空邮箱和密码。

### 6.2 `UserIdentity` 模型

新增 `app/models/user_identity.py`，包含：

- `id`；
- `user_id`；
- `provider`；
- `provider_subject`；
- `display_name`；
- `avatar`；
- `verified`；
- `created_at`；
- `updated_at`；
- `last_login_at`。

约束和索引：

- `user_id -> users.id` 外键；
- `ON DELETE CASCADE`；
- `(provider, provider_subject)` 命名联合唯一约束；
- `user_id` 索引；
- `(user_id, provider)` 联合索引。

本阶段不增加 ORM relationship、身份服务或路由逻辑。

### 6.3 模型注册

在以下位置显式导入 `UserIdentity`：

- `app/models/__init__.py`；
- `alembic/env.py`。

确保 `Base.metadata` 和 `alembic check` 能识别新表。

## 7. Alembic `0007_qq_login`

迁移链：

```text
0006_email_registration
  → 0007_qq_login
```

### 7.1 Upgrade

1. 将 `users.email` 改为可空；
2. 将 `users.hashed_password` 改为可空；
3. 创建 `user_identities` 表；
4. 创建联合唯一约束、外键和索引；
5. 保留现有邮箱用户、角色、权限及关联数据；
6. 不清洗、改写或回填历史用户数据。

### 7.2 Downgrade

1. 在删除身份表或修改字段前，查询是否存在 `email IS NULL` 或 `hashed_password IS NULL` 的用户；
2. 如果存在，抛出明确错误并停止降级；
3. 数据满足条件时删除 `user_identities`；
4. 恢复 `users.email` 和 `users.hashed_password` 的 `NOT NULL`；
5. 不伪造邮箱、不删除 QQ-only 用户、不静默丢弃身份数据。

## 8. 测试设计

### 8.1 配置测试

验证：

- QQ endpoint、TTL 和 HTTP timeout 默认值；
- `APP_ID`、`APP_KEY` 环境变量映射；
- 两个回调地址读取；
- 测试不读取运行时 `.env`。

### 8.2 ORM 测试

使用 SQLite 内存数据库验证：

- 可创建 `email=None`、`hashed_password=None` 的 QQ-only 用户；
- 可创建 QQ identity；
- identity 字段可持久化；
- `(provider, provider_subject)` 唯一约束存在于元数据。

### 8.3 PostgreSQL 迁移测试

`tests/test_qq_login_migration.py` 默认跳过，只有设置 `RUN_QQ_LOGIN_MIGRATION=1` 才执行。

测试覆盖：

- `0006 -> 0007`；
- 历史邮箱用户数据保留；
- 两个用户字段变为可空；
- identity 表列、索引、唯一约束和级联外键；
- QQ-only 用户和 identity 插入；
- 重复 provider/subject 被拒绝；
- `alembic check` 无漂移；
- 有 QQ-only 用户时 downgrade 失败且数据保持；
- 清理 QQ-only 数据后安全 downgrade；
- 恢复 `NOT NULL`；
- 再次 upgrade 到 head。

### 8.4 认证测试 Redis 隔离

`AuthService` 内部的 `SessionService` 会访问 Redis。认证单元测试使用进程内最小 Fake Redis，并 monkeypatch `app.services.session_service.get_redis`，覆盖 session active 检查和撤销写入，避免默认测试连接真实 Redis。

不修改生产 Redis 连接行为，不新增外部 Fake Redis 依赖。

### 8.5 部署静态测试

验证：

- Secrets 注入；
- 必填配置检查；
- 运行时环境文件写入；
- 回调地址不回退到测试域名；
- Redis 前缀按环境隔离；
- 工作流不显式输出 Secret。

## 9. 文件清单

### 9.1 新增

- `app/models/user_identity.py`；
- `alembic/versions/0007_qq_login.py`；
- `tests/test_qq_login_config.py`；
- `tests/test_qq_login_models.py`；
- `tests/test_qq_login_migration.py`；
- `plan/QQ_OAUTH_PHASE_1_IMPLEMENTATION_PLAN.md`；
- `plan/QQ_OAUTH_PHASE_1_EXECUTION.md`。

### 9.2 修改

- `.env.local.example`；
- `.env.deploy.example`；
- `.github/workflows/deploy.yml`；
- `app/core/config.py`；
- `app/models/user.py`；
- `app/models/__init__.py`；
- `alembic/env.py`；
- `tests/test_auth_service.py`；
- `tests/test_phase_4_deployment_checks.py`；
- 依赖当前 migration head 的验证脚本和集成测试。

## 10. 验证命令

```bash
pdm run pytest tests/test_auth_service.py
pdm run pytest \
  tests/test_qq_login_config.py \
  tests/test_qq_login_models.py \
  tests/test_qq_login_migration.py \
  tests/test_user_management_models.py \
  tests/test_phase_4_deployment_checks.py
pdm run pytest
pdm run ruff check \
  app/core/config.py \
  app/models/__init__.py \
  app/models/user.py \
  app/models/user_identity.py \
  alembic/env.py \
  alembic/versions/0007_qq_login.py \
  tests/test_auth_service.py \
  tests/test_qq_login_config.py \
  tests/test_qq_login_models.py \
  tests/test_qq_login_migration.py \
  tests/test_phase_4_deployment_checks.py
pdm run lint
pdm lock --check
git diff --check
```

隔离 PostgreSQL 可用时再显式执行：

```bash
RUN_QQ_LOGIN_MIGRATION=1 pdm run pytest tests/test_qq_login_migration.py
```

## 11. 验收标准

- QQ 配置可通过环境变量加载，仓库和日志中没有真实 Secret；
- 部署工作流安全传播 QQ 配置，生产缺少回调地址时明确失败；
- QQ-only 用户可保存空邮箱和空密码；
- `UserIdentity` 约束、索引和级联外键符合设计；
- ORM 与 Alembic 迁移结构一致；
- 有 QQ-only 数据时 downgrade 明确失败且不破坏数据；
- 现有邮箱认证、管理员、Session、Refresh Token 和 RBAC 测试不回归；
- 默认测试不访问真实 QQ、共享 Redis 或共享数据库；
- PostgreSQL 迁移往返要么在隔离资源中通过，要么明确记录为未执行，不能虚报；
- 第一阶段之外的 QQ OAuth 业务没有被提前实现。
