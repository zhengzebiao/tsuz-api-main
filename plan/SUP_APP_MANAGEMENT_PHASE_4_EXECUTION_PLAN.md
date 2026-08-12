# Context

根据 [SUB_APP_DEVELOPMENT_PLAN.md](../SUB_APP_DEVELOPMENT_PLAN.md)，现在实施子应用管理 App 的第四阶段“API 与权限”。前三阶段已经完成 `App` 模型和迁移、App ID/Secret 安全工具、严格 Schema 以及 `AdminAppService`；本阶段将这些能力接入 `/admin/apps` 管理 API，并补齐权限 Seed、路由注册、HTTP 事务边界和一次性 Secret 响应的禁止缓存策略。

本阶段不修改数据库结构、不执行 Alembic、不接入 Redis，也不实现删除、应用编码、菜单、OAuth 回调、Token Scope、权限范围、状态监控或完整密钥轮换。真实 PostgreSQL 并发锁、真实认证授权和完整生命周期集成验证留到第五阶段，并继续使用临时隔离基础设施。

# Requirements and constraints

- 实现 App 列表、详情、创建、编辑、禁用、启用和 Secret 重新生成管理路由。
- 分别使用 `app:read`、`app:create`、`app:update`、`app:disable`、`app:enable`、`app:regenerate_secret` 权限。
- 复用现有 `require_permissions()`，未认证返回 401，权限不足返回 403。
- `APP_NOT_FOUND` 映射为 404，`APP_VERSION_CONFLICT` 映射为 409，创建和 Secret 生成失败映射为不泄露内部信息的固定 500 错误。
- Service 只负责 `flush`；写路由在响应 Schema 构造成功后提交，任意领域错误、响应构造错误或其他异常先回滚。
- 创建与 Secret 重新生成响应设置 `Cache-Control: no-store`；所有普通响应不得包含明文 Secret 或 `app_secret_hash`。
- 六项权限通过现有幂等 Seed 流程关联到 `admin` 角色。
- API 测试仅使用隔离 SQLite 和授权替身，不连接开发 PostgreSQL/Redis，不执行迁移或 Redis 清理命令。

# Implementation plan

1. **新增 App 管理 API**
   - 创建 `app/api/admin_apps.py`，提供 `/admin/apps` 下七个管理端点。
   - 复用 `AdminAppService`、App Schema 和 `require_permissions()`，不复制业务、锁、审计或凭证生成逻辑。
   - 增加统一写事务辅助函数：先执行 Service 并构造响应，再提交；所有异常路径先回滚。
   - 增加固定领域错误到 HTTP 状态码的安全映射，并仅为两个一次性 Secret 响应设置 `no-store`。

2. **注册路由与权限**
   - 修改 `app/main.py`，注册 `admin_apps_router`。
   - 修改 `app/seed/__main__.py`，向 `DEFAULT_PERMISSIONS` 增加六项 App 管理权限；复用现有 `ensure_permission()` 和 `ensure_role_permission()` 保持幂等。

3. **增加隔离 API 与 Seed 测试**
   - 新增 `tests/test_admin_apps_api.py`，使用 SQLite `StaticPool`、独立 Session、小型 FastAPI 测试 App 和授权服务替身。
   - 覆盖路由注册、准确权限名、401/403、完整 API 生命周期、安全响应、`no-store`、404/409/422/500、审计和异常回滚。
   - 修改 `tests/test_seed.py`，显式确认六项 App 权限存在且重复 Seed 不产生重复记录或关联。

4. **导出开发记录**
   - 新增 `docs/SUB_APP_API_PERMISSION_IMPLEMENTATION.md`，记录实际 API、权限、事务、错误映射、缓存控制、测试和隔离结果。
   - 明确第五阶段才进行临时 PostgreSQL 的真实行锁并发、真实权限和完整生命周期集成验证。

# Critical files

- `app/api/admin_apps.py` — App 管理路由、事务边界、错误映射和禁止缓存响应头。
- `app/main.py` — 管理路由注册。
- `app/seed/__main__.py` — App 权限定义与 admin 角色关联。
- `tests/test_admin_apps_api.py` — 隔离 API、权限、事务和敏感响应测试。
- `tests/test_seed.py` — App 权限 Seed 幂等验证。
- `docs/SUB_APP_API_PERMISSION_IMPLEMENTATION.md` — 第四阶段开发记录。

# Verification

- `pdm run ruff check app/api/admin_apps.py app/main.py app/seed/__main__.py tests/test_admin_apps_api.py tests/test_seed.py`
- `pdm run pytest tests/test_admin_apps_api.py tests/test_seed.py -q`
- 运行 App 第一至第四阶段相关测试，确认数据层、安全 Schema 和 Service 无回归。
- `pdm run lint`
- `pdm run test`
- `git diff --check`
- 检查 OpenAPI 路径、权限名、错误映射、提交/回滚和 `Cache-Control: no-store`。
- 检查普通响应、审计和异常中不存在明文 Secret 或 `app_secret_hash`。
- 不执行 Alembic，不连接开发 PostgreSQL 或 Redis；第五阶段真实验证必须使用临时 PostgreSQL 和隔离 Redis。
