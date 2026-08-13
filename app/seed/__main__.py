import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.logging import configure_logging
from app.core.security import hash_password
from app.models.role import Role, user_roles
from app.models.user import User

logger = logging.getLogger(__name__)

DEFAULT_ADMIN_EMAIL = "admin@example.com"
DEFAULT_ADMIN_PASSWORD = "password123"
DEFAULT_ROLE = "admin"


def ensure_admin_user(db: Session) -> User:
    user = db.scalar(select(User).where(User.email == DEFAULT_ADMIN_EMAIL))
    if user is not None:
        logger.info("seed skipped existing admin user email=%s", DEFAULT_ADMIN_EMAIL)
        return user
    user = User(email=DEFAULT_ADMIN_EMAIL, hashed_password=hash_password(DEFAULT_ADMIN_PASSWORD), is_active=True)
    db.add(user)
    db.flush()
    logger.info("seed created admin user email=%s", DEFAULT_ADMIN_EMAIL)
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


def seed(db: Session) -> None:
    logger.info("seed started target=python-main")
    admin = ensure_admin_user(db)
    role = ensure_role(db, DEFAULT_ROLE)
    ensure_user_role(db, admin, role)
    logger.info("seed completed target=python-main")


def main() -> None:
    configure_logging()
    db = SessionLocal()
    try:
        seed(db)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("seed failed target=python-main")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
