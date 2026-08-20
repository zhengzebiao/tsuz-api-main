# QQ OAuth 第四阶段实施计划：完整测试、真实联调与验收

## 1. 阶段目标

本阶段关闭 QQ OAuth 的迁移、隔离集成、真实测试应用 acceptance 和质量验收工作。重点是验证已经完成的 QQ 登录实现能够在现有认证、Session、JWT、Refresh Token 和 RBAC 闭环中稳定运行，同时保证迁移回滚不会静默破坏既有邮箱用户。

本阶段不重新设计 QQ OAuth，不新增消费端页面，也不改变既有邮箱登录协议。

## 2. 实施范围

### 2.1 隔离 QQ 集成验证

使用 fake QQ provider、本机 PostgreSQL 临时数据库和隔离 Redis key prefix 验证：

- `/auth/qq/login` 生成授权地址，使用固定 `QQ_REDIRECT_URI`；
- state 只以 SHA-256 派生 key 保存，具备 TTL 且只能消费一次；
- callback 使用固定 `QQ_TICKET_REDIRECT_URI` 返回一次性 ticket；
- ticket 只以 SHA-256 派生 key 保存，具备 TTL 且只能消费一次；
- `/auth/qq/exchange` 复用 `AuthService.complete_login()` 创建 Session 和令牌；
- QQ-only 用户的 `/auth/me` 响应不暴露 provider subject；
- disabled、blacklisted、provider failure、state/ticket 过期和重放均得到稳定结果；
- 临时 PostgreSQL、Redis namespace、API 进程和 fake provider 均清理；
- 授权码、provider access token、openid 不进入 API 日志。

### 2.2 QQ 迁移与模型漂移验证

复用邮箱 Phase 4 的本机 PostgreSQL 管理连接模式，在随机临时数据库中运行：

```text
0006_email_registration -> 0007_qq_login -> downgrade -> 0007_qq_login
```

验证 legacy email/password 用户保留、QQ-only nullable 字段、`user_identities` 的约束/索引/FK、provider subject 冲突和 Alembic check。存在 QQ-only 数据时，危险 downgrade 必须拒绝；清理 QQ-only 数据后才允许安全回滚。

### 2.3 真实 QQ 测试应用 acceptance

真实入口为 `scripts/validate_real_qq_phase_4.py`，PDM 命令为：

```bash
RUN_PHASE4_REAL_QQ=1 \
APP_ENV=test \
PHASE4_REAL_QQ_API_BASE_URL='<test-api-url>' \
pdm run phase4-qq-real-validate
```

入口只允许 test、staging 或 loopback API 地址，并要求显式 gate。它调用 `/auth/qq/login`，由操作人员完成测试应用授权；由于当前没有消费端页面，ticket 通过隐藏输入在内存中接收后立即调用 `/auth/qq/exchange`，随后验证 Bearer `/auth/me` 和 ticket 重放拒绝。输出只包含布尔状态和数量，不输出授权码、state、ticket、openid、provider access token 或本系统令牌。

真实 QQ 只能在人工授权、固定回调可达且测试 API/隔离资源可用时标记通过；条件不足时记录 skipped/blocker，不将 fake provider 结果替代真实 acceptance。

### 2.4 文档与质量验收

- README 增加 QQ 路由、固定回调、ticket exchange、Redis 约束、测试命令和真实 gate 说明；
- 本文件记录实际实施边界；
- `QQ_OAUTH_PHASE_4_EXECUTION.md` 只记录实际执行结果；
- 运行 focused QQ 测试、fake QQ 集成、QQ 迁移测试、全量 lint、全量 pytest、lock 和 diff 检查。

## 3. 安全边界

- 不读取、修改或覆盖运行时 `.env`；凭据仅由进程环境或应用运行时配置提供；
- APP_KEY、数据库/Redis 凭据、state、ticket、openid、QQ access token、本系统 access/refresh token 和完整敏感 URL 不写入代码、计划、执行记录、日志或测试输出；
- 不访问生产 PostgreSQL 或生产 Redis，不执行生产迁移、部署、rollback 或 push；
- Redis 不保存 OAuth token、openid、授权码、完整 profile 或本系统令牌；
- callback 不使用请求 host、请求参数或动态回跳地址构造 provider/consumer URL；
- 真实 QQ 和外部资源操作保持显式 opt-in。

## 4. 范围外事项

本阶段不处理：

1. QQ OAuth 第一阶段基础配置和第二阶段 service foundation 的重新开发；
2. 第三阶段路由/schema 基础功能的重新开发；
3. QQ 与邮箱绑定、解绑、邮箱补充和账号合并；
4. 消费端前端页面；
5. 正式生产备份 gate、生产数据库/Redis、远程部署、线上 smoke 或代码 push。

## 5. 验收标准

- fake QQ 隔离全链路通过；
- QQ migration upgrade/downgrade/head roundtrip 通过且无 model drift；
- 全量 Ruff、pytest、lock、diff 检查通过；
- 真实 QQ 仅在实际完成测试应用授权和 callback 后标记通过，否则明确记录原因；
- 所有执行记录均不包含敏感值或未执行事项的伪造结果。
