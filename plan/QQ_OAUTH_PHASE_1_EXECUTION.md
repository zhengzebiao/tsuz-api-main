# QQ OAuth 第一阶段开发执行记录

## 1. 执行范围

本次根据 [QQ OAuth 第一阶段实施计划](QQ_OAUTH_PHASE_1_IMPLEMENTATION_PLAN.md) 完成“配置、用户模型与数据库迁移”，并处理完整测试中暴露的认证单元测试 Redis 隔离问题。

本阶段已完成：

1. QQ OAuth 配置和环境变量映射；
2. 本地与部署环境示例配置；
3. GitHub Actions 部署 Secret/Variable 传播；
4. `users.email` 和 `users.hashed_password` 数据库层可空；
5. `UserIdentity` ORM 模型；
6. Alembic `0007_qq_login`；
7. 配置、模型、迁移和部署静态测试；
8. 依赖最新 Alembic head 的既有验证断言更新；
9. `tests/test_auth_service.py` 的 Redis 单元测试隔离修复；
10. 中文实施计划和本执行记录导出。

本阶段明确未实现：

- QQ HTTP 请求；
- Redis OAuth state/ticket；
- QQ 登录、回调和 ticket exchange 路由；
- 首次 QQ 登录自动注册事务；
- QQ 与邮箱绑定或解绑；
- `/auth/me` 的身份响应扩展；
- 真实 QQ 扫码联调；
- 生产数据库迁移；
- 生产部署。

## 2. 配置与部署变更

### 2.1 应用配置

在 `app/core/config.py` 增加：

| 配置 | 当前行为 |
| --- | --- |
| `APP_ID` | 显式映射环境变量，默认空 |
| `APP_KEY` | 显式映射环境变量，默认空 |
| `QQ_REDIRECT_URI` | 默认空，由环境提供 |
| `QQ_TICKET_REDIRECT_URI` | 默认空，由环境提供 |
| `QQ_AUTHORIZE_URL` | QQ authorize endpoint |
| `QQ_TOKEN_URL` | QQ token endpoint |
| `QQ_OPENID_URL` | QQ openid endpoint |
| `QQ_USER_INFO_URL` | QQ 用户资料 endpoint |
| `QQ_STATE_PREFIX` | 环境隔离 Redis 前缀 |
| `QQ_TICKET_PREFIX` | 环境隔离 Redis 前缀 |
| `QQ_STATE_TTL_SECONDS` | `300` |
| `QQ_TICKET_TTL_SECONDS` | `60` |
| `QQ_HTTP_TIMEOUT_SECONDS` | `10` |

配置加载不发起 QQ 网络请求，也不输出密钥。

### 2.2 环境模板

已更新：

- `.env.local.example`；
- `.env.deploy.example`。

`APP_KEY` 仅使用空值或占位符，未读取、写入或输出任何真实 QQ 凭证。

测试环境示例使用：

- 测试 API callback；
- 测试消费端地址；
- `tsuz:main:test:qq:*` 隔离前缀。

### 2.3 部署工作流

`.github/workflows/deploy.yml` 已实现：

- 从 GitHub Environment Secrets 注入 `APP_ID`、`APP_KEY`；
- 从 Environment Variables 注入两个回调地址、QQ endpoints、前缀、TTL 和超时；
- 生成运行时环境文件前检查 `APP_ID`、`APP_KEY` 和两个回调地址；
- 两个回调地址直接使用部署环境值，不再静默回退到测试域名；
- 其他非敏感 QQ 配置保留安全默认值；
- 保持 `umask 077`、远端环境文件权限控制和本地生成文件清理；
- 静态测试确认工作流没有显式输出 `APP_ID` 或 `APP_KEY`。

## 3. 用户模型与身份模型

### 3.1 `User` 可空字段

`app/models/user.py` 已将：

```python
email: Mapped[str | None]
hashed_password: Mapped[str | None]
```

并显式设置数据库列可空。

保留了邮箱唯一索引。现有邮箱注册、邮箱登录和管理员创建用户的请求层校验没有放宽。

### 3.2 `UserIdentity`

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

已建立：

- `user_id -> users.id` 外键；
- `ON DELETE CASCADE`；
- `(provider, provider_subject)` 联合唯一约束；
- `user_id` 索引；
- `(user_id, provider)` 联合索引。

`app/models/__init__.py` 和 `alembic/env.py` 已注册该模型。

## 4. Alembic `0007_qq_login`

新增：

```text
alembic/versions/0007_qq_login.py
```

迁移链：

```text
0006_email_registration
  → 0007_qq_login
```

### 4.1 Upgrade

- 将 `users.email` 改为可空；
- 将 `users.hashed_password` 改为可空；
- 创建 `user_identities` 表；
- 创建联合唯一约束、级联外键和查询索引；
- 不改写历史邮箱用户数据。

### 4.2 Downgrade 保护

降级前先检查：

```sql
SELECT count(*)
FROM users
WHERE email IS NULL OR hashed_password IS NULL;
```

存在此类用户时抛出明确错误，且检查发生在删除身份表和恢复 `NOT NULL` 之前。

未实现以下破坏性降级方式：

- 填充伪邮箱；
- 填充伪密码；
- 删除 QQ-only 用户；
- 静默删除身份记录。

## 5. 认证测试 Redis 隔离修复

### 5.1 原因

完整测试此前在 `tests/test_auth_service.py` 失败，调用链为：

```text
AuthService.current_user/logout
  → SessionService
  → app.services.session_service.get_redis()
  → 真实 Redis 连接
```

测试虽然替换了 token、refresh token 和 blacklist service，但 `AuthService.sessions` 仍保留真实 `SessionService`，因此认证单元测试意外访问运行环境 Redis。

### 5.2 修复

在 `tests/test_auth_service.py` 增加最小进程内 Fake Redis：

- `get()`；
- `set()`；
- 记录可选 TTL。

通过自动使用的 pytest fixture monkeypatch：

```python
app.services.session_service.get_redis
```

从而覆盖 session active 检查和会话撤销写入路径。

该修复：

- 不修改生产代码；
- 不连接共享 Redis；
- 不新增 `fakeredis` 等依赖；
- 不通过 skip 隐藏失败。

## 6. 测试和验证结果

### 6.1 认证服务定向测试

执行：

```text
pdm run pytest tests/test_auth_service.py
```

结果：

```text
19 passed, 1 warning
```

认证测试已不再访问真实 Redis。

### 6.2 QQ、用户模型和部署定向测试

执行：

```text
pdm run pytest tests/test_qq_login_config.py tests/test_qq_login_models.py tests/test_qq_login_migration.py tests/test_user_management_models.py tests/test_phase_4_deployment_checks.py
```

结果：

```text
21 passed, 1 skipped, 1 warning
```

跳过项是默认 opt-in 的 PostgreSQL QQ 迁移往返测试。

### 6.3 完整默认测试

执行：

```text
pdm run pytest
```

结果：

```text
305 passed, 16 skipped, 1 warning
```

完整默认测试没有失败。16 个跳过项为项目中需要显式环境或基础设施的迁移/集成测试，其中包括 QQ PostgreSQL 迁移测试。

### 6.4 QQ 相关定向 Ruff

执行 QQ 第一阶段相关文件的 `ruff check`，包括配置、模型、迁移、认证测试、QQ 测试和部署检查测试。

结果：

```text
All checks passed!
```

### 6.5 仓库级 Ruff

执行：

```text
pdm run lint
```

结果：未通过。

报告内容主要包括：

- `0001` 至 `0006` 既有 Alembic 文件的 `I001` import-order；
- 既有 FastAPI 管理端路由中的 `B008` `Depends(...)`/依赖工厂调用；
- 其他既有模块的 import-order。

这些问题不位于本次 QQ 第一阶段新增文件；QQ 第一阶段相关文件的定向 Ruff 已全部通过。本次未扩大范围修改历史迁移和管理端 API。

### 6.6 依赖锁和差异检查

执行结果：

```text
pdm lock --check：通过
git diff --check：通过
```

### 6.7 PostgreSQL 迁移往返

首次检查专用测试端口 `127.0.0.1:55432` 时未检测到服务。随后根据本地环境确认 PostgreSQL 运行在 `127.0.0.1:5432`，并在显式允许默认端口后执行迁移测试：

```bash
RUN_QQ_LOGIN_MIGRATION=1 \\
QQ_LOGIN_ALLOW_DEFAULT_PORT=1 \\
QQ_LOGIN_ADMIN_DATABASE_URL='postgresql+psycopg://test_user:test_password@127.0.0.1:5432/postgres' \\
pdm run pytest tests/test_qq_login_migration.py
```

结果：

```text
1 passed, 1 warning
```

迁移测试通过，覆盖：

- `0006_email_registration -> 0007_qq_login` upgrade；
- 历史邮箱用户数据保留；
- `email` 和 `hashed_password` 可空；
- `user_identities` 列、索引、联合唯一约束和 `ON DELETE CASCADE`；
- QQ-only 用户和 QQ identity 插入；
- 重复 provider/subject 被拒绝；
- `alembic check` 无模型漂移；
- 存在 QQ-only 用户时 downgrade 安全失败；
- 清理 QQ-only 数据后安全 downgrade；
- 恢复 `NOT NULL`；
- 再次 upgrade 到 head。

测试使用随机临时数据库，并在 `finally` 中终止连接、删除临时数据库和释放 engine。测试输出没有记录完整数据库凭证。

### 6.8 警告

测试中的唯一警告为项目既有的 Starlette TestClient/httpx 弃用提示：

```text
StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated
```

该警告不影响测试结果，也不属于 QQ 第一阶段功能。

## 7. 文件变更摘要

### 7.1 新增

```text
alembic/versions/0007_qq_login.py
app/models/user_identity.py
tests/test_qq_login_config.py
tests/test_qq_login_migration.py
tests/test_qq_login_models.py
plan/QQ_OAUTH_PHASE_1_IMPLEMENTATION_PLAN.md
plan/QQ_OAUTH_PHASE_1_EXECUTION.md
```

### 7.2 本阶段修改

```text
.env.deploy.example
.env.local.example
.github/workflows/deploy.yml
alembic/env.py
app/core/config.py
app/models/__init__.py
app/models/user.py
tests/test_auth_service.py
tests/test_phase_4_deployment_checks.py
```

另外更新了依赖当前 migration head 的验证脚本和集成测试，使 `upgrade head` 后的预期 revision 指向 `0007_qq_login`，同时保留需要验证历史中间版本的测试语义。

工作区中原有的其他修改已保留，未执行 commit 或 push。

## 8. 第一阶段结论

### 8.1 已完成

- QQ 配置、环境模板和部署传播已实现；
- `User` 可支持 QQ-only 用户；
- `UserIdentity` ORM 和 `0007_qq_login` 已实现；
- downgrade 安全保护已实现；
- 配置、模型、部署和默认回归测试通过；
- 认证测试的真实 Redis 依赖已隔离；
- 完整默认 pytest 通过；
- QQ 相关定向 Ruff、锁文件检查和差异检查通过；
- 中文计划和执行记录已导出。

### 8.2 尚待环境验证

本阶段计划内的 PostgreSQL 迁移环境验证已经完成。当前仍未执行的内容是：

- 真实 QQ OAuth 扫码和第三方 HTTP 联调；
- 生产数据库迁移；
- 生产部署和正式 Secret 验证。

这些内容明确属于后续阶段或生产发布流程，不属于本阶段的本地迁移验收。

### 8.3 验收判断

QQ OAuth 第一阶段的代码实现、默认自动化测试和隔离 PostgreSQL 迁移往返验证已经完成。

仓库级 Ruff 仍有既有历史问题，但 QQ 第一阶段相关文件的定向 Ruff 已通过；该问题不改变本阶段功能验证结论。

下一阶段应从 QQ OAuth Service、state/ticket 和自动注册基础能力开始，不应在第一阶段记录中宣称真实 QQ 登录链路已经可用。
