# 权限管理 Permission：第二阶段开发执行记录

## 1. 开发范围

本阶段根据 [PERMISSION_SYNC_DESIGN.md](../docs/PERMISSION_SYNC_DESIGN.md) 和 [PERMISSION_MANAGEMENT_PHASE_2_IMPLEMENTATION_PLAN.md](PERMISSION_MANAGEMENT_PHASE_2_IMPLEMENTATION_PLAN.md) 完成“权限声明与路由扫描器”：

1. 为 `require_permissions()` 增加严格权限编码校验；
2. 为权限依赖增加有类型的可扫描元数据；
3. 实现 FastAPI 路由及递归依赖树扫描；
4. 实现权限绑定排序、去重和冲突检测；
5. 实现 `/admin` 路由权限覆盖检查及精确 allowlist；
6. 增加声明和扫描器测试；
7. 对真实 `create_app()` 扫描结果建立基线断言；
8. 执行定向测试、权限相关回归、完整 lint 和完整测试。

本阶段未实现：

- `PermissionSyncService` 和同步命令；
- PostgreSQL advisory lock；
- `permissions`、`permission_endpoints` 或角色关联写入；
- Seed 和本地部署流程调整；
- 权限管理 Schema、Service 和 API；
- `AuthService` 权限状态过滤；
- `AuthorizationService` 数据库实时状态检查；
- Session 撤销或权限管理审计；
- `PermissionSpec`。

---

## 2. 严格权限声明

修改：

```text
app/api/dependencies.py
```

新增规则：

- 至少一个权限；
- 每项必须是字符串；
- 原始值不能包含首尾空白；
- 不能为空；
- 最大长度 128；
- 必须符合：

  ```regex
  ^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$
  ```

- 同一 `require_permissions()` 调用内不能重复。

校验失败抛出 `PermissionDeclarationError`。校验在内部依赖创建前执行，因此错误声明在应用模块导入或路由注册时直接失败，而不是推迟到用户请求时才发现。

合法权限保留调用顺序并转换为不可变 `tuple[str, ...]`。通过 `PermissionDependency` Protocol 明确依赖函数具备：

```text
required_permissions: tuple[str, ...]
```

请求时仍将同一元组传给 `AuthorizationService.require_permissions()`。原有 Bearer Token 读取、认证失败 401、权限不足 403 和多权限 AND 语义没有改变。

---

## 3. FastAPI 路由扫描器

新增：

```text
app/services/permission_scanner.py
```

公开输出类型：

- `ScannedPermissionBinding`；
- `ScannedRoute`；
- `PermissionScanResult`。

扫描结果包含：

```text
permission_names
bindings(permission_name, http_method, path, route_name)
routes(http_method, path, route_name, required_permissions)
```

### 3.1 遍历行为

扫描器接受 `FastAPI` 或 `APIRouter`：

1. 处理直接注册的 `APIRoute`；
2. 对当前 FastAPI 版本的 Included Router 使用 effective route context；
3. 获取 Router include 后的完整 Path 和合并后的依赖；
4. 递归遍历 `Dependant.dependencies`；
5. 只读取依赖函数的 `required_permissions` 元数据；
6. 不执行依赖，不解析源码；
7. 非 `APIRoute` 的 OpenAPI、文档和其他 Starlette 路由不会进入权限扫描。

这覆盖：

- 端点函数参数中的 `Depends(require_permissions(...))`；
- route decorator 的 dependencies；
- APIRouter 自身 dependencies；
- `include_router(..., dependencies=...)`；
- 嵌套 Router；
- 普通依赖内部继续嵌套的权限依赖。

### 3.2 确定性与冲突规则

- HTTP Method 转为大写；
- FastAPI 模板路径原样保存，例如 `/admin/apps/{app_id}`；
- 权限名、绑定和路由清单统一排序；
- 相同权限、Method 和 Path 的重复声明去重；
- 不同权限或不同 HTTP Method 分别保留；
- 相同权限/Method/Path 对应不同 route name 时抛出 `PermissionScanError`，避免后续数据库复合主键对应不确定 route name；
- 依赖元数据不是不可变 tuple 或内容非法时扫描整体失败。

---

## 4. `/admin` 权限覆盖检查

默认规则：Path 等于 `/admin` 或以 `/admin/` 开头的每个 HTTP Method 都必须至少扫描到一个权限。

未声明时抛出：

```text
AdminRoutePermissionCoverageError
```

可通过：

```text
admin_route_allowlist={("GET", "/admin/status")}
```

为有意公开的管理例外放行。allowlist 必须是精确 `(Method, Path)`，Method 规范化为大写；不会因为路径前缀相似而大范围跳过检查。

`/health`、`/auth/login`、`/auth/refresh` 等非管理公开接口允许没有权限声明，并且不会产生权限绑定。

---

## 5. 测试实现

### 5.1 声明测试

扩展：

```text
tests/test_api_dependencies.py
```

新增覆盖：

- 合法多权限元数据和不可变 tuple；
- 无参数；
- 非字符串；
- 空字符串；
- 首尾空白；
- 大写字符；
- 缺少冒号或多个冒号；
- 非法连接符；
- resource/action 以数字开头；
- 超过 128 字符；
- 重复权限。

保留并继续通过既有：授权成功、无凭证 401、认证失败 401、权限不足 403。

### 5.2 扫描器测试

新增：

```text
tests/test_permission_scanner.py
```

共 6 个测试，覆盖：

1. route 级单/多权限、嵌套依赖、重复依赖去重和多 Method；
2. Router dependencies、嵌套 include dependencies 和 route dependencies 聚合；
3. 排序确定、重复扫描一致、公开路由无绑定；
4. 未保护 `/admin` 路由失败及 allowlist 放行；
5. 同一绑定 route name 冲突失败；
6. 真实主应用权限目录和全部管理路由覆盖。

---

## 6. 真实应用扫描结果

使用 `create_app()` 实例直接运行扫描器，结果：

```text
permissions=21
bindings=26
admin_routes=26
public_bindings=[]
```

扫描到 21 个不同权限：

```text
app:create
app:disable
app:enable
app:read
app:regenerate_secret
app:update
role:create
role:disable
role:enable
role:read
role:update
user:assign_roles
user:blacklist
user:create
user:disable
user:enable
user:force_logout
user:read
user:recover
user:reset_password
user:update
```

26 个绑定对应当前 26 个 `/admin` HTTP Method/Path 操作。`/health`、`/auth/login`、`/auth/refresh` 均未进入绑定集合。

该结果与设计文档中第二阶段基线一致：App 6 个、User 10 个、Role 5 个，共 21 个不同权限。Seed 中历史 `user:write` 不由当前路由声明，因此本阶段扫描结果不包含它；第三阶段首次同步前仍需按设计审查其 missing 变化。

---

## 7. 验证结果

### 7.1 声明和扫描器定向测试

执行：

```bash
pdm run test -- tests/test_api_dependencies.py tests/test_permission_scanner.py
```

结果：

```text
25 passed, 1 warning
```

### 7.2 权限相关 API 和服务回归

执行：

```bash
pdm run test -- \
  tests/test_admin_apps_api.py \
  tests/test_admin_users_api.py \
  tests/test_admin_user_roles_api.py \
  tests/test_admin_roles_api.py \
  tests/test_authorization_service.py
```

结果：

```text
30 passed, 1 warning
```

确认 App、User、用户角色和 Role 管理接口原有权限调用、401/403 边界及 AuthorizationService AND 语义无回归。

### 7.3 静态检查

执行：

```bash
pdm run lint
git diff --check
```

结果：

```text
All checks passed!
```

`git diff --check` 无输出，通过。

### 7.4 完整回归

执行：

```bash
pdm run test
```

结果：

```text
187 passed, 9 skipped, 1 warning
```

9 个 skipped 仍是需要显式环境变量开启的真实 PostgreSQL/Redis 集成测试，不是失败；本阶段为纯声明和路由扫描，不新增外部基础设施测试。

### 7.5 警告说明

所有测试唯一警告仍为项目既有：

```text
StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated
```

与本阶段权限声明和扫描器实现无关，本阶段未升级依赖。

---

## 8. 最终文件变更

修改：

```text
app/api/dependencies.py
tests/test_api_dependencies.py
```

新增：

```text
app/services/permission_scanner.py
tests/test_permission_scanner.py
plan/PERMISSION_MANAGEMENT_PHASE_2_IMPLEMENTATION_PLAN.md
plan/PERMISSION_MANAGEMENT_PHASE_2_EXECUTION.md
```

未修改现有 App、User、Role 路由中的权限参数。

---

## 9. 第二阶段验收结论

第二阶段“权限声明与路由扫描器”已完成：

- 权限编码在路由注册前严格校验；
- 合法依赖暴露不可变、有类型的 `required_permissions` 元数据；
- 原有请求鉴权和错误映射保持不变；
- 直接路由、Included Router、Router 级和嵌套依赖均可扫描；
- 权限绑定按 Method/Path 聚合、去重和稳定排序；
- 冲突声明使扫描整体失败；
- `/admin` 路由权限覆盖可自动检查；
- 当前 21 个不同权限、26 个管理绑定均准确扫描；
- 公开接口未错误进入权限目录；
- 定向测试、权限回归、lint、完整回归和空白检查全部通过；
- 未提前实现第三阶段数据库同步、Seed 集成或后续管理 API/鉴权变化。
