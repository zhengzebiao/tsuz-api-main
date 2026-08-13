# 权限管理 Permission：第一阶段实施计划

## 1. Context

根据 [PERMISSION_SYNC_DESIGN.md](../docs/PERMISSION_SYNC_DESIGN.md) 实施权限模块第一阶段“权限数据层与迁移”。现有 `Permission` 只有整数主键、唯一权限编码 `name` 和说明字段；后续路由扫描、数据库同步、权限启停以及管理 API 需要稳定的数据结构保存权限声明状态、展示信息、版本和 API 绑定快照。

本阶段目标是在不改变已有权限 ID、编码、说明及 `role_permissions` 关联的前提下：

1. 扩展 `permissions` 表及 ORM 模型；
2. 新增 `permission_endpoints` API 绑定表及 ORM 模型；
3. 新增安全、可逆的 `0005_permission_management` 迁移；
4. 通过模型测试和真实 PostgreSQL 迁移往返验证数据兼容性。

本阶段只建立数据基础，不实现路由扫描、权限同步、权限管理 API 或鉴权行为变更。

---

## 2. 范围与约束

- `Permission.id` 保持整数自增主键，不改为 UUID，不重建既有记录；
- `Permission.name` 继续表示全局唯一的稳定权限编码，不新增或重命名 `code`；
- 不增加 `permissions.app_id`；
- 不修改 `role_permissions`、`user_roles` 的结构和数据；
- 不修改 `require_permissions()`、`AuthService`、`AuthorizationService`、Seed、路由和部署流程；
- 时间字段沿用项目当前无时区 `DateTime` 约定；
- 迁移沿用 [0004_role_management.py](../alembic/versions/0004_role_management.py) 的“先以可空形式增加、回填历史数据、再收紧为非空”模式；
- 真实迁移往返只能运行于本次创建并清理的隔离临时 PostgreSQL 数据库，不在共享开发或生产数据库执行降级。

---

## 3. 数据模型计划

### 3.1 扩展 Permission

修改 [permission.py](../app/models/permission.py)，保留：

```text
id
name
description
```

新增：

| 字段 | 类型 | 可空 | 默认值 | 用途 |
| --- | --- | --- | --- | --- |
| `display_name` | `String(128)` | 否 | 空字符串 | 管理端展示名称；历史数据迁移时回填为 `name` |
| `is_declared` | `Boolean` | 否 | `true` | 当前代码是否仍声明权限 |
| `is_enabled` | `Boolean` | 否 | `true` | 管理员是否启用权限 |
| `disabled_at` | `DateTime` | 是 | `NULL` | 最近一次禁用时间 |
| `disabled_reason` | `String(500)` | 是 | `NULL` | 禁用原因 |
| `missing_at` | `DateTime` | 是 | `NULL` | 权限最近一次从代码声明中消失的时间 |
| `created_at` | `DateTime` | 否 | 当前时间 | 创建时间 |
| `updated_at` | `DateTime` | 否 | 当前时间 | 最近更新时间 |
| `version` | `Integer` | 否 | `1` | 后续乐观锁版本 |

增加两个非唯一普通索引：

```text
ix_permissions_is_declared
ix_permissions_is_enabled
```

保留现有 `ix_permissions_id` 和唯一 `ix_permissions_name`。

### 3.2 新增 PermissionEndpoint

新增 [permission_endpoint.py](../app/models/permission_endpoint.py)，对应表：

```text
permission_endpoints
- permission_id
- http_method
- path
- route_name
```

约束：

- 复合主键为 `(permission_id, http_method, path)`；
- `permission_id` 外键指向 `permissions.id`，使用 `ON DELETE CASCADE`；
- `http_method` 使用 `String(16)`；
- `path` 使用 `String(2048)`，保存路由模板路径；
- `route_name` 使用 `String(255)`；
- 增加非唯一索引 `ix_permission_endpoints_http_method_path`。

本阶段不增加 ORM relationship；项目当前对关联数据采用显式表查询，保持现有风格。

---

## 4. 迁移计划

新增 [0005_permission_management.py](../alembic/versions/0005_permission_management.py)：

```text
0004_role_management
    ↓
0005_permission_management
```

### 4.1 upgrade

1. 为 `permissions` 增加新列；历史数据需要回填的必填列先设为 `nullable=True`，并同时保留最终 server default；
2. 使用显式更新回填所有既有权限：

   ```text
   display_name = name
   is_declared = true
   is_enabled = true
   created_at = CURRENT_TIMESTAMP
   updated_at = CURRENT_TIMESTAMP
   version = 1
   ```

3. 不修改历史 `id`、`name`、`description`；
4. 将必填列收紧为 `NOT NULL`；
5. 创建两个权限状态索引；
6. 创建 `permission_endpoints` 及其复合主键、级联外键和方法/路径索引。

### 4.2 downgrade

按逆序：

1. 删除 API 绑定索引和 `permission_endpoints` 表；
2. 删除两个权限状态索引；
3. 删除 Permission 新增字段；
4. 恢复 `0004_role_management` 的 `permissions(id, name, description)` 结构。

不得重建 `permissions` 表，不得删除权限、角色或 `role_permissions` 数据。

---

## 5. Alembic 元数据计划

修改 [alembic/env.py](../alembic/env.py)，显式导入 `permission_endpoint` 模块，使新表进入 `Base.metadata` 并参与 `alembic check`。

不调整空的 `app/models/__init__.py`，避免为本阶段引入项目当前不存在的集中导出模式。

---

## 6. 测试计划

### 6.1 模型测试

新增 [test_permission_management_models.py](../tests/test_permission_management_models.py)，覆盖：

- Permission 整数自增 ID；
- `name` 唯一性及原说明字段语义；
- 新字段默认值和自定义值持久化；
- 字段长度、可空性、主键和索引元数据；
- PermissionEndpoint 复合主键、级联外键和方法/路径索引；
- 重复 API 绑定被数据库拒绝；
- 不同权限或 HTTP Method 可以绑定同一路径；
- `role_permissions` 关联保持可用。

### 6.2 真实 PostgreSQL 迁移测试

新增 [test_permission_management_migration.py](../tests/test_permission_management_migration.py)，默认跳过，仅在显式设置环境变量后运行。测试：

```text
创建随机临时数据库
  → upgrade 0004_role_management
  → 插入历史权限、角色和 role_permissions
  → upgrade 0005_permission_management
  → 检查回填、字段、索引、默认值、约束和自增 ID
  → 检查重复绑定约束
  → alembic check
  → downgrade 0004_role_management
  → 检查原结构和历史关联保留
  → upgrade 0005_permission_management
  → 再次检查回填和 alembic check
  → 删除临时数据库
```

安全边界：

- 默认只允许本机 PostgreSQL；
- 默认拒绝 PostgreSQL 5432；
- 使用默认端口或远程环境必须显式允许；
- 只删除测试创建的随机数据库；
- Alembic 错误输出必须脱敏数据库 URL 和密码。

### 6.3 回归测试

运行：

- [test_auth_service.py](../tests/test_auth_service.py)；
- [test_authorization_service.py](../tests/test_authorization_service.py)；
- [test_seed.py](../tests/test_seed.py)；
- [test_role_management_models.py](../tests/test_role_management_models.py)；
- 完整默认测试套件。

新增非空字段必须通过 ORM/server default 保持现有 `Permission(name=..., description=...)` 调用兼容。

新增迁移成为 Alembic head 后，同步更新既有用户、App、角色真实验证脚本及对应 opt-in 测试中的最终 head revision 断言为 `0005_permission_management`。仅调整最终 `upgrade head/current` 预期，不改变它们各自验证的历史迁移中间节点和业务流程。

---

## 7. 验证命令

```bash
pdm run test -- tests/test_permission_management_models.py tests/test_permission_management_migration.py
pdm run test -- \
  tests/test_permission_management_models.py \
  tests/test_auth_service.py \
  tests/test_authorization_service.py \
  tests/test_seed.py \
  tests/test_role_management_models.py
pdm run lint
pdm run test
git diff --check
```

真实迁移测试使用显式隔离环境变量运行，不把连接凭证写入计划或执行记录。

---

## 8. 验收标准

- `Permission.id` 仍为整数自增主键；
- `Permission.name` 仍为唯一权限编码；
- 既有权限的 ID、名称和说明在迁移往返中保持不变；
- `display_name` 对历史权限正确回填为 `name`；
- 状态、时间和版本默认值正确；
- `permission_endpoints` 的复合主键、级联外键和索引正确；
- `role_permissions` 数据在升级、降级和再次升级后不丢失；
- `alembic check` clean；
- 定向测试、lint 和完整回归通过；
- 未提前实现第二至第五阶段功能。
