"""Shared access policy; self declaration never constitutes email proof."""

CATEGORIES = {
    "student": "Студент",
    "staff": "Сотрудник",
    "applicant": "Абитуриент",
    "external": "Внешний",
}


def self_registration_enabled(session):
    return getattr(session, "info", {}).get("test_self_registration_enabled", False) is True


def can_register(session, person):
    if self_registration_enabled(session) and person.declared_category:
        return (
            person.declared_category in {"student", "staff"}
            and bool(person.time_login)
            and bool(person.full_name and len(person.full_name.split()) >= 2)
        )
    return person.kind == "student" and person.verified_at is not None and bool(person.time_login)


def is_guest(session, person):
    if self_registration_enabled(session) and person.declared_category:
        return person.declared_category in {"applicant", "external"}
    return person.kind == "guest"


def can_check_in(session, person):
    return can_register(session, person) or is_guest(session, person)
