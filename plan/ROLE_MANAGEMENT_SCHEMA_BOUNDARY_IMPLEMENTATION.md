# 角色管理 Role：第二阶段严格 Schema 与领域边界开发记录

## 1. 开发范围

本阶段根据 [ROLE_MANAGEMENT_IMPLEMENTATION_PLAN.md](ROLE_MANAGEMENT_IMPLEMENTATION_PLAN.md) 和 [ROLE_MANAGEMENT_PHASE_2_EXECUTION_PLAN.md](ROLE_MANAGEMENT_PHASE_2_EXECUTION_PLAN.md)，在第一阶段角色数据层和 `0004_role_management` 迁移的基础上，仅实现严格 Schema 与领域边界：

1. 新增角色创建、编辑、禁用请求 Schema；
2. 新增角色完整、精简、分页和动作响应 Schema；
3. 新增用户角色集合整体替换请求和响应 Schema；
4. 实现文本规范化、长度限制、额外字段拒绝和重复角色 ID 校验；
5. 增加纯 Schema 验证测试。

本阶段未实现：

- `AdminRoleService` 或用户角色分配 Service；
- `/admin/roles` 或用户角色 API 路由；
- 权限 Seed；
- 鉴权过滤、Session 撤销和审计；
- 数据库迁移或 Redis 逻辑。

---

## 2. Schema 实现

### 2.1 角色请求 Schema

新增文件：

```text
app/schemas/admin_role.py
```

所有管理请求继承严格基类并使用 `ConfigDict(extra="forbid")`，客户端不能提交未声明字段。

#### `AdminRoleCreate`

- `name`：1～64 字符，去除首尾空白，纯空白拒绝；
- `description`：默认为空字符串，最大 255 字符，去除首尾空白；
- 未声明 `id`、`is_enabled`、禁用元数据、时间字段和 `version`，创建状态由后续 Service 固定控制。

#### `AdminRoleUpdate`

- 仅包含可选 `name`、可选 `description` 和必填正整数 `version`；
- 名称继续执行去空白和非空校验；
- 描述支持去空白后变为空字符串，表示清空描述；
- 显式 `null` 的名称和描述被拒绝，未提交字段保留 `exclude_unset` 的部分更新语义；
- `is_enabled`、`disabled_at`、`disabled_reason`、`id`、时间和其他字段会被严格额外字段校验拒绝。

`StrictInt` 用于版本字段，避免布尔值被 Python 整数兼容规则误接受。

#### `AdminRoleDisableRequest`

- 仅接受可选 `reason`；
- 最大 500 字符；
- 首尾空白被去除，纯空白规范化为 `None`。

### 2.2 角色响应 Schema

- `AdminRoleResponse` 支持 `from_attributes`，只返回角色公开管理字段：ID、名称、描述、启用状态、禁用元数据、时间和版本；
- `AdminRoleSummary` 仅返回 `id`、`name`、`description`、`is_enabled`，供用户角色响应复用；
- `AdminRoleListResponse` 提供 `items`、`total`、`page` 和 `page_size`；
- `AdminRoleActionResponse` 在完整角色字段上增加 `changed` 和非负 `revoked_sessions`；
- 响应模型没有权限集合、用户集合、密码、Token 或 Session 内容字段，并且额外字段同样会被拒绝。

### 2.3 用户角色 Schema

扩展文件：

```text
app/schemas/admin_user.py
```

新增 `AdminUserRoleAssignment`：

- `role_ids` 使用严格整数列表；
- 允许空列表，表示后续整体移除普通用户角色；
- 每个 ID 必须为正整数；
- 重复 ID 在 Schema 层直接拒绝；
- `version` 为必填正整数；
- 额外字段拒绝。

新增 `AdminUserRolesResponse`：

- 返回 `user_id`、`roles`、`version`、`changed`、`revoked_sessions`；
- `roles` 使用精简的 `AdminRoleSummary`；
- 不声明密码哈希、权限、Token、Session 或其他敏感用户字段。

---

## 3. 测试

新增：

```text
tests/test_role_management_schemas.py
```

测试覆盖：

1. 创建请求默认描述和文本规范化；
2. 空白名称、超长名称/描述和客户端注入状态、ID、时间、版本字段拒绝；
3. 编辑请求部分更新、描述清空、正版本要求和显式空值拒绝；
4. 编辑请求提交状态或禁用字段时拒绝；
5. 禁用原因规范化、纯空白转 `None`、长度限制和额外字段拒绝；
6. 完整角色、精简角色、分页和动作响应字段形状；
7. 角色响应拒绝权限集合等越界字段；
8. 用户角色分配允许空集合、拒绝非正数、布尔值和重复 ID；
9. 用户角色响应仅承载安全的角色摘要，拒绝密码哈希和权限注入。

测试仅使用 `Role` 内存对象和 Pydantic 校验，不创建测试数据库，不连接 PostgreSQL 或 Redis。

---

## 4. 验证结果

### 4.1 定向静态检查

执行：

```bash
pdm run ruff check app/schemas/admin_role.py app/schemas/admin_user.py tests/test_role_management_schemas.py
```

结果：通过。

### 4.2 定向测试

执行：

```bash
pdm run pytest tests/test_role_management_schemas.py tests/test_admin_user_service.py tests/test_admin_users_api.py -q
```

结果：

```text
22 passed, 1 warning
```

警告是项目已有的 FastAPI/Starlette TestClient 与 `httpx` 的弃用提示，与本阶段 Schema 改动无关。

### 4.3 完整质量检查

执行：

```bash
pdm run lint
pdm run test
git diff --check
```

结果：

```text
lint: passed
test: 122 passed, 5 skipped, 1 warning
git diff --check: passed
```

5 个跳过项是需要显式隔离环境的既有 PostgreSQL/Redis 集成验证；本阶段不要求连接这些外部资源。

---

## 5. 本阶段文件清单

| 文件 | 状态 | 说明 |
| --- | --- | --- |
| `app/schemas/admin_role.py` | 新增 | 角色请求、响应、分页、动作和精简摘要 Schema |
| `app/schemas/admin_user.py` | 修改 | 用户角色整体替换请求与响应 Schema |
| `tests/test_role_management_schemas.py` | 新增 | 严格字段边界和安全响应测试 |
| `plan/ROLE_MANAGEMENT_PHASE_2_EXECUTION_PLAN.md` | 新增 | 第二阶段执行计划 |
| `plan/ROLE_MANAGEMENT_SCHEMA_BOUNDARY_IMPLEMENTATION.md` | 新增 | 第二阶段实际开发与验证记录 |

未修改：

- `app/models/role.py`；
- Alembic 迁移；
- Service、API 路由、权限 Seed、鉴权和 Redis 逻辑。

---

## 6. 第二阶段验收结论

第二阶段“严格 Schema 与领域边界”验收项已满足：

- 空白名称、超长字段和重复角色 ID 在 Schema 层被拒绝；
- 角色编辑不能提交状态、禁用元数据或其他越界字段；
- 角色响应不包含权限集合；
- 用户角色响应使用精简角色摘要，不包含密码、Token、Session 或权限集合；
- 角色分配允许空集合并要求正整数用户版本；
- 定向测试、完整测试、lint 和 diff 空白检查均通过；
- 未提前实现第三阶段 Service 或后续 API、Seed、鉴权功能。

下一阶段为“角色服务与用户角色分配服务”，将消费本阶段已固定的 Schema 契约。
