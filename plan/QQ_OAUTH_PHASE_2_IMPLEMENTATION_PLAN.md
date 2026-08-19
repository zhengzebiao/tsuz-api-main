# QQ OAuth 第二阶段实施计划

## 1. 背景

本计划依据 [QQ 扫码登录接入完整方案](../docs/jiggly-scribbling-bunny.md)，实施第二阶段“QQ OAuth 服务、state 与 ticket 基础能力”。

第一阶段已经提供 QQ 配置、`UserIdentity` ORM 模型和 `0007_qq_login` 数据库迁移。本阶段在这些基础上新增可被后续路由复用的内部服务，完成 QQ HTTP 边界、一次性 state、QQ 身份查找与注册、一次性 ticket，但不开放任何对外 QQ 登录接口。

## 2. 阶段目标

1. 新增同步 `QQOAuthService`，统一封装 QQ authorize、token、openid 和 user-info 交互。
2. 使用高熵随机值和 Redis 哈希 key 实现一次性 state 与 ticket。
3. 使用 Redis Lua `GET + DELETE` 原子消费，防止重放和并发重复消费。
4. 兼容 QQ token 的 JSON/查询字符串响应和 openid 的 JSON/JSONP 响应。
5. 对 QQ 返回的 `client_id`、`openid`、错误码和响应格式进行严格校验。
6. 实现已有 QQ 身份资料更新和首次 QQ-only 用户自动注册事务。
7. 给首次 QQ 用户关联启用的 `normal` 角色，并通过数据库唯一约束处理并发首次登录。
8. 在授权完成后只签发一次性 ticket，不创建本系统 Session、JWT 或 Refresh Token。
9. 扩展日志脱敏规则，避免 OAuth 凭证和身份标识进入日志。
10. 将 `httpx` 提升为生产运行依赖并同步锁文件。

## 3. 范围边界

### 3.1 本阶段实现

- `app/services/qq_oauth_service.py`；
- 固定 QQ 授权 URL 构建；
- QQ token、openid 和用户资料 HTTP 请求与解析；
- state/ticket 生成、哈希 key、TTL、`NX` 写入和原子消费；
- QQ identity 查询、更新和首次注册；
- QQ-only `User` 创建；
- 启用的 `normal` 角色关联；
- 数据库唯一冲突恢复和孤立用户保护；
- 用户禁用/拉黑状态校验；
- QQ OAuth 日志敏感字段脱敏；
- fake HTTP、FakeRedis 和隔离数据库单元测试；
- `httpx` 正式依赖和 `pdm.lock` 更新；
- 本中文计划和第二阶段执行记录。

### 3.2 本阶段不实现

- `GET /auth/qq/login`；
- `GET /auth/qq/callback`；
- `POST /auth/qq/exchange`；
- 任何其他 QQ 对外 HTTP 路由；
- `QQTicketExchangeRequest`；
- `/auth/me` 或 `UserResponse` 身份字段扩展；
- 邮箱绑定、解绑或补充邮箱；
- 消费端前端改造；
- 在 ticket 消费阶段调用 `AuthService.complete_login()`；
- 真实 QQ 网络联调；
- 生产数据库迁移、生产 Redis 操作或生产部署；
- commit 或 push，除非后续明确要求。

## 4. 安全约束

1. `APP_KEY`、数据库密码和其他 Secret 不写入代码、计划、执行记录、测试输出或日志。
2. 不读取、修改或覆盖运行时 `.env`。
3. callback 使用固定的 `QQ_REDIRECT_URI`，不从请求 Host、请求参数或外部输入动态拼接。
4. state/ticket 明文只返回给调用方，Redis key 只保存其 SHA-256；Redis value 不保存明文 state/ticket。
5. Redis 不保存 QQ access token、openid、authorization code、用户完整资料、本系统 Access Token 或 Refresh Token。
6. 原始 QQ response body、code、access token、openid、state、ticket 和 `APP_KEY` 不进入异常消息或日志。
7. 单元测试只使用 fake HTTP、FakeRedis 和隔离数据库，不访问真实 QQ、共享 Redis、共享数据库或生产资源。
8. 所有测试和执行记录只描述真实执行结果，不预填或虚报结果。

## 5. `httpx` 正式依赖调整

当前 `httpx` 只位于 `[dependency-groups].dev`，但第二阶段生产代码会在 `app/services/qq_oauth_service.py` 中直接导入并调用它。

生产部署通常只安装 `[project].dependencies`，不会安装开发依赖组。如果仍把 `httpx` 留在 dev 组，生产进程加载 QQ OAuth 服务时可能出现：

```text
ModuleNotFoundError: No module named 'httpx'
```

因此本阶段将 `httpx>=0.28.1` 从 dev 组移动到正式运行依赖，并通过 PDM 重新生成锁定结果，使 `httpx` 属于 default 依赖组。该调整只改变依赖声明和锁文件，不会读取 `APP_KEY`，也不会发起真实 QQ 请求。

## 6. 服务接口与异常边界

`QQOAuthService` 接收 SQLAlchemy `DbSession`，并允许注入：

- `Settings`；
- Redis 客户端；
- HTTP GET callable 或客户端。

生产默认复用 `get_redis()` 和同步 `httpx`；测试完全注入 fake。

主要接口：

- `build_authorize_url() -> str`：校验配置、生成并保存 state，返回 QQ 授权 URL；
- `complete_authorization(code, state) -> str`：消费 state，完成 QQ HTTP 交互、身份处理并返回 ticket；
- `issue_ticket(user_id) -> str`：签发一次性 ticket；
- `consume_ticket(ticket) -> int`：原子消费 ticket，只返回正整数 user id；
- 内部 identity upsert 方法：已有身份更新或首次注册。

异常使用固定、安全的业务消息。HTTP timeout、请求错误、非 2xx、QQ 错误码、格式错误和关键字段缺失统一转换为 provider 错误，不传播原始响应或包含敏感查询参数的底层异常文本。

## 7. state 与 ticket 设计

### 7.1 state

- 使用 `secrets.token_urlsafe(32)`，提供约 32 字节随机熵；
- Redis key：`QQ_STATE_PREFIX + sha256_text(state)`；
- Redis value：固定非敏感标记；
- TTL：`QQ_STATE_TTL_SECONDS`，默认 300 秒；
- 写入使用 `NX`；随机碰撞时重新生成；
- Lua 脚本在 Redis 内原子执行 `GET` 和 `DEL`；
- 缺失、过期、错误和重复消费统一失败；
- 并发消费同一 state 至多一个成功。

### 7.2 ticket

- 使用 `secrets.token_urlsafe(48)`；
- Redis key：`QQ_TICKET_PREFIX + sha256_text(ticket)`；
- Redis value：仅十进制 `user_id`；
- TTL：`QQ_TICKET_TTL_SECONDS`，默认 60 秒；
- 写入使用 `NX`，消费使用 Lua 原子 `GET + DELETE`；
- 缺失、过期、伪造、非正整数和重复消费统一失败；
- 消费方法不创建 Session、JWT 或 Refresh Token。

## 8. QQ HTTP 与响应解析

### 8.1 授权 URL

授权 URL 固定包含：

- `response_type=code`；
- `client_id=APP_ID`；
- 固定 `redirect_uri=QQ_REDIRECT_URI`；
- `scope=get_user_info`；
- 高熵 state。

授权 URL 不包含 `APP_KEY`，不接受外部 callback 参数。

### 8.2 Token

请求固定 token endpoint，并传入授权码、APP ID、APP KEY 和固定 callback。

兼容：

- JSON object；
- 查询字符串 `access_token=...&expires_in=...`。

只接受非空字符串 `access_token`。QQ `error`、`error_description`、非零 `ret`、格式错误或缺少 token 均安全失败。

### 8.3 OpenID

请求固定 openid endpoint，兼容：

- 纯 JSON object；
- 严格 JSONP `callback({...});`。

要求：

- `client_id` 与配置的 `APP_ID` 完全相等；
- `openid` 是非空字符串；
- JSONP 不允许额外前后内容；
- 原始 body 和 openid 不进入日志或异常。

### 8.4 用户资料

请求参数包括：

- `access_token`；
- `oauth_consumer_key=APP_ID`；
- `openid`。

若响应包含 `ret`，必须等于 0。安全提取昵称和头像，字段按 ORM 长度限制处理：

- `display_name` 最大 128；
- `avatar` 最大 2048；
- `provider_subject` 最大 255。

## 9. 身份事务与并发

### 9.1 已有身份

1. 按 `provider="qq"` 和 `provider_subject=openid` 查询并锁定 identity；
2. 锁定并加载对应 user；
3. 调用 `ensure_user_can_authenticate()`；
4. 更新 identity 的展示名、头像、`verified` 和 `last_login_at`；
5. 仅在本地 `User.display_name` 为空时补充 QQ 昵称；
6. 不覆盖用户已经维护的非空本地展示名；
7. 提交更新。

### 9.2 首次注册

同一数据库事务内：

1. 查询并锁定启用的 `normal` 角色；
2. 创建 `email=None`、`hashed_password=None` 的 active QQ-only 用户；
3. 创建 QQ `UserIdentity`；
4. 插入 `user_roles`；
5. 提交事务。

角色缺失或禁用时回滚，不留下半成品用户、identity 或角色关联。

### 9.3 并发首次登录

可能发生唯一冲突的用户、identity 和角色写入放入 `begin_nested()` 保存点。数据库联合唯一约束作为最终兜底：

- 仅把 `(provider, provider_subject)` 唯一冲突视为并发同一 QQ 身份；
- 保存点回滚后重新查询已提交 identity；
- 重新加载 user 并执行 active/blacklist 校验；
- 返回同一用户；
- 其他约束错误不吞掉；
- 保存点回滚同时移除本次临时 User，避免孤立用户。

授权完成后只签发 ticket，不调用 `AuthService.complete_login()`。

## 10. 日志脱敏

扩展 `app/core/logging.py` 的字段级规则，覆盖：

- `APP_KEY` / `app_key`；
- `oauth_code`、`qq_code`、`authorization_code`；
- `state`、`oauth_state`、`qq_state`；
- `ticket`、`qq_ticket`、`oauth_ticket`；
- `openid`、`qq_openid`；
- QQ/OAuth access token。

不增加过宽的通用 `code` 或 `id` 匹配，避免误伤普通业务日志。现有 password、JWT、Bearer、数据库 URL、PEM key 和 app secret 脱敏继续保留。

## 11. 测试设计

新增 `tests/test_qq_oauth_service.py`，覆盖：

1. 配置缺失时安全失败；
2. 授权 URL 参数和固定 callback；
3. state/ticket Redis key 不含明文、value 最小化、TTL 正确；
4. state/ticket 重复和并发消费至多一次成功；
5. ticket value 非正整数或格式错误时失败；
6. token JSON/查询字符串解析；
7. openid JSON/JSONP 解析；
8. `client_id` 严格匹配和 openid 非空校验；
9. timeout、非 2xx、QQ 错误码和格式错误的安全异常；
10. 首次 QQ-only 注册和 `normal` 角色关联；
11. 已有身份复用、资料更新和本地展示名保护；
12. 禁用/拉黑用户拒绝；
13. 角色缺失/禁用时完整回滚；
14. 唯一冲突恢复且不产生孤立用户；
15. 授权完成只创建 identity 和 ticket，不创建本系统 `AuthSession`；
16. 异常与日志不包含 APP KEY、code、access token、openid、state、ticket 或 QQ 原始 body；
17. QQ OAuth 日志字段脱敏且普通 `code`/`id` 不被误伤。

## 12. 文件清单

### 12.1 新增

- `app/services/qq_oauth_service.py`；
- `tests/test_qq_oauth_service.py`；
- `plan/QQ_OAUTH_PHASE_2_IMPLEMENTATION_PLAN.md`；
- `plan/QQ_OAUTH_PHASE_2_EXECUTION.md`。

### 12.2 修改

- `app/core/logging.py`；
- `tests/test_logging.py`；
- `pyproject.toml`；
- `pdm.lock`。

不修改：

- `app/api/auth.py`；
- `app/schemas/auth.py`；
- 第三阶段 `/auth/me`、exchange Schema 和路由。

## 13. 验证命令

```bash
pdm run pytest \
  tests/test_qq_oauth_service.py \
  tests/test_auth_service.py \
  tests/test_email_auth_service.py \
  tests/test_redis_state_services.py \
  tests/test_logging.py
pdm run pytest
pdm run ruff check \
  app/services/qq_oauth_service.py \
  app/core/logging.py \
  tests/test_qq_oauth_service.py \
  tests/test_logging.py
pdm run lint
pdm lock --check
git diff --check
```

仓库级 lint 如仍只包含已有历史问题，应在执行记录中如实列出，不扩大范围修改历史迁移或无关 API。

## 14. 验收标准

- QQ HTTP、state、ticket 和身份事务基础能力已实现；
- 没有新增对外 QQ 路由或第三阶段响应行为；
- state/ticket 一次性消费和并发语义符合设计；
- Redis、异常和日志不泄露明文凭证或 QQ 身份标识；
- 首次注册、normal 角色、禁用/拉黑保护和唯一冲突恢复符合设计；
- 授权完成不创建 AuthSession、JWT 或 Refresh Token；
- `httpx` 属于正式运行依赖且锁文件一致；
- 定向测试、完整 pytest、Ruff、锁检查和差异检查结果被真实记录；
- 真实 QQ 联调、生产迁移和生产部署明确保留到后续阶段。
