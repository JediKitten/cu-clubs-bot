"""Read authorized-user OAuth JSON from stdin into the app's private named volume."""

import json
import os
import pwd
import sys
import tempfile
from pathlib import Path


def main() -> None:
    data = json.load(sys.stdin)
    if data.get("type") != "authorized_user" or data.get("token_uri") != (
        "https://oauth2.googleapis.com/token"
    ):
        raise ValueError("Expected authorized-user Google credentials")
    if not all(
        isinstance(data.get(key), str) and data[key]
        for key in ("client_id", "client_secret", "refresh_token")
    ):
        raise ValueError("Incomplete Google credentials")
    owner = pwd.getpwnam("app")
    directory = Path("/run/cu-clubs-secrets")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    os.chown(directory, owner.pw_uid, owner.pw_gid)
    fd, filename = tempfile.mkstemp(prefix=".oauth-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            os.fchown(stream.fileno(), owner.pw_uid, owner.pw_gid)
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(filename, directory / "google-user.json")
    finally:
        if os.path.exists(filename):
            os.unlink(filename)
    print("Google credentials installed with private permissions")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(f"Credential installation failed: {type(exc).__name__}") from None
