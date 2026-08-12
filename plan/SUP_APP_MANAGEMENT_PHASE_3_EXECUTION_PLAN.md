# Context

根据 [SUB_APP_DEVELOPMENT_PLAN.md](../SUB_APP_DEVELOPMENT_PLAN.md)，现在实施子应用管理 App 的第三阶段“业务服务”。第一阶段已经完成 `App` 数据模型和 `apps` 表迁移，第二阶段已经完成 App ID/Secret 安全工具与严格 Schema；本阶段新增 `AdminAppService`，实现查询、创建、乐观锁编辑、行锁状态变更、Secret 重新生成和安全审计。

本阶段不实现 App 管理 API、路由注册、权限 Seed、数据库迁移、Redis 逻辑或 HTTP 缓存响应头。Service 在调用方事务中修改数据并写入审计，只执行 `flush`，不自行 `commit`；第四阶段 API 负责成功提交和异常回滚。

# Requirements and constraints

- 列表支持分页、名称/App ID 模糊搜索、启用状态筛选和 `created_at DESC, id DESC` 稳定排序。
- 创建时由后端生成 App ID 和 App Secret，数据库只保存 Secret 的 SHA-256 Hash；明文仅作为创建方法返回值在内存中传递。
- App ID 唯一冲突在 savepoint 中有限重试；领域异常不泄露数据库细节、明文 Secret 或 Secret Hash。
- 编辑只接受 Schema 允许的基本资料并使用 `id + version` 乐观锁；无真实变化时不递增版本，也不产生审计。
- 启用、禁用和 Secret 重新生成使用 `SELECT ... FOR UPDATE` 行锁；重复状态操作保持幂等。
- Secret 重新生成后旧 Secret 立即无法匹配，新 Secret 只返回一次；审计只记录 `secret_changed=true` 和原因。
- 有效变更与对应 `AuditEvent` 必须位于同一调用方事务；失败时可整体回滚。
- 测试数据库必须是测试框架创建并销毁的 SQLite 内存数据库；不连接开发 PostgreSQL 或开发 Redis。

# Implementation plan

1. **新增 App Service**
   - 创建 `app/services/admin_app_service.py`。
   - 定义 `AdminAppService` 及固定错误码领域异常：`APP_NOT_FOUND`、`APP_VERSION_CONFLICT`、`APP_CREATION_FAILED`、`APP_SECRET_GENERATION_FAILED`。
   - 复用 `generate_app_id()`、`generate_app_secret()`、`hash_app_secret()` 和 `verify_app_secret()`。

2. **实现查询和创建**
   - `list_apps()` 组合分页、关键词、状态筛选和稳定排序，并返回记录及总数。
   - `get_app()` 按内部主键查询，不存在时抛出 `APP_NOT_FOUND`。
   - `create_app()` 生成凭证、只持久化 Hash，在 nested transaction/savepoint 中处理 App ID 冲突重试，并添加不含凭证的 `app.created` 审计。

3. **实现并发安全修改**
   - `update_app()` 先检查版本和真实变化，再通过带主键与版本条件的 SQL UPDATE 原子递增版本；冲突时抛出 `APP_VERSION_CONFLICT`。
   - `_lock_app()` 使用 `SELECT ... FOR UPDATE`；`disable_app()` 和 `enable_app()` 在行锁内幂等修改状态、禁用元数据、更新时间与版本。
   - `regenerate_secret()` 在行锁内生成与旧凭证不同的新 Secret，覆盖 Hash并更新时间与版本。

4. **接入安全审计**
   - 创建、编辑、有效状态变化和 Secret 重新生成使用 `target_type="app"` 写入现有 `AuditEvent`。
   - 编辑和状态审计只记录非敏感字段变化；Secret 审计只保存 `{"secret_changed": true}`，原因写入 `reason`。
   - request ID 优先使用显式参数，其次使用 `request_id_context`，最后使用 `unknown`。
   - Service 仅 `flush`，不 `commit`，保证调用方可以原子提交或整体回滚。

5. **增加隔离单元测试与开发记录**
   - 新增 `tests/test_admin_app_service.py`，使用每个测试独立的 SQLite 内存数据库。
   - 覆盖查询、创建、冲突重试、乐观锁、状态幂等、`FOR UPDATE` 语句、Secret 轮换、敏感信息排除和事务回滚。
   - 新增 `docs/SUB_APP_BUSINESS_SERVICE_IMPLEMENTATION.md`，记录实际实现、验证结果和数据库/Redis 隔离约束。

# Critical files

- `app/services/admin_app_service.py` — App 查询、创建、编辑、状态、Secret 和审计业务逻辑。
- `tests/test_admin_app_service.py` — 第三阶段隔离 Service 测试。
- `docs/SUB_APP_BUSINESS_SERVICE_IMPLEMENTATION.md` — 第三阶段开发记录。
- `app/models/app.py` — 既有 App 数据模型，本阶段只复用。
- `app/models/audit_event.py` — 既有审计模型，本阶段只复用。
- `app/core/security.py` — 既有 App 凭证安全工具，本阶段只复用。
- `app/schemas/admin_app.py` — 既有创建和编辑输入 Schema，本阶段只复用。

# Verification

- `pdm run ruff check app/services/admin_app_service.py tests/test_admin_app_service.py`
- `pdm run pytest tests/test_admin_app_service.py -q`
- `pdm run pytest tests/test_app_management_models.py tests/test_app_management_security_schema.py tests/test_admin_app_service.py -q`
- `pdm run lint`
- `pdm run test`
- 检查锁查询包含 `FOR UPDATE`，编辑 SQL 同时限定主键和版本。
- 检查审计、异常和普通返回值中没有明文 Secret 或 Secret Hash。
- 不执行 Alembic，不连接开发 PostgreSQL，不连接 Redis；真实 PostgreSQL 并发锁测试留到第五阶段临时环境验证。
- 检查最终 Git diff，确认没有提前实现第四阶段 API、权限或响应缓存逻辑。
