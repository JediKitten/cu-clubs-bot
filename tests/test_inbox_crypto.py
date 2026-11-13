"""Verification input survives restart without leaving codes in the durable inbox."""

import json

import pytest
from pydantic import SecretStr

from cu_clubs.inbox_crypto import open_update, seal_update

KEY = SecretStr("synthetic-test-key-with-32-characters")


def test_authenticated_input_roundtrip_without_plaintext():
    payload = {"update_id": 15, "message": {"text": "123456", "chat": {"id": 101}}}
    stored = seal_update(payload, KEY)
    assert "123456" not in json.dumps(stored)
    assert stored["update_id"] == 15
    assert open_update(json.loads(json.dumps(stored)), KEY) == payload
    assert seal_update(payload, KEY) != stored


@pytest.mark.parametrize("mutation", ["key", "ciphertext", "identity"])
def test_wrong_key_or_tampering_is_rejected_without_leaking_input(mutation):
    stored = seal_update({"update_id": 15, "message": {"text": "654321"}}, KEY)
    key = KEY
    if mutation == "key":
        key = SecretStr("different-synthetic-key-with-32-characters")
    elif mutation == "ciphertext":
        stored["sealed_update"] = stored["sealed_update"][:-5] + "xxxxx"
    else:
        stored["update_id"] = 16
    with pytest.raises(ValueError) as error:
        open_update(stored, key)
    assert "654321" not in str(error.value)


def test_existing_offline_updates_remain_readable():
    payload = {"update_id": 1}
    assert seal_update(payload, None) == payload
    assert open_update(payload, KEY) == payload
