"""Own-app maintenance over SSH: python /app/deploy/ops.py restore /data/backup.dump."""

import argparse
import json
import os
import stat
import subprocess
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit


def load_environment(path=Path("/data/runtime-env.json")):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Runtime environment must be an owned private regular file")
    return {**os.environ, **json.loads(path.read_text())}


def restore(path, env):
    if env.get("SELF_SERVICE_WORKER_ENABLED", "false").lower() not in {"false", "0"}:
        raise ValueError("Disable the worker before restoring")
    url = urlsplit(
        env["CU_CLUBS_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    )
    # Credentials stay out of process arguments and command output.
    child_env = dict(
        env,
        PGHOST=url.hostname or "",
        PGPORT=str(url.port or 5432),
        PGUSER=unquote(url.username or ""),
        PGPASSWORD=unquote(url.password or ""),
        PGDATABASE=unquote(url.path.lstrip("/")),
    )
    for key, value in parse_qsl(url.query):
        if key in {"sslmode", "sslrootcert", "sslcert", "sslkey", "connect_timeout"}:
            child_env["PG" + key.upper()] = value
    return subprocess.call(
        [
            "pg_restore",
            "--exit-on-error",
            "--no-owner",
            "--no-acl",
            "--single-transaction",
            "--clean",
            "--if-exists",
            "--dbname",
            child_env["PGDATABASE"],
            str(path),
        ],
        env=child_env,
    )


def main():
    parser = argparse.ArgumentParser(description="Maintenance for this application only")
    parser.add_argument("command", choices=["restore", "migrate"])
    parser.add_argument("backup", nargs="?", type=Path)
    args = parser.parse_args()
    env = load_environment()
    if args.command == "migrate":
        return subprocess.call(["/app/.venv/bin/cu-clubs", "migrate"], env=env, cwd="/app")
    if args.backup is None or not args.backup.is_file():
        parser.error("restore requires an existing custom-format dump")
    return restore(args.backup, env)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        raise SystemExit(f"Maintenance failed: {type(exc).__name__}") from None
