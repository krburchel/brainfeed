# BrainFeed ↔ Hermes Agent

Lets Hermes save, search and edit Kevin's BrainFeed notes and reminders, and
deliver due reminders to Telegram/Discord, without giving the VPS any
Supabase credential.

```
Telegram/Discord ──► Hermes (VPS) ──HTTPS──► brainfeed-api (Supabase Edge Function) ──► Postgres
                       │  X-BrainFeed-Token                 │ service role stays here
                       └─ cron (no-agent, every 2m) ─ deliver ┘
```

Nothing listens on the VPS. All traffic is outbound HTTPS from the helper.

## Files

| Path | Purpose |
|---|---|
| `brainfeed/SKILL.md` | Hermes skill: when and how to use the helper |
| `brainfeed/scripts/brainfeed.py` | Helper CLI, Python 3.8+ stdlib only. Agent entry point: `call <file>.json` |
| `setup.sh` | `install`, `activate`, `status`, `test`, `uninstall` |
| `tests/test_api.py` | End-to-end tests (throwaway accounts only) |
| `../supabase/functions/brainfeed-api/index.ts` | The API |

## Shell safety

Hermes never puts user text in a shell command. For every operation it writes a JSON
request with its file-writing tool to `~/.config/brainfeed/inbox/<name>.json`
(`<name>`: letters, digits, `-`, `_`) and runs `brainfeed.py call <name>.json`. The helper
accepts only plain files owned by the current user, at most 64 KB, directly in the inbox (no
links, no paths). It deletes each file after reading it, and accepts only these ops:
`add_note`, `search_notes`, `get_note`, `edit_note`, `append_note`, `check_items`, `list_tags`,
`attach_photos`, `add_reminder`, `list_reminders`, `edit_reminder`, `calendar`.

**Photos from Telegram:** Hermes copies the image from its own gateway cache into the inbox
under a fixed name it chooses (`cp -- '<cache path>' inbox/photo-1.jpg`; the path must come
from the gateway and match `^[A-Za-z0-9._/-]+$`). It then calls `add_note` with
`"photos": ["photo-1.jpg"]`. The helper checks every photo before creating anything: plain
file, owned, directly in the inbox, ≤10 MB, real image bytes. It uploads each photo and deletes
the copy. If an upload fails, the note is kept and the result lists `photo_errors`. Delivery and token operations can't be called this way. The manual
`add` / `search` / `remind` commands are for a person at a terminal.

## iPhone Shortcuts

`ios/build_shortcut.py` builds two Shortcuts with their **own** token, named "iPhone Shortcut"
in Settings → Connected agents, so they can be revoked separately from Hermes:

- **Save to BrainFeed:** links and text from the Share sheet (Instagram, Safari, selected
  text) go to `/v1/notes` with source `ios`. Run it directly to type a note.
- **Save Photo to BrainFeed:** images go to JPEG, then `/v1/photos?source=ios`, one note each.

There are two shortcuts because one combined shortcut using "Get Images from Input" downloaded
pictures from shared web pages instead of saving the link. The token is embedded in the
shortcuts, so don't share them. Rotation: revoke "iPhone Shortcut", register a new fingerprint,
rebuild and re-import.

## Credentials

- **Token:** 256 bits from Python `secrets.token_hex(32)`, stored as 64 lowercase hex chars in
  `~/.config/brainfeed/token` (`/root/.config/brainfeed/token` for root), mode 600, dir 700.
  The helper refuses to use it if group/other can read it.
- **Fingerprint:** `sha256:` + lowercase hex SHA-256 of the 64 ASCII token characters
  (no newline). Example format: `sha256:4786ccca…6ec6f` (64 hex chars).
  This is the only thing that leaves the VPS. Kevin adds it in BrainFeed → menu → Settings →
  Connected agents.
- **Server side** stores only the fingerprint (`public.api_tokens.token_hash`). On each
  request it hashes the presented token and compares it with every active fingerprint
  using a constant-time comparison (no early exit).
- **Header:** the token is sent as `X-BrainFeed-Token: <hex>`, never `Authorization`.
  Supabase's gateway logs the first 10 characters of any `Authorization` value, so the API
  rejects requests that carry one. We checked the platform logs after testing: no part of a
  token sent this way appears in them.
- **Never printed:** the helper never prints the token, never takes it as an argument, and
  reports HTTP errors as status + server message only. Tests assert the token is absent from
  all output.
- **Rotate:** run `brainfeed.py init-token --next` (prints the new fingerprint), Kevin adds it in
  Settings, then run `brainfeed.py rotate-token`. That verifies the new token works, swaps the
  files and revokes the old token.
- **Revoke:** Kevin clicks Revoke in Settings (takes effect on the next request), or the helper
  runs `brainfeed.py revoke-token`.

## Database / RLS design

| Object | Notes |
|---|---|
| `notes`, `reminders`, `settings` | RLS: `auth.uid() = user_id` for the web app. The API uses the service role but filters **every** query by the token's `user_id`; clients cannot send `user_id` (unknown fields → 400). |
| `api_tokens` | `user_id, name, token_hash (sha256:<64 hex>, unique), created_at, last_used_at, revoked_at`. RLS: owners can see and add their own rows, and may update only `name` / `revoked_at` (column-level grant). They can never change a hash or the owner. |
| `bf_tag_counts(user)` | Tag → note count for non-archived notes, most-used first (same as the web app's tag list). `service_role` only. |
| trigger `notes_check_parent` | Nesting rules for every path (web app and API): one level only, same owner, never itself. Every write that sets a parent first takes a per-user transaction advisory lock (`private.nesting_lock`), so competing moves (A into B while B into A, or a child added under A while A moves into B) are checked one at a time and the second is refused. Its messages are passed through to the agent as 400s. |
| `bf_update_note(user, note, body, pinned, archived, set_parent, parent, add_tags, remove_tags)` | The API's `notes/update` in one transaction: locks the row (`FOR NO KEY UPDATE`), applies body / pin / archive, then parent, then tag changes. All or nothing; concurrent tag edits queue on the row lock. `service_role` only. |
| Lock order | Every path is **row → nesting lock**: a parent change (web app UPDATE or `bf_update_note`) holds the note's row lock, and the `notes_check_parent` trigger then takes the per-user advisory lock. Nothing takes the advisory lock first and then waits for a note row in `FOR UPDATE`/`NO KEY UPDATE` mode. A child insert takes the advisory lock and then only a `KEY SHARE` lock on its parent (foreign-key check), which never conflicts with `NO KEY UPDATE`. So web and API moves serialize without deadlocking. |
| `bf_claim_reminders(user, limit, lease)` | Atomic `UPDATE … FOR UPDATE SKIP LOCKED`: picks due, not-done, unclaimed reminders and stamps a `claim_id` plus a lease (default 5 min). `EXECUTE` is granted to `service_role` only. |
| `bf_ack_reminders(user, claim_id)` | Marks a claim delivered. One-time → `done = true`. Repeating → `due_at` moves to the next future occurrence. Sets `last_sent_at`. `service_role` only. |
| `private.next_occurrence(due, repeat, tz)` | Computed in the user's IANA zone and always offset from the original date, so 9:00 stays 9:00 across DST and the 31st stays the 31st (clamped in short months). |
| trigger `reminders_before_update` | Changing `due_at` or `done` clears any claim. Moving a **done** reminder to a future time reactivates it. |
| `private.allowed_emails` + trigger | Only allow-listed emails can sign up. |

## API (base `https://bzvibdjrknqvmurwjroq.supabase.co/functions/v1/brainfeed-api`)

All requests need the `X-BrainFeed-Token` header. Bodies are JSON objects, and unknown fields
are rejected. A database failure is always a 500 `db_error`, never a 404 or a default (for example, if the
time-zone setting can't be read, nothing is scheduled; UTC is used only when no zone is set). Errors come back as `{"error": code, "message": text}` with 400 / 401 / 404 / 409 / 413 / 500 / 503.
The helper prints them as `BrainFeed API error <status> (<code>): <message>`.

| Method & path | Body | Returns |
|---|---|---|
| `GET /v1/status` | – | `ok, token, account (masked), timezone, now_local, notes, open_reminders` |
| `POST /v1/notes` | `body` (≤20k), `tags?[]`, `source?` (hermes / telegram / discord / sms / ios), `parent_id?` (save inside that note) | `note` |
| `POST /v1/notes/search` | `q?` (words AND-ed, `#tag` filters), `tags?[]`, `pinned?`, `limit?` ≤50, `before?` (a `created_at` from a result, used exactly), `before_id?` (that note's `id`: with `before`, continues after it, ties broken by id), `archived?` (true = only archived, false = none; omitted = both), `parent_id?` (notes inside that note) | `notes[]` newest first (then by id) |
| `POST /v1/notes/get` | `id` | `note`, `parent` (`id`, `first_line`) or null, `children[]` (`id`, `first_line`, `archived`, `created_at`; newest first, then by id, at most 100), `children_truncated` (true when there are more: continue with `notes/search` + `parent_id` + `before`/`before_id` of the last child), `checklist[]` (`n`, `done`, `text`) |
| `POST /v1/notes/update` | `id`, `body?`, `add_tags?[]`, `remove_tags?[]`, `pinned?`, `archived?` (true hides from the feed, false restores), `parent_id?` (note id = move inside, null = take out) | `note` (hashtags re-derived from a new body; manual tags kept). Atomic via `bf_update_note` |
| `POST /v1/notes/check` | `id`, `check?[]` / `uncheck?[]` (item numbers, or item text: an exact case-insensitive match, else text found in exactly one item), `add?[]` (new items, after the last item, or a new list at the end) | `note`, `checked`, `unchecked`, `added`, `checklist[]`, `message` ("3/5 done"). Errors `no_match` / `ambiguous` (400). Written only if the note is unchanged since it was read (retried, then 409), so concurrent ticks never overwrite each other. On a retry, an item picked **by number** must still have the text it had the first time; if the list was reordered meanwhile, it answers 409 `conflict` and changes nothing. Items picked by text are matched again by text |
| `POST /v1/tags` | `limit?` ≤500 | `tags[]` (`tag`, `count`), non-archived notes, most-used first |
| `POST /v1/notes/append` | `id`, `body` | `note` with `\n\n— Oct 4, 8:30 PM\n<body>` added (stamp in the user's zone, built server-side). A single database UPDATE (`bf_append_note`), so concurrent appends never overwrite each other |
| `POST /v1/calendar` | `from_local?` (`YYYY-MM-DD`, default today), `days?` 1–31 (default 7) | `overdue_before[]` (open reminders due before the range) and `days[]`: `date`, `weekday`, `reminders[]` (`time_local`, `repeat`, `status`: scheduled / overdue / repeat / done) and `notes[]` (non-archived, `first_line`). Repeats expanded with the same wall-clock, anchored, month-end-clamped rules as delivery |
| `POST /v1/reminders` | `body` (≤2k), `due_at` (ISO **with** offset) **or** `due_local` (`YYYY-MM-DDTHH:MM` in the user's zone) **or** `dates_local` (list of 1–100 local times, at least one in the future; stored as repeat `dates`), `repeat?`, `source?` | `reminder` with `due_local` |
| `POST /v1/reminders/search` | `q?`, `include_done?`, `limit?` ≤100 | `reminders[]` soonest first |
| `POST /v1/reminders/update` | `id`, `body?`, `due_at?`/`due_local?`, `dates_local?` (replaces a specific-dates list atomically), `repeat?` (`none` clears), `done?`. A single time on a specific-dates reminder is rejected | `reminder` |
| `POST /v1/reminders/claim` | `limit?` ≤50, `lease_seconds?` 30–3600 | `claim_id`, `reminders[]` with `late_minutes` |
| `POST /v1/reminders/ack` | `claim_id` | `acked`, updated `reminders[]` |
| `POST /v1/token/revoke` | – | revokes the presented token |

| `POST /v1/photos` | raw image bytes (≤10 MB). Query: `note_id?` (attach to that note, else a new "📷 Photo" note), `source?`, `name?`. Unknown parameters are rejected, and captions never go in the URL. | `note`, `message` |

No delete endpoints and no way to run queries.

**Photos:** the type comes from the file's leading bytes (JPEG, PNG, GIF, WebP only; 415 otherwise),
never from its name or headers. Files are stored at `attachments/<user_id>/<note_id>/…` in the
private bucket. Attachments are appended atomically by `bf_append_attachment` (service role only),
with at most 10 per note. If storing a photo fails, a note created for it is removed again.

## Reminder behavior

- **Channel:** Telegram by default (`activate --deliver telegram`). Use `discord` or
  `telegram,discord` for both. Change later with `hermes cron edit brainfeed-reminders --deliver …`.
- **Time zones:** stored as UTC `timestamptz`. Displayed and interpreted in
  `settings.timezone` (IANA name, set from the browser on first login, editable in Settings).
  Hermes should send `due_local`, and the API converts it, handling DST.
- **Cadence:** a Hermes no-agent cron job runs every 2 min (no LLM tokens). Empty output = no
  message.
- **Duplicates:** `deliver` claims (5-min lease), prints the reminders, then acks at once. A
  claimed reminder can't be claimed again during its lease, and an acked one is done or moved
  to its next occurrence, so overlapping or repeated runs don't double-send. If the ack fails,
  the lease expires and the reminder is retried (at-least-once). The edge case: the ack
  succeeds but Hermes then fails to post the message. That reminder is not resent, but it still
  shows as "sent" in the web app.
- **Hermes offline:** reminders simply stay due. On the next run they're delivered with "(was
  due …)" if 10+ minutes late. A repeating reminder that missed several occurrences is sent
  once, then moved to its next future time.
- **API offline:** `deliver` stays silent. After about 30 min of consecutive failures it posts
  one warning, and one "working again" message when it recovers. A revoked token triggers one
  warning immediately.
- **Recurring:** `daily`, `weekdays` (Mon–Fri), `weekly`, `monthly`, `yearly`, or `dates`
  (a list of specific dates: delivery steps to the next listed date and finishes after the last).
  A database trigger keeps a `dates` reminder's `due_at` on one of its dates whatever changes it,
  and claims return `has_more_dates` so the last delivery doesn't promise more.
- **Editing a delivered reminder:** changing its time to the future reactivates it. Editing
  only the text does not.

## Procedures (on the VPS)

```
bash hermes/setup.sh install      # from the verified checkout: skill + cron script + token; prints fingerprint

S=~/.hermes/skills/productivity/brainfeed/scripts/setup.sh   # installed copy
bash $S activate --deliver telegram   # after the fingerprint is added in Settings
bash $S status
bash $S test                          # read-only checks
hermes cron list                      # find the brainfeed-reminders job ID, then:
hermes cron runs <id>                 # its run history (this command takes the ID, not the name)
bash $S uninstall [--revoke|--keep-token] [--delete-config|--keep-config]
```

Uninstall removes the cron job, skill and cron script. With `--revoke` it also revokes the
token, and with `--delete-config` it removes the local token and config. It never touches notes
or reminders. Without a terminal and without flags, it keeps the token and config.

## Troubleshooting

- **`hermes cron status` reports no gateway heartbeat:** cron jobs only fire inside a running
  gateway (`hermes gateway`). If Hermes chats on Telegram but reports no heartbeat, the terminal
  that ran `setup.sh` may be a different environment (for example a sandboxed terminal backend
  or another `HERMES_HOME`/profile) from the gateway. Install from the gateway's environment.
- **Is delivery running?** Kevin can check Settings → Connected agents: "last used" should
  refresh every ~2 minutes when the job fires.

## Tests

`tests/test_api.py` runs the real helper against the deployed API with two throwaway accounts.
It covers: auth failures and refusal of the `Authorization` header; the token never appearing in
output; cross-account isolation and `user_id` injection; note add/search/edit/tag rules; DST
conversion; exclusive claims, lease expiry and retry, ack for one-time and repeating reminders,
late labels, reactivation rules; tag counts (archived excluded); checklist ticks by text and
number, exact-vs-partial matching, ambiguous/no-match errors, starting a new list, concurrent ticks
all landing; nesting (add inside, move in/out, children/parent, one-level and cross-account
refusals, and racing reciprocal moves / child-add-while-moving, which must never break the
one-level rule; web-app (signed-in PostgREST) moves racing API moves on the same note, which must never fail
or deadlock: a smoke test only, since it also passed against the old advisory-then-row order (HTTP
jitter is far wider than the in-database window), so the lock order itself rests on the rule above); child truncation and keyset paging with `before`/`before_id`; combined edits applied atomically (a refused move leaves the body and tags untouched); the archived search filter; photos (caption + photo, photo-only, attach more, fake
images rejected before anything is created, bad names, links, size, other users' notes,
Shortcut-style raw upload, non-image/unknown-parameter/10-per-note limits); request files (shell metacharacters and heredoc delimiters stored
verbatim, bad names, links, oversized and non-JSON files, disallowed ops, file deleted after use);
outage alert-once and recover-once; refusal of http:// and loose
token permissions; and rotation plus revocation. Not covered (they need a fault- or timing-injection
hook the production API deliberately doesn't have): the numbered-checklist 409 retry branch and
injected database read failures. **Never run it against a real account.**
