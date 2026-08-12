# 子应用管理 App：第五阶段测试与验证开发记录

## 1. 开发范围

本阶段根据 [SUB_APP_DEVELOPMENT_PLAN.md](../SUB_APP_DEVELOPMENT_PLAN.md) 实施第五阶段“测试与验证”，完成：

1. `apps` 迁移升级、降级、再升级和 `alembic check`；
2. 真实 PostgreSQL `SELECT ... FOR UPDATE` 行锁与并发一致性验证；
3. 真实 RS256 JWT、数据库角色及六项 App 权限验证；
4. App 管理 HTTP 完整生命周期验证；
5. 一次性 Secret、Hash 存储和旧 Secret 立即失效验证；
6. 响应、审计、异常及 API 日志敏感信息泄漏检查；
7. 默认单元/API 测试与 opt-in 真实集成测试；
8. 既有用户管理验证器对当前 Alembic head 的兼容修正。

本阶段没有新增范围外业务功能，未实现：

- 删除子应用；
- 应用编码或菜单管理；
- OAuth 回调地址或 Token Scope；
- 权限范围配置；
- 子应用访问状态监控；
- Secret 双密钥过渡、吊销列表等完整轮换机制。

---

## 2. 第五阶段验证入口

新增：

```text
scripts/validate_app_phase_5.py
tests/test_app_phase_5_integration.py
```

新增 PDM 命令：

```bash
pdm run app-phase5-validate
```

验证器支持以下分组：

```bash
pdm run app-phase5-validate --only migration
pdm run app-phase5-validate --only concurrency
pdm run app-phase5-validate --only http
pdm run app-phase5-validate --only all
```

默认 `all` 依次执行迁移、并发和真实 HTTP/JWT 三组验证。每组返回只包含状态、计数和非敏感标识的结构化报告，不返回明文 App Secret、Secret Hash、Token、密码、RSA 私钥或数据库口令。

真实集成测试默认跳过，只有显式设置以下变量时才会连接临时基础设施：

```bash
RUN_APP_PHASE_5_INTEGRATION=1
```

因此日常 `pdm run test` 不依赖 PostgreSQL 或 Redis 服务。

---

## 3. 临时基础设施安全边界

本阶段真实验证使用本次会话创建的两个无持久卷临时容器：

| 服务 | 临时容器 | 主机地址 | 隔离方式 |
| --- | --- | --- | --- |
| PostgreSQL 16 | `tsuz-app-phase5-postgres` | `127.0.0.1:55432` | 每组创建随机 `tsuz_app_phase5_*` 数据库 |
| Redis 7 | `tsuz-app-phase5-redis` | `127.0.0.1:56379` | DB 15 与随机 `auth:app-phase5:<suffix>:` 前缀 |

验证器具有以下防误用保护：

- 默认只允许 localhost、`127.0.0.1` 或 `::1`；
- 默认拒绝 PostgreSQL 开发常用端口 `5432`；
- 默认拒绝 Redis 开发常用端口 `6379`；
- 远程地址或默认端口只能通过显式 allow 配置启用；
- PostgreSQL 清理只终止并删除本次生成的随机临时数据库；
- Redis 清理只使用 `SCAN + DELETE` 删除本次随机前缀；
- 不执行 `FLUSHDB` 或 `FLUSHALL`；
- 不挂载或复用开发数据库数据卷。

验证结束后已确认随机 PostgreSQL 数据库和 Redis Key 前缀没有残留。临时容器在最终复核后停止并删除，开发 PostgreSQL/Redis 未被连接、写入或清空。

---

## 4. Alembic 迁移回环验证

迁移验证在随机临时数据库中执行：

```text
base → 0002_user_management → head
     → 0002_user_management → head
```

验证步骤：

1. 升级到 `0002_user_management`；
2. 插入一条用于兼容性验证的既有用户；
3. 升级到当前 head；
4. 检查 `apps` 表结构、索引、约束和数据库默认值；
5. 执行 `alembic check`；
6. 降级回 `0002_user_management`；
7. 确认 `apps` 表被移除且既有用户保留；
8. 再次升级到 head 并确认 revision。

实际结果：

| 检查项 | 结果 |
| --- | --- |
| 当前 revision | `0003_app_management` |
| `alembic check` | `clean` |
| `apps` 字段 | 14 个全部验证 |
| `apps` 索引 | 4 个全部验证 |
| `app_id` 唯一性 | 通过 |
| `is_enabled` 默认值 | `true` |
| `version` 默认值 | `1` |
| 时间字段默认值 | 通过 |
| 既有用户数据保留 | 通过 |
| 临时数据库清理 | 通过 |

同时修改 `scripts/validate_phase_4.py` 和 `tests/test_phase_4_integration.py`：旧用户管理验证仍在 `0002_user_management` 检查其目标结构，但在 `alembic check` 和最终 revision 检查前升级到当前 head `0003_app_management`。

---

## 5. 真实 PostgreSQL 并发验证

并发测试不使用 SQLite 模拟锁。验证器为每个并发事务创建独立 SQLAlchemy Session，记录第二个连接的 PostgreSQL 后端 PID，并查询 `pg_stat_activity`：

```sql
SELECT wait_event_type = 'Lock'
FROM pg_stat_activity
WHERE pid = :backend_pid
```

只有确认竞争事务真实进入 `Lock` 等待后，才允许第一个事务提交。因此验证结果证明了 PostgreSQL 行锁行为，而不只是检查 SQL 是否包含 `FOR UPDATE`。

### 5.1 并发禁用

两个事务同时禁用同一个 App：

- 第一个事务：`changed=true`；
- 第二个事务等待行锁后读取最新状态：`changed=false`；
- App 版本只增加一次；
- 首次禁用原因不会被第二次幂等请求覆盖；
- 只产生一条 `app.disabled` 审计。

### 5.2 并发 Secret 重新生成

两个事务同时重新生成同一个 App 的 Secret：

- 第二个事务真实等待第一个事务持有的行锁；
- 两个事务串行提交，Secret 值互不相同；
- 版本按两次有效变化递增；
- 产生两条 `app.secret_regenerated` 审计；
- 初始 Secret 在第一次轮换后失效；
- 第一次并发轮换结果在第二次提交后失效；
- 数据库最终 Hash 只匹配最后一次成功提交的 Secret。

这符合本期“单密钥立即替换”设计，不引入双密钥过渡或吊销列表。

### 5.3 乐观锁编辑

两个 Session 基于同一旧版本编辑：

- 先提交的编辑成功；
- 后提交的旧版本编辑抛出 `APP_VERSION_CONFLICT`；
- 最终资料保持先提交事务的值，不发生丢失更新。

并发报告关键结果：

```text
row_lock_waits_verified = 2
disable_changes = [true, false]
secret_rotations_serialized = 2
optimistic_conflict = APP_VERSION_CONFLICT
```

---

## 6. 真实 JWT 与权限验证

HTTP 验证动态生成临时 2048 位 RSA 密钥，以独立 issuer、audience、数据库和 Redis Key 前缀启动 Uvicorn 子进程。所有认证均调用真实 `/auth/login`，未使用依赖覆盖或授权替身。

验证数据包括：

- 一个不具备任何 App 权限的用户；
- 六个单权限角色；
- 六个分别只属于一个单权限角色的用户；
- 幂等 Seed 创建的 admin 角色和全部 App 权限。

六项权限均已验证：

- `app:read`；
- `app:create`；
- `app:update`；
- `app:enable`；
- `app:disable`；
- `app:regenerate_secret`。

每个单权限用户登录后，其 Access Token scope 必须与预期单项权限完全一致。权限边界覆盖：

- 无 Bearer Token：401；
- 已认证但无目标权限：403；
- 使用错误单权限 Token 调用目标端点：403；
- 使用正确单权限 Token：成功。

实际报告：

```text
permissions_verified = 6
permission_denials_verified = 8
real_jwt_permissions = true
```

---

## 7. 完整 HTTP 生命周期

真实 HTTP 验证执行以下流程：

```text
创建
  → 列表与详情
  → 无变化编辑
  → 有效编辑
  → 旧版本编辑冲突
  → 禁用
  → 重复禁用
  → 启用
  → 重复启用
  → 重新生成 Secret
  → 详情复核
```

验证结果：

- 创建响应只在本次响应中返回初始 Secret；
- 创建响应设置 `Cache-Control: no-store`；
- 列表、详情、编辑、禁用和启用响应不含 `app_secret` 或 `app_secret_hash`；
- 无变化编辑不增加版本、不生成更新审计；
- 旧版本编辑返回 409 `APP_VERSION_CONFLICT`；
- 重复禁用和重复启用返回 `changed=false`，不重复增加版本或审计；
- 重新生成响应只在本次响应中返回新 Secret，并设置 `Cache-Control: no-store`；
- 数据库只保存 64 位 Secret Hash，不保存明文；
- 轮换后旧 Secret 立即失效，新 Secret 可以匹配最终 Hash；
- 后续详情无法再次取得明文 Secret。

一次性 Secret 响应共验证 2 个。

---

## 8. 审计和 Request ID

完整生命周期产生并验证五条有效变化审计：

```text
app.created
app.updated
app.disabled
app.enabled
app.secret_regenerated
```

每条审计均由执行对应动作的真实单权限用户产生，Actor 不是 Seed admin 替身。五个动作由五个不同权限主体完成，从数据库查询的 Actor 与登录用户一致。

同时验证：

- 五条审计均包含对应请求的 Request ID；
- 禁用审计记录业务原因；
- 幂等状态操作不重复创建审计；
- Secret 轮换审计的 changes 仅为 `{"secret_changed": true}`；
- 审计中不包含明文 Secret、Secret Hash、Token 或完整认证头。

实际报告：

```text
lifecycle_audits_verified = 5
request_ids_verified = 5
```

---

## 9. 日志敏感信息防护

修改 `app/core/logging.py`，在现有集中脱敏基础上增加：

- `app_secret` 键值字段脱敏；
- `app_secret_hash` 键值字段脱敏；
- JSON 中同名字段脱敏；
- 无字段名的裸 `app_secret_...` 值脱敏。

新增日志单元测试覆盖环境变量式、JSON 式和裸 Secret 三类输入。业务代码仍保持不记录 Secret，集中日志脱敏只作为额外兜底。

真实 HTTP 验证捕获 API 子进程日志，并使用本次运行实际生成的敏感值执行扫描。扫描目标包括：

- 初始 App Secret；
- 重新生成的 App Secret；
- 数据库 Secret Hash；
- Access Token；
- 测试用户密码；
- 临时 RSA 私钥。

结果：

```text
sensitive_log_scan = clean
```

验证器自身的异常输出和最终报告也会通过相同的敏感信息脱敏边界，不输出连接口令或业务凭证。

---

## 10. 自动化测试结果

执行的主要命令：

```bash
pdm run lint
pdm run test
pdm run app-phase5-validate
RUN_APP_PHASE_5_INTEGRATION=1 pdm run pytest tests/test_app_phase_5_integration.py -q
RUN_PHASE_4_INTEGRATION=1 pdm run pytest tests/test_phase_4_integration.py -q
```

实际结果：

| 验证 | 结果 |
| --- | --- |
| 完整 Ruff | `All checks passed!` |
| 默认完整测试 | `109 passed, 5 skipped` |
| 第五阶段统一验证器 | 迁移、并发、HTTP/JWT 三组全部通过 |
| 第五阶段 opt-in 集成测试 | `3 passed` |
| 既有用户管理 opt-in 回归 | `2 passed` |
| 敏感 API 日志扫描 | `clean` |

默认完整测试中的 5 项 skip 为两组需要显式启用临时外部服务的集成测试，属于预期行为。

测试期间观察到两类既有依赖兼容提示：

1. Starlette `TestClient` 对当前 `httpx` 的弃用警告；
2. passlib 读取当前 bcrypt 版本元数据时的兼容提示。

两者均未导致认证、Hash 或测试失败，本阶段未擅自升级认证和 Web 测试依赖。

---

## 11. 验收结论

第五阶段已验证本期子应用管理 App 满足以下验收条件：

- 管理员可以创建 App 并一次性获得 Secret；
- 数据库只保存 Secret Hash；
- 列表、详情、编辑、启用、禁用和 Secret 重新生成接口可用；
- 编辑仅允许应用名称、图标、访问地址和服务账号名称；
- 启用和禁用具有幂等行为；
- Secret 重新生成后旧 Secret 立即失效；
- 六项 App 权限均通过真实 JWT 和数据库角色验证；
- 所有有效生命周期变化均有真实 Actor 和 Request ID 审计；
- 普通响应、审计和 API 日志中不存在 Secret 或 Secret Hash 泄漏；
- 并发编辑可以检测版本冲突；
- 并发状态变化和 Secret 重新生成通过真实 PostgreSQL 行锁保持一致；
- Alembic 迁移回环、完整静态检查、默认测试和真实集成测试全部通过。

本阶段只完成计划内测试、验证、安全兜底和兼容修正，没有扩展到未标记为本期范围的功能。
