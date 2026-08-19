# QQ OAuth 第二阶段开发执行记录

## 1. 执行范围

本次根据 [QQ OAuth 第二阶段实施计划](QQ_OAUTH_PHASE_2_IMPLEMENTATION_PLAN.md)，完成“QQ OAuth 服务、state 与 ticket 基础能力”。

本阶段已完成：

1. 新增同步 `QQOAuthService`，统一封装 QQ authorize、token、openid 和用户资料交互；
2. 使用高熵随机 state 和 ticket，并在 Redis key 中只保存 SHA-256；
3. 使用 Redis Lua `GET + DELETE` 原子消费 state 和 ticket；
4. 兼容 QQ token 的 JSON/查询字符串响应和 openid 的严格 JSON/JSONP 响应；
5. 严格校验 QQ `client_id`、openid、provider 错误码、HTTP 状态和响应格式；
6. 实现 QQ identity 查找、资料更新、首次 QQ-only 用户注册和启用 `normal` 角色关联；
7. 实现用户禁用/拉黑校验，以及身份唯一冲突恢复；
8. 授权完成只签发一次性 ticket，不创建本系统 `AuthSession`、JWT 或 Refresh Token；
9. 扩展 OAuth/QQ OAuth 日志字段脱敏；
10. 将 `httpx` 从开发依赖提升为正式运行依赖并重新生成 `pdm.lock`；
11. 新增 fake HTTP、FakeRedis、隔离 SQLite 数据库单元测试；
12. 导出本阶段实际执行记录。

本阶段明确未实现：

- `GET /auth/qq/login`；
- `GET /auth/qq/callback`；
- `POST /auth/qq/exchange`；
- 任何其他对外 QQ HTTP 路由；
- `QQTicketExchangeRequest`、`/auth/me` 或 `UserResponse` 身份字段扩展；
- 邮箱绑定、解绑或补充邮箱；
- ticket 消费阶段调用 `AuthService.complete_login()`；
- 真实 QQ 网络联调；
- 生产数据库迁移、生产 Redis 操作或生产部署；
- push。

## 2. 实现摘要

### 2.1 `QQOAuthService`

新增 [app/services/qq_oauth_service.py](../app/services/qq_oauth_service.py)，服务构造函数支持注入：

- SQLAlchemy `DbSession`；
- `Settings`；
- Redis 客户端；
- 同步 HTTP GET callable 或兼容 `.get()` 的 HTTP 客户端。

主要接口：

- `build_authorize_url() -> str`：校验配置、生成 state，并返回包含固定 `QQ_REDIRECT_URI` 的 QQ 授权地址；
- `complete_authorization(code, state) -> str`：消费 state、完成 QQ HTTP 交互和身份处理，并返回一次性 ticket；
- `issue_ticket(user_id) -> str`；
- `consume_ticket(ticket) -> int`；
- `upsert_identity(profile) -> User`：处理既有 QQ 身份和首次注册。

错误边界使用固定业务消息。超时、请求错误、非 2xx、QQ 错误码、格式异常和关键字段缺失不会把原始第三方响应、授权 code、access token、openid 或其他敏感值传入异常文本。

### 2.2 state 和 ticket

state：

- `secrets.token_urlsafe(32)` 生成；
- Redis key 为 `QQ_STATE_PREFIX + sha256(state)`；
- Redis value 仅为固定的 `pending` 标记；
- 使用配置 TTL 和 `NX` 写入；
- Lua 脚本原子 `GET + DELETE` 消费。

ticket：

- `secrets.token_urlsafe(48)` 生成；
- Redis key 为 `QQ_TICKET_PREFIX + sha256(ticket)`；
- Redis value 仅为十进制 `user_id`；
- 使用配置 TTL 和 `NX` 写入；
- Lua 脚本原子 `GET + DELETE` 消费；
- 消费只返回正整数 user id，不创建本系统 Session 或 Token。

### 2.3 QQ HTTP 边界

实现了固定 endpoint 和超时参数传递：

- token 请求携带 `client_id`、`client_secret`、授权 code 和固定 callback；
- token 响应兼容 JSON 和查询字符串；
- openid 响应兼容纯 JSON 和严格 `callback({...});` JSONP；
- openid 的 `client_id` 必须与配置的 `APP_ID` 完全相等；
- openid 必须为非空字符串；
- user-info 响应若包含 `ret`，必须为 `0`；
- 昵称和头像按 ORM 字段长度限制截断；
- provider 错误、空响应和不可信响应统一映射为安全错误。

### 2.4 身份事务

既有身份：

- 按 `(provider="qq", provider_subject)` 查询；
- 锁定对应用户并调用 `ensure_user_can_authenticate()`；
- 更新 QQ display name、avatar、verified 和 last login 时间；
- 只在本地 `User.display_name` 为空时补充昵称。

首次注册：

- 要求存在启用的 `normal` 角色；
- 同一事务创建 `email=None`、`hashed_password=None` 的 active QQ-only 用户；
- 创建 `UserIdentity` 和 `user_roles` 关联；
- 唯一约束冲突通过保存点回滚并重新读取已提交身份；
- 认证失败或角色不可用时回滚，不保留孤立用户和 identity。

## 3. 日志和依赖变更

### 3.1 日志脱敏

修改 [app/core/logging.py](../app/core/logging.py)，扩展 key/value 和 JSON 字段脱敏，覆盖：

- `APP_KEY` / `app_key`；
- `oauth_code`、`qq_code`、`authorization_code`；
- `state`、`oauth_state`、`qq_state`；
- `ticket`、`oauth_ticket`、`qq_ticket`；
- `openid`、`qq_openid`；
- `oauth_access_token`、`qq_access_token` 以及既有 `access_token`。

没有增加通用 `code` 或 `id` 匹配，因此普通 `code=ordinary-code` 和 `id=ordinary-id` 仍保持原文。

### 3.2 `httpx` 运行依赖

修改 [pyproject.toml](../pyproject.toml)，将 `httpx>=0.28.1` 从 `[dependency-groups].dev` 移至 `[project].dependencies`，保证生产安装默认依赖时可以加载 QQ OAuth 服务。

使用 PDM 重新生成 [pdm.lock](../pdm.lock)。

## 4. 测试覆盖

新增 [tests/test_qq_oauth_service.py](../tests/test_qq_oauth_service.py)，覆盖：

1. 配置缺失安全失败；
2. 授权 URL 参数、固定 callback 和 state 存储；
3. state/ticket 哈希 key、最小 Redis value 和 TTL；
4. state 并发消费至多一次成功；
5. ticket 重复消费、伪造值、非正整数和格式错误；
6. token JSON/查询字符串解析；
7. openid JSON/严格 JSONP 解析；
8. `client_id` 完全匹配和 openid 非空校验；
9. timeout 传递和 HTTP 异常安全映射；
10. 首次 QQ-only 注册、identity 和 `normal` 角色关联；
11. 已有 identity 复用、资料更新和本地 display name 保护；
12. 禁用/拉黑用户拒绝；
13. normal 角色缺失/禁用时完整回滚；
14. profile 字段长度边界；
15. 授权完成不创建 `AuthSession`，Redis 不保存 access token 或 openid。

扩展 [tests/test_logging.py](../tests/test_logging.py)，验证 OAuth 敏感字段脱敏以及普通 `code`/`id` 不被误伤。

所有 OAuth 测试使用 fake HTTP、FakeRedis 和隔离 SQLite 数据库，不访问真实 QQ、共享 Redis 或共享数据库。

## 5. 实际验证结果

### 5.1 第二阶段定向测试

执行：

```bash
pdm run pytest tests/test_qq_oauth_service.py tests/test_logging.py
```

结果：

```text
20 passed, 1 warning
```

警告为现有依赖环境中的 Starlette/httpx 弃用提示：

```text
StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
```

### 5.2 完整默认测试

执行：

```bash
pdm run pytest
```

结果：

```text
321 passed, 16 skipped, 1 warning
```

16 个跳过项是项目中需要显式环境或基础设施的既有迁移/集成测试。完整默认测试没有失败。

### 5.3 第二阶段定向 Ruff

执行：

```bash
pdm run ruff check \
  app/services/qq_oauth_service.py \
  app/core/logging.py \
  tests/test_qq_oauth_service.py \
  tests/test_logging.py
```

结果：

```text
All checks passed!
```

### 5.4 仓库级 Ruff

执行：

```bash
pdm run lint
```

结果：未通过，报告的是仓库既有问题，主要包括：

- `alembic/versions/0001_initial_auth_schema.py` 至 `0006_email_registration.py` 的 `I001` import-order；
- 既有管理端 API 的 `B008` `Depends(...)`/依赖工厂调用；
- 其他既有模块的 import-order 和依赖声明风格问题。

本阶段新增或修改的 QQ OAuth、日志和测试文件定向 Ruff 已通过。本次未扩大范围修改历史 Alembic 或无关管理端 API。

### 5.5 锁文件和差异检查

执行：

```bash
pdm lock --check
git diff --check
```

结果：均通过。

PDM 锁文件重新生成时实际报告：

```text
Changes are written to pdm.lock.
0:01:51 🔒 Lock successful.
```

## 6. 安全边界确认

- 未读取、修改或覆盖运行时 `.env`；
- 未把真实 `APP_KEY`、数据库密码或其他 Secret 写入新增代码、计划、执行记录或测试输出；
- state/ticket 明文不写入 Redis；
- Redis 不保存 QQ access token、openid、授权 code、JWT、Refresh Token 或完整用户资料；
- provider 原始响应和敏感参数不进入业务异常文本；
- 日志脱敏覆盖 OAuth 相关字段；
- 测试不访问真实 QQ、共享 Redis 或生产数据库；
- 未新增对外 QQ 路由；
- 未在第二阶段提前创建本系统 Session、JWT 或 Refresh Token；
- 未执行真实 QQ 联调、生产迁移、生产部署或 push。

## 7. 文件变更摘要

新增：

```text
app/services/qq_oauth_service.py
plan/QQ_OAUTH_PHASE_2_IMPLEMENTATION_PLAN.md
plan/QQ_OAUTH_PHASE_2_EXECUTION.md
tests/test_qq_oauth_service.py
```

修改：

```text
app/core/logging.py
pdm.lock
pyproject.toml
tests/test_logging.py
```

工作区中其他已有修改保持不变。本记录生成时尚未执行 commit；本阶段未执行 push。

## 8. 第二阶段结论

QQ OAuth 第二阶段的内部服务、state/ticket 基础能力、HTTP 解析、身份事务、日志脱敏、测试和依赖锁定已完成。

定向测试、完整默认 pytest、定向 Ruff、`pdm lock --check` 和 `git diff --check` 均通过。仓库级 Ruff 仍有既有历史问题，但不涉及本阶段新增或修改文件。

真实 QQ OAuth 登录闭环和对外路由仍属于后续阶段，不能据此记录宣称生产或真实第三方登录已经可用。
