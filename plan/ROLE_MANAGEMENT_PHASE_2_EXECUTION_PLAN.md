# Context

根据 [ROLE_MANAGEMENT_IMPLEMENTATION_PLAN.md](ROLE_MANAGEMENT_IMPLEMENTATION_PLAN.md)，本阶段实施角色管理第二阶段“严格 Schema 与领域边界”。第一阶段已经完成 `Role` 模型字段和 `0004_role_management` 迁移；本阶段仅定义后续角色 Service、API 和用户角色分配所需的稳定 Pydantic 数据契约。

本阶段不修改模型或迁移，不实现 Service、API、权限 Seed、鉴权过滤、会话撤销、审计，也不连接开发 PostgreSQL 或 Redis。

## Requirements and constraints

- 所有管理请求使用 Pydantic v2 和 `ConfigDict(extra="forbid")`。
- 角色名称长度为 1～64，描述最大 255，禁用原因最大 500；名称和文本首尾空白必须规范化，纯空白名称必须拒绝。
- 角色创建只能提交名称和描述，状态、ID、时间、禁用元数据和版本均由服务端控制。
- 角色编辑只能提交可选名称/描述以及必填正整数版本；状态字段不能通过编辑 Schema 注入，描述支持规范化为空字符串。
- 禁用请求只接受可选原因。
- 角色响应必须包含角色管理公开字段；动作响应额外返回 `changed` 与非负 `revoked_sessions`。
- 用户角色替换请求允许空角色集合，角色 ID 必须为正整数且不可重复，必须提供正整数用户版本。
- 用户角色响应仅返回用户 ID、精简角色摘要、版本、变更状态和撤销 Session 数，不暴露密码、Token、Session、权限集合或其他敏感字段。

## Implementation

1. 新增 `app/schemas/admin_role.py`：
   - `AdminRoleCreate`、`AdminRoleUpdate`、`AdminRoleDisableRequest`；
   - `AdminRoleSummary`、`AdminRoleResponse`、`AdminRoleListResponse`、`AdminRoleActionResponse`；
   - 复用严格 extra-forbid 基类、字段约束和文本规范化函数。
2. 扩展 `app/schemas/admin_user.py`：
   - 新增 `AdminUserRoleAssignment`，校验正整数、重复 ID 和用户版本；
   - 新增 `AdminUserRolesResponse`，嵌套精简 `AdminRoleSummary`。
3. 新增 `tests/test_role_management_schemas.py`，覆盖创建/编辑/禁用边界、响应安全字段、分页、空角色集合和重复角色 ID；测试只使用内存 ORM 对象及纯 Pydantic 校验。

## Verification

- `pdm run ruff check app/schemas/admin_role.py app/schemas/admin_user.py tests/test_role_management_schemas.py`
- `pdm run pytest tests/test_role_management_schemas.py tests/test_admin_user_service.py tests/test_admin_users_api.py -q`
- `pdm run lint`
- `pdm run test`
- `git diff --check`

本阶段不运行 Alembic，不执行数据库迁移，不访问开发 PostgreSQL 或 Redis。完成后确认 diff 未引入 Service、API、Seed、鉴权或会话逻辑。
