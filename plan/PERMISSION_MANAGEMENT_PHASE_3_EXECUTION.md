# 权限管理 Permission：第三阶段开发执行记录

## 1. 开发范围

本阶段根据 [PERMISSION_SYNC_DESIGN.md](../docs/PERMISSION_SYNC_DESIGN.md) 完成“同步服务、命令和 Seed 集成”：

1. 实现只读权限同步差异计划；
2. 实现 PostgreSQL advisory lock 下的事务同步；
3. 同步创建、恢复、missing 和 API endpoint 快照；
4. 保留管理员编辑值、禁用状态和历史角色关联；
5. 将声明权限幂等关联至 admin 角色；
6. 对授权集合发生变化的受影响 Session 去重撤销；
7. 新增默认、`--dry-run` 和 `--check` 权限同步命令；
8. 移除 Seed 中重复维护的权限目录；
9. 将本地初始化调整为迁移 → Seed → 同步 → 启动；
10. 增加服务、命令、Seed、初始化和 opt-in PostgreSQL 并发测试；
11. 执行定向回归、完整测试、lint 和空白检查。

本阶段未实现：

- 权限管理 Schema、Service 和 API；
- AuthService 的声明/启用状态 Scope 过滤；
- AuthorizationService 的数据库权限状态检查；
- 权限启用/禁用管理 API；
- Session 管理 API；
- 自动同步 AuditEvent 管理员 Actor；
- 自动 deploy/rollback workflow 中的同步步骤。

---

## 2. 同步服务

新增：

```text
app/services/permission_sync_service.py
```

### 2.1 只读计划

`PermissionSyncService.build_plan()`：

- 读取扫描结果、Permission、PermissionEndpoint、admin 角色和角色关联；
- 生成不可变、稳定排序的 `PermissionSyncPlan`；
- 计算 `created`、`restored`、`marked_missing`、endpoint 新增/删除、admin grants、版本变化和受影响用户；
- 不修改 ORM 对象、不写 PostgreSQL、不访问 Redis；
- `to_dict()` 只输出权限编码、方法、路径、路由名和计数，不输出凭证或数据库连接信息。

### 2.2 apply 与幂等

`apply_plan()`：

- 在 PostgreSQL 事务中调用 `pg_advisory_xact_lock()`；
- 获取锁后使用原扫描目标重新计算计划，避免使用锁外陈旧差异；
- 新权限设置 `display_name=name`、空 description、声明/启用状态和 version 1；
- 既有权限恢复或标记 missing 时保留 ID、角色关联、管理员展示信息和禁用状态；
- missing 首次写入 `missing_at`，重复 missing 不覆盖时间；
- missing 清除当前 endpoint，恢复重建扫描绑定；
- endpoint/声明状态变化的既有权限只递增一次 version；
- 声明权限缺少 admin 关联时幂等插入；
- 服务只 flush，不 commit。

### 2.3 Session 撤销

通过现有 `SessionService.revoke_user_sessions()`：

- 对权限有效集合变化和新增 admin 授权的用户做 DISTINCT 去重；
- 纯 endpoint 路由名/绑定变化不撤销 Session；
- Redis 或数据库写入异常向上抛出，命令负责 rollback；
- Redis revoked 写入本身保持重试幂等。

---

## 3. 同步命令

新增：

```text
app/commands/__init__.py
app/commands/sync_permissions.py
```

`pyproject.toml` 新增：

```text
permission-sync = "python -m app.commands.sync_permissions"
```

命令支持：

```bash
pdm run permission-sync
pdm run permission-sync --dry-run
pdm run permission-sync --check
```

行为：

- 默认模式：扫描 → build plan → advisory lock 下 apply → commit → 输出 JSON summary；
- `--dry-run`：只输出差异计划，不 commit、不 apply、不写 Redis；
- `--check`：只读检查，无差异返回 0，有差异返回 1；
- 扫描、数据库、Redis 或配置异常 rollback 并返回 2；
- 错误输出只使用异常类型，不输出 PostgreSQL URL、密码、Token、Session ID 或 Authorization Header；
- `--dry-run` 和 `--check` 互斥。

---

## 4. Seed 和本地初始化

修改：

```text
app/seed/__main__.py
scripts/init_local.py
```

实际变更：

- 删除 `DEFAULT_PERMISSIONS`；
- 删除 `ensure_permission()` 和 `ensure_role_permission()`；
- Seed 只创建 admin 用户、admin 角色和 user_roles 关联；
- 初始化在 migration 和 Seed 后执行 `app.commands.sync_permissions`；
- README 增加声明唯一来源、首次 `user:write` missing 审查、dry-run/check 和生产显式顺序说明；
- 用户、App、Role opt-in 验证器在 Seed 后显式执行权限同步，保持历史测试 fixture 可用。

---

## 5. 测试实现

新增：

```text
tests/test_permission_sync_service.py
tests/test_sync_permissions_command.py
tests/test_permission_sync_concurrency.py
```

覆盖：

- 真实 `create_app()` 首次同步目录；
- 重复同步零差异和零版本变化；
- 管理员字段和禁用状态保留；
- missing、恢复、ID/关联保留和首次时间；
- endpoint 变更和权限版本；
- admin 授权幂等；
- 跨多个角色的用户 Session 去重撤销；
- Redis 失败 rollback 与重试；
- 缺少 admin 角色和非 PostgreSQL lock 边界；
- 命令默认/dry-run/check、退出码、安全输出；
- Seed 无权限目录副作用；
- 本地初始化命令顺序；
- 显式环境变量开启的真实 PostgreSQL advisory lock 并发场景。

---

## 6. 验证结果

### 6.1 定向测试

执行：

```bash
pdm run test -- \
  tests/test_permission_sync_service.py \
  tests/test_sync_permissions_command.py \
  tests/test_permission_sync_concurrency.py \
  tests/test_seed.py \
  tests/test_init_local.py \
  tests/test_permission_scanner.py
```

结果：

```text
34 passed, 1 skipped, 1 warning
```

跳过项是默认关闭的真实 PostgreSQL 并发测试。

### 6.2 权限、角色和 Session 回归

执行：

```bash
pdm run test -- \
  tests/test_admin_user_roles_api.py \
  tests/test_admin_role_service.py \
  tests/test_admin_roles_api.py \
  tests/test_user_session_revocation.py \
  tests/test_auth_service.py \
  tests/test_authorization_service.py
```

结果：

```text
48 passed, 1 warning
```

### 6.3 完整测试

执行：

```bash
pdm run test
```

结果：

```text
204 passed, 10 skipped, 1 warning
```

10 个 skipped 为显式环境变量控制的真实 PostgreSQL/Redis 或并发验证，不是失败。

### 6.4 静态检查

执行：

```bash
pdm run lint
git diff --check
```

结果：

```text
All checks passed!
```

`git diff --check` 无输出。

### 6.5 真实 PostgreSQL 并发测试

测试按设计要求只接受隔离 PostgreSQL 管理连接，默认拒绝远程服务和 5432。当前执行环境的默认 phase-three 测试连接端口 `127.0.0.1:55432` 无服务；尝试使用当前共享的 5432 被安全策略拒绝，因为测试会创建和删除临时数据库。因此本次未宣称真实并发验证通过，测试保持显式 opt-in skipped/blocked 状态。

测试实现仍包含：

- 随机临时数据库；
- PostgreSQL-only 校验；
- advisory lock 等待断言；
- 锁内重算后第二实例零创建；
- 21 permissions、26 endpoints 和 21 admin grants 最终断言；
- 仅删除本次创建数据库。

### 6.6 Alembic 和本地 dry-run 限制

`pdm run alembic check` 和默认 `pdm run permission-sync --dry-run` 针对当前本地数据库执行时分别遇到：

- 当前数据库 revision 不是最新 head，Alembic 报 `Target database is not up to date`；
- 本地数据库还未达到可用的权限同步结构/Seed 状态，命令以安全 `ProgrammingError` 失败。

这些命令没有修改代码仓库，也没有在本次验证中执行迁移或同步写入。应在隔离数据库完成 `alembic upgrade head`、admin Seed 后再执行。

唯一测试警告仍为项目既有：

```text
StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated
```

---

## 7. 最终文件变更

新增：

```text
app/services/permission_sync_service.py
app/commands/__init__.py
app/commands/sync_permissions.py
tests/test_permission_sync_service.py
tests/test_sync_permissions_command.py
tests/test_permission_sync_concurrency.py
plan/PERMISSION_MANAGEMENT_PHASE_3_IMPLEMENTATION_PLAN.md
plan/PERMISSION_MANAGEMENT_PHASE_3_EXECUTION.md
```

修改：

```text
app/seed/__main__.py
pyproject.toml
scripts/init_local.py
scripts/validate_phase_4.py
scripts/validate_app_phase_5.py
scripts/validate_role_management_phase_5.py
tests/test_seed.py
tests/test_init_local.py
README.md
```

---

## 8. 第三阶段验收结论

第三阶段代码实现已完成：

- 同步计划纯读、稳定且可 JSON 输出；
- PostgreSQL advisory lock 下重新规划并应用；
- 创建、恢复、missing、endpoint 快照和 admin 授权均实现；
- 管理员展示值、说明、禁用状态、权限 ID 和角色关联均保留；
- Session 影响用户去重并通过既有 SessionService 撤销；
- 默认、dry-run、check 命令已提供；
- Seed 不再维护第二份权限目录；
- 本地初始化显式执行权限同步；
- 定向测试、权限回归、完整测试、lint 和空白检查通过；
- 真实 PostgreSQL 并发验证因当前环境隔离连接不可用/共享数据库操作未获授权，未被虚报为通过；
- 第四阶段管理 API 和 Auth 状态变化未提前实现。
