from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.forward_verification import VerificationDetection
from app.mail_sync import sync_once
from app.storage import counts, initialize
from tests.test_storage import purchase_message


class FakeImap:
    def __init__(self, raw: bytes):
        self.raw = raw

    def login(self, _email, _password): return "OK", []
    def select(self, _folder, readonly=False): return "OK", [b"1"]
    def response(self, _name): return "UIDVALIDITY", [b"77"]
    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [b"9"]
        return "OK", [(b"BODY[]", self.raw)]
    def close(self): return "OK", []
    def logout(self): return "BYE", []
    def _simple_command(self, *_args): return "OK", []


class MailSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = Settings(
            username="owner",
            password="password",
            secret="s" * 32,
            api_key="k" * 24,
            cookie_secure=False,
            mail_mode="direct",
            imap_email="owner@example.com",
            imap_auth_code="auth-code",
            imap_host="imap.example.com",
            imap_port=993,
            imap_folder="INBOX",
            source_email="",
            auto_confirm_forwarding=True,
            sync_on_start=False,
            sync_interval_minutes=1440,
            data_dir=root,
        )
        initialize(self.settings.database)

    def tearDown(self):
        self.temp.cleanup()

    def test_direct_sync_stores_official_message_once(self):
        fake = FakeImap(purchase_message())
        with patch("app.mail_sync.imaplib.IMAP4_SSL", return_value=fake):
            first = sync_once(self.settings, attempts=1)
            second = sync_once(self.settings, attempts=1)
        self.assertTrue(first.ok)
        self.assertEqual(first.new_messages, 1)
        self.assertEqual(second.new_messages, 0)
        self.assertEqual(counts(self.settings.database)["tickets"], 1)
        self.assertEqual(len(list(self.settings.raw_dir.glob("*.eml"))), 1)

    def test_unsafe_verification_does_not_abort_forward_sync(self):
        forward = replace(
            self.settings,
            mail_mode="forward",
            source_email="owner@example.com",
        )
        detection = VerificationDetection(
            True,
            "https://confirm.example.com/action?token=secret",
            "action_selected",
            "确认邮件自动转发",
            "<verification@example.test>",
            "example.com",
        )
        with patch("app.mail_sync.imaplib.IMAP4_SSL", return_value=FakeImap(purchase_message())), patch(
            "app.mail_sync.detect_forward_verification", return_value=detection
        ), patch(
            "app.mail_sync.confirm_forward_verification", side_effect=ValueError("unsafe target")
        ):
            result = sync_once(forward, attempts=1)
        self.assertTrue(result.ok)
        self.assertEqual(result.verifications_failed, 1)
        self.assertEqual(result.new_messages, 0)


if __name__ == "__main__":
    unittest.main()
