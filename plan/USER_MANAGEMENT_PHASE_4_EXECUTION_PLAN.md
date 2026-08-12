# 用户管理第四阶段执行计划：测试和接口验证

## 背景与范围

第四阶段在前三阶段数据模型、认证授权、Session 撤销和 `/admin/users` 接口的基础上，补充真实基础设施验证和端到端接口验证。

本阶段覆盖：

- 既有单元测试与 API 测试回归。
- Alembic `0001 → 0002` 升级、`alembic check`、降级到 `0001`、再次升级到 `head`。
- 真实 PostgreSQL 16 中旧用户和旧 Session 数据保留、字段回填、索引和审计表验证。
- 真实 Redis Session 撤销值和 TTL 验证。
- 以真实 HTTP 请求执行登录、权限边界、用户创建、编辑、禁用/启用、拉黑/恢复、密码重置和强制下线流程。
- 验证数据库 Session 撤销、Redis 即时撤销、Access/Refresh Token 失效、幂等语义和审计 Request ID。

本阶段不执行：

- 生产数据库、生产 Redis 或现有开发数据上的 downgrade、清库或破坏性操作。
- 删除用户、角色分配、组织、MFA、设备、登录记录和审计查询。
- 生产环境部署、真实域名调用或真实密钥验证。
- 新增业务数据库迁移。

## 验证入口和隔离策略

新增 `scripts/validate_phase_4.py`，并提供 PDM 命令：

```bash
pdm run phase4-validate
```

脚本默认连接本地 Compose 的 PostgreSQL 管理库和 Redis 专用 DB：

- `PHASE4_ADMIN_DATABASE_URL`，默认 `postgresql+psycopg://test_user:test_password@127.0.0.1:5432/postgres`。
- `PHASE4_REDIS_URL`，默认 `redis://127.0.0.1:6379/15`。
- `PHASE4_API_HOST`，默认 `127.0.0.1`。
- `PHASE4_API_PORT`，默认自动选择空闲端口。

验证前应确认 PostgreSQL 16 和 Redis 7 已健康运行。所有临时数据库名、Redis key prefix、用户邮箱和 Session ID 都包含随机后缀。脚本拒绝非本地主机，若在已经审批且隔离的测试环境执行，必须显式设置 `PHASE4_ALLOW_REMOTE=1`。

资源清理保证：

- PostgreSQL 临时数据库在正常和异常路径都执行 `DROP DATABASE`。
- API 验证使用独立 Uvicorn 子进程，流程结束后终止并回收。
- Redis 只扫描和删除本次随机 prefix 下的 key，不执行 `FLUSHDB` 或 `FLUSHALL`。
- JWT 测试密钥只保存在验证子进程内存中，不写入仓库或环境文件。
- 报告和错误输出不包含数据库密码、JWT 私钥、完整 Access/Refresh Token 或 Authorization Header。

## 覆盖矩阵

### Alembic 和 PostgreSQL

- 在临时数据库运行 `upgrade 0001`。
- 以旧结构插入用户和活动 Session。
- 运行 `upgrade 0002` 并确认旧数据保留。
- 确认 `is_blacklisted=false`、`created_at`、`updated_at` 和 `version=1` 已回填。
- 确认 Session 撤销列、`audit_events` 表和用户/Session/审计关键索引存在。
- `alembic check` 返回 `No new upgrade operations detected`。
- `downgrade 0001` 成功且旧数据保留。
- 再次 `upgrade head`，`alembic current` 为 `0002_user_management`。

### Redis Session 撤销

真实 HTTP 管理流程会创建多个真实 Session，并逐项确认：

- 改邮箱使用 `email_changed` 撤销目标全部 Session。
- 禁用使用 `user_disabled` 撤销目标全部 Session。
- 拉黑使用 `user_blacklisted` 撤销目标全部 Session。
- 重置密码使用 `password_reset` 撤销目标全部 Session。
- 强制下线使用 `admin_force_logout` 撤销目标全部 Session。
- PostgreSQL `status/revoked_at/revoked_reason` 正确写入。
- Redis 对应 Session key 值为 `revoked`，TTL 大于 0 且不超过 Refresh Token 最大有效期。
- 重复强制下线返回 `revoked_sessions=0`。
- 启用和恢复不复活旧 Session。

### HTTP 管理接口

真实流程验证：

1. 未携带 Token 请求管理列表返回 401。
2. Seed 管理员登录并调用 `/auth/me`。
3. 创建用户返回 201，邮箱标准化，响应不含密码字段。
4. 普通用户 Token 调用管理接口返回 403。
5. 列表和详情返回安全用户资料。
6. PATCH 邮箱/显示名称递增版本并撤销旧 Session。
7. 禁用、启用、拉黑、恢复独立维护状态；被拉黑用户不能直接启用。
8. 密码重置使旧密码、旧 Access Token 和旧 Refresh Token 失效，新密码可以登录。
9. 强制下线使当前 Token 失效，并且重复请求不重复撤销。
10. 审计包含管理员、目标、动作、结果、变化和指定 Request ID，且不含密码、哈希或 Token。

## 自动化测试

默认测试仍不访问外部服务：

```bash
pdm run lint
pdm run test
```

新增 `tests/test_phase_4_integration.py` 默认跳过。只有显式配置以下开关才执行真实验证：

```bash
RUN_PHASE_4_INTEGRATION=1 pdm run pytest tests/test_phase_4_integration.py -q
```

该测试复用第四阶段脚本的隔离资源和流程；缺少真实 PostgreSQL/Redis 时不会污染默认测试结果。

## 执行记录

验证日期：2026-08-11。

### 快速验证

以下命令均通过：

- `pdm run lint`：Ruff `All checks passed!`。
- `pdm run test`：78 项通过，2 项第四阶段集成测试默认跳过，1 条既有 Starlette `TestClient` 依赖弃用警告。
- `git diff --check`：通过。
- `python -m py_compile scripts/validate_phase_4.py tests/test_phase_4_integration.py`：通过。

默认测试仍然不连接外部 PostgreSQL 或 Redis；第四阶段集成测试通过环境变量显式启用。

### Alembic 真实迁移验证

执行：

```bash
pdm run phase4-validate -- --only migration
```

结果：通过。

- 使用 PostgreSQL 16 临时数据库 `tsuz_phase4_migration_0a29a66b8629`，不触碰现有 `test_auth` 数据库。
- 旧用户和旧 Session 在 `0001 → 0002` 升级后保留。
- `is_blacklisted=false`、`created_at`、`updated_at` 和 `version=1` 回填正确。
- Session 撤销字段、`audit_events` 表及关键索引创建成功。
- `alembic check` 返回 `No new upgrade operations detected`。
- `downgrade 0001` 成功，旧用户和旧 Session 仍然保留。
- 再次升级到 `head` 成功，`alembic current` 为 `0002_user_management`。
- 临时数据库已清理。

### PostgreSQL、Redis 和真实 HTTP 流程

执行：

```bash
pdm run phase4-validate -- --only management
```

结果：通过。

- 使用 PostgreSQL 16 临时数据库 `tsuz_phase4_api_e09b9fb8bc5e` 和 Redis 7 专用 key namespace；没有执行 `FLUSHDB` 或 `FLUSHALL`。
- 真实 Uvicorn 子进程通过 `/health` 后，使用真实 HTTP 请求验证管理流程。
- 验证了未认证管理请求 401，以及普通用户调用管理接口 403。
- 验证了创建、列表、详情、PATCH 改邮箱、禁用、启用、拉黑、恢复、重置密码和强制下线。
- 验证了 5 类 Session 撤销原因：`email_changed`、`user_disabled`、`user_blacklisted`、`password_reset`、`admin_force_logout`。
- 共验证 5 个真实 Session 的 PostgreSQL 状态、撤销时间、撤销原因、Redis `revoked` 标记和 TTL。
- 旧 Access Token 和 Refresh Token 在邮箱修改、禁用、拉黑、密码重置和强制下线后均被拒绝。
- 重复强制下线返回 `revoked_sessions=0`；启用和恢复没有复活旧 Session。
- 验证 8 个 Request ID 和全部 8 类管理审计动作，审计 actor/target 正确且不含密码、密码哈希或 Token。
- 临时数据库、Redis 测试 key 和 API 子进程已清理。

### 显式集成测试

执行：

```bash
RUN_PHASE_4_INTEGRATION=1 pdm run pytest tests/test_phase_4_integration.py -q
```

结果：2 项通过，1 条既有 Starlette `TestClient` 依赖弃用警告。

### 结果和限制

第四阶段的本地隔离迁移、Redis Session 撤销和真实 HTTP 管理流程验证已完成。验证使用本地 PostgreSQL 16/Redis 7 服务和随机临时资源，没有执行开发或生产数据库的降级、清库或真实密钥操作。

继续保留 1 条来自 FastAPI/Starlette `TestClient` 的依赖弃用警告；它不影响本阶段业务验证，未在本阶段擅自升级依赖。生产环境部署、跨服务调用、真实域名、真实密钥和高并发压力测试不属于本阶段执行范围。
