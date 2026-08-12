# 用户管理第二阶段执行计划：认证与权限基础

## 背景与范围

根据 `USER_MANAGEMENT_IMPLEMENTATION_PLAN.md` 的第二阶段，在第一阶段的用户状态、Session 撤销元数据和审计模型基础上，建立后续管理 API 共用的认证与权限边界。

本阶段只实现：

- 管理 API 通用权限依赖。
- 管理员细粒度用户权限 Seed。
- 登录、Refresh 和 `/auth/me` 的拉黑状态检查。
- SessionService 的用户全部 Session 撤销能力。

本阶段不实现 `/admin/users` 路由、用户管理请求与响应 Schema、用户管理业务操作或审计查询接口。

## 执行内容

### 1. 统一认证状态与权限校验

新增 `app/services/authorization_service.py`：

- 用户只有在 `is_active=true` 且 `is_blacklisted=false` 时可以认证。
- 验证 Access Token 的签名及 `sub`、`jti`、`sid` 必需声明。
- 检查 JTI 黑名单。
- 检查 Redis 和 PostgreSQL Session 状态。
- 检查当前用户的启用和拉黑状态。
- 从空格分隔的 Token `scope` 中校验一个或多个必需权限。
- 区分认证失败与权限不足，供 HTTP 层分别返回 401 和 403。

### 2. 增加 FastAPI 权限依赖

新增 `app/api/dependencies.py`：

- 提供共享的 `HTTPBearer(auto_error=False)`。
- 提供 `require_access_token()`。
- 提供 `require_permissions(*permissions)` 依赖工厂。
- 认证失败统一返回 `401 invalid access token`。
- Token 有效但 scope 不足返回 `403 insufficient permissions`。
- 权限校验成功后返回当前 `User`，供后续管理接口取得操作人 ID。

`app/api/auth.py` 复用共享 Bearer 和 Access Token 提取逻辑，避免重复维护认证头处理。

### 3. 同步认证流程的用户状态检查

修改 `app/services/auth_service.py`：

- 登录拒绝被禁用或拉黑用户，对外继续返回通用无效凭据错误。
- 密码校验成功后锁定用户行并再次检查认证状态，再创建 Session，避免与禁用或拉黑并发时创建新的活动 Session。
- Refresh Token 轮换后重新检查用户和数据库 Session 状态；状态失效时撤销当前 Session，不签发新的 Access Token。
- `/auth/me` 同时检查 JTI、Redis/数据库 Session、启用状态和拉黑状态。
- 登出和 Refresh Token 重放撤销 Session 时同步写入数据库撤销时间及原因。

### 4. 扩展 SessionService

修改 `app/services/session_service.py` 和 `app/services/refresh_token_service.py`：

- `SessionService` 可接收数据库 Session，同时保留 Redis-only 用法。
- 单 Session 撤销同步更新 PostgreSQL 和 Redis。
- 新增 `revoke_user_sessions(user_id, reason) -> int`：锁定并撤销用户全部活动 Session，返回实际撤销数量。
- PostgreSQL 写入 `status=revoked`、`revoked_at` 和 `revoked_reason`。
- Redis Session 撤销标记设置 TTL，覆盖 Refresh Token 的最大有效期。
- Session 活动检查在有数据库连接时同时检查 Redis 和 PostgreSQL。

### 5. 更新管理员权限 Seed

修改 `app/seed/__main__.py`，管理员角色幂等补齐：

- `user:read`
- `user:create`
- `user:update`
- `user:disable`
- `user:enable`
- `user:blacklist`
- `user:recover`
- `user:reset_password`
- `user:force_logout`

保留原有 `user:write`，避免破坏已有 RBAC 和 Token scope 兼容性。

### 6. 自动化测试

新增或扩展测试，覆盖：

- 权限依赖的成功、401 和 403 分支。
- Access Token 必需声明、JTI、Session、用户状态和 scope 校验。
- 登录、Refresh 和 `/auth/me` 拒绝被拉黑用户。
- 用户全部 Session 撤销的数据库状态、时间、原因、Redis TTL 和幂等返回值。
- 管理员权限 Seed 的完整集合和重复执行幂等性。
- 原有认证、Refresh Token、Redis 和日志行为回归。

## 验证结论

第二阶段已于 2026-08-11 完成并通过自动化验证：

- `pdm run lint`：通过，Ruff 未发现问题。
- `pdm run test`：通过，共收集并通过 64 项测试。
- 管理权限依赖能正确区分 401 认证失败和 403 权限不足。
- 登录、Refresh、`/auth/me` 均会拒绝被拉黑用户。
- 单 Session 和用户全部 Session 撤销会同步更新 PostgreSQL 与 Redis，并设置 Redis TTL。
- Session 批量撤销重复执行返回 0，不重复修改已撤销 Session。
- 管理员角色 Seed 能幂等补齐全部用户管理权限，并保留原 `user:write` 权限。
- 原有认证、Refresh Token、Redis 状态、日志、健康检查、本地初始化和第一阶段模型测试全部通过。
- 测试仅保留 1 条来自 FastAPI/Starlette `TestClient` 依赖的弃用警告，与本阶段改动无关。

## 最终结论

第二阶段要求的认证与权限基础已具备，可作为第三阶段用户管理接口的权限校验、当前管理员识别和全部 Session 撤销基础。本阶段没有新增数据库迁移，也没有提前实现第三阶段管理 API。
