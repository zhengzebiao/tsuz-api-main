# `app` 目录说明

`app/` 是项目的核心应用代码目录，各子目录职责大致如下。

## `api/`

定义对外提供的 HTTP API 路由。

- `auth.py`：登录、刷新 Token、注销、当前用户信息。
- `admin_users.py`：管理员对用户的创建、修改、禁用、拉黑、重置密码等操作。
- `dependencies.py`：API 依赖项，例如获取当前用户、校验 JWT 和权限。
- `health.py`：健康检查接口，例如 `/health`。

## `core/`

存放基础设施和全局配置，不承载具体业务流程。

- `config.py`：读取 `.env` 和应用配置。
- `database.py`：SQLAlchemy 数据库连接、Session 和模型基类。
- `redis.py`：Redis 连接。
- `security.py`：密码哈希、JWT 密钥处理等安全工具。
- `logging.py`：日志配置和请求 ID 中间件。

## `models/`

定义数据库模型，描述数据库表结构。

- `user.py`：用户表。
- `role.py`、`permission.py`：角色和权限。
- `session.py`：登录会话。
- `audit_event.py`：审计事件。

## `schemas/`

定义 API 请求和响应的数据结构，主要使用 Pydantic 完成数据校验。

- `auth.py`：登录请求、Token 响应、用户响应等。
- `admin_user.py`：管理员用户管理相关的请求和响应。

可以简单理解为：`models` 面向数据库，`schemas` 面向 API 数据校验。

## `services/`

实现业务逻辑。

- `auth_service.py`：登录、刷新 Token、注销、获取当前用户。
- `token_service.py`：生成和校验 JWT。
- `refresh_token_service.py`：刷新 Token 的保存、轮换和复用检测。
- `session_service.py`：会话管理。
- `blacklist_service.py`：Token 黑名单。
- `authorization_service.py`：认证和权限判断。
- `admin_user_service.py`：管理员用户管理业务逻辑。

## `seed/`

用于初始化基础数据，不是普通 API 路由。

- 创建默认管理员、角色和权限。
- 设计为幂等，多次执行不会重复创建数据。
- 可以通过 `python -m app.seed` 或 `pdm run seed` 执行。

## `main.py`

应用入口，负责：

- 创建 FastAPI 实例。
- 注册中间件。
- 注册各个路由。
- 配置 Swagger、ReDoc、CORS 和日志。

## 整体调用关系

```text
HTTP 请求
   ↓
api 路由
   ↓
schemas 校验输入输出
   ↓
services 执行业务逻辑
   ↓
models 访问 PostgreSQL
   ↓
core 提供数据库、Redis、安全和配置能力
```

例如登录流程：

```text
POST /auth/login
  → app/api/auth.py
  → app/schemas/auth.py 校验参数
  → app/services/auth_service.py
  → app/models/user.py 查询用户
  → app/core/security.py 校验密码
  → app/services/token_service.py 生成 JWT
```

这种分层方式将路由、业务逻辑、数据库结构和基础设施彼此分离，便于测试和维护。
