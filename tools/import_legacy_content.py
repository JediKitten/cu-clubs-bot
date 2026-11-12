"""Import reviewed, pinned public UI resources; never execute upstream code.

Authoring command: uv run --no-project --with pyyaml python tools/import_legacy_content.py SOURCE
PyYAML is an authoring dependency only; the application loads the resulting JSON.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

import yaml

BASELINE = "3a09cc0039d86641827dcbc28e74dd024b821732"
UPSTREAM = "https://github.com/nlypage/cu-clubs-bot"
DESTINATION = Path(__file__).resolve().parents[1] / "src/cu_clubs/legacy/resources"
RESOURCES = {
    "bot/locales/ru.yml": "ru.yml",
    "bot/telegram.yml": "telegram.yml",
    "bot/logo.png": "logo.png",
    "bot/mail.html": "mail.html",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--fetch-email-images", action="store_true")
    args = parser.parse_args()
    source = args.source.resolve()
    head = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    if head != BASELINE:
        raise SystemExit("Source commit changed: obtain compatibility review before importing")
    messages = yaml.safe_load((source / "bot/locales/ru.yml").read_text())
    if not isinstance(messages, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in messages.items()
    ):
        raise SystemExit("Expected the upstream flat Russian string catalog")
    DESTINATION.mkdir(parents=True, exist_ok=True)
    previous_path = DESTINATION / "manifest.json"
    previous = json.loads(previous_path.read_text()) if previous_path.exists() else {}
    resources = {}
    for original, filename in RESOURCES.items():
        target = DESTINATION / filename
        shutil.copyfile(source / original, target)
        resources[filename] = {
            "source_path": original,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        }
    catalog = DESTINATION / "ru.json"
    catalog.write_text(json.dumps(messages, ensure_ascii=False, indent=2) + "\n")
    resources["ru.json"] = {
        "derived_from": "ru.yml",
        "sha256": hashlib.sha256(catalog.read_bytes()).hexdigest(),
    }
    manifest = {
        "upstream": UPSTREAM,
        "commit": BASELINE,
        "message_count": len(messages),
        "resources": resources,
        "banners": {
            role: {"source_config_key": f"bot.banner.{role}", "status": "original_required"}
            for role in ("auth", "menu", "personal-account", "clubs", "club-owner", "events")
        },
        "club_avatar": {"source": "production Telegram file ID", "status": "original_required"},
        "club_intro": {"source": "production Telegram video note", "status": "original_required"},
    }
    for role, entry in manifest["banners"].items():
        image = DESTINATION / f"{role}.png"
        if image.exists():
            sha = hashlib.sha256(image.read_bytes()).hexdigest()
            entry.update(status="available", file=image.name, origin="user_original", sha256=sha)
            resources[image.name] = {"source": "user_original", "sha256": sha}

    class Images(HTMLParser):
        def __init__(self):
            super().__init__()
            self.urls: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag == "img":
                self.urls.append(dict(attrs)["src"])

    images = Images()
    images.feed((DESTINATION / "mail.html").read_text())
    previous_images = {entry["source_url"]: entry for entry in previous.get("external_images", [])}
    manifest["external_images"] = []
    for index, url in enumerate(images.urls, start=1):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "eimage.sendsay.ru":
            raise SystemExit("Unexpected external email image host; review required")
        image = DESTINATION / f"mail-image-{index}.png"
        entry = {
            "source_file": "mail.html",
            "source_url": url,
            "status": "original_required",
        }
        if "error_type" in previous_images.get(url, {}):
            entry["error_type"] = previous_images[url]["error_type"]
        if args.fetch_email_images and not image.exists():
            try:
                with urlopen(url, timeout=20) as response:
                    content = response.read(5 * 1024 * 1024 + 1)
            except URLError as exc:
                entry["error_type"] = type(exc).__name__
                print(f"Email image {index} unavailable; original recorded as required")
            else:
                if len(content) > 5 * 1024 * 1024 or not content.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise SystemExit("Expected a bounded original PNG email image")
                image.write_bytes(content)
        if image.exists():
            content = image.read_bytes()
            if len(content) > 5 * 1024 * 1024 or not content.startswith(b"\x89PNG\r\n\x1a\n"):
                raise SystemExit("Expected a bounded original PNG email image")
            sha = hashlib.sha256(content).hexdigest()
            resources[image.name] = {"source_url": url, "sha256": sha}
            entry.pop("error_type", None)
            entry.update(status="available", file=image.name, sha256=sha)
        manifest["external_images"].append(entry)
    (DESTINATION / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    print(f"Imported {len(messages)} exact legacy messages and {len(resources)} resources")


if __name__ == "__main__":
    main()
