# 应用间权限管理实施方案

> 状态：部分完成（第一阶段代码、单元/API 测试和本地质量检查已完成；隔离跨服务 PostgreSQL/Redis HTTP smoke 待显式授权和专用资源）
>
> 本方案基于 `tsuz-api-main` 与 `tsuz-api-jcc` 当前 FastAPI、PostgreSQL、Redis、SQLAlchemy、Alembic、PyJWT 和 PDM 架构。
>
> 相关设计：[JCC 应用间权限管理设计](../../tsuz-api-jcc/docs/APP_TO_APP_PERMISSION_MANAGEMENT.md)

## 1. 已确认业务配置与关键决策

| 项目 | 决策或配置 | 状态/来源 |
| --- | --- | --- |
| 统一签发中心 | `tsuz-api-main` 通过 `POST /internal/oauth/token` 签发 Service Token | 已确认；阶段设计 |
| 应用注册 | 两个服务身份均通过现有 `POST /admin/apps` 创建；App ID 随机生成，Secret 只在创建/重生成响应中一次返回 | 已确认；现有 main API 与阶段计划 |
| 授权模型 | `caller app → target app → scope`，Grant 不重复保存 target，target 从 Resource Scope 推导 | 已确认；设计文档 |
| Token | RS256、`token_use=service`、单值 audience、固定 300 秒、无 refresh token | 已确认；阶段计划与代码测试 |
| 第一阶段链路 | main → JCC `jcc:record:read`；JCC → main `main:application:read` | 已确认；阶段计划 |
| 即时撤销 | 不实现；Grant 撤销只阻止新 Token，已签发 Token 最长持续到 `exp` | 已确认；阶段边界 |
| 真实 smoke | 只允许随机隔离 PostgreSQL/Redis、随机端口和临时密钥；不连接共享/生产资源 | 已确认；安全约束，当前待执行 |

## 2. 背景与现状

main 已拥有 App 注册、一次性 Secret、用户 JWT、权限同步和用户 Actor 审计能力；JCC 已拥有 `SampleProfile` 只读数据和用户 JWT 资源服务。第一阶段补齐两服务之间的最小只读通信闭环，同时保持用户认证链路独立。

可复用入口：

- main [App 管理 API](../app/api/admin_apps.py)、[App Secret 校验](../app/core/security.py)；
- main [权限声明与同步](../app/services/permission_scanner.py)、[数据库与迁移](../alembic/env.py)；
- JCC [SampleProfile](../../tsuz-api-jcc/app/models/sample_profile.py)、[用户鉴权](../../tsuz-api-jcc/app/deps/auth.py)；
- 两端现有日志脱敏、Request ID、中间件和 PDM 测试脚本。

现状差距是：没有 Resource Scope/Service Grant 表、Service Token endpoint、独立 service claims 验证、内部只读接口和双向短期客户端。

## 3. 目标与非目标

### 3.1 目标

1. 新增 Resource Scope 与 App Service Grant 数据层和管理员 API；
2. 新增独立 Service Token 签发与验证，严格校验 issuer、audience、token type、时间、claim 类型和 scope；
3. 打通两个只读内部接口及两端内存 Token cache；
4. 保持用户 `/auth/*`、`/api/profile`、Redis blacklist/session 和既有 App 管理 API 兼容；
5. 用定向测试、全量测试、lint、锁文件检查和隔离环境 smoke 验证安全边界。

### 3.2 非目标

- 应用间写接口、幂等键、复杂审批、限流、mTLS 和应用 Actor 审计；
- 多 Secret 并行轮换、JWKS、Introspection、即时 Token 撤销和 Refresh Token；
- 通过迁移/Seed/固定脚本创建 App 身份或写入长期 Secret；
- main 直接访问 JCC 数据库；
- 生产迁移、部署或真实长期 Secret 注入。

## 4. 核心流程与契约

```text
管理员 /admin/apps 创建两个 App
  ↓ 保存一次性 Secret 到 Secret Store
管理员创建 Resource Scope 与 Service Grant
  ↓
客户端 Basic App 凭证 → main /internal/oauth/token
  ↓ 300 秒 Service Token
目标服务独立验证 iss/sub/aud/token_use/scope/时间
  ↓
main → JCC /internal/v1/records
JCC → main /internal/v1/applications/{app_id}
```

main token endpoint 只接受 `client_credentials`、`audience` 和空格分隔 scope；请求的 scope 必须是有效 Grant 的完整子集。错误凭证统一为 `401 invalid_client`，未授权 scope 为 `400 invalid_scope`，成功/错误响应均为 `Cache-Control: no-store`。

资源端使用独立 Bearer scheme。JCC records 只返回 active `SampleProfile` 的 `id/slug/display_name/is_active`；main application endpoint 复用安全 `AdminAppResponse`，不返回 Secret 或 Hash。

## 5. 数据与状态设计

main 新增：

- `resource_scopes`：`target_app_id`、三段式 `scope_code`、描述、启用状态和时间字段；唯一约束为 `(target_app_id, scope_code)`；
- `app_service_grants`：`caller_app_id`、`scope_id`、enabled/revoked 状态、有效时间、用户 Actor、撤销原因；唯一约束为 `(caller_app_id, scope_id)`；
- Alembic `0007_app_service_authorization` 只创建表、约束、索引和外键，不写入业务数据。

Grant 查询同时要求 caller、推导的 target、Scope enabled、Grant enabled、`valid_from` 已到且 `expires_at` 未到。状态变更与 AuditEvent 在同一事务提交。

## 6. 模块与配置

main 新增/修改：

- `app/models/resource_scope.py`、`app/models/app_service_grant.py`；
- `app/services/resource_scope_service.py`、`app/services/app_service_grant_service.py`、`app/services/service_token_service.py`；
- `app/api/admin_resource_scopes.py`、`app/api/admin_service_grants.py`、`app/api/internal_oauth.py`、`app/api/internal.py`；
- `app/deps/service_auth.py`、`app/clients/jcc_client.py`；
- 配置示例、README、运行时 `httpx` 与 `python-multipart`。

JCC 新增/修改：

- `app/deps/service_auth.py`、`app/api/internal.py`、`app/schemas/internal.py`、`app/clients/main_client.py`；
- `app/core/config.py`、日志 Basic 脱敏、运行时 `httpx`；
- 三套环境示例与 README。

Secret 只通过运行时 Secret Store 注入。JCC 只接收 Service Token 公钥，不接收 main 私钥。两端 Client 均显式配置 timeout、只在 token endpoint 使用 Basic，并在内存中提前刷新短期 Token。

## 7. 测试与验收

已实现并验证：模型约束、Scope/Grant API、Token claims/TTL/错误边界、两端 service auth、内部资源字段白名单、Client cache/错误脱敏、用户 Token 隔离、权限目录和迁移 head 静态断言。

待执行：使用显式 opt-in 的随机隔离 PostgreSQL/Redis、随机端口和临时 RSA key，真实 HTTP 创建 App、创建 Scope/Grant、双向 Token/资源调用、禁用 target、撤销 Grant、日志扫描和资源清理。当前环境未授权写入动态删除资源的验证脚本，不能将该项标记为通过。

## 8. 分阶段实施顺序

### 第一阶段：最小只读闭环

> 状态：部分完成
>
> JCC 阶段计划：[APP_TO_APP_PERMISSION_MANAGEMENT_IMPLEMENTATION_PHASE_1_PLAN.md](../../tsuz-api-jcc/docs/APP_TO_APP_PERMISSION_MANAGEMENT_IMPLEMENTATION_PHASE_1_PLAN.md)
>
> main 阶段计划：[APP_TO_APP_PERMISSION_MANAGEMENT_IMPLEMENTATION_PHASE_1_PLAN.md](APP_TO_APP_PERMISSION_MANAGEMENT_IMPLEMENTATION_PHASE_1_PLAN.md)
>
> 执行记录：[APP_TO_APP_PERMISSION_MANAGEMENT_IMPLEMENTATION_PHASE_1_EXECUTION.md](APP_TO_APP_PERMISSION_MANAGEMENT_IMPLEMENTATION_PHASE_1_EXECUTION.md)

内容：Resource Scope/Grant、Client Credentials、独立 service auth、两个内部只读接口、双向 clients、配置/文档和自动化测试。

### 后续阶段

写接口、应用 Actor 审计、限流、即时撤销、密钥轮换和生产联调另行规划，不在本阶段提前实现。

## 9. 部署、回滚与安全

部署顺序为 migration → permission-sync → API 启动；`0007` 为扩展式新增表，生产回滚优先使用前向修复，不依赖 downgrade。App Secret、Token、Hash、私钥和 Authorization 内容不得进入日志、审计、URL、普通响应或提交。

## 10. 当前结论

第一阶段的业务代码和默认自动化质量门槛已完成；main 与 JCC 的真实双向 HTTP smoke、真实临时 PostgreSQL migration round-trip 仍是发布前条件。未获得专用资源和明确授权前，不连接共享数据库/Redis，不执行生产迁移、部署或长期凭证注入。
