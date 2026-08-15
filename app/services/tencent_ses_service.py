from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from tencentcloud.common import credential
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.ses.v20201002 import models, ses_client

from app.core.config import Settings, settings

logger = logging.getLogger("app.auth.email")


class EmailProviderError(RuntimeError):
    """Raised when the configured email provider cannot accept a message."""


# Keep a provider-specific name available to callers that want to distinguish
# SES failures without depending on the SDK exception hierarchy.
TencentSesError = EmailProviderError


@dataclass(frozen=True)
class EmailSendResult:
    message_id: str | None
    request_id: str | None


class TencentSesService:
    """Small boundary around Tencent Cloud SES's SendEmail API."""

    def __init__(self, configured_settings: Settings | None = None, client: Any | None = None) -> None:
        self.settings = configured_settings or settings
        self.client = client if client is not None else self._build_client()

    def send_verification_email(
        self,
        recipient_email: str,
        code: str,
        *,
        purpose: str = "verification",
    ) -> EmailSendResult:
        request = self._build_request(recipient_email, code)
        masked_recipient = self._mask_email(recipient_email)
        try:
            response = self.client.SendEmail(request)
        except Exception as exc:
            # SDK exception text can contain request details. Do not include it
            # in application logs or expose it to an API caller.
            logger.error(
                "email send failed recipient=%s purpose=%s",
                masked_recipient,
                purpose,
            )
            raise EmailProviderError("email provider unavailable") from exc

        message_id = getattr(response, "MessageId", None)
        request_id = getattr(response, "RequestId", None)
        logger.info(
            "email send succeeded recipient=%s purpose=%s request_id=%s",
            masked_recipient,
            purpose,
            request_id or "unknown",
        )
        return EmailSendResult(message_id=message_id, request_id=request_id)

    def _build_client(self) -> Any:
        if not self.settings.tencentcloud_secret_id or not self.settings.tencentcloud_secret_key:
            raise EmailProviderError("email provider is not configured")
        http_profile = HttpProfile(
            endpoint=self.settings.tencentcloud_ses_endpoint,
            reqTimeout=self.settings.email_api_timeout_seconds,
        )
        client_profile = ClientProfile(httpProfile=http_profile)
        cam_credential = credential.Credential(
            self.settings.tencentcloud_secret_id,
            self.settings.tencentcloud_secret_key,
        )
        return ses_client.SesClient(
            cam_credential,
            self.settings.tencentcloud_region,
            client_profile,
        )

    def _build_request(self, recipient_email: str, code: str) -> models.SendEmailRequest:
        template = models.Template()
        template.TemplateID = self.settings.email_template_id
        template.TemplateData = json.dumps(
            {
                "code": code,
                "expire_minutes": self.settings.email_code_expire_minutes,
            },
            separators=(",", ":"),
        )

        request = models.SendEmailRequest()
        request.FromEmailAddress = (
            f"{self.settings.email_from_name} <{self.settings.email_from_address}>"
        )
        request.Subject = self.settings.email_subject
        request.Destination = [recipient_email]
        request.Template = template
        request.TriggerType = 1
        return request

    def _mask_email(self, email: str) -> str:
        normalized = email.strip()
        local, separator, domain = normalized.partition("@")
        if not separator:
            return "***"
        visible_local = local[:1] or "*"
        return f"{visible_local}***@{domain[:1]}***"
