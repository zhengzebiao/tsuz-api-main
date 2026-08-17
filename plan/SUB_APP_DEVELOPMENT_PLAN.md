# 子应用管理 App 开发方案

## 1. 文档目标

本文档描述主应用中“子应用管理 App”模块的开发方案。

本期只实现需求中标记为 ✅ 的能力：

1. 新增子应用；
2. 编辑子应用信息；
3. 启用、禁用子应用；
4. 配置应用名称、图标、访问地址、服务账号；
5. 重新生成 App Secret。

列表和详情查询虽然不是单独的业务操作，但它们是管理页面完成上述功能所必需的基础接口，因此一并实现。

本期不实现：

- 删除子应用；
- 应用编码管理；
- 菜单管理；
- OAuth 回调地址；
- Token Scope；
- 权限范围配置；
- 子应用访问状态监控；
- Secret 双密钥过渡、吊销列表等完整密钥轮换机制。

---

## 2. 现有项目适配原则

实现继续沿用项目当前的技术和分层方式：

- FastAPI 提供 HTTP API；
- Pydantic 定义请求和响应模型；
- SQLAlchemy 定义数据模型并访问数据库；
- Alembic 管理数据库迁移；
- Service 层承载业务规则、事务、锁和审计；
- 使用现有认证和权限依赖保护管理接口；
- 使用现有 `AuditEvent` 模型记录重要操作；
- 编辑操作采用乐观锁；
- 状态变更和 Secret 重新生成采用数据库行锁；
- 数据修改与审计记录在同一事务内提交。

模块调用关系：

```text
Admin Apps API
  → Admin App Schemas
  → AdminAppService
  → App / AuditEvent Models
  → PostgreSQL
```

---

## 3. 核心设计

### 3.1 App ID

`app_id` 是子应用对外公开的唯一身份标识。

设计规则：

- 创建子应用时由后端自动生成；
- 建议格式为 `app_<32位随机十六进制字符串>`；
- 创建后不可修改；
- 数据库中建立唯一约束；
- 极小概率发生冲突时由服务端重新生成，不直接把数据库唯一约束错误返回给客户端；
- 管理接口使用数据库内部主键 `id` 定位资源，对外鉴权场景使用 `app_id`。

示例：

```text
app_2d92f64361ea4e249f5c9a0de38bc092
```

### 3.2 App Secret

`app_secret` 是子应用服务主体的凭证。

设计规则：

- 创建子应用时由后端使用安全随机数自动生成；
- 推荐使用 Python 标准库 `secrets.token_urlsafe(32)`；
- 数据库只保存 `app_secret_hash`，不保存明文；
- 明文 Secret 只在以下响应中展示一次：
  - 创建子应用成功；
  - 重新生成 Secret 成功；
- 列表、详情、编辑和状态接口均不得返回 Secret 或 Secret Hash；
- Secret 不得写入普通日志、异常信息和审计详情；
- Secret 响应添加 `Cache-Control: no-store`；
- 重新生成后旧 Secret 立即失效。

App Secret 是高熵随机值，可复用 `app/core/security.py` 中的 SHA-256 工具保存哈希。后续实现子应用身份校验时，应使用常量时间比较方式校验哈希。

### 3.3 服务账号

本期采用一对一的简化模型：

- 每个子应用本身就是一个非人工服务主体；
- `app_id + app_secret` 是该服务主体的凭证；
- `service_account_name` 是服务账号的可编辑展示名称；
- 不在 `users` 表中创建虚假用户；
- 不复用普通用户账号；
- 子应用被禁用后，其服务主体也应被视为不可用。

如果未来一个子应用需要多个服务账号，再单独增加 `service_accounts` 表及对应管理模块。

### 3.4 启用与禁用

状态接口采用“设置目标状态”的语义，不提供 `toggle` 接口。

原因：

- `enable`、`disable` 的意图明确；
- 接口容易实现幂等；
- 网络重试不会意外翻转为相反状态；
- 更便于审计。

禁用规则：

- 设置 `is_enabled=false`；
- 记录 `disabled_at`；
- 记录可选的 `disabled_reason`；
- 重复禁用返回 `changed=false`；
- 重复禁用不覆盖已有禁用时间和原因。

启用规则：

- 设置 `is_enabled=true`；
- 清空 `disabled_at` 和 `disabled_reason`；
- 已启用时重复调用返回 `changed=false`。

---

## 4. 数据库设计

### 4.1 `apps` 表

| 字段 | 类型 | 是否为空 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | Integer | 否 | 自增 | 数据库内部主键 |
| `app_id` | String(64) | 否 | 后端生成 | 公开 App ID，唯一且不可修改 |
| `app_secret_hash` | String(64) | 否 | 后端生成 | Secret 的 SHA-256 哈希 |
| `name` | String(128) | 否 | 无 | 应用名称 |
| `icon_url` | String(2048) | 是 | `NULL` | 应用图标地址 |
| `access_url` | String(2048) | 否 | 无 | 子应用访问地址，仅允许 HTTP/HTTPS |
| `service_account_name` | String(128) | 否 | 无 | 服务账号展示名称 |
| `is_enabled` | Boolean | 否 | `true` | 是否启用 |
| `disabled_at` | DateTime(timezone=True) | 是 | `NULL` | 最近一次禁用时间 |
| `disabled_reason` | String(500) | 是 | `NULL` | 禁用原因 |
| `secret_updated_at` | DateTime(timezone=True) | 否 | 当前时间 | Secret 最近生成时间 |
| `created_at` | DateTime(timezone=True) | 否 | 当前时间 | 创建时间 |
| `updated_at` | DateTime(timezone=True) | 否 | 当前时间 | 最近修改时间 |
| `version` | Integer | 否 | `1` | 乐观锁版本号 |

### 4.2 索引与约束

需要增加：

- `app_id` 唯一索引或唯一约束；
- `name` 普通索引，用于名称搜索；
- `is_enabled` 普通索引，用于状态筛选；
- `version >= 1` 检查约束可按项目现有约定决定是否添加。

不需要为 `app_secret_hash` 建立公开查询索引。后续凭证校验应先通过 `app_id` 查询 App，再校验 Secret。

### 4.3 数据库迁移

建议新增迁移文件：

```text
alembic/versions/0003_app_management.py
```

迁移内容：

1. 创建 `apps` 表；
2. 创建唯一约束和必要索引；
3. `downgrade` 中按相反顺序移除索引和表。

同时在 `alembic/env.py` 中导入新的 App 模型，使 Alembic 能获取完整的 `Base.metadata`。

---

## 5. 数据模型

建议新增：

```text
app/models/app.py
```

模型名称建议使用 `App`，表名使用 `apps`。

关键模型约束：

- `app_id` 不提供普通业务更新入口；
- `app_secret_hash` 不进入任何 API 响应模型；
- 时间字段统一使用项目当前的 UTC/时区处理约定；
- `version` 每次有效修改后递增；
- 启用、禁用和重新生成 Secret 都属于有效修改，应更新 `updated_at` 和 `version`。

---

## 6. Schema 设计

建议新增：

```text
app/schemas/admin_app.py
```

### 6.1 创建请求 `AdminAppCreate`

字段：

```json
{
  "name": "项目管理",
  "icon_url": "https://static.example.com/project.png",
  "access_url": "https://project.example.com",
  "service_account_name": "项目管理服务账号"
}
```

校验规则：

- `name`：去除首尾空格后不能为空，最大 128 字符；
- `icon_url`：可为空；不为空时必须是 HTTP/HTTPS URL，最大 2048 字符；
- `access_url`：必须是 HTTP/HTTPS URL，最大 2048 字符；
- `service_account_name`：去除首尾空格后不能为空，最大 128 字符。

### 6.2 编辑请求 `AdminAppUpdate`

允许字段：

- `name`；
- `icon_url`；
- `access_url`；
- `service_account_name`；
- `version`。

请求示例：

```json
{
  "name": "新项目管理",
  "version": 1
}
```

不允许编辑：

- `app_id`；
- `app_secret`；
- `app_secret_hash`；
- `is_enabled`；
- 系统时间字段。

### 6.3 禁用请求 `AdminAppDisableRequest`

```json
{
  "reason": "子应用暂停维护"
}
```

`reason` 可选；不为空时去除首尾空格，最大 500 字符。

### 6.4 Secret 重新生成请求 `AdminAppRegenerateSecretRequest`

```json
{
  "reason": "原 Secret 可能泄露"
}
```

建议要求填写原因，便于审计。最大 500 字符。

### 6.5 普通响应 `AdminAppResponse`

返回字段：

- `id`；
- `app_id`；
- `name`；
- `icon_url`；
- `access_url`；
- `service_account_name`；
- `is_enabled`；
- `disabled_at`；
- `disabled_reason`；
- `secret_updated_at`；
- `created_at`；
- `updated_at`；
- `version`。

不得返回：

- `app_secret`；
- `app_secret_hash`。

### 6.6 创建响应 `AdminAppCreateResponse`

```json
{
  "app": {
    "id": 1,
    "app_id": "app_2d92f64361ea4e249f5c9a0de38bc092",
    "name": "项目管理",
    "icon_url": "https://static.example.com/project.png",
    "access_url": "https://project.example.com",
    "service_account_name": "项目管理服务账号",
    "is_enabled": true,
    "version": 1
  },
  "app_secret": "app_secret_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
}
```

### 6.7 Secret 重新生成响应 `AdminAppSecretResponse`

```json
{
  "app_id": "app_2d92f64361ea4e249f5c9a0de38bc092",
  "app_secret": "app_secret_yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy",
  "secret_updated_at": "2026-08-01T10:00:00Z"
}
```

---

## 7. API 设计

管理路由统一使用：

```text
/admin/apps
```

### 7.1 子应用列表

```http
GET /admin/apps?page=1&page_size=20&keyword=project&is_enabled=true
```

功能：

- 分页；
- 按应用名称或 App ID 模糊搜索；
- 按启用状态筛选；
- 使用稳定排序，建议按 `created_at DESC, id DESC`。

权限：

```text
app:read
```

响应不得包含 Secret 或 Secret Hash。

### 7.2 子应用详情

```http
GET /admin/apps/{id}
```

权限：

```text
app:read
```

App 不存在时返回：

```text
404 APP_NOT_FOUND
```

### 7.3 新增子应用

```http
POST /admin/apps
```

权限：

```text
app:create
```

处理流程：

1. 校验请求字段；
2. 自动生成唯一 `app_id`；
3. 自动生成高强度 `app_secret`；
4. 计算 Secret Hash；
5. 保存子应用；
6. 创建 `app.created` 审计记录；
7. 在同一事务中提交；
8. 在响应中一次性返回明文 Secret；
9. 添加 `Cache-Control: no-store` 响应头。

### 7.4 编辑子应用

```http
PATCH /admin/apps/{id}
```

权限：

```text
app:update
```

允许修改：

- 应用名称；
- 图标 URL；
- 访问地址；
- 服务账号展示名称。

并发控制：

- 请求必须携带 `version`；
- 更新条件包含当前版本号；
- 成功后 `version + 1`；
- 版本不一致返回 `409 APP_VERSION_CONFLICT`。

如果请求中没有任何有效变更，可返回当前数据且不增加版本，也不创建无意义审计记录。

### 7.5 禁用子应用

```http
POST /admin/apps/{id}/disable
```

权限：

```text
app:disable
```

处理规则：

- 使用 `SELECT ... FOR UPDATE` 锁定 App；
- App 当前已禁用时返回 `changed=false`；
- 首次禁用时设置状态、时间和原因；
- 创建 `app.disabled` 审计记录；
- 提交后该 App 凭证应立即被视为不可用。

响应示例：

```json
{
  "app": {
    "id": 1,
    "app_id": "app_2d92f64361ea4e249f5c9a0de38bc092",
    "is_enabled": false,
    "version": 2
  },
  "changed": true
}
```

### 7.6 启用子应用

```http
POST /admin/apps/{id}/enable
```

权限：

```text
app:enable
```

处理规则：

- 使用行锁；
- 已启用时返回 `changed=false`；
- 启用时清空 `disabled_at` 和 `disabled_reason`；
- 创建 `app.enabled` 审计记录。

### 7.7 重新生成 App Secret

```http
POST /admin/apps/{id}/regenerate-secret
```

权限：

```text
app:regenerate_secret
```

处理流程：

1. 使用 `SELECT ... FOR UPDATE` 锁定 App；
2. 生成新的高强度 Secret；
3. 计算新 Hash；
4. 使用新 Hash 覆盖旧 Hash；
5. 更新 `secret_updated_at`、`updated_at` 和 `version`；
6. 创建 `app.secret_regenerated` 审计记录；
7. 审计中只记录 Secret 已变更及操作原因，不记录 Secret 或 Hash；
8. 提交后旧 Secret 立即失效；
9. 响应只展示一次新 Secret；
10. 添加 `Cache-Control: no-store` 响应头。

该接口不是幂等接口。如果服务端已提交但客户端丢失响应，明文 Secret 无法恢复，管理员只能再次生成。

---

## 8. Service 层设计

建议新增：

```text
app/services/admin_app_service.py
```

服务类建议命名：

```text
AdminAppService
```

建议提供以下方法：

```text
list_apps(...)
get_app(...)
create_app(...)
update_app(...)
disable_app(...)
enable_app(...)
regenerate_secret(...)
```

### 8.1 事务规则

- Service 方法只负责修改当前事务中的数据；
- 路由或项目现有统一事务边界负责提交和回滚；
- 数据修改与对应审计事件必须处于同一事务；
- 任一环节失败时整体回滚。

### 8.2 并发规则

普通资料编辑：

- 使用 `version` 乐观锁；
- 避免两个管理员的编辑互相覆盖。

启用、禁用、重新生成 Secret：

- 使用数据库行锁；
- 确保状态判断和修改是一个原子过程；
- 避免并发重新生成 Secret 导致返回值与数据库最终状态不一致。

### 8.3 敏感数据规则

以下内容不得进入日志和审计记录：

- 明文 App Secret；
- Secret Hash；
- 完整认证请求头；
- 包含 App Secret 的请求体。

日志可以记录：

- 数据库 App 主键；
- App ID；
- 操作类型；
- 操作结果；
- 管理员用户 ID。

---

## 9. 权限设计

在 `app/seed/__main__.py` 中增加：

| 权限 | 说明 |
| --- | --- |
| `app:read` | 查看子应用列表和详情 |
| `app:create` | 创建子应用 |
| `app:update` | 编辑子应用基本信息 |
| `app:enable` | 启用子应用 |
| `app:disable` | 禁用子应用 |
| `app:regenerate_secret` | 重新生成 App Secret |

本期默认将上述权限分配给现有 `admin` 角色。

Seed 必须保持幂等：

- 权限已存在时不重复创建；
- 角色已关联权限时不重复关联；
- 重复运行不会报错或产生重复数据。

---

## 10. 审计设计

复用现有 `AuditEvent` 模型，新增以下事件类型：

```text
app.created
app.updated
app.enabled
app.disabled
app.secret_regenerated
```

统一字段建议：

- `target_type="app"`；
- `target_id` 使用 `apps.id`；
- Actor 使用当前管理员用户；
- 记录请求上下文中已有的 IP、User-Agent 等信息；
- 记录操作前后必要的非敏感变化。

### 10.1 创建审计

记录：

- App ID；
- 应用名称；
- 访问地址；
- 服务账号名称；
- 初始启用状态。

不记录：

- Secret；
- Secret Hash。

### 10.2 编辑审计

只记录发生变化的字段，例如：

```json
{
  "changes": {
    "name": {
      "before": "项目管理",
      "after": "新项目管理"
    }
  }
}
```

### 10.3 状态审计

禁用时记录：

- `before=true`；
- `after=false`；
- 禁用原因。

启用时记录：

- `before=false`；
- `after=true`。

重复调用且 `changed=false` 时，默认不创建新的状态变更审计事件。

### 10.4 Secret 审计

只记录：

```json
{
  "secret_changed": true,
  "reason": "原 Secret 可能泄露"
}
```

严禁记录明文 Secret、新旧 Hash 或可用于还原凭证的信息。

---

## 11. 错误码设计

| HTTP 状态 | 错误码 | 场景 |
| --- | --- | --- |
| 401 | 项目现有认证错误码 | 未登录或 Token 无效 |
| 403 | 项目现有权限错误码 | 当前用户缺少所需权限 |
| 404 | `APP_NOT_FOUND` | 指定 App 不存在 |
| 409 | `APP_VERSION_CONFLICT` | 编辑时版本号冲突 |
| 422 | FastAPI 校验错误 | 名称为空、字段过长或 URL 不合法 |

数据库异常不直接暴露给客户端。唯一约束冲突、SQL 详情和内部堆栈只进入受控服务端日志。

---

## 12. 代码文件规划

建议增加或修改以下文件：

| 文件 | 操作 | 作用 |
| --- | --- | --- |
| `app/models/app.py` | 新增 | 定义 `App` 数据库模型 |
| `app/schemas/admin_app.py` | 新增 | 定义创建、编辑、状态、Secret 和响应 Schema |
| `app/services/admin_app_service.py` | 新增 | 实现业务逻辑、事务、并发控制和审计 |
| `app/api/admin_apps.py` | 新增 | 实现 `/admin/apps` 管理接口 |
| `app/core/security.py` | 修改 | 增加 App ID 和 App Secret 生成函数 |
| `app/main.py` | 修改 | 注册 `admin_apps_router` |
| `app/seed/__main__.py` | 修改 | 初始化 App 管理权限并关联 admin 角色 |
| `alembic/env.py` | 修改 | 导入 App 模型元数据 |
| `alembic/versions/0003_app_management.py` | 新增 | 创建 `apps` 表及索引 |

---

## 13. 测试方案

建议新增：

```text
tests/test_app_management_models.py
tests/test_admin_app_service.py
tests/test_admin_apps_api.py
```

### 13.1 模型与安全测试

1. 创建 App 时生成符合格式的 App ID；
2. 多次生成的 App ID 不重复；
3. Secret 具有足够随机长度；
4. 数据库只保存 Secret Hash；
5. 数据库中的 Hash 可以匹配创建时返回的 Secret；
6. 模型默认处于启用状态；
7. `version` 默认值为 1。

### 13.2 创建接口测试

1. 具有 `app:create` 权限时可以创建；
2. 自动生成 App ID 和 Secret；
3. 创建响应返回一次明文 Secret；
4. 响应包含 `Cache-Control: no-store`；
5. 审计记录中不包含 Secret 或 Hash；
6. URL 非 HTTP/HTTPS 时返回 422；
7. 无 Token 返回 401；
8. 权限不足返回 403。

### 13.3 查询接口测试

1. 列表支持分页；
2. 支持按名称和 App ID 搜索；
3. 支持按启用状态筛选；
4. 详情不存在时返回 `APP_NOT_FOUND`；
5. 列表和详情响应不含 Secret；
6. 列表和详情响应不含 Secret Hash。

### 13.4 编辑接口测试

1. 可以修改名称、图标、访问地址和服务账号名称；
2. 不能修改 App ID；
3. 不能通过编辑接口修改 Secret；
4. 不能通过编辑接口修改启用状态；
5. 成功编辑后版本号递增；
6. 旧版本编辑返回 `409 APP_VERSION_CONFLICT`；
7. 无实际变化时不产生无意义审计事件。

### 13.5 启用和禁用测试

1. 启用状态的 App 可以被禁用；
2. 禁用时记录时间和原因；
3. 重复禁用返回 `changed=false`；
4. 重复禁用不覆盖原禁用时间和原因；
5. 禁用状态的 App 可以被启用；
6. 启用后清空禁用时间和原因；
7. 重复启用返回 `changed=false`；
8. 有效状态变化会递增版本号；
9. 有效状态变化会写入正确审计事件。

### 13.6 Secret 重新生成测试

1. 可以生成新的 Secret；
2. 新 Secret 与旧 Secret 不同；
3. 数据库 Hash 发生变化；
4. 旧 Secret 不再匹配；
5. 新 Secret 可以匹配数据库 Hash；
6. `secret_updated_at` 被更新；
7. `version` 被递增；
8. 响应包含 `Cache-Control: no-store`；
9. 普通详情接口无法再次获取明文 Secret；
10. 审计只记录 `secret_changed=true` 和原因；
11. 审计、日志和异常信息均不包含新旧 Secret。

### 13.7 集成流程测试

覆盖完整生命周期：

```text
创建
  → 列表查询
  → 详情查询
  → 编辑基本信息
  → 禁用
  → 重复禁用
  → 启用
  → 重新生成 Secret
  → 验证旧 Secret 失效
```

---

## 14. 实施顺序

### 第一阶段：数据层

1. 新增 `App` 模型；
2. 在 Alembic 环境中注册模型；
3. 创建数据库迁移；
4. 验证升级和降级迁移。

### 第二阶段：安全与 Schema

1. 增加 App ID 生成函数；
2. 增加 App Secret 生成函数；
3. 定义请求和响应 Schema；
4. 确保普通响应从类型层面排除敏感字段。

### 第三阶段：业务服务

1. 实现查询和创建；
2. 实现带乐观锁的编辑；
3. 实现带行锁的启用和禁用；
4. 实现带行锁的 Secret 重新生成；
5. 接入审计记录。

### 第四阶段：API 与权限

1. 实现管理路由；
2. 注册路由；
3. 添加权限 Seed；
4. 为 Secret 响应设置禁止缓存响应头。

### 第五阶段：测试与验证

1. 完成模型和 Service 单元测试；
2. 完成 API 权限和错误场景测试；
3. 完成生命周期集成测试；
4. 运行格式化、静态检查和完整测试套件；
5. 检查日志、响应和审计数据中不存在 Secret 泄漏。

---

## 15. 验收标准

满足以下条件后，本期子应用管理模块可以验收：

- 管理员可以创建子应用并一次性获得 App Secret；
- 数据库不保存明文 Secret；
- 管理员可以查询列表和详情；
- 管理员可以编辑应用名称、图标、访问地址和服务账号名称；
- 管理员可以幂等地启用或禁用子应用；
- 管理员可以重新生成 Secret，且旧 Secret 立即失效；
- 所有接口均按权限控制；
- 所有有效变更均有审计记录；
- 审计、日志和普通 API 响应中均不存在 Secret 或 Secret Hash；
- 并发编辑能够检测版本冲突；
- 并发状态变更和 Secret 重新生成不会产生不一致结果；
- 数据库迁移、单元测试和集成测试全部通过。
