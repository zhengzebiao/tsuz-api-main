import json
import logging
import re
import time
from contextvars import ContextVar
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import settings

request_id_context: ContextVar[str] = ContextVar("request_id", default="")
_original_log_record_factory = logging.getLogRecordFactory()
_redacting_factory_configured = False

_PEM_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*KEY-----.*?-----END [A-Z ]*KEY-----", re.DOTALL)
_BEARER_RE = re.compile(r"(?i)Authorization:\s*Bearer\s+[^\s,;]+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_FIELD_RE = re.compile(
    r"(?i)\b(password|access_token|refresh_token|jwt_private_key|jwt_public_key|github_[a-z0-9_]*secret)"
    r"\s*[=:]\s*([^\s,;]+)"
)
_JSON_FIELD_RE = re.compile(
    r'(?i)("(?:password|access_token|refresh_token|jwt_private_key|jwt_public_key|github_[a-z0-9_]*secret)"\s*:\s*")'
    r'([^"]+)(")'
)
_URL_PASSWORD_RE = re.compile(r"\b((?:postgresql(?:\+psycopg)?|redis)://[^:\s/@]+):[^@\s]+@")


def redact_sensitive(value: object) -> object:
    if not isinstance(value, str):
        return value
    redacted = _PEM_KEY_RE.sub("[REDACTED_KEY]", value)
    redacted = _BEARER_RE.sub("Authorization: Bearer [REDACTED]", redacted)
    redacted = _JWT_RE.sub("[REDACTED_JWT]", redacted)
    redacted = _JSON_FIELD_RE.sub(r"\1[REDACTED]\3", redacted)
    redacted = _FIELD_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
    redacted = _URL_PASSWORD_RE.sub(r"\1:[REDACTED]@", redacted)
    return redacted


def _redacting_log_record_factory(*args, **kwargs) -> logging.LogRecord:
    record = _original_log_record_factory(*args, **kwargs)
    record.msg = redact_sensitive(record.getMessage())
    record.args = ()
    record.request_id = request_id_context.get()
    return record


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
            "service": settings.service_name,
            "env": settings.app_env,
            "request_id": getattr(record, "request_id", request_id_context.get()),
        }
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    global _redacting_factory_configured
    if not _redacting_factory_configured:
        logging.setLogRecordFactory(_redacting_log_record_factory)
        _redacting_factory_configured = True
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter() if settings.log_format == "json" else logging.Formatter("%(levelname)s %(name)s %(message)s")
    )
    logging.basicConfig(level=settings.log_level.upper(), handlers=[handler], force=True)


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get(settings.request_id_header) or str(uuid4())
        token = request_id_context.set(request_id)
        started_at = time.perf_counter()
        try:
            response = await call_next(request)
            response.headers[settings.request_id_header] = request_id
            logging.getLogger("app.request").info(
                "request completed method=%s path=%s status_code=%s duration_ms=%.2f",
                request.method,
                request.url.path,
                response.status_code,
                (time.perf_counter() - started_at) * 1000,
            )
            return response
        finally:
            request_id_context.reset(token)
