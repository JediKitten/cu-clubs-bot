#!/usr/bin/env python3
"""Propose this project's image tag using the current reviewed sealed delivery.

No plaintext runtime configuration is read from Actions. Mutations are never
retried automatically; protected-master review and provenance stay platform-owned.
"""

import argparse
import base64
import copy
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace

SOURCE = "Jedi_kitten/cu-clubs-bot"
PLATFORM_URL = "https://git.cu3rd.ru/cu3rd/self-service.git"
SOURCE_URL = "https://git.cu3rd.ru/Jedi_kitten/cu-clubs-bot.git"
PREFIX = "/repos/cu3rd/self-service"
DELIVERY_PATH = "deployments/cu-clubs-bot/test/api.yaml"
WORKER_KEY = "SELF_SERVICE_WORKER_ENABLED"
SHA = re.compile(r"[0-9a-f]{40}")


class Rejected(ValueError):
    """Only fixed, non-sensitive descriptions may be reported to the job log."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, token):
        if not token:
            raise Rejected("Delivery credential is missing")
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def call(self, method, path, body=None):
        if not path.startswith(PREFIX + "/"):
            raise Rejected("Unexpected API path")
        request = urllib.request.Request(
            "https://git.cu3rd.ru/api/v1" + path,
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={"Authorization": "token " + self.token, "Content-Type": "application/json"},
        )
        for attempt in range(3 if method == "GET" else 1):
            try:
                with self.opener.open(request, timeout=20) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if method == "GET" and error.code in {429, 500, 502, 503, 504} and attempt < 2:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                raise Rejected("Forgejo rejected the delivery request") from None
            except (OSError, ValueError):
                if method == "GET" and attempt < 2:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                raise Rejected("Forgejo request failed; inspect state before retrying") from None


def git(root, *arguments):
    return subprocess.check_output(
        ["git", "-C", str(root), *arguments], text=True, stderr=subprocess.DEVNULL
    ).strip()


def checkout_revision(root, origin):
    if git(root, "remote", "get-url", "origin") != origin:
        raise Rejected("Unexpected checkout origin")
    revision = git(root, "rev-parse", "HEAD")
    if not SHA.fullmatch(revision) or git(root, "status", "--porcelain", "--untracked-files=all"):
        raise Rejected("Delivery requires a clean exact checkout")
    return revision


def source_tag(env, source_revision):
    if env.get("SOURCE_REPOSITORY") != SOURCE:
        raise Rejected("Unexpected source repository")
    if env.get("SOURCE_EVENT") not in {"push", "workflow_dispatch"}:
        raise Rejected("Delivery is allowed only for push or workflow_dispatch")
    default = env.get("SOURCE_DEFAULT_BRANCH", "")
    if default != "main" or env.get("SOURCE_REF") != "refs/heads/" + default:
        raise Rejected("Delivery requires the source default branch")
    tag = env.get("SOURCE_SHA", "")
    if not SHA.fullmatch(tag) or tag != source_revision:
        raise Rejected("Image tag must equal the checked-out source job SHA")
    return tag


def candidate(existing, app, tag, worker, validate):
    if app.get("repo") != SOURCE or not SHA.fullmatch(tag):
        raise Rejected("Unexpected application or image tag")
    if not isinstance(worker, str) or worker not in {"", "true", "false"}:
        raise Rejected("Worker override must be empty, true or false")
    validate(existing, app)
    result = copy.deepcopy(existing)
    result["tag"] = tag
    if worker:
        if app.get("variables", {}).get(WORKER_KEY) != WORKER_KEY:
            raise Rejected("Worker override is not explicitly declared by the platform")
        result["env"][WORKER_KEY] = worker
    validate(result, app)
    # Defense in depth: only these two values may differ from the reviewed file.
    restored = copy.deepcopy(result)
    restored["tag"] = existing["tag"]
    if worker:
        restored["env"][WORKER_KEY] = existing["env"][WORKER_KEY]
    if restored != existing:
        raise Rejected("Delivery would modify immutable configuration")
    return result


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def platform_runtime(root):
    """Use the platform's actual validator/unique YAML loader and OIDC helper."""
    config = _module("_selfservice_delivery_config", root / "scripts/config.py")
    previous = sys.modules.get("config")
    sys.modules["config"] = config
    try:
        official = _module("_selfservice_delivery_official", root / "scripts/deploy.py")
    finally:
        if previous is None:
            sys.modules.pop("config", None)
        else:
            sys.modules["config"] = previous
    app = config.projects(root)["cu-clubs-bot"]["envs"]["test"]["apps"]["api"]
    return SimpleNamespace(
        app=app,
        validate=config.validate_delivery,
        load=lambda value: official.yaml.load(value, Loader=config.UniqueLoader),
        dump=lambda value: official.yaml.safe_dump(value, sort_keys=False, allow_unicode=True),
        provenance=official.provenance,
    )


def _master(api, revision):
    if api.call("GET", PREFIX + "/branches/master")["commit"]["id"] != revision:
        raise Rejected("Platform master changed; rerun from its current revision")


def _file(api, ref):
    result = api.call(
        "GET", PREFIX + "/contents/" + DELIVERY_PATH + "?ref=" + urllib.parse.quote(ref, safe="")
    )
    if result.get("encoding") != "base64" or not SHA.fullmatch(result.get("sha", "")):
        raise Rejected("Unexpected delivery file metadata")
    try:
        raw = base64.b64decode("".join(result["content"].split()), validate=True).decode("utf-8")
    except (ValueError, UnicodeError):
        raise Rejected("Invalid encoded delivery file") from None
    if result["sha"] != blob_sha(raw):
        raise Rejected("Delivery blob does not match its contents")
    return result["sha"], raw


def blob_sha(content):
    raw = content.encode()
    return hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()


def propose(api, runtime, revision, old_sha, old_content, tag, worker):
    """All API mutations live here, with exact revision/blob/content guards."""
    _master(api, revision)
    remote_sha, remote_content = _file(api, revision)
    if (remote_sha, remote_content) != (old_sha, old_content):
        raise Rejected("Reviewed delivery differs from the checkout")
    existing = runtime.load(old_content)
    data = candidate(existing, runtime.app, tag, worker, runtime.validate)
    if data == existing:
        return None
    content = runtime.dump(data)
    proof = runtime.provenance(DELIVERY_PATH, content)
    if not proof or not proof.startswith("\n\n<!-- delivery-provenance: "):
        raise Rejected("OIDC provenance is unavailable")
    _master(api, revision)
    # The platform approver requires the official delivery branch's 10-hex suffix.
    branch = "deploy/cu-clubs-bot/test/api-" + uuid.uuid4().hex[:10]
    api.call("POST", PREFIX + "/branches", {"new_branch_name": branch, "old_ref_name": revision})
    branch_path = PREFIX + "/branches/" + urllib.parse.quote(branch, safe="")
    if api.call("GET", branch_path)["commit"]["id"] != revision:
        raise Rejected("New branch no longer matches the reviewed revision")
    if _file(api, branch) != (old_sha, old_content):
        raise Rejected("New branch delivery changed before writing")
    title = f"deploy: cu-clubs-bot-test-api {tag}"
    written = api.call(
        "PUT",
        PREFIX + "/contents/" + DELIVERY_PATH,
        {
            "branch": branch,
            "message": title,
            "sha": old_sha,
            "content": base64.b64encode(content.encode()).decode(),
        },
    )
    commit = written.get("commit", {}).get("sha", "")
    if not SHA.fullmatch(commit) or api.call("GET", branch_path)["commit"]["id"] != commit:
        raise Rejected("Delivery branch changed after writing")
    if _file(api, branch) != (blob_sha(content), content):
        raise Rejected("Written delivery does not match provenance")
    _master(api, revision)
    pr = api.call(
        "POST",
        PREFIX + "/pulls",
        {
            "head": branch,
            "base": "master",
            "title": title,
            "body": (
                f"Delivery from `{SOURCE}`. Changes only the image tag and, when explicitly "
                "requested, SELF_SERVICE_WORKER_ENABLED. Existing sealed secrets and all other "
                "environment values are preserved from the reviewed master.\n\n"
                "Review and merge using the platform's normal protected-branch policy." + proof
            ),
        },
    )
    number = pr.get("number")
    if type(number) is not int or number <= 0:
        raise Rejected("Delivery response has no pull request number")
    return f"https://git.cu3rd.ru/cu3rd/self-service/pulls/{number}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args()
    source_revision = checkout_revision(args.source_root, SOURCE_URL)
    tag = source_tag(os.environ, source_revision)
    revision = checkout_revision(args.root, PLATFORM_URL)
    api = API(os.environ.get("DELIVERY_TOKEN", ""))
    _master(api, revision)
    runtime = platform_runtime(args.root)
    old_content = (args.root / DELIVERY_PATH).read_text(encoding="utf-8")
    old_sha = git(args.root, "rev-parse", "HEAD:" + DELIVERY_PATH)
    result = propose(
        api, runtime, revision, old_sha, old_content, tag, os.environ.get(WORKER_KEY, "")
    )
    print("Delivery PR: " + result if result else "Delivery already matches; no PR created")


if __name__ == "__main__":
    try:
        main()
    except Rejected as error:
        print("Delivery rejected: " + str(error), file=sys.stderr)
        sys.exit(1)
    except Exception:
        # YAML/API/subprocess exceptions can contain credentials or full configs.
        print(
            "Delivery failed; verify checked-out revisions, configuration and access",
            file=sys.stderr,
        )
        sys.exit(1)
