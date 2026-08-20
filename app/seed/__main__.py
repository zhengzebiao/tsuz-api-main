import logging
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.logging import configure_logging
from app.core.security import hash_password
from app.models.role import Role, user_roles
from app.models.user import User

logger = logging.getLogger(__name__)

DEFAULT_ROLE = "admin"
NORMAL_ROLE = "normal"


def get_seed_credentials() -> tuple[str, str]:
    email = os.getenv("SEED_ADMIN_EMAIL", "").strip()
    password = os.getenv("SEED_ADMIN_PASSWORD", "")
    if not email:
        raise RuntimeError("SEED_ADMIN_EMAIL is required")
    if not password:
        raise RuntimeError("SEED_ADMIN_PASSWORD is required")
    return email, password


def ensure_admin_user(db: Session, *, email: str, password: str) -> User:
    user = db.scalar(select(User).where(User.email == email))
    if user is not None:
        logger.info("seed skipped existing admin user email=%s", email)
        return user
    user = User(email=email, hashed_password=hash_password(password), is_active=True)
    db.add(user)
    db.flush()
    logger.info("seed created admin user email=%s", email)
    return user


def ensure_role(db: Session, name: str) -> Role:
    role = db.scalar(select(Role).where(Role.name == name))
    if role is not None:
        logger.info("seed skipped existing role name=%s", name)
        return role
    role = Role(name=name)
    db.add(role)
    db.flush()
    logger.info("seed created role name=%s", name)
    return role


def ensure_user_role(db: Session, user: User, role: Role) -> None:
    exists = db.execute(
        select(user_roles.c.user_id).where(
            user_roles.c.user_id == user.id,
            user_roles.c.role_id == role.id,
        )
    ).first()
    if exists is not None:
        logger.info("seed skipped existing user role user_id=%s role=%s", user.id, role.name)
        return
    db.execute(user_roles.insert().values(user_id=user.id, role_id=role.id))
    logger.info("seed attached role user_id=%s role=%s", user.id, role.name)


def seed(db: Session, *, email: str, password: str) -> None:
    logger.info("seed started target=python-main")
    admin = ensure_admin_user(db, email=email, password=password)
    admin_role = ensure_role(db, DEFAULT_ROLE)
    ensure_user_role(db, admin, admin_role)
    ensure_role(db, NORMAL_ROLE)
    logger.info("seed completed target=python-main")


def main() -> None:
    configure_logging()
    email, password = get_seed_credentials()
    db = SessionLocal()
    try:
        seed(db, email=email, password=password)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("seed failed target=python-main")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
