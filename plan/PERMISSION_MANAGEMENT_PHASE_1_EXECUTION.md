# 权限管理 Permission：第一阶段开发执行记录

## 1. 开发范围

本阶段根据 [PERMISSION_SYNC_DESIGN.md](../docs/PERMISSION_SYNC_DESIGN.md) 和 [PERMISSION_MANAGEMENT_PHASE_1_IMPLEMENTATION_PLAN.md](PERMISSION_MANAGEMENT_PHASE_1_IMPLEMENTATION_PLAN.md) 完成“权限数据层与迁移”：

1. 扩展 `Permission` SQLAlchemy 模型；
2. 新增 `PermissionEndpoint` API 绑定模型；
3. 新增 `0005_permission_management` Alembic 迁移；
4. 将新模型注册到 Alembic 元数据；
5. 新增模型、约束和关联测试；
6. 新增默认跳过、显式启用的真实 PostgreSQL 迁移往返测试；
7. 执行定向、lint、完整回归及真实迁移验证。

本阶段未实现：

- `require_permissions()` 元数据和声明校验；
- FastAPI 路由扫描器；
- 权限同步服务和命令；
- PostgreSQL advisory lock 同步并发控制；
- 权限管理 Schema、Service 和 API；
- Seed 与本地初始化流程调整；
- `AuthService` Scope 状态过滤；
- `AuthorizationService` 数据库实时状态检查；
- 权限状态变化后的 Session 撤销或管理员审计。

---

## 2. Permission 数据模型

修改：

```text
app/models/permission.py
```

保留：

- `id: Integer` 自增主键；
- `name: String(128)` 唯一权限编码；
- `description: String(255)` 权限说明。

新增字段：

| 字段 | 类型 | 可空 | ORM/数据库默认值 |
| --- | --- | --- | --- |
| `display_name` | `String(128)` | 否 | 空字符串 |
| `is_declared` | `Boolean` | 否 | `true` |
| `is_enabled` | `Boolean` | 否 | `true` |
| `disabled_at` | `DateTime` | 是 | `NULL` |
| `disabled_reason` | `String(500)` | 是 | `NULL` |
| `missing_at` | `DateTime` | 是 | `NULL` |
| `created_at` | `DateTime` | 否 | 当前时间 |
| `updated_at` | `DateTime` | 否 | 当前时间，ORM 更新时使用 `onupdate=func.now()` |
| `version` | `Integer` | 否 | `1` |

新增普通索引：

```text
ix_permissions_is_declared
ix_permissions_is_enabled
```

保留唯一 `ix_permissions_name` 和现有 ID 索引。模型没有引入 `code` 或 `app_id`，也没有修改现有权限主键语义。

`display_name` 在 ORM 和数据库直接创建的新记录上默认空字符串。后续第三阶段同步新权限时会显式设置 `display_name=name`；本阶段迁移则把所有历史权限回填为其现有 `name`。

---

## 3. API 绑定模型

新增：

```text
app/models/permission_endpoint.py
```

模型对应 `permission_endpoints`：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `permission_id` | `Integer` | 复合主键；外键到 `permissions.id`；`ON DELETE CASCADE` |
| `http_method` | `String(16)` | 复合主键 |
| `path` | `String(2048)` | 复合主键 |
| `route_name` | `String(255)` | 非空 |

复合主键：

```text
(permission_id, http_method, path)
```

这使同一权限、方法和路径只能保存一次，同时允许：

- 一个权限绑定多个 API；
- 一个 API 绑定多个权限；
- 同一路径的不同 HTTP Method 分别保存。

新增非唯一索引：

```text
ix_permission_endpoints_http_method_path(http_method, path)
```

本阶段只建立绑定快照结构，不写入任何实际路由绑定。

---

## 4. Alembic 元数据

修改：

```text
alembic/env.py
```

在显式模型导入中增加 `permission_endpoint`，确保 `Base.metadata` 可以发现新表并由 `alembic check` 检测 ORM 与迁移结构差异。

空的 `app/models/__init__.py` 保持不变。

---

## 5. 0005 数据库迁移

新增：

```text
alembic/versions/0005_permission_management.py
```

迁移链：

```text
0004_role_management
    ↓
0005_permission_management
```

### 5.1 upgrade

实际升级顺序：

1. 以临时可空形式增加 `display_name`、状态、时间和版本等字段，并设置最终保留的数据库默认值；
2. 增加本来就允许为空的禁用、缺失字段；
3. 通过显式 `UPDATE` 回填历史权限；
4. 将六个必填新增字段收紧为 `NOT NULL`；
5. 创建声明状态和启用状态索引；
6. 创建 `permission_endpoints` 及其复合主键、级联外键和方法/路径索引。

历史权限回填结果：

```text
display_name = name
is_declared = true
is_enabled = true
disabled_at = NULL
disabled_reason = NULL
missing_at = NULL
created_at = CURRENT_TIMESTAMP
updated_at = CURRENT_TIMESTAMP
version = 1
```

迁移不更新 `id`、`name` 和 `description`，也不删除或重建 `permissions`。

### 5.2 downgrade

实际降级顺序：

1. 删除绑定方法/路径索引；
2. 删除 `permission_endpoints`；
3. 删除两个 Permission 状态索引；
4. 按逆序删除新增 Permission 字段。

降级后 `permissions` 恢复为：

```text
id
name
description
```

权限记录、角色记录及 `role_permissions` 关联继续保留。

---

## 6. 历史验证脚本的 Alembic head 兼容调整

`0005_permission_management` 成为新的 Alembic head 后，更新以下既有真实验证脚本及 opt-in 测试的最终 revision 断言：

```text
scripts/validate_phase_4.py
tests/test_phase_4_integration.py
scripts/validate_app_phase_5.py
tests/test_app_phase_5_integration.py
scripts/validate_role_management_phase_5.py
tests/test_role_management_phase_5_integration.py
```

调整范围仅限它们执行 `upgrade head` 后的 `alembic current` 期望值，从 `0004_role_management` 更新为 `0005_permission_management`。用户、App 和角色验证自身使用的历史迁移中间节点、并发场景、HTTP/JWT/Redis 流程没有改变。

---

## 7. 测试实现

### 7.1 模型和约束测试

新增：

```text
tests/test_permission_management_models.py
```

共 9 个测试，覆盖：

1. Permission 新字段默认值；
2. 整数自增 ID 连续生成；
3. 展示信息、状态、禁用/缺失元数据和版本可持久化；
4. `name` 唯一约束保持生效；
5. Permission 字段长度、可空性和状态索引元数据；
6. PermissionEndpoint 复合主键、级联外键和索引元数据；
7. 重复 `(permission_id, http_method, path)` 被拒绝；
8. 不同权限或不同 HTTP Method 可以保存同一路径；
9. 既有 `role_permissions` 关联仍可正常持久化。

测试使用 SQLite 内存数据库，不连接开发 PostgreSQL 或 Redis。

### 7.2 真实 PostgreSQL 迁移往返测试

新增：

```text
tests/test_permission_management_migration.py
```

默认测试套件中该测试跳过，只有显式设置：

```text
RUN_PERMISSION_MANAGEMENT_PHASE_1_MIGRATION=1
```

才会运行。验证器默认：

- 只接受 PostgreSQL；
- 只允许 localhost、`127.0.0.1` 或 `::1`；
- 拒绝默认端口 5432；
- 如需使用默认端口或远程隔离环境，必须显式允许；
- 创建随机 `tsuz_permission_phase1_migration_*` 临时数据库；
- 结束时只终止该临时数据库连接并删除该数据库；
- Alembic 失败输出会替换数据库 URL 和密码。

真实测试覆盖：

- `0004 → 0005 → 0004 → 0005`；
- 历史 Permission ID、name、description 保留；
- 历史 `display_name=name` 回填；
- 新状态、时间和版本默认值；
- Permission 字段可空性和四个索引；
- PermissionEndpoint 列、复合主键、级联外键和索引；
- 重复绑定约束；
- 新权限整数自增 ID；
- `role_permissions` 在升级、降级、再次升级后保留；
- 两次 `alembic check` clean；
- 最终 revision 为 `0005_permission_management`。

本次实际运行结束后测试创建的随机数据库由 fixture 清理，没有保留测试数据。

---

## 8. 验证结果

### 8.1 初始数据层测试

执行：

```bash
pdm run test -- \
  tests/test_permission_management_models.py \
  tests/test_permission_management_migration.py
```

结果：

```text
9 passed, 1 skipped, 1 warning
```

迁移测试按设计在没有显式环境变量时跳过。

### 8.2 权限相关定向回归

最终执行：

```bash
pdm run test -- \
  tests/test_permission_management_models.py \
  tests/test_auth_service.py \
  tests/test_authorization_service.py \
  tests/test_seed.py \
  tests/test_role_management_models.py
```

结果：

```text
38 passed, 1 warning
```

确认：

- 现有 `Permission(name=..., description=...)` 创建方式兼容；
- 登录和 Refresh Token 的既有 Scope 行为未改变；
- AuthorizationService 既有行为未改变；
- Seed 重复执行仍保持幂等；
- 角色数据层与 `role_permissions` 无回归。

### 8.3 静态检查

执行：

```bash
pdm run lint
git diff --check
```

结果：全部通过，无 lint 或空白错误。

开发过程中定向 lint 首次发现迁移测试存在一个未使用 import，删除后定向与完整 lint 均通过。

### 8.4 完整回归

执行：

```bash
pdm run test
```

结果：

```text
166 passed, 9 skipped, 1 warning
```

9 个跳过项包括既有用户、App、角色 opt-in 集成测试，以及本阶段默认跳过的 Permission 迁移测试。

### 8.5 真实 PostgreSQL 迁移往返

使用当前本机 PostgreSQL 16 容器的管理员连接，但没有对其现有 `test_auth` 开发数据库执行迁移或降级。测试通过管理员数据库连接创建随机隔离数据库，在临时数据库完成全部操作后自动删除。

执行结果：

```text
1 passed, 1 warning
```

验证流程：

```text
创建随机临时数据库
  → upgrade 0004_role_management
  → 插入历史权限、角色和 role_permissions
  → upgrade 0005_permission_management
  → 检查 12 个 Permission 字段、可空性、默认值和索引
  → 检查 permission_endpoints 复合主键、级联外键和索引
  → 检查历史数据和关联保留
  → 插入新权限并检查整数自增 ID
  → 检查重复绑定被拒绝
  → alembic check
  → downgrade 0004_role_management
  → 检查 Permission 恢复为 3 列且绑定表删除
  → 检查历史权限和 role_permissions 保留
  → upgrade 0005_permission_management
  → 再次检查回填、关联、alembic check 和 current
  → 自动删除随机临时数据库
```

结果：全部通过。

### 8.6 警告说明

所有测试中的唯一警告均为项目现有的 Starlette TestClient/httpx 弃用提示：

```text
StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated
```

该警告与本阶段权限数据层改动无关，本阶段未擅自升级依赖。

---

## 9. 最终文件变更

修改：

```text
app/models/permission.py
alembic/env.py
scripts/validate_phase_4.py
tests/test_phase_4_integration.py
scripts/validate_app_phase_5.py
tests/test_app_phase_5_integration.py
scripts/validate_role_management_phase_5.py
tests/test_role_management_phase_5_integration.py
```

新增：

```text
app/models/permission_endpoint.py
alembic/versions/0005_permission_management.py
tests/test_permission_management_models.py
tests/test_permission_management_migration.py
plan/PERMISSION_MANAGEMENT_PHASE_1_IMPLEMENTATION_PLAN.md
plan/PERMISSION_MANAGEMENT_PHASE_1_EXECUTION.md
```

本次会话开始前已存在的未跟踪设计文档：

```text
docs/PERMISSION_SYNC_DESIGN.md
```

该设计文档是本阶段开发依据，本阶段未将其覆盖或删除。

---

## 10. 第一阶段验收结论

第一阶段“权限数据层与迁移”已完成：

- Permission 保持整数自增 ID 和唯一 `name` 权限编码；
- 权限展示、声明状态、启用状态、禁用/缺失元数据、时间和版本字段已建立；
- `permission_endpoints` API 绑定快照表已建立；
- `0005_permission_management` 可安全升级、降级和再次升级；
- 历史 Permission ID、name、description 保持不变；
- 历史权限展示名称正确回填为编码；
- `role_permissions` 关联在迁移往返中不丢失；
- ORM 元数据与数据库迁移结构一致，`alembic check` clean；
- 数据层测试、权限相关回归、完整 lint、完整测试和真实 PostgreSQL 迁移验证全部通过；
- 未提前实现第二阶段及之后的路由扫描、同步、管理 API 或鉴权变更。
