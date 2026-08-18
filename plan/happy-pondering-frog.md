# Context

首次部署需要先准备 Docker 网络和 PostgreSQL/Redis，再由现有 `.github/workflows/deploy.yml` 完成 API 镜像构建、TCR 归档和应用发布。用户确认不需要 product Environment 的 Required reviewers，且 `init.yml` 与 `deploy.yml` 不建立触发或状态文件关联：`init.yml` 只负责基础设施初始化，API 发布和数据库引导全部由 Deploy 完成。

用户进一步确认：每次**正常发布**都固定执行 Alembic migration、seed 和 permission synchronization；历史镜像回滚不执行这些操作。seed 的管理员账号和密码不再硬编码，改从对应 GitHub Environment 的 Secrets 注入。

# 推荐实现

## 1. 新增独立的 `init.yml`

新增 `.github/workflows/init.yml`，仅支持 `workflow_dispatch`，不调用、不触发 `deploy.yml`，也不写 `.initialized` 或其他供 Deploy 读取的标记。

输入和行为：

- `environment`：`test` / `product`；Job 绑定该 Environment，但不依赖 Required reviewers；
- `confirmation`：要求输入 `INITIALIZE-test` 或 `INITIALIZE-product`，避免误操作；
- 使用对应 Environment 的 `DEPLOY_HOST/PORT/USER/PATH`、`DOCKER_NETWORK_NAME`、基础设施 Variables，以及 `POSTGRES_PASSWORD` Secret；
- 从仓库 checkout `docker-compose.infra.yml`，在服务器部署目录单独维护 `.env.infra`（权限 `0600`），不覆盖应用运行时 `.env`；
- 幂等创建外部 Docker 网络，执行 `docker compose --env-file .env.infra -f docker-compose.infra.yml up -d postgres redis`，然后用 `pg_isready` 和 `redis-cli ping` 重试确认基础设施可用；
- 不执行 `docker compose down -v`、删除 volume、`docker system prune`、系统安装、密钥生成或 API/nginx 启动；
- 基础设施已经存在时安全复用，失败后可重新运行；完成后不自动启动 Deploy。

`init.yml` 的职责边界为：

```text
init.yml = Docker 网络 + PostgreSQL + Redis 初始化/检查
 deploy.yml = API 构建、迁移、seed、权限同步、API/nginx 发布
```

不配置 product Required reviewers；如需生产审批，属于仓库/运维流程另行处理，不作为本次 Workflow 依赖。

## 2. 将正常发布的数据库引导加入 `deploy.yml`

修改 `.github/workflows/deploy.yml`，保持现有 tag 解析、服务器端 Git checkout/build/push、TCR、配置上传、健康检查和回滚逻辑。

在以下阶段插入固定引导步骤：

```text
prepare-image：服务器拉取固定 tag、校验 commit、构建并推送镜像
  ↓
deploy：上传运行时 .env / Compose / nginx，确认外部网络
  ↓
正常发布：alembic current → upgrade head → current
  ↓
正常发布：seed
  ↓
正常发布：permission-sync --dry-run → apply → --check
  ↓
正常发布：docker compose up -d --no-build api nginx
  ↓
健康检查、smoke test、公网检查
```

具体规则：

- 仅当 `SHOULD_BUILD=true`（tag 正常发布）执行三类引导；`workflow_dispatch` 历史镜像回滚只执行 TCR pull 和容器更新，不执行 migration、seed 或 permission-sync；
- 引导使用刚刚构建的目标 API 镜像和已上传的 `.env`，所有命令加 `--no-deps`，不提前启动 API 容器；
- migration 固定执行 `alembic current`、`alembic upgrade head`、`alembic current`；
- seed 固定执行 `python -m app.seed`；
- permission synchronization 固定依次执行 `python -m app.commands.sync_permissions --dry-run`、无参数写同步、`--check`；失败立即停止，不切换 API 容器；
- migration、seed、permission-sync 都设计为可重试；`PermissionSyncService` 现有 PostgreSQL advisory lock 和命令层事务逻辑继续复用；
- 只有全部引导成功后才执行 `docker compose up`，确保初始化失败不会停止当前运行版本或切换到未完成版本。

## 3. 将 seed 凭据改为 Environment Secrets

修改 `app/seed/__main__.py`：

- 删除硬编码的管理员密码，并不再依赖固定默认管理员账号；
- 从 `SEED_ADMIN_EMAIL`、`SEED_ADMIN_PASSWORD` 读取凭据；两项缺失或为空时立即失败，且错误和日志不得输出密码；
- 将凭据作为 `seed(...)` 的显式参数传入 `ensure_admin_user(...)`，保留幂等行为：账号已经存在时只跳过创建，不在每次发布中重置其密码；
- 保留 `DEFAULT_ROLE = "admin"` 及现有角色/用户角色关联逻辑；
- 更新 `tests/test_seed.py`、`tests/test_permission_sync_concurrency.py` 等直接调用 `seed()` 的测试，使用显式测试凭据，不在生产代码恢复硬编码密码。

在 `.github/workflows/deploy.yml` 的对应 Environment Secrets 中新增并强制校验：

```text
SEED_ADMIN_EMAIL
SEED_ADMIN_PASSWORD
```

建议使用 `secrets` 而不是 `vars`，密码不写入提交的配置文件、镜像、日志或长期运行时 `.env`。Deploy 将 Secret 仅注入一次性 seed 命令（可通过权限 `0600` 的临时远端文件/环境传递），seed 命令完成后清理临时凭据；API/nginx 常驻容器不需要这两个变量。

## 4. 文档与配置更新

更新：

- `.env.deploy.example`：注明 `SEED_ADMIN_EMAIL`、`SEED_ADMIN_PASSWORD` 是 GitHub Environment Secrets，仅供正常 Deploy 的 seed 命令使用；补充 `init.yml` 所需的基础设施变量说明，不写入真实值；
- `docs/GITHUB_AUTO_DEPLOYMENT.md`：说明 `init.yml` 与 Deploy 独立、首次流程为先 Init 再推送 tag、product 不需要 Required reviewers、每次正常 Deploy 固定执行 migration/seed/permission-sync、回滚不执行这三项，以及 seed Secret 的配置方式；
- `README.md`：删除“生产不自动 seed/sync”与新行为冲突的表述，改为说明普通正常发布固定执行，历史回滚不执行；更新 seed 账号由 Environment Secret 提供；
- `plan/SERVER_LOCAL_BUILD_TCR_FIRST_RELEASE_RUNBOOK.md`：把首次发布顺序改为 `init.yml → 推送 tag → Deploy 在启动前执行 migration/seed/permission-sync`；移除先让 API 失败再运行 Migrate 的旧建议；
- `plan/SERVER_LOCAL_BUILD_TCR_DEPLOYMENT_EXECUTION.md`：补充独立 Init、正常发布引导和 seed Secrets 的最终实施边界。

`migrate.yml` 保持独立的人工 migration 工具，不作为 Deploy 的前置 Workflow，也不被 Init 调用。

## 5. 测试和验证

扩展 `tests/test_phase_4_deployment_checks.py` 或新增部署 Workflow 检查测试，覆盖：

- `init.yml` 只有 `workflow_dispatch`，不触发/调用 Deploy，包含 Docker 网络和 PostgreSQL/Redis 启动检查；
- Init 不包含 `down -v`、volume 删除或 prune；
- Deploy 正常路径的顺序为 `alembic current → upgrade head → current → seed → permission dry-run/apply/check → compose up`；
- 引导步骤受 `SHOULD_BUILD=true` 保护，回滚路径不包含引导命令；
- Workflow 从 `secrets.SEED_ADMIN_EMAIL`、`secrets.SEED_ADMIN_PASSWORD` 注入，并检查缺失即失败；
- 不把 seed 密码写入常驻 `.env`、Dockerfile、日志或镜像 label；
- seed 命令不再硬编码密码，现有 seed 和 permission-sync 测试继续通过。

本地验证：

1. 解析所有 Workflow YAML，并对新增/修改的 Bash `run` block 执行 `bash -n`。
2. 运行 `pdm run lint`。
3. 运行 `pdm run pytest tests/test_seed.py tests/test_sync_permissions_command.py tests/test_phase_4_deployment_checks.py -q`。
4. 运行 `git diff --check`，确认没有 `.env`、私钥、token 或真实密码进入变更。
5. 外部验收：先手动运行 Init，确认 PostgreSQL/Redis/network；再推送一个 test tag，确认服务器在 API 启动前完成 migration、seed、permission-sync；随后普通 tag 发布确认三项仍幂等成功；最后用 `workflow_dispatch` 回滚历史 tag，确认不会执行三项数据库引导。

# 关键文件

- `.github/workflows/init.yml`：独立的首次基础设施初始化。
- `.github/workflows/deploy.yml`：正常发布的 migration、seed、permission-sync 与 API/nginx 部署；回滚保持独立路径。
- `app/seed/__main__.py`：Environment Secret 驱动的幂等管理员 seed。
- `app/commands/sync_permissions.py`、`app/services/permission_sync_service.py`：复用现有权限扫描、事务和 advisory lock。
- `docker-compose.infra.yml`、`docker-compose.deploy.yml`：基础设施与目标 API 一次性命令/部署。
- `tests/test_seed.py`、`tests/test_sync_permissions_command.py`、`tests/test_phase_4_deployment_checks.py`：业务与 Workflow 回归测试。
