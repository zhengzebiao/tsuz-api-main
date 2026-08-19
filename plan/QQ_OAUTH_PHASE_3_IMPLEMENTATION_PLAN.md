# QQ OAuth 第三阶段实施计划：认证路由、响应模型与兼容调整

## 目标

根据 `docs/jiggly-scribbling-bunny.md` 第三阶段要求，将第二阶段已完成的 `QQOAuthService` 接入现有认证闭环，并让 `/auth/me`、管理端响应和密码重置安全支持 QQ-only 用户。

## 范围

- 新增严格的 `QQTicketExchangeRequest`。
- 新增不暴露 `provider_subject`、openid、内部 ID 或令牌的 `UserIdentityResponse`。
- 扩展 `UserResponse`，保留既有 `id`、`username`、`roles`，增加权限、展示名、头像和安全身份列表。
- 仅新增：
  - `GET /auth/qq/login`
  - `GET /auth/qq/callback`
  - `POST /auth/qq/exchange`
- 回调仅使用配置中的固定 `QQ_TICKET_REDIRECT_URI`，不接受请求提供的回跳地址。
- ticket 交换先原子消费 ticket，再复用 `AuthService.complete_login()` 创建现有 Session、JWT 和 Refresh Token。
- 扩展 `AuthService.current_user()`，保持既有 JWT、黑名单、Session、用户状态和 RBAC 校验。
- 对 QQ-only 用户的空邮箱、空密码路径做最小兼容；管理员密码重置明确返回 409 且不产生变更。
- 管理端只读 `email` 响应允许为 `null`，创建和更新输入规则保持不变。
- 使用 Fake QQ 服务、FakeRedis 和隔离 SQLite 测试，不进行真实 QQ、生产数据库/Redis、部署或 push 操作。

## 实施步骤

1. 更新认证和管理员响应模型。
2. 在认证路由中加入固定目标的 QQ 登录、回调和 ticket 交换，并定义稳定错误映射。
3. 扩展认证服务的 `/auth/me` 数据查询和空密码保护。
4. 增加 QQ-only 管理员密码重置业务错误和 API 映射。
5. 增加路由、服务、schema、管理员回归测试。
6. 执行 focused pytest、全量 pytest、focused Ruff、仓库 lint、锁文件和 diff 检查。
7. 仅根据真实命令结果编写执行记录。

## 安全和边界

- 不读取或修改运行时 `.env`，不把 `APP_KEY` 或其他 secret 写入源代码、计划、执行记录、测试输出或日志。
- Redis 仅使用 state/ticket 的 SHA-256 key；不保存明文 state/ticket、QQ access token、openid、授权码、本系统 token 或完整 profile。
- 回调失败只向固定消费端追加稳定的 `qq_error=oauth_failed`，不拼接敏感值或异常文本。
- 不实现 QQ 与邮箱绑定/解绑、邮箱补充、Cookie 认证、全局 Cookie CSRF、动态回跳、`/auth/qq/url` 或独立 JWT/Session/RBAC。

## 预期验收

- 三个 QQ 路由注册且旧认证路由保持可用。
- ticket 只能消费一次，交换前不创建本地登录 Session。
- QQ-only `/auth/me` 返回 `username: null`，身份响应不泄露 provider subject。
- QQ-only 管理员密码重置返回 `409 PASSWORD_RESET_UNAVAILABLE` 且密码、版本、Session、审计记录均不改变。
- 全量测试和本阶段 focused Ruff 通过；仓库级 lint 的既有无关问题如实记录。
