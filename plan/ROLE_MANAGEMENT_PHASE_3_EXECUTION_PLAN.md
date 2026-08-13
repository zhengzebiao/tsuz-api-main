# Context

根据 [ROLE_MANAGEMENT_IMPLEMENTATION_PLAN.md](ROLE_MANAGEMENT_IMPLEMENTATION_PLAN.md)，本阶段实施角色管理第三阶段“角色服务与用户角色分配服务”。前两阶段已经完成角色数据层、`0004_role_management` 迁移以及严格角色和用户角色 Schema；本阶段仅实现这些数据契约背后的业务 Service。

本阶段不新增 HTTP API，不修改权限 Seed、AuthService、Schema、ORM 或 Alembic 迁移，也不实现角色权限配置。测试使用内存 SQLite 和 Fake Redis，不连接开发 PostgreSQL 或 Redis。

## Requirements and constraints

- 角色列表支持分页、名称/描述关键词搜索、启用状态筛选和稳定排序。
- 角色创建固定为启用状态，名称保持全局唯一，重复名称返回固定领域错误。
- 角色编辑使用 `version` 乐观锁；无实际变化时不递增版本、不审计、不撤销 Session。
- `admin` 核心角色允许修改描述，但禁止改名和禁用。
- 角色启用、禁用使用行锁并保持幂等；禁用保留用户和权限关联。
- 角色名称变化及角色禁用撤销全部关联用户的活跃 Session；仅修改描述和重新启用不撤销 Session。
- 角色关联用户查询支持分页、关键词、活跃状态和黑名单状态筛选。
- 用户角色采用完整集合整体替换，先校验全部目标角色，再修改关联，禁止部分成功。
- 已禁用角色不能新增，但用户已有的已禁用角色可以原样保留。
- 用户角色无变化时不递增用户版本、不撤销 Session、不写审计。
- 禁止管理员移除自己的 `admin`，禁止移除最后一名活跃且未拉黑管理员的 `admin`。
- 角色及用户角色审计不包含密码、Token、Session ID、权限集合或其他敏感内容。
- `AdminRoleService` 和新增角色分配方法采用调用方管理事务，业务数据与 `AuditEvent` 可在同一数据库事务中回滚。

## Implementation

1. 新增 `app/services/admin_role_service.py`：
   - 领域错误：`ROLE_NOT_FOUND`、`ROLE_NAME_ALREADY_EXISTS`、`ROLE_VERSION_CONFLICT`、`PROTECTED_ROLE_OPERATION`、`ROLE_DISABLED`；
   - `list_roles()`、`get_role()`、`create_role()`、`update_role()`；
   - `disable_role()`、`enable_role()`、`list_role_users()`；
   - 角色保护、乐观锁、行锁、Session 撤销和安全审计。
2. 扩展 `app/services/admin_user_service.py`：
   - `get_user_roles()`；
   - `assign_roles()`；
   - 完整集合差集更新、用户版本检查、禁用角色规则、管理员安全规则、Session 撤销和审计。
3. 新增或扩展 Service 单元测试：
   - `tests/test_admin_role_service.py`；
   - `tests/test_admin_user_service.py`。
4. 新增实际开发记录：
   - `plan/ROLE_MANAGEMENT_SERVICE_IMPLEMENTATION.md`。

## Transaction strategy

- `AdminRoleService` 写操作只修改 ORM 状态、写入审计并 `flush`，不提交事务。
- `AdminUserService.assign_roles()` 同样只 `flush`，不改变该 Service 既有方法的提交行为。
- 领域校验在关联变更之前完成；失败时不产生部分角色关联更新。
- 数据库回滚可恢复角色、用户版本、关联表、Session 数据行和审计记录。
- Redis 撤销是外部安全副作用，不宣称随数据库事务回滚；真实事务与 Redis 行为留到第五阶段隔离环境验证。

## Verification

```bash
pdm run ruff check app/services/admin_role_service.py app/services/admin_user_service.py tests/test_admin_role_service.py tests/test_admin_user_service.py
pdm run pytest tests/test_admin_role_service.py tests/test_admin_user_service.py tests/test_role_management_schemas.py -q
pdm run lint
pdm run test
git diff --check
```

最终检查 Git diff，确认不包含 API、权限 Seed、AuthService、Schema、ORM 或迁移变更。本阶段不运行 Alembic，不访问开发 PostgreSQL 或 Redis。
