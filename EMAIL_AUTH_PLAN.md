# 邮箱注册与登录方案

> 本阶段只实现邮箱注册、验证、密码登录和找回密码；数据结构保留手机号、微信等登录方式的扩展能力。

## 1. 实现范围

### 本期实现

- 邮箱验证码发送
- 邮箱注册
- 邮箱密码登录
- 忘记密码与密码重置
- 复用现有 JWT、Refresh Token、Session、RBAC 和注销机制
- 本地邮件调试与自动化测试

### 暂不实现

- 手机验证码登录
- 微信扫码登录
- 其他 OAuth 登录

以上登录方式以后通过统一身份模型扩展，不重复实现 Token 和 Session 逻辑。

---

## 2. 需要提供的资料

### 业务资料

- 产品名称、Logo
- 正式网站及前端域名
- 测试环境域名
- 发件人名称和客服邮箱
- 用户协议 URL、隐私政策 URL
- 注册、验证码、密码重置等邮件文案
- 新注册用户的默认角色，例如 `user`

### 邮件服务配置

可使用企业邮箱 SMTP、Amazon SES、阿里云邮件推送、SendGrid 等服务。

需要准备：

- 发信域名，例如 `mail.example.com`
- 发件邮箱，例如 `no-reply@example.com`
- SMTP/API 地址、端口、用户名和密钥
- SPF、DKIM、DMARC DNS 记录
- 服务商发送额度、退信和费用告警

生产密钥必须通过部署平台 Secret 注入，不得提交到 Git。

### 建议环境变量

```dotenv
EMAIL_PROVIDER=smtp
EMAIL_FROM_ADDRESS=no-reply@example.com
EMAIL_FROM_NAME=Your Product

SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=__SECRET__
SMTP_PASSWORD=__SECRET__
SMTP_USE_TLS=true

FRONTEND_BASE_URL=https://www.example.com
EMAIL_CODE_EXPIRE_SECONDS=600
EMAIL_CODE_RESEND_INTERVAL_SECONDS=60
EMAIL_CODE_MAX_ATTEMPTS=5
REGISTRATION_DEFAULT_ROLE=user
PASSWORD_MIN_LENGTH=10
```

本地开发推荐使用 Mailpit：

```dotenv
EMAIL_PROVIDER=mailpit
SMTP_HOST=mailpit
SMTP_PORT=1025
```

Mailpit Web UI 可映射到 `http://127.0.0.1:8025` 查看测试邮件。

---

## 3. 核心设计

当前 `users` 表直接保存邮箱和密码，不利于以后接入手机号、微信等登录方式。建议拆分为：

```text
User（用户主体）
├── AuthIdentity（邮箱、手机号、微信等登录身份）
├── PasswordCredential（密码凭证）
└── Session / Token（现有会话体系）
```

各登录渠道只负责完成身份验证，验证成功后统一调用登录完成服务：

```text
邮箱密码 / 手机验证码 / 微信授权
→ 获取统一 User
→ 检查用户状态
→ 创建 Session
→ 加载角色和权限
→ 签发 Access Token 和 Refresh Token
```

这样以后增加手机号或微信登录时，不需要修改 JWT、RBAC、Refresh Token 和注销流程。

---

## 4. 数据模型

### 用户主体 `users`

保留用户公共信息：

```text
id
status
is_active
display_name
avatar_url
created_at
updated_at
last_login_at
```

迁移期间暂时保留现有 `email` 和 `hashed_password` 字段，不要一次性删除。

### 登录身份 `auth_identities`

```text
id
user_id
provider            # email / phone / wechat_open / ...
provider_app_id     # 微信等渠道使用，可空
subject             # 标准化邮箱、手机号或 OpenID
verified_at
is_primary
status
created_at
updated_at
last_login_at
```

唯一约束：

```text
UNIQUE(provider, provider_app_id, subject)
```

邮箱身份示例：

```text
provider = email
provider_app_id = ""
subject = user@example.com
```

### 密码凭证 `password_credentials`

```text
id
user_id
password_hash
password_algorithm
password_updated_at
failed_attempts
locked_until
created_at
updated_at
```

密码凭证与登录身份分离，可以支持以后只有手机号或微信、没有密码的账号。

### Redis 验证码

验证码使用随机 `challenge_id` 保存到 Redis：

```text
auth:challenge:email:{challenge_id}
```

内容包括：

- 用途：`register`、`password_reset`、`bind_email`
- 邮箱哈希
- 验证码哈希
- 过期时间
- 已尝试次数
- 是否已消费

验证码不保存明文，成功使用后立即删除，不同用途之间不能混用。

---

## 5. API 设计

### 发送注册验证码

```http
POST /auth/email/register/code
```

```json
{
  "email": "user@example.com"
}
```

返回：

```json
{
  "challenge_id": "random-id",
  "expires_in": 600,
  "resend_after": 60
}
```

### 邮箱注册

```http
POST /auth/email/register
```

```json
{
  "challenge_id": "random-id",
  "code": "123456",
  "password": "strong-password",
  "accept_terms": true
}
```

处理流程：

1. 原子校验并消费验证码。
2. 检查邮箱是否已经绑定。
3. 在同一数据库事务中创建用户、邮箱身份和密码凭证。
4. 分配默认普通用户角色，客户端不能指定角色。
5. 调用统一登录服务并返回现有 Token 响应。

### 邮箱密码登录

```http
POST /auth/email/login
```

```json
{
  "email": "user@example.com",
  "password": "strong-password"
}
```

当前 `/auth/login` 可以在兼容期保留，内部转调新的邮箱登录服务。建议将请求字段从含义模糊的 `username` 攑为明确的 `email`。

### 忘记密码

```http
POST /auth/password/forgot/code
```

```json
{
  "email": "user@example.com"
}
```

无论邮箱是否存在，都返回相同内容，防止账号枚举。

### 重置密码

```http
POST /auth/password/reset
```

```json
{
  "challenge_id": "random-id",
  "code": "123456",
  "new_password": "new-strong-password"
}
```

重置成功后撤销该用户原有 Session 和 Refresh Token，要求使用新密码重新登录。

### 身份管理扩展接口

为后续多登录方式预留：

```http
GET  /auth/me/identities
POST /auth/me/email/bind/code
POST /auth/me/email/bind
```

以后可以按相同结构增加手机号和微信绑定接口。

---

## 6. 服务拆分

建议增加以下服务：

### `EmailAuthService`

- 发送邮箱验证码
- 邮箱注册
- 邮箱密码认证
- 忘记密码和密码重置

### `IdentityService`

- 按 `provider + subject` 查询身份
- 创建和绑定身份
- 检查身份唯一性
- 防止一个邮箱绑定多个用户

### `VerificationChallengeService`

- 生成验证码
- 保存验证码哈希
- 校验过期时间和尝试次数
- 原子消费验证码
- 实现邮箱、IP 和设备限流

### `EmailProvider`

定义统一邮件发送接口，按环境使用：

- `SmtpEmailProvider`
- `MailpitEmailProvider`
- 测试专用 `FakeEmailProvider`

### `LoginCompletionService`

所有渠道验证成功后统一完成：

- 用户状态检查
- Session 创建
- 角色权限加载
- Access Token 和 Refresh Token 签发
- 登录时间和安全审计记录

---

## 7. 关键安全规则

- 邮箱验证码有效期建议 10 分钟。
- 同一验证码最多尝试 5 次。
- 验证码只保存哈希，验证成功后立即失效。
- 按邮箱、IP 和设备限制发送频率。
- 注册和忘记密码接口不得暴露邮箱是否存在。
- 登录失败统一返回 `401 INVALID_CREDENTIALS`，不区分邮箱不存在或密码错误。
- 密码长度建议为 10～128 个字符。
- 密码使用现有 bcrypt；后续可渐进迁移到 Argon2id。
- 重置密码后撤销旧会话。
- 用户、身份、密码和默认角色必须在同一数据库事务中创建。
- 数据库唯一约束作为防重复注册的最终保障。
- 日志禁止记录密码、验证码、Access Token、Refresh Token 和 SMTP 密钥。

邮箱标准化建议：

- 去除首尾空格。
- 域名转为小写。
- 不删除 `+tag`。
- 不擅自删除邮箱用户名中的点。
- 唯一索引使用标准化后的邮箱。

---

## 8. 数据迁移

采用三阶段迁移，避免影响现有账号。

### 阶段一：扩展

1. 新建 `auth_identities` 和 `password_credentials`。
2. 将现有 `users.email` 回填为已验证邮箱身份。
3. 将现有密码哈希回填到密码凭证表。
4. 建立唯一索引和外键。
5. 保留旧字段和旧登录接口。

迁移前需检查邮箱标准化后是否出现重复；发现冲突时输出报告并人工处理，不能自动合并账号。

### 阶段二：切换

1. 新登录接口查询身份表和密码凭证表。
2. 旧 `/auth/login` 调用新服务以保持兼容。
3. 新注册只写入新模型。
4. 继续复用现有角色、权限和 Token 体系。

### 阶段三：清理

确认客户端和数据全部迁移后：

1. 停止读取和写入旧邮箱、密码字段。
2. 将旧字段改为可空或删除。
3. 更新管理员 Seed 和运维脚本。
4. 移除旧接口及兼容代码。

---

## 9. 其他登录方式的扩展

统一身份表预留以下 Provider：

```text
email
phone
wechat_open
wechat_mp
google
apple
```

未来手机号登录只需增加：

- 手机号 E.164 标准化
- 短信 Provider
- 手机验证码 Challenge
- `provider=phone` 的身份记录
- 手机登录路由和 Schema

未来微信扫码登录只需增加：

- 微信 OAuth Provider
- `state` 和回调校验
- 一次性登录 ticket
- `provider=wechat_open`
- `provider_app_id=AppID`
- `subject=OpenID`

手机号或微信认证成功后，都调用同一个 `LoginCompletionService`，不重新实现 Session 和 Token。

多身份绑定必须遵循：

- 新身份先完成渠道验证。
- 已被其他用户占用的身份不能自动转移。
- 不根据昵称、头像等信息自动合并账号。
- 解绑后至少保留一种可用登录方式。

---

## 10. 测试与验收

至少覆盖：

- 正常发送邮箱验证码并注册。
- 验证码错误、过期、超次数及重复使用。
- 同一邮箱并发注册不会创建两个用户。
- 未同意协议不能注册。
- 新用户获得默认普通角色。
- 正确密码登录成功。
- 错误密码返回 401，而不是 500。
- 禁用用户不能登录。
- 忘记密码对存在和不存在邮箱返回一致响应。
- 重置密码后旧密码和旧会话失效。
- `/auth/me`、`/auth/refresh`、`/auth/logout` 正常工作。
- 现有用户迁移后仍能使用原邮箱密码登录。
- 日志中不存在密码、验证码和 Token。
- 本地可通过 Mailpit 完成完整邮件流程。

---

## 11. 推荐实施顺序

1. 创建身份和密码凭证模型及数据库迁移。
2. 回填现有邮箱用户，并让旧登录接口转调新服务。
3. 提取统一 `LoginCompletionService`。
4. 接入 SMTP/Mailpit 邮件 Provider。
5. 实现 Redis Challenge、验证码限流和邮箱注册。
6. 实现忘记密码和密码重置。
7. 补充身份管理接口、安全审计和自动化测试。
8. 客户端迁移完成后清理旧字段和旧接口。

最终结构应保持：

```text
邮箱完成身份验证
→ IdentityService 找到统一 User
→ LoginCompletionService 创建现有 Session
→ 签发 Access Token 和 Refresh Token
```

该设计可以完成当前邮箱注册登录需求，同时为手机号、微信扫码及其他 OAuth 登录保留稳定扩展入口。
