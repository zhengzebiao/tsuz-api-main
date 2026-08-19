# QQ 扫码登录接入完整方案

## 一、背景与目标

当前后端采用 FastAPI + SQLAlchemy/Alembic + PostgreSQL + Redis + RS256 JWT。现有账号和邮箱登录都会复用 `AuthService.complete_login()` 创建数据库会话、Access Token 和 Refresh Token，后续通过 Bearer Token 完成鉴权；认证路由统一使用 `/auth/...`。

本次只在后端接入 QQ 网站应用 OAuth 登录，不改造 HttpOnly Cookie，也不修改或关注消费端前端页面。QQ 授权成功后，后端生成短期、一次性的 ticket，浏览器携带 ticket 跳回固定消费端地址；消费端再调用交换接口获得现有的 `TokenResponse`。

### 已确认的功能边界

- 用户访问 `GET /auth/qq/login` 后，后端直接返回 302 并跳转到 QQ 授权地址。
- 不增加“获取 QQ 授权 URL”接口。
- QQ 授权成功后自动注册并登录。
- 本期不实现 QQ 与邮箱的绑定、解绑，也不要求 QQ 用户补充邮箱。
- QQ OAuth 回调地址固定为：
  - 测试：`https://test-api.tusz.online/auth/qq/callback`
  - 正式：`https://api.tusz.online/auth/qq/callback`
- 后端路由继续保持 `/auth/xx`，不切换成 `/api/auth/xx`。
- QQ 回调完成后通过一次性 ticket 跳到固定环境消费端地址，不接受任意回跳 URL，也不携带业务上下文：
  - 测试默认：`https://test.tusz.online/login`
  - 正式默认：`https://tusz.online/login`
  - 实际地址由 `QQ_TICKET_REDIRECT_URI` 环境变量配置。
- 消费端拿 ticket 调用 `POST /auth/qq/exchange`，换取当前已有的 `TokenResponse`。
- 不新增“通过 access_token 登录”的接口。
- QQ-only 用户没有真实邮箱和密码，因此 `users.email`、`users.hashed_password` 调整为允许为空；现有邮箱用户的数据和行为保持不变。
- `UserResponse` 保留现有 `id`、`username`、`roles` 字段；QQ-only 用户的 `username` 返回 `null`。
- `UserResponse` 同级新增：
  - `permissions: string[]`
  - `display_name`
  - `avatar`
  - `identities`
- `identities` 不向消费端暴露 QQ openid，只返回 `provider`、`display_name`、`avatar`、`verified`。

## 二、接口和完整登录流程

### 1. 发起 QQ 登录

```http
GET /auth/qq/login
```

后端执行：

1. 生成不可预测的随机 `state`。
2. Redis 只保存 `sha256(state)` 对应的登录状态，TTL 为 300 秒。
3. 拼接 QQ 网站应用 OAuth 授权地址。
4. 返回 302，浏览器直接进入 QQ 授权页面。

本接口不接受任意回跳地址参数，也不提供 JSON 授权 URL 响应。

### 2. QQ 授权回调

```http
GET /auth/qq/callback?code=...&state=...
```

后端执行：

1. 原子消费 Redis 中的 state，校验过期和重放。
2. 使用 `code + APP_ID + APP_KEY + QQ_REDIRECT_URI` 向 QQ 换取 QQ access token。
3. 使用 QQ access token 获取 openid。
4. 校验 QQ 返回的 `client_id` 与配置的 `APP_ID` 完全一致。
5. 获取 QQ 用户昵称、头像等基本资料。
6. 根据 `(provider="qq", provider_subject=openid)` 查询身份记录。
7. 已存在身份：检查对应本地用户是否可登录，更新安全的 QQ 展示资料和最近登录时间。
8. 首次登录：自动创建本地用户、绑定 QQ 身份，并分配启用状态的 `normal` 角色。
9. 生成一次性 ticket，Redis 只保存 ticket 哈希和 `user_id`，TTL 为 60 秒。
10. 返回 302 到固定消费端地址：

```text
https://test.tusz.online/login?ticket=一次性ticket
```

正式环境同理使用正式消费端地址。

URL 中只出现短期、一次性 ticket，不出现 QQ access token、本系统 Access Token 或 Refresh Token。

### 3. 消费 ticket 换取本系统 Token

```http
POST /auth/qq/exchange
Content-Type: application/json

{
  "ticket": "一次性ticket"
}
```

后端执行：

1. 原子读取并删除 ticket。
2. ticket 不存在、已过期或已消费时统一返回 401。
3. 读取关联 `user_id`。
4. 调用现有 `AuthService.complete_login(user.id)`。
5. 返回当前已有的 `TokenResponse`：

```json
{
  "access_token": "...",
  "refresh_token": "...",
  "token_type": "Bearer",
  "expires_in": 3600
}
```

第一次交换成功后，同一个 ticket 再次提交必须失败。

## 三、配置和环境变量

### 1. 应用配置

修改 `app/core/config.py`，增加 QQ 配置。QQ 平台凭据严格使用已确认的环境变量名称：

```dotenv
APP_ID=
APP_KEY=
```

其余配置：

```dotenv
QQ_REDIRECT_URI=
QQ_TICKET_REDIRECT_URI=
QQ_AUTHORIZE_URL=https://graph.qq.com/oauth2.0/authorize
QQ_TOKEN_URL=https://graph.qq.com/oauth2.0/token
QQ_OPENID_URL=https://graph.qq.com/oauth2.0/me
QQ_USER_INFO_URL=https://graph.qq.com/user/get_user_info
QQ_STATE_PREFIX=auth:test:qq:state:
QQ_TICKET_PREFIX=auth:test:qq:ticket:
QQ_STATE_TTL_SECONDS=300
QQ_TICKET_TTL_SECONDS=60
QQ_HTTP_TIMEOUT_SECONDS=10
```

Pydantic 字段可使用别名或等价的显式映射，确保读取的是准确的 `APP_ID` 和 `APP_KEY`。

### 2. 环境矩阵

测试环境：

```dotenv
QQ_REDIRECT_URI=https://test-api.tusz.online/auth/qq/callback
QQ_TICKET_REDIRECT_URI=https://test.tusz.online/login
QQ_STATE_PREFIX=tsuz:main:test:qq:state:
QQ_TICKET_PREFIX=tsuz:main:test:qq:ticket:
```

正式环境：

```dotenv
QQ_REDIRECT_URI=https://api.tusz.online/auth/qq/callback
QQ_TICKET_REDIRECT_URI=https://tusz.online/login
QQ_STATE_PREFIX=tsuz:main:prod:qq:state:
QQ_TICKET_PREFIX=tsuz:main:prod:qq:ticket:
```

测试和正式环境应分别使用不同的 QQ 网站应用、`APP_ID` 和 `APP_KEY`。

### 3. 配置文件和部署注入

更新以下配置模板中与 QQ 有关的示例项，但不得写入真实 `APP_KEY`：

- `.env.local.example`
- 仓库实际使用的测试、正式环境模板
- `.env.deploy.example`

修改 `.github/workflows/deploy.yml`：

- `APP_ID`、`APP_KEY` 从 GitHub Environment Secrets 注入。
- QQ 回调地址、消费端回跳地址、Redis 前缀、TTL 和超时从 Environment Variables 注入。
- 生成服务器运行时 `.env` 时写入上述值。
- 不在日志、命令输出或仓库文件中打印密钥。

## 四、数据库和身份模型

### 1. 新增 QQ 身份表

新增 `app/models/user_identity.py`，定义 `UserIdentity`：

- `id`
- `user_id`：外键关联 `users.id`，用户删除时级联删除身份
- `provider`：本期固定为 `qq`
- `provider_subject`：QQ openid，仅后端保存和匹配
- `display_name`：QQ 昵称，可空
- `avatar`：QQ 头像地址，可空
- `verified`：第三方身份是否已验证
- `created_at`
- `updated_at`
- `last_login_at`

约束与索引：

- `(provider, provider_subject)` 唯一约束，防止同一 QQ 身份绑定多个本地用户。
- `user_id` 索引。
- 可增加 `(user_id, provider)` 索引，便于查询用户身份。

在 `app/models/__init__.py` 和 `alembic/env.py` 中导入该模型，确保 SQLAlchemy 元数据和 Alembic 能识别新表。

### 2. 调整用户表

修改 `app/models/user.py`：

```python
email: Mapped[str | None]
hashed_password: Mapped[str | None]
```

原因：QQ 自动注册用户没有真实邮箱，也没有本地密码。

邮箱注册、账号登录和管理员创建用户仍然要求提供合法邮箱与密码，因此这些业务接口的请求模型和校验不放宽；只是在数据库层允许 QQ-only 用户为空。

### 3. Alembic 迁移

新增 `alembic/versions/0007_qq_login.py`，上一个版本是 `0006_email_registration`。

升级内容：

1. 将 `users.email` 改为允许 NULL。
2. 保留邮箱唯一性；PostgreSQL 唯一索引天然允许多行 NULL。
3. 将 `users.hashed_password` 改为允许 NULL。
4. 创建 `user_identities` 表。
5. 创建身份唯一约束和索引。
6. 不修改或清洗现有邮箱用户数据。

降级内容：

- 在将字段恢复为非空前，检测是否存在 QQ-only 空邮箱/空密码用户。
- 如果存在无法安全降级的数据，迁移应明确失败，而不是静默填充伪邮箱、删除身份或破坏用户数据。
- 数据满足条件时再删除身份表并恢复非空约束。

## 五、QQ OAuth 服务设计

新增 `app/services/qq_oauth_service.py`，作为 QQ 平台、Redis 状态、用户身份创建之间的业务边界。

### 1. 可复用的现有能力

必须复用：

- `app/core/security.py` 中的 `sha256_text()`：哈希 state 和 ticket。
- `app/core/redis.py` 中的 `get_redis()`：获取 Redis 客户端。
- `app/services/authorization_service.py` 中的 `ensure_user_can_authenticate()`：禁止被禁用或拉黑用户通过 QQ 绕过限制。
- `app/services/auth_service.py` 中的 `complete_login()`：统一生成数据库 Session、Access Token、Refresh Token、角色和权限声明。
- `app/models/role.py` 中的 `user_roles`：给首次注册 QQ 用户分配 `normal` 角色。

不新建第二套 JWT、Refresh Token、Session 或 RBAC 逻辑。

### 2. state 管理

- 使用 `secrets.token_urlsafe()` 生成高熵 state。
- Redis Key 使用 `QQ_STATE_PREFIX + sha256(state)`。
- Redis 中不保存明文 state。
- TTL 为 300 秒。
- 回调校验使用 Redis 原子 GET+DELETE；可使用 Lua 脚本实现，兼容并保证一次性。
- 缺失、过期、错误、已消费的 state 均拒绝。

### 3. QQ HTTP 调用

服务负责调用：

- QQ authorize
- QQ token
- QQ openid
- QQ user info

要求：

- 设置连接/读取超时。
- 使用固定配置的 `QQ_REDIRECT_URI`，不根据请求 Host 动态拼接。
- token 接口兼容 QQ 历史上的查询字符串响应和 JSON 响应。
- openid 接口兼容纯 JSON 和 `callback({...});` JSONP 包装。
- 校验 `client_id == APP_ID`。
- openid 必须为非空字符串。
- QQ 返回异常、超时、格式错误统一转换为安全的业务错误。
- 不把 QQ 原始错误体直接返回给调用方。
- HTTP 客户端边界应可注入或可替换，单元测试不得真实请求 QQ。

若当前依赖没有合适的异步/同步 HTTP 客户端，可优先复用开发依赖中的 `httpx` 并将其加入正式依赖；也可以使用标准库实现小型同步客户端。实现时选择与现有同步 FastAPI 路由风格最一致、最易测试的一种，不引入完整 OAuth 框架。

### 4. 自动注册和并发一致性

已有 QQ 身份：

1. 查询 `(qq, openid)`。
2. 锁定或读取对应本地用户。
3. 调用 `ensure_user_can_authenticate()`。
4. 更新 QQ 昵称、头像和最近登录时间。
5. 不覆盖用户后续自行维护的顶层资料，除非顶层字段为空；身份表始终保存最新 QQ 展示资料。

首次 QQ 登录：

1. 查询并锁定启用状态的 `normal` 角色。
2. `normal` 角色不存在或已禁用时，返回服务不可用，不创建半成品用户。
3. 创建用户：`email=None`、`hashed_password=None`、`display_name=QQ昵称`、启用且未拉黑。
4. 创建 `UserIdentity(provider="qq", provider_subject=openid, ...)`。
5. 写入 `user_roles`，分配 `normal` 角色。
6. 在一个数据库事务内提交。

并发首次登录：

- 依靠 `(provider, provider_subject)` 唯一约束兜底。
- 捕获唯一冲突后回滚本次事务并重新查询已创建身份。
- 不得留下没有身份的孤立用户，也不得创建两个本地用户。

### 5. ticket 管理

- 使用 `secrets.token_urlsafe(48)` 等高熵随机值。
- Redis Key 使用 `QQ_TICKET_PREFIX + sha256(ticket)`。
- Redis Value 只保存 `user_id` 和必要状态，不保存 QQ access token、本系统 Token 或用户资料。
- TTL 为 60 秒。
- 使用原子 GET+DELETE 消费。
- 不在任何日志中记录 ticket 明文。
- ticket 交换后才调用 `AuthService.complete_login()`，避免用户完成 QQ 授权但未回到消费端时提前创建无用本地会话。

## 六、API 路由和响应模型

### 1. 新增路由

修改 `app/api/auth.py`，只新增以下三个 QQ 路由：

```text
GET  /auth/qq/login
GET  /auth/qq/callback
POST /auth/qq/exchange
```

不新增：

```text
GET /auth/qq/url
```

也不改动现有：

- `/auth/login`
- `/auth/email/*`
- `/auth/password/*`
- `/auth/refresh`
- `/auth/logout`
- `/auth/me`

原有 Bearer Token 提取逻辑不改为 Cookie 模式。

### 2. 回调失败策略

回调中的 OAuth 失败不能把 QQ 原始错误、code、openid、ticket 或内部异常拼到 URL。

建议在 `QQ_TICKET_REDIRECT_URI` 基础上，仅携带稳定且非敏感的错误码，例如：

```text
https://test.tusz.online/login?qq_error=oauth_failed
```

或者对配置缺失、Redis/数据库不可用等服务端故障直接返回通用 503。具体映射在 API 层固定，不接受外部指定失败地址。

### 3. Schema 调整

修改 `app/schemas/auth.py`。

新增 ticket 请求：

```python
class QQTicketExchangeRequest(_StrictModel):
    ticket: str = Field(min_length=1, max_length=256)
```

新增安全的身份响应：

```python
class UserIdentityResponse(BaseModel):
    provider: str
    display_name: str | None = None
    avatar: str | None = None
    verified: bool
```

扩展现有 `UserResponse`：

```python
class UserResponse(BaseModel):
    id: str
    username: str | None
    roles: list[str]
    permissions: list[str]
    display_name: str | None
    avatar: str | None
    identities: list[UserIdentityResponse]
```

列表字段使用 `Field(default_factory=list)`，避免共享可变默认值。

`identities` 明确不包含：

- `provider_subject`
- `openid`
- QQ access token
- 任何密钥或内部数据库 ID

### 4. `/auth/me` 扩展

修改 `AuthService.current_user()`：

1. 保留现有 JWT 校验、黑名单校验、Session 校验和用户状态校验。
2. 复用 `_build_claims()` 获得角色和有效权限。
3. 查询该用户的身份列表。
4. 顶层 `display_name` 优先使用本地用户资料。
5. 顶层 `avatar` 可从主身份（本期唯一 QQ 身份）派生。
6. 邮箱用户的 `username` 继续返回邮箱。
7. QQ-only 用户的 `username` 返回 `null`。
8. 返回同级 `permissions` 和安全的 `identities`。

已有邮箱用户仍可按照原结构读取 `id`、`username`、`roles`；新增字段为向后兼容扩展，仅 `username` 类型从必填字符串放宽为可空。

## 七、现有代码兼容调整

由于数据库字段允许为空，需要对依赖非空假设的代码做最小兼容处理：

- `AuthService.login_by_email()`：找到 QQ-only 用户或密码为空时统一按无效凭据处理，不能调用 `verify_password()` 传入空哈希。
- 邮箱找回密码：QQ-only 用户没有邮箱，不会被邮箱查询命中；原有防枚举行为保持不变。
- 管理端用户列表和角色关联搜索：对可空 email 的 `lower/contains` 查询保持 SQL NULL 安全，并允许响应中的 email 为空。
- `AdminUserResponse.email` 调整为可空，以便管理员查看 QQ-only 用户；管理员创建/更新用户的请求仍要求合法邮箱。
- 管理员密码重置：若对 QQ-only 用户设置新密码但没有邮箱，仍不能通过现有邮箱登录；本期可以明确拒绝或只允许在同时补充邮箱后操作，不在 QQ 登录流程中自动绑定邮箱。
- Seed、邮箱注册等已有创建路径继续写入非空邮箱和密码。

## 八、日志和安全要求

### 必须禁止记录

- `APP_KEY`
- QQ authorization code
- QQ access token
- openid 明文（业务日志中不记录；数据库身份匹配除外）
- 一次性 state
- 一次性 ticket
- 本系统 Access Token 和 Refresh Token

扩展 `app/core/logging.py` 的敏感字段脱敏规则，使 `APP_KEY`、`code`（仅在明确 OAuth 字段上下文中）、`ticket` 等字段不会因异常信息被输出；避免使用过宽规则误伤普通日志。

### 必须实现

- QQ OAuth state 防登录 CSRF。
- state 和 ticket 都是短期且一次性。
- 固定回调地址和固定消费端地址，避免开放重定向。
- `APP_KEY` 只在服务端和 Secret 管理系统中存在。
- QQ API 超时和异常隔离。
- 被禁用/拉黑用户无法从 QQ 重新登录。
- 数据库唯一约束防止身份重复。
- 回调错误响应不泄露第三方细节。

本方案继续使用 Bearer Token，不引入浏览器自动携带的认证 Cookie，因此本期不需要全局 Cookie CSRF 改造；QQ 登录流程本身仍必须依赖 state 防护。

## 九、需要修改或新增的关键文件

### 核心实现

- `app/core/config.py`
- `app/core/logging.py`
- `app/models/user.py`
- `app/models/user_identity.py`（新增）
- `app/models/__init__.py`
- `app/schemas/auth.py`
- `app/schemas/admin_user.py`
- `app/services/auth_service.py`
- `app/services/qq_oauth_service.py`（新增）
- `app/api/auth.py`
- `alembic/env.py`
- `alembic/versions/0007_qq_login.py`（新增）

### 部署配置

- `.env.local.example`
- `.env.deploy.example`
- 实际纳入版本管理的测试/正式示例环境文件
- `.github/workflows/deploy.yml`

### 测试

- `tests/test_qq_oauth_service.py`（新增）
- `tests/test_qq_auth_api.py`（新增）
- `tests/test_auth_service.py`
- `tests/test_auth_api.py`
- `tests/test_user_management_models.py`
- 管理端用户 schema/service/API 相关测试
- 新增或扩展 QQ 迁移测试
- `tests/test_phase_4_deployment_checks.py` 或新的 QQ 部署配置检查文件

## 十、测试与验收

### 1. 配置测试

验证：

- `APP_ID`、`APP_KEY` 能被正确加载。
- QQ URL、TTL、前缀、超时有正确默认值。
- 测试和正式 Redis 命名空间隔离。
- 缺少必要 QQ 配置时，登录入口安全失败，不生成不完整授权地址。

### 2. OAuth 和 Redis 单元测试

覆盖：

- `/auth/qq/login` 返回 302。
- 授权地址包含正确 `APP_ID`、固定回调 URL、scope 和随机 state。
- Redis 只存 state 哈希，不存明文。
- state 过期、错误、缺失和重放均失败。
- token 响应兼容 JSON 和查询字符串。
- openid 响应兼容 JSON 和 JSONP。
- `client_id` 不匹配时拒绝。
- openid 缺失时拒绝。
- QQ 超时、错误码和格式异常映射为安全错误。
- 日志中不出现 APP_KEY、code、QQ access token、openid、state、ticket。

### 3. 自动注册和身份测试

覆盖：

- 首次 QQ 登录创建 `email=None`、`hashed_password=None` 的本地用户。
- 自动分配启用的 `normal` 角色。
- 创建唯一 QQ 身份。
- 缺失或禁用 `normal` 角色时事务完全回滚。
- 再次登录复用同一个本地用户。
- 更新 QQ 身份昵称、头像、最近登录时间。
- 被禁用或拉黑用户登录失败。
- 两个并发首次登录不会创建重复身份或孤立用户。

### 4. ticket 测试

覆盖：

- ticket 使用高熵随机值。
- Redis 只保存 ticket 哈希和 `user_id`。
- TTL 为 60 秒。
- 回调地址只含 ticket，不含任何 token 或 openid。
- 第一次交换成功并返回现有 `TokenResponse`。
- 第二次交换同一个 ticket 返回 401。
- 过期、伪造或缺失 ticket 返回统一 401。
- ticket 尚未交换前不创建本系统登录 Session。

### 5. UserResponse 和兼容性测试

覆盖：

- 现有邮箱用户仍返回 `id`、邮箱 `username`、`roles`。
- 新增 `permissions` 为启用且已声明的有效权限列表。
- QQ-only 用户 `username` 为 `null`。
- 返回 `display_name`、`avatar` 和 `identities`。
- identity 响应不包含 openid/provider_subject。
- 现有账号登录、邮箱登录、刷新、登出、权限校验全部保持通过。
- 密码为空时账号登录不会触发哈希校验异常。

### 6. 迁移测试

从 `0006_email_registration` 开始验证：

1. 写入一个旧版邮箱用户。
2. 升级到 `0007_qq_login`。
3. 旧用户和角色权限数据完整保留。
4. email/password 字段允许 NULL。
5. 可创建 QQ-only 用户和身份。
6. 身份唯一约束有效。
7. `alembic check` 无模型漂移。
8. 无 QQ-only 数据时可以安全降级。
9. 存在空邮箱/密码 QQ 用户时，降级明确失败且不破坏数据。

### 7. 部署检查

验证 `.github/workflows/deploy.yml`：

- 从 Secrets 注入 `APP_ID`、`APP_KEY`。
- 生成环境文件包含所有 QQ 配置。
- 不输出秘密。
- 测试与正式回调地址正确。
- 测试与正式 Redis 前缀隔离。

### 8. 执行命令

```bash
pdm run lint
pdm run test
pdm run alembic-current
```

如果启用 PostgreSQL/Redis 集成验证，再按照项目现有的 opt-in 环境变量运行对应迁移和集成测试。

### 9. 测试数据库与 Redis 隔离

测试使用的数据库需要区分测试类型，不能默认连接开发、测试业务库或正式数据库：

1. **单元测试和大部分 API 测试**：优先使用 SQLite `:memory:`、事务回滚或 Mock，不依赖持久化 PostgreSQL 数据；测试结束后销毁 Session 和 Engine。
2. **Alembic 迁移测试**：连接专用的本地 PostgreSQL 管理地址，在测试开始时创建带随机后缀的临时数据库，执行基础版本到 `0007_qq_login` 的升级、检查和降级验证；测试完成后在 `finally` 中终止连接并 `DROP DATABASE`。迁移测试默认不执行，只有显式设置 opt-in 环境变量时才运行。
3. **PostgreSQL/Redis 集成测试**：必须使用与业务数据隔离的专用测试数据库和 Redis 命名空间。推荐每次测试创建随机数据库名，并使用随机 Redis key prefix；测试结束后删除测试数据库、清理该 prefix 下的 Redis Key 并关闭连接。若受现有基础设施限制只能使用长期运行的测试库，也必须使用专用账号、专用库和专用 prefix，禁止指向生产库或共享业务数据，并在测试前后清理测试数据。
4. **真实 QQ 端到端联调**：只允许使用 QQ 测试应用、测试回调地址和隔离的测试数据库/Redis；不得复用正式 `APP_ID`、`APP_KEY`、正式数据库或正式 Redis。联调产生的用户、身份、Session、Refresh Token 和 Redis Key 应在验收后清理。

迁移测试和集成测试应在测试日志中输出“使用隔离测试资源”的状态，但不得输出完整数据库连接串、密码、`APP_KEY`、Token、state、ticket 或 openid。文档中出现的 `.env.test` 仅表示测试环境配置模板，不代表普通测试可以直接复用其中的持久化业务数据；执行前应确认 `DATABASE_URL` 和 `REDIS_URL` 指向专用测试资源。

上线前使用真实 QQ 测试应用进行端到端验收：

```text
/auth/qq/login
→ QQ 扫码授权
→ /auth/qq/callback
→ 固定消费端 ?ticket=...
→ POST /auth/qq/exchange
→ TokenResponse
→ Bearer Token 调用 /auth/me
```

确认测试环境完整闭环后，再配置正式 QQ 应用和正式回调地址上线。

## 十一、实施顺序

本模块采用四个阶段分步开发。每个阶段完成后先执行本阶段验收，再进入下一阶段；不跨阶段提前开放业务接口。测试阶段不得真实请求 QQ，统一对 HTTP 客户端、Redis 和外部依赖做边界 Mock；真实 QQ 联调只在最后阶段使用测试应用执行。

### 第一阶段：配置、用户模型与数据库迁移

开发内容：

1. 扩展 `app/core/config.py`，增加 `APP_ID`、`APP_KEY`、固定回调地址、固定消费端回跳地址、QQ API 地址、Redis 前缀、TTL 和 HTTP 超时配置；
2. 更新 `.env.local.example`、`.env.deploy.example` 及测试/正式环境模板，确认 `APP_KEY` 只能通过 Secret 注入；
3. 修改 `app/models/user.py`，将 `email` 和 `hashed_password` 调整为可空，同时保持邮箱注册、邮箱登录和管理员创建用户的请求校验不变；
4. 新增 `app/models/user_identity.py`，定义 QQ 身份字段、外键级联关系、`(provider, provider_subject)` 唯一约束和必要索引；
5. 在 `app/models/__init__.py`、`alembic/env.py` 中注册新模型；
6. 新增 `alembic/versions/0007_qq_login.py`，放宽用户字段非空约束并创建 `user_identities` 表；
7. 为迁移补充安全降级检查：存在 QQ-only 用户时不得通过填充伪数据或删除身份记录强行降级；
8. 补充模型、迁移和配置加载测试，确认现有邮箱用户数据不被修改。

本阶段不实现：

- QQ HTTP 调用；
- Redis state 或 ticket；
- QQ 登录路由和交换接口；
- 自动注册用户事务。

阶段验收：

- ORM、数据库迁移和 Alembic 元数据一致；
- `0007_qq_login` 可以安全升级，既有邮箱用户和角色权限完整保留；
- QQ-only 用户可以保存空邮箱和空密码；
- `(provider, provider_subject)` 唯一约束有效；
- 缺少 QQ 配置时不会生成不完整的授权地址；
- 原有邮箱登录、管理员用户管理和权限行为保持不变。

### 第二阶段：QQ OAuth 服务、state 与 ticket 基础能力

开发内容：

1. 新增 `app/services/qq_oauth_service.py`，集中封装 QQ authorize、token、openid 和用户资料调用；
2. 使用 `secrets` 生成高熵 state，Redis 只保存 `sha256(state)` 对应的状态，并实现 TTL 和原子 GET+DELETE 消费；
3. 兼容 QQ token 接口的查询字符串/JSON 响应，以及 openid 接口的 JSON/JSONP 响应；
4. 校验 QQ 返回的 `client_id` 与配置的 `APP_ID` 完全一致，并对超时、错误码、格式错误和空 openid 做统一业务异常转换；
5. 实现按 QQ 身份查询、更新展示资料和首次自动注册逻辑；首次注册时在一个事务中创建 QQ-only 用户、绑定启用的 `normal` 角色和 `UserIdentity`；
6. 依靠身份唯一约束处理并发首次登录，冲突后回滚并重新读取已创建身份，不留下孤立用户；
7. 实现高熵一次性 ticket 的签发和原子消费，Redis 只保存 ticket 哈希与 `user_id`，不提前创建本系统 Session；
8. 复用 `sha256_text()`、`get_redis()` 和 `ensure_user_can_authenticate()`，不新建 JWT、Refresh Token 或 RBAC 逻辑；
9. 补充 QQ HTTP 解析、state、ticket、自动注册、并发一致性和敏感信息日志测试。

本阶段不实现：

- 对外开放的 QQ HTTP 路由；
- 消费端页面跳转之外的前端改造；
- 将 QQ access token 暴露给消费端。

阶段验收：

- Redis 从不保存明文 state、明文 ticket 或 QQ access token；
- state 和 ticket 均有正确 TTL，且只能成功消费一次；
- QQ 返回格式和 `client_id` 校验符合设计；
- 首次登录只创建一个本地用户和一个 QQ 身份，并绑定启用的 `normal` 角色；
- 被禁用或拉黑用户无法通过 QQ 绕过认证限制；
- QQ 原始错误、密钥、openid、state 和 ticket 不进入日志或异常响应。

### 第三阶段：认证路由、响应模型与兼容调整

按以下顺序实现：

1. 在 `app/schemas/auth.py` 新增 `QQTicketExchangeRequest` 和安全的 `UserIdentityResponse`，扩展 `UserResponse` 的 `permissions`、`display_name`、`avatar`、`identities` 字段；
2. 在 `app/api/auth.py` 新增 `GET /auth/qq/login`，生成 state 并返回 302 到固定 QQ 授权地址；
3. 新增 `GET /auth/qq/callback`，校验并消费 state，完成 QQ 授权、身份处理和 ticket 签发，再 302 到固定 `QQ_TICKET_REDIRECT_URI`；
4. 新增 `POST /auth/qq/exchange`，原子消费 ticket 后调用现有 `AuthService.complete_login(user.id)`，返回既有 `TokenResponse`；
5. 固定回调失败映射，只向消费端传递稳定且非敏感的错误码，禁止把 QQ code、openid、原始错误或内部异常拼入 URL；
6. 扩展 `AuthService.current_user()` 和 `/auth/me`，返回有效权限及不包含 `provider_subject` 的身份列表；
7. 对 `login_by_email()`、管理员用户列表/响应、密码重置和其他依赖 email/password 非空的路径做最小兼容处理；
8. 保持 `/auth/login`、`/auth/refresh`、`/auth/logout` 和现有 Bearer Token 提取逻辑不变；
9. 补充路由、Schema、`/auth/me`、异常响应、权限校验和现有认证回归测试。

本阶段不实现：

- QQ 与邮箱的绑定、解绑或补充邮箱；
- HttpOnly Cookie 或全局 Cookie CSRF；
- 任意回跳 URL、多消费端动态回跳或 `/auth/qq/url` 接口；
- 独立于现有 Session/JWT/RBAC 的登录体系。

阶段验收：

- `/auth/qq/login → QQ → /auth/qq/callback → 固定消费端 → /auth/qq/exchange` 闭环可通过自动化测试；
- ticket 只能交换一次，交换前不会创建本系统登录 Session；
- QQ-only 用户的 `username` 为 `null`，身份响应不泄露 openid；
- 邮箱用户原有 `id`、`username`、`roles` 读取方式和既有认证接口保持兼容；
- 回调失败不会泄露第三方细节，也不会产生开放重定向。

### 第四阶段：完整测试、真实 QQ 联调与部署验收

开发和验证内容：

1. 执行配置、模型、迁移、QQ OAuth 服务、ticket、路由和现有认证回归测试；
2. 执行 `pdm run lint`、`pdm run test` 和 `pdm run alembic-current`，必要时执行项目现有 PostgreSQL/Redis 集成测试；
3. 从 `0006_email_registration`/当前数据库状态升级到 `0007_qq_login`，验证历史邮箱用户、QQ-only 用户、身份唯一约束和安全降级行为；
4. 检查测试与正式环境的 `APP_ID`/`APP_KEY`、回调地址、消费端地址、Redis 前缀、TTL 和超时配置隔离；
5. 在测试环境使用真实 QQ 测试应用完成一次扫码登录闭环，确认只能得到短期 ticket，消费后才能获得 `TokenResponse`；
6. 验证测试环境被禁用/拉黑用户、重复 state、重复 ticket、过期 ticket 和 QQ API 异常的处理结果；
7. 检查 `.github/workflows/deploy.yml` 从 Secrets 注入 `APP_ID`、`APP_KEY`，且部署日志、环境文件生成输出和应用日志均不泄露敏感信息；
8. 完成正式环境迁移前备份、灰度发布、健康检查和回滚兼容评估，确认正式 QQ 应用使用正式回调地址。

阶段验收：

- 测试环境可以完成“QQ 扫码授权 → 回调 → 固定消费端 → ticket 交换 → Bearer Token 调用 `/auth/me`”完整链路；
- 所有自动化测试和 lint 通过，迁移无模型漂移；
- state、ticket、QQ access token、本系统 Token、APP_KEY 和 openid 均未进入日志或 URL；
- 测试/正式环境配置和 Redis 命名空间完全隔离；
- 现有邮箱、管理员、Session、Refresh Token 和 RBAC 功能回归通过；
- 具备可执行的正式发布和失败回滚方案后，才配置正式 QQ 应用并上线。

## 十二、不在本期范围内

- 前端页面或前端认证状态改造。
- HttpOnly Cookie。
- Cookie 与 Bearer 双兼容。
- 全局 CSRF Token。
- QQ 邮箱获取和绑定。
- 已有邮箱账号绑定 QQ。
- QQ 解绑。
- 多消费端动态回跳。
- `/auth/qq/url` 授权 URL 接口。
- 将 QQ access token 暴露给消费端。
- 新建一套独立于现有 Session/JWT/RBAC 的登录体系。
