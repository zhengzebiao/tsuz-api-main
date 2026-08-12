# 子应用管理 App：第二阶段安全与 Schema 开发记录

## 1. 开发范围

本阶段根据 [SUB_APP_DEVELOPMENT_PLAN.md](../SUB_APP_DEVELOPMENT_PLAN.md) 实施第二阶段“安全与 Schema”，内容包括：

1. 增加 App ID 安全随机生成函数；
2. 增加 App Secret 安全随机生成函数；
3. 增加 App Secret SHA-256 Hash 和常量时间校验函数；
4. 定义 App 管理请求和响应 Schema；
5. 从普通响应类型中排除明文 App Secret 和 `app_secret_hash`；
6. 增加安全工具和 Schema 单元测试。

本阶段未实现：

- App 管理 API；
- App Service 业务逻辑；
- 数据库迁移或数据库写入；
- Redis 逻辑；
- 权限 Seed；
- 审计业务流程；
- `Cache-Control: no-store` HTTP 响应头，待 API 阶段接入。

---

## 2. 安全工具实现

修改文件：

```text
app/core/security.py
```

### 2.1 App ID

新增 `generate_app_id()`，使用标准库密码学安全随机函数 `token_hex(16)` 生成：

```text
app_<32位小写十六进制字符>
```

示例：

```text
app_2d92f64361ea4e249f5c9a0de38bc092
```

App ID 由后续 Service 在创建 App 时生成，数据库唯一约束仍由第一阶段的数据层保证。

### 2.2 App Secret

新增 `generate_app_secret()`，使用 `token_urlsafe(32)` 生成高熵随机值，并添加 `app_secret_` 前缀。

明文 Secret 的使用规则：

- 只允许在创建成功响应中返回一次；
- 只允许在重新生成成功响应中返回一次；
- 列表、详情、编辑、启用和禁用响应不得返回；
- 不写入日志、异常信息或审计记录；
- 后续 API 接入时，包含明文 Secret 的响应必须设置 `Cache-Control: no-store`。

### 2.3 Hash 和校验

新增：

- `hash_app_secret()`：复用现有 `sha256_text()`，生成 64 位十六进制 SHA-256 摘要；
- `verify_app_secret()`：对计算出的 Hash 和数据库 Hash 使用 `hmac.compare_digest` 常量时间比较。

数据库仍只保存 `app_secret_hash`，不保存明文 Secret。本阶段没有新增数据库字段，也没有执行数据库操作。

---

## 3. Schema 实现

新增文件：

```text
app/schemas/admin_app.py
```

请求模型统一使用 Pydantic v2 的 `ConfigDict(extra="forbid")`，因此未声明字段（包括 Secret 和 Hash 字段）会被拒绝。

### 3.1 创建请求

`AdminAppCreate` 包含：

- `name`；
- `icon_url`；
- `access_url`；
- `service_account_name`。

校验规则：

- 名称和服务账号去除首尾空格后不能为空；
- 名称最多 128 字符；
- 图标地址可为空，访问地址必须存在；
- 图标地址和访问地址仅允许 HTTP/HTTPS；
- URL 最多 2048 字符。

### 3.2 编辑请求

`AdminAppUpdate` 允许：

- `name`；
- `icon_url`；
- `access_url`；
- `service_account_name`；
- `version`。

`version` 必须为正整数。`icon_url` 可以显式设置为 `null` 以清空图标；其他必填资料字段不能显式设置为 `null`。

编辑 Schema 不包含：

- `app_id`；
- `app_secret`；
- `app_secret_hash`；
- `is_enabled`；
- 系统时间字段。

### 3.3 状态和 Secret 请求

- `AdminAppDisableRequest`：原因可选，首尾空白会被去除，空白原因归一化为 `None`，最多 500 字符；
- `AdminAppRegenerateSecretRequest`：原因必填，去除首尾空格后不能为空，最多 500 字符。

### 3.4 响应模型

`AdminAppResponse` 只声明非敏感公开字段：

- 内部 ID 和 App ID；
- 名称、图标地址、访问地址、服务账号名称；
- 启用状态和禁用信息；
- Secret 更新时间、创建时间、更新时间；
- 版本号。

`AdminAppListResponse` 和 `AdminAppActionResponse` 基于普通响应模型，也不包含 Secret 或 Hash。

只有以下两个专用响应模型声明明文 Secret：

- `AdminAppCreateResponse`；
- `AdminAppSecretResponse`。

`app_secret_hash` 不属于任何响应模型。

---

## 4. 测试

新增文件：

```text
tests/test_app_management_security_schema.py
```

测试覆盖：

- App ID 格式和 100 次生成不重复；
- App Secret 前缀、长度和 100 次生成不重复；
- Hash 长度为 64；
- 正确 Secret 可以校验，错误 Secret 和旧 Secret 不能校验；
- 创建请求文本字段归一化；
- URL 协议、长度和必填校验；
- 编辑版本号校验和清空图标；
- 状态原因和 Secret 重新生成原因归一化；
- 未声明的 `app_secret`、`app_secret_hash` 字段被拒绝；
- 普通 App 响应和状态响应不包含 Secret 或 Hash；
- 明文 Secret 只出现在创建和重新生成专用响应中。

测试不使用 PostgreSQL 和 Redis，仅使用纯 Python、Pydantic 以及内存中的 App 对象。

---

## 5. 验证结果

本阶段完成后执行：

```bash
pdm run ruff check app/core/security.py app/schemas/admin_app.py tests/test_app_management_security_schema.py
pdm run pytest tests/test_app_management_security_schema.py -q
pdm run lint
pdm run test
```

验证结果：

- 定向 Ruff 检查：通过；
- 第二阶段定向测试：`9 passed`；
- 完整 Ruff 检查：通过；
- 完整测试：`90 passed, 2 skipped`；
- 测试环境存在 1 条既有 `StarletteDeprecationWarning`，与本阶段改动无关。

验证确认：

- 安全工具没有明文 Secret 日志输出；
- 普通响应类型不声明敏感字段，并拒绝额外注入 Secret 或 Hash；
- 请求模型拒绝敏感字段注入；
- 现有认证、用户管理和第一阶段数据层测试未回归。

---

## 6. 数据库和 Redis 隔离约束

本阶段没有数据库迁移、数据库读写或 Redis 逻辑，因此没有连接开发 PostgreSQL 或开发 Redis，也没有执行任何清理命令。

后续阶段如果需要数据库验证，必须使用临时 PostgreSQL 容器、临时数据库或隔离测试数据库，不得在开发库执行迁移回滚、清空数据或写入集成测试数据。

后续阶段如果需要 Redis 验证，必须使用临时 Redis 容器或独立测试实例，并使用独立数据库编号、Key 前缀和连接地址；不得使用开发 Redis 的 Key 前缀，也不得执行开发 Redis 的 `FLUSHDB` 或 `FLUSHALL`。

---

## 7. 下一阶段

下一阶段为“业务服务”，预计实现：

1. App 列表和详情查询；
2. App 创建及凭证持久化；
3. 使用乐观锁编辑基本信息；
4. 使用行锁启用和禁用 App；
5. 使用行锁重新生成 Secret；
6. 接入非敏感审计记录。

Service 接入本阶段安全工具和 Schema 时，必须继续遵守明文 Secret 只展示一次、数据库只保存 Hash、普通响应排除敏感字段的约束。
