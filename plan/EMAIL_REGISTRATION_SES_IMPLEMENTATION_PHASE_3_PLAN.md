# 邮箱注册、登录与密码找回：第三阶段实现计划

> 基于 [EMAIL_REGISTRATION_SES_IMPLEMENTATION_PLAN.md](EMAIL_REGISTRATION_SES_IMPLEMENTATION_PLAN.md) 的第三阶段计划。

## 1. 背景与目标

第二阶段已经完成腾讯云 SES Provider、Redis Verification Challenge 和统一登录完成能力。本阶段在不改变既有认证兼容性的前提下，补齐邮箱验证码注册、邮箱密码登录、找回密码验证码和密码重置 HTTP 接口。

目标：

1. 通过邮箱验证码创建已验证的新用户；
2. 固定绑定已启用的 `normal` 角色并自动登录；
3. 增加明确的 `/auth/email/login`，同时保留旧 `/auth/login`；
4. 通过邮箱验证码完成密码重置并撤销所有旧会话；
5. 对注册和找回密码保持安全的邮箱不可枚举行为；
6. 复用第二阶段 Challenge、SES、Session、Refresh Token 和统一登录基础。

## 2. 范围与约束

新增接口：

- `POST /auth/email/register/code`；
- `POST /auth/email/register`；
- `POST /auth/email/login`；
- `POST /auth/password/forgot/code`；
- `POST /auth/password/reset`。

保留接口：

- `POST /auth/login` 继续接受 `username` 字段，并复用邮箱密码登录路径；
- `/auth/refresh`、`/auth/logout`、`/auth/me` 行为不变。

安全约束：

- 邮箱统一 `strip().lower()`；
- 密码长度为 10～128 个字符；
- 验证码、Challenge 用途和原子消费完全复用第二阶段服务；
- Redis/SES 失败不暴露底层错误；
- 不在日志、响应或审计数据中写入密码、验证码、Token、Secret、完整 Challenge ID 或完整邮箱；
- 未知邮箱的找回密码响应与已知邮箱保持相同结构和状态，未知邮箱不发送邮件；
- 客户端 IP 仅由受信代理解析逻辑传入 Challenge 服务，直连请求忽略伪造的 `X-Forwarded-For`。

## 3. 设计与修改文件

### 3.1 Schema 和配置

修改：

- `app/schemas/auth.py`：增加严格的邮箱注册、邮箱登录、找回密码和密码重置请求/响应模型；拒绝未知字段；验证码限制为六位数字；密码限制为 10～128 个字符；
- `app/core/config.py`：增加 `trusted_proxy_ips` 配置；
- `.env.local.example`、`.env.test.example`、`.env.product.example`、`.env.deploy.example`：增加 `TRUSTED_PROXY_IPS` 示例，不填入真实 Secret。

新增：

- `app/api/client_ip.py`：解析直连 IP 和受信代理的 `X-Forwarded-For` 链。无效 Header 或非受信对端回退到连接对端，不使用通配符信任。

### 3.2 EmailAuthService

新增 `app/services/email_auth_service.py`，统一编排：

- 注册验证码 Challenge 创建与 SES 发送；
- 注册 Challenge 消费、用户创建、验证时间写入、`normal` 角色绑定和自动登录；
- 邮箱密码登录；
- 找回密码验证码发送；
- 密码重置、密码版本更新和全量 Session/Refresh Token 撤销。

注册事务：

```text
消费 register Challenge
  ↓
锁定并检查 normal 角色和邮箱唯一性
  ↓
创建 active、非黑名单、已验证用户
  ↓
绑定 normal
  ↓
提交数据库事务
  ↓
AuthService.complete_login()
```

密码重置事务：

```text
锁定并检查用户
  ↓
消费 password_reset Challenge
  ↓
更新 bcrypt 密码、password_changed_at、version
  ↓
SessionService.revoke_user_sessions()
  ↓
提交事务
  ↓
返回固定成功消息，不返回 Token
```

### 3.3 认证路由

修改 `app/api/auth.py`：

- 增加五个邮箱认证路由；
- 调用 `get_client_ip(request)` 获取限流 IP；
- 将验证码错误映射为 `400`，限流映射为 `429`，SES/Redis/服务配置故障映射为 `503`；
- 登录错误统一映射为 `401 invalid credentials`；
- 找回密码始终返回固定消息和相同字段形状。

修改 `app/services/auth_service.py`：

- 增加 `login_by_email()`；
- 让旧 LoginRequest 与邮箱登录共享规范化、用户状态校验、bcrypt 和 `complete_login()`；
- 登录失败日志不再包含完整邮箱。

## 4. 测试和验收

新增：

- `tests/test_email_auth_service.py`：覆盖 SES 失败清理、注册事务、normal 角色、邮箱唯一性、自动登录、登录复用、找回密码不可枚举、密码重置和会话撤销；
- `tests/test_email_registration_api.py`：覆盖五个 HTTP 接口的响应、输入校验、错误映射和旧接口兼容；
- `tests/test_client_ip.py`：覆盖直连伪造 XFF、受信代理链、无效 Header、缺失客户端地址和通配符配置。

更新：

- `tests/test_auth_service.py`：覆盖 `login_by_email()` 和登录日志脱敏；
- `tests/test_email_registration_config.py`：覆盖受信代理默认配置。

普通测试注入 SES Client、Challenge Service、AuthService 和 Redis 替身，不进行真实腾讯云网络调用。

## 5. 验证命令

```bash
pdm run pytest tests/test_client_ip.py \
  tests/test_email_auth_service.py \
  tests/test_email_registration_api.py \
  tests/test_auth_api.py \
  tests/test_auth_service.py \
  tests/test_email_registration_config.py
pdm run ruff check .
pdm run pytest
pdm lock --check
git diff --check
```

第四阶段继续负责真实 SES 收信、真实 Redis 多进程并发、生产 nginx/CAM 配置和部署检查，本阶段不执行真实外部服务冒烟。
