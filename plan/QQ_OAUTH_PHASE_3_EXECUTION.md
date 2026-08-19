# QQ OAuth 第三阶段执行记录：认证路由、响应模型与兼容调整

## 执行范围

本阶段完成认证路由、认证响应模型、`/auth/me` 扩展和 QQ-only 用户兼容调整。测试使用 fake 服务、FakeRedis、隔离 SQLite 或项目现有测试夹具；未执行真实 QQ 联调、生产数据库/Redis 操作、部署或 push。

未读取或修改运行时 `.env`，执行记录不包含 secret、APP_KEY、state、ticket、openid、QQ access token 或本系统 token。

## 实际变更

- `app/schemas/auth.py`
  - 新增严格的 `QQTicketExchangeRequest`，ticket 长度限制为 1—256，并拒绝额外字段。
  - 新增安全的 `UserIdentityResponse`，仅返回 provider、展示名、头像和 verified。
  - 扩展 `UserResponse`，保留 id、username、roles，并增加 permissions、display_name、avatar、identities；QQ-only username 可为 null。
- `app/schemas/admin_user.py`
  - 只读 `AdminUserResponse.email` 改为可空；创建和更新请求的邮箱约束未放宽。
- `app/api/auth.py`
  - 新增 `GET /auth/qq/login`、`GET /auth/qq/callback` 和 `POST /auth/qq/exchange`。
  - callback 只使用配置中的固定消费端地址，成功追加 ticket，失败追加稳定的 `qq_error=oauth_failed`。
  - exchange 先消费 ticket，再调用 `AuthService.complete_login()`；无效 ticket/用户状态映射 401，基础设施或持久化失败映射 503。
  - callback ticket 签发失败也映射为稳定错误回跳。
- `app/services/auth_service.py`
  - 空密码哈希按无效邮箱凭据处理，不调用密码校验函数传入 None。
  - `current_user()` 返回有效权限、安全身份列表、展示名和头像，并保持既有令牌、Session、黑名单和用户状态校验。
- `app/services/admin_user_service.py` 和 `app/api/admin_users.py`
  - QQ-only 用户密码重置返回 `PASSWORD_RESET_UNAVAILABLE`，HTTP 状态为 409；拒绝前不生成密码哈希，也不改变版本、Session 或审计记录。
- 测试
  - 新增 `tests/test_qq_auth_api.py`。
  - 扩展认证服务和管理员服务/API 回归覆盖，包括 QQ-only `/auth/me`、空密码登录、可空 email 序列化、密码重置拒绝和固定回跳安全性。

## 实际验证结果

### Focused pytest

命令：

```text
pdm run pytest tests/test_qq_auth_api.py tests/test_auth_api.py tests/test_auth_service.py tests/test_admin_user_service.py tests/test_admin_users_api.py tests/test_admin_roles_api.py tests/test_admin_role_service.py
```

结果：

```text
102 passed, 1 warning in 17.81s
```

警告为现有 Starlette/httpx TestClient deprecation warning，未由本阶段引入。

### Full pytest

命令：

```text
pdm run pytest
```

结果：

```text
347 passed, 16 skipped, 1 warning in 22.38s
```

警告同上。

### Focused Ruff

命令：

```text
pdm run ruff check app/api/auth.py app/schemas/auth.py app/schemas/admin_user.py app/services/auth_service.py app/services/admin_user_service.py tests/test_qq_auth_api.py tests/test_auth_service.py tests/test_admin_user_service.py tests/test_admin_users_api.py
```

结果：

```text
All checks passed!
```

### Repository lint

命令：

```text
pdm run lint
```

结果：失败。仓库级检查仍报告既有的 Alembic 迁移 import 排序问题，以及多个未纳入本阶段范围的 admin API FastAPI dependency B008 问题。未修改这些无关历史 lint 问题。本阶段涉及的 focused Ruff 检查已通过。

### 依赖和 diff 检查

命令：

```text
pdm lock --check
git diff --check
```

结果：均通过，无输出。

## 范围外事项

- 未进行真实 QQ provider 请求或端到端扫码联调。
- 未操作生产数据库、Redis、部署环境或运行时 `.env`。
- 未提交或 push 代码。
- 未实现 QQ 与邮箱绑定/解绑、邮箱补充、Cookie/CSRF 改造、动态回跳或 `/auth/qq/url`。
