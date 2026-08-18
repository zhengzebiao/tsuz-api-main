from __future__ import annotations

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
DEPLOY_WORKFLOW = ROOT_DIR / ".github/workflows/deploy.yml"
INIT_WORKFLOW = ROOT_DIR / ".github/workflows/init.yml"
MIGRATE_WORKFLOW = ROOT_DIR / ".github/workflows/migrate.yml"
COMPOSE_FILE = ROOT_DIR / "docker-compose.deploy.yml"
NGINX_CONFIG = ROOT_DIR / "nginx/default.conf"
CI_WORKFLOW = ROOT_DIR / ".github/workflows/ci.yml"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_deploy_workflow_injects_ses_secrets_without_logging_them() -> None:
    workflow = _read(DEPLOY_WORKFLOW)

    assert "TENCENTCLOUD_SECRET_ID: ${{ secrets.TENCENTCLOUD_SECRET_ID }}" in workflow
    assert "TENCENTCLOUD_SECRET_KEY: ${{ secrets.TENCENTCLOUD_SECRET_KEY }}" in workflow
    assert ': "${TENCENTCLOUD_SECRET_ID:?Missing TENCENTCLOUD_SECRET_ID secret}"' in workflow
    assert ': "${TENCENTCLOUD_SECRET_KEY:?Missing TENCENTCLOUD_SECRET_KEY secret}"' in workflow
    assert "umask 077" in workflow
    assert "chmod 600 '$DEPLOY_PATH/.env'" in workflow
    assert "rm -f .env.deploy.generated" in workflow
    assert 'echo "$TENCENTCLOUD_SECRET' not in workflow
    assert "RUN_PHASE4_REAL_SES" not in workflow
    assert "PHASE4_SES_RECIPIENT" not in workflow


def test_init_workflow_is_independent_and_non_destructive() -> None:
    workflow = _read(INIT_WORKFLOW)

    assert "workflow_dispatch:" in workflow
    assert "INITIALIZE-test" in workflow
    assert "INITIALIZE-product" in workflow
    assert "POSTGRES_PASSWORD: ${{ secrets.POSTGRES_PASSWORD }}" in workflow
    assert "docker network inspect" in workflow
    assert "up -d postgres redis" in workflow
    assert "pg_isready" in workflow
    assert "redis-cli ping" in workflow
    assert "deploy.yml" not in workflow
    assert "workflow_call" not in workflow
    assert ".initialized" not in workflow
    assert "down -v" not in workflow
    assert "volume rm" not in workflow
    assert "system prune" not in workflow
    assert "api nginx" not in workflow
    assert "app.seed" not in workflow
    assert "sync_permissions" not in workflow
    assert "alembic" not in workflow
    assert ". ./.env.infra" not in workflow


def test_normal_deploy_bootstrap_is_ordered_and_excludes_rollback() -> None:
    workflow = _read(DEPLOY_WORKFLOW)
    bootstrap_start = workflow.index("- name: Run normal release database bootstrap")
    deploy_start = workflow.index("- name: Deploy with docker compose", bootstrap_start)
    bootstrap = workflow[bootstrap_start:deploy_start]
    rollback = workflow[deploy_start:]

    assert "if: ${{ env.SHOULD_BUILD == 'true' }}" in bootstrap
    assert "SEED_ADMIN_EMAIL: ${{ secrets.SEED_ADMIN_EMAIL }}" in bootstrap
    assert "SEED_ADMIN_PASSWORD: ${{ secrets.SEED_ADMIN_PASSWORD }}" in bootstrap
    assert ': "${SEED_ADMIN_EMAIL:?Missing SEED_ADMIN_EMAIL secret}"' in bootstrap
    assert ': "${SEED_ADMIN_PASSWORD:?Missing SEED_ADMIN_PASSWORD secret}"' in bootstrap
    commands = (
        "api alembic current",
        "api alembic upgrade head",
        "api alembic current",
        "api python -m app.seed",
        "api python -m app.commands.sync_permissions --dry-run",
        "api python -m app.commands.sync_permissions\n",
        "api python -m app.commands.sync_permissions --check",
    )
    positions: list[int] = []
    cursor = 0
    for command in commands:
        position = bootstrap.index(command, cursor)
        positions.append(position)
        cursor = position + len(command)
    assert positions == sorted(positions)
    assert "docker compose --env-file .env -f docker-compose.deploy.yml up" not in bootstrap
    assert "SEED_ADMIN_EMAIL=" not in workflow[workflow.index("cat > .env.deploy.generated") : bootstrap_start]
    assert "SEED_ADMIN_PASSWORD=" not in workflow[workflow.index("cat > .env.deploy.generated") : bootstrap_start]
    assert "printf 'SEED_ADMIN_EMAIL=%q\\n'" in bootstrap
    assert "printf 'SEED_ADMIN_PASSWORD=%q\\n'" in bootstrap
    assert ".env.seed" not in workflow
    assert "alembic" not in rollback
    assert "app.seed" not in rollback
    assert "sync_permissions" not in rollback


def test_deploy_workflow_uses_explicit_compose_project_name() -> None:
    workflow = _read(DEPLOY_WORKFLOW)

    assert "COMPOSE_PROJECT_NAME: ${{ vars.COMPOSE_PROJECT_NAME }}" in workflow
    assert ': "${COMPOSE_PROJECT_NAME:?Missing COMPOSE_PROJECT_NAME environment variable}"' in workflow
    assert '[[ ! "$COMPOSE_PROJECT_NAME" =~ ^[a-z0-9][a-z0-9_-]*$ ]]' in workflow

    compose_commands = [
        line
        for line in workflow.splitlines()
        if "docker compose" in line and "- name:" not in line
    ]
    assert compose_commands
    assert all(
        'docker compose -p "$COMPOSE_PROJECT_NAME"' in line
        or "docker compose -p '$COMPOSE_PROJECT_NAME'" in line
        for line in compose_commands
    )
    assert 'printf \'COMPOSE_PROJECT_NAME=%q\\n\' "$COMPOSE_PROJECT_NAME"' in workflow
    assert "$compose pull api && $compose up -d --no-build api nginx" in workflow


def test_deploy_workflow_writes_isolated_email_namespaces() -> None:
    workflow = _read(DEPLOY_WORKFLOW)

    assert "EMAIL_CHALLENGE_PREFIX=${EMAIL_CHALLENGE_PREFIX:-auth:$DEPLOY_ENV:email:challenge:}" in workflow
    assert "EMAIL_SEND_LIMIT_PREFIX=${EMAIL_SEND_LIMIT_PREFIX:-auth:$DEPLOY_ENV:email:send:}" in workflow
    assert "EMAIL_IP_SEND_LIMIT_PREFIX=${EMAIL_IP_SEND_LIMIT_PREFIX:-auth:$DEPLOY_ENV:email:ip-send:}" in workflow
    assert "TRUSTED_PROXY_IPS=${TRUSTED_PROXY_IPS:-127.0.0.1,::1}" in workflow


def test_deployment_migration_requires_explicit_forward_revision_and_backup() -> None:
    workflow = _read(MIGRATE_WORKFLOW)

    assert "revision:" in workflow
    assert "backup_confirmed:" in workflow
    assert 'if [ "$DEPLOY_ENV" = "product" ] && [ "$BACKUP_CONFIRMED" != "true" ]' in workflow
    assert "alembic current" in workflow
    assert "alembic upgrade '$REVISION'" in workflow
    assert workflow.count("alembic current") >= 2
    assert "downgrade" not in workflow


def test_deploy_compose_uses_generated_environment_and_external_backend() -> None:
    compose = _read(COMPOSE_FILE)

    assert "env_file:" in compose
    assert "- .env" in compose
    assert "external: true" in compose
    assert "name: ${DOCKER_NETWORK_NAME:-product-backend}" in compose


def test_nginx_forwards_client_chain_for_trusted_proxy_validation() -> None:
    nginx = _read(NGINX_CONFIG)

    assert nginx.count("proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;") == 2
    assert "TRUSTED_PROXY_IPS" in nginx
    assert "Authorization" not in nginx


def test_ci_does_not_enable_real_ses_smoke() -> None:
    ci = _read(CI_WORKFLOW)

    assert "RUN_PHASE4_REAL_SES" not in ci
    assert "PHASE4_SES_RECIPIENT" not in ci
    assert "TENCENTCLOUD_SECRET_KEY" not in ci
