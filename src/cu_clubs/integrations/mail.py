"""SMTP delivery. Authentication and message bodies are never logged."""

import asyncio
import smtplib
import ssl
from email.headerregistry import Address
from email.message import EmailMessage

from cu_clubs.config import Settings


class MailDeliveryError(RuntimeError):
    """Safe public error: excludes server responses, recipients and credentials."""


def _address(value: str) -> str:
    try:
        address = Address(addr_spec=value)
        if not address.username or not address.domain or "\r" in value or "\n" in value:
            raise ValueError
        return address.addr_spec
    except (ValueError, IndexError):
        raise MailDeliveryError("A valid single email address is required") from None


def _send(settings: Settings, to_address: str, subject: str, body: str) -> None:
    password = settings.smtp_password
    if not all((settings.smtp_host, settings.smtp_username, settings.smtp_sender, password)):
        raise MailDeliveryError("SMTP credentials and sender are not configured")
    if not password.get_secret_value():
        raise MailDeliveryError("SMTP credentials and sender are not configured")
    use_ssl = settings.smtp_use_ssl
    starttls = settings.smtp_starttls
    if use_ssl == starttls:
        raise MailDeliveryError("Select exactly one secure SMTP mode: SSL or STARTTLS")
    recipient = _address(to_address)
    sender = _address(settings.smtp_sender)
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    context = ssl.create_default_context()
    try:
        factory = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        kwargs = {"timeout": 20}
        if use_ssl:
            kwargs["context"] = context
        with factory(settings.smtp_host, settings.smtp_port, **kwargs) as smtp:
            smtp.ehlo()
            if starttls:
                smtp.starttls(context=context)
                smtp.ehlo()
            smtp.login(settings.smtp_username, password.get_secret_value())
            if smtp.send_message(message, from_addr=sender, to_addrs=[recipient]):
                raise MailDeliveryError("SMTP server did not accept the message")
    except (OSError, smtplib.SMTPException):
        raise MailDeliveryError("SMTP delivery failed; check connection and credentials") from None


async def send_verification_code(settings: Settings, to_address: str, code: str) -> None:
    """Send a code generated and stored by the application; do not invent validity here."""
    if not code or not code.isascii() or not code.isdigit() or len(code) > 12:
        raise MailDeliveryError("Verification code must contain 1 to 12 ASCII digits")
    await asyncio.to_thread(
        _send,
        settings,
        to_address,
        "Код подтверждения CU Clubs",
        f"Ваш код подтверждения: {code}\n\nЕсли вы не запрашивали код, проигнорируйте письмо.\n",
    )


async def send_test_email(settings: Settings, to_address: str) -> None:
    """Explicit setup check, invoked only by the operator's send-test command."""
    await asyncio.to_thread(
        _send,
        settings,
        to_address,
        "Проверка почты CU Clubs",
        "Это тестовое письмо CU Clubs для проверки настройки SMTP.\n",
    )
