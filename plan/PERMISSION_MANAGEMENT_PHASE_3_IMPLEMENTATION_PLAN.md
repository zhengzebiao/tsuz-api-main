# 权限管理 Permission：第三阶段实施计划

## 1. Context

第一阶段已经建立 `Permission` 状态字段和 `permission_endpoints` 绑定表；第二阶段已经让 `require_permissions()` 声明可校验、可扫描，并能从真实 `create_app()` 确定性得到 21 个权限和 26 个 Method/Path 绑定。

第三阶段负责将代码声明安全同步到 PostgreSQL，并消除 Seed 中手工权限目录这一第二数据源。同步必须支持部署并发、只读检查、幂等重试、权限废弃/恢复、admin 授权以及授权集合变化后的 Session 撤销。

本阶段不实现权限管理 API，也不修改登录 Scope 过滤或请求时数据库权限状态检查。

## 2. 范围与约束

- 路由扫描结果是 API 权限目录唯一来源；
- `build_plan()` 只读 PostgreSQL，不写 Redis；
- `apply_plan()` 获取固定 PostgreSQL 事务级 advisory lock 后重新计算差异；
- 服务只 `flush`，命令统一 `commit/rollback`；
- 新权限使用 `display_name=name`、空说明、声明且启用、版本 1；
- 已有权限的展示信息、说明、禁用状态及禁用元数据不被覆盖；
- missing 只修改声明状态并清理 endpoint，不删除权限或角色关联；
- 恢复复用原 ID/角色关联并清空 `missing_at`；
- endpoint 或声明状态变化时，每项既有权限每次同步最多递增一次版本；
- admin 角色必须由 Seed 预先建立，同步服务不创建角色；
- 声明权限全部幂等关联给 admin 角色；
- 权限有效集合或 admin 授权变化时，去重撤销受影响用户 Session；
- dry-run/check 不通过写入后 rollback 模拟只读；
- 自动同步仅输出安全摘要，不伪造管理员审计 Actor。

## 3. 同步服务设计

新增 `app/services/permission_sync_service.py`：

1. `PermissionSyncPlan` 保存扫描目标、created、restored、marked missing、endpoint added/removed、admin grants、unchanged、需递增版本的权限和受影响用户；
2. 计划全部使用排序 tuple，支持 `has_changes` 和安全 JSON 输出；
3. `build_plan()` 批量查询 Permission、PermissionEndpoint、admin 关联和受影响用户，不执行任何写入；
4. `apply_plan()` 执行 `pg_advisory_xact_lock`，并在锁内用计划携带的目标扫描结果重新构建差异；
5. 应用新建、恢复、missing、endpoint 快照和 admin 授权；
6. 使用既有 `SessionService.revoke_user_sessions()` 撤销去重用户的活跃 Session；
7. Redis 或数据库失败由命令回滚当前数据库事务，重试依赖所有写入和 Redis revoked 标记的幂等性收敛；
8. 非 PostgreSQL write apply 明确失败，SQLite 仅用于替换 lock 边界后的单元测试。

## 4. 命令设计

新增：

```text
app/commands/__init__.py
app/commands/sync_permissions.py
```

增加 PDM 脚本：

```bash
pdm run permission-sync
pdm run permission-sync --dry-run
pdm run permission-sync --check
```

语义：

- 默认：扫描、只读计划、锁内重算、应用、commit、输出实际摘要；
- dry-run：只扫描和 build plan，输出完整差异，退出 0；
- check：只扫描和 build plan，无差异退出 0，有差异退出 1；
- 配置、扫描、数据库或 Redis 失败 rollback 并使用独立非零退出码；
- dry-run/check 互斥，错误输出只公开异常类型，不输出连接凭证或 Token。

## 5. Seed 与初始化调整

- 删除 `DEFAULT_PERMISSIONS`、`ensure_permission()` 和 `ensure_role_permission()`；
- Seed 只幂等建立 admin 用户、admin 角色和 user-role 关联；
- `scripts/init_local.py` 调整为 migration -> Seed -> permission sync -> 启动 API/nginx；
- 用户/App/角色既有 opt-in 验证器在两次 Seed 后显式执行一次权限同步，再建立测试用户；
- README 明确声明唯一来源、三个同步命令、首次 `user:write` 漂移审查和生产部署显式步骤；
- 不把同步加入自动 deploy/rollback workflow。

## 6. 测试计划

### 6.1 服务单元测试

覆盖：

- 真实主应用首次同步 21 权限、26 endpoint、21 admin grants；
- 重复同步零差异、零版本变化、零 Session 撤销；
- 管理员展示信息、说明和禁用状态保留；
- missing 首次时间、重复 missing、endpoint 清理和角色关联保留；
- 恢复原 ID、关联和 endpoint，且不启用管理员禁用权限；
- route name/endpoint 差异和单次版本递增；
- admin grant、跨角色用户去重和纯 endpoint 变化不撤销；
- Redis 失败 rollback 后可重试；
- 缺少 admin 角色、非 PostgreSQL apply 和只读 plan 边界。

### 6.2 命令、Seed 与初始化测试

覆盖默认 commit、dry-run/check 只读、退出码、异常 rollback、安全输出、参数互斥、Seed 无权限副作用及初始化严格顺序。

### 6.3 真实 PostgreSQL 并发测试

新增默认跳过的 `tests/test_permission_sync_concurrency.py`：

- 仅显式环境变量开启；
- 默认只允许 localhost 且拒绝 5432；
- 创建随机临时数据库，升级到 head，Seed admin；
- 两个独立 Session 在同一外部计划上并发 apply；
- 第一个持有 advisory lock 时第二个必须等待；
- 第二个获取锁后重算为零创建；
- 最终校验 21 permissions、26 endpoints、21 admin grants 和零差异；
- 最终只删除本次随机数据库，不访问 Redis。

## 7. 验证命令

```bash
pdm run test -- \
  tests/test_permission_sync_service.py \
  tests/test_sync_permissions_command.py \
  tests/test_permission_sync_concurrency.py \
  tests/test_seed.py \
  tests/test_init_local.py \
  tests/test_permission_scanner.py
pdm run lint
pdm run test
git diff --check
```

在经过明确授权的隔离 PostgreSQL 管理连接上显式运行并发测试。生产首次同步前按顺序执行 migration、Seed/admin 检查、dry-run、人工审查、默认同步和 check。

## 8. 验收标准

- 当前 21/26 目录首次同步准确，admin 授权完整；
- 重复同步零差异且不改变版本或 Session；
- missing/恢复保留 ID 和角色关联；
- 管理员编辑值和禁用状态不被覆盖；
- 并发实例通过 advisory lock 串行并在锁内重算；
- dry-run/check 完全只读；
- Redis 失败不提交数据库，重试可收敛；
- Seed 不再维护权限目录，本地初始化显式同步；
- 历史 `user:write` 被报告为 missing 而非删除；
- 定向测试、lint、完整回归、空白检查和授权后的真实并发验证通过；
- 未提前实现第四阶段管理 API 或鉴权变化。
