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
`add_note`, `search_notes`, `get_note`, `edit_note`, `add_reminder`, `list_reminders`,
`edit_reminder`. Delivery and token operations can't be called this way. The manual
`add` / `search` / `remind` commands are for a person at a terminal.

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
| `bf_claim_reminders(user, limit, lease)` | Atomic `UPDATE … FOR UPDATE SKIP LOCKED`: picks due, not-done, unclaimed reminders and stamps a `claim_id` plus a lease (default 5 min). `EXECUTE` is granted to `service_role` only. |
| `bf_ack_reminders(user, claim_id)` | Marks a claim delivered. One-time → `done = true`. Repeating → `due_at` moves to the next future occurrence. Sets `last_sent_at`. `service_role` only. |
| `private.next_occurrence(due, repeat, tz)` | Computed in the user's IANA zone and always offset from the original date, so 9:00 stays 9:00 across DST and the 31st stays the 31st (clamped in short months). |
| trigger `reminders_before_update` | Changing `due_at` or `done` clears any claim. Moving a **done** reminder to a future time reactivates it. |
| `private.allowed_emails` + trigger | Only allow-listed emails can sign up. |

## API (base `https://bzvibdjrknqvmurwjroq.supabase.co/functions/v1/brainfeed-api`)

All requests need the `X-BrainFeed-Token` header. Bodies are JSON objects, and unknown fields
are rejected. Errors come back as `{"error": code, "message": text}` with 400 / 401 / 404 / 413 / 500 / 503.

| Method & path | Body | Returns |
|---|---|---|
| `GET /v1/status` | – | `ok, token, account (masked), timezone, now_local, notes, open_reminders` |
| `POST /v1/notes` | `body` (≤20k), `tags?[]`, `source?` (hermes / telegram / discord / sms) | `note` |
| `POST /v1/notes/search` | `q?` (words AND-ed, `#tag` filters), `tags?[]`, `pinned?`, `limit?` ≤50, `before?` ISO | `notes[]` newest first |
| `POST /v1/notes/get` | `id` | `note` |
| `POST /v1/notes/update` | `id`, `body?`, `add_tags?[]`, `remove_tags?[]`, `pinned?` | `note` (hashtags re-derived from a new body; manual tags kept) |
| `POST /v1/reminders` | `body` (≤2k), `due_at` (ISO **with** offset) **or** `due_local` (`YYYY-MM-DDTHH:MM` in the user's zone), `repeat?`, `source?` | `reminder` with `due_local` |
| `POST /v1/reminders/search` | `q?`, `include_done?`, `limit?` ≤100 | `reminders[]` soonest first |
| `POST /v1/reminders/update` | `id`, `body?`, `due_at?`/`due_local?`, `repeat?` (`none` clears), `done?` | `reminder` |
| `POST /v1/reminders/claim` | `limit?` ≤50, `lease_seconds?` 30–3600 | `claim_id`, `reminders[]` with `late_minutes` |
| `POST /v1/reminders/ack` | `claim_id` | `acked`, updated `reminders[]` |
| `POST /v1/token/revoke` | – | revokes the presented token |

No delete endpoints and no way to run queries.

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
- **Recurring:** `daily`, `weekdays` (Mon–Fri), `weekly`, `monthly`, `yearly`.
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
late labels, reactivation rules; request files (shell metacharacters and heredoc delimiters stored
verbatim, bad names, links, oversized and non-JSON files, disallowed ops, file deleted after use);
outage alert-once and recover-once; refusal of http:// and loose
token permissions; and rotation plus revocation. **Never run it against a real account.**
