# 邮箱注册、登录与密码找回：第二阶段实现计划

> 基于 [EMAIL_REGISTRATION_SES_IMPLEMENTATION_PLAN.md](EMAIL_REGISTRATION_SES_IMPLEMENTATION_PLAN.md) 的第二阶段计划。
>
> 范围：SES Provider、Redis 验证码 Challenge 和统一认证基础；不提前实现第三阶段 HTTP 邮箱认证接口。

## 1. 背景与目标

第一阶段已完成腾讯云 SES SDK 依赖、CAM/SES 配置、验证码策略配置、`users.email_verified_at` 字段和 `0006_email_registration` 迁移。第二阶段需要把这些配置接入可复用的后端服务，供后续邮箱注册、邮箱登录、找回密码和密码重置流程使用。

目标：

1. 通过腾讯云官方 SDK 发送模板验证码邮件；
2. 在 Redis 中安全保存并原子消费验证码 Challenge；
3. 实现邮箱/IP 发送限流和用途隔离；
4. 把现有登录的 Session、RBAC Claims、Access Token 和 Refresh Token 创建逻辑提取为统一能力；
5. 确认密码重置依赖的 Session/Refresh Token 撤销能力保持可复用；
6. 用无真实网络副作用的单元测试完成阶段验收。

## 2. 范围与约束

- 使用腾讯云 `tencentcloud.ses.v20201002` 的 `SesClient` 和 `SendEmailRequest`，不实现 FakeEmailProvider、SMTP 或 Mailpit。
- 固定使用 `ap-guangzhou`、`ses.tencentcloudapi.com`、`noreply@notify.tusz.online`、显示名 `tusz.online`、模板 `57044`、主题 `邮箱验证码` 和 `TriggerType=1`。
- `TemplateData` 使用 `{"code":"<6位数字>","expire_minutes":10}`，其中 `expire_minutes` 必须为数字。
- CAM Secret 只从运行时配置注入，不进入代码、测试日志、API 响应或邮件日志。
- Challenge 默认 TTL 600 秒，验证码最多错误 5 次；注册和 `password_reset` 用途不可互用。
- 同一邮箱 60 秒最多发送一次、每小时最多 10 次；同一 IP 每分钟最多 5 次。
- Redis 限流和 Challenge 消费使用 Lua 脚本；客户端 IP 由受信代理边界解析后传入服务，不由服务直接信任任意 `X-Forwarded-For`。
- 本阶段不新增 API Schema、认证路由、用户注册事务或 `EmailAuthService`。

## 3. 设计与修改文件

### 3.1 SES Provider

新增 `app/services/tencent_ses_service.py`：

- 封装 SDK Client 创建、endpoint、Region 和请求超时；
- 构造发件人、收件人、主题、模板参数和触发类型；
- 支持注入 Client，便于测试；
- 返回腾讯云 MessageId/RequestId；
- 统一转换 SDK/网络异常为内部 `EmailProviderError`；
- 仅记录脱敏收件地址、用途和 RequestId。

### 3.2 Verification Challenge

新增 `app/services/verification_challenge_service.py`：

- 标准化邮箱：去除首尾空格并整体小写；
- 使用 `secrets` 生成 6 位数字验证码和随机 Challenge ID；
- Redis Hash 保存 `purpose`、邮箱 Hash、验证码 Hash、错误次数和状态；
- 使用配置的 Challenge 前缀和 600 秒 TTL；
- 使用 Lua 脚本执行邮箱短期限流、邮箱小时限流和 IP 限流；
- 使用 Lua 脚本原子校验用途、邮箱、错误次数和验证码，并在成功时立即删除；
- 提供 SES 发送失败时的 Challenge 清理方法；
- 对 Redis 故障转换为内部状态异常，不暴露 Redis 细节。

### 3.3 统一认证完成逻辑

修改 `app/services/auth_service.py`：

- 增加 `complete_login(user_id)`；
- 加锁并重新检查用户状态；
- 创建数据库 Session；
- 加载启用角色和有效声明权限；
- 签发 Access Token 和 Refresh Token；
- 提交事务并返回现有 `TokenResponse`；
- 让现有 `login()` 在密码校验成功后复用该方法，不改变既有接口行为。

密码重置基础能力复用现有 `app/services/session_service.py` 的 `revoke_user_sessions()` 和 `app/services/refresh_token_service.py` 的 Session 活跃性校验，不在本阶段复制实现。

### 3.4 配置与测试

修改：

- `app/core/config.py`；
- `.env.local.example`；
- `.env.test.example`；
- `.env.product.example`；
- `.env.deploy.example`；
- `tests/test_email_registration_config.py`；
- `tests/test_auth_service.py`。

新增：

- `tests/test_tencent_ses_service.py`；
- `tests/test_verification_challenge_service.py`。

## 4. 实施步骤

1. 增加 Challenge 前缀和邮箱/IP 限流配置，并同步所有环境示例；
2. 实现 SES Provider 和内部异常边界；
3. 实现 Challenge 创建、Hash/TTL 存储、限流和原子消费；
4. 提取 `AuthService.complete_login()`，保持旧登录流程兼容；
5. 增加 Provider 参数与日志安全测试；
6. 增加 Challenge 过期、错误次数、用途隔离、重复消费、限流和并发消费测试；
7. 增加统一登录和既有 Session/Refresh Token 撤销回归测试；
8. 运行定向测试、lint、全量测试和锁文件检查；
9. 输出阶段执行记录到 `plan/EMAIL_REGISTRATION_SES_IMPLEMENTATION_PHASE_2_EXECUTION.md`。

## 5. 验收标准

- SES 请求 endpoint、Region、发件人、模板 ID、主题和 TriggerType 正确；
- `TemplateData.expire_minutes` 序列化为数字；
- CAM Secret、验证码和完整收件地址不出现在日志或响应；
- Redis 不保存验证码明文；
- Challenge 有正确 TTL，注册和密码重置用途不可混用；
- 错误次数达到 5 次后 Challenge 失效；
- 验证码成功消费后不能再次使用；
- 并发消费同一 Challenge 只有一个请求成功；
- 邮箱短期/小时限流和 IP 限流正确，超限时配额不会部分残留；
- 现有 `/auth/login` 的 Token、Session 和 RBAC Claims 行为不变；
- `revoke_user_sessions()` 后旧 Session 和 Refresh Token 不能继续使用；
- 阶段二不出现 HTTP 注册、邮箱登录或密码重置路由。

## 6. 验证命令

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

真实腾讯云 SES 冒烟测试、Redis 服务端集成测试和 HTTP 邮箱认证接口属于后续阶段，不在本阶段执行。
