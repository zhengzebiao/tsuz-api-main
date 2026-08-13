# 权限管理 Permission：第四阶段实施计划

## 1. Context

前三阶段已经完成：

- `Permission` 和 `PermissionEndpoint` 数据结构及迁移；
- `require_permissions()` 严格声明与 FastAPI 路由扫描；
- PostgreSQL 权限同步、advisory lock、admin 授权、Session 撤销、命令和 Seed 集成。

第四阶段负责将权限状态接入管理和运行时鉴权：管理员可以查询权限与 API 绑定、编辑展示信息、禁用和启用权限；登录和刷新只签发当前有效权限；请求鉴权同时检查 JWT Scope 和数据库权限状态。

本阶段不新增数据库迁移，不提供权限手动创建、权限编码编辑、物理删除、API 绑定编辑或角色权限分配接口，也不提前实现第五阶段真实 PostgreSQL/Redis/RS256 生命周期验证脚本。

## 2. 范围与约束

- `Permission.id` 保持整数自增主键；
- `Permission.name` 保持不可编辑的权限编码；
- `resource`、`action` 从 `name` 派生，不重复持久化；
- 权限有效条件统一为 `is_declared=true && is_enabled=true`；
- `is_declared` 和 endpoint 只能由同步服务维护；
- 管理员只可编辑 `display_name` 和 `description`；
- 展示信息更新使用 `version` 乐观锁；
- 启停操作使用行锁，重复请求幂等；
- 禁用保留 endpoint 和全部 `role_permissions`；
- `permission:enable` 不允许被禁用；
- missing 权限不允许启用；
- 禁用时只撤销通过启用角色拥有该权限的 DISTINCT 用户 Session；
- Service 只 flush，API 统一 commit/rollback；
- 有效管理员变更写入安全 AuditEvent；
- 不记录 Token、Authorization Header、Session ID 或凭证。

## 3. 严格 Schema

新增 `app/schemas/admin_permission.py`：

- `AdminPermissionUpdate`：可选 `display_name`、可选 `description`、必填正 `StrictInt version`；
- `AdminPermissionDisableRequest`：可选禁用原因；
- endpoint、列表项、详情、动作和分页响应；
- 所有请求使用 `ConfigDict(extra="forbid")`；
- 展示名去除首尾空白且不能为空；
- 说明去除首尾空白并允许清空；
- 显式 null、越界值、bool version 和特权字段均拒绝；
- 响应只包含权限管理所需安全字段。

## 4. AdminPermissionService

新增 `app/services/admin_permission_service.py`：

### 4.1 查询

- 列表支持分页、keyword、resource、is_declared、is_enabled；
- keyword 不区分大小写搜索 name、display_name 和 description；
- resource 使用完整 `resource:` 前缀匹配；
- endpoint_count 与 role_count 使用相关子查询，避免多表连接导致乘积计数；
- 按 `name ASC, id ASC` 稳定排序；
- 详情按 Method、Path、route_name 稳定返回 endpoint。

### 4.2 编辑

- 校验当前 `version`；
- 只比较并更新展示名和说明；
- 使用 `WHERE id=? AND version=?` 原子更新；
- 并发丢失区分不存在与版本冲突；
- 无变化不递增版本、不写审计、不撤销 Session；
- 成功写 `permission.updated`。

### 4.3 禁用与启用

- 使用 `SELECT ... FOR UPDATE`；
- 禁用 `permission:enable` 返回核心权限保护冲突；
- 禁用写入状态、时间、原因和新版本；
- 通过启用角色查询 DISTINCT 用户并调用 `SessionService.revoke_user_sessions()`；
- 启用只允许 `is_declared=true` 的权限；
- 启用清空禁用元数据，但不恢复旧 Session；
- 重复目标状态操作返回 `changed=false`；
- 有效状态变化写 `permission.disabled` 或 `permission.enabled`；
- 审计与数据库状态共享调用方事务。

固定领域错误：

- `PERMISSION_NOT_FOUND`；
- `PERMISSION_VERSION_CONFLICT`；
- `PROTECTED_PERMISSION_OPERATION`；
- `PERMISSION_NOT_DECLARED`。

## 5. 管理 API

新增 `app/api/admin_permissions.py` 并在 `app/main.py` 注册：

| Method | Path | 权限 | 用途 |
| --- | --- | --- | --- |
| GET | `/admin/permissions` | `permission:read` | 分页查询权限 |
| GET | `/admin/permissions/{permission_id}` | `permission:read` | 查询权限及绑定 |
| PATCH | `/admin/permissions/{permission_id}` | `permission:update` | 编辑展示信息 |
| POST | `/admin/permissions/{permission_id}/disable` | `permission:disable` | 禁用权限 |
| POST | `/admin/permissions/{permission_id}/enable` | `permission:enable` | 启用权限 |

明确不提供 collection POST 或 DELETE。

HTTP 映射：

- 不存在：404；
- 版本冲突、核心保护、missing 启用：409；
- Schema 错误：422；
- 认证失败：401；
- 权限不足：403。

写接口在严格响应模型构造成功后 commit，领域错误和其他异常均 rollback。

## 6. Auth 与 Authorization 接入

修改 `AuthService._get_user_permissions()`：

- 继续只读取启用角色；
- 增加 `Permission.is_declared=true`；
- 增加 `Permission.is_enabled=true`；
- 登录和 refresh 继续复用同一 `_build_claims()`。

修改 `AuthorizationService.require_permissions()`：

1. 完成 Token、blacklist、Session 和用户认证；
2. 校验 JWT Scope 包含全部所需权限；
3. 一次查询所需权限的 declared/enabled 数据库状态；
4. 不存在、disabled 或 missing 均返回 PermissionDeniedError；
5. 保持认证状态错误为 401，授权状态错误为 403。

数据库实时检查只校验当前端点要求的权限状态，不在每个请求中重新计算用户完整 RBAC 集合。

## 7. 路由目录基线

新增五个管理操作和四个权限编码后，真实 `create_app()` 基线变为：

- 25 个不同权限；
- 31 个 Method/Path 绑定；
- 31 个受保护 `/admin` 操作；
- 首次空库同步 25 Permission、31 PermissionEndpoint、25 admin grants。

第二、第三阶段执行记录继续保留当时的 21/26 历史结果，不回写历史。

## 8. 测试计划

### 8.1 Schema

覆盖文本规范化、长度、StrictInt、显式 null、extra、特权字段和响应字段白名单。

### 8.2 Service

覆盖列表筛选和计数、详情绑定、乐观锁、no-op、行锁、核心保护、missing 冲突、启停幂等、关联保留、DISTINCT 用户撤销、禁用角色排除、Redis 写入、审计及事务 rollback。

### 8.3 API

覆盖 OpenAPI/Security、401/403、独立权限、生命周期、404/409/422、安全响应、无创建/删除端点、Session/AuditEvent 和异常 rollback。

### 8.4 Auth

覆盖登录和 refresh 过滤 disabled/missing 权限，Authorization 拒绝数据库不存在、disabled、missing 和混合状态权限，并回归现有认证错误边界。

### 8.5 回归

更新 scanner、sync service 和 opt-in concurrency 的 25/31/25 基线，并执行用户、App、Role、Session、Auth 全量相关回归。

## 9. 验证命令

```bash
pdm run test -- \
  tests/test_permission_management_schemas.py \
  tests/test_admin_permission_service.py \
  tests/test_admin_permissions_api.py \
  tests/test_auth_service.py \
  tests/test_authorization_service.py \
  tests/test_api_dependencies.py \
  tests/test_permission_scanner.py \
  tests/test_permission_sync_service.py \
  tests/test_sync_permissions_command.py \
  tests/test_permission_sync_concurrency.py
pdm run lint
pdm run test
git diff --check
```

真实 PostgreSQL/Redis/RS256 验证保留为显式 opt-in；没有隔离端点时不对共享数据库执行迁移、同步或破坏性验证。

## 10. 验收标准

- 权限查询、详情、编辑、禁用和启用 API 可用；
- 无创建、编码编辑、绑定编辑或删除 API；
- 查询计数和 endpoint 绑定准确；
- 乐观锁、行锁、幂等和核心保护正确；
- 禁用保留关联并撤销正确用户 Session；
- 登录和刷新只签发 declared+enabled 权限；
- 旧 Scope 无法访问数据库 disabled/missing/不存在权限；
- 401/403/404/409/422 边界稳定；
- 有效操作写安全审计，no-op 不写无意义审计；
- 真实应用基线为 25/31/31，同步基线为 25/31/25；
- 定向测试、管理回归、完整测试、lint 和空白检查通过；
- 第五阶段真实生命周期验证未提前实现。
