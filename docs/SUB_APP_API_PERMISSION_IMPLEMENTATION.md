# 子应用管理 App：第四阶段 API 与权限开发记录

## 1. 开发范围

本阶段根据 [SUB_APP_DEVELOPMENT_PLAN.md](../SUB_APP_DEVELOPMENT_PLAN.md) 实施第四阶段“API 与权限”，完成：

1. `/admin/apps` 管理路由；
2. App 管理权限控制；
3. Service 调用方事务提交和异常回滚；
4. App 领域错误到 HTTP 状态码的映射；
5. 主应用路由注册；
6. App 管理权限 Seed；
7. 一次性 Secret 响应的 `Cache-Control: no-store`；
8. 隔离 API 和权限测试。

本阶段未实现：

- 删除子应用；
- 应用编码、菜单、OAuth 回调、Token Scope 或权限范围管理；
- 子应用访问状态监控；
- Secret 双密钥过渡、吊销列表等完整轮换机制；
- 数据库迁移或模型字段修改；
- Redis 逻辑；
- 真实 PostgreSQL 行锁并发和真实 JWT/角色集成验证，留到第五阶段。

---

## 2. 管理 API

新增文件：

```text
app/api/admin_apps.py
```

实现以下接口：

| 方法 | 路径 | 权限 | 响应 |
| --- | --- | --- | --- |
| GET | `/admin/apps` | `app:read` | 分页 App 列表 |
| GET | `/admin/apps/{app_id}` | `app:read` | App 详情 |
| POST | `/admin/apps` | `app:create` | App 与一次性 Secret |
| PATCH | `/admin/apps/{app_id}` | `app:update` | App、`changed` |
| POST | `/admin/apps/{app_id}/disable` | `app:disable` | App、`changed` |
| POST | `/admin/apps/{app_id}/enable` | `app:enable` | App、`changed` |
| POST | `/admin/apps/{app_id}/regenerate-secret` | `app:regenerate_secret` | App ID、一次性新 Secret、更新时间 |

列表接口支持：

- `page >= 1`；
- `1 <= page_size <= 100`；
- 名称或 App ID 关键词搜索；
- `is_enabled` 状态筛选；
- Service 中既有的 `created_at DESC, id DESC` 稳定排序。

主应用在 `app/main.py` 注册 `admin_apps_router`，OpenAPI 中可以获取全部七个接口。

---

## 3. 认证和权限

所有 App 管理接口复用现有 `require_permissions()`：

- 未携带 Bearer Token：返回 401 `invalid access token`；
- Token 无效：返回 401 `invalid access token`；
- Token 有效但缺少目标权限：返回 403 `insufficient permissions`；
- 权限通过后依赖返回当前管理员 `User`，其 ID 作为审计 Actor。

`app/seed/__main__.py` 的 `DEFAULT_PERMISSIONS` 新增：

- `app:read`；
- `app:create`；
- `app:update`；
- `app:enable`；
- `app:disable`；
- `app:regenerate_secret`。

现有 Seed 循环会调用 `ensure_permission()` 和 `ensure_role_permission()`，因此权限已存在或 admin 角色已关联时不会重复创建或重复关联。

---

## 4. 事务边界

第三阶段 `AdminAppService` 只执行 `flush`，不自行提交。本阶段 API 新增统一写操作执行辅助逻辑：

1. 调用 Service；
2. 使用严格 Pydantic Schema 构造完整响应；
3. 响应构造成功后执行 `db.commit()`；
4. 捕获 `AdminAppError` 时先 `db.rollback()`，再映射为 HTTP 错误；
5. 捕获其他异常时也先 `db.rollback()`，然后继续抛出。

这保证：

- App 业务变化和对应 `AuditEvent` 原子提交；
- 领域错误不会留下未提交修改；
- 响应序列化失败不会提交业务数据；
- 提交失败时不会向客户端返回尚未可靠持久化的明文 Secret；
- 创建或 Secret 重新生成失败时不会错误设置成功响应的禁止缓存头。

查询接口不修改事务，也不执行提交。

---

## 5. 错误映射

固定错误映射如下：

| 领域错误 | HTTP 状态 | 响应 detail |
| --- | --- | --- |
| `AppNotFoundError` | 404 | `APP_NOT_FOUND` |
| `AppVersionConflictError` | 409 | `APP_VERSION_CONFLICT` |
| `AppCreationError` | 500 | `APP_CREATION_FAILED` |
| `AppSecretGenerationError` | 500 | `APP_SECRET_GENERATION_FAILED` |

未单独声明的 `AdminAppError` 使用安全的 400 兜底。响应只使用固定错误码，不包含 SQL、数据库约束参数、明文 Secret 或 Secret Hash。

Pydantic/FastAPI 请求校验错误仍返回 422，例如：

- URL 不是 HTTP/HTTPS；
- 版本号无效；
- 字段超长；
- 编辑请求注入 `is_enabled`、`app_secret` 或其他未声明字段。

---

## 6. Secret 响应与缓存控制

只有两个响应可以包含明文 Secret：

- 创建 App 的 `AdminAppCreateResponse`；
- 重新生成 Secret 的 `AdminAppSecretResponse`。

这两个接口仅在事务成功提交后设置：

```http
Cache-Control: no-store
```

列表、详情、编辑、禁用和启用使用普通响应 Schema：

- 不声明 `app_secret`；
- 不声明 `app_secret_hash`；
- 额外敏感字段无法通过响应模型注入。

API 测试还验证重新生成后普通详情接口无法再次获取明文 Secret，旧 Secret 不再匹配数据库 Hash，新 Secret可以匹配。

---

## 7. 测试

新增：

```text
tests/test_admin_apps_api.py
```

修改：

```text
tests/test_seed.py
```

API 测试覆盖：

- 主应用路由和 HTTP 方法注册；
- 未认证 401 和权限不足 403；
- 每个端点要求准确的 App 权限名；
- 创建返回 201、一次性 Secret 和 `no-store`；
- 列表分页、关键词和状态筛选；
- 详情响应安全；
- 编辑资料、版本递增和 `changed`；
- 禁用、重复禁用、启用的幂等状态行为；
- Secret 重新生成、旧 Secret 失效和 `no-store`；
- 普通响应和审计不含 Secret 或 Hash；
- 404 `APP_NOT_FOUND`；
- 409 `APP_VERSION_CONFLICT`；
- 敏感字段和非法 URL 的 422；
- 创建失败的固定 500 错误和事务回滚；
- Secret 生成失败的固定 500 错误和 Hash 回滚；
- Request ID 进入对应 App 审计。

Seed 测试显式验证六项 App 权限存在，且连续运行两次 Seed 后权限数和角色关联数仍与 `DEFAULT_PERMISSIONS` 一致。

---

## 8. 验证结果

执行：

```bash
pdm run ruff check app/api/admin_apps.py app/main.py app/seed/__main__.py tests/test_admin_apps_api.py tests/test_seed.py
pdm run pytest tests/test_admin_apps_api.py tests/test_seed.py -q
pdm run pytest tests/test_app_management_models.py tests/test_app_management_security_schema.py tests/test_admin_app_service.py tests/test_admin_apps_api.py tests/test_seed.py -q
pdm run lint
pdm run test
```

结果：

- 第四阶段定向 Ruff：通过；
- API 与 Seed 定向测试：`7 passed`；
- App 第一至第四阶段相关测试：`31 passed`；
- 完整 Ruff：通过；
- 完整测试：`108 passed, 2 skipped`；
- 测试环境保留 1 条既有 `StarletteDeprecationWarning`，与本阶段实现无关。

---

## 9. 数据库和 Redis 隔离

本阶段没有运行 Alembic，没有连接或写入开发 PostgreSQL。所有 API 数据库测试使用测试函数独立创建并销毁的 SQLite 内存数据库；事务回滚测试也只在该内存数据库执行。

本阶段没有 Redis 业务逻辑，没有连接开发 Redis，也没有执行 `FLUSHDB`、`FLUSHALL` 或任何 Redis 清理命令。

第五阶段若验证真实 PostgreSQL 行锁、真实认证权限或完整生命周期，必须使用临时 PostgreSQL 容器、随机临时数据库或测试框架管理的隔离数据库；Redis 必须使用独立连接地址、数据库编号和 Key 前缀，不能操作开发 Redis 的 Key 空间。

---

## 10. 下一阶段

第五阶段“测试与验证”预计完成：

1. 临时 PostgreSQL 中的真实行锁和并发行为验证；
2. 真实认证 Token 与 App 权限边界验证；
3. 创建至 Secret 重新生成的完整生命周期集成测试；
4. 响应、日志和审计的敏感信息复核；
5. 必要的临时基础设施验证和最终验收。

第五阶段不得在开发 PostgreSQL 上运行写入型集成测试或迁移回滚，也不得清空或复用开发 Redis Key 空间。
