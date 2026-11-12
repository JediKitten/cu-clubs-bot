"""Run the available application process; worker/migrate are future commands."""

import argparse

import uvicorn

from cu_clubs.app import create_app
from cu_clubs.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="CU Clubs stage 0 application")
    parser.add_argument("command", choices=["api"], help="Run the HTTP API")
    parser.parse_args()
    settings = Settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
