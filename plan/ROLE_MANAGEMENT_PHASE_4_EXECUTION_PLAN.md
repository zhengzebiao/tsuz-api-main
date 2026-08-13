# 角色管理第四阶段执行计划

## Context

根据 [ROLE_MANAGEMENT_IMPLEMENTATION_PLAN.md](ROLE_MANAGEMENT_IMPLEMENTATION_PLAN.md)，本阶段实施第四阶段“API、权限 Seed 与鉴权接入”。前三阶段已经完成角色数据模型和迁移、严格请求/响应 Schema、`AdminRoleService`，以及 `AdminUserService` 的用户角色查询和完整集合替换；当前缺口是把这些能力公开为受独立权限保护的管理 API，将新权限幂等加入 Seed，并保证禁用角色及其权限不再进入登录、刷新和当前用户的鉴权声明。

本阶段不实现角色权限配置、角色删除、权限目录管理，也不执行第五阶段的真实 PostgreSQL/Redis 集成验证或迁移往返。API 单元测试继续使用隔离的内存 SQLite、授权 Stub 和 Fake Redis。

## Requirements and constraints

- 角色管理路由：列表、详情、创建、编辑、禁用、启用、关联用户列表。
- 用户角色路由：查询当前角色集合、完整集合替换角色集合。
- 每个路由使用独立的最小权限，不用粗粒度的单一 `role:manage` 替代。
- 写 API 由 API 层管理事务：成功提交；领域错误、响应构造错误或数据库错误回滚。
- 领域错误映射稳定：不存在为 404；版本冲突、重名、保护规则和禁用规则为 409；请求约束错误为 422。
- API 响应不得泄露密码、Token、Session ID、权限集合或其他敏感字段。
- 角色被禁用后，登录、刷新和 `current_user()` 只加载启用角色及启用角色权限；角色关联数据本身保留。
- 权限 Seed 可重复执行，不产生重复 Permission 或 role-permission 关联；新管理权限默认授予 `admin`。
- 不修改角色权限配置 API、角色删除、Auth 外部协议、ORM 和 Alembic 迁移。

## Implementation

1. 新增角色管理 API router 并在 `app/main.py` 注册：
   - `GET /admin/roles`、`GET /admin/roles/{role_id}`、`POST /admin/roles`、`PATCH /admin/roles/{role_id}`；
   - `POST /admin/roles/{role_id}/disable`、`POST /admin/roles/{role_id}/enable`；
   - `GET /admin/roles/{role_id}/users`。
2. 扩展用户管理 API：
   - `GET /admin/users/{user_id}/roles`；
   - `PUT /admin/users/{user_id}/roles`。
3. 增加角色管理与用户角色分配权限 Seed，并在鉴权查询中排除禁用角色及其权限。
4. 补充 API、授权、事务、Seed、OpenAPI 与禁用角色鉴权测试。
5. 添加阶段实际实现记录并完成静态检查和回归测试。

## Verification

```bash
pdm run ruff check app/api/admin_roles.py app/api/admin_users.py app/services/auth_service.py app/seed/__main__.py app/main.py tests/test_admin_roles_api.py tests/test_admin_users_api.py tests/test_auth_service.py tests/test_seed.py
pdm run pytest tests/test_admin_roles_api.py tests/test_admin_users_api.py tests/test_auth_service.py tests/test_seed.py tests/test_role_management_schemas.py -q
pdm run lint
pdm run test
git diff --check
```

最终确认未加入角色权限配置、角色删除、模型或迁移修改；本阶段不访问开发 PostgreSQL/Redis，不执行 Alembic downgrade。
