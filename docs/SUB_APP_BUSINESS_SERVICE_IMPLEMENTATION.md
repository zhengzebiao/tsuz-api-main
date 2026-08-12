# 子应用管理 App：第三阶段业务服务开发记录

## 1. 开发范围

本阶段根据 [SUB_APP_DEVELOPMENT_PLAN.md](../SUB_APP_DEVELOPMENT_PLAN.md) 实施第三阶段“业务服务”，完成：

1. App 列表和详情查询；
2. App 创建及凭证 Hash 持久化；
3. 带乐观锁的基本资料编辑；
4. 带行锁的启用和禁用；
5. 带行锁的 App Secret 重新生成；
6. App 变更的非敏感审计记录；
7. Service 隔离单元测试。

本阶段未实现：

- App 管理 API 和路由注册；
- App 管理权限 Seed；
- 数据库迁移或模型字段修改；
- Redis 逻辑；
- Secret HTTP 响应的 `Cache-Control: no-store`，待第四阶段 API 接入；
- PostgreSQL 并发阻塞集成验证，待第五阶段在临时 PostgreSQL 中执行。

---

## 2. Service 实现

新增文件：

```text
app/services/admin_app_service.py
```

新增 `AdminAppService`，提供：

- `list_apps()`；
- `get_app()`；
- `create_app()`；
- `update_app()`；
- `disable_app()`；
- `enable_app()`；
- `regenerate_secret()`。

### 2.1 领域错误

Service 使用固定错误码，不向上层暴露 SQL、约束参数、Secret 或 Hash：

| 异常 | 错误码 | 场景 |
| --- | --- | --- |
| `AppNotFoundError` | `APP_NOT_FOUND` | App 不存在 |
| `AppVersionConflictError` | `APP_VERSION_CONFLICT` | 编辑版本冲突 |
| `AppCreationError` | `APP_CREATION_FAILED` | App ID 重试耗尽或内部创建失败 |
| `AppSecretGenerationError` | `APP_SECRET_GENERATION_FAILED` | 无法生成与旧凭证不同的新 Secret |

第四阶段 API 将负责把这些领域错误转换为对应 HTTP 状态码，并在异常路径回滚事务。

---

## 3. 查询与创建

### 3.1 列表和详情

`list_apps()` 支持：

- `page` 和 `page_size` 分页；
- 按名称或 App ID 进行大小写不敏感的模糊搜索；
- 按 `is_enabled` 筛选；
- 按 `created_at DESC, id DESC` 稳定排序；
- 同时返回当前页数据和筛选后的总数。

`get_app()` 使用数据库内部主键查询，不存在时抛出 `APP_NOT_FOUND`。

### 3.2 创建和 App ID 冲突处理

`create_app()` 复用第二阶段安全工具：

1. `generate_app_secret()` 生成高熵明文 Secret；
2. `hash_app_secret()` 计算 SHA-256 Hash；
3. `generate_app_id()` 生成 App ID；
4. 数据库只保存 Hash；
5. 方法返回 `(App, app_secret)`，让后续 API 仅在创建响应中展示一次明文；
6. 创建 `app.created` 审计并 `flush`，不提交事务。

App ID 插入位于 nested transaction/savepoint 中。若数据库确认冲突来自候选 App ID，则生成新 ID 并有限重试；重试耗尽后只抛出固定 `APP_CREATION_FAILED`，不会返回数据库异常详情。

创建审计只记录：

- App ID；
- 名称；
- 访问地址；
- 服务账号名称；
- 初始启用状态。

创建审计不记录明文 Secret 或 Secret Hash。

---

## 4. 编辑与并发控制

`update_app()` 只从 `AdminAppUpdate` 读取允许编辑的字段，并排除 `version` 后比较真实变化。

处理规则：

1. 先确认 App 存在；
2. 请求版本与当前版本不同则抛出 `APP_VERSION_CONFLICT`；
3. 没有真实变化时返回 `changed=false`，版本不递增，不写审计；
4. 存在变化时执行同时包含 `App.id` 和 `App.version` 条件的 SQL UPDATE；
5. 成功后设置 `updated_at` 并执行 `version + 1`；
6. `rowcount` 不为 1 时区分不存在和并发版本冲突；
7. `app.updated` 审计只记录真正发生变化的非敏感字段。

因此两个管理员使用相同旧版本并发编辑时，只有一个更新可以成功，另一个会得到版本冲突。

---

## 5. 状态与 Secret 操作

### 5.1 行锁

`disable_app()`、`enable_app()` 和 `regenerate_secret()` 均通过统一 `_lock_app()` 查询：

```text
SELECT ... FROM apps WHERE apps.id = ? FOR UPDATE
```

SQLite 内存单元测试不会执行真实行锁阻塞，因此测试通过 PostgreSQL SQL 方言编译确认三个操作的查询都带 `FOR UPDATE`。真实并发行为在第五阶段使用临时 PostgreSQL 验证。

### 5.2 禁用和启用

禁用规则：

- 首次禁用设置 `is_enabled=false`；
- 记录当前 UTC 时间和可选原因；
- 递增版本并更新时间；
- 写入 `app.disabled` 审计；
- 重复禁用返回 `changed=false`，不覆盖原时间和原因，不写重复审计。

启用规则：

- 将 `is_enabled` 恢复为 `true`；
- 清空 `disabled_at` 和 `disabled_reason`；
- 递增版本并更新时间；
- 写入 `app.enabled` 审计；
- 重复启用返回 `changed=false`，不递增版本，不写重复审计。

### 5.3 Secret 重新生成

`regenerate_secret()` 在行锁内：

1. 生成新 Secret；
2. 使用常量时间校验确认候选 Secret 不匹配旧 Hash；
3. 覆盖数据库 Hash；
4. 同时更新 `secret_updated_at` 和 `updated_at`；
5. 递增版本；
6. 写入 `app.secret_regenerated` 审计；
7. 返回 `(App, new_secret)`，明文仅保留在当前内存返回值中。

Secret 审计格式为：

```json
{
  "secret_changed": true
}
```

操作原因存放在审计 `reason` 字段。审计不包含新旧 Secret、新旧 Hash 或其他可用于恢复凭证的信息。

---

## 6. 事务与审计

与总体方案一致，第三阶段 Service：

- 只修改当前 SQLAlchemy Session；
- 对业务数据和 `AuditEvent` 执行 `flush`；
- 不执行 `commit`；
- 不在领域错误中执行隐式提交；
- 允许第四阶段 API 在成功时统一提交，在失败时统一回滚。

测试验证在 Service 完成禁用和审计后执行调用方 `rollback()`，App 状态、禁用信息、版本和审计事件会一起恢复或移除，证明业务变化和审计处于同一事务。

审计统一使用：

- `target_type="app"`；
- 当前管理员用户 ID；
- 显式 request ID、当前 `request_id_context` 或 `unknown`；
- 仅必要的非敏感变化。

---

## 7. 测试

新增文件：

```text
tests/test_admin_app_service.py
```

测试覆盖：

- 列表分页、关键词搜索、状态筛选和稳定排序；
- 详情查询及 `APP_NOT_FOUND`；
- 创建 App、仅保存 Hash、创建审计安全性；
- App ID 冲突后的 savepoint 重试；
- App ID 冲突重试耗尽后的固定安全错误；
- 编辑真实变化、清空图标、版本递增和变化审计；
- 无变化时不递增版本、不产生审计；
- 旧版本编辑冲突；
- 禁用、重复禁用、启用、重复启用的幂等行为；
- 三个锁操作均生成 `FOR UPDATE` SQL；
- Secret 重新生成后旧 Secret 失效、新 Secret 可校验；
- 意外生成相同 Secret 时重试；
- Secret 重试耗尽时不修改 Hash、不写审计；
- Secret 审计不包含明文或 Hash；
- Service 不内部提交，调用方回滚会同时撤销业务变化和审计。

所有数据库测试均使用每个测试单独创建并销毁的 SQLite 内存数据库，没有连接开发 PostgreSQL。

---

## 8. 验证结果

本阶段开发过程中执行：

```bash
pdm run ruff check app/services/admin_app_service.py tests/test_admin_app_service.py
pdm run pytest tests/test_admin_app_service.py -q
pdm run pytest tests/test_app_management_models.py tests/test_app_management_security_schema.py tests/test_admin_app_service.py -q
pdm run lint
pdm run test
```

最终结果：

- 定向 Ruff 检查：通过；
- 第三阶段 Service 测试：`12 passed`；
- App 第一至第三阶段测试：`24 passed`；
- 完整 Ruff：通过；
- 完整测试：`102 passed, 2 skipped`；
- 测试环境存在 1 条既有 `StarletteDeprecationWarning`，与本阶段实现无关。

---

## 9. 数据库和 Redis 隔离

本阶段没有运行 Alembic，也没有连接或写入开发 PostgreSQL。所有 Service 数据库写测试使用 SQLite 内存数据库，测试结束后立即销毁引擎和表。

本阶段没有 Redis 业务逻辑，也没有连接开发 Redis，未执行 `FLUSHDB`、`FLUSHALL` 或任何 Redis 清理命令。

后续 PostgreSQL 集成和并发验证必须使用临时 PostgreSQL 容器、明确隔离的临时数据库或测试框架管理的数据库；不得在开发库执行测试写入、迁移回滚或清空操作。

---

## 10. 下一阶段

第四阶段“API 与权限”预计实现：

1. `/admin/apps` 管理路由；
2. Service 调用方事务提交和异常回滚；
3. 领域错误到 HTTP 状态码的映射；
4. 路由注册；
5. App 管理权限 Seed；
6. 创建和 Secret 重新生成响应的 `Cache-Control: no-store`。

第四阶段必须继续保证列表、详情、编辑和状态响应不包含明文 Secret 或 Secret Hash。
