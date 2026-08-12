import hashlib
import hmac
from secrets import token_hex, token_urlsafe

from passlib.context import CryptContext

password_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def parse_pem_key(value: str) -> str:
    return value.replace("\\n", "\n").strip()


def hash_password(password: str) -> str:
    return password_context.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    return password_context.verify(password, hashed_password)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def generate_app_id() -> str:
    return f"app_{token_hex(16)}"


def generate_app_secret() -> str:
    return f"app_secret_{token_urlsafe(32)}"


def hash_app_secret(app_secret: str) -> str:
    return sha256_text(app_secret)


def verify_app_secret(app_secret: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_app_secret(app_secret), expected_hash)
