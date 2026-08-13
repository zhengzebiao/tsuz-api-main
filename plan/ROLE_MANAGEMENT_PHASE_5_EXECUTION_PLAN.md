# 角色管理第五阶段执行计划

## Context

根据 [ROLE_MANAGEMENT_IMPLEMENTATION_PLAN.md](ROLE_MANAGEMENT_IMPLEMENTATION_PLAN.md) 实施第五阶段“测试与验证”。前四阶段已经完成角色数据层和 `0004_role_management` 迁移、严格 Schema、角色和用户角色 Service、管理 API、权限 Seed，以及禁用角色鉴权过滤。

现有 SQLite 单元和 API 测试覆盖了主要业务分支，但 SQLite 不执行 PostgreSQL 的真实 `SELECT ... FOR UPDATE` 锁语义，API 测试也使用授权替身和 Fake Redis。本阶段不增加新的角色业务功能，而是新增可重复执行的隔离验证器，覆盖真实 PostgreSQL 迁移和并发、真实 Redis Session 撤销、真实 RS256 JWT 和 HTTP 生命周期，并修复验证中确认的最小安全缺口。

## Requirements and constraints

- 默认 `pdm run test` 不依赖外部服务；真实集成测试必须通过环境变量显式启用。
- 真实验证只连接明确隔离的 PostgreSQL/Redis：随机临时数据库、唯一 Redis 前缀、无持久卷临时容器。
- 默认拒绝 PostgreSQL `5432` 和 Redis `6379`，避免误用开发服务；远程地址或默认端口必须显式放行。
- PostgreSQL 清理只删除本次创建的 `tsuz_role_phase5_*` 数据库；Redis 只通过 `SCAN + DELETE` 清理 `auth:role-phase5:<suffix>:` 前缀，不执行 `FLUSHDB` 或 `FLUSHALL`。
- 迁移验证覆盖 `0003_app_management → 0004_role_management → 0003_app_management → head`，并确认既有用户、角色、权限和关联不丢失。
- 并发验证必须通过 `pg_stat_activity.wait_event_type = 'Lock'` 证明真实行锁等待。
- HTTP 验证必须使用真实登录、RS256 JWT、数据库角色权限、真实 PostgreSQL/Redis 和 Uvicorn 子进程，不使用授权替身。
- 响应、审计、错误报告和 API 日志不得包含密码、哈希、Token、Session ID、JWT 私钥或数据库口令。
- 不实现角色删除、角色权限配置、权限目录、子应用角色同步或其他范围外功能。

## Implementation

1. 新增 `scripts/validate_role_management_phase_5.py`：
   - 支持 `migration`、`concurrency`、`http`、`all` 四个验证组；
   - 使用 `ROLE_PHASE5_*` 环境变量；
   - 创建和清理随机临时 PostgreSQL 数据库；
   - 使用唯一 Redis 前缀并验证清理；
   - 动态生成临时 RSA 密钥并启动 Uvicorn 子进程；
   - 对命令错误、HTTP 错误和报告统一脱敏。
2. 迁移组验证：
   - 迁移前插入既有用户、角色、权限、`user_roles` 和 `role_permissions`；
   - 升级后检查角色 9 个字段、3 个索引、默认值、非空约束和关联保留；
   - 执行 `alembic check`；
   - downgrade 后确认只移除角色管理字段和索引，既有数据保留；
   - 再次升级到 `0004_role_management`。
3. 并发组验证：
   - 并发禁用同一角色：一次有效变化、一次幂等，无重复版本或审计；
   - 并发启用同一角色：一次有效变化、一次幂等，禁用元数据正确清空；
   - 并发整体替换同一用户角色：竞争事务等待用户行锁并返回 `USER_VERSION_CONFLICT`；
   - 角色乐观锁编辑：旧版本返回 `ROLE_VERSION_CONFLICT`；
   - 检查最终关联、版本和审计一致。
4. HTTP/JWT/Redis 组验证：
   - Seed 重复执行两次并验证六项角色权限及 admin 关联；
   - 为每项权限创建单权限角色和用户，真实登录后验证 Token roles/scope；
   - 验证 401、403、OpenAPI 和独立权限边界；
   - 完成角色创建、列表、详情、关联用户、无变化编辑、有效编辑、版本冲突、禁用/重复禁用、启用/重复启用；
   - 验证角色禁用和用户角色替换撤销 PostgreSQL/Redis Session，旧 Access/Refresh Token 失效；
   - 验证禁用角色不进入新 Token，重新启用后重新登录恢复；
   - 验证用户角色完整集合替换、禁用角色保留、禁止新增禁用角色和固定错误码；
   - 验证审计动作、Actor、Request ID、原因和安全 changes。
5. 新增 `tests/test_role_management_phase_5_integration.py`：
   - 三个测试分别复用迁移、并发和 HTTP 验证组；
   - 仅当 `RUN_ROLE_MANAGEMENT_PHASE_5_INTEGRATION=1` 时执行。
6. 扩展 `pyproject.toml`：
   - 增加 `role-phase5-validate` PDM 命令。
7. 根据真实日志扫描结果移除认证和 Refresh Token 日志中的原始 Session ID、JTI 和 Refresh Token 替换 Hash 片段，并增加回归断言。
8. 新增第五阶段实现记录，如实记录全部实际结果和既有依赖警告。

## Verification

```bash
pdm run ruff check \
  app/services/auth_service.py \
  app/services/refresh_token_service.py \
  scripts/validate_role_management_phase_5.py \
  tests/test_auth_service.py \
  tests/test_refresh_token_service.py \
  tests/test_role_management_phase_5_integration.py

pdm run pytest \
  tests/test_role_management_models.py \
  tests/test_role_management_schemas.py \
  tests/test_admin_role_service.py \
  tests/test_admin_user_service.py \
  tests/test_admin_roles_api.py \
  tests/test_admin_user_roles_api.py \
  tests/test_auth_service.py \
  tests/test_refresh_token_service.py \
  tests/test_seed.py -q
```

在专用临时服务中运行：

```bash
pdm run role-phase5-validate --only migration
pdm run role-phase5-validate --only concurrency
pdm run role-phase5-validate --only http
pdm run role-phase5-validate --only all
RUN_ROLE_MANAGEMENT_PHASE_5_INTEGRATION=1 \
  pdm run pytest tests/test_role_management_phase_5_integration.py -q
```

最终运行：

```bash
pdm run lint
pdm run test
git diff --check
```

验证结束后停止并删除本次临时 PostgreSQL/Redis 容器，确认随机数据库和 Redis 前缀无残留，并审阅 Git diff 确认没有范围外业务功能。
