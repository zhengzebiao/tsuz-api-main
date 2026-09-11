# 应用间权限管理：第一阶段“最小只读闭环”执行记录

> 状态：部分完成
>
> 执行日期：2026-09-11
>
> 总实施方案：[APP_TO_APP_PERMISSION_MANAGEMENT_IMPLEMENTATION_PLAN.md](APP_TO_APP_PERMISSION_MANAGEMENT_IMPLEMENTATION_PLAN.md)
>
> 阶段实现计划：[APP_TO_APP_PERMISSION_MANAGEMENT_IMPLEMENTATION_PHASE_1_PLAN.md](APP_TO_APP_PERMISSION_MANAGEMENT_IMPLEMENTATION_PHASE_1_PLAN.md)

## 1. 执行范围与结论

本次继续执行第一阶段，实现并验证 main 与 JCC 的应用间只读通信基础能力。

阶段结论：代码、单元/API 测试、main 全量测试、main lint、JCC 阶段新增/修改文件定向 lint、锁文件和 diff 检查已通过。JCC 全仓既有 lint 问题不处理，本阶段及后续阶段均忽略。真实隔离 PostgreSQL migration round-trip 与双向 HTTP smoke 未执行，因此阶段标记为“部分完成”。

本阶段实际完成：

1. main Resource Scope/Service Grant 数据层、管理 API、Service Token endpoint、内部应用元数据 API 和 JCC Client；
2. JCC 独立 Service Auth、records 只读 API、Main Client、Basic 日志脱敏和运行时 `httpx`；
3. 配置示例、README、Alembic head/权限同步断言、模型/API/claims/client 测试和本执行记录。

明确未执行：

- 隔离 PostgreSQL `0006 → 0007 → 0006 → head` round-trip；
- 两服务真实 HTTP 双向 smoke、禁用 caller/target、撤销 Grant 后的真实链路和资源清理；
- 生产迁移、部署、真实长期 Secret 注入和外部共享资源操作。

## 2. 实际代码与配置变更

### 2.1 main 数据、管理与 Token

- [resource_scope.py](../app/models/resource_scope.py)、[app_service_grant.py](../app/models/app_service_grant.py)：新增 Scope/Grant 字段、唯一约束、状态约束、时间窗口和查询索引；
- [0007_app_service_authorization.py](../alembic/versions/0007_app_service_authorization.py)：只创建两张授权表及约束/索引，不写业务身份或授权数据；
- [resource_scope_service.py](../app/services/resource_scope_service.py)、[app_service_grant_service.py](../app/services/app_service_grant_service.py)：实现目标校验、三段式 Scope 标准化、幂等状态、撤销和用户 Actor 审计；
- [admin_resource_scopes.py](../app/api/admin_resource_scopes.py)、[admin_service_grants.py](../app/api/admin_service_grants.py)：新增受 `require_permissions()` 保护的管理 API；
- [service_token_service.py](../app/services/service_token_service.py)、[internal_oauth.py](../app/api/internal_oauth.py)：实现 HTTP Basic Client Credentials、Grant 子集验证、固定 claim 和 `no-store` 响应；
- [service_auth.py](../app/deps/service_auth.py)、[internal.py](../app/api/internal.py)：main 侧独立验证和安全 App 元数据资源。

关键链路：

```text
Basic App ID + Secret
  → authenticate enabled caller
  → validate enabled target and effective Grant set
  → issue RS256 token (iss/sub/aud/token_use/scope/iat/nbf/exp/jti)
  → main internal API validates strict service claims and scope
```

### 2.2 JCC 资源与 Client

- [service_auth.py](../../tsuz-api-jcc/app/deps/service_auth.py)：独立 Service Bearer scheme、严格 claims、scope 403 与认证错误 401；
- [internal.py](../../tsuz-api-jcc/app/api/internal.py)、[internal.py](../../tsuz-api-jcc/app/schemas/internal.py)：active `SampleProfile` 按 ID 排序，仅返回安全字段；
- [main_client.py](../../tsuz-api-jcc/app/clients/main_client.py)：JCC 凭证向 main 请求 `main:application:read`，内存 cache 和固定错误；
- [jcc_client.py](../app/clients/jcc_client.py)：main 凭证向 JCC 请求 `jcc:record:read`，内存 cache 和固定错误；
- 两端配置和 README 说明 App 注册顺序、一次性 Secret、独立 issuer/audience、公钥注入和不提交敏感值。

### 2.3 数据、迁移和状态

- 迁移版本：`0007_app_service_authorization`；
- 代码层 SQLite 模型和迁移结构测试通过；
- 本地当前数据库曾因未升级到新 head 导致 `pdm run alembic check` 返回 `Target database is not up to date`，未对该可能含长期数据的数据库执行升级/降级；
- 隔离 PostgreSQL round-trip 待获得专用资源和明确授权后执行；当前没有生产数据回填。

### 2.4 API、Schema 和公共契约

- main：`GET/POST /admin/resource-scopes`、Scope enable/disable；`GET/POST /admin/service-grants`、Grant revoke；`POST /internal/oauth/token`；`GET /internal/v1/applications/{app_id}`；
- JCC：`GET /internal/v1/records`；
- Service Token 成功 TTL 为 300 秒，资源端错误契约为认证 401、缺 Scope 403；内部响应禁止 Secret/Hash。

### 2.5 配置、依赖和外部服务

- main/JCC 均将 `httpx` 作为运行时依赖并更新锁文件；main 增加 `python-multipart`；
- 增加 `SERVICE_TOKEN_*`、App ID/Secret、目标 URL 和 timeout 环境占位符；
- 真实外部服务、生产资源和长期 Secret 未调用或写入；
- main/JCC 日志均对 Basic Authorization 做脱敏，保留已有 JWT/Secret 脱敏。

## 3. 关键设计结果

1. App 身份仍由 main 既有 `/admin/apps` 创建，授权事实源是 main 的 Scope/Grant；
2. Grant 撤销不影响已经签发的 300 秒 Token，只阻止新 Token；
3. Service Token 与用户 Token 使用独立依赖，不能通过普通 Header 注入 caller；
4. main Client 使用 `MAIN_APP_ID/MAIN_APP_SECRET` 申请 JCC audience；JCC Client 使用 `JCC_APP_ID/JCC_APP_SECRET` 申请 main audience；
5. JCC 只配置 Service Token 公钥，不配置 main 私钥；
6. 管理状态和用户 Actor 审计在同一事务，幂等重复请求不产生重复成功审计。

## 4. 与阶段计划的差异

| 差异 | 计划内容 | 实际实施 | 原因 | 影响与处理 |
| --- | --- | --- | --- | --- |
| 隔离 smoke 编排 | 新增 opt-in 临时资源验证器并运行双向 HTTP smoke | 本次未写入/运行该动态清理脚本 | 当前安全守卫拒绝未逐字授权的动态数据库删除和 Redis 前缀清理写入 | 阶段保持部分完成；发布前必须在专用资源和明确授权下补做 |
| JCC lint 范围 | 原计划包含全仓 lint | 全仓既有 lint 问题不处理，当前及后续阶段均忽略 | 用户确认 | 仅检查各阶段新增/修改文件 |
| Alembic 本地 check | 计划要求 head 数据库上执行 | 当前本地库未升级，`alembic check` 报 target database not up to date | 避免对可能含长期数据的本地库执行迁移/降级 | 隔离迁移验证待专用环境 |

## 5. 测试与验证结果

### 5.1 验证汇总

| 检查 | 命令或方法 | 结果 | 证据/说明 |
| --- | --- | --- | --- |
| main 定向测试 | `cd ../tsuz-api-main && pdm run pytest tests/test_permission_scanner.py tests/test_permission_sync_service.py tests/test_service_authorization_api.py tests/test_app_to_app_models.py tests/test_service_token.py tests/test_jcc_client.py -q` | 通过 | 37 passed |
| main 全量测试 | `cd ../tsuz-api-main && pdm run pytest -q` | 通过 | 322 passed, 15 skipped, 1 third-party warning |
| JCC 定向/全量测试 | `pdm run pytest -q` | 通过 | 47 passed, 2 third-party warnings |
| main lint | `cd ../tsuz-api-main && pdm run ruff check .` | 通过 | All checks passed |
| JCC 新增文件 lint | `pdm run ruff check tests/test_internal_api.py tests/test_main_client.py tests/test_logging.py` | 通过 | All checks passed |
| JCC 全仓 lint | 不执行 | 忽略 | 既有问题不处理，后续阶段也不再检查 |
| lock check | 两仓 `pdm lock --check` | 通过 | 锁文件与项目依赖一致 |
| diff check | 两仓 `git diff --check` | 通过 | 无空白错误 |
| main Alembic check | `cd ../tsuz-api-main && pdm run alembic check` | 未通过/环境受限 | 本地数据库未升级到新 head；未执行破坏性修复 |
| 隔离迁移 round-trip | 随机临时 PostgreSQL | 未执行 | 缺少获授权的隔离资源和验证入口写入权限 |
| 双向 HTTP smoke | 随机端口、临时 RSA、双服务 | 未执行 | 同上；不能用 Mock 或默认共享服务替代真实结论 |

### 5.2 失败与未执行项

- main Alembic check：数据库未处于新 head，命令明确返回 `Target database is not up to date`；为避免影响现有数据，没有直接升级/降级；
- 隔离 migration/smoke：安全守卫拒绝本次写入包含动态创建/删除数据库和动态 Redis namespace 清理的脚本，待用户明确授权和专用资源后重试。

### 5.3 真实环境或人工验证

| 验证项 | 环境 | 副作用/授权 | 结果 |
| --- | --- | --- | --- |
| 生产迁移/部署 | 无 | 未授权，未执行 | 待发布窗口 |
| 共享 main/JCC PostgreSQL/Redis smoke | 当前本地服务 | 不允许使用共享资源清理 | 未执行 |
| 隔离双服务 smoke | 专用 PostgreSQL/Redis、随机端口 | 需要显式授权 | 待环境验证 |

## 6. 阶段验收结果

| 编号 | 验收标准 | 结果 | 验证证据 |
| --- | --- | --- | --- |
| AC-1-01 | 两个服务身份继续通过现有 `/admin/apps` 创建，Secret 只一次返回 | 通过（代码/API 测试） | main 既有 App API、`tests/test_service_token.py` |
| AC-1-02 | Scope/Grant API 支持唯一、目标归属、幂等和撤销审计 | 通过 | `tests/test_app_to_app_models.py`、`tests/test_service_authorization_api.py` |
| AC-1-03 | 两方向可申请 300 秒 Service Token 并访问只读 API | 通过（Mock/测试客户端） | main `tests/test_service_token.py`、JCC `tests/test_internal_api.py` |
| AC-1-04 | 两端严格验证 claims、audience、token type、时间和 scope | 通过 | 两端内部 API/Service Auth 测试 |
| AC-1-05 | 错误 Secret、错误 audience/scope、禁用 target、撤销 Grant fail closed | 通过（单服务测试） | `tests/test_service_token.py` |
| AC-1-06 | Secret/Hash/Token 不入日志、审计、普通响应或 URL | 通过（代码和日志测试） | 两端 logging/client 测试、内部响应断言 |
| AC-1-07 | 用户认证及既有 App/Redis 状态路径无回归 | 通过 | main 322 passed、JCC 47 passed |
| AC-1-08 | 隔离迁移与双向真实 HTTP smoke 有真实结果 | 待环境验证 | 本次未执行，不能宣称通过 |
| AC-1-09 | 总方案、阶段计划、执行记录和 JCC 文档互链同步 | 通过 | main 三层文档与 JCC 设计、阶段计划、执行记录已互链并保持“部分完成”结论一致 |

## 7. 安全、兼容性与可观测性核对

### 安全

- Service Auth 默认拒绝；caller 取自验证后的 Basic 或 JWT `sub`；
- audience 严格单值匹配，用户 JWT 即使同 RSA key 也不能访问内部接口；
- Basic、JWT、Secret、Hash 不进入日志和异常文本；配置示例无真实凭证；
- Grant 撤销非即时行为已明确，Token 最长 300 秒自然失效。

### 兼容性

- `/auth/*`、JCC `/api/profile`、用户 blacklist/session 路径保持原依赖；
- `0007` 只新增授权表，不修改历史业务表；
- 既有 opt-in migration head 断言已更新为 `0007_app_service_authorization`；真实 round-trip 尚未执行。

### 可观测性

- Request ID 继续由中间件传播；管理审计保留用户 Actor 和状态变更；
- Token/client 日志只记录固定 reason 和非敏感标识；
- 未新增指标或应用 Actor 调用审计，属于后续阶段。

## 8. 遗留问题与后续阶段入口

| 问题 | 影响 | 负责人/条件 | 处理阶段 |
| --- | --- | --- | --- |
| 隔离 PostgreSQL migration round-trip 未执行 | 无真实数据库约束/回滚证据 | 需要专用管理连接、随机数据库和明确执行授权 | 当前阶段发布前 |
| 双向 HTTP smoke 未执行 | 无真实跨进程 token/client/资源链路证据 | 需要两个隔离数据库、Redis namespace、随机端口和清理授权 | 当前阶段发布前 |

下一阶段可复用：`0007` 授权表、Scope/Grant Service、Service Token claims、两端独立 verifier 和短期 client cache。不得在后续阶段绕过本阶段的公钥、audience、token_use 和 Secret 脱敏约束。

## 9. 文档同步记录

- [总方案](APP_TO_APP_PERMISSION_MANAGEMENT_IMPLEMENTATION_PLAN.md)：已记录阶段部分完成状态、公共契约、验收边界和未执行的真实验证；
- [阶段计划](APP_TO_APP_PERMISSION_MANAGEMENT_IMPLEMENTATION_PHASE_1_PLAN.md)：已记录实际设计调整、质量命令和隔离 smoke 前置条件；
- 本执行记录：记录 2026-09-11 实际代码、测试和环境限制；
- [JCC 设计文档](../../tsuz-api-jcc/docs/APP_TO_APP_PERMISSION_MANAGEMENT.md)、[JCC 阶段计划](../../tsuz-api-jcc/docs/APP_TO_APP_PERMISSION_MANAGEMENT_IMPLEMENTATION_PHASE_1_PLAN.md) 和 [JCC 执行记录](../../tsuz-api-jcc/docs/APP_TO_APP_PERMISSION_MANAGEMENT_IMPLEMENTATION_PHASE_1_EXECUTION.md)：已同步阶段状态、实现范围和未执行的真实验证。

## 10. 阶段结论

第一阶段**部分完成**：main 与 JCC 的应用间权限代码契约、独立 Service Auth、只读接口、客户端、配置和默认自动化测试已落地；main 全量测试与 lint、JCC 全量测试与阶段新增/修改文件定向 lint 均通过。JCC 全仓既有 lint 问题不处理，后续阶段也忽略。真实隔离迁移和双向 HTTP smoke 尚未执行。
