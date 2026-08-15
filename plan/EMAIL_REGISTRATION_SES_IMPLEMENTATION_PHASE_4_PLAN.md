# 邮箱注册与腾讯云 SES：第四阶段实现计划

> 基于 [EMAIL_REGISTRATION_SES_IMPLEMENTATION_PLAN.md](EMAIL_REGISTRATION_SES_IMPLEMENTATION_PLAN.md) 的第四阶段计划。

## 1. 阶段目标

第四阶段负责在不触碰生产资源的前提下，验证第二、三阶段的邮箱认证能力，并完成真实 SES 发信入口和部署前检查：

1. 使用一次性临时 PostgreSQL 数据库运行 Alembic 迁移和邮箱认证集成测试；
2. 使用一次性临时 Redis 容器验证 TTL、限流、Lua 原子消费、Session 撤销和 Refresh Token 状态；
3. 保持普通单元测试、CI 和集成 Provider stub 不调用腾讯云网络；
4. 通过显式开关执行 SES 资源预检和一次真实发信；
5. 校验部署 workflow、环境变量、nginx 代理链和生产迁移 workflow；
6. 生成可审计的计划与执行记录，不保存 Secret、完整收件人、验证码或 Token。

## 2. 隔离测试设计

### 2.1 PostgreSQL

`scripts/validate_email_registration_phase_4.py` 默认仅允许连接本机 PostgreSQL 管理库，创建随机名称的临时数据库，执行：

```text
alembic upgrade head
alembic check
pytest tests/test_phase_4_email_integration.py -q
```

验证完成后通过终止活动连接和 `DROP DATABASE` 清理。不得对开发数据库执行清库或回滚。

### 2.2 Redis

验证脚本启动随机名称的 `redis:7-alpine` 容器，仅绑定 `127.0.0.1` 的随机宿主端口。集成测试通过独立 key prefix 连接该 Redis，验证：

- Challenge TTL 和过期策略；
- Redis 仅保存邮箱 Hash、验证码 Hash，不保存明文邮箱和验证码；
- 注册与密码重置用途隔离；
- 邮箱/IP 发送限流；
- 错误次数和达到上限后的删除；
- Lua 原子消费在并发竞争下最多成功一次；
- 密码重置后的 Session 撤销 key 和 TTL；
- Refresh Token 相关 Redis 状态。

测试结束在 `finally` 中强制删除临时容器，不使用 `FLUSHDB`/`FLUSHALL` 清理共享 Redis。

## 3. 普通测试与真实 SES 门控

普通 `pytest` 使用 Provider stub 或 SDK mock，不调用 Tencent Cloud。真实 SES 只允许以下两个条件同时满足时运行：

```text
RUN_PHASE4_REAL_SES=1
PHASE4_SES_RECIPIENT=<受控收件箱环境变量>
```

收件人只能从 `PHASE4_SES_RECIPIENT` 读取，不硬编码、不写入仓库或执行文档。变量缺失时在发送前安全失败，不猜测收件人。

真实发信前检查：

- Region 为 `ap-guangzhou`；
- endpoint 为 `ses.tencentcloudapi.com`；
- 发件地址、显示名和模板 ID 分别为既定配置；
- SES identity 已验证并具备发信状态；
- DNS/DKIM 等身份属性可用；
- 发件地址可用；
- 模板已审核并包含 `code`、`expire_minutes` 变量；
- 日配额可用。

预检通过后只发送一次随机六位验证码，不自动重试；输出仅包含脱敏收件人、截断后的 RequestId/MessageId 和安全状态。若服务端提供可查询 MessageId，则只查询一次发送状态。

## 4. 部署检查

静态检查覆盖：

- deploy workflow 从 GitHub Environment Secrets 注入 CAM Secret；不在日志中 echo Secret；生成环境文件使用 `umask 077`，上传后远端 `chmod 600`，本地立即删除；
- SES Region、endpoint、发件配置、验证码策略、Redis key namespace 和 `TRUSTED_PROXY_IPS` 变量名称一致；
- docker compose 使用上传的 `.env` 和外部 backend 网络；
- nginx 仅转发代理链，应用层按受信代理配置决定是否信任 `X-Forwarded-For`；
- migrate workflow 要求显式 revision，product 迁移要求备份确认，并执行 `current → upgrade → current`；
- workflow 不执行生产 downgrade，不在 CI 中启用真实 SES。

生产策略为 forward-fix：生产数据库只执行向前兼容迁移；不可变镜像可以回滚，但旧镜像启动前必须确认其 schema 兼容性。第四阶段不执行生产数据库、Redis、DNS、CAM、nginx 或发布操作。

## 5. 验收命令

```bash
pdm run pytest tests/test_tencent_ses_service.py tests/test_verification_challenge_service.py \
  tests/test_email_auth_service.py tests/test_email_registration_api.py \
  tests/test_email_registration_migration.py tests/test_auth_service.py \
  tests/test_phase_4_deployment_checks.py
pdm run ruff check .
pdm run pytest
pdm lock --check
git diff --check

RUN_PHASE4_EMAIL_INTEGRATION=1 \
  pdm run python scripts/validate_email_registration_phase_4.py
```

真实 SES 仅在用户已经设置 gate 和收件人环境变量时执行：

```bash
RUN_PHASE4_EMAIL_INTEGRATION=1 \
RUN_PHASE4_REAL_SES=1 \
PHASE4_SES_RECIPIENT='<环境变量提供的受控收件箱>' \
  pdm run python scripts/validate_email_registration_phase_4.py
```

执行记录不得复制真实收件人值。
