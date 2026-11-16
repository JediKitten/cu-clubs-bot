"""Versioned, source-attributed help content, independent from access policy."""

import json
from importlib.resources import files

from cu_clubs.services import ValidationError

HANDBOOK_URL = "https://note.cu.ru/at/e6a6a602-ddfb-4252-9659-0ac57cfb2a7c"


def content():
    resource = files("cu_clubs").joinpath("faq_resources/ru.json")
    value = json.loads(resource.read_text(encoding="utf-8"))
    if value["handbook_url"] != HANDBOOK_URL:
        raise ValueError("Unexpected FAQ handbook source")
    return value


def handbook_entries(section):
    if section not in {"clubs", "levels"}:
        raise ValidationError("Раздел справки недоступен.")
    handbook = content()["handbook"]
    if not handbook["verified_at"]:
        return []
    return handbook[section]


def bot_entries(*, self_registration):
    entries = []
    for item in content()["bot"]["entries"]:
        item = dict(item)
        if item["id"] == "registration":
            key = (
                "answer_self_registration" if self_registration else "answer_verified_registration"
            )
            item["answer"] = item[key]
        entries.append(item)
    return entries
