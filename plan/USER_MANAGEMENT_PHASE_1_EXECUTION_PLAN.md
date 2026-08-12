# 用户管理第一阶段执行计划：模型和迁移

## 背景与范围

根据 `USER_MANAGEMENT_IMPLEMENTATION_PLAN.md` 的第一阶段，为后续认证状态校验、用户管理接口、全量会话撤销和管理员审计建立稳定的数据层。

当前 `users` 只有邮箱、密码哈希和启用状态，`sessions` 缺少撤销元数据，也没有持久化审计表。本阶段只实现模型与 Alembic `0002` 迁移，不提前实现第二至第四阶段的权限依赖、认证流程、管理 API 或批量撤销服务。

## 执行步骤

### 1. 扩展用户模型

修改 `app/models/user.py`：

- 保留现有字段和邮箱唯一索引。
- 增加 `display_name`。
- 增加独立的 `is_blacklisted` 状态。
- 增加禁用时间和原因：`disabled_at`、`disabled_reason`。
- 增加拉黑时间和原因：`blacklisted_at`、`blacklisted_reason`。
- 增加 `password_changed_at`。
- 增加 `created_at`、`updated_at`。
- 增加用于乐观锁的 `version`。
- 显示名称长度为 128，禁用和拉黑原因长度为 500。
- 为 `is_blacklisted=false`、时间戳和 `version=1` 提供与数据库迁移一致的默认值。
- 时间列沿用当前模型和 `0001` 的无时区 UTC `DateTime` 约定，由数据库通过 `CURRENT_TIMESTAMP` 生成默认值。
- 为启用状态和拉黑状态建立组合索引。

### 2. 扩展 Session 模型

修改 `app/models/session.py`：

- 增加可空的 `revoked_at`。
- 增加可空的 `revoked_reason`，最大长度为 64。
- 为 `user_id` 和 `status` 建立组合索引，以支持查询用户的活动 Session。
- 本阶段不实现会话撤销业务逻辑，现有登录、刷新和登出流程继续兼容新增字段。

### 3. 增加最小审计模型

新建 `app/models/audit_event.py`，建立 `AuditEvent` 模型，包含：

- `id`
- `actor_user_id`
- `action`
- `target_type`
- `target_id`
- `result`
- `reason`
- `changes_json`
- `request_id`
- `created_at`

设计约束：

- `actor_user_id` 外键关联 `users.id`，保证操作人引用有效。
- `target_id` 使用整数但不建立用户外键，避免审计记录与目标用户产生级联删除关系。
- `changes_json` 使用 SQLAlchemy `JSON` 类型，只允许保存非敏感字段变更。
- 为操作人、动作、目标、Request ID 和创建时间建立索引。
- 本阶段不增加审计查询 API。

### 4. 创建 Alembic `0002` 迁移

新建 `alembic/versions/0002_user_management.py`：

- Revision ID 为 `0002_user_management`。
- 以 `0001_initial_auth_schema` 为唯一父修订。
- 向 `users` 添加用户管理字段和组合索引。
- 向 `sessions` 添加撤销元数据和组合索引。
- 创建 `audit_events` 表、外键和索引。
- ORM 模型与迁移声明相同的索引，避免后续 Alembic 自动生成结构漂移。

已有用户数据的迁移流程：

1. 先以可空形式增加新的必填字段。
2. 回填 `is_blacklisted=false`。
3. 将 `created_at` 和 `updated_at` 回填为迁移执行时间。
4. 将 `version` 回填为 `1`。
5. 回填完成后，将这些字段收紧为非空。
6. 保留数据库侧默认值，使绕过 ORM 的合法插入也符合模型不变量。

`0001` 已保证 `users.is_active`、`sessions.status` 和 `sessions.created_at` 非空，因此不重复改写已有 Session 数据。

`downgrade()` 按依赖逆序执行：

1. 删除审计索引和 `audit_events` 表。
2. 删除 Session 新索引和字段。
3. 删除用户新索引和字段。
4. 保留 `0001` 原有结构和数据。

### 5. 注册 Alembic 模型元数据

修改 `alembic/env.py`：

- 延续当前显式导入模型模块的方式，加入 `audit_event`。
- 确保 `Base.metadata` 包含审计表。
- 不改变空的 `app/models/__init__.py` 公共接口。

### 6. 补充数据层测试

新建 `tests/test_user_management_models.py`：

- 沿用项目现有 SQLite 内存数据库测试模式。
- 验证用户管理字段及其默认值。
- 验证可空的禁用和拉黑元数据。
- 验证 Session 撤销时间和原因能够持久化。
- 验证审计事件的 JSON 变更、操作人、目标和 Request ID 能够持久化读取。
- 不包含第二阶段的拉黑登录校验和权限 Seed 测试。
- 不包含第三阶段的管理 API 测试。

## 验证步骤

### 静态检查与自动化测试

```bash
pdm run lint
pdm run test
```

验证目标：

- 模型、迁移和测试符合 Ruff 规则。
- 现有认证行为没有因新增非空字段产生回归。
- 新增数据层测试全部通过。

### PostgreSQL 真实迁移验证

在当前 PostgreSQL 服务器中创建独立的临时测试数据库，不直接修改项目正在使用的数据库：

1. 在临时数据库执行 `alembic upgrade 0001_initial_auth_schema`。
2. 按 `0001` 旧结构插入用户和 Session 数据。
3. 执行 `alembic upgrade 0002_user_management`。
4. 检查旧数据仍然存在。
5. 检查用户的 `is_blacklisted`、`created_at`、`updated_at` 和 `version` 已正确回填。
6. 检查 Session 新字段、`audit_events` 表和新增索引存在。
7. 执行 `alembic check`，确认 ORM 元数据和迁移后的数据库结构一致。
8. 执行 `alembic downgrade 0001_initial_auth_schema`。
9. 再次执行 `alembic upgrade head`，验证迁移能够回滚并重新应用。
10. 使用 `alembic current` 确认最终版本为 `0002_user_management (head)`。
11. 验证结束后删除临时数据库。

不会在未隔离的开发数据库或生产数据库上执行降级验证。

## 验证结论

第一阶段已于 2026-08-11 完成并通过验证。

### 静态检查与自动化测试结论

- `pdm run lint`：通过，Ruff 未发现问题。
- `pdm run test`：通过，共收集并通过 46 项测试。
- 新增的 `tests/test_user_management_models.py` 中 3 项数据层测试全部通过。
- 原有认证、Refresh Token、Redis 状态、日志、健康检查和本地初始化测试全部通过，未发现由模型扩展引起的行为回归。
- 测试仅保留 1 条来自 FastAPI/Starlette `TestClient` 依赖的弃用警告，与本阶段改动无关。

### PostgreSQL 真实迁移结论

验证使用当前运行中的 PostgreSQL 16 容器，但在其中创建独立临时数据库；没有在项目现有的 `test_auth` 数据库上执行 `0002`，也没有修改其数据或 Alembic 版本。

验证结果如下：

- `0001_initial_auth_schema → 0002_user_management` 升级成功。
- 按 `0001` 结构预先插入的旧用户和旧 Session 在迁移后仍然存在。
- 旧用户正确回填：
  - `is_blacklisted=false`
  - `created_at` 非空
  - `updated_at` 非空
  - `version=1`
- 旧 Session 保持 `status=active`，新增的 `revoked_at` 和 `revoked_reason` 均为 `NULL`。
- `audit_events` 表创建成功。
- 用户状态、活动 Session 和审计查询所需的新增索引均创建成功。
- `alembic check` 返回 `No new upgrade operations detected`，说明 ORM 元数据与迁移后的 PostgreSQL 结构一致。
- `0002_user_management → 0001_initial_auth_schema` 降级成功，新增表、字段和索引被正确移除，`0001` 原有用户数据保留。
- 从 `0001_initial_auth_schema` 再次升级至 `head` 成功。
- `alembic current` 最终输出 `0002_user_management (head)`。
- 离线完整迁移 SQL 生成成功。
- 所有临时验证数据库均已删除，没有残留测试数据库。

### 最终结论

第一阶段的用户、Session 和审计模型与 `0002_user_management` 迁移满足执行计划要求；迁移能够在真实 PostgreSQL 上保留旧数据、完成字段回填、往返升级和降级，并与 SQLAlchemy ORM 元数据保持一致。当前实现可以作为第二阶段“认证与权限基础”的数据层前置条件。

## 本阶段不包含

- 管理 API 权限依赖。
- 用户管理权限 Seed。
- 登录和 Refresh 流程的拉黑检查。
- 用户管理接口。
- 用户全部 Session 撤销服务。
- Redis Session 批量撤销。
- 审计记录查询接口。
- 第二至第四阶段的其他功能。
