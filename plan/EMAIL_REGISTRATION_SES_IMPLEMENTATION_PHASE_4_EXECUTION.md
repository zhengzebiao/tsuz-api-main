# 邮箱注册与腾讯云 SES：第四阶段执行记录

## 1. 执行范围

本次根据 [EMAIL_REGISTRATION_SES_IMPLEMENTATION_PLAN.md](EMAIL_REGISTRATION_SES_IMPLEMENTATION_PLAN.md) 完成第四阶段“测试、真实邮件验证与部署检查”的代码和验证入口：

- 新增临时 PostgreSQL/Redis 隔离验证脚本；
- 新增真实 PostgreSQL/Redis 邮箱认证集成测试；
- 新增显式 gate 控制的 SES 资源预检和单次发信入口；
- 增加 deploy/migrate/compose/nginx/CI 静态部署检查；
- 更新 deploy workflow 的 SES 配置注入和敏感文件权限；
- 生成第四阶段计划和执行文档。

未执行生产数据库、生产 Redis、生产发布、生产 downgrade、DNS、SPF/DKIM/DMARC 修改、CAM 权限修改或 nginx 线上变更。

## 2. 实现结果

### 2.1 隔离验证入口

新增 [scripts/validate_email_registration_phase_4.py](../scripts/validate_email_registration_phase_4.py)，入口为：

```bash
RUN_PHASE4_EMAIL_INTEGRATION=1 \
  pdm run python scripts/validate_email_registration_phase_4.py
```

脚本行为：

1. 默认只接受本机 PostgreSQL 管理连接；
2. 创建随机名称临时数据库；
3. 执行 `alembic upgrade head` 和 `alembic check`；
4. 启动 loopback-only 的临时 `redis:7-alpine` 容器和随机宿主端口；
5. 注入独立数据库、Redis URL 和 key prefix，运行隔离测试；
6. 在 `finally` 中删除 Redis 容器并终止连接后删除 PostgreSQL 临时数据库；
7. 失败输出脱敏，不输出数据库密码或其他凭证。

测试没有对共享 Redis 执行 `FLUSHDB`/`FLUSHALL`。

### 2.2 真实基础设施集成覆盖

新增 [tests/test_phase_4_email_integration.py](../tests/test_phase_4_email_integration.py)，使用受控 Provider stub，避免集成测试重复发送真实邮件。测试在临时 PostgreSQL 和真实 Redis 上覆盖：

- `email_verified_at` 迁移字段；
- 启用的 `normal` 角色和有效权限绑定；
- Challenge TTL、邮箱 Hash/验证码 Hash 存储；
- 注册标准化、已验证用户创建、自动登录；
- 邮箱登录；
- 已知/未知邮箱找回密码响应结构一致，未知邮箱不发送邮件；
- 密码重置、旧密码失效；
- 多 Session 撤销、Redis 撤销 key 和 TTL；
- 错误验证码次数上限和 Challenge 删除；
- `register`/`password_reset` 用途隔离；
- Redis Lua 并发消费最多一次成功；
- 邮箱发送限流；
- 测试 key prefix 清理。

### 2.3 SES 真实发信 gate

验证脚本仅在以下 gate 开启时读取收件人并创建真实 SES Provider：

```text
RUN_PHASE4_REAL_SES=1
PHASE4_SES_RECIPIENT=<环境变量值>
```

收件人只从 `PHASE4_SES_RECIPIENT` 读取，代码和本记录不保存具体地址。gate 关闭时完全跳过真实 SES 发送；变量缺失时在调用 SES 前失败，不猜测或替换收件人。

真实 gate 开启后会在发送前检查 Region、endpoint、发件 identity、DNS 属性、发件地址、模板审核和变量、日配额；预检通过后只发送一次随机六位验证码，不自动重试。输出只保留脱敏收件人和截断后的请求/消息标识，并最多查询一次发送状态。

本次已按显式 gate 再次执行一次 SES 真实验证尝试。收件人来自环境变量 `PHASE4_SES_RECIPIENT`，具体地址不写入本记录。`GetEmailIdentity` 已通过，SES 预检随后在 `ListEmailAddress` 阶段失败，脚本在发送前停止，因此本次没有调用 Tencent Cloud `SendEmail`，也没有自动重试。输出未包含或记录 CAM Secret；失败原文按脚本设计未暴露。

## 3. 部署检查实现

新增 [tests/test_phase_4_deployment_checks.py](../tests/test_phase_4_deployment_checks.py)，静态校验：

- deploy workflow 从 GitHub Environment Secrets 注入 Tencent CAM Secret；
- 生成环境文件前使用 `umask 077`，上传后远端 `chmod 600`，完成后删除本地临时文件；
- SES 配置、Redis namespace 和 `TRUSTED_PROXY_IPS` 变量写入生成环境；
- workflow 不启用真实 SES gate、不打印 Secret；
- migrate workflow 要求显式 revision，product 迁移要求备份确认，并执行 `current → upgrade → current`；
- migration workflow 不包含 downgrade；
- docker compose 使用 `.env` 和 external backend network；
- nginx 转发代理链，未配置任意 Authorization 日志；
- CI 不配置真实 SES gate 或 CAM Secret。

生产迁移仍采用 forward-fix；不可变镜像回滚前需确认旧镜像与当前 schema 兼容。此次没有执行线上迁移或回滚。

## 4. 验证结果

### 4.1 定向测试

```text
53 passed, 1 skipped, 1 warning
```

覆盖 Tencent SES Provider、Verification Challenge、EmailAuthService、邮箱认证 API、迁移和 AuthService。

### 4.2 全量测试

```text
283 passed, 15 skipped, 1 warning
```

### 4.3 临时 PostgreSQL + Redis 集成

执行：

```bash
RUN_PHASE4_EMAIL_INTEGRATION=1 \
  pdm run python scripts/validate_email_registration_phase_4.py
```

结果：

```text
[PASS] isolated email integration: 1 passed, 1 warning
resources_cleaned: true
temporary_redis: true
```

临时数据库和 Redis 容器均在脚本退出路径清理。执行记录不保存临时连接密码。

### 4.4 SES gate 与真实预检结果

此前未开启 gate 时，`--only ses` 安全跳过预检和发送。

随后按用户要求设置 gate，并执行一次真实发信：

```bash
RUN_PHASE4_REAL_SES=1 \
PHASE4_SES_RECIPIENT='<环境变量提供的受控收件箱>' \
  pdm run python scripts/validate_email_registration_phase_4.py --only ses
```

结果：

```text
[PASS] SES smoke: preflight passed; status=sent; status_request_id=available
```

本次 `GetEmailIdentity`、`ListEmailAddress`、模板变量和日配额预检均已通过。模板内容接口返回 Base64 编码，验证脚本已兼容解码后检查 `code` 与 `expire_minutes` 变量。随后向 `PHASE4_SES_RECIPIENT` 执行了一次真实 `SendEmail`，结果为 `sent`；收件人、MessageId 和 RequestId 仅以脱敏/截断形式输出。本次补充配置 `ses:GetSendEmailStatus` 后，状态查询已成功返回脱敏的 `status_request_id`。未触发重试。此次真实发送已完成，需由受控收件箱人工确认邮件到达及变量替换。

### 4.5 静态质量检查

以下检查通过：

```text
pdm run ruff check .       -> All checks passed!
pdm lock --check           -> passed
 git diff --check           -> passed
```

## 5. 安全与限制确认

- CAM Secret 未写入源代码、执行记录、测试输出或提交内容；
- 没有输出真实 SES 收件人、验证码、密码、完整 Token、Authorization Header 或完整 Challenge ID；
- Redis Challenge 只存 hash，不存明文验证码；
- 普通 pytest 和 CI 不调用真实 SES；
- 真实 SES 不自动重试；
- 未执行生产资源操作；
- 真实收件箱中的邮件内容和变量替换仍需在显式 gate 开启后由受控收件人人工确认。
