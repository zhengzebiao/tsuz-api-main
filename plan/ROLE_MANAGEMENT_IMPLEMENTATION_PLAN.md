# 角色管理与用户角色分配具体实现方案

## 1. 文档目标

本文档描述主应用中“角色管理 Role”模块，以及用户管理中“分配角色”能力的开发方案。

根据 `docs/MAIN_APP_MODULES.md`，本期只实现角色模块中带 ✅ 的能力：

1. 新增角色；
2. 编辑角色；
3. 启用、禁用角色；
4. 查看角色关联的用户。

同时完成用户管理中的：

5. 查看用户当前角色；
6. 给用户分配角色。

为支撑管理页面，角色列表和角色详情属于上述功能必需的基础查询接口，因此一并实现。

本期不实现：

- 删除或归档角色；
- 给角色分配权限；
- 权限目录管理；
- 角色使用情况统计；
- 角色变更记录查询；
- 用户直接分配权限；
- 子应用角色、子应用权限同步和菜单管理。

---

## 2. 已确认的设计决策

### 2.1 角色范围

本期角色采用**主应用全局角色**：

- 继续使用现有 `roles` 表；
- 不增加 `app_id`；
- 不绑定子应用；
- 角色名称保持全平台唯一；
- 子应用角色及权限同步后续单独设计和实施。

### 2.2 用户角色分配方式

用户角色采用**完整集合整体替换**：

```text
用户当前角色：[admin, auditor]
提交角色集合：[auditor, operator]
最终角色集合：[auditor, operator]
```

服务端计算差集：

```text
新增：operator
移除：admin
保留：auditor
```

该方式具有以下特点：

- 接口语义简单；
- 前端读取当前角色后一次保存；
- 重复提交相同集合时幂等；
- 审计可以清晰记录变更前后的完整集合。

### 2.3 权限边界

本期新建角色初始不包含权限，可以分配给用户，但不会自动产生任何接口权限。

现有授权关系保持：

```text
用户
  → user_roles
角色
  → role_permissions
权限
```

本期不提供修改 `role_permissions` 的接口，现有 Seed 和认证流程仍可继续使用该关联表。

---

## 3. 现有项目基础

项目当前已经具备：

- `Role` 模型及 `roles` 表；
- `user_roles` 用户角色关联表；
- `role_permissions` 角色权限关联表；
- `Permission` 模型；
- 登录和刷新 Token 时加载用户角色及权限；
- `require_permissions()` 管理接口权限依赖；
- `AuditEvent` 管理操作审计模型；
- `SessionService.revoke_user_sessions()` 全量撤销用户会话；
- 用户管理和子应用管理的 Schema、Service、API、领域错误及测试模式；
- Alembic `0001` 至 `0003` 迁移链。

需要补充：

- 角色的描述、状态、时间和乐观锁字段；
- 角色管理 Schema；
- 角色管理 Service；
- `/admin/roles` 管理 API；
- 用户角色查询及整体替换 API；
- 角色管理权限 Seed；
- 禁用角色的鉴权过滤；
- 对应模型、Service、API 和集成测试。

模块调用关系：

```text
Admin Roles API
  → Admin Role Schemas
  → AdminRoleService
  → Role / User / AuditEvent Models
  → PostgreSQL / Redis

Admin Users API
  → User Role Schemas
  → AdminUserService
  → User / Role / user_roles / AuditEvent
  → PostgreSQL / Redis
```

---

## 4. 数据模型设计

### 4.1 扩展 `roles` 表

保留现有字段：

```text
id
name
```

新增字段：

| 字段 | 类型 | 是否为空 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `description` | String(255) | 否 | 空字符串 | 角色说明 |
| `is_enabled` | Boolean | 否 | `true` | 是否启用 |
| `disabled_at` | DateTime | 是 | `NULL` | 最近一次禁用时间 |
| `disabled_reason` | String(500) | 是 | `NULL` | 禁用原因 |
| `created_at` | DateTime | 否 | 当前时间 | 创建时间 |
| `updated_at` | DateTime | 否 | 当前时间 | 最近更新时间 |
| `version` | Integer | 否 | `1` | 乐观锁版本号 |

索引和约束：

- 保留 `name` 唯一索引；
- 增加 `is_enabled` 普通索引；
- 名称最大长度保持 64；
- 描述最大长度为 255；
- 禁用原因最大长度为 500。

### 4.2 保留关联表

现有两个关联表不改变：

```text
user_roles
- user_id
- role_id

role_permissions
- role_id
- permission_id
```

禁用角色时：

- 不删除 `user_roles`；
- 不删除 `role_permissions`；
- 保留历史关联，便于查询和重新启用；
- 鉴权时忽略该角色及其权限。

### 4.3 数据库迁移

新增：

```text
alembic/versions/0004_role_management.py
```

迁移顺序：

1. 以可空形式增加已有数据需要回填的必填字段；
2. 将既有角色回填为：
   - `description=''`；
   - `is_enabled=true`；
   - `created_at=CURRENT_TIMESTAMP`；
   - `updated_at=CURRENT_TIMESTAMP`；
   - `version=1`；
3. 将必填字段收紧为非空；
4. 保留数据库默认值；
5. 创建 `is_enabled` 索引；
6. `downgrade()` 按逆序删除新增索引和字段，恢复 `0003` 结构。

迁移不得删除或重建现有角色、用户角色和角色权限数据。

---

## 5. 角色状态与安全规则

### 5.1 启用与禁用

使用明确的目标状态接口，不提供 `toggle`：

```text
POST /admin/roles/{role_id}/disable
POST /admin/roles/{role_id}/enable
```

禁用规则：

- 设置 `is_enabled=false`；
- 保存 `disabled_at` 和可选 `disabled_reason`；
- 递增 `version`；
- 保留用户及权限关联；
- 撤销所有关联用户的活跃 Session；
- 重复禁用返回 `changed=false`；
- 重复禁用不覆盖首次禁用时间和原因。

启用规则：

- 设置 `is_enabled=true`；
- 清空 `disabled_at` 和 `disabled_reason`；
- 递增 `version`；
- 重复启用返回 `changed=false`；
- 不自动给用户创建 Session，用户需要重新登录。

### 5.2 `admin` 核心角色保护

Seed 创建的 `admin` 是当前系统核心管理角色，应增加保护：

- 允许修改描述；
- 不允许修改角色名称；
- 不允许禁用；
- 不允许管理员从自己身上移除 `admin`；
- 不允许从最后一名可用管理员身上移除 `admin`。

这样可以避免管理 API 因核心角色被破坏而无法继续授权。

### 5.3 会话撤销

角色变化可能改变 Token 中已有的 `roles` 和 `scope`，因此以下操作必须撤销受影响用户的活跃 Session：

- 禁用角色；
- 修改角色名称；
- 给用户新增角色；
- 从用户移除角色。

仅修改角色描述不影响鉴权，不撤销 Session。

---

## 6. Schema 设计

新增：

```text
app/schemas/admin_role.py
```

并扩展：

```text
app/schemas/admin_user.py
```

所有管理请求继续使用：

```python
ConfigDict(extra="forbid")
```

禁止客户端注入未声明字段。

### 6.1 角色创建

```text
AdminRoleCreate
- name: str，1～64
- description: str，默认空字符串，最大 255
```

处理规则：

- `name` 去除首尾空白；
- 空白名称拒绝；
- `description` 去除首尾空白；
- 状态由服务端固定为启用；
- 客户端不能在创建时指定 ID、状态、时间或版本。

### 6.2 角色编辑

```text
AdminRoleUpdate
- name: str，可选，1～64
- description: str，可选，最大 255
- version: int，必填且大于 0
```

限制：

- 只允许修改名称和描述；
- 不允许通过编辑接口修改启用状态；
- 无实际变化时返回 `changed=false`，不递增版本。

### 6.3 角色禁用请求

```text
AdminRoleDisableRequest
- reason: str，可选，最大 500
```

### 6.4 角色响应

```text
AdminRoleResponse
- id
- name
- description
- is_enabled
- disabled_at
- disabled_reason
- created_at
- updated_at
- version
```

动作响应额外返回：

```text
changed
revoked_sessions
```

### 6.5 用户角色分配请求

```text
AdminUserRoleAssignment
- role_ids: list[int]
- version: int
```

校验规则：

- `role_id` 必须是正整数；
- 不允许重复 ID；
- 列表可以为空，表示移除普通用户全部角色；
- 必须提供当前用户 `version`，用于检测并发覆盖。

角色分配响应：

```text
AdminUserRolesResponse
- user_id
- roles
- version
- changed
- revoked_sessions
```

每个角色使用精简、安全的角色响应，不返回权限集合。

---

## 7. Service 设计

### 7.1 新增 `AdminRoleService`

新增：

```text
app/services/admin_role_service.py
```

职责：

- 分页查询角色；
- 查询角色详情；
- 创建角色；
- 编辑角色；
- 禁用角色；
- 启用角色；
- 分页查询关联用户；
- 执行角色领域校验；
- 写入角色审计；
- 撤销受影响用户会话。

Service 写操作只进行业务修改、审计写入和必要的 `flush`，由 API 调用方统一负责 `commit/rollback`，保持与 `AdminAppService` 一致的事务边界。

### 7.2 角色列表

支持：

```text
page
page_size
keyword
is_enabled
```

搜索字段：

- `name`；
- `description`。

排序：

```text
created_at DESC, id DESC
```

确保分页结果稳定。

### 7.3 创建角色

流程：

1. 规范化名称和描述；
2. 检查角色名是否存在；
3. 创建启用状态角色；
4. 捕获数据库唯一约束冲突并返回固定领域错误；
5. 写入 `role.created` 审计；
6. 由 API 提交事务。

### 7.4 编辑角色

流程：

1. 查询角色；
2. 校验客户端 `version`；
3. 校验 `admin` 角色保护规则；
4. 计算实际变化；
5. 无变化时返回 `changed=false`；
6. 使用 `WHERE id=? AND version=?` 更新；
7. 名称变化时撤销全部关联用户 Session；
8. 写入 `role.updated` 审计；
9. 由 API 提交事务。

### 7.5 禁用与启用角色

流程：

1. 使用 `SELECT ... FOR UPDATE` 锁定角色；
2. 校验角色存在及核心角色保护规则；
3. 已处于目标状态时返回幂等结果；
4. 修改状态元数据并递增版本；
5. 禁用时撤销全部关联用户 Session；
6. 写入状态变化审计；
7. 由 API 提交事务。

### 7.6 查看关联用户

通过 `user_roles` 查询：

```text
GET /admin/roles/{role_id}/users
```

支持：

```text
page
page_size
keyword
is_active
is_blacklisted
```

返回现有安全用户响应字段，不返回：

- `hashed_password`；
- Token；
- Session 内容；
- 用户权限集合。

### 7.7 扩展 `AdminUserService`

增加：

```text
get_user_roles(user_id)
assign_roles(user_id, role_ids, version, actor_user_id, request_id)
```

角色分配流程：

1. 使用行锁读取目标用户；
2. 校验用户 `version`；
3. 查询用户当前角色；
4. 一次查询提交的全部目标角色；
5. 如果存在无效角色 ID，整体拒绝；
6. 已禁用角色不能新增，但用户已有的禁用角色可以原样保留；
7. 计算新增、移除和保留集合；
8. 执行管理员安全规则；
9. 无变化时返回 `changed=false`，不递增版本、不撤销 Session；
10. 只增删实际变化的 `user_roles` 行；
11. 递增用户 `version`；
12. 撤销目标用户全部活跃 Session；
13. 写入 `user.roles_assigned` 审计；
14. 提交后返回最新角色集合。

---

## 8. 领域错误与 HTTP 映射

角色管理固定错误建议：

| 领域错误 | HTTP 状态 | detail |
| --- | --- | --- |
| 角色不存在 | 404 | `ROLE_NOT_FOUND` |
| 角色名重复 | 409 | `ROLE_NAME_ALREADY_EXISTS` |
| 角色版本冲突 | 409 | `ROLE_VERSION_CONFLICT` |
| 核心角色禁止操作 | 409 | `PROTECTED_ROLE_OPERATION` |
| 角色已禁用且不能新增分配 | 409 | `ROLE_DISABLED` |
| 用户不存在 | 404 | `USER_NOT_FOUND` |
| 用户版本冲突 | 409 | `USER_VERSION_CONFLICT` |
| 不能移除自己的管理员角色 | 409 | `SELF_OPERATION_NOT_ALLOWED` |
| 不能移除最后一个管理员 | 409 | `LAST_ACTIVE_ADMIN` |

Pydantic 请求字段错误返回 422。

错误响应只返回固定错误码，不返回 SQL、约束名称、内部堆栈或敏感数据。

---

## 9. 管理 API 设计

### 9.1 角色管理 API

统一前缀：

```text
/admin/roles
```

| 方法 | 路径 | 权限 | 用途 |
| --- | --- | --- | --- |
| GET | `/admin/roles` | `role:read` | 分页查询角色 |
| GET | `/admin/roles/{role_id}` | `role:read` | 查询角色详情 |
| POST | `/admin/roles` | `role:create` | 新增角色 |
| PATCH | `/admin/roles/{role_id}` | `role:update` | 编辑角色 |
| POST | `/admin/roles/{role_id}/disable` | `role:disable` | 禁用角色 |
| POST | `/admin/roles/{role_id}/enable` | `role:enable` | 启用角色 |
| GET | `/admin/roles/{role_id}/users` | `role:read` | 查看角色关联用户 |

### 9.2 用户角色 API

扩展现有：

```text
/admin/users
```

| 方法 | 路径 | 权限 | 用途 |
| --- | --- | --- | --- |
| GET | `/admin/users/{user_id}/roles` | `user:read` | 查询用户当前角色 |
| PUT | `/admin/users/{user_id}/roles` | `user:assign_roles` | 整体替换用户角色 |

采用 `PUT` 是因为提交内容表示用户角色集合的完整目标状态，重复提交相同请求应得到相同结果。

### 9.3 事务边界

所有写接口统一执行：

1. 调用 Service；
2. 构造严格响应模型；
3. 响应构造成功后 `db.commit()`；
4. 领域错误时 `db.rollback()`；
5. 其他异常时也先 `rollback()` 再抛出；
6. 业务数据和 `AuditEvent` 在同一数据库事务提交。

---

## 10. 权限 Seed

扩展 `app/seed/__main__.py`：

```text
role:read
role:create
role:update
role:disable
role:enable
user:assign_roles
```

说明：

- 角色列表、详情和关联用户查询统一使用 `role:read`；
- 创建、编辑、启用和禁用使用独立权限；
- 用户角色修改使用 `user:assign_roles`；
- 现有 `ensure_permission()` 和 `ensure_role_permission()` 保持幂等；
- 新权限默认关联 Seed 的 `admin` 角色；
- 不新增 `role:delete` 和 `role:assign_permissions`。

---

## 11. 审计设计

角色管理审计动作：

```text
role.created
role.updated
role.disabled
role.enabled
```

用户角色分配审计动作：

```text
user.roles_assigned
```

角色审计：

```text
target_type = role
target_id = role.id
```

用户角色分配审计：

```text
target_type = user
target_id = user.id
```

`changes_json` 示例：

```json
{
  "roles": {
    "from": [
      {"id": 1, "name": "admin"}
    ],
    "to": [
      {"id": 2, "name": "auditor"}
    ]
  },
  "revoked_sessions": 1
}
```

禁止记录：

- 密码及密码哈希；
- Access Token 或 Refresh Token；
- Authorization Header；
- Session ID；
- 权限之外的敏感用户资料。

---

## 12. 登录与鉴权调整

修改 `app/services/auth_service.py`：

### 12.1 角色声明

`_get_user_roles()` 只返回：

```text
Role.is_enabled = true
```

的角色。

### 12.2 权限声明

`_get_user_permissions()` 显式连接 `Role`，并只聚合启用角色关联的权限：

```text
用户
  → user_roles
启用角色
  → role_permissions
权限
```

### 12.3 生效规则

- 禁用角色时立即撤销关联用户 Session，旧 Token 失效；
- 用户角色集合变化时立即撤销该用户 Session，旧 Token 失效；
- 用户重新登录后生成最新 `roles` 和 `scope`；
- 启用角色不会自动恢复旧 Session，用户需要重新登录；
- 禁用角色的关联数据仍保留，但不会进入 Token。

---

## 13. 分阶段实施步骤

本模块采用五个阶段分步开发。每个阶段完成后先验证，再进入下一阶段，不跨阶段提前加入业务功能。

### 第一阶段：角色数据层与迁移

开发内容：

1. 扩展 `app/models/role.py`；
2. 增加角色状态、时间、描述和版本字段；
3. 新增 `0004_role_management` 迁移；
4. 验证既有角色数据回填；
5. 补充角色模型测试；
6. 验证 Alembic upgrade、downgrade、upgrade。

本阶段不实现：

- Schema；
- Service；
- API；
- 权限 Seed；
- 用户角色分配逻辑。

阶段验收：

- 既有 `admin` 角色完整保留；
- 新字段默认值正确；
- ORM 与迁移结构一致；
- 迁移可安全往返。

### 第二阶段：严格 Schema 与领域边界

开发内容：

1. 新增 `app/schemas/admin_role.py`；
2. 定义创建、编辑、禁用、响应和分页 Schema；
3. 扩展用户角色查询及分配 Schema；
4. 实现文本规范化和严格字段限制；
5. 增加 Schema 验证测试。

本阶段不实现：

- Service 数据修改；
- API 路由；
- 权限 Seed。

阶段验收：

- 空白名称、超长字段、重复角色 ID 被拒绝；
- 编辑接口不能提交状态字段；
- 响应模型不暴露敏感用户字段；
- 角色分配允许空集合并要求用户版本。

### 第三阶段：角色服务与用户角色分配服务

开发内容：

1. 新增 `AdminRoleService`；
2. 实现角色列表、详情、创建和编辑；
3. 实现角色启用、禁用和关联用户查询；
4. 扩展 `AdminUserService` 查询及整体替换角色；
5. 实现 `admin` 角色保护和最后管理员保护；
6. 接入乐观锁、行锁、会话撤销和审计；
7. 补充 Service 单元测试和事务回滚测试。

本阶段不实现：

- HTTP API；
- 权限 Seed；
- 角色权限配置。

阶段验收：

- 角色生命周期业务规则正确；
- 角色分配幂等且并发安全；
- 无效角色不会产生部分更新；
- 鉴权相关变化会撤销会话；
- 核心 `admin` 角色不可被破坏；
- 业务变更与审计可在同一事务回滚。

### 第四阶段：API、权限 Seed 与鉴权接入

开发内容：

1. 新增 `/admin/roles` 路由；
2. 扩展 `/admin/users/{user_id}/roles` 路由；
3. 注册角色路由；
4. 增加角色管理和用户角色分配权限 Seed；
5. 调整 AuthService，仅加载启用角色及其权限；
6. 补充 API 认证、授权、错误映射和 OpenAPI 测试。

阶段验收：

- 所有路由在 OpenAPI 中存在；
- 无 Token 返回 401；
- 权限不足返回 403；
- 各接口使用正确的独立权限；
- 404、409、422 错误稳定；
- 禁用角色不再进入 Token；
- Seed 重复执行不产生重复权限或关联。

### 第五阶段：测试与验证

开发内容：

1. 完成模型、Schema、Service 和 API 全量测试；
2. 完成角色生命周期集成测试；
3. 完成用户角色整体替换集成测试；
4. 验证真实 PostgreSQL 迁移和锁行为；
5. 验证真实 Redis Session 撤销；
6. 验证真实登录、JWT 角色及 Scope 更新；
7. 运行静态检查和完整回归测试；
8. 检查审计和响应无敏感信息。

阶段验收：

- 完整测试套件通过；
- 迁移可往返且保留既有数据；
- 并发编辑能检测版本冲突；
- 并发启停和角色分配不会产生不一致；
- 禁用角色及角色重新分配会使旧 Session 失效；
- 重新登录后 Token 只包含最新启用角色及权限；
- 当前用户、子应用和认证功能无回归。

---

## 14. 测试清单

### 14.1 模型与迁移

1. 新角色默认启用；
2. 描述默认空字符串；
3. 禁用时间和原因默认为空；
4. 创建、更新时间正确；
5. 版本默认 `1`；
6. 角色名称保持唯一；
7. 既有角色迁移后正确回填；
8. `user_roles` 和 `role_permissions` 数据不丢失；
9. downgrade 后恢复原结构；
10. `alembic check` 无结构漂移。

### 14.2 角色查询与创建

1. 列表分页正确；
2. 名称和描述关键词搜索不区分大小写；
3. 启用状态筛选正确；
4. 排序稳定；
5. 详情不存在返回 `ROLE_NOT_FOUND`；
6. 创建时规范化名称和描述；
7. 重复名称返回固定冲突错误；
8. 创建审计不包含敏感数据。

### 14.3 编辑角色

1. 可以修改名称和描述；
2. 版本正确时更新成功；
3. 旧版本返回 `ROLE_VERSION_CONFLICT`；
4. 无变化返回 `changed=false`；
5. 无变化不递增版本；
6. 无变化不写无意义审计；
7. 修改名称撤销关联用户 Session；
8. 只修改描述不撤销 Session；
9. `admin` 角色不能改名。

### 14.4 启用与禁用

1. 普通角色可以禁用；
2. 禁用记录时间和原因；
3. 重复禁用幂等；
4. 重复禁用不覆盖原原因；
5. 禁用撤销全部关联用户 Session；
6. 禁用不删除用户或权限关联；
7. 角色可以重新启用；
8. 重新启用清空禁用元数据；
9. 重复启用幂等；
10. `admin` 角色不能禁用；
11. 有效状态变化写入审计。

### 14.5 查看角色关联用户

1. 角色不存在返回 404；
2. 分页和筛选正确；
3. 禁用角色仍能查看已有用户关联；
4. 响应不包含密码哈希；
5. 无关联用户时返回空列表和 `total=0`。

### 14.6 用户角色分配

1. 可以查询用户当前角色；
2. 可以整体替换角色集合；
3. 可以给普通用户清空角色；
4. 重复提交相同集合返回 `changed=false`；
5. 无效角色 ID 使整个请求失败；
6. 重复角色 ID 在 Schema 层拒绝；
7. 不能新增已禁用角色；
8. 可以原样保留用户已有的禁用角色；
9. 旧用户版本返回 `USER_VERSION_CONFLICT`；
10. 有效变化递增用户版本；
11. 有效变化撤销目标用户全部 Session；
12. 管理员不能移除自己的 `admin`；
13. 不能移除最后一名可用管理员的 `admin`；
14. 审计准确记录角色集合前后变化。

### 14.7 鉴权与 API

1. 所有 API 要求 Bearer Token；
2. 权限不足返回 403；
3. 每个写操作使用对应权限；
4. 禁用角色不进入 JWT `roles`；
5. 禁用角色的权限不进入 JWT `scope`；
6. 用户角色变化后旧 Session 失效；
7. 重新登录后获得最新角色和权限；
8. API 响应不包含敏感字段；
9. Seed 重复执行保持幂等；
10. OpenAPI 路径和请求响应模型完整。

---

## 15. 关键文件

预计修改：

```text
app/models/role.py
app/schemas/admin_user.py
app/services/admin_user_service.py
app/api/admin_users.py
app/services/auth_service.py
app/seed/__main__.py
app/main.py
```

预计新增：

```text
alembic/versions/0004_role_management.py
app/schemas/admin_role.py
app/services/admin_role_service.py
app/api/admin_roles.py
tests/test_role_management_models.py
tests/test_admin_role_service.py
tests/test_admin_roles_api.py
```

预计扩展测试：

```text
tests/test_admin_user_service.py
tests/test_admin_users_api.py
tests/test_auth_service.py
tests/test_seed.py
```

---

## 16. 验证命令

每个阶段先运行定向测试，再运行完整检查：

```bash
pdm run lint
pdm run test
```

迁移验证：

```bash
pdm run migrate
pdm run alembic-current
alembic check
```

真实迁移往返应使用独立临时 PostgreSQL 数据库：

```text
0003_app_management
  → 0004_role_management
  → 0003_app_management
  → head
```

不得在生产数据库或未隔离的开发数据库执行 downgrade 验证。

---

## 17. 验收标准

满足以下条件后，本期角色管理模块可以验收：

- 管理员可以新增、编辑、启用和禁用普通角色；
- 管理员可以分页查看角色及其关联用户；
- 管理员可以查询并整体替换用户角色；
- 角色名称保持全局唯一；
- `admin` 核心角色不能改名、禁用或被不安全移除；
- 禁用角色保留关联，但不再参与登录鉴权；
- 角色或用户授权变化会立即撤销受影响 Session；
- 重新登录后 JWT 只包含最新启用角色及其权限；
- 所有管理接口均有独立权限控制；
- 所有有效变更均有安全审计；
- 幂等操作不产生无意义版本和审计变化；
- 数据库迁移、静态检查、单元测试和集成测试全部通过；
- 不包含角色权限配置、角色删除、子应用角色及其他范围外功能。
