# CU Clubs rebuild

The current product is a rebuild and extension of `nlypage/cu-clubs-bot`, pinned
to `3a09cc0039d86641827dcbc28e74dd024b821732`. Read `docs/legacy-compatibility.md`
before implementing or reviewing Telegram flows, event logic, QR, emails, or media.

- Preserve exact upstream copy and the original images wherever the flow has
  the same meaning. Use `cu_clubs.legacy` and its pinned resources rather than
  rewriting shared messages or generating replacement artwork.
- The user's latest instruction to preserve copy overrides cosmetic wording in
  earlier specifications. New business requirements still apply: treat them as
  explicit extensions or differences, with a row in the compatibility matrix.
- Keep original club/profile text blocks intact and append new fields such as
  level, public team and permissions. Public team never grants organizer access.
- Assign an agent the `legacy_compatibility_guard` role for each feature batch
  that touches shared flows or content. Request its independent compatibility
  review before merging/deploying the batch; if no agent is active, start one.
  The guard checks source paths, exact copy, media hashes, logic and exceptions.
- Update the compatibility matrix with concrete evidence and remaining gaps.
  A stored reference is not an implemented or verified behavior.
- Original banners were provided by the owner. Preserve their bytes and author
  marks. Telegram file IDs are specific to a bot: upload originals using the
  new bot instead of transferring IDs from the old configuration.
- Assign a separate UX reviewer when changing Telegram buttons or multi-step
  flows. Review all parent/back/cancel routes, retained input, number of actions
  per screen, and whether the next action matches the user's task. Keep concrete
  change requests and acceptance criteria in `docs/ux-review-*.md`, report them
  to the lead, and distinguish proposed work from implemented and tested work.
  Do not send proposals to external people or silently change business policy.
- Never execute upstream deployment scripts or copy production databases,
  credentials, logs, notebooks with participant data, or nonpublic capability links
  from operational configuration. Public upstream UI references stay in the pinned
  snapshot; review them before exposing any operational link in a new screen.
  Do not contact or modify the old running bot without explicit authorization.
- Keep Telegram/API business rules in shared application services. Historical
  QR limits and automatic registration on check-in must not override the newer
  owner-approved attendance rules.
