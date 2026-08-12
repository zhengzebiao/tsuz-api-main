# 子应用管理 App：第一阶段数据层开发记录

## 1. 开发范围

本阶段根据 [SUB_APP_DEVELOPMENT_PLAN.md](../SUB_APP_DEVELOPMENT_PLAN.md) 实施第一阶段“数据层”，只包含以下内容：

1. 新增 `App` SQLAlchemy 数据模型；
2. 将 App 模型注册到 Alembic 元数据；
3. 创建 `apps` 表数据库迁移；
4. 编写数据层测试；
5. 在临时 PostgreSQL 环境中验证迁移升级、降级和再次升级。

本阶段未实现以下内容：

- App 管理 API；
- Pydantic Schema；
- Service 业务逻辑；
- App ID 和 App Secret 生成逻辑；
- App Secret 校验逻辑；
- 权限 Seed；
- App 管理审计业务流程；
- Redis 相关逻辑。

---

## 2. 数据模型实现

新增文件：

```text
app/models/app.py
```

新增 `App` 模型，对应数据库表 `apps`。

### 2.1 字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `Integer` | 数据库内部主键 |
| `app_id` | `String(64)` | 子应用公开唯一标识 |
| `app_secret_hash` | `String(64)` | App Secret 哈希值，不保存明文 Secret |
| `name` | `String(128)` | 应用名称 |
| `icon_url` | `String(2048)`，可空 | 应用图标地址 |
| `access_url` | `String(2048)` | 子应用访问地址 |
| `service_account_name` | `String(128)` | 服务账号展示名称 |
| `is_enabled` | `Boolean` | 是否启用，默认 `true` |
| `disabled_at` | `DateTime`，可空 | 禁用时间 |
| `disabled_reason` | `String(500)`，可空 | 禁用原因 |
| `secret_updated_at` | `DateTime` | Secret 最近更新时间 |
| `created_at` | `DateTime` | 创建时间 |
| `updated_at` | `DateTime` | 最近更新时间 |
| `version` | `Integer` | 乐观锁版本，默认 `1` |

时间字段遵循项目现有模型约定，使用无时区 `DateTime`。后续业务层统一按项目既有 UTC 时间处理方式写入时间值。

### 2.2 索引

新增以下索引：

- `ix_apps_id`：主键 ID 索引；
- `ix_apps_app_id`：唯一 App ID 索引；
- `ix_apps_name`：应用名称索引；
- `ix_apps_is_enabled`：启用状态索引。

`app_secret_hash` 没有建立公开查询索引。后续凭证校验应先通过 `app_id` 查询 App，再校验 Secret。

---

## 3. Alembic 元数据注册

修改文件：

```text
alembic/env.py
```

在现有模型显式导入列表中加入 App 模型：

```python
from app.models import app, audit_event, permission, role, session, user  # noqa: F401
```

这样 Alembic 的 `Base.metadata` 可以发现 `apps` 表，并支持 `alembic check` 对模型和迁移状态进行比对。

---

## 4. 数据库迁移

新增文件：

```text
alembic/versions/0003_app_management.py
```

迁移关系：

```text
0001_initial_auth_schema
    ↓
0002_user_management
    ↓
0003_app_management
```

### 4.1 `upgrade`

升级迁移会：

1. 创建 `apps` 表；
2. 创建 App ID 唯一索引；
3. 创建名称索引；
4. 创建启用状态索引；
5. 为 `is_enabled` 设置数据库默认值 `true`；
6. 为 `secret_updated_at`、`created_at`、`updated_at` 设置数据库当前时间默认值；
7. 为 `version` 设置数据库默认值 `1`。

迁移不会插入任何业务 App 数据，App ID 和 Secret 应由后续 Service 创建时生成。

### 4.2 `downgrade`

降级迁移会按以下顺序清理：

1. 启用状态索引；
2. 名称索引；
3. App ID 唯一索引；
4. 主键 ID 索引；
5. `apps` 表。

该降级只影响本阶段创建的 `apps` 表，不修改用户、角色、权限、会话和审计表。

---

## 5. 数据层测试

新增文件：

```text
tests/test_app_management_models.py
```

当前覆盖：

- App 默认启用；
- 默认版本号为 `1`；
- `secret_updated_at`、`created_at`、`updated_at` 自动生成；
- 禁用时间和禁用原因可空；
- 应用基本字段可持久化；
- `app_id` 唯一约束有效。

测试使用 SQLite 内存数据库，不连接开发 PostgreSQL，也不连接 Redis。

---

## 6. 验证结果

### 6.1 静态检查

```bash
pdm run ruff check app/models/app.py alembic/env.py alembic/versions/0003_app_management.py tests/test_app_management_models.py
pdm run lint
```

结果：通过。

### 6.2 数据层测试

```bash
pdm run pytest tests/test_app_management_models.py -q
```

结果：

```text
3 passed
```

### 6.3 完整测试

```bash
pdm run test
```

结果：

```text
81 passed, 2 skipped
```

### 6.4 PostgreSQL 临时环境迁移回环

迁移回环验证使用临时 PostgreSQL 容器完成，未使用当前开发数据库：

```text
临时 PostgreSQL 容器
  → upgrade 0003_app_management
  → 检查 apps 表、字段、默认值和索引
  → alembic check
  → downgrade 0002_user_management
  → 确认 apps 表删除且已有表仍存在
  → upgrade head
  → 确认当前版本为 0003_app_management
  → 停止并删除临时容器
```

验证结果：通过。

检查内容包括：

- `apps` 表存在；
- 14 个字段完整；
- 必填字段不可为空；
- `icon_url`、禁用相关字段可为空；
- `app_id` 唯一索引存在；
- 名称和启用状态索引存在；
- 默认启用状态生效；
- 默认版本号为 `1`；
- 时间默认值生效；
- 降级后 `apps` 表被删除；
- 既有用户、角色、权限、会话和审计表仍存在；
- 再次升级到 `head` 成功。

---

## 7. 数据库和 Redis 操作约束

后续开发和验证必须遵循以下规则：

### 7.1 开发数据库

不得在当前开发数据库上直接执行以下操作：

- 测试迁移升级或降级；
- 删除表或删除数据库；
- 清空业务数据；
- 执行破坏性回滚；
- 运行会写入测试数据的集成测试。

当前开发数据库只用于开发人员明确授权的日常开发操作。

### 7.2 临时数据库

涉及数据库的测试、迁移回环和集成验证，统一使用：

- 临时 PostgreSQL 容器；或
- 明确创建的临时数据库；或
- 测试框架创建的隔离数据库。

临时资源使用完成后必须清理。清理前确认目标是临时资源，不能误操作开发数据库。

推荐验证模式：

```text
启动无持久化临时 PostgreSQL 容器
  → 使用独立端口和独立 DATABASE_URL
  → 执行迁移和测试
  → 收集验证结果
  → 停止并删除临时容器
```

### 7.3 Redis

本阶段没有 Redis 变更，也没有连接 Redis。

后续如果功能涉及 Redis：

- 使用临时 Redis 容器或独立测试 Redis 实例；
- 使用独立数据库编号、Key 前缀和连接地址；
- 不得使用开发环境的 Redis Key 前缀；
- 不得执行开发 Redis 的 `FLUSHDB` 或 `FLUSHALL`；
- 验证完成后清理临时 Redis 容器和临时数据。

---

## 8. 本阶段文件清单

| 文件 | 状态 | 说明 |
| --- | --- | --- |
| `app/models/app.py` | 新增 | App SQLAlchemy 数据模型 |
| `alembic/env.py` | 修改 | 注册 App 模型元数据 |
| `alembic/versions/0003_app_management.py` | 新增 | `apps` 表迁移 |
| `tests/test_app_management_models.py` | 新增 | App 数据层测试 |
| `docs/SUB_APP_DATA_LAYER_IMPLEMENTATION.md` | 新增 | 本阶段开发记录 |

---

## 9. 下一阶段

下一阶段为“安全与 Schema”，预计实现：

1. App ID 生成函数；
2. App Secret 生成函数；
3. App Secret Hash 处理；
4. 创建、编辑、启用、禁用和 Secret 响应 Schema；
5. 确保普通响应中不包含 Secret 或 Secret Hash。

在下一阶段开始前，仍需遵循本文件中的临时数据库和临时 Redis 约束。
