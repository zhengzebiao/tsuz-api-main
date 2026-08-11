# 用户管理第三阶段执行计划：用户管理接口

## 背景与范围

第三阶段在已有用户管理模型、审计模型、统一认证授权依赖和 Session 撤销能力之上，实现 `/admin/users` 用户管理接口。

本阶段已实现：

- 用户列表和详情。
- 新增用户。
- 编辑邮箱和显示名称。
- 禁用、启用用户。
- 拉黑、恢复用户。
- 管理员重置密码。
- 强制撤销用户全部活动 Session。

本阶段未实现：

- 删除用户。
- 角色分配、权限管理。
- 组织部门、MFA、设备和登录记录。
- 审计查询接口。
- 第四阶段本地 PostgreSQL/Redis 完整流程验证。
- 新增数据库迁移。

## API 与权限

| 方法 | 路径 | 权限 |
| --- | --- | --- |
| GET | `/admin/users` | `user:read` |
| GET | `/admin/users/{user_id}` | `user:read` |
| POST | `/admin/users` | `user:create` |
| PATCH | `/admin/users/{user_id}` | `user:update` |
| POST | `/admin/users/{user_id}/disable` | `user:disable` |
| POST | `/admin/users/{user_id}/enable` | `user:enable` |
| POST | `/admin/users/{user_id}/blacklist` | `user:blacklist` |
| POST | `/admin/users/{user_id}/recover` | `user:recover` |
| POST | `/admin/users/{user_id}/reset-password` | `user:reset_password` |
| POST | `/admin/users/{user_id}/force-logout` | `user:force_logout` |

所有管理接口复用 `require_permissions()`。认证失败仍返回 401，Token 有效但权限不足返回 403。

## 业务和并发规则

- 管理请求 Schema 使用 `extra="forbid"`，客户端不能传入角色、权限、状态字段、密码哈希或其他未允许字段。
- 新增用户会标准化邮箱、校验 10～128 字符密码、使用 bcrypt 哈希，并依赖邮箱唯一索引防止并发重复创建。
- 列表支持分页、关键字、启用状态和拉黑状态筛选；关键字匹配邮箱和显示名称，响应不包含密码哈希。
- 资料编辑使用 `version` 乐观锁；没有实际变化时不增加版本。邮箱变化会撤销全部 Session。
- 禁用、启用、拉黑、恢复、重置密码和强制下线使用目标用户行锁。
- 禁用和拉黑拒绝管理员自操作，并保护最后一个有效管理员；状态动作具备幂等语义，不覆盖首次原因和时间。
- 禁用、拉黑、邮箱修改和密码重置会撤销用户全部活动 Session；启用和恢复不恢复旧 Session。
- 重置密码在进入锁事务前完成密码策略校验和哈希计算。
- 所有写操作写入 `audit_events`，记录操作人、目标、动作、结果、原因、Request ID 和非敏感变化；密码、密码哈希和 Token 不进入审计。

## 验证结论

第三阶段自动化验证已完成：

- `pdm run lint`：通过，Ruff 未发现问题。
- 第三阶段新增服务和 API 测试：14 项通过。
- `pdm run test`：通过，共收集并通过 78 项测试。
- 测试仅使用内存 SQLite 和 FakeRedis，没有连接开发或生产数据库。
- 测试仍有 1 条来自 FastAPI/Starlette `TestClient` 依赖的弃用警告，与本阶段业务代码无关。
