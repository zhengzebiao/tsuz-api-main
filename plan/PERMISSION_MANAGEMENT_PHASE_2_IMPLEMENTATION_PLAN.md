# 权限管理 Permission：第二阶段实施计划

## 1. Context

根据 [PERMISSION_SYNC_DESIGN.md](../docs/PERMISSION_SYNC_DESIGN.md) 实施权限模块第二阶段“权限声明与路由扫描器”。第一阶段已经扩展 `Permission` 数据模型并建立 `permission_endpoints` 绑定表；第三阶段同步服务需要一个不访问数据库、可确定性重复执行的代码声明扫描结果。

现有 `require_permissions()` 只在请求时把权限元组传给 `AuthorizationService`，依赖函数没有供扫描器读取的元数据，也未在应用加载阶段校验权限编码。第二阶段将字符串声明变成严格、可扫描的配置，并遍历 FastAPI 已注册路由及依赖树，生成权限与 `HTTP Method + Path + route_name` 的绑定目录。

本阶段不写数据库，不实现权限同步、管理 API、Seed 调整或权限状态鉴权。

---

## 2. 范围与约束

- 保留 `require_permissions("resource:action")` 字符串调用方式；
- 不引入 `PermissionSpec`，不机械修改已有路由声明；
- 权限编码符合 `^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$`，最大长度 128；
- 至少传入一个权限，参数必须是字符串，禁止空白、大小写、非法字符、多个冒号和同一声明内重复权限；
- 校验在依赖创建时完成，错误声明不能注册为可用路由依赖；
- 多权限继续使用现有 AND 语义；
- 扫描器只读取 FastAPI 路由和依赖元数据，不解析源码，不连接 PostgreSQL 或 Redis；
- `/admin` 路由默认必须声明权限；有意公开的例外只能通过精确 Method/Path allowlist 放行；
- 扫描结果必须排序、去重并保持确定性。

---

## 3. 权限声明计划

修改 [dependencies.py](../app/api/dependencies.py)：

1. 增加权限编码正则和最大长度常量；
2. 增加 `PermissionDeclarationError` 配置异常；
3. 增加 `validate_permission_names()`，返回不可变的 `tuple[str, ...]`；
4. 为权限依赖定义清晰类型，包含 `required_permissions` 属性；
5. `require_permissions()` 在创建内部依赖前完成校验；
6. 把校验后的元组附加到内部依赖并继续传给 `AuthorizationService`；
7. 保持现有 Bearer Token、401、403 和授权成功行为不变。

错误信息仅包含无效权限编码及配置原因，不包含请求 Token、Header 或数据库信息。

---

## 4. 路由扫描器计划

新增 [permission_scanner.py](../app/services/permission_scanner.py)：

### 4.1 输出结构

使用不可变数据类表示：

- `ScannedPermissionBinding`：`permission_name`、`http_method`、`path`、`route_name`；
- `ScannedRoute`：Method、Path、路由名和该路由聚合后的权限元组；
- `PermissionScanResult`：权限名集合、绑定集合和完整 HTTP 路由清单。

### 4.2 路由遍历

扫描入口接受 `FastAPI` 或 `APIRouter`：

1. 处理直接注册的 `APIRoute`；
2. 递归处理 FastAPI 当前版本的 Included Router；
3. 使用 effective route context 获得 Router include 后的完整路径及合并依赖；
4. 递归遍历每条路由的 `Dependant.dependencies`；
5. 只读取带 `required_permissions` 元数据的依赖；
6. 为多 Method 路由分别生成绑定；
7. 忽略 OpenAPI、文档、WebSocket 和其他非 `APIRoute` 路由。

FastAPI Included Router 的版本兼容细节集中在扫描器私有辅助函数中，不扩散到同步服务或 API。

### 4.3 去重、冲突与覆盖检查

- 相同权限、Method 和 Path 的重复依赖只输出一条绑定；
- 同一路径不同 Method 分别保存；
- 单个 API 多权限分别生成绑定；
- 相同权限/Method/Path 如果出现不同 route name，扫描整体失败，避免数据库复合主键对应不确定名称；
- `/admin` 路由无声明时抛出 `AdminRoutePermissionCoverageError`；
- allowlist 采用精确 `(HTTP_METHOD, path)`，Method 规范化为大写；
- 权限名、绑定和路由清单统一排序，重复扫描结果完全一致。

---

## 5. 测试计划

### 5.1 `require_permissions()`

扩展 [test_api_dependencies.py](../tests/test_api_dependencies.py)：

- 合法单权限和多权限；
- `required_permissions` 为不可变 tuple；
- 无参数、非字符串、空字符串；
- 首尾空白、大写、非法字符；
- 缺少或多个冒号；
- resource/action 非法首字符；
- 超过 128 字符；
- 同一声明重复权限；
- 现有成功、401、403 行为无回归。

### 5.2 扫描器

新增 [test_permission_scanner.py](../tests/test_permission_scanner.py)：

- 单路由单/多权限；
- 多 Method 分离；
- 重复依赖去重；
- 嵌套依赖；
- Router 自身、include_router 和 route 级依赖聚合；
- 完整路径模板和 route name；
- 扫描顺序确定、重复结果一致；
- 公开接口不进入绑定目录；
- 未保护 `/admin` 路由失败；
- 精确 allowlist 放行；
- 冲突 route name 失败；
- 真实 `create_app()` 当前 21 个权限、26 个管理 Method/Path 绑定全部被扫描。

---

## 6. 验证方案

```bash
pdm run test -- tests/test_api_dependencies.py tests/test_permission_scanner.py
pdm run test -- \
  tests/test_admin_apps_api.py \
  tests/test_admin_users_api.py \
  tests/test_admin_user_roles_api.py \
  tests/test_admin_roles_api.py \
  tests/test_authorization_service.py
pdm run lint
pdm run test
git diff --check
```

另外直接使用 `create_app()` 执行扫描，核对：

- 21 个不同权限声明；
- 26 个权限 Method/Path 绑定；
- 26 个受保护管理路由 Method；
- `/health`、`/auth/login`、`/auth/refresh` 没有绑定。

本阶段不执行 Alembic，不访问开发 PostgreSQL/Redis，不写权限表。

---

## 7. 验收标准

- 非法权限声明在应用注册前失败；
- 合法声明提供不可变且有清晰类型的扫描元数据；
- 当前全部管理 API 权限准确扫描；
- Router 和嵌套依赖中的权限不会遗漏；
- 重复绑定正确去重，不同 Method 正确区分；
- 扫描结果确定且重复运行一致；
- 未声明权限的 `/admin` 路由被发现；
- 公开接口不会错误进入权限目录；
- 原有授权、401、403 及多权限 AND 语义保持不变；
- 定向测试、lint 和完整回归通过；
- 未提前实现第三阶段及之后的数据库同步或鉴权变更。
