from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.user import User
from app.models.user_identity import UserIdentity


def test_qq_identity_schema_allows_qq_only_user() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = User(email=None, hashed_password=None, display_name="QQ user")
        db.add(user)
        db.flush()
        db.add(
            UserIdentity(
                user_id=user.id,
                provider="qq",
                provider_subject="openid-123",
                display_name="QQ user",
                avatar="https://example.test/avatar.png",
                verified=True,
            )
        )
        db.commit()

        identity = db.query(UserIdentity).one()
        assert identity.user_id == user.id
        assert identity.provider == "qq"
        assert identity.provider_subject == "openid-123"
        assert identity.verified is True


def test_qq_identity_has_provider_subject_unique_constraint() -> None:
    constraints = UserIdentity.__table__.constraints

    assert any(
        constraint.name == "uq_user_identities_provider_provider_subject"
        and [column.name for column in constraint.columns] == ["provider", "provider_subject"]
        for constraint in constraints
    )
