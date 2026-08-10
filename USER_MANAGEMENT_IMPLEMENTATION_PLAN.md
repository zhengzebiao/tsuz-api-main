# 用户管理模块具体实现方案

根据 `MAIN_APP_MODULES.md`，本次只实现用户模块中带 ✅ 的六项功能：

1. 新增用户
2. 编辑用户
3. 禁用、启用用户
4. 拉黑、恢复用户
5. 重置密码
6. 强制下线

为支撑管理页面，补充“用户列表、用户详情”两个只读接口。暂不实现删除用户、角色分配、组织部门、MFA、登录设备、登录记录和个人权限覆盖。

---

## 1. 当前项目需要补充的能力

当前用户模型只有邮箱、密码和启用状态：

```text
id
email
hashed_password
is_active
```

当前 Session 只记录状态和创建时间。

为了实现用户管理，需要补充：

- 用户基础资料和创建、更新时间
- 独立的拉黑状态
- 禁用、拉黑原因和时间
- 密码更新时间
- Session 撤销时间和原因
- 管理 API 权限校验
- 管理员操作审计

---

## 2. 用户状态设计

“禁用”和“拉黑”不要复用一个字段，建议分别使用：

```text
is_active          是否启用
is_blacklisted     是否被拉黑
```

用户是否允许登录：

```text
login_allowed = is_active AND NOT is_blacklisted
```

| 状态 | 使用场景 | 恢复方式 |
| --- | --- | --- |
| 禁用 | 离职、暂停使用、临时停用 | 启用 |
| 拉黑 | 违规、攻击、安全风险 | 解除拉黑 |
| 禁用且拉黑 | 两种限制同时存在 | 分别解除 |

建议规则：

- 禁用用户时，不自动拉黑。
- 拉黑用户时，不修改其原有启用状态。
- 解除拉黑后，不自动启用用户。
- 启用被拉黑用户时返回冲突，必须先解除拉黑。
- 禁用和拉黑都立即强制下线。
- 恢复或启用后不自动创建新 Session，用户必须重新登录。

---

## 3. 数据模型调整

### 3.1 扩展 `users`

建议向现有用户表增加：

```text
display_name            varchar(128), nullable
is_blacklisted          boolean, default false

disabled_at             timestamp, nullable
disabled_reason         varchar(500), nullable

blacklisted_at          timestamp, nullable
blacklisted_reason      varchar(500), nullable

password_changed_at     timestamp, nullable
created_at              timestamp
updated_at              timestamp
version                 integer, default 1
```

保留现有字段：

```text
id
email
hashed_password
is_active
```

`version` 用于乐观锁，避免两个管理员同时编辑时静默覆盖数据。

### 3.2 扩展 `sessions`

建议增加：

```text
revoked_at              timestamp, nullable
revoked_reason          varchar(64), nullable
```

`revoked_reason` 可以使用：

```text
user_disabled
user_blacklisted
password_reset
admin_force_logout
email_changed
user_logout
refresh_token_reuse
```

本次不增加设备、IP、User-Agent 等字段，因为登录设备和登录记录不在当前范围内。

### 3.3 最小审计表

敏感管理操作必须留下审计记录，但本次不实现完整的日志查询模块。

```text
audit_events
- id
- actor_user_id
- action
- target_type
- target_id
- result
- reason
- changes_json
- request_id
- created_at
```

用户管理操作的 `action`：

```text
user.created
user.updated
user.disabled
user.enabled
user.blacklisted
user.recovered
user.password_reset
user.force_logout
```

`changes_json` 只能保存非敏感字段的前后差异，禁止保存密码哈希、明文密码、Token 或完整 Authorization Header。

---

## 4. 管理权限

建议新增以下稳定权限编码：

```text
user:read
user:create
user:update
user:disable
user:enable
user:blacklist
user:recover
user:reset_password
user:force_logout
```

建议使用独立权限，不使用一个 `user:manage` 覆盖全部操作。

需要新增通用权限依赖：

```text
require_permissions("user:create")
require_permissions("user:update")
```

校验过程：

1. 解析并验证 Access Token。
2. 检查 JTI 黑名单。
3. 检查 Session 是否有效。
4. 加载当前用户，检查未禁用、未拉黑。
5. 检查 Token scope 是否包含所需权限。
6. 无 Token 返回 401；权限不足返回 403。

Seed 中的管理员角色需要补齐这些权限。客户端不能自行指定角色或权限。

---

## 5. API 设计

统一使用：

```text
/admin/users
```

### 5.1 用户列表和详情

| 方法 | 地址 | 权限 | 用途 |
| --- | --- | --- | --- |
| GET | `/admin/users` | `user:read` | 用户列表 |
| GET | `/admin/users/{user_id}` | `user:read` | 用户详情 |

列表支持：

```text
page
page_size
keyword
is_active
is_blacklisted
```

`keyword` 第一阶段查询邮箱和显示名称。

响应中禁止返回：

- `hashed_password`
- Session Token
- Refresh Token
- 管理密钥

### 5.2 新增用户

```http
POST /admin/users
```

权限：

```text
user:create
```

请求：

```json
{
  "email": "user@example.com",
  "display_name": "Example User",
  "password": "strong-password",
  "is_active": true
}
```

处理流程：

1. 标准化邮箱。
2. 校验密码长度和强度。
3. 检查邮箱唯一性。
4. 使用现有密码哈希方法生成密码哈希。
5. 创建用户，默认 `is_blacklisted=false`。
6. 写入审计记录。
7. 提交数据库事务。
8. 返回用户资料，不返回密码和密码哈希。

建议规则：

- 密码长度 10～128 个字符。
- 客户端不能指定角色和权限。
- 如果普通用户必须具有基础权限，由服务端分配固定默认角色。
- 不在响应、日志或审计记录中返回明文密码。

错误：

```text
409 EMAIL_ALREADY_EXISTS
400 INVALID_PASSWORD
```

数据库唯一索引是防止并发创建重复邮箱的最终保障，不能只依赖创建前查询。

### 5.3 编辑用户

```http
PATCH /admin/users/{user_id}
```

权限：

```text
user:update
```

请求：

```json
{
  "email": "new@example.com",
  "display_name": "New Name",
  "version": 2
}
```

可编辑字段仅限：

- 邮箱
- 显示名称

状态、密码、角色不能通过通用编辑接口修改，必须使用专用操作接口。

处理规则：

- 使用 `exclude_unset` 区分“未提交”和“设为空”。
- 邮箱修改前标准化并检查唯一性。
- 修改邮箱后强制撤销该用户现有 Session。
- 使用 `version` 防止并发覆盖。
- 没有实际字段变化时可直接返回当前数据。
- 不允许通过该接口修改 `is_active`、`is_blacklisted` 或密码哈希。

错误：

```text
404 USER_NOT_FOUND
409 EMAIL_ALREADY_EXISTS
409 USER_VERSION_CONFLICT
```

### 5.4 禁用用户

```http
POST /admin/users/{user_id}/disable
```

权限：

```text
user:disable
```

请求：

```json
{
  "reason": "Employee left the organization"
}
```

操作：

1. 锁定用户行。
2. 设置 `is_active=false`。
3. 保存 `disabled_at` 和 `disabled_reason`。
4. 撤销该用户所有活动 Session。
5. 写入审计记录。
6. 提交事务。

限制：

- 管理员不能通过该接口禁用自己。
- 不能禁用系统中最后一个有效管理员。
- 禁用原因必填，长度限制为 1～500。
- 重复禁用保持幂等。

### 5.5 启用用户

```http
POST /admin/users/{user_id}/enable
```

权限：

```text
user:enable
```

操作：

```text
is_active = true
disabled_at = null
disabled_reason = null
```

限制：

- 如果用户仍处于拉黑状态，返回 `409 USER_BLACKLISTED`。
- 启用后不恢复旧 Session，用户必须重新登录。
- 重复启用保持幂等。

### 5.6 拉黑用户

```http
POST /admin/users/{user_id}/blacklist
```

权限：

```text
user:blacklist
```

请求：

```json
{
  "reason": "Abnormal automated requests"
}
```

操作：

1. 设置 `is_blacklisted=true`。
2. 保存 `blacklisted_at` 和 `blacklisted_reason`。
3. 强制撤销全部 Session。
4. 写入审计记录。
5. 不修改 `is_active`。

限制：

- 原因必填。
- 管理员不能拉黑自己。
- 不能拉黑最后一个有效管理员。
- 重复拉黑保持幂等。

### 5.7 恢复拉黑用户

```http
POST /admin/users/{user_id}/recover
```

权限：

```text
user:recover
```

操作：

```text
is_blacklisted = false
blacklisted_at = null
blacklisted_reason = null
```

恢复后：

- 不自动修改 `is_active`。
- 不恢复已撤销 Session。
- 用户处于启用状态时可以重新登录。
- 用户原本已禁用时，仍需单独调用启用接口。

### 5.8 重置密码

```http
POST /admin/users/{user_id}/reset-password
```

权限：

```text
user:reset_password
```

请求：

```json
{
  "new_password": "new-strong-password"
}
```

处理：

1. 校验新密码策略。
2. 生成新的 bcrypt 哈希。
3. 更新 `hashed_password` 和 `password_changed_at`。
4. 强制撤销该用户全部 Session。
5. 写入审计记录。
6. 返回成功消息，不返回密码和密码哈希。

响应：

```json
{
  "message": "password reset",
  "revoked_sessions": 2
}
```

### 5.9 强制下线

```http
POST /admin/users/{user_id}/force-logout
```

权限：

```text
user:force_logout
```

请求可选：

```json
{
  "reason": "Security review"
}
```

响应：

```json
{
  "message": "user logged out",
  "revoked_sessions": 3
}
```

本次只实现全部下线，不实现指定设备或指定 Session 下线。

---

## 6. 强制下线实现

当前 `SessionService` 只能按一个 `sid` 撤销，需要增加：

```text
revoke_user_sessions(user_id, reason) -> int
```

执行逻辑：

1. 查询该用户所有 `status=active` 的数据库 Session。
2. 更新为 `status=revoked`。
3. 写入 `revoked_at` 和 `revoked_reason`。
4. 对每个 `sid` 写入 Redis：

```text
{SESSION_PREFIX}{sid} = revoked
```

5. 为 Redis Key 设置 TTL，至少覆盖 Refresh Token 的最大剩余有效期。
6. 返回实际撤销的 Session 数量。

当前 Refresh Token 轮换会检查 Session 状态，因此被撤销的 Session 不能继续刷新 Token。

### 跨子应用即时失效

Access Token 是离线 JWT。如果子应用只校验 JWT 签名，不检查 Session 状态，强制下线后旧 Access Token 可能继续使用到过期。

如果要求所有子应用即时下线，需要让子应用鉴权中间件根据 JWT 中的 `sid` 检查 Redis Session 状态，或者调用统一 Token introspection 接口。

推荐：

- Access Token 保持较短有效期。
- 所有受保护服务从 JWT 读取 `sid`。
- 敏感接口检查 Redis Session 状态。
- 禁用、拉黑、重置密码、强制下线都写入 Session 撤销标记。

---

## 7. 登录流程同步修改

当前登录只检查 `is_active`，需要统一增加：

```text
user.is_active is true
AND
user.is_blacklisted is false
```

以下流程都必须检查：

- 密码登录
- Refresh Token
- `/auth/me`
- 管理 API 当前管理员认证

建议提取统一函数：

```text
ensure_user_can_authenticate(user)
```

避免不同登录路径的状态判断不一致。

对外统一返回：

```text
401 INVALID_CREDENTIALS
```

不要向未认证用户暴露账号是被禁用、拉黑还是不存在。内部审计日志可以记录真实原因。

---

## 8. 推荐文件结构

```text
app/
├── api/
│   ├── dependencies.py
│   └── admin_users.py
├── models/
│   ├── user.py
│   ├── session.py
│   └── audit_event.py
├── schemas/
│   └── admin_user.py
├── services/
│   ├── admin_user_service.py
│   ├── authorization_service.py
│   └── session_service.py
└── main.py

alembic/versions/
└── 0002_user_management.py

tests/
├── test_admin_users_api.py
├── test_admin_user_service.py
├── test_admin_authorization.py
└── test_user_session_revocation.py
```

| 文件 | 职责 |
| --- | --- |
| `admin_users.py` | 路由、HTTP 状态码和请求依赖 |
| `admin_user.py` | 请求与响应 Schema |
| `admin_user_service.py` | 用户创建、编辑和状态转换 |
| `authorization_service.py` | 当前管理员和权限检查 |
| `session_service.py` | 单 Session 和全部 Session 撤销 |
| `audit_event.py` | 最小管理员操作审计 |
| `0002_user_management.py` | 数据库字段、默认值和索引迁移 |

在 `app/main.py` 注册新的管理路由。

---

## 9. Schema 建议

```text
AdminUserCreate
- email: EmailStr
- display_name: str | None
- password: str
- is_active: bool = true

AdminUserUpdate
- email: EmailStr | None
- display_name: str | None
- version: int

UserStatusReason
- reason: str

AdminPasswordReset
- new_password: str

AdminUserResponse
- id
- email
- display_name
- is_active
- is_blacklisted
- disabled_at
- disabled_reason
- blacklisted_at
- blacklisted_reason
- password_changed_at
- created_at
- updated_at
- version

ForceLogoutResponse
- message
- revoked_sessions
```

所有输入 Schema 建议配置 `extra="forbid"`，防止客户端误传 `roles`、`permissions`、`hashed_password` 等敏感字段。

---

## 10. 事务与并发规则

所有修改操作使用数据库事务。

推荐执行顺序：

```text
校验管理员权限
→ SELECT 用户 FOR UPDATE
→ 校验状态转换
→ 修改用户和 Session
→ 写 Redis 撤销标记
→ 写审计记录
→ commit
```

重点规则：

- 创建用户依赖邮箱唯一索引处理并发冲突。
- 状态操作使用行锁或 `version` 防止并发覆盖。
- Redis 撤销成功但数据库事务失败时，最多导致用户被安全地提前下线。
- 数据库提交成功但 Redis 写入失败时必须报警并重试；数据库 Session 状态仍应阻止认证中心继续接受该 Session。
- 每次请求记录现有 `X-Request-ID`，用于关联审计和错误日志。

---

## 11. 必要防护规则

1. 管理员不能禁用或拉黑自己。
2. 禁止禁用或拉黑最后一个有效管理员。
3. 被拉黑用户不能通过“启用”绕过拉黑。
4. 禁用、拉黑、重置密码和强制下线必须撤销全部 Session。
5. 状态恢复不恢复旧 Session。
6. 客户端不能指定角色、权限或密码哈希。
7. 密码、Token 和完整认证请求体不进入日志。
8. 列表和详情响应不得返回密码相关字段。
9. 管理接口必须同时验证登录状态和管理权限。
10. 所有敏感操作必须写审计记录。

---

## 12. 测试方案

### 新增用户

- 正常创建。
- 重复邮箱返回 409。
- 邮箱标准化后重复仍返回 409。
- 弱密码被拒绝。
- 无 `user:create` 权限返回 403。
- 响应和日志中不包含密码。

### 编辑用户

- 修改显示名称。
- 修改邮箱。
- 重复邮箱返回 409。
- 旧 `version` 返回冲突。
- 修改邮箱后原 Session 被撤销。
- 不能通过 PATCH 修改状态、角色或密码哈希。

### 启用、禁用

- 禁用后不能登录和刷新 Token。
- 禁用时所有 Session 被撤销。
- 重复禁用保持幂等。
- 被拉黑用户不能直接启用。
- 管理员不能禁用自己。
- 最后一个管理员不能被禁用。

### 拉黑、恢复

- 拉黑后不能登录或刷新 Token。
- 拉黑时不改变 `is_active`。
- 恢复后保留原启用状态。
- 已禁用用户恢复后仍不能登录。
- 管理员不能拉黑自己。

### 重置密码

- 新密码可以登录。
- 旧密码不能登录。
- 所有旧 Session 和 Refresh Token 失效。
- 密码和哈希不出现在响应、日志或审计中。

### 强制下线

- 所有活动 Session 状态变成 `revoked`。
- Redis 中生成对应 Session 撤销标记。
- 旧 Refresh Token 无法刷新。
- `/auth/me` 拒绝旧 Access Token。
- 重复强制下线返回 `revoked_sessions=0`。

---

## 13. 实施顺序

### 第一阶段：模型和迁移

- 扩展用户和 Session 模型。
- 增加最小审计模型。
- 创建 Alembic `0002` 迁移。
- 回填创建时间、状态和版本字段。

### 第二阶段：认证与权限基础

- 增加管理 API 权限依赖。
- 更新 Seed 权限。
- 登录和 Refresh 流程增加拉黑检查。
- 扩展 SessionService 支持用户全部下线。

### 第三阶段：用户管理接口

按以下顺序实现：

1. 用户列表和详情
2. 新增用户
3. 编辑用户
4. 禁用和启用
5. 拉黑和恢复
6. 重置密码
7. 强制下线

### 第四阶段：测试和接口验证

- 单元测试。
- API 测试。
- Alembic Upgrade 测试。
- Redis Session 撤销测试。
- 使用本地 PostgreSQL、Redis 调用完整管理流程。

---

## 14. 验收标准

完成后应满足：

- 管理员能新增和编辑用户。
- 管理员能独立控制启用状态和拉黑状态。
- 禁用、拉黑、重置密码和强制下线会撤销全部 Session。
- 被禁用或拉黑用户无法登录和刷新 Token。
- 恢复操作不会错误恢复旧 Session。
- 所有管理接口都有独立权限保护。
- 不能通过编辑接口修改角色、权限或密码哈希。
- 不提供删除、角色分配、组织、MFA、设备和登录记录功能。
- 所有敏感操作都可通过管理员、目标用户和 Request ID 追踪。
- 现有 JWT、Refresh Token、RBAC 和注销机制保持兼容。

---

## 15. `sessions` 扩展说明

`sessions` 是数据库中的用户登录会话表，对应项目中的 `app/models/session.py`。用户登录成功后，系统会创建一条 Session，并将其中的 `sid` 写入 Access Token 和 Refresh Token。

当前 Session 主要记录：

```text
id
sid
user_id
status
created_at
```

本方案建议增加：

```text
revoked_at              timestamp, nullable
revoked_reason          varchar(64), nullable
```

### 字段用途

- `revoked_at`：记录会话被撤销的时间。
- `revoked_reason`：记录会话被撤销的原因。

建议的撤销原因：

```text
user_disabled
user_blacklisted
password_reset
admin_force_logout
email_changed
user_logout
refresh_token_reuse
```

### 使用场景

#### 管理员强制下线

撤销目标用户全部活动 Session，并记录：

```text
status = revoked
revoked_at = 当前时间
revoked_reason = admin_force_logout
```

#### 禁用或拉黑用户

除修改用户状态外，还要撤销该用户所有 Session，防止其继续使用已经登录的设备：

```text
revoked_reason = user_disabled
```

或：

```text
revoked_reason = user_blacklisted
```

#### 管理员重置密码

密码重置后撤销旧会话，防止旧设备继续使用：

```text
revoked_reason = password_reset
```

#### 查询会话状态

以后管理后台可以根据 `status` 查询用户当前活动会话数量，也可以根据 `revoked_at` 和 `revoked_reason` 排查用户为什么下线。

### PostgreSQL 和 Redis 的分工

项目同时使用 PostgreSQL 和 Redis 保存 Session 相关状态：

| 存储 | 用途 |
| --- | --- |
| PostgreSQL `sessions` | 持久化会话记录、管理后台查询、撤销时间和原因、审计排查 |
| Redis Session Key | 快速判断某个 `sid` 是否已被撤销 |

撤销 Session 时，两边都要更新：

```text
PostgreSQL:
sessions.status = revoked
sessions.revoked_at = now
sessions.revoked_reason = ...

Redis:
SESSION_PREFIX + sid = revoked
```

只更新 Redis 会导致数据库缺少管理记录；只更新数据库则可能无法及时阻止依赖 Redis 的认证检查。

### 跨子应用的注意事项

Access Token 是离线 JWT。如果子应用只校验 JWT 签名，不检查 `sid` 的 Session 状态，强制下线后旧 Access Token 可能继续使用到过期。

如果要求跨子应用即时失效，子应用鉴权中间件需要根据 JWT 中的 `sid` 检查 Redis Session 状态，或调用统一 Token introspection 接口。否则应保持较短的 Access Token 有效期。

---

## 16. 审计记录说明

审计记录（Audit Record）是系统对重要操作行为的持久化留痕，用于回答：

> 谁，在什么时间，从哪里，对哪个对象，执行了什么操作，结果如何？

它主要用于：

- 安全追踪
- 管理员行为审查
- 问题排查
- 责任定位
- 合规检查
- 账号异常恢复

审计记录不是普通运行日志，也不是业务数据。

### 与普通日志的区别

```text
普通日志：程序发生了什么
登录日志：用户如何登录
审计记录：管理员对系统做了什么
```

例如：

- 普通日志：Redis 请求超时。
- 登录日志：用户登录失败。
- 审计记录：管理员禁用了某个用户。

### 用户管理中需要记录的操作

```text
user.created
user.updated
user.disabled
user.enabled
user.blacklisted
user.recovered
user.password_reset
user.force_logout
```

例如，管理员禁用用户后，可以记录：

```json
{
  "actor_user_id": 1,
  "action": "user.disabled",
  "target_type": "user",
  "target_id": 1001,
  "result": "success",
  "reason": "员工离职",
  "changes": {
    "is_active": {
      "from": true,
      "to": false
    }
  },
  "request_id": "req-abc-123",
  "created_at": "2026-08-10T18:30:00Z"
}
```

字段含义：

| 字段 | 含义 |
| --- | --- |
| `actor_user_id` | 执行操作的管理员 |
| `action` | 操作编码 |
| `target_type` | 被操作对象类型，例如 `user` |
| `target_id` | 被操作对象 ID |
| `result` | `success` 或 `failure` |
| `reason` | 操作原因或失败原因 |
| `changes_json` | 修改前后的字段变化 |
| `request_id` | HTTP 请求追踪 ID |
| `created_at` | 操作时间 |

建议额外保存经过脱敏或哈希处理的：

```text
ip_address
user_agent
```

### 审计记录存储和权限

- 关键管理员操作存入 PostgreSQL 的 `audit_events` 表。
- 普通应用运行日志输出为 JSON，交给 Loki、ELK 或云日志平台保存。
- 审计记录默认只允许查看，不允许普通管理员修改或删除。
- 建议使用独立权限：

```text
audit:read
```

### 审计记录禁止保存的内容

禁止保存：

- 明文密码
- 密码哈希
- Access Token
- Refresh Token
- 验证码
- JWT 私钥
- SMTP 密码
- API Secret
- 完整 `Authorization` Header

例如重置密码时，只记录：

```json
{
  "action": "user.password_reset",
  "target_id": 1001,
  "result": "success",
  "changes": {
    "password_changed": true,
    "revoked_sessions": 3
  }
}
```

### 用户管理操作与审计流程

以拉黑用户为例：

```text
管理员发起拉黑请求
→ 校验管理员身份和权限
→ 查询并锁定目标用户
→ 修改 is_blacklisted=true
→ 撤销目标用户全部 Session
→ 写入审计记录
→ 提交数据库事务
```

最终可以追踪：

```text
哪个管理员
在什么时间
从哪个来源地址
以什么请求 ID
将哪个用户拉黑
拉黑原因是什么
撤销了多少个登录会话
```

---

## 17. 幂等操作说明

### 17.1 什么是幂等

幂等是指：

> 对同一个目标重复执行相同操作一次或多次，最终状态与只执行一次相同。

例如用户最初处于启用状态：

```text
第一次禁用：
is_active: true → false
撤销 3 个活动 Session

第二次禁用：
is_active: false → false
没有新的 Session 需要撤销
```

第二次调用不应返回系统异常，也不应重复修改禁用时间、覆盖第一次禁用原因或重复执行业务副作用。可以返回：

```json
{
  "id": 1001,
  "is_active": false,
  "changed": false,
  "revoked_sessions": 0
}
```

第一次执行则可以返回：

```json
{
  "id": 1001,
  "is_active": false,
  "changed": true,
  "revoked_sessions": 3
}
```

幂等主要保证业务状态和核心副作用一致。第二次请求可以记录“重复操作、状态未变化”的审计记录，但不应把它记录成一次新的状态变更。

### 17.2 为什么需要幂等

管理员点击操作后，服务端可能已经完成修改，但响应因网络超时没有到达前端。前端重试时，如果接口具备幂等性，系统会返回当前状态，而不会重复执行操作。

这样可以避免：

- 前端重复点击导致异常。
- 网关或客户端重试造成重复修改。
- 禁用时间和原因被无意覆盖。
- 重复发送通知。
- 重复撤销会话。
- 将已完成的操作误报为 500 错误。

### 17.3 建议保持幂等的用户管理操作

| 操作 | 接口示例 | 重复调用结果 |
| --- | --- | --- |
| 禁用用户 | `POST /admin/users/{id}/disable` | 保持禁用，`changed=false` |
| 启用用户 | `POST /admin/users/{id}/enable` | 保持启用，`changed=false` |
| 拉黑用户 | `POST /admin/users/{id}/blacklist` | 保持拉黑，`changed=false` |
| 恢复用户 | `POST /admin/users/{id}/recover` | 保持未拉黑，`changed=false` |
| 强制下线 | `POST /admin/users/{id}/force-logout` | `revoked_sessions=0` |

具体规则：

- 禁用：保留第一次禁用时间和原因，不重复撤销已经撤销的 Session。
- 启用：保持启用，不恢复旧 Session，也不重复发送通知；如果用户仍被拉黑，稳定返回 `409 USER_BLACKLISTED`。
- 拉黑：保留第一次拉黑时间和原因，不重复撤销 Session。
- 恢复：保持未拉黑，不修改 `is_active`，不恢复已撤销 Session。
- 强制下线：第一次返回实际撤销数量，重复调用返回 `revoked_sessions=0`。

### 17.4 其他操作的幂等性

| 操作 | 是否建议幂等 | 说明 |
| --- | --- | --- |
| 用户登出 | 建议 | 已退出的会话可以继续返回登出成功 |
| 新增用户 | 默认不建议 | 重复邮箱返回 409；需要安全重试时使用 `Idempotency-Key` |
| 编辑用户 | 部分 | 相同目标值不重复改变；使用 `version` 时可能返回版本冲突 |
| 重置密码 | 默认不建议 | 可能更新时间、撤销会话和发送通知；需要安全重试时使用 `Idempotency-Key` |
| 登录 | 不建议 | 每次成功登录通常创建新的 Session 和 Token |
| Refresh Token | 不建议 | 轮换后的旧 Token 重复提交应按重放处理 |

### 17.5 Idempotency-Key

对于新增用户和重置密码等可能因网络重试而重复执行的非幂等操作，可以支持：

```http
Idempotency-Key: random-request-id
```

服务端按“操作人 + 操作类型 + Idempotency-Key”保存首次请求结果。相同 Key 再次提交时返回首次结果，不重复创建用户、修改密码或发送通知。

幂等键应设置过期时间，并校验同一个 Key 不能用于不同请求内容，避免错误复用。

### 17.6 实现注意事项

- 状态变更接口应使用目标状态语义，而不是“切换状态”语义；避免设计 `toggle-disable` 这类接口。
- 需要保留首次操作时间和原因时，重复请求不能覆盖原值。
- 数据库状态更新和 Session 撤销应放在同一业务事务中处理。
- Redis 写入应使用幂等的 `set` 操作，并设置合理 TTL。
- 审计记录应区分“状态发生变化”和“重复请求但状态未变化”。
