
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

---

## 18. 编辑用户接口为什么使用 PATCH

编辑接口使用：

```http
PATCH /admin/users/{user_id}
```

是因为它执行的是**局部更新**。调用方只需要提交希望修改的字段，不必提交完整用户对象。

例如，只修改显示名称：

```http
PATCH /admin/users/1001
Content-Type: application/json
```

```json
{
  "display_name": "新名称",
  "version": 2
}
```

请求中没有提交 `email`，所以邮箱保持原值不变。

### PATCH 与 PUT 的区别

#### PATCH：局部更新

```json
{
  "display_name": "新名称",
  "version": 2
}
```

含义是只修改 `display_name`，其余字段保持不变。

服务端使用 Pydantic 的 `exclude_unset` 区分：

- 未提交字段：不修改。
- 明确提交 `null`：将字段清空，前提是该字段允许为空。
- 提交具体值：更新为新值。

#### PUT：完整替换

如果使用：

```http
PUT /admin/users/1001
```

通常表示客户端提交用户资源的完整可修改状态。未提交字段可能被解释为清空或恢复默认值，因此客户端需要掌握完整资源状态。

PUT 不适合当前编辑接口，原因包括：

- 管理员通常只修改一个或少数字段。
- 启用、禁用、拉黑和恢复由专用接口控制。
- 密码由重置密码专用接口控制。
- 角色和权限不允许通过普通编辑接口修改。
- 要求客户端回传完整用户对象，容易覆盖其他管理员刚完成的修改。

### 为什么不使用 POST

`POST` 更适合创建资源或执行带动作语义的命令。因此本方案使用：

```text
POST  /admin/users                           创建用户
PATCH /admin/users/{user_id}                 局部编辑用户资料
POST  /admin/users/{user_id}/disable         执行禁用动作
POST  /admin/users/{user_id}/reset-password  执行重置密码动作
```

### Schema 和服务端处理建议

编辑请求 Schema 可以设计为：

```python
class AdminUserUpdate(BaseModel):
    email: EmailStr | None = None
    display_name: str | None = None
    version: int
```

处理时只提取客户端实际提交的可编辑字段：

```python
changes = payload.model_dump(
    exclude_unset=True,
    exclude={"version"},
)
```

`version` 只用于乐观锁并发检查，不属于要更新到用户资料中的字段。

因此，当前接口保持 PATCH 最合适：

> PATCH 表示修改该用户提交的部分字段，而不是使用请求内容完整替换用户资源。

---

## 19. 乐观锁与悲观锁说明

### 19.1 为什么需要并发控制

两个管理员可能在相近时间修改同一个用户。如果没有并发控制，两个请求都可能基于同一份旧数据进行判断，导致：

- 后提交的请求覆盖先提交的修改。
- 审计记录中的修改前状态不准确。
- `changed` 和 `revoked_sessions` 返回值不准确。
- 启用操作基于过期的拉黑状态作出决定。
- 禁用、拉黑与强制下线之间遗漏或重复处理 Session。
- “最后一个有效管理员”保护在并发请求下被绕过。

并发控制的目标不是禁止所有并发，而是确保同一用户的冲突操作按可预测的方式执行。

### 19.2 乐观锁

乐观锁假设并发冲突较少，因此读取数据时不锁行；提交更新时检查数据版本是否仍然与客户端读取时一致。

用户表增加：

```text
version integer, default 1
```

客户端读取用户时得到：

```json
{
  "id": 1001,
  "display_name": "原名称",
  "version": 2
}
```

编辑时提交相同版本：

```json
{
  "display_name": "新名称",
  "version": 2
}
```

服务端执行条件更新：

```sql
UPDATE users
SET display_name = :display_name,
    version = version + 1,
    updated_at = NOW()
WHERE id = :user_id
  AND version = :expected_version;
```

结果：

- 影响 1 行：更新成功，版本从 2 变为 3。
- 影响 0 行：用户不存在，或已经被其他请求修改。
- 用户存在但版本不同：返回 `409 USER_VERSION_CONFLICT`，客户端重新获取最新数据后再决定是否提交。

优点：

- 读取时不持有数据库锁。
- 不阻塞其他读写请求。
- 适合冲突较少、允许用户重新加载后重试的资料编辑。

缺点：

- 发生冲突时当前请求失败，需要重新读取和提交。
- 所有修改用户行的代码都必须正确递增 `version`。

本方案主要用于：

```text
PATCH /admin/users/{user_id}
```

### 19.3 悲观锁（锁行）

悲观锁假设并发冲突可能发生，因此在读取目标行时立即锁定它，使其他试图修改或锁定同一行的事务等待。

PostgreSQL 示例：

```sql
BEGIN;

SELECT *
FROM users
WHERE id = :user_id
FOR UPDATE;

-- 检查状态、修改用户、撤销 Session、写审计

COMMIT;
```

SQLAlchemy 示例：

```python
user = db.scalar(
    select(User)
    .where(User.id == user_id)
    .with_for_update()
)
```

行锁从 `SELECT ... FOR UPDATE` 开始持有，到事务 `commit()` 或 `rollback()` 时释放。它不会永久锁住用户，也不是应用进程里的 Python 线程锁。

当两个请求锁定同一用户时：

```text
请求 A 获得用户行锁
→ 请求 B 等待
→ 请求 A 修改并提交，释放锁
→ 请求 B 获得锁并读取 A 提交后的最新状态
→ 请求 B 再按幂等和业务规则处理
```

优点：

- 复杂的“读取、判断、修改”流程可以基于稳定状态执行。
- 适合需要同时修改用户、Session 和审计记录的敏感操作。
- 第二个请求获得锁后能看到第一个请求提交的最新状态。

缺点：

- 持锁时间过长会增加等待。
- 多行锁顺序不一致可能产生死锁。
- 外部网络调用和耗时密码哈希不应放在持锁事务内。

本方案主要用于：

```text
禁用用户
启用用户
拉黑用户
恢复拉黑
重置密码
强制下线
```

### 19.4 “锁行”的准确含义

“锁定用户行”是指在数据库事务中对目标 `users` 记录执行 `SELECT ... FOR UPDATE`。锁定期间：

- 普通 `SELECT` 通常仍可读取该行的已提交版本。
- 其他 `UPDATE`、`DELETE` 或 `SELECT ... FOR UPDATE` 会等待当前事务结束。
- 事务提交后锁自动释放。
- 如果事务回滚，修改被撤销并释放锁。

行锁只能保护被锁定的具体记录，不能自动保护聚合条件或不存在的记录。例如：

- 新增用户时目标行尚不存在，不能靠用户行锁防重复创建，应使用邮箱唯一索引。
- “最后一个有效管理员”是对多行数据的统计约束，只锁一个目标用户不足以保证安全。

### 19.5 最后一个有效管理员的并发保护

假设系统中还有两个有效管理员，两个请求同时分别禁用其中一个：

```text
请求 A 看到有效管理员数量为 2
请求 B 也看到有效管理员数量为 2
→ 两个请求都通过检查
→ 最终有效管理员数量变为 0
```

即使两个请求分别锁定了各自的目标用户行，也无法阻止该问题，因为它们锁的是不同记录。

可选方案：

1. 使用 PostgreSQL 事务级 advisory lock，把所有“移除有效管理员”操作串行化。
2. 锁定一条固定的管理员角色或专用保护记录，再检查有效管理员数量。
3. 使用 `SERIALIZABLE` 事务隔离级别，并捕获序列化失败后重试。

本项目建议采用事务级 advisory lock 或专用保护行，因为行为明确，且只串行化少量管理员状态操作。

### 19.6 乐观锁和悲观锁如何配合

两种锁不是二选一，可以在不同接口中使用：

```text
普通资料编辑：version 乐观锁
敏感状态操作：SELECT ... FOR UPDATE 悲观锁
创建用户：数据库唯一约束
只读查询：普通一致性读取
```

采用悲观锁的操作也必须递增 `users.version`。这样可以处理以下场景：

```text
管理员 A 打开用户编辑页面，读取 version=2
管理员 B 禁用该用户，悲观锁操作成功并把 version 更新为 3
管理员 A 仍使用 version=2 提交资料编辑
→ 乐观锁更新失败并返回 409
→ 避免基于旧状态提交修改
```

### 19.7 实现注意事项

- 数据库事务应尽可能短。
- 在进入悲观锁事务前完成请求格式校验和密码哈希等耗时计算。
- 不要在持有数据库行锁时调用邮件、短信、微信或其他外部网络服务。
- 多行加锁使用统一顺序：先锁用户，再按 Session ID 升序锁 Session。
- 设置合理的数据库锁等待超时，并将超时转换为可识别的冲突或重试错误。
- 幂等接口获得锁后必须重新读取状态，再决定返回 `changed=false` 或执行修改。
- 所有修改用户行的接口都要维护 `version` 和 `updated_at`。
- 审计记录必须基于锁定后读取到的真实前后状态生成。
