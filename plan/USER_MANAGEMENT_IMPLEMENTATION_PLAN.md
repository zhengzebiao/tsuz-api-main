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

并发控制：只读查询不使用悲观锁或乐观锁，使用数据库普通一致性读取。列表和详情不应使用 `SELECT ... FOR UPDATE`，避免无必要地阻塞管理操作。

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

并发控制：新增用户不锁定目标用户行，因为目标行尚不存在。使用邮箱唯一索引保证并发创建时最多一个请求成功，并捕获唯一约束冲突返回 `409 EMAIL_ALREADY_EXISTS`。如果需要防止同一创建请求因网络重试重复执行，可额外使用 `Idempotency-Key`。

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

并发控制：使用 `version` 乐观锁，不使用 `SELECT ... FOR UPDATE`。更新 SQL 必须同时匹配 `id` 和客户端提交的 `version`，成功后将 `version + 1`。受影响行数为 0 时重新查询：用户不存在返回 404，版本已变化返回 `409 USER_VERSION_CONFLICT`。

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

1. 在事务中使用 `SELECT ... FOR UPDATE` 悲观锁锁定目标用户行。
2. 检查自操作、用户当前状态和最后一个有效管理员约束。
3. 设置 `is_active=false`。
4. 保存 `disabled_at` 和 `disabled_reason`。
5. 撤销该用户所有活动 Session。
6. 写入审计记录。
7. 提交事务并释放行锁。

并发控制：使用悲观锁。禁用涉及状态判断、最后一个有效管理员校验、批量撤销 Session 和审计记录，必须保证同一用户的状态操作串行执行。若目标属于管理员，仅锁目标用户行不足以保护“最后一个有效管理员”约束，还需使用事务级 advisory lock、锁定管理员角色保护行，或可序列化事务来串行化该全局检查。

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

1. 在事务中使用 `SELECT ... FOR UPDATE` 悲观锁锁定目标用户行。
2. 检查用户是否仍被拉黑。
3. 如果已经启用，按幂等规则返回 `changed=false`。
4. 更新：

```text
is_active = true
disabled_at = null
disabled_reason = null
```

5. 写入审计记录并提交事务。

并发控制：使用悲观锁。启用前必须检查 `is_blacklisted`，需要与并发拉黑、禁用或恢复操作串行执行，避免基于过期状态作出判断。启用不恢复旧 Session。

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

1. 在事务中使用 `SELECT ... FOR UPDATE` 悲观锁锁定目标用户行。
2. 检查自操作、用户当前状态和最后一个有效管理员约束。
3. 设置 `is_blacklisted=true`。
4. 保存 `blacklisted_at` 和 `blacklisted_reason`。
5. 强制撤销全部 Session。
6. 写入审计记录。
7. 不修改 `is_active`，提交事务并释放行锁。

并发控制：使用悲观锁。拉黑与启用、恢复、禁用、密码重置和强制下线必须对同一目标用户串行执行。若目标属于管理员，“最后一个有效管理员”检查还必须使用与禁用接口相同的全局串行化机制。

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

1. 在事务中使用 `SELECT ... FOR UPDATE` 悲观锁锁定目标用户行。
2. 如果已经解除拉黑，按幂等规则返回 `changed=false`。
3. 更新：

```text
is_blacklisted = false
blacklisted_at = null
blacklisted_reason = null
```

4. 写入审计记录并提交事务。

并发控制：使用悲观锁，与拉黑、启用、禁用、密码重置和强制下线串行执行。恢复仅修改黑名单字段，不恢复旧 Session，也不自动启用已禁用用户。

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

1. 在进入锁事务前校验新密码策略并生成 bcrypt 哈希，避免在持锁期间执行耗时计算。
2. 在事务中使用 `SELECT ... FOR UPDATE` 悲观锁锁定目标用户行。
3. 校验用户当前状态。
4. 更新 `hashed_password` 和 `password_changed_at`。
5. 强制撤销该用户全部 Session。
6. 写入审计记录并提交事务。
7. 返回成功消息，不返回密码和密码哈希。

并发控制：使用悲观锁，确保同一用户的并发密码重置、禁用、拉黑或强制下线按顺序执行。并发资料编辑使用乐观锁；所有用户修改都必须递增 `version`，使资料编辑在并发状态或密码变更后得到版本冲突，而不是基于旧版本继续提交。重置密码不是天然幂等操作；若要支持网络安全重试，应结合 `Idempotency-Key`，避免重复更新时间、撤销新 Session 或重复发送通知。

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

处理：

1. 在事务中使用 `SELECT ... FOR UPDATE` 悲观锁锁定目标用户行。
2. 查询并锁定该用户的活动 Session，或使用带状态条件的原子批量更新。
3. 将活动 Session 更新为 `revoked`，写入撤销时间和原因。
4. 写入 Redis 撤销标记和审计记录。
5. 提交事务并返回实际撤销数量。

并发控制：使用悲观锁锁定目标用户，并对活动 Session 使用 `FOR UPDATE` 或条件批量更新，保证并发强制下线只由一个请求撤销每条 Session。重复请求返回 `revoked_sessions=0`。与并发登录之间还需要在创建 Session 时锁定同一用户行，或在 Session 创建前后重新校验用户状态，避免强制下线扫描结束后又生成新 Session。

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

所有修改操作都使用数据库事务，但不同接口采用不同的并发控制方式。

### 10.1 各接口锁策略

| 接口 | 并发控制 | 说明 |
| --- | --- | --- |
| `GET /admin/users` | 不加锁 | 普通一致性读取，不阻塞管理写操作 |
| `GET /admin/users/{user_id}` | 不加锁 | 普通一致性读取；需要最新数据时由客户端重新查询 |
| `POST /admin/users` | 唯一约束，不使用用户行锁 | 目标行尚不存在；依赖邮箱唯一索引防止并发重复创建 |
| `PATCH /admin/users/{user_id}` | 乐观锁 | 使用 `version` 条件更新，版本不一致返回 409 |
| `POST /admin/users/{user_id}/disable` | 悲观锁 | 锁定目标用户行后检查状态、撤销 Session 并写审计 |
| `POST /admin/users/{user_id}/enable` | 悲观锁 | 锁定后检查 `is_blacklisted`，避免与拉黑并发冲突 |
| `POST /admin/users/{user_id}/blacklist` | 悲观锁 | 锁定后修改状态、撤销 Session 并写审计 |
| `POST /admin/users/{user_id}/recover` | 悲观锁 | 锁定后解除拉黑，避免与并发拉黑互相覆盖 |
| `POST /admin/users/{user_id}/reset-password` | 悲观锁 | 密码哈希在锁外生成，锁定后更新密码并撤销 Session |
| `POST /admin/users/{user_id}/force-logout` | 悲观锁 | 锁定用户，并锁定或条件更新其活动 Session |

### 10.2 悲观锁接口的执行顺序

```text
校验管理员身份和权限
→ 在事务外完成不依赖用户当前状态的耗时计算
→ 开始事务并 SELECT 用户 FOR UPDATE
→ 校验用户当前状态和操作约束
→ 修改用户和 Session
→ 写 Redis 撤销标记
→ 写审计记录
→ commit / rollback 并释放行锁
```

### 10.3 乐观锁编辑接口的执行顺序

```text
校验管理员身份和权限
→ 校验请求字段
→ UPDATE users
     SET ..., version = version + 1
   WHERE id = :user_id AND version = :expected_version
→ 受影响行为 0 时区分用户不存在或版本冲突
→ 根据修改内容撤销 Session、写审计记录
→ commit
```

### 10.4 统一规则

- 创建用户依赖邮箱唯一索引处理并发冲突；创建前查询只用于友好提示，不是并发安全保障。
- 所有修改 `users` 行的接口都必须将 `version` 加 1，包括采用悲观锁的禁用、启用、拉黑、恢复和重置密码操作。这样并发的资料编辑会因旧版本返回冲突。
- 幂等状态操作在发现目标状态已满足时返回 `changed=false`；没有实际修改用户行时不增加 `version`。
- 管理操作锁目标用户行时，登录创建 Session 的流程也应锁定同一用户行或在创建前后重新校验状态，避免禁用或强制下线完成后又产生新 Session。
- “最后一个有效管理员”属于跨多行约束，只锁目标用户行不够；应使用事务级 advisory lock、专用保护行或可序列化事务，将相关检查和修改串行化。
- Redis 撤销成功但数据库事务失败时，最多导致用户被安全地提前下线。
- 数据库提交成功但 Redis 写入失败时必须报警并重试；数据库 Session 状态仍应阻止认证中心继续接受该 Session。
- 对多行加锁时必须保持统一顺序，例如先锁用户、再按 Session ID 升序锁 Session，降低死锁概率。
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
