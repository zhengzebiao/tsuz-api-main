# 权限管理 Permission：第四阶段开发执行记录

## 1. 开发范围

本阶段根据 `plan/PERMISSION_SYNC_DESIGN.md` 完成“管理 API 与鉴权接入”：

1. 新增严格 Permission 管理 Schema；
2. 实现权限列表、详情、展示信息编辑、禁用和启用 Service；
3. 新增五个 `/admin/permissions` 管理操作并注册路由；
4. 增加 `permission:read/update/disable/enable` 声明；
5. 实现核心 `permission:enable` 禁用保护；
6. missing 权限启用保护；
7. 接入管理员审计和 Session 撤销；
8. 登录和刷新 Scope 过滤权限声明/启用状态；
9. 请求鉴权实时校验数据库权限状态；
10. 更新真实路由扫描和同步基线；
11. 增加 Schema、Service、API、Auth 和 Authorization 测试；
12. 执行定向测试、管理回归、完整测试、lint 和空白检查。

本阶段未实现：

- 权限后台创建；
- 权限编码或 API 绑定编辑；
- 权限删除；
- 角色权限分配 API；
- 新数据库迁移；
- 第五阶段真实 PostgreSQL/Redis/RS256 生命周期验证脚本；
- 自动生产部署同步。

---

## 2. 严格 Schema

新增：

```text
app/schemas/admin_permission.py
```

实现：

- `AdminPermissionUpdate`；
- `AdminPermissionDisableRequest`；
- `AdminPermissionEndpointResponse`；
- `AdminPermissionResponse`；
- `AdminPermissionDetailResponse`；
- `AdminPermissionActionResponse`；
- `AdminPermissionListResponse`。

实际规则：

- 请求 Schema 全部 `extra="forbid"`；
- `display_name` 去除首尾空白且不能为空；
- `description` 去除首尾空白并允许清空；
- `version` 使用正 `StrictInt`，拒绝 bool；
- 显式 null、越界字段、name、状态、时间和 endpoint 注入均被拒绝；
- 响应仅包含权限管理安全字段。

---

## 3. AdminPermissionService

新增：

```text
app/services/admin_permission_service.py
```

### 3.1 查询

- 支持分页、keyword、resource、is_declared、is_enabled；
- keyword 搜索 name、display_name、description；
- resource 使用严格小写 resource 编码和 `resource:` 前缀；
- endpoint_count 和 role_count 使用独立相关子查询；
- 按 name、id 稳定排序；
- 详情按 Method、Path、route_name 返回完整 endpoint 快照。

### 3.2 编辑

- 只允许 display_name 和 description；
- 先校验 version，再计算实际变化；
- 使用条件 UPDATE 原子递增 version；
- 并发失败区分不存在和版本冲突；
- no-op 不增加版本、不撤销 Session、不写审计；
- 成功写 `permission.updated`。

### 3.3 禁用和启用

- 使用 `SELECT ... FOR UPDATE`；
- `permission:enable` 不允许被禁用；
- missing 权限不允许启用；
- 重复目标状态操作幂等；
- 禁用保留 endpoint 和全部 role_permissions；
- 禁用只查询启用角色，并对 DISTINCT 用户调用 SessionService；
- 启用不恢复旧 Session；
- 有效变化写 `permission.disabled` 或 `permission.enabled`；
- 审计、权限状态和数据库 Session 状态共享 API 调用事务。

固定领域错误：

```text
PERMISSION_NOT_FOUND
PERMISSION_VERSION_CONFLICT
PROTECTED_PERMISSION_OPERATION
PERMISSION_NOT_DECLARED
```

---

## 4. 管理 API

新增：

```text
app/api/admin_permissions.py
```

修改：

```text
app/main.py
```

实际路由：

| Method | Path | 权限 |
| --- | --- | --- |
| GET | `/admin/permissions` | `permission:read` |
| GET | `/admin/permissions/{permission_id}` | `permission:read` |
| PATCH | `/admin/permissions/{permission_id}` | `permission:update` |
| POST | `/admin/permissions/{permission_id}/disable` | `permission:disable` |
| POST | `/admin/permissions/{permission_id}/enable` | `permission:enable` |

未注册 collection POST 和 DELETE。

API 层：

- 显式构造 resource/action、计数和 endpoint 响应；
- 领域错误映射到 404/409；
- Pydantic 错误保持 422；
- 认证和授权通过既有依赖保持 401/403；
- 写操作在响应构造成功后 commit；
- 领域错误和其他异常均 rollback。

---

## 5. 登录和请求鉴权

修改：

```text
app/services/auth_service.py
app/services/authorization_service.py
```

`AuthService._get_user_permissions()` 现在要求：

```text
Role.is_enabled=true
Permission.is_declared=true
Permission.is_enabled=true
```

登录和 refresh 继续使用同一个 `_build_claims()`，确保 Scope 过滤一致。

`AuthorizationService.require_permissions()` 现在：

1. 完成 JWT、blacklist、Session 和用户状态认证；
2. 校验 JWT Scope；
3. 一次查询全部 required permission；
4. 要求每项数据库权限存在、declared 且 enabled；
5. 任一状态不满足返回 PermissionDeniedError。

因此旧 Token 即使包含 disabled、missing 或数据库不存在的编码也不能通过。认证状态错误仍为 401，权限状态错误为 403。

---

## 6. 路由目录和同步基线

新增 Permission 管理路由后，真实 `create_app()` 扫描结果为：

```text
permissions=25
bindings=31
protected_admin_operations=31
```

空库首次同步基线相应更新为：

```text
permissions=25
permission_endpoints=31
admin_grants=25
```

更新测试：

```text
tests/test_permission_scanner.py
tests/test_permission_sync_service.py
tests/test_permission_sync_concurrency.py
```

第二、第三阶段执行记录仍保留当时真实 21/26 历史结果，没有被回写。

---

## 7. 测试实现

新增：

```text
tests/test_permission_management_schemas.py
tests/test_admin_permission_service.py
tests/test_admin_permissions_api.py
```

扩展：

```text
tests/test_auth_service.py
tests/test_authorization_service.py
tests/test_permission_scanner.py
tests/test_permission_sync_service.py
tests/test_permission_sync_concurrency.py
```

覆盖：

- Schema 规范化、边界、严格版本和字段白名单；
- 列表筛选、计数、详情 endpoint 和稳定排序；
- 编辑乐观锁、并发冲突和 no-op；
- 启停行锁、幂等、核心保护和 missing 冲突；
- 启用角色 DISTINCT 用户 Session 撤销；
- disabled 角色用户排除；
- 审计安全字段和事务 rollback；
- API 路由、OpenAPI Security、401/403、404/409/422；
- 不存在创建和删除 API；
- 登录和 refresh 排除 disabled/missing 权限；
- Authorization 拒绝不存在、disabled、missing 和混合状态权限；
- 25/31/31 scanner 及 25/31/25 sync 基线。

---

## 8. 验证结果

### 8.1 Schema 与 Service 定向测试

执行：

```bash
pdm run test -- \
  tests/test_permission_management_schemas.py \
  tests/test_admin_permission_service.py
```

结果：

```text
14 passed, 1 warning
```

### 8.2 API 定向测试

执行：

```bash
pdm run test -- tests/test_admin_permissions_api.py
```

结果：

```text
5 passed, 1 warning
```

### 8.3 Auth 与 Authorization 定向测试

执行：

```bash
pdm run test -- \
  tests/test_auth_service.py \
  tests/test_authorization_service.py \
  tests/test_api_dependencies.py
```

结果：

```text
47 passed, 1 warning
```

### 8.4 第四阶段权限定向套件

执行：

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
```

结果：

```text
88 passed, 1 skipped, 1 warning
```

跳过项为显式 opt-in 的真实 PostgreSQL advisory-lock concurrency 测试。

### 8.5 用户、App、Role、Session 回归

执行：

```bash
pdm run test -- \
  tests/test_admin_users_api.py \
  tests/test_admin_user_roles_api.py \
  tests/test_admin_apps_api.py \
  tests/test_admin_roles_api.py \
  tests/test_admin_user_service.py \
  tests/test_admin_app_service.py \
  tests/test_admin_role_service.py \
  tests/test_user_session_revocation.py \
  tests/test_auth_api.py
```

结果：

```text
76 passed, 1 warning
```

### 8.6 完整测试

执行：

```bash
pdm run test
```

结果：

```text
228 passed, 10 skipped, 1 warning
```

10 个 skipped 是现有显式环境变量控制的真实 PostgreSQL/Redis/并发集成测试，不是失败。

### 8.7 静态和空白检查

执行：

```bash
pdm run lint
git diff --check
```

结果：

```text
All checks passed!
```

`git diff --check` 无输出。

唯一警告仍为项目既有：

```text
StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated
```

本阶段未使用共享数据库执行迁移或同步写入，也未声称真实 PostgreSQL/Redis/RS256 生命周期验证通过；该范围属于第五阶段。

---

## 9. 最终文件变更

新增：

```text
app/schemas/admin_permission.py
app/services/admin_permission_service.py
app/api/admin_permissions.py
tests/test_permission_management_schemas.py
tests/test_admin_permission_service.py
tests/test_admin_permissions_api.py
plan/PERMISSION_MANAGEMENT_PHASE_4_IMPLEMENTATION_PLAN.md
plan/PERMISSION_MANAGEMENT_PHASE_4_EXECUTION.md
```

修改：

```text
app/main.py
app/services/auth_service.py
app/services/authorization_service.py
tests/test_auth_service.py
tests/test_authorization_service.py
tests/test_permission_scanner.py
tests/test_permission_sync_service.py
tests/test_permission_sync_concurrency.py
```

用户现有文档移动：

```text
docs/PERMISSION_SYNC_DESIGN.md
  → plan/PERMISSION_SYNC_DESIGN.md
```

实现过程没有恢复或覆盖该移动。

---

## 10. 第四阶段验收结论

第四阶段代码实现已完成：

- 严格 Schema、Service 和五个管理 API 已实现；
- 不存在后台权限创建、编码修改、绑定编辑或删除接口；
- 权限查询计数和 endpoint 详情可用；
- 乐观锁、行锁、幂等、核心保护和 missing 冲突已实现；
- 禁用权限保留关联并撤销正确用户 Session；
- 登录和刷新只签发 declared+enabled 权限；
- 请求时数据库实时状态检查阻止旧 Scope 使用失效权限；
- 管理员有效变更写入安全审计并与业务事务一致；
- 真实应用目录为 25/31/31，同步基线为 25/31/25；
- 定向、管理回归、完整测试、lint 和空白检查通过；
- 第五阶段真实基础设施生命周期验证未提前实现或虚报。
