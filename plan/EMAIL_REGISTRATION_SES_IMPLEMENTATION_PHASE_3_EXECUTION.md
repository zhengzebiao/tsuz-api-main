# 邮箱注册、登录与密码找回：第三阶段开发执行记录

## 1. 执行范围

本次根据 [EMAIL_REGISTRATION_SES_IMPLEMENTATION_PLAN.md](EMAIL_REGISTRATION_SES_IMPLEMENTATION_PLAN.md) 完成第三阶段“邮箱注册、登录与密码找回接口”。

本阶段已完成：

1. 邮箱验证码注册接口；
2. 邮箱密码登录接口；
3. 找回密码验证码接口；
4. 验证码密码重置接口；
5. 旧 `/auth/login` 与新的邮箱登录共享认证完成逻辑；
6. 邮箱标准化、密码策略、统一错误响应和受信代理 IP 解析；
7. 阶段三服务、API 和客户端 IP 测试；
8. 阶段三计划与执行文档。

本阶段明确未执行：

- 真实腾讯云 SES 网络冒烟；
- 真实生产 Redis 多进程并发验证；
- 生产 nginx、CAM Secret、DNS/SPF/DKIM/DMARC 和部署发布检查；
- 前端页面、注册链接、手机号/OAuth 身份模型拆分。

## 2. API 实现

新增路由：

```text
POST /auth/email/register/code
POST /auth/email/register
POST /auth/email/login
POST /auth/password/forgot/code
POST /auth/password/reset
```

### 2.1 注册验证码

`EmailAuthService.send_registration_code()` 使用第二阶段 Challenge 服务创建 `register` Challenge，再调用 `TencentSesService` 发送模板验证码。API 只返回：

```json
{
  "challenge_id": "...",
  "expires_in": 600,
  "resend_after": 60
}
```

验证码不出现在 HTTP 响应。SES 失败时调用 Challenge 清理方法，并返回 `503`。

### 2.2 邮箱注册

注册服务：

- 标准化邮箱；
- 校验 10～128 个字符密码；
- 原子消费 `register` Challenge；
- 锁定并检查启用的 `normal` 角色；
- 在数据库事务中创建 active、非黑名单用户；
- 写入 `email_verified_at`；
- 绑定 `normal` 角色；
- 提交后复用 `AuthService.complete_login()` 自动签发 Session、Access Token 和 Refresh Token。

邮箱唯一冲突映射为安全的注册失败响应，`normal` 缺失或禁用时不会创建无角色用户。

### 2.3 邮箱登录

新增 `/auth/email/login` 使用 `email` 字段。`AuthService.login_by_email()` 与旧 `/auth/login` 共用：

- 邮箱标准化；
- 用户 active/blacklist 校验；
- bcrypt 密码校验；
- Session、RBAC Claims、Access Token 和 Refresh Token 创建。

所有失败统一返回 `401 invalid credentials`，登录日志不记录完整邮箱。

### 2.4 找回密码和重置密码

找回密码接口对已存在和不存在邮箱返回相同消息、状态码和字段结构：

```json
{
  "message": "如果邮箱已注册，验证码将发送到该邮箱",
  "challenge_id": "...",
  "expires_in": 600,
  "resend_after": 60
}
```

未知邮箱不会发送 SES 邮件，并立即删除不可用 Challenge。已存在且可认证的用户使用 `password_reset` Challenge 发送验证码。

密码重置：

- 校验并原子消费 `password_reset` Challenge；
- 更新 bcrypt 密码、`password_changed_at` 和 `version`；
- 调用 `SessionService.revoke_user_sessions()` 撤销全部旧 Session，并写入 Redis 撤销标记；
- 提交事务；
- 只返回固定成功消息，不返回 Token。

## 3. 受信代理 IP

新增 `app/api/client_ip.py`：

- 非受信连接对端完全忽略 `X-Forwarded-For`；
- 受信代理只从有效 IP 的转发链中按右向左查找第一个非受信地址；
- 无效 Header、缺失连接地址或通配符信任配置安全回退；
- `TRUSTED_PROXY_IPS` 已同步到环境示例。

Challenge 服务仍只接收路由层解析后的 IP，不读取任何 HTTP Header。

## 4. 文件变更

新增：

- `app/api/client_ip.py`；
- `app/services/email_auth_service.py`；
- `tests/test_client_ip.py`；
- `tests/test_email_auth_service.py`；
- `tests/test_email_registration_api.py`；
- `plan/EMAIL_REGISTRATION_SES_IMPLEMENTATION_PHASE_3_PLAN.md`；
- `plan/EMAIL_REGISTRATION_SES_IMPLEMENTATION_PHASE_3_EXECUTION.md`。

修改：

- `app/api/auth.py`；
- `app/core/config.py`；
- `app/schemas/auth.py`；
- `app/services/auth_service.py`；
- `.env.local.example`；
- `.env.test.example`；
- `.env.product.example`；
- `.env.deploy.example`；
- `nginx/default.conf`；
- `tests/test_auth_service.py`；
- `tests/test_email_registration_config.py`。

## 5. 测试与验证

定向阶段三测试：

```text
54 passed, 1 warning
```

全量测试：

```text
283 passed, 14 skipped, 1 warning
```

Ruff：

```text
All checks passed!
```

额外检查：

- `pdm lock --check`；
- `git diff --check`。

两项检查在提交前执行并通过。

## 6. 阶段结论

第三阶段接口和服务链路已经落地：

- 邮箱验证码能够驱动注册并自动登录；
- 新用户写入已验证邮箱并固定绑定 `normal`；
- 新旧邮箱密码登录路径共用认证基础；
- 找回密码不暴露邮箱是否存在；
- 密码重置撤销全部旧会话和 Refresh Token；
- 输入、错误、日志和受信代理边界均有自动化测试覆盖；
- 真实 SES 和生产部署验证保留到第四阶段。
