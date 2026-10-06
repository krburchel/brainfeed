#!/usr/bin/env python3
"""End-to-end tests for the BrainFeed agent API + Hermes helper.

Runs the real helper (as a subprocess) against the deployed API. Needs two
throwaway accounts, each with a registered token:

    BF_TEST_DIR_A   config dir (config.json + token) for test user A
    BF_TEST_DIR_B   config dir for test user B (isolation checks)
    BF_TEST_NOTE_B  id of a note that belongs to user B

Never point this at a real account: it creates notes and reminders.
"""
import json
import os
import subprocess
import sys
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(HERE, "..", "brainfeed", "scripts", "brainfeed.py")
DIR_A = os.environ["BF_TEST_DIR_A"]
DIR_B = os.environ["BF_TEST_DIR_B"]
NOTE_B = os.environ["BF_TEST_NOTE_B"]
with open(os.path.join(DIR_A, "config.json")) as _f:
    BASE = json.load(_f)["base_url"]


def run(*args, cfg=DIR_A, stdin=None, check=True):
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", ""), "BRAINFEED_CONFIG_DIR": cfg}
    p = subprocess.run([sys.executable, HELPER, *args], env=env, input=stdin, capture_output=True, text=True, timeout=60)
    if check and p.returncode != 0:
        raise AssertionError(f"helper {args} failed: {p.stderr}")
    return p


def j(*args, **kw):
    return json.loads(run(*args, "--json", **kw).stdout)


def raw(method, path, body=None, headers=None):
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(), method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read() or b"{}")


def token(cfg):
    with open(os.path.join(cfg, "token")) as f:
        return f.read().strip()


TOKEN_A = token(DIR_A)


def sql_time(minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


class Auth(unittest.TestCase):
    def test_missing_token(self):
        self.assertEqual(raw("GET", "/v1/status")[0], 401)

    def test_malformed_token(self):
        self.assertEqual(raw("GET", "/v1/status", headers={"X-BrainFeed-Token": "nope"})[0], 401)

    def test_unknown_token(self):
        self.assertEqual(raw("GET", "/v1/status", headers={"X-BrainFeed-Token": "ab" * 32})[0], 401)

    def test_authorization_header_refused(self):
        # Even a valid token is refused in Authorization (gateway logs its prefix).
        self.assertEqual(raw("GET", "/v1/status", headers={"Authorization": "Bearer " + "cd" * 32})[0], 400)

    def test_status(self):
        d = j("status")
        self.assertTrue(d["ok"])
        self.assertEqual(d["timezone"], "America/Los_Angeles")
        self.assertIn("***@", d["account"])

    def test_token_never_printed(self):
        tok = token(DIR_A)
        for args in (["status"], ["status", "--json"], ["search", "x"], ["fingerprint"]):
            p = run(*args)
            self.assertNotIn(tok, p.stdout + p.stderr)

    def test_error_output_has_no_token(self):
        p = run("get", "00000000-0000-0000-0000-000000000000", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn(token(DIR_A), p.stdout + p.stderr)

    def test_unknown_route(self):
        self.assertEqual(raw("POST", "/v1/sql", {"q": "select 1"}, {"X-BrainFeed-Token": token(DIR_A)})[0], 404)


class Isolation(unittest.TestCase):
    def test_cannot_pass_user_id(self):
        s, d = raw("POST", "/v1/notes", {"body": "x", "user_id": "00000000-0000-0000-0000-000000000000"},
                   {"X-BrainFeed-Token": token(DIR_A)})
        self.assertEqual(s, 400)
        self.assertIn("user_id", d["message"])

    def test_cannot_read_other_users_note(self):
        p = run("get", NOTE_B, check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("404", p.stderr)

    def test_cannot_edit_other_users_note(self):
        p = run("edit", NOTE_B, "--body", "hijacked", check=False)
        self.assertIn("404", p.stderr)
        self.assertNotEqual(j("get", NOTE_B, cfg=DIR_B)["note"]["body"], "hijacked")

    def test_search_only_own(self):
        own = j("search", "--limit", "50")["notes"]
        self.assertNotIn(NOTE_B, [n["id"] for n in own])


class Notes(unittest.TestCase):
    def test_add_search_edit(self):
        n = j("add", "Overlay idea for stream #Twitch", "--tag", "ideas", "--source", "telegram")["note"]
        self.assertEqual(sorted(n["tags"]), ["ideas", "twitch"])
        self.assertEqual(n["source"], "telegram")
        found = j("search", "overlay", "--tag", "twitch")["notes"]
        self.assertIn(n["id"], [x["id"] for x in found])
        e = j("edit", n["id"], "--body", "Overlay idea #youtube", "--pin")["note"]
        self.assertEqual(sorted(e["tags"]), ["ideas", "youtube"])  # manual tag kept, hashtag swapped
        self.assertTrue(e["pinned"])
        e = j("edit", n["id"], "--add-tag", "Later", "--remove-tag", "ideas")["note"]
        self.assertEqual(sorted(e["tags"]), ["later", "youtube"])

    def test_add_from_stdin(self):
        n = j("add", "-", stdin="multi\nline note #stdin")["note"]
        self.assertEqual(n["body"], "multi\nline note #stdin")

    def test_hashtag_search(self):
        j("add", "hashtag search target #zebra")
        self.assertTrue(j("search", "#zebra")["notes"])

    def test_validation(self):
        self.assertIn("400", run("add", "   ", check=False).stderr)
        self.assertIn("400", run("add", "x", "--tag", "bad tag!", check=False).stderr)


class Reminders(unittest.TestCase):
    def test_local_time_and_dst(self):
        # 9:00 local on a PST date must be 17:00Z; on a PDT date 16:00Z.
        r = j("remind", "dst check winter", "--at", "2027-01-15T09:00")["reminder"]
        self.assertTrue(r["due_at"].startswith("2027-01-15T17:00:00"))
        r = j("remind", "dst check summer", "--at", "2027-07-15T09:00")["reminder"]
        self.assertTrue(r["due_at"].startswith("2027-07-15T16:00:00"))

    def test_offset_required_for_utc(self):
        self.assertIn("400", run("remind", "x", "--at-utc", "2027-01-01T09:00", check=False).stderr)

    def test_claim_is_exclusive_and_ack_completes(self):
        r = j("remind", "one-shot due now", "--at-utc", sql_time(-1))["reminder"]
        out = run("deliver").stdout
        self.assertIn("⏰ one-shot due now", out)
        self.assertNotIn("one-shot due now", run("deliver").stdout)  # never twice
        mine = [x for x in j("reminders", "--all")["reminders"] if x["id"] == r["id"]][0]
        self.assertTrue(mine["done"])
        self.assertIsNotNone(mine["last_sent_at"])

    def test_unacked_claim_is_retried_after_lease(self):
        r = j("remind", "lease test", "--at-utc", sql_time(-1))["reminder"]
        tok = token(DIR_A)
        s, c = raw("POST", "/v1/reminders/claim", {"lease_seconds": 30}, {"X-BrainFeed-Token": tok})
        self.assertIn(r["id"], [x["id"] for x in c["reminders"]])
        s, c2 = raw("POST", "/v1/reminders/claim", {}, {"X-BrainFeed-Token": tok})
        self.assertNotIn(r["id"], [x["id"] for x in c2["reminders"]])  # leased
        import time; time.sleep(32)
        s, c3 = raw("POST", "/v1/reminders/claim", {}, {"X-BrainFeed-Token": tok})
        self.assertIn(r["id"], [x["id"] for x in c3["reminders"]])  # lease expired -> retried
        raw("POST", "/v1/reminders/ack", {"claim_id": c3["claim_id"]}, {"X-BrainFeed-Token": tok})

    def test_repeating_advances_and_stays_open(self):
        r = j("remind", "weekly thing", "--at-utc", sql_time(-3), "--repeat", "weekly")["reminder"]
        self.assertIn("repeats weekly", run("deliver").stdout)
        mine = [x for x in j("reminders")["reminders"] if x["id"] == r["id"]][0]
        self.assertFalse(mine["done"])
        due = datetime.fromisoformat(mine["due_at"].replace("Z", "+00:00"))
        self.assertGreater(due, datetime.now(timezone.utc) + timedelta(days=6))

    def test_late_delivery_is_labelled(self):
        j("remind", "late one", "--at-utc", sql_time(-90))
        self.assertIn("late one (was due", run("deliver").stdout)

    def test_editing_delivered_reminder_reactivates(self):
        r = j("remind", "reactivate me", "--at-utc", sql_time(-1))["reminder"]
        run("deliver")
        e = j("edit-reminder", r["id"], "--at-utc", sql_time(60))["reminder"]
        self.assertFalse(e["done"])
        e = j("edit-reminder", r["id"], "--body", "text only edit")["reminder"]
        self.assertFalse(e["done"])

    def test_text_edit_does_not_reactivate(self):
        r = j("remind", "stay done", "--at-utc", sql_time(-1))["reminder"]
        run("deliver")
        e = j("edit-reminder", r["id"], "--body", "still done")["reminder"]
        self.assertTrue(e["done"])

    def test_nothing_due_is_silent(self):
        run("deliver")
        self.assertEqual(run("deliver").stdout, "")


class CallRequests(unittest.TestCase):
    """The agent path: JSON request files, never user text in a shell command."""

    def write(self, name, obj, cfg=DIR_A, raw_text=None):
        inbox = os.path.join(cfg, "inbox")
        os.makedirs(inbox, mode=0o700, exist_ok=True)
        path = os.path.join(inbox, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw_text if raw_text is not None else json.dumps(obj))
        return path

    def call(self, name, check=True):
        p = run("call", name, check=check)
        return p if not check else json.loads(p.stdout)

    def test_hostile_text_is_stored_verbatim(self):
        canary = os.path.join(DIR_A, "pwned")
        body = ("line one\nBRAINFEED_EOF\ntouch " + canary + "\n$(touch " + canary + ") `touch " + canary + "`"
                " ; touch " + canary + " && echo \"quoted\" 'single' \\ #tag")
        path = self.write("hostile.json", {"op": "add_note", "body": body, "source": "telegram"})
        note = self.call("hostile.json")["note"]
        self.assertEqual(note["body"], body.strip())
        self.assertFalse(os.path.exists(canary))
        self.assertFalse(os.path.exists(path))  # consumed

    def test_all_ops(self):
        self.write("a.json", {"op": "add_note", "body": "call path note #callpath"})
        nid = self.call("a.json")["note"]["id"]
        self.write("b.json", {"op": "search_notes", "q": "#callpath"})
        self.assertIn(nid, [n["id"] for n in self.call("b.json")["notes"]])
        self.write("c.json", {"op": "get_note", "id": nid})
        self.assertEqual(self.call("c.json")["note"]["id"], nid)
        self.write("d.json", {"op": "edit_note", "id": nid, "add_tags": ["edited"], "pinned": True})
        self.assertIn("edited", self.call("d.json")["note"]["tags"])
        self.write("e.json", {"op": "add_reminder", "body": "call path reminder", "due_local": "2027-03-01T09:00"})
        rid = self.call("e.json")["reminder"]["id"]
        self.write("f.json", {"op": "list_reminders", "q": "call path"})
        self.assertIn(rid, [r["id"] for r in self.call("f.json")["reminders"]])
        self.write("g.json", {"op": "edit_reminder", "id": rid, "repeat": "weekly"})
        self.assertEqual(self.call("g.json")["reminder"]["repeat"], "weekly")

    def test_rejects_bad_names(self):
        for name in ["../config.json", "a/b.json", "x.txt", "token", ".json", "a b.json", "$(id).json", "-rf.json"]:
            p = run("call", name, check=False)
            self.assertNotEqual(p.returncode, 0, name)
        self.assertTrue(os.path.exists(os.path.join(DIR_A, "config.json")))

    def test_rejects_links(self):
        target = os.path.join(DIR_A, "config.json")
        link = os.path.join(DIR_A, "inbox", "link.json")
        os.makedirs(os.path.dirname(link), exist_ok=True)
        os.symlink(target, link)
        p = self.call("link.json", check=False)
        self.assertIn("regular file", p.stderr)
        self.assertFalse(os.path.lexists(link))      # link removed
        self.assertTrue(os.path.exists(target))       # target untouched

    def test_rejects_disallowed_ops_and_fields(self):
        for op in ["claim", "ack", "revoke", "deliver", "token_revoke", None]:
            self.write("op.json", {"op": op})
            self.assertIn("Unknown op", self.call("op.json", check=False).stderr)
        self.write("uid.json", {"op": "add_note", "body": "x", "user_id": "00000000-0000-0000-0000-000000000000"})
        self.assertIn("400", self.call("uid.json", check=False).stderr)

    def test_rejects_bad_json_and_large_files(self):
        path = self.write("bad.json", None, raw_text="{not json")
        self.assertIn("not valid JSON", self.call("bad.json", check=False).stderr)
        self.assertFalse(os.path.exists(path))
        self.write("big.json", None, raw_text='{"op":"add_note","body":"' + "x" * 70000 + '"}')
        self.assertIn("too large", self.call("big.json", check=False).stderr)
        self.write("arr.json", [1, 2])
        self.assertIn("JSON object", self.call("arr.json", check=False).stderr)

    def test_missing_file(self):
        self.assertIn("No request file", self.call("nope.json", check=False).stderr)

    def test_status_reports_inbox(self):
        self.assertEqual(j("status")["inbox"], os.path.join(DIR_A, "inbox"))


def tiny_png():
    import struct, zlib
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    raw = b"\x00\xff\x00\x00"  # one red pixel
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def post_bytes(path, data, tok):
    req = urllib.request.Request(BASE + path, data=data, method="POST")
    req.add_header("X-BrainFeed-Token", tok)
    req.add_header("Content-Type", "application/octet-stream")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read() or b"{}")


class Photos(unittest.TestCase):
    def put(self, name, data, cfg=DIR_A):
        inbox = os.path.join(cfg, "inbox")
        os.makedirs(inbox, mode=0o700, exist_ok=True)
        path = os.path.join(inbox, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def req(self, name, obj):
        with open(self.put(name, b""), "w") as f:
            json.dump(obj, f)

    def call(self, name, check=True):
        p = run("call", name, check=check)
        return json.loads(p.stdout) if p.stdout.strip() else {}, p

    def test_add_note_with_photo_and_caption(self):
        photo = self.put("photo-1.png", tiny_png())
        self.req("p1.json", {"op": "add_note", "body": "Shiny Ralts! #pokemon", "photos": ["photo-1.png"], "source": "telegram"})
        out, _ = self.call("p1.json")
        note = out["note"]
        self.assertEqual(note["body"], "Shiny Ralts! #pokemon")
        self.assertEqual(len(note["attachments"]), 1)
        self.assertEqual(note["attachments"][0]["type"], "image/png")
        self.assertTrue(note["attachments"][0]["path"].endswith("photo-1.png"))
        self.assertIn(note["id"], note["attachments"][0]["path"])
        self.assertFalse(os.path.exists(photo))  # copy deleted after upload

    def test_photo_without_caption_and_attach_more(self):
        self.put("a.png", tiny_png())
        self.req("p2.json", {"op": "add_note", "photos": ["a.png"]})
        note = self.call("p2.json")[0]["note"]
        self.assertEqual(note["body"], "📷 Photo")
        self.put("b.png", tiny_png()); self.put("c.png", tiny_png())
        self.req("p3.json", {"op": "attach_photos", "id": note["id"], "photos": ["b.png", "c.png"]})
        self.assertEqual(len(self.call("p3.json")[0]["note"]["attachments"]), 3)

    def test_fake_image_rejected_before_note_is_created(self):
        before = j("status")["notes"]
        fake = self.put("evil.jpg", b"#!/bin/sh\necho not an image\n")
        self.req("p4.json", {"op": "add_note", "body": "should not exist", "photos": ["evil.jpg"]})
        _, p = self.call("p4.json", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("not a JPEG", p.stderr)
        self.assertEqual(j("status")["notes"], before)
        self.assertFalse(os.path.exists(fake))

    def test_bad_photo_names_links_and_size(self):
        for bad in ["../config.json", "x.txt", "a b.png", "token"]:
            self.req("p5.json", {"op": "add_note", "body": "x", "photos": [bad]})
            self.assertNotEqual(self.call("p5.json", check=False)[1].returncode, 0, bad)
        link = os.path.join(DIR_A, "inbox", "link.png")
        os.symlink(os.path.join(DIR_A, "config.json"), link)
        self.req("p6.json", {"op": "add_note", "body": "x", "photos": ["link.png"]})
        self.assertIn("regular file", self.call("p6.json", check=False)[1].stderr)
        self.assertTrue(os.path.exists(os.path.join(DIR_A, "config.json")))
        self.put("big.jpg", b"\xff\xd8\xff" + b"0" * (10 * 1024 * 1024))
        self.req("p7.json", {"op": "add_note", "body": "x", "photos": ["big.jpg"]})
        self.assertIn("MB", self.call("p7.json", check=False)[1].stderr)

    def test_cannot_attach_to_other_users_note(self):
        self.put("x.png", tiny_png())
        self.req("p8.json", {"op": "attach_photos", "id": NOTE_B, "photos": ["x.png"]})
        out, p = self.call("p8.json", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("404", " ".join(out.get("photo_errors", [])))

    def test_shortcut_style_raw_upload_and_text(self):
        s, d = post_bytes("/v1/photos?source=ios", tiny_png(), TOKEN_A)
        self.assertEqual(s, 200)
        self.assertEqual((d["note"]["body"], d["note"]["source"]), ("📷 Photo", "ios"))
        self.assertIn("Photo saved", d["message"])
        s, d = raw("POST", "/v1/notes", {"body": "https://www.instagram.com/p/abc123/", "source": "ios"}, {"X-BrainFeed-Token": TOKEN_A})
        self.assertEqual((s, d["note"]["source"]), (200, "ios"))
        self.assertIn("Saved to BrainFeed", d["message"])

    def test_server_rejects_non_images_params_and_limits(self):
        self.assertEqual(post_bytes("/v1/photos", b"<html>not an image</html>", TOKEN_A)[0], 415)
        self.assertEqual(post_bytes("/v1/photos?caption=hi", tiny_png(), TOKEN_A)[0], 400)
        self.assertEqual(post_bytes("/v1/photos", b"", TOKEN_A)[0], 400)
        self.assertEqual(post_bytes("/v1/photos", tiny_png(), "ab" * 32)[0], 401)
        s, d = post_bytes("/v1/photos", tiny_png(), TOKEN_A)
        nid = d["note"]["id"]
        for _ in range(9):
            self.assertEqual(post_bytes(f"/v1/photos?note_id={nid}", tiny_png(), TOKEN_A)[0], 200)
        s, d = post_bytes(f"/v1/photos?note_id={nid}", tiny_png(), TOKEN_A)
        self.assertEqual(s, 400)
        self.assertIn("at most 10", d["message"])


class CalendarAndNotes13(unittest.TestCase):
    """Skill 1.3: calendar, append_note, archive via edit_note, specific dates."""

    def call(self, obj, name="c13.json", check=True):
        inbox = os.path.join(DIR_A, "inbox")
        os.makedirs(inbox, mode=0o700, exist_ok=True)
        with open(os.path.join(inbox, name), "w") as f:
            json.dump(obj, f)
        p = run("call", name, check=check)
        return json.loads(p.stdout) if p.returncode == 0 else p

    def local_day(self, offset_days=0):
        # Today's date in BrainFeed's time zone, from the API itself
        cal = self.call({"op": "calendar", "days": 1})
        today = datetime.strptime(cal["from_local"], "%Y-%m-%d")
        return (today + timedelta(days=offset_days)).strftime("%Y-%m-%d")

    def test_append_note(self):
        n = j("add", "Shiny hunt log #pokemon")["note"]
        out = self.call({"op": "append_note", "id": n["id"], "body": "1,377 eggs, SHINY"})
        body = out["note"]["body"]
        self.assertRegex(body, r"^Shiny hunt log #pokemon\n\n— [A-Z][a-z]{2} \d{1,2}, \d{1,2}:\d{2} [AP]M\n1,377 eggs, SHINY$")
        self.assertIn("Added", out["message"])
        self.assertIn("404", self.call({"op": "append_note", "id": NOTE_B, "body": "x"}, check=False).stderr)

    def test_archive_via_edit(self):
        n = j("add", "archive me")["note"]
        a = self.call({"op": "edit_note", "id": n["id"], "archived": True})["note"]
        self.assertIsNotNone(a["archived_at"])
        b = self.call({"op": "edit_note", "id": n["id"], "archived": False})["note"]
        self.assertIsNone(b["archived_at"])

    def test_specific_dates_reminder(self):
        d1, d2, d3 = self.local_day(3), self.local_day(10), self.local_day(20)
        name = "recycling " + os.urandom(4).hex()
        r = self.call({"op": "add_reminder", "body": name, "dates_local": [f"{d2}T18:30", f"{d1}T18:30", f"{d3}T18:30"]})["reminder"]
        self.assertEqual(r["repeat"], "dates")
        self.assertEqual(len(r["dates"]), 3)
        self.assertRegex(r["due_local"], r"6:30\sPM$")
        cal = self.call({"op": "calendar", "from_local": self.local_day(0), "days": 21})
        hits = [d["date"] for d in cal["days"] for x in d["reminders"] if x["body"] == name]
        self.assertEqual(hits, [d1, d2, d3])

    def test_specific_dates_validation(self):
        past = self.local_day(-3)
        for bad in ({"dates_local": [f"{past}T09:00"]},
                    {"dates_local": [f"{self.local_day(2)}T09:00"], "due_local": f"{self.local_day(2)}T09:00"},
                    {"repeat": "dates", "due_local": f"{self.local_day(2)}T09:00"},
                    {"dates_local": []}):
            p = self.call({"op": "add_reminder", "body": "x", **bad}, check=False)
            self.assertIn("400", p.stderr, bad)

    def test_calendar_projects_repeats_and_notes(self):
        tomorrow = self.local_day(1)
        ws = "weekly sync " + os.urandom(4).hex()
        self.call({"op": "add_reminder", "body": ws, "due_local": f"{tomorrow}T09:00", "repeat": "weekly"})
        j("add", "calendar note today")
        cal = self.call({"op": "calendar", "days": 15})
        self.assertEqual(len(cal["days"]), 15)
        by = {d["date"]: d for d in cal["days"]}
        self.assertEqual([x["status"] for x in by[tomorrow]["reminders"] if x["body"] == ws], ["scheduled"])
        self.assertEqual([x["status"] for x in by[self.local_day(8)]["reminders"] if x["body"] == ws], ["repeat"])
        self.assertEqual([x["time_local"] for x in by[self.local_day(8)]["reminders"] if x["body"] == ws], ["9:00 AM"])
        self.assertIn("calendar note today", [n["first_line"] for n in by[cal["from_local"]]["notes"]])

    def test_calendar_dst(self):
        # 9:00 AM weekly from Oct 30 2026 (PDT) stays 9:00 AM on Nov 6 (PST).
        dw = "dst weekly " + os.urandom(4).hex()
        self.call({"op": "add_reminder", "body": dw, "due_local": "2026-10-30T09:00", "repeat": "weekly"})
        cal = self.call({"op": "calendar", "from_local": "2026-11-01", "days": 14})
        times = {d["date"]: x["time_local"] for d in cal["days"] for x in d["reminders"] if x["body"] == dw}
        self.assertEqual(times, {"2026-11-06": "9:00 AM", "2026-11-13": "9:00 AM"})

    def test_calendar_validation(self):
        self.assertIn("400", self.call({"op": "calendar", "days": 40}, check=False).stderr)
        self.assertIn("400", self.call({"op": "calendar", "from_local": "next week"}, check=False).stderr)


class Review131(unittest.TestCase):
    """Fixes from Hermes's 1.3 review."""

    def call(self, obj, name="r131.json", check=True):
        inbox = os.path.join(DIR_A, "inbox")
        os.makedirs(inbox, mode=0o700, exist_ok=True)
        with open(os.path.join(inbox, name), "w") as f:
            json.dump(obj, f)
        p = run("call", name, check=check)
        return json.loads(p.stdout) if p.returncode == 0 else p

    def day(self, n):
        today = datetime.strptime(self.call({"op": "calendar", "days": 1})["from_local"], "%Y-%m-%d")
        return (today + timedelta(days=n)).strftime("%Y-%m-%d")

    def test_dates_reminder_cannot_be_split(self):
        r = self.call({"op": "add_reminder", "body": "split " + os.urandom(3).hex(),
                       "dates_local": [f"{self.day(3)}T18:30", f"{self.day(9)}T18:30"]})["reminder"]
        p = self.call({"op": "edit_reminder", "id": r["id"], "due_local": f"{self.day(5)}T10:00"}, check=False)
        self.assertIn("400", p.stderr)
        self.assertIn("dates_local", p.stderr)
        e = self.call({"op": "edit_reminder", "id": r["id"], "dates_local": [f"{self.day(4)}T08:00", f"{self.day(6)}T08:00", f"{self.day(8)}T08:00"]})["reminder"]
        self.assertEqual(len(e["dates"]), 3)
        self.assertEqual(e["due_at"], e["dates"][0])
        o = self.call({"op": "edit_reminder", "id": r["id"], "repeat": "none"})["reminder"]
        self.assertIsNone(o["repeat"]); self.assertIsNone(o["dates"])

    def test_concurrent_appends_are_all_kept(self):
        import concurrent.futures
        n = j("add", "concurrency log " + os.urandom(3).hex())["note"]
        def one(i):
            inbox = os.path.join(DIR_A, "inbox")
            with open(os.path.join(inbox, f"ap{i}.json"), "w") as f:
                json.dump({"op": "append_note", "id": n["id"], "body": f"entry-{i}"}, f)
            return run("call", f"ap{i}.json").returncode
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            self.assertEqual(list(ex.map(one, range(8))), [0] * 8)
        body = j("get", n["id"])["note"]["body"]
        self.assertEqual(sorted(x for x in body.split() if x.startswith("entry-")), sorted(f"entry-{i}" for i in range(8)))

    def test_calendar_lists_overdue_before_range(self):
        name = "old overdue " + os.urandom(3).hex()
        self.call({"op": "add_reminder", "body": name, "due_local": f"{self.day(-3)}T09:00"})
        cal = self.call({"op": "calendar", "days": 2})
        self.assertIn(name, [x["body"] for x in cal["overdue_before"]])
        run("deliver")  # tidy: deliver it so it doesn't linger as overdue


class Skill14(unittest.TestCase):
    """Skill 1.4: list_tags, check_items, notes inside notes, archived filter."""

    def call(self, obj, name="s14.json", check=True):
        inbox = os.path.join(DIR_A, "inbox")
        os.makedirs(inbox, mode=0o700, exist_ok=True)
        with open(os.path.join(inbox, name), "w") as f:
            json.dump(obj, f)
        p = run("call", name, check=check)
        return json.loads(p.stdout) if p.returncode == 0 else p

    def test_list_tags_counts_and_skips_archived(self):
        t = "tg" + os.urandom(3).hex()
        j("add", f"one #{t}"); j("add", f"two #{t}")
        gone = j("add", f"three #{t}")["note"]
        self.call({"op": "edit_note", "id": gone["id"], "archived": True})
        tags = {x["tag"]: x["count"] for x in self.call({"op": "list_tags"})["tags"]}
        self.assertEqual(tags[t], 2)
        self.assertIn("400", self.call({"op": "list_tags", "user_id": "x"}, check=False).stderr)

    def test_check_items(self):
        n = j("add", "# Groceries\n- [ ] Milk\n- [ ] eggs\n- [x] Bread\n- [ ] oat milk\nnotes after")["note"]
        out = self.call({"op": "check_items", "id": n["id"], "check": ["eggs"], "uncheck": [3], "add": ["Coffee"]})
        self.assertEqual([(x["text"], x["done"]) for x in out["checklist"]],
                         [("Milk", False), ("eggs", True), ("Bread", False), ("oat milk", False), ("Coffee", False)])
        self.assertTrue(out["note"]["body"].endswith("- [ ] Coffee\nnotes after"))
        self.assertEqual(out["message"], "1/5 done")
        # "milk" is exactly item 1 (exact match beats "oat milk"); "mil" is ambiguous
        self.assertEqual(self.call({"op": "check_items", "id": n["id"], "check": ["milk"]})["checked"], ["Milk"])
        self.assertIn("ambiguous", self.call({"op": "check_items", "id": n["id"], "check": ["mil"]}, check=False).stderr)
        self.assertIn("no_match", self.call({"op": "check_items", "id": n["id"], "check": ["kale"]}, check=False).stderr)
        self.assertIn("no item 9", self.call({"op": "check_items", "id": n["id"], "check": [9]}, check=False).stderr)
        got = self.call({"op": "get_note", "id": n["id"]})
        self.assertEqual([x["n"] for x in got["checklist"]], [1, 2, 3, 4, 5])
        self.assertIn("404", self.call({"op": "check_items", "id": NOTE_B, "add": ["x"]}, check=False).stderr)

    def test_check_items_starts_a_list(self):
        n = j("add", "Packing for the trip")["note"]
        out = self.call({"op": "check_items", "id": n["id"], "add": ["charger", "socks"]})
        self.assertEqual(out["note"]["body"], "Packing for the trip\n\n- [ ] charger\n- [ ] socks")

    def test_concurrent_checks_all_land(self):
        import concurrent.futures
        n = j("add", "\n".join(f"- [ ] item{i}" for i in range(4)))["note"]
        def one(i):
            inbox = os.path.join(DIR_A, "inbox")
            with open(os.path.join(inbox, f"ck{i}.json"), "w") as f:
                json.dump({"op": "check_items", "id": n["id"], "check": [f"item{i}"]}, f)
            return run("call", f"ck{i}.json", check=False).returncode
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            self.assertEqual(list(ex.map(one, range(4))), [0] * 4)
        self.assertTrue(all(x["done"] for x in self.call({"op": "get_note", "id": n["id"]})["checklist"]))

    def test_notes_inside_notes(self):
        outer = j("add", "Shiny hunts " + os.urandom(3).hex())["note"]
        kid = self.call({"op": "add_note", "body": "Charmander: 1,204 eggs", "parent_id": outer["id"]})["note"]
        self.assertEqual(kid["parent_id"], outer["id"])
        loose = j("add", "Pikachu hunt")["note"]
        self.call({"op": "edit_note", "id": loose["id"], "parent_id": outer["id"]})
        got = self.call({"op": "get_note", "id": outer["id"]})
        self.assertEqual([c["id"] for c in got["children"]], [kid["id"], loose["id"]])
        self.assertEqual(self.call({"op": "get_note", "id": kid["id"]})["parent"]["id"], outer["id"])
        inside = self.call({"op": "search_notes", "parent_id": outer["id"]})["notes"]
        self.assertEqual({x["id"] for x in inside}, {kid["id"], loose["id"]})
        # one level only, and never into someone else's note (same answer, nothing leaked)
        self.assertIn("cannot hold other notes", self.call({"op": "add_note", "body": "x", "parent_id": kid["id"]}, check=False).stderr)
        other = j("add", "standalone")["note"]
        self.assertIn("cannot be moved inside", self.call({"op": "edit_note", "id": outer["id"], "parent_id": other["id"]}, check=False).stderr)
        self.assertIn("cannot hold other notes", self.call({"op": "add_note", "body": "x", "parent_id": NOTE_B}, check=False).stderr)
        out = self.call({"op": "edit_note", "id": loose["id"], "parent_id": None})["note"]
        self.assertIsNone(out["parent_id"])

    def test_search_archived_filter(self):
        w = "arch" + os.urandom(3).hex()
        keep = j("add", f"{w} keep")["note"]
        gone = j("add", f"{w} gone")["note"]
        self.call({"op": "edit_note", "id": gone["id"], "archived": True})
        ids = lambda **kw: {x["id"] for x in self.call({"op": "search_notes", "q": w, **kw})["notes"]}
        self.assertEqual(ids(), {keep["id"], gone["id"]})
        self.assertEqual(ids(archived=False), {keep["id"]})
        self.assertEqual(ids(archived=True), {gone["id"]})


class Outage(unittest.TestCase):
    """Delivery failures are silent at first, alert once, then recover once."""

    def test_alert_once_then_recover(self):
        import shutil, tempfile
        d = tempfile.mkdtemp()
        try:
            shutil.copy(os.path.join(DIR_A, "token"), d)
            os.chmod(os.path.join(d, "token"), 0o600)
            with open(os.path.join(d, "config.json"), "w") as f:
                json.dump({"base_url": "https://127.0.0.1:9/unreachable"}, f)
            outs = [run("deliver", cfg=d).stdout for _ in range(16)]
            self.assertEqual(outs[:14], [""] * 14)
            self.assertIn("paused", outs[14])
            self.assertEqual(outs[15], "")  # alerted only once
            with open(os.path.join(d, "config.json"), "w") as f:
                json.dump({"base_url": BASE}, f)
            self.assertIn("working again", run("deliver", cfg=d).stdout)
            self.assertNotIn("working again", run("deliver", cfg=d).stdout)
        finally:
            shutil.rmtree(d)

    def test_rejects_http_and_loose_permissions(self):
        import shutil, tempfile
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(d, "config.json"), "w") as f:
                json.dump({"base_url": BASE.replace("https://", "http://")}, f)
            shutil.copy(os.path.join(DIR_A, "token"), d)
            os.chmod(os.path.join(d, "token"), 0o600)
            self.assertIn("https", run("status", cfg=d, check=False).stderr)
            with open(os.path.join(d, "config.json"), "w") as f:
                json.dump({"base_url": BASE}, f)
            os.chmod(os.path.join(d, "token"), 0o644)
            self.assertIn("chmod 600", run("status", cfg=d, check=False).stderr)
        finally:
            shutil.rmtree(d)


class ZTokenLifecycle(unittest.TestCase):
    """Runs last: rotates user B's token, then revokes it."""

    def test_rotate_then_revoke(self):
        old = token(DIR_B)
        # An unregistered next token must be refused, keeping the old one.
        scratch = run("init-token", "--next", "--force", cfg=DIR_B).stdout.strip()
        self.assertRegex(scratch, r"^sha256:[0-9a-f]{64}$")
        self.assertNotEqual(run("rotate-token", cfg=DIR_B, check=False).returncode, 0)
        self.assertEqual(token(DIR_B), old)
        # BF_TEST_NEXT_TOKEN_FILE: a token already registered for user B.
        pre = os.environ.get("BF_TEST_NEXT_TOKEN_FILE")
        if not pre:
            self.skipTest("BF_TEST_NEXT_TOKEN_FILE not set")
        import shutil
        shutil.copy(pre, os.path.join(DIR_B, "token.next"))
        os.chmod(os.path.join(DIR_B, "token.next"), 0o600)
        self.assertIn("Rotated", run("rotate-token", cfg=DIR_B).stdout)
        s, _ = raw("GET", "/v1/status", headers={"X-BrainFeed-Token": old})
        self.assertEqual(s, 401)  # old token revoked
        self.assertTrue(j("status", cfg=DIR_B)["ok"])
        run("revoke-token", cfg=DIR_B)
        self.assertNotEqual(run("status", cfg=DIR_B, check=False).returncode, 0)
        out = run("deliver", cfg=DIR_B).stdout
        self.assertIn("token was rejected", out)
        self.assertEqual(run("deliver", cfg=DIR_B).stdout, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
