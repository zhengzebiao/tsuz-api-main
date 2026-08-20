# QQ OAuth 第四阶段执行记录

## 1. 执行范围

本阶段收尾范围为 QQ 隔离集成、迁移/模型漂移回环、真实 QQ 测试应用 acceptance 入口、README/计划文档和质量检查。不执行生产 PostgreSQL、生产 Redis、远程部署、线上 smoke、消费端页面或代码 push。

未将运行时 `.env` 内容写入本记录；本记录不包含 APP_ID、APP_KEY、数据库/Redis 凭据、state、ticket、openid、QQ access token、本系统 access/refresh token 或完整敏感 URL。

## 2. 实际实现

### 2.1 隔离 QQ 集成

`scripts/validate_qq_phase_4.py` 使用 fake QQ provider、本机 PostgreSQL 随机临时数据库和隔离 Redis prefix，入口为：

```bash
RUN_PHASE4_QQ_INTEGRATION=1 pdm run phase4-qq-validate
```

脚本先将仓库根目录加入导入路径，确保通过 PDM 直接执行时可以加载 `app` 包；同时验证 disabled/blacklisted callback 的稳定错误回跳、重复固定 provider callback URL 和 URL query 中 OAuth 敏感值的日志脱敏。

### 2.2 迁移验证

`tests/test_qq_login_migration.py` 复用邮箱 Phase 4 的本机 PostgreSQL 管理连接约定，入口为：

```bash
RUN_QQ_LOGIN_MIGRATION=1 pdm run pytest tests/test_qq_login_migration.py -q
```

测试创建随机临时数据库，执行 `0006_email_registration -> 0007_qq_login -> downgrade -> head`，覆盖 legacy 用户保留、QQ-only nullable 字段、identity 约束/索引/FK、重复 provider subject、危险 downgrade 拒绝、安全 downgrade 和 Alembic check。

### 2.3 真实 QQ acceptance 入口

新增 PDM 命令：

```text
phase4-qq-real-validate = "python scripts/validate_real_qq_phase_4.py"
```

真实入口要求 `RUN_PHASE4_REAL_QQ=1`、`APP_ENV=test` 和受保护的 test/staging/loopback `PHASE4_REAL_QQ_API_BASE_URL`。它通过浏览器完成人工授权，在无消费端页面时以隐藏输入接收一次性 ticket，随后立即 exchange、调用 `/auth/me` 并验证 ticket replay 返回 401。入口未执行真实 provider 时不会输出通过结果。

### 2.4 文档

- README 增加 QQ 三个路由、固定 callback、ticket exchange、Redis 哈希/TTL、QQ-only 用户、fake 验证和真实 gate 说明；
- 新增 `plan/QQ_OAUTH_PHASE_4_IMPLEMENTATION_PLAN.md`；
- 新增本执行记录。

## 3. 实际验证结果

### 3.1 Fake QQ 隔离集成

命令：

```bash
RUN_PHASE4_QQ_INTEGRATION=1 pdm run phase4-qq-validate
```

结果：

```text
[PASS] QQ phase 4 validation: {
  "authorization": true,
  "bearer_me": true,
  "callback": true,
  "disabled_and_blacklisted": true,
  "fixed_consumer": true,
  "provider_failure": true,
  "replay_and_expiry": true,
  "temporary_resources_cleaned": true,
  "ticket_exchange": true
}
```

### 3.2 QQ migration roundtrip

命令：

```bash
RUN_QQ_LOGIN_MIGRATION=1 pdm run pytest tests/test_qq_login_migration.py -q
```

结果：

```text
1 passed, 1 warning in 4.41s
```

警告为现有 Starlette/httpx TestClient deprecation warning。

### 3.3 全量质量检查

本阶段最后一次完整质量检查的实际结果：

```text
pdm run lint       -> All checks passed!
pdm run pytest -q  -> 347 passed, 17 skipped, 1 warning
pdm lock --check   -> passed
git diff --check   -> passed
```

warning 仍为现有 Starlette/httpx TestClient deprecation warning。

## 4. 真实 QQ acceptance 状态

状态：`SKIPPED / NOT ACCEPTED`

实际执行记录：

1. 未设置 gate 时执行 `pdm run phase4-qq-real-validate`，入口按设计返回退出码 2，并提示必须显式设置 `RUN_PHASE4_REAL_QQ=1`；
2. 随后仅在 loopback 上启动本地 API，并以 `APP_ENV=test`、loopback `PHASE4_REAL_QQ_API_BASE_URL` 和 `PHASE4_REAL_QQ_SKIP_BROWSER=1` 启动受保护入口；
3. 入口成功请求 `/auth/qq/login` 并进入等待人工授权/ticket 的阶段，但按本次决定不继续扫码、浏览器授权或手工 ticket exchange，随后因无交互输入退出；
4. 因此没有完成真实 QQ 测试应用授权、固定 callback、真实 ticket exchange 或真实 `/auth/me` acceptance，不能标记为 PASS。

fake provider 结果不能代替真实 QQ 通过。消费端页面不存在不是后端流程阻塞；若后续需要真实 acceptance，应由操作人员完成 test/staging 或 loopback API 的固定 callback 授权，再按入口提示提供一次性 ticket 并立即 exchange。测试应用凭据未写入仓库或本记录。

## 5. 安全与范围确认

- 未访问生产数据库或生产 Redis；
- 未执行生产迁移、远程部署、线上 smoke 或 push；
- 未修改或覆盖运行时 `.env`；
- 未输出或保存 OAuth 授权码、state、ticket、openid、provider token、本系统 token、数据库密码或 Redis 密码；
- Redis 只使用 state/ticket 的 SHA-256 派生 key，且隔离验证结束后清理 namespace；
- 保留既有 Ruff 自动修复，未扩展到历史无关清理。
