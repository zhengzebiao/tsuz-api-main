# Context

第一至三阶段已经完成用户管理数据模型与 `0002_user_management` 迁移、认证/RBAC/Session 撤销基础，以及 `/admin/users` 全部管理接口。现有 78 项自动化测试主要使用内存 SQLite、FakeRedis 和 FastAPI `TestClient`；第一阶段文档记录过一次临时 PostgreSQL 迁移往返验证，但仓库中还没有可重复执行的第四阶段验证入口，也没有使用真实 PostgreSQL/Redis 调用完整管理流程。

本阶段的目标是把 `USER_MANAGEMENT_IMPLEMENTATION_PLAN.md` 第四阶段落地为可重复、隔离且不会触碰开发/生产数据的验证工具和测试：保留默认快速测试的可用性，增加显式 opt-in 的真实基础设施验证，覆盖 Alembic Upgrade/Downgrade/Re-upgrade、Redis Session 撤销，以及从登录到 `/admin/users` 全部管理操作的端到端流程，并记录真实验证结论。

## 现状与约束

- 现有单元/API 测试位于 `tests/test_admin_user_service.py`、`tests/test_admin_users_api.py` 及认证、授权、Seed、Session 测试文件中，默认不应依赖外部服务。
- `app.core.database` 在导入时创建 SQLAlchemy engine，真实集成进程必须在导入 `app` 前设置数据库和 Redis 环境变量。
- Alembic 通过 `app.core.config.settings.database_url` 读取连接；`0001_initial_auth_schema` 和 `0002_user_management` 已支持迁移往返。
- `SessionService.revoke_user_sessions()` 同时更新 PostgreSQL `sessions` 和 Redis `{SESSION_PREFIX}{sid}`，TTL 由 Refresh Token 最大寿命决定。
- 管理 API 依赖管理员 Access Token 的细粒度 scope；管理员 Seed 通过 `python -m app.seed` 幂等补齐权限。
- 本地当前有 PostgreSQL 16 和 Redis 7 容器，但不能假设其数据库、Redis DB 或 API 进程可被安全清空；禁止在未隔离的 `test_auth`、开发或生产数据库上执行 downgrade、清库或破坏性测试。

## 推荐实施方案

### 1. 增加可重复的真实基础设施验证脚本

新建 `scripts/validate_phase_4.py`，作为第四阶段唯一编排入口，默认只做参数检查，必须显式执行 `pdm run phase4-validate` 或 `python scripts/validate_phase_4.py` 才连接外部服务。

脚本使用显式环境变量（提供安全默认值仅适用于本地 Compose）：

- `PHASE4_ADMIN_DATABASE_URL`：具备创建/删除临时数据库权限的 PostgreSQL 管理连接。
- `PHASE4_REDIS_URL`：专用 Redis URL，默认使用独立 Redis DB 编号。
- `PHASE4_API_HOST`、`PHASE4_API_PORT`：本地 API 子进程监听地址。
- 临时数据库名、Redis key prefix 和测试邮箱使用唯一后缀；所有资源在 `finally` 中清理。

脚本分为三个可单独报告的步骤：

1. **Alembic 往返验证**
   - 通过管理连接创建唯一临时 PostgreSQL 数据库，并以 `DATABASE_URL` 环境覆盖运行 `alembic upgrade 0001_initial_auth_schema`。
   - 使用旧表结构插入一条用户和一条 Session，再运行 `alembic upgrade 0002_user_management`。
   - 验证旧数据保留、状态/时间/version 字段回填、Session 撤销列、审计表和关键索引存在。
   - 运行 `alembic check`，随后 `alembic downgrade 0001_initial_auth_schema`，确认新增结构被移除且旧数据保留，再次 `alembic upgrade head` 并用 `alembic current` 确认 `0002_user_management (head)`。
   - 失败时保留清晰的命令、数据库和 revision 信息；正常或异常退出都删除临时数据库。

2. **真实 PostgreSQL + Redis 的管理 API 流程**
   - 在临时数据库升级到 head 并执行幂等 Seed；生成仅存在于临时目录/子进程环境的 RSA 测试密钥。
   - 在迁移后的环境变量下启动独立 Uvicorn 子进程，等待 `/health`，用真实 HTTP 客户端调用接口，而不是直接调用 Service。
   - 以 Seed 管理员登录，取得 Access/Refresh Token，按完整业务顺序调用：列表/详情、创建、PATCH 编辑、目标用户登录、禁用、启用、目标重新登录、拉黑、恢复、重置密码、重新登录、强制下线。
   - 对每一步检查 HTTP 状态码、稳定错误码、版本和状态独立性、密码切换、旧 Refresh Token/Access Token 被拒绝、审计 actor/target/action/Request ID，以及响应/日志不含密码哈希或 Token。
   - 验证禁用、拉黑、改邮箱、重置密码和强制下线返回的撤销数量，并在真实数据库检查 `status/revoked_at/revoked_reason`，在真实 Redis 检查对应 session key 的 `revoked` 值和 TTL；验证启用/恢复不复活旧 Session，重复强制下线返回 0。
   - 使用专用 Redis key prefix 清理测试 key，停止并回收 API 子进程，不影响已有本地服务。

3. **验证报告输出**
   - 输出每个步骤的 PASS/FAIL、临时资源、迁移 revision 和接口摘要；不输出连接密码、JWT 私钥、完整 Token 或 Authorization Header。
   - 退出码非零表示任一验证失败，便于本地和 CI 手工调用。

### 2. 补充可回归的集成测试封装

新建 `tests/test_phase_4_integration.py`，测试只在 `RUN_PHASE_4_INTEGRATION=1` 时启用，否则使用 pytest skip，不影响默认 `pdm run test`。测试复用脚本中的安全配置/清理辅助函数，避免在测试文件中复制业务流程；至少提供：

- 真实 Redis Session 批量撤销、TTL 和重复调用断言。
- 真实 PostgreSQL 迁移往返和旧数据保留断言。
- 真实 HTTP 管理流程的关键断言和权限边界断言。

默认单元测试继续覆盖 FakeRedis/SQLite 快速路径；真实集成测试必须通过专用连接变量运行，不读取或清空默认数据库。

### 3. 提供稳定命令和阶段文档

- 在 `pyproject.toml` 增加 `phase4-validate = "python scripts/validate_phase_4.py"`，同时保留 `pdm run lint`、`pdm run test` 等既有命令。
- 新建 `USER_MANAGEMENT_PHASE_4_EXECUTION_PLAN.md`，记录第四阶段范围、运行前提、隔离策略、覆盖矩阵、实际命令、验证结果、警告和未覆盖内容。
- 如需要让集成环境可发现参数，在 `.env.test.example` 或文档中只记录非敏感变量名和示例主机/端口，不提交任何真实密码、JWT 私钥或 Token。
- 不新增业务迁移，不改变 `/admin/users` API 契约；若真实 PostgreSQL/Redis 验证发现实现缺陷，只做必要的最小修复并为该回归补测试。

## 关键文件

- `scripts/validate_phase_4.py`：临时 PostgreSQL/Redis 资源管理、Alembic 往返、API 子进程和完整流程编排。
- `tests/test_phase_4_integration.py`：显式 opt-in 的真实基础设施回归测试。
- `pyproject.toml`：第四阶段验证命令。
- `USER_MANAGEMENT_PHASE_4_EXECUTION_PLAN.md`：执行说明和最终验证结论。
- 复用 `alembic/env.py`、`alembic/versions/0001_initial_auth_schema.py`、`alembic/versions/0002_user_management.py`、`app/services/session_service.py`、`app/services/auth_service.py`、`app/seed/__main__.py`、`app/api/admin_users.py` 及现有测试 fixtures；不重复实现认证、Session 或用户管理业务逻辑。

## Verification

1. 先运行默认快速验证：`pdm run lint`、`pdm run test`、`git diff --check`。
2. 确认 Docker PostgreSQL 16/Redis 7 健康，并准备专用管理员连接、专用 Redis DB/prefix 和临时 JWT 密钥；不使用生产配置。
3. 运行 `pdm run phase4-validate`，确认 Alembic `upgrade → check → downgrade → upgrade`、真实 Redis 撤销和真实 HTTP 管理流程均通过；记录完整输出但脱敏敏感值。
4. 必要时单独运行 `RUN_PHASE_4_INTEGRATION=1 pdm run pytest tests/test_phase_4_integration.py -q`，确认 opt-in 集成测试通过；默认 pytest 不应因缺少外部服务失败。
5. 最终检查临时数据库已删除、Redis 专用 key 已清理、API 子进程已回收、工作区无生成的密钥/环境文件；更新第四阶段文档，明确实际通过项、警告和未执行的生产验证。
