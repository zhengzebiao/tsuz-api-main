from __future__ import annotations

from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
DEPLOY_WORKFLOW = ROOT_DIR / ".github/workflows/deploy.yml"
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
    assert ": \"${TENCENTCLOUD_SECRET_ID:?Missing TENCENTCLOUD_SECRET_ID secret}\"" in workflow
    assert ": \"${TENCENTCLOUD_SECRET_KEY:?Missing TENCENTCLOUD_SECRET_KEY secret}\"" in workflow
    assert "umask 077" in workflow
    assert "chmod 600 '$DEPLOY_PATH/.env'" in workflow
    assert "rm -f .env.deploy.generated" in workflow
    assert 'echo "$TENCENTCLOUD_SECRET' not in workflow
    assert "RUN_PHASE4_REAL_SES" not in workflow
    assert "PHASE4_SES_RECIPIENT" not in workflow


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
