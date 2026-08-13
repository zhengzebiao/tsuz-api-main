# 权限声明、同步与管理具体实现方案

## 1. 文档目标

本文档描述主应用中“权限管理 Permission”模块，以及 API 权限声明自动同步能力的具体实现方案。

本期实现：

1. 在 FastAPI 路由中声明 API 所需权限；
2. 扫描已注册路由并同步权限到数据库；
3. 保存权限与 `HTTP Method + Path` 的绑定关系；
4. 权限列表、详情、展示信息编辑；
5. 权限启用、禁用；
6. API 不再声明权限时自动标记为废弃；
7. 登录、刷新 Token 和接口鉴权识别权限状态；
8. 权限状态变化后的 Session 撤销、审计和测试验证。

本期不实现：

- 后台手动创建 API 权限；
- 修改权限编码；
- 物理删除权限；
- 给角色分配权限的管理接口；
- 用户直接授予或拒绝权限；
- 子应用权限同步；
- 菜单权限和数据权限。

权限生效链路：

```text
API 声明权限
    → 部署命令扫描路由
    → 同步权限及 API 绑定到数据库
    → 角色通过 role_permissions 拥有权限
    → 用户通过 user_roles 获得角色
    → 登录或刷新 Token 时生成 scope
    → 请求 API 时校验 Token scope 和数据库权限状态
```

---

## 2. 对现有系统字段的确认

### 2.1 `id` 继续使用整数自增主键

当前 `Permission` 模型已经是：

```python
id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
```

本方案保持不变：

- `id` 是数据库内部整数主键；
- PostgreSQL 负责生成递增值；
- API 路径继续使用整数 `permission_id`；
- 迁移必须保留现有权限 ID；
- `role_permissions.permission_id` 关联不变。

不改成 UUID，也不重新生成已有 ID。

### 2.2 `name` 继续表示权限编码

当前系统中：

```text
Permission.name = app:read
Permission.name = user:create
Permission.name = role:disable
```

`AuthService` 读取 `Permission.name` 写入 JWT `scope`，Seed、测试和角色权限关联也都使用该字段。因此本期不将其重命名为 `code`。

统一语义：

| 字段 | 语义 |
| --- | --- |
| `name` | 稳定的权限编码，例如 `app:read`，创建后不可修改 |
| `display_name` | 后台展示名称，例如“查看应用”，允许编辑 |
| `description` | 权限说明，允许编辑 |

这样可避免修改现有鉴权、Seed、测试和数据关联，同时解决“权限编码”和“展示名称”混用的问题。

### 2.3 本期不增加 `app_id`

当前权限是**主应用全局权限**，`name` 在全平台唯一。现有 `apps` 表中的字段还存在两种容易混淆的含义：

- `apps.id`：数据库整数主键；
- `apps.app_id`：对外使用的字符串应用标识和凭证标识。

本期 API 全部来自主应用，不需要用 `app_id` 区分权限所属应用，因此不在 `permissions` 表增加该字段。

如果后续实施“子应用权限同步”，应单独设计：

```text
owner_app_id → ForeignKey("apps.id")
```

并重新确认权限唯一规则是全局 `name` 唯一，还是 `(owner_app_id, name)` 唯一。该设计不应提前混入本期主应用权限管理。

### 2.4 `resource` 和 `action` 从编码派生

权限编码固定使用：

```text
resource:action
```

例如：

```text
name = role:disable
resource = role
action = disable
```

本期不重复保存 `resource`、`action` 字段，避免它们与 `name` 不一致。Schema 返回和筛选时可以从 `name` 拆分得到。

权限编码校验规则：

```regex
^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$
```

并继续受现有 `String(128)` 长度限制。

---

## 3. 已确认的设计决策

### 3.1 代码声明是 API 权限来源

API 权限不能由管理员在后台随意创建。后台创建一条没有任何 API 声明的权限不会自动保护接口，容易产生“看起来存在、实际无效”的权限。

本期采用：

```text
API 代码声明 → 同步命令创建数据库权限
```

后台不提供 `POST /admin/permissions`。

### 3.2 数据库负责状态、展示信息和授权关系

代码负责：

- 权限编码；
- 哪些 API 使用该权限。

第一版新权限首次同步时，`display_name` 默认使用权限编码，`description` 默认使用空字符串；管理员可在后台补充展示名称和说明。代码中的默认展示元数据作为后续可选增强，不作为第一版同步所必需的数据源。

数据库负责：

- 管理员编辑后的展示名称和说明；
- 启用、禁用状态；
- 与角色的现有授权关系；
- 状态时间、版本和审计。

同步时不得覆盖管理员已经修改的 `display_name`、`description` 和 `is_enabled`。

### 3.3 废弃不是删除

当权限不再被任何已注册 API 声明时：

```text
is_declared = false
missing_at = 当前时间
```

同时清理该权限的 API 绑定快照，但保留：

- `permissions` 记录；
- 权限 ID；
- `role_permissions` 关联；
- 审计和历史信息。

如果代码以后重新声明相同 `name`：

```text
is_declared = true
missing_at = NULL
```

原权限 ID 和角色关联继续使用；管理员之前手动设置的 `is_enabled` 不被覆盖。

### 3.4 禁用与废弃分别表示不同含义

| 状态 | 控制方 | 含义 |
| --- | --- | --- |
| `is_declared=true` | 代码同步 | 当前至少有一个 API 声明该权限 |
| `is_declared=false` | 代码同步 | 当前代码中已不存在该权限，即废弃 |
| `is_enabled=true` | 管理员 | 管理员允许该权限生效 |
| `is_enabled=false` | 管理员 | 管理员紧急关闭该权限 |

权限有效条件：

```text
is_declared == true && is_enabled == true
```

### 3.5 生产环境使用部署命令，不在每个 Worker 启动时写库

当前生产入口使用 Gunicorn 多 Worker。如果每个 Worker 启动时都同步权限，会产生并发写入、重复审计和启动失败放大的问题。

因此推荐：

```text
数据库迁移
    → 权限同步命令
    → 启动/滚动更新 API 服务
```

开发环境可以显式执行同步命令，但正式环境不在 FastAPI startup hook 中自动写数据库。

---

## 4. 数据模型设计

### 4.1 扩展 `permissions` 表

保留现有字段：

```text
id
name
description
```

其中 `name` 继续是权限编码。

新增字段：

| 字段 | 类型 | 是否为空 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `display_name` | String(128) | 否 | 由 `name` 回填 | 后台展示名称 |
| `is_declared` | Boolean | 否 | `true` | 当前代码是否仍声明此权限 |
| `is_enabled` | Boolean | 否 | `true` | 管理员是否启用 |
| `disabled_at` | DateTime | 是 | `NULL` | 最近一次禁用时间 |
| `disabled_reason` | String(500) | 是 | `NULL` | 禁用原因 |
| `missing_at` | DateTime | 是 | `NULL` | 最近一次从代码中消失的时间 |
| `created_at` | DateTime | 否 | 当前时间 | 创建时间 |
| `updated_at` | DateTime | 否 | 当前时间 | 最近更新时间 |
| `version` | Integer | 否 | `1` | 乐观锁版本号 |

索引和约束：

- 保留 `name` 唯一索引；
- 保留 `id` 索引；
- 增加 `is_declared` 普通索引；
- 增加 `is_enabled` 普通索引；
- 不增加 `app_id`；
- 不修改 `role_permissions` 的外键和主键。

状态展示可以组合为：

```text
is_declared=true,  is_enabled=true  → active
is_declared=true,  is_enabled=false → disabled
is_declared=false, is_enabled=true  → missing
is_declared=false, is_enabled=false → missing + disabled
```

### 4.2 新增 `permission_endpoints` 绑定表

为了能明确回答“权限绑定了哪些 API”，增加同步生成的绑定快照：

```text
permission_endpoints
- permission_id
- http_method
- path
- route_name
```

建议约束：

```text
PRIMARY KEY (permission_id, http_method, path)
FOREIGN KEY permission_id → permissions.id ON DELETE CASCADE
INDEX (http_method, path)
```

说明：

- 一个权限可以绑定多个 API；
- 一个 API 可以声明多个权限；
- 表中内容只能由同步服务维护，后台不能手动编辑；
- 路由路径保存模板，例如 `/admin/apps/{app_id}`，不保存具体请求值；
- 同一权限、方法、路径重复扫描时必须去重。

实际鉴权仍使用 API 代码中的声明，数据库绑定表用于查询、审计和同步状态判断，不允许后台动态修改绑定以改变接口安全规则。

### 4.3 数据库迁移

新增迁移：

```text
alembic/versions/0005_permission_management.py
```

升级顺序：

1. 以可空形式增加已有数据需要回填的字段；
2. 既有权限回填：
   - `display_name=name`；
   - `is_declared=true`；
   - `is_enabled=true`；
   - `created_at=CURRENT_TIMESTAMP`；
   - `updated_at=CURRENT_TIMESTAMP`；
   - `version=1`；
3. 将必填字段收紧为非空；
4. 保留数据库默认值；
5. 创建状态索引；
6. 创建 `permission_endpoints` 表及索引；
7. 不修改权限 ID、`name`、`description` 和 `role_permissions` 数据。

降级顺序：

1. 删除 `permission_endpoints`；
2. 删除新增状态索引；
3. 删除新增字段；
4. 恢复 `0004_role_management` 时的权限结构。

迁移不得删除或重建现有权限记录。

---

## 5. API 权限声明设计

### 5.1 第一版继续使用字符串声明

第一版不新增 `PermissionSpec`，继续保持当前调用方式：

```python
@router.get("")
def list_apps(
    _actor: User = Depends(require_permissions("app:read")),
):
    ...
```

原因：

- 当前所有管理 API 已经使用字符串权限编码；
- 扫描权限只需要权限编码，不需要额外对象；
- 不必机械修改当前约 21 个权限和 30 多处依赖引用；
- 不扩大现有鉴权依赖、路由和测试的改动范围；
- JWT `scope`、Seed 迁移及权限数据库字段语义保持不变。

新权限首次同步时使用：

```text
name = 声明的权限编码
display_name = 声明的权限编码
description = ""
```

管理员之后可以在权限管理页面编辑 `display_name` 和 `description`，同步任务不会覆盖这些值。

### 5.2 给依赖函数附加可扫描元数据

`require_permissions()` 创建依赖函数时：

1. 校验传入的每个字符串权限编码；
2. 将权限编码元组继续传给现有鉴权服务；
3. 在依赖函数上附加只读的权限声明元数据，供扫描器读取。

示意：

```python
def require_permissions(*permissions: str) -> Callable[..., User]:
    validated_permissions = validate_permission_names(permissions)

    def dependency(...):
        return AuthorizationService(db).require_permissions(
            access_token,
            validated_permissions,
        )

    dependency.required_permissions = validated_permissions
    return dependency
```

实际实现需要为附加属性定义清晰类型，避免使用无类型的动态属性。扫描器只读取该元数据，不使用正则解析 Python 源码，也不依赖依赖函数名称。

### 5.3 `require_permissions()` 声明校验

为了在应用加载或扫描阶段尽早发现错误，`require_permissions()` 应对权限参数执行严格校验：

1. 至少传入一个权限编码；
2. 每个参数必须是字符串；
3. 去除首尾空白后不能为空；
4. 原始值不能包含首尾空白，不能静默修正错误编码；
5. 必须符合 `resource:action` 格式；
6. 只允许小写字母、数字和下划线；
7. `resource` 和 `action` 都必须以小写字母开头；
8. 只能包含一个冒号；
9. 编码长度不能超过数据库 `Permission.name` 的 128 字符限制；
10. 同一次声明中不能包含重复权限。

统一校验正则：

```regex
^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$
```

接受示例：

```python
require_permissions("app:read")
require_permissions("user:assign_roles")
require_permissions("app:read", "app:update")
```

拒绝示例：

```python
require_permissions()                         # 没有权限
require_permissions("")                       # 空字符串
require_permissions(" app:read")              # 首尾空白
require_permissions("App:Read")               # 大写字母
require_permissions("app read")               # 缺少冒号
require_permissions("app:read:detail")         # 多个冒号
require_permissions("app:read", "app:read")  # 重复权限
```

校验失败应抛出明确的配置异常，使应用加载、CI 路由扫描或同步命令直接失败，而不是等到用户请求接口时才发现。异常信息可以包含无效权限编码和错误原因，但不得包含 Token、请求凭证或其他敏感信息。

多权限继续保持现有 **AND 语义**：

```python
require_permissions("app:read", "app:update")
```

表示用户必须同时拥有 `app:read` 和 `app:update`。如果以后需要 OR 语义，应新增 `require_any_permission()`，不能改变现有函数行为。

对应测试至少覆盖：

- 合法单权限和多权限声明；
- 无参数、非字符串和空字符串；
- 首尾空白、大写、非法字符、冒号数量和超长编码；
- 重复权限；
- 校验失败时不生成可注册的依赖；
- 合法声明附加的 `required_permissions` 为规范、不可变的元组；
- 增加校验后，现有授权成功、401、403 和多权限 AND 行为无回归。

### 5.4 `PermissionSpec` 作为后续可选增强

如果以后确实需要在代码中统一维护中文默认名称和说明，可以再引入不可变声明对象：

```python
@dataclass(frozen=True)
class PermissionSpec:
    name: str
    default_display_name: str
    default_description: str = ""
```

届时应让 `require_permissions()` 同时兼容字符串和 `PermissionSpec`，逐步迁移，不能要求一次性修改所有路由。`frozen=True` 仅用于防止权限声明对象在运行时被意外修改，不是路由扫描和同步的必要条件。

### 5.5 扫描 FastAPI 已注册路由

扫描器以 `create_app()` 返回的 FastAPI 应用为入口：

1. 遍历所有 `APIRoute`；
2. 递归遍历路由的依赖树；
3. 读取 `require_permissions()` 附加的权限声明；
4. 为每个权限汇总 `method + path + route_name`；
5. 对结果排序和去重；
6. 输出确定性的扫描结果。

公开接口没有权限声明时不会进入权限目录，例如：

```text
/health
/auth/login
/auth/refresh
```

此外增加安全测试：除明确 allowlist 外，所有 `/admin` 路由必须至少声明一个权限，防止新管理 API 漏加鉴权。

---

## 6. 权限同步服务

新增 `PermissionSyncService`，在单一事务中完成同步。

### 6.1 同步流程

1. 扫描并完整校验所有 API 权限声明；
2. 计算声明权限、数据库权限和 API 绑定的差异计划；
3. 获取 PostgreSQL 事务级 advisory lock，重新读取并检查差异，防止多个部署任务并发同步；
4. 查询全部现有权限及绑定；
5. 数据库不存在的权限：
   - 创建 `Permission`；
   - `name` 使用声明编码；
   - `display_name` 默认等于 `name`；
   - `description` 默认为空字符串；
   - `is_declared=true`、`is_enabled=true`；
6. 数据库已存在且仍声明：
   - 设置 `is_declared=true`；
   - 清空 `missing_at`；
   - 不覆盖 `display_name`、`description`、`is_enabled`；
7. 数据库存在但扫描结果不存在：
   - 设置 `is_declared=false`；
   - 首次缺失时写入 `missing_at`；
   - 保留 `role_permissions`；
8. 用扫描结果同步 `permission_endpoints`；
9. 将新声明权限幂等关联到受保护的 `admin` 角色，保持当前系统“admin 拥有全部主应用权限”的行为；
10. 对权限有效集合发生变化而受影响的用户去重后撤销活跃 Session，包括禁用、废弃、恢复为有效状态和新增 admin 授权；
11. 对新建、废弃、恢复等实际状态变化递增 `version`，无变化时不递增；
12. 提交事务并输出同步摘要。

同步摘要至少包含：

```text
created
restored
marked_missing
endpoint_bindings_added
endpoint_bindings_removed
admin_grants_added
sessions_revoked
unchanged
```

### 6.2 同步计划与副作用边界

同步服务拆成两个步骤：

```text
build_plan() → 只读取路由和数据库，生成确定性差异计划
apply_plan() → 在事务和 advisory lock 下执行数据库、角色关联及 Session 变更
```

`--dry-run` 和 `--check` 只能调用 `build_plan()`：

- 不写数据库；
- 不写 Redis；
- 不撤销 Session；
- 不写管理员审计；
- 不依赖“最终回滚”消除外部副作用。

默认同步调用 `build_plan()` 后再调用 `apply_plan()`。数据库更新、绑定、角色关联和数据库 Session 状态必须处于同一事务；Redis 撤销写入不是数据库事务的一部分，若 Redis 写入失败，默认同步整体报错并停止部署，同时保持数据库事务不提交。需要为数据库已更新但进程在 Redis 写入后崩溃的极端窗口提供幂等重试验证。

### 6.3 幂等和失败规则

- 同一版本代码重复同步不增加权限、绑定或角色关联；
- 无实际状态变化时不递增 `version`；
- 管理员禁用的权限同步后仍保持禁用；
- 任一声明不合法时，在写数据库前整体失败；
- 任一数据库写入失败时整体回滚；
- 不允许出现部分权限已同步、部分未同步的状态；
- 同一同步计划重试必须安全；
- 唯一约束仍作为并发写入的最终保护。

### 6.4 同步命令

新增：

```text
python -m app.commands.sync_permissions
```

并增加 PDM 命令：

```bash
pdm run permission-sync
```

建议支持：

```bash
pdm run permission-sync --dry-run
pdm run permission-sync --check
```

语义：

- 默认：执行同步并提交；
- `--dry-run`：输出差异但回滚，不修改数据库；
- `--check`：只检查是否存在待同步差异，存在差异时返回非零退出码。

### 6.5 首次部署前的兼容性检查

当前代码基线中，管理路由共有 21 个不同的权限声明：App 6 个、User 10 个、Role 5 个；当前 Seed 则有 22 个权限。差异是 Seed 中存在 `user:write`，但现有管理 API 没有任何 `require_permissions("user:write")` 声明。

权限管理路由完成后将再增加：

```text
permission:read
permission:update
permission:disable
permission:enable
```

因此在没有其他路由变化的前提下，首次同步预计得到 25 个有效声明权限，并将历史 `user:write` 标记为 `missing`。这些数量应作为本次实施的基线断言；以后路由变化时应显式更新断言，而不是静默接受目录漂移。

正式应用迁移前必须确认：

1. 如果仍有 API 需要它，为对应 API 增加声明；
2. 如果已经不用，接受同步后将其标记为废弃；
3. 不允许未经 dry-run 审查直接在生产应用首次同步。

---

## 7. 后台权限管理规则

### 7.1 创建

不提供后台手动创建 API 权限。新权限必须先在 API 中声明，再由同步命令创建。

### 7.2 编辑

仅允许编辑：

- `display_name`；
- `description`；
- `version` 作为乐观锁请求字段。

禁止编辑：

- `id`；
- `name`；
- `is_declared`；
- API 绑定；
- 创建时间；
- 状态字段。

无实际变化时返回 `changed=false`，不递增版本、不写无意义审计。

### 7.3 禁用

使用明确目标状态接口：

```text
POST /admin/permissions/{permission_id}/disable
```

禁用规则：

- 使用行锁读取权限；
- 设置 `is_enabled=false`；
- 保存 `disabled_at` 和可选 `disabled_reason`；
- 递增 `version`；
- 保留 API 绑定和角色关联；
- 撤销通过角色拥有该权限的用户活跃 Session；
- 重复禁用返回 `changed=false`；
- 重复禁用不覆盖首次禁用时间和原因。

核心恢复权限 `permission:enable` 不允许被禁用，避免后台无法恢复其他权限。该保护规则与现有 `admin` 核心角色保护保持一致。

### 7.4 启用

```text
POST /admin/permissions/{permission_id}/enable
```

启用规则：

- 只有 `is_declared=true` 的权限可以启用；
- 设置 `is_enabled=true`；
- 清空禁用时间和原因；
- 递增 `version`；
- 重复启用返回 `changed=false`；
- 不自动恢复旧 Session，用户重新登录或刷新后取得最新 Scope。

废弃权限调用启用接口返回固定冲突错误，必须先由代码重新声明并同步。

### 7.5 废弃

不提供管理员手动废弃接口。废弃只能由同步结果触发：

```text
代码已无声明 → is_declared=false
```

废弃后：

- 不进入新 JWT `scope`；
- 请求鉴权时视为无效；
- 撤销受影响用户的活跃 Session；
- 不删除角色关联；
- 后台仍可查询其历史信息；
- 代码重新声明并同步后可以恢复。

---

## 8. Schema 设计

新增：

```text
app/schemas/admin_permission.py
```

请求 Schema 使用：

```python
ConfigDict(extra="forbid")
```

### 8.1 编辑请求

```text
AdminPermissionUpdate
- display_name: str，可选，1～128
- description: str，可选，最大 255
- version: int，必填且大于 0
```

处理规则：

- 文本去除首尾空白；
- 空白展示名称拒绝；
- 不允许提交 `name`、状态、时间和 API 绑定字段。

### 8.2 禁用请求

```text
AdminPermissionDisableRequest
- reason: str，可选，最大 500
```

### 8.3 响应

```text
AdminPermissionResponse
- id
- name
- display_name
- description
- resource
- action
- is_declared
- is_enabled
- disabled_at
- disabled_reason
- missing_at
- endpoint_count
- role_count
- created_at
- updated_at
- version
```

详情响应额外返回：

```text
endpoints
- http_method
- path
- route_name
```

动作响应额外返回：

```text
changed
revoked_sessions
```

---

## 9. Service 设计

### 9.1 `AdminPermissionService`

职责：

- 分页查询权限；
- 查询权限详情及 API 绑定；
- 编辑展示信息；
- 启用、禁用权限；
- 执行核心权限保护；
- 查询受权限影响的用户；
- 撤销 Session；
- 写入管理员操作审计。

写操作只执行业务修改、审计写入和必要的 `flush`，由 API 层统一 `commit/rollback`，与现有角色管理事务模式保持一致。

### 9.2 列表查询

支持：

```text
page
page_size
keyword
resource
is_declared
is_enabled
```

搜索字段：

- `name`；
- `display_name`；
- `description`。

稳定排序：

```text
name ASC, id ASC
```

### 9.3 编辑流程

1. 查询权限；
2. 校验请求 `version`；
3. 规范化展示名称和说明；
4. 计算实际变化；
5. 无变化返回幂等结果；
6. 使用 `WHERE id=? AND version=?` 乐观锁更新；
7. 写入 `permission.updated` 审计；
8. 由 API 提交事务。

编辑展示信息不影响鉴权，不撤销 Session。

### 9.4 禁用和启用流程

1. 使用 `SELECT ... FOR UPDATE` 锁定权限；
2. 校验权限存在；
3. 执行核心权限和废弃状态保护；
4. 已处于目标状态时返回幂等结果；
5. 修改状态元数据并递增版本；
6. 禁用时查询受影响用户并逐个撤销活跃 Session；
7. 写入状态变化审计；
8. 由 API 提交事务。

受影响用户查询链路：

```text
permission
    → role_permissions
    → 启用角色
    → user_roles
    → 用户
```

用户 ID 必须 `DISTINCT`，避免一个用户通过多个角色拥有同一权限时重复撤销。

---

## 10. 管理 API 设计

统一前缀：

```text
/admin/permissions
```

| 方法 | 路径 | 所需权限 | 用途 |
| --- | --- | --- | --- |
| GET | `/admin/permissions` | `permission:read` | 分页查询权限 |
| GET | `/admin/permissions/{permission_id}` | `permission:read` | 查询详情和 API 绑定 |
| PATCH | `/admin/permissions/{permission_id}` | `permission:update` | 编辑展示信息 |
| POST | `/admin/permissions/{permission_id}/disable` | `permission:disable` | 禁用权限 |
| POST | `/admin/permissions/{permission_id}/enable` | `permission:enable` | 启用权限 |

明确不提供：

```text
POST   /admin/permissions
DELETE /admin/permissions/{permission_id}
```

原因是 API 权限只能来自代码声明，且权限不能物理删除。

### 10.1 固定错误映射

| 领域错误 | HTTP 状态 | detail |
| --- | --- | --- |
| 权限不存在 | 404 | `PERMISSION_NOT_FOUND` |
| 权限版本冲突 | 409 | `PERMISSION_VERSION_CONFLICT` |
| 核心权限禁止操作 | 409 | `PROTECTED_PERMISSION_OPERATION` |
| 废弃权限不能启用 | 409 | `PERMISSION_NOT_DECLARED` |
| 请求字段错误 | 422 | Pydantic 标准错误 |

错误响应不得包含 SQL、约束名称、Token、Session ID 或内部堆栈。

---

## 11. 登录与鉴权调整

### 11.1 JWT Scope 生成

修改 `AuthService._get_user_permissions()`，除现有启用角色条件外，再增加：

```text
Permission.is_declared = true
Permission.is_enabled = true
```

完整条件：

```text
用户
    → user_roles
    → Role.is_enabled=true
    → role_permissions
    → Permission.is_declared=true
    → Permission.is_enabled=true
```

登录和 Refresh Token 都必须使用相同规则生成最新 `scope`。

### 11.2 请求时检查数据库权限状态

当前 `AuthorizationService` 只检查 JWT `scope`。为了保证权限禁用或废弃立即生效，增加数据库状态检查：

1. JWT `scope` 必须包含接口要求的全部权限编码；
2. 数据库中对应权限必须全部存在；
3. 对应权限必须同时 `is_declared=true`、`is_enabled=true`；
4. 任一条件不满足返回 403。

这样即使旧 Token 仍带有权限编码，也不能继续调用已禁用或废弃的 API。

### 11.3 Session 撤销

以下变化撤销受影响用户的活跃 Session：

- 权限被禁用；
- 权限被同步标记为废弃；
- 新权限自动关联给 `admin` 角色，导致管理员授权集合变化。

效果：

- 旧 Access Token 因 Session 已撤销返回 401；
- 旧 Refresh Token 不能继续刷新；
- 用户重新登录后取得最新 Scope；
- 重新启用或恢复权限不会复活旧 Session。

数据库状态检查和 Session 撤销同时保留：前者保证实时拒绝，后者清理旧授权会话并与现有角色管理安全模式一致。

---

## 12. Seed 与部署流程调整

### 12.1 Seed

当前 `DEFAULT_PERMISSIONS` 是手工权限目录。实现同步后，不能继续同时维护“路由声明”和 `DEFAULT_PERMISSIONS` 两份来源。

调整原则：

- API 权限目录以路由声明为准；
- 删除 Seed 中手工维护的 `DEFAULT_PERMISSIONS` 权限目录；
- Seed 只继续创建默认 admin 用户、admin 角色和用户角色关联；
- Seed 不隐式同步权限，初始化或部署流程必须在 Seed 后显式执行 `permission-sync`；
- 同步服务将所有主应用已声明权限幂等关联给 admin 角色；
- Seed 与同步命令分别重复执行，都不产生重复用户、角色、权限或关联。

这样权限声明只有路由代码一份来源，同时保留 Seed 和同步命令各自清晰、可审查的职责。

### 12.2 本地初始化

调整本地初始化顺序：

```text
alembic upgrade head
    → seed admin 用户和角色
    → permission-sync
    → 启动 API 和 nginx
```

### 12.3 正式部署

现有部署不自动执行 Seed，因此权限同步必须作为经过审核的独立部署步骤。同步命令依赖 `0005` 新增的字段和表，不能在迁移前连接旧结构执行数据库 dry-run。

推荐顺序：

```text
1. 在 CI 中执行纯路由扫描和声明完整性测试
2. alembic upgrade head
3. permission-sync --dry-run
4. 审查同步差异
5. permission-sync
6. permission-sync --check
7. 启动或滚动更新 API
```

`0005` 必须保持扩展式、向后兼容迁移，使旧版本 API 在“迁移完成、服务尚未滚动”的短暂阶段仍可运行。正式 API 新版本必须在同步成功后才启动；同步失败时停止部署，保持旧版本 API 运行。

新增权限可以按上述顺序先同步后滚动发布，因为旧版本只是不使用新权限。**移除权限声明必须采用两阶段发布**：

```text
阶段 A：先发布所有 Worker 都能识别权限状态字段、数据库实时检查和新路由逻辑的兼容版本
阶段 B：确认旧 Worker 已全部退出后，再用移除声明的新版本执行同步，将权限标记为 missing
```

不能在仍有旧 Worker 运行时提前把权限标记为 `missing`，也不能让旧 Worker 在同步后继续按旧查询发放包含废弃权限的 Token。部署健康检查应确认运行实例版本一致。

首次上线必须人工检查 `user:write` 等待废弃权限。后续正常部署可以自动执行幂等同步，但仍应保留同步摘要。

---

## 13. 审计设计

### 13.1 管理员操作审计

管理员操作写入现有 `AuditEvent`：

```text
permission.updated
permission.disabled
permission.enabled
```

统一：

```text
target_type = permission
target_id = permission.id
```

状态审计记录：

- 修改前后状态；
- 禁用原因；
- 撤销 Session 数量；
- Actor 用户；
- Request ID。

禁止记录：

- Access Token、Refresh Token；
- Authorization Header；
- Session ID；
- JWT 完整内容；
- 数据库凭证。

### 13.2 自动同步记录

现有 `AuditEvent.actor_user_id` 是非空用户外键，同步命令没有真实管理员用户，不能伪造 Actor。

因此本期自动同步使用结构化日志和命令摘要记录：

```text
permission.sync.created
permission.sync.restored
permission.sync.missing
permission.sync.completed
```

不向 `AuditEvent` 写入虚假的管理员 ID。未来如果系统增加 `actor_type=system`，再将自动同步纳入统一审计表。

---

## 14. 分阶段实施步骤

本模块采用五个阶段开发。每个阶段完成后先验证，再进入下一阶段。

### 第一阶段：权限数据层与迁移

开发内容：

1. 扩展 `Permission` 模型；
2. 新增 `permission_endpoints` 模型；
3. 新增 `0005_permission_management` 迁移；
4. 回填现有权限字段；
5. 保留现有 ID、名称和角色关联；
6. 增加模型和迁移测试；
7. 验证 upgrade、downgrade、upgrade。

本阶段不实现：

- 路由扫描；
- 同步命令；
- 管理 Schema、Service 和 API；
- 鉴权变更。

阶段验收：

- `id` 仍为整数自增主键；
- `name` 仍保存原权限编码；
- 现有 `role_permissions` 不丢失；
- 新字段默认值正确；
- ORM 与迁移结构一致；
- 迁移可安全往返。

### 第二阶段：权限声明与路由扫描器

开发内容：

1. 保留现有字符串权限声明方式；
2. 扩展 `require_permissions()` 校验权限编码并保存可扫描元数据；
3. 实现 FastAPI 路由依赖树扫描；
4. 校验权限编码和重复绑定；
5. 增加 `/admin` 路由鉴权覆盖检查；
6. 增加扫描器单元测试。

本阶段不引入 `PermissionSpec`，也不机械修改现有路由权限参数。

本阶段不写数据库。

阶段验收：

- 当前所有管理 API 权限均被准确扫描；
- 同一权限绑定多个 API 时正确聚合；
- 非法编码和冲突声明使扫描整体失败；
- 公开接口不会错误进入权限目录；
- 未声明权限的管理 API 被测试发现。

### 第三阶段：同步服务、命令和 Seed 集成

开发内容：

1. 实现 `PermissionSyncService`；
2. 实现创建、恢复、废弃和绑定同步；
3. 增加 PostgreSQL advisory lock；
4. 保留管理员编辑值和禁用状态；
5. 幂等关联 admin 角色；
6. 对授权变化撤销受影响 Session；
7. 实现默认、dry-run 和 check 命令；
8. 调整 Seed 和本地初始化流程；
9. 补充同步事务、幂等和并发测试。

阶段验收：

- 重复同步零差异；
- 禁用状态不被同步覆盖；
- 废弃和恢复不改变权限 ID 或角色关联；
- 并发同步串行化；
- 扫描或写入失败无部分数据；
- admin 新权限关联幂等。

### 第四阶段：管理 API 与鉴权接入

开发内容：

1. 新增严格管理 Schema；
2. 实现 `AdminPermissionService`；
3. 新增权限列表、详情、编辑、禁用和启用 API；
4. 注册管理路由；
5. 增加四项权限管理权限声明；
6. 调整 `AuthService` Scope 查询；
7. 调整 `AuthorizationService` 数据库状态检查；
8. 实现核心恢复权限保护；
9. 接入审计和 Session 撤销；
10. 补充 Schema、Service、API 和 Auth 测试。

阶段验收：

- 无 Token 返回 401；
- 权限不足返回 403；
- 各接口使用独立权限；
- 禁用或废弃权限立即失效；
- 旧 Session 被撤销；
- 不存在创建和删除 API；
- 错误码及 OpenAPI 稳定。

### 第五阶段：真实集成与完整回归

开发内容：

1. 完成模型、Schema、扫描器、同步、Service、API 和 Auth 全量测试；
2. 使用真实 PostgreSQL 验证迁移和同步并发；
3. 使用真实 Redis 验证 Session 撤销；
4. 使用真实 RS256 JWT 验证 Scope 和权限状态；
5. 验证权限创建、禁用、废弃、恢复和重新启用生命周期；
6. 验证 Seed、同步和 admin 授权幂等；
7. 执行敏感信息扫描；
8. 运行完整静态检查和回归测试。

阶段验收：

- 完整测试通过；
- 迁移可往返且保留已有权限和角色关联；
- 同步并发、幂等和回滚正确；
- 禁用和废弃使旧 Token/Session 失效；
- 恢复后必须重新登录取得最新 Scope；
- 用户、App、角色和认证功能无回归。

---

## 15. 严格测试清单

### 15.1 模型与迁移

1. `Permission.id` 为整数自增主键；
2. 新权限自动取得下一个 ID；
3. `name` 仍唯一且保存权限编码；
4. 既有权限 ID、名称和说明迁移后不变；
5. `display_name` 正确回填为 `name`；
6. `is_declared=true`、`is_enabled=true`、`version=1`；
7. 时间和状态字段可空性正确；
8. 状态索引存在且唯一性正确；
9. `permission_endpoints` 复合主键和外键正确；
10. 同一绑定不能重复写入；
11. `role_permissions` 数据不丢失；
12. downgrade 后恢复原结构且原权限仍存在；
13. 再次 upgrade 后数据仍一致；
14. `alembic check` 无结构漂移。

### 15.2 权限声明和扫描

1. 合法字符串权限编码可以声明且保留现有鉴权行为；
2. 无参数、非字符串、空字符串、首尾空白、大写、非法字符、缺少或存在多个冒号、超长编码被拒绝；
3. 同一次声明的重复权限被拒绝；
4. 校验失败时不生成可注册的依赖；
5. 合法声明的 `required_permissions` 是规范、不可变的元组；
6. 单个 API 单权限扫描正确；
7. 单个 API 多权限扫描正确；
8. 多权限继续使用 AND 语义；
9. 同一权限绑定多个 API 正确聚合；
10. 不同 HTTP Method 的同一路径分别保存；
11. 嵌套依赖和 Router 级依赖可以扫描；
12. 重复依赖不会产生重复绑定；
13. 扫描结果排序确定，重复运行结果一致；
14. `/health`、登录和刷新接口不进入权限目录；
15. 除 allowlist 外，每个 `/admin` API 都有权限声明；
16. 当前所有 `require_permissions` 权限均出现在扫描结果；
17. 附加的扫描元数据不会改变现有 401、403 和授权成功行为；
18. 新权限同步后的 `display_name=name` 且 `description=""`。

### 15.3 同步服务

1. 空数据库首次同步创建全部声明权限；
2. 新权限默认使用 `display_name=name` 和 `description=""`；
3. 重复同步不创建重复权限；
4. 重复同步不递增无变化权限版本；
5. 已编辑的展示信息不会被同步覆盖；
6. 管理员禁用状态不会被同步恢复；
7. 未声明权限设置 `is_declared=false` 和 `missing_at`；
8. 重复缺失不覆盖首次 `missing_at`；
9. 重新声明后恢复且清空 `missing_at`；
10. 恢复保持原权限 ID；
11. 恢复保持原 `role_permissions`；
12. 绑定新增、删除和去重正确；
13. admin 自动授权幂等；
14. 不删除废弃权限的 admin 关联；
15. 权限失效时受影响用户去重撤销 Session；
16. 新增 admin 授权后管理员旧 Session 被撤销；
17. dry-run 输出差异且数据库、Redis、Session 和审计均无变化；
18. check 在无差异时退出 0，有差异时非 0，且无任何写入副作用；
19. 非法声明在数据库写入前失败；
20. 写入异常使权限、绑定和关联整体回滚；
21. Redis 撤销失败时数据库事务不提交并返回非零退出码；
22. 同一同步计划失败重试后结果一致且无重复副作用；
23. 恢复为有效权限时撤销因授权集合变化而受影响的旧 Session；
24. 两个真实 PostgreSQL 并发同步通过 advisory lock 串行执行；
25. 并发完成后没有重复权限和绑定。

### 15.4 权限查询与编辑

1. 列表分页正确；
2. 编码、展示名称和说明关键词搜索正确；
3. resource、声明状态和启用状态筛选正确；
4. 排序稳定；
5. 详情返回完整 API 绑定；
6. 不存在返回 `PERMISSION_NOT_FOUND`；
7. 编辑规范化展示名称和说明；
8. 空白和超长字段返回 422；
9. extra 字段返回 422；
10. 不能修改 `name` 和状态字段；
11. 正确版本更新成功；
12. 旧版本返回 `PERMISSION_VERSION_CONFLICT`；
13. 无变化返回 `changed=false`；
14. 无变化不递增版本、不写审计；
15. 编辑展示信息不撤销 Session。

### 15.5 启用与禁用

1. 普通已声明权限可以禁用；
2. 禁用记录时间和原因；
3. 禁用递增版本；
4. 重复禁用幂等；
5. 重复禁用不覆盖首次原因；
6. 禁用保留 API 和角色关联；
7. 禁用撤销全部受影响用户 Session；
8. 同一用户通过多个角色拥有权限时只处理一次；
9. 禁用权限不进入新 JWT Scope；
10. `permission:enable` 不能禁用；
11. 普通已声明权限可以重新启用；
12. 启用清空禁用元数据；
13. 重复启用幂等；
14. 废弃权限不能启用；
15. 重新启用不恢复旧 Session；
16. 有效状态变化写入正确审计。

### 15.6 废弃与恢复

1. API 删除声明后权限自动废弃；
2. 废弃清空该权限的 API 绑定快照；
3. 废弃保留权限记录和角色关联；
4. 废弃权限不进入新 JWT Scope；
5. 旧 Token 即使含编码也被数据库状态检查拒绝；
6. 废弃撤销受影响用户 Access/Refresh Session；
7. 相同编码重新声明后恢复原 ID；
8. 恢复重建 API 绑定；
9. 恢复不覆盖管理员禁用状态；
10. 恢复为有效权限时撤销授权集合已变化用户的旧 Session；
11. 恢复不自动复活旧 Session；
12. 重新登录后仅在已声明且已启用时重新获得权限。

### 15.7 API 与授权边界

1. 所有权限管理 API 要求 Bearer Token；
2. 无 Token 返回 401；
3. 已认证但权限不足返回 403；
4. `permission:read` 只能查询；
5. `permission:update` 只能编辑展示信息；
6. `permission:disable` 只能禁用；
7. `permission:enable` 只能启用；
8. 错误单权限 Token 调用目标接口返回 403；
9. 不存在 POST 创建接口；
10. 不存在 DELETE 接口；
11. 404、409、422 固定错误稳定；
12. OpenAPI 包含正确路径、Security 和请求响应模型；
13. 响应不包含 Token、Session 或内部字段。

### 15.8 Auth、JWT 与 Session

1. 登录只加载启用角色；
2. 登录只加载已声明且启用权限；
3. Refresh 使用相同过滤规则；
4. 数据库权限不存在时，即使 JWT Scope 包含也返回 403；
5. 数据库权限禁用时，即使 JWT Scope 包含也返回 403；
6. 数据库权限废弃时，即使 JWT Scope 包含也返回 403；
7. 禁用和废弃后 PostgreSQL Session 为 revoked；
8. Redis Session Key 为 revoked 且 TTL 合法；
9. 旧 Access Token 返回 401；
10. 旧 Refresh Token 返回 401；
11. 重新登录后 Scope 与数据库有效权限完全一致；
12. 重新启用不会自动恢复旧 Session。

### 15.9 审计、日志和敏感信息

1. 编辑写入 `permission.updated`；
2. 禁用写入 `permission.disabled`；
3. 启用写入 `permission.enabled`；
4. Actor、Target、Request ID 和变化前后值正确；
5. 幂等操作不产生额外有效审计；
6. 自动同步不伪造 `actor_user_id`；
7. 同步日志包含计数但不包含敏感数据；
8. 响应、审计、异常和日志不包含 Access/Refresh Token；
9. 不记录 Authorization Header、Session ID、JWT 或数据库密码。

### 15.10 回归测试

1. 当前用户管理 API 无回归；
2. 当前 App 管理 API 无回归；
3. 当前角色管理 API 无回归；
4. 用户角色分配无回归；
5. 现有 admin 登录和刷新无回归；
6. 现有 Seed 重复执行保持幂等；
7. 角色禁用过滤仍然生效；
8. 完整测试套件和静态检查通过。

---

## 16. 真实集成验证方案

新增：

```text
scripts/validate_permission_management_phase_5.py
tests/test_permission_management_phase_5_integration.py
```

增加命令：

```bash
pdm run permission-phase5-validate
```

真实集成 pytest 默认跳过，仅在显式环境变量开启时运行，避免日常测试依赖 PostgreSQL 和 Redis。

### 16.1 临时基础设施安全边界

参考角色管理验证器：

- 使用独立临时 PostgreSQL 数据库；
- 使用独立 Redis DB 和随机 Key 前缀；
- 默认只允许 localhost；
- 默认拒绝 PostgreSQL 5432 和 Redis 6379；
- 不复用开发或生产数据卷；
- 只删除本次创建的数据库；
- Redis 使用 `SCAN + DELETE` 清理随机前缀；
- 禁止 `FLUSHDB` 和 `FLUSHALL`；
- 验证结束后检查数据库和 Redis 无测试残留。

### 16.2 真实迁移往返

```text
0004_role_management
    → 0005_permission_management
    → 0004_role_management
    → 0005_permission_management
```

升级前插入：

- 既有权限及明确 ID；
- 一个角色；
- 一条 `role_permissions` 关联。

升级后验证新增字段、索引、默认值、自增序列及关联保留；降级后验证原结构和数据仍存在；再次升级后执行 `alembic check`。

### 16.3 真实同步并发

两个独立 SQLAlchemy Session 同时执行权限同步：

- 第二个事务确实等待 PostgreSQL advisory lock；
- 第一个完成后第二个继续；
- 最终权限、绑定、admin 关联无重复；
- 第二次结果为无差异；
- 数据库约束和事务状态一致。

### 16.4 真实 HTTP、JWT 和 Redis 生命周期

使用动态生成的 2048 位 RSA 密钥、临时 PostgreSQL、独立 Redis 前缀和 Uvicorn 子进程验证：

```text
同步权限
    → Seed admin
    → 登录取得 JWT
    → 查询权限及 API 绑定
    → 编辑展示信息
    → 禁用普通权限
    → 验证旧 Access/Refresh Token 失效
    → 重新登录确认 Scope 排除该权限
    → 重新启用
    → 重新登录确认 Scope 恢复
    → 使用变更后的测试路由清单同步废弃
    → 验证旧 Token 和数据库状态拒绝
    → 恢复声明并同步
    → 重新登录确认原 ID 和角色关联继续有效
```

同时为四项权限管理权限创建独立最小权限用户，验证每个 API 的 401、403 和正确单权限边界。

---

## 17. 预计修改文件

预计修改：

```text
app/models/permission.py
app/models/__init__.py
app/api/dependencies.py
app/services/auth_service.py
app/services/authorization_service.py
app/seed/__main__.py
app/main.py
alembic/env.py
scripts/init_local.py
pyproject.toml
README.md
```

预计新增：

```text
alembic/versions/0005_permission_management.py
app/models/permission_endpoint.py
app/services/permission_scanner.py
app/services/permission_sync_service.py
app/services/admin_permission_service.py
app/schemas/admin_permission.py
app/api/admin_permissions.py
app/commands/__init__.py
app/commands/sync_permissions.py
scripts/validate_permission_management_phase_5.py
tests/test_permission_management_models.py
tests/test_permission_scanner.py
tests/test_permission_sync_service.py
tests/test_admin_permission_service.py
tests/test_admin_permissions_api.py
tests/test_permission_management_phase_5_integration.py
```

预计扩展测试：

```text
tests/test_api_dependencies.py
tests/test_auth_service.py
tests/test_authorization_service.py
tests/test_seed.py
tests/test_init_local.py
```

---

## 18. 验证命令

每个阶段先运行定向测试，再运行完整检查：

```bash
pdm run lint
pdm run test
```

权限同步验证：

```bash
pdm run permission-sync --dry-run
pdm run permission-sync
pdm run permission-sync --check
```

迁移验证：

```bash
pdm run migrate
pdm run alembic-current
alembic check
```

真实集成验证：

```bash
pdm run permission-phase5-validate
```

真实迁移 downgrade 必须使用独立临时 PostgreSQL 数据库，不得在生产数据库或未隔离的开发数据库执行。

---

## 19. 验收标准

满足以下条件后，本期权限管理模块可以验收：

- 权限继续使用整数自增 `id`；
- 现有 `name` 继续作为稳定权限编码，不改成 `code`；
- 本期不引入 `app_id`；
- 所有主应用管理 API 都通过代码声明权限；
- 同步结果可查询每项权限绑定的 Method 和 Path；
- 新声明权限自动创建，重复同步幂等；
- 代码移除声明后权限自动废弃但不删除；
- 恢复声明后继续使用原 ID 和角色关联；
- 管理员可以编辑展示信息、启用和禁用权限；
- 不提供手动创建、编码编辑和物理删除 API；
- 管理员禁用状态不会被同步覆盖；
- 禁用和废弃权限不会进入 JWT Scope，也不能通过旧 Scope 访问；
- 相关用户 Session 被撤销，旧 Access/Refresh Token 失效；
- 所有有效管理员变更有安全审计；
- 自动同步不伪造管理员 Actor；
- 迁移、同步并发、真实 PostgreSQL、Redis 和 RS256 JWT 验证通过；
- 用户、App、角色、认证和 Seed 完整回归通过；
- 首次生产同步前已明确处理现有 `user:write` 漂移。
