# 邮箱注册、登录与密码找回：第二阶段开发执行记录

## 1. 执行范围

本次根据 [EMAIL_REGISTRATION_SES_IMPLEMENTATION_PLAN.md](EMAIL_REGISTRATION_SES_IMPLEMENTATION_PLAN.md) 完成第二阶段“SES Provider、验证码与认证基础”。

本阶段已完成：

1. 腾讯云 SES `SendEmail` Provider 边界封装；
2. Redis Verification Challenge 生成、Hash 存储、TTL、用途隔离、错误次数、邮箱/IP 限流和 Lua 原子消费；
3. `AuthService.complete_login()` 统一登录完成能力；
4. Challenge 配置同步到本地、测试、部署和产品环境示例；
5. SES、Challenge、统一登录和 Session/Refresh Token 撤销基础测试。

本阶段明确未实现：

- HTTP 注册、邮箱登录、忘记密码和重置密码接口；
- `EmailAuthService` 和用户注册事务；
- FakeEmailProvider、SMTP/Mailpit 或真实 SES 网络冒烟；
- 前端页面、注册链接和身份模型拆表；
- STS 临时凭证、`TENCENTCLOUD_SESSION_TOKEN` 和 `X-TC-Token`。

## 2. SES Provider

新增 [app/services/tencent_ses_service.py](../app/services/tencent_ses_service.py)。

### 2.1 SDK 和请求

Provider 使用第一阶段已经锁定的 `tencentcloud-sdk-python`：

- `Credential(SecretId, SecretKey)`；
- `HttpProfile(endpoint=ses.tencentcloudapi.com, reqTimeout=10)`；
- `ClientProfile`；
- `SesClient(..., region=ap-guangzhou, ...)`；
- `models.SendEmailRequest` 和 `models.Template`。

请求固定使用：

```text
FromEmailAddress = tusz.online <noreply@notify.tusz.online>
Destination = [recipient_email]
Subject = 邮箱验证码
Template.TemplateID = 57044
TriggerType = 1
```

`TemplateData` 使用紧凑 JSON：

```json
{"code":"123456","expire_minutes":10}
```

其中 `code` 是字符串，`expire_minutes` 是数字，而不是字符串。

### 2.2 错误和日志

- 支持注入 SDK Client，普通单元测试不访问腾讯云网络；
- 缺失 SecretId/SecretKey 时 fail closed，抛出内部 `EmailProviderError`；
- SDK 或网络异常统一转换为 `EmailProviderError("email provider unavailable")`；
- 日志仅记录脱敏收件地址、用途和腾讯云 RequestId；
- 不记录验证码、Secret、完整收件地址、完整请求体或底层异常文本。

## 3. Verification Challenge

新增 [app/services/verification_challenge_service.py](../app/services/verification_challenge_service.py)。

### 3.1 Challenge 数据

Challenge Key：

```text
{EMAIL_CHALLENGE_PREFIX}{challenge_id}
```

Redis Hash 字段：

```text
purpose=register|password_reset
email_hash=sha256(normalized_email)
code_hash=sha256(challenge_id:code)
attempts=0
status=active
```

验证码使用 `secrets.choice()` 生成，默认 6 位数字；Challenge 默认 TTL 为 600 秒。明文验证码只保留在进程返回值中，未写入 Redis。

邮箱标准化为去除首尾空格并整体转小写，不移除 `+tag`，不修改用户名中的点号。

### 3.2 限流

使用 Redis Lua 脚本保证邮箱短期窗口、邮箱小时计数和 IP 分钟计数在同一原子操作中完成：

- 同一邮箱 60 秒内最多发送 1 次；
- 同一邮箱 1 小时最多发送 10 次；
- 同一 IP 1 分钟最多发送 5 次。

限流 Key 只使用邮箱/IP 的 SHA-256，发送失败时后续业务层可以删除 Challenge；不自动重试 SES。

客户端 IP 由调用层提供，Challenge 服务不读取或信任用户提交的任意 `X-Forwarded-For`。

### 3.3 原子消费

第二个 Lua 脚本原子完成：

1. Challenge 存在且仍为 `active`；
2. 用途和邮箱 Hash 匹配；
3. 错误尝试次数未达到 5 次；
4. 比较 `sha256(challenge_id:code)`；
5. 正确时删除 Challenge；
6. 错误时递增次数，达到上限时删除 Challenge。

因此不同用途不能互用、同一 Challenge 不能重复消费，并发请求最多一个成功。

## 4. 认证基础重构

在 [app/services/auth_service.py](../app/services/auth_service.py) 增加 `complete_login(user_id)`：

```text
锁定并检查用户状态
  ↓
创建数据库 Session
  ↓
加载启用角色及有效声明权限
  ↓
签发 Access Token
  ↓
签发 Refresh Token
  ↓
提交事务
  ↓
返回 TokenResponse
```

现有 `login()` 保留邮箱查询、状态检查和 bcrypt 密码校验，认证成功后委托 `complete_login()`。既有 `/auth/login`、refresh、logout、me 的行为和 Claims 逻辑保持不变，第三阶段可直接复用该方法实现邮箱注册后的自动登录和邮箱密码登录。

密码重置基础能力不重复实现：现有 [app/services/session_service.py](../app/services/session_service.py) 的 `revoke_user_sessions()` 会更新所有目标用户的 DB Session 和 Redis 撤销标记；现有 Refresh Token 轮换在 Session 被撤销后拒绝继续轮换。管理员密码重置已维护 `password_changed_at` 和用户 `version`，后续邮箱重置接口可复用相同约定。

## 5. 测试和验证

新增/更新测试：

```text
tests/test_tencent_ses_service.py
tests/test_verification_challenge_service.py
tests/test_auth_service.py
tests/test_email_registration_config.py
```

覆盖：

- SES endpoint、timeout、地域、发件人、主题、模板 ID、触发类型；
- `expire_minutes` 数字类型和验证码字符串类型；
- SDK 异常转换及日志敏感信息保护；
- Challenge Hash 不含明文验证码或邮箱/IP；
- Challenge TTL、用途/邮箱隔离、错误次数、上限失效和一次性消费；
- Lua 契约下限流回滚和并发消费单成功；
- 统一登录成功签发 Token/Session，以及用户状态重新检查；
- Session 全量撤销和旧 Refresh Token 失效能力由既有测试回归覆盖。

执行命令：

```bash
pdm run pytest tests/test_tencent_ses_service.py \
  tests/test_verification_challenge_service.py \
  tests/test_auth_service.py \
  tests/test_email_registration_config.py \
  tests/test_redis_state_services.py \
  tests/test_user_session_revocation.py \
  tests/test_refresh_token_service.py
pdm run ruff check .
pdm run pytest
pdm lock --check
git diff --check
```

真实 SES 冒烟测试、Redis 服务端 Lua 集成测试和 HTTP 邮箱认证链路留到后续阶段；本阶段所有 Provider 测试均无真实网络副作用。

## 6. 阶段结论

第二阶段实现目标已落地：

- SES 请求集中在 Provider，参数符合模板 57044 约定；
- Secret 不写入代码、响应或日志；
- 验证码只存 Hash，具有 TTL、用途隔离、限流和原子消费；
- 统一登录完成逻辑可被后续邮箱认证复用；
- Session/Refresh Token 撤销基础能力保持兼容；
- HTTP 邮箱注册、登录、找回密码和重置密码没有提前实现，下一步从方案第三阶段开始。
