# 邮箱注册、登录与密码找回：第一阶段开发执行记录

## 1. 执行范围

本次根据 [EMAIL_REGISTRATION_SES_IMPLEMENTATION_PLAN.md](EMAIL_REGISTRATION_SES_IMPLEMENTATION_PLAN.md) 完成第一阶段“邮件配置、用户模型与数据库迁移”。

本阶段已完成：

1. 腾讯云 SES 官方 Python SDK 依赖和锁文件；
2. CAM 子用户长期凭证、SES 和验证码策略配置；
3. 用户邮箱验证时间字段；
4. `0006_email_registration` Alembic 迁移；
5. 历史用户邮箱验证时间回填；
6. 配置、模型和迁移验证测试；
7. 既有迁移验证脚本及集成测试的当前 head 更新；
8. 目标数据库 `normal` 角色和有效权限只读核对。

本阶段明确未实现：

- SES `SendEmail` 业务发送服务；
- Redis Challenge、验证码 Hash、TTL、限流和原子消费；
- 邮箱注册、邮箱登录和密码找回 API；
- 新用户注册事务、角色绑定和自动登录；
- 前端页面或注册链接；
- STS 临时凭证、`TENCENTCLOUD_SESSION_TOKEN` 和 `X-TC-Token`。

## 2. 配置和依赖变更

### 2.1 运行时依赖

在 `pyproject.toml` 增加：

```text
tencentcloud-sdk-python>=3.0.0
```

已刷新 `pdm.lock`，并验证以下 SDK 模块可以导入：

```python
from tencentcloud.ses.v20201002 import ses_client, models
```

### 2.2 应用配置

在 `app/core/config.py` 增加以下配置：

| 配置项 | 值 |
| --- | --- |
| `TENCENTCLOUD_SECRET_ID` | 默认空，通过运行环境 Secret 注入 |
| `TENCENTCLOUD_SECRET_KEY` | 默认空，通过运行环境 Secret 注入 |
| `TENCENTCLOUD_REGION` | `ap-guangzhou` |
| `TENCENTCLOUD_SES_ENDPOINT` | `ses.tencentcloudapi.com` |
| `EMAIL_FROM_ADDRESS` | `noreply@notify.tusz.online` |
| `EMAIL_FROM_NAME` | `tusz.online` |
| `EMAIL_TEMPLATE_ID` | `57044` |
| `EMAIL_SUBJECT` | `邮箱验证码` |
| `EMAIL_CODE_EXPIRE_MINUTES` | `10` |
| `EMAIL_CODE_LENGTH` | `6` |
| `EMAIL_CODE_MAX_ATTEMPTS` | `5` |
| `EMAIL_CODE_RESEND_INTERVAL_SECONDS` | `60` |
| `EMAIL_API_TIMEOUT_SECONDS` | `10` |

同步更新：

- `.env.local.example`
- `.env.test.example`
- `.env.product.example`
- `.env.deploy.example`

示例文件只包含占位凭证。真实 `.env` 中的 `SecretId` 和 `SecretKey` 未读取、未输出、未写入文档或提交到 Git。

当前认证方案为 CAM 子用户长期凭证。应用不配置或使用 STS 临时凭证和 `X-TC-Token`。

## 3. 用户模型和迁移

### 3.1 ORM 字段

在 `app/models/user.py` 增加：

```python
email_verified_at: Mapped[datetime | None] = mapped_column(DateTime)
```

字段可空且无数据库默认值：

- 历史账号由迁移回填为 `created_at`；
- 新建 ORM 用户默认保持 `NULL`；
- 后续邮箱验证码注册成功时显式写入当前时间。

现有 `users.email`、`users.hashed_password`、邮箱密码登录和认证流程未改变。

### 3.2 Alembic 迁移

新增：

```text
alembic/versions/0006_email_registration.py
```

迁移链：

```text
0005_permission_management
  → 0006_email_registration
```

Upgrade：

```sql
ALTER TABLE users
ADD COLUMN email_verified_at TIMESTAMP NULL;

UPDATE users
SET email_verified_at = created_at
WHERE email IS NOT NULL
  AND email_verified_at IS NULL;
```

Downgrade 只删除 `email_verified_at`，不删除或修改用户、角色、权限及关联数据。

## 4. 角色和权限核对

对目标数据库执行只读核对：

```text
NORMAL_ROLE_COUNT=1
NORMAL_ROLE_ENABLED=True
NORMAL_EFFECTIVE_PERMISSION_COUNT=3
NORMAL_EFFECTIVE_PERMISSIONS=app:create,app:disable,app:enable
```

有效权限按照现有认证 Claim 逻辑核对：

- `Role.is_enabled = true`；
- `Permission.is_enabled = true`；
- `Permission.is_declared = true`。

未创建、启用、禁用或重新分配任何角色和权限。

## 5. 测试和验证

### 5.1 新增测试

```text
tests/test_email_registration_config.py
tests/test_email_registration_migration.py
```

更新：

```text
tests/test_user_management_models.py
tests/test_permission_management_migration.py
```

迁移 head 由 `0005_permission_management` 更新为 `0006_email_registration` 的验证入口包括：

- `scripts/validate_phase_4.py`
- `scripts/validate_app_phase_5.py`
- `scripts/validate_role_management_phase_5.py`
- `scripts/validate_permission_management_phase_5.py`
- 对应 phase 集成测试。

### 5.2 已执行结果

依赖和 SDK 导入：

```text
pdm lock --check：通过
Tencent SES SDK import：通过
```

定向测试：

```text
23 passed, 1 skipped
```

完整测试：

```text
242 passed, 14 skipped, 1 warning
```

完整 lint：

```text
pdm run ruff check .
All checks passed!
```

临时 PostgreSQL 迁移往返：

```text
1 passed
```

覆盖：

- `0005 -> 0006` upgrade；
- 历史用户 `email_verified_at` 回填；
- 新用户 `email_verified_at` 可为 `NULL`；
- `normal` 角色启用状态和有效权限保留；
- `0006 -> 0005` downgrade；
- 再次升级到 head；
- `alembic check` clean。

其他检查：

```text
git diff --check：通过
```

唯一测试警告为项目既有的 Starlette TestClient/httpx 弃用提示，不影响结果。

## 6. 文件变更摘要

新增：

```text
alembic/versions/0006_email_registration.py
plan/EMAIL_REGISTRATION_SES_IMPLEMENTATION_PLAN.md
plan/EMAIL_REGISTRATION_SES_IMPLEMENTATION_EXECUTION.md
tests/test_email_registration_config.py
tests/test_email_registration_migration.py
```

修改：

```text
.env.deploy.example
.env.local.example
.env.product.example
.env.test.example
app/core/config.py
app/models/user.py
pdm.lock
pyproject.toml
scripts/validate_app_phase_5.py
scripts/validate_permission_management_phase_5.py
scripts/validate_phase_4.py
scripts/validate_role_management_phase_5.py
tests/test_app_phase_5_integration.py
tests/test_permission_management_migration.py
tests/test_permission_management_phase_5_integration.py
tests/test_phase_4_integration.py
tests/test_role_management_phase_5_integration.py
tests/test_user_management_models.py
```

原方案文档仍保留在 `docs/EMAIL_REGISTRATION_SES_IMPLEMENTATION_PLAN.md`，本目录新增同内容计划文档和本执行记录，便于后续按阶段继续实施。

## 7. 第一阶段结论

第一阶段验收通过：

- SES SDK 依赖已锁定并可导入；
- CAM/SES/验证码配置已具备；
- 用户模型与 Alembic 迁移一致；
- 历史用户数据未丢失且已回填邮箱验证时间；
- `normal` 角色存在、启用并保留现有有效权限；
- 既有邮箱密码登录回归通过；
- 完整测试和 lint 通过；
- 未提前实现第二阶段及后续业务功能。

后续应从方案第二阶段开始，实现 SES Provider、Redis Challenge 和统一认证基础能力。
