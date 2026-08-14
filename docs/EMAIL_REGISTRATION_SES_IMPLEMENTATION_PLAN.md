# 邮箱注册、登录与密码找回方案（腾讯云 SES）

> 状态：方案已确认，可进入实现阶段
>
> 本方案基于当前 `tsuz-api-main` 的 FastAPI、PostgreSQL、Redis、SQLAlchemy、Alembic、JWT、Refresh Token、Session 和 RBAC 架构，直接接入腾讯云邮件推送 SES，不引入 `FakeEmailProvider`。

## 1. 已确认业务配置

| 项目 | 配置 |
| --- | --- |
| 邮件服务商 | 腾讯云邮件推送 SES |
| SES 地域 | 广州 `ap-guangzhou` |
| 发信域名 | `notify.tusz.online` |
| 发信地址 | `noreply@notify.tusz.online` |
| 发件人显示名 | `tusz.online` |
| 邮件模板 ID | `57044` |
| 验证码模板变量 | `code` |
| 过期时间模板变量 | `expire_minutes` |
| `expire_minutes` 类型 | 数字 |
| 验证码长度 | 6 位数字 |
| 验证码有效期 | 10 分钟（600 秒） |
| 验证码重发间隔 | 60 秒 |
| 验证码最大错误次数 | 5 次 |
| 新用户默认角色 | `normal` |
| `normal` 角色 | 目标数据库已存在、已启用，沿用其现有权限 |
| 注册完成行为 | 注册成功后自动登录并返回 Token |
| 忘记密码 | 本期一并实现 |
| 密码重置完成行为 | 撤销全部旧会话，不自动登录 |
| 前端注册 URL | 不配置；本流程不发送注册链接 |
| 用户协议 | 本期不增加协议同意字段 |
| 邮件 Provider | 直接使用腾讯云 SES SDK/API，不实现 Fake Provider |
| 腾讯云 CAM | 已申请，运行时通过 Secret 注入 |

## 2. 目标

本期完成以下能力：

1. 用户通过邮箱验证码注册账号。
2. 用户通过邮箱和密码登录。
3. 注册成功后自动创建 Session，并返回现有 Access Token 和 Refresh Token。
4. 用户通过邮箱验证码找回并重置密码。
5. 密码重置成功后撤销该用户全部已有 Session 和 Refresh Token。
6. 继续复用现有 JWT、Refresh Token 轮换、Session 撤销、RBAC 和注销机制。
7. 邮件统一使用腾讯云 SES 模板 `57044` 发送。

本期不实现手机号、微信、OAuth，也不进行 `AuthIdentity`/`PasswordCredential` 拆表迁移。

## 3. 当前架构适配

当前用户表已经直接保存邮箱和密码：

- [app/models/user.py](../app/models/user.py) 的 `users.email`
- [app/models/user.py](../app/models/user.py) 的 `users.hashed_password`
- [app/services/auth_service.py](../app/services/auth_service.py) 按邮箱查询并校验密码
- [app/services/auth_service.py](../app/services/auth_service.py) 负责创建 Session、签发 Access Token 和 Refresh Token
- [app/services/session_service.py](../app/services/session_service.py) 负责数据库和 Redis Session 撤销
- [app/core/redis.py](../app/core/redis.py) 提供 Redis 连接

本期采用增量实现：

```text
users.email
users.hashed_password
users.email_verified_at       # 新增

users
  └── user_roles
        └── normal
```

不立即创建 `auth_identities` 和 `password_credentials`，避免影响现有管理员用户 API、Seed、登录接口和历史账号数据。后续如果接入手机号、微信或 OAuth，再通过 expand-contract 方式拆分身份模型。

## 4. API 设计

### 4.1 发送注册验证码

```http
POST /auth/email/register/code
Content-Type: application/json
```

请求：

```json
{
  "email": "user@example.com"
}
```

成功响应：

```json
{
  "challenge_id": "random-challenge-id",
  "expires_in": 600,
  "resend_after": 60
}
```

处理规则：

1. 去除邮箱首尾空格。
2. 邮箱域名和整体邮箱地址统一转为小写。
3. 按邮箱和 IP 检查发送频率。
4. 生成 6 位密码学安全随机数字验证码。
5. Redis 只保存验证码 Hash，不保存明文验证码。
6. 创建用途为 `register` 的 Challenge，TTL 为 600 秒。
7. 调用腾讯云 SES 模板 57044 发送邮件。
8. 邮件发送失败时不返回成功的 Challenge；清理或使对应 Challenge 失效，并返回 503。

如果邮箱已经注册，建议仍返回统一的成功响应或统一业务响应，不能直接暴露“邮箱已注册”，避免账号枚举。最终创建用户时由数据库唯一约束兜底。

### 4.2 邮箱注册

```http
POST /auth/email/register
Content-Type: application/json
```

请求：

```json
{
  "email": "user@example.com",
  "challenge_id": "random-challenge-id",
  "code": "123456",
  "password": "strong-password"
}
```

成功响应直接复用现有 `TokenResponse`：

```json
{
  "access_token": "...",
  "refresh_token": "...",
  "token_type": "Bearer",
  "expires_in": 3600
}
```

事务处理：

```text
标准化邮箱
  ↓
校验密码长度（10～128）
  ↓
校验并原子消费 register Challenge
  ↓
在数据库事务中检查邮箱唯一性
  ↓
创建用户（active=true、blacklisted=false）
  ↓
写入 email_verified_at
  ↓
绑定已存在且启用的 normal 角色
  ↓
提交数据库事务
  ↓
复用统一登录完成逻辑创建 Session 和 Token
  ↓
返回 TokenResponse
```

客户端不能提交或影响以下字段：

- `role`
- `role_id`
- `is_active`
- `is_blacklisted`
- `permissions`
- `is_admin`

注册时固定绑定：

```text
role.name = normal
role.is_enabled = true
```

代码只查询并绑定 `normal`，不在注册请求中动态创建角色。若部署错误导致 `normal` 缺失或被禁用，应拒绝注册并记录服务端配置错误，而不是创建无角色用户。

### 4.3 邮箱密码登录

新增明确接口：

```http
POST /auth/email/login
Content-Type: application/json
```

请求：

```json
{
  "email": "user@example.com",
  "password": "strong-password"
}
```

成功响应继续使用现有 `TokenResponse`。

现有接口保留：

```http
POST /auth/login
```

兼容期内继续支持现有 `username` 字段，内部转调相同的邮箱密码认证和统一登录完成逻辑。新客户端使用 `/auth/email/login` 与明确的 `email` 字段。

登录失败统一返回：

```http
401 Unauthorized
```

```json
{
  "detail": "invalid credentials"
}
```

不区分邮箱不存在、密码错误、用户禁用或用户拉黑。

### 4.4 发送找回密码验证码

```http
POST /auth/password/forgot/code
Content-Type: application/json
```

请求：

```json
{
  "email": "user@example.com"
}
```

无论邮箱是否存在，均返回相同响应：

```json
{
  "message": "如果邮箱已注册，验证码将发送到该邮箱",
  "expires_in": 600,
  "resend_after": 60
}
```

邮箱存在时：

1. 创建用途为 `password_reset` 的 Challenge。
2. 调用 SES 模板 57044 发送验证码。
3. 返回 Challenge 信息供客户端继续重置密码。

邮箱不存在时：

- 不发送邮件。
- 不返回“用户不存在”。
- 返回内容、状态码和主要响应时序尽量保持一致。

为了完成重置，响应中仍需要提供 `challenge_id`；对不存在邮箱可以返回一个不可用的随机 Challenge ID，或由 API 统一返回可选字段。实现时不能让客户端通过字段是否存在推断邮箱是否注册。

### 4.5 重置密码

```http
POST /auth/password/reset
Content-Type: application/json
```

请求：

```json
{
  "email": "user@example.com",
  "challenge_id": "random-challenge-id",
  "code": "123456",
  "new_password": "new-strong-password"
}
```

成功响应：

```json
{
  "message": "密码重置成功，请使用新密码登录"
}
```

处理规则：

1. 校验新密码长度为 10～128 个字符。
2. 校验 Challenge 的用途为 `password_reset`。
3. 校验邮箱、Challenge、验证码三者一致。
4. 原子消费验证码，成功后立即失效。
5. 更新 `users.hashed_password`。
6. 更新 `password_changed_at` 和用户版本号。
7. 撤销该用户所有数据库 Session。
8. 写入 Redis Session 撤销标记。
9. 使旧 Refresh Token 不能继续轮换。
10. 提交事务。
11. 不返回 Access Token 或 Refresh Token，要求使用新密码重新登录。

## 5. 腾讯云 SES 接入

### 5.1 使用官方 SDK

建议增加腾讯云 Python SDK 依赖：

```text
 tencentcloud-sdk-python
```

实际实现中使用 `tencentcloud.ses.v20201002` 模块的 `SesClient` 和 `SendEmailRequest`。SDK 负责生成腾讯云 API 3.0 TC3-HMAC-SHA256 签名，业务层不自行实现签名算法。

### 5.2 运行时配置

建议在 `app/core/config.py` 增加：

```dotenv
TENCENTCLOUD_SECRET_ID=__SECRET__
TENCENTCLOUD_SECRET_KEY=__SECRET__
TENCENTCLOUD_REGION=ap-guangzhou
TENCENTCLOUD_SES_ENDPOINT=ses.tencentcloudapi.com

EMAIL_FROM_ADDRESS=noreply@notify.tusz.online
EMAIL_FROM_NAME=tusz.online
EMAIL_TEMPLATE_ID=57044
EMAIL_SUBJECT=邮箱验证码
EMAIL_CODE_EXPIRE_MINUTES=10
EMAIL_CODE_LENGTH=6
EMAIL_CODE_MAX_ATTEMPTS=5
EMAIL_CODE_RESEND_INTERVAL_SECONDS=60
EMAIL_API_TIMEOUT_SECONDS=10
```

当前使用腾讯云 CAM 子用户长期密钥，只配置 `SecretId` 和 `SecretKey`，不使用 STS 临时凭证，也不配置 `TENCENTCLOUD_SESSION_TOKEN` 或 `X-TC-Token`。生产密钥必须通过部署平台 Secret 注入，不能提交到 Git、日志或异常响应。

### 5.3 SendEmail 请求

逻辑请求体：

```json
{
  "FromEmailAddress": "tusz.online <noreply@notify.tusz.online>",
  "Destination": ["user@example.com"],
  "Subject": "邮箱验证码",
  "Template": {
    "TemplateID": 57044,
    "TemplateData": "{\"code\":\"123456\",\"expire_minutes\":10}"
  },
  "TriggerType": 1
}
```

`TemplateData` 的内层 JSON 中：

- `code` 为字符串。
- `expire_minutes` 为数字 `10`，不是字符串 `"10"`。
- 参数名不带 `{{ }}`。

Python 构造方式：

```python
json.dumps(
    {
        "code": code,
        "expire_minutes": settings.email_code_expire_minutes,
    },
    separators=(",", ":"),
)
```

腾讯云通用签名请求头由 SDK 自动生成，包含：

```text
Authorization
Content-Type
Host
X-TC-Action
X-TC-Timestamp
X-TC-Version
X-TC-Region
```

本项目使用 CAM 子用户长期密钥，不携带 `X-TC-Token`。腾讯云 SDK 仅使用 `SecretId` 和 `SecretKey` 自动生成 TC3-HMAC-SHA256 签名。

### 5.4 邮件发送服务职责

建议新增：

```text
app/services/tencent_ses_service.py
```

职责：

- 创建腾讯云 SES Client。
- 构造 `SendEmailRequest`。
- 固定使用广州地域和模板 57044。
- 传入收件人、验证码和过期时间。
- 将 `expire_minutes` 作为数字写入模板数据。
- 设置 10 秒请求超时。
- 捕获 SDK 异常并转换为内部邮件发送异常。
- 日志只记录脱敏收件地址、用途、腾讯云 `RequestId`，不能记录验证码、SecretKey 或完整请求体。

建议不要在 API 路由中直接调用腾讯云 SDK，路由只调用认证服务，邮件 SDK 细节集中在 SES Service 中。

## 6. Redis Challenge 设计

### 6.1 Challenge Key

建议使用独立配置前缀：

```text
{email_challenge_prefix}{challenge_id}
```

例如：

```text
auth:product:email:challenge:random-id
```

Redis Hash 内容：

```text
purpose=register|password_reset
email_hash=<sha256>
code_hash=<sha256(challenge_id:code)>
attempts=0
status=active
```

TTL：

```text
600 seconds
```

验证码成功校验后立即删除，或原子标记为 consumed 后删除。注册和密码重置使用不同用途，不能混用。

### 6.2 限流 Key

按邮箱：

```text
{email_send_limit_prefix}{email_hash}
```

按 IP：

```text
{email_ip_send_limit_prefix}{ip_hash}
```

默认限制：

- 同一邮箱 60 秒内最多发送 1 次。
- 同一 IP 1 分钟内最多发送 5 次。
- 同一邮箱 1 小时内最多发送 10 次。

IP 来源必须只信任受控反向代理传递的地址。当前 nginx 已设置：

```nginx
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
```

实现时需要结合受信代理部署边界解析客户端 IP，不能直接信任用户自己提交的任意 `X-Forwarded-For` 请求头。直连 API 容器时可使用 `request.client.host`，经 nginx 时使用受信代理追加链中的最左侧地址。

### 6.3 原子消费

验证码校验和消费必须是原子的，避免两个并发注册请求同时使用同一个验证码：

```text
读取 Challenge
  ↓
校验 status、purpose、email_hash、TTL、attempts
  ↓
校验 code_hash
  ↓
成功：删除 Challenge
失败：递增 attempts，达到 5 次时删除或标记失效
```

建议使用 Redis Lua 脚本实现检查、递增和删除，避免多进程部署下的竞态。

## 7. 数据库迁移

建议新增：

```text
alembic/versions/0006_email_registration.py
```

新增字段：

```sql
ALTER TABLE users
ADD COLUMN email_verified_at TIMESTAMP NULL;
```

现有用户迁移策略：

```sql
UPDATE users
SET email_verified_at = created_at
WHERE email IS NOT NULL
  AND email_verified_at IS NULL;
```

已有账号视为历史已验证邮箱，确保原有邮箱密码登录不受影响。

新注册账号在验证码消费成功并创建用户时写入当前时间。

不删除现有 `email` 和 `hashed_password` 字段，不改变现有 `/auth/login` 的兼容行为。

## 8. `normal` 角色处理

已确认目标数据库中存在并启用 `normal` 角色，且注册用户沿用其现有权限。

注册时：

```python
role = select(Role).where(
    Role.name == "normal",
    Role.is_enabled.is_(True),
)
```

绑定角色后，Token Claims 继续通过现有 RBAC 查询生成：

```text
roles = normal
scope = normal 角色当前启用且已声明权限的集合
```

注册代码不重新定义 `normal` 的权限，也不隐式追加权限。角色权限仍通过现有角色/权限管理和权限同步机制维护。

部署前后建议保留以下检查：

```sql
SELECT id, name, is_enabled
FROM roles
WHERE name = 'normal';
```

```sql
SELECT p.name
FROM role_permissions rp
JOIN roles r ON r.id = rp.role_id
JOIN permissions p ON p.id = rp.permission_id
WHERE r.name = 'normal'
  AND r.is_enabled = TRUE
  AND p.is_enabled = TRUE
  AND p.is_declared = TRUE
ORDER BY p.name;
```

如果 `normal` 被禁用或不存在，注册接口应 fail closed，返回服务不可用或服务配置错误，不临时创建角色。

## 9. 服务拆分

建议新增以下服务：

```text
app/services/tencent_ses_service.py
app/services/verification_challenge_service.py
app/services/email_auth_service.py
```

### `TencentSesService`

- 直接封装腾讯云 SDK。
- 不保留 Fake Provider。
- 对 SDK 异常做内部异常转换。
- 支持记录腾讯云 RequestId。

### `VerificationChallengeService`

- 生成验证码和 Challenge ID。
- 持久化验证码 Hash。
- 实现 TTL、错误次数、用途隔离。
- 实现邮箱/IP 限流。
- 实现原子消费。

### `EmailAuthService`

- 发送注册验证码。
- 创建邮箱注册用户。
- 邮箱密码认证。
- 发送找回密码验证码。
- 重置密码并撤销旧会话。

### `AuthService`

从现有登录代码中提取统一登录完成方法：

```text
校验用户状态
  ↓
锁定用户记录
  ↓
创建 Session
  ↓
加载角色和权限
  ↓
签发 Access Token
  ↓
签发 Refresh Token
  ↓
提交事务
```

邮箱密码登录和邮箱注册成功后的自动登录都调用该方法，避免重复 Token/Session 逻辑。

## 10. 代码变更清单

### 配置和依赖

- `app/core/config.py`
- `.env.local.example`
- `.env.test.example`
- `.env.product.example`
- `.env.deploy.example`
- `pyproject.toml`
- `pdm.lock`

### API 和 Schema

- `app/api/auth.py`
- `app/schemas/auth.py`

新增接口：

```text
POST /auth/email/register/code
POST /auth/email/register
POST /auth/email/login
POST /auth/password/forgot/code
POST /auth/password/reset
```

### 服务

- `app/services/auth_service.py`
- `app/services/tencent_ses_service.py`
- `app/services/verification_challenge_service.py`
- `app/services/email_auth_service.py`

### 模型和迁移

- `app/models/user.py`
- `alembic/versions/0006_email_registration.py`

### 测试

- `tests/test_tencent_ses_service.py`
- `tests/test_verification_challenge_service.py`
- `tests/test_email_auth_service.py`
- `tests/test_email_registration_api.py`
- 更新 `tests/test_auth_api.py`
- 更新 `tests/test_auth_service.py`

测试中不实现 FakeEmailProvider；对腾讯云 SDK Client 做边界 Mock，另提供一个需要真实凭证的 SES 联调/冒烟测试，默认不在普通 CI 中执行。

## 11. 异常响应

### 验证码发送

| 场景 | 响应 |
| --- | --- |
| 参数格式错误 | `422` |
| 发送过于频繁 | `429` |
| SES 调用超时或失败 | `503` |
| 正常发送 | `200` |

### 注册

| 场景 | 响应 |
| --- | --- |
| 验证码错误 | `400` |
| 验证码过期 | `400` |
| 验证码超过最大尝试次数 | `400` |
| 邮箱已注册 | `409` 或统一注册失败响应 |
| 密码不符合策略 | `422` |
| `normal` 缺失/禁用 | `503` |
| 注册成功 | `200`，返回 TokenResponse |

### 密码重置

| 场景 | 响应 |
| --- | --- |
| 找回验证码请求 | 存在和不存在邮箱返回相同响应 |
| 验证码无效 | `400` |
| 新密码不符合策略 | `422` |
| 用户状态不允许操作 | 统一业务错误 |
| 重置成功 | `200`，不返回 Token |

## 12. 安全要求

1. 验证码使用 `secrets` 生成，不能使用普通伪随机数。
2. Redis 不存明文验证码。
3. Challenge 必须绑定邮箱 Hash、用途和 Challenge ID。
4. 验证码成功后立即消费。
5. 注册验证码和密码重置验证码不能互用。
6. 验证码错误最多 5 次。
7. 注册、找回密码接口不能暴露邮箱是否存在。
8. 登录失败统一返回 `invalid credentials`。
9. 密码使用现有 bcrypt。
10. 密码长度限制为 10～128 个字符。
11. 重置密码后撤销所有 Session 和 Refresh Token。
12. 日志不得包含密码、验证码、完整 Token、SecretKey、Authorization 请求头。
13. 邮件日志只记录脱敏邮箱、用途、发送结果、MessageId/RequestId。
14. SES API 调用使用后端 CAM 子用户长期凭证，前端永远不能接触 SecretId/SecretKey；本项目不使用 STS 临时凭证和 `X-TC-Token`。
15. 不在邮件中发送注册链接或密码，只发送验证码。
16. SES 请求设置超时，避免阻塞 API Worker。
17. 默认不对发送失败自动重试，避免用户收到多封相同验证码；如需重试，应使用明确的幂等策略。

## 13. 测试和验收

### SES Service

- 请求域名为 `ses.tencentcloudapi.com`。
- Region 为 `ap-guangzhou`。
- Action 为 `SendEmail`。
- TemplateID 为 `57044`。
- FromEmailAddress 为 `noreply@notify.tusz.online`，显示名为 `tusz.online`。
- Subject 为 `邮箱验证码`。
- `TemplateData.code` 为验证码字符串。
- `TemplateData.expire_minutes` 为数字 10。
- SDK 异常被转换为内部邮件服务异常。
- 日志不包含 SecretKey、验证码和完整请求体。

### 注册

- 正常发送验证码并完成注册。
- 注册用户的邮箱为标准化后的值。
- 注册用户 `email_verified_at` 不为空。
- 注册用户自动绑定 `normal` 角色。
- 注册用户沿用 `normal` 现有有效权限。
- 注册成功自动返回 Access Token 和 Refresh Token。
- 同一邮箱不能重复注册。
- 同一验证码不能重复消费。
- 并发注册不会创建两个用户。
- 验证码错误、过期、超次数均被拒绝。
- `normal` 缺失或禁用时不会创建无角色账号。

### 登录

- `/auth/email/login` 使用 email/password 登录。
- 旧 `/auth/login` 继续兼容。
- 正确密码登录成功。
- 错误密码返回 401。
- 禁用或拉黑用户不能登录。
- Refresh、Logout、Me 继续正常工作。

### 找回密码

- 已注册邮箱能够收到模板 57044 验证码。
- 未注册邮箱和已注册邮箱返回一致的找回响应。
- 密码重置成功后新密码可登录。
- 旧密码不能登录。
- 重置密码后旧 Session 全部失效。
- 重置密码后旧 Refresh Token 不能轮换。
- 重置密码接口不返回 Token。
- 注册验证码不能用于密码重置，反之亦然。

## 14. 部署前检查清单

### 腾讯云 SES

- [ ] 广州地域 `ap-guangzhou` 可用。
- [ ] `notify.tusz.online` 域名验证通过。
- [ ] SPF、DKIM、DMARC 配置完成。
- [ ] `noreply@notify.tusz.online` 发信地址验证通过。
- [ ] 模板 `57044` 审核通过并启用。
- [ ] 模板变量准确为 `code` 和 `expire_minutes`。
- [ ] 账号发送额度和频率满足业务需求。
- [ ] 使用专用 CAM 用户或角色。
- [ ] CAM 具备 SES 发信最小权限。
- [ ] 生产 Secret 已注入部署平台。
- [ ] 生产日志不会输出腾讯云密钥。

### 数据库和权限

- [ ] Alembic 迁移已在测试环境执行。
- [ ] 现有用户 `email_verified_at` 已按迁移策略回填。
- [ ] `normal` 角色存在。
- [ ] `normal.is_enabled = true`。
- [ ] `normal` 权限清单已核对。
- [ ] 权限均符合当前 `is_enabled=true`、`is_declared=true` 的要求。
- [ ] 生产环境完成迁移前备份和回滚评估。

### Redis

- [ ] 生产 Redis 使用独立 DB/Key Prefix。
- [ ] Challenge Key 和限流 Key 不与其他环境冲突。
- [ ] TTL 和限流配置已核对。
- [ ] Redis Lua/事务原子消费测试通过。

### API

- [ ] 注册、登录、找回密码和重置密码接口已完成自动化测试。
- [ ] 注册成功自动登录测试通过。
- [ ] 重置密码后旧会话失效测试通过。
- [ ] 通过 nginx 时客户端 IP 限流逻辑测试通过。
- [ ] 直连 API 和经 nginx 访问的行为符合预期。

## 15. 实施顺序

本模块采用四个阶段分步开发。每个阶段完成后先验证，再进入下一阶段；不跨阶段提前加入业务功能。腾讯云 SES 使用真实 Provider，测试阶段对 SDK Client 做边界 Mock，不实现 `FakeEmailProvider`。

### 第一阶段：邮件配置、用户模型与数据库迁移

开发内容：

1. 增加腾讯云 SES SDK 依赖及锁文件变更；
2. 扩展 `app/core/config.py`，增加 CAM、SES 地域、发信地址、模板 ID、验证码策略配置；
3. 扩展 `app/models/user.py`，增加 `email_verified_at`；
4. 新增 Alembic `0006_email_registration` 迁移；
5. 回填现有用户的 `email_verified_at`，确保历史账号登录兼容；
6. 验证目标数据库中 `normal` 角色存在且启用；
7. 核对 `normal` 角色现有有效权限，不在本模块中重新分配权限；
8. 补充模型和 Alembic upgrade 测试。

本阶段不实现：

- Redis Challenge；
- 腾讯云 SES 发送逻辑；
- 注册、登录和密码找回 API；
- 新用户注册事务。

阶段验收：

- ORM 与迁移结构一致；
- 迁移可安全 upgrade；
- 既有用户数据未丢失；
- `normal` 角色存在且 `is_enabled=true`；
- `normal` 角色的权限清单与业务预期一致；
- 历史邮箱密码登录行为不变。

### 第二阶段：SES Provider、验证码与认证基础

开发内容：

1. 新增 `app/services/tencent_ses_service.py`；
2. 使用腾讯云官方 SDK 调用 `SendEmail`；
3. 固定使用 `ap-guangzhou`、`noreply@notify.tusz.online` 和模板 `57044`；
4. 以数字类型传递 `expire_minutes`；
5. 新增 `app/services/verification_challenge_service.py`；
6. 实现验证码生成、Hash 存储、TTL、用途隔离和最大错误次数；
7. 实现邮箱/IP 发送限流；
8. 实现 Redis Challenge 原子校验和消费；
9. 从 `AuthService` 提取注册成功后的统一登录完成逻辑；
10. 确认密码重置后撤销用户全部 Session 和 Refresh Token 的基础能力；
11. 补充 SES SDK 边界测试、验证码服务测试和 Redis 状态测试。

本阶段不实现：

- HTTP 注册接口；
- HTTP 邮箱登录接口；
- HTTP 忘记密码和重置密码接口；
- 前端页面或注册链接。

阶段验收：

- SES 请求参数、模板 ID、发信地址和地域正确；
- CAM Secret 不进入日志、响应和代码仓库；
- `TemplateData.expire_minutes` 为数字；
- 验证码过期、错误次数和重复消费规则正确；
- 注册验证码与密码重置验证码不能混用；
- 并发消费同一 Challenge 只允许一个请求成功；
- 统一登录完成逻辑不破坏现有 Token、Session 和 RBAC 行为。

### 第三阶段：邮箱注册、登录与密码找回接口

按以下顺序实现：

1. `POST /auth/email/register/code` 发送注册验证码；
2. `POST /auth/email/register` 校验验证码、创建用户、绑定 `normal` 并自动登录；
3. `POST /auth/email/login` 实现明确的邮箱密码登录；
4. 保留 `POST /auth/login`，兼容现有 `username` 请求字段；
5. `POST /auth/password/forgot/code` 发送找回密码验证码；
6. `POST /auth/password/reset` 校验验证码、更新密码并撤销全部旧会话；
7. 统一邮箱标准化、密码校验和业务异常响应；
8. 防止注册和找回密码接口暴露邮箱是否存在；
9. 补充 API Schema、服务和事务回滚测试。

注册成功处理必须在一个数据库事务中完成：

```text
验证码原子消费
  ↓
创建用户
  ↓
绑定启用的 normal 角色
  ↓
提交事务
  ↓
创建 Session 并签发 Token
```

密码重置成功处理必须为：

```text
验证码原子消费
  ↓
更新 bcrypt 密码
  ↓
撤销全部 Session 和 Refresh Token
  ↓
提交事务
  ↓
不返回 Token，要求重新登录
```

阶段验收：

- 用户可以通过邮箱验证码完成注册；
- 注册成功自动返回 Access Token 和 Refresh Token；
- 新用户自动绑定 `normal`，并沿用其现有权限；
- 正确邮箱密码可以登录；
- 错误凭证统一返回 401；
- 已注册和未注册邮箱的找回密码响应不可枚举；
- 新密码可以登录，旧密码失效；
- 重置密码后旧 Session 和 Refresh Token 全部失效；
- 旧 `/auth/login`、`/auth/refresh`、`/auth/logout`、`/auth/me` 保持兼容。

### 第四阶段：测试、真实邮件验证与部署检查

开发和验证内容：

1. 执行完整单元测试；
2. 执行注册、邮箱登录、验证码和密码重置 API 测试；
3. 执行 Alembic upgrade 和迁移数据回填测试；
4. 执行 Redis TTL、限流、原子消费和 Session 撤销测试；
5. 对腾讯云 SDK Client 做无真实网络依赖的边界 Mock 测试；
6. 在测试环境使用已申请的 CAM Secret，向受控收件箱执行一次真实 SES 冒烟验证；
7. 验证邮件模板 57044 的 `code` 和数字 `expire_minutes` 替换结果；
8. 核对广州地域、域名、发件地址、模板状态和发送额度；
9. 核对生产环境 `normal` 角色及其权限；
10. 注入生产 CAM Secret，确认 Secret 不出现在日志和部署输出；
11. 完成生产迁移、灰度发布和回滚兼容检查。

阶段验收：

- 完整邮箱注册链路可以成功收信、注册并自动登录；
- 完整忘记密码链路可以成功收信、重置密码并使旧会话失效；
- 所有敏感信息均未进入日志；
- 现有认证和管理员功能回归测试通过；
- 生产迁移和旧版本兼容窗口可控；
- 具备按不可变镜像回滚、按前向修复迁移数据库的发布方案。

## 16. 实现完成标准

完成后，核心链路应为：

```text
邮箱
  ↓
腾讯云 SES 模板 57044 验证
  ↓
Redis Challenge 原子消费
  ↓
创建 users 记录
  ↓
绑定 normal 角色
  ↓
统一登录完成服务
  ↓
Session + Access Token + Refresh Token
```

找回密码链路应为：

```text
邮箱
  ↓
腾讯云 SES 模板 57044 验证
  ↓
Redis password_reset Challenge 原子消费
  ↓
更新 bcrypt 密码
  ↓
撤销全部旧 Session / Refresh Token
  ↓
使用新密码重新登录
```

当前没有剩余的产品流程阻塞项。进入编码前只需在部署配置中实际注入已申请的腾讯云 CAM Secret，并在目标环境再次验证 SES 资源与 `normal` 角色状态。
