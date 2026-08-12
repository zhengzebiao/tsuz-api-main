# Context

根据 [SUB_APP_DEVELOPMENT_PLAN.md](../SUB_APP_DEVELOPMENT_PLAN.md) 实施子应用管理 App 的第五阶段“测试与验证”。前四阶段已经完成 `App` 模型与 `0003_app_management` 迁移、App ID/Secret 安全工具与严格 Schema、`AdminAppService`、`/admin/apps` API 和六项权限 Seed；本阶段不增加新的业务功能，而是使用可重复执行的自动化验证覆盖真实 PostgreSQL 并发、真实 RS256 JWT/角色权限、完整 HTTP 生命周期、迁移回环和敏感信息泄漏检查，并修复验证中确认的最小缺口。

现有 SQLite 单元和 API 测试已经覆盖主要业务分支，但 SQLite 不执行 PostgreSQL 的真实 `SELECT ... FOR UPDATE` 语义，第四阶段 API 测试也使用授权替身。仓库已有用户管理集成验证模式 `scripts/validate_phase_4.py` 和 `tests/test_phase_4_integration.py`，第五阶段沿用其“显式启用、临时数据库、唯一 Redis 前缀、API 子进程、脱敏报告”模式，同时为 App 模块提供独立入口和更严格的本地端口隔离。

所有真实验证只连接无持久卷的临时 PostgreSQL/Redis 容器：使用不同于开发服务的主机端口、随机临时数据库、Redis DB 15 和唯一 Key 前缀。绝不连接或修改开发 PostgreSQL/Redis，绝不执行 `FLUSHDB` 或 `FLUSHALL`；清理只针对本次生成的数据库、Redis 前缀和临时容器。

# Requirements and constraints

- 保持第五阶段范围为测试、验证、必要的安全加固和文档，不实现删除、应用编码、菜单、OAuth 回调、Token Scope、权限范围、状态监控或双密钥轮换。
- 默认 `pdm run test` 不依赖外部服务；真实集成测试必须通过环境变量显式启用，否则保持 skip。
- 真实迁移验证覆盖 `0002_user_management → 0003_app_management → 0002_user_management → head`、`alembic check`、表字段、默认值、索引和既有数据保留。
- 真实并发验证覆盖乐观锁编辑冲突、并发禁用的行锁等待与幂等结果，以及并发 Secret 重新生成的串行执行和最终 Hash 一致性。
- 真实 HTTP 验证使用实际登录、RS256 Token、数据库角色权限和 Redis 会话状态，不使用授权替身；覆盖 401/403、六项 App 权限边界和完整生命周期。
- 创建与重新生成响应可以一次性返回 Secret，且必须设置 `Cache-Control: no-store`；其他响应、审计、异常、验证报告和 API 日志不得包含明文 Secret 或 Hash。
- 修正旧用户管理验证脚本对 Alembic head 的硬编码，使其在新增 `0003_app_management` 后仍能验证 `0002_user_management` 中间状态并最终升级到当前 head。

# Implementation plan

1. **建立 App 第五阶段隔离验证器**
   - 新增 `scripts/validate_app_phase_5.py`，提供 `migration`、`concurrency`、`http` 和 `all` 四种运行模式以及结构化安全报告。
   - 配置使用 `APP_PHASE5_*` 环境变量；本地默认使用 PostgreSQL `55432` 和 Redis `56379`，拒绝误用开发默认端口 `5432/6379`，远程或默认端口必须显式放行。
   - 临时数据库名称使用 `tsuz_app_phase5_*` 前缀；Redis 使用 DB 15 下的随机 `auth:app-phase5:<suffix>:` 前缀。数据库通过上下文管理器创建、终止连接和删除；Redis 仅使用 `SCAN + DELETE` 清理对应前缀。
   - 命令失败、HTTP 错误和最终报告统一脱敏 App Secret、Token、密码、JWT 私钥和数据库口令；报告只输出计数与状态，不输出凭证。

2. **验证迁移和真实 PostgreSQL 并发语义**
   - 迁移组先升级到 `0002_user_management` 并写入一条既有用户，再升级 `head`；检查 `apps` 的 14 个字段、约束、默认值和四个索引，并运行 `alembic check`。
   - 降级到 `0002_user_management`，确认仅 `apps` 被移除且既有用户保留；再次升级 `head` 并确认当前 revision 为 `0003_app_management`。
   - 并发组在独立临时数据库中使用多个 SQLAlchemy Session 调用现有 `AdminAppService`，通过 PostgreSQL 后端 PID 和 `pg_stat_activity` 验证竞争事务实际处于 `Lock` 等待。
   - 并发禁用断言一次 `changed=true`、一次 `changed=false`、版本只增加一次、首次禁用原因不被覆盖且只有一条 `app.disabled` 审计。
   - 并发 Secret 重新生成断言两个事务串行提交、产生两条安全审计，最终 Hash 只匹配最后一次成功提交的 Secret，初始 Secret 和中间 Secret 均失效。
   - 乐观锁编辑断言旧版本抛出 `APP_VERSION_CONFLICT`，且不会覆盖先提交的更新。

3. **验证真实 JWT 权限和完整 HTTP 生命周期**
   - 在独立临时数据库执行迁移并重复 Seed 两次，确认 App 权限及 admin 角色关联幂等；动态生成临时 RS256 密钥，以隔离环境启动 Uvicorn 子进程。
   - 创建无 App 权限用户及六个单权限角色/用户，通过真实 `/auth/login` 获取 Token，解码并确认每个 Token 只含预期 App scope。
   - 对六项权限分别验证成功动作和错误权限 Token 的 403；同时验证未携带 Token 为 401。
   - 通过真实 HTTP 完成 `创建 → 列表/详情 → 无变化编辑 → 有效编辑 → 旧版本冲突 → 禁用/重复禁用 → 启用/重复启用 → Secret 重新生成 → 详情复核`。
   - 直接检查 PostgreSQL 中只保存 Secret Hash；轮换后旧 Secret 失效、新 Secret 匹配。普通响应不含 Secret/Hash，两个一次性 Secret 响应设置 `no-store`，幂等操作不增加版本或审计。
   - 检查 `AuditEvent` 的动作、Actor、Request ID、原因和非敏感 changes；扫描响应、审计和捕获的 API 日志，确认实际 Secret、Hash、Token、密码及私钥均未泄漏。

4. **补强自动化测试和日志防护**
   - 新增 `tests/test_app_phase_5_integration.py`，复用验证器三个分组；仅当 `RUN_APP_PHASE_5_INTEGRATION=1` 时运行，默认测试套件保持 skip。
   - 修改 `app/core/logging.py`，在集中脱敏逻辑中加入 `app_secret`、`app_secret_hash` 字段和裸 `app_secret_...` 模式，形成“业务代码不记录 + 日志层兜底”的双重防护。
   - 扩充 `tests/test_logging.py`，验证键值、JSON 和无字段名前缀的 App Secret 均被脱敏。
   - 更新 `scripts/validate_phase_4.py` 与 `tests/test_phase_4_integration.py` 对当前 Alembic head 的断言，保持既有用户管理隔离验证可用。
   - 在 `pyproject.toml` 增加 `app-phase5-validate` PDM 命令，不改变现有 lint/test 命令。

5. **导出第五阶段计划与开发记录**
   - 新增 `plan/SUP_APP_MANAGEMENT_PHASE_5_EXECUTION_PLAN.md`，记录执行方案、隔离边界和验收命令。
   - 新增 `docs/SUB_APP_TEST_VALIDATION_IMPLEMENTATION.md`，记录实际迁移、并发、JWT 权限、生命周期、敏感信息检查、完整测试结果及临时资源清理情况。
   - 文档明确临时容器、随机数据库和 Redis 前缀均属于本次验证，开发 PostgreSQL/Redis 未连接、未写入、未清空。

# Critical files

- `scripts/validate_app_phase_5.py` — 第五阶段迁移、并发、真实 HTTP/JWT 和泄漏验证编排。
- `tests/test_app_phase_5_integration.py` — opt-in 临时基础设施集成测试。
- `app/core/logging.py` 与 `tests/test_logging.py` — App Secret 日志脱敏兜底及测试。
- `scripts/validate_phase_4.py` 与 `tests/test_phase_4_integration.py` — 既有验证器当前 Alembic head 兼容修正。
- `pyproject.toml` — 可重复执行的第五阶段验证命令。
- `plan/SUP_APP_MANAGEMENT_PHASE_5_EXECUTION_PLAN.md` 与 `docs/SUB_APP_TEST_VALIDATION_IMPLEMENTATION.md` — 计划日志和开发记录。

# Verification

1. 运行不需要外部服务的定向测试与静态检查：
   - `pdm run ruff check app/core/logging.py scripts/validate_app_phase_5.py scripts/validate_phase_4.py tests/test_logging.py tests/test_app_phase_5_integration.py tests/test_phase_4_integration.py`
   - App 第一至第五阶段相关 SQLite 单元/API 测试。
2. 启动两个无持久卷、独立主机端口的临时容器（PostgreSQL `55432`、Redis `56379`），确认不复用开发容器、端口或数据卷。
3. 将 `APP_PHASE5_ADMIN_DATABASE_URL` 和 `APP_PHASE5_REDIS_URL` 指向临时容器，运行：
   - `pdm run app-phase5-validate`
   - `RUN_APP_PHASE_5_INTEGRATION=1 pdm run pytest tests/test_app_phase_5_integration.py -q`
4. 使用同一临时容器和隔离环境变量运行既有 `RUN_PHASE_4_INTEGRATION=1` 测试，确认 `0003` 引入后用户管理集成流程无回归。
5. 运行 `pdm run lint`、`pdm run test` 和 `git diff --check`；默认测试应全部通过，仅 opt-in 外部集成测试保持 skip。
6. 检查验证报告和 API 日志中不存在 App Secret、Secret Hash、Token、密码、私钥或数据库口令；确认 Redis 只清理随机前缀，未调用 `FLUSHDB` 或 `FLUSHALL`。
7. 停止并删除本次临时容器，复查开发 PostgreSQL/Redis 未被操作；审阅 Git diff，确认没有实现第五阶段范围外的功能。
