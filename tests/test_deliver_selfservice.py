"""Offline delivery boundary/race tests; no API, git push or PR is performed."""

import base64
import copy
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "deliver_selfservice", ROOT / "scripts/deliver_selfservice.py"
)
delivery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(delivery)
REVISION, TAG, COMMIT = "1" * 40, "2" * 40, "3" * 40


def fixture():
    existing = {
        "repository": delivery.SOURCE,
        "tag": "initial",
        "env": {delivery.WORKER_KEY: "false", "UNCHANGED": "line one\nline two", "OTHER": "true"},
        "sealedEnv": {f"SECRET_{index}": "A" * 256 + "=" for index in range(7)},
    }
    app = {
        "repo": delivery.SOURCE,
        "variables": {key: key for key in existing["env"]},
        "secrets": {key: key for key in existing["sealedEnv"]},
    }
    validated, proofs = [], []

    def validate(value, settings):
        validated.append(copy.deepcopy(value))
        assert settings == app
        assert set(value["env"]) == set(app["variables"])
        assert set(value["sealedEnv"]) == set(app["secrets"])

    def provenance(path, content):
        proofs.append((path, content))
        return "\n\n<!-- delivery-provenance: synthetic-oidc-token -->"

    # JSON is a YAML subset. Inject a codec so the project's check job needs
    # no platform dependencies; production uses its real strict YAML validator.
    runtime = SimpleNamespace(
        app=app, load=json.loads, dump=json.dumps, validate=validate, provenance=provenance
    )
    return existing, runtime, validated, proofs


class FakeAPI:
    def __init__(self, content, *, master_race=0, branch_race="", file_race=""):
        self.original = content
        self.content = content
        self.calls = []
        self.head = REVISION
        self.master_reads = 0
        self.master_race = master_race
        self.branch_race = branch_race
        self.file_race = file_race
        self.written = False

    def call(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if method == "GET" and path.endswith("/branches/master"):
            self.master_reads += 1
            return {
                "commit": {"id": "9" * 40 if self.master_reads == self.master_race else REVISION}
            }
        if method == "GET" and "/branches/" in path:
            stage = "after" if self.written else "before"
            return {"commit": {"id": "9" * 40 if self.branch_race == stage else self.head}}
        if method == "GET" and "/contents/" in path:
            ref = parse_qs(urlparse(path).query)["ref"][0]
            stage = "master" if ref == REVISION else "after" if self.written else "before"
            raw = self.original if ref == REVISION else self.content
            if self.file_race == stage:
                raw += " "
            return {
                "encoding": "base64",
                "sha": delivery.blob_sha(raw),
                "content": base64.b64encode(raw.encode()).decode(),
            }
        if method == "POST" and path.endswith("/branches"):
            assert body["old_ref_name"] == REVISION
            return {}
        if method == "PUT":
            assert path == delivery.PREFIX + "/contents/" + delivery.DELIVERY_PATH
            assert body["sha"] == delivery.blob_sha(self.original)
            self.content = base64.b64decode(body["content"]).decode()
            self.head, self.written = COMMIT, True
            return {"commit": {"sha": COMMIT}}
        if method == "POST" and path.endswith("/pulls"):
            assert body["base"] == "master"
            return {"number": 123}
        raise AssertionError((method, path))


@pytest.mark.parametrize("worker", ["", "false", "true"])
def test_candidate_preserves_every_sealed_and_other_environment_value(worker):
    old, runtime, validated, _ = fixture()
    before = copy.deepcopy(old)
    proposed = delivery.candidate(old, runtime.app, TAG, worker, runtime.validate)
    assert old == before and proposed is not old
    assert proposed["sealedEnv"] == before["sealedEnv"]
    assert proposed["sealedEnv"] is not old["sealedEnv"]
    assert proposed["tag"] == TAG
    assert proposed["env"] == {**before["env"], delivery.WORKER_KEY: worker or "false"}
    assert validated == [before, proposed]


@pytest.mark.parametrize("worker", [None, True, "TRUE", "False", "1", " true", "false\n"])
def test_worker_override_rejects_ambiguous_values(worker):
    old, runtime, _, _ = fixture()
    with pytest.raises(delivery.Rejected):
        delivery.candidate(old, runtime.app, TAG, worker, runtime.validate)


def test_worker_must_be_explicitly_declared_with_same_source_name():
    old, runtime, _, _ = fixture()
    runtime.app["variables"][delivery.WORKER_KEY] = "SOME_OTHER_VARIABLE"
    with pytest.raises(delivery.Rejected, match="explicitly declared"):
        delivery.candidate(old, runtime.app, TAG, "true", runtime.validate)


@pytest.mark.parametrize("tag", ["latest", "short", "A" * 40, "2" * 41])
def test_arbitrary_or_abbreviated_image_tag_is_never_accepted(tag):
    old, runtime, _, _ = fixture()
    with pytest.raises(delivery.Rejected):
        delivery.candidate(old, runtime.app, tag, "", runtime.validate)


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_default_branch_source_events_accept_exact_checked_out_job_sha(event):
    env = {
        "SOURCE_REPOSITORY": delivery.SOURCE,
        "SOURCE_EVENT": event,
        "SOURCE_REF": "refs/heads/main",
        "SOURCE_DEFAULT_BRANCH": "main",
        "SOURCE_SHA": TAG,
    }
    assert delivery.source_tag(env, TAG) == TAG


@pytest.mark.parametrize(
    "origin,revision,dirty",
    [
        ("https://elsewhere/repo.git", REVISION, ""),
        (delivery.PLATFORM_URL, "short", ""),
        (delivery.PLATFORM_URL, REVISION, "?? untracked.py"),
        (delivery.PLATFORM_URL, REVISION, " M scripts/config.py"),
    ],
)
def test_checkout_rejects_wrong_origin_or_unreviewed_files(monkeypatch, origin, revision, dirty):
    def git(_root, *args):
        return origin if args[0] == "remote" else revision if args[0] == "rev-parse" else dirty

    monkeypatch.setattr(delivery, "git", git)
    with pytest.raises(delivery.Rejected):
        delivery.checkout_revision(Path("synthetic"), delivery.PLATFORM_URL)


@pytest.mark.parametrize(
    "override",
    [
        {"SOURCE_REPOSITORY": "attacker/cu-clubs-bot"},
        {"SOURCE_EVENT": "pull_request"},
        {"SOURCE_EVENT": "pull_request_target"},
        {"SOURCE_REF": "refs/heads/feature"},
        {"SOURCE_REF": "refs/tags/release"},
        {"SOURCE_DEFAULT_BRANCH": "master"},
        {"SOURCE_DEFAULT_BRANCH": ""},
        {"SOURCE_SHA": "short"},
        {"SOURCE_SHA": "A" * 40},
        {"SOURCE_SHA": "4" * 40},
    ],
)
def test_source_repository_event_ref_and_full_job_sha_are_fixed(override):
    env = {
        "SOURCE_REPOSITORY": delivery.SOURCE,
        "SOURCE_EVENT": "push",
        "SOURCE_REF": "refs/heads/main",
        "SOURCE_DEFAULT_BRANCH": "main",
        "SOURCE_SHA": TAG,
    }
    assert delivery.source_tag(env, TAG) == TAG
    with pytest.raises(delivery.Rejected):
        delivery.source_tag({**env, **override}, TAG)


def test_success_uses_exact_master_old_blob_and_provenance_content():
    old, runtime, _, proofs = fixture()
    content = json.dumps(old)
    api = FakeAPI(content)
    result = delivery.propose(api, runtime, REVISION, delivery.blob_sha(content), content, TAG, "")
    assert result.endswith("/pulls/123")
    assert len(proofs) == 1 and proofs[0] == (delivery.DELIVERY_PATH, api.content)
    proposed = json.loads(api.content)
    assert proposed["sealedEnv"] == old["sealedEnv"] and proposed["env"] == old["env"]
    mutations = [(method, path, body) for method, path, body in api.calls if method != "GET"]
    assert [method for method, _, _ in mutations] == ["POST", "PUT", "POST"]
    assert mutations[0][2]["old_ref_name"] == REVISION
    assert mutations[1][2]["sha"] == delivery.blob_sha(content)
    assert "synthetic-oidc-token" in mutations[-1][2]["body"]
    assert all("merge" not in unquote(path) for _, path, _ in api.calls)


def test_identical_delivery_does_not_request_oidc_create_branch_or_open_pr():
    old, runtime, _, proofs = fixture()
    old["tag"] = TAG
    content = json.dumps(old)
    api = FakeAPI(content)
    assert (
        delivery.propose(api, runtime, REVISION, delivery.blob_sha(content), content, TAG, "false")
        is None
    )
    assert not proofs and all(method == "GET" for method, _, _ in api.calls)


@pytest.mark.parametrize(
    "kwargs,expected_mutations",
    [
        ({"master_race": 1}, []),
        ({"master_race": 2}, []),
        ({"master_race": 3}, ["POST", "PUT"]),
        ({"branch_race": "before"}, ["POST"]),
        ({"branch_race": "after"}, ["POST", "PUT"]),
        ({"file_race": "master"}, []),
        ({"file_race": "before"}, ["POST"]),
        ({"file_race": "after"}, ["POST", "PUT"]),
    ],
)
def test_revision_and_branch_races_abort_before_pr(kwargs, expected_mutations):
    old, runtime, _, _ = fixture()
    content = json.dumps(old)
    api = FakeAPI(content, **kwargs)
    with pytest.raises(delivery.Rejected):
        delivery.propose(api, runtime, REVISION, delivery.blob_sha(content), content, TAG, "")
    assert [method for method, _, _ in api.calls if method != "GET"] == expected_mutations
    assert not any(path.endswith("/pulls") for _, path, _ in api.calls)


def test_missing_provenance_fails_before_any_mutation():
    old, runtime, _, _ = fixture()
    runtime.provenance = lambda *_: ""
    content = json.dumps(old)
    api = FakeAPI(content)
    with pytest.raises(delivery.Rejected, match="OIDC"):
        delivery.propose(api, runtime, REVISION, delivery.blob_sha(content), content, TAG, "")
    assert all(method == "GET" for method, _, _ in api.calls)


@pytest.mark.parametrize("method,attempts", [("GET", 3), ("POST", 1), ("PUT", 1)])
def test_only_reads_retry_and_network_errors_never_echo_secrets(monkeypatch, method, attempts):
    calls = []
    api = delivery.API("SYNTHETIC_PRIVATE_TOKEN")

    def unavailable(*args, **kwargs):
        calls.append(1)
        raise URLError("SYNTHETIC_PRIVATE_CONFIGURATION")

    monkeypatch.setattr(api.opener, "open", unavailable)
    monkeypatch.setattr(delivery.time, "sleep", lambda _: None)
    with pytest.raises(delivery.Rejected) as error:
        api.call(
            method, delivery.PREFIX + "/branches", {"secret": "SYNTHETIC_PRIVATE_CONFIGURATION"}
        )
    assert len(calls) == attempts
    assert "SYNTHETIC_PRIVATE" not in str(error.value)


def test_api_http_error_and_redirect_do_not_expose_response_payload(monkeypatch):
    api = delivery.API("SYNTHETIC_PRIVATE_TOKEN")

    def denied(*args, **kwargs):
        raise HTTPError(
            "https://git.cu3rd.ru", 403, "SYNTHETIC_PRIVATE", {}, io.BytesIO(b"SYNTHETIC_PRIVATE")
        )

    monkeypatch.setattr(api.opener, "open", denied)
    with pytest.raises(delivery.Rejected) as error:
        api.call("GET", delivery.PREFIX + "/branches/master")
    assert "SYNTHETIC_PRIVATE" not in str(error.value)
    assert (
        delivery.NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere") is None
    )


def test_workflow_delivery_has_only_credential_and_declared_worker_override():
    workflow = (ROOT / ".forgejo/workflows/publish.yml").read_text()
    job = workflow.split("  deliver:\n", 1)[1]
    assert "enable-openid-connect: true" in job and "container: python:3.12-slim" in job
    assert "vars.SELF_SERVICE_DELIVERY_READY == 'true'" in job
    assert (
        "github.event_name == 'push'" in job and "github.event_name == 'workflow_dispatch'" in job
    )
    assert "toJSON(secrets)" not in job and "toJSON(vars)" not in job
    assert "SELF_SERVICE_WORKER_ENABLED: ${{ vars.SELF_SERVICE_WORKER_ENABLED }}" in job
    assert "DELIVERY_TOKEN: ${{ secrets.SELF_SERVICE_DELIVERY_TOKEN }}" in job
    assert "SOURCE_SHA: ${{ github.sha }}" in job and "--delivery-token" not in job
    assert "-r platform/requirements.txt" in job
