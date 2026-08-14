# 权限管理 Permission：第五阶段开发执行记录

## 1. 开发范围

本阶段根据 [PERMISSION_SYNC_DESIGN.md](PERMISSION_SYNC_DESIGN.md) 和 [PERMISSION_MANAGEMENT_PHASE_5_EXECUTION_PLAN.md](PERMISSION_MANAGEMENT_PHASE_5_EXECUTION_PLAN.md)，完成：

1. 新增权限第五阶段统一隔离验证器；
2. 真实验证 `0005_permission_management` PostgreSQL 迁移往返；
3. 真实验证权限同步 advisory lock 和权限启停行锁；
4. 验证 Redis 撤销失败时数据库事务 rollback 及安全重试；
5. 使用真实 PostgreSQL、Redis、Uvicorn 和 RS256 JWT 验证权限管理 HTTP 生命周期；
6. 验证权限禁用、重新启用、同步废弃和声明恢复；
7. 验证旧 Access/Refresh、JWT Scope 和数据库权限状态边界；
8. 验证 Seed、权限同步和 admin 授权幂等；
9. 增加默认 skip 的 opt-in 集成测试及 PDM 命令；
10. 执行权限定向、管理回归、现有真实集成、完整测试、lint、空白和残留检查。

本阶段未实现：

- 权限后台手动创建；
- 权限编码或 API 绑定编辑；
- 权限物理删除；
- 角色权限分配 API；
- 子应用权限同步；
- 菜单权限或数据权限；
- 新数据库迁移或认证协议。

---

## 2. 验证入口

新增：

```text
scripts/validate_permission_management_phase_5.py
tests/test_permission_management_phase_5_integration.py
```

新增 PDM 命令：

```bash
pdm run permission-phase5-validate
```

验证器支持：

```bash
pdm run permission-phase5-validate --only migration
pdm run permission-phase5-validate --only concurrency
pdm run permission-phase5-validate --only http
pdm run permission-phase5-validate --only all
```

真实集成 pytest 默认跳过，仅在以下变量显式开启时运行：

```bash
RUN_PERMISSION_MANAGEMENT_PHASE_5_INTEGRATION=1
```

因此日常 `pdm run test` 不依赖 PostgreSQL 或 Redis。

---

## 3. 临时基础设施与安全边界

实际验证使用两个无持久卷临时容器：

| 服务 | 容器 | 主机地址 | 隔离方式 |
| --- | --- | --- | --- |
| PostgreSQL 16 | `tsuz-permission-phase5-postgres` | `127.0.0.1:55432` | 每组随机 `tsuz_permission_phase5_*` 数据库 |
| Redis 7 | `tsuz-permission-phase5-redis` | `127.0.0.1:56379` | DB 15 和唯一 `auth:permission-phase5:<suffix>:` 前缀 |

验证器默认：

- 只允许 localhost、`127.0.0.1` 或 `::1`；
- 拒绝 PostgreSQL `5432` 和 Redis `6379`；
- 远程地址或默认端口必须显式放行；
- PostgreSQL 只删除本次创建的随机数据库；
- Redis 只使用 `SCAN + DELETE` 清理本次前缀；
- 不执行 `FLUSHDB` 或 `FLUSHALL`；
- 不挂载或复用开发数据卷；
- 动态生成临时 2048 位 RSA 密钥；
- 使用随机本地 Uvicorn 端口和独立 issuer/audience。

完成全部验证后，PostgreSQL 随机数据库和 Redis 前缀残留检查均为空，专用容器已经停止并自动删除。当前已有 `postgres:5432`、`redis:6379` 和应用容器未被验证器使用或停止。

---

## 4. 真实 Alembic 迁移往返

实际流程：

```text
0004_role_management
  → 0005_permission_management
  → 0004_role_management
  → 0005_permission_management
```

升级前插入：

- 一项带明确 ID 和说明的既有 Permission；
- 一个既有 Role；
- 一条 `role_permissions` 关联。

升级后验证：

- Permission 12 个字段及 nullable 规则；
- `ix_permissions_id`、唯一 name、declared 和 enabled 共 4 个索引；
- `permission_endpoints` 4 个字段；
- `(permission_id, http_method, path)` 复合主键；
- Permission CASCADE 外键；
- Method/Path 索引；
- 既有 ID、编码、说明和角色关联保留；
- `display_name=name` 回填；
- 状态、时间和 version 默认值正确；
- 新 Permission 的自增 ID 连续；
- 重复 endpoint 被数据库约束拒绝；
- downgrade 后恢复 `0004` 结构且数据保留；
- 再次 upgrade 后 `alembic check` clean。

最终报告：

```text
current_revision = 0005_permission_management
alembic_check = clean
permission_columns_verified = 12
permission_indexes_verified = 4
endpoint_columns_verified = 4
legacy_permission_data_preserved = true
role_associations_preserved = true
sequence_preserved = true
duplicate_endpoint_rejected = true
```

---

## 5. 真实 PostgreSQL 并发与失败重试

### 5.1 同步 advisory lock

两个独立事务并发对真实 `create_app()` 目录执行同步。第二个事务的 backend PID 通过：

```sql
SELECT wait_event_type = 'Lock'
FROM pg_stat_activity
WHERE pid = :backend_pid
```

确认真实等待 advisory lock 后才释放第一事务。

最终：

- 第一事务创建 25 Permission、31 endpoint、25 admin grant；
- 第二事务重建计划后零差异；
- 最终不存在重复权限、endpoint 或 admin grant。

### 5.2 权限启停行锁

两个独立事务分别并发禁用和启用同一普通权限：

- 两次均证明第二事务真实进入 Lock 等待；
- 禁用结果为 `[true, false]`；
- 启用结果为 `[true, false]`；
- 状态版本每轮只递增一次；
- 首次禁用原因不被重复请求覆盖；
- 每种状态变化只写一条审计；
- endpoint 和三个 `role_permissions` 关联保留。

目标用户通过两个启用角色拥有同一权限，实际只撤销一次 Session，证明 DISTINCT 用户查询有效。PostgreSQL Session 状态、原因、Redis 值和 TTL 均正确。

### 5.3 乐观锁与 Redis 失败 rollback

- 两个 Session 使用同一旧版本更新展示信息；胜者提交后，旧版本返回 `PERMISSION_VERSION_CONFLICT`；
- 模拟同步期间 Redis Session 撤销失败；调用方 rollback 后 Permission declared 状态、version、endpoint 和 PostgreSQL Session 均未部分提交；
- 恢复 Redis 后同一同步重试一次成功，再次 build plan 为零差异；
- 真实 Redis 撤销值和 TTL 正确。

最终报告：

```text
advisory_lock_waits_verified = 1
row_lock_waits_verified = 2
sync_counts_verified = [25, 31, 25]
disable_changes = [true, false]
enable_changes = [true, false]
distinct_session_revocations = 1
permission_update_conflict = PERMISSION_VERSION_CONFLICT
redis_failure_rollback_verified = true
sync_retry_idempotency_verified = true
```

---

## 6. 真实 RS256、HTTP、Redis 和同步生命周期

### 6.1 初始化与权限边界

临时数据库中执行：

```text
alembic upgrade head
  → Seed 两次
  → permission-sync 两次
  → permission-sync --check
```

确认最终目录：

```text
permissions = 25
permission_endpoints = 31
admin_grants = 25
```

建立：

- 无权限用户；
- `permission:read/update/disable/enable` 四个单权限角色和用户；
- 只拥有真实 `app:read` 的生命周期用户；
- Seed admin 用户。

所有主体通过真实 `/auth/login` 获取 Token，并用动态公钥、RS256、issuer 和 audience 验证 claims。

权限边界结果：

```text
permissions_verified = 4
permission_denials_verified = 6
real_rs256_permissions = true
```

覆盖：

- 无 Token 返回 401；
- 无权限返回 403；
- 四个错误单权限调用返回 403；
- 四项权限均用于正确操作；
- OpenAPI 含五个操作、Security 和请求/响应模型；
- collection POST 和 DELETE 不存在。

### 6.2 管理 API 生命周期

验证：

- 列表 resource/declared/enabled 筛选；
- `app:read` 详情和两个 endpoint 快照；
- 展示信息 no-op 不增加版本或审计；
- 有效更新和旧版本冲突；
- 404、409、422 固定边界；
- `permission:enable` 禁用保护；
- 响应字段白名单。

### 6.3 禁用、启用、废弃和恢复

实际流程：

```text
app:read 已声明且启用
  → 禁用
  → 旧 Access/Refresh 失效
  → 新登录 Scope 排除
  → 重新启用
  → 新登录 Scope 恢复
  → 同步目录移除 app:read
  → 标记 missing 并清空 endpoint
  → 旧 Token/Session 失效
  → 恢复 Session 仅隔离验证数据库状态仍返回 403
  → 恢复真实声明目录
  → 原 ID、展示信息和角色关联保留
  → endpoint 重建
  → 重新登录 Scope 恢复
```

真实结果：

- 禁用保留 endpoint 和角色关联；
- admin 和生命周期用户的活跃 Session 均被撤销；
- 重复禁用/启用幂等；
- 重新启用不会恢复旧 Session；
- missing 同步删除两个 `app:read` endpoint，保留 Permission ID、展示信息和角色关联；
- missing 权限调用 enable 返回 `PERMISSION_NOT_DECLARED`；
- 为区分 Session 401 与数据库状态 403，测试仅在隔离数据库中恢复一条旧 Session 并清理对应 Redis revocation，旧 Scope 调用 `/admin/apps` 仍返回 403；
- 恢复声明后原 ID、展示信息、启用状态和关联保持，两个 endpoint 重建；
- 只有重新登录后 Scope 恢复。

### 6.4 审计和敏感信息

确认恰好存在：

```text
permission.updated
permission.disabled
permission.enabled
```

并验证：

- Actor、Target、三个 Request ID 正确；
- 禁用原因和 `revoked_sessions=2` 正确；
- no-op 不增加审计；
- 同步不伪造管理员 AuditEvent；
- 响应、审计、异常报告和 API 日志不含收集到的密码、Hash、Access/Refresh Token、SID/JTI、Authorization、RSA 密钥或基础设施凭证。

最终报告：

```text
catalog_counts = [25, 31, 25]
lifecycle_audits_verified = 3
request_ids_verified = 3
redis_revocations_verified = 5
old_sessions_rejected = true
disabled_scope_filter = true
missing_scope_filter = true
database_status_denial = true
reenabled_scope_restore = true
missing_restore_identity_preserved = true
seed_sync_idempotency_verified = true
sensitive_log_scan = clean
```

---

## 7. 验证中修复的验证器问题

没有发现需要修改生产业务代码的缺口。真实验证过程中修复了以下新增验证器问题：

1. 首版同步命令 JSON 读取会把结构化日志 JSON 当成同步结果；改为排除带 `level/logger/message` 的日志对象后读取安全报告。
2. 首版并发 TTL 断言固定为一天，而进程默认配置为七天；改为使用实际 `refresh_token_expire_days`。
3. 独立单权限边界初次对生命周期目标执行 disable，意外改变后续状态；调整为边界请求只使用错误权限，避免成功副作用。
4. 禁用 `app:read` 会同时撤销 admin 和目标用户 Session，不是单一目标 Session；修正真实计数、Redis 和审计断言。
5. in-process 同步最初继承父验证进程的默认 Redis/session prefix；增加显式 settings 替换、`get_redis.cache_clear()` 和 finally 恢复，确保同步与 Uvicorn 使用同一隔离 namespace。
6. 敏感错误信息不再包含 SID 片段。

这些修复均只涉及第五阶段验证器和测试，未修改 `app/` 生产行为。

---

## 8. 自动化验证结果

### 8.1 新集成测试默认行为

```text
3 skipped, 1 warning
```

符合显式 opt-in 设计。

### 8.2 权限定向与 Auth/Seed 回归

```text
99 passed, 1 warning
```

### 8.3 用户、App、Role、Session 管理回归

```text
76 passed, 1 warning
```

### 8.4 权限第五阶段统一验证器

```text
migration: passed
concurrency: passed
http: passed
all: passed
```

### 8.5 权限 opt-in 集成测试

```text
3 passed, 1 warning
```

### 8.6 既有真实集成回归

同一专用 `55432/56379` 基础设施中实际运行：

```text
用户管理 phase4-validate: migration + API passed
App phase5-validate: migration + concurrency + HTTP passed
Role phase5-validate: migration + concurrency + HTTP passed
```

### 8.7 完整检查

```text
pdm run lint: All checks passed!
pdm run test: 228 passed, 13 skipped, 1 warning
git diff --check: passed
```

13 个默认 skipped 包括：

- App 第五阶段真实集成 3 项；
- Permission 第五阶段真实集成 3 项；
- Role 第五阶段真实集成 3 项；
- Permission 第一阶段迁移 1 项；
- Permission 同步并发 1 项；
- 用户管理真实集成 2 项。

唯一 pytest 警告仍为项目既有：

```text
StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated
```

真实验证初始化密码 Hash 时，passlib 仍输出既有 bcrypt 版本元数据兼容提示；Hash、登录、JWT 和全部认证断言正常。Redis 客户端与 Redis 7 镜像还输出 maintenance notification 命令不支持的 debug 提示，不影响读写、TTL、清理或验证结果。本阶段未擅自升级依赖或镜像。

---

## 9. 真实验收与回归结果汇总

本节记录最终一次真实验收及回归执行结果。所有真实基础设施命令均连接专用 `127.0.0.1:55432` PostgreSQL 和 `127.0.0.1:56379/15` Redis，没有连接当前开发环境的 `5432/6379` 服务。

### 9.1 权限第五阶段完整真实验收

执行：

```bash
pdm run permission-phase5-validate --only all
```

真实结果：

```text
[PASS] Permission Alembic migration roundtrip
  current_revision = 0005_permission_management
  alembic_check = clean
  permission_columns_verified = 12
  permission_indexes_verified = 4
  endpoint_columns_verified = 4
  legacy_permission_data_preserved = true
  role_associations_preserved = true
  sequence_preserved = true
  duplicate_endpoint_rejected = true
  temporary_resources_cleaned = true

[PASS] Permission PostgreSQL concurrency and Redis rollback
  advisory_lock_waits_verified = 1
  row_lock_waits_verified = 2
  sync_counts_verified = [25, 31, 25]
  disable_changes = [true, false]
  enable_changes = [true, false]
  distinct_session_revocations = 1
  permission_update_conflict = PERMISSION_VERSION_CONFLICT
  redis_failure_rollback_verified = true
  sync_retry_idempotency_verified = true
  associations_consistent = true
  temporary_resources_cleaned = true

[PASS] Permission JWT, Redis, sync, and HTTP lifecycle
  permissions_verified = 4
  permission_denials_verified = 6
  catalog_counts = [25, 31, 25]
  lifecycle_audits_verified = 3
  request_ids_verified = 3
  redis_revocations_verified = 5
  old_sessions_rejected = true
  disabled_scope_filter = true
  missing_scope_filter = true
  database_status_denial = true
  reenabled_scope_restore = true
  missing_restore_identity_preserved = true
  seed_sync_idempotency_verified = true
  real_rs256_permissions = true
  sensitive_log_scan = clean
  temporary_resources_cleaned = true

Permission phase 5 validation completed successfully.
```

验收项与真实证据对应关系：

| 验收项 | 真实结果 | 结论 |
| --- | --- | --- |
| 迁移升级、降级、再升级 | revision 回到 `0005_permission_management`，`alembic_check=clean` | 通过 |
| 既有权限数据保留 | ID、编码、说明、自增序列和角色关联全部保留 | 通过 |
| 同步并发 | 第二事务被 PostgreSQL 证明确实等待 advisory lock，最终 25/31/25 | 通过 |
| 权限启停并发 | 两次真实行锁等待，禁用和启用均为 `[true, false]` | 通过 |
| Redis 失败回滚 | 数据库无部分提交，恢复后重试成功并收敛为零差异 | 通过 |
| Session 撤销 | PostgreSQL 状态、原因、Redis 值和 TTL 均正确 | 通过 |
| RS256 和 Scope | 四个单权限主体真实签发并验证，disabled/missing 不进入新 Scope | 通过 |
| 旧 Token 实时失效 | 禁用/废弃后旧 Access/Refresh 返回 401；恢复 Session 后数据库状态仍返回 403 | 通过 |
| 废弃与恢复 | 原 ID、展示信息和角色关联保留，两个 endpoint 正确移除和重建 | 通过 |
| Seed 和同步幂等 | 两次 Seed、两次同步及最终 `--check` 均无重复或漂移 | 通过 |
| 审计与敏感信息 | 三项有效审计准确，响应/审计/报告/API 日志扫描 clean | 通过 |

### 9.2 权限 opt-in 集成验收

执行：

```bash
RUN_PERMISSION_MANAGEMENT_PHASE_5_INTEGRATION=1 \
  pdm run pytest tests/test_permission_management_phase_5_integration.py -q
```

真实结果：

```text
3 passed, 1 warning in 16.05s
```

三个测试分别重新执行迁移、并发和 HTTP 真实验证组，全部通过。唯一 warning 是既有 Starlette TestClient/httpx 弃用提示。

### 9.3 权限定向与相关管理回归

权限定向、Auth、Authorization、Seed 和同步回归执行结果：

```text
99 passed, 1 warning in 6.03s
```

覆盖：

- Permission 模型、Schema 和迁移约束；
- 路由扫描、同步计划、同步命令和幂等；
- Permission Service/API；
- 登录 Scope、Refresh Scope 和请求时数据库状态检查；
- API 依赖和 Seed。

用户、App、Role、Session 和 Auth 管理回归执行结果：

```text
76 passed, 1 warning in 10.69s
```

未发现权限第五阶段验证入口对既有管理功能造成行为回归。

### 9.4 既有真实基础设施回归

在同一专用 `55432/56379` 基础设施中运行现有用户、App 和 Role 验证器，真实结果如下。

用户管理：

```text
[PASS] Alembic migration roundtrip
  current_revision = 0005_permission_management
  alembic_check = clean
  legacy_user_preserved = true
  legacy_session_preserved = true

[PASS] PostgreSQL/Redis management API flow
  permission_boundary = 401/403
  redis_revocations_verified = 5
  revoked_database_sessions = 5
  request_ids_verified = 8
  audit_actions = 8
  temporary_resources_cleaned = true

Phase 4 validation completed successfully.
```

App 管理：

```text
[PASS] App Alembic migration roundtrip
  current_revision = 0005_permission_management
  alembic_check = clean
  app_columns_verified = 14
  app_indexes_verified = 4
  legacy_user_preserved = true
  temporary_resources_cleaned = true

[PASS] App PostgreSQL concurrency
  row_lock_waits_verified = 2
  disable_changes = [true, false]
  secret_rotations_serialized = 2
  optimistic_conflict = APP_VERSION_CONFLICT
  temporary_resources_cleaned = true

[PASS] App JWT permission and lifecycle
  permissions_verified = 6
  permission_denials_verified = 8
  lifecycle_audits_verified = 5
  request_ids_verified = 5
  one_time_secret_responses = 2
  old_secret_invalidated = true
  real_jwt_permissions = true
  sensitive_log_scan = clean
  temporary_resources_cleaned = true

App phase 5 validation completed successfully.
```

Role 管理：

```text
[PASS] Role Alembic migration roundtrip
  current_revision = 0005_permission_management
  alembic_check = clean
  role_columns_verified = 9
  role_indexes_verified = 3
  legacy_role_data_preserved = true
  role_associations_preserved = true
  temporary_resources_cleaned = true

[PASS] Role PostgreSQL concurrency
  row_lock_waits_verified = 3
  disable_changes = [true, false]
  enable_changes = [true, false]
  role_assignment_conflict = USER_VERSION_CONFLICT
  role_update_conflict = ROLE_VERSION_CONFLICT
  associations_consistent = true
  temporary_resources_cleaned = true

[PASS] Role JWT, Redis, and HTTP lifecycle
  permissions_verified = 6
  permission_denials_verified = 8
  lifecycle_audits_verified = 5
  request_ids_verified = 5
  redis_revocations_verified = 2
  old_sessions_rejected = true
  disabled_role_claim_filter = true
  reenabled_role_claim_restore = true
  user_role_replacement_verified = true
  seed_idempotency_verified = true
  real_jwt_permissions = true
  sensitive_log_scan = clean
  temporary_resources_cleaned = true

Role phase 5 validation completed successfully.
```

用户、App 和 Role 的迁移、并发、真实授权、Session 撤销及生命周期均无回归。

### 9.5 最终完整回归和环境清理

最终执行：

```bash
pdm run lint
pdm run test
git diff --check
```

真实结果：

```text
lint = All checks passed!
pytest = 228 passed, 13 skipped, 1 warning in 17.31s
git diff --check = passed
```

真实基础设施残留检查：

```text
PostgreSQL tsuz_permission_phase5_* databases = 0
Redis auth:permission-phase5:* keys = 0
```

随后停止并自动删除：

```text
tsuz-permission-phase5-postgres
tsuz-permission-phase5-redis
```

专用验证资源清理完成，现有开发 PostgreSQL、Redis 和应用容器保持运行且未被修改。

---

## 10. 最终文件变更

新增：

```text
scripts/validate_permission_management_phase_5.py
tests/test_permission_management_phase_5_integration.py
plan/PERMISSION_MANAGEMENT_PHASE_5_EXECUTION_PLAN.md
plan/PERMISSION_MANAGEMENT_PHASE_5_EXECUTION.md
```

修改：

```text
pyproject.toml
```

没有修改生产 `app/` 文件或数据库迁移。

---

## 11. 第五阶段验收结论

第五阶段“真实集成与完整回归”验收项已满足：

- `0005_permission_management` 可真实升级、降级、再升级，既有权限 ID、数据、序列和角色关联保留；
- `alembic check` clean；
- 权限同步 advisory lock 和权限启停行锁通过 PostgreSQL 真实等待验证；
- 并发同步、启停、版本和审计保持幂等一致；
- Redis 失败时数据库无部分提交，恢复后重试安全；
- 真实 PostgreSQL/Redis Session 撤销和 TTL 正确；
- 旧 Access/Refresh 在权限禁用和废弃后失效；
- 真实 RS256 Token 的 Scope 仅包含 declared+enabled 权限；
- 即使恢复旧 Session，数据库权限状态检查仍阻止 missing 权限旧 Scope；
- 禁用、启用、废弃和恢复均保留预期 ID、展示信息和角色关联；
- Seed、同步、admin grant 和最终 check 幂等；
- 权限管理独立权限、OpenAPI、固定错误、完整 HTTP 生命周期和安全审计通过；
- 响应、审计、报告、异常和 API 日志敏感信息扫描 clean；
- 用户、App、Role、认证、Session、Seed、完整默认测试和既有真实集成无回归；
- 未实现范围外权限功能。



# 真实验证结果

## PostgreSQL 迁移

- 0004 → 0005 → 0004 → 0005 往返通过
- Alembic check clean
- Permission 12 个字段、4 个索引验证通过
- PermissionEndpoint 主键、外键、索引验证通过
- 既有 Permission ID、说明、自增序列和角色关联均保留
- 重复 endpoint 被数据库约束拒绝

## PostgreSQL 并发与失败恢复

- advisory lock 真实等待：1 次
- 权限行锁真实等待：2 次
- 并发禁用结果：true、false
- 并发启用结果：true、false
- 多角色同权限的 DISTINCT 用户撤销正确
- 乐观锁冲突返回 PERMISSION_VERSION_CONFLICT
- Redis 撤销失败时数据库事务正确回滚
- 恢复后重试成功，再次执行收敛为零差异

## HTTP、JWT、Redis 与同步生命周期

- 最终目录：25 Permission / 31 endpoint / 25 admin grant
- 四项权限管理权限均以独立最小权限用户验证
- 无 Token 401、权限不足 403
- OpenAPI 和 Security 声明正确
- 权限展示信息编辑、no-op、版本冲突和固定错误码通过
- 权限禁用、重复禁用、启用、重复启用通过
- 禁用和废弃均撤销真实 PostgreSQL/Redis Session
- 旧 Access/Refresh Token 失效
- disabled/missing 权限不进入新 JWT Scope
- 恢复旧 Session 后，数据库状态检查仍使旧 Scope 返回 403
- 恢复声明后原 ID、展示信息和角色关联保留
- endpoint 正确移除及重建
- 重新登录后 Scope 恢复
- 敏感日志扫描 clean

# 回归结果

```text
权限第五阶段统一验证器：migration/concurrency/http/all 全部通过
权限 opt-in 集成测试：3 passed
权限定向与 Auth/Seed 回归：99 passed
用户、App、Role、Session 回归：76 passed
现有用户/App/Role 真实集成验证：全部通过
完整测试：228 passed, 13 skipped, 1 warning
Lint：All checks passed!
git diff --check：通过
```

13 个 skipped 均为默认关闭的真实基础设施集成测试。唯一 pytest 警告仍是项目既有的 Starlette TestClient/httpx 弃用提示。

验证使用的 55432/56379 专用临时容器已经停止并自动删除，随机 PostgreSQL 数据库和 Redis 前缀均无残留；现有 5432/6379 开发服务未被操作。

本阶段没有发现需要修改生产业务代码的问题，也没有新增范围外权限功能。