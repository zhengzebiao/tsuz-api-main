# Role 模块给角色分配权限执行记录

## 实施状态

角色权限查询和完整集合整体替换功能已完成代码实现、单元/API 测试及默认完整回归。

本次无需数据库迁移：直接复用既有 `role_permissions(role_id, permission_id)` 复合主键关联表。

## 实际实现

### Schema

`app/schemas/admin_role.py` 新增：

- `AdminRolePermissionAssignment`；
- `AdminRolePermissionSummary`；
- `AdminRolePermissionsResponse`。

请求使用严格整数、正数、去重、必填版本和 `extra="forbid"`。响应只包含安全权限摘要。

### Service

`app/services/admin_role_service.py` 新增：

- `get_role_permissions()`；
- `assign_permissions()`；
- 角色行锁与版本校验；
- Permission 全量存在性、声明状态、启用状态校验；
- added/removed/retained 差集更新；
- admin 核心角色保护；
- 角色版本递增；
- 关联用户 Session 撤销；
- `role.permissions_assigned` 安全审计。

`app/services/admin_permission_service.py` 新增固定领域错误：

```text
PERMISSION_DISABLED
```

### API

`app/api/admin_roles.py` 新增：

```text
GET /admin/roles/{role_id}/permissions
PUT /admin/roles/{role_id}/permissions
```

权限边界：

```text
GET -> role:read
PUT -> role:assign_permissions
```

写接口复用 API 层统一 commit/rollback，确保关联、角色版本、Session 数据库状态和审计处于同一事务。

### 权限目录

新路由声明新增 `role:assign_permissions`，实际同步目录更新为：

```text
26 Permission
33 PermissionEndpoint
26 admin grant
```

固定目录测试及 Role/Permission phase-five 验证器断言已更新。

## 业务行为验证

新增测试覆盖：

- 查询全部角色权限和稳定排序；
- 新增、移除、替换、清空和幂等；
- 请求 ID 严格校验；
- Permission 不存在整体失败；
- 禁用/废弃权限不能新增；
- 已有关联的禁用/废弃权限可以保留或移除；
- 角色版本冲突；
- Role 和目标 Permission 均发出 `FOR UPDATE`；
- admin 当前有效权限不可移除；
- 多用户 Session 撤销；
- 角色版本只递增一次；
- 审计 from/to、Request ID 和撤销数量；
- Service 调用方 rollback；
- API 401/403、404/409/422、OpenAPI 和响应字段白名单；
- API 领域失败时关联、版本和审计 rollback；
- 重新登录后 Scope 反映角色权限新集合；
- Scanner/Sync 26/33/26 新基线及幂等。

## 已执行验证

### 应用代码初始回归

命令：

```bash
pdm run ruff check \
  app/schemas/admin_role.py \
  app/services/admin_role_service.py \
  app/services/admin_permission_service.py \
  app/api/admin_roles.py

pdm run pytest \
  tests/test_role_management_schemas.py \
  tests/test_admin_role_service.py \
  tests/test_admin_roles_api.py -q
```

结果：

```text
All checks passed
24 passed, 1 warning
```

### 新增 Schema/Service/API 测试

结果：

```text
All checks passed
34 passed, 1 warning
```

### Permission Scanner/Sync 基线

命令：

```bash
pdm run pytest \
  tests/test_permission_scanner.py \
  tests/test_permission_sync_service.py -q
```

结果：

```text
15 passed, 1 warning
```

验证了主应用目录为 26 Permission / 33 Endpoint，并验证首次同步创建 26/33/26、第二次同步零差异。

### 真实集成测试默认边界

命令：

```bash
pdm run pytest \
  tests/test_role_management_phase_5_integration.py \
  tests/test_permission_management_phase_5_integration.py -q
```

结果：

```text
6 skipped, 1 warning
```

这是预期结果：真实 PostgreSQL/Redis 集成测试默认必须通过环境变量显式启用。

### 完整静态检查和回归

命令：

```bash
pdm run lint
pdm run test
```

结果：

```text
All checks passed
239 passed, 13 skipped, 1 warning
```

skip 均为默认关闭的真实基础设施集成测试。

## 警告

测试仍有既有依赖警告：

```text
StarletteDeprecationWarning:
Using httpx with starlette.testclient is deprecated; install httpx2 instead.
```

本次未修改依赖版本；警告不影响当前测试结果。

## 真实 PostgreSQL/Redis/RS256 验证

状态：**通过**。

获得明确授权后，使用本机已有镜像启动隔离基础设施：

```text
postgres:16-alpine -> 127.0.0.1:55432
redis:7-alpine     -> 127.0.0.1:56379
```

当前运行在 5432/6379 的开发 PostgreSQL/Redis 未被验证器使用。

### Role 真实并发

命令：

```bash
pdm run role-phase5-validate --only concurrency
```

结果：

```text
row_lock_waits_verified=3
disable_changes=[true, false]
enable_changes=[true, false]
role_assignment_conflict=USER_VERSION_CONFLICT
role_update_conflict=ROLE_VERSION_CONFLICT
associations_consistent=true
temporary_resources_cleaned=true
```

### Role 真实 JWT、Redis 和 HTTP

命令：

```bash
pdm run role-phase5-validate --only http
```

结果：

```text
permissions_verified=7
permission_denials_verified=9
lifecycle_audits_verified=6
request_ids_verified=6
redis_revocations_verified=3
old_sessions_rejected=true
disabled_role_claim_filter=true
reenabled_role_claim_restore=true
user_role_replacement_verified=true
seed_idempotency_verified=true
real_jwt_permissions=true
sensitive_log_scan=clean
temporary_resources_cleaned=true
```

该流程实际验证了：

- `role:assign_permissions` 独立 Scope；
- GET/PUT OpenAPI Security；
- 角色权限完整集合替换和重复提交幂等；
- `role.permissions_assigned` 审计及 Request ID；
- 真实 PostgreSQL 关联和角色版本；
- 真实 Redis Session 撤销和合法 TTL；
- 旧 Access/Refresh 返回 401；
- 重新登录后 RS256 JWT Scope 包含新分配权限；
- 禁用/重启角色后的 Scope 生命周期。

### Permission 真实并发与同步

命令：

```bash
pdm run permission-phase5-validate --only concurrency
```

结果：

```text
advisory_lock_waits_verified=1
row_lock_waits_verified=2
sync_counts_verified=[26, 33, 26]
disable_changes=[true, false]
enable_changes=[true, false]
distinct_session_revocations=1
permission_update_conflict=PERMISSION_VERSION_CONFLICT
redis_failure_rollback_verified=true
sync_retry_idempotency_verified=true
associations_consistent=true
temporary_resources_cleaned=true
```

### Permission 真实 JWT、Redis、同步和 HTTP

命令：

```bash
pdm run permission-phase5-validate --only http
```

结果：

```text
catalog_counts=[26, 33, 26]
permissions_verified=4
permission_denials_verified=6
lifecycle_audits_verified=3
request_ids_verified=3
redis_revocations_verified=5
old_sessions_rejected=true
disabled_scope_filter=true
missing_scope_filter=true
database_status_denial=true
reenabled_scope_restore=true
missing_restore_identity_preserved=true
seed_sync_idempotency_verified=true
real_rs256_permissions=true
sensitive_log_scan=clean
temporary_resources_cleaned=true
```

### Opt-in 集成测试

命令：

```bash
RUN_ROLE_MANAGEMENT_PHASE_5_INTEGRATION=1 \
RUN_PERMISSION_MANAGEMENT_PHASE_5_INTEGRATION=1 \
  pdm run pytest \
    tests/test_role_management_phase_5_integration.py \
    tests/test_permission_management_phase_5_integration.py -q
```

结果：

```text
6 passed, 1 warning
```

### 最终完整回归

真实集成完成后再次执行：

```bash
pdm run lint
pdm run test
git diff --check
```

结果：

```text
All checks passed
239 passed, 13 skipped, 1 warning
git diff --check passed
```

默认完整测试中的 13 个 skip 仍是外部基础设施测试的预期 opt-in 行为；同一实现已通过上面的 6 个显式真实集成测试。

## 真实验证警告

除 Starlette/httpx 既有警告外，真实验证还输出既有 passlib/bcrypt 版本元数据兼容警告，以及 Redis 7 不支持 maintenance-notification 子命令的 debug 信息。密码 Hash/验证、Redis 读写/TTL/清理和全部验收均成功，本次未修改依赖。

## 基础设施安全与清理

- 真实验证仅使用 `127.0.0.1:55432` 和 `127.0.0.1:56379`；
- 验证器只创建随机 `tsuz_role_phase5_*` / `tsuz_permission_phase5_*` 数据库和唯一 Redis namespace；
- 每个验证报告均返回 `temporary_resources_cleaned=true`；
- 两个临时 Docker 容器已停止并因 `--rm` 自动移除；
- 最终 `docker ps -a` 未发现 `tsuz-role-permission-postgres` 或 `tsuz-role-permission-redis`；
- 当前开发 PostgreSQL 5432 和 Redis 6379 仍在运行且未被操作；
- 未执行 `FLUSHDB`、`FLUSHALL` 或 `docker compose down -v`。

## 最终验收结论

以下项目全部通过：

- 角色权限 Schema、Service 和 API；
- 整体替换、幂等、版本、状态和核心角色规则；
- PostgreSQL 行锁、关联一致性和事务 rollback；
- 真实 Redis Session 撤销、TTL 和旧 Access/Refresh 失效；
- 安全审计、Request ID 和敏感信息扫描；
- OpenAPI、认证/授权和固定错误；
- 真实 RS256 Scope 更新；
- Scanner/Sync 26/33/26 收敛和幂等；
- Role/Permission opt-in 真实集成；
- 完整 lint、默认测试和 whitespace 检查。

Role 模块“给角色分配权限”功能满足本次计划的完整验收标准。