# 角色管理 Role：第三阶段角色服务与用户角色分配服务开发记录

## 1. 开发范围

本阶段根据 [ROLE_MANAGEMENT_IMPLEMENTATION_PLAN.md](ROLE_MANAGEMENT_IMPLEMENTATION_PLAN.md) 和 [ROLE_MANAGEMENT_PHASE_3_EXECUTION_PLAN.md](ROLE_MANAGEMENT_PHASE_3_EXECUTION_PLAN.md)，在角色数据层和严格 Schema 基础上实现：

1. `AdminRoleService` 角色查询、创建、编辑、启用和禁用；
2. 角色关联用户分页查询；
3. `AdminUserService` 用户角色查询和完整集合整体替换；
4. 角色/用户乐观锁、行锁、`admin` 核心保护；
5. 鉴权相关变更的 Session 撤销；
6. 角色和用户角色变更的安全审计；
7. Service 单元测试、锁意图测试和事务回滚测试。

本阶段未实现：

- `/admin/roles` 或用户角色 HTTP API；
- 角色管理和用户角色分配权限 Seed；
- AuthService 对禁用角色的鉴权过滤；
- 角色权限配置；
- 真实 PostgreSQL/Redis 集成验证；
- ORM、Schema 或 Alembic 迁移修改。

---

## 2. `AdminRoleService`

新增：

```text
app/services/admin_role_service.py
```

### 2.1 领域错误

固定错误码：

- `ROLE_NOT_FOUND`；
- `ROLE_NAME_ALREADY_EXISTS`；
- `ROLE_VERSION_CONFLICT`；
- `PROTECTED_ROLE_OPERATION`；
- `ROLE_DISABLED`。

数据库完整性异常不会向调用方暴露 SQL、约束名称或内部异常文本。

### 2.2 查询

`list_roles()` 支持：

- 分页；
- 名称和描述关键词不区分大小写搜索；
- 启用状态筛选；
- `created_at DESC, id DESC` 稳定排序。

`get_role()` 在角色不存在时返回固定 `ROLE_NOT_FOUND` 领域错误。

`list_role_users()`：

- 先验证角色存在；
- 通过 `user_roles` 查询关联用户；
- 支持邮箱/显示名关键词、活跃状态和黑名单状态筛选；
- 按用户 ID 稳定分页；
- 已禁用角色仍可查询保留的用户关联；
- Service 只返回 `User` 对象，后续 API 将使用既有安全用户响应 Schema。

### 2.3 创建和编辑

角色创建：

- 消费 `AdminRoleCreate` 已规范化文本；
- 服务端固定新角色启用；
- 预检查名称并使用嵌套事务处理并发唯一冲突；
- 写入 `role.created` 安全审计；
- 只 `flush`，不提交调用方事务。

角色编辑：

- 使用请求 `version` 做前置检查和 `WHERE id/version` 原子更新；
- 无实际变化时不递增版本、不撤销 Session、不写审计；
- `admin` 角色允许修改描述，但禁止改名；
- 名称变化撤销全部关联用户活跃 Session；
- 仅描述变化不撤销 Session；
- 重名和旧版本分别返回固定领域错误；
- 有效变化写入 `role.updated` 审计。

### 2.4 启用、禁用和 Session 撤销

状态操作使用 `SELECT ... FOR UPDATE` 锁定角色：

- `admin` 角色禁止禁用；
- 重复禁用/启用返回 `changed=false`；
- 重复禁用不覆盖首次禁用时间和原因；
- 有效禁用设置状态、时间、原因并递增版本；
- 禁用撤销全部关联用户的活跃 Session；
- 有效启用清空禁用元数据并递增版本，不恢复旧 Session；
- 状态变化不删除 `user_roles` 或 `role_permissions`；
- 只为有效变化写 `role.disabled` / `role.enabled` 审计。

---

## 3. 用户角色完整集合替换

扩展：

```text
app/services/admin_user_service.py
```

新增 `get_user_roles()`：

- 先验证用户存在；
- 返回当前全部角色，包括仍保留关联的已禁用角色；
- 按角色名称和 ID 稳定排序。

新增 `assign_roles()`：

1. 行锁读取目标用户；
2. 校验用户版本；
3. 读取当前角色集合；
4. 一次读取全部目标角色；
5. 任一 ID 不存在时以 `ROLE_NOT_FOUND` 整体拒绝；
6. 已禁用角色不能新增，但已有已禁用角色可以保留；
7. 计算新增和移除差集，只修改实际变化的关联行；
8. 相同集合幂等返回，不递增版本、不撤销 Session、不审计；
9. 普通用户允许清空角色；
10. 禁止操作者移除自己的 `admin`；
11. 禁止移除最后一名活跃且未拉黑管理员的 `admin`；
12. 有效变化递增用户版本和更新时间；
13. 以 `user_roles_changed` 原因撤销目标用户全部活跃 Session；
14. 写入 `user.roles_assigned` 审计；
15. 只 `flush`，由后续 API 管理提交或回滚。

用户角色审计只记录稳定排序的角色 ID、角色名称和撤销数量，不记录密码、密码哈希、权限、Token、Session ID 或其他敏感用户资料。

---

## 4. 事务边界

`AdminRoleService` 写操作及 `AdminUserService.assign_roles()` 使用调用方管理事务：

- 业务数据和 `AuditEvent` 处于同一数据库事务；
- 回滚会恢复角色/用户版本、状态、关联表、数据库 Session 行和审计；
- 创建和角色重名更新通过保存点隔离唯一约束异常；
- 不改变 `AdminUserService` 既有用户创建、状态、密码等方法的提交行为。

Redis 撤销属于外部安全副作用，不宣称可以通过数据库回滚恢复。本阶段测试通过 Fake Redis 隔离；真实 PostgreSQL/Redis 事务与并发行为留到第五阶段验证。

---

## 5. 测试

新增：

```text
tests/test_admin_role_service.py
```

扩展：

```text
tests/test_admin_user_service.py
```

覆盖：

- 角色列表分页、搜索、筛选和稳定排序；
- 角色详情不存在；
- 创建默认启用、重名冲突和安全审计；
- 并发唯一冲突通过保存点转换为固定错误；
- 编辑版本冲突、无变化、重名和 `admin` 改名保护；
- 名称变化撤销关联用户 Session，仅描述变化不撤销；
- 禁用、启用幂等、禁用元数据和关联保留；
- 角色状态行锁和关联用户查询；
- 用户角色查询和完整集合替换；
- 普通用户清空角色和重复提交幂等；
- 无效角色整体失败；
- 禁用角色新增拒绝和已有禁用角色保留；
- 用户版本冲突；
- 用户版本递增和 Session 撤销；
- 自移除及最后管理员保护；
- 用户与核心角色行锁；
- 角色状态/用户角色关联、数据库 Session 行和审计事务回滚；
- 审计不包含密码、权限或 Session ID。

测试使用内存 SQLite，通过 PostgreSQL dialect 编译断言 `FOR UPDATE` 锁意图，通过 Fake Redis 隔离 Session 撤销。

---

## 6. 验证结果

### 6.1 定向验证

执行：

```bash
pdm run ruff check app/services/admin_role_service.py app/services/admin_user_service.py tests/test_admin_role_service.py tests/test_admin_user_service.py
pdm run pytest tests/test_admin_role_service.py tests/test_admin_user_service.py tests/test_role_management_schemas.py -q
```

结果：

```text
ruff: passed
pytest: 37 passed, 1 warning
```

### 6.2 完整验证

执行：

```bash
pdm run lint
pdm run test
git diff --check
```

结果：

```text
lint: passed
test: 143 passed, 5 skipped, 1 warning
git diff --check: passed
```

警告是项目已有的 FastAPI/Starlette TestClient 与 `httpx` 弃用提示，与本阶段 Service 改动无关。5 个跳过项是需要显式隔离 PostgreSQL/Redis 环境的既有集成测试。

---

## 7. 本阶段文件清单

| 文件 | 状态 | 说明 |
| --- | --- | --- |
| `app/services/admin_role_service.py` | 新增 | 角色生命周期、关联用户、保护、锁、Session 撤销和审计 |
| `app/services/admin_user_service.py` | 修改 | 用户角色查询和完整集合替换 |
| `tests/test_admin_role_service.py` | 新增 | 角色 Service、锁、审计和回滚测试 |
| `tests/test_admin_user_service.py` | 修改 | 用户角色分配、安全规则、锁和回滚测试 |
| `plan/ROLE_MANAGEMENT_PHASE_3_EXECUTION_PLAN.md` | 新增 | 第三阶段执行计划 |
| `plan/ROLE_MANAGEMENT_SERVICE_IMPLEMENTATION.md` | 新增 | 第三阶段实际开发与验证记录 |

未修改：

- API 路由和 `app/main.py`；
- 权限 Seed；
- `AuthService`；
- Schema；
- ORM 模型；
- Alembic 迁移。

---

## 8. 第三阶段验收结论

第三阶段“角色服务与用户角色分配服务”验收项已满足：

- 角色生命周期业务规则、幂等和版本控制已实现；
- 用户角色完整集合替换幂等且使用用户行锁；
- 无效或已禁用的新角色不会产生部分更新；
- 角色名称、禁用和用户角色变化会按规则撤销 Session；
- 核心 `admin` 角色不能改名、禁用或被不安全移除；
- 业务变更与审计可在同一数据库事务回滚；
- 定向测试、完整测试、lint 和 diff 空白检查通过；
- 未提前实现第四阶段 API、权限 Seed 或鉴权过滤。
