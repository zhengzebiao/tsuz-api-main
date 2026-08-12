# 角色管理 Role：第一阶段数据层与迁移开发记录

## 1. 开发范围

本阶段根据 [ROLE_MANAGEMENT_IMPLEMENTATION_PLAN.md](ROLE_MANAGEMENT_IMPLEMENTATION_PLAN.md) 和 [ROLE_MANAGEMENT_PHASE_1_EXECUTION_PLAN.md](ROLE_MANAGEMENT_PHASE_1_EXECUTION_PLAN.md) 实施第一阶段“角色数据层与迁移”，只包含以下内容：

1. 扩展现有 `Role` SQLAlchemy 数据模型；
2. 新增 `0004_role_management` 数据库迁移；
3. 安全回填既有角色数据；
4. 补充角色模型和关联兼容性测试；
5. 更新历史验证脚本对新 Alembic head 的断言；
6. 在临时 PostgreSQL 环境中验证升级、降级和再次升级。

本阶段未实现以下内容：

- 角色管理 Pydantic Schema；
- `AdminRoleService` 或用户角色分配 Service；
- `/admin/roles` 或用户角色分配 API；
- 角色管理权限 Seed；
- 禁用角色的登录与鉴权过滤；
- 会话撤销和角色管理审计；
- 用户角色集合替换逻辑；
- 角色权限配置；
- Redis 相关逻辑。

---

## 2. 数据模型实现

修改文件：

```text
app/models/role.py
```

保留原有 `id`、`name` 字段以及 `user_roles`、`role_permissions` 两张关联表，并扩展角色管理所需字段。

### 2.1 字段

| 字段 | 类型 | 可空 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | `Integer` | 否 | 数据库生成 | 内部主键 |
| `name` | `String(64)` | 否 | 无 | 全平台唯一角色名称 |
| `description` | `String(255)` | 否 | 空字符串 | 角色说明 |
| `is_enabled` | `Boolean` | 否 | `true` | 是否启用 |
| `disabled_at` | `DateTime` | 是 | `NULL` | 最近一次禁用时间 |
| `disabled_reason` | `String(500)` | 是 | `NULL` | 禁用原因 |
| `created_at` | `DateTime` | 否 | 当前时间 | 创建时间 |
| `updated_at` | `DateTime` | 否 | 当前时间 | 最近更新时间 |
| `version` | `Integer` | 否 | `1` | 后续乐观锁版本号 |

时间字段继续遵循项目现有约定，使用无时区 `DateTime`。`updated_at` 在 ORM 层配置 `onupdate=func.now()`，为后续角色编辑提供自动更新时间能力。

`description`、`is_enabled`、时间字段和 `version` 同时提供 Python 默认值与数据库默认值，因此以下两种写入方式均能得到一致的默认状态：

- SQLAlchemy ORM 使用 `Role(name=...)` 创建；
- 直接 SQL 只提供 `name` 插入。

### 2.2 索引和约束

保留：

- `ix_roles_id`：角色主键 ID 索引；
- `ix_roles_name`：角色名称唯一索引。

新增：

- `ix_roles_is_enabled`：角色启用状态普通索引。

角色名称的唯一约束未改变，后续角色管理仍采用主应用全局唯一名称。

### 2.3 既有关联表

以下表结构没有修改：

```text
user_roles
- user_id
- role_id

role_permissions
- role_id
- permission_id
```

模型测试和真实 PostgreSQL 迁移验证均确认两类关联可继续使用，迁移往返过程中关联记录不丢失。

---

## 3. Alembic 元数据

[alembic/env.py](../alembic/env.py) 在本阶段开始前已显式导入 `role`：

```python
from app.models import app, audit_event, permission, role, session, user  # noqa: F401
```

因此扩展后的 Role 模型会自动进入 `Base.metadata`，本阶段无需修改 Alembic 模型注册逻辑。真实 PostgreSQL 上执行 `alembic check` 返回：

```text
No new upgrade operations detected.
```

这表明 ORM 元数据与 `0004_role_management` 迁移后的数据库结构一致。

---

## 4. 数据库迁移

新增文件：

```text
alembic/versions/0004_role_management.py
```

迁移关系：

```text
0001_initial_auth_schema
    ↓
0002_user_management
    ↓
0003_app_management
    ↓
0004_role_management
```

### 4.1 `upgrade`

升级按旧数据安全迁移顺序执行：

1. 以临时可空形式添加 `description`、`is_enabled`、`created_at`、`updated_at` 和 `version`，并同时设置最终需要保留的数据库默认值；
2. 添加可空的 `disabled_at` 和 `disabled_reason`；
3. 使用一次显式 `UPDATE ... COALESCE(...)` 回填所有既有角色；
4. 将五个必填字段收紧为 `NOT NULL`；
5. 创建 `ix_roles_is_enabled`。

既有角色回填结果：

```text
description = ''
is_enabled = true
disabled_at = NULL
disabled_reason = NULL
created_at = CURRENT_TIMESTAMP
updated_at = CURRENT_TIMESTAMP
version = 1
```

该迁移不删除、不重建 `roles`，因此原有角色的 `id` 和 `name` 保持不变，外键关联也不会中断。

### 4.2 `downgrade`

降级按逆序执行：

1. 删除 `ix_roles_is_enabled`；
2. 删除 `version`；
3. 删除 `updated_at`；
4. 删除 `created_at`；
5. 删除 `disabled_reason`；
6. 删除 `disabled_at`；
7. 删除 `is_enabled`；
8. 删除 `description`。

降级后 `roles` 恢复为 `0003_app_management` 时的 `id`、`name` 两列，但角色记录、`user_roles` 和 `role_permissions` 数据全部保留。再次升级会为所有仍存在的角色重新执行一致的默认回填。

---

## 5. 数据层测试

新增文件：

```text
tests/test_role_management_models.py
```

共增加 5 个测试，覆盖：

1. 新角色默认描述为空字符串；
2. 新角色默认启用；
3. 禁用时间和禁用原因默认 `NULL`；
4. 创建、更新时间自动生成；
5. 版本默认 `1`；
6. 描述和禁用元数据可以持久化；
7. 角色名称唯一约束继续生效；
8. 字段长度、可空性和状态索引的 ORM 元数据正确；
9. `user_roles` 与 `role_permissions` 关联仍可正常持久化。

测试使用 SQLite 内存数据库，不连接开发 PostgreSQL，也不连接 Redis。

为验证兼容性，定向测试同时包含：

- `tests/test_seed.py`；
- `tests/test_auth_service.py`；
- `tests/test_admin_user_service.py`。

这些测试中的现有 `Role(name=...)` 调用无需修改并全部通过。

---

## 6. 历史验证脚本兼容调整

Alembic head 从 `0003_app_management` 推进为 `0004_role_management` 后，以下文件中的“最终 head revision”断言同步更新：

```text
scripts/validate_phase_4.py
tests/test_phase_4_integration.py
scripts/validate_app_phase_5.py
tests/test_app_phase_5_integration.py
```

调整只涉及最终 `head` 期望值；历史迁移中间节点、用户管理验证、App 管理验证和业务流程均未改变。

---

## 7. 验证结果

### 7.1 定向兼容性测试

执行：

```bash
pdm run pytest \
  tests/test_role_management_models.py \
  tests/test_seed.py \
  tests/test_auth_service.py \
  tests/test_admin_user_service.py \
  -q
```

结果：

```text
27 passed, 1 warning
```

警告来自现有 FastAPI/Starlette TestClient 对 `httpx` 的弃用提示，与本阶段改动无关。

### 7.2 静态检查

执行：

```bash
pdm run ruff check \
  app/models/role.py \
  alembic/versions/0004_role_management.py \
  tests/test_role_management_models.py \
  scripts/validate_phase_4.py \
  scripts/validate_app_phase_5.py \
  tests/test_phase_4_integration.py \
  tests/test_app_phase_5_integration.py

pdm run lint
git diff --check
```

结果：全部通过，无 lint 或空白错误。

### 7.3 完整测试

执行：

```bash
pdm run test
```

结果：

```text
114 passed, 5 skipped, 1 warning
```

5 个跳过项是需要显式环境变量和隔离 PostgreSQL/Redis 的既有集成验证；本阶段单独完成了针对 `0004` 的真实 PostgreSQL 迁移往返。

### 7.4 PostgreSQL 临时环境迁移往返

验证使用无持久化的 PostgreSQL 16 临时容器和随机宿主端口，未连接当前开发数据库：

```text
临时 PostgreSQL
  → upgrade 0003_app_management
  → 插入既有 admin 角色、用户、权限
  → 插入 user_roles 和 role_permissions 关联
  → upgrade 0004_role_management
  → 检查字段、可空性、长度、默认值和索引
  → 检查 admin 回填及两类关联
  → 直接 SQL 插入角色并检查数据库默认值
  → alembic check
  → downgrade 0003_app_management
  → 确认仅新增字段和索引删除
  → 确认角色与关联记录仍在
  → upgrade head
  → 确认全部角色重新回填
  → 确认 current 为 0004_role_management
  → 删除临时容器
```

验证结果：通过。

具体结果：

- `roles` 升级后共有 9 个预期字段；
- 必填字段全部为 `NOT NULL`；
- `disabled_at` 和 `disabled_reason` 可空；
- 名称、描述和禁用原因长度分别为 64、255、500；
- `ix_roles_id`、唯一 `ix_roles_name` 和 `ix_roles_is_enabled` 均存在；
- `description`、`is_enabled`、时间字段、`version` 的数据库默认值存在并生效；
- 既有 `admin` 的主键和名称保持不变；
- 既有 `admin` 正确回填为空描述、启用状态、当前时间和版本 `1`；
- `user_roles` 和 `role_permissions` 记录在升级、降级、再次升级后均保留；
- 降级后 `roles` 仅保留 `id`、`name`；
- 再次升级后所有现存角色重新正确回填；
- `alembic check` 无结构漂移；
- `alembic current` 为 `0004_role_management (head)`；
- 临时 PostgreSQL 容器已确认删除。

---

## 8. 数据库和 Redis 操作约束

### 8.1 开发数据库

本阶段没有在当前开发数据库执行升级、降级、清表或测试数据写入。后续迁移回环仍不得在生产或共享开发数据库执行。

### 8.2 临时数据库

涉及破坏性迁移验证时继续使用：

- 无持久化临时 PostgreSQL 容器；或
- 明确创建并可安全删除的隔离测试数据库。

临时资源必须使用独立连接地址，并在验证结束后确认清理。

### 8.3 Redis

本阶段没有 Redis 变更，也没有连接 Redis。后续角色状态或角色分配涉及 Session 撤销时，应使用临时 Redis 或独立测试命名空间，不得对开发 Redis 执行 `FLUSHDB` 或 `FLUSHALL`。

---

## 9. 本阶段文件清单

| 文件 | 状态 | 说明 |
| --- | --- | --- |
| `app/models/role.py` | 修改 | 增加角色描述、状态、禁用元数据、时间和版本字段 |
| `alembic/versions/0004_role_management.py` | 新增 | 角色字段回填安全、可逆迁移 |
| `tests/test_role_management_models.py` | 新增 | 角色数据层和关联兼容性测试 |
| `scripts/validate_phase_4.py` | 修改 | 最终 Alembic head 更新为 `0004` |
| `tests/test_phase_4_integration.py` | 修改 | 同步历史验证的 head 断言 |
| `scripts/validate_app_phase_5.py` | 修改 | 最终 Alembic head 更新为 `0004` |
| `tests/test_app_phase_5_integration.py` | 修改 | 同步历史验证的 head 断言 |
| `plan/ROLE_MANAGEMENT_PHASE_1_EXECUTION_PLAN.md` | 新增 | 第一阶段实施计划 |
| `plan/ROLE_MANAGEMENT_DATA_LAYER_IMPLEMENTATION.md` | 新增 | 第一阶段实际开发和验证记录 |

工作区中其他已有文档改动未由本阶段修改或清理。

---

## 10. 第一阶段验收结论

[ROLE_MANAGEMENT_IMPLEMENTATION_PLAN.md](ROLE_MANAGEMENT_IMPLEMENTATION_PLAN.md) 第一阶段验收项已满足：

- 既有 `admin` 角色完整保留；
- 新字段的 ORM 和数据库默认值正确；
- ORM 与 Alembic 迁移结构一致；
- `user_roles` 和 `role_permissions` 关联数据不丢失；
- `0003 → 0004 → 0003 → head` 可安全往返；
- 静态检查、定向测试和完整回归测试通过；
- 未提前实现 Schema、Service、API、权限 Seed、鉴权过滤或用户角色分配逻辑。

---

## 11. 下一阶段

下一阶段为“严格 Schema 与领域边界”，预计实现：

1. 新增 `app/schemas/admin_role.py`；
2. 定义角色创建、编辑、禁用、响应和分页 Schema；
3. 扩展用户角色查询及分配 Schema；
4. 实现严格字段限制、文本规范化和重复角色 ID 校验；
5. 增加 Schema 验证测试。

进入下一阶段前应保持本阶段确定的字段长度、状态边界和迁移结构不变。
