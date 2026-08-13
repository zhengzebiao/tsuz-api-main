# 角色管理 Role：第四阶段 API、权限 Seed 与鉴权接入开发记录

## 1. 开发范围

本阶段根据 [ROLE_MANAGEMENT_IMPLEMENTATION_PLAN.md](ROLE_MANAGEMENT_IMPLEMENTATION_PLAN.md) 和 [ROLE_MANAGEMENT_PHASE_4_EXECUTION_PLAN.md](ROLE_MANAGEMENT_PHASE_4_EXECUTION_PLAN.md)，在前三阶段数据层、Schema 和 Service 基础上完成：

1. `/admin/roles` 角色生命周期与关联用户 API；
2. `/admin/users/{user_id}/roles` 用户角色查询和完整集合替换 API；
3. 角色管理和用户角色分配权限 Seed；
4. 登录、刷新和当前用户鉴权声明对禁用角色的过滤；
5. API 认证、授权、错误映射、事务、OpenAPI 和回归测试。

本阶段未实现：

- 角色权限配置 API；
- 权限目录管理；
- 角色删除或归档；
- 子应用角色和权限同步；
- 真实 PostgreSQL/Redis 集成及迁移往返验证。

---

## 2. 角色管理 API

新增 [app/api/admin_roles.py](../app/api/admin_roles.py)，注册到 [app/main.py](../app/main.py)。

| 方法 | 路径 | 权限 | 响应 |
| --- | --- | --- | --- |
| GET | `/admin/roles` | `role:read` | `AdminRoleListResponse` |
| GET | `/admin/roles/{role_id}` | `role:read` | `AdminRoleResponse` |
| POST | `/admin/roles` | `role:create` | `AdminRoleResponse`，201 |
| PATCH | `/admin/roles/{role_id}` | `role:update` | `AdminRoleActionResponse` |
| POST | `/admin/roles/{role_id}/disable` | `role:disable` | `AdminRoleActionResponse` |
| POST | `/admin/roles/{role_id}/enable` | `role:enable` | `AdminRoleActionResponse` |
| GET | `/admin/roles/{role_id}/users` | `role:read` | 安全的 `AdminUserListResponse` |

实现要点：

- 所有端点通过既有 `require_permissions()` 要求 Bearer Token 和对应权限；
- 角色列表和关联用户列表支持既有 Service 的分页、筛选和稳定排序；
- 创建、编辑、启停 API 复用 `AdminRoleService`；
- 关联用户响应使用 `AdminUserResponse`，不暴露密码哈希、Token、Session 或权限集合；
- 写操作先构造严格响应，再提交数据库事务；领域错误和其他异常都会 rollback；
- `ROLE_NOT_FOUND` 映射 404；重名、版本冲突、核心角色保护映射 409；请求字段约束由 FastAPI/Pydantic 返回 422；
- 错误响应只包含固定领域错误码，不泄露 SQL、约束名或内部异常文本；
- Request ID 通过既有请求上下文传递到 Service 审计。

---

## 3. 用户角色 API

扩展 [app/api/admin_users.py](../app/api/admin_users.py)：

| 方法 | 路径 | 权限 | 响应 |
| --- | --- | --- | --- |
| GET | `/admin/users/{user_id}/roles` | `user:read` | `AdminUserRolesResponse` |
| PUT | `/admin/users/{user_id}/roles` | `user:assign_roles` | `AdminUserRolesResponse` |

行为：

- GET 返回用户当前角色，包括保留关联的已禁用角色；查询响应固定 `changed=false`、`revoked_sessions=0`；
- PUT 消费严格 `AdminUserRoleAssignment`，支持空集合整体清空普通用户角色；
- PUT 复用 `AdminUserService.assign_roles()` 的版本校验、目标角色整体校验、禁用角色规则、admin 保护、Session 撤销和安全审计；
- 响应中的角色只使用 `AdminRoleSummary`，不包含权限集合；
- 成功后 API commit，领域错误和异常 rollback，保证关联表、用户版本、数据库 Session 行和审计同一事务提交或回滚；
- `USER_NOT_FOUND` 和 `ROLE_NOT_FOUND` 映射 404；用户版本、禁用角色、自移除和最后管理员保护映射 409；重复角色 ID 等 Schema 错误映射 422。

---

## 4. 权限 Seed

扩展 [app/seed/__main__.py](../app/seed/__main__.py) 的 `DEFAULT_PERMISSIONS`：

```text
role:read
role:create
role:update
role:disable
role:enable
user:assign_roles
```

继续使用既有 `ensure_permission()` 和 `ensure_role_permission()`：

- 新权限默认关联 Seed 创建的 `admin` 角色；
- 权限和关联重复执行保持幂等；
- 未新增 `role:delete` 或 `role:assign_permissions`；
- 未实现角色权限配置接口。

---

## 5. 鉴权接入

修改 [app/services/auth_service.py](../app/services/auth_service.py)：

- `_get_user_roles()` 仅读取 `Role.is_enabled = true` 的角色；
- `_get_user_permissions()` 显式连接 `Role`，仅聚合启用角色关联的权限；
- 登录、刷新和 `current_user()` 统一经过 `_build_claims()`，因此禁用角色不会进入 JWT `roles`，禁用角色的权限不会进入 JWT `scope`；
- 禁用角色的 `user_roles` 和 `role_permissions` 数据仍保留，重新启用后可重新参与后续登录；
- 既有角色禁用和用户角色变化时的 Session 撤销逻辑保持不变。

---

## 6. 测试与验证

新增或扩展：

- [tests/test_admin_roles_api.py](../tests/test_admin_roles_api.py)：角色 API 注册、OpenAPI、认证授权、独立权限、生命周期、响应安全、错误映射和事务回滚；
- [tests/test_admin_user_roles_api.py](../tests/test_admin_user_roles_api.py)：用户角色 API、整体替换、幂等、禁用/无效角色、版本冲突、admin 保护和事务回滚；
- [tests/test_auth_service.py](../tests/test_auth_service.py)：登录、刷新、当前用户过滤禁用角色及其权限；
- [tests/test_seed.py](../tests/test_seed.py)：新权限存在并保持 Seed 幂等；
- 既有 `tests/test_admin_users_api.py`、Schema、Service 和其他回归测试继续通过。

定向验证：

```bash
pdm run ruff check app/api/admin_roles.py app/api/admin_users.py app/services/auth_service.py app/seed/__main__.py app/main.py tests/test_admin_roles_api.py tests/test_admin_user_roles_api.py tests/test_auth_service.py tests/test_seed.py tests/test_role_management_schemas.py
pdm run pytest tests/test_admin_roles_api.py tests/test_admin_user_roles_api.py tests/test_admin_users_api.py tests/test_auth_service.py tests/test_seed.py tests/test_role_management_schemas.py -q
```

结果：

```text
ruff: passed
pytest: 42 passed, 1 warning
```

完整验证：

```bash
pdm run lint
pdm run test
git diff --check
```

结果：

```text
lint: passed
test: 157 passed, 5 skipped, 1 warning
git diff --check: passed
```

警告仍为项目已有的 Starlette TestClient 与 `httpx` 依赖组合弃用提示。5 个跳过项是需要隔离 PostgreSQL/Redis 环境的既有集成测试，本阶段未连接真实基础设施。

---

## 7. 第四阶段验收结论

第四阶段“API、权限 Seed 与鉴权接入”验收项已满足：

- 角色管理和用户角色 API 已注册并出现在 OpenAPI；
- 所有新增管理 API 均要求 Bearer Token；
- 各端点使用计划规定的独立权限；
- 404、409、422 错误映射稳定且不泄露内部信息；
- 角色写 API 和用户角色 PUT API 具备统一 commit/rollback 事务边界；
- Seed 重复执行不产生重复权限或关联；
- 禁用角色及其权限不再进入登录、刷新和当前用户鉴权声明；
- API 响应和审计载荷不包含密码、Token、Session ID 或权限集合；
- 未提前实现角色权限配置、删除角色或第五阶段真实 PostgreSQL/Redis 验证。
