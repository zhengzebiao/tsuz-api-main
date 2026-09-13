# 子应用权限上报与资源统计内部接口实施方案

> 状态：实施中（权限上报与 JCC 统计代码已落地，阶段文档和真实隔离验证待补齐）
>
> 本方案基于主应用与 JCC 当前 FastAPI、SQLAlchemy、PostgreSQL、Redis、RS256 Service Token 和 JCC 结构化快照架构。
>
> 相关文档：[应用间权限管理总方案](APP_TO_APP_PERMISSION_MANAGEMENT_IMPLEMENTATION_PLAN.md)、[第一阶段实现计划](SUB_APP_PERMISSION_REPORTING_IMPLEMENTATION_PHASE_1_PLAN.md)。

## 1. 已确认业务配置与关键决策

| 项目 | 决策 | 状态/来源 |
| --- | --- | --- |
| 权限上报 | 子应用用 Service Token `PUT /internal/v1/permissions/report` 提交完整权限快照 | 已确认；本次需求 |
| caller 身份 | 取已验证 token `sub`，不接受请求体身份 | 已确认；安全约束 |
| 上报授权 | `main:permission:report` | 已确认；本期实现 |
| 权限归属 | `permissions.owner_app_id`，主应用权限为 NULL，名称全局唯一 | 已确认；本期实现 |
| admin 角色 | 新上报权限自动幂等关联到 `admin` | 已确认；本次需求 |
| 资源统计 | JCC 提供 `GET /internal/v1/resource-statistics`，统计当前结构化 snapshot | 已确认；本次需求 |
| 统计授权 | `jcc:stats:read` | 已确认；本期实现 |
| Dashboard | 本期不实现主应用 UI 或多子应用聚合层 | 范围约束 |

## 2. 背景、现状与目标

主应用已有路由权限扫描/同步和应用间 Service Token，但扫描器无法识别子应用权限；JCC 已有 current snapshot 与 `/jcc/*` 用户 API，但没有向主应用提供资源统计的内部只读契约。

本方案目标：

1. 子应用完整上报权限后，主应用可在权限管理和角色权限分配中查询；
2. 新权限自动加入 admin 角色，重复上报安全幂等；
3. 子应用下线某权限时标记 missing，保留权限 ID 和角色关联；
4. 主应用自身 permission-sync 不污染子应用权限；
5. 主应用/JCC 可以通过固定 Service Token scope 查询 JCC 当前资源数量；
6. 保持现有用户认证、角色、App 管理、JCC 用户 API 和 records 内部 API 兼容。

## 3. 核心流程与契约

```text
JCC/子应用部署
  → 使用自身 App 凭证向 main 申请 Service Token
  → PUT /internal/v1/permissions/report
  → main 从 token.sub 确定 caller
  → 校验完整列表、命名和冲突
  → 单事务写入 owner、声明状态、admin role_permissions、Session 撤销

main JccClient
  → 申请 jcc:stats:read Service Token
  → GET JCC /internal/v1/resource-statistics
  → JCC 锁定 current snapshot
  → 同一 snapshot 统计结构化资源
```

### 3.1 权限报告

请求：

```http
PUT /internal/v1/permissions/report
Authorization: Bearer <service-token>
Content-Type: application/json
```

```json
{
  "permissions": [
    {
      "code": "jcc:data:read",
      "display_name": "JCC data read",
      "description": "Read published JCC data"
    }
  ]
}
```

`permissions` 最多 500 项，严格禁止额外字段和重复 code；code 为小写三段式。空列表表示 caller 当前没有声明权限。成功响应仅返回安全计数：`caller_app_id`、`created`、`restored`、`marked_missing`、`admin_grants_added`、`sessions_revoked`、`unchanged`。

错误：无效 token 401，缺 scope 403，名称冲突 409 `PERMISSION_NAME_CONFLICT`，caller 禁用/不存在、admin 缺失或数据库异常 503 `PERMISSION_REPORT_UNAVAILABLE`。

### 3.2 资源统计

```http
GET /internal/v1/resource-statistics
Authorization: Bearer <service-token with jcc:stats:read>
```

返回 `snapshot` 元数据及固定顺序的 `items`：`heroes`、`traits`、`trait_tiers`、`hero_traits`、`equipment`、`equipment_recipes`、`augments`、`adventures`、`galaxies`。每项只有 `resource` 和非负 `count`。无 current snapshot 或查询异常统一返回 503 `JCC_DATA_UNAVAILABLE`，不回退 raw。

## 4. 数据模型、迁移与一致性

主应用 Alembic revision `0008_permission_reporting`：

```text
permissions.owner_app_id: String(64), nullable,
  FK apps.app_id ON DELETE RESTRICT
index(owner_app_id)
index(owner_app_id, is_declared)
```

历史权限保持 NULL。`Permission.name` 继续全局唯一，以避免用户 JWT 的字符串 scope 在应用之间产生歧义。报告 service 以 caller 维度 advisory lock（PostgreSQL）和 caller 行锁串行化同一应用报告；校验全部通过后创建/恢复/标记 missing，并在同一事务幂等增加 admin 关联。新增 admin 授权后撤销 admin 用户活跃 Session，Redis 失败时不提交数据库。

主应用 `PermissionSyncService` 只查询 `owner_app_id IS NULL` 的记录和 endpoint bindings。子应用权限不生成主应用 endpoint binding，不被主应用同步标记 missing。

JCC 统计 repository 先取得 current snapshot，再为所有模型添加同一 snapshot 条件；trait tiers 通过 trait snapshot 关联计数，确保不跨版本混合。

## 5. 模块与服务拆分

### 主应用

- [app/services/permission_report_service.py](../app/services/permission_report_service.py)：完整报告同步、冲突、admin 授权、Session 撤销和安全摘要日志；
- [app/api/internal_permissions.py](../app/api/internal_permissions.py)：Service scope、请求校验、错误映射和事务提交；
- [app/schemas/internal_permission_report.py](../app/schemas/internal_permission_report.py)：严格报告契约；
- [app/clients/jcc_client.py](../app/clients/jcc_client.py)：JCC records 和 stats 的按 scope 内存 token cache。

### JCC

- [app/jcc_data/repository.py](../../tsuz-api-jcc/app/jcc_data/repository.py)：current snapshot 固定资源计数；
- [app/api/internal.py](../../tsuz-api-jcc/app/api/internal.py)、[app/schemas/internal.py](../../tsuz-api-jcc/app/schemas/internal.py)：统计只读内部接口；
- [app/clients/main_client.py](../../tsuz-api-jcc/app/clients/main_client.py)：按 scope 获取 main Service Token、上报权限；
- [scripts/report_permissions.py](../../tsuz-api-jcc/scripts/report_permissions.py)：部署阶段显式上报 JCC 权限。

## 6. 安全、兼容与运维

- token issuer、audience、`token_use`、时间、scope 严格校验；caller 不由请求体传入；
- 权限报告整批校验后写入，名称全局唯一；不接受任意权限 ID、owner 或数据库字段；
- 报告不伪造系统 AuditEvent Actor，日志只记录非敏感 caller、Request ID 和计数；
- admin 新授权撤销活跃 Session；权限恢复不自动复活旧 Session；
- Secret、Token、Hash、密码和完整 Authorization 不进入日志、URL、响应或文档；
- 统计接口只读 current snapshot，不提供 raw 路径、source attributes 或任意 SQL；
- 主应用 permission-sync、旧 `/internal/v1/records`、用户 `/auth/*` 和 JCC `/jcc/*` 保持原语义；
- 生产顺序：migration → main permission-sync → 创建/Grant scope → 子应用报告 → 统计 smoke → 滚动启动；真实生产凭证和迁移须由发布窗口执行。

## 7. 分阶段实施顺序

### 第一阶段：主应用权限上报闭环

> 状态：实施中
>
> 阶段计划：[SUB_APP_PERMISSION_REPORTING_IMPLEMENTATION_PHASE_1_PLAN.md](SUB_APP_PERMISSION_REPORTING_IMPLEMENTATION_PHASE_1_PLAN.md)
>
> 执行记录：待创建

实现 owner 字段与迁移、报告 Schema/Service/API、admin 幂等授权、主应用同步隔离和测试。JCC 统计不属于本阶段。

验收：完整上报/空列表/missing/恢复/冲突/禁用 caller/重复调用和事务回滚正确；新权限可通过主应用权限与角色 API 查询及分配。

### 第二阶段：JCC 资源统计与子应用 Client

> 状态：实施中
>
> 阶段计划：待创建
>
> 执行记录：待创建

实现同一 current snapshot 统计接口、JCC/Main Client scope 隔离、JCC 权限报告命令及定向测试。

验收：正确 Service Token 可读统计；错误 token/scope 失败；无 current 返回 503；报告和统计均不泄露凭证或 raw。

### 第三阶段：隔离环境验证与上线文档

> 状态：未开始
>
> 阶段计划：待创建
>
> 执行记录：待创建

使用随机隔离 PostgreSQL、Redis、端口和临时密钥执行 `0007 → 0008 → 0007 → head`、两服务真实 HTTP、权限/统计链路、日志扫描和清理。生产部署、长期 Secret 注入和公网暴露不在普通 CI 执行。

## 8. 测试与验收

主应用：

- `tests/test_permission_report_service.py`：创建、幂等、missing、冲突、role association；
- `tests/test_internal_permission_report_api.py`：Service Auth、请求 Schema、路由和固定错误；
- 现有 permission/role/auth 全量回归；
- JCC `tests/test_jcc_client.py`：records/stats scope cache 和 Bearer；

JCC：

- `tests/test_internal_statistics_api.py`：current snapshot 计数、scope 和安全响应；
- `tests/test_internal_api.py`、`tests/test_main_client.py`、`tests/test_jcc_api.py` 回归。

已执行质量结果：main `327 passed, 15 skipped`；JCC `102 passed`；两端本阶段新增/修改 Python 文件 Ruff 通过；两端 lock/diff check 通过。真实 PostgreSQL migration round-trip、跨进程 HTTP smoke、生产迁移和长期凭证注入未执行。

## 9. 风险、回滚与待确认项

| 风险 | 影响 | 缓解/回滚 |
| --- | --- | --- |
| 子应用权限未使用全限定命名 | 名称冲突或 scope 歧义 | Schema 拒绝非三段式；修复后重报，不物理删除历史记录 |
| 新旧 Worker 并存 | 旧同步逻辑误标记子应用权限 | 先迁移并部署兼容版本，再启用报告；必要时停止报告入口 |
| Redis 撤销失败 | 会话状态与数据库短暂不一致 | DB 不提交；重新报告；鉴权仍检查数据库权限状态 |
| JCC 没有 current | 主应用统计不可用 | 固定 503，不伪造 0；同步成功后重试 |
| Token 撤销非即时 | Grant 撤销后旧 token 最多存活既有 TTL | 沿用现有 300 秒 Service Token 约束；敏感操作另行缩短或即时撤销设计 |

当前没有阻塞实现的业务待确认项。若要求同一权限编码跨应用重复、统计历史趋势或 Dashboard 聚合，应另立方案，因为会改变唯一约束、API 形态或数据状态。

## 10. 完成标准

```text
子应用 PUT 完整权限列表
  → main owner_app_id + permission 状态 + admin role_permissions 原子更新
  → 主应用权限/角色接口可见，用户重新登录/刷新获得权限
  → main JccClient 请求 JCC stats
  → JCC current snapshot 固定计数响应
```

所有阶段验收、迁移、跨服务 smoke、日志安全和文档互链均有真实证据后，方案才标记已完成；生产和外部验证未执行前继续保持待执行状态。
