# 权限管理第五阶段执行计划

## Context

根据 [PERMISSION_SYNC_DESIGN.md](PERMISSION_SYNC_DESIGN.md) 实施第五阶段“真实集成与完整回归”。前四阶段已完成 `0005_permission_management`、严格权限声明与扫描、PostgreSQL 同步和 Seed 集成、权限管理 API，以及登录 Scope 和请求时数据库状态检查。

现有 SQLite 单元/API 测试覆盖主要业务分支，但不执行 PostgreSQL advisory lock 和 `SELECT ... FOR UPDATE` 的真实等待语义，API 测试也使用授权替身和 Fake Redis。本阶段不增加权限业务功能，而是新增可重复执行的隔离验证器，覆盖真实迁移、同步/启停并发、Redis Session 撤销、RS256 JWT、HTTP 完整生命周期和敏感信息检查，并只修复真实验证确认的最小缺口。

## 安全边界

- 默认 `pdm run test` 不依赖外部服务；真实集成测试必须通过 `RUN_PERMISSION_MANAGEMENT_PHASE_5_INTEGRATION=1` 显式启用。
- 只使用随机 `tsuz_permission_phase5_*` 临时 PostgreSQL 数据库和唯一 `auth:permission-phase5:<suffix>:` Redis 前缀。
- 默认只允许 localhost，并拒绝 PostgreSQL `5432` 和 Redis `6379`；远程地址或默认端口必须显式放行。
- PostgreSQL 只删除本次创建的随机数据库；Redis 只执行 `SCAN + DELETE` 清理本次前缀，禁止 `FLUSHDB`/`FLUSHALL`。
- HTTP 验证动态生成临时 2048 位 RSA 密钥，使用隔离 issuer/audience 和随机本地 Uvicorn 端口。
- 响应、审计、命令报告、异常和 API 日志不得包含密码、Hash、Token、SID/JTI、Authorization Header、RSA 私钥或基础设施凭证。
- 不实现权限手工创建、编码/API 绑定修改、物理删除、角色权限分配、子应用权限同步、菜单权限或数据权限。

## 实施内容

1. 新增 `scripts/validate_permission_management_phase_5.py`：
   - 支持 `migration`、`concurrency`、`http`、`all` 四个验证组；
   - 提供临时数据库、Redis namespace、RSA 密钥、Uvicorn 子进程和统一脱敏；
   - 使用 `PERMISSION_PHASE5_*` 环境变量配置专用基础设施。
2. 迁移组：
   - 验证 `0004 → 0005 → 0004 → head`；
   - 检查 Permission 12 个字段、4 个索引、endpoint 复合主键/外键/索引；
   - 保留既有 Permission ID、编码、说明、自增序列和 `role_permissions`；
   - 执行 `alembic check` 并验证重复 endpoint 被数据库约束拒绝。
3. 并发和失败组：
   - 两个事务并发同步，使用 `pg_stat_activity.wait_event_type='Lock'` 证明 advisory lock 等待；
   - 并发禁用/启用同一权限，证明真实行锁、幂等、版本和单条审计；
   - 通过多个启用角色授予同一权限，验证 DISTINCT 用户的 PostgreSQL/Redis Session 撤销；
   - 验证展示信息乐观锁冲突；
   - 模拟 Redis 撤销失败，确认权限、endpoint、Session 和版本 rollback，恢复后重试一次成功并收敛为零差异。
4. HTTP/JWT/Redis 组：
   - Seed 两次、同步两次并执行 `--check`，确认 25 Permission、31 endpoint、25 admin grant；
   - 为 `permission:read/update/disable/enable` 建立四个单权限用户，使用真实登录和 RS256 Token 验证独立权限及 401/403；
   - 验证 OpenAPI、列表/详情、展示更新/no-op/版本冲突、404/409/422、核心权限保护和安全响应；
   - 完成 `app:read` 禁用、重复禁用、重新启用、重复启用、同步废弃、数据库状态 403、恢复声明和重新登录 Scope 恢复；
   - 验证 ID、展示信息和角色关联保留，endpoint 正确移除/重建；
   - 核对 AuditEvent Actor、Request ID、原因、状态前后值、撤销数量和幂等无额外审计。
5. 新增 `tests/test_permission_management_phase_5_integration.py`：
   - 三个 opt-in 测试分别复用 migration、concurrency、http 报告；
   - 默认 skip，不改变日常测试的外部依赖边界。
6. 扩展 `pyproject.toml`，增加 `permission-phase5-validate` 命令。
7. 新增第五阶段开发执行记录，只写入实际执行结果、skip、警告和验证中确认的修复。

## 验证命令

```bash
pdm run ruff check \
  scripts/validate_permission_management_phase_5.py \
  tests/test_permission_management_phase_5_integration.py

pdm run pytest tests/test_permission_management_phase_5_integration.py -q
```

在无持久卷、非默认端口的专用 PostgreSQL 16 和 Redis 7 中运行：

```bash
pdm run permission-phase5-validate --only migration
pdm run permission-phase5-validate --only concurrency
pdm run permission-phase5-validate --only http
pdm run permission-phase5-validate --only all
RUN_PERMISSION_MANAGEMENT_PHASE_5_INTEGRATION=1 \
  pdm run pytest tests/test_permission_management_phase_5_integration.py -q
```

随后运行权限定向测试、用户/App/Role/Session/Seed 回归和最终检查：

```bash
pdm run lint
pdm run test
git diff --check
```

验证完成后删除专用临时容器，确认随机数据库和 Redis 前缀无残留，并确认当前开发 PostgreSQL/Redis 未被操作。

## 验收标准

- 真实迁移可往返，`alembic check` clean，已有权限数据、序列和关联保留；
- advisory lock 和权限启停行锁等待被 PostgreSQL 证明，并发后无重复版本、审计或关联；
- Seed、同步、check、失败回滚和重试保持幂等；
- 禁用/废弃撤销真实 PostgreSQL/Redis Session，旧 Access/Refresh 失效且旧 Session 不复活；
- 真实 RS256 Scope 只包含 declared+enabled 权限，数据库状态检查阻止旧 Scope；
- 权限查询、更新、启停、废弃、恢复、绑定、独立权限和固定错误完整生命周期通过；
- 响应、审计、报告、异常和 API 日志敏感信息扫描 clean；
- 用户、App、Role、认证、Session、Seed 和完整默认测试无回归，skip 和依赖警告如实记录。
