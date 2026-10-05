#!/usr/bin/env python3
"""BrainFeed helper for Hermes Agent.

Talks to the BrainFeed agent API over HTTPS. Python 3.8+ standard library
only. Touches only the BrainFeed config directory (default ~/.config/brainfeed,
override with BRAINFEED_CONFIG_DIR):

    config.json   {"base_url": "https://.../functions/v1/brainfeed-api"}
    token         64 lowercase hex chars (mode 600). Never printed.
    state.json    reminder-delivery health (written by "deliver")
    inbox/        request files for "call" (read once, then deleted)

Agent use: "call NAME.json". The agent writes {"op": ..., ...} to
inbox/NAME.json with its file-writing tool, so user text never passes
through a shell. The shell command contains only fixed, safe tokens.

    call NAME.json   ops: add_note, search_notes, get_note, edit_note, append_note,
                     attach_photos, add_reminder, list_reminders, edit_reminder,
                     calendar
                     add_note / attach_photos take "photos": [image files that
                     are also in inbox/]; each is uploaded, then deleted.

Manual commands (add --json to any of them for machine-readable output):
    status                         check connection, token and account
    add TEXT|- [--tag T]... [--source S]
    search [QUERY] [--tag T]... [--pinned] [--limit N]
    get ID
    edit ID [--body TEXT|-] [--add-tag T]... [--remove-tag T]... [--pin|--unpin]
    remind TEXT --at "YYYY-MM-DDTHH:MM" [--repeat R]   (time in BrainFeed's zone)
    remind TEXT --at-utc ISO8601 [--repeat R]
    reminders [QUERY] [--all] [--limit N]
    edit-reminder ID [--body TEXT] [--at ..|--at-utc ..] [--repeat R|none] [--done|--undone]
    deliver                        cron mode: print due reminders, mark them sent
    init-token [--next]            create a token file, print only its fingerprint
    fingerprint [--next]           print sha256:<hex> of the token
    rotate-token                   switch to token.next and revoke the old token
    revoke-token                   revoke the current token on the server
"""
import argparse
import hashlib
import json
import os
import secrets
import stat
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

VERSION = "1.3.1"
CONFIG_DIR = os.path.expanduser(os.environ.get("BRAINFEED_CONFIG_DIR", "~/.config/brainfeed"))
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
TOKEN_FILE = os.path.join(CONFIG_DIR, "token")
NEXT_TOKEN_FILE = os.path.join(CONFIG_DIR, "token.next")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")
INBOX_DIR = os.path.join(CONFIG_DIR, "inbox")
INBOX_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
MAX_REQUEST_BYTES = 64000
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_PHOTOS = 10
PHOTO_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")
# op -> (method, path). Only these; delivery and token operations are not callable.
CALL_OPS = {
    "add_note": ("POST", "/v1/notes"),
    "search_notes": ("POST", "/v1/notes/search"),
    "get_note": ("POST", "/v1/notes/get"),
    "edit_note": ("POST", "/v1/notes/update"),
    "append_note": ("POST", "/v1/notes/append"),
    "calendar": ("POST", "/v1/calendar"),
    "attach_photos": None,  # handled locally: uploads inbox photos to a note
    "add_reminder": ("POST", "/v1/reminders"),
    "list_reminders": ("POST", "/v1/reminders/search"),
    "edit_reminder": ("POST", "/v1/reminders/update"),
}
REPEATS = ["daily", "weekdays", "weekly", "monthly", "yearly"]
ALERT_AFTER_FAILURES = 15  # ~30 min of failed deliveries at a 2-minute cadence


class HelperError(Exception):
    pass


# ------------------------------------------------------------ files
def load_config():
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        raise HelperError(f"Missing {CONFIG_FILE}. Run the BrainFeed setup script first.")
    except ValueError:
        raise HelperError(f"{CONFIG_FILE} is not valid JSON.")
    url = str(cfg.get("base_url", "")).rstrip("/")
    if not url.startswith("https://"):
        raise HelperError("base_url must use https://")
    return url


def read_token(path=TOKEN_FILE):
    try:
        st = os.stat(path)
    except FileNotFoundError:
        raise HelperError(f"Missing token file {path}. Run: brainfeed.py init-token")
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise HelperError(f"{path} must be readable only by its owner (chmod 600).")
    with open(path) as f:
        tok = f.read().strip()
    if len(tok) != 64 or any(c not in "0123456789abcdef" for c in tok):
        raise HelperError(f"{path} does not contain a valid token.")
    return tok


def write_private(path, content):
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.replace(path + ".tmp", path)
    os.chmod(path, 0o600)


def fingerprint_of(tok):
    return "sha256:" + hashlib.sha256(tok.encode("ascii")).hexdigest()


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state):
    write_private(STATE_FILE, json.dumps(state))


# ------------------------------------------------------------- http
def api(method, path, body=None, token=None, raw=None):
    url = load_config() + path
    tok = token or read_token()
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-BrainFeed-Token", tok)  # not Authorization: the gateway logs its prefix
    req.add_header("Content-Type", "application/octet-stream" if raw is not None else "application/json")
    req.add_header("User-Agent", "brainfeed-hermes/" + VERSION)
    try:
        with urllib.request.urlopen(req, timeout=60 if raw is not None else 20) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        # Report only status + the server's message; never request headers.
        try:
            msg = json.loads(e.read().decode()).get("message", "")
        except Exception:
            msg = ""
        err = HelperError(f"BrainFeed API error {e.code}: {msg or e.reason}")
        err.status = e.code
        raise err from None
    except urllib.error.URLError as e:
        err = HelperError(f"Could not reach BrainFeed: {getattr(e, 'reason', 'network error')}")
        err.status = 0
        raise err from None


# ---------------------------------------------------------- output
def text_arg(value):
    if value == "-":
        return sys.stdin.read()
    return value


def show_note(n):
    tags = " ".join("#" + t for t in n.get("tags", []))
    pin = " 📌" if n.get("pinned") else ""
    when = n.get("created_at", "")[:16].replace("T", " ")
    print(f"[{n['id']}] {when} UTC{pin}\n  {n['body']}\n  {tags}".rstrip())


def show_reminder(r):
    rep = f" · repeats {r['repeat']}" if r.get("repeat") else ""
    done = " · done" if r.get("done") else ""
    sent = f" · last sent {r['last_sent_local']}" if r.get("last_sent_local") else ""
    print(f"[{r['id']}] {r.get('due_local')}{rep}{done}{sent}\n  {r['body']}")


def emit(args, data, human):
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        human(data)


# --------------------------------------------------------- commands
def read_request(name):
    """Read and delete inbox/NAME.json. Only plain files directly in the inbox."""
    if not (name.endswith(".json") and 6 <= len(name) <= 69 and set(name[:-5]) <= INBOX_NAME_CHARS):
        raise HelperError("Request name must look like note-1.json (letters, digits, - and _ only).")
    path = os.path.join(INBOX_DIR, name)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise HelperError(f"No request file {path}")
    try:
        if not stat.S_ISREG(st.st_mode):
            raise HelperError("Request must be a regular file (not a link or directory).")
        if st.st_uid != os.getuid():
            raise HelperError("Request file must be owned by the current user.")
        if st.st_size > MAX_REQUEST_BYTES:
            raise HelperError("Request file is too large.")
        with open(path, encoding="utf-8") as f:
            req = json.load(f)
    except ValueError:
        raise HelperError("Request file is not valid JSON.")
    finally:
        if os.path.islink(path) or os.path.isfile(path):
            os.remove(path)
    if not isinstance(req, dict):
        raise HelperError("Request must be a JSON object.")
    op = req.pop("op", None)
    if op not in CALL_OPS:
        raise HelperError(f"Unknown op {op!r}. Allowed: {', '.join(sorted(CALL_OPS))}")
    return op, req


def inbox_file(name, exts, max_bytes):
    """Validate NAME as a plain, owned, size-capped file directly in the inbox."""
    if not (isinstance(name, str) and name.lower().endswith(exts)):
        raise HelperError(f"Photo names must end in {', '.join(exts)}: {name!r}")
    stem = name.rsplit(".", 1)[0]
    if not (1 <= len(stem) <= 64 and set(stem) <= INBOX_NAME_CHARS):
        raise HelperError(f"Photo name must use letters, digits, - and _ only: {name!r}")
    path = os.path.join(INBOX_DIR, name)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise HelperError(f"No photo file {path}")
    if not stat.S_ISREG(st.st_mode):
        raise HelperError(f"{name} must be a regular file (not a link or directory).")
    if st.st_uid != os.getuid():
        raise HelperError(f"{name} must be owned by the current user.")
    if st.st_size == 0 or st.st_size > max_bytes:
        raise HelperError(f"{name} must be between 1 byte and {max_bytes // (1024 * 1024)} MB.")
    return path


def is_image(head):
    return (head[:3] == b"\xff\xd8\xff" or head[:8] == b"\x89PNG\r\n\x1a\n"
            or head[:6] in (b"GIF87a", b"GIF89a") or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"))


def take_photos(names):
    """Validate every photo before anything is created; returns [(name, path)]."""
    if names is None:
        return []
    if not isinstance(names, list) or not 1 <= len(names) <= MAX_PHOTOS:
        raise HelperError(f"photos must be a list of 1-{MAX_PHOTOS} file names.")
    out = []
    for n in names:
        path = inbox_file(n, PHOTO_EXTS, MAX_PHOTO_BYTES)
        with open(path, "rb") as f:
            if not is_image(f.read(12)):
                raise HelperError(f"{n} is not a JPEG, PNG, GIF or WebP image.")
        out.append((n, path))
    return out


def upload_photos(note_id, photos, source):
    """Upload validated photos to a note; delete each file afterwards."""
    from urllib.parse import urlencode
    result, errors = None, []
    for name, path in photos:
        try:
            with open(path, "rb") as f:
                data = f.read(MAX_PHOTO_BYTES + 1)
            q = urlencode({"note_id": note_id, "source": source, "name": name.rsplit(".", 1)[0]})
            result = api("POST", "/v1/photos?" + q, raw=data)
        except HelperError as e:
            errors.append(f"{name}: {e}")
        finally:
            if os.path.islink(path) or os.path.isfile(path):
                os.remove(path)
    return result, errors


def discard(names):
    for n in names if isinstance(names, list) else []:
        if isinstance(n, str) and set(n.replace(".", "")) <= INBOX_NAME_CHARS and "/" not in n:
            p = os.path.join(INBOX_DIR, n)
            if os.path.islink(p) or os.path.isfile(p):
                os.remove(p)


def cmd_call(args):
    op, body = read_request(args.name)
    names = body.pop("photos", None) if op in ("add_note", "attach_photos") else None
    try:
        if op == "attach_photos" and names is None:
            raise HelperError("attach_photos needs a photos list.")
        photos = take_photos(names)
    except HelperError:
        discard(names)
        raise
    if op == "attach_photos":
        note_id = body.pop("id", None)
        if body:
            discard(names)
            raise HelperError("attach_photos takes only id and photos.")
        if not isinstance(note_id, str):
            discard(names)
            raise HelperError("attach_photos needs the note id.")
        result, errors = upload_photos(note_id, photos, "hermes")
    else:
        if op == "add_note" and photos and not str(body.get("body", "")).strip():
            body["body"] = "📷 Photo"
        method, path = CALL_OPS[op]
        try:
            result = api(method, path, body)
        except HelperError:
            discard(names)
            raise
        errors = []
        if photos:
            src = body.get("source", "hermes")
            uploaded, errors = upload_photos(result["note"]["id"], photos, src)
            if uploaded:
                result = uploaded
    if errors:
        result = dict(result or {}, photo_errors=errors)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if errors and not (result or {}).get("note"):
        return 1


def cmd_status(args):
    data = api("GET", "/v1/status")
    data["inbox"] = INBOX_DIR
    emit(args, data, lambda d: print(
        f"BrainFeed connected ✓\n  account: {d['account']}\n  token: {d['token']}\n"
        f"  timezone: {d['timezone']} (now {d['now_local']})\n"
        f"  notes: {d['notes']} · open reminders: {d['open_reminders']}\n  inbox: {d['inbox']}"))


def cmd_add(args):
    body = {"body": text_arg(args.text), "source": args.source}
    if args.tag:
        body["tags"] = args.tag
    data = api("POST", "/v1/notes", body)
    emit(args, data, lambda d: (print("Saved ✓"), show_note(d["note"])))


def cmd_search(args):
    body = {"limit": args.limit}
    if args.query:
        body["q"] = args.query
    if args.tag:
        body["tags"] = args.tag
    if args.pinned:
        body["pinned"] = True
    data = api("POST", "/v1/notes/search", body)

    def human(d):
        if not d["notes"]:
            print("No matching notes.")
        for n in d["notes"]:
            show_note(n)
    emit(args, data, human)


def cmd_get(args):
    data = api("POST", "/v1/notes/get", {"id": args.id})
    emit(args, data, lambda d: show_note(d["note"]))


def cmd_edit(args):
    body = {"id": args.id}
    if args.body is not None:
        body["body"] = text_arg(args.body)
    if args.add_tag:
        body["add_tags"] = args.add_tag
    if args.remove_tag:
        body["remove_tags"] = args.remove_tag
    if args.pin:
        body["pinned"] = True
    if args.unpin:
        body["pinned"] = False
    data = api("POST", "/v1/notes/update", body)
    emit(args, data, lambda d: (print("Updated ✓"), show_note(d["note"])))


def due_fields(args, required):
    if args.at and args.at_utc:
        raise HelperError("Use --at or --at-utc, not both.")
    if args.at:
        return {"due_local": args.at}
    if args.at_utc:
        return {"due_at": args.at_utc}
    if required:
        raise HelperError("A time is required: --at 'YYYY-MM-DDTHH:MM' (BrainFeed's time zone).")
    return {}


def cmd_remind(args):
    body = {"body": text_arg(args.text), "source": args.source, **due_fields(args, True)}
    if args.repeat:
        body["repeat"] = args.repeat
    data = api("POST", "/v1/reminders", body)
    emit(args, data, lambda d: (print(f"Reminder set ✓ ({d['timezone']})"), show_reminder(d["reminder"])))


def cmd_reminders(args):
    body = {"limit": args.limit, "include_done": args.all}
    if args.query:
        body["q"] = args.query
    data = api("POST", "/v1/reminders/search", body)

    def human(d):
        if not d["reminders"]:
            print("No reminders.")
        for r in d["reminders"]:
            show_reminder(r)
    emit(args, data, human)


def cmd_edit_reminder(args):
    body = {"id": args.id, **due_fields(args, False)}
    if args.body is not None:
        body["body"] = text_arg(args.body)
    if args.repeat:
        body["repeat"] = args.repeat
    if args.done:
        body["done"] = True
    if args.undone:
        body["done"] = False
    data = api("POST", "/v1/reminders/update", body)
    emit(args, data, lambda d: (print("Updated ✓"), show_reminder(d["reminder"])))


def cmd_deliver(args):
    """Cron mode. stdout becomes the Telegram/Discord message; empty = silent.

    Claims due reminders (5-minute lease), prints them, then acks the claim
    so they are never sent twice. Network/API failures stay silent until
    they persist, then alert once, and announce recovery once.
    """
    state = load_state()
    try:
        claim = api("POST", "/v1/reminders/claim", {"limit": 20, "lease_seconds": 300})
    except HelperError as e:
        fails = state.get("failures", 0) + 1
        state["failures"] = fails
        auth = getattr(e, "status", 0) == 401
        if (auth or fails >= ALERT_AFTER_FAILURES) and not state.get("alerted"):
            state["alerted"] = True
            print("⚠️ BrainFeed reminders are paused: "
                  + ("the token was rejected (revoked?)." if auth else f"{e}. Will keep retrying."))
        save_state(state)
        return 0

    lines = []
    if state.get("alerted"):
        lines.append("✅ BrainFeed reminders are working again.")
    if state.get("failures") or state.get("alerted"):
        save_state({})

    items = claim.get("reminders", [])
    for r in items:
        late = r.get("late_minutes", 0)
        note = f" (was due {r['due_local']})" if late >= 10 else ""
        if r.get("repeat") == "dates":
            rep = " · more dates scheduled" if r.get("has_more_dates") else " · last scheduled date"
        else:
            rep = f" · repeats {r['repeat']}" if r.get("repeat") else ""
        lines.append(f"⏰ {r['body']}{note}{rep}")

    if items:
        # Ack right away: stdout is handed to Hermes for delivery when we exit.
        try:
            api("POST", "/v1/reminders/ack", {"claim_id": claim["claim_id"]})
        except HelperError:
            # Not acked: the lease expires in 5 min and they will be retried.
            lines.append("(BrainFeed couldn't mark these as sent; you may see them again.)")

    if lines:
        print("\n".join(lines))
    return 0


def cmd_init_token(args):
    path = NEXT_TOKEN_FILE if args.next else TOKEN_FILE
    if os.path.exists(path) and not args.force:
        raise HelperError(f"{path} already exists. Use --force to replace it.")
    tok = secrets.token_hex(32)  # 256 bits
    write_private(path, tok)
    print(fingerprint_of(tok))


def cmd_fingerprint(args):
    print(fingerprint_of(read_token(NEXT_TOKEN_FILE if args.next else TOKEN_FILE)))


def cmd_rotate_token(args):
    new = read_token(NEXT_TOKEN_FILE)
    api("GET", "/v1/status", token=new)  # must already be registered in BrainFeed
    old = read_token(TOKEN_FILE)
    write_private(TOKEN_FILE, new)
    os.remove(NEXT_TOKEN_FILE)
    try:
        api("POST", "/v1/token/revoke", {}, token=old)
        print("Rotated ✓ New token active, old token revoked.")
    except HelperError as e:
        print(f"New token active. Revoking the old one failed ({e}); revoke it in BrainFeed → Connected agents.")


def cmd_revoke_token(args):
    api("POST", "/v1/token/revoke", {})
    print("Token revoked ✓ This helper can no longer access BrainFeed.")


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    p = argparse.ArgumentParser(prog="brainfeed.py", description="BrainFeed helper for Hermes")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)
    _add = sub.add_parser
    sub.add_parser = lambda *a, **k: _add(*a, parents=[common], **k)

    sub.add_parser("status").set_defaults(fn=cmd_status)

    s = sub.add_parser("call"); s.add_argument("name"); s.set_defaults(fn=cmd_call)

    s = sub.add_parser("add"); s.add_argument("text"); s.add_argument("--tag", action="append")
    s.add_argument("--source", default="hermes", choices=["hermes", "telegram", "discord", "sms", "ios"]); s.set_defaults(fn=cmd_add)

    s = sub.add_parser("search"); s.add_argument("query", nargs="?"); s.add_argument("--tag", action="append")
    s.add_argument("--pinned", action="store_true"); s.add_argument("--limit", type=int, default=20); s.set_defaults(fn=cmd_search)

    s = sub.add_parser("get"); s.add_argument("id"); s.set_defaults(fn=cmd_get)

    s = sub.add_parser("edit"); s.add_argument("id"); s.add_argument("--body")
    s.add_argument("--add-tag", action="append"); s.add_argument("--remove-tag", action="append")
    g = s.add_mutually_exclusive_group(); g.add_argument("--pin", action="store_true"); g.add_argument("--unpin", action="store_true")
    s.set_defaults(fn=cmd_edit)

    s = sub.add_parser("remind"); s.add_argument("text"); s.add_argument("--at"); s.add_argument("--at-utc")
    s.add_argument("--repeat", choices=REPEATS)
    s.add_argument("--source", default="hermes", choices=["hermes", "telegram", "discord", "sms"]); s.set_defaults(fn=cmd_remind)

    s = sub.add_parser("reminders"); s.add_argument("query", nargs="?"); s.add_argument("--all", action="store_true")
    s.add_argument("--limit", type=int, default=25); s.set_defaults(fn=cmd_reminders)

    s = sub.add_parser("edit-reminder"); s.add_argument("id"); s.add_argument("--body")
    s.add_argument("--at"); s.add_argument("--at-utc"); s.add_argument("--repeat", choices=REPEATS + ["none"])
    g = s.add_mutually_exclusive_group(); g.add_argument("--done", action="store_true"); g.add_argument("--undone", action="store_true")
    s.set_defaults(fn=cmd_edit_reminder)

    sub.add_parser("deliver").set_defaults(fn=cmd_deliver)

    s = sub.add_parser("init-token"); s.add_argument("--next", action="store_true"); s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init_token)
    s = sub.add_parser("fingerprint"); s.add_argument("--next", action="store_true"); s.set_defaults(fn=cmd_fingerprint)
    sub.add_parser("rotate-token").set_defaults(fn=cmd_rotate_token)
    sub.add_parser("revoke-token").set_defaults(fn=cmd_revoke_token)

    args = p.parse_args(argv)
    try:
        return args.fn(args) or 0
    except HelperError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
