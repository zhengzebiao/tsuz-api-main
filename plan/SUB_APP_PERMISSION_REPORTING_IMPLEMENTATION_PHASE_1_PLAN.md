# 子应用权限上报与资源统计：第一阶段“权限上报闭环”实现计划

> 状态：实施中
>
> 总实施方案：[SUB_APP_PERMISSION_REPORTING_IMPLEMENTATION_PLAN.md](SUB_APP_PERMISSION_REPORTING_IMPLEMENTATION_PLAN.md)
>
> 阶段执行记录：待创建
>
> 范围：实现主应用接收子应用完整权限快照、admin 角色自动授权和权限目录隔离；不提前实现 JCC 资源统计接口。

## 1. 背景与阶段基准

### 1.1 前置阶段状态

主应用现有 `0007_app_service_authorization`（本阶段迁移前置版本）、App 注册、Service Token、Resource Scope/Grant、独立 Service Auth 和用户角色/权限模型已经落地，依据 [APP_TO_APP_PERMISSION_MANAGEMENT_IMPLEMENTATION_PHASE_1_EXECUTION.md](APP_TO_APP_PERMISSION_MANAGEMENT_IMPLEMENTATION_PHASE_1_EXECUTION.md)。主应用权限同步已经由 `PermissionSyncService` 管理自身路由声明和 `admin` 角色授权。

### 1.2 当前仓库事实

- `Permission.name` 全局唯一，`PermissionSyncService` 原来会读取全部 Permission；必须增加归属筛选，避免子应用权限被误标记 missing。
- `AuditEvent.actor_user_id` 非空，系统 Service Token 上报不伪造管理员 Actor；使用安全结构化日志。
- 既有权限管理和角色权限响应测试要求未新增字段，子应用归属用于内部/管理查询时采用独立 owner 过滤和后续兼容扩展。
- 当前分支工作区在本阶段实施前无用户代码修改；JCC 位于相邻独立仓库，统计接口属于同一总方案的后续阶段。

## 2. 阶段目标与约束

1. 迁移 `permissions.owner_app_id`，历史主应用权限保持 NULL、ID 和角色关联不变；
2. 增加 `PUT /internal/v1/permissions/report`，caller 从验证后的 Service Token `sub` 获取；
3. 完整快照原子同步，新增权限自动关联 admin 角色，重复上报幂等，缺失权限保留记录并标记 missing；
4. 请求严格限制三段式权限编码、数量和字段，冲突整批拒绝；
5. 本阶段不实现 JCC 统计 API、Dashboard 聚合、权限删除、应用 Actor 审计或历史版本。

## 3. 详细设计

### 3.1 Schema 与接口

- `PermissionReportRequest.permissions` 最多 500 项，禁止额外字段和重复 code；
- item 为 `code`、`display_name`、`description`，code 必须匹配 `resource:domain:action` 的小写三段式格式；
- caller 不出现在请求体中；接口依赖 `require_service_scope("main:permission:report")`；
- 成功返回 caller 和 created/restored/marked_missing/admin_grants_added/sessions_revoked/unchanged 摘要；
- 认证失败 401、缺 scope 403、名称冲突 409、数据库/初始化问题 503。

### 3.2 Service 与事务

`PermissionReportService` 在单一调用方 Session 中：

1. 取得 caller 行锁，并在 PostgreSQL 取得 caller 维度 advisory lock；
2. 锁定 admin 角色；
3. 在任何写入前检查全局名称归属冲突；
4. 创建或恢复 caller 所有权限，空列表/消失项设置 `is_declared=false` 和 `missing_at`；
5. flush 后幂等补齐 `role_permissions`；
6. 新 admin 授权时对 admin 用户调用现有 `SessionService` 撤销会话；
7. flush，由 API 层 commit，异常 rollback。

上报只初始化新权限的展示字段；不覆盖管理员编辑的展示文本和禁用状态。系统上报不写虚假 `AuditEvent`。

### 3.3 主应用权限同步兼容

`PermissionSyncService` 的全部权限和 endpoint binding 查询仅限定 `owner_app_id IS NULL`。主应用 scanner 继续只同步自身路由；子应用权限不被标记 missing、不创建主应用 endpoint binding、不被主应用同步覆盖。

### 3.4 迁移

新增 Alembic revision `0008_permission_reporting`（文件名 `0008_sub_app_permission_reporting.py`），在 `permissions` 增加可空 `owner_app_id` 外键至 `apps.app_id`（`ON DELETE RESTRICT`），并增加 owner 与声明状态索引。升级不回填历史权限；生产回滚优先前向修复，downgrade 只在隔离测试使用。

## 4. 实施步骤

1. 增加模型字段和 `0008` migration；
2. 隔离主应用 PermissionSync 查询；
3. 增加报告 Schema、Service、内部路由和 app 注册；
4. 增加主应用权限目录 owner 查询参数和三段式展示兼容；
5. 增加 Service/API/Schema/回滚/幂等测试；
6. 执行定向 Ruff、pytest、全量 pytest、lock/diff 检查；
7. 根据真实结果更新总方案、本计划和执行记录。

## 5. 测试与验收计划

- Schema：空列表、501 项、重复、额外字段、二段/四段式 code、空白/大小写/长度；
- Service：首次创建、重复上报、恢复、空列表 missing、保留 role association、名称冲突整批无写入、admin grant 幂等；
- API：缺 token、错误 audience/issuer、缺 scope、禁用/不存在 caller、admin 未初始化、固定错误和 Request ID；
- 回归：主应用 permission-sync 不处理 `owner_app_id` 非空记录；既有权限/角色/登录路径不回归；
- 质量命令：`pdm run ruff check app tests/test_permission_report_service.py tests/test_internal_permission_report_api.py alembic/versions/0008_sub_app_permission_reporting.py`、定向和全量 pytest、`pdm lock --check`、`git diff --check`；
- 真实 PostgreSQL migration round-trip 与并发上报需要隔离资源和显式授权，普通阶段不伪造为通过。

## 6. 阶段验收标准

| 编号 | 验收标准 | 实现位置 | 状态 |
| --- | --- | --- | --- |
| AC-1-01 | 上报 caller 从 Service Token 获取，不信任请求体身份 | `app/api/internal_permissions.py`, `app/deps/service_auth.py` | 计划中 |
| AC-1-02 | 完整权限快照新增/恢复/missing 幂等且不物理删除 | `app/services/permission_report_service.py` | 计划中 |
| AC-1-03 | 新权限自动加入 admin 角色，重复关联不重复写入 | `app/services/permission_report_service.py` | 计划中 |
| AC-1-04 | 主应用同步只处理 owner 为空的权限 | `app/services/permission_sync_service.py` | 计划中 |
| AC-1-05 | 编码/数量/冲突和事务错误 fail closed | `app/schemas/internal_permission_report.py`, API/Service | 计划中 |
| AC-1-06 | 迁移保留历史权限 ID、字段和角色关系 | `alembic/versions/0008_sub_app_permission_reporting.py` | 待隔离迁移验证 |
| AC-1-07 | 定向、全量测试及静态检查真实记录 | tests and execution record | 计划中 |

## 7. 风险与回滚

- `owner_app_id` migration 失败：停止发布，保留旧应用；生产不自动 downgrade；
- 名称冲突：整批拒绝，调用方修复命名空间后重试；
- Redis 撤销失败：数据库事务不提交，重试完整快照；
- 旧 Worker 与新字段并存：先发布兼容版本，再开放报告调用；
- 真实 PostgreSQL 和跨服务 smoke 只使用随机隔离资源。

## 8. 交付物

- `0008` migration、Permission owner 字段、报告 Schema/Service/API；
- 主应用权限同步隔离和管理查询兼容；
- 定向测试与回归测试；
- 更新总方案和本阶段执行记录。
