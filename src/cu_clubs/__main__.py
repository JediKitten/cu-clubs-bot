"""Application processes and explicit operational commands."""

import argparse
import asyncio
import logging
from pathlib import Path

import uvicorn
from aiogram import Bot
from aiogram.types import BotCommand
from alembic import command
from alembic.config import Config

from cu_clubs.app import create_app
from cu_clubs.config import Settings
from cu_clubs.db import make_database
from cu_clubs.services import ClubService


async def bootstrap(settings: Settings) -> None:
    if not settings.bootstrap_leader_telegram_id or not settings.bootstrap_admin_ids:
        raise ValueError("Bootstrap requires numeric admin and leader IDs")
    engine, sessions = make_database(settings)
    try:
        async with sessions() as session, session.begin():
            club = await ClubService(session).bootstrap(
                settings.bootstrap_admin_ids, settings.bootstrap_leader_telegram_id
            )
        print(f"Club ready: {club['public_id']} ({club['level']})")
    finally:
        await engine.dispose()


async def configure_webhook(settings: Settings) -> None:
    if not settings.external_delivery_enabled or not settings.telegram_webhook_url:
        raise ValueError("Webhook requires explicit delivery and a public HTTPS URL")
    if not settings.telegram_webhook_url.startswith("https://"):
        raise ValueError("Webhook URL must use HTTPS")
    bot = Bot(settings.telegram_bot_token.get_secret_value())
    try:
        identity = await bot.get_me()
        if identity.username != settings.telegram_bot_username:
            raise ValueError("Telegram token belongs to a different bot")
        await bot.set_webhook(
            settings.telegram_webhook_url,
            secret_token=settings.telegram_webhook_secret.get_secret_value(),
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=False,
        )
        await bot.set_my_commands([BotCommand(command="start", description="Открыть меню клубов")])
        result = await bot.get_webhook_info()
        if result.url != settings.telegram_webhook_url:
            raise ValueError("Webhook verification failed")
        print(f"Webhook configured for @{identity.username}")
    finally:
        await bot.session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="CU Clubs application")
    parser.add_argument(
        "command",
        choices=["api", "migrate", "bootstrap", "worker", "webhook", "backup", "mail-test"],
    )
    parser.add_argument("--to", help="Explicit recipient for mail-test")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings()
    try:
        if args.command == "api":
            from cu_clubs.logging_privacy import OAuthQueryFilter

            logging.getLogger("uvicorn.access").addFilter(OAuthQueryFilter())
            uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
        elif args.command == "migrate":
            config = Config(str(Path.cwd() / "alembic.ini"))
            command.upgrade(config, "head")
        elif args.command == "bootstrap":
            asyncio.run(bootstrap(settings))
        elif args.command == "webhook":
            asyncio.run(configure_webhook(settings))
        elif args.command == "worker":
            from cu_clubs.worker import run

            run(settings)
        elif args.command == "backup":
            from cu_clubs.worker import prepare_backup

            async def backup_once():
                engine, sessions = make_database(settings)
                try:
                    created = await prepare_backup(sessions, settings, force=True)
                    print("Backup queued" if created else "Backup already queued or disabled")
                finally:
                    await engine.dispose()

            asyncio.run(backup_once())
        elif args.command == "mail-test":
            from cu_clubs.integrations.mail import send_test_email

            if not args.to:
                parser.error("mail-test requires --to")
            asyncio.run(send_test_email(settings, args.to))
            print("Test email accepted by SMTP server")
    except Exception as exc:
        # Network exception strings can include credential-bearing URLs.
        raise SystemExit(f"Command failed: {type(exc).__name__}") from None


if __name__ == "__main__":
    main()
