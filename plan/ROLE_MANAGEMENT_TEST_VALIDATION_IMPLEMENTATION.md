# 角色管理第五阶段：测试与验证开发记录

## 1. 开发范围

本阶段根据 [ROLE_MANAGEMENT_IMPLEMENTATION_PLAN.md](ROLE_MANAGEMENT_IMPLEMENTATION_PLAN.md) 和 [ROLE_MANAGEMENT_PHASE_5_EXECUTION_PLAN.md](ROLE_MANAGEMENT_PHASE_5_EXECUTION_PLAN.md)，完成：

1. 角色模型、Schema、Service、API、Auth 和 Seed 的定向与完整回归；
2. `0004_role_management` 真实 PostgreSQL 迁移升级、降级、再升级和 `alembic check`；
3. 真实 PostgreSQL 角色启停、用户角色替换和角色编辑并发验证；
4. 真实 Redis Session 撤销验证；
5. 真实 RS256 JWT、数据库角色权限和管理 HTTP 生命周期验证；
6. 禁用角色过滤、重新启用恢复、用户角色整体替换和旧 Token 失效验证；
7. 响应、审计、异常、报告和 API 日志敏感信息检查；
8. 认证日志中 Session ID、JTI 和 Refresh Token 替换 Hash 片段的最小安全加固。

本阶段没有实现：

- 角色删除或归档；
- 给角色配置权限；
- 权限目录管理；
- 子应用角色和权限同步；
- 用户直接权限分配；
- 新的认证协议或 Token 格式。

---

## 2. 验证入口

新增：

```text
scripts/validate_role_management_phase_5.py
tests/test_role_management_phase_5_integration.py
```

新增 PDM 命令：

```bash
pdm run role-phase5-validate
```

验证器支持：

```bash
pdm run role-phase5-validate --only migration
pdm run role-phase5-validate --only concurrency
pdm run role-phase5-validate --only http
pdm run role-phase5-validate --only all
```

真实集成 pytest 默认跳过，只有显式设置以下变量时运行：

```bash
RUN_ROLE_MANAGEMENT_PHASE_5_INTEGRATION=1
```

因此日常 `pdm run test` 不需要 PostgreSQL 或 Redis。

---

## 3. 临时基础设施和安全边界

本次真实验证使用两个无持久卷临时容器：

| 服务 | 容器 | 主机地址 | 隔离方式 |
| --- | --- | --- | --- |
| PostgreSQL 16 | `tsuz-role-phase5-postgres` | `127.0.0.1:55432` | 每组创建随机 `tsuz_role_phase5_*` 数据库 |
| Redis 7 | `tsuz-role-phase5-redis` | `127.0.0.1:56379` | DB 15 和随机 `auth:role-phase5:<suffix>:` 前缀 |

验证器默认：

- 只允许 localhost、`127.0.0.1` 或 `::1`；
- 拒绝 PostgreSQL 默认端口 `5432`；
- 拒绝 Redis 默认端口 `6379`；
- 远程地址和默认端口必须显式允许；
- PostgreSQL 只删除本次创建的随机数据库；
- Redis 只使用 `SCAN + DELETE` 清理本次随机前缀；
- 不执行 `FLUSHDB` 或 `FLUSHALL`；
- 不挂载或复用开发数据卷。

验证后直接检查 PostgreSQL 数据库列表和 Redis DB 15，确认角色、用户和 App 三组验证生成的临时数据库及 Key 前缀均无残留。

---

## 4. 真实 Alembic 迁移往返

迁移流程：

```text
0003_app_management
  → 0004_role_management
  → 0003_app_management
  → 0004_role_management
```

升级前写入：

- 一名既有用户；
- 一个既有角色；
- 一个既有权限；
- 一条 `user_roles` 关联；
- 一条 `role_permissions` 关联。

升级后验证：

- `roles` 9 个字段与 ORM 一致；
- 7 个必填字段为 NOT NULL；
- `disabled_at` 和 `disabled_reason` 可空；
- `description=''`、`is_enabled=true`、`version=1`；
- `created_at` 和 `updated_at` 有数据库默认值；
- `ix_roles_id`、唯一 `ix_roles_name`、非唯一 `ix_roles_is_enabled` 均存在；
- 既有用户、角色、权限和两个关联均保留；
- `alembic check` 为 clean。

降级到 `0003` 后确认角色只保留 `id`、`name`，状态索引被删除，但既有数据和关联仍然存在。再次升级后当前 revision 为 `0004_role_management`。

实际报告：

```text
current_revision = 0004_role_management
alembic_check = clean
role_columns_verified = 9
role_indexes_verified = 3
legacy_role_data_preserved = true
role_associations_preserved = true
```

---

## 5. 真实 PostgreSQL 并发验证

每个并发场景使用独立 SQLAlchemy Session。第二个事务记录 PostgreSQL backend PID，验证器查询：

```sql
SELECT wait_event_type = 'Lock'
FROM pg_stat_activity
WHERE pid = :backend_pid
```

只有确认第二个事务实际进入 Lock 等待后才释放第一个事务。

### 5.1 并发禁用

两个事务禁用同一普通角色：

- 第一个返回 `changed=true`；
- 第二个等待锁后返回 `changed=false`；
- 版本只从 1 增加到 2；
- 首次禁用原因不被覆盖；
- 只有一条 `role.disabled` 审计；
- `user_roles` 和 `role_permissions` 保留。

### 5.2 并发启用

两个事务启用同一角色：

- 第一个返回 `changed=true`；
- 第二个等待锁后返回 `changed=false`；
- 版本只增加一次；
- 禁用时间和原因清空；
- 只有一条 `role.enabled` 审计。

### 5.3 并发用户角色替换

两个事务以相同旧用户版本替换同一用户的角色集合：

- 第一个事务成功；
- 第二个事务真实等待用户行锁；
- 读取最新版本后返回 `USER_VERSION_CONFLICT`；
- 最终角色集合、用户版本和审计只反映第一个事务；
- 没有部分关联或重复审计。

### 5.4 乐观锁角色编辑

两个 Session 读取相同角色版本：

- 先提交的描述修改成功；
- 后提交的旧版本修改返回 `ROLE_VERSION_CONFLICT`；
- 最终描述保持先提交值。

实际报告：

```text
row_lock_waits_verified = 3
disable_changes = [true, false]
enable_changes = [true, false]
role_assignment_conflict = USER_VERSION_CONFLICT
role_update_conflict = ROLE_VERSION_CONFLICT
associations_consistent = true
```

---

## 6. 真实 JWT 和权限边界

HTTP 验证动态生成临时 2048 位 RSA 密钥，使用独立 issuer、audience、数据库、Redis 前缀和 Uvicorn 子进程。

验证数据包括：

- 一个无角色管理权限用户；
- 六个分别只拥有一项角色权限的角色和用户；
- 一个只拥有 `user:read` 的用户；
- 一个同时关联启用角色和禁用角色的目标用户；
- Seed 创建的 admin 角色和权限。

验证六项权限：

```text
role:read
role:create
role:update
role:disable
role:enable
user:assign_roles
```

每个主体通过真实 `/auth/login` 获取 Token，使用临时公钥、issuer 和 audience 验证签名，并确认 Token `roles`/`scope` 只包含该启用角色及其权限。

权限边界结果：

```text
permissions_verified = 6
permission_denials_verified = 8
real_jwt_permissions = true
```

覆盖：

- 无 Bearer Token 返回 401；
- 已认证但无权限返回 403；
- 错误单权限 Token 调用目标端点返回 403；
- 正确单权限 Token 可以完成对应动作；
- OpenAPI 包含全部角色和用户角色路径、方法、security 及 Schema。

---

## 7. 真实角色和用户角色 HTTP 生命周期

验证流程：

```text
角色创建
  → 列表/详情
  → 无变化编辑
  → 有效编辑
  → 旧版本冲突
  → 查看关联用户
  → 禁用/重复禁用
  → 重新登录检查 claims
  → 启用/重复启用
  → 重新登录检查 claims 恢复
  → 查询用户角色
  → 整体替换用户角色
  → 重新登录检查最终 claims
```

结果：

- 创建角色默认启用且版本为 1；
- 列表筛选、详情和关联用户查询正确；
- 无变化编辑不增加版本或审计；
- 有效编辑增加版本；
- 旧版本返回 409 `ROLE_VERSION_CONFLICT`；
- 重复禁用和启用均保持幂等；
- `admin` 角色禁用返回 409 `PROTECTED_ROLE_OPERATION`；
- 无效角色返回 404 `ROLE_NOT_FOUND`；
- 禁用角色不能新增分配，返回 409 `ROLE_DISABLED`；
- 已有关联的禁用角色可以原样保留；
- 用户旧版本返回 409 `USER_VERSION_CONFLICT`；
- 重复角色 ID 返回 422；
- 用户角色完整集合替换正确递增用户版本。

---

## 8. Redis Session 和 JWT claims

### 8.1 角色禁用

目标用户先通过真实登录获得 Access/Refresh Token。禁用其关联角色后验证：

- PostgreSQL Session 状态为 `revoked`；
- `revoked_at` 已写入；
- `revoked_reason = role_disabled`；
- Redis `SESSION_PREFIX + sid` 值为 `revoked`；
- Redis TTL 合法；
- 旧 Access Token 调用 `/auth/me` 返回 401；
- 旧 Refresh Token 调用 `/auth/refresh` 返回 401；
- `user_roles` 和 `role_permissions` 仍保留；
- 重新登录后的 JWT 不包含禁用角色。

角色重新启用后再次登录，角色重新进入 JWT `roles`。本阶段没有自动恢复旧 Session。

### 8.2 用户角色替换

替换目标用户角色集合后验证：

- PostgreSQL/Redis Session 撤销；
- `revoked_reason = user_roles_changed`；
- 旧 Access/Refresh Token 均失效；
- 新登录 Token 只包含最终启用角色；
- 最终无权限角色的 `scope` 为空；
- 仍关联的禁用角色不进入 `roles` 或 `scope`。

实际报告：

```text
redis_revocations_verified = 2
old_sessions_rejected = true
disabled_role_claim_filter = true
reenabled_role_claim_restore = true
user_role_replacement_verified = true
```

---

## 9. Seed、审计和敏感信息

Seed 在同一临时数据库连续执行两次。最终：

- 六项角色管理权限各只有一条 Permission；
- admin 与六项权限的关联各只有一条；
- 验证报告 `seed_idempotency_verified = true`。

验证审计动作：

```text
role.created
role.updated
role.disabled
role.enabled
user.roles_assigned
```

同时验证：

- Actor 为执行对应真实权限动作的用户；
- 五个有效动作包含指定 Request ID；
- 禁用原因正确；
- 幂等操作不产生额外有效变化审计；
- 角色集合前后值正确；
- 响应和审计不含密码哈希、Token、权限集合或 Session ID。

首次真实日志扫描发现认证日志会记录原始 Session ID、JTI 和 Refresh Token 替换 Hash 前缀。为满足计划中的敏感信息要求，已最小修改：

- 登录、刷新成功日志只记录用户 ID；
- 登出日志只记录布尔状态；
- Refresh Token 过期、重放、复用、状态错误日志不再记录 Session ID；
- Refresh Token 轮换日志不再记录替换 Hash 前缀；
- 单元测试新增 Session ID/JTI 不进入日志的断言。

修复后真实 API 日志扫描：

```text
sensitive_log_scan = clean
```

---

## 10. 自动化验证结果

定向角色、Auth 和 Seed 测试：

```text
75 passed, 1 warning
```

角色第五阶段统一验证器：

```text
migration: passed
concurrency: passed
http: passed
```

角色 opt-in 集成测试：

```text
3 passed, 1 warning
```

既有真实集成回归：

```text
用户管理：2 passed, 1 warning
子应用管理：3 passed, 1 warning
```

完整检查：

```text
pdm run lint: passed
pdm run test: 157 passed, 8 skipped, 1 warning
```

默认测试新增 3 个预期 skip，为角色第五阶段 opt-in 集成测试；连同已有用户和 App 集成测试共 8 个 skip。

警告仍为项目已有的 Starlette TestClient/httpx 弃用提示。真实验证初始化密码 Hash 时，passlib 还会输出 bcrypt 版本元数据兼容提示；Hash、登录和全部认证断言均正常，本阶段未擅自升级依赖。

---

## 11. 第五阶段验收结论

第五阶段“测试与验证”验收项已满足：

- 角色模型、Schema、Service、API、Auth 和 Seed 回归通过；
- `0004_role_management` 可真实升级、降级、再升级，既有数据和关联保留；
- `alembic check` clean；
- 角色并发禁用和启用通过真实 PostgreSQL 行锁保持幂等一致；
- 用户角色并发替换真实等待锁并检测旧版本冲突；
- 角色编辑可检测乐观锁冲突；
- 角色禁用和用户角色变化撤销真实 PostgreSQL/Redis Session；
- 旧 Access/Refresh Token 立即失效；
- 禁用角色及权限不进入新 JWT，重新启用后重新登录恢复；
- 真实 JWT 独立权限、401/403、完整 HTTP 生命周期和固定错误码通过；
- Seed 重复执行幂等；
- 响应、审计、报告和 API 日志敏感信息扫描 clean；
- 用户管理、子应用管理和认证真实集成回归通过；
- 未实现范围外角色功能。
