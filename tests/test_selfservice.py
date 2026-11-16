"""Platform-only configuration and process safety checks."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy" / f"{name}.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_platform_environment_private_credentials(tmp_path):
    runtime = module("selfservice")
    env = runtime.prepare_environment(
        {
            "DATABASE_URL": "postgresql://user:secret@db/pool",
            "CU_CLUBS_GOOGLE_CREDENTIALS_JSON": '{"token":"private"}',
            "UNRELATED_SECRET": "not persisted",
        },
        tmp_path,
    )
    assert env["CU_CLUBS_DATABASE_URL"] == "postgresql+psycopg://user:secret@db/pool"
    assert env["CU_CLUBS_SELF_SERVICE_POOLER"] == "true"
    assert not runtime.enabled(env, "SELF_SERVICE_WORKER_ENABLED")
    assert runtime.enabled(env, "SELF_SERVICE_SSH_ENABLED", True)
    assert "CU_CLUBS_GOOGLE_CREDENTIALS_JSON" not in env
    for name in ("google-credentials.json", "runtime-env.json"):
        assert (tmp_path / name).stat().st_mode & 0o777 == 0o600
    saved = json.loads((tmp_path / "runtime-env.json").read_text())
    assert "UNRELATED_SECRET" not in saved
    assert (
        module("ops").load_environment(tmp_path / "runtime-env.json")["CU_CLUBS_DATABASE_URL"]
        == env["CU_CLUBS_DATABASE_URL"]
    )


def test_private_write_does_not_follow_symlink(tmp_path):
    target = tmp_path / "target"
    target.write_text("untouched")
    (tmp_path / "env.tmp").symlink_to(target)
    module("selfservice").private_write(tmp_path / "env", "secret")
    assert (tmp_path / "env").read_text() == "secret"
    assert target.read_text() == "untouched"


def test_ssh_environment_rejects_public_file(tmp_path):
    path = tmp_path / "env"
    path.write_text("{}")
    path.chmod(0o644)
    with pytest.raises(ValueError):
        module("ops").load_environment(path)


def test_supervisor_terminates_peer_on_child_exit(tmp_path):
    pidfile = tmp_path / "pid"
    child = (
        f"import os,time,pathlib; pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    code = module("selfservice").supervise(
        [
            [sys.executable, "-c", child],
            [sys.executable, "-c", "import time; time.sleep(.5)"],
        ],
        os.environ.copy(),
        shutdown_timeout=2,
    )
    assert code == 1
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_pooler_settings_only_opt_in(monkeypatch):
    from cu_clubs import db
    from cu_clubs.config import Settings

    observed = []
    monkeypatch.setattr(db, "create_async_engine", lambda *args, **kwargs: observed.append(kwargs))
    db.make_database(Settings(_env_file=None))
    db.make_database(Settings(_env_file=None, self_service_pooler=True))
    assert "pool_size" not in observed[0]
    assert observed[1]["pool_size"] == 2
    assert observed[1]["max_overflow"] == 0
    assert observed[1]["connect_args"]["prepare_threshold"] is None


def test_supervisor_forwards_termination(tmp_path):
    import signal
    import time

    pidfile = tmp_path / "pid"
    runtime_path = ROOT / "deploy" / "selfservice.py"
    child = (
        f"import os,time,pathlib; pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    script = (
        "import runpy,os,sys; "
        f"runtime=runpy.run_path({str(runtime_path)!r}); "
        f'sys.exit(runtime["supervise"]([[sys.executable,"-c",{child!r}]],os.environ.copy(),2))'
    )
    supervisor = subprocess.Popen([sys.executable, "-c", script])
    try:
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pidfile.exists()
        supervisor.send_signal(signal.SIGTERM)
        assert supervisor.wait(timeout=5) == 0
        with pytest.raises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait()


def test_restore_disabled_with_worker(tmp_path):
    with pytest.raises(ValueError, match="Disable the worker"):
        module("ops").restore(tmp_path / "backup", {"SELF_SERVICE_WORKER_ENABLED": "true"})


def test_private_write_survives_stale_temporary(tmp_path):
    (tmp_path / "env.tmp").write_text("stale")
    module("selfservice").private_write(tmp_path / "env", "fresh")
    assert (tmp_path / "env").read_text() == "fresh"
    assert (tmp_path / "env").stat().st_mode & 0o777 == 0o600


def test_restore_preserves_ssl_configuration(tmp_path, monkeypatch):
    ops = module("ops")
    calls = []
    monkeypatch.setattr(ops.subprocess, "call", lambda command, **kwargs: calls.append(kwargs))
    ops.restore(
        tmp_path / "backup",
        {
            "CU_CLUBS_DATABASE_URL": "postgresql+psycopg://u:p@db/name?sslmode=verify-full&sslrootcert=%2Fdata%2Fca.crt"
        },
    )
    assert calls[0]["env"]["PGSSLMODE"] == "verify-full"
    assert calls[0]["env"]["PGSSLROOTCERT"] == "/data/ca.crt"


def test_main_restores_private_host_key_mode(tmp_path, monkeypatch):
    runtime = module("selfservice")
    host_key = tmp_path / "ssh_host_ed25519_key"
    host_key.write_text("key")
    host_key.chmod(0o660)

    class Migration:
        pid = 0

        def wait(self):
            return 0

    supervised = []
    monkeypatch.setattr(runtime, "DATA", tmp_path)
    monkeypatch.setattr(runtime, "prepare_environment", lambda env: {})
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *args, **kwargs: Migration())
    monkeypatch.setattr(
        runtime, "supervise", lambda commands, env: supervised.append(commands) or 0
    )
    assert runtime.main() == 0
    assert host_key.stat().st_mode & 0o777 == 0o600
    assert supervised[0][-1][0] == "/usr/sbin/sshd"
