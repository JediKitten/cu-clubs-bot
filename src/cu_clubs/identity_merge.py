"""Attach an email-verified Telegram account to its pre-existing attendance identity."""

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from cu_clubs.event_models import Event, Registration, RegistrationHistory
from cu_clubs.identity_models import EmailChallenge
from cu_clubs.models import Audit, GlobalAdmin, Membership, Person, Subscription, TelegramAccount


async def imported_person(session, login, own_id):
    """Only an unverified, accountless imported student can be claimed after email proof."""
    person = await session.scalar(
        select(Person).where(Person.time_login == login, Person.id != own_id).with_for_update()
    )
    if person is None:
        return None, False
    account = await session.scalar(
        select(TelegramAccount.telegram_id).where(TelegramAccount.person_id == person.id)
    )
    eligible = account is None and person.verified_at is None and person.kind == "student"
    return person, not eligible


async def merge_into_imported(session, old, canonical, actor):
    """Caller holds the login and Telegram locks and has verified the code."""
    from cu_clubs.attendance import merge_attendance_people
    from cu_clubs.attendance_models import AttendanceQR

    old_id, new_id = old.id, canonical.id
    from cu_clubs.api_models import ApiGrant, ApiLink
    from cu_clubs.surveys import merge_survey_people

    await merge_survey_people(session, old_id, new_id)
    await session.execute(
        update(ApiGrant).where(ApiGrant.person_id == old_id).values(person_id=new_id)
    )
    await session.execute(
        update(ApiLink).where(ApiLink.person_id == old_id).values(person_id=new_id)
    )
    await merge_attendance_people(session, old_id, new_id, actor)
    # Preserve independent club roles and subscriptions without duplicating them.
    for member in list(
        await session.scalars(select(Membership).where(Membership.person_id == old_id))
    ):
        existing = await session.get(Membership, (member.club_id, new_id))
        flags = {
            key: getattr(member, key) or bool(existing and getattr(existing, key))
            for key in ("edit_profile", "manage_events", "record_attendance")
        }
        role = (
            "leader"
            if member.role == "leader" or (existing and existing.role == "leader")
            else "organizer"
        )
        await session.execute(
            delete(Membership).where(
                Membership.club_id == member.club_id, Membership.person_id.in_([old_id, new_id])
            )
        )
        await session.execute(
            insert(Membership).values(club_id=member.club_id, person_id=new_id, role=role, **flags)
        )
    if await session.get(GlobalAdmin, old_id):
        await session.execute(insert(GlobalAdmin).values(person_id=new_id).on_conflict_do_nothing())
        await session.execute(delete(GlobalAdmin).where(GlobalAdmin.person_id == old_id))
    for row in list(
        await session.scalars(select(Subscription).where(Subscription.person_id == old_id))
    ):
        await session.execute(
            insert(Subscription)
            .values(club_id=row.club_id, person_id=new_id, created_at=row.created_at)
            .on_conflict_do_nothing()
        )
    await session.execute(delete(Subscription).where(Subscription.person_id == old_id))
    for row in list(
        await session.scalars(select(Registration).where(Registration.person_id == old_id))
    ):
        existing = await session.get(Registration, (row.event_id, new_id))
        if existing is None:
            await session.execute(
                update(Registration)
                .where(Registration.event_id == row.event_id, Registration.person_id == old_id)
                .values(person_id=new_id)
            )
        else:
            if row.status == "active" and existing.status != "active":
                existing.status, existing.registered_at, existing.cancelled_at = (
                    "active",
                    row.registered_at,
                    None,
                )
            await session.execute(
                delete(Registration).where(
                    Registration.event_id == row.event_id, Registration.person_id == old_id
                )
            )
    await session.execute(
        update(RegistrationHistory)
        .where(RegistrationHistory.person_id == old_id)
        .values(person_id=new_id)
    )
    await session.execute(
        update(Event).where(Event.author_person_id == old_id).values(author_person_id=new_id)
    )
    for event in await session.scalars(
        select(Event).where(Event.start_registration_snapshot.contains([str(old_id)]))
    ):
        event.start_registration_snapshot = list(
            dict.fromkeys(
                str(new_id) if value == str(old_id) else value
                for value in event.start_registration_snapshot
            )
        )
    await session.execute(
        update(AttendanceQR).where(AttendanceQR.person_id == old_id).values(person_id=new_id)
    )
    await session.execute(
        update(EmailChallenge).where(EmailChallenge.person_id == old_id).values(person_id=new_id)
    )
    await session.execute(
        update(TelegramAccount).where(TelegramAccount.person_id == old_id).values(person_id=new_id)
    )
    old.time_login, old.verified_at, old.kind = None, None, None
    session.add(
        Audit(
            actor_telegram_id=actor,
            action="identity.merged",
            entity_id=new_id,
            details={"previous_person_id": str(old_id), "canonical_person_id": str(new_id)},
        )
    )
    await session.flush()
    return canonical
