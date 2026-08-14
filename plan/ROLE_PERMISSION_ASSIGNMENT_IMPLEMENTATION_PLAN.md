# Role 模块给角色分配权限实现计划

## Context

`docs/MAIN_APP_MODULES.md` 将“给角色分配权限”列为 Role 模块能力。现有 Role 管理已经完成角色生命周期、关联用户查询和用户角色整体替换；Permission 管理已经完成声明扫描、同步、启停/废弃以及 declared+enabled 鉴权过滤，但 `role_permissions` 尚无管理接口。

本次基于现有全局 Role 和 `role_permissions` 关联表，增加角色权限查询与完整集合整体替换。实现不增加迁移、不允许手工创建或删除权限，并继续由权限扫描/同步维护权限目录和 admin 默认授权。

## 接口契约

### 查询角色权限

```text
GET /admin/roles/{role_id}/permissions
权限：role:read
```

返回角色全部历史关联权限，包括禁用或已废弃权限，以便管理页面安全地保留或移除历史关联。

### 整体替换角色权限

```text
PUT /admin/roles/{role_id}/permissions
权限：role:assign_permissions
```

请求：

```json
{
  "permission_ids": [1, 2, 3],
  "version": 4
}
```

`permission_ids` 表示完整目标集合；空集合表示清空普通角色权限。请求拒绝非正整数、布尔值、重复 ID、无效版本和额外字段。

响应包含：

- `role_id`；
- 安全权限摘要列表；
- 最新角色 `version`；
- `changed`；
- `revoked_sessions`。

## 业务规则

1. 使用角色 `SELECT ... FOR UPDATE` 和 `version` 防止并发覆盖。
2. 目标 Permission 必须全部存在，否则返回 `PERMISSION_NOT_FOUND`，不允许部分更新。
3. 只有 `is_declared=true AND is_enabled=true` 的权限可以新增；分别使用 `PERMISSION_NOT_DECLARED` 和 `PERMISSION_DISABLED` 固定错误。
4. 角色已有的禁用/废弃权限可以原样保留或显式移除。
5. `admin` 核心角色不能移除任何当前 declared+enabled 权限，返回 `PROTECTED_ROLE_OPERATION`。
6. 重复提交同一集合返回 `changed=false`，不递增版本、不撤销 Session、不写审计。
7. 有效变化只增删实际差异，角色版本递增一次，并撤销该角色全部关联用户的 PostgreSQL/Redis Session。
8. 写入 `role.permissions_assigned` 审计，只记录权限 `{id, name}` 前后集合、撤销数量、Actor 和 Request ID。
9. 权限分配不修改 Permission 状态、版本、展示信息或 endpoint 快照。
10. AuthService 继续按 enabled Role 和 declared+enabled Permission 生成 JWT Scope。

## 实现内容

### Schema

扩展 `app/schemas/admin_role.py`：

- `AdminRolePermissionAssignment`；
- `AdminRolePermissionSummary`；
- `AdminRolePermissionsResponse`。

### Service

扩展 `app/services/admin_role_service.py`：

- `get_role_permissions()`；
- `assign_permissions()`；
- 当前/目标权限批量查询；
- 权限审计安全值；
- 差集更新、核心角色保护、版本、Session 撤销和审计。

扩展 `app/services/admin_permission_service.py`，增加共享固定领域错误 `PermissionDisabledError`。

### API

扩展 `app/api/admin_roles.py`：

- 注册 GET/PUT 角色权限路由；
- 使用 `role:read` 和 `role:assign_permissions` 独立权限；
- 映射 404/409/422；
- 复用 API 层统一 commit/rollback 事务边界。

### 声明目录

新 PUT 路由声明 `role:assign_permissions`。现有扫描和同步将目录从：

```text
25 Permission / 31 Endpoint / 25 admin grant
```

更新为：

```text
26 Permission / 33 Endpoint / 26 admin grant
```

Seed 仍只创建 admin 身份和角色，不手工创建权限。

## 关键文件

修改：

- `app/schemas/admin_role.py`
- `app/services/admin_role_service.py`
- `app/services/admin_permission_service.py`
- `app/api/admin_roles.py`
- `tests/test_role_management_schemas.py`
- `tests/test_admin_role_service.py`
- `tests/test_admin_roles_api.py`
- `tests/test_auth_service.py`
- `tests/test_permission_scanner.py`
- `tests/test_permission_sync_service.py`
- `scripts/validate_role_management_phase_5.py`
- `scripts/validate_permission_management_phase_5.py`
- 对应 Role/Permission opt-in 集成测试

无需修改模型或新增 Alembic 迁移。

## 验证

1. Schema、Service、API、Auth、Scanner、Sync 定向测试。
2. 完整 `pdm run lint` 和 `pdm run test`。
3. 隔离 PostgreSQL 16、Redis 7 上运行 Role/Permission phase-five concurrency 与 HTTP 验证。
4. 使用真实 RS256 登录验证：权限分配撤销旧 Access/Refresh，重新登录后 Scope 反映新集合。
5. 验证 OpenAPI、独立 401/403、固定错误、审计、Redis TTL、26/33/26 同步幂等。
6. `git diff --check` 和敏感信息审阅。

## 验收标准

- 查询和完整集合替换接口可用且响应安全；
- 更新原子、严格、幂等、版本并发安全；
- 禁用/废弃权限及 admin 核心角色规则正确；
- 有效变化撤销全部受影响 Session，并写安全审计；
- JWT Scope 和数据库权限状态一致；
- 权限同步收敛为 26/33/26；
- 定向、完整和真实集成验证全部通过，或未执行项在执行文档中如实注明。