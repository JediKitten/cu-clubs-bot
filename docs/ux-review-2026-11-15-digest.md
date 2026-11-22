# UX review: admin digest export — 15 November 2026

> Исторический отчёт: результаты и ограничения относятся к указанному пакету,
> не являются новой проверкой 22 ноября. Текущее состояние —
> [project-status.md](project-status.md), передача — [handover.md](handover.md).

## Scope and status

Owner-approved workflow: admin action **«Создать дайджест»** → choose a week →
include all canonical public events (open and previously published, including
held historical events) or select events manually → preview → export a JSON file
for Figma. The export contains CU Clubs events only, ordered by Moscow event
time. Figma duplicates six mixed-layout frames and reflows the remaining events
while preserving every event's content.

This is the independent UX acceptance checklist and source review. The admin
flow and Figma plugin are implemented; live Telegram acceptance and a complete
visual Figma run remain separate. The legacy weekly PNG digest is a separate
flow and does not satisfy this export requirement.

## Acceptance criteria

- The action is available only to an authorized administrator and is labeled
  **«Создать дайджест»**. Each screen shows the next meaningful action.
- Week selection is explicit. The next screen offers an unambiguous choice
  between all eligible events and manual selection.
- Eligible events are canonical CU Clubs public events: open and previously
  published, including held historical events. The displayed order uses event
  start time in `Europe/Moscow`; ties have a stable, deterministic order.
  Closed, draft, or non-CU-Clubs events never appear.
- Manual selection supports multiple pages without losing checked items. Back
  from selection, preview, or export returns to the prior step with the week and
  all selections intact. Cancel exits cleanly and does not create an export.
- Before preview/export, event data and eligibility are refreshed or checked
  against the selected version. If the week or an event changed, explain what
  changed and let the admin review the refreshed selection; do not silently
  export stale or newly ineligible records.
- An empty week has a clear message and a direct action to choose another week
  or cancel; it never offers an empty file as if a digest were ready.
- Preview makes the selected week, event count, ordering, and event content
  reviewable before export. The JSON schema matches the Figma import contract.
  The six mixed-layout frames are duplicated as specified; reflow preserves all
  remaining event text and fields without truncation or omission.
- JSON contains no private profile or roster fields, emails, attendance,
  registration-only text, or secrets. Public event description and location may
  contain intentionally public contact content. Only event fields in the
  approved Figma schema are exported.
- The sent Telegram document belongs to the current digest window and is removed
  when navigation replaces that window or a replacement export is sent. This
  preserves the user's ability to download the file while the window remains
  open.

## Review findings

The first source implementation is present in `src/cu_clubs/digest.py`,
`src/cu_clubs/digest_ui.py`, and the shared screen manager. This is a source
review only; no Telegram runtime or Figma import was performed.

### Initial findings and disposition

- Eligibility includes `held` meetings under the root-approved canonical public
  event contract; it is not restricted to currently published meetings.
- Public descriptions and room/location fields are required by the Figma schema.
  The no-PII boundary excludes private profile, roster, email, attendance,
  registration-only, and secret fields; it does not silently redact intentionally
  public event text.
- File cleanup on next navigation or replacement is the approved lifetime
  policy; no expiry timer is required.
- The first source pass found three blockers: manual selection returned to week
  selection instead of the mode choice and could lose checks after reselecting
  the same week; all-events export did not detect a newly eligible event added
  after selection; and an empty week did not clearly explain that there were no
  eligible events. The current source addresses these: manual Back returns to
  the mode choice with same-week selections retained, all-mode exact-set
  comparison is performed during the snapshot, and empty states have neutral
  copy plus a direct week-change action.
- The initial 500-event recovery finding is resolved: an oversized all-events
  selection stays on the mode screen with a manual-selection action. Reaching
  the manual cap retains the current page and checked items. A PostgreSQL test
  covers both recovery paths.

The source review is otherwise acceptable for the reviewed routes and state
retention. Independent review did not test Telegram runtime or a complete Figma
import. Separately, the lead verified 397 Python tests, 13 Node tests, strict
TypeScript compilation, successful plugin build, and the real six-frame source
analyzer (26 club rows, 11 other rows, 35 calendar dates). These checks do not
establish final visual acceptance in the Figma editor.
