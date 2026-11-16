"""Opt-in platform entrypoint; never bootstraps roles or configures Telegram."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DATA = Path("/data")
ENV_FILE = DATA / "runtime-env.json"


def enabled(env, name, default=False):
    value = env.get(name, str(default)).lower()
    if value not in {"true", "false", "1", "0"}:
        raise ValueError(f"{name} must be true or false")
    return value in {"true", "1"}


def private_write(path, content):
    # Replace atomically without following an existing symlink or exposing permissions.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", prefix=f".{path.name}.", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare_environment(source, data=DATA):
    env = dict(source)
    url = env.get("DATABASE_URL")
    if not url:
        raise ValueError("Managed DATABASE_URL is required")
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    elif not url.startswith("postgresql+psycopg://"):
        raise ValueError("Managed database must use PostgreSQL")
    env["CU_CLUBS_DATABASE_URL"] = url
    env["CU_CLUBS_SELF_SERVICE_POOLER"] = "true"
    env["CU_CLUBS_HOST"] = "0.0.0.0"
    env.setdefault("CU_CLUBS_PORT", "8000")
    env.setdefault("CU_CLUBS_BACKUP_DIRECTORY", str(data / "backups"))
    data.mkdir(parents=True, exist_ok=True)
    (data / "backups").mkdir(exist_ok=True)
    credentials = env.pop("CU_CLUBS_GOOGLE_CREDENTIALS_JSON", None)
    if credentials:
        json.loads(credentials)
        credentials_path = data / "google-credentials.json"
        private_write(credentials_path, credentials)
        env["CU_CLUBS_GOOGLE_CREDENTIALS_PATH"] = str(credentials_path)
    # SSH sessions do not inherit pod environment; only application config is persisted.
    snapshot = {
        key: value for key, value in env.items() if key.startswith(("CU_CLUBS_", "SELF_SERVICE_"))
    }
    private_write(data / "runtime-env.json", json.dumps(snapshot))
    return env


def supervise(commands, env, shutdown_timeout=20):
    children = []
    stopping = False
    signaled = set()

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        for child in children:
            if child.pid in signaled:
                continue
            signaled.add(child.pid)
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        for command in commands:
            if stopping:
                return 0
            children.append(subprocess.Popen(command, env=env, start_new_session=True))
        while not stopping:
            if any(child.poll() is not None for child in children):
                return 1
            time.sleep(0.2)
        return 0
    finally:
        stop(signal.SIGTERM, None)
        deadline = time.monotonic() + shutdown_timeout
        for child in children:
            try:
                child.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main():
    os.umask(0o077)
    env = prepare_environment(os.environ)
    commands = [["cu-clubs", "migrate"], ["cu-clubs", "api"]]
    # Migrations are supervised too, so SIGTERM cannot leave a migration child behind.
    # Unlike long-running processes a successful migration is expected to exit.
    migration = subprocess.Popen(commands[0], env=env, start_new_session=True)
    interrupted = False

    def stop_migration(signum, frame):
        nonlocal interrupted
        interrupted = True
        try:
            os.killpg(migration.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    previous = {sig: signal.signal(sig, stop_migration) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        if migration.wait() != 0 or interrupted:
            return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    commands = commands[1:]
    if enabled(env, "SELF_SERVICE_WORKER_ENABLED"):
        commands.append(["cu-clubs", "worker"])
    if enabled(env, "SELF_SERVICE_SSH_ENABLED", True):
        host_key = DATA / "ssh_host_ed25519_key"
        if not host_key.exists():
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)], check=True
            )
        # fsGroup тома при каждом монтировании делает ключ 0660, а sshd такой ключ отвергает.
        host_key.chmod(0o600)
        commands.append(["/usr/sbin/sshd", "-D", "-e", "-f", "/app/deploy/sshd_config"])
    return supervise(commands, env)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # Exception text can contain secret-bearing URLs or credential JSON.
        sys.exit(f"Self-service startup failed: {type(exc).__name__}")
