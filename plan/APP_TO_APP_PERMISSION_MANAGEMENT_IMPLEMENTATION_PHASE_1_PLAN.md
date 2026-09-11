# 应用间权限管理：第一阶段“最小只读闭环”实现计划

> 状态：部分完成（代码和默认自动化验证已完成；隔离跨服务 smoke 待专用资源与显式授权）
>
> 总实施方案：[APP_TO_APP_PERMISSION_MANAGEMENT_IMPLEMENTATION_PLAN.md](APP_TO_APP_PERMISSION_MANAGEMENT_IMPLEMENTATION_PLAN.md)
>
> JCC 设计基准：[APP_TO_APP_PERMISSION_MANAGEMENT.md](../../tsuz-api-jcc/docs/APP_TO_APP_PERMISSION_MANAGEMENT.md)

## 1. 阶段目标

在不改变用户 Token、Session、blacklist 和现有 App 管理语义的前提下，建立以下最小闭环：

```text
main App → JCC App → jcc:record:read
JCC App → main App → main:application:read
```

## 2. 前置依赖

- main 现有 `POST /admin/apps`、用户权限依赖、AuditEvent 和 RSA 私钥；
- JCC 现有 `SampleProfile`、数据库依赖、用户 JWT 资源 API；
- 部署环境通过 Secret Store 注入真实 App ID/Secret 和 Service Token 公钥；
- 隔离 smoke 所需的专用 PostgreSQL 管理连接、两个 Redis namespace/实例和随机端口。

## 3. 开发内容

1. 新增 `resource_scopes`、`app_service_grants` 和 `0007_app_service_authorization`；
2. 新增 `/admin/resource-scopes`、`/admin/service-grants`，声明七项用户管理权限并写入用户 Actor 审计；
3. 新增 `/internal/oauth/token`，完成 HTTP Basic、严格表单、Grant 子集、300 秒 RS256 Service Token、`no-store` 和安全错误；
4. 新增 main/JCC 独立 Service Auth；
5. 新增 main `/internal/v1/applications/{app_id}`、JCC `/internal/v1/records`；
6. 新增 `JccClient` 与 `MainClient`，显式 timeout 和内存 Token cache；
7. 补充配置示例、README、日志 Basic 脱敏、迁移 head/权限目录断言和自动化测试；
8. 先运行定向测试，再运行两端全量测试、lint、lock check、diff check 和隔离 smoke。

## 4. 本阶段不实现

- 应用间写接口、幂等键、限流、mTLS 和应用 Actor 审计；
- 多 Secret、无停机轮换、JWKS、Introspection、即时撤销和 Refresh Token；
- 通过 Seed/迁移/脚本创建长期 App 凭证；
- 生产数据库迁移、部署和真实长期 Secret 注入。

## 5. 实施步骤与验收标准

### 5.1 数据与管理 API

- 目标 App、Scope code、caller、时间窗口和重复状态校验严格；
- Grant target 由 Scope 推导；撤销不静默恢复；
- 管理操作与 AuditEvent 在同一事务；
- 验收证据：`tests/test_app_to_app_models.py`、`tests/test_service_authorization_api.py`。

### 5.2 Token 与资源端

- 正确凭证可以申请固定 300 秒 Token；错误 Secret、禁用 caller/target、撤销/过期 Grant、未授权 scope fail closed；
- main/JCC 独立验证 issuer、严格 audience、`token_use=service`、时间、必需 claim 类型和接口 scope；
- 用户 JWT 不能调用内部 API；内部响应不含 Secret/Hash；
- 验收证据：`tests/test_service_token.py`、JCC `tests/test_internal_api.py`。

### 5.3 Client 与配置

- main Client 使用 main 凭证请求 JCC audience；JCC Client 使用 JCC 凭证请求 main audience；
- Basic 仅出现在 token endpoint，Token cache 不持久化，错误固定且不泄密；
- 配置示例只含占位符，JCC 不接收私钥；
- 验收证据：main `tests/test_jcc_client.py`、JCC `tests/test_main_client.py`、README 与 `.env*.example`。

### 5.4 质量与隔离环境

- 默认自动化命令通过；
- `0006 → 0007 → 0006 → head` 仅在随机临时 PostgreSQL 上执行；
- 双向 HTTP smoke 仅在显式 opt-in、资源隔离和清理守卫通过后执行；
- 未执行的真实验证必须保留“待环境验证”状态。

## 6. 当前实现文件清单

main：`alembic/versions/0007_app_service_authorization.py`、两个模型、三个 Service、四个 API、Service Auth、JCC Client、配置/日志/路由、相关测试与权限/迁移断言。

JCC：Service Auth、内部 records API/schema、Main Client、配置/日志/路由、相关测试与配置/README。

## 7. 质量命令

```bash
# main
cd /Users/zhengzebiao/code/tsuz-api-main
pdm run pytest -q
pdm run ruff check .
pdm lock --check
git diff --check

# JCC
cd /Users/zhengzebiao/code/tsuz-api-jcc
pdm run pytest -q
pdm run ruff check tests/test_internal_api.py tests/test_main_client.py tests/test_logging.py
pdm lock --check
git diff --check
```

JCC 全仓既有 lint 问题不处理，本阶段及后续阶段均忽略；只检查各阶段新增/修改文件。

## 8. 设计调整记录

- 迁移 head 从 `0006_email_registration` 更新为 `0007_app_service_authorization`，同步既有 opt-in migration 验证的 head 断言；
- 权限目录从 26 permissions/33 bindings 更新为 33 permissions/40 bindings；
- Scope/Grant 生命周期审计测试按实际五次状态变更计数；重复幂等和失败请求不产生成功审计；
- 两端 clients 的凭证角色以代码契约为准：main Client 使用 `MAIN_APP_ID/MAIN_APP_SECRET`，JCC Client 使用 `JCC_APP_ID/JCC_APP_SECRET`。
