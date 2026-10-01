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
