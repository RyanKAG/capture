"""Tests for the email-triggered flow:
"status" email -> watcher -> server -> waiting client captures -> /send -> email to MAIL_TO.

Run:  python3 -m unittest -v
"""
import json
import threading
import time
import unittest
import urllib.request

from test_server import PNG, Base  # also puts client/ on sys.path
import client  # noqa: E402
import watcher


class FakeMailbox:
    """Stands in for ImapMailbox. `messages` is a shared list of (uid, from, subject)."""

    def __init__(self, messages):
        self.messages = messages

    def __call__(self, cfg):  # used as mailbox_factory
        return self

    def connect(self):
        pass

    def latest_uid(self):
        return max((m[0] for m in self.messages), default=0)

    def messages_after(self, uid):
        return sorted((m[0], m[1], m[2], f"<{m[0]}@mail.example>") for m in self.messages if m[0] > uid)

    def close(self):
        pass


def watch_cfg(**kw):
    cfg = watcher.load_watch_config({"SMTP_USER": "me@gmail.com", "SMTP_PASS": "x", "CHECK_INTERVAL": "0.05",
                                     "TRUSTED_SENDERS": "a@x.com, b@x.com, boss@x.com, me@x.com"})
    cfg.update(kw)
    return cfg


class WatcherLogicTest(unittest.TestCase):
    def run_watcher(self, cfg, old, new):
        fired = []
        msgs = list(old)
        w = watcher.Watcher(cfg, lambda sender, subject, msgid: fired.append(sender),
                            mailbox_factory=FakeMailbox(msgs))
        w.start()
        time.sleep(0.2)          # baseline taken; old messages must be ignored
        msgs.extend(new)
        time.sleep(0.3)
        w.stop()
        return fired

    def test_old_status_emails_do_not_fire(self):
        fired = self.run_watcher(watch_cfg(), old=[(1, "a@x.com", "status")], new=[])
        self.assertEqual(fired, [])

    def test_new_status_email_fires_once(self):
        fired = self.run_watcher(watch_cfg(), old=[(1, "a@x.com", "status")],
                                 new=[(2, "boss@x.com", "  Status ")])
        self.assertEqual(fired, ["boss@x.com"])

    def test_other_subjects_ignored(self):
        fired = self.run_watcher(watch_cfg(), old=[], new=[
            (5, "a@x.com", "hello"), (6, "a@x.com", "status report"), (7, "a@x.com", "Re: status")])
        self.assertEqual(fired, [])

    def test_each_status_email_fires(self):
        fired = self.run_watcher(watch_cfg(), old=[], new=[(3, "a@x.com", "status"), (4, "b@x.com", "STATUS")])
        self.assertEqual(fired, ["a@x.com", "b@x.com"])

    def test_untrusted_sender_ignored(self):
        fired = self.run_watcher(watch_cfg(trusted={"me@x.com"}), old=[], new=[
            (2, "stranger@x.com", "status"), (3, "me@x.com", "status"), (4, "", "status")])
        self.assertEqual(fired, ["me@x.com"])

    def test_no_trusted_senders_means_nothing_fires(self):
        fired = self.run_watcher(watch_cfg(trusted=set()), old=[], new=[(2, "me@x.com", "status")])
        self.assertEqual(fired, [])

    def test_custom_trigger_subject(self):
        fired = self.run_watcher(watch_cfg(subject="screenshot"), old=[], new=[
            (2, "a@x.com", "status"), (3, "a@x.com", "Screenshot")])
        self.assertEqual(fired, ["a@x.com"])


class TriggerFlowTest(Base):
    def setUp(self):
        super().setUp()
        self.api.wait_timeout = 0.5
        self.stop = threading.Event()
        self.captures = 0

    def tearDown(self):
        self.stop.set()
        time.sleep(0.6)  # let the client's open long-poll finish
        super().tearDown()

    def fake_capture(self, fmt="png"):
        self.captures += 1
        return PNG, "pc_screenshot_test.png"

    def start_client(self, name="pc1"):
        t = threading.Thread(target=client.listen, daemon=True, kwargs=dict(
            server=self.url, retries=3, capture=self.fake_capture, client_name=name,
            stop=self.stop.is_set))
        t.start()
        time.sleep(0.3)  # connected and long-polling
        return t

    def wait_for_emails(self, n, timeout=5):
        end = time.time() + timeout
        while time.time() < end and len(self.smtp.received) < n:
            time.sleep(0.05)
        return len(self.smtp.received)

    def get_wait(self, query):
        with urllib.request.urlopen(f"{self.url}/wait?{query}", timeout=5) as r:
            return json.loads(r.read())

    def test_no_capture_without_trigger(self):
        self.start_client()
        time.sleep(1.5)  # several long-poll timeouts pass
        self.assertEqual(self.captures, 0)
        self.assertEqual(self.smtp.received, [])

    def test_manual_trigger_captures_and_emails(self):
        self.start_client()
        urllib.request.urlopen(urllib.request.Request(
            self.url + "/trigger", data=b'{"to": "me@example.com"}', method="POST",
            headers={"Content-Type": "application/json"}), timeout=5)
        self.assertEqual(self.wait_for_emails(1), 1)
        self.assertEqual(self.emailed_attachment().get_content(), PNG)
        self.assertEqual(self.captures, 1)

    def test_status_email_end_to_end(self):
        """'status' from a trusted sender -> exactly one screenshot, emailed back to that sender only."""
        msgs = [(10, "me@x.com", "status")]
        w = watcher.Watcher(watch_cfg(), lambda s, subj, mid: self.api.trigger.fire(s, subj, mid),
                            mailbox_factory=FakeMailbox(msgs))
        w.start()
        self.start_client()
        time.sleep(0.2)
        self.assertEqual(self.smtp.received, [])   # the old one didn't fire
        msgs.append((11, "stranger@evil.com", "status"))  # untrusted: ignored
        msgs.append((12, "boss@x.com", "Status"))
        self.assertEqual(self.wait_for_emails(1), 1)
        time.sleep(0.5)
        w.stop()
        self.assertEqual(len(self.smtp.received), 1)
        self.assertEqual(self.smtp.received[0]["to"], ["<boss@x.com>"])
        self.assertEqual(self.emailed()["In-Reply-To"], "<12@mail.example>")
        self.assertEqual(self.emailed()["Subject"], "Re: Status")

    def test_two_clients_both_send(self):
        self.start_client("pc1")
        self.start_client("pc2")
        self.api.trigger.fire("me@example.com")
        self.assertEqual(self.wait_for_emails(2), 2)

    def test_trigger_while_client_busy_is_not_lost(self):
        """A trigger that fires between two long-polls is picked up on the next poll."""
        boot = self.get_wait("after=-1")["boot"]
        self.api.trigger.fire("me@example.com")       # nobody is waiting right now
        r = self.get_wait(f"after=0&boot={boot}")     # client comes back with its old seq
        self.assertTrue(r["capture"])
        self.assertEqual(r["requests"], [1])

    def test_server_restart_does_not_hang_client(self):
        r = self.get_wait("after=42&boot=oldboot")    # seq from a previous server run
        self.assertFalse(r["capture"])
        self.assertEqual(r["seq"], 0)

    def test_retry_on_smtp_failure_during_trigger(self):
        self.smtp.fail_next = 4  # first /send gets 502, client resends the same screenshot
        self.start_client()
        self.api.trigger.fire("me@example.com")
        self.assertEqual(self.wait_for_emails(1, timeout=15), 1)  # client waits 5s before resending
        self.assertEqual(self.captures, 1)


if __name__ == "__main__":
    unittest.main()
