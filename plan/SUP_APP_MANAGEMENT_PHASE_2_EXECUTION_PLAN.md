# Context

根据 [SUB_APP_DEVELOPMENT_PLAN.md](../SUB_APP_DEVELOPMENT_PLAN.md)，现在实施子应用管理 App 的第二阶段“安全与 Schema”。第一阶段已经完成 `App` SQLAlchemy 数据模型和 `apps` 表迁移；本阶段为后续 Service/API 提供统一的凭证生成、哈希校验和请求/响应类型，并从类型层面避免普通 App 响应泄露明文 Secret 或 Secret Hash。

本阶段不实现 Service、API、权限 Seed、审计流程、数据库迁移或 Redis 逻辑，也不连接或修改开发 PostgreSQL/Redis。

# Requirements and constraints

- App ID 由后端生成，格式为 `app_` 加 32 位随机十六进制字符，使用密码学安全随机源。
- App Secret 由后端生成，使用 `secrets.token_urlsafe(32)` 级别的随机性，并加 `app_secret_` 前缀；明文只由创建/重新生成响应显式承载。
- 增加 App Secret Hash 和校验工具：哈希结果为 64 位十六进制 SHA-256 摘要；校验使用常量时间比较。
- 创建、编辑、禁用、重新生成 Secret 请求 Schema 拒绝未声明字段；名称和服务账号去除首尾空格且不能为空；URL 仅允许 HTTP/HTTPS，长度不超过 2048；原因字段最多 500 字符。
- 普通 App 响应、列表响应和状态操作响应只包含非敏感 App 字段，不声明 `app_secret` 或 `app_secret_hash`；创建响应和 Secret 重新生成响应分别显式包含一次明文 `app_secret`。
- 编辑 Schema 只允许基本资料和正整数 `version`，不提供 App ID、Secret、状态或时间字段入口；允许显式清空 `icon_url`，其他必填资料字段不能显式传 null。
- 测试只使用纯函数和内存对象，不运行迁移，不操作开发数据库和 Redis。

# Implementation plan

1. **扩展安全工具**
   - 修改 `app/core/security.py`。
   - 新增 `generate_app_id()`，使用 `token_hex(16)` 生成 `app_<32 hex>`。
   - 新增 `generate_app_secret()`，使用 `token_urlsafe(32)` 生成带前缀的高熵 Secret。
   - 新增 `hash_app_secret()` 复用现有 `sha256_text()`；新增 `verify_app_secret()` 使用 `hmac.compare_digest`。
   - 不记录输入 Secret、Hash 或生成值，不改变现有密码和 JWT 工具。

2. **新增 App 管理 Schema**
   - 创建 `app/schemas/admin_app.py`。
   - 定义严格请求模型：`AdminAppCreate`、`AdminAppUpdate`、`AdminAppDisableRequest`、`AdminAppRegenerateSecretRequest`。
   - 使用 Pydantic v2、`ConfigDict(extra="forbid")`、`Field`、`field_validator` 和 `AnyHttpUrl` 实现字段长度、协议、空白归一化和版本约束。
   - 定义非敏感 `AdminAppResponse`、分页 `AdminAppListResponse`、状态操作 `AdminAppActionResponse`、一次性 `AdminAppCreateResponse` 和 `AdminAppSecretResponse`。
   - 普通响应覆盖 App 的公开字段；创建和 Secret 重新生成响应才声明明文 `app_secret`。

3. **补充安全与 Schema 测试**
   - 新增 `tests/test_app_management_security_schema.py`。
   - 验证 App ID 格式和随机性、Secret 前缀/长度/随机性、Hash 长度、正确/错误 Secret 校验和旧 Secret 失效。
   - 验证请求字段归一化、HTTP/HTTPS URL 校验、长度限制、空白字段、版本和原因校验、未知敏感字段拒绝。
   - 使用 `App` 内存对象验证普通响应不包含 Secret 或 Secret Hash，并验证明文 Secret 仅出现在专用的一次性响应中。

4. **导出开发结果**
   - 开发完成后新增 `docs/SUB_APP_SECURITY_SCHEMA_IMPLEMENTATION.md`，记录实际完成内容、验证结果、未实现范围以及数据库/Redis 隔离约束。

# Critical files

- `app/core/security.py` — App ID/Secret 生成、Secret Hash 和常量时间校验。
- `app/schemas/admin_app.py` — App 管理请求与安全响应类型。
- `tests/test_app_management_security_schema.py` — 第二阶段纯单元测试。
- `docs/SUB_APP_SECURITY_SCHEMA_IMPLEMENTATION.md` — 第二阶段开发记录。
- `app/models/app.py` — 响应字段和 Hash 长度的现有数据层参考，本阶段不修改。

# Verification

- `pdm run ruff check app/core/security.py app/schemas/admin_app.py tests/test_app_management_security_schema.py`
- `pdm run pytest tests/test_app_management_security_schema.py -q`
- `pdm run lint`
- `pdm run test`
- 使用测试和静态检查确认普通响应模型没有 `app_secret` 或 `app_secret_hash`。
- 不执行 Alembic，不连接开发 PostgreSQL，不连接开发 Redis；本阶段不需要数据库或 Redis 验证。
- 完成后检查 Git diff，确认不提前实现第三至第五阶段功能。
