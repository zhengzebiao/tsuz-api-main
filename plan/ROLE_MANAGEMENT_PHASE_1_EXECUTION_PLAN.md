# Context

根据 [ROLE_MANAGEMENT_IMPLEMENTATION_PLAN.md](ROLE_MANAGEMENT_IMPLEMENTATION_PLAN.md) 仅实施角色管理第一阶段“角色数据层与迁移”。当前 [role.py](../app/models/role.py) 只有 `id`、`name` 以及既有的 `user_roles`、`role_permissions` 关联表；Alembic 迁移链当前到 `0003_app_management`。本阶段需要在不重建 `roles` 表、不改变关联表、不影响现有 `Role(name=...)` 调用的前提下，为后续角色管理补齐描述、启用状态、禁用元数据、时间戳和乐观锁版本，并验证既有 `admin` 角色及其用户/权限关联在迁移往返中完整保留。

本阶段严格不实现 Schema、Service、API、权限 Seed、鉴权过滤或用户角色分配逻辑。

## Requirements and constraints

- 继续使用主应用全局 `roles` 表，不增加 `app_id`，角色名称继续保持全平台唯一。
- 新增字段：`description`、`is_enabled`、`disabled_at`、`disabled_reason`、`created_at`、`updated_at`、`version`。
- `description` 默认空字符串，`is_enabled` 默认 `true`，`version` 默认 `1`；创建和更新时间默认数据库当前时间。
- 仅 `disabled_at` 和 `disabled_reason` 可空；其余新增字段迁移完成后必须为 `NOT NULL`。
- 新增 `ix_roles_is_enabled` 普通索引，保留既有 `ix_roles_id` 和唯一 `ix_roles_name`。
- 迁移必须采用“先加可空列、回填旧数据、再收紧非空”的顺序，不得删除或重建 `roles`。
- `user_roles` 和 `role_permissions` 的结构与数据均不得改变。
- 时间字段沿用项目现有无时区 `DateTime` 约定，不为本模块单独引入新的时间类型。
- 迁移降级验证只能使用独立临时 PostgreSQL，不能在生产或共享开发数据库执行。
- 保留当前工作区已有、与本阶段无关的文档修改，不覆盖或清理它们。

## Implementation plan

1. **扩展 Role ORM 模型**
   - 修改 [app/models/role.py](../app/models/role.py)，保留两张关联表及 `id`、`name` 定义。
   - 参考 [app.py](../app/models/app.py) 和 [user.py](../app/models/user.py) 的 SQLAlchemy 2.x `Mapped`/`mapped_column` 风格，增加描述、状态、禁用元数据、创建/更新时间和版本字段。
   - 为 `description`、`is_enabled`、时间字段和 `version` 同时提供符合设计的 Python 默认值及数据库默认值，确保 ORM 建表与 Alembic 迁移后的真实数据库行为一致。
   - 使用 `onupdate=func.now()` 维护 ORM 更新时的 `updated_at`，并用 `__table_args__` 声明 `ix_roles_is_enabled`。
   - [alembic/env.py](../alembic/env.py) 已显式导入 `role`，无需额外注册模型。

2. **创建 `0004_role_management` 迁移**
   - 新增 [0004_role_management.py](../alembic/versions/0004_role_management.py)，`down_revision` 指向 `0003_app_management`。
   - `upgrade()` 先添加带最终 server default 的临时可空必填列和两个可空禁用列，再使用显式 `UPDATE ... COALESCE(...)` 回填所有既有角色，最后将必填列收紧为 `NOT NULL` 并创建状态索引。
   - `downgrade()` 按逆序删除状态索引和七个新增字段，只恢复 `roles(id, name)`，不删除角色记录或任何关联表。

3. **补充角色模型测试**
   - 新增 [test_role_management_models.py](../tests/test_role_management_models.py)，沿用现有 SQLite 内存数据库 fixture。
   - 验证仅传 `name` 时的全部默认值、禁用字段持久化、角色名称唯一约束、字段长度/可空性/索引元数据，以及既有用户和权限关联仍可正常持久化。
   - 运行现有 Seed、认证与管理员用户服务测试，确认 `Role(name=...)` 的已有用法无回归。

4. **更新历史验证对新 Alembic head 的断言**
   - 更新 [validate_phase_4.py](../scripts/validate_phase_4.py)、[test_phase_4_integration.py](../tests/test_phase_4_integration.py)、[validate_app_phase_5.py](../scripts/validate_app_phase_5.py) 和 [test_app_phase_5_integration.py](../tests/test_app_phase_5_integration.py) 中最终 head revision 的固定值。
   - 保留这些历史脚本的迁移中间节点和业务验证内容，只将最终 `head` 期望值改为 `0004_role_management`。

5. **验证并导出执行记录**
   - 运行定向测试、全量 lint 和完整测试套件。
   - 使用无持久化临时 PostgreSQL 容器执行 `0003 → 0004 → 0003 → head` 往返，检查字段、默认值、索引、`alembic check`、旧角色及关联数据。
   - 完成后新增 [ROLE_MANAGEMENT_DATA_LAYER_IMPLEMENTATION.md](ROLE_MANAGEMENT_DATA_LAYER_IMPLEMENTATION.md)，准确记录实际改动、命令、结果和临时资源清理状态。

## Critical files

- [app/models/role.py](../app/models/role.py) — 扩展 Role ORM 字段及状态索引。
- [alembic/versions/0004_role_management.py](../alembic/versions/0004_role_management.py) — 新增旧数据回填安全、可逆的迁移。
- [tests/test_role_management_models.py](../tests/test_role_management_models.py) — 新增第一阶段数据层测试。
- [scripts/validate_phase_4.py](../scripts/validate_phase_4.py)、[scripts/validate_app_phase_5.py](../scripts/validate_app_phase_5.py) — 兼容新的迁移 head。
- [alembic/versions/0001_initial_auth_schema.py](../alembic/versions/0001_initial_auth_schema.py)、[alembic/versions/0002_user_management.py](../alembic/versions/0002_user_management.py)、[alembic/versions/0003_app_management.py](../alembic/versions/0003_app_management.py) — 仅作为迁移风格和往返基线，不修改。

## Verification

- 定向测试：

  ```bash
  pdm run pytest tests/test_role_management_models.py tests/test_seed.py tests/test_auth_service.py tests/test_admin_user_service.py -q
  ```

- 静态检查与完整回归：

  ```bash
  pdm run lint
  pdm run test
  git diff --check
  ```

- 临时 PostgreSQL 迁移往返：

  ```text
  upgrade 0003_app_management
    → 插入 admin、用户、权限和两类关联
    → upgrade 0004_role_management
    → 检查回填、字段、默认值、索引和关联
    → alembic check
    → downgrade 0003_app_management
    → 确认仅新增字段/索引删除，角色与关联保留
    → upgrade head
    → 确认重新回填且 current 为 0004_role_management
    → 删除临时容器
  ```

- 最终范围检查：确认未新增角色 Schema、Service、API、权限 Seed、鉴权过滤或用户角色分配逻辑。
