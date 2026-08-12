# Context

根据项目根目录的 [SUB_APP_DEVELOPMENT_PLAN.md](../../../../Users/zhengzebiao/code/tsuz-api-main/SUB_APP_DEVELOPMENT_PLAN.md)，现在只实施子应用管理 App 的第一阶段“数据层”：新增 `App` SQLAlchemy 模型、让 Alembic 发现模型、创建 `apps` 表迁移，并验证迁移升级/降级。暂不实现 API、Schema、Service、Secret 生成、权限 Seed 或审计业务流程。

当前项目使用 SQLAlchemy `Base` 统一元数据，模型位于 `app/models/`；Alembic 在 [alembic/env.py](../../../../Users/zhengzebiao/code/tsuz-api-main/alembic/env.py) 中显式导入模型；历史迁移按 `0001`、`0002` 顺序递进，现有时间列使用无时区 `sa.DateTime()`。数据层应遵循这些现有约定，避免仅为新表引入与当前模型/服务不一致的时区行为；文档中的 `DateTime(timezone=True)` 在实际实现中统一落为当前项目使用的 `DateTime()`。

# Requirements and constraints

- 新增 `apps` 表，字段覆盖方案文档中的数据设计：`id`、`app_id`、`app_secret_hash`、`name`、`icon_url`、`access_url`、`service_account_name`、`is_enabled`、`disabled_at`、`disabled_reason`、`secret_updated_at`、`created_at`、`updated_at`、`version`。
- `app_id` 必须非空且唯一；`app_secret_hash` 必须非空，长度适配 SHA-256 十六进制摘要；名称、URL、服务账号字段长度与方案文档一致。
- `is_enabled` 默认启用；`version` 默认 1；可空的禁用元数据使用 `NULL`。
- 为后续列表和状态过滤建立 `app_id` 唯一索引、`name` 索引、`is_enabled` 索引；不要增加 Secret 明文列，也不要在数据库中保存明文 Secret。
- 新迁移的 `down_revision` 为 `0002_user_management`，升级和降级必须可逆，不能改动既有用户、会话和审计表。
- 不修改已有未提交的文档改动，不实现第一阶段之外的功能。

# Implementation plan

1. **新增模型文件**
   - 创建 [app/models/app.py](../../../../Users/zhengzebiao/code/tsuz-api-main/app/models/app.py)，参考 [app/models/user.py](../../../../Users/zhengzebiao/code/tsuz-api-main/app/models/user.py) 的 `Mapped`/`mapped_column` 风格。
   - 定义 `App`，表名为 `apps`，声明表级索引：`ix_apps_name`、`ix_apps_is_enabled`；`app_id` 使用唯一索引或 `unique=True`，并保持迁移生成的索引名称稳定。
   - 时间列沿用现有模型的 `DateTime`/`func.now()` 约定；布尔和版本字段提供 Python 默认值及必要的 server default，保证 ORM 建表测试和真实迁移行为一致。

2. **注册 Alembic 元数据**
   - 修改 [alembic/env.py](../../../../Users/zhengzebiao/code/tsuz-api-main/alembic/env.py)，在现有显式模型导入列表中加入 `app`，确保 `Base.metadata` 包含 `apps` 表。
   - `app/models/__init__.py` 当前为空，不改变其公共导出约定；以 Alembic 显式导入作为本项目当前注册方式。

3. **创建 `0003` 数据库迁移**
   - 新增 [alembic/versions/0003_app_management.py](../../../../Users/zhengzebiao/code/tsuz-api-main/alembic/versions/0003_app_management.py)，遵循 `0002_user_management.py` 的类型标注、revision 注释和 `op.create_table` 风格。
   - `upgrade()` 创建 `apps` 表、唯一 App ID 索引/约束以及名称、启用状态索引；所有必填列设置 `nullable=False`，默认值设置为与模型一致的 server default。
   - `downgrade()` 按索引、约束、表的逆序删除，只删除本迁移创建的对象。
   - 迁移不填充业务 App 数据；App ID、Secret 等由后续 Service 创建时生成。

4. **补充数据层测试**
   - 在现有模型测试模式基础上新增 [tests/test_app_management_models.py](../../../../Users/zhengzebiao/code/tsuz-api-main/tests/test_app_management_models.py)，使用项目已有 SQLite 内存数据库 fixture 方式或复用/扩展 [tests/test_user_management_models.py](../../../../Users/zhengzebiao/code/tsuz-api-main/tests/test_user_management_models.py) 的 `Base.metadata.create_all` 模式。
   - 验证模型默认值（启用、版本号、时间字段）、可空禁用字段、字段持久化和 `app_id` 唯一约束；测试数据只使用伪造的 hash，避免引入尚未实现的 Secret 生成逻辑。
   - 如果新增独立 fixture 会影响现有测试，优先在新测试文件中局部定义，减少非必要范围。

5. **验证迁移状态与可逆性**
   - 运行 `pdm run lint` 和相关模型测试/完整 `pdm run test`。
   - 运行 `pdm run alembic check` 或等价 Alembic 元数据检查，确认导入模型后没有未生成的差异。
   - 在可用的 PostgreSQL 环境中执行 `alembic upgrade 0003_app_management`、检查表/列/索引，再执行 `alembic downgrade 0002_user_management`，确认 `apps` 被移除且既有表和数据不受影响；最后升级到 `head` 并检查当前 revision 为 `0003_app_management`。
   - 若本地 PostgreSQL 不可用，仍执行 SQLite ORM 测试、迁移脚本静态检查和完整测试，并明确记录 PostgreSQL round-trip 未执行；不把仅 SQLite 的结果当作迁移验证的替代品。

# Critical files

- [app/models/app.py](../../../../Users/zhengzebiao/code/tsuz-api-main/app/models/app.py) — 新增 App ORM 模型。
- [alembic/env.py](../../../../Users/zhengzebiao/code/tsuz-api-main/alembic/env.py) — 注册模型元数据。
- [alembic/versions/0003_app_management.py](../../../../Users/zhengzebiao/code/tsuz-api-main/alembic/versions/0003_app_management.py) — 新增数据库迁移。
- [tests/test_app_management_models.py](../../../../Users/zhengzebiao/code/tsuz-api-main/tests/test_app_management_models.py) — 新增数据层测试。
- [app/models/user.py](../../../../Users/zhengzebiao/code/tsuz-api-main/app/models/user.py)、[app/models/session.py](../../../../Users/zhengzebiao/code/tsuz-api-main/app/models/session.py)、[alembic/versions/0002_user_management.py](../../../../Users/zhengzebiao/code/tsuz-api-main/alembic/versions/0002_user_management.py) — 仅作为现有实现模式参考，不应被本阶段改动。

# Verification

- 静态检查：`pdm run lint`。
- 自动化测试：`pdm run test`，至少包含 App 模型默认值、持久化、唯一约束测试。
- Alembic 元数据检查：`pdm run alembic check`。
- PostgreSQL round-trip：升级至 `0003_app_management` → 检查 `apps` 表及全部字段/索引 → 降级至 `0002_user_management` → 确认 `apps` 删除且既有数据保持 → 再升级至 `head`。
- 完成后检查 `git diff`，确认本阶段只包含模型、Alembic 注册、`0003` 迁移和数据层测试，不包含 API 或业务逻辑实现。
